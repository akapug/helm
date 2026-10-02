"""A tip reviewed BEFORE the pin landed gets its pin from a backfill verb.

The verdict writer pins every tip it records (task/2383), but only from that
land on: a row reviewed earlier names a tip that only its lane branch keeps,
and one `git gc --prune` after a branch delete takes it. `helm lr
backfill-review-pins` walks the dispatch ledger and hands each such tip to the
same `dispatches.pin_reviewed_tips` the writer calls (task/3627). A dry run is
the default; `--apply` pins.

Every row here is written by THE WRITER BEFORE THE LAND: the pin is patched
out while the verdict or hold records, and each arm first asserts that the
repository holds no pin, so a pin read afterwards is the backfill's doing.
Every arm runs in a real temporary repository and prunes for real.
"""
import json
import os
import subprocess
import unittest
from unittest import mock

from helm import cli, dispatches, eventledger, landreq, pk
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_lr_close as _close
from tests.test_landreq import run as _lr_run

VERB = "backfill-review-pins"
LIVE = "refs/helm-reviewed/"
RETIRED = "refs/helm-retired/reviewed/"
READER = "seat-reader"          # the recipient of every row sent here


def setUpModule():
    """No dispatch row this module writes walks the host's process table."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


class PinBackfillBase(_close.CloseBase):

    def doomed_tip(self, branch):
        """One commit on a throwaway branch off trunk; trunk stays checked out."""
        self.git("checkout", "-q", "-b", branch, self.main)
        tip = self.commit("work only %s holds" % branch, path=branch + ".txt")
        self.git("checkout", "-q", self.main)
        return tip

    def resolves(self, sha):
        p = subprocess.run(["git", "--git-dir", self.gitdir(), "cat-file",
                            "-e", sha + "^{commit}"], capture_output=True)
        return p.returncode == 0

    def gc(self, *branches):
        for branch in branches:
            self.git("branch", "-D", branch)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now", "--quiet")

    def pins(self):
        """{ref: sha} for every review pin, live or retired, in the repo."""
        listed = self.git("for-each-ref", "--format=%(refname) %(objectname)",
                          LIVE, RETIRED)
        return dict(line.split(" ", 1) for line in listed.splitlines())

    def pin_events(self):
        try:
            with open(pk.events_path(), encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        return [r for r in rows if r.get("verb") == "dispatch-pin-failed"]

    def pre_land(self):
        """The writer before task/2383: the verdict or hold records, and
        nothing pins its tip."""
        return mock.patch.object(dispatches, "pin_reviewed_tips",
                                 return_value=None)

    def pre_land_verdict(self, branch, polarity="fix"):
        tip = self.doomed_tip(branch)
        with self.pre_land():
            row = self.verdict_row(polarity, ref=tip, lane="lane/" + branch)
        self.assertEqual(self.state(row["id"])["reviewed_tip"], tip)
        self.assertEqual(self.pins(), {}, "the pre-land writer pinned")
        return row, tip

    def state(self, rid):
        return dispatches.snapshot()[0][rid]

    def send(self, tip, lane):
        row, why, _sent = dispatches.send(
            READER, lane, "review " + lane, tip, repo=self.repo,
            key="key-" + lane, sign=False, new_work=True,
            task=self.review_task["id"])
        self.assertIsNone(why)
        return row

    def hold_clean(self, row, tip):
        """A source-clean hold by the row's own recipient (task/3053)."""
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(row["recipient"], None)):
            return dispatches.mark_hold(row["id"], "awaiting the land gate; fab Ran 5 tests OK",
                                        source_clean_tip=tip)

    def rebind(self, rid, repo_id, repo_root):
        """Rewrite the row's creating event so it names `repo_id` and
        `repo_root`: the ledger of a repository removed, or of a checkout
        path reused, after its rows were written."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == rid and "repo_id" in event:
                    event["repo_id"] = repo_id
                    event["repo_root"] = repo_root
                f.write(json.dumps(event, separators=(",", ":")) + "\n")

    def other_repository(self):
        """A clone of the fixture repository at another path: it holds every
        commit the fixture does, so a pin written into it would succeed and
        read back, and it is a DIFFERENT repository."""
        other = os.path.join(self.tmp, "other-repository")
        subprocess.run(["git", "clone", "-q", "--no-local", self.repo, other],
                       check=True, capture_output=True)
        return other

    def pins_in(self, where):
        listed = self.git("for-each-ref", "--format=%(refname) %(objectname)",
                          LIVE, RETIRED, cwd=where)
        return dict(line.split(" ", 1) for line in listed.splitlines())

    def backfill(self, *args):
        return _lr_run([VERB] + list(args))

    def report(self, *args, rc=0):
        got, out, err = self.backfill("--json", *args)
        self.assertEqual(got, rc, err)
        return json.loads(out)

    def repo_entry(self, report, key):
        hits = [r for r in report["repos"] if r["repo"] == key]
        self.assertEqual(len(hits), 1, "no single entry for %s in %s"
                         % (key, [r["repo"] for r in report["repos"]]))
        return hits[0]

    def outcomes(self, entry):
        return sorted((t["id"], t["role"], t["outcome"]) for t in entry["tips"])


class ReviewPinBackfillTest(PinBackfillBase):

    def test_a_pre_land_verdict_is_pinned_on_apply_and_not_on_a_dry_run(self):
        row, tip = self.pre_land_verdict("pre-land")
        key = self.state(row["id"])["repo_id"]
        rc, out, err = self.backfill()
        self.assertEqual(rc, 0, err)
        self.assertIn("dry run", out)
        self.assertIn(key, out)
        self.assertEqual(self.pins(), {}, "the dry run pinned")
        entry = self.repo_entry(self.report(), key)
        self.assertEqual(entry["counts"]["to-pin"], 1)
        self.assertEqual(self.outcomes(entry),
                         [(row["id"], "reviewed", "to-pin")])
        self.assertEqual(self.pins(), {})
        rc, out, err = self.backfill("--apply")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("dry run", out)
        self.assertEqual(self.pins(), {LIVE + row["id"]: tip})
        self.gc("pre-land")
        self.assertTrue(self.resolves(tip), "the backfilled pin let it go")

    def test_a_patch_tip_and_a_source_clean_tip_get_their_role_refs(self):
        tip = self.doomed_tip("cured")
        self.git("checkout", "-q", "cured")
        cure = self.commit("the reviewer's cure", path="cure.txt")
        self.git("checkout", "-q", self.main)
        clean = self.doomed_tip("held-clean")
        with self.pre_land():
            fixed = self.send(tip, "lane/cured")
            _out, err = self.mark_verdict(fixed["id"], tip, "findings",
                                          polarity="fix", patch_tip=cure)
            self.assertIsNone(err, err)
            held = self.send(clean, "lane/held-clean")
            _out, err = self.hold_clean(held, clean)
            self.assertIsNone(err, err)
        self.assertEqual(self.state(fixed["id"])["patch_tip"], cure)
        self.assertEqual(self.state(held["id"])["source_clean_tip"], clean)
        self.assertEqual(self.pins(), {}, "the pre-land writer pinned")
        report = self.report("--apply")
        entry = self.repo_entry(report, self.state(held["id"])["repo_id"])
        self.assertEqual(entry["counts"]["pinned"], 3)
        self.assertEqual(self.pins(), {
            LIVE + fixed["id"]: tip,
            LIVE + fixed["id"] + "-patch": cure,
            LIVE + held["id"] + "-source-clean": clean})
        self.gc("cured", "held-clean")
        self.assertTrue(self.resolves(tip) and self.resolves(cure)
                        and self.resolves(clean))

    def test_a_lost_tip_is_counted_and_the_pass_goes_on(self):
        lost, gone = self.pre_land_verdict("pruned")
        kept, tip = self.pre_land_verdict("kept")
        self.prune(gone, "refs/heads/pruned")
        report = self.report("--apply")
        entry = self.repo_entry(report, self.state(kept["id"])["repo_id"])
        self.assertEqual(self.outcomes(entry), sorted([
            (lost["id"], "reviewed", "lost"),
            (kept["id"], "reviewed", "pinned")]))
        self.assertEqual((entry["counts"]["lost"], entry["counts"]["pinned"],
                          entry["counts"]["failed"]), (1, 1, 0))
        self.assertEqual(self.pins(), {LIVE + kept["id"]: tip})
        self.assertEqual(self.pin_events(), [],
                         "a lost tip was handed to the pin and failed there")
        # A LOST TIP IS A FACT THE PASS REPORTS, NOT A PASS THAT FAILED: the
        # object was pruned before the pass and no re-run can pin it.
        self.assertEqual(report["status"], "COMPLETE")
        rc, out, err = self.backfill()
        self.assertEqual(rc, 0, err)
        self.assertIn("1 lost before this pass", out)

    def test_a_second_apply_pins_nothing_new(self):
        """And a pin the landed writer already put at the tip is left
        alone from the first pass on."""
        early, early_tip = self.pre_land_verdict("early")
        late_tip = self.doomed_tip("late")
        late = self.verdict_row("fix", ref=late_tip, lane="lane/late")
        self.assertEqual(self.pins(), {LIVE + late["id"]: late_tip})
        first = self.repo_entry(self.report("--apply"),
                                self.state(late["id"])["repo_id"])
        self.assertEqual(self.outcomes(first), sorted([
            (early["id"], "reviewed", "pinned"),
            (late["id"], "reviewed", "already")]))
        pinned = self.pins()
        self.assertEqual(pinned, {LIVE + early["id"]: early_tip,
                                  LIVE + late["id"]: late_tip})
        with mock.patch.object(dispatches, "_pin_write",
                               wraps=dispatches._pin_write) as write:
            second = self.repo_entry(self.report("--apply"),
                                     self.state(late["id"])["repo_id"])
        write.assert_not_called()
        self.assertEqual(self.outcomes(second), sorted([
            (early["id"], "reviewed", "already"),
            (late["id"], "reviewed", "already")]))
        self.assertEqual(self.pins(), pinned)

    def test_a_closed_rows_tip_goes_to_the_retired_namespace(self):
        """Where the close would have moved it, had the verdict pinned it:
        never a live pin on a row that no longer owes anything."""
        row, tip = self.pre_land_verdict("closed-first", polarity="concur")
        _out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.pins(), {}, "the close had nothing to move")
        entry = self.repo_entry(self.report("--apply"),
                                self.state(row["id"])["repo_id"])
        self.assertEqual(self.outcomes(entry),
                         [(row["id"], "reviewed", "pinned")])
        self.assertEqual([t["namespace"] for t in entry["tips"]], ["retired"])
        self.assertEqual(self.pins(), {RETIRED + row["id"]: tip})
        self.gc("closed-first")
        self.assertTrue(self.resolves(tip), "the retired pin let it go")
        with mock.patch.object(dispatches, "_pin_write",
                               wraps=dispatches._pin_write) as write:
            again = self.repo_entry(self.report("--apply"),
                                    self.state(row["id"])["repo_id"])
        write.assert_not_called()
        self.assertEqual(self.outcomes(again),
                         [(row["id"], "reviewed", "already")])
        self.assertEqual(self.pins(), {RETIRED + row["id"]: tip})

    def test_a_repository_that_no_longer_exists_is_unknown(self):
        self.unknown_repository("gone")

    def test_a_repository_git_cannot_read_is_unknown(self):
        self.unknown_repository("unreadable")

    def unknown_repository(self, how):
        row, tip = self.pre_land_verdict("elsewhere")
        where = os.path.join(self.tmp, "elsewhere-" + how)
        if how == "unreadable":
            os.makedirs(where)
            with open(os.path.join(where, ".git"), "w", encoding="utf-8") as f:
                f.write("not a gitfile\n")
        self.rebind(row["id"], os.path.join(where, ".git"), where)
        state = self.state(row["id"])
        self.assertEqual((state["repo_id"], state["reviewed_tip"]),
                         (os.path.join(where, ".git"), tip),
                         "the fixture did not move the row's repository")
        rc, out, err = self.backfill("--apply")
        self.assertEqual(rc, 1, err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("INCOMPLETE", out)
        report = self.report("--apply", rc=1)
        self.assertEqual(report["status"], "INCOMPLETE")
        entry = self.repo_entry(report, os.path.join(where, ".git"))
        self.assertFalse(entry["readable"])
        self.assertTrue(entry["why"])
        self.assertEqual(self.outcomes(entry),
                         [(row["id"], "reviewed", "unreadable")])
        self.assertEqual(entry["counts"]["pinned"], 0)
        self.assertEqual(self.pins(), {}, "a pin went into a guessed repo")

    def test_an_unknown_flag_is_refused_and_the_help_names_the_verb(self):
        rc, _out, err = self.backfill()
        self.assertEqual(rc, 0, err)
        for junk in ("--bogus", "extra"):
            rc, _out, err = self.backfill("--apply", junk)
            self.assertEqual(rc, 2, junk)
            self.assertIn("unknown arg '%s'" % junk, err)
        self.assertIn(VERB + " [--apply] [--json]", cli._VERB_HELP["lr"])
        self.assertIn(VERB + " [--apply] [--json]", landreq.USAGE)
        doc = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "VERBS.md")
        with open(doc, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("helm lr " + VERB, text)
        # THE DRY RUN WRITES NO REF AND NO RECEIPT, and says so no wider:
        # the strict ledger read may refresh helm's own fold checkpoint.
        for surface in (cli._VERB_HELP["lr"], text):
            self.assertIn("INCOMPLETE", surface)
            self.assertIn("fold checkpoint", surface)
            self.assertNotIn("every pin held", surface)


class PinIdentityAndStrictnessTest(PinBackfillBase):
    """The pin goes only into the repository the row NAMES, measured, and
    the pass reads the whole ledger or refuses by name (task/3627 FIX)."""

    def test_a_checkout_path_now_holding_another_repository_is_unknown(self):
        row, tip = self.pre_land_verdict("reused")
        real = self.state(row["id"])["repo_id"]
        other = self.other_repository()
        self.rebind(row["id"], real, other)
        state = self.state(row["id"])
        self.assertEqual((state["repo_id"], state["repo_root"]), (real, other))
        report = self.report("--apply", rc=1)
        self.assertEqual(report["status"], "INCOMPLETE")
        entry = self.repo_entry(report, real)
        self.assertEqual(self.outcomes(entry),
                         [(row["id"], "reviewed", "unreadable")])
        self.assertIn("not the repository the row names",
                      entry["tips"][0]["why"])
        self.assertEqual(self.pins(), {})
        self.assertEqual(self.pins_in(other), {},
                         "the pin went into the reused checkout")

    def test_an_ambient_git_dir_cannot_send_the_pin_elsewhere(self):
        row, tip = self.pre_land_verdict("ambient")
        other = self.other_repository()
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(other, ".git")}):
            report = self.report("--apply")
        self.assertEqual(self.pins_in(other), {},
                         "an ambient GIT_DIR took the pin")
        self.assertEqual(self.pins(), {LIVE + row["id"]: tip})
        self.assertEqual(report["status"], "COMPLETE")

    def test_the_verdict_writer_asks_the_same_identity(self):
        """ONE RESOLVER: the writer the backfill calls refuses the reused
        checkout and ignores an ambient GIT_DIR exactly as the census does."""
        row, tip = self.pre_land_verdict("writer")
        other = self.other_repository()
        state = self.state(row["id"])
        why = dispatches.pin_reviewed_tips(dict(state, repo_root=other),
                                           [("reviewed", tip)])
        self.assertIn("not the repository the row names", why or "")
        self.assertEqual((self.pins(), self.pins_in(other)), ({}, {}))
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(other, ".git")}):
            self.assertIsNone(dispatches.pin_reviewed_tips(
                state, [("reviewed", tip)]))
        self.assertEqual(self.pins_in(other), {})
        self.assertEqual(self.pins(), {LIVE + row["id"]: tip})

    def test_a_corrupt_ledger_line_fails_the_pass_by_name(self):
        """The census claims the WHOLE record, so a complete line the
        lenient reader would skip refuses the pass instead."""
        row, _tip = self.pre_land_verdict("corrupt")
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
            f.write("{this line is not json}\n")
        for args in ((), ("--apply",)):
            rc, out, err = self.backfill(*args)
            self.assertEqual(rc, 1, out)
            self.assertIn("dispatch ledger", err)
        self.assertEqual(self.pins(), {}, "a partial read pinned")

    def test_a_closed_rows_live_pin_is_retired(self):
        """A close whose retirement failed left the pin live, and a live pin
        on a role the row no longer carries: both move to the retired
        namespace, where the close would have put them."""
        tip = self.doomed_tip("closed-live")
        row = self.verdict_row("concur", ref=tip, lane="lane/closed-live")
        with mock.patch.object(dispatches, "retire_review_pins",
                               return_value=None):
            _out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        stray = self.doomed_tip("stray")
        self.git("update-ref", LIVE + row["id"] + "-source-clean", stray)
        self.assertEqual(self.pins(), {
            LIVE + row["id"]: tip, LIVE + row["id"] + "-source-clean": stray},
            "control: the close left both pins live")
        report = self.report("--apply")
        self.assertEqual(self.pins(), {
            RETIRED + row["id"]: tip,
            RETIRED + row["id"] + "-source-clean": stray})
        self.assertEqual(report["status"], "COMPLETE")
        with mock.patch.object(dispatches, "_pin_write",
                               wraps=dispatches._pin_write) as write:
            again = self.repo_entry(self.report("--apply"),
                                    self.state(row["id"])["repo_id"])
        write.assert_not_called()
        self.assertEqual({t["outcome"] for t in again["tips"]}, {"already"})


if __name__ == "__main__":
    unittest.main()
