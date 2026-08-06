#!/usr/bin/env python3
"""Work identity is a CHAIN, not a lane string.

Every new dispatch declares exactly one of `--new-work` or `--supersedes <id>`.
The lane stays a free-text label; the chain root is what says WHICH WORK a row
is. These tests walk one row through its whole life — including the states a
verb-shaped feature usually forgets, because they are the same object later:
parent open, parent cancelled, parent verdicted, parent missing, parent
ambiguous, two children on one parent, an unreadable ledger, and a corrupt
chain field.

THE RULE FOR UNKNOWN: a row whose chain cannot be resolved is UNKNOWN, and
UNKNOWN fails toward REFUSING. It never silently means "new work" — the whole
defect being repaired is unlinked rows that nobody chose to create.

All writes use scratch HELM_HOME/HELM_CHAT_DIR; the real ledger is untouched.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import cli, dispatches, eventledger, gate, home, seats

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "HELM_CHAT_ROOM", "HELM_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class ChainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chain-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        # The fleet suite cap counts REAL processes off /proc; an empty fake
        # proc tree keeps the gate.run fixtures deterministic under box load.
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.main = self.git("symbolic-ref", "--short", "HEAD")
        self.a = self.commit("a")
        self.b = self.commit("b")
        self.c = self.commit("c")

        # THE ROSTER STAYS EMPTY HERE, DELIBERATELY. Do not add
        # `seats.write_roster(...)` to this setUp: an empty roster is UNKNOWN,
        # which the recipient guard PROCEEDS on (fail-open), and that is what
        # every legacy test in this file relies on. Rostering sentinels here
        # flips the whole file from UNKNOWN-proceed to "populated roster, this
        # name absent = REFUSED", which took six tests red at once and left
        # this lane abandoned for ten hours — including
        # test_an_EMPTY_roster_PROCEEDS_because_it_is_unknown_not_negative,
        # whose entire contract is that the roster is empty. That test is the
        # guard: it fails the moment anyone re-adds a global registration.
        # A test that needs a rostered seat declares it in its OWN body.

    def tearDown(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        p = subprocess.run(["git", "-C", self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, text):
        path = os.path.join(self.repo, "state")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def root(self, lane="lane-a", **kw):
        """One row that roots its own chain."""
        kw.setdefault("ref", self.a)
        row, why = dispatches.add("codex-3", lane, repo=self.repo,
                                  kind="review", notify=False, new_work=True,
                                  _reason=True, **kw)
        self.assertIsNone(why)
        return row

    def child(self, parent, lane="lane-b", **kw):
        kw.setdefault("ref", self.a)
        return dispatches.add("codex-3", lane, repo=self.repo, kind="review",
                              notify=False, supersedes=parent, _reason=True,
                              **kw)

    def legacy_row(self, rid, **extra):
        """One row in the v1 schema — the shape everything on the live ledger
        was written in before chains existed."""
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        row = {"v": 1, "id": rid, "seq": 0, "status": "open",
               "ts": "2026-07-01T00:00:00Z", "recipient": "codex-3",
               "lane": "ancient", "tip": self.a, "deadline_s": 600}
        row.update(extra)
        self.assertTrue(eventledger.append(path, row))
        return row

    def rewrite(self, rid, **fields):
        """Overwrite fields on one row's opening event, the way a hand-edit or
        a forged ledger would. Real code never rewrites an append-only ledger;
        this is how a corrupt row gets in front of replay."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == rid and event.get("event") == "dispatch":
                    event.update(fields)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")


class RequiredFieldTest(ChainBase):
    """A required field is how you make a relation real. Optional is how 124
    land loops were built with no way to close them."""

    def test_a_row_that_names_no_work_identity_is_refused_and_not_recorded(self):
        row, why = dispatches.add("codex-3", "lane-a", ref=self.a,
                                  repo=self.repo, kind="review", notify=False,
                                  _reason=True)
        self.assertIsNone(row)
        self.assertEqual(why, dispatches.CHAIN_REQUIRED)
        self.assertIn("--new-work", why)
        self.assertIn("--supersedes", why)
        self.assertEqual(dispatches.rows(), {})

    def test_send_refuses_the_same_way_and_never_delivers(self):
        with mock.patch.object(seats, "dm") as dm:
            row, why, sent = dispatches.send(
                "codex-3", "lane-a", "review this", self.a, repo=self.repo,
                kind="review", sign=False)
        self.assertIsNone(row)
        self.assertEqual(why, dispatches.CHAIN_REQUIRED)
        self.assertFalse(sent)
        dm.assert_not_called()
        self.assertEqual(dispatches.rows(), {})

    def test_naming_both_is_refused_because_work_is_one_or_the_other(self):
        parent = self.root()
        row, why = dispatches.add("codex-3", "lane-b", ref=self.a,
                                  repo=self.repo, kind="review", notify=False,
                                  new_work=True, supersedes=parent["id"],
                                  _reason=True)
        self.assertIsNone(row)
        self.assertEqual(why, dispatches.CHAIN_EXCLUSIVE)
        self.assertEqual(len(dispatches.rows()), 1)

    def test_the_cli_refuses_both_verbs_with_the_usage_exit_code(self):
        """rc 2, like every other missing required flag on this verb. `--kind`
        already exits 2; one required flag exiting 2 and the other exiting 1
        for the same class of mistake is a surface that teaches nothing."""
        for verb, argv in (
                ("add", ["add", "codex-3", "lane-a", "--ref", self.a,
                         "--kind", "review", "--repo", self.repo]),
                ("send", ["send", "codex-3", "lane-a", "body", "--ref", self.a,
                          "--kind", "review", "--repo", self.repo])):
            rc, _out, err = run(dispatches.cmd_dispatch, argv)
            self.assertEqual(rc, 2, verb)
            self.assertIn("WORK IDENTITY", err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(dispatches.rows(), {})

    def test_the_cli_accepts_the_bare_new_work_flag(self):
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["add", "codex-3", "lane-a", "--ref", self.a,
                            "--kind", "review", "--repo", self.repo,
                            "--new-work"])
        self.assertEqual(rc, 0, err)
        self.assertIn("NEW WORK", out)
        rid = next(iter(dispatches.rows()))
        self.assertIn(rid[:12], out)

    def test_the_cli_links_a_continuation_and_says_so(self):
        parent = self.root()
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["add", "codex-3", "renamed-lane", "--ref", self.a,
                            "--kind", "review", "--repo", self.repo,
                            "--supersedes", parent["id"][:12]])
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTINUES %s" % parent["id"][:12], out)
        rows = dispatches.rows()
        kid = next(r for r in rows.values() if r["id"] != parent["id"])
        self.assertEqual(kid["chain_root"], parent["id"])

    def test_usage_renders_the_requirement_on_both_verbs(self):
        """A usage line that renders a required flag as optional teaches the
        exact omission the requirement exists to prevent."""
        self.assertIn("--new-work|--supersedes ID", dispatches.USAGE)
        self.assertEqual(dispatches.USAGE.count("--new-work|--supersedes ID"), 2)
        self.assertNotIn("[--new-work", dispatches.USAGE)


class ChainShapeTest(ChainBase):
    def test_new_work_roots_its_own_chain(self):
        row = self.root()
        self.assertEqual(row["chain_root"], row["id"])
        self.assertIsNone(row["supersedes"])

    def test_a_continuation_inherits_the_root_and_names_its_parent(self):
        parent = self.root()
        kid, why = self.child(parent["id"])
        self.assertIsNone(why)
        self.assertEqual(kid["supersedes"], parent["id"])
        self.assertEqual(kid["chain_root"], parent["id"])
        self.assertNotEqual(kid["id"], parent["id"])

    def test_the_root_survives_an_arbitrarily_long_rename_chain(self):
        """ONE HOP, not a walk: each row stores its resolved root, so round ten
        under a tenth name still answers with round one's id."""
        row = self.root(lane="round-1")
        rid = row["id"]
        for n in range(2, 11):
            kid, why = self.child(rid, lane="round-%d" % n)
            self.assertIsNone(why)
            self.assertEqual(kid["chain_root"], row["id"])
            self.assertEqual(kid["supersedes"], rid)
            rid = kid["id"]
        self.assertEqual(len(dispatches.rows()), 10)

    def test_the_chain_survives_a_fresh_disk_replay(self):
        parent = self.root()
        kid, _why = self.child(parent["id"])
        replayed = dispatches.snapshot()[0]
        self.assertEqual(replayed[kid["id"]]["chain_root"], parent["id"])
        self.assertEqual(replayed[kid["id"]]["supersedes"], parent["id"])
        self.assertEqual(replayed[parent["id"]]["chain_root"], parent["id"])
        self.assertIsNone(replayed[parent["id"]]["supersedes"])


class LifecycleWalkTest(ChainBase):
    """THE STUCK AND TERMINAL STATES. A feature designed as a verb on the
    primary case ships holes that are just the same object later in its own
    life, so every one of these is stated rather than discovered."""

    def test_an_OPEN_parent_is_a_legitimate_parent(self):
        parent = self.root()
        self.assertEqual(parent["status"], "open")
        kid, why = self.child(parent["id"])
        self.assertIsNone(why)
        self.assertEqual(kid["chain_root"], parent["id"])

    def test_a_CANCELLED_parent_is_a_legitimate_parent(self):
        """Refusing here would push the caller to `--new-work`, i.e. to a lie,
        and abandoning a round does not un-happen the work it was part of."""
        parent = self.root()
        dispatches.mark_cancel(parent["id"], "reviewer went dark")
        self.assertEqual(dispatches.rows()[parent["id"]]["status"], "cancelled")
        kid, why = self.child(parent["id"])
        self.assertIsNone(why)
        self.assertEqual(kid["chain_root"], parent["id"])

    def test_a_VERDICTED_parent_is_a_legitimate_parent(self):
        parent = self.root()
        dispatches.mark_verdict(parent["id"], self.a, "findings", polarity="fix")
        kid, why = self.child(parent["id"])
        self.assertIsNone(why)
        self.assertEqual(kid["chain_root"], parent["id"])

    def test_a_parent_that_does_not_exist_REFUSES_and_records_nothing(self):
        """UNKNOWN never means new work. This is the exact door the 124 came
        through: a re-dispatch that named nothing at all."""
        kid, why = self.child("deadbeefdeadbeef")
        self.assertIsNone(kid)
        self.assertIn("no such dispatch", why)
        self.assertIn("--supersedes", why)
        self.assertEqual(dispatches.rows(), {})

    def test_an_AMBIGUOUS_parent_prefix_REFUSES(self):
        """Two rows match the prefix, so mutating either would be a guess."""
        made = []
        for i in range(2):
            row = dispatches._base("codex-3", "lane-%d" % i, self.a, None, 600,
                                   self.repo, kind="review", new_work=True,
                                   rid="ab" + "%030x" % i)[0]
            out, why, _existed = dispatches._append_dispatch(row)
            self.assertIsNone(why)
            made.append(out)
        shared = made[0]["id"][:8]
        self.assertTrue(made[1]["id"].startswith(shared))
        kid, why = self.child(shared)
        self.assertIsNone(kid)
        self.assertIn("ambiguous", why)
        self.assertEqual(len(dispatches.rows()), 2)

    def test_TWO_ROWS_MAY_CLAIM_ONE_PARENT_only_as_an_explicit_fork(self):
        """One review spawning two independent fixes is real, but it must be a
        stated override rather than the default shape of an accidental resend."""
        parent = self.root()
        one, why = self.child(parent["id"], lane="fix-registry")
        self.assertIsNone(why)
        two, why = self.child(parent["id"], lane="fix-cold-start", force=True)
        self.assertIsNone(why)
        warning = " ".join(two.get(dispatches._WRITE_WARNINGS, ()))
        self.assertIn(one["id"][:12], warning)
        self.assertIn("deliberate fork", warning)
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(one["chain_root"], parent["id"])
        self.assertEqual(two["chain_root"], parent["id"])
        self.assertEqual(one["supersedes"], two["supersedes"])

    def test_an_UNREADABLE_ledger_REFUSES_rather_than_root_new_work(self):
        """Storage the writer cannot read is UNKNOWN, and the only safe reading
        of UNKNOWN is a refusal. Rooting a fresh chain here would mint exactly
        the unlinked row this field exists to prevent, at the moment the ledger
        is least able to show it."""
        parent = self.root()
        pid = parent["id"]
        os.replace(dispatches.ledger_path(),
                   os.path.join(self.tmp, "ledger.bak"))
        victim = os.path.join(self.tmp, "hardlink-victim")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("safe")
        os.link(victim, dispatches.ledger_path())
        self.assertIsNotNone(dispatches.snapshot()[1])
        kid, why = self.child(pid)
        self.assertIsNone(kid)
        self.assertIn("UNKNOWN", why)
        self.assertIn("NOT recorded", why)
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "safe")

    def test_a_parent_with_a_CORRUPT_chain_REFUSES(self):
        """Malformed is not absent. Absent is the LEGACY branch and it permits;
        letting a corrupted field fall into it would launder a broken relation
        into a legacy one, which is the lane's own defect in a costume."""
        parent = self.root()
        for bad in ("not-a-hex-id", "", 7, ["a" * 32], "AB" * 8):
            self.rewrite(parent["id"], chain_root=bad)
            replayed = dispatches.snapshot()[0][parent["id"]]
            self.assertEqual(replayed["chain_root"], dispatches.CHAIN_UNKNOWN,
                             bad)
            kid, why = self.child(parent["id"])
            self.assertIsNone(kid, bad)
            self.assertIn("UNKNOWN", why)
            self.assertEqual(len(dispatches.rows()), 1, bad)

    def test_a_legacy_parent_has_NO_chain_and_is_never_retrofitted(self):
        """History stays legacy. A child may still name it — the child's own
        honest statement is "my chain begins at this row I can name" — and the
        parent's stored row is not rewritten to agree."""
        parent = self.root()
        self.rewrite(parent["id"], chain_root=None, supersedes=None)
        replayed = dispatches.snapshot()[0][parent["id"]]
        self.assertIsNone(replayed["chain_root"])
        kid, why = self.child(parent["id"])
        self.assertIsNone(why)
        self.assertEqual(kid["chain_root"], parent["id"])
        self.assertIsNone(dispatches.snapshot()[0][parent["id"]]["chain_root"])

    def test_a_v1_row_reads_as_legacy_not_as_new_work(self):
        self.legacy_row("c" * 32)
        row = dispatches.snapshot()[0]["c" * 32]
        self.assertIsNone(row["chain_root"])
        self.assertIsNone(row["supersedes"])

    def test_a_v1_SHAPED_row_carrying_a_forged_chain_reads_UNKNOWN(self):
        """A v1 row predates chains, so it cannot honestly have one — but the
        field is still read through the validator rather than hardcoded to
        None. Hardcoding would be a second, silent way for a forged value to
        reach a consumer: the corrupt root would propagate into every row that
        named this one as its parent."""
        self.legacy_row("d" * 32, chain_root="not-a-hex-id")
        row = dispatches.snapshot()[0]["d" * 32]
        self.assertEqual(row["chain_root"], dispatches.CHAIN_UNKNOWN)
        kid, why = self.child("d" * 32)
        self.assertIsNone(kid)
        self.assertIn("UNKNOWN", why)

    def test_a_retry_that_changed_its_parent_names_different_work(self):
        """`supersedes` is SEMANTIC. Returning the first row here would keep a
        relation the caller explicitly corrected."""
        first = self.root(lane="round-1")
        other = self.root(lane="other-root")
        row, why, sent = dispatches.send(
            "codex-3", "round-2", "same body", self.a, repo=self.repo,
            kind="review", key="fixed-key", sign=False, supersedes=first["id"])
        self.assertIsNone(why)
        self.assertTrue(sent)
        clash, why, sent = dispatches.send(
            "codex-3", "round-2", "same body", self.a, repo=self.repo,
            kind="review", key="fixed-key", sign=False, supersedes=other["id"])
        self.assertIsNone(clash)
        self.assertIn("different work", why)
        self.assertFalse(sent)

    def test_the_auto_key_separates_rounds_that_continue_different_work(self):
        """Identical in every visible field, different parents: two operations,
        two rows. Folding them onto one id would lose a real dispatch to a hash
        the caller cannot see."""
        one = self.root(lane="root-one")
        two = self.root(lane="root-two")
        a, why, _sent = dispatches.send(
            "codex-3", "shared-lane", "identical body", self.a, repo=self.repo,
            kind="review", sign=False, supersedes=one["id"])
        self.assertIsNone(why)
        b, why, _sent = dispatches.send(
            "codex-3", "shared-lane", "identical body", self.a, repo=self.repo,
            kind="review", sign=False, supersedes=two["id"])
        self.assertIsNone(why)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["chain_root"], one["id"])
        self.assertEqual(b["chain_root"], two["id"])


class DuplicateSuccessorWarningTest(ChainBase):
    """Catch duplicate intent before append, from the writer's locked snapshot."""

    def warnings(self, row):
        return " ".join(row.get(dispatches._WRITE_WARNINGS, ()))

    def test_an_OPEN_successor_refuses_a_second_child_without_force(self):
        parent = self.root()
        first, why = self.child(parent["id"], lane="round-2")
        self.assertIsNone(why)
        second, why = self.child(parent["id"], lane="round-2-again")
        self.assertIsNone(second)
        self.assertIn(first["id"][:12], why)
        self.assertIn(parent["id"][:12], why)
        self.assertIn("--force", why)
        self.assertEqual(len(dispatches.rows()), 2)

    def test_force_records_a_deliberate_fork_and_keeps_the_warning(self):
        parent = self.root()
        first, why = self.child(parent["id"], lane="fix-one")
        self.assertIsNone(why)
        second, why = self.child(parent["id"], lane="fix-two", force=True)
        self.assertIsNone(why)
        self.assertIn(first["id"][:12], self.warnings(second))
        self.assertIn("deliberate fork", self.warnings(second))
        self.assertEqual(len(dispatches.rows()), 3)

    def test_NEW_WORK_with_an_OPEN_same_lane_refuses_unless_forced(self):
        first = self.root(lane="shared-label")
        dup, why = dispatches.add("codex-3", "shared-label", repo=self.repo,
                                  ref=self.b, kind="review", notify=False,
                                  new_work=True, _reason=True)
        self.assertIsNone(dup)
        self.assertIn("born-wrong", why)
        # --force admits a deliberate fork
        forced = self.root(lane="shared-label", ref=self.b, force=True)
        warning = self.warnings(forced)
        self.assertIn(first["id"][:12], warning)
        self.assertIn("born-wrong", warning)
        self.assertIn("--force", warning)
        self.assertEqual(len(dispatches.rows()), 2)

    def test_NEW_WORK_with_a_different_lane_has_no_warning(self):
        self.root(lane="one")
        second = self.root(lane="two", ref=self.b)
        self.assertNotIn(dispatches._WRITE_WARNINGS, second)

    def test_same_lane_in_another_repository_is_not_a_duplicate_signal(self):
        first = self.root(lane="shared-label")
        self.rewrite(first["id"], repo_id="/foreign/project/.git")
        second = self.root(lane="shared-label", ref=self.b)
        self.assertNotIn(dispatches._WRITE_WARNINGS, second)

    def test_a_CLOSED_successor_does_not_block_the_next_round(self):
        parent = self.root()
        closed, why = self.child(parent["id"], lane="closed-round")
        self.assertIsNone(why)
        dispatches.mark_verdict(closed["id"], closed["tip"], "fix", "fix")
        next_round, why = self.child(parent["id"], lane="next-round")
        self.assertIsNone(why)
        self.assertNotIn(dispatches._WRITE_WARNINGS, next_round)

    def test_the_duplicate_check_adds_no_snapshot_read(self):
        with mock.patch.object(dispatches, "snapshot",
                               wraps=dispatches.snapshot) as snap:
            self.root(lane="one-read")
        self.assertEqual(snap.call_count, 1)
        parent = self.root(lane="parent")
        with mock.patch.object(dispatches, "snapshot",
                               wraps=dispatches.snapshot) as snap:
            child, why = self.child(parent["id"], lane="two-existing-reads")
        self.assertIsNone(why)
        self.assertEqual(snap.call_count, 2)  # resolve parent + locked append

    def test_concurrent_children_serialize_to_one_unforced_successor(self):
        parent = self.root()
        barrier = threading.Barrier(2)
        original = dispatches._append_dispatch
        results = []

        def append(row, force=False):
            barrier.wait()
            return original(row, force=force)

        def write(lane, ref):
            results.append(self.child(parent["id"], lane=lane, ref=ref))

        with mock.patch.object(dispatches, "_append_dispatch", side_effect=append):
            threads = [threading.Thread(target=write, args=("race-a", self.b)),
                       threading.Thread(target=write, args=("race-b", self.c))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        rows = [row for row, _why in results if row is not None]
        refusals = [why for row, why in results if row is None]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(refusals), 1)
        self.assertIn("--force", refusals[0])
        self.assertEqual(len(dispatches.rows()), 2)

    def test_unreadable_snapshot_names_the_duplicate_check_UNKNOWN(self):
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "PermissionError: denied")):
            row, why = dispatches.add(
                "codex-3", "lane-a", ref=self.a, repo=self.repo,
                kind="review", notify=False, new_work=True, _reason=True)
        self.assertIsNone(row)
        self.assertIn("duplicate-mint check UNKNOWN", why)
        self.assertIn("NOT recorded", why)

    def test_public_help_matches_the_explicit_fork_contract(self):
        help_text = cli._VERB_HELP["dispatch"]
        send_help = help_text.split("|add", 1)[0]
        self.assertIn("[--force]", send_help)
        self.assertIn("second OPEN child", help_text)
        self.assertIn("REFUSES", help_text)

    def test_CLI_requires_force_for_a_duplicate_successor(self):
        parent = self.root()
        first, why = self.child(parent["id"], lane="first-child")
        self.assertIsNone(why)
        argv = ["add", "codex-3", "second-child", "--ref", self.b,
                "--kind", "review", "--repo", self.repo,
                "--supersedes", parent["id"]]
        rc, _out, err = run(dispatches.cmd_dispatch, argv)
        self.assertEqual(rc, 1, err)
        self.assertIn(first["id"][:12], err)
        self.assertIn("--force", err)
        self.assertEqual(len(dispatches.rows()), 2)
        rc, out, err = run(dispatches.cmd_dispatch, argv + ["--force"])
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING", err)
        self.assertIn("deliberate fork", err)
        self.assertIn("CONTINUES", out)
        self.assertEqual(len(dispatches.rows()), 3)

    def test_CLI_new_work_same_lane_refuses_without_force(self):
        self.root(lane="shared-label")
        rc, out, err = run(
            dispatches.cmd_dispatch,
            ["add", "codex-3", "shared-label", "--ref", self.b,
             "--kind", "review", "--repo", self.repo, "--new-work"])
        self.assertEqual(rc, 1, err)
        self.assertIn("born-wrong", err)
        # --force wins
        rc2, out2, err2 = run(
            dispatches.cmd_dispatch,
            ["add", "codex-3", "shared-label", "--ref", self.b,
             "--kind", "review", "--repo", self.repo, "--new-work", "--force"])
        self.assertEqual(rc2, 0, err2)
        self.assertIn("born-wrong", err2)
        self.assertIn("NEW WORK", out2)

    def test_send_refuses_before_DM_and_force_preserves_the_warning(self):
        parent = self.root()
        first, why = self.child(parent["id"], lane="first-child")
        self.assertIsNone(why)
        with mock.patch.object(seats, "dm") as dm:
            row, why, sent = dispatches.send(
                "codex-3", "second-child", "review", self.b,
                repo=self.repo, kind="review", sign=False,
                supersedes=parent["id"])
        self.assertIsNone(row)
        self.assertIn("--force", why)
        self.assertFalse(sent)
        dm.assert_not_called()
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "dm-1"}, None)) as dm:
            row, why, sent = dispatches.send(
                "codex-3", "second-child", "review", self.b,
                repo=self.repo, kind="review", sign=False,
                supersedes=parent["id"], force=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.assertIn(first["id"][:12], self.warnings(row))
        dm.assert_called_once()

    def test_send_UNKNOWN_reconciliation_keeps_the_locked_warning(self):
        parent = self.root()
        first, why = self.child(parent["id"], lane="first-child")
        self.assertIsNone(why)
        with mock.patch.object(seats, "dm", return_value=(None, "offline")), \
             mock.patch.object(
                 dispatches, "_reconcile_send",
                 return_value=(None, "obligation UNKNOWN", False)):
            row, why, sent = dispatches.send(
                "codex-3", "second-child", "review", self.b,
                repo=self.repo, kind="review", sign=False,
                supersedes=parent["id"], force=True)
        self.assertIsNone(row)
        self.assertFalse(sent)
        self.assertIn("obligation UNKNOWN", why)
        self.assertIn(first["id"][:12], why)
        self.assertIn("write warning", why)

    def test_idempotent_send_retry_recovers_the_ephemeral_warning(self):
        parent = self.root()
        first, why = self.child(parent["id"], lane="first-child")
        self.assertIsNone(why)
        args = ("codex-3", "second-child", "review", self.b)
        kw = {"repo": self.repo, "kind": "review", "sign": False,
              "supersedes": parent["id"]}
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "dm-1"}, None)) as dm:
            row, why, sent = dispatches.send(*args, force=True, **kw)
            retry, retry_why, retry_sent = dispatches.send(*args, **kw)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.assertFalse(retry_sent)
        self.assertIn("already recorded", retry_why)
        self.assertIn(first["id"][:12], self.warnings(retry))
        self.assertEqual(dm.call_count, 1)


class RebindKeepsTheChainTest(ChainBase):
    def test_a_rebind_CONTINUES_the_row_it_moves_it_does_not_invent_work(self):
        """helm's own only production caller. A rebind is one obligation
        addressed to a different seat; stamping it `--new-work` would fabricate
        a second piece of work out of one. The replacement links first so its
        writer-boundary duplicate refusal cannot destroy the source row."""
        seats.write_roster("ds4pro")
        # The SOURCE seat this row is addressed to must be rostered too,
        # not just the seat it moves to: `self.root()` sends to codex-3,
        # and an unrostered recipient is refused at write time. The base
        # class used to roster it for every test in the file, which flipped
        # every OTHER test from "empty roster = UNKNOWN = proceed" to
        # "populated roster, name absent = REFUSED" — six of them red.
        seats.write_roster("codex-3")
        parent = self.root(lane="moved-lane")
        out, why = dispatches.rebind(parent["id"], "ds4pro", reason="starved",
                                     force=True, repo=self.repo, notify=False)
        self.assertIsNone(why)
        self.assertEqual(out["old"]["status"], "cancelled")
        self.assertEqual(out["new"]["supersedes"], parent["id"])
        self.assertEqual(out["new"]["chain_root"], parent["id"])

    def test_rebind_refuses_when_the_successor_already_reached_a_VERDICT(self):
        """The case the writer's duplicate-fork refusal does NOT cover.

        Measured by disabling the guard: the writer refuses a replacement only
        while the successor is OPEN. A parent whose successor had reached a
        VERDICT was rebound and MINTED A SIBLING against finished work, with no
        refusal at all — completed work resurrected as a fresh obligation. The
        guard therefore lives in rebind() before any write, not in the writer."""
        seats.write_roster("ds4pro")
        seats.write_roster("codex-3")
        parent = self.root(lane="settled-lane")
        child, why = self.child(parent["id"], lane="took-it-over")
        self.assertIsNone(why)
        self.assertTrue(child["id"])
        _row, verr = dispatches.mark_verdict(child["id"], child["tip"],
                                             "findings recorded", polarity="fix")
        self.assertIsNone(verr, "the planted verdict was refused: %s" % verr)
        # MUST-HIT CONTROL: the successor really is terminal, so the refusal
        # below is about a VERDICT and not about a still-open sibling.
        self.assertEqual(dispatches.snapshot()[0][child["id"]]["status"],
                         "verdict")

        before = len(dispatches.snapshot()[0])
        out, why = dispatches.rebind(
            parent["id"], "ds4pro", reason="health override", force=True,
            repo=self.repo, notify=False)
        self.assertIsNone(out, "a sibling was minted against finished work")
        self.assertIn(child["id"][:12], why)
        self.assertIn("old row remains OPEN", why)
        self.assertEqual(len(dispatches.snapshot()[0]), before,
                         "the refusal still wrote a row")

    def test_rebind_force_does_not_authorize_a_duplicate_fork_or_cancel_source(self):
        seats.write_roster("ds4pro")
        seats.write_roster("codex-3")
        parent = self.root(lane="moved-lane")
        child, why = self.child(parent["id"], lane="already-moving")
        self.assertIsNone(why)
        # POSITIVE CONTROL on the OTHER channel of that same result: a
        # None `why` means "no refusal", which an empty/never-written
        # child would also produce. The row has to actually exist.
        self.assertTrue(child["id"])
        out, why = dispatches.rebind(
            parent["id"], "ds4pro", reason="health override", force=True,
            repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn(child["id"][:12], why)
        self.assertIn("old row remains OPEN", why)
        current = dispatches.rows()
        self.assertEqual(current[parent["id"]]["status"], "open")
        self.assertEqual(len(current), 2)

    def test_a_rebound_row_can_be_rebound_again_and_the_root_holds(self):
        seats.write_roster("ds4pro")
        seats.write_roster("gemini")
        seats.write_roster("codex-3")
        parent = self.root(lane="moved-lane")
        first, why = dispatches.rebind(parent["id"], "ds4pro",
                                       reason="starved", force=True,
                                       repo=self.repo, notify=False)
        # Do not discard the refusal channel: `[0]` threw it away, so a
        # rebind that refused outright would surface as a confusing
        # TypeError three lines later instead of the reason it gave.
        self.assertIsNone(why)
        self.assertTrue(first["new"]["id"])
        second = dispatches.rebind(first["new"]["id"], "gemini",
                                   reason="starved again", force=True,
                                   repo=self.repo, notify=False)[0]
        self.assertEqual(second["new"]["chain_root"], parent["id"])
        self.assertEqual(second["new"]["supersedes"], first["new"]["id"])


class SpiralCountsTheChainTest(ChainBase):
    """The guard OVERCOUNTED a finished spiral and UNDERCOUNTED a continuing
    one, from one cause: it counted a lane string. Both directions are pinned
    here, because fixing either alone is what kept failing."""

    def review(self, lane, ref, supersedes=None, force=False):
        row, why = dispatches.add(
            "codex-3", lane, ref=ref, repo=self.repo, kind="review",
            notify=False, _reason=True, new_work=supersedes is None,
            supersedes=supersedes, force=force)
        self.assertIsNone(why)
        return row

    def test_UNDERCOUNT_a_renamed_continuation_keeps_counting(self):
        """The live incident: `gate-mints-its-own-evidence` landed and
        `gate-epoch-is-append-order` opened immediately to close a hole in it.
        Round 10 of the same work under a new name, and every same-lane rule
        read it as round 1."""
        one = self.review("gate-mints-its-own-evidence", self.a)
        two = self.review("gate-epoch-is-append-order", self.b,
                          supersedes=one["id"])
        self.review("gate-receipt-binds-the-tip", self.c, supersedes=two["id"])
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertEqual(info["rounds"], 3)
        self.assertEqual(info["chain"], one["id"])
        self.assertEqual(info["lane"], "gate-receipt-binds-the-tip",
                         "the cure command must name what the seat calls this "
                         "work NOW, not what round one called it")

    def test_OVERCOUNT_a_reused_lane_name_starts_a_new_count(self):
        """Two unrelated pieces of work sharing a name are not a spiral, and a
        finished spiral must not keep counting because its label came back."""
        self.review("shared-name", self.a)
        self.review("shared-name", self.b, force=True)
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertIsNone(info, "two chains with one label is not a spiral")

    def test_one_chain_at_two_rounds_still_fires(self):
        one = self.review("shared-name", self.a)
        self.review("shared-name", self.b, supersedes=one["id"])
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertEqual(info["rounds"], 2)

    def test_a_legacy_row_still_groups_by_its_lane(self):
        """History keeps EXACTLY today's behaviour: it has no chain, and
        inventing one would be the lane-name heuristic all over again."""
        one = self.review("legacy-lane", self.a)
        two = self.review("legacy-lane", self.b, force=True)
        for rid in (one["id"], two["id"]):
            self.rewrite(rid, chain_root=None, supersedes=None)
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertEqual(info["rounds"], 2)
        self.assertEqual(info["chain"], "lane:legacy-lane")

    def test_a_legacy_row_never_merges_into_a_chained_one(self):
        legacy = self.review("shared-name", self.a)
        self.rewrite(legacy["id"], chain_root=None, supersedes=None)
        self.review("shared-name", self.b, force=True)
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertIsNone(info)

    def test_a_CORRUPT_chain_is_not_evidence_of_a_round(self):
        """TWO rows from two unrelated chains, both corrupted. Skipping them is
        the only reading that is not a fabrication: counting them would file
        them under one shared UNKNOWN bucket and report a two-round spiral
        between two pieces of work that were never related — a made-up number
        behind a hard block, which is the exact thing `kind` exists to refuse."""
        one = self.review("lane-one", self.a)
        two = self.review("lane-two", self.b)
        for row in (one, two):
            self.rewrite(row["id"], chain_root="not-a-hex-id")
        for row in (one, two):
            self.assertEqual(dispatches.snapshot()[0][row["id"]]["chain_root"],
                             dispatches.CHAIN_UNKNOWN)
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertIsNone(info, "a row whose chain cannot be read must not be "
                                "counted, and two of them must not become one "
                                "fake chain")

    def test_corrupting_a_round_drops_it_from_its_own_chain(self):
        one = self.review("shared-name", self.a)
        two = self.review("shared-name", self.b, supersedes=one["id"])
        self.assertEqual(dispatches.review_spiral("integrator")[0]["rounds"], 2)
        self.rewrite(two["id"], chain_root="not-a-hex-id")
        info, err = dispatches.review_spiral("integrator")
        self.assertIsNone(err)
        self.assertIsNone(info)

    def test_the_stop_guard_latch_separates_two_chains_on_one_lane_name(self):
        """The latch keyed on the lane string too, so the first chain's block
        swallowed the second chain's. Same defect, one layer up.

        The detector is stubbed on purpose: this asserts the LATCH, and the only
        way to hold (lane, rounds) fixed while varying the chain is to say so
        directly. The detector's own chain keying is measured by the tests
        above."""
        session = "sess-1"
        seen = {}

        def finding(*_a, **_k):
            return dict(seen), None
        with mock.patch.object(dispatches, "review_spiral", side_effect=finding):
            for chain in ("a" * 32, "b" * 32):
                seen.update(chain=chain, lane="shared-name", rounds=3,
                            peer="codex-3", recipients=["codex-3"], span_h=1.0)
                block, _warn = seats._spiral_gate(session, "main", "integrator")
                self.assertTrue(block, "chain %s must block on its own" % chain[0])
                again, _warn = seats._spiral_gate(session, "main", "integrator")
                self.assertIsNone(again,
                                  "the same state must latch, not re-block")


class DischargeWalksTheChainTest(ChainBase):
    """Proof-gated discharge asks "is this the SAME WORK", not "does this
    plausibly relate". Any later approved trunk commit trivially contains a
    change already on trunk, so the old question could not tell the
    difference."""

    def setUp(self):
        super().setUp()
        from helm import landreq
        self.landreq = landreq

    def contrary(self):
        row, why, _sent = dispatches.send(
            "codex-3", "feature-r1", "review r1", self.a, repo=self.repo,
            kind="review", key="r1", sign=False, new_work=True)
        self.assertIsNone(why)
        dispatches.mark_verdict(row["id"], self.a, "findings", polarity="fix")
        return row

    def approve(self, tip, lane, supersedes=None):
        row, why, _sent = dispatches.send(
            "codex-3", lane, "review " + lane, tip, repo=self.repo,
            kind="review", key="key-" + lane, sign=False,
            new_work=supersedes is None, supersedes=supersedes)
        self.assertIsNone(why)
        # A BOUND approve, minted for real. The old fixture wrote an ungated
        # approve and IGNORED mark_verdict's return — when the writer began
        # refusing ungated approves, every discharge test failed downstream
        # with "no later APPROVE binds", three asserts away from the cause.
        # SUITE is patched rather than argv passed: a custom-argv receipt
        # records no interpreter and bind refuses it by design. Every fixture
        # caller commits `tip` immediately before approving, so the repo HEAD
        # equals the reviewed tip and the receipt binds.
        with mock.patch.object(gate, "SUITE", (
                "-c", "import sys; print('Ran 1 test in 0.0s\\n\\nOK', "
                "file=sys.stderr)")):
            g, gerr = gate.run(repo=self.repo)
        self.assertIsNone(gerr, gerr)
        _got, verr = dispatches.mark_verdict(
            row["id"], tip, gate.evidence_line(g), polarity="approve")
        self.assertIsNone(verr, verr)   # assert the WRITE, not just downstream
        return row

    def test_a_contrary_with_a_CORRUPT_chain_can_never_be_discharged(self):
        first = self.contrary()
        fixed = self.commit("resolve")
        self.approve(fixed, "feature-r2", supersedes=first["id"])
        self.rewrite(first["id"], chain_root="not-a-hex-id")
        lr, why = self.landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)

    def test_TWO_CORRUPT_chains_are_not_a_match_they_are_two_unknowns(self):
        """THE HOLE THE MUTATION RUN FOUND. Equality on the replayed value is
        not enough on its own: two independently corrupted rows both read
        UNKNOWN, UNKNOWN == UNKNOWN, and a discharge would have been granted on
        the strength of two broken fields agreeing that they are broken. The
        contrary's chain is checked EXPLICITLY, before any candidate is
        compared, so corruption can never launder itself into a match.

        Every other clause here is satisfied — same author, same repo, same
        tip, real Git lineage, landed on trunk — so this refusal comes from the
        chain check and nothing else."""
        first = self.contrary()
        fixed = self.commit("resolve")
        second = self.approve(fixed, "feature-r2", supersedes=first["id"])
        self.rewrite(first["id"], chain_root="not-a-hex-id")
        self.rewrite(second["id"], chain_root="not-a-hex-id")
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[first["id"]]["chain_root"],
                         snap[second["id"]]["chain_root"])
        lr, why = self.landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(lr)
        self.assertIn("malformed chain_root", why)
        self.assertFalse(self.landreq.get(first["id"])[0]["discharged"])

    def test_a_LEGACY_contrary_still_discharges_on_the_old_rule(self):
        """Backward compatibility is the risk, and this is the row that carries
        it: every contrary written before chains existed would otherwise be
        permanently undischargeable — the same debt in a new costume."""
        first = self.contrary()
        fixed = self.commit("resolve")
        second = self.approve(fixed, "feature-r2")
        for rid in (first["id"], second["id"]):
            self.rewrite(rid, chain_root=None, supersedes=None)
        self.assertIsNone(dispatches.snapshot()[0][first["id"]]["chain_root"])
        lr, why = self.landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")   # via lr close
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_a_chained_contrary_refuses_a_LEGACY_candidate(self):
        """The candidate cannot prove it continues this work, so it does not.
        Fail toward refusing."""
        first = self.contrary()
        fixed = self.commit("resolve")
        second = self.approve(fixed, "feature-r2", supersedes=first["id"])
        self.rewrite(second["id"], chain_root=None, supersedes=None)
        lr, why = self.landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(lr)
        self.assertIn("SAME WORK CHAIN", why)

    def test_a_chained_contrary_refuses_a_CORRUPT_candidate(self):
        first = self.contrary()
        fixed = self.commit("resolve")
        second = self.approve(fixed, "feature-r2", supersedes=first["id"])
        self.rewrite(second["id"], chain_root="not-a-hex-id")
        lr, why = self.landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(lr)
        self.assertIn("SAME WORK CHAIN", why)

    def test_a_grandchild_round_can_discharge_the_root(self):
        """A chain is work identity, not a parent pointer: round three resolves
        round one's debt because they are the same work."""
        first = self.contrary()
        mid = self.commit("partial")
        self.approve(mid, "feature-r2", supersedes=first["id"])
        fixed = self.commit("resolve")
        third = self.approve(fixed, "feature-r3",
                             supersedes=dispatches.rows()[first["id"]]["id"])
        lr, why = self.landreq.discharge(first["id"], fixed, "r3 approved")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")   # via lr close
        self.assertEqual(lr["superseding_id"], third["id"])

    def test_lr_show_names_the_chain_a_refusal_sends_the_reader_to(self):
        """`discharge` refuses on a chain mismatch and its refusal names a
        `--supersedes <id>`. The reader it sends away must be able to SEE the
        chain this row is on — a surface that hides the field a verb refuses on
        makes the refusal unactionable."""
        first = self.contrary()
        fixed = self.commit("resolve")
        second = self.approve(fixed, "feature-r2", supersedes=first["id"])
        shown = self.landreq._render_show(self.landreq.get(second["id"])[0])
        self.assertIn("chain     %s" % first["id"], shown)
        self.assertIn("continues %s" % first["id"], shown)
        rooted = self.landreq._render_show(self.landreq.get(first["id"])[0])
        self.assertIn("chain     %s" % first["id"], rooted)
        self.assertNotIn("continues", rooted)

    def test_lr_show_says_LEGACY_rather_than_a_dash_for_a_pre_chain_row(self):
        """"Written before chains existed" and "value missing" are different
        facts, and a dash would render them the same."""
        first = self.contrary()
        self.rewrite(first["id"], chain_root=None, supersedes=None)
        shown = self.landreq._render_show(self.landreq.get(first["id"])[0])
        self.assertIn("chain     LEGACY", shown)

    def test_a_vanished_row_is_UNKNOWN_not_a_free_pass(self):
        """The land request is projected from this ledger; if the row is gone
        by the following read, its chain — and so whether any candidate is the
        same work — cannot be established."""
        first = self.contrary()
        fixed = self.commit("resolve")
        self.approve(fixed, "feature-r2", supersedes=first["id"])
        real = dispatches.snapshot_with_verdicts

        def without_the_row():
            current, verdicts, unavailable = real()
            current.pop(first["id"], None)
            return current, verdicts, unavailable
        with mock.patch.object(dispatches, "snapshot_with_verdicts",
                               side_effect=without_the_row):
            lr, why = self.landreq.discharge(first["id"], fixed, "resolved")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)


class ReplayIsTheBackstopTest(ChainBase):
    def test_clean_chain_reads_three_ways_and_only_three(self):
        self.assertIsNone(dispatches._replay_chain(None))
        self.assertEqual(dispatches._replay_chain("a" * 32), "a" * 32)
        for bad in ("", "zz", "A" * 32, "a" * 65, 1, True, [], {}, b"a" * 32,
                    "a" * 7):
            self.assertEqual(dispatches._replay_chain(bad),
                             dispatches.CHAIN_UNKNOWN, repr(bad))

    def test_a_forged_chain_never_reaches_a_consumer_as_a_plausible_id(self):
        parent = self.root()
        self.rewrite(parent["id"], chain_root="../../etc/passwd")
        replayed = dispatches.snapshot()[0][parent["id"]]
        self.assertEqual(replayed["chain_root"], dispatches.CHAIN_UNKNOWN)


class DischargedCloseDoorTest(ChainBase):
    """The #177 door, end to end: a never-verdicted BUILD row closes on the
    authority of its successor's landed APPROVE — and writer and replay must
    agree, or the ledger answers differently on reload (hc2's named risk).

    The live case that motivates it: 55bfc7c1 billed kimi 116 minutes while
    its work sat on trunk under ds4pro's APPROVE, minted under a DRIFTED LANE
    LABEL so no same-lane rule could ever tie them. The chain-tier discharge
    covers the recorded-relation case (f894eed2); the drifted-label case is
    what the ATTESTED close exists for, and this door must refuse it rather
    than invent the relation."""

    def _build_row(self, lane="build-lane"):
        row, why = dispatches.add("ds4pro", lane, ref=self.a, repo=self.repo,
                                  kind="build", notify=False, new_work=True,
                                  _reason=True)
        self.assertIsNone(why)
        return row

    def _landed_review(self, build, lane="review-lane"):
        """A review row that supersedes the build, verdicted approve, then
        landed — via real events so the replay arm sees the same ledger the
        writer did."""
        review, why = dispatches.add("codex-3", lane, ref=self.b,
                                     repo=self.repo, kind="review",
                                     notify=False, supersedes=build["id"],
                                     _reason=True)
        self.assertIsNone(why)
        # The receipt must POSTDATE the row's opening (gate._bind refuses
        # minted <= opened, second-granular): a fixture that runs both in the
        # same second is the race, so let the clock tick once.
        time.sleep(1.1)
        # An approve must carry a verified gate token; the gate needs the
        # reviewed tip to be the repo HEAD, which the fixture satisfies
        # (self.b is the tip commit). SUITE is patched the way the existing
        # discharge fixtures do it — a custom-argv receipt records no
        # interpreter and bind refuses it by design.
        with mock.patch.object(gate, "SUITE", (
                "-c", "import sys; print('Ran 1 test in 0.0s\\n\\nOK', "
                "file=sys.stderr)")):
            g, gerr = gate.run(repo=self.repo)
        self.assertIsNone(gerr, gerr)
        row, why = dispatches.mark_verdict(review["id"], self.b,
                                           gate.evidence_line(g),
                                           polarity="approve")
        self.assertIsNone(why, why)
        # The landed close — written through the real writer, so the ledger
        # holds exactly the terminal the fleet's own rows carry tonight.
        from helm import landreq
        out, lerr = landreq.close_landed(review["id"], repo=self.repo,
                                         trunk=self.main, live=True)
        self.assertIsNone(lerr, lerr)
        return review

    def test_an_open_build_row_closes_on_its_landed_successor(self):
        build = self._build_row()
        review = self._landed_review(build)
        # POSITIVE CONTROL, unconditional: the walk finds the discharger
        # BEFORE the door is asked, so the close below is the door acting.
        current = dispatches.rows()
        tier, by, why = dispatches.discharging_row(
            build["id"], current,
            is_landed=lambda tip: dispatches._writer_landed(current, tip))
        self.assertEqual((tier, by), (dispatches.DISCHARGE_TIER_CHAIN,
                                      review["id"]), why)
        row, err = dispatches._record_close_proven(
            build["id"], "discharged", None, evidence="rode in on the review",
            discharging_id=by, discharging_tip=self.b,
            discharge_tier=tier)
        self.assertIsNone(err, err)
        self.assertEqual(row["close_reason"], "discharged")
        # THE WHOLE POINT: replay from disk agrees with the writer's
        # projection, field for field.
        replayed = dispatches.rows()[build["id"]]
        for key in ("close_reason", "discharging_id", "discharging_tip",
                    "discharge_tier", "status"):
            self.assertEqual(replayed.get(key), row.get(key), key)
        self.assertEqual(replayed["status"], "closed")

    def test_a_never_landed_successor_refuses_at_the_door(self):
        build = self._build_row()
        review, why = dispatches.add("codex-3", "review-lane", ref=self.b,
                                     repo=self.repo, kind="review",
                                     notify=False, supersedes=build["id"],
                                     _reason=True)
        self.assertIsNone(why)
        row, why = dispatches.mark_verdict(review["id"], self.b, "approved", polarity="fix")
        self.assertIsNone(why, why)
        # CONTROL: the row IS open and unlanded — the refusal below is the
        # landed leg, not a missing chain.
        current = dispatches.rows()
        self.assertEqual(current[build["id"]]["status"], "open")
        row, err = dispatches._record_close_proven(
            build["id"], "discharged", None, evidence="no land yet",
            discharging_id=review["id"], discharging_tip=self.b,
            discharge_tier=dispatches.DISCHARGE_TIER_CHAIN)
        self.assertIsNone(row)
        self.assertIn("discharge refused", err)

    def test_a_verdicted_row_cannot_take_the_polarity_less_door(self):
        """A row WITH a verdict has its own proof-bearing doors; discharged
        must refuse it, not offer a polarity-free terminal."""
        review = self.root(lane="its-own-row")
        row, why = dispatches.mark_verdict(review["id"], self.a, "reviewed",
                                           polarity="fix")
        self.assertIsNone(why, why)
        # CONTROL: the row IS verdicted — the refusal below is the domain
        # gate, not a missing chain.
        self.assertEqual(dispatches.rows()[review["id"]]["status"], "verdict")
        row, err = dispatches._record_close_proven(
            review["id"], "discharged", None, evidence="wrong door",
            discharging_id="2" * 32, discharging_tip=self.b,
            discharge_tier=dispatches.DISCHARGE_TIER_CHAIN)
        self.assertIsNone(row)
        self.assertIn("refused", err)

    def test_a_forged_event_dies_on_REPLAY_not_just_at_the_writer(self):
        """hc2's named risk, from the other side: an event appended OUTSIDE
        the writer (disk tamper, a bug in a future caller) must be inert when
        the ledger is re-read. The writer check is not the boundary; replay
        is."""
        build = self._build_row()
        review = self._landed_review(build)
        # A forged close that names the REAL discharger but was never
        # validated: hand-appended with a wrong discharge_tier. The writer
        # never saw it. Replay must refuse to apply it.
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        seq = max(e.get("seq", 0) for e in events if e.get("id") == build["id"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 3, "event": "close", "id": build["id"], "seq": seq + 1,
                "ts": "2026-01-02T00:00:00Z", "close_reason": "discharged",
                "close_proof_version": 1, "close_evidence": "forged",
                "discharging_id": review["id"], "discharging_tip": self.b,
                "discharge_tier": "self-declared"},
                separators=(",", ":")) + "\n")
        replayed = dispatches.rows()[build["id"]]
        self.assertEqual(replayed["status"], "open",
                         "a forged close event must be inert on replay")
        self.assertIsNone(replayed.get("close_reason"))

    def test_replay_refuses_a_discharge_whose_landed_leg_is_UNPROVEN(self):  # noqa: VACUOUS_ASSERTION — the positive control is the sibling test: the SAME fixture with a landed close DOES close (same observable, opposite verdict), so this refusal is the landed leg and not a door that never opens
        """The replay arm's landed leg is tri-state: no landing closure on
        the ledger is UNKNOWN, and UNKNOWN must refuse — never narrow to
        'nothing recorded, so landed'. Measured by mutant: flipping the
        replay leg's fall-through to True admits exactly this event."""
        build = self._build_row()
        review, why = dispatches.add("codex-3", "review-lane", ref=self.b,
                                     repo=self.repo, kind="review",
                                     notify=False, supersedes=build["id"],
                                     _reason=True)
        self.assertIsNone(why)
        # The discharger must carry APPROVE so the walk reaches the LANDED
        # leg — a fix-polarity discharger dies on polarity first and the
        # landed leg is never consulted (measured: that shape of this test
        # let the unknown-admits mutant survive).
        time.sleep(1.1)
        with mock.patch.object(gate, "SUITE", (
                "-c", "import sys; print('Ran 1 test in 0.0s\\n\\nOK', "
                "file=sys.stderr)")):
            g, gerr = gate.run(repo=self.repo)
        self.assertIsNone(gerr, gerr)
        row, why = dispatches.mark_verdict(review["id"], self.b,
                                           gate.evidence_line(g),
                                           polarity="approve")
        self.assertIsNone(why, why)
        # A forged close event — the discharger EXISTS, reaches the row, and
        # carries APPROVE, but no landing is recorded anywhere. Replay must
        # refuse it on the unproven landed leg.
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        seq = max(e.get("seq", 0) for e in events if e.get("id") == build["id"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 3, "event": "close", "id": build["id"], "seq": seq + 1,
                "ts": "2026-01-02T00:00:00Z", "close_reason": "discharged",
                "close_proof_version": 1, "close_evidence": "forged",
                "discharging_id": review["id"], "discharging_tip": self.b,
                "discharge_tier": dispatches.DISCHARGE_TIER_CHAIN},
                separators=(",", ":")) + "\n")
        replayed = dispatches.rows()[build["id"]]
        self.assertEqual(replayed["status"], "open",
                         "an unproven landed leg must refuse on replay")
        self.assertIsNone(replayed.get("close_reason"))

    def test_an_UNDECLARED_verdict_row_refuses_at_WRITER_AND_REPLAY(self):
        """The integrator's measured attack (2026-08-04): the domain gate
        nested under `status != "verdict"` meant a discharged close on an
        UNDECLARED verdict row skipped every check — replay CLOSED the row on
        a discharging id naming nothing on the ledger. The gate is hoisted;
        this pins the refusal at both boundaries for exactly that row class
        (polarity None — the one value the discharged domain tuple admits)."""
        review = self.root(lane="undeclared-row")
        # An UNDECLARED verdict: polarity None via a forged verdict event
        # (the real writer refuses None, which is why the 28 live UNDECLARED
        # rows are all legacy/hand-written — the exposure oi measured).
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        seq = max(e.get("seq", 0) for e in events if e.get("id") == review["id"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 3, "event": "verdict", "id": review["id"],
                "seq": seq + 1, "ts": "2026-01-01T00:00:00Z",
                "reviewed_tip": self.a, "verdict_ref": "findings",
                "polarity": None},
                separators=(",", ":")) + "\n")
        # CONTROL: the row really is a verdicted, polarity-less row.
        self.assertEqual(dispatches.rows()[review["id"]]["status"], "verdict")
        self.assertIsNone(dispatches.rows()[review["id"]].get("polarity"))
        # WRITER refuses:
        row, err = dispatches._record_close_proven(
            review["id"], "discharged", None, evidence="attack",
            discharging_id="9" * 32, discharging_tip=self.b,
            discharge_tier=dispatches.DISCHARGE_TIER_CHAIN)
        self.assertIsNone(row)
        self.assertIn("refused", err)
        # REPLAY refuses the hand-appended event too:
        events = eventledger.events(path)
        seq = max(e.get("seq", 0) for e in events if e.get("id") == review["id"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 3, "event": "close", "id": review["id"],
                "seq": seq + 1, "ts": "2026-01-02T00:00:00Z",
                "close_reason": "discharged", "close_proof_version": 1,
                "close_evidence": "forged",
                "discharging_id": "9" * 32, "discharging_tip": self.b,
                "discharge_tier": dispatches.DISCHARGE_TIER_CHAIN},
                separators=(",", ":")) + "\n")
        replayed = dispatches.rows()[review["id"]]
        self.assertIsNone(replayed.get("close_reason"),
                          "replay closed a verdicted row on a forged "
                          "discharge — the F1 hole is back")

    def test_a_forged_discharge_names_the_wrong_discharger_and_dies(self):
        """The event's say-so is never trusted: the writer re-walks the
        ledger, and an event naming a row that does NOT reach the target is
        refused even when another row does."""
        build = self._build_row()
        self._landed_review(build)
        row, err = dispatches._record_close_proven(
            build["id"], "discharged", None, evidence="forged attribution",
            discharging_id="9" * 32, discharging_tip=self.b,
            discharge_tier=dispatches.DISCHARGE_TIER_CHAIN)
        self.assertIsNone(row)
        self.assertIn("not", err)


class DischargingRowTest(unittest.TestCase):
    """#177 — A BUILD ROW HAS NO DOOR OF ITS OWN.

    Every reason in _CLOSE_POLARITY keys on the row's OWN polarity, and a build
    row whose successor was minted before it was ever verdicted has none. It
    sits AWAITING_REVIEW while its work is on trunk. Measured 2026-08-04: five
    refusals across four verbs and two files, three rows, one predicate — one
    of them billing the fleet's most loaded reviewer 227 minutes for work that
    had landed three hours earlier.

    The authority is therefore ANOTHER row's verdict, admitted only when every
    leg of that voucher holds. The tests below are one leg each, because a
    voucher that can be satisfied by three of four legs is not a voucher."""

    ROW = "1" * 32
    SUCC = "2" * 32

    def _cur(self, **succ):
        row = {"id": self.ROW, "kind": "build", "status": "open",
               "lane": "alpha", "supersedes": None}
        s = {"id": self.SUCC, "kind": "review", "status": "verdict",
             "polarity": "approve", "lane": "alpha", "supersedes": self.ROW,
             "reviewed_tip": "t" * 40}
        s.update(succ)
        return {self.ROW: row, self.SUCC: s}

    def _landed(self, yes=True):
        return (lambda tip: yes)

    def test_a_vouching_successor_on_the_chain_discharges_it(self):  # noqa: VACUOUS_ASSERTION — the two assertions above it are unconditional positives on the SAME call's result (tier is chain, by names the successor); assertIsNone(why) only pins that a success carries no reason string
        tier, by, why = dispatches.discharging_row(
            self.ROW, self._cur(), is_landed=self._landed())
        self.assertEqual(tier, dispatches.DISCHARGE_TIER_CHAIN)
        self.assertEqual(by, self.SUCC, "the receipt must name WHO discharged it")
        self.assertIsNone(why)

    def test_a_FIX_successor_does_not_discharge_it(self):
        """The successor exists and reaches the row; its verdict says the work
        was NOT accepted. A door that ignored polarity would close a parent on
        the strength of a rejection."""
        # CONTROL: the same fixture with approve DOES discharge, so the refusal
        # below is the polarity leg and not a broken chain walk.
        self.assertTrue(dispatches.discharging_row(
            self.ROW, self._cur(), is_landed=self._landed())[0])
        tier, _by, why = dispatches.discharging_row(
            self.ROW, self._cur(polarity="fix"), is_landed=self._landed())
        self.assertIsNone(tier)
        self.assertIn("APPROVE", why)

    def test_an_UNLANDED_successor_does_not_discharge_it(self):
        """An approve whose tip never reached trunk discharges nothing — the
        parent's work is not on trunk either."""
        self.assertTrue(dispatches.discharging_row(
            self.ROW, self._cur(), is_landed=self._landed())[0])   # CONTROL
        tier, _by, why = dispatches.discharging_row(
            self.ROW, self._cur(), is_landed=self._landed(False))
        self.assertIsNone(tier)
        self.assertIn("landed", why)

    def test_could_not_look_is_not_landed(self):
        """is_landed returning None is UNKNOWN, and unknown must never pass as
        proof — a cap that skips a case must not produce a verdict."""
        self.assertTrue(dispatches.discharging_row(
            self.ROW, self._cur(), is_landed=self._landed())[0])   # CONTROL
        tier, _by, _why = dispatches.discharging_row(
            self.ROW, self._cur(), is_landed=lambda tip: None)
        self.assertIsNone(tier, "an unknown landing must not discharge a row")

    def test_no_recorded_relation_asks_for_an_ATTESTED_close(self):
        """The two rows this door deliberately cannot free. Their work landed
        under rows sharing neither their chain nor their lane, so no derivation
        can prove the relation — the refusal must SAY that rather than fall
        back to a weaker guess."""
        cur = self._cur()
        cur[self.SUCC]["supersedes"] = None       # nothing points at the row
        tier, by, why = dispatches.discharging_row(
            cur[self.ROW]["id"], cur, is_landed=self._landed())
        self.assertIsNone(tier)
        self.assertIsNone(by)
        self.assertIn("attested", why)

    def test_a_terminal_row_is_not_rediscovered(self):
        cur = self._cur()
        cur[self.ROW]["status"] = "closed"
        tier, _by, why = dispatches.discharging_row(
            self.ROW, cur, is_landed=self._landed())
        self.assertIsNone(tier)
        self.assertIn("terminal", why)


if __name__ == "__main__":
    unittest.main()


class SpiralGateKeysOnTheLedgersNameTest(unittest.TestCase):
    """@helm-claude's two-name probe, as a regression fixture.

    THE BUG: review_spiral matched the caller's RESOLVED SEAT NAME against the
    ledger's recorded sender, and those are not the same string for every
    seat. Measured on the live ledger 2026-08-01 — nine distinct senders —
    `helm-claude-2` authors 17 rows under its own name and IS seen, while
    `helm-claude` authors as bare `claude` (the family floor) and was
    structurally invisible: TEN ROUNDS on one chain, all night, zero
    detections. Same code, same rung, opposite outcomes, decided entirely by
    which string got recorded. A seat in a ten-round spiral looked identical
    to a seat with no rows at all."""

    def test_an_unmatched_name_is_UNKNOWN_and_says_so(self):
        """The whole finding in one assertion: a name the ledger never
        recorded returns an ERROR naming the problem, never a quiet None that
        reads as 'no spiral'."""
        from unittest import mock
        rows = {"a": {"sender": "claude", "kind": "review", "lane": "x",
                      "recipient": "codex", "ref": "a" * 40}}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)):
            info, err = dispatches.review_spiral("helm-claude")
        self.assertIsNone(info)
        self.assertIsNotNone(err)
        self.assertIn("UNKNOWN, not zero", err)
        self.assertIn("helm-claude", err)

    def test_a_name_the_ledger_DOES_carry_is_still_measured(self):
        """POSITIVE CONTROL. Without it the arm above passes on a version that
        reports every seat unmatched — which is exactly what my first cut did,
        by iterating snapshot()'s dict over its KEYS instead of its values and
        building an empty sender set."""
        from unittest import mock
        rows = {"a": {"sender": "claude", "kind": "review", "lane": "x",
                      "recipient": "codex", "ref": "a" * 40}}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)):
            seen = dispatches._sender_strings()
        self.assertIn("claude", seen)

    def test_the_sender_census_reads_VALUES_not_keys(self):
        """The bug my own fix shipped first, pinned so it cannot come back:
        snapshot() returns a dict KEYED BY ID. Iterating it directly walks id
        strings, every isinstance(row, dict) is False, and the census is
        empty — which reports every seat as unmatched and reproduces the
        blindness this lane exists to remove."""
        from unittest import mock
        rows = {"id-not-a-row": {"sender": "codex-2"},
                "another-id": {"sender": "kimi"}}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)):
            seen = dispatches._sender_strings()
        self.assertEqual(seen, {"codex-2", "kimi"})
        self.assertNotIn("id-not-a-row", seen)      # never the keys

    def test_an_unreadable_ledger_is_an_empty_census_not_a_crash(self):
        """Fail-open: a Stop rung may never wedge a turn. An empty census then
        reports every seat unmatched, which is UNKNOWN — the honest answer
        when the ledger cannot be read at all."""
        from unittest import mock
        with mock.patch.object(dispatches, "snapshot",
                               side_effect=OSError("ledger gone")):
            self.assertEqual(dispatches._sender_strings(), set())
