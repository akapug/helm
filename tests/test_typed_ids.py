#!/usr/bin/env python3
"""What a seat types into `helm dispatch` and `helm lr show` (task/3382).

MEASURED on the three local seats over 40.5 h of transcripts: 78 calls to a
dispatch subverb that does not exist (47 of them `show`) each got the whole
8,645-character usage, and 7 of 11 such episodes never recovered; 85 of 4,866
typed hex tokens named nothing, and the odd lengths were bad 51-69 % of the
time, because a seat padded the 12-hex prefix a listing prints to the 40 hex
`verdict` demanded.

So: show, read, get, status and brief answer as `triage <id>`; an unknown
subverb or a known one with bad arguments prints ONE usage line, never the
whole grammar; every row id and every tip accepts a unique prefix; an
ambiguous prefix refuses and lists its candidates; a token that names nothing
refuses and names the row or commit its longest matching prefix resolves to.
A tip that BINDS is still bound as the full commit id, and every check that
guards what is bound still runs on that full id.
"""
import json
import subprocess
import unittest
from unittest import mock

from helm import dispatches, eventledger, landreq, typedids
from tests.test_dispatches import DispatchBase, run

ALIASES = ("show", "read", "get", "status", "brief")


def _lr(args):
    return run(landreq.cmd_lr, args)


class OneUsageLineTest(DispatchBase):
    """An unknown subverb, or a known one given bad arguments, is answered
    with one usage line; the whole grammar stays on --help and bare."""

    def usage_lines(self, err):
        return [line for line in err.splitlines()
                if "usage: helm dispatch" in line]

    def test_an_unknown_subverb_names_its_closest_in_one_line(self):
        """The alias `brief` now answers as triage, so the refusal this arm
        pins is measured on a token that is STILL unknown: `brefs`, whose
        closest match is `briefs` (the census) — exactly the match `brief`
        measured before the alias, so the arm tests the same door."""
        rc, out, err = run(dispatches.cmd_dispatch, ["brefs", "abc"])
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, "")
        lines = err.strip().splitlines()
        self.assertEqual(len(lines), 2, err)
        self.assertIn("'brefs'", lines[0])
        self.assertIn("--help", lines[0])
        self.assertIn("usage: helm dispatch briefs [--cut]", lines[1])
        self.assertLess(len(err), 1500)
        self.assertNotIn(dispatches.USAGE, err)

    def test_an_unknown_subverb_with_no_near_neighbour_lists_the_subverbs(self):
        rc, _out, err = run(dispatches.cmd_dispatch, ["zzqqxxy"])
        self.assertEqual(rc, 2, err)
        lines = err.strip().splitlines()
        self.assertEqual(len(lines), 1, err)
        for verb in ("send", "verdict", "hold", "triage", "show", "status"):
            self.assertIn(verb, lines[0])
        self.assertNotIn(dispatches.USAGE, err)

    def test_help_and_bare_still_print_the_whole_usage(self):
        seen = 0
        for argv in ([], ["--help"], ["-h"]):
            rc, _out, err = run(dispatches.cmd_dispatch, argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(dispatches.USAGE, err, argv)
            seen += 1
        self.assertEqual(seen, 3, "the argv sweep did not run")

    def test_a_known_subverb_with_bad_arguments_prints_its_own_line(self):  # noqa: VACUOUS_ASSERTION — five literal argv shapes, each asserting its own usage line, and the sweep ends on an unconditional exact count
        seen = 0
        for argv, head in (
                (["verdict", "id", "tip"], "usage: helm dispatch verdict <"),
                (["add", "only-one"], "usage: helm dispatch add <"),
                (["list", "--wat"], "usage: helm dispatch list ["),
                (["show"], "usage: helm dispatch show <"),
                (["status", "--all-projects"], "usage: helm dispatch status <")):
            rc, _out, err = run(dispatches.cmd_dispatch, argv)
            self.assertEqual(rc, 2, (argv, err))
            self.assertNotIn(dispatches.USAGE, err, argv)
            self.assertIn(head, err, argv)
            self.assertEqual(len(self.usage_lines(err)), 1, (argv, err))
            self.assertLess(len(err), 1500, argv)
            seen += 1
        self.assertEqual(seen, 5, "the argv sweep did not run")

    def test_the_usage_names_the_aliases_and_a_prefix_tip(self):
        for alias in ALIASES:
            self.assertIn(alias, dispatches.DISPATCH_READ_VERBS)
        self.assertIn("show <id-or-unique-prefix>", dispatches.USAGE)
        self.assertIn("verdict <id-or-unique-prefix> <reviewed-tip> ",
                      dispatches.USAGE)
        self.assertNotIn("<full-reviewed-tip>", dispatches.USAGE)


class TriageAliasTest(DispatchBase):

    def test_show_read_get_status_and_brief_answer_exactly_as_triage(self):
        row = self.add(lane="alias-row")
        rc, want, werr = run(dispatches.cmd_dispatch,
                             ["triage", row["id"][:12]])
        self.assertEqual(rc, 0, werr)
        self.assertIn(row["id"][:12], want)
        got = {alias: run(dispatches.cmd_dispatch, [alias, row["id"][:12]])
               for alias in ALIASES}
        self.assertEqual(got, {alias: (0, want, werr) for alias in ALIASES})

    def test_briefs_still_answers_its_census_not_triage(self):
        """`briefs` is its own census: one argument, a census line, and no
        trace of the triage view of the same row."""
        row = self.add(lane="census-row-3382f")
        rc, out, _err = run(dispatches.cmd_dispatch, ["briefs"])
        self.assertEqual(rc, 0, _err)
        self.assertNotIn(row["id"], out,
                         "the census answered as triage for a named row")
        census = [line for line in out.splitlines()
                  if "brief" in line.lower()]
        self.assertTrue(census, out)


class RowIdMatrixTest(DispatchBase):
    """verb x id-state for the row id: full, a unique prefix, an ambiguous
    prefix, no match, and a 12-hex prefix padded with zeros to 40."""

    SOLO = "cd" * 16
    TWINS = ("ab" * 6 + "1" * 20, "ab" * 6 + "2" * 20)

    def mint(self, rid, **kw):
        with mock.patch.object(dispatches.os, "urandom",
                               return_value=bytes.fromhex(rid)):
            return self.add(**kw)

    def setUp(self):
        super().setUp()
        self.solo = self.mint(self.SOLO, lane="solo")
        for n, rid in enumerate(self.TWINS):
            self.mint(rid, lane="twin-%d" % n)

    def states(self):
        return {"full": self.SOLO,
                "prefix": self.SOLO[:10],
                "ambiguous": "ab" * 6,
                "none": self.SOLO[:11] + "0",
                "padded": self.SOLO[:12] + "0" * 28}

    def refused_as(self, state, rc, err):
        """The shared refusal contract for the three refusing states; each
        caller counts the states it swept, unconditionally."""
        self.assertNotEqual(rc, 0, (state, err))
        if state == "ambiguous":
            self.assertIn("ambiguous", err, err)
            for rid in self.TWINS:
                self.assertIn(rid[:13], err, (state, err))
            self.assertIn("twin-0", err)
        else:
            self.assertIn("no such", err, (state, err))
            self.assertIn(self.SOLO, err, (state, err))
            self.assertIn("did you mean", err, (state, err))

    def test_the_triage_aliases(self):  # noqa: VACUOUS_ASSERTION — every state is swept under a subTest and the sweep ends on an unconditional exact count
        seen = []
        for state, token in self.states().items():
            with self.subTest(state=state):
                rc, out, err = run(dispatches.cmd_dispatch, ["show", token])
                if state in ("full", "prefix"):
                    self.assertEqual(rc, 0, err)
                    self.assertIn(self.SOLO[:12], out)
                else:
                    self.refused_as(state, rc, err)
                seen.append(state)
        self.assertEqual(seen, list(self.states()))

    def test_hold(self):  # noqa: VACUOUS_ASSERTION — every state is swept under a subTest and the sweep ends on an unconditional exact count
        seen = []
        for state, token in self.states().items():
            with self.subTest(state=state):
                rc, out, err = run(dispatches.cmd_dispatch,
                                   ["hold", token, "waiting", "on", "a", "box"])
                row = dispatches.snapshot()[0][self.SOLO]
                if state in ("full", "prefix"):
                    self.assertEqual(rc, 0, err)
                    self.assertEqual(row["status"], "held")
                    _row, why = dispatches.mark_release(self.SOLO)
                    self.assertIsNone(why)
                else:
                    self.refused_as(state, rc, err)
                    self.assertEqual(row["status"], "open")
                seen.append(state)
        self.assertEqual(seen, list(self.states()))

    def test_verdict_row_id(self):  # noqa: VACUOUS_ASSERTION — every state is swept under a subTest, the sweep ends on an exact count, and the full id is asserted unconditionally
        states = self.states()
        seen = []
        # The refusals first: the prefix arm writes this row's one verdict,
        # and the full id is exercised on a row of its own below.
        for state in ("ambiguous", "none", "padded", "prefix"):
            token = states[state]
            with self.subTest(state=state), self.verdict_author():
                rc, out, err = run(dispatches.cmd_dispatch, [
                    "verdict", token, self.a, "--concur", "--measured",
                    "read clean"])
                row = dispatches.snapshot()[0][self.SOLO]
                if state == "prefix":
                    self.assertEqual(rc, 0, err)
                    self.assertEqual(row["status"], "verdict")
                    self.assertEqual(row["reviewed_tip"], self.a)
                else:
                    self.refused_as(state, rc, err)
                    self.assertEqual(row["status"], "open")
                seen.append(state)
        self.assertEqual(len(seen), 4)
        full = self.add(lane="full-id")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", full["id"], self.a, "--concur", "--measured",
                "read clean"])
        self.assertEqual(rc, 0, err)

    def child(self, verb, token, lane):
        argv = [verb, "seat-a", lane]
        if verb == "send":
            argv += ["please", "read", "this"]
        return run(dispatches.cmd_dispatch, argv + [
            "--ref", self.a, "--kind", "review", "--repo", self.repo,
            "--supersedes", token])

    def test_send_and_add_supersedes(self):  # noqa: VACUOUS_ASSERTION — the refusals are swept to an exact count, the ledger is pinned at its three fixture rows, and both successes are asserted unconditionally
        before = sorted(dispatches.rows())
        self.assertEqual(len(before), 3)
        seen = 0
        for state, token in self.states().items():
            if state in ("full", "prefix"):
                continue
            for verb in ("send", "add"):
                with self.subTest(state=state, verb=verb):
                    rc, _out, err = self.child(verb, token, "kid-" + state)
                    self.refused_as(state, rc, err)
                    self.assertIn("--supersedes", err)
                seen += 1
        self.assertEqual(seen, 6)
        self.assertEqual(sorted(dispatches.rows()), before)
        rc, out, err = self.child("add", self.SOLO[:10], "kid-prefix")
        self.assertEqual(rc, 0, err)
        kid = next(r for r in dispatches.rows().values()
                   if r.get("lane") == "kid-prefix")
        self.assertEqual(kid["supersedes"], self.SOLO)
        other = self.add(lane="other-parent")
        rc, out, err = self.child("add", other["id"], "kid-full")
        self.assertEqual(rc, 0, err)

    def test_lr_show(self):  # noqa: VACUOUS_ASSERTION — every state is swept under a subTest and the sweep ends on an unconditional exact count
        seen = []
        for state, token in self.states().items():
            with self.subTest(state=state):
                rc, out, err = _lr(["show", token])
                if state in ("full", "prefix"):
                    self.assertEqual(rc, 0, err)
                    self.assertIn(self.SOLO[:12], out)
                else:
                    self.refused_as(state, rc, err)
                seen.append(state)
        self.assertEqual(seen, list(self.states()))

    def test_a_short_or_uppercase_token_is_told_what_it_lacks(self):
        _row, why = dispatches._resolve_row(dispatches.snapshot()[0],
                                            self.SOLO[:6])
        self.assertIn("at least 8", why)
        _row, why = dispatches._resolve_row(dispatches.snapshot()[0],
                                            self.SOLO[:12].upper())
        self.assertIn(self.SOLO, why)


class TipMatrixTest(DispatchBase):
    """The reviewed tip `verdict` binds, and the --patch-tip it records: a
    unique prefix resolves in the row's repository and is bound as the FULL
    commit id; everything else refuses and names what it could mean."""

    def verdict(self, row, tip, *extra):
        with self.verdict_author():
            return run(dispatches.cmd_dispatch, [
                "verdict", row["id"], tip, *(extra or (
                    "--concur", "--measured", "read clean"))])

    def events(self, rid):
        return [e for e in dispatches.history(rid)
                if e.get("event") == "verdict"]

    def kinds(self, rid):
        """Every event the row carries: a refusal leaves the dispatch alone."""
        return [e.get("event") for e in dispatches.history(rid)]

    def test_a_unique_prefix_binds_the_full_commit_id(self):  # noqa: VACUOUS_ASSERTION — each length unpacks exactly one verdict event and compares it to the full id; the sweep ends on an exact count
        seen = 0
        for n in (7, 12, 13, 39):
            with self.subTest(length=n):
                row = self.add(lane="prefix-%d" % n)
                rc, out, err = self.verdict(row, self.a[:n])
                self.assertEqual(rc, 0, err)
                self.assertIn("VERDICT", out)
                [event] = self.events(row["id"])
                self.assertEqual(event["reviewed_tip"], self.a)
                self.assertEqual(
                    dispatches.snapshot()[0][row["id"]]["reviewed_tip"],
                    self.a)
            seen += 1
        self.assertEqual(seen, 4)

    def test_the_full_id_binds_as_before(self):
        row = self.add(lane="full-tip")
        rc, _out, err = self.verdict(row, self.a)
        self.assertEqual(rc, 0, err)
        [event] = self.events(row["id"])
        self.assertEqual(event["reviewed_tip"], self.a)

    def test_a_prefix_of_another_commit_is_still_a_stale_verdict(self):
        row = self.add(lane="other-commit")
        rc, _out, err = self.verdict(row, self.b[:12])
        self.assertEqual(rc, 1, err)
        self.assertIn("stale verdict", err)
        self.assertIn(self.b, err)
        self.assertEqual(self.kinds(row["id"]), ["dispatch"])

    def test_no_match_refuses_and_names_the_candidate(self):
        row = self.add(lane="no-match")
        flip = "0" if self.a[11] != "0" else "1"
        rc, _out, err = self.verdict(row, self.a[:11] + flip)
        self.assertEqual(rc, 1, err)
        self.assertIn(self.a, err)
        # Composed with task/3382 L2: the refusal names the candidate on its
        # FIRST line, and ends with L2's one corrected line.
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertIn(self.a, lines[0], err)
        self.assertTrue(lines[-1].startswith("corrected: "), err)
        self.assertEqual(
            sum(line.startswith("corrected: ") for line in lines), 1, err)
        self.assertEqual(self.kinds(row["id"]), ["dispatch"])

    def test_a_prefix_padded_to_forty_refuses_and_names_its_first_twelve(self):
        row = self.add(lane="padded")
        padded = self.a[:12] + "0" * 28
        rc, _out, err = self.verdict(row, padded)
        self.assertEqual(rc, 1, err)
        self.assertIn("names no", err)
        self.assertIn(self.a, err)
        self.assertEqual(self.kinds(row["id"]), ["dispatch"])

    def test_an_ambiguous_prefix_refuses_and_lists_the_candidates(self):
        row = self.add(lane="ambiguous")
        twin = self.a[:12] + ("f" if self.a[12] != "f" else "e") + self.a[13:]
        real = dispatches._disambiguate

        def both(repo, prefix, env=None):
            got = real(repo, prefix, env)
            return got + [twin] if twin.startswith(prefix) else got
        with mock.patch.object(dispatches, "_disambiguate", side_effect=both):
            rc, _out, err = self.verdict(row, self.a[:12])
        self.assertEqual(rc, 1, err)
        self.assertIn("ambiguous", err)
        self.assertIn(self.a, err)
        self.assertIn(twin, err)
        self.assertEqual(self.kinds(row["id"]), ["dispatch"])

    def test_a_ref_name_is_not_a_prefix(self):
        row = self.add(lane="ref-name")
        rc, _out, err = self.verdict(row, "HEAD")
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.kinds(row["id"]), ["dispatch"])

    def test_a_patch_tip_prefix_is_recorded_as_the_full_commit(self):
        row = self.add(lane="patch-prefix")
        rc, out, err = self.verdict(
            row, self.a[:12], "--fix", "--measured", "--finding-count", "1",
            "--prior-relation", "new", "--worse-than-main", "state",
            "--patch-tip", self.b[:12], "a cure on b")
        self.assertEqual(rc, 0, err)
        [event] = self.events(row["id"])
        self.assertEqual(event["reviewed_tip"], self.a)
        self.assertEqual(event["patch_tip"], self.b)

    def test_a_source_clean_hold_binds_the_full_tip_and_names_a_padded_ones(self):
        """`hold --source-clean` resolved prefixes before task/3382 (the
        prior art this lane reuses); its refusal now names the candidate."""
        recipient = "seat-b"
        real = dispatches._acting_author

        def acting(action="author this dispatch"):
            return (recipient, None) if action == "hold this row" \
                else real(action)
        row = self.add(recipient=recipient)
        with mock.patch.object(dispatches, "_acting_author", acting), \
                mock.patch.object(dispatches, "_nudge", lambda *a: None):
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "hold", row["id"], "awaiting the land gate",
                "--source-clean", self.c[:12] + "0" * 28])
            self.assertEqual(rc, 1, err)
            self.assertIn(self.c, err)
            self.assertEqual(self.kinds(row["id"]), ["dispatch"])
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "hold", row["id"], "awaiting the land gate",
                "--source-clean", self.c[:12]])
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            dispatches.snapshot()[0][row["id"]]["source_clean_tip"], self.c)
        self.assertEqual(self.kinds(row["id"]), ["dispatch", "hold"])

    def test_the_hint_names_what_a_typed_tip_could_mean(self):
        self.assertIn(self.a, typedids.tip_hint(
            self.repo, self.a[:12] + "0" * 28))
        self.assertIn(self.a, typedids.tip_hint(
            None, self.a[:12] + "0" * 28, known=(self.a,)))
        self.assertEqual(typedids.tip_hint(self.repo, "not-hex"), "")


class AWriteDoorTakesOnlyAFullIdTest(DispatchBase):
    """A full object id is 40 hex (sha1) or 64 hex (sha256), and nothing
    between (task/3437). The fold still replays stored rows under the 40-to-64
    span, which this lane does not narrow, so a row whose stored tip is 41 to
    63 hex still folds. Each WRITE door that demands a full id refuses such a
    tip and still takes 40 and 64: one arm per distinct door, not one per
    site."""

    def planted(self, tip, **kwargs):
        """A row whose stored seq-0 tip is `tip`. The send door already
        refuses a tip that is not a full id, so the ledger is rewritten here,
        which is the only way such a tip reaches a row. The fold must keep the
        planted tip, or the arm is about a row that does not exist."""
        row = self.add(**kwargs)
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == row["id"] \
                        and event.get("event") == "dispatch":
                    event["tip"] = tip
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], tip)
        return row

    def verdict(self, length):
        tip = "b" * length
        row = self.planted(tip, lane="verdict-%d" % length)
        out, why = dispatches.mark_verdict(row["id"], tip, "read clean", "fix")
        return tip, out, why, dispatches.snapshot()[0][row["id"]]

    def test_the_verdict_door_binds_40_and_64_hex_and_refuses_50(self):
        tip, out, why, folded = self.verdict(40)
        self.assertIsNone(why)
        self.assertEqual(out["reviewed_tip"], tip)
        self.assertEqual(folded["status"], "verdict")
        tip, out, why, folded = self.verdict(64)
        self.assertIsNone(why)
        self.assertEqual(out["reviewed_tip"], tip)
        self.assertEqual(folded["status"], "verdict")
        # RED on the base: the 50-hex tip the row carries passed the 40-to-64
        # gate, matched the row, and BOUND a verdict to a name no commit has.
        _tip, out, why, folded = self.verdict(50)
        self.assertEqual(
            why, "verdict needs the full exact reviewed commit id")
        self.assertIsNone(out)
        self.assertEqual(folded["status"], "open")

    def retip(self, tip):
        row = self.planted(tip, lane="retip-%d" % len(tip), kind="build")
        return dispatches.retip(row["id"], self.b, reason="trunk moved",
                                notify=False)

    def test_the_retip_door_refuses_a_row_whose_tip_is_50_hex(self):
        # 40 and 64 pass the anchor guard. Neither names a commit in this
        # repository, so a LATER rung refuses them; the arm reads only that
        # the anchor guard let them through.
        _out, why = self.retip("b" * 40)
        self.assertNotIn("no derivable current tip", why)
        self.assertIn("retip REFUSED", why)
        _out, why = self.retip("b" * 64)
        self.assertNotIn("no derivable current tip", why)
        self.assertIn("retip REFUSED", why)
        out, why = self.retip("b" * 50)
        self.assertIsNone(out)
        self.assertIn("no derivable current tip", why)

    def resolve(self, printed):
        """`_resolve_tip` when rev-parse prints `printed` (git itself never
        prints 41 to 63 hex; this is the one representative of the doors that
        read a full id out of git's own output)."""
        done = subprocess.CompletedProcess([], 0, printed + "\n", "")
        with mock.patch.object(dispatches.subprocess, "run",
                               return_value=done) as spawned:
            got = dispatches._resolve_tip(self.repo, "HEAD",
                                          infer_sha_branch=False)
        self.assertEqual(spawned.call_count, 1)
        return got

    def test_the_resolver_takes_40_and_64_hex_from_git_and_refuses_50(self):
        self.assertEqual(self.resolve("c" * 40), ("c" * 40, None))
        self.assertEqual(self.resolve("c" * 64), ("c" * 64, None))
        self.assertEqual(self.resolve("c" * 50), (None, None))

    def test_the_privacy_door_asks_git_only_about_a_full_id(self):
        carried, why = dispatches._remotes_carrying(
            self.repo, self.a, ["origin"])
        self.assertIsNone(why)
        self.assertEqual(carried, {})
        carried, why = dispatches._remotes_carrying(
            self.repo, "d" * 64, ["origin"])
        self.assertIsNone(carried)
        self.assertNotIn("is not a full commit id", why)
        carried, why = dispatches._remotes_carrying(
            self.repo, "d" * 50, ["origin"])
        self.assertIsNone(carried)
        self.assertIn("is not a full commit id", why)

    def test_the_hint_never_offers_a_50_hex_name_it_was_handed(self):
        sixty_four, fifty = "e" * 64, "d" * 50
        self.assertIn(sixty_four, typedids.tip_hint(
            None, sixty_four[:12] + "0" * 28, known=(sixty_four,)))
        self.assertIn(self.a, typedids.tip_hint(
            None, self.a[:12] + "0" * 28, known=(self.a,)))
        self.assertEqual(typedids.tip_hint(
            None, fifty[:12] + "0" * 28, known=(fifty,)), "")


if __name__ == "__main__":
    unittest.main()
