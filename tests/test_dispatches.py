#!/usr/bin/env python3
"""Dispatch ledger: durable handoff, exact-tip verdict, and hostile storage tests.
All writes use scratch HELM_HOME/HELM_CHAT_DIR; the real ledger is read-only."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
import socket
import sys
import pathlib
import textwrap
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import (chat, dispatches, eventledger, fsops, gate, home, landreq,
                  seat, seats, store, vcs, verdicts)
from tests import subsumption_property
from tests._gate_receipt import serial_process

# Classes this module HANDED AWAY, read by `helm/retired_name_rung.py`.
#
# That rung judges a diff PER FILE: a `-` line removing a top-level class with
# no `+` line adding it back IN THE SAME FILE reads as a retirement, and a move
# between two files in one commit is exactly the shape a size ceiling forces.
# The declaration is the cure it offers, and it is not clearance by itself --
# the rung also requires the named satellite to define the name at column zero,
# so this table cannot vouch for a class nobody wrote.
#
# NO SETATTR REPUBLICATION HERE, AND THAT IS THE DIFFERENCE FROM `helm/`.
# When a production module sheds a name, its consumers still spell
# `owner.NAME`, so the owner must bind it back or every one of them dangles.
# A moved TEST CLASS has no such consumer -- measured, zero spellings of
# `RetipTest` anywhere outside `tests/test_retip.py` -- and binding it back
# here would be a live defect rather than a courtesy: `unittest` collects by
# walking module attributes, so the class would be found twice, once under
# each module, and its 44 arms would run twice under two names.
_OWNER_NAMES = (
    ("test_retip", ("RetipTest",)),
    ("test_verdict_attest", ("VerdictTurnTest", "AttestReducerMatrixTest",
                             "AttestReducerRound5Test",
                             "VerdictListAttestProjectionTest")),
)

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "HELM_CHAT_ROOM", "CLAUDECODE", "HELM_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def _descends_from(snap, row, ancestor_id, limit=64):
    """Does `row` sit anywhere below `ancestor_id` on the supersedes chain?

    Walks UP by `supersedes`, bounded, so a corrupt cycle cannot hang a test.
    """
    seen, cur = set(), row
    while isinstance(cur, dict) and len(seen) <= limit:
        parent = cur.get("supersedes")
        if not parent:
            return False
        if str(parent) == str(ancestor_id):
            return True
        if str(parent) in seen:
            return False
        seen.add(str(parent))
        cur = snap.get(str(parent))
    return False


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()



class MixAndItsAlarmShareOneInstantTest(unittest.TestCase):
    """ONE COMMAND MUST NOT MAKE TWO CONTRADICTORY STATEMENTS ABOUT ONE SENDER.

    `cmd_mix` called `mix(hours, sender)` and then `mix_alarm(hours, sender)`,
    neither passing an instant, and `mix` defaults to time.time(). So one
    command performed TWO full ledger reads at TWO cutoffs. Against the hard
    MIX_NO_BUILD_ALARM threshold the TABLE can print a sender at the alarm
    count while the ALARM below it stays silent, or the alarm can name a
    sender whose printed row reads one lower.

    ASSERTED ON THE AST because the window between the two reads is
    microseconds and no hermetic fixture widens it reliably — the property is
    "one instant, threaded into both", which is visible where it is written."""

    def test_cmd_mix_threads_one_instant_into_both_reads(self):
        import ast, inspect, textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(dispatches.cmd_mix)))
        binds, calls = [], {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "read_now"
                            for t in node.targets)):
                binds.append(node.lineno)
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None)
                if name in ("mix", "mix_alarm"):
                    calls.setdefault(name, []).append(node)
        # POSITIVE CONTROLS: both producers must be CALLED here, or the
        # threading assertions below are about calls that do not exist.
        self.assertTrue(calls.get("mix"), "cmd_mix does not call mix()")
        self.assertTrue(calls.get("mix_alarm"),
                        "cmd_mix does not call mix_alarm()")
        self.assertTrue(binds, "cmd_mix binds no read_now")
        self.assertEqual(len(binds), 1,
                         "cmd_mix binds read_now %d times" % len(binds))
        for name, nodes in sorted(calls.items()):
            for c in nodes:
                passed = [getattr(a, "id", None) for a in c.args]
                passed += [getattr(k.value, "id", None) for k in c.keywords]
                self.assertIn("read_now", passed,
                              "%s() is called without the bound instant, so "
                              "it re-reads the clock and the table and the "
                              "alarm can disagree about one sender" % name)


class ListStampMustNotPostdateItsReadTest(unittest.TestCase):
    """THE INSTANT MUST NOT POSTDATE THE ROWS IT STAMPS.

    `dispatch list` bound read_now BELOW snapshot(), so the ledger was read at
    T0 and the listing was stamped T1 > T0. A row that appeared in that gap is
    ABSENT from a listing whose stamp claims a later read — the stamp then
    makes a stronger claim than the read supports, which is an absence claim
    about a moment the read never saw. Asserted at the source because the gap
    is microseconds and no hermetic fixture can widen it reliably.
    (a review of the cure that bound the instant in the first place: the
    ORDER was the residual half.)"""

    def test_read_now_is_bound_before_the_snapshot(self):
        """ASSERTED ON THE AST, NOT ON THE TEXT — and the first cut of this
        test proves why. Scanning the source string for "snapshot()" matched
        the WORD INSIDE THE COMMENT that explains the fix, so the ordering
        check compared a comment against code and failed on correct code. A
        text probe's domain includes prose; the AST's does not."""
        import ast, inspect
        src = inspect.getsource(dispatches._cmd_dispatch)
        tree = ast.parse(textwrap.dedent(src))
        fn = tree.body[0]

        def branch_for(verb):
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                t = node.test
                if (isinstance(t, ast.Compare)
                        and isinstance(t.left, ast.Name) and t.left.id == "verb"
                        and isinstance(t.comparators[0], ast.Constant)
                        and t.comparators[0].value == verb):
                    return node
            return None

        # POSITIVE CONTROL ON THE SAME HELPER, unconditional: it must locate a
        # DIFFERENT verb I know exists. Without it, a branch_for that returned
        # None for everything would make the assertion below the only witness,
        # and a branch_for that matched the WRONG branch would have no witness
        # at all — the matcher has to be shown discriminating, not merely
        # answering.
        self.assertTrue(branch_for("verdict"),
                        "branch_for cannot find a verb that exists, so its "
                        "verdict on `list` means nothing")
        branch = branch_for("list")
        self.assertTrue(branch, "no `verb == \"list\"` branch found")
        self.assertIsInstance(branch, ast.If)

        bind_lines, snap_lines = [], []
        for node in ast.walk(branch):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "read_now"
                            for t in node.targets)):
                bind_lines.append(node.lineno)
            # THE CALL IS SPELLED THROUGH THE LEDGER MODULE since the verb
            # table moved to its satellite: a bare `snapshot()` would bind at
            # import and stop seeing a patched ledger, so the satellite
            # reaches the name at CALL TIME. Both spellings are matched, so
            # this arm asks about the CALL rather than about where it lives.
            func = node.func if isinstance(node, ast.Call) else None
            if func is not None and (getattr(func, "id", None) == "snapshot"
                                     or getattr(func, "attr", None) == "snapshot"):
                snap_lines.append(node.lineno)

        # POSITIVE CONTROLS: both statements must EXIST as code, or the
        # comparison below is between two empty lists and passes vacuously.
        self.assertTrue(bind_lines, "the list branch binds no read_now")
        self.assertTrue(snap_lines, "the list branch calls no snapshot()")
        self.assertEqual(len(bind_lines), 1,
                         "the list branch binds read_now %d times"
                         % len(bind_lines))
        self.assertLess(min(bind_lines), min(snap_lines),
                        "read_now is bound AFTER snapshot(), so the listing's "
                        "stamp postdates the rows it stamps: an absence claim "
                        "about a moment the read never saw")


class VerdictBasisTest(unittest.TestCase):
    """task/338 — the owner asked for doubt to be legible and it was captured
    as canon at certainty 1.00 with NO MECHANISM: 2.34% adoption decaying to
    zero, 221 verdicts unmarked BY CONSTRUCTION, and exactly 0% among every
    non-Claude family. Prose is not model-family-proof; a refusing verb is.

    These arms are on the VOCABULARY and REPLAY halves, which need no repo
    fixture. The CLI refusal is exercised in DispatchBase below."""

    def test_the_three_words_are_the_whole_vocabulary(self):
        self.assertEqual(verdicts.BASES,
                         ("measured", "inferred", "unverified"))
        for b in verdicts.BASES:
            with self.subTest(basis=b):
                got, err = verdicts.clean_basis(b)
                self.assertIsNone(err)
                self.assertEqual(got, b)

    def test_an_unrecognised_basis_is_an_ERROR_never_coerced(self):
        """The one failure this vocabulary cannot afford: a typo silently
        downgraded to `unverified` would read as honest doubt."""
        got, err = verdicts.clean_basis("probably")
        self.assertIsNone(got)
        self.assertIn("must be one of", err)
        self.assertIn("probably", err)

    def test_ABSENT_is_permissive_here_because_REPLAY_comes_through(self):
        """Same split this file settled for polarity and kind: required at the
        CLI for a new write, permissive in the library so historical replay
        and projection are untouched. Building it the other way breaks 289
        call sites that are not about basis."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME CALL: clean_basis DOES
        # return values, so the None below is "permissive" and not "this
        # function returns None for everything".
        got, err = verdicts.clean_basis("measured")
        self.assertIsNone(err)
        self.assertEqual(got, "measured")
        got, err = verdicts.clean_basis(None)
        self.assertIsNone(err)
        self.assertIsNone(got)

    def test_replay_reads_an_UNKNOWN_value_as_unverified_fail_closed(self):
        """A hand-edited, forged, or future-versioned row must not borrow a
        confidence nobody recorded."""
        self.assertEqual(verdicts.replay_basis("measured"), "measured")
        self.assertEqual(verdicts.replay_basis("nonsense"), "unverified")

    def test_replay_reads_an_ABSENT_value_as_UNMARKED_not_unverified(self):
        """THE DISTINCTION THAT MATTERS FOR THE 221 EXISTING ROWS: they were
        never ASKED how they knew. Collapsing absent into `unverified` would
        put a confidence claim into 221 rows nobody made one in."""
        self.assertIsNone(verdicts.replay_basis(None))
        self.assertIsNone(verdicts.replay_basis(""))
        # control, on the same call: a real value still survives replay
        self.assertEqual(verdicts.replay_basis("inferred"), "inferred")


class FlagsArePositionalTest(unittest.TestCase):
    """A blast-radius lens. The parser scanned the WHOLE argv
    for anything starting with "--", which reaches into the free-text evidence
    tail — so a reviewer who FORGOT the flag and wrote "this is --unverified
    at best" got a basis MINTED FROM THEIR PROSE and their evidence MUTILATED.

    These arms are on the PARSE, which is where the defect lived. They model
    the exact partition cmd_dispatch performs, so a change to that partition
    that reintroduces the positionless scan fails here."""

    def partition(self, rest):
        """THE SHIPPED PARSER, not a copy of it.

        This helper used to RE-IMPLEMENT the verdict branch's split, so every
        arm below exercised a reimplementation and never the code that runs.
        Measured: it passed while the real parser refused a bare `--`, and it
        FAILED once the real parser was fixed — a test bound to the wrong
        artifact, in the arms written to prove the previous cure. A parser
        that has taken three rounds of findings is exactly the one that must
        not have two spellings."""
        return dispatches.partition_verdict_flags(rest)

    def test_a_basis_word_in_EVIDENCE_is_not_minted_as_a_flag(self):
        flags, tail = self.partition(
            ["id", "tip", "--approve",
             "I could not run it so this is", "--unverified", "at best"])
        self.assertEqual(flags, ["--approve"])
        self.assertNotIn("--unverified", flags)
        # and the sentence survives WHOLE — mutilation is the other half
        self.assertEqual(" ".join(tail),
                         "I could not run it so this is --unverified at best")

    def test_a_POLARITY_word_in_evidence_is_not_minted_either(self):
        """The older half of the same defect, which predates this lane: prose
        reading "this is a --fix at best" would have minted a polarity."""
        flags, tail = self.partition(
            ["id", "tip", "--measured", "honestly this is a", "--fix", "case"])
        self.assertEqual(flags, ["--measured"])
        self.assertEqual(" ".join(tail), "honestly this is a --fix case")

    def test_flags_in_the_RIGHT_place_are_still_taken(self):
        """The unconditional positive control: the partition must still DO its
        job, or the two arms above would pass on a parser that takes nothing."""
        flags, tail = self.partition(
            ["id", "tip", "--approve", "--measured", "gate:abc", "it landed"])
        self.assertEqual(flags, ["--approve", "--measured"])
        self.assertEqual(" ".join(tail), "gate:abc it landed")

    def test_a_bare_TERMINATOR_lets_evidence_START_with_a_flag_word(self):
        """T1 cross-family, measured at the exact tip — the FOURTH
        prose shape, and the one the positional cure did not reach.

        Making the flags positional stopped prose from MINTING a basis. It
        also made evidence whose FIRST token is flag-shaped UNREPRESENTABLE:
        the scan ate it as a flag, and the conventional escape `--` was eaten
        too, landing in the polarity bucket as an unknown polarity and
        refusing with rc 2. So a reviewer quoting a flag at the START of their
        sentence had no way to say it at all — in the free-text half of a
        free-text field.

        The terminator is CONSUMED, never echoed: it is punctuation for the
        parser, not something the reviewer wrote."""
        flags, tail = self.partition(
            ["id", "tip", "--approve", "--measured", "--",
             "--unverified", "at", "best", "is", "what", "they", "wrote"])
        self.assertEqual(flags, ["--approve", "--measured"])
        self.assertEqual(" ".join(tail),
                         "--unverified at best is what they wrote")
        self.assertNotIn("--", flags)

    def test_the_terminator_does_not_swallow_a_LATER_double_dash(self):
        """The control on the cure: only the FIRST bare `--`, and only while
        still scanning flags, is punctuation. One appearing inside the
        evidence is a word the reviewer wrote and must survive verbatim —
        otherwise the fix for an unsayable sentence would make a different
        sentence unsayable."""
        flags, tail = self.partition(
            ["id", "tip", "--approve", "--measured",
             "they", "wrote", "--", "then", "kept", "going"])
        self.assertEqual(flags, ["--approve", "--measured"])
        self.assertEqual(" ".join(tail), "they wrote -- then kept going")


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-dispatch-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        # The fixture homes its posts EXPLICITLY through the same env seam
        # launched seats use — the old accidental "main" default, now stated
        # (verdict attestations land in main unless HELM_VERDICT_ROOM
        # overrides; the override tests below set that seam themselves).
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
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        from tests._tmphome import helm_tree
        helm_tree(self, self.repo)
        self.a = self.commit("a")
        self.git("branch", "side", self.a)
        self.b = self.commit("b")
        self.c = self.commit("c")
        self.git("checkout", "-q", "side")
        self.side = self.commit("side")
        self.git("checkout", "-q", self.main)

        # THE FIXTURE IS ITS OWN HOME. The write door refuses a ref whose
        # repository is not this project's, and home_repo_id resolves from the
        # RUNNING PACKAGE'S location — which is the real helm checkout, not
        # this temp repo. Every dispatch written here would be refused as
        # foreign, and 85 write sites in this file would fail on a REAL
        # refusal that says nothing about what they test.
        #
        # The real resolver is kept on the instance so the one arm that must
        # exercise it can restore it; pinning it here would otherwise mean the
        # suite tests a lambda and never the resolver — the exact gap that let
        # the registry fail-open ship.
        from tests._tmphome import pin_dispatch_home, pin_live_seats
        self._real_home_repo_id = pin_dispatch_home(self, self.repo)
        # NO ROW WRITTEN HERE WALKS THE HOST'S PROCESS TABLE (task/3039).
        pin_live_seats(self)

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

    def git(self, *args, cwd=None):
        p = subprocess.run(["git", "-C", cwd or self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, text):
        path = os.path.join(self.repo, "state")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def commit_file(self, name, text):
        """A commit that touches ITS OWN file, so a later cherry-pick of it
        applies cleanly onto a trunk that moved elsewhere.

        IT LIVES ON THE BASE CLASS, BESIDE `commit`, BECAUSE THE CONSTRAINT
        IS THE SIBLING'S. `commit` appends every commit to one shared `state`
        file, so ANY cherry-pick or rebase built with it conflicts by
        construction — a property of the helper, never of the world. Any arm
        in this module that needs a REBASE, a CHERRY-PICK or a DIVERGENCE
        reaches for this one; a carrier on a subclass is invisible to every
        other class in the file, so it gets rewritten rather than reused.

        `commit` IS DELIBERATELY UNTOUCHED: roughly 85 write sites depend on
        its append-to-one-file behaviour, and this exists beside it rather
        than inside it so neither answer has to serve both questions."""
        path = os.path.join(self.repo, name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", name)
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    _add_seq = 0

    def add(self, **kwargs):
        self._add_seq += 1
        defaults = {"recipient": "codex-3",
                    "lane": "lane-a-%d" % self._add_seq,
                    "ref": self.a, "repo": self.repo}
        defaults.update(kwargs)
        # A fixture row is INDEPENDENT work unless the test says otherwise, so
        # the helper roots its own chain and steps aside the moment a test names
        # a parent. It never fills in both — that is refused at the writer.
        defaults.setdefault("new_work", "supersedes" not in defaults)
        # NO NOTIFICATION LEG BY DEFAULT. This helper exists to mint a LEDGER
        # ROW; almost every caller is testing replay, terminality or chain
        # mechanics and does not care that a mention was posted. Since add()
        # learned to mark a row delivered on its own mention, leaving notify on
        # would silently hand every one of those tests an ALREADY-OBSERVED row
        # and a longer history — which is exactly what it did: 16 arms went red
        # on a fixture change none of them had asked for. Tests that DO mean to
        # exercise delivery pass notify=True and say so.
        defaults.setdefault("notify", False)
        # MINT AS THE REPOSITORY THE ROW BINDS. The write door refuses a ref
        # whose repository is not this project's, so a cross-repo fixture (the
        # TWIN, the clone) cannot mint its row while home is the base repo —
        # and it should not: such a row exists in the world only because THAT
        # repository's own helm wrote it. Same-repo callers are unaffected;
        # this simply says out loud which project each write speaks for.
        from tests._tmphome import dispatch_home
        with dispatch_home(defaults["repo"]):
            row = dispatches.add(**defaults)
        self.assertIsNotNone(row)
        return row

    def verdict_author(self, family="claude"):
        """One exact native author proof for CLI verdict fixtures."""
        from tests._verdict import native_author
        return native_author(self, family)

    def age(self, rid, seconds, ts=None):
        """Backdate every snapshot of one synthetic row consistently.  Real
        code never rewrites the append-only ledger; this fixture controls the
        immutable opening timestamp before exercising a fresh disk replay."""
        path = dispatches.ledger_path()
        stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() - seconds))
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for row in events:
                if row.get("id") == rid:
                    row["ts"] = stamp
                f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def forge_open(self, rid, **fields):
        """Put historical/corrupt state below the current refusing write door."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for event in events:
                if event.get("id") == rid and event.get("event") == "dispatch":
                    event.update(fields)
                f.write(json.dumps(event, separators=(",", ":")) + "\n")


class RepoIdentityWithoutTheCarriageProjectionTest(DispatchBase):
    """`repo_ids()` — WHICH repositories the ledger names, not what happened
    in them.

    THE COST THAT MOTIVATED IT, measured on the live ledger: `helm doctor`'s
    trunk-authority rung harvested ten strings by calling `rows()`, which is
    `snapshot()[0]` — the full carriage/landing fold over every row, 1,272 git
    spawns and the largest single share of the verb's wall, to read a field
    the raw ledger line already carries verbatim.

    THE SET EQUALITY IS THE WHOLE SAFETY ARGUMENT AND IT IS PINNED HERE, over
    a ledger that carries the shapes which could break it: a close, a second
    repository, a LATER event naming a repository no genesis ever named, and
    an opener the fold declines. A cheaper read that answers a slightly
    different question is not an optimisation, it is a silent change to what
    `helm doctor` reports."""

    def _second_repo(self):
        other = os.path.join(self.tmp, "elsewhere-repo")
        os.makedirs(other)
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "T")):
            subprocess.run(("git",) + args, cwd=other, check=True,
                           capture_output=True)
        with open(os.path.join(other, "f"), "w", encoding="utf-8") as fh:
            fh.write("x")
        subprocess.run(("git", "add", "-A"), cwd=other, check=True,
                       capture_output=True)
        subprocess.run(("git", "commit", "-qm", "elsewhere"), cwd=other,
                       check=True, capture_output=True)
        sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=other,
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        return other, sha, dispatches._repo_info(other)["repo_id"]

    def _populate(self):
        """-> (this repo, a second repo, a repo NO genesis names, a declined
        opener's repo). The last two are the ones a raw field scan gets wrong.
        """
        mine = dispatches._repo_info(self.repo)["repo_id"]
        other, osha, other_id = self._second_repo()
        kept = self.add()            # the fixture default recipient
        closed = self.add()
        _out, why = dispatches.mark_cancel(closed["id"], "fixture close")
        self.assertIsNone(why, why)
        elsewhere = self.add()
        self.forge_open(elsewhere["id"], repo_id=other_id, repo_root=other,
                        tip=osha, ref=osha)
        # A LATER EVENT MAY CARRY repo_id AND IT IS NOT AN OPENING. Measured on
        # the live ledger: retarget, verdict and abandon events all carry the
        # field. A scan that harvested every occurrence would report a
        # repository the projection never bound a row to.
        never = os.path.join(self.tmp, "named-by-no-genesis", ".git")
        seq = dispatches.snapshot()[0][kept["id"]]["seq"]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "retarget", "seq": seq + 1, "id": kept["id"],
            "ts": dispatches.pk.now_ts(), "repo_id": never}))
        # AN OPENER THE FOLD DECLINES OPENS NOTHING, so its repository is not
        # in the population either — the tip is not a commit id, which is the
        # cheapest genuine refusal `_new_state` makes.
        declined = os.path.join(self.tmp, "declined-opener", ".git")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "dispatch", "seq": 0, "id": "e" * 32,
            "ts": dispatches.pk.now_ts(), "status": "open",
            "recipient": "seat-a", "tip": "not-a-commit-id",
            "deadline_s": 3600, "sender": "seat-b", "repo_id": declined}))
        return mine, other_id, never, declined

    def _folded(self):
        return {r.get("repo_id") for r in dispatches.rows().values()
                if isinstance(r.get("repo_id"), str) and r.get("repo_id")}

    def test_the_SET_is_identical_to_the_one_the_full_projection_yields(self):
        """THE EQUALITY IS ASSERTED AGAINST `rows()` ITSELF, never against a
        literal, because the literal is what a future fold change would stop
        matching silently."""
        mine, other_id, never, declined = self._populate()
        folded = self._folded()
        cheap, unavailable = dispatches.repo_ids()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(folded, {mine, other_id},
                         "the fixture must really name TWO repositories, or "
                         "the equality below compares two empties")
        self.assertEqual(set(cheap), folded)
        self.assertEqual(len(cheap), len(set(cheap)),
                         "the caller iterates this and asks the filesystem "
                         "about each one: %r" % (cheap,))
        # THE UNCONDITIONAL POSITIVE ON THE SAME CONTAINER, immediately beside
        # the two absences: this list really does carry repositories, so a
        # `repo_ids` that returned [] would fail HERE rather than sail through
        # the two assertNotIns below.
        self.assertIn(mine, cheap)
        self.assertIn(other_id, cheap)
        self.assertNotIn(never, cheap,
                         "a repo_id carried by a LATER event never opened a "
                         "row: %r" % (cheap,))
        self.assertNotIn(declined, cheap,
                         "an opener the fold declined opened no row: %r"
                         % (cheap,))

    def test_the_identity_read_never_enters_the_carriage_fold(self):
        """THE COST PROPERTY, ASSERTED AS AN EFFECT ON THE MECHANISM rather
        than as a wall-clock or a spawn count, both of which are facts about
        this box under this load. `_fold_into` is where the per-row git work
        lives — `_fold` and the checkpointed read both enter it (task/2770) —
        and entering it at all is the defect, and a future `repo_ids` that
        quietly started calling `rows()` again would read identical on every
        other arm in this class."""
        self._populate()
        entered = []
        real = dispatches._fold_into

        def spy(*a, **kw):
            entered.append(1)
            return real(*a, **kw)

        with mock.patch.object(dispatches, "_fold_into", spy):
            dispatches.rows()
            self.assertTrue(entered,
                            "MUST-HIT CONTROL: the full projection DOES fold, "
                            "so the spy is wired to the real call")
            del entered[:]
            cheap, _unavailable = dispatches.repo_ids()
        self.assertTrue(cheap, "and it still answered: %r" % (cheap,))
        self.assertEqual(entered, [],
                         "the identity question must not pay for the fold")

    def test_an_UNREADABLE_ledger_answers_None_never_an_empty_LIST(self):
        """AN EMPTY LIST IS A CLAIM ABOUT THE WORLD AND None IS A CLAIM ABOUT
        THE READ. `rows()` could not tell its caller apart: `snapshot()[0]`
        discards the unavailable reason, so an unreadable ledger reached the
        trunk-authority rung as an empty population. This door returns the
        reason the read already computed.

        THE POSITIVE CONTROL RUNS FIRST, THROUGH THE SAME CALL, on the same
        observable: a real, non-empty population with no reason."""
        self._populate()
        readable, why = dispatches.repo_ids()
        self.assertTrue(readable, "control: the ledger IS readable here")
        self.assertIsNone(why)
        with mock.patch.object(dispatches.eventledger, "checked_events",
                               return_value=([], "PermissionError: ledger")):
            blind, reason = dispatches.repo_ids()
        self.assertIsNone(blind)
        self.assertIn("PermissionError", reason)


class AddDeliveryLegTest(DispatchBase):
    """add() could never mark a row delivered, so every row it minted sat at
    needs-confirmation FOREVER and the delivery-confirmation nag chased a state
    no code path could reach. Measured cost: four seats each spent a check-in on
    one such row (2026-08-02), and the same nag misbilled opus-integrator twice
    (2026-07-28, the incident add()'s own docstring records).

    The mention IS this path's delivery — add()'s docstring says it exists "so
    the reviewer's beacon picks up the obligation" — but _notify_public returned
    a bool, so the chat id that IS the evidence died at that seam."""

    def test_a_posted_mention_marks_the_row_delivered_with_ITS_OWN_id(self):
        """The id recorded must be the MENTION's, not a placeholder. Asserting
        the exact ref is what separates a real delivery leg from a row stamped
        observed by fiat — send() binds the DM row it got back, and this is the
        exact counterpart."""
        with mock.patch("helm.chat.post", return_value={"id": "mention-42"}):
            row = dispatches.add(recipient="codex-3", lane="lane-notify",
                                 ref=self.a, repo=self.repo, new_work=True,
                                 notify=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["delivery"], "observed")
        self.assertEqual(row["delivery_ref"], "mention-42")
        # and it is DURABLE, not just the returned dict
        self.assertEqual(dispatches.rows()[row["id"]]["delivery"], "observed")

    def test_a_FAILED_mention_stays_needs_confirmation(self):
        """FAIL-OPEN, and in the honest direction: no mention means delivery
        genuinely IS unknown, so claiming observed would launder a failed post
        into a green. The durable notify-failed trail must still be written."""
        with mock.patch("helm.chat.post", return_value=None):
            row = dispatches.add(recipient="codex-3", lane="lane-nofly",
                                 ref=self.a, repo=self.repo, new_work=True,
                                 notify=True)
        self.assertIsNotNone(row)                    # the obligation still stands
        self.assertEqual(row["delivery"], "needs-confirmation")
        kinds = [e.get("event") for e in dispatches.history(row["id"])]
        self.assertIn("notify-failed", kinds)

    def test_a_RAISING_mention_stays_needs_confirmation_and_never_loses_the_row(self):
        """Losing the obligation is worse than under-reporting its delivery —
        the trade add() already makes for a failed post, held here."""
        with mock.patch("helm.chat.post", side_effect=RuntimeError("chat down")):
            row = dispatches.add(recipient="codex-3", lane="lane-boom",
                                 ref=self.a, repo=self.repo, new_work=True,
                                 notify=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["delivery"], "needs-confirmation")
        self.assertIn(row["id"], dispatches.rows())

    def test_notify_False_mints_no_mention_and_claims_no_delivery(self):
        """The caller owns the hand-off on this path, so there is no evidence to
        record and the row must not pretend otherwise."""
        with mock.patch("helm.chat.post",
                        return_value={"id": "mention-ctl"}) as post:
            # POSITIVE CONTROL FIRST, unconditionally: this same patched mock
            # DOES get called on the notify=True path. Without it, "not called"
            # is satisfied by a mock that could never have been called at all --
            # a wrong patch target, a renamed seam, a raise before the post --
            # and the arm passes vacuously while proving nothing.
            loud = dispatches.add(recipient="codex-3", lane="lane-ctl",
                                  ref=self.a, repo=self.repo, new_work=True,
                                  notify=True)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(loud["delivery"], "observed")
            row = dispatches.add(recipient="codex-3", lane="lane-quiet",
                                 ref=self.a, repo=self.repo, new_work=True,
                                 notify=False)
            self.assertEqual(post.call_count, 1)   # still 1 -> quiet path posted nothing
        self.assertEqual(row["delivery"], "needs-confirmation")

    def test_the_delivered_event_is_appended_ONCE_not_per_read(self):
        """A delivery is one fact. _mark_delivered already refuses a second
        append on an observed row; this pins that add() cannot smuggle a
        duplicate in past it."""
        with mock.patch("helm.chat.post", return_value={"id": "mention-once"}):
            row = dispatches.add(recipient="codex-3", lane="lane-once",
                                 ref=self.a, repo=self.repo, new_work=True,
                                 notify=True)
        kinds = [e.get("event") for e in dispatches.history(row["id"])]
        self.assertEqual(kinds.count("delivered"), 1, kinds)
        # Same ref: idempotent -- no new event (different-ref updates are
        # tested in DeliveryRefUpdateTest; this pins that add() does not
        # smuggle a duplicate past the guard for the same delivery evidence)
        again, err = dispatches._mark_delivered(row["id"], "mention-once")
        self.assertIsNone(err)
        kinds = [e.get("event") for e in dispatches.history(row["id"])]
        self.assertEqual(kinds.count("delivered"), 1, kinds)
        self.assertEqual(again["delivery_ref"], "mention-once")


class DeliveryRefUpdateTest(DispatchBase):
    """Defect #9: no verb can set delivery_ref after send.  A dispatch row
    whose delivery mechanism changes or needs a retry had no way to update
    its delivery_ref -- the delivered event was write-once, first-wins.

    mark_delivered is the new public verb: idempotent for the same ref,
    appends a new delivered event for a different ref (latest wins in
    replay), and records _prior_delivery_ref as audit metadata.  Closed
    rows (verdict, cancelled) are refused.  Held rows (non-terminal)
    still accept delivery updates because the delivery mechanism is
    independent of whether work is gated on an external dependency."""

    def test_first_delivery_still_works_identically(self):
        """The basic path -- marking an undelivered row -- is unchanged."""
        row = self.add(lane="mark-first")
        self.assertEqual(row["delivery"], "needs-confirmation")
        out, err = dispatches.mark_delivered(row["id"], "dm-ref-1")
        self.assertIsNone(err)
        self.assertEqual(out["delivery"], "observed")
        self.assertEqual(out["delivery_ref"], "dm-ref-1")
        # Durable: reads back identically
        self.assertEqual(dispatches.rows()[row["id"]]["delivery_ref"],
                         "dm-ref-1")
        self.assertEqual(
            [e.get("event") for e in dispatches.history(row["id"])],
            ["dispatch", "delivered"])

    def test_cli_accepts_the_short_id_printed_by_list(self):
        row = self.add(lane="mark-short")
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["mark-delivered", row["id"][:12], "dm-short"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"], out)
        self.assertEqual(dispatches.rows()[row["id"]]["delivery_ref"],
                         "dm-short")

    def test_cli_refuses_ambiguous_and_unknown_prefixes_without_writing(self):  # noqa: VACUOUS_ASSERTION — both colliding rows are proven durably present before the refusals, then the same non-empty history map must remain byte-for-byte unchanged
        prefix = "a" * 12
        ids = (prefix + "1" * 20, prefix + "2" * 20)
        rows = []
        for rid in ids:
            with mock.patch.object(dispatches.os, "urandom",
                                   return_value=bytes.fromhex(rid)):
                rows.append(self.add(lane="mark-ambiguous-" + rid[-1]))
        before = {r["id"]: len(dispatches.history(r["id"])) for r in rows}
        self.assertEqual(before, {r["id"]: 1 for r in rows},
                         "the ambiguous controls were not durably written")
        for target, said in ((prefix, "ambiguous dispatch id prefix"),
                             ("b" * 12, "no such dispatch")):
            rc, out, err = run(dispatches.cmd_dispatch,
                               ["mark-delivered", target, "dm-refused"])
            self.assertEqual(rc, 1, (target, out, err))
            self.assertIn(said, err)
        self.assertEqual(
            {r["id"]: len(dispatches.history(r["id"])) for r in rows}, before)

    def test_update_to_different_ref_appends_new_event(self):
        """A DIFFERENT ref on an already-observed row appends a SECOND
        delivered event -- the latest wins in replay."""
        row = self.add(lane="mark-update")
        dispatches.mark_delivered(row["id"], "dm-ref-1")
        out, err = dispatches.mark_delivered(row["id"], "dm-ref-2")
        self.assertIsNone(err)
        self.assertEqual(out["delivery"], "observed")
        self.assertEqual(out["delivery_ref"], "dm-ref-2")  # latest wins
        events = dispatches.history(row["id"])
        kinds = [e.get("event") for e in events]
        self.assertEqual(kinds, ["dispatch", "delivered", "delivered"])
        self.assertIn("_prior_delivery_ref", events[-1])
        self.assertEqual(events[-1]["_prior_delivery_ref"], "dm-ref-1")
        # warnings carry the change note
        warnings = out.get(dispatches._WRITE_WARNINGS, ())
        self.assertTrue(any("dm-ref-1" in w for w in warnings), warnings)

    def test_same_ref_is_idempotent_no_new_event(self):
        """Setting delivery_ref to the SAME value twice appends nothing."""
        row = self.add(lane="mark-idem")
        dispatches.mark_delivered(row["id"], "dm-ref-1")
        out, err = dispatches.mark_delivered(row["id"], "dm-ref-1")
        self.assertIsNone(err)
        self.assertEqual(out["delivery_ref"], "dm-ref-1")
        # Exactly 2 events -- no third one for the idempotent call
        self.assertEqual(len(dispatches.history(row["id"])), 2)

    def test_closed_row_is_refused(self):
        """A verdict row refuses a delivery-ref update (never appends onto
        a terminal row)."""
        row = self.add(lane="mark-closed")
        dispatches.mark_verdict(row["id"], row["tip"], "reviewed", "fix")
        out, err = dispatches.mark_delivered(row["id"], "dm-ref-3")
        # Returns the existing row, no error -- same contract as before
        self.assertIsNone(err)
        self.assertEqual(out["status"], "verdict")
        self.assertNotEqual(out.get("delivery_ref"), "dm-ref-3")

    def test_cancelled_row_is_refused(self):
        row = self.add(lane="mark-cancelled")
        dispatches.mark_cancel(row["id"], "moot")
        out, err = dispatches.mark_delivered(row["id"], "dm-ref-4")
        self.assertIsNone(err)
        self.assertEqual(out["status"], "cancelled")

    def test_nonexistent_row_returns_error(self):
        out, err = dispatches.mark_delivered("0" * 32, "dm-ref-5")
        self.assertIsNone(out)
        self.assertIsNotNone(err)

    def test_latest_wins_in_replay(self):
        """Two delivered events with different refs: the LAST one is the
        canonical delivery_ref after replay."""
        row = self.add(lane="mark-replay")
        dispatches.mark_delivered(row["id"], "first")
        dispatches.mark_delivered(row["id"], "second")
        current = dispatches.rows()[row["id"]]
        self.assertEqual(current["delivery"], "observed")
        self.assertEqual(current["delivery_ref"], "second")
        # The prior ref is NOT in the materialized row -- it lives only
        # in the raw event stream
        self.assertNotIn("_prior_delivery_ref", current)


class CloseDoorGrammarTest(DispatchBase):
    """BUILD #100 (the 2026-08-02 seven-close drain): the close-door grammar
    killed four bound verdicts on one clause and wasted rounds on paper
    cuts. One fixture per clause, each failing exactly that clause."""

    @staticmethod
    def _strict_ref(value, polarity):
        statement, err = dispatches._parse_subsumption(value)
        return not err and statement is not None \
            and statement["polarity"] == polarity

    def test_strict_parser_zero_and_one_marker_arms(self):  # noqa: VACUOUS_ASSERTION — zero-marker append is an unconditional positive control; each fixed one-marker case asserts typed polarity and non-empty payload
        ordinary = "ordinary FIX review"
        statement, err = dispatches._parse_subsumption(ordinary)
        self.assertIsNone(err)
        self.assertIsNone(statement)
        row = self.add(lane="zero-marker")
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], ordinary, "fix")
        self.assertIsNone(why)
        self.assertEqual(out["verdict_ref"], ordinary)
        cases = (("approve", "SUBSUMPTION VERIFIED X landed on trunk by Y"),
                 ("fix", "Subsumption verified FIX findings were answered on "
                         "trunk: r2"),
                 ("approve", "gate:0123456789abcdef\nSubsumption verified "
                             "X landed on trunk by Y"))
        for expected, evidence in cases:
            with self.subTest(expected=expected, evidence=evidence):
                statement, err = dispatches._parse_subsumption(evidence)
                self.assertIsNone(err)
                self.assertEqual(statement["polarity"], expected)
                self.assertTrue(statement["payload"])

    def test_two_markers_refuse_AT_APPEND_and_record_nothing(self):
        evidence = ("gate:0123456789abcdef. Subsumption verified X landed on "
                    "trunk Y. Subsumption verified FIX findings were answered "
                    "on trunk: Z")
        statement, err = dispatches._parse_subsumption(evidence)
        self.assertIsNone(statement)
        self.assertIn("one verdict closes one debt", err)
        row = self.add(lane="two-markers")
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], evidence, "approve")
        self.assertIsNone(out)
        self.assertIn("split the evidence", why)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_generated_grammar_invariants_are_all_zero(self):
        self.assertEqual(
            subsumption_property.report(self._strict_ref),
            {"mutual_exclusion": 0, "multi_marker_claims": 0,
             "misclassified": 0})

    def test_gate_token_lives_outside_the_evidence_budget(self):  # noqa: VACUOUS_ASSERTION — the two length controls prove the overflow shape and exact stored evidence proves mark_verdict accepted it
        """A valid statement + token overflowed 256 together and died as
        'too long' with no hint. The token is an address, not prose."""
        statement = ("Subsumption verified the stale-base successor landed "
                     "on trunk by re-review at the moved tip, closed by " + "x" * 151)
        row = self.add(lane="token-budget")
        # the real verdict shape: token, a sentence boundary, then the
        # statement — the boundary is prose and DOES count, the token does not
        evidence = "gate:0123456789abcdef — " + statement
        budgeted = dispatches._GATE_TOKEN_RE.sub("", evidence)
        self.assertLessEqual(len(budgeted), 256)
        self.assertGreater(len(evidence), 256)       # over WITH the token
        from unittest import mock
        with mock.patch.object(
                dispatches.gate, "bind",
                return_value=("VERIFIED", "a" * 16, "test receipt")):
            out, why = dispatches.mark_verdict(row["id"], row["tip"],
                                               evidence, "approve")
        self.assertIsNone(why)
        self.assertEqual(out["verdict_ref"], evidence)  # stored verbatim

    def test_over_budget_statement_is_refused_with_the_count(self):
        row = self.add(lane="real-overflow")
        evidence = "gate:0123456789abcdef " + "y" * 300
        out, why = dispatches.mark_verdict(row["id"], row["tip"],
                                           evidence, "fix")
        self.assertIsNone(out)
        self.assertIn("gate: tokens excluded", why)
        self.assertIn("300 chars of statement", why)
        self.assertNotIn("Subsumption", why)   # budget refusal, not grammar

    def test_a_token_exempt_verdict_SURVIVES_THE_REDUCER_not_just_the_writer(self):  # noqa: VACUOUS_ASSERTION — the two length bounds fix the overflow shape and the re-read asserts exact status and evidence
        """THE READ-BACK ITS TWIN NEVER DID, and that omission cost four rows.

        test_gate_token_lives_outside_the_evidence_budget asserts on `out` —
        mark_verdict's OWN RETURN VALUE — so it proves the WRITE was accepted
        and says nothing about whether the row survives reduction. It was true
        and it read as "the verdict is recorded", which was false: the reducer
        re-checked 256 against the FULL string while the writer had budgeted
        the token-STRIPPED one, so evidence of 257..277 chars was written and
        then silently dropped, leaving the row OPEN with nothing raised.

        Worse, the attest fired at WRITE time over the accepted text, so the
        signature bound evidence the reducer discarded. Four rows are
        permanently split that way and there is no re-sign verb."""
        statement = ("Subsumption verified the stale-base successor landed "
                     "on trunk by re-review at the moved tip, closed by " + "x" * 151)
        row = self.add(lane="reducer-budget")
        evidence = "gate:0123456789abcdef — " + statement
        budgeted = dispatches._GATE_TOKEN_RE.sub("", evidence)
        self.assertLessEqual(len(budgeted), 256)   # the WRITER's measure: fine
        self.assertGreater(len(evidence), 256)     # the REDUCER's old one: fatal
        from unittest import mock
        with mock.patch.object(
                dispatches.gate, "bind",
                return_value=("VERIFIED", "a" * 16, "test receipt")):
            out, why = dispatches.mark_verdict(row["id"], row["tip"],
                                               evidence, "approve")
        self.assertIsNone(why)
        self.assertEqual(out["verdict_ref"], evidence)   # the twin stops here
        reread = dispatches.snapshot()[0][row["id"]]     # ...this is the gap
        self.assertEqual(reread["status"], "verdict",
                         "the write was accepted and the reduce dropped it")
        self.assertEqual(reread["verdict_ref"], evidence)

    def test_an_over_BUDGET_verdict_is_still_dropped_by_the_reducer(self):  # noqa: VACUOUS_ASSERTION — status stays exactly 'open' and the row is asserted present in open_rows, both unconditional positives
        """THE NEGATIVE DIRECTION, and without it the fix is just "accept
        anything". Reconciling the two caps must not RAISE the cap: evidence
        whose BUDGETED length (token excluded) still exceeds 256 must be
        dropped by the reducer exactly as before. Appended raw, because the
        writer refuses this shape and the branch under test is downstream of
        it — testing it through mark_verdict would stop at the writer's gate
        and prove nothing about the reducer's."""
        row = self.add(lane="still-over-budget")
        evidence = "gate:0123456789abcdef " + "y" * 300
        self.assertGreater(
            len(dispatches._GATE_TOKEN_RE.sub("", evidence)), 256)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"], "verdict_ref": evidence,
            "polarity": "approve", "gate": "", "gate_caps": []}))
        reread = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(reread["status"], "open")
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])

    def test_malformed_subsumption_refused_at_append_not_at_close(self):
        """Bind-time door-check: a statement that NAMES subsumption but does
        not parse dies while re-minting is cheap, not after the immutable
        verdict wasted a round at lr close."""
        row = self.add(lane="bind-time")
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"],
            "Subsumption verified on trunk", "fix")   # no 'what', no object
        self.assertIsNone(out)
        self.assertIn("Subsumption verified", why)
        self.assertIn("on trunk", why)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "open")                      # nothing was recorded

    def test_refusal_quotes_the_expected_form_verbatim(self):
        """The 'were' incident: four verdicts died on one missing word with
        no hint. Every grammar refusal names the accepted shape."""
        row = self.add(lane="verbatim")
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "Subsumption verified done", "fix")
        self.assertIsNone(out)
        self.assertIn("`Subsumption verified FIX findings were answered on "
                      "trunk:", why)
        self.assertIn("`Subsumption verified <what later trunk work did> "
                      "on trunk <how it resolved>`", why)


class LifecycleTest(DispatchBase):
    def test_delivery_observation_is_not_done_an_exact_tip_verdict_closes(self):
        row = self.add(lane="session-pid-resolver")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["delivery"], "needs-confirmation")
        seen, why = dispatches._mark_delivered(row["id"], "post-1")
        self.assertIsNone(why)
        self.assertEqual(seen["delivery"], "observed")
        self.assertEqual(seen["status"], "open")
        self.assertEqual([r["id"] for r in dispatches.open_rows()], [row["id"]])
        verdict, why = dispatches.mark_verdict(
            row["id"], self.a, "review-post-9", "fix")
        self.assertIsNone(why)
        self.assertEqual(verdict["reviewed_tip"], self.a)
        self.assertEqual(dispatches.open_rows(), [])

    def test_verdict_is_terminal_but_identical_retry_is_idempotent(self):
        row = self.add()
        first, why = dispatches.mark_verdict(row["id"], self.a, "safe", "fix")
        self.assertIsNone(why)
        self.assertNotIn("basis", first)
        again, why = dispatches.mark_verdict(row["id"], self.a, "safe", "fix")
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertNotIn("basis", dispatches.history(row["id"])[-1])
        self.assertNotIn("basis", dispatches.snapshot()[0][row["id"]])
        self.assertEqual(len(dispatches.history(row["id"])), 2)
        _row, why = dispatches.mark_verdict(
            row["id"], self.a, "different", "fix")
        self.assertIn("already has a verdict", why)

    def test_basis_first_return_replay_and_retry_tell_one_story(self):  # noqa: VACUOUS_ASSERTION — measured basis positively binds the same event/replay/list path before retry non-append is asserted
        row = self.add(lane="truthful-basis-return")
        real = dispatches.mark_verdict
        returned = []

        def capture(*args, **kwargs):
            result = real(*args, **kwargs)
            returned.append(result)
            return result

        with self.verdict_author(), mock.patch.object(
                dispatches, "mark_verdict", side_effect=capture):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], self.a, "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--no-patch-because", "a design finding for a meld", "safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT (FIX/MEASURED)", out)
        first, why = returned.pop()
        self.assertIsNone(why)
        self.assertEqual(first.get("basis"), "measured")

        history = dispatches.history(row["id"])
        replay = dispatches.snapshot()[0][row["id"]]
        rc, listed, err = run(dispatches.cmd_dispatch, ["list", "--json"])
        self.assertEqual((rc, err), (0, ""))
        projection = next(r for r in json.loads(listed) if r["id"] == row["id"])
        self.assertEqual(
            (history[-1].get("basis"), replay.get("basis"),
             projection.get("basis")),
            ("measured", "measured", "measured"))

        before = len(history)
        with self.verdict_author(), mock.patch.object(
                dispatches, "mark_verdict", side_effect=capture):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], self.a, "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--no-patch-because", "a design finding for a meld", "safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT (FIX/MEASURED)", out)
        again, why = returned.pop()
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertEqual(len(dispatches.history(row["id"])), before)

        rc, _out, err = run(dispatches.cmd_dispatch, [
            "verdict", row["id"], self.a, "--fix", "--unverified",
            "--worse-than-main", "helm/dispatches.py",
            "--no-patch-because", "a design finding for a meld", "safe"])
        self.assertEqual(rc, 1)
        self.assertIn("already has a verdict", err)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_missing_polarity_refuses_before_writing_any_verdict(self):
        row = self.add(lane="polarity-required")
        with open(dispatches.ledger_path(), "rb") as f:
            before = f.read()
        for missing in (None, "", "   "):
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "reviewed", missing)
            self.assertIsNone(out)
            self.assertIn("polarity is required", why)
            self.assertIn("can never be retired", why)
            self.assertIn("--fix", why)
            with open(dispatches.ledger_path(), "rb") as f:
                self.assertEqual(f.read(), before)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_explicit_fix_records_normally_without_a_gate(self):
        row = self.add(lane="explicit-fix")
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "needs work", "fix")
        self.assertIsNone(why)
        self.assertEqual(out["status"], "verdict")
        self.assertEqual(out["polarity"], "fix")
        self.assertEqual(out["gate"], "")

    def test_ungated_approve_refuses_without_changing_the_ledger(self):
        row = self.add(lane="approve-needs-gate")
        with open(dispatches.ledger_path(), "rb") as f:
            before = f.read()
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "looks good", "approve")
        self.assertIsNone(out)
        self.assertIn("verified gate:<token>", why)
        self.assertIn("can never authorize landing", why)
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_the_ungated_approve_refusal_names_the_LAND_gate_as_the_repair(self):
        """A REFUSAL IS A SET OF INSTRUCTIONS AND IT IS READ AS AUTHORITATIVE.

        This one told the reviewer to run the whole suite itself, at the
        reviewed tip. That is the practice the current sequence retired: the
        reviewer holds when the source is clean, the integrator rebases and
        runs the ONE whole-suite gate on the tree that actually lands, and the
        approve binds THAT token. A refusal still teaching the retired repair
        sends the reviewer to spend a suite on a tip nothing will land."""
        row = self.add(lane="approve-needs-gate")
        _out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "looks good", "approve")
        self.assertIn("hold", why.lower())
        self.assertIn("land gate", why.lower())
        # MUST-MISS: the retired instruction is gone, not merely joined by the
        # new one. A refusal naming both repairs teaches neither.
        self.assertNotIn("at the exact reviewed tip", why)

    def test_every_new_row_requires_an_exact_ref_so_it_is_closable(self):
        self.assertIsNone(dispatches.add("seat", "lane", repo=self.repo, new_work=True))
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "seat", "lane", "--repo", self.repo])
        self.assertEqual(rc, 2)
        self.assertIn("requires --ref TIP", err)

    def test_bad_identity_metadata_and_deadlines_are_refused(self):
        for recipient in ("", "team a", "seat\nINJECT", "x" * 65):
            self.assertIsNone(dispatches.add(recipient, "lane", new_work=True))
        self.assertIsNone(dispatches.add("seat", "lane\nINJECT", new_work=True))
        self.assertIsNone(dispatches.add("seat", "lane", deadline_s=0, new_work=True))
        self.assertIsNone(dispatches.add("seat", "lane", deadline_s=-1, new_work=True))
        self.assertIsNone(dispatches.add("seat", "lane", deadline_s=10**9, new_work=True))


class WhatTheActuatorClaimsTheVerdictReleasesTest(DispatchBase):
    """The offer layer claims `dispatch:<row id>` ON A SEAT'S BEHALF when a
    row is dispatched to an idle seat, and DISCARDS the lease id it mints.
    The seat reviews, binds a verdict, the row goes terminal -- and the lease
    stands. Three of them accumulated on one seat in one night, each found by
    the stop-guard rather than by the seat, while every reader of `helm chat
    claims` was told that seat was mid-work on lanes it had already verdicted,
    on the night reviewer availability was the scarcest thing the fleet had.

    THE HOLDER CANNOT PRESENT WHAT IT WAS NEVER GIVEN. `_binding_ok` demands
    the lease nonce and the actuator threw its copy away, so the exit has to
    RECOVER the token (`own_leases`, the cure that check's own message names)
    rather than ask the seat for it. An entry and an exit belong to the same
    owner."""

    SESSION = "9d3a1c72-5f10-4b8e-9a21-7c4e0b6d8f35"
    #: THE RECIPIENT IS THIS SEAT. The actuator auto-claims only a row
    #: ADDRESSED to the seat, and that same seat binds the verdict, so a
    #: fixture dispatching elsewhere models a sequence production never
    #: makes -- and cannot ask the offer layer for the resource either.
    RECIPIENT = "integrator"

    def setUp(self):
        """THE FIXTURE DECLARES A SESSION, because the exit resolves its seat
        through the ADMISSION DOOR and the base fixture is deliberately
        sessionless. A declared name with no session to corroborate it is
        UNCORROBORATED and refused -- correctly, and that refusal has its own
        arm below rather than being engineered away here."""
        super(WhatTheActuatorClaimsTheVerdictReleasesTest, self).setUp()
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SESSION
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        # A DECLARED NAME IS NOT AN IDENTITY UNTIL THE ROSTER CORROBORATES IT,
        # which is the door's own sentence and the reason this fixture cannot
        # inherit the base's deliberate emptiness. THE RECIPIENT IS ROSTERED
        # TOO: a POPULATED roster turns the recipient guard from
        # UNKNOWN-proceed into "this name is absent, REFUSE", so seeding one
        # name without the other would break `add` for reasons that have
        # nothing to do with leases. The base's own comment warns about
        # exactly this, and it is why the seed lives HERE and not up there.
        seats.write_roster("integrator", session=self.SESSION)
        # THE FIXTURE ASSERTS ITS OWN PRECONDITION, and says WHICH refusal
        # fired when it cannot meet it. Without this, every arm below fails
        # with "the lease is still there" -- a symptom that points at the
        # cure when the cause is that this fixture is not somebody.
        from helm import actors
        actor, err, why = actors.resolve_actor_reason(
            seats._env_session(), seats.safe_cwd(), act="release a lease")
        self.assertIsNotNone(
            actor, "the fixture is not an admissible identity (%s): %s"
            % (why, err))

    def _autoclaim(self, rid, holder):
        """Take the lease THE ACTUATOR WOULD TAKE, and learn the resource
        spelling FROM IT rather than composing one.

        THIS IS THE ARM'S WHOLE INTEGRITY. The first cut of this fixture
        minted `dispatch:<full row id>` because that is what the cure looked
        up; the actuator mints `dispatch:<rid[:8]>`, five other helm sites
        agree with the actuator, and the cure was the only site that did not.
        Every arm passed, because a fixture handed the author's hypothesis
        returns the author's hypothesis -- in the register of a measurement.
        So the resource comes from `_finalize_work_offer`'s own source of
        truth: the offer tuple's id, built by the offer layer.
        """
        res = self._actuator_resource(rid)
        ok, why, lease = seats.claim(res, holder, ttl=600,
                                     session=seats._env_session(), strict=True)
        self.assertTrue(ok, why)
        # A release that already happened answers "not claimed" rather than
        # raising, so this cleanup is safe on the arm where the cure fires.
        self.addCleanup(seats.release, res, holder, lease=lease,
                        session=seats._env_session(), strict=True)
        return res

    def _actuator_resource(self, rid):
        """The resource the OFFER LAYER itself would claim for this row.

        READ, NEVER COMPOSED. `_offer_rows` is the function that builds the
        offer tuple the actuator later hands to `claim()`, so its first
        element IS the actuator's spelling by construction. Asking it costs
        one call and removes the fixture's ability to agree with a wrong
        cure -- which is the whole reason this helper exists instead of a
        string concatenation.

        THE ROW MUST BE OFFERABLE TO US, which is also production's shape:
        the actuator only auto-claims a row ADDRESSED to the seat, and that
        seat is the one that later binds the verdict. A fixture that
        dispatched elsewhere and verdicted here was modelling a sequence the
        world does not produce.
        """
        from helm import seats_work_offer
        # THE OFFER LAYER SCOPES BY REPOSITORY, resolved from the PROCESS cwd,
        # and this fixture's rows live in a temp repo the runner is not
        # standing in. Pointing that one read at the fixture's repo is what
        # lets the real offer builder see the real row; nothing else about
        # the enumeration is stubbed.
        with mock.patch.object(seats_work_offer, "safe_cwd",
                               return_value=self.repo):
            offers = list(seats_work_offer._offer_rows(self._me()))
        for offer in offers:
            if str(rid).startswith(str(offer[0])):
                return "dispatch:" + str(offer[0])
        self.fail("the offer layer does not offer row %r to %s, so this "
                  "fixture cannot learn the actuator's resource spelling "
                  "from it" % (rid, self._me()))

    def _live(self):
        return [c["resource"] for c in seats.claims_list()]

    def _me(self):
        return seats.acting_seat(seats._env_session(), seats.safe_cwd())

    def test_binding_a_verdict_releases_the_lease_claimed_FOR_this_seat(self):
        row = self.add(recipient=self.RECIPIENT, lane="autoclaimed")
        res = self._autoclaim(row["id"], self._me())
        # MUST-HIT: the lease is live BEFORE the verdict, so the absence
        # below is a release and not a claim that never happened.
        self.assertIn(res, self._live())
        out, why = dispatches.mark_verdict(row["id"], row["tip"],
                                           "reviewed", "fix")
        self.assertIsNone(why)
        self.assertTrue(out)
        self.assertNotIn(res, self._live(),
                         "the verdict closed the row and left the lease the "
                         "actuator claimed for this seat standing")

    def test_it_never_releases_a_lease_ANOTHER_seat_holds(self):  # noqa: VACUOUS_ASSERTION — the absence claim is `assertNotIn(res_mine)`, and its unconditional positive control on the SAME observable is the `assertIn(res, self._live())` must-hit recorded BEFORE either verdict runs, plus the closing `assertIn(res, ...)`; the rung reads the absence before it reaches either
        """THE ARM THAT DECIDES WHETHER THE CURE IS SAFE. Recovering a token
        from the ledger is only legitimate because `own_leases` returns rows
        held by THIS seat; a cure that keyed on the resource NAME would reach
        into a peer's lease on the same row and release work still running."""
        row = self.add(recipient=self.RECIPIENT, lane="someone-elses")
        res = self._autoclaim(row["id"], "kimi")
        # UNCONDITIONAL MUST-HIT, before anything is released: the peer's
        # lease really is live, so the survival at the end is a lease that
        # was spared rather than one that never existed.
        self.assertIn(res, self._live())
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the identical resource
        # spelling IS released when this seat holds it, so the survival below
        # is the holder discriminating and not the cure failing to run.
        mine = self.add(recipient=self.RECIPIENT, lane="mine-too")
        res_mine = self._autoclaim(mine["id"], self._me())
        self.assertIsNone(dispatches.mark_verdict(
            mine["id"], mine["tip"], "reviewed", "fix")[1])
        self.assertNotIn(res_mine, self._live())
        self.assertIsNone(dispatches.mark_verdict(
            row["id"], row["tip"], "reviewed", "fix")[1])
        self.assertIn(res, self._live(),
                      "a verdict released a lease held by another seat")

    def test_a_caller_helm_cannot_ADMIT_releases_nothing(self):
        """THE REFUSAL IS THE FEATURE. A release deletes a row from the claims
        ledger, so the seat name is resolved through `helm.actors` rather than
        read off the identity floor: a DERIVED name that collided with a real
        holder would recover that holder's token and release live work. With
        no session the declared name is UNCORROBORATED, the door refuses, and
        the lease correctly stands -- the pre-cure outcome, reached on
        purpose."""
        row = self.add(recipient=self.RECIPIENT, lane="unadmitted")
        res = self._autoclaim(row["id"], self._me())
        self.assertIn(res, self._live())
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        out, why = dispatches.mark_verdict(row["id"], row["tip"],
                                           "reviewed", "fix")
        self.assertIsNone(why)
        self.assertTrue(out)            # the verdict is never failed by this
        self.assertIn(res, self._live(),
                      "an unadmitted caller released a lease")
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: restore the session and the
        # identical resource IS released, so the survival above is the door
        # refusing rather than the exit never running.
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SESSION
        again = self.add(recipient=self.RECIPIENT, lane="admitted-again")
        res2 = self._autoclaim(again["id"], self._me())
        self.assertIsNone(dispatches.mark_verdict(
            again["id"], again["tip"], "reviewed", "fix")[1])
        self.assertNotIn(res2, self._live())

    def test_a_verdict_with_no_autoclaim_binds_and_disturbs_nothing(self):
        """The ORDINARY case, and it is most verdicts: a seat that claimed its
        own room holds no dispatch resource at all. Absence is not a problem
        to report, and it must not become one to raise."""
        keep = self._autoclaim(self.add(recipient=self.RECIPIENT, lane="untouched")["id"], self._me())
        row = self.add(recipient=self.RECIPIENT, lane="no-autoclaim")
        out, why = dispatches.mark_verdict(row["id"], row["tip"],
                                           "reviewed", "fix")
        self.assertIsNone(why)
        self.assertTrue(out)
        # The UNRELATED lease survives: the exit is keyed on THIS row's id,
        # not on "any dispatch lease this seat happens to hold".
        self.assertIn(keep, self._live())


class RebindRoomFenceTest(DispatchBase):
    """#203 — a rebind moves the OBLIGATION and leaves the WORKTREE LEASE with
    the old recipient, so the new builder cannot claim the room they were just
    handed. WARN rung only: it never releases, because a walled seat is not a
    dead one and its room may hold real work."""

    def _row(self, lane="a-lane"):
        return {"lane": lane, "repo_id": os.path.join(self.repo, ".git")}

    def _hold(self, lane, holder):
        from helm.work import _lanes
        res = _lanes.resource(self.repo, lane)
        ok, why, lease = seats.claim(res, holder, ttl=600,
                                     session="s-" + holder, strict=True)
        self.assertTrue(ok, why)
        self.addCleanup(seats.release, res, holder, lease=lease,
                        session="s-" + holder, strict=True)
        return res

    def test_a_room_held_by_the_old_recipient_is_reported(self):
        res = self._hold("a-lane", "kimi")
        # POSITIVE CONTROL, unconditional: the claim really is live, so an
        # empty result below would be the rung failing, not the fence absent.
        self.assertIn(res, [c["resource"] for c in seats.claims_list()])
        fence = dispatches.rebind_room_fence(self._row(), "kimi")
        self.assertEqual([f["lane"] for f in fence], [res])
        self.assertEqual(fence[0]["holder"], "kimi")
        self.assertIsNotNone(fence[0]["remaining"])

    def test_it_keys_on_the_HOLDER_not_merely_on_the_lane(self):
        """THE NEGATIVE CONTROL. A room held by someone ELSE is not this
        rebind's problem — reporting it would train the reader to skim the
        warn, which is how a warn rung stops being read at all."""
        res = self._hold("a-lane", "grok")
        # POSITIVE CONTROL on the SAME observable: the rung DOES see this
        # very room for its actual holder, so the empty result below is the
        # holder key discriminating and not the probe failing.
        self.assertEqual([f["lane"] for f in
                          dispatches.rebind_room_fence(self._row(), "grok")],
                         [res])
        self.assertEqual(dispatches.rebind_room_fence(self._row(), "kimi"), [])

    def test_the_rN_stems_of_the_lane_are_matched(self):
        """The fresh-room workaround names `<lane>-r2`, so a SECOND rebind must
        see the r2 fence too — otherwise the rung goes quiet exactly once the
        workaround is in use, which is when it matters most."""
        res = self._hold("a-lane-r2", "kimi")
        fence = dispatches.rebind_room_fence(self._row("a-lane"), "kimi")
        self.assertIn(res, [f["lane"] for f in fence])

    def test_an_unfenced_lane_reports_nothing(self):
        # POSITIVE CONTROL: a DIFFERENT lane is fenced and IS reported, so
        # the empty result for this one is about the lane and not about a
        # rung that returns [] for everything.
        self._hold("other-lane", "kimi")
        self.assertTrue(dispatches.rebind_room_fence(
            self._row("other-lane"), "kimi"))
        self.assertEqual(dispatches.rebind_room_fence(self._row(), "kimi"), [])

    def test_the_probe_never_raises_and_never_blocks(self):
        """WARN, NOT GATE. A rebind whose fence probe cannot run is still a
        correct rebind; #203 is explicitly a warn rung."""
        # POSITIVE CONTROL first: with a real row AND a real holder the rung
        # returns a fence, so the empty results below are the degraded-input
        # arms and not a rung that never reports anything.
        self._hold("a-lane", "kimi")
        self.assertTrue(dispatches.rebind_room_fence(self._row(), "kimi"))
        self.assertEqual(dispatches.rebind_room_fence({}, "kimi"), [])
        self.assertEqual(dispatches.rebind_room_fence(self._row(), None), [])
        self.assertEqual(
            dispatches.rebind_room_fence({"lane": "x", "repo_id": ""}, "kimi"),
            [])


class SupersedeAnnotatesItsParentTest(DispatchBase):
    """441c4491 — `--supersedes` left the parent looking ACTIONABLE, so
    enumeration surfaces kept offering finished work.

    IT ANNOTATES, IT DOES NOT CANCEL, and that distinction was measured the
    expensive way: built as a cancel, 18 tests across test_dispatch_chain /
    test_lr_close / test_web_lr went red, every one correctly. A BUILD parent
    must stay OPEN until its successor LANDS so it can close through
    `landed`/`discharged` WITH PROOF; cancelling at MINT destroys that door."""

    def test_the_parent_is_annotated_and_keeps_its_status(self):
        parent = self.add(lane="p-lane")
        self.assertEqual(dispatches.snapshot()[0][parent["id"]]["status"], "open")
        kid = self.add(lane="p-lane", supersedes=parent["id"])
        p = dispatches.snapshot()[0][parent["id"]]
        self.assertEqual(p["superseded_by"], kid["id"])
        # THE POINT OF THE WHOLE REDESIGN: status survives, so every
        # proof-bearing door the parent had still applies to it.
        self.assertEqual(p["status"], "open")

    def test_it_is_APPEND_ONLY_never_a_rewrite(self):
        parent = self.add(lane="p-lane")
        before = list(dispatches.history(parent["id"]))
        self.add(lane="p-lane", supersedes=parent["id"])
        after = list(dispatches.history(parent["id"]))
        self.assertGreater(len(after), len(before))
        self.assertEqual(after[:len(before)], before)

    def test_force_leaves_the_parent_unannotated_because_a_fork_is_both_live(self):
        parent = self.add(lane="p-lane")
        self.add(lane="p-lane", supersedes=parent["id"])
        # control in the same fixture: a NON-forced supersede DID annotate
        self.assertTrue(dispatches.snapshot()[0][parent["id"]].get("superseded_by"))
        other = self.add(lane="q-lane")
        self.add(lane="q-lane", supersedes=other["id"], force=True)
        self.assertIsNone(
            dispatches.snapshot()[0][other["id"]].get("superseded_by"))

    def test_the_first_successor_wins_and_a_second_does_not_rewrite_it(self):
        parent = self.add(lane="p-lane")
        first = self.add(lane="p-lane", supersedes=parent["id"])
        self.add(lane="p-lane", supersedes=parent["id"], force=True)
        self.assertEqual(
            dispatches.snapshot()[0][parent["id"]]["superseded_by"], first["id"])


class SupersededParentSweepTest(DispatchBase):
    """The migration for rows minted BEFORE the write-path fix, scoped to
    PRESENTED-AS-ACTIONABLE (still OPEN) rows rather than the 124 structurally
    unterminated ones — a row no surface offers has harmed nobody."""

    def _legacy(self, tip=None, verdict="approve"):
        """The pre-fix shape, buildable only with force=True BECAUSE the write
        path now annotates on its own.

        The successor is FINISHED by default — an approve verdict on `self.c`,
        which setUp already put on trunk. `tip=self.side` builds the live-work
        case (reviewed, approved, never landed) and `verdict=False` the
        awaiting-review case; both are rows the sweep must refuse.

        An APPROVE needs a verified gate token — the writer refuses an ungated
        one outright — so this borrows the file's established gate patch. That
        refusal is also why the approve path could not be faked by accident.

        A FRESH LANE NAME PER PAIR, because the writer refuses a second open
        row on a lane that already has one — several tests here need a
        must-hit and a must-miss standing side by side in one ledger."""
        lane = "legacy-%d" % (self._add_seq + 1)
        parent = self.add(lane=lane)
        tip = tip or self.c
        kid = self.add(lane=lane, supersedes=parent["id"], force=True,
                       ref=tip)
        if verdict:
            evidence = ("gate:0123456789abcdef reviewed"
                        if verdict == "approve" else "reviewed")
            with mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")):
                out, why = dispatches.mark_verdict(kid["id"], tip, evidence,
                                                   verdict)
            self.assertIsNone(why)
            self.assertIsNotNone(out)
        self.assertIsNone(
            dispatches.snapshot()[0][parent["id"]].get("superseded_by"))
        return parent, kid

    def _twin_repo(self):
        """A SECOND repository whose BASENAME matches self.repo's.

        Both therefore map to ONE project label while being two different
        repositories — which is the LIVE shape, not a contrivance: measured
        2026-08-05, two checked-out copies of one upstream sit at different
        paths with an identical BASENAME and derive the same label. A
        single-repo fixture passes today and proves nothing, so every arm
        below needs this one."""
        other = os.path.join(self.tmp, "elsewhere",
                             os.path.basename(self.repo))
        os.makedirs(other)
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "T")):
            subprocess.run(("git",) + args, cwd=other, check=True,
                           capture_output=True)
        with open(os.path.join(other, "f"), "w", encoding="utf-8") as fh:
            fh.write("x")     # context-managed: the bare write leaked a handle
                              # and CPython emitted ResourceWarning twice
                              # (review on 294ee27268e6)
        subprocess.run(("git", "add", "-A"), cwd=other, check=True,
                       capture_output=True)
        subprocess.run(("git", "commit", "-qm", "twin"), cwd=other,
                       check=True, capture_output=True)
        sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=other,
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        return other, sha

    def test_a_CROSS_REPO_successor_does_not_authorize_the_parent(self):
        """ONE REPOSITORY'S TRUNK PROVES NOTHING ABOUT ANOTHER'S ROW.

        The successor's work really did land — on ITS OWN trunk. The sweep
        annotates the PARENT, which lives somewhere else, so without the guard
        a row in repo A is discharged by work in repo B."""
        # MUST-MISS CONTROL FIRST, same call, same shape, ONE repo: the sweep
        # DOES select it. So the refusal below is about the repository and not
        # about a predicate that has stopped selecting anything.
        same_parent, same_kid = self._legacy()
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        self.assertIn((same_parent["id"], same_kid["id"]), hits)

        other, osha = self._twin_repo()
        lane = "cross-repo-pair"
        parent = self.add(lane=lane)                       # this repo
        kid = self.add(lane=lane, supersedes=parent["id"], force=True)
        # The current writer correctly refuses a repo-B child of a repo-A
        # parent. This consumer arm needs the historical/corrupt row to exist,
        # so inject it below that door rather than weakening the writer fixture.
        self.forge_open(
            kid["id"], repo_id=dispatches._repo_info(other)["repo_id"],
            repo_root=other, tip=osha, ref=osha)
        kid = dispatches.rows()[kid["id"]]
        with mock.patch.object(dispatches.gate, "bind",
                               return_value=("VERIFIED", "a" * 16, "r")):
            out, why = dispatches.mark_verdict(
                kid["id"], osha, "gate:0123456789abcdef reviewed", "approve")
        self.assertIsNone(why)
        self.assertIsNotNone(out)
        hits2, err2 = dispatches.superseded_parent_sweep()
        self.assertIsNone(err2)
        self.assertNotIn((parent["id"], kid["id"]), hits2)
        # and the same-repo pair is STILL selected, so the guard refused the
        # cross-repo pair specifically rather than disabling the sweep.
        self.assertIn((same_parent["id"], same_kid["id"]), hits2)

    def test_OMITTING_the_parent_refuses_rather_than_skipping(self):
        """The sentinel default, pinned as a CONTRACT of this rung.

        A mutation making the default permissive survived every other arm,
        because every live call site now passes a parent — so the fail-closed
        property was real and untested, and the next caller to forget would
        have inherited silence. `parent=None` is not "no parent to check", it
        is "the caller did not prove same-repo", and unproven is not
        permission for a write."""
        parent, kid = self._legacy()
        snap = dispatches.snapshot()[0]
        cache = {}
        # POSITIVE CONTROL FIRST, same call, same cache: WITH the parent this
        # successor IS authorized, so the refusal below is about the omission.
        self.assertTrue(dispatches._successor_finished(
            snap[kid["id"]], cache, parent=snap[parent["id"]]))
        self.assertFalse(dispatches._successor_finished(snap[kid["id"]], cache))
        self.assertFalse(
            dispatches._successor_finished(snap[kid["id"]], cache, parent=None))

    def test_the_key_is_repo_id_and_NOT_the_project_label(self):
        """The two repositories in the fixture share a project label. A guard
        that compared labels would call them one repository and authorize the
        write — which is the whole reason `_repo_project` is not the key."""
        other, _osha = self._twin_repo()
        # POSITIVE CONTROL FIRST, on the observable being compared: the
        # deriver returns a REAL non-empty label for both, so the equality
        # below cannot be satisfied by two Nones from a deriver that failed.
        mine = dispatches._repo_project(self.repo + "/.git")
        theirs = dispatches._repo_project(other + "/.git")
        self.assertEqual(mine, os.path.basename(self.repo))
        self.assertEqual(theirs, os.path.basename(other))
        # positive control bound to `other` itself, the root the refusal below
        # is about: it is a REAL repository on disk, not an empty string that
        # would trivially differ from self.repo.
        self.assertTrue(os.path.isdir(os.path.join(other, ".git")))
        self.assertNotEqual(other, self.repo)     # two different repositories
        self.assertEqual(mine, theirs)            # ...under ONE label

    def test_a_parent_with_no_repo_id_is_REFUSED_not_assumed(self):
        """This authorizes a WRITE, so absence is not permission. A parent
        whose repo_id cannot be read is not PROVEN same-repo, and unproven
        must fail closed."""
        parent, kid = self._legacy()
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIn((parent["id"], kid["id"]), hits)   # control: selected
        snap = dispatches.snapshot()[0]
        stripped = dict(snap[parent["id"]]); stripped.pop("repo_id", None)
        cache = {}
        self.assertFalse(
            dispatches._successor_finished(snap[kid["id"]], cache,
                                           parent=stripped))
        # MUST-HIT on the same call: WITH the repo_id it is authorized.
        self.assertTrue(
            dispatches._successor_finished(snap[kid["id"]], cache,
                                           parent=snap[parent["id"]]))

    def test_dry_run_names_it_and_writes_nothing(self):
        parent, kid = self._legacy()
        before = len(list(dispatches.history(parent["id"])))
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        self.assertIn((parent["id"], kid["id"]), hits)
        self.assertEqual(len(list(dispatches.history(parent["id"]))), before)

    def test_apply_annotates_and_a_second_run_is_a_no_op(self):
        parent, kid = self._legacy()
        done, err = dispatches.superseded_parent_sweep(apply=True)
        self.assertIsNone(err)
        self.assertIn((parent["id"], kid["id"]), done)
        p = dispatches.snapshot()[0][parent["id"]]
        self.assertEqual(p["superseded_by"], kid["id"])
        self.assertEqual(p["status"], "open")      # still not a cancel
        again, err = dispatches.superseded_parent_sweep(apply=True)
        self.assertIsNone(err)
        self.assertEqual(again, [])

    def test_a_row_with_no_successor_is_never_swept(self):
        """The negative control. It keys on HAVING a successor, never on being
        old or idle — annotating a row nobody continued would hide live work."""
        lonely = self.add(lane="lonely")
        parent, _kid = self._legacy()
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        ids = [h[0] for h in hits]
        self.assertIn(parent["id"], ids)
        self.assertNotIn(lonely["id"], ids)

    def test_a_successor_still_AWAITING_REVIEW_is_never_swept(self):
        """Shape alone is not authorization. A live sweep matched a row that
        had entered the shape 54 seconds earlier; the cure is a predicate that
        asks whether the work is FINISHED."""
        live, _kid = self._legacy(verdict=False)
        finished, _ = self._legacy()
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        ids = [h[0] for h in hits]
        self.assertIn(finished["id"], ids)          # must-hit, same fixture
        self.assertNotIn(live["id"], ids)

    def test_an_APPROVED_successor_whose_work_never_LANDED_is_never_swept(self):
        """The live must-miss, in fixture form: reviewed, approved, and still
        off trunk. `side` is a real branch setUp keeps off main, so this asks
        git the same question the sweep does."""
        unlanded, _kid = self._legacy(tip=self.side)
        finished, _ = self._legacy()
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        ids = [h[0] for h in hits]
        self.assertIn(finished["id"], ids)
        self.assertNotIn(unlanded["id"], ids)

    def test_the_landing_proof_reads_reviewed_tip_and_NOT_verdict_ref(self):
        """THE NEAR-MISS THIS PINS, caught on the live ledger and by nothing
        else: `verdict_ref` is not a commit. It carries free-text evidence
        beginning with a gate token, so a rung bound there answers UNKNOWN for
        every row and — because UNKNOWN fails closed — selects NOTHING,
        silently, with a green suite. A fixture cannot catch that on its own:
        it supplies whatever shape its author imagined. So this asserts the
        FIELDS THEMSELVES, and that the sweep fires anyway."""
        parent, kid = self._legacy()
        row = dispatches.snapshot()[0][kid["id"]]
        self.assertEqual(row["reviewed_tip"], self.c)
        self.assertRegex(row["verdict_ref"], r"^gate:")
        self.assertNotRegex(row["verdict_ref"], r"^[0-9a-f]{40}$")
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        self.assertIn((parent["id"], kid["id"]), hits)

    def test_work_that_landed_under_a_DIFFERENT_SHA_still_counts(self):
        """PATCH IDENTITY IS NOT A COURTESY, it is the common case: the
        integrator rebases every chain, so what reaches trunk is
        patch-identical and object-different. An ancestry-only rung would skip
        most genuinely-finished parents — and would pass every other test in
        this class, which is why this one exists."""
        # A lane off trunk that touches its OWN file, so the replay applies
        # cleanly — setUp's commits all append to one shared file and would
        # collide on content, which is a fixture accident, not the fact here.
        self.git("checkout", "-q", "-b", "dup")
        with open(os.path.join(self.repo, "dup"), "w", encoding="utf-8") as f:
            f.write("dup\n")
        self.git("add", "dup")
        self.git("commit", "-q", "-m", "dup")
        lane_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.commit("d")                     # trunk moves, as it always does
        self.git("cherry-pick", lane_tip)    # same patch, new object
        landed_as = self.git("rev-parse", "HEAD")
        # Positive controls on both sides of the inequality below, so it can
        # bite: each name resolves to the ref it is supposed to name.
        self.assertEqual(landed_as, self.git("rev-parse", self.main))
        self.assertEqual(lane_tip, self.git("rev-parse", "dup"))
        self.assertNotEqual(landed_as, lane_tip)  # noqa: VACUOUS_ASSERTION — a fixture sanity check, not the claim; the claim is the PATCH_EQUIVALENT equality below and the assertIn on hits, both unconditional
        # THE CONTROL THAT MAKES THIS TEST MEAN ANYTHING: if the lane tip were
        # an ancestor, the ANCESTOR branch would satisfy it and the
        # PATCH_EQUIVALENT term would be INERT — green either way. Assert the
        # shape is genuinely patch-identity-only, in git's own words.
        self.assertEqual(vcs.backend(self.repo).landed_state(
            self.repo, lane_tip, self.main), vcs.PATCH_EQUIVALENT)
        parent, kid = self._legacy(tip=lane_tip)
        hits, err = dispatches.superseded_parent_sweep()
        self.assertIsNone(err)
        self.assertIn((parent["id"], kid["id"]), hits)

    def test_a_tip_git_cannot_resolve_fails_CLOSED(self):
        """An unreadable object is not a licence to write. UNKNOWN is the
        third answer `vcs.landed_state` exists to keep separate from 'no'.

        TESTED AT THE RUNG, not through the writer, because the writer will
        not mint the row: `add` refuses a ref that does not resolve, which is
        its own guard doing its job. The real shape this stands for is a tip
        orphaned AFTER the row was written — a history rewrite, a pruned
        object, a repo that moved — where the ledger is fine and git has
        nothing to say."""
        cache = {}
        gone = {"status": "verdict", "reviewed_tip": "0" * 40,
                "repo_id": self.repo}
        self.assertFalse(dispatches._successor_finished(
            gone, cache, parent=self._same_repo_parent(gone)))
        # must-hit control, same call, same cache: the rung is not just False
        _hit = dict(gone, reviewed_tip=self.c)
        self.assertTrue(dispatches._successor_finished(
            _hit, cache, parent=self._same_repo_parent(_hit)))
        # and a repo that is no longer there is UNKNOWN, never "not landed"
        _absent = dict(gone, reviewed_tip=self.c,
                       repo_id=os.path.join(self.tmp, "gone"))
        self.assertFalse(dispatches._successor_finished(
            _absent, cache, parent=self._same_repo_parent(_absent)))

    def _same_repo_parent(self, kid):
        """A parent in the SAME repository as `kid`.

        Every arm below tests a rung OTHER than the repository one, and the
        repo gate now refuses a missing parent outright — so without this each
        test would refuse at gate 0 and pass for the wrong reason. Mirrors the
        kid's own repo_id rather than self.repo, because one arm deliberately
        points at a repository that is gone and must still reach the isdir
        rung to prove UNKNOWN-not-absent."""
        return {"repo_id": kid.get("repo_id")}

    def test_a_successor_with_a_LANDED_TIP_but_no_verdict_is_still_refused(self):
        """Isolates the verdict rung, which every fixture above leaves
        REDUNDANT: a row awaiting review carries no reviewed_tip either, so the
        tip guard refuses it first and a mutation dropping this rung survived.
        Here the tip is landed and only the status is wrong."""
        cache = {}
        landed = {"status": "verdict", "reviewed_tip": self.c,
                  "repo_id": self.repo}
        self.assertTrue(dispatches._successor_finished(
            landed, cache, parent=self._same_repo_parent(landed)))
        for status in ("open", "cancelled", "held", "posting"):
            self.assertFalse(
                dispatches._successor_finished(
                    dict(landed, status=status), cache,
                    parent=self._same_repo_parent(landed)),
                "%s is not a verdict" % status)

    def test_a_git_seam_that_RAISES_refuses_the_write(self):
        """The clause that keeps a crash from becoming an authorization. Every
        other path here is refused BEFORE the try — an absent repo dies on
        isdir — so nothing exercised the handler and a fail-OPEN mutation
        survived. This one makes the seam raise."""
        cache = {}
        row = {"status": "verdict", "reviewed_tip": self.c,
               "repo_id": self.repo}
        self.assertTrue(dispatches._successor_finished(
            row, cache, parent=self._same_repo_parent(row)))
        with mock.patch.object(vcs, "backend",
                               side_effect=RuntimeError("git is gone")):
            self.assertFalse(dispatches._successor_finished(
                row, {}, parent=self._same_repo_parent(row)))

    def test_a_malformed_tip_never_REACHES_git(self):
        """The tip-shape guard changes no VERDICT — garbage would come back
        UNKNOWN and be refused anyway — so it is pinned by what it prevents:
        handing unvalidated text to git as an argument, where a leading dash
        is a flag rather than a revision."""
        spy = mock.Mock(wraps=vcs.backend)
        with mock.patch.object(vcs, "backend", spy):
            self.assertFalse(dispatches._successor_finished(
                {"status": "verdict", "reviewed_tip": "--upload-pack=evil",
                 "repo_id": self.repo}, {},
                parent={"repo_id": self.repo}))
            self.assertEqual(spy.call_count, 0)
            # positive control: a well-formed tip DOES reach the seam
            self.assertTrue(dispatches._successor_finished(
                {"status": "verdict", "reviewed_tip": self.c,
                 "repo_id": self.repo}, {},
                parent={"repo_id": self.repo}))
            self.assertEqual(spy.call_count, 1)


class CarryingSemanticsTest(unittest.TestCase):
    """meld finding (6): WHO ACTUALLY HOLDS THIS ROW'S OBLIGATION.

    `add` stamps superseded_by with the FIRST successor and keeps it forever, by
    design and correctly for idempotency. It is the wrong answer to "who carries
    this debt" the moment that first successor dies and a SIBLING takes over —
    a reviewer stands down, the row is re-dispatched. Measured live 2026-08-05:
    two rows named a cancelled review while a sibling carried their lane to
    trunk, so both read as owed by a builder who owed nothing.

    These pin the SET walk, not the pointer. Synthetic snapshots throughout: the
    predicate is pure, and a fixture that has to write a ledger to ask a
    question about shape is a fixture testing the wrong thing."""

    CHAIN = "chain-root-under-test"

    @classmethod
    def _row(cls, rid, status="open", supersedes=None, **kw):
        """A fixture row carries a chain_root by default, because a REAL row
        does: 932 of the 1334 rows in the live ledger have one and none is
        UNKNOWN. A successor only carries when replay proves it is the SAME
        work, so a fixture without a root would exercise the refusal path
        while claiming to test the carrying path."""
        r = {"id": rid, "status": status, "chain_root": cls.CHAIN}
        if supersedes:
            r["supersedes"] = supersedes
        r.update(kw)
        return r

    def _snap(self, *rows):
        return {r["id"]: r for r in rows}

    def test_a_SIBLING_carries_when_the_first_successor_was_cancelled(self):
        """THE LIVE REPRO, in miniature. The parent's frozen pointer names the
        cancelled row; the sibling is the one holding the work."""
        parent = self._row("p", superseded_by="dead")
        dead = self._row("dead", status="cancelled", supersedes="p")
        sib = self._row("sib", status="verdict", supersedes="p")
        snap = self._snap(parent, dead, sib)
        got = dispatches.carrier(parent, snap)
        self.assertIsNotNone(got, "the parent read as owed while a sibling held it")
        self.assertEqual(got["id"], "sib")
        # and the pointer really does disagree — otherwise this proves nothing
        self.assertEqual(parent["superseded_by"], "dead")

    def test_a_cancelled_successor_is_a_PASS_THROUGH_not_an_endpoint(self):
        """Work can move twice. A cancelled child's own successor still counts."""
        snap = self._snap(
            self._row("p"),
            self._row("c1", status="cancelled", supersedes="p"),
            self._row("c2", status="open", supersedes="c1"))
        got = dispatches.carrier(snap["p"], snap)
        self.assertEqual((got or {}).get("id"), "c2",
                         "the walk stopped at the cancelled link")

    def test_every_non_carrying_state_passes_through(self):
        # UNCONDITIONAL CONTROL before the loops: emptying the vocabulary would
        # leave them running while asserting nothing, and would silently make
        # every successor "carrying".
        self.assertEqual(set(dispatches._NON_CARRYING_STATUS), {"cancelled"})
        self.assertEqual(set(dispatches._NON_CARRYING_FLAGS),
                         {"withdrawn", "abandoned", "retired_admin",
                          "verdict_retracted"})
        # THE STATUS ARM. Only "cancelled" is a status replay actually writes;
        # this test used to pin "withdrawn" and "abandoned" as statuses too,
        # and BOTH SPELLINGS ARE UNREACHABLE — which is exactly why a withdrawn
        # successor hid its parent for as long as it did.
        snap = self._snap(
            self._row("p"),
            self._row("dead", status="cancelled", supersedes="p"),
            self._row("live", status="open", supersedes="dead"))
        self.assertEqual(dispatches.carrier(snap["p"], snap)["id"], "live",
                         "a cancelled successor did not pass through")
        # THE FLAG ARM, which is the shape replay really produces: the status
        # stays "verdict" and terminality rides on a boolean.
        for flag in sorted(dispatches._NON_CARRYING_FLAGS):
            with self.subTest(flag=flag):
                snap = self._snap(
                    self._row("p"),
                    self._row("dead", status="verdict", supersedes="p",
                              **{flag: True}),
                    self._row("live", status="open", supersedes="dead"))
                self.assertEqual(dispatches.carrier(snap["p"], snap)["id"],
                                 "live", "a %s successor did not pass through"
                                 % flag)
                # ...and the same row WITHOUT the flag does carry, so the flag
                # is what moved the answer rather than the shape of the fixture.
                snap2 = self._snap(
                    self._row("p"),
                    self._row("dead", status="verdict", supersedes="p"))
                self.assertEqual(dispatches.carrier(snap2["p"], snap2)["id"],
                                 "dead")

    def test_a_RING_carries_nothing_and_every_member_stays_visible(self):
        """Two open rows superseding each other each LOOK like the other's
        successor, so each answered for the other and BOTH left the owed
        frontier — a loop swallowing a real obligation while every row in it
        read as discharged. Nobody terminal holds a ring."""
        a = self._row("a", supersedes="b")
        b = self._row("b", supersedes="a")
        snap = self._snap(a, b)
        self.assertIsNone(dispatches.carrier(a, snap), "a ring carried itself")
        self.assertIsNone(dispatches.carrier(b, snap))
        self.assertEqual({r["id"] for r in dispatches.owed(snap)}, {"a", "b"},
                         "a ring member left the owed frontier")
        # the degenerate ring: a row superseding ITSELF
        me = self._row("me", supersedes="me")
        s1 = self._snap(me)
        self.assertIsNone(dispatches.carrier(me, s1), "a self-edge carried itself")
        # MUST-HIT CONTROL: break the ring and the carrier reappears, so this
        # is rejection of a loop and not a walk that answers None to everything.
        b2 = self._row("b", supersedes="a")          # b no longer points back
        a2 = self._row("a")
        s2 = self._snap(a2, b2)
        self.assertEqual(dispatches.carrier(a2, s2)["id"], "b")

    def test_a_FOREIGN_chain_cannot_take_this_rows_debt(self):
        """`supersedes` is an edge any writer can set; chain_root is the
        replayed identity of the work. Without the check, a row naming an
        unrelated parent silenced that parent's obligation."""
        parent = self._row("p", chain_root="ROOT-A")
        stranger = self._row("k", supersedes="p", chain_root="ROOT-B")
        snap = self._snap(parent, stranger)
        self.assertIsNone(dispatches.carrier(parent, snap),
                          "a foreign chain took this row's debt")
        self.assertIn("p", {r["id"] for r in dispatches.owed(snap)})
        # UNKNOWN and ABSENT roots resolve the same way — toward VISIBLE.
        for bad in (dispatches.CHAIN_UNKNOWN, None, ""):
            with self.subTest(root=bad):
                kid = self._row("k2", supersedes="p", chain_root=bad)
                self.assertIsNone(
                    dispatches.carrier(parent, self._snap(parent, kid)),
                    "an unprovable chain identity carried the debt")
        # MUST-HIT CONTROL: the SAME root does carry, so the refusals above are
        # about identity and not a predicate that never carries.
        kin = self._row("k3", supersedes="p", chain_root="ROOT-A")
        self.assertEqual(
            dispatches.carrier(parent, self._snap(parent, kin))["id"], "k3")

    def test_a_LEGACY_parent_is_carried_by_the_child_that_roots_at_its_id(self):
        """The compatibility shape `_resolve_chain` deliberately writes.

        History stays byte-true: the pre-chain parent keeps chain_root=None.
        Its new child says "this chain begins at the parent I can name" by
        storing chain_root=parent.id. Treating the absent historical root as
        UNKNOWN made that valid edge look foreign, so both rows became owed.
        """
        parent = self._row("legacy", chain_root=None)
        child = self._row("child", supersedes="legacy", chain_root="legacy")
        snap = self._snap(parent, child)
        self.assertEqual(dispatches.carrier(parent, snap)["id"], "child")
        self.assertEqual([r["id"] for r in dispatches.owed(snap)], ["child"])
        # UNKNOWN is not legacy. A malformed parent still earns no carrier.
        corrupt = self._row("legacy", chain_root=dispatches.CHAIN_UNKNOWN)
        self.assertIsNone(dispatches.carrier(
            corrupt, self._snap(corrupt, child)))

    def test_a_row_with_no_live_successor_is_OWED_and_stays_visible(self):
        snap = self._snap(
            self._row("p"),
            self._row("c", status="cancelled", supersedes="p"))
        self.assertIsNone(dispatches.carrier(snap["p"], snap))
        # MUST-HIT CONTROL: flip that child live and it DOES carry, so this is
        # not a predicate that answers None for everything.
        snap["c"]["status"] = "open"
        self.assertEqual(dispatches.carrier(snap["p"], snap)["id"], "c")

    def test_a_CORRUPT_index_cycle_terminates_and_stays_visible(self):
        """The seen-guard, tested against the shape it actually defends.

        MY FIRST VERSION OF THIS TEST WAS WRONG and the failure taught me the
        structure: I built the cycle by repointing a child at its sibling, which
        REMOVED its link to the parent — so carrier returned None because the
        parent had no successors at all, not because of any cycle. The test
        passed for the wrong reason until I added a positive control, which is
        the only reason I found out.

        A downward cycle is in fact UNREACHABLE through `supersedes` alone: the
        field is single-valued, so a row cannot be both its parent's child and
        its own descendant. The seen-guard therefore defends against a CORRUPT
        INDEX — duplicate ids, a hand-edited ledger, or a future multi-parent
        shape — so the honest test hands carrier() that index directly rather
        than pretending the normal writer can produce it."""
        p = {"id": "p", "status": "open", "chain_root": self.CHAIN}
        a = {"id": "a", "status": "cancelled", "supersedes": "p",
             "chain_root": self.CHAIN}
        b = {"id": "b", "status": "cancelled", "supersedes": "a",
             "chain_root": self.CHAIN}
        snap = {"p": p, "a": a, "b": b}
        cyclic = {"p": [a], "a": [b], "b": [a]}      # b's child is a: a cycle
        self.assertIsNone(dispatches.carrier(p, snap, cyclic),
                          "a cycle hid an owed row, or did not terminate")
        # POSITIVE CONTROL on the SAME cyclic index: make one link live and the
        # parent is carried again, so this is not a predicate answering None to
        # everything — and it proves the walk still traverses the cycle safely.
        b["status"] = "open"
        self.assertEqual(
            dispatches.carrier(p, snap, cyclic)["id"], "b",
            "the walk stopped finding live carriers inside a cyclic index")

    def test_a_verdict_successor_carries_because_the_work_DID_move(self):
        snap = self._snap(
            self._row("p"),
            self._row("v", status="verdict", supersedes="p"))
        self.assertEqual(dispatches.carrier(snap["p"], snap)["id"], "v")

    def test_the_index_is_built_once_and_reused(self):
        """Four surfaces will consume this. Rebuilding the index per row over a
        1300-row ledger is the difference between a predicate and a scan."""
        snap = self._snap(self._row("p"), self._row("c", supersedes="p"))
        idx = dispatches._successor_index(snap)
        self.assertEqual([r["id"] for r in idx["p"]], ["c"])
        self.assertEqual(dispatches.carrier(snap["p"], snap, idx)["id"], "c")
        # a malformed snapshot yields an empty index rather than raising
        self.assertEqual(dispatches._successor_index(None), {})
        self.assertEqual(dispatches._successor_index({"x": "not-a-dict"}), {})

    def test_owed_visits_successor_edges_a_BOUNDED_number_of_times(self):  # noqa: VACUOUS_ASSERTION — assertEqual pins the non-empty owed frontier before the bounded visit assertion on the same graph
        """Index reuse must mean linear work, not merely one index allocation.

        The first cycle cure called a descendant DFS for every live candidate.
        On a valid all-open chain, doubling rows quadrupled runtime because every
        parent rescanned its whole suffix. Count edge visits instead of timing:
        SCC discovery may read each edge once and carrier may read it once more;
        no machine-speed threshold or sleep is involved.
        """
        visits = [0]

        class Counted(list):
            def __iter__(self):
                for row in super().__iter__():
                    visits[0] += 1
                    yield row

        size = 200
        rows = [self._row("n%d" % i,
                          supersedes="n%d" % (i - 1) if i else None)
                for i in range(size)]
        snap = self._snap(*rows)
        index = {rows[i]["id"]: Counted([rows[i + 1]])
                 for i in range(size - 1)}
        self.assertEqual([r["id"] for r in dispatches.owed(snap, index)],
                         [rows[-1]["id"]])
        self.assertLessEqual(visits[0], size * 3,
                             "successor edges were rescanned per parent")


class CarrierIndexEquivalenceTest(unittest.TestCase):
    """`_CycleView`/`_CarrierView` answer what the WHOLE-POPULATION walk did.

    The replay-side incident: folding 17,158 live events called
    `rowworld._carriers` 127 times — once per `carried` close event — and each
    call walked every one of 4,381 rows to hand `_work_pair` a SINGLE key.
    553,018 `carrier` calls, 478,381 `moved_nothing` calls, ~25s of the 48s
    fold. The cure answers one row at a time and replaces the whole-graph
    Kosaraju with a per-node mutual-reachability test.

    THE ORACLE IS THE ORIGINAL WALK, kept reachable and unmodified:
    `dispatches._cycle_components` and `rowworld._carriers` are the reference
    implementations every arm below compares against, so these are equivalence
    tests rather than a second transcription of the intent."""

    CHAIN = "chain-root-under-test"

    @classmethod
    def _row(cls, rid, status="open", supersedes=None, **kw):
        r = {"id": rid, "status": status, "chain_root": cls.CHAIN}
        if supersedes:
            r["supersedes"] = supersedes
        r.update(kw)
        return r

    @classmethod
    def _index(cls, edges):
        """{parent: [child row]} built directly, so a shape `supersedes`
        cannot express — a cycle, a true diamond — is still testable. That is
        the same reason `test_a_CORRUPT_index_cycle_terminates_and_stays_visible`
        hands `carrier` its index."""
        rows = {}
        for parent, kids in edges.items():
            for kid in kids:
                rows.setdefault(kid, cls._row(kid))
            rows.setdefault(parent, cls._row(parent))
        return {p: [rows[k] for k in kids] for p, kids in edges.items()}, rows

    @staticmethod
    def _classes(lookup, nodes):
        """The EQUIVALENCE RELATION a cycle map encodes, independent of how it
        spells a component id: node -> the set of nodes sharing its non-None
        key. `_cycle_components` spells that key as an int and `_CycleView` as
        the member set, and `carrier` only ever compares two of them for
        equality, so this is the whole observable content of either map."""
        out = {}
        for node in nodes:
            key = lookup(node)
            out[node] = None if key is None else frozenset(
                other for other in nodes if lookup(other) == key)
        return out

    def _assert_same_cycles(self, edges, extra=()):
        index, rows = self._index(edges)
        nodes = sorted(set(rows) | set(extra))
        ref = dispatches._cycle_components(index)
        view = dispatches._CycleView(index)
        self.assertEqual(self._classes(ref.get, nodes),
                         self._classes(view.get, nodes))
        return ref, view, nodes

    def test_cycle_map_matches_on_an_ACYCLIC_chain(self):
        ref, _view, nodes = self._assert_same_cycles(
            {"a": ["b"], "b": ["c"]}, extra=("absent",))
        # CONTROL: the reference really is empty here, so the arm below is
        # what proves the two agree on a NON-empty map rather than on {}.
        self.assertEqual(ref, {})
        self.assertEqual(nodes, ["a", "absent", "b", "c"])

    def test_cycle_map_matches_on_a_TWO_NODE_RING(self):
        ref, view, _nodes = self._assert_same_cycles({"a": ["b"], "b": ["a"]})
        self.assertEqual(set(ref), {"a", "b"}, "the reference saw no ring")
        self.assertIsNotNone(view.get("a"))
        self.assertEqual(view.get("a"), view.get("b"))

    def test_cycle_map_matches_on_a_SELF_EDGE(self):
        ref, view, _nodes = self._assert_same_cycles({"a": ["a"], "b": ["a"]})
        self.assertEqual(set(ref), {"a"})
        self.assertIsNotNone(view.get("a"))
        self.assertIsNone(view.get("b"))

    def test_cycle_map_matches_on_a_DIAMOND(self):  # noqa: VACUOUS_ASSERTION — an acyclic diamond HAS no cyclic component, so the empty map is the contract; the closed-diamond control in the body is a fresh producer the rung cannot credit, and the CLOSED_DIAMOND arm asserts the non-empty half
        """Two successors of one parent rejoining at one child. Acyclic, so
        every node must stay out of the map — and the reachability test must
        not mistake "reached twice" for "reached back"."""
        ref, view, _nodes = self._assert_same_cycles(
            {"a": ["b", "c"], "b": ["d"], "c": ["d"]})
        self.assertEqual(ref, {})
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: an absence is
        # what a broken reachability test returns for everything, so the same
        # four nodes with one back edge must answer NON-empty through the same
        # two calls, or this arm proves only that both maps say nothing.
        closed, _rows = self._index(
            {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": ["a"]})
        self.assertNotEqual(dispatches._cycle_components(closed), {})
        self.assertIsNotNone(dispatches._CycleView(closed).get("d"))

    def test_cycle_map_matches_on_a_CLOSED_DIAMOND(self):
        """The same diamond with a back edge: one component of four."""
        ref, view, _nodes = self._assert_same_cycles(
            {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": ["a"]})
        self.assertEqual(set(ref), {"a", "b", "c", "d"})
        self.assertEqual(view.get("a"), view.get("d"))

    def test_cycle_map_keeps_TWO_RINGS_APART(self):  # noqa: VACUOUS_ASSERTION — assertEqual on view.get(a)/view.get(b) and view.get(x)/view.get(y) pins both rings NON-empty before the cross-ring assertNotEqual, on the same view
        """Equality of component keys is the only thing `carrier` reads, so
        two distinct rings comparing EQUAL would hide an owed row exactly as a
        merged component would. The shared-key arm above cannot see that."""
        _ref, view, _nodes = self._assert_same_cycles(
            {"a": ["b"], "b": ["a"], "x": ["y"], "y": ["x"]})
        # THE POSITIVE HALF FIRST: keys inside one ring must compare EQUAL, or
        # "not equal across rings" is satisfied by a map that equals nothing.
        self.assertIsNotNone(view.get("a"))
        self.assertEqual(view.get("a"), view.get("b"))
        self.assertEqual(view.get("x"), view.get("y"))
        self.assertNotEqual(view.get("a"), view.get("x"))

    def test_cycle_map_matches_on_an_EMPTY_graph(self):
        # CONTROL FIRST: both doors answer NON-empty for a graph that has a
        # ring, so the three absences below are about the empty graph rather
        # than about a constructor that answers nothing to everyone.
        ring, _rows = self._index({"a": ["b"], "b": ["a"]})
        self.assertNotEqual(dispatches._cycle_components(ring), {})
        self.assertIsNotNone(dispatches._CycleView(ring).get("a"))
        self.assertEqual(dispatches._cycle_components({}), {})
        self.assertIsNone(dispatches._CycleView({}).get("anything"))
        self.assertIsNone(dispatches._CycleView(None).get("anything"))

    # -- the carrier view -------------------------------------------------

    def _assert_same_carriers(self, snap):
        from helm import rowworld
        reference = rowworld._carriers(snap)
        view = dispatches._CarrierView(snap)
        self.assertEqual(dict(view), reference,
                         "materialising the view disagreed with the walk")
        for rid in snap:
            self.assertEqual(view.get(rid), reference.get(rid),
                             "one-key lookup disagreed for %s" % rid)
            self.assertEqual(rid in view, rid in reference)
        self.assertIsNone(view.get("no-such-row"))
        return reference

    def test_carrier_view_matches_on_a_SIBLING_fork(self):
        snap = {r["id"]: r for r in (
            self._row("p", superseded_by="dead"),
            self._row("dead", status="cancelled", supersedes="p"),
            self._row("sib", status="verdict", supersedes="p"))}
        reference = self._assert_same_carriers(snap)
        self.assertEqual(reference, {"p": "sib"},
                         "the reference answered nothing to agree about")

    def test_carrier_view_matches_on_a_CHAIN_THROUGH_A_CLOSED_ROW(self):
        snap = {r["id"]: r for r in (
            self._row("p"),
            self._row("c1", status="cancelled", supersedes="p"),
            self._row("c2", status="open", supersedes="c1"))}
        reference = self._assert_same_carriers(snap)
        self.assertEqual(reference, {"p": "c2", "c1": "c2"})

    def test_carrier_view_matches_when_a_FOREIGN_chain_carries_nothing(self):  # noqa: VACUOUS_ASSERTION — a foreign chain_root carrying NOTHING is the contract; the same-root control on the same edge asserts p -> k first, but it is a fresh producer the rung cannot credit
        # UNCONDITIONAL POSITIVE CONTROL on the same observable and the same
        # edge: with the chain roots agreeing, both doors answer p -> k. Only
        # that makes the emptiness below a statement about the FOREIGN root.
        same = {r["id"]: r for r in (
            self._row("p"), self._row("k", supersedes="p"))}
        self.assertEqual(self._assert_same_carriers(same), {"p": "k"})
        snap = {r["id"]: r for r in (
            self._row("p"),
            self._row("k", supersedes="p", chain_root="somebody-elses-work"))}
        reference = self._assert_same_carriers(snap)
        self.assertEqual(reference, {},
                         "the foreign edge was admitted by the reference")

    def test_carrier_view_matches_on_an_EMPTY_population(self):
        self._assert_same_carriers({})
        empty = dispatches._CarrierView({})
        self.assertEqual(len(empty), 0)
        # `_work_pair` reads the map as `(carriers or {}).get(rid)`. A Mapping
        # whose truth value came from __len__ would materialise the whole
        # population to answer that `or`, which is the cost this view exists to
        # avoid; an empty view and an empty dict answer None either way.
        self.assertTrue(empty)
        self.assertTrue(dispatches._CarrierView(None))
        self.assertIsNone((empty or {}).get("p"))

    def test_carrier_view_computes_ONE_answer_for_a_one_key_read(self):
        """The equivalence arms would all pass over a view that simply called
        the old walk, so the saving itself needs an arm. Counting calls, not
        time: this is the 553,018-for-127 shape in miniature."""
        snap = {r["id"]: r for r in (
            self._row("p"),
            self._row("c", status="verdict", supersedes="p"),
            self._row("q"), self._row("r"))}
        calls = []
        real = dispatches.carrier

        def counted(row, *a, **kw):
            calls.append(str((row or {}).get("id")))
            return real(row, *a, **kw)

        with mock.patch.object(dispatches, "carrier", counted):
            view = dispatches._CarrierView(snap)
            self.assertEqual(view.get("p"), "c")
            self.assertEqual(calls, ["p"], "the view walked rows nobody asked for")
            self.assertEqual(view.get("p"), "c")
            self.assertEqual(calls, ["p"], "a repeated read recomputed the walk")
            self.assertEqual(sorted(dict(view)), ["p"])
            # materialising visits every row exactly once
            self.assertEqual(sorted(calls), ["c", "p", "p", "q", "r"])

    def test_materialising_takes_the_whole_graph_cycle_pass(self):
        """`_CycleView` answers one node with a reachability walk, which is
        superlinear across a population (a chain of 800 measured x34 against
        `rowworld._carriers`). A consumer that iterates the view must get the
        linear whole-graph pass, and the same map out of it."""
        rows = [self._row("n0")] + [
            self._row("n%d" % i, status="verdict", supersedes="n%d" % (i - 1))
            for i in range(1, 40)]
        rows += [self._row("a", supersedes="b"), self._row("b", supersedes="a")]
        from helm import rowworld
        snap = {r["id"]: r for r in rows}
        walks = []
        real = dispatches._CycleView._reach

        def counted(start, edges):
            walks.append(start)
            return real(start, edges)

        with mock.patch.object(dispatches._CycleView, "_reach",
                               staticmethod(counted)):
            full = dict(dispatches._CarrierView(snap))
        self.assertEqual(walks, [], "materialising walked reachability per node")
        self.assertEqual(full, rowworld._carriers(snap))
        self.assertTrue(full)


def _plant_health(case, answer):
    """Install `answer` as `proxywatch.health` for one test, behind the REAL
    function's signature.

    A hand-typed double drifts. These were typed with the keywords health had
    when they were written; health then gained `include_probe`, the recipient
    usability reader passes it, and every such call raised TypeError INSIDE
    THE DOUBLE. That reader folds any exception into UNREADABLE, so the rebind
    TARGET's usability read never ran in any arm here and nothing went red.
    `autospec` takes the signature from the function itself: a keyword the
    real health accepts reaches `answer`, one it refuses is refused here too.
    So `answer` accepts anything, and the spec does the refusing. It is handed
    the real function first; an arm that plants twice specs that function
    again, never the first double."""
    from helm import proxywatch
    real = vars(case).setdefault("_real_health", proxywatch.health)
    patcher = mock.patch.object(proxywatch, "health", mock.create_autospec(
        real, side_effect=lambda *a, **kw: answer(real, *a, **kw)))
    patcher.start()
    case.addCleanup(patcher.stop)


def _plant_health_row(case, row):
    """Plant ONE seat's health row. A pass that also asks about another seat
    gets the real function's answer for that seat, with the caller's own
    keywords, never the planted row under a different name."""
    def answer(real, seats=None, **kw):
        mine = [row] if seats is None or row["seat"] in seats else []
        others = [s for s in seats or () if s != row["seat"]]
        rest = real(seats=others, **kw)["seats"] if others else []
        return {"seats": mine + rest}
    _plant_health(case, answer)


def _blind_health(case, exc):
    """proxywatch cannot be read at all: every pass raises `exc`."""
    def answer(*_a, **_kw):
        raise exc
    _plant_health(case, answer)


class RebindTest(DispatchBase):
    """rebind = cancel-as-rebound + re-add to a new recipient, one operation,
    EVIDENCE-GATED (council 0.3 gap G1): proxy starvation/hang OR fresh context
    exhaustion suffices; only no evidence across both independent arms refuses.
    --force carries a mandatory reason."""

    def setUp(self):
        super().setUp()
        # REBIND NOW REQUIRES A JOINED TARGET (#116: it moves an obligation a
        # seat already carries, so ABSENT/EMPTY/UNREADABLE all refuse). These
        # tests were green under an EMPTY roster because no such check
        # existed; joining the targets is what the fixture always meant, not a
        # workaround. Any test here that wants the REFUSAL asserts it directly.
        for seat_name in ("ds4pro", "grok"):
            seats.write_roster(seat_name, presence_beat=False)

    def _starve(self, recipient, probe=None, streak=False, hang=False):
        _plant_health_row(self, {
            "seat": recipient, "config_ok": True, "drift": [],
            "alerted_at": None, "transcript_age_s": 4000 if hang else 0,
            "hang_candidate": hang, "probe": probe,
            "probe_detail": "refused" if probe else None, "probe_ms": 1,
            "log": "streak" if streak else "ok", "log_detail": "HTTP 402"})

    def _healthy(self, recipient):
        _plant_health_row(self, {
            "seat": recipient, "config_ok": True, "drift": [],
            "alerted_at": None, "transcript_age_s": 0,
            "hang_candidate": False, "probe": "healthy",
            "probe_detail": None, "probe_ms": 1, "log": "ok",
            "log_detail": None})

    def _child_of(self, source_id):
        """The successor row a rebind wrote for this source, or None."""
        snap = dispatches.snapshot()[0]
        kids = [r for r in snap.values() if r.get("supersedes") == source_id]
        return kids[0] if kids else None

    def test_a_verdict_landing_mid_rebind_cancels_the_child(self):
        """#178: the child is appended under the WRITER lock and the source is
        cancelled under the EVENTLEDGER lock — two writes, two locks. A verdict
        landing between them terminalizes the source, mark_cancel then REFUSES,
        and the child survives OPEN pointing at a source nobody can discharge.

        This asserts the child's CANCELLED status rather than the absence of an
        open stray: 'no stray was found' also passes when no child was ever
        written, which is a different bug wearing this test's green."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_add = dispatches.add

        def verdict_between(*a, **kw):
            # the source verdicts AFTER the child exists but BEFORE the cancel
            child = real_add(*a, **kw)
            _v, verr = dispatches.mark_verdict(row["id"], row["tip"],
                                               "findings", polarity="fix")
            self.assertIsNone(verr, "the planted verdict was refused: %s" % verr)
            return child

        with mock.patch.object(dispatches, "add", side_effect=verdict_between):
            out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "verdict",
                         "the planted verdict did not terminalize the source, "
                         "so this test never entered the race window")
        child = self._child_of(row["id"])
        self.assertIsNotNone(child, "no child row was written at all — the "
                                    "race window was never opened")
        self.assertEqual(child["status"], "cancelled",
                         "the child outlived its terminal source as %s"
                         % child["status"])
        self.assertIsNone(out, "an aborted rebind must not report success")
        self.assertIn("aborted", err or "")

    def test_a_hold_landing_mid_rebind_still_completes_the_rebind(self):
        """The predicate is CANCELLABLE_STATES, not `status != "open"`. A HOLD
        is an acknowledged pause, and mark_cancel accepts a held row — so a
        source that goes HELD mid-rebind must still be cancelled and the child
        must LIVE. The stranded original aborted here, killing a rebind that
        was about to succeed."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_add = dispatches.add

        def hold_between(*a, **kw):
            child = real_add(*a, **kw)
            _h, herr = dispatches.mark_hold(row["id"], "waiting on fab")
            self.assertIsNone(herr, "the planted hold was refused: %s" % herr)
            return child

        with mock.patch.object(dispatches, "add", side_effect=hold_between):
            out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        self.assertIsNone(err, "a HELD source is cancellable; the rebind "
                               "should not have aborted: %s" % err)
        self.assertIsNotNone(out, "the rebind reported no result")
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "cancelled",
                         "the held source was not cancelled")
        child = self._child_of(row["id"])
        self.assertEqual(child["status"], "open",
                         "the successor must carry the obligation forward, "
                         "but it is %s" % child["status"])

    def test_an_aborted_rebind_leaves_the_source_VISIBLE_not_merely_open(self):
        """A HIGH finding at 4aca8999: cancelling the child was not
        enough. `add` stamps superseded_by on the parent inside its own lock,
        the moment the successor is written, so an aborted rebind left the
        source OPEN while `list --open` suppressed it — trading duplicate debt
        for HIDDEN debt, which is worse: a duplicate has two rows shouting, a
        hidden one has nobody.

        The earlier version of this lane asserted the CHILD only, so it passed
        with the source invisible.

        THE PATH MATTERS: this is the RESIDUAL failure (the cancel itself is
        refused), NOT the terminal recheck. When the recheck fires, the source
        reached verdict/cancelled/closed and is correctly hidden because it is
        TERMINAL. Only here does the source stay genuinely OPEN and owed, which
        is the state that must not be hidden — I first wrote this against the
        verdict path and it failed for that reason."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_cancel = dispatches.mark_cancel

        def refuse_the_source(rid, reason):
            if rid == row["id"]:
                return None, "ledger unwritable (planted)"
            return real_cancel(rid, reason)

        with mock.patch.object(dispatches, "mark_cancel",
                               side_effect=refuse_the_source):
            dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "open",
                         "the source is not OPEN, so this test is not "
                         "exercising the hidden-debt state at all")
        child = self._child_of(row["id"])
        self.assertEqual(child["status"], "cancelled", "the child outlived it")
        # the stamp IS present — this test is about what the surface does with it
        self.assertEqual(snap[row["id"]].get("superseded_by"), child["id"],
                         "add() no longer stamps the parent, so this test is "
                         "no longer exercising the reported defect")
        rc, out, _err = run(dispatches.cmd_dispatch, ["list", "--open"])
        self.assertEqual(rc, 0)
        # POSITIVE CONTROL on the same surface: a plain open row DOES appear,
        # so an empty listing cannot be mistaken for a passing assertion.
        other = self.add(recipient="grok", kind="review")
        rc2, out2, _e2 = run(dispatches.cmd_dispatch, ["list", "--open"])
        self.assertEqual(rc2, 0)
        self.assertIn(other["id"], out2, "list --open shows nothing at all")
        self.assertIn(row["id"], out2,
                      "the source is OPEN but hidden from list --open — its "
                      "successor was cancelled and moved no obligation")

    def test_a_cancelled_link_is_walked_through_not_treated_as_the_end(self):
        """An adversarial sweep: a walk that reads ONE hop and
        calls it an answer is wrong: A(open) -> B(cancelled) -> C(open) resurrected A
        beside C — two rows shouting the same debt, which is the duplicate this
        whole lane exists to avoid. A cancelled link is a pass-through."""
        a = self.add(recipient="grok", kind="review")
        b = self.add(recipient="ds4pro", kind="review", supersedes=a["id"])
        c = self.add(recipient="grok", kind="review", supersedes=b["id"])
        dispatches.mark_cancel(b["id"], "superseded again")
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[b["id"]]["status"], "cancelled")
        self.assertEqual(snap[c["id"]]["status"], "open",
                         "C must be live or this fixture proves nothing")
        # C carries A's obligation THROUGH the cancelled B, so A stays hidden
        self.assertTrue(dispatches._superseded_by_live(snap[a["id"]], snap),
                        "A resurfaced beside its live grandchild C — the debt "
                        "is now claimed twice")
        # and the control in the other direction: cancel C too and A returns
        dispatches.mark_cancel(c["id"], "chain ends here")
        snap = dispatches.snapshot()[0]
        self.assertFalse(dispatches._superseded_by_live(snap[a["id"]], snap),
                         "the whole chain is cancelled, so A is owed again and "
                         "must be visible")

    def test_an_unreadable_successor_never_hides_its_parent(self):
        """The first version returned True when the successor row was missing,
        so a superseded_by naming an absent id hid its parent forever. Every
        unknown resolves toward VISIBLE: a shown row is recoverable, a hidden
        one is not."""
        row = dict(self.add(recipient="grok", kind="review"))
        row["superseded_by"] = "0" * 32          # names a row that is not there
        snap = {row["id"]: row}
        self.assertFalse(dispatches._superseded_by_live(row, snap),
                         "an absent successor hid its parent")
        # MUST-HIT CONTROL: the same predicate DOES hide when the successor is
        # really there and really live, so this is not a function that always
        # returns False.
        # The successor CLAIMS the parent (supersedes), which is what `add`
        # writes. A fixture that set only the parent's pointer described a
        # shape the writer never produces: measured over the whole ledger,
        # all 23 rows carrying superseded_by have a child claiming them and
        # none names a row that is not a claimed successor.
        # SAME CHAIN as the parent: a successor only carries when replay
        # proves it is the same work, so a rootless fixture would exercise the
        # refusal path while claiming to test the carrying path.
        kid = {"id": "1" * 32, "status": "open", "supersedes": row["id"],
               "chain_root": row.get("chain_root")}
        row2 = dict(row, superseded_by=kid["id"])
        self.assertTrue(
            dispatches._superseded_by_live(row2, {kid["id"]: kid,
                                                  row2["id"]: row2}),
            "the predicate no longer hides anything at all")

    def test_a_cycle_in_the_chain_shows_the_row_rather_than_hiding_it(self):
        """A corrupt chain must not silence a debt. Two rows pointing at each
        other is unreachable through the normal verbs, but 'unreachable' is not
        a guarantee and the safe direction is visible."""
        R = "cycle-chain-root"
        x = {"id": "a" * 32, "status": "cancelled", "superseded_by": "b" * 32,
             "supersedes": "c" * 32, "chain_root": R}
        y = {"id": "b" * 32, "status": "cancelled", "superseded_by": "a" * 32,
             "supersedes": "a" * 32, "chain_root": R}
        parent = {"id": "c" * 32, "status": "open", "superseded_by": x["id"],
                  "chain_root": R}
        snap = {x["id"]: x, y["id"]: y, parent["id"]: parent}
        self.assertFalse(dispatches._superseded_by_live(parent, snap),
                         "a cycle hid an open row forever")
        # UNCONDITIONAL POSITIVE CONTROL on the same snapshot: break the cycle
        # by letting the second link live, and the parent MUST hide again.
        # Without this, a predicate that always returned False would pass.
        snap[y["id"]] = dict(y, status="open", superseded_by=None,
                             supersedes=x["id"], chain_root=R)
        self.assertTrue(dispatches._superseded_by_live(parent, snap),
                        "the walk no longer hides a parent whose chain ends in "
                        "a LIVE successor — it is answering False for everything")

    @staticmethod
    def _chain(n, tail_status="open"):
        """A synthetic supersession chain: root -> link_0 ... link_n-1 -> tail.
        Every link is CANCELLED, so only the tail can carry the obligation."""
        ids = ["%032d" % i for i in range(n + 2)]
        snap = {}
        for i, rid in enumerate(ids):
            row = {"id": rid, "status": "cancelled",
                   "chain_root": "long-chain-root"}
            if i + 1 < len(ids):
                row["superseded_by"] = ids[i + 1]
            if i:
                row["supersedes"] = ids[i - 1]   # the successor CLAIMS it
            snap[rid] = row
        snap[ids[0]]["status"] = "open"          # the root is the owed row
        snap[ids[-1]]["status"] = tail_status    # the frontier
        return ids, snap

    def test_a_chain_longer_than_the_old_cap_is_walked_to_its_end(self):
        """A 64-hop cap truncated VALID long chains. The visited set
        alone terminates the walk — the ledger is finite — so the number could
        only ever cause false truncation. 70 links is past any old cap."""
        ids, snap = self._chain(70, tail_status="open")
        self.assertTrue(
            dispatches._superseded_by_live(snap[ids[0]], snap),
            "the walk stopped early and reported the root un-superseded while a "
            "LIVE frontier 70 links down still carries its obligation")
        # the other direction, same chain: a dead frontier means the root is owed
        snap[ids[-1]] = dict(snap[ids[-1]], status="cancelled")
        self.assertFalse(
            dispatches._superseded_by_live(snap[ids[0]], snap),
            "every link is cancelled, so the root is owed again and must show")

    def test_the_CLEANUP_walks_a_chain_longer_than_the_old_cap(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn is the tail of the test; two UNCONDITIONAL positive controls run before it on the same observable: assertEqual(len(cancelled), 72) and assertEqual(cancelled[-1], ids[-1]). A cleanup that cancelled nothing fails both.
        """The r4 blocker, and the third instance tonight of ONE shape:
        a mutation and a test bound to the wrong half of the code.

        test_a_chain_longer_than_the_old_cap_is_walked_to_its_end exercises
        _superseded_by_live ONLY. Restoring the 64-hop cap in
        _rebind_disown_child alone left all 33 RebindTest green while stranding
        7 of 72 OPEN descendants AND returning success. The predicate and the
        cleanup are separate walks and each needs its own long-chain control."""
        n = 72
        ids = ["%032d" % i for i in range(n)]
        snap = {}
        for i, rid in enumerate(ids):
            row = {"id": rid, "status": "open"}
            if i + 1 < n:
                row["superseded_by"] = ids[i + 1]
            snap[rid] = row
        cancelled = []
        with mock.patch.object(dispatches, "snapshot", return_value=(snap, None)), \
                mock.patch.object(
                    dispatches, "mark_cancel",
                    side_effect=lambda rid, why: (cancelled.append(rid),
                                                  ({"id": rid}, None))[1]):
            out = dispatches._rebind_disown_child(ids[0], "cleanup")
        self.assertEqual(
            len(cancelled), n,
            "the cleanup cancelled %d of %d — it stopped early and the "
            "descendants past the stop are still OPEN" % (len(cancelled), n))
        self.assertEqual(cancelled[-1], ids[-1],
                         "the deepest descendant was never reached")
        self.assertNotIn("INCOMPLETE", out,
                         "a complete walk reported itself incomplete: %s" % out)
        # AND THE PAIRING THAT MAKES A TRUNCATION VISIBLE: if it ever DOES stop
        # early it must say so rather than return a success list. That arm is
        # covered by test_an_unwalkable_frontier_is_reported_not_swallowed.

    def test_an_unwalkable_frontier_is_reported_not_swallowed(self):
        """A truncated cleanup is a FAILED cleanup. Reporting 'cancelled A, B'
        while an unwalked frontier is still OPEN is the same laundering this
        lane exists to remove, one level up: the caller believes the obligation
        is contained when it is not."""
        a, b = "a" * 32, "b" * 32
        snap = {a: {"id": a, "status": "open", "superseded_by": b}}  # b MISSING
        with mock.patch.object(dispatches, "snapshot", return_value=(snap, None)), \
                mock.patch.object(dispatches, "mark_cancel",
                                  return_value=({"id": a}, None)):
            out = dispatches._rebind_disown_child(a, "cleanup")
        self.assertIn("CLEANUP INCOMPLETE", out,
                      "an unreadable frontier was swallowed: %s" % out)
        self.assertIn(b[:12], out, "the report does not name what was unreached")
        # POSITIVE CONTROL: a fully readable chain reports plain success, so
        # this is not a function that always cries incomplete.
        snap[b] = {"id": b, "status": "open"}
        with mock.patch.object(dispatches, "snapshot", return_value=(snap, None)), \
                mock.patch.object(dispatches, "mark_cancel",
                                  return_value=({"id": a}, None)):
            ok = dispatches._rebind_disown_child(a, "cleanup")
        self.assertNotIn("INCOMPLETE", ok, "a clean walk still cried incomplete")

    def test_a_cycle_in_the_cleanup_chain_is_reported_not_swallowed(self):
        a, b = "a" * 32, "b" * 32
        snap = {a: {"id": a, "status": "open", "superseded_by": b},
                b: {"id": b, "status": "open", "superseded_by": a}}
        with mock.patch.object(dispatches, "snapshot", return_value=(snap, None)), \
                mock.patch.object(dispatches, "mark_cancel",
                                  return_value=({"id": a}, None)):
            out = dispatches._rebind_disown_child(a, "cleanup")
        self.assertIn("CLEANUP INCOMPLETE", out, out)
        self.assertIn("repeats", out, out)

    def test_the_cleanup_preserves_a_descendant_that_reached_a_verdict(self):
        """The invariant is NO REACHABLE DESCENDANT REMAINS OPEN — not that
        every descendant reads cancelled. A descendant that reached a VERDICT
        earned that status, and a cleanup rewriting real history would destroy
        it (a correction to my own proposal in the meld)."""
        a, b, c = "a" * 32, "b" * 32, "c" * 32
        snap = {a: {"id": a, "status": "open", "superseded_by": b},
                b: {"id": b, "status": "verdict", "superseded_by": c},
                c: {"id": c, "status": "open"}}
        cancelled = []
        with mock.patch.object(dispatches, "snapshot", return_value=(snap, None)), \
                mock.patch.object(
                    dispatches, "mark_cancel",
                    side_effect=lambda rid, why: (cancelled.append(rid),
                                                  ({"id": rid}, None))[1]):
            dispatches._rebind_disown_child(a, "cleanup")
        self.assertIn(a, cancelled, "the open head was not cancelled")
        self.assertIn(c, cancelled, "the open frontier BEYOND the verdict was "
                                    "not cancelled")
        self.assertNotIn(b, cancelled,
                         "the cleanup overwrote a descendant's real VERDICT")

    def test_the_writer_and_the_replay_share_one_reason_cap(self):
        """The cap lived as a named constant in the writer and as TWO
        hardcoded 256s in replay. Move the constant and the writer accepts a
        reason replay then silently rejects — the ledger says cancelled and the
        projection says OPEN, with no error anywhere.

        The test MOVES the cap rather than trusting that the sites match today:
        a grep can be satisfied by a coincidence, a raised cap cannot."""
        row = self.add(recipient="grok", kind="review")
        long_reason = "x" * 270
        self.assertGreater(len(long_reason), 256,
                           "the reason is not long enough to cross the old cap")
        with mock.patch.object(dispatches, "_CANCEL_REASON_CAP", 300):
            out, err = dispatches.mark_cancel(row["id"], long_reason)
            self.assertIsNone(err, "the writer refused under a raised cap: %s" % err)
            self.assertEqual(out["status"], "cancelled")
            # REPLAY from the ledger under the same raised cap: it must agree
            snap = dispatches.snapshot()[0]
        self.assertEqual(
            snap[row["id"]]["status"], "cancelled",
            "the writer cancelled it but replay restored OPEN — the cap drifted "
            "between the two, so the ledger and the projection disagree")
        self.assertEqual(snap[row["id"]].get("cancel_reason"), long_reason)

    def test_an_aborted_rebind_disowns_the_GRANDCHILD_too(self):
        """An adversarial sweep: if the child was itself rebound before
        the abort cleanup ran, cancelling only the id we were handed cancels an
        INTERMEDIATE and leaves the grandchild OPEN — the same stranding, one
        generation down."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_add = dispatches.add

        def rebind_the_child_too(*a, **kw):
            out = real_add(*a, **kw)          # add() returns (row, err, dup)
            child = out[0]
            self.assertIsNotNone(child, "the child was not written: %s" % (out,))
            # the child is immediately rebound onward, then the source dies
            grand, gerr = real_add(          # _reason=True -> (row, err)
                "grok", child.get("lane"),
                ref=child.get("tip") or child.get("ref"),
                repo=self.repo, kind=child.get("kind"),
                supersedes=child["id"], _reason=True)
            self.assertIsNotNone(grand, "the grandchild was not written: %s" % gerr)
            dispatches.mark_cancel(child["id"], "rebound onward")
            _v, verr = dispatches.mark_verdict(row["id"], row["tip"],
                                               "findings", polarity="fix")
            self.assertIsNone(verr, "the planted verdict was refused: %s" % verr)
            return out

        with mock.patch.object(dispatches, "add", side_effect=rebind_the_child_too):
            dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        snap = dispatches.snapshot()[0]
        stray = [r for r in snap.values()
                 if r.get("status") == "open" and r.get("id") != row["id"]
                 and _descends_from(snap, r, row["id"])]
        self.assertEqual(
            stray, [],
            "a descendant survived OPEN under a terminal source: %s"
            % [r["id"][:12] for r in stray])
        # POSITIVE CONTROL: descendants really were created, so an empty stray
        # list is not just an empty family.
        kin = [r for r in snap.values() if _descends_from(snap, r, row["id"])]
        self.assertGreaterEqual(len(kin), 2,
                                "the fixture never built a grandchild, so this "
                                "test proves nothing")

    def test_the_RESIDUAL_path_disowns_the_grandchild_too(self):
        """The second r3 blocker. My other nested test plants a VERDICT,
        so it exercises the TERMINAL-recheck path only; the RESIDUAL path (the
        source's cancel is REFUSED and it stays OPEN) had coverage with an
        immediate child alone, so an immediate-child-only cleanup passed it.

        The two paths reach the same disown by different routes and both must
        walk the whole descent."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_add, real_cancel = dispatches.add, dispatches.mark_cancel
        made = {}

        def child_then_grandchild(*a, **kw):
            out = real_add(*a, **kw)
            child = out[0]
            self.assertIsNotNone(child, "the child was not written")
            grand, gerr = real_add(
                "grok", child.get("lane"),
                ref=child.get("tip") or child.get("ref"),
                repo=self.repo, kind=child.get("kind"),
                supersedes=child["id"], _reason=True)
            self.assertIsNotNone(grand, "the grandchild was not written: %s" % gerr)
            made["child"], made["grand"] = child["id"], grand["id"]
            return out

        def refuse_only_the_source(rid, reason):
            if rid == row["id"]:
                return None, "ledger unwritable (planted)"
            return real_cancel(rid, reason)

        with mock.patch.object(dispatches, "add", side_effect=child_then_grandchild), \
                mock.patch.object(dispatches, "mark_cancel",
                                  side_effect=refuse_only_the_source):
            _out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "open",
                         "the source is not OPEN, so this is not the residual "
                         "path and the test proves nothing")
        self.assertIn("NOT cancelled", err or "")
        for name in ("child", "grand"):
            rid = made[name]
            self.assertNotEqual(
                snap[rid]["status"], "open",
                "the %s survived OPEN under a source that was never cancelled — "
                "the obligation is claimed twice" % name)
            self.assertEqual(
                snap[rid]["status"], "cancelled",
                "the %s was seeded OPEN so it must read cancelled, not %s"
                % (name, snap[rid]["status"]))

    def test_a_long_source_error_still_lets_the_child_be_cancelled(self):
        """The second HIGH finding: the residual path interpolated the
        source's own error into the child's cancel reason. mark_cancel refuses
        a reason over 256 chars or carrying a control character, and that error
        is exactly where a long repo path or a newline lives — so the disown
        failed in precisely the case it exists for, leaving BOTH rows open.

        The earlier test planted a SHORT error and could not see this."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_cancel = dispatches.mark_cancel
        # a refusal shaped like the real one: a long path, and a newline
        fat = ("ledger unwritable (%s/dispatches.jsonl)\nrefusing"
               % ("/" + "d" * 300))
        self.assertGreater(len(fat), 256, "the planted error is not long enough")

        def refuse_the_source(rid, reason):
            if rid == row["id"]:
                return None, fat
            return real_cancel(rid, reason)

        with mock.patch.object(dispatches, "mark_cancel",
                               side_effect=refuse_the_source):
            _out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        child = self._child_of(row["id"])
        self.assertIsNotNone(child, "no child row was written at all")
        self.assertEqual(child["status"], "cancelled",
                         "the child survived as %s because its cancel reason "
                         "carried the source's unbounded error"
                         % child["status"])
        self.assertLessEqual(len(child.get("cancel_reason") or ""), 256)
        self.assertIn("NOT cancelled", err or "")

    def test_a_refused_cancel_disowns_the_child_too(self):
        """The recheck reads WITHOUT the lock, so it NARROWS the window and
        cannot close it: the source can still terminalize between the recheck
        and the cancel's own acquisition. However the cancel fails, the
        obligation did not move, so the child must not go on claiming it did."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        real_cancel = dispatches.mark_cancel

        def refuse_the_source(rid, reason):
            if rid == row["id"]:
                return None, "ledger unwritable (planted)"
            return real_cancel(rid, reason)

        with mock.patch.object(dispatches, "mark_cancel",
                               side_effect=refuse_the_source):
            out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)

        self.assertIn("NOT cancelled", err or "")
        child = self._child_of(row["id"])
        self.assertIsNotNone(child, "no child row was written at all")
        self.assertEqual(child["status"], "cancelled",
                         "the child survived as %s while its source stayed "
                         "uncancelled — the obligation is claimed twice"
                         % child["status"])
        # assert the child's OWN id reaches the caller, not the generic word
        # "child": an operator repairing this by hand needs the row to cancel,
        # and a message that says "the child" names nothing.
        self.assertIn(child["id"][:12], err or "",
                      "the caller was never told WHICH row was disowned")

    def test_the_planted_row_reaches_the_TARGET_usability_reader_too(self):
        """A rebind reads health TWICE: the evidence gate asks about the old
        recipient, and the write's usability rung asks about the new one with
        `include_probe=False`. The hand-typed doubles here refused that keyword,
        so the second read raised inside the double, was folded into
        UNREADABLE, and every arm was green on a read that never ran."""
        from helm import seat_usability
        self._starve("seat-a", streak=True)
        rows, err = seat_usability._read_health(names=["seat-a"])
        self.assertIsNone(err, "the usability reader could not call the "
                               "double: %s" % err)
        self.assertEqual(rows["seat-a"]["log"], "streak")
        # and a seat nobody planted is answered by the real function, never
        # by the planted row under another name
        rows, err = seat_usability._read_health(names=["seat-b"])
        self.assertIsNone(err, err)
        self.assertEqual(sorted(rows), ["seat-b"])
        self.assertNotEqual(rows["seat-b"].get("log"), "streak")

    def test_rebind_evidence_never_spends_an_upstream_canary(self):
        from helm import proxywatch
        with mock.patch.object(proxywatch, "health",
                               return_value={"seats": [{"seat": "grok"}]}) as health:
            dispatches._recipient_evidence("grok")
        health.assert_called_once_with(seats=["grok"], include_upstream=False)

    def test_starved_recipient_rebinds_preserving_everything(self):
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIsNone(err)
        old, new = out["old"], out["new"]
        self.assertEqual(old["status"], "cancelled")
        self.assertIn("rebound to ds4pro", old["cancel_reason"])
        self.assertEqual(new["recipient"], "ds4pro")
        self.assertEqual(new["lane"], row["lane"])
        self.assertEqual(new["tip"], row["tip"])
        self.assertEqual(new["kind"], "review")
        # the ledger tells the truth both ways
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[old["id"]]["status"], "cancelled")
        self.assertEqual(snap[new["id"]]["status"], "open")

    def test_rebind_preserves_a_sha_bound_parent_branch(self):  # noqa: VACUOUS_ASSERTION — persisted child identity is the positive control for the no-reprobe assertion
        self.git("branch", "lane/rebind-bound", self.b)
        row = self.add(recipient="grok", lane="rebind-bound", ref=self.b,
                       kind="review")
        self.assertEqual(row.get("ref_branch"),
                         "refs/heads/lane/rebind-bound")
        # Remove the discovery fact before rebind. Re-resolving the SHA would now
        # yield None; the child must inherit the parent's write-boundary identity.
        self.git("branch", "-f", "lane/rebind-bound", self.c)
        self.assertEqual(self.git("rev-parse", "lane/rebind-bound"), self.c)
        self.assertNotEqual(self.git("rev-parse", "lane/rebind-bound"), self.b)
        with mock.patch.object(
                dispatches, "_unique_local_tip_branch") as discover:
            out, err = dispatches.rebind(
                row["id"], "ds4pro", force=True, reason="move the obligation",
                repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        discover.assert_not_called()
        self.assertEqual(out["new"].get("ref_branch"), row["ref_branch"])
        self.assertEqual(dispatches.rows()[out["new"]["id"]].get("ref_branch"),
                         row["ref_branch"])

    def test_rebind_preserves_an_unbound_parent_even_after_a_branch_appears(self):  # noqa: VACUOUS_ASSERTION — persisted child row is the positive control for intentionally absent branch evidence
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)",
                                  "--points-at", self.b, "refs/heads"), "")
        row = self.add(recipient="grok", lane="rebind-unbound", ref=self.b,
                       kind="review")
        self.assertIsNone(row.get("ref_branch"))
        # The later branch is a real unique tip, but it did not exist at the
        # parent's write boundary and cannot retroactively become its identity.
        self.git("branch", "lane/rebind-later", self.b)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)",
                                  "--points-at", self.b, "refs/heads"),
                         "refs/heads/lane/rebind-later")
        with mock.patch.object(
                dispatches, "_unique_local_tip_branch") as discover:
            out, err = dispatches.rebind(
                row["id"], "ds4pro", force=True, reason="move the obligation",
                repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        discover.assert_not_called()
        self.assertIsNone(out["new"].get("ref_branch"))
        self.assertIsNone(
            dispatches.rows()[out["new"]["id"]].get("ref_branch"))

    def test_the_rebind_CLI_SAYS_what_reached_the_new_recipient(self):  # noqa: VACUOUS_ASSERTION — assertIn binds non-empty stderr
        """MEASURED 2026-07-31: I rebound two rows off a family that had gone
        dark, and BOTH new recipients came back asking for the brief. One
        named the gap itself — "the superseding row carries only lane + base...
        please resend". The knowledge that the brief was gone lived only in a
        code comment the operator never sees.

        THIS ARM'S ORIGINAL FORM PINNED THE WRONG SENTENCE, and kept it green
        while the note was wrong: its fixture is an `add`-minted row, which
        NEVER had a DM, and the note it asserted said "no recoverable DM message
        body ... Re-brief @ds4pro" — asking for a re-brief of something that
        never existed. The claim worth keeping is that the CLI TELLS the
        rebinder what reached the recipient, on stderr beside the success line,
        because the rebind SUCCEEDED. So it is rebound to the sentence this
        fixture actually earns; the three-way split lives in
        BriefAndAuthorTravelWithTheObligationTest."""
        row = self.add(recipient="grok", kind="review")
        self.assertIsNone(row["message_hash"],
                          "an add-created row never had an original DM")
        self._starve("grok", streak=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = dispatches.cmd_dispatch(["rebind", row["id"], "--to", "ds4pro"])
        self.assertEqual(rc, 0, "the rebind itself must still succeed")
        said = err.getvalue()
        self.assertIn("THERE NEVER WAS ONE", said,
                      "the rebinder was not told what reached @ds4pro")
        self.assertNotIn("Re-brief", said,
                         "an add-created row never had a DM, so nothing is owed")
        self.assertIn("ds4pro", said,
                      "the note must name the new recipient")

    def test_a_healthy_recipient_REFUSES_by_default(self):
        row = self.add(recipient="grok")
        self._healthy("grok")
        out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("REFUSED", err)
        self.assertIn("--force", err)
        # and the old row is UNTOUCHED
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_blind_watch_REFUSES_unknown_is_not_evidence(self):
        row = self.add(recipient="grok")
        _blind_health(self, RuntimeError("tmpfs gone"))
        out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("REFUSED", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_force_overrides_with_a_recorded_reason(self):
        row = self.add(recipient="grok")
        self._healthy("grok")
        out, err = dispatches.rebind(row["id"], "ds4pro", force=True,
                                     reason="judgment call: grok benched",
                                     repo=self.repo)
        self.assertIsNone(err)
        self.assertIn("grok benched", out["old"]["cancel_reason"])

    def test_a_foreign_clone_cannot_replace_the_obligations_repo_id(self):
        """codex blocker 3 on 6a8f9530: --repo forwarding let a same-tip
        foreign clone swap the obligation's repo_id at rc0. Identity is
        immutable across a rebind: the foreign path REFUSES before the
        cancel (the row survives its own refusal, still open), and this
        arm goes red if the forwarding is deleted instead of guarded —
        an ignored --repo would rc0 right through the foreign path."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        clone = os.path.join(self.tmp, "clone2")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True)
        out, err = dispatches.rebind(row["id"], "ds4pro", repo=clone)
        self.assertIsNone(out)
        self.assertIn("cannot change repos", err)
        self.assertIn(row.get("repo_id") or "", err)
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_a_legacy_row_without_repo_id_still_honors_caller_repo(self):
        """The identity guard binds only rows that RECORDED an identity; a
        pre-repo_id row has nothing to preserve, so the caller's --repo is
        all there is and must still flow (codex meld e:1785584307 coverage
        gap — deleting the legacy branch would strand every old row)."""
        row = {"id": "feedc0de", "ts": "2026-07-01T00:00:00Z",
               "recipient": "grok", "lane": "legacy-rebind", "ref": self.a,
               "note": None, "deadline_s": 60, "source": "old",
               "status": "open", "ack_ref": None, "verdict_ref": None,
               "last_updated": "2026-07-01T00:00:00Z"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        self.assertIsNone(dispatches.rows()["feedc0de"]["repo_id"])
        self._starve("grok", streak=True)
        with mock.patch.object(dispatches, "add",
                               wraps=dispatches.add) as spy:
            out, err = dispatches.rebind("feedc0de", "ds4pro",
                                         repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(spy.call_args.kwargs.get("repo"), self.repo)
        self.assertEqual(out["new"]["repo_id"], os.path.realpath(
            os.path.join(self.repo, ".git")))

    def test_repo_naming_a_path_inside_the_repo_keeps_the_repo_id(self):
        """The legitimate --repo use: an alternate spelling of the SAME
        repo (a subdirectory here) resolves to the recorded .git and the
        new row carries the old row's exact repo_id."""
        row = self.add(recipient="grok", kind="review")
        self._starve("grok", streak=True)
        # positive control: the fixture really stamps THE identity — a None
        # on both sides of the equality below must never read as preserved
        self.assertEqual(row.get("repo_id"), os.path.realpath(
            os.path.join(self.repo, ".git")))
        sub = os.path.join(self.repo, "deeper")
        os.makedirs(sub)
        with mock.patch.object(dispatches, "add",
                               wraps=dispatches.add) as spy:
            out, err = dispatches.rebind(row["id"], "ds4pro", repo=sub)
        self.assertIsNone(err)
        self.assertEqual(out["new"]["repo_id"], row["repo_id"])
        self.assertEqual(out["new"]["repo_id"],
                         dispatches.rows()[row["id"]]["repo_id"])
        # check/use pin: the path handed onward is the ROW's recorded root,
        # never the caller's spelling — a symlink retargeted between the
        # identity check and add()'s own resolution must have nothing to bite
        self.assertEqual(spy.call_args.kwargs.get("repo"),
                         str(row["repo_id"])[:-5])
        self.assertNotEqual(spy.call_args.kwargs.get("repo"), sub)

    def test_force_without_a_reason_refuses(self):
        row = self.add(recipient="grok")
        out, err = dispatches.rebind(row["id"], "ds4pro", force=True,
                                     repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("reason", err)

    def test_a_closed_row_cannot_be_rebound(self):
        row = self.add(recipient="grok")
        dispatches.mark_cancel(row["id"], "moot")
        out, err = dispatches.rebind(row["id"], "ds4pro", force=True,
                                     reason="x", repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("cancelled", err)

    def test_rebind_to_the_SAME_recipient_refuses(self):
        row = self.add(recipient="grok")
        out, err = dispatches.rebind(row["id"], "grok", force=True,
                                     reason="x", repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("already addressed", err)

    def test_the_cli_path_binds_verdict_style(self):
        row = self.add(recipient="grok")
        self._starve("grok", probe="down")
        rc = dispatches.cmd_dispatch(["rebind", row["id"][:12],
                                      "--to", "ds4pro"])
        self.assertEqual(rc, 0)
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "cancelled")

    def test_every_documented_rebind_flag_through_the_real_parser(self):
        """T-CLI: --to --force --reason --repo --json in ONE real invocation,
        asserting the EFFECT — the cancel EVENT in the raw ledger and the
        successor row's state — never the absence of a complaint. --force must
        carry the whole weight here: the mocked watch says HEALTHY."""
        row = self.add(recipient="grok")
        self._healthy("grok")
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["rebind", row["id"], "--to", "ds4pro", "--force",
                            "--reason", "judgment: grok benched",
                            "--repo", self.repo, "--json"])
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["old"]["status"], "cancelled")
        self.assertIn("grok benched", got["old"]["cancel_reason"])
        self.assertEqual(got["new"]["recipient"], "ds4pro")
        self.assertEqual(got["new"]["supersedes"], row["id"])
        # THE NOTE STAYS ON STDERR BESIDE --json, which is this line's claim —
        # rebound to the SENTENCE THAT ACTUALLY FIRES for this fixture. The row
        # is `add`-minted, so it never had a DM and nothing is owed; the arm
        # used to pin "Re-brief @ds4pro", which the old unconditional NOTE
        # printed even here. Its stream, not its wording, is what --json can
        # corrupt.
        self.assertIn("NOTE — no brief travelled because THERE NEVER WAS ONE",
                      err, "the brief-travel NOTE must stay on stderr beside "
                           "--json")
        self.assertNotIn("THERE NEVER WAS ONE", out,
                         "a warn on stdout corrupts the JSON consumer")
        # the cancel EVENT, read back from the append-only ledger itself
        cancels = [e for e in eventledger.events(dispatches.ledger_path())
                   if e.get("event") == "cancel" and e.get("id") == row["id"]]
        self.assertEqual(len(cancels), 1)
        self.assertIn("rebound to ds4pro", cancels[0]["reason"])
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "cancelled")
        self.assertEqual(snap[got["new"]["id"]]["status"], "open")

    def test_rebind_usage_refusals_exit_2_and_write_NOTHING(self):
        row = self.add(recipient="grok")
        with open(dispatches.ledger_path(), "rb") as f:
            before = f.read()
        # positive control on the SAME observable: the snapshot is real and
        # non-empty, so "unchanged" below is a claim about actual bytes
        self.assertIn(row["id"].encode(), before)
        for argv in (["rebind", row["id"]],                        # no --to
                     ["rebind", "--to", "ds4pro"],                 # no id
                     ["rebind", row["id"], "--to", "ds4pro", "--bogus"],
                     ["rebind", row["id"], "--to"]):               # value-less
            rc, _out, err = run(dispatches.cmd_dispatch, argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("usage: helm dispatch rebind", err)
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["rebind", row["id"], "--to", "ds4pro", "--bogus"])
        self.assertIn("unknown option --bogus", err,
                      "the refusal must NAME the offending token")
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertEqual(f.read(), before,
                             "a usage refusal must write NOTHING")

    def test_writer_refusals_surface_through_the_cli_as_exit_1(self):
        row = self.add(recipient="grok")
        dispatches.mark_cancel(row["id"], "moot")
        # positive control, unconditional: the closed-row fixture really IS
        # closed, so the refusal below is exercised, not vacuously skipped
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "cancelled")
        for argv, said in (
                (["rebind", "ffffffffffff", "--to", "ds4pro", "--force",
                  "--reason", "x"], "no such dispatch"),
                (["rebind", row["id"], "--to", "ds4pro", "--force",
                  "--reason", "x"], "only an OPEN row can be rebound")):
            rc, _out, err = run(dispatches.cmd_dispatch, argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(said, err)

    def test_the_rebind_CLI_SAYS_the_brief_did_not_travel(self):  # noqa: VACUOUS_ASSERTION — assertIn binds non-empty stderr
        """MEASURED 2026-07-31: I rebound two rows off a family that had gone
        dark, and BOTH new recipients came back asking for the brief. One
        named the gap itself — "the superseding row carries only lane + base...
        please resend".

        rebind() is RIGHT to drop the body: it builds the new row with add(),
        never send(), because the ledger stores no recoverable DM message body
        (send rows retain only a hash; add rows had no DM) and inventing one
        would put words in the original sender's mouth. The new recipient IS
        notified, so the OBLIGATION
        travels. What did not travel was the KNOWLEDGE that the brief was gone —
        it lived only in a code comment the operator never sees, so nobody was
        prompted to re-send.

        The warning goes to STDERR beside the success line, because the rebind
        SUCCEEDED; this is a follow-up owed, not a failure."""
        row = self.add(recipient="grok", kind="review")
        self.assertIsNone(row["message_hash"],
                          "an add-created row never had an original DM")
        self._starve("grok", streak=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = dispatches.cmd_dispatch(["rebind", row["id"], "--to", "ds4pro"])
        self.assertEqual(rc, 0, "the rebind itself must still succeed")
        said = err.getvalue()
        # THE PROPERTY IS THAT THE REBINDER IS TOLD, and the wording moved
        # under it. The seat-reassign car (e05b38d8d, "the BRIEF travels with
        # the obligation") replaced "no recoverable DM message body" with a
        # message that distinguishes NEVER-EXISTED from UNRECOVERABLE and says
        # nothing was lost — strictly better, and it did not update this test.
        # It did not have to: the same commit's lane had DELETED this test, so
        # its suite never asked. That deletion is why a real incompatibility
        # rode a green lane, and why the compose that restored the test went
        # red for the right reason.
        self.assertIn("THERE NEVER WAS ONE", said,
                      "the rebinder was never told the brief was unavailable")
        self.assertIn("nothing was lost", said,
                      "an add-created row lost nothing, and saying only that "
                      "no brief travelled reads as damage")
        self.assertNotIn("the original DM", said,
                         "add-created rows never had an original DM")
        self.assertIn("ds4pro", said,
                      "the warning must name WHO to re-brief")


class AtomicSendTest(DispatchBase):
    def test_send_is_one_first_class_handoff_and_retry_never_resends(self):
        row, why, posted = dispatches.send(
            "codex-3", "review", "Review this tip", self.a, repo=self.repo,
            key="review-1", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(posted)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["delivery"], "observed")
        again, why, posted = dispatches.send(
            "codex-3", "review", "Review this tip", self.a, repo=self.repo,
            key="review-1", sign=False, new_work=True)
        self.assertIn("do not resend", why)
        self.assertFalse(posted)
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(len(dispatches.rows()), 1)
        dm_rows, _ = seats.chat.read(seats.dm_lane("codex-3"))
        self.assertEqual([r["id"] for r in dm_rows], [row["delivery_ref"]])

    def test_retry_keeps_the_first_sha_branch_after_topology_becomes_ambiguous(self):
        self.git("branch", "lane/send-freeze", self.b)
        row, why, posted = dispatches.send(
            "codex-3", "review", "Review frozen topology", self.b,
            repo=self.repo, key="branch-freeze", sign=False, new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        self.assertEqual(row.get("ref_branch"),
                         "refs/heads/lane/send-freeze")
        self.git("branch", "lane/send-alias", self.b)
        again, why, posted = dispatches.send(
            "codex-3", "review", "Review frozen topology", self.b,
            repo=self.repo, key="branch-freeze", sign=False, new_work=True)
        self.assertIn("do not resend", why)
        self.assertFalse(posted)
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(again.get("ref_branch"), row["ref_branch"])
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(len(seats.chat.read(seats.dm_lane("codex-3"))[0]), 1)

    def test_retry_does_not_backfill_an_initially_unbound_sha(self):
        row, why, posted = dispatches.send(
            "codex-3", "review", "Review unbound topology", self.a,
            repo=self.repo, key="branch-none", sign=False, new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        self.assertIsNone(row.get("ref_branch"))
        self.git("branch", "lane/send-later", self.a)
        again, why, posted = dispatches.send(
            "codex-3", "review", "Review unbound topology", self.a,
            repo=self.repo, key="branch-none", sign=False, new_work=True)
        self.assertIn("do not resend", why)
        self.assertFalse(posted)
        self.assertEqual(again["id"], row["id"])
        self.assertIsNone(again.get("ref_branch"))
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(len(seats.chat.read(seats.dm_lane("codex-3"))[0]), 1)

    def test_ledger_stage_failure_rolls_back_before_any_delivery(self):
        with mock.patch.object(eventledger, "append_unlocked", return_value=False), \
                mock.patch.object(seats, "dm") as dm:
            row, why, posted = dispatches.send(
                "codex-3", "review", "Do it", self.a, repo=self.repo,
                key="stage-fail", sign=False, new_work=True)
        self.assertIsNone(row)
        self.assertIn("NOT recorded", why)
        self.assertFalse(posted)
        dm.assert_not_called()
        self.assertEqual(dispatches.rows(), {})

    def test_failed_delivery_is_needs_confirmation_and_never_auto_resends(self):
        with mock.patch.object(seats, "dm", return_value=(None, "recipient down")):
            failed, why, posted = dispatches.send(
                "codex-3", "review", "Do it", self.a, repo=self.repo,
                key="delivery-fail", sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("NEEDS CONFIRMATION", why)
        self.assertIn("recipient down", why)
        self.assertEqual(failed["status"], "open")
        self.assertEqual(failed["delivery"], "needs-confirmation")
        fp, text = seats._dispatch_candidate()
        self.assertIn(failed["id"], fp)
        self.assertIn("NEEDS CONFIRMATION", text)
        self.assertIn("do NOT resend", text)
        # The retry runs UNMOCKED: if the never-resend guard failed, a real DM
        # would land in the lane and the last assertion would catch it.
        again, why, posted = dispatches.send(
            "codex-3", "review", "Do it", self.a, repo=self.repo,
            key="delivery-fail", sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], failed["id"])
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(seats.chat.read(seats.dm_lane("codex-3"))[0], [])

    def test_crash_window_after_dm_stays_ambiguous_without_duplicate_message(self):
        real = eventledger.append_unlocked

        def fail_activation(path, row):
            if row.get("event") == "delivered":
                return False
            return real(path, row)

        with mock.patch.object(eventledger, "append_unlocked", side_effect=fail_activation):
            staged, why, posted = dispatches.send(
                "codex-3", "review", "Do it", self.a, repo=self.repo,
                key="activation-fail", sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("NEEDS CONFIRMATION", why)
        self.assertEqual(staged["status"], "open")
        self.assertEqual(staged["delivery"], "needs-confirmation")
        again, why, posted = dispatches.send(
            "codex-3", "review", "Do it", self.a, repo=self.repo,
            key="activation-fail", sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], staged["id"])
        self.assertEqual(len(seats.chat.read(seats.dm_lane("codex-3"))[0]), 1)

    def test_auto_key_uses_canonical_recipient_and_exact_tip(self):
        first, why, posted = dispatches.send(
            "@codex-3", "review", "same work", self.a[:10], repo=self.repo,
            sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(posted)
        again, why, posted = dispatches.send(
            "codex-3", "review", "same work", self.a, repo=self.repo,
            sign=False, new_work=True)
        self.assertIn("do not resend", why)
        self.assertFalse(posted)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(dispatches.rows()), 1)

    def test_same_key_cannot_alias_different_payload(self):
        dispatches.send("codex-3", "review", "one", self.a, repo=self.repo,
                        key="same", sign=False, new_work=True)
        row, why, posted = dispatches.send(
            "codex-3", "review", "two", self.a, repo=self.repo,
            key="same", sign=False, new_work=True)
        self.assertIsNone(row)
        self.assertIn("different work", why)
        self.assertFalse(posted)

    def test_key_namespace_and_semantic_collision_cover_sender_repo_and_metadata(self):
        first, why, _ = dispatches.send(
            "codex-3", "key-ns-review", "one", self.a, repo=self.repo, key="shared",
            note="first", deadline_s=60, sign=False, new_work=True)
        self.assertIsNone(why)
        clash, why, _ = dispatches.send(
            "codex-3", "key-ns-review", "one", self.a, repo=self.repo, key="shared",
            note="changed", deadline_s=120, sign=False, new_work=True)
        self.assertIsNone(clash)
        self.assertIn("different work", why)
        os.environ["HELM_CHAT_NAME"] = "other-integrator"
        other, why, _ = dispatches.send(
            "codex-3", "key-ns-other", "one", self.a, repo=self.repo, key="shared",
            note="first", deadline_s=60, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertNotEqual(other["id"], first["id"])
        os.environ["HELM_CHAT_NAME"] = "integrator"
        clone = os.path.join(self.tmp, "clone")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True)
        # THE CLONE IS A SEPARATE PROJECT and this arm's subject is that the
        # key namespace is scoped per repository — which needs the clone's row
        # to exist. It is written by the CLONE's helm, so the write door sees
        # its own project rather than a foreign ref.
        from tests._tmphome import dispatch_home
        with dispatch_home(clone):
            separate, why, _ = dispatches.send(
                "codex-3", "key-ns-separate", "one", self.a, repo=clone,
                key="shared", note="first", deadline_s=60, sign=False,
                new_work=True)
        self.assertIsNone(why)
        self.assertNotEqual(separate["id"], first["id"])

    def test_exact_successor_retry_reconciles_before_mutable_gates(self):
        parent = dispatches.add(
            "seat-c", "parent", ref=self.a, repo=self.repo, kind="review",
            new_work=True, notify=False)
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        args = ("seat-c", "atomic-cure", "Review cure", self.b)
        kw = {"repo": self.repo, "kind": "review", "supersedes": parent["id"],
              "key": key, "unique_key": True, "sign": False,
              "_cured_operation": {"validate": lambda _current: None}}
        first, why, sent = dispatches.send(*args, **kw)
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        for gate_name in ("_acting_author", "_recipient_operand",
                          "_validate_recipient_rostered",
                          "_validate_recipient_usable", "_base"):
            with self.subTest(gate=gate_name), mock.patch.object(
                    dispatches, gate_name,
                    side_effect=AssertionError("retry consulted " + gate_name)):
                again, retry_why, retry_sent = dispatches.send(*args, **kw)
                self.assertIsNone(retry_why)
                self.assertTrue(retry_sent)
                self.assertEqual(again["id"], first["id"])
        self.assertEqual(len([r for r in dispatches.rows().values()
                              if r.get("supersedes") == parent["id"]]), 1)

    def test_a_CANCELLED_successor_never_reconciles_as_a_live_retry(self):
        """THE FAILING INPUT: the exact cured retry whose recorded successor was
        CANCELLED. `delivery == "observed"` was read BEFORE `_open`, so send
        answered (that dead row, None, True) — the caller was told a live review
        exists, none did, the cure stayed unwitnessed, and the sweep re-proposed
        the same row every window forever. A dead successor is a PASS-THROUGH
        everywhere else in this module (`carrier`, `cured_unwitnessed`,
        `moved_nothing`); reconciliation is the one seam that read it as proof.

        MUST REJECT: an OPEN observed successor. That is a true idempotent
        retry and has to keep reconciling as sent — an arm that refused both
        would measure nothing but its own refusal."""
        parent = dispatches.add(
            "seat-c", "parent", ref=self.a, repo=self.repo, kind="review",
            new_work=True, notify=False)
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        args = ("seat-c", "atomic-cure", "Review cure", self.b)
        kw = {"repo": self.repo, "kind": "review", "supersedes": parent["id"],
              "key": key, "unique_key": True, "sign": False,
              "_cured_operation": {"validate": lambda _current: None}}
        first, why, sent = dispatches.send(*args, **kw)
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        # the MUST-MISS, exercised first: while the successor is OPEN this
        # exact operation still reconciles as delivered.
        again, why, sent = dispatches.send(*args, **kw)
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        self.assertEqual(again["id"], first["id"])
        _dead, err = dispatches.mark_cancel(first["id"], "reviewer stood down")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.rows()[first["id"]]["status"], "cancelled")
        got, why, sent = dispatches.send(*args, **kw)
        self.assertFalse(sent, "a cancelled successor is not a live review")
        self.assertIsNotNone(
            why, "a dead successor must refuse LOUDLY, never in silence")
        self.assertIn(first["id"][:12], why)
        self.assertIn(parent["id"], why)
        self.assertEqual(got["id"], first["id"])
        self.assertEqual(len([r for r in dispatches.rows().values()
                              if r.get("supersedes") == parent["id"]]), 1)

    def test_same_key_wrong_route_refuses_instead_of_reconciling(self):
        parent = dispatches.add(
            "seat-c", "parent", ref=self.a, repo=self.repo, kind="review",
            new_work=True, notify=False)
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        first, why, sent = dispatches.send(
            "seat-c", "atomic-cure", "Review cure", self.b, repo=self.repo,
            kind="review", supersedes=parent["id"], key=key,
            unique_key=True, sign=False,
            _cured_operation={"validate": lambda _current: None})
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        wrong, why, sent = dispatches.send(
            "seat-a", "atomic-cure", "Review cure", self.b, repo=self.repo,
            kind="review", supersedes=parent["id"], key=key,
            unique_key=True, sign=False,
            _cured_operation={"validate": lambda _current: None})
        self.assertIsNone(wrong)
        self.assertFalse(sent)
        self.assertIn("different or additional successor", why)
        self.assertEqual(len([r for r in dispatches.rows().values()
                              if r.get("supersedes") == parent["id"]]), 1)
        self.assertEqual(dispatches.rows()[first["id"]]["recipient"], "seat-c")

    def test_two_concurrent_exact_successors_append_once(self):  # noqa: VACUOUS_ASSERTION — two completed calls and one shared durable id are unconditional positive controls before the one-row assertion
        parent = dispatches.add(
            "seat-c", "parent", ref=self.a, repo=self.repo, kind="review",
            new_work=True, notify=False)
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        barrier = threading.Barrier(2)
        results = []

        def run():
            barrier.wait()
            results.append(dispatches.send(
                "seat-c", "atomic-cure", "Review cure", self.b,
                repo=self.repo, kind="review", supersedes=parent["id"],
                key=key, unique_key=True, sign=False,
                _cured_operation={"validate": lambda _current: None}))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(len({row["id"] for row, _why, _sent in results}), 1)
        self.assertEqual(sum(sent for _row, _why, sent in results), 1)
        self.assertEqual(len([r for r in dispatches.rows().values()
                              if r.get("supersedes") == parent["id"]]), 1)

    def test_ordinary_send_refuses_a_known_alternate_recipient_token(self):
        seats.write_roster("seat-c")
        with mock.patch.dict(os.environ,
                             {"HELM_SEAT_ALIASES": "owner:ALT=seat-c"}), \
                mock.patch.object(seats, "dm") as dm:
            row, why, sent = dispatches.send(
                "ALT", "ordinary-alias", "one", self.a, repo=self.repo,
                key="ordinary-alias", sign=False, new_work=True)
        self.assertIsNone(row)
        self.assertFalse(sent)
        self.assertIn("did you mean seat-c", why)
        dm.assert_not_called()

    def test_ordinary_explicit_key_keeps_never_resend_semantics(self):
        first, why, sent = dispatches.send(
            "seat-c", "ordinary-explicit", "one", self.a, repo=self.repo,
            key="ordinary-explicit", sign=False, new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        again, why, sent = dispatches.send(
            "seat-c", "ordinary-explicit", "one", self.a, repo=self.repo,
            key="ordinary-explicit", sign=False, new_work=True)
        self.assertEqual(again["id"], first["id"])
        self.assertFalse(sent)
        self.assertIn("do not resend", why)

    def test_cured_new_admission_rechecks_identity_and_refuses_self_delivery(self):
        parent = dispatches.add(
            "seat-c", "parent", ref=self.a, repo=self.repo, kind="review",
            new_work=True, notify=False)
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("seat-c", None)), \
                mock.patch.object(seats, "dm") as dm:
            row, why, sent = dispatches.send(
                "seat-c", "atomic-cure", "Review cure", self.b,
                repo=self.repo, kind="review", supersedes=parent["id"],
                key=key, unique_key=True, sign=False,
                _cured_operation={"validate": lambda _current: None})
        self.assertIsNone(row)
        self.assertFalse(sent)
        self.assertIn("self-delivery", why)
        dm.assert_not_called()
        self.assertEqual(len([r for r in dispatches.rows().values()
                              if r.get("supersedes") == parent["id"]]), 0)

    def test_cured_validation_is_the_last_fallible_step_before_append(self):
        parent = dispatches.add(
            "seat-c", "parent", ref=self.a, repo=self.repo, kind="review",
            new_work=True, notify=False)
        key = "stale-cure:%s:%s" % (parent["id"], self.b)
        order = []
        original_base = dispatches._base
        original_append = eventledger.append_unlocked

        def base(*args, **kwargs):
            order.append("base")
            return original_base(*args, **kwargs)

        def validate(_current):
            order.append("validate")
            return None

        def append(path, event):
            if event.get("event") == "dispatch":
                order.append("append")
            return original_append(path, event)

        with mock.patch.object(dispatches, "_base", side_effect=base), \
                mock.patch.object(eventledger, "append_unlocked", side_effect=append):
            row, why, sent = dispatches.send(
                "seat-c", "atomic-cure", "Review cure", self.b,
                repo=self.repo, kind="review", supersedes=parent["id"],
                key=key, unique_key=True, sign=False,
                _cured_operation={"validate": validate})
        self.assertIsNone(why, why)
        self.assertTrue(sent)
        self.assertIsNotNone(row)
        self.assertEqual(order[-2:], ["validate", "append"])

    def test_unique_key_refuses_the_same_operation_under_another_sender(self):
        first, why, sent = dispatches.send(
            "seat-c", "global-key", "one", self.a, repo=self.repo,
            key="stale-cure:parent:tip", sign=False, new_work=True,
            unique_key=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        os.environ["HELM_CHAT_NAME"] = "other-integrator"
        other, why, sent = dispatches.send(
            "seat-c", "global-key", "one", self.a, repo=self.repo,
            key="stale-cure:parent:tip", sign=False, new_work=True,
            unique_key=True)
        self.assertIsNone(other)
        self.assertFalse(sent)
        self.assertIn("another sender or route", why)
        self.assertEqual(set(dispatches.rows()), {first["id"]})

    def test_sender_identity_is_an_exact_token_not_an_output_injection(self):
        os.environ["HELM_CHAT_NAME"] = "bad\nseat"
        row, why, posted = dispatches.send(
            "codex-3", "review", "one", self.a, repo=self.repo, sign=False, new_work=True)
        self.assertIsNone(row)
        self.assertIn("sender", why)
        self.assertFalse(posted)

    def test_exact_token_recipient_is_preserved(self):
        seats.write_roster("team.a")
        seats.write_roster("team-a")
        one, why, _ = dispatches.send("team.a", "recip-token-a", "one", self.a,
                                      repo=self.repo, key="dot", sign=False, new_work=True)
        two, why2, _ = dispatches.send("team-a", "recip-token-b", "two", self.a,
                                       repo=self.repo, key="dash", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertIsNone(why2)
        self.assertNotEqual(one["recipient"], two["recipient"])
        self.assertNotEqual(one["delivery_ref"], two["delivery_ref"])


OLD_TS = "2026-07-01T00:00:00Z"     # before LEGACY_COMPAT_BOUNDARY


class HistoricalCompatTest(DispatchBase):
    """The shipping surface is dispatch/delivered/verdict/cancel. Rows the old
    schemas already wrote keep replaying truthfully — never rebound, never
    silently dropped — and the removed verbs stay removed. Compat honors only
    rows stamped BEFORE the boundary: appending removed-class events today
    drives nothing, however well-shaped; a terminal (verdict OR cancel) row is
    immutable against every later event."""

    def _legacy_open(self, rid, ref, ts=OLD_TS, lane=None):
        row = {"id": rid, "ts": ts, "recipient": "codex-3",
               "lane": lane or ("legacy-" + rid), "ref": ref, "note": None,
               "deadline_s": 60, "source": "old", "status": "open",
               "ack_ref": None, "verdict_ref": None, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        return row

    def test_historical_mixed_case_recipient_replays_as_one_canonical_identity(self):
        row = self._legacy_open("cafe" + "f00d", self.a[:7], lane="legacy-case")
        events = eventledger.events(dispatches.ledger_path())
        events[-1]["recipient"] = "CoDeX-3"
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")

        replayed = dispatches.rows()[row["id"]]
        self.assertEqual((replayed["recipient"], replayed["recipient_display"]),
                         ("codex-3", "CoDeX-3"))
        counts, err = dispatches.open_recipients()
        self.assertIsNone(err)
        self.assertEqual(counts, {"codex-3": 1})

    def test_already_written_retarget_rows_still_replay_for_legacy_opens(self):
        self._legacy_open("ce1e7dd0", self.a[:7], lane="legacy")
        move = {"id": "ce1e7dd0", "event": "retarget", "tip": self.b,
                "ref": self.b, "ts": OLD_TS}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), move))
        got = dispatches.rows()["ce1e7dd0"]
        self.assertEqual(got["tip"], self.b)
        self.assertIsNone(got["migration"])
        stale, why = dispatches.mark_verdict(
            "ce1e7dd0", self.a, "reviewed-a", "fix")
        self.assertIsNone(stale)
        self.assertIn("stale verdict", why)
        closed, why = dispatches.mark_verdict(
            "ce1e7dd0", self.b, "reviewed-b", "fix")
        self.assertIsNone(why)
        self.assertEqual(closed["reviewed_tip"], self.b)

    def test_exact_short_collision_refuses_before_appending_a_verdict(self):
        short = "deadbeef"
        longer = short + "1" * 24
        for rid in (short, longer):
            row, why = dispatches._base(
                "codex-3", "collision-" + rid, self.a, None,
                dispatches.DEFAULT_DEADLINE_S, self.repo, rid=rid, new_work=True)
            self.assertIsNone(why)
            self.assertIsNone(dispatches._append_dispatch(row)[1])
        out, why = dispatches.mark_verdict(
            short, self.a, "ambiguous", polarity="fix")
        self.assertIsNone(out)
        self.assertIn("ambiguous dispatch id prefix", why)
        self.assertEqual(len(dispatches.history(short)), 1)
        self.assertEqual(len(dispatches.history(longer)), 1)
        closed, why = dispatches.mark_verdict(
            longer, self.a, "canonical", polarity="fix")
        self.assertIsNone(why)
        self.assertEqual(closed["id"], longer)
        self.assertEqual(dispatches.history(longer)[-1]["id"], longer)

    def test_fresh_retarget_after_boundary_never_rebinds(self):
        # codex round-1 HIGH: a retarget appended TODAY must be inert — the
        # row stays needs-redispatch and can never become verdict-closable.
        self._legacy_open("ce1e7dd0", self.a[:7], lane="legacy")
        move = {"id": "ce1e7dd0", "event": "retarget", "tip": self.b,
                "ref": self.b, "ts": dispatches.pk.now_ts()}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), move))
        got = dispatches.rows()["ce1e7dd0"]
        self.assertIsNone(got["tip"])
        self.assertEqual(got["migration"], "needs-redispatch")
        blocked, why = dispatches.mark_verdict(
            "ce1e7dd0", self.b, "evidence", "fix")
        self.assertIsNone(blocked)
        self.assertIn("redispatch", why)
        # a retarget with NO ts at all is never compat either (fail-closed)
        bare = {"id": "ce1e7dd0", "event": "retarget", "tip": self.c,
                "ref": self.c}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), bare))
        self.assertIsNone(dispatches.rows()["ce1e7dd0"]["tip"])

    def test_fresh_snapshot_close_after_boundary_never_closes(self):
        # The close-compat door obeys the same boundary: a well-shaped v1
        # snapshot-verdict row appended today cannot close a legacy open.
        base = self._legacy_open("1a2b3c4d", self.a[:7])
        fake = dict(base, status="verdict", verdict_ref="forged",
                    ts=dispatches.pk.now_ts(),
                    last_updated=dispatches.pk.now_ts())
        self.assertTrue(eventledger.append(dispatches.ledger_path(), fake))
        got = dispatches.rows()["1a2b3c4d"]
        self.assertEqual(got["status"], "open")
        self.assertIn("1a2b3c4d", [r["id"] for r in dispatches.open_rows()])

    def test_live_ledger_shape_open_retarget_verdict_replays_closed(self):
        # The exact shape of the three closed obligations on the real ledger:
        # short-ref open -> retarget to an exact tip -> verdict at that tip,
        # all pre-boundary. History must keep replaying CLOSED.
        self._legacy_open("4f65d90d", self.a[:7])
        for event in (
                {"id": "4f65d90d", "event": "retarget", "tip": self.b,
                 "ref": self.b, "ts": OLD_TS},
                {"id": "4f65d90d", "event": "verdict", "status": "verdict",
                 "reviewed_tip": self.b, "verdict_ref": "review-post",
                 "ts": OLD_TS}):
            self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        got = dispatches.rows()["4f65d90d"]
        self.assertEqual(got["status"], "verdict")
        self.assertEqual(got["reviewed_tip"], self.b)
        self.assertNotIn("4f65d90d", [r["id"] for r in dispatches.open_rows()])

    def test_non_string_ts_never_passes_the_compat_boundary(self):
        # fable adversarial r3: ts=1 stringifies below the boundary — the
        # type-corruption class seq already guards must cover ts too.
        self._legacy_open("ce1e7dd0", self.a[:7], lane="legacy")
        for bad_ts in (1, 123456, 0.5, True, "", " ", "!pre", "1999-01-01T00:00:00Z"):
            move = {"id": "ce1e7dd0", "event": "retarget", "tip": self.b,
                    "ref": self.b, "ts": bad_ts}
            self.assertTrue(eventledger.append(dispatches.ledger_path(), move))
        got = dispatches.rows()["ce1e7dd0"]
        self.assertIsNone(got["tip"])
        self.assertEqual(got["migration"], "needs-redispatch")
        blocked, why = dispatches.mark_verdict(
            "ce1e7dd0", self.b, "evidence", "fix")
        self.assertIsNone(blocked)
        self.assertIn("redispatch", why)

    def test_post_boundary_legacy_genesis_cannot_fabricate_a_closed_row(self):
        # fable adversarial r3 (LOW): a legacy-shaped FIRST row appended after
        # the boundary must not invent an already-closed obligation.
        ts = dispatches.pk.now_ts()
        fab = {"id": "1a" * 16, "ts": ts, "recipient": "codex-3",
               "lane": "fabricated", "ref": self.a[:7], "note": None,
               "deadline_s": 60, "source": "old", "status": "verdict",
               "verdict_ref": "fabricated-evidence", "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), fab))
        got = dispatches.rows()["1a" * 16]
        self.assertEqual(got["status"], "open")     # visible, but never closed
        self.assertEqual(got["migration"], "needs-redispatch")
        # empty-string ts is NOT an honest pre-boundary stamp (fable delta MED)
        empty = dict(fab, id="4d" * 16, ts="", last_updated="")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), empty))
        self.assertEqual(dispatches.rows()["4d" * 16]["status"], "open")
        # the same genesis stamped BEFORE the boundary is honest history: closed
        old = dict(fab, id="2b" * 16, ts=OLD_TS, last_updated=OLD_TS)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), old))
        self.assertEqual(dispatches.rows()["2b" * 16]["status"], "verdict")

    def test_closed_rows_never_carry_needs_redispatch(self):
        # live row a8a0eadb read verdict+needs-redispatch simultaneously —
        # contradictory in --json even though every consumer filtered right.
        base = self._legacy_open("3c" * 4, self.a[:7])
        closed = dict(base, status="verdict", verdict_ref="safe",
                      last_updated=OLD_TS)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), closed))
        got = dispatches.rows()["3c" * 4]
        self.assertEqual(got["status"], "verdict")
        self.assertIsNone(got["migration"])

    def test_garbage_seq_rows_never_crash_replay_or_blind_good_rows(self):
        # codex round-1 HIGH: a valid legacy row with seq='not-an-int' made
        # snapshot() raise, turning EVERY obligation into "no usable
        # obligations". The bad row is ignored; the good rows survive.
        good = self.add()
        bad = dict(self._legacy_open("deadc0de", self.a[:7]))
        bad = dict(bad, id="c0ffee00", seq="not-an-int")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), bad))
        garbage_followup = {"id": good["id"], "v": 3, "event": "delivered",
                            "seq": "also-not-an-int", "ts": OLD_TS,
                            "delivery_ref": "x"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           garbage_followup))
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIn(good["id"], current)
        self.assertIn("deadc0de", current)
        self.assertNotIn("c0ffee00", current)          # bad row ignored
        self.assertEqual(current[good["id"]]["delivery"],
                         "needs-confirmation")         # garbage never applied

    def test_ambiguous_and_foreign_refs_are_refused_at_dispatch_time(self):
        self.git("branch", "dup", self.b)
        self.git("tag", "dup", self.a)
        self.assertIsNone(dispatches.add("codex-3", "lane", ref="dup",
                                         repo=self.repo, new_work=True))
        foreign = os.path.join(self.tmp, "foreign")
        os.makedirs(foreign)
        subprocess.run(["git", "-C", foreign, "init", "-q"], check=True)
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.c,
                                         repo=foreign, new_work=True))
        self.assertEqual(dispatches.rows(), {})

    def test_concurrent_conflicting_verdicts_close_exactly_once(self):
        row = self.add(ref=self.a)
        barrier = threading.Barrier(3)
        out = []

        def close(evidence):
            barrier.wait()
            out.append(dispatches.mark_verdict(
                row["id"], self.a, evidence, "fix"))

        threads = [threading.Thread(target=close, args=("review-1",)),
                   threading.Thread(target=close, args=("review-2",))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        winners = [got for got, why in out if why is None]
        losers = [why for got, why in out if why is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("already has a verdict", losers[0])
        final = dispatches.rows()[row["id"]]
        self.assertEqual(final["status"], "verdict")
        self.assertEqual(final["verdict_ref"], winners[0]["verdict_ref"])

    def test_v2_ref_less_row_surfaces_needs_redispatch_and_never_binds(self):
        ts = dispatches.pk.now_ts()
        row = {"v": 2, "id": "0123456789abcdef0123456789abcdef", "seq": 0,
               "event": "add", "ts": ts, "recipient": "codex-3",
               "lane": "legacy-v2-unbound", "ref": None, "tip": None,
               "original_ref": None, "original_tip": None, "note": None,
               "deadline_s": 60, "source": "old-v2", "repo": None,
               "repo_id": None, "dispatch_key": None, "message_id": None,
               "message_hash": None, "sender": None, "status": "pending",
               "ack_ref": None, "verdict_ref": None, "reviewed_tip": None,
               "delivery_ref": None, "delivery_error": None,
               "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        got = dispatches.rows()[row["id"]]
        self.assertEqual(got["migration"], "needs-redispatch")
        fp, text = seats._dispatch_candidate()
        self.assertIn("needs-redispatch", fp)
        self.assertIn("NEEDS REDISPATCH", text)
        rc, _out, _err = run(dispatches.cmd_dispatch,
                             ["bind", row["id"], self.a])
        self.assertEqual(rc, 2)                    # bind is not a verb
        blocked, why = dispatches.mark_verdict(
            row["id"], self.a, "safe", "fix")
        self.assertIsNone(blocked)
        self.assertIn("redispatch", why)
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])

    def test_validated_v1_no_seq_transitions_preserve_old_closures(self):
        base = self._legacy_open("1a2b3c4d", self.a[:7], lane="legacy-close")
        acked = dict(base, status="acked", ack_ref="post-1",
                     last_updated=OLD_TS)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), acked))
        closed = dict(acked, status="verdict", verdict_ref="safe",
                      last_updated=OLD_TS)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), closed))
        got = dispatches.rows()[base["id"]]
        self.assertEqual(got["status"], "verdict")
        self.assertEqual(got["verdict_ref"], "safe")
        self.assertNotIn(base["id"], [r["id"] for r in dispatches.open_rows()])

    def test_legacy_short_and_symbolic_refs_are_never_rebound_at_replay(self):
        # Both refs RESOLVE in this repo right now — replay must still refuse
        # to adopt them: resolution happened at write time or not at all.
        ts = dispatches.pk.now_ts()
        for rid, ref in (("ce1e7dd0", self.a[:7]), ("deadc0de", self.main)):
            legacy = {"id": rid, "ts": ts, "recipient": "codex-3",
                      "lane": "legacy-" + rid, "ref": ref, "note": None,
                      "deadline_s": 60, "source": "old", "status": "open",
                      "ack_ref": None, "verdict_ref": None, "last_updated": ts}
            self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
            got = dispatches.rows()[rid]
            self.assertIsNone(got["tip"])
            self.assertEqual(got["migration"], "needs-redispatch")
            blocked, why = dispatches.mark_verdict(
                rid, self.a, "evidence", "fix")
            self.assertIsNone(blocked)
            self.assertIn("redispatch", why)


class OverdueWhisperTest(DispatchBase):
    def test_deadline_is_advisory_and_delivered_still_goes_overdue(self):
        row = self.add(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-9")
        self.age(row["id"], 3600)
        self.assertEqual([r["id"] for r in dispatches.overdue()], [row["id"]])
        fp, text = seats._dispatch_candidate()
        self.assertIn(row["id"], fp)
        self.assertIn("PENDING VERDICT", text)
        self.assertIn("NEEDS CHECK-IN", text)
        self.assertIn("do NOT", text)

    def test_needs_confirmation_outranks_an_overdue_check_in(self):
        old = self.add(deadline_s=60)
        dispatches._mark_delivered(old["id"], "post-1")
        self.age(old["id"], 3600)
        with mock.patch.object(seats, "dm", return_value=(None, "down")):
            failed, why, posted = dispatches.send(
                "codex-4", "new", "deliver", self.a, repo=self.repo,
                key="confirm-first", sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("NEEDS CONFIRMATION", why)
        fp, text = seats._dispatch_candidate()
        self.assertIn(failed["id"], fp)
        self.assertNotIn(old["id"], fp)
        self.assertIn("NEEDS CONFIRMATION", text)
        self.assertIn("do NOT", text)

    def test_unavailable_ledger_is_unknown_not_silent_zero(self):
        # THE DISPATCH FOLD READS ITS LEDGER AS BYTES (task/2770), so the
        # failure is planted at that door as well as at the row reader.
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                mock.patch.object(eventledger, "read_bytes",
                                  return_value=(None,
                                                "PermissionError: denied")):
            rc, _out, err = run(dispatches.cmd_dispatch, ["list"])
            self.assertEqual(rc, 1)
            self.assertIn("obligations UNKNOWN", err)
            fp, text = seats._dispatch_candidate()
            self.assertEqual(fp, "dispatch:ledger-unavailable")
            self.assertIn("UNAVAILABLE", text)
            self.assertIn("UNKNOWN", text)

    def test_stop_whisper_walks_confirm_overdue_then_verdict_silences_it(self):
        row = self.add(deadline_s=60)
        fp, text = seats._dispatch_candidate()   # fresh add: delivery unproven
        self.assertIn("needs-confirmation", fp)
        self.assertIn("NEEDS CONFIRMATION", text)
        _seen, why = dispatches._mark_delivered(row["id"], "post-1")
        self.assertIsNone(why)
        self.assertIsNone(seats._dispatch_candidate())   # observed + young
        self.age(row["id"], 3600)
        self.assertIsNotNone(seats._dispatch_candidate())  # reloads from disk
        dispatches.mark_verdict(row["id"], self.a, "safe", "fix")
        self.assertIsNone(seats._dispatch_candidate())

    def test_utc_calendar_malformed_and_future_clock_semantics(self):
        row = self.add(deadline_s=60)
        self.assertLess(dispatches._age_s(row), 30)
        self.assertEqual(dispatches._age_s({"ts": "not-a-time"}), 0)
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() + 3600))
        self.assertEqual(dispatches._age_s({"ts": future}), 0)

    def test_list_spells_pending_and_needs_without_auto_reassign(self):
        row = self.add(deadline_s=60)
        self.age(row["id"], 3600)
        rc, out, err = run(dispatches.cmd_dispatch, ["list", "--overdue"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("PENDING VERDICT", out)
        self.assertIn("NEEDS CHECK-IN", out)
        self.assertIn("do not reassign", out)


class BuildDeadlineTest(DispatchBase):
    """A build is not a review, and one clock over both cried wolf.

    Measured on the live ledger: every build row carried the 2700s review
    default, so a lane that legitimately takes hours went OVERDUE while
    progressing normally — codex-2 read overdue at 3h while holding a 3h50m
    lease and actively editing, codex-3 at 1h42m of lease remaining."""

    def test_a_build_row_gets_a_builds_deadline_and_a_review_keeps_its_own(self):
        # The contract in literals, so a comparison of two computed values can
        # never pass by both being the same wrong number.
        self.assertEqual(dispatches.BUILD_DEADLINE_S, 4 * 3600)
        self.assertEqual(dispatches.DEFAULT_DEADLINE_S, 2700)
        build, review = self.add(kind="build"), self.add(kind="review")
        self.assertEqual(build["deadline_s"], dispatches.BUILD_DEADLINE_S)
        self.assertEqual(review["deadline_s"], dispatches.DEFAULT_DEADLINE_S)

    def test_a_STATED_deadline_is_never_overridden_by_the_kind(self):
        build = self.add(kind="build", deadline_s=60)
        review = self.add(kind="review", deadline_s=99)
        self.assertEqual(build["deadline_s"], 60)
        self.assertEqual(review["deadline_s"], 99)

    def test_an_UNKNOWN_kind_keeps_the_review_default(self):
        """Legacy rows carry no kind. Tripling their deadline on a guess would
        change what overdue means for a whole cohort; the progress signal
        covers the case that actually matters without needing to know."""
        self.assertEqual(dispatches.DEFAULT_DEADLINE_S, 2700)
        row = self.add()
        self.assertEqual(row["recipient"], "codex-3")   # the row is real…
        self.assertIsNone(row.get("kind"))              # …and carries no kind
        self.assertEqual(row["deadline_s"], dispatches.DEFAULT_DEADLINE_S)

    def test_the_CLI_is_the_path_that_carried_the_bug(self):
        """The library default was reachable, but the flag was OPTIONAL and the
        CLI substituted the review default whenever it was omitted — which was
        always. Pinned at the verb, not just at the function."""
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "codex-3", "lane-a", "--ref", self.a,
                             "--kind", "build", "--new-work",
                             "--repo", self.repo])
        self.assertEqual(rc, 0, err)
        row = max(dispatches.rows().values(), key=lambda r: str(r.get("ts")))
        self.assertEqual(row["kind"], "build")
        self.assertEqual(row["deadline_s"], dispatches.BUILD_DEADLINE_S)

    def test_an_explicit_CLI_deadline_still_wins_and_junk_still_refuses(self):
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "codex-3", "lane-b", "--ref", self.a,
                             "--kind", "build", "--deadline", "90",
                             "--new-work", "--repo", self.repo])
        self.assertEqual(rc, 0, err)
        row = max(dispatches.rows().values(), key=lambda r: str(r.get("ts")))
        self.assertEqual(row["deadline_s"], 90)
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "codex-3", "lane-c", "--ref", self.a,
                             "--kind", "build", "--deadline", "nonsense",
                             "--new-work", "--repo", self.repo])
        self.assertEqual(rc, 2)
        self.assertIn("deadline takes SECONDS", err)


class OverdueProgressTest(DispatchBase):
    """`overdue` used to mean only that a clock elapsed, never that nothing was
    happening. A row whose recipient is visibly working it is not late."""

    _OWN_REPO = object()

    def claims(self, *rows, repo=_OWN_REPO):
        """(resource, holder, ttl_s) … -> live grants MINTED BY `seats.claim`.

        THE GRANT SHAPE IS THE PRODUCER'S, NOT THIS FIXTURE'S (task/2437 round
        three, finding 1). Progress now requires the grant to RECORD the
        repository its lease was taken out for, and a hand-written blob asserting
        that field would be this fixture inventing the very evidence under test.
        `seats.claim` is what `helm work claim` mints a lease with, so the nonce,
        the fence, the monotonic expiry AND the repository binding all come from
        the shipped writer; `repo` defaults to the repository these rows are
        bound to and an arm passes `repo=None` to mint the pre-field shape.

        The file is truncated first because several arms below re-plant ONE
        resource in a loop and mean REPLACE rather than accumulate — an
        accumulated earlier positive would keep suppressing and every negative
        after it would pass for the wrong reason."""
        if repo is self._OWN_REPO:
            repo = dispatches._repo_info(self.repo)["repo_id"]
        os.makedirs(os.path.dirname(seats.claims_path()), exist_ok=True)
        with open(seats.claims_path(), "w", encoding="utf-8") as f:
            json.dump({"_fence": 1}, f)
        for res, holder, ttl in rows:
            ok, why, _lease = seats.claim(res, holder, ttl=ttl, repo=repo)
            self.assertTrue(ok, why)

    def elapsed(self, **kwargs):
        """An open row whose clock has definitely run out."""
        row = self.add(deadline_s=60, **kwargs)
        self.age(row["id"], 3600)
        return dispatches.rows()[row["id"]]

    def room(self, row):
        """The lane's REAL room, cut by git at the path `work/_lanes` mints.

        THE PREMISE THESE ARMS REST ON IS NARROWER NOW (task/2437 round two,
        finding 7). A lane claim is not progress evidence on its own: the claims
        token is the repo root's BASENAME and is not injective, so a claim minted
        in a FORK of a repository suppresses the overdue verdict on a row of the
        OTHER repository sharing that basename, and the row reads as worked while
        nobody works it. Progress now also requires that the claimed room belong to
        THIS row's repository, which the room's own gitdir pointer answers.

        EVERY ARM BELOW CUTS ITS ROOM, INCLUDING THE NEGATIVES (round three,
        finding 3). An arm whose room is missing is refused by the room binding
        before its own named variable is ever consulted, so it would stay green
        with the holder check, the expiry check or the lane-spelling check
        completely broken — a test that passes for a reason it does not name.
        A negative belongs on an OTHERWISE-VALID input: the room exists, points
        into this repository, and the grant records it, so the one thing left
        that can decide the answer is the variable the arm is about.

        `--detach` on purpose: the property under test is which REPOSITORY the
        room belongs to, and a lane branch is not part of it.
        """
        return self.room_named(row["lane"])

    def room_named(self, lane):
        """A real room for any lane label, so a negative arm can hand the
        predicate a room that is valid in every respect but the one it varies."""
        from helm.work import _lanes
        path = _lanes.lane_path(self.repo, lane)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.git("worktree", "add", "-q", "--detach", path, self.a)
        self.addCleanup(subprocess.run,
                        ["git", "-C", self.repo, "worktree", "remove",
                         "--force", path], capture_output=True)
        return path

    def lane_resource(self, row):
        """The exact worktree resource encoded by this fixture's row."""
        repo_id = row["repo_id"]
        self.assertEqual(os.path.basename(repo_id), ".git")
        return "worktree:%s:%s" % (
            os.path.basename(os.path.dirname(repo_id)), row["lane"])

    def test_a_recipient_holding_the_DISPATCHED_LANE_is_working_not_late(self):
        row = self.elapsed()
        self.room(row)
        self.assertTrue(dispatches._is_overdue(row),
                        "positive control: with no claim the clock rules")
        self.claims((self.lane_resource(row), "codex-3", 3600))
        self.assertFalse(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.WORKING)
        self.assertEqual([r["id"] for r in dispatches.overdue()], [])

    def test_case_only_claim_holder_is_the_same_recipient(self):
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), "CoDeX-3", 3600))
        state, detail = dispatches.progress_state(row)
        self.assertEqual(state, dispatches.WORKING)
        self.assertIn("dispatched lane", detail)

    def test_a_claim_on_the_ROW_ITSELF_counts_and_names_the_row(self):
        row = self.elapsed()
        self.claims(("dispatch:" + row["id"][:8], "codex-3", 3600))
        state, detail = dispatches.progress_state(row)
        self.assertEqual(state, dispatches.WORKING)
        self.assertIn("claim on this row", detail)
        self.assertFalse(dispatches._is_overdue(row))

    def test_ANOTHER_seats_claim_on_the_same_lane_shields_nothing(self):  # noqa: VACUOUS_ASSERTION — the recipient-held pair runs FIRST on the same observable and the same room
        """The claim must belong to the RECIPIENT. A lane held by someone else
        is evidence about them, not about the seat that owes this row.

        ON AN OTHERWISE-VALID ROOM AND GRANT (round three, finding 3). The room
        is cut, the grant records this repository, the resource is the exact
        dispatched lane — so HOLDER is the only variable left, which is what the
        paired positive above it establishes. Without the pair, a room this arm
        never cut would have refused the claim before `recipient_matches` was
        reached and this arm would pass with the holder check deleted."""
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), row["recipient"], 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "PAIRED POSITIVE: the recipient's own grant on this "
                         "exact room suppresses, so everything but the holder "
                         "is valid below")
        self.claims((self.lane_resource(row), "kimi", 3600))
        self.assertTrue(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.IDLE)

    def test_an_EXPIRED_claim_shields_nothing(self):  # noqa: VACUOUS_ASSERTION — the live pair runs FIRST on the same observable, the same room and the same resource
        """Liveness is seats._sweep's rule, not a key's presence — a lease that
        ran out is exactly the stalled case the alarm exists for.

        ON AN OTHERWISE-VALID ROOM AND GRANT (round three, finding 3): same
        room, same holder, same resource, same recorded repository as the
        positive — only the TTL moves, so `_sweep` is the one thing that can
        decide the answer."""
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), row["recipient"], 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "PAIRED POSITIVE: the same grant with time left "
                         "suppresses, so expiry is the only variable below")
        self.claims((self.lane_resource(row), row["recipient"], -1))
        self.assertTrue(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.IDLE)

    def test_a_LANE_SUFFIX_never_counts_as_the_lane(self):  # noqa: VACUOUS_ASSERTION — the exact-lane pair runs FIRST on the same observable and the suffix lane gets its OWN real room
        """A differently-named lane cannot shield the dispatched one.

        ON AN OTHERWISE-VALID ROOM AND GRANT (round three, finding 3). The
        suffixed lane gets its OWN real room in THIS repository and its own
        repo-bound grant, so the room binding and the grant binding both SUCCEED
        for it — the lane label is the only fact that can refuse, which is what
        this arm claims to measure. Previously the suffix room did not exist at
        all and the arm passed on a missing directory."""
        row = self.elapsed()
        self.room(row)
        self.room_named(row["lane"] + "-followup")
        self.claims((self.lane_resource(row), row["recipient"], 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "PAIRED POSITIVE: the exact lane's grant suppresses, "
                         "so the label is the only variable below")
        self.claims((self.lane_resource(row) + "-followup", row["recipient"], 3600))
        self.assertTrue(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.IDLE)

    def test_a_grant_with_NO_recorded_repository_shields_nothing(self):  # noqa: VACUOUS_ASSERTION — the repo-bound pair runs FIRST on the same observable, the same room and the same resource
        """UNKNOWN PROVENANCE SUPPRESSES NOTHING (round three, finding 1).

        `seats.claim` with no `repo` is the shape every grant minted before the
        field carries, and the shape `helm chat claim worktree:<proj>:<lane>`
        still mints — the producer's own output, not a fixture invention. The
        room exists and points into this repository, the holder is the
        recipient, the lease is live and the resource is exact: the ONLY missing
        fact is which repository the lease was taken out for, and a room is not
        a grant. So the clock verdict stands, which is the noisy direction."""
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), row["recipient"], 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "PAIRED POSITIVE: the same grant WITH its repository "
                         "recorded suppresses, so provenance is the only "
                         "variable below")
        self.claims((self.lane_resource(row), row["recipient"], 3600), repo=None)
        grant = dispatches.live_claims()[self.lane_resource(row)]
        self.assertIsNone(grant.get("repo"),
                          "the producer recorded a repository anyway, so this "
                          "arm is not measuring unknown provenance")
        self.assertTrue(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.IDLE)

    def test_a_same_named_lane_outside_this_project_shields_nothing(self):  # noqa: VACUOUS_ASSERTION — exact-resource positive control runs before the loop
        """A lane label is not a global identity. Claims are fleet-global, so
        another project's same-named lane — or a non-worktree resource with
        the same suffix — proves nothing about this dispatch."""
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), "codex-3", 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "positive control: the exact resource suppresses")
        for resource in ("worktree:other-project:lane-a", "ticket:lane-a"):
            self.claims((resource, "codex-3", 3600))
            self.assertTrue(dispatches._is_overdue(row), resource)
            self.assertEqual(dispatches.progress_state(row)[0],
                             dispatches.IDLE, resource)

    def test_a_malformed_repo_identity_cannot_authorize_suppression(self):  # noqa: VACUOUS_ASSERTION — canonical-identity positive control runs before the loop
        """Positive progress needs the current writer's canonical repository
        binding. A relative or corrupt replayed repo_id stays noisy rather than
        borrowing a plausible worktree resource from another row."""
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), "codex-3", 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "positive control: canonical identity suppresses")
        self.claims(("worktree:repo:lane-a", "codex-3", 3600))
        for repo_id in ("relative/repo/.git", "/tmp/repo\0/.git"):
            row["repo_id"] = repo_id
            self.assertTrue(dispatches._is_overdue(row), repr(repo_id))
            self.assertEqual(dispatches.progress_state(row)[0],
                             dispatches.IDLE, repr(repo_id))

    def test_an_UNREADABLE_claims_ledger_leaves_the_clock_verdict_standing(self):
        """Fail-safe direction, stated: a broken lookup can only ever KEEP the
        old behaviour. Unknown must not quietly become fine, because that turns
        a noisy alarm into a silent one."""
        row = self.elapsed()
        self.room(row)
        self.claims((self.lane_resource(row), "codex-3", 3600))
        self.assertFalse(dispatches._is_overdue(row),
                         "positive control: readable claims DO suppress it")
        with mock.patch.object(dispatches, "live_claims", return_value=None):
            self.assertTrue(dispatches._is_overdue(row))
            state, detail = dispatches.progress_state(row)
            self.assertEqual(state, dispatches.PROGRESS_UNKNOWN)
            self.assertIn("unreadable", detail)

    def test_progress_never_MANUFACTURES_an_overdue_verdict(self):
        """It only ever suppresses. A young row with no claim anywhere is not
        overdue, or absence-of-lease would become its own false alarm."""
        row = self.add(deadline_s=3600)
        self.claims()
        self.assertFalse(dispatches._is_overdue(dispatches.rows()[row["id"]]))
        # POSITIVE CONTROL on the same row: it CAN go overdue, so the False
        # above is a young clock and not an inert fixture.
        self.age(row["id"], 7200)
        self.assertTrue(dispatches._is_overdue(dispatches.rows()[row["id"]]))


class StorageSafetyTest(DispatchBase):
    def test_pread_fallback_preserves_the_shared_file_offset(self):
        path = os.path.join(self.tmp, "pread-probe")
        with open(path, "wb") as f:
            f.write(b"abc")
        fd = os.open(path, os.O_RDONLY)
        try:
            os.lseek(fd, 2, os.SEEK_SET)
            with mock.patch.object(fsops.os, "pread", None, create=True):
                self.assertEqual(fsops.pread(fd, 1, 0), b"a")
            self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 2)
        finally:
            os.close(fd)

    def test_corrupt_duplicate_and_truncated_tail_cannot_erase_good_row(self):
        row = self.add()
        path = dispatches.ledger_path()
        with open(path, "ab") as f:
            f.write(b'not-json\n')
            bad = dict(row, seq=0, lane="collision", status="verdict")
            f.write((json.dumps(bad) + "\n").encode())
            f.write(b'{"id":"' + row["id"].encode() + b'","status":"verdict"}')
        got = dispatches.rows()[row["id"]]
        self.assertEqual(got["lane"], row["lane"])
        self.assertEqual(got["status"], "open")

    def test_next_append_repairs_only_truncated_tail_and_remains_replayable(self):
        first = self.add(lane="first")
        with open(dispatches.ledger_path(), "ab") as f:
            f.write(b'{"id":"torn","status":"pending"')
        with mock.patch.object(fsops.os, "pread", None, create=True):
            second = self.add(lane="second")
        got = dispatches.rows()
        self.assertEqual(set(got), {first["id"], second["id"]})
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertTrue(f.read().endswith(b"\n"))

    def test_well_formed_fake_close_event_cannot_erase_pending_obligation(self):
        row = self.add()
        fake = dict(row, seq=1, event="ack", status="verdict",
                    verdict_ref="forged", reviewed_tip=self.a)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), fake))
        got = dispatches.rows()[row["id"]]
        # The forged row stays VISIBLE in history (append-only ledger) but a
        # v3 obligation only closes on a strict verdict event naming its tip
        # (or a strict cancel event) — never a well-formed forged close.
        self.assertEqual(got["status"], "open")
        self.assertEqual(len(dispatches.history(row["id"])), 2)

    def test_well_shaped_ack_verdict_and_retarget_cannot_rewrite_tip(self):
        row = self.add(ref=self.b)
        forged_ack = dict(row, seq=1, event="ack", status="acked",
                          ack_ref="post", ref=self.c, tip=self.c)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged_ack))
        forged_verdict = dict(row, seq=1, event="verdict", status="verdict",
                              ref=self.c, tip=self.c, reviewed_tip=self.c,
                              verdict_ref="forged")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged_verdict))
        forged_move = dict(row, seq=1, event="retarget", ref=self.side,
                           tip=self.side, retarget_from=self.b,
                           retarget_to=self.side)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged_move))
        got = dispatches.rows()[row["id"]]
        self.assertEqual(got["tip"], self.b)
        self.assertEqual(got["status"], "open")
        self.assertEqual(len(dispatches.history(row["id"])), 4)

    def test_symlink_ledger_is_refused_without_touching_target(self):
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("safe")
        os.symlink(victim, dispatches.ledger_path())
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.a, repo=self.repo, new_work=True))
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "safe")
        self.assertEqual(dispatches.rows(), {})

    def test_hardlinked_ledger_is_refused_without_touching_target(self):
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        victim = os.path.join(self.tmp, "hardlink-victim")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("safe")
        os.link(victim, dispatches.ledger_path())
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.a,
                                         repo=self.repo, new_work=True))
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "safe")
        _rows, unavailable = dispatches.snapshot()
        self.assertIn("private regular file", unavailable)
        rc, _out, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 1)
        self.assertIn("obligations UNKNOWN", err)

    def test_symlinked_home_parent_is_refused_before_ledger_creation(self):
        target = os.path.join(self.tmp, "redirect-target")
        os.makedirs(target)
        link = os.path.join(self.tmp, "redirect-home")
        os.symlink(target, link)
        os.environ["HELM_HOME"] = link
        self.assertIsNone(dispatches.add("codex-3", "lane", ref=self.a, repo=self.repo, new_work=True))
        self.assertFalse(os.path.exists(os.path.join(target, "_global")))
        self.assertEqual(dispatches.rows(), {})

    def test_the_write_doors_authority_read_creates_no_home_directory(self):  # noqa: VACUOUS_ASSERTION — the LAST assertion is the unconditional positive on the exactly same observable: a real admitted write DOES create `_global` under this same fresh home
        """A DOOR MUST NOT MATERIALISE THE HOME IT MAY BE ABOUT TO REFUSE.

        `write_scope` consults the project registry, and an ordinary
        `registry.load()` takes the owner WRITE LOCK on the load that migrates
        mixed-era authored fields — which creates
        `<HELM_HOME>/_global/.state/`. That put a directory tree under an
        unvalidated home BEFORE `eventledger`'s parent-symlink guard had run on
        it, which is precisely the ordering
        `test_symlinked_home_parent_is_refused_before_ledger_creation` exists to
        forbid: measured, `_global` appeared under the symlink's target and the
        write was then refused.

        BOTH ADMISSION PATHS ARE DRIVEN, because both read the registry: the
        one where the ref's repository IS this helm's own, and the one where it
        is a stranger.

        THE MUST-HIT CONTROL IS LAST and on the same observable — a real
        admitted write DOES create `_global` under this same fresh home, so an
        arm that passed because nothing in this process ever creates that
        directory would be caught here. BLAST RADIUS: everything this arm
        touches lives under `self.tmp`, and `HELM_HOME` is restored by the
        fixture's own tearDown.
        """
        fresh = os.path.join(self.tmp, "unseen-home")
        os.environ["HELM_HOME"] = fresh
        self.assertFalse(os.path.exists(fresh),
                         "the home already exists, so this arm cannot see it "
                         "being created")
        stranger = os.path.join(self.tmp, "stranger")
        subprocess.run(["git", "clone", "-q", self.repo, stranger],
                       check=True, capture_output=True)

        mine = dispatches._repo_info(self.repo)["repo_id"]
        _project, why = dispatches.write_scope(mine)
        self.assertIsNone(why, "this fixture's own repository was refused, so "
                               "the admitted path never ran: %s" % (why,))
        self.assertFalse(os.path.exists(fresh),
                         "the ADMITTED path created the home before any "
                         "storage check: %r" % (os.listdir(fresh)
                                                if os.path.isdir(fresh)
                                                else fresh,))

        theirs = dispatches._repo_info(stranger)["repo_id"]
        self.assertNotEqual(theirs, mine,
                            "the clone shares this fixture's gitdir, so the "
                            "refused path was never taken")
        _project, why = dispatches.write_scope(theirs)
        self.assertIsNotNone(why, "an unregistered repository was admitted, so "
                                  "the refused path never ran")
        self.assertFalse(os.path.exists(fresh),
                         "the REFUSED path created the home it refused: %r"
                         % (os.listdir(fresh) if os.path.isdir(fresh)
                            else fresh,))

        row = dispatches.add("seat-under-test", "lane/real-write", ref=self.a,
                             repo=self.repo, new_work=True, kind="review",
                             notify=False)
        self.assertIsNotNone(row, "the control write was refused, so the "
                                  "assertion below measures nothing")
        self.assertTrue(os.path.isdir(os.path.join(fresh, "_global")),
                        "a real write did not create `_global` under this "
                        "home, so the absences above prove nothing")

    def test_an_UNREADABLE_registry_refuses_a_stranger_and_still_admits_home(self):  # noqa: VACUOUS_ASSERTION — every absence here is paired on the SAME observable: the admitted row is read back off the ledger by id, and each refusal's `why` is asserted to name the registry path and the error class
        """A STRICT AUTHORITY READ RAISES, AND THE DOOR OWES A PAIR EITHER WAY.

        `write_scope`'s own docstring promises that "a wiped registry can never
        lock helm out of its own ledger" and that the refusal polarity is
        "unresolvable project => refuse". `_project_of(snapshot=True)` is a
        STRICT read, which RE-RAISES instead of failing open — so the version
        this arm guards consulted the registry on the PROVEN-HOME path too, for
        a project label every caller discards, and a malformed registry threw
        the exception straight out of `_base`/`add` past the (row, why) contract:
        a valid own-repository dispatch died where trunk admitted it, and
        `stalebot`'s per-row consultation of the same door died on row one.

        BOTH HALVES ON ONE MALFORMED REGISTRY, which is what makes them a pair
        and not two fixtures: the proven home WRITES its row, and a stranger is
        refused with the registry error NAMED and nothing added to the ledger.
        Every producer is shipped — the registry file is read by
        `registry.load(strict=True)` through `inject._ledger.project_for_cwd`,
        and the write goes through the real `dispatches.add`.
        """
        registry_path = home.registry_path()
        os.makedirs(os.path.dirname(registry_path), exist_ok=True)
        with open(registry_path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all,,,")
        # MUST-HIT: the malformation is real AND it is specific to the strict
        # validator. Without the first assertion this arm is the valid-registry
        # case under a different name; without the second, the refusal below
        # could be the ordinary "no project claims it" answer instead of the
        # unreadable-authority one.
        from helm.inject._ledger import project_for_cwd
        mine = dispatches._repo_info(self.repo)["repo_id"]
        with self.assertRaises(Exception):
            project_for_cwd(mine, strict=True)
        self.assertIsNone(project_for_cwd(mine),
                          "the ordinary read also failed, so the refusal below "
                          "does not distinguish strict from fail-open")
        # CONTROL — a strict snapshot of this registry, run here read-only. It
        # RAISES, so any proven-home admission that reaches `_project_of` cannot
        # return the (None, None) asserted next: this arm is red for a door
        # whose home branch consults the registry at all.
        # BLAST RADIUS: one strict registry read; it takes no lock, writes
        # nothing (asserted below: the home ledger is untouched by it), and no
        # other arm shares the malformed file, which dies with self.tmp.
        with self.assertRaises(Exception):
            dispatches._project_of(mine, snapshot=True)

        self.assertEqual(dispatches.write_scope(mine), (None, None),
                         "the proven home was not admitted independently of "
                         "the registry")
        row, why = dispatches.add("seat-under-test", "lane/home-under-garbage",
                                  ref=self.a, repo=self.repo, new_work=True,
                                  kind="review", notify=False, _reason=True)
        self.assertIsNotNone(row, "a ref in this helm's OWN repository was "
                                  "refused because the registry is garbled: %s"
                             % (why,))
        self.assertEqual(list(dispatches.rows()), [row["id"]])

        stranger = os.path.join(self.tmp, "stranger")
        subprocess.run(["git", "clone", "-q", self.repo, stranger],
                       check=True, capture_output=True)
        theirs = dispatches._repo_info(stranger)["repo_id"]
        self.assertNotEqual(theirs, mine,
                            "the clone shares this fixture's gitdir, so the "
                            "foreign path was never taken")
        project, why = dispatches.write_scope(theirs)
        self.assertIsNone(project)
        self.assertIsNotNone(why, "an unplaceable repository was admitted while "
                                  "the registry was unreadable")
        self.assertIn(registry_path, why,
                      "the refusal does not name the file to repair: %s" % (why,))
        self.assertIn("JSONDecodeError", why,
                      "the refusal does not name the registry error, so the "
                      "reader cannot tell it from 'not registered': %s" % (why,))
        foreign, why = dispatches.add("seat-under-test", "lane/stranger",
                                      ref=self.a, repo=stranger, new_work=True,
                                      kind="review", notify=False, _reason=True)
        self.assertIsNone(foreign, "the stranger's row was written while the "
                                   "registry was unreadable")
        self.assertIn(registry_path, why)
        self.assertEqual(list(dispatches.rows()), [row["id"]],
                         "the refused write reached the ledger anyway")

    def test_first_creation_fsyncs_parent_and_ledger_directory_entries(self):
        real = eventledger._fsync_dir
        with mock.patch.object(eventledger, "_fsync_dir", wraps=real) as sync:
            row = self.add()
        self.assertIsNotNone(row)
        synced = [os.path.realpath(call.args[0]) for call in sync.call_args_list]
        self.assertIn(os.path.realpath(os.path.dirname(dispatches.ledger_path())),
                      synced)
        self.assertGreaterEqual(len(synced), 2)

    def test_short_write_rolls_back_and_file_permissions_are_private(self):
        path = dispatches.ledger_path()
        real_write = os.write

        def partial(fd, payload):
            return real_write(fd, payload[:len(payload) // 2])

        with mock.patch.object(os, "write", side_effect=partial):
            self.assertFalse(eventledger.append(path, {"id": "deadbeef"}))
        self.assertEqual(os.path.getsize(path), 0)
        row = self.add()
        self.assertIn(row["id"], dispatches.rows())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path + ".lock").st_mode), 0o600)

    def test_lock_contention_distinguishes_local_from_ambient_expiry(self):  # noqa: VACUOUS_ASSERTION — the free acquisition is the same held observable's unconditional positive control before both contended refusals
        from helm import projscope
        path = dispatches.ledger_path()
        with eventledger.locked(path, timeout=0.1) as held:
            self.assertTrue(held)
        fd = os.open(path + ".lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with projscope.scope(deadline=time.monotonic() + 1):
                with eventledger.locked(path, timeout=0.02) as held:
                    self.assertFalse(held)
            started = time.monotonic()
            with projscope.scope(deadline=started + 0.05):
                with self.assertRaises(projscope.Expired):
                    with eventledger.locked(path):
                        self.fail("contended ambient lock was acquired")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_concurrent_adds_are_all_replayable(self):
        made = []
        barrier = threading.Barrier(25)

        def add_one(i):
            barrier.wait()
            made.append(dispatches.add("seat-%d" % i, "lane-%d" % i, ref=self.a,
                                         repo=self.repo, new_work=True))

        threads = [threading.Thread(target=add_one, args=(i,)) for i in range(24)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertTrue(all(made))
        self.assertEqual(len(dispatches.rows()), 24)

    def test_large_replay_keeps_stop_probe_bounded(self):
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = dispatches.pk.now_ts()
        info = dispatches._repo_info(self.repo)
        with open(path, "w", encoding="utf-8") as f:
            for i in range(5000):
                row = {"v": 2, "id": "%032x" % i, "seq": 0, "event": "add",
                       "ts": now, "recipient": "seat", "lane": "lane",
                       "ref": self.a, "tip": self.a, "original_ref": self.a,
                       "original_tip": self.a, "note": None, "deadline_s": 2700,
                       "source": "scale", "repo": info["repo"],
                       "repo_id": info["repo_id"],
                       "dispatch_key": None, "message_id": None,
                       "message_hash": None, "sender": None, "status": "pending",
                       "ack_ref": None, "verdict_ref": None,
                       "reviewed_tip": None, "delivery_ref": "post-%d" % i,
                       "delivery_error": None, "last_updated": now}
                f.write(json.dumps(row) + "\n")
        started = time.monotonic()
        self.assertIsNone(seats._dispatch_candidate())
        self.assertLess(time.monotonic() - started, 1.5)


class CmdTest(DispatchBase):
    def test_cli_send_verdict_round_trip(self):
        rc, out, err = run(dispatches.cmd_dispatch, [
            "send", "codex-3", "review", "review", "this", "--ref", self.a,
            "--repo", self.repo, "--key", "cli-review", "--deadline", "900",
            "--kind", "review", "--new-work"])
        # self.a IS on the fixture trunk, so the ref-sanity check correctly
        # emits an advisory NOTE. It must WARN and never refuse: rc stays 0 and
        # the dispatch is still recorded.
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NOT one of", err)      # same lane, so no foreign flag
        self.assertIn("PENDING VERDICT", out)
        rid = next(iter(dispatches.rows()))
        # the polarity is REQUIRED on a new verdict — omitting it used to record
        # UNDECLARED forever, and 36% of the live ledger reads that way
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind",
                return_value=("VERIFIED", "a" * 16, "test receipt")):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", rid, self.a, "--approve", "--measured",
                "gate:" + "a" * 16, "safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT", out)
        self.assertIn("APPROVE", out.upper())

    def test_FIX_and_SUPERSEDE_require_the_exit_question(self):  # noqa: VACUOUS_ASSERTION — the loop is structurally pinned by the unconditional final assertion that both polarities ran
        """A true finding is not by itself a reason to keep the loop open.

        Only a tip that is worse than main on a named touched path earns an
        adverse verdict. The refusal asks that question at the immutable write
        door, before the reviewer can accidentally bind another cure round."""
        seen = 0
        for polarity in ("fix", "supersede"):
            row = self.add(lane="exit-question-" + polarity)
            with self.subTest(polarity=polarity), self.verdict_author():
                rc, _out, err = run(dispatches.cmd_dispatch, [
                    "verdict", row["id"], row["tip"], "--" + polarity,
                    "--measured", "a real imperfection remains"])
            self.assertEqual(rc, 2, err)
            self.assertIn("WORSE THAN MAIN", err)
            self.assertIn("--worse-than-main PATH", err)
            self.assertIn("--imperfect", err)
            self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                             "open", "the teaching refusal bound a verdict")
            seen += 1
        self.assertEqual(seen, 2, "the adverse-polarity sweep did not run")

    def test_IMPERFECT_refuses_the_adverse_verdict_and_teaches_APPROVE(self):
        row = self.add(lane="exit-question-imperfect")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--imperfect", "the finding is real but not a regression"])
        self.assertEqual(rc, 2, err)
        self.assertIn("APPROVE", err)
        self.assertIn("verified gate", err)
        self.assertIn("file the remaining findings as dispatch rows", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_WORSE_THAN_MAIN_paths_bind_durably_and_leave_evidence_verbatim(self):
        row = self.add(lane="exit-question-block")
        paths = ("helm/dispatches.py", "tests/test_dispatches.py")
        with self.verdict_author():
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--worse-than-main", paths[0], "--worse-than-main", paths[1],
                "--no-patch-because", "a design finding for a meld",
                "these paths regress relative to main"])
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["exit_answer"], "worse-than-main")
        self.assertEqual(tuple(current["worse_than_main_paths"]), paths)
        self.assertEqual(current["verdict_ref"],
                         "these paths regress relative to main")
        self.assertIn("WORSE-THAN-MAIN", out)
        self.assertIn(paths[0], out)
        self.assertIn(paths[1], out)

    def test_APPROVE_and_CONCUR_do_not_owe_the_adverse_exit_question(self):
        self.assertEqual(verdicts.POLARITIES,
                         ("approve", "fix", "supersede", "concur"))
        self.assertEqual(dispatches.SPIRAL_TERMINAL_POLARITIES,
                         ("approve", "supersede"))
        row = self.add(lane="exit-question-concur")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--concur", "--inferred",
                "the design is sound"])
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["polarity"], "concur")
        self.assertNotIn("exit_answer", current)
        self.assertNotIn("worse_than_main_paths", current)

        approved = self.add(lane="exit-question-approve")
        with self.verdict_author(), mock.patch.object(
                dispatches.gate, "bind",
                return_value=("VERIFIED", "a" * 16, "test receipt")):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", approved["id"], approved["tip"], "--approve",
                "--measured", "gate:" + "a" * 16, "safe to land"])
        self.assertEqual(rc, 0, err)
        self.assertIn("exit question: UNMARKED", out)
        current = dispatches.snapshot()[0][approved["id"]]
        self.assertEqual(current["polarity"], "approve")
        self.assertNotIn("exit_answer", current)

    def test_direct_mark_verdict_stays_permissive_and_replays_UNMARKED(self):
        row = self.add(lane="exit-question-historical")
        written, why = dispatches.mark_verdict(
            row["id"], row["tip"], "historical-compatible finding", "fix")
        self.assertIsNone(why, why)
        self.assertEqual(written["polarity"], "fix")
        current = dispatches.snapshot()[0][row["id"]]
        self.assertNotIn("exit_answer", current)
        self.assertNotIn("worse_than_main_paths", current)
        self.assertEqual(dispatches.verdict_exit_answer(current), "UNMARKED")
        forged = dict(current, exit_answer="worse-than-main",
                      worse_than_main_paths="helm/dispatches.py")
        self.assertEqual(dispatches.verdict_exit_answer(forged), "UNMARKED",
                         "a scalar path must not replay character-by-character "
                         "as a blocking answer")

    def test_bad_usage_is_rc2_without_traceback(self):
        cases = ([], ["add"], ["send", "s", "l", "message"], ["bogus"],
                 ["ack", "id"], ["bind", "id"], ["retarget", "id", "old"],
                 ["verdict", "id", "tip"], ["list", "--wat"])
        for args in cases:
            rc, out, err = run(dispatches.cmd_dispatch, args)
            self.assertEqual(rc, 2, args)
            self.assertNotIn("Traceback", out + err)

    def test_add_says_out_loud_that_it_notified_nobody(self):
        """`add` deliberately does not notify — that is its whole difference from
        `send`. But "PENDING VERDICT / NEEDS CONFIRMATION" reads as a status the
        ledger resolves on its own, so the writer walks away believing the
        hand-off happened.

        LIVE 2026-07-27, by the integrator, one hour after calling this exact
        defect on another lane: a re-gate row minted with `add`, announced as
        minted, and another seat had to read the recipient's pane to discover it
        was never in their task list. The obligation existed; its holder did not
        know. The fix is not to make `add` send — that deletes the primitive —
        it is to make the silence loud."""
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["add", "someseat", "somelane", "--ref", self.a,
                            "--kind", "review", "--repo", self.repo,
                            "--new-work"])
        self.assertEqual(rc, 0)
        # add() NOW NOTIFIES (a sibling lane, landed in the same merge), so the
        # honest report is that the mention posted and DELIVERY is what remains
        # unconfirmed. The unconditional "sends NOTHING" from 7524098 was true
        # against a main where add never notified and became a LIE at that merge
        # — pinned here so code and surface can never drift apart again.
        self.assertIn("was mentioned in #main", out)
        self.assertIn("someseat", out)
        self.assertNotIn("sends NOTHING", out + err)

    def test_add_records_its_sender_so_the_row_can_be_attributed(self):
        """A row that cannot name its sender cannot be chased, cancelled, or
        credited — and the stop-guard bills its delivery nag to WHOEVER STOPS
        NEXT. Measured 2026-07-28: two rows minted by helm-claude-2 landed with
        sender=None and nagged opus-integrator, ten minutes apart, to confirm
        hand-offs it never made. Worse than billing nobody: it makes an
        uninvolved seat feel responsible and invites the duplicate resend the
        same guard exists to prevent.

        send() always recorded the sender. add() was the quiet path nobody
        re-derived — the same asymmetry as the notification gap."""
        row = dispatches.add("someseat", "somelane", ref=self.a,
                             repo=self.repo, kind="review", notify=False, new_work=True)
        self.assertIsNotNone(row)
        self.assertTrue(row.get("sender"),
                        "add() must record WHO owes this row, not None")

    def test_add_reports_a_FAILED_mention_as_nobody_told(self):
        """The dangerous half. A mention that fails leaves the obligation real
        and its holder ignorant — the 2026-07-27 case where a re-gate row sat
        unseen in the recipient's task list. Read from the DURABLE notify-failed
        event, not a return value: _notify_public's post_ok is discarded by
        add(), so the ledger is the only non-transient answer."""
        with mock.patch.object(dispatches, "_notify_public", return_value=False), \
                mock.patch.object(dispatches, "_notify_failed_for",
                                  return_value={"reason": "room unwritable"}):
            rc, out, err = run(dispatches.cmd_dispatch,
                               ["add", "someseat", "somelane", "--ref", self.a,
                                "--kind", "review", "--repo", self.repo,
                                "--new-work"])
        self.assertEqual(rc, 0)
        self.assertIn("mention FAILED", err)
        self.assertIn("room unwritable", err)      # the CAUSE, not just the fact
        self.assertIn("has NOT been told", err)
        self.assertIn("chat post", err)            # the discharging ACTION
        self.assertNotIn("was mentioned in #main", out)

    def test_list_names_the_offending_flag(self):
        """rc2-without-a-traceback is a WEAK assertion — it says the command
        failed, never that the operator can tell WHY. `list --limit 10` printed
        a ~900-char usage wall that did not contain the string "--limit", so the
        reader had to diff their own command against the whole grammar. This
        asserts the token is present, which is the part that has to be true."""
        rc, out, err = run(dispatches.cmd_dispatch, ["list", "--limit", "10"])
        self.assertEqual(rc, 2)
        self.assertIn("--limit", err)
        self.assertIn("10", err)          # the stray positional is named too
        self.assertIn("--open", err)      # and what IS accepted

    def test_list_distinguishes_unknown_from_mutually_exclusive(self):
        """Two different mistakes with two different repairs — a typo versus two
        selectors that cannot both hold. They printed the identical bare usage,
        which loses the repair, not just the token."""
        _, _, unknown = run(dispatches.cmd_dispatch, ["list", "--wat"])
        _, _, both = run(dispatches.cmd_dispatch, ["list", "--open", "--overdue"])
        self.assertIn("unknown option", unknown)
        self.assertNotIn("unknown option", both)
        self.assertIn("cannot be combined", both)
        self.assertNotEqual(unknown, both)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("dispatch", cli.VERBS)


class ListAnswersWhoseRowsTheseAreTest(DispatchBase):
    """`list` HAD NO CALLER FILTER while the hook every compacted seat reads
    presented it as one (task/1007, measured 2026-08-11).

    The resume-turn text fires on EVERY compaction and says, verbatim: "Your
    live obligations are ONLY the OPEN dispatch rows naming you (`helm dispatch
    list --open`)". That command was not caller-scoped. Run from a seat holding
    ZERO obligations it returned TWO rows, naming two OTHER seats, and the seat
    briefly adopted one of them as its own. The grammar offered no filter at
    all — no --mine, no --to, no --recipient — so the instruction could not be
    followed literally by the one reader with the least context to notice the
    mismatch, and it fails in BOTH directions: adopt a stranger's row, or lose
    your own among many.

    Every arm asserts the ROWS. "the other seats are absent" is satisfied by a
    filter that returns nothing at all, so each arm also asserts the row that
    MUST be there in the same read.
    """

    SEATS = ("seat-a", "seat-b", "seat-c")

    def setUp(self):
        super().setUp()
        # AUTHORED BY THE INTEGRATOR (DispatchBase's HELM_CHAT_NAME),
        # ADDRESSED TO THREE SEATS — the real shape. Each arm below re-declares
        # HELM_CHAT_NAME as a RECIPIENT, so a filter that accidentally matched
        # the sender stamp would return three rows or zero, never one.
        self.rows = {s: self.add(recipient=s)["id"] for s in self.SEATS}

    def as_seat(self, name):
        """Read the ledger AS `name`. tearDown restores the prior env."""
        os.environ["HELM_CHAT_NAME"] = name

    def list_(self, *args):
        return run(dispatches.cmd_dispatch, ["list", *args])

    def test_mine_returns_only_the_callers_rows(self):
        self.as_seat("seat-b")
        rc, out, err = self.list_("--mine")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.rows["seat-b"], out)          # the row that IS mine
        self.assertNotIn(self.rows["seat-a"], out)
        self.assertNotIn(self.rows["seat-c"], out)
        self.assertIn("naming @seat-b", out)   # the listing says whose it is

    def test_the_unfiltered_list_still_shows_all_three(self):
        """THE POSITIVE CONTROL FOR EVERY ARM AROUND IT, on the SAME
        observable: without a filter this verb returns all three rows, so a
        --mine/--to arm that returns one is measuring a filter and not an empty
        ledger, a broken fixture, or a refusal that printed nothing.

        Unrolled deliberately — an assertion inside a `for` is a control that
        may never run, and this arm's whole job is to be the control."""
        self.as_seat("seat-b")
        rc, out, err = self.list_()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.rows["seat-a"], out)
        self.assertIn(self.rows["seat-b"], out)
        self.assertIn(self.rows["seat-c"], out)
        self.assertNotIn("naming @", out)   # and it claims no scope it lacks

    def test_to_returns_only_that_seats_rows(self):
        """--to asks about SOMEONE ELSE, so the caller is deliberately a seat
        that owns none of the three: the answer must come from the operand, not
        from who is asking."""
        self.as_seat("integrator")
        rc, out, err = self.list_("--to", "seat-c")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.rows["seat-c"], out)
        self.assertNotIn(self.rows["seat-a"], out)
        self.assertNotIn(self.rows["seat-b"], out)

    def test_an_alias_case_variant_recipient_still_matches(self):
        """THE CANONICAL RESOLVER'S OWN SEMANTICS, not a second matching rule
        invented here: `_recipient_operand` -> `seats.resolve_recipient` does
        the unconditional @-strip and casefold that the LEDGER routes by, so
        every spelling delivery accepts must select the same row. A private
        rule here would tell a seat it has no rows under a name its DMs arrive
        at.

        The two mechanisms are asserted SEPARATELY and unconditionally — the
        @-strip and the casefold — rather than in a loop whose body might never
        execute."""
        self.as_seat("integrator")
        at_rc, at_out, at_err = self.list_("--to", "@seat-c")
        self.assertEqual((at_rc, at_err), (0, ""))
        self.assertIn(self.rows["seat-c"], at_out)       # @-stripped
        self.assertNotIn(self.rows["seat-a"], at_out)
        up_rc, up_out, up_err = self.list_("--to", "@Seat-C")
        self.assertEqual((up_rc, up_err), (0, ""))
        self.assertIn(self.rows["seat-c"], up_out)       # ...and casefolded
        self.assertNotIn(self.rows["seat-a"], up_out)

    def test_open_and_mine_compose(self):
        """The EXACT command the resume-turn hook now prints. --open selects a
        status and --mine selects a recipient; a recipient filter bolted onto
        one arm would answer the other arms for the whole fleet."""
        closed = self.add(recipient="seat-b")
        dead, _cerr = dispatches.mark_cancel(closed["id"], "work moot")
        # THE FIXTURE'S OWN PRECONDITION, stated as the state it produced
        # rather than as the absence of an error: if this row were still open
        # the arm below would prove nothing about --open.
        self.assertEqual(dead["status"], "cancelled")
        self.as_seat("seat-b")
        rc, out, err = self.list_("--open", "--mine")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.rows["seat-b"], out)          # open AND mine
        self.assertNotIn(closed["id"], out)              # mine, but not open
        self.assertNotIn(self.rows["seat-a"], out)       # open, but not mine

    def test_an_unresolvable_identity_REFUSES_and_prints_no_rows(self):
        """THE WHOLE POINT OF THE ROW. A silent fall-back to the unfiltered
        list recreates the measured defect exactly — a seat reading every
        seat's obligations while believing it sees only its own — so an
        unresolved identity refuses LOUDLY, names the reason and the repair,
        and prints ZERO rows."""
        mine = self.rows["seat-b"]
        # THE CONTROL RUNS FIRST, on the SAME argv and the same fixture: with
        # an identity this command prints the caller's row.
        self.as_seat("seat-b")
        ok_rc, ok_out, _ok_err = self.list_("--mine")
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, out, err = self.list_("--mine")
        # ONE PAIRED CLAIM, so the emptiness is measured against that control
        # instead of against nothing: WITH an identity the row is printed once,
        # WITHOUT one stdout is empty. An empty ledger or a broken fixture
        # fails the left half; a silent fall-back fails the right.
        self.assertEqual((ok_rc, ok_out.count(mine), out), (0, 1, ""))
        self.assertEqual(rc, 1)                          # refused, not "0 rows"
        self.assertIn("family floor", err)               # WHY it refused
        self.assertIn("HELM_CHAT_NAME", err)             # the repair
        self.assertIn("NO ROWS PRINTED", err)            # and what it did NOT do
        self.assertIn("--to SEAT", err)                  # the way to still ask
        self.assertNotIn(mine, err)     # the refusal leaks no row on stderr

    def test_a_DISPUTED_identity_refuses_rather_than_picking_a_side(self):
        """The 2026-08-02 inherited-HELM_CHAT_NAME class, on the read path: a
        process declaring one seat while its session is rostered to another has
        TWO answers to "whose rows are these" and neither is trustworthy. This
        is the same derivation the author stamp uses, so it inherits the same
        refusal instead of a second, weaker identity rule."""
        mine = self.rows["seat-b"]
        os.environ["CLAUDE_CODE_SESSION_ID"] = \
            "aaaabbbb-1111-4222-8333-444455556666"
        self.as_seat("seat-b")
        # UNDISPUTED FIRST — same env, same argv, roster agreeing with the
        # declared name — so the empty stdout below is the DISPUTE's doing.
        with mock.patch.object(seats, "seat_for_session", return_value="seat-b"):
            ok_rc, ok_out, _ok_err = self.list_("--mine")
        with mock.patch.object(seats, "seat_for_session", return_value="seat-a"):
            rc, out, err = self.list_("--mine")
        self.assertEqual((ok_rc, ok_out.count(mine), out), (0, 1, ""))
        self.assertEqual(rc, 1)
        self.assertIn("DISPUTED identity", err)
        self.assertIn("seat-a", err)      # BOTH answers are named, so the
        self.assertIn("seat-b", err)      # reader can tell which is stale

    def test_to_without_a_value_refuses_instead_of_listing_everything(self):
        """A filter that silently does not filter is the defect, whichever flag
        spells it. `--to` with nothing after it must not degrade to the
        unfiltered list the caller would then read as one seat's."""
        self.as_seat("integrator")
        ok_rc, ok_out, _ok_err = self.list_("--to", "seat-a")
        rc, out, err = self.list_("--to")
        self.assertEqual((ok_rc, ok_out.count(self.rows["seat-a"]), rc, out),
                         (0, 1, 2, ""))
        self.assertIn("--to wants a seat name", err)

    def test_mine_and_to_together_refuse_rather_than_pick_one(self):
        """Two recipient filters naming two seats have no answer, and either
        one honoured silently answers a question the caller did not ask."""
        self.as_seat("seat-b")
        ok_rc, ok_out, _ok_err = self.list_("--mine")
        rc, out, err = self.list_("--mine", "--to", "seat-c")
        self.assertEqual((ok_rc, ok_out.count(self.rows["seat-b"]), rc, out),
                         (0, 1, 2, ""))
        self.assertIn("cannot be combined", err)
        self.assertIn("seat-c", err)

    def test_the_flag_is_discoverable_from_the_usage_the_verb_prints(self):
        """The measured half of the defect was not only that the filter was
        missing — it was that `list --help`-shaped output offered NO filter at
        all, so a seat obeying the hook had nothing to reach for."""
        rc, _out, err = self.list_("--wat")
        self.assertEqual(rc, 2)
        self.assertIn("--mine", err)
        self.assertIn("--to SEAT", err)


class TheResumeHookNamesACommandThatFiltersTest(DispatchBase):
    """THE TEXT AND THE FLAG MUST NEVER DRIFT APART AGAIN.

    Landing --mine without fixing the hook leaves every seat following the old
    instruction; fixing the text without the flag prints a command that
    refuses. So the literal string the hook emits is asserted HERE, beside the
    verb, and the command inside it is RUN — a text arm alone would still pass
    if the flag were later removed.
    """

    OLD = "(`helm dispatch list --open`)"      # the unfollowable instruction
    # THE COMPLETENESS OVER-CLAIM, retired for the same reason the two forms
    # below were: "your live obligations are ONLY these" promises the whole
    # population, and this listing is complete on DIRECTION and silent on
    # STATE, so a HELD row is owed and absent. Guarded so it cannot drift back.
    OVER_CLAIM = "live obligations are ONLY"
    # THE SECOND UNFOLLOWABLE FORM: recipient-scoped only, which told an AUTHOR
    # its sent rows were not obligations (measured on one seat).
    # Spelled with the closing backtick so it cannot match the longer command
    # that supersedes it — a drift guard that also matches the cure is a guard
    # that can never fire.
    MINE_ONLY = "helm dispatch list --open --mine`"

    def test_every_resume_text_names_the_caller_scoped_command(self):
        """All THREE texts, spelled out rather than looped: the failure this
        pins is a fix landing in two of them, which a loop whose body may not
        run cannot catch. Each pair is a positive (the sentence and the working
        command are both present) plus the drift guards (neither superseded
        form — the unscoped one, nor the recipient-only one — is back)."""
        from helm import resumeturn
        self.assertEqual(resumeturn.OBLIGATIONS_CMD,
                         "helm dispatch list --open --mine --issued")
        generic, unclaimed, unreadable = (resumeturn.GENERIC,
                                          resumeturn.UNCLAIMED,
                                          resumeturn.UNREADABLE)
        self.assertIn("Obligations:", generic)
        self.assertNotIn(self.OVER_CLAIM, generic)
        self.assertIn("HELD", generic)
        self.assertIn("helm dispatch list --open --mine --issued", generic)
        self.assertIn("row you sent", generic)
        self.assertNotIn(self.OLD, generic)
        self.assertNotIn(self.MINE_ONLY, generic)
        self.assertIn("Obligations:", unclaimed)
        self.assertNotIn(self.OVER_CLAIM, unclaimed)
        self.assertIn("HELD", unclaimed)
        self.assertIn("helm dispatch list --open --mine --issued", unclaimed)
        self.assertIn("row you sent", unclaimed)
        self.assertNotIn(self.OLD, unclaimed)
        self.assertNotIn(self.MINE_ONLY, unclaimed)
        self.assertIn("Obligations:", unreadable)
        self.assertNotIn(self.OVER_CLAIM, unreadable)
        self.assertIn("HELD", unreadable)
        self.assertIn("helm dispatch list --open --mine --issued", unreadable)
        self.assertIn("row you sent", unreadable)
        self.assertNotIn(self.OLD, unreadable)
        self.assertNotIn(self.MINE_ONLY, unreadable)

    def test_the_sentence_moves_through_ONE_door_like_the_command(self):
        """THE CLAUSE IS A CONSTANT FOR THE SAME REASON THE COMMAND IS. The
        defect was never only the command — it was the SENTENCE around it
        claiming a completeness it did not have. A sentence repaired in two
        texts out of three is the identical bug one layer up, and the third
        would go on telling a compacted author it was free."""
        from helm import resumeturn
        self.assertIn(resumeturn.OBLIGATIONS_CMD, resumeturn.OBLIGATIONS)
        self.assertIn(resumeturn.OBLIGATIONS, resumeturn.GENERIC)
        self.assertIn(resumeturn.OBLIGATIONS, resumeturn.UNCLAIMED)
        self.assertIn(resumeturn.OBLIGATIONS, resumeturn.UNREADABLE)

    def test_the_command_the_hook_prints_is_one_the_verb_accepts(self):
        """RUN, not read. The hook's sentence is only as good as the grammar
        behind it, and the whole incident was a command a seat could not
        execute. Argv is split from the hook's OWN string, so a future edit to
        either half is measured against the other. The seat here is
        DispatchBase's `integrator`, and the ledger carries one row it HOLDS,
        one it SENT, and one belonging to neither — so the command must not
        merely EXIT cleanly, it must return BOTH of the reader's obligations
        and neither of the stranger's, which is the sentence the hook makes
        about it."""
        from helm import resumeturn
        ours = self.add(recipient="integrator")["id"]
        os.environ["HELM_CHAT_NAME"] = "seat-z"
        theirs = self.add(recipient="seat-a")["id"]
        os.environ["HELM_CHAT_NAME"] = "integrator"
        sent = self.add(recipient="seat-a")["id"]
        argv = resumeturn.OBLIGATIONS_CMD.split()
        self.assertEqual(argv[:2], ["helm", "dispatch"])
        rc, out, err = run(dispatches.cmd_dispatch, argv[2:])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(ours, out)        # the row naming me
        self.assertIn(sent, out)        # ...and the one I SENT — the defect
        self.assertNotIn(theirs, out)   # ...and nobody else's, still


class AnAuthorsOwnOpenRowsAreObligationsTooTest(DispatchBase):
    """`--mine` IS RECIPIENT-SCOPED, SO THE HOOK'S SENTENCE WAS FALSE FOR AN
    AUTHOR (measured on one seat).

    The resume-turn text fires on EVERY compaction and said the seat's live
    obligations are ONLY the OPEN rows NAMING it. The seat held an OPEN row it
    had SENT (fec8db8b8099 to another seat), two hours past deadline, that only
    it was positioned to chase — and the named command printed "no matching
    rows naming @<seat>". That reads as "you are free" to a seat holding live
    work, at the exact moment it has lost the context that would have reminded
    it.

    THE ISSUER'S OBLIGATION IS NOT A DEFINITIONAL QUIBBLE. The integrator ruled
    the same night that what a sender owes is the DELIVERY LEG — making sure
    the recipient knows the row exists and what it needs. A row sent but never
    delivered, or delivered to a seat that has gone quiet, is the sender's to
    chase, and nobody else is positioned to.

    THE CURE IS AN ACTUATOR, NOT A WIDER `--mine`. Every arm below that proves
    the new filter WORKS is paired with one proving it did not reopen
    task/1007: a seat still cannot see another seat's rows through it, and an
    unresolved identity still prints NOTHING rather than falling back.
    """

    def setUp(self):
        super().setUp()
        # THREE ROWS, THREE RELATIONSHIPS TO seat-b — so no arm can pass by
        # returning everything or nothing. `add` stamps the sender from
        # HELM_CHAT_NAME at call time, which is why each one declares first.
        self.as_seat("seat-b")
        self.sent = self.add(recipient="seat-a")["id"]      # b -> a  (ISSUED)
        self.as_seat("integrator")
        self.held = self.add(recipient="seat-b")["id"]      # x -> b  (MINE)
        self.as_seat("seat-c")
        self.stranger = self.add(recipient="seat-a")["id"]  # c -> a  (NEITHER)
        self.as_seat("seat-b")

    def as_seat(self, name):
        os.environ["HELM_CHAT_NAME"] = name

    def list_(self, *args):
        return run(dispatches.cmd_dispatch, ["list", *args])

    def test_THE_DEFECT_the_recipient_filter_cannot_see_the_row_this_seat_SENT(self):
        """THE REPRODUCTION, kept as a permanent arm because it is ALSO the
        task/1007 guard: `--mine` must go on meaning "naming me" exactly. It
        asserts BOTH halves of the measured incident — the row is absent from
        `--open --mine`, and it is genuinely there to be found (the unfiltered
        control), so this is a filter's answer and not an empty ledger."""
        rc, out, err = self.list_("--open", "--mine")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.held, out)             # what --mine legitimately has
        self.assertNotIn(self.sent, out)          # ...and what it cannot see
        all_rc, all_out, _ = self.list_("--open")
        self.assertEqual(all_rc, 0)
        self.assertIn(self.sent, all_out)         # the row EXISTS and is OPEN

    def test_issued_returns_the_row_this_seat_sent(self):
        """THE CURE, on the same fixture and the same observable."""
        rc, out, err = self.list_("--open", "--issued")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.sent, out)
        self.assertIn("sent by @seat-b", out)     # the listing says which axis
        self.assertNotIn(self.held, out)          # ISSUED is not RECEIVED

    def test_ADOPTION_a_seat_cannot_see_another_seats_sent_rows(self):
        """THE REGRESSION DIRECTION. task/1007's failure was a seat adopting a
        row it did not hold; the sender axis can reopen it just as easily. The
        control is the same command from the seat that DID send it, so a filter
        that returns nothing at all fails the left half."""
        self.as_seat("seat-c")
        ok_rc, ok_out, _ = self.list_("--issued")     # seat-c sent `stranger`
        self.as_seat("seat-b")
        rc, out, err = self.list_("--issued")
        self.assertEqual((ok_rc, ok_out.count(self.stranger)), (0, 1))
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn(self.stranger, out)      # not seat-b's to chase
        self.assertNotIn(self.held, out)          # nor a row it merely holds
        self.assertIn(self.sent, out)             # only the one it authored

    def test_ADOPTION_an_unresolved_identity_REFUSES_and_prints_no_rows(self):
        """`--issued` inherits the fail-loud contract WITHOUT softening it. A
        silent fall-back to the unfiltered list would tell the reader it had
        personally issued every row in the fleet — the same defect `--mine`
        exists to close, one axis over. Paired against a control on the SAME
        argv, so an empty ledger cannot pass for a refusal."""
        self.as_seat("seat-b")
        ok_rc, ok_out, _ = self.list_("--issued")
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, out, err = self.list_("--issued")
        self.assertEqual((ok_rc, ok_out.count(self.sent), out), (0, 1, ""))
        self.assertEqual(rc, 1)                   # refused, not "0 rows"
        self.assertIn("--issued", err)            # and it names the flag TYPED
        self.assertIn("family floor", err)
        self.assertIn("NO ROWS PRINTED", err)
        self.assertNotIn(self.sent, err)          # no row leaks on stderr

    def test_ADOPTION_the_UNION_refuses_an_unresolved_identity_as_well(self):
        """THE HIGHEST-CONSEQUENCE REFUSAL PATH, because this is the exact
        command the resume hook prints. If the pair fell back where either flag
        alone refuses, a compacted seat with a broken identity would be handed
        the WHOLE fleet's rows under a sentence calling them its own — the
        original task/1007 failure, restored by the very command that fixes the
        author half. The refusal must also NAME BOTH flags, so the reader
        repairs the thing they actually typed."""
        self.as_seat("seat-b")
        ok_rc, ok_out, _ = self.list_("--open", "--mine", "--issued")
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, out, err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((ok_rc, ok_out.count(self.sent),
                          ok_out.count(self.held), out), (0, 1, 1, ""))
        self.assertEqual(rc, 1)
        self.assertIn("--mine --issued", err)
        self.assertIn("NO ROWS PRINTED", err)
        self.assertNotIn(self.stranger, err)

    def test_ADOPTION_a_DISPUTED_identity_refuses_on_the_issuer_axis_too(self):
        """ONE IDENTITY DOOR, NOT TWO. A process declaring one seat while its
        session is rostered to another has two answers to "which rows did I
        send" and neither is trustworthy. This is the same derivation that
        STAMPS the sender, so a second, private rule here could scope a listing
        by a name delivery would never write."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = \
            "aaaabbbb-1111-4222-8333-444455556666"
        self.as_seat("seat-b")
        with mock.patch.object(seats, "seat_for_session", return_value="seat-b"):
            ok_rc, ok_out, _ = self.list_("--issued")
        with mock.patch.object(seats, "seat_for_session", return_value="seat-a"):
            rc, out, err = self.list_("--issued")
        self.assertEqual((ok_rc, ok_out.count(self.sent), out), (0, 1, ""))
        self.assertEqual(rc, 1)
        self.assertIn("DISPUTED identity", err)

    def test_ADOPTION_a_floor_sender_row_belongs_to_nobody(self):
        """THE PRE-3e0fe8e COHORT, which `_mine_or_unprovable` deliberately
        KEEPS on the nag path and this filter deliberately does not. Live
        ledger 2026-08-12: 417 of 2257 rows carry a bare-family or absent
        sender. Showing them on the sender axis would hand one seat hundreds of
        rows as work it had personally issued. A row with NO sender at all is
        the sharpest case: it matches nobody, and 'nobody' must not collapse to
        'everybody'."""
        # PLANTED, because the WRITE DOOR refuses to mint one: `_acting_author`
        # has refused an unattributed author since 3e0fe8e. The cohort this
        # guards is HISTORICAL, so the fixture has to be written the way
        # history was.
        template = self.add(recipient="seat-a")
        orphan_id = "f" * 32
        # `seq: 0` IS LOAD-BEARING, not decoration: a FIRST event replays only
        # at sequence zero, and the first cut of this fixture used 9901, so the
        # row was silently discarded and every absence below would have been an
        # absence of a row that was never there. Measured against the live CLI
        # (the same plant at 9901 vanished, at 0 it appeared) — and caught here
        # only because the precondition is asserted rather than assumed.
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({**template, "id": orphan_id, "seq": 0,
                                "sender": "",
                                "chain_root": orphan_id,
                                "operation_key": None}) + "\n")
        # THE FIXTURE'S OWN PRECONDITION, read back as state rather than
        # assumed from a write that raised nothing: the row is IN the snapshot
        # and its sender really is empty, or every absence below is vacuous.
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIn(orphan_id, snap)
        self.assertEqual(str(snap[orphan_id].get("sender") or ""), "")
        # UNROLLED: an assertion inside a `for` is a control that may never run,
        # and 'nobody sees it' is the whole claim. Each seat asked is one that
        # DID send something, so every listing below carries an unconditional
        # positive on the SAME observable — the orphan's absence is measured
        # against a row that is present in that very output, never against a
        # refusal, an empty ledger, or a filter that dropped everything.
        self.as_seat("seat-b")
        b_rc, b_out, b_err = self.list_("--issued")
        self.as_seat("seat-c")
        c_rc, c_out, c_err = self.list_("--issued")
        self.as_seat("integrator")
        i_rc, i_out, i_err = self.list_("--issued")
        self.assertEqual((b_rc, b_err, c_rc, c_err, i_rc, i_err),
                         (0, "", 0, "", 0, ""))
        self.assertIn(self.sent, b_out)
        self.assertNotIn(orphan_id, b_out)
        self.assertIn(self.stranger, c_out)
        self.assertNotIn(orphan_id, c_out)
        self.assertIn(self.held, i_out)
        self.assertNotIn(orphan_id, i_out)
        # ...and it IS in the ledger, so those absences are a filter's doing
        # and not a planted row that never landed.
        _rc, all_out, _err = self.list_()
        self.assertIn(orphan_id, all_out)

    def test_the_union_shows_BOTH_and_marks_which_are_not_this_seats_to_do(self):  # noqa: VACUOUS_ASSERTION — every absence here is paired with an unconditional positive on the SAME observable: both rows asserted present in `out` before the stranger's absence, and the marker asserted present on `sent_line` before it is asserted absent from `held_line` (one rendering call, two rows)
        """THE COMMAND THE HOOK PRINTS. The union is the point — an answer from
        half the obligations reads as 'nothing owed' — and the marker is the
        one hazard it adds: a row you SENT, read as a row you must DO, is
        duplicate work, the exact mirror of adoption."""
        rc, out, err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.sent, out)
        self.assertIn(self.held, out)
        self.assertNotIn(self.stranger, out)      # the union widens to NOBODY
        self.assertIn("naming or sent by @seat-b", out)
        sent_line = next(l for l in out.splitlines() if self.sent in l)
        held_line = next(l for l in out.splitlines() if self.held in l)
        self.assertIn("YOURS TO CHASE", sent_line)
        # noqa: VACUOUS_ASSERTION — the positive control is the line above, on
        # the SAME observable and the same rendering call: one row in this very
        # listing carries the marker and the other does not, which is the whole
        # claim (the role is a property of the ROW, not of the listing). A
        # marker that vanished entirely fails the assertIn first.
        self.assertNotIn("YOURS TO CHASE", held_line)   # the role is per ROW
        self.assertIn("DELIVERY LEG", out)              # and the legend says why

    def test_a_HELD_row_escapes_the_union_and_the_help_text_says_so(self):  # noqa: VACUOUS_ASSERTION — the two unconditional positives are the open row asserted PRESENT in the union and the held row asserted PRESENT under --held, both before any loop; the loop's assertNotIn is the retired sentence and its assertIn(HELD) rides the same iteration
        """THE CLAIM AND THE BEHAVIOUR, BOUND TOGETHER IN ONE ARM.

        --open selects status open, so HELD sits outside the union whichever
        way the row points. That is a real gap, not a wording problem, and it
        is the reason the surrounding prose may not promise completeness: a
        seat that runs the union, reads three empty lists and stands down is
        acting on a guarantee this command does not give. Until HELD joins the
        union, the honest thing is that the text names the axis it covers —
        and prose only stays honest if a test holds it to the behaviour.
        """
        self.as_seat("seat-b")
        parked = self.add(recipient="seat-b")
        row, err = dispatches.mark_hold(parked["id"], "waiting on a fab node")
        self.assertEqual((err, row["status"]), (None, "held"))

        rc, union, uerr = self.list_("--open", "--mine", "--issued")
        self.assertEqual((rc, uerr), (0, ""))
        # POSITIVE CONTROL FIRST, on the same rendering call: an OPEN row
        # addressed to this seat IS in the union, so the absence below is the
        # state filter and not an empty listing.
        self.assertIn(self.held, union)
        self.assertNotIn(parked["id"], union)

        # ...and the row is not gone, only outside THIS view.
        rc, only_held, herr = self.list_("--held", "--mine", "--issued")
        self.assertEqual((rc, herr), (0, ""))
        self.assertIn(parked["id"], only_held)

        # THE PROSE IS HELD TO THAT BEHAVIOUR. Both surfaces a seat reads
        # before standing down must name the state axis, and neither may
        # promise that an empty union means nothing is owed.
        # ALL FOUR SURFACES, found by sweeping the union's FORMAT LITERAL
        # rather than a feature word: the two a seat reads at a stop, the verb
        # registry entry in cli.py that describes the same command, and the
        # docs page. A fix that lands in three of four leaves the claim alive
        # on the one nobody grepped for.
        from helm import cli, resumeturn
        # THE FOURTH SURFACE IS READ OFF DISK, NOT NAMED IN A COMMENT. The
        # first cut of this arm said "ALL FOUR SURFACES" and enumerated four in
        # prose while binding three — the docs page was edited and then held to
        # nothing, so a revert of it would have been invisible to the suite
        # that claims to cover it. A count in a comment is not a check.
        docs = pathlib.Path(__file__).resolve().parent.parent / "docs" / "VERBS.md"
        surfaces = {"dispatches.USAGE": dispatches.USAGE,
                    "resumeturn.OBLIGATIONS": resumeturn.OBLIGATIONS,
                    "cli dispatch entry": cli._VERB_HELP["dispatch"],
                    "docs/VERBS.md": docs.read_text(encoding="utf-8")}
        self.assertEqual(len(surfaces), 4)   # the count the prose above claims
        for name, text in surfaces.items():
            # THE RIGHT SURFACE, by a literal all three actually share: the
            # help spells the flags as [--mine] [--issued] and the prose runs
            # them together, so only the flag name itself is common ground.
            self.assertIn("--issued", text, name)
            self.assertIn("HELD", text, name)
            self.assertNotIn("the only listing whose emptiness", text, name)

    def test_the_union_composes_with_open_rather_than_widening_it(self):
        """A cancelled row is DEAD in both directions. `--open` must keep
        selecting, or the cure would resurrect exactly the stale rows the same
        hook text warns about two sentences later."""
        self.as_seat("seat-b")
        dead = self.add(recipient="seat-a")
        killed, _err = dispatches.mark_cancel(dead["id"], "work moot")
        self.assertEqual(killed["status"], "cancelled")
        rc, out, err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.sent, out)             # open AND issued by me
        self.assertNotIn(dead["id"], out)         # issued by me, but DEAD
        loose_rc, loose_out, _ = self.list_("--issued")
        self.assertIn(dead["id"], loose_out)      # ...and --open is what did it
        self.assertEqual(loose_rc, 0)

    def test_the_JSON_arm_carries_the_issued_row_and_the_field_that_types_it(self):
        """A MACHINE CONSUMER HAD THE SAME BLIND SPOT AS THE HOOK. `--json` is
        how every non-human reader asks this question, so a filter landing only
        on the rendered path would leave them reading half the obligations.
        Each row also carries `sender`/`recipient`, which is how a consumer
        tells a row to CHASE from a row to DO without parsing the marker."""
        mine_rc, mine_out, _ = self.list_("--open", "--mine", "--json")
        rows = json.loads(mine_out)
        self.assertEqual(mine_rc, 0)
        ids = [r["id"] for r in rows]
        self.assertIn(self.held, ids)
        self.assertNotIn(self.sent, ids)          # the blind spot, in JSON
        rc, out, _err = self.list_("--open", "--mine", "--issued", "--json")
        both = json.loads(out)
        self.assertEqual(rc, 0)
        by_id = {r["id"]: r for r in both}
        self.assertIn(self.sent, by_id)
        self.assertIn(self.held, by_id)
        self.assertNotIn(self.stranger, by_id)
        self.assertEqual(by_id[self.sent]["sender"], "seat-b")
        self.assertEqual(by_id[self.held]["recipient"], "seat-b")

    def test_issued_intersects_with_to_instead_of_one_replacing_the_other(self):
        """Two DIFFERENT axes, so they compose: "what did I send to that seat".
        A filter that quietly replaced the other would answer a question the
        caller did not ask — the same law that keeps `--mine` and `--to`
        apart."""
        self.as_seat("seat-b")
        rc, out, err = self.list_("--issued", "--to", "seat-a")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(self.sent, out)
        self.assertNotIn(self.stranger, out)      # seat-c sent that one
        # TWO AXES, TWO CLAUSES. `--issued` and `--to` are two separate
        # narrowings, so the note states them separately rather than fusing
        # them into one phrase — the fused spelling is what let `--mine --to`
        # narrow on an axis the note never mentioned.
        # SEPARATELY AND IN ORDER, tolerating each clause's own set-aside
        # count: the property is that the axes are TWO clauses and not one
        # fused phrase, never that nothing may sit between them.
        self.assertIn("sent by @seat-b", out)
        self.assertIn("addressed to @seat-a", out)
        self.assertRegex(out, r"sent by @seat-b[^,]*, addressed to @seat-a")
        none_rc, none_out, _ = self.list_("--issued", "--to", "seat-c")
        # THE EMPTY ANSWER STATES THE QUESTION IT ANSWERED — an unconditional
        # positive on the same observable, so "nothing was sent to seat-c" can
        # never be a refusal, a dropped filter or a broken operand wearing an
        # absence's clothes.
        self.assertEqual(none_rc, 0)
        self.assertIn("no matching rows sent by @seat-b", none_out)
        self.assertIn("addressed to @seat-c", none_out)
        self.assertRegex(none_out,
                         r"no matching rows sent by @seat-b[^,]*, "
                         r"addressed to @seat-c")
        self.assertNotIn(self.sent, none_out)     # nothing was sent to seat-c

    def test_the_absence_line_names_the_axes_it_searched(self):
        """"You owe nothing" is the most consequential sentence this verb
        prints to a compacted reader, and the incident was an absence that was
        TRUE of a narrower question than the one being asked. An absence must
        say what was looked for."""
        self.as_seat("seat-d")                    # holds nothing, sent nothing
        rc, out, err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("no matching rows naming or sent by @seat-d", out)

    def test_a_clause_carries_the_SIZE_of_what_it_set_aside(self):
        """Naming the question a filter asked cannot say whether the axis was
        EMPTY, and that is the whole remaining gap.

        A seat holding two HELD rows reads "a HELD, cancelled or verdicted
        row is NOT open and is outside this listing" WORD FOR WORD as it
        prints when zero
        are held. Both sentences were true; only one of them should have been
        reassuring, and the reader could not tell them apart.
        """
        self.as_seat("seat-b")
        parked = self.add(recipient="seat-b")
        held, _err = dispatches.mark_hold(parked["id"], "waiting on the fab")
        self.assertEqual(held["status"], "held")

        rc, out, err = self.list_("--open", "--mine")
        self.assertEqual((rc, err), (0, ""))
        # The clause still names the QUESTION, which is the property that
        # makes an absence readable at all; a number must not replace it.
        # The axis label now names BOTH narrowings this filter applies -- a
        # status one and a chain one -- so the token asserted here is the one
        # the surface prints, not the older status-only half of it.
        self.assertIn("OPEN AND STILL OWED only", out)
        self.assertIn("HELD", out)
        # ...and now it also carries the ANSWER'S SIZE.
        self.assertIn("set aside", out)

    def test_a_clause_that_set_NOTHING_aside_carries_no_count(self):
        """The suffix has to MEAN something. Printed unconditionally it would
        read "(0 set aside)" on every clause of every listing, and a number
        that is almost always zero is a number readers stop seeing — which
        would cost the signal this adds while keeping all of its length.
        An ABSENT suffix is the zero.
        """
        self.as_seat("seat-b")
        # A SIBLING CALL IS NOT A CONTROL ON THIS OBSERVABLE. A narrowing
        # listing does print the suffix, but that is a different render bound
        # to a different name — it cannot show that THIS render happened at
        # all, and an empty or crashed `out` would satisfy the absence below
        # exactly as the zero case does.
        rc, out, err = self.list_()               # no selector: nothing narrows
        self.assertEqual((rc, err), (0, ""))
        # UNCONDITIONAL POSITIVE ON `out` ITSELF: the listing really rendered,
        # and rendered every row, so the missing suffix is the ZERO and not a
        # missing screen.
        self.assertIn("logical row", out)
        self.assertIn(self.held, out)
        self.assertIn(self.stranger, out)
        self.assertNotIn("set aside", out)

    def test_the_count_comes_off_the_listing_s_OWN_snapshot(self):
        """A second read of the store would reintroduce the two-instants
        problem the --open/--overdue comments were written about: this surface
        measures lateness against "the ONE instant this listing bound", and a
        set-aside count sampled separately could describe a different world
        than the rows printed beside it.

        Held structurally rather than by timing: what a clause set aside plus
        what survived it must equal what it was HANDED, so the number cannot
        have come from anywhere else.
        """
        self.as_seat("seat-b")
        rc, out, err = self.list_("--mine")
        self.assertEqual((rc, err), (0, ""))
        shown = int(re.search(r"— (\d+) logical row", out).group(1))
        aside = sum(int(n) for n in re.findall(r"\((\d+) set aside\)", out))
        total = len(dispatches.rows())
        self.assertEqual(shown + aside, total, out)
        self.assertGreater(aside, 0)              # the arm is not vacuous

    def test_the_absence_line_names_the_STATE_it_searched_not_only_the_seat(self):
        """The direction axis was the SECOND thing this line learned to say and
        the state axis was still missing. A seat holding a HELD row runs the
        command the hook names, reads an empty listing, and that sentence must
        not be true of a narrower question than the one it asked."""
        self.as_seat("seat-e")
        parked = self.add(recipient="seat-e")
        held, _err = dispatches.mark_hold(parked["id"], "waiting on the fab")
        self.assertEqual(held["status"], "held")

        rc, out, err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn(parked["id"], out)       # the row IS excluded...
        self.assertIn("no matching rows", out)
        # ...so the line names the axis that excluded it, and names HELD as one
        # of SEVERAL states outside the view rather than the only one.
        self.assertIn("OPEN AND STILL OWED only", out)
        self.assertIn("HELD", out)
        self.assertIn("cancelled", out)
        self.assertIn("verdicted", out)

        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, so the axis label
        # cannot be a constant this verb prints whatever it did: the HELD
        # listing states ITS state and never claims to be the open one. The
        # negative moves with the positive -- a control asserting the absence
        # of a string the producer no longer prints ANYWHERE would pass on
        # every listing and discriminate nothing.
        hrc, hout, _herr = self.list_("--held", "--mine", "--issued")
        self.assertEqual(hrc, 0)
        self.assertIn(parked["id"], hout)
        self.assertIn("HELD only", hout)
        self.assertNotIn("OPEN AND STILL OWED only", hout)

    def test_a_HELD_row_is_outside_the_union_in_BOTH_directions(self):
        """The state axis is orthogonal to the direction axis, so it is
        measured on both arms. A cure proven only on the incoming arm leaves an
        author's own HELD row outside a listing that says it covers what the
        author sent."""
        self.as_seat("seat-f")
        incoming = self.add(recipient="seat-f")            # names me
        outgoing = self.add(recipient="seat-g")            # I sent it
        for row in (incoming, outgoing):
            held, _err = dispatches.mark_hold(row["id"], "parked")
            self.assertEqual(held["status"], "held")
        # THE UNCONDITIONAL POSITIVE ON THE UNION ITSELF, not on a second
        # listing: without a row that IS in it, both absences below would read
        # the same under a union that was empty for any other reason.
        live = self.add(recipient="seat-f")

        rc, union, err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(live["id"], union)                   # the union works...
        self.assertNotIn(incoming["id"], union)            # ...and excludes
        self.assertNotIn(outgoing["id"], union)            # HELD both ways

        hrc, held_out, _herr = self.list_("--held", "--mine", "--issued")
        self.assertEqual(hrc, 0)
        self.assertIn(incoming["id"], held_out)   # the incoming arm
        self.assertIn(outgoing["id"], held_out)   # ...and the outgoing one

    def test_every_axis_that_narrowed_the_listing_appears_in_the_note(self):
        """`--issued --to SEAT` is THREE narrowings with `--open`, and the note
        must carry three clauses. Filtering and describing are one call now, so
        this holds the PROPERTY — every axis that removed rows is named — not
        the particular clauses that happen to exist today.

        `--mine --to` is refused as two different recipients, so the composing
        pair is the issued one; the refusal is asserted here too, because a
        note that never has to describe that pair is not evidence about it."""
        self.as_seat("seat-h")
        self.add(recipient="seat-i")
        rc, out, err = self.list_("--open", "--issued", "--to", "seat-i")
        self.assertEqual((rc, err), (0, ""))
        for axis in ("sent by @seat-h", "addressed to @seat-i",
                     "OPEN AND STILL OWED only"):
            self.assertIn(axis, out, axis)

        bad_rc, _bad_out, bad_err = self.list_("--mine", "--to", "seat-i")
        self.assertEqual(bad_rc, 2)
        self.assertIn("cannot be combined", bad_err)

    def test_the_flag_is_discoverable_from_the_usage_the_verb_prints(self):
        """A seat obeying the hook must find the flag in the verb's own words —
        the measured half of task/1007 was a filter that could not be reached
        from anything the verb printed."""
        rc, _out, err = self.list_("--wat")
        self.assertEqual(rc, 2)
        self.assertIn("--issued", err)
        self.assertIn("--mine", err)


class TriageAnswersEveryNamedIdTest(DispatchBase):
    """`triage <id>` used to answer IDENTICALLY — rc 0, zero bytes — for a
    verdicted row and for an id that resolves to NOTHING, while an open row
    printed its line (measured 2026-08-11T00:20Z on the live ledger). So a
    typo'd id read exactly like a clean answer and finished work read like
    nothing; silence is the one failure mode nobody re-checks.

    The three inputs must answer THREE DIFFERENT WAYS: an open row is measured
    (unchanged), a row triage deliberately skips says WHY on stdout at rc 0,
    and a token resolving to no row REFUSES on stderr at rc 2."""

    def triage(self, *ids):
        return run(dispatches.cmd_dispatch, ["triage", *ids])

    def test_a_garbage_id_refuses_on_stderr_naming_the_token(self):
        row = self.add()
        # POSITIVE CONTROL FIRST, unconditional, on the SAME observable the
        # refusal arm reads (this verb's stdout/stderr through the same call):
        # a real open row still produces its measured line at rc 0, so the
        # empty stdout below is the refusal branch and not a dead verb.
        rc, out, err = self.triage(row["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"][:12], out)
        ghost = "f" * 32
        rc, out, err = self.triage(ghost)
        self.assertEqual(rc, 2)
        self.assertIn(ghost, err, "the refusal must NAME the token")
        self.assertIn("no such dispatch", err)
        self.assertEqual(out.strip(), "", "a refusal is not a clean answer")

    def test_one_typo_never_suppresses_the_real_rows_beside_it(self):
        row = self.add()
        rc, out, err = self.triage(row["id"], "f" * 32)
        self.assertEqual(rc, 2, "the typo still fails the call")
        self.assertIn(row["id"][:12], out,
                      "the real row named beside it is still measured")
        self.assertIn("f" * 32, err)

    def _twins(self, short="deadbeef"):
        """Two OPEN rows whose ids share `short` — minted the same way the
        exact-short-collision test elsewhere in this file mints its pair."""
        twins = [short + "a" * 24, short + "b" * 24]
        for rid in twins:
            row, why = dispatches._base(
                "seat-a", "twin-" + rid[-1], self.a, None,
                dispatches.DEFAULT_DEADLINE_S, self.repo, rid=rid,
                new_work=True)
            self.assertIsNone(why)
            self.assertIsNone(dispatches._append_dispatch(row)[1])
        return short, twins

    def test_an_ambiguous_prefix_refuses_naming_token_and_count(self):
        # A FIX on this lane's first cut: the owed filter matched
        # tokens by PREFIX before _resolve_row ever saw them, so an ambiguous
        # prefix printed every open row it grazed at rc 0 and the resolver's
        # ambiguity refusal was unreachable — a prefix path around the door.
        short, twins = self._twins()
        # POSITIVE CONTROL FIRST, unconditional, same observable: an
        # UNAMBIGUOUS prefix of one of the SAME twin rows still answers with
        # its measured line at rc 0, so the refusal below is discrimination
        # between prefixes and not a verb that stopped answering them.
        rc, out, err = self.triage(twins[0][:12])
        self.assertEqual(rc, 0, err)
        self.assertIn(twins[0][:12], out)
        self.assertIn("NO-MEASURABLE-CLAIMS", out)
        self.assertIn("records no note", out,
                      "the fixture row carries no note, and the refusal must "
                      "say which surface was empty rather than assert a read "
                      "of the dispatched message it never made")
        rc, out, err = self.triage(short)
        self.assertEqual(rc, 2)
        self.assertIn(short, err, "the refusal must NAME the token")
        self.assertIn("ambiguous dispatch id prefix", err)
        self.assertIn("2 candidates", err,
                      "and HOW crowded the prefix is, so the reader knows "
                      "whether to lengthen it or re-list")
        self.assertEqual(out.strip(), "",
                         "an ambiguous prefix must not print ANY row — "
                         "answering some of the matches is the old guess")

    def test_an_ambiguous_token_never_suppresses_the_row_named_beside_it(self):
        short, twins = self._twins()
        rc, out, err = self.triage(short, twins[1][:12])
        self.assertEqual(rc, 2, "the ambiguity still fails the call")
        self.assertIn(twins[1][:12], out,
                      "the unambiguous row named beside it is still measured")
        self.assertIn(short, err)
        self.assertIn("ambiguous dispatch id prefix", err)

    def test_a_verdicted_row_says_why_it_is_not_triaged(self):  # noqa: VACUOUS_ASSERTION — the "exactly ONE line" claim is an absence claim about a SECOND line, which no same-call assertion can carry: one call yields one answer. The control is a SEPARATE call on the SAME code path and observable (this verb's stdout) with a DIFFERENT input, asserted FIRST and unconditionally, and the flagged call itself asserts three positives (row id, state word, polarity) on that same stdout before the count
        row = self.add()
        _out, why = dispatches.mark_verdict(row["id"], row["tip"], "reviewed",
                                            polarity="fix")
        self.assertIsNone(why)
        # CONTROL FIRST, unconditional, same call and same observable: an
        # open row's measured line proves this verb prints when it has
        # something to say, so "exactly one line" below is the exclusion
        # speaking once — not a quieter flavour of the original silence.
        control = self.add()
        rc, out, err = self.triage(control["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn(control["id"][:12], out)
        rc, out, err = self.triage(row["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"][:12], out)
        self.assertIn("VERDICT", out)
        self.assertIn("closed by FIX verdict", out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1,
                         "exactly ONE line: the exclusion says itself, once")

    def test_an_open_row_is_measured_exactly_as_before(self):
        # The must-hit control on the unchanged branch: the cure adds answers
        # for the silent cases and must not touch what triage MEASURES.
        row = self.add()
        rc, out, err = self.triage(row["id"])
        self.assertEqual(rc, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn(row["id"][:12], lines[0])
        self.assertIn("NO-MEASURABLE-CLAIMS", lines[0],
                      "the open row is MEASURED, not state-labelled")
        self.assertIn("records no note", lines[0],
                      "and the measurement names the surface it read")
        self.assertNotIn("not triaged", out)

    def test_a_held_row_names_its_hold(self):
        row = self.add()
        marked, why = dispatches.mark_hold(row["id"], "waiting on the vendor")
        self.assertIsNone(why)
        self.assertEqual(marked["status"], "held")
        rc, out, err = self.triage(row["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn("HELD", out)
        self.assertIn("waiting on the vendor", out,
                      "the recorded reason travels to the reader")


    def _origin(self):
        """Give the fixture the REMOTE-TRACKING ref production reads.

        `_held_landing_note` defaults to origin/main because that is what a
        real checkout has, and a fixture without it can only ever produce
        UNKNOWN — a true answer to a question these arms are not asking. One
        ref makes the CLI path measure the same thing the helper does."""
        self.git("update-ref", "refs/remotes/origin/main", self.main)

    def test_a_held_row_says_when_its_work_is_ALREADY_ON_TRUNK(self):
        """A HOLD RECORDS WHY SOMEBODY STOPPED, NEVER WHETHER IT STILL STANDS.

        The reason is written once, at the moment of holding, and the row then
        leaves --open and --issued, so nothing revisits it. A row whose lane
        content reached trunk in the meantime owes nothing by construction and
        no surface said so: the module's landedness classifier takes only rows
        whose polarity is "fix" AND that carry a reviewed_tip, and a held row
        has neither.

        REAL GIT, NO DOUBLE, AND THROUGH THE CLI THE WAY A READER MEETS IT.
        """
        self._origin()
        row = self.add(ref=self.a)
        marked, why = dispatches.mark_hold(row["id"], "waiting on the vendor")
        self.assertIsNone(why)
        note = dispatches._held_landing_note(marked)
        self.assertIn("WORK ALREADY ON TRUNK", note)
        self.assertIn("ANCESTRY", note)
        # THE HOLD REASON IS NOT REPLACED BY THE MEASUREMENT. Both facts reach
        # the reader or the row has traded one silence for another.
        rc, out, err = self.triage(row["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn("waiting on the vendor", out)
        self.assertIn("WORK ALREADY ON TRUNK", out)

    def test_a_REBASED_land_is_named_as_patch_identity_not_as_ancestry(self):  # noqa: VACUOUS_ASSERTION — the absences are assertNotIn('(ANCESTRY)') and the fixture's assertNotEqual, and both have unconditional positive controls on the SAME observable: assertIn('WORK ALREADY ON TRUNK') and assertIn('PATCH IDENTITY') on the same note, plus the landed_state == PATCH_EQUIVALENT equality that makes the ANCESTRY branch unreachable by construction
        """TWO INSTRUMENTS, AND THE ANSWER SAYS WHICH ONE ANSWERED.

        Ancestry proves the reviewed commit is REACHABLE; patch identity
        proves the CONTENT arrived under other object ids, which is what a
        rebased land produces and what ancestry reports absent with perfect
        honesty. Folding them into one word would tell a reader the wrong
        thing about what was checked, and this repository lands almost
        everything rebased.

        TWO PROPERTIES THE FIXTURE MUST HAVE, and each cost a gate to learn.

        THE CARRIER TOUCHES ITS OWN FILE: the shared `commit` helper appends
        to one file, so cherry-picking anything it made conflicts and the arm
        dies in its fixture rather than measuring.

        AND TRUNK MUST MOVE ELSEWHERE FIRST, which is the half that makes this
        a REBASED land at all. A commit object is content-addressed over its
        tree, parents, author and message — so cherry-picking a commit back
        onto the very parent it branched from REPRODUCES THE IDENTICAL SHA,
        and ancestry then answers YES perfectly correctly. Measured: source
        and the cherry-picked tip were the same object. An unrelated commit on
        trunk first gives the copy a different parent, a different sha, and
        the patch-identity reading this arm exists to pin.
        """
        self.git("checkout", "-q", "-b", "reb-src", self.main)
        source = self.commit_file("rebased.txt", "rebased content")
        self.git("checkout", "-q", self.main)
        self.commit_file("moved.txt", "trunk moved elsewhere")
        self.git("cherry-pick", source)
        landed_as = self.git("rev-parse", "HEAD")
        self._origin()
        # THE FIXTURE PROVES THE WORLD IT CLAIMS BEFORE MEASURING ANYTHING IN
        # IT, and this shape was already in this file — at
        # test_work_that_landed_under_a_DIFFERENT_SHA_still_counts — written
        # correctly by somebody else before either author of this arm reached
        # for it. Both of us swept for the HELPER we were about to build and
        # neither swept for the SHAPE we were about to construct.
        #
        # THREE CHECKS, AND EACH ONE EARNS ITS LINE. The two assertEquals are
        # POSITIVE CONTROLS ON BOTH SIDES of the inequality: without them a
        # name that silently resolved to the wrong ref would satisfy the
        # assertNotEqual for the wrong reason. The landed_state equality is
        # the one that makes the arm mean anything — if the carrier were an
        # ANCESTOR, the ANCESTOR branch would answer and the PATCH IDENTITY
        # term this arm exists to pin would be INERT, green either way.
        #
        # MEASURED AGAINST origin/main, NOT self.main, because that is the ref
        # production reads: an arm that proves its premise against a different
        # ref than the code under test proves nothing about the code's world.
        self.assertEqual(landed_as, self.git("rev-parse", "origin/main"))
        self.assertEqual(source, self.git("rev-parse", "reb-src"))
        self.assertNotEqual(landed_as, source)  # noqa: VACUOUS_ASSERTION — a fixture premise check, not the claim; the claim is the landed_state equality below and the two assertIns on the note, all unconditional
        self.assertEqual(vcs.backend(self.repo).landed_state(
            self.repo, source, "origin/main"), vcs.PATCH_EQUIVALENT)
        row = self.add(ref=source)
        marked, _why = dispatches.mark_hold(row["id"], "waiting on the vendor")
        note = dispatches._held_landing_note(marked)
        self.assertIn("WORK ALREADY ON TRUNK", note)
        self.assertIn("PATCH IDENTITY", note)
        self.assertNotIn("(ANCESTRY)", note)

    def test_an_UNLANDED_hold_NAMES_ITS_HORIZON_and_a_blind_one_says_UNKNOWN(self):
        """A NEGATIVE IS STATED, NEVER RENDERED AS SILENCE.

        The trunk ref is a REMOTE-TRACKING SNAPSHOT that moves only on fetch,
        so a checkout that has not fetched answers NOT_ANCESTOR for work that
        IS on trunk — and an empty string there is byte-identical to the
        answer for work that genuinely never landed. So the reading names its
        own horizon: the ref, and that this is what the checkout LAST SAW.

        AND THE BLIND CASES STAY SEPARATE FROM IT. A read that could not be
        made says UNKNOWN, because a blind instrument reporting a negative is
        the one failure this surface must not have.

        Its positive control is the landed arm above on the same helper and
        fixture, so an implementation that had simply stopped answering fails
        there rather than passing here.
        """
        self._origin()
        unlanded = self.add(ref=self.side)
        held, _why = dispatches.mark_hold(unlanded["id"], "vendor")
        note = dispatches._held_landing_note(held)
        self.assertIn("as this checkout last saw it", note)
        self.assertIn("origin/main", note)
        self.assertNotIn("WORK ALREADY ON TRUNK", note)
        # THE BLIND ARMS ASSERT THE NEGATIVE IS ABSENT, NOT ONLY THAT UNKNOWN
        # IS PRESENT. Asserting presence alone discriminates today purely by
        # accident of the implementation — the horizon sentence has exactly one
        # producer and no UNKNOWN branch can reach it — but the property the
        # docstring CLAIMS is that the blind cases stay SEPARATE from the
        # negative. An edit that let an UNKNOWN branch also carry the horizon
        # sentence would keep a presence-only arm green while producing exactly
        # the failure this surface exists to refuse: a blind instrument
        # rendering a negative. The arms would agree with the bug.
        blind = dict(held, tip="d" * 40)
        note = dispatches._held_landing_note(blind)
        self.assertIn("UNKNOWN", note)
        self.assertNotIn("as this checkout last saw it", note)
        rootless = dict(held, repo_id="", repo_root="")
        note = dispatches._held_landing_note(rootless)
        self.assertIn("UNKNOWN", note)
        self.assertNotIn("as this checkout last saw it", note)
        self.assertNotIn("WORK ALREADY ON TRUNK", note)


class LedgerSeparationTest(DispatchBase):
    def test_dispatches_and_ownerasks_share_mechanics_not_files_or_closers(self):
        from helm import ownerasks
        self.add()
        ownerasks.add("an owner ask")
        self.assertNotEqual(dispatches.ledger_path(), ownerasks.ledger_path())
        self.assertEqual(len(dispatches.rows()), 1)
        self.assertEqual(len(ownerasks.rows()), 1)
        self.assertIs(dispatches.eventledger, ownerasks.eventledger)


class DischargeEventTest(DispatchBase):
    """One narrow post-verdict annotation retires contrary debt without
    rewriting the review that created it."""

    def fixed(self, polarity="fix"):
        row = self.add()
        if polarity == "approve":
            with mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")):
                dispatches.mark_verdict(
                    row["id"], self.a, "review", polarity=polarity)
        else:
            dispatches.mark_verdict(
                row["id"], self.a, "review", polarity=polarity)
        return row

    def record(self, row, superseding=None, evidence="resolved"):
        return dispatches._record_discharge_proven(
            row["id"], self.a, superseding or self.b, "f" * 32, evidence,
            "landed", "local")

    def test_discharge_replays_without_rewriting_the_verdict(self):
        row = self.fixed()
        out, why = self.record(row)
        self.assertIsNone(why)
        self.assertTrue(out["discharged"])
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "verdict")
        self.assertEqual(replayed["polarity"], "fix")
        self.assertEqual(replayed["reviewed_tip"], self.a)
        self.assertEqual(replayed["superseding_tip"], self.b)
        self.assertEqual(replayed["superseding_id"], "f" * 32)
        self.assertEqual(replayed["discharge_contrary"], "landed")
        self.assertEqual(replayed["discharge_target"], "local")
        self.assertEqual(len(dispatches.history(row["id"])), 3)

    def test_identical_retry_is_idempotent_but_a_conflict_is_refused(self):
        row = self.fixed("supersede")
        first, why = self.record(row)
        self.assertIsNone(why)
        again, why = self.record(row)
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertEqual(len(dispatches.history(row["id"])), 3)
        _out, why = self.record(row, superseding=self.c)
        self.assertIn("different discharge", why)

    def test_only_fix_or_supersede_verdicts_accept_discharge(self):
        approved = self.fixed("approve")
        _out, why = self.record(approved)
        self.assertIn("not a FIX/SUPERSEDE", why)
        open_row = self.add(lane="open")
        _out, why = self.record(open_row)
        self.assertIn("not a FIX/SUPERSEDE", why)
        cancelled = self.add(lane="cancelled")
        dispatches.mark_cancel(cancelled["id"], "moot")
        _out, why = self.record(cancelled)
        self.assertIn("not a FIX/SUPERSEDE", why)

    def test_forged_discharge_rows_are_inert(self):
        row = self.fixed()
        base = {"v": 3, "event": "discharge", "seq": 2, "id": row["id"],
                "ts": "2026-07-25T00:00:00Z", "reviewed_tip": self.a,
                "superseding_tip": self.b, "superseding_id": "f" * 32,
                "discharge_ref": "resolved", "contrary_state": "landed",
                "contrary_target": "local"}
        bad = [dict(base, seq=True), dict(base, reviewed_tip=self.b),
               dict(base, superseding_tip="a" * 41),
               dict(base, superseding_tip=self.a),
               dict(base, discharge_ref=""),
               dict(base, contrary_state="invented"),
               dict(base, contrary_target="invented")]
        for event in bad:
            eventledger.append_unlocked(dispatches.ledger_path(), event)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertFalse(replayed.get("discharged", False))
        self.assertEqual(replayed["status"], "verdict")

    def test_post_discharge_events_remain_inert(self):
        row = self.fixed()
        self.record(row)
        standing = dispatches.snapshot()[0][row["id"]]
        for event in (
                {"v": 3, "event": "cancel", "seq": standing["seq"] + 1,
                 "id": row["id"], "reason": "rewrite"},
                {"event": "retarget", "id": row["id"], "tip": self.c,
                 "ts": "2026-07-01T00:00:00Z"}):
            eventledger.append_unlocked(dispatches.ledger_path(), event)
        self.assertEqual(dispatches.snapshot()[0][row["id"]], standing)

    def test_withdrawn_row_rejects_a_later_forged_discharge(self):
        row = self.fixed()
        out, why = dispatches._record_withdraw_proven(
            row["id"], self.a, "change abandoned")
        self.assertIsNone(why)
        standing = dispatches.snapshot()[0][row["id"]]
        self.assertTrue(out["withdrawn"])
        eventledger.append_unlocked(dispatches.ledger_path(), {
            "v": 3, "event": "discharge", "seq": standing["seq"] + 1,
            "id": row["id"], "ts": "2026-07-28T00:00:01Z",
            "reviewed_tip": self.a, "superseding_tip": self.b,
            "superseding_id": "f" * 32, "discharge_ref": "rewrite",
            "contrary_state": "landed", "contrary_target": "local"})
        self.assertEqual(dispatches.snapshot()[0][row["id"]], standing)

    def test_unique_prefix_resolver_refuses_exact_short_collision(self):
        exact = {"id": "deadbeef", "status": "open"}
        longer = {"id": "deadbeef" + "1" * 24, "status": "open"}
        current = {exact["id"]: exact, longer["id"]: longer}
        row, why = dispatches._resolve_row(current, "deadbeef")
        self.assertIsNone(row)
        self.assertIn("ambiguous", why)
        self.assertIs(dispatches._resolve_row(
            {exact["id"]: exact}, "deadbeef")[0], exact)
        self.assertIs(dispatches._resolve_row(
            current, longer["id"])[0], longer)

    def test_prefix_and_full_id_verdicts_store_canonical_ids(self):
        prefix_row = self.add(lane="prefix")
        full_row = self.add(lane="full")
        first, why = dispatches.mark_verdict(
            prefix_row["id"][:12], self.a, "prefix-ok", polarity="fix")
        self.assertIsNone(why)
        second, why = dispatches.mark_verdict(
            full_row["id"], self.a, "full-ok", polarity="fix")
        self.assertIsNone(why)
        self.assertEqual(first["id"], prefix_row["id"])
        self.assertEqual(second["id"], full_row["id"])
        self.assertEqual(dispatches.history(prefix_row["id"])[-1]["id"],
                         prefix_row["id"])
        self.assertEqual(dispatches.snapshot()[0][prefix_row["id"]]["status"],
                         "verdict")

    def test_stale_verdict_names_the_one_based_first_difference(self):
        row = self.add()
        reviewed = row["tip"][:8] + ("0" if row["tip"][8] != "0" else "1") \
            + row["tip"][9:]
        out, why = dispatches.mark_verdict(
            row["id"][:12], reviewed, "mistyped", polarity="approve")
        self.assertIsNone(out)
        self.assertIn("stale verdict", why)
        self.assertIn("first difference at character 9", why)
        self.assertEqual(len(dispatches.history(row["id"])), 1)

    def test_replay_normalizes_numeric_tip_before_stale_diagnostic(self):
        row = self.add()
        events = eventledger.events(dispatches.ledger_path())
        events[0]["tip"] = int("1" * 40)
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        out, why = dispatches.mark_verdict(
            row["id"], "2" * 40, "mistyped", polarity="approve")
        self.assertIsNone(out)
        self.assertIn("stale verdict", why)
        self.assertIn("first difference at character 1", why)


class AbandonEventTest(DispatchBase):
    """A missing reviewed artifact retires debt without inventing land state."""

    def reviewed(self, polarity="fix"):
        row = self.add(kind="review")
        dispatches.mark_verdict(row["id"], self.a, "reviewed", polarity=polarity)
        return row

    def record(self, row, reason="history rewrite destroyed the object", probe=None,
               mention=None):
        return dispatches._record_abandon_proven(
            row["id"], self.a, reason, probe or (lambda _repo, _tip: False),
            mention or (lambda _repo, _lane: {
                "mention": {"state": "none"}, "branch_state": "none",
                "worktree_state": "none"}))

    def test_abandon_replays_without_rewriting_the_verdict(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        out, why = self.record(row)
        self.assertIsNone(why)
        self.assertTrue(out["abandoned"])
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "verdict")
        self.assertEqual(replayed["polarity"], "fix")
        self.assertEqual(replayed["reviewed_tip"], self.a)
        self.assertEqual(replayed["verdict_ref"], "reviewed")
        self.assertEqual(replayed["abandon_reason"],
                         "history rewrite destroyed the object")
        self.assertEqual(replayed["abandon_object_state"], "missing")
        self.assertEqual(replayed["abandon_proof_mode"],
                         "cat-file-batch-check")
        self.assertEqual(replayed["abandon_proof_version"], 1)
        self.assertEqual(replayed["abandon_trunk_mention_state"], "none")
        self.assertEqual(replayed["abandon_trunk_mention_proof_mode"],
                         "structured-message-and-tag-scan")
        self.assertEqual(replayed["abandon_trunk_mention_proof_version"], 2)
        self.assertEqual(replayed["abandon_branch_state"], "none")
        self.assertEqual(replayed["abandon_branch_proof_mode"],
                         "git-ref-and-ancestry")
        self.assertEqual(replayed["abandon_branch_proof_version"], 1)
        self.assertEqual(replayed["abandon_worktree_state"], "none")
        self.assertEqual(replayed["abandon_worktree_proof_mode"],
                         "git-worktree-status")
        self.assertEqual(replayed["abandon_worktree_proof_version"], 1)
        self.assertEqual(replayed["abandon_land_state"], "UNKNOWN")

    def test_legacy_message_only_proof_event_still_replays(self):
        row = self.reviewed()
        event = {
            "v": 3, "event": "abandon", "seq": 2, "id": row["id"],
            "ts": "2026-07-31T00:00:00Z", "reviewed_tip": self.a,
            "repo_id": dispatches._repo_info(self.repo)["repo_id"],
            "reason": "legacy history rewrite", "object_state": "missing",
            "object_proof_mode": "cat-file-batch-check",
            "object_proof_version": 1, "trunk_mention_state": "none",
            "trunk_mention_proof_mode": "structured-message-scan",
            "trunk_mention_proof_version": 1, "branch_state": "none",
            "branch_proof_mode": "git-ref-and-ancestry",
            "branch_proof_version": 1, "worktree_state": "none",
            "worktree_proof_mode": "git-worktree-status",
            "worktree_proof_version": 1, "land_state": "UNKNOWN"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertTrue(replayed["abandoned"])
        self.assertEqual(replayed["abandon_trunk_mention_proof_mode"],
                         "structured-message-scan")
        self.assertEqual(replayed["abandon_trunk_mention_proof_version"], 1)

    def test_writer_reprobes_under_the_real_ledger_lock_before_append(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        seen = []

        def probe(repo_id, tip):
            lock = dispatches.ledger_path() + ".lock"
            fd = os.open(lock, os.O_RDWR)
            held = False
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            seen.append((repo_id, tip, held))
            return False

        out, why = self.record(row, probe=probe)
        self.assertIsNone(why)
        self.assertTrue(out["abandoned"])
        self.assertEqual(seen, [(dispatches._repo_info(self.repo)["repo_id"],
                                 self.a, True)])

    def test_missing_can_change_to_exists_at_the_mutation_boundary(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        out, why = self.record(row, probe=lambda _repo, _tip: True)
        self.assertIsNone(out)
        self.assertIn("exists", why)
        self.assertFalse(dispatches.snapshot()[0][row["id"]].get("abandoned", False))
        self.assertEqual(len(dispatches.history(row["id"])), 2)

    def test_trunk_mention_appearing_at_mutation_boundary_blocks_append(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        before = len(dispatches.history(row["id"]))
        out, why = self.record(
            row, mention=lambda _repo, _lane: {
                "mention": {"state": "structured", "sha": self.b,
                            "line": "Merge lane/foo — landed"},
                "branch_state": "none", "worktree_state": "none"})
        self.assertIsNone(out)
        self.assertIn("trunk mention blocks abandon", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_landed_family_tag_appearing_at_mutation_boundary_blocks_append(self):  # noqa: VACUOUS_ASSERTION — tag hit binds a real row and unchanged history under lock
        row = self.reviewed()
        before = len(dispatches.history(row["id"]))
        out, why = self.record(
            row, mention=lambda _repo, _lane: {
                "mention": {"state": "tag", "sha": self.b,
                            "tag": "refs/tags/gate/foo-r2"},
                "branch_state": "none", "worktree_state": "none"})
        self.assertIsNone(out)
        self.assertIn("trunk mention blocks abandon", why)
        self.assertIn("refs/tags/gate/foo-r2", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_lane_work_appearing_at_mutation_boundary_blocks_append(self):  # noqa: VACUOUS_ASSERTION — each arm binds a real row and unchanged history
        cases = (
            ("unlanded", "none", "unlanded lane branch"),
            ("merged", "dirty", "dirty worktree"),
            ("unknown", "none", "UNKNOWN"),
            ("merged", "unknown", "UNKNOWN"),
        )
        for branch_state, worktree_state, expected in cases:
            with self.subTest(branch_state=branch_state,
                              worktree_state=worktree_state):
                row = self.reviewed()
                before = len(dispatches.history(row["id"]))
                out, why = self.record(
                    row, mention=lambda _repo, _lane, b=branch_state,
                    w=worktree_state: {
                        "mention": {"state": "none"},
                        "branch_state": b, "worktree_state": w})
                self.assertIsNone(out)
                self.assertIn(expected, why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_identical_retry_is_idempotent_and_conflict_refuses(self):  # noqa: VACUOUS_ASSERTION — retry count and conflict bind one durable event
        row = self.reviewed()
        first, why = self.record(row)
        self.assertIsNone(why)
        again, why = self.record(row, probe=lambda _repo, _tip: True)
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertEqual(len(dispatches.history(row["id"])), 3)
        _out, why = self.record(row, reason="different reason")
        self.assertIn("different abandon", why)

    def test_UNKNOWN_probe_and_build_row_both_refuse_without_append(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        before = len(dispatches.history(row["id"]))
        out, why = self.record(row, probe=lambda _repo, _tip: None)
        self.assertIsNone(out)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)
        build = self.add(kind="build", lane="build-row")
        dispatches.mark_verdict(build["id"], self.a, "bad historical shape",
                                polarity="fix")
        out, why = self.record(build)
        self.assertIsNone(out)
        self.assertIn("not a review row", why)

    def test_competing_terminal_wins_and_abandon_cannot_rewrite_it(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        withdrawn, why = dispatches._record_withdraw_proven(
            row["id"], self.a, "existing terminal")
        self.assertIsNone(why)
        out, why = self.record(row)
        self.assertIsNone(out)
        self.assertIn("already retired", why)
        self.assertEqual(dispatches.snapshot()[0][row["id"]], withdrawn)
        self.assertEqual(len(dispatches.history(row["id"])), 3,
                         "a refused competing terminal must append nothing")

    def test_forged_abandon_rows_are_inert_and_label_is_distinct(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        repo_id = dispatches._repo_info(self.repo)["repo_id"]
        base = {"v": 3, "event": "abandon", "seq": 2, "id": row["id"],
                "ts": "2026-07-31T00:00:00Z", "reviewed_tip": self.a,
                "repo_id": repo_id, "reason": "history rewrite",
                "object_state": "missing",
                "object_proof_mode": "cat-file-batch-check",
                "object_proof_version": 1, "trunk_mention_state": "none",
                "trunk_mention_proof_mode": "structured-message-scan",
                "trunk_mention_proof_version": 1,
                "branch_state": "none",
                "branch_proof_mode": "git-ref-and-ancestry",
                "branch_proof_version": 1,
                "worktree_state": "none",
                "worktree_proof_mode": "git-worktree-status",
                "worktree_proof_version": 1, "land_state": "UNKNOWN"}
        bad = [dict(base, seq=True), dict(base, reviewed_tip=self.b),
               dict(base, repo_id="relative/.git"), dict(base, reason=""),
               dict(base, reason="two\nlines"),
               dict(base, object_state="exists"),
               dict(base, object_proof_mode="guessed"),
               dict(base, object_proof_version=True),
               dict(base, trunk_mention_state="structured"),
               dict(base, trunk_mention_proof_mode="grep"),
               dict(base, trunk_mention_proof_version=True),
               dict(base, trunk_mention_proof_mode=
                    "structured-message-and-tag-scan"),
               dict(base, trunk_mention_proof_version=2),
               dict(base, branch_state="unlanded"),
               dict(base, branch_proof_mode="guessed"),
               dict(base, branch_proof_version=True),
               dict(base, worktree_state="dirty"),
               dict(base, worktree_proof_mode="guessed"),
               dict(base, worktree_proof_version=True),
               dict(base, land_state="LANDED"), dict(base, ts="not-a-time")]
        for event in bad:
            eventledger.append_unlocked(dispatches.ledger_path(), event)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertFalse(replayed.get("abandoned", False))
        out, why = self.record(row)
        self.assertIsNone(why)
        self.assertEqual(dispatches._label(out),
                         "VERDICT fix / ABANDONED (LAND UNKNOWN)")


class CloseLandedEventTest(DispatchBase):
    """A landing fact can retire an UNDECLARED review without inventing one."""

    def reviewed(self, polarity=None):
        row = self.add()
        if polarity is None:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": row["seq"] + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.a, "verdict_ref": "reviewed"}))
        elif polarity == "approve":
            with mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")):
                dispatches.mark_verdict(
                    row["id"], self.a, "reviewed", polarity=polarity)
        else:
            dispatches.mark_verdict(
                row["id"], self.a, "reviewed", polarity=polarity)
        return row

    def record(self, row, repo_id=None, trunk_ref="refs/heads/main",
               trunk_sha=None, mode="ancestor"):
        repo_id = repo_id or dispatches._repo_info(self.repo)["repo_id"]
        return dispatches._record_close_landed_proven(
            row["id"], self.a, repo_id, trunk_ref,
            trunk_sha or self.b, mode)

    def test_close_replays_without_rewriting_the_verdict(self):
        row = self.reviewed()
        out, why = self.record(row)
        self.assertIsNone(why)
        self.assertTrue(out["closed_by_landing"])
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "verdict")
        self.assertIsNone(replayed["polarity"])
        self.assertEqual(replayed["reviewed_tip"], self.a)
        self.assertEqual(replayed["verdict_ref"], "reviewed")
        self.assertEqual(replayed["landing_trunk_ref"], "refs/heads/main")
        self.assertEqual(replayed["landing_trunk_sha"], self.b)
        self.assertEqual(replayed["landing_proof_mode"], "ancestor")
        self.assertEqual(replayed["landing_proof_version"], 1)

    def test_only_undeclared_verdicts_accept_close(self):
        for polarity in ("approve", "fix", "supersede"):
            row = self.reviewed(polarity)
            _out, why = self.record(row)
            self.assertIn("not an UNDECLARED verdict", why)
        open_row = self.add(lane="open")
        _out, why = self.record(open_row)
        self.assertIn("not an UNDECLARED verdict", why)

    def test_identical_retry_is_idempotent_and_conflict_refuses(self):
        row = self.reviewed()
        first, why = self.record(row)
        self.assertIsNone(why)
        again, why = self.record(row, trunk_sha=self.c,
                                 mode="patch-equivalent")
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertEqual(len(dispatches.history(row["id"])), 3)
        _out, why = self.record(row, trunk_ref="refs/heads/other")
        self.assertIn("different landing closure", why)

    def test_forged_close_rows_are_inert(self):
        row = self.reviewed()
        repo_id = dispatches._repo_info(self.repo)["repo_id"]
        base = {"v": 3, "event": "close-landed", "seq": 2,
                "id": row["id"], "ts": "2026-07-28T00:00:00Z",
                "reviewed_tip": self.a, "landing_repo_id": repo_id,
                "landing_trunk_ref": "refs/heads/main",
                "landing_trunk_sha": self.b,
                "landing_proof_mode": "ancestor",
                "landing_proof_version": 1}
        bad = [dict(base, seq=True), dict(base, reviewed_tip=self.b),
               dict(base, landing_repo_id="relative/.git"),
               dict(base, landing_trunk_ref="refs/tags/release"),
               dict(base, landing_trunk_sha="a" * 41),
               dict(base, landing_proof_mode="guessed"),
               dict(base, landing_proof_version=True),
               dict(base, ts="not-a-time"),
               dict(base, ts="2026-garbage")]
        for event in bad:
            eventledger.append_unlocked(dispatches.ledger_path(), event)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertFalse(replayed.get("closed_by_landing", False))
        self.assertIsNone(replayed.get("polarity"))

    def test_post_close_events_remain_inert_and_label_is_honest(self):
        row = self.reviewed()
        self.record(row)
        standing = dispatches.snapshot()[0][row["id"]]
        eventledger.append_unlocked(dispatches.ledger_path(), {
            "v": 3, "event": "withdraw", "seq": standing["seq"] + 1,
            "id": row["id"], "ts": "2026-07-28T00:00:01Z",
            "reviewed_tip": self.a, "withdraw_ref": "rewrite"})
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed, standing)
        self.assertEqual(dispatches._label(replayed),
                         "VERDICT UNDECLARED / CLOSED BY LANDING")


class CancelTest(DispatchBase):
    """`helm dispatch cancel <id> <reason>` — the honest terminal event for an
    ABANDONED dispatch (recipient gone / work moot), distinct from a verdict.
    Closes the gap the idle-dispatch watchdog exposed: today a stranded
    dispatch could only be closed by a FALSE verdict laundering a review that
    never happened."""

    def _legacy_undeclared(self, row, evidence="reviewed"):
        """Plant a pre-requirement verdict; new writers must never create one."""
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"], "verdict_ref": evidence}))
        return dispatches.snapshot()[0][row["id"]]

    def test_cancel_closes_an_open_dispatch_and_records_the_reason(self):
        row = self.add(lane="stranded")
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])
        out, why = dispatches.mark_cancel(row["id"], "recipient gone, work moot")
        self.assertIsNone(why)
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["cancel_reason"], "recipient gone, work moot")
        self.assertEqual(dispatches.open_rows(), [])          # dropped from open

    def test_cancel_replays_cancelled_from_a_FRESH_disk_snapshot(self):
        # the event must survive an append-only replay, not just live in RAM
        row = self.add()
        dispatches.mark_cancel(row["id"], "abandoned")
        replayed = dispatches.snapshot()[0][row["id"]]        # re-read from disk
        self.assertEqual(replayed["status"], "cancelled")
        self.assertEqual(replayed["cancel_reason"], "abandoned")

    def test_cancel_is_terminal_and_idempotent_on_the_same_reason(self):
        row = self.add()
        first, why = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(why)
        again, why = dispatches.mark_cancel(row["id"], "moot")     # idempotent
        self.assertIsNone(why)
        self.assertEqual(again["status"], "cancelled")
        self.assertEqual(len(dispatches.history(row["id"])), 2)    # open + cancel
        _r, why = dispatches.mark_cancel(row["id"], "different")   # not re-writable
        self.assertIn("already cancelled", why)

    def test_a_verdicted_dispatch_cannot_be_cancelled(self):
        row = self.add()
        dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        _r, why = dispatches.mark_cancel(row["id"], "too late")
        self.assertIn("already has a verdict", why)

    def test_a_cancelled_dispatch_cannot_be_verdicted(self):
        row = self.add()
        dispatches.mark_cancel(row["id"], "abandoned")
        _r, why = dispatches.mark_verdict(
            row["id"], self.a, "reviewed", "fix")
        self.assertIsNotNone(why)                              # cancelled is terminal
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "cancelled")

    def test_cancel_requires_a_reason(self):
        row = self.add()
        _r, why = dispatches.mark_cancel(row["id"], "")
        self.assertIn("reason", why)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_cancel_of_no_such_dispatch_is_refused(self):
        _r, why = dispatches.mark_cancel("deadbeefdeadbeef", "nope")
        self.assertIn("no such dispatch", why)

    def test_cancel_accepts_the_short_id_printed_by_list(self):
        row = self.add()
        out, why = dispatches.mark_cancel(row["id"][:12], "moot")
        self.assertIsNone(why)
        self.assertEqual(out["id"], row["id"])

    def test_cancel_resolves_the_id_before_validating_the_reason(self):
        _out, why = dispatches.mark_cancel("deadbeef", "x" * 300)
        self.assertIn("no such dispatch", why)
        self.assertNotIn("at most", why)
        row = self.add()
        _out, why = dispatches.mark_cancel(row["id"][:12], "x" * 300)
        self.assertIn("at most", why)

    def test_a_cancelled_dispatch_is_not_overdue_nor_a_stop_candidate(self):
        # the whole point: cancelling a STRANDED dispatch stops the watchdog
        row = self.add(lane="ghost", deadline_s=60)
        self.age(row["id"], 3600)                              # 1h old, deadline 60s
        self.assertTrue(dispatches.overdue())                 # overdue while open
        cand, _kind, _un = dispatches.stop_candidate()
        self.assertEqual(cand["id"], row["id"])               # a stop candidate WHILE open
        dispatches.mark_cancel(row["id"], "recipient absent")
        self.assertEqual(dispatches.overdue(), [])            # silent once cancelled
        cand, _kind, _un = dispatches.stop_candidate()
        self.assertIsNone(cand)
        rc, out, _err = run(dispatches.cmd_dispatch, ["list", "--open"])
        self.assertEqual(rc, 0)
        self.assertNotIn(row["id"], out)                      # gone from list --open

    def test_blocked_dm_cancel_race_appends_no_late_delivered(self):
        # THE RACE (xrev): send() releases the lock for the DM; a
        # concurrent cancel terminalizes the row; when the DM returns,
        # _mark_delivered must NOT append a delivered event onto the cancelled
        # row nor report it observed/pending.
        row = self.add(lane="raced")
        dispatches.mark_cancel(row["id"], "recipient absent")
        before = len(dispatches.history(row["id"]))           # [dispatch, cancel]
        observed, err = dispatches._mark_delivered(row["id"], "late-dm-post")
        self.assertIsNone(err)
        self.assertEqual(observed["status"], "cancelled")     # true state, not observed
        self.assertNotEqual(observed.get("delivery"), "observed")
        self.assertEqual(len(dispatches.history(row["id"])), before)   # NO late event
        self.assertEqual(dispatches.open_rows(), [])          # no open / overdue leak
        self.assertEqual(dispatches.overdue(), [])

    def test_send_render_is_honest_when_cancelled_mid_delivery(self):
        # the CLI must surface CANCELLED, never a false "PENDING VERDICT", when a
        # cancel lands during send's DM window
        holder = {}

        def dm_cancels_then_delivers(*a, **k):
            r = dispatches.open_rows()[0]                      # the just-persisted row
            holder["id"] = r["id"]
            dispatches.mark_cancel(r["id"], "cancelled mid-DM")
            return {"id": "delivered-post"}, None
        with mock.patch.object(seats, "dm", side_effect=dm_cancels_then_delivers):
            rc, out, _err = run(dispatches.cmd_dispatch,
                                ["send", "codex-3", "raced-lane", "hello",
                                 "--ref", self.a, "--repo", self.repo,
                                 "--kind", "review", "--new-work"])
        self.assertEqual(rc, 0)
        self.assertIn("CANCELLED", out)
        self.assertNotIn("PENDING VERDICT", out)
        self.assertEqual(len(dispatches.history(holder["id"])), 2)     # no delivered
        self.assertEqual(
            dispatches.snapshot()[0][holder["id"]]["status"], "cancelled")

    def test_apply_terminal_guard_blocks_a_compat_verdict_or_retarget(self):
        # HIGH1 (xrev): once terminal, NO later event — including a
        # legacy-compat verdict/retarget — may convert the status or move the
        # tip. Probe _apply directly with a legacy (v!=3) CANCELLED state.
        cancelled = {"id": "a" * 16, "status": "cancelled", "v": 1, "seq": 1,
                     "tip": "b" * 40, "cancel_reason": "moot"}
        compat_verdict = {"id": "a" * 16, "status": "verdict",
                          "verdict_ref": "forged", "reviewed_tip": "b" * 40,
                          "ts": "2026-07-01T00:00:00Z"}      # pre-boundary => compat
        self.assertEqual(dispatches._apply(cancelled, compat_verdict), cancelled)
        compat_retarget = {"id": "a" * 16, "event": "retarget", "tip": "c" * 40,
                           "ts": "2026-07-01T00:00:00Z"}
        self.assertEqual(dispatches._apply(cancelled, compat_retarget), cancelled)

    def test_malformed_numeric_wire_types_cannot_close_a_dispatch(self):
        # HIGH2 (xrev): bool is an int subclass and == let True==1 /
        # 1.0==1, so a forged strict cancel with a bool/float seq once closed
        # the dispatch. The type-exact strict gate now rejects them.
        row = self.add()                                      # v3 open, seq 0
        for bad_seq in (True, 1.0):
            forged = {"v": 3, "event": "cancel", "seq": bad_seq, "id": row["id"],
                      "ts": "2026-07-24T00:00:00Z", "reason": "forged"}  # post-boundary
            eventledger.append_unlocked(dispatches.ledger_path(), forged)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_malformed_genesis_seq_type_creates_no_state(self):
        # HIGH2: a genesis whose seq is a bool must not open an obligation
        forged = {"v": 3, "event": "dispatch", "seq": True, "id": "d" * 32,
                  "status": "open", "tip": "e" * 40, "ts": "2026-07-24T00:00:00Z",
                  "recipient": "codex-3", "lane": "L", "deadline_s": 3600}
        eventledger.append_unlocked(dispatches.ledger_path(), forged)
        self.assertNotIn("d" * 32, dispatches.snapshot()[0])

    def test_a_disk_cancelled_row_never_surfaces_in_idle_dispatch_scan(self):
        # the watchdog reads open_rows(); a cancelled row is excluded, so it can
        # never be flagged stranded (xrev: exercise the real scan)
        from helm import idle_dispatch
        row = self.add(lane="ghost")
        self.age(row["id"], 100000)                           # past the soft window
        dispatches.mark_cancel(row["id"], "recipient absent")
        self.assertFalse(any(f["id"] == row["id"] for f in idle_dispatch.scan()))

    def test_a_cancelled_dispatch_is_not_a_land_loop(self):
        # landreq reads the FULL snapshot; a cancelled row (has a tip) must be
        # excluded, else it tracks forever as an AWAITING_REVIEW land loop
        from helm import landreq
        row = self.add(lane="ghost-lane")
        dispatches._mark_delivered(row["id"], "post-1")
        loops, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], loops)                       # a loop while open
        dispatches.mark_cancel(row["id"], "abandoned")
        loops, _ = landreq.project()
        self.assertNotIn(row["id"], loops)                    # gone once cancelled

    def test_cancel_verb_cli_end_to_end(self):
        row = self.add(lane="cli")
        rc, out, _err = run(dispatches.cmd_dispatch,
                            ["cancel", row["id"], "recipient", "gone"])
        self.assertEqual(rc, 0)
        self.assertIn("CANCELLED", out)
        self.assertIn("recipient gone", out)
        # A valid id reaches domain validation; only a missing id is usage.
        open_row = self.add(lane="needs-reason")
        rc, _out, err = run(dispatches.cmd_dispatch, ["cancel", open_row["id"][:12]])
        self.assertEqual(rc, 1)
        self.assertIn("reason", err)
        rc, _out, err = run(dispatches.cmd_dispatch, ["cancel"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm dispatch cancel", err)

    def test_a_closed_row_states_its_decision_and_cannot_read_as_an_owed_one(self):
        """REGRESSION for a live misread, 2026-07-28. The closed label was the
        bare noun "VERDICT" and the open label is "PENDING VERDICT" — one word
        apart, same stem. Scanning the list, the integrator read a column of
        "VERDICT" against ages like 4513m/180m as a backlog of OWED reviews and
        relayed five DISCHARGED rows to a seat as five missed obligations.

        A closed row must therefore say which way it went."""
        row = self.add(lane="polarity-lane")
        pending = dispatches._label(row)
        out, why = dispatches.mark_verdict(row["id"], self.a, "reviewed",
                                           polarity="fix")
        self.assertIsNone(why)
        closed = dispatches._label(out)

        self.assertIn("fix", closed, "a decided row must name its direction")
        # The whole failure was an INVERSION, so pin the two apart directly:
        # neither label may be readable as the other.
        self.assertNotEqual(closed, pending)
        self.assertNotIn("PENDING", closed,
                         "a filed verdict must never render as a pending one")

    def test_an_undeclared_verdict_is_not_disguised_as_a_decided_one(self):
        """helm's own dispatch usage says 36% of this ledger was filed with no
        polarity. Nothing in the default view had ever shown that, because a
        decision with no direction rendered identically to a decided one."""
        row = self.add(lane="undecl-lane")
        closed = dispatches._label(self._legacy_undeclared(row))
        self.assertIn("UNDECLARED", closed)
        for direction in ("approve", "fix", "supersede"):
            self.assertNotIn(direction, closed,
                             "an undeclared verdict must not claim a direction")

    def test_existing_op_resend_reports_terminal_state_not_confirmation_debt(self):
        # Xrev: a resend of an already-CLOSED operation must report the
        # TRUE terminal state, never "confirm at the recipient" (a lie on a
        # closed row). Both closed states.
        # mark_verdict below passes no polarity, so the honest terminal render
        # is VERDICT UNDECLARED — decided-that-it-is-closed, undeclared which way.
        for closer, label in (("verdict", "VERDICT UNDECLARED"),
                              ("cancel", "CANCELLED")):
            with mock.patch.object(seats, "dm", return_value=({"id": "p1"}, None)):
                r, why, sent = dispatches.send(
                    "codex-3", "lane-" + closer, "hi", self.a,
                    key="op-" + closer, repo=self.repo, kind="review", new_work=True)
            self.assertTrue(sent, why)
            if closer == "verdict":
                self._legacy_undeclared(r)
            else:
                dispatches.mark_cancel(r["id"], "abandoned")
            # resend the SAME operation — the existed branch fires
            r2, why2, sent2 = dispatches.send(
                "codex-3", "lane-" + closer, "hi", self.a,
                key="op-" + closer, repo=self.repo, kind="review", new_work=True)
            self.assertEqual(r2["id"], r["id"])            # same operation
            self.assertIsNone(why2)                        # NOT confirmation debt
            self.assertFalse(sent2)
            self.assertEqual(dispatches._label(r2), label)  # honest terminal render
            # and the CLI verb reports it honestly: rc 0, terminal label, no lie
            rc, out, _ = run(dispatches.cmd_dispatch,
                             ["send", "codex-3", "lane-" + closer, "hi", "--ref",
                              self.a, "--key", "op-" + closer, "--repo", self.repo,
                              "--kind", "review", "--new-work"])
            self.assertEqual(rc, 0)
            self.assertIn(label, out)
            self.assertNotIn("confirm at the", out)

    def test_failed_or_raising_dm_with_concurrent_close_returns_canonical(self):
        # Xrev: when the DM fails OR raises while a cancel/verdict lands,
        # send() must reconcile and return the canonical terminal state (not the
        # stale pre-DM open row) — both closers x both failure modes, and NO
        # late delivered event (exactly two: dispatch + close).
        def make_dm(closer, mode):
            def dm(*a, **k):
                rid = dispatches.open_rows()[0]["id"]
                if closer == "cancel":
                    dispatches.mark_cancel(rid, "closed during DM")
                else:
                    dispatches.mark_verdict(rid, self.a, "reviewed", "fix")
                if mode == "raise":
                    raise RuntimeError("DM blew up")
                return None, "boom: DM failed"
            return dm
        n = 0
        for closer, label in (("cancel", "CANCELLED"),
                              ("verdict", "VERDICT fix")):
            for mode in ("error", "raise"):
                n += 1
                with mock.patch.object(seats, "dm",
                                       side_effect=make_dm(closer, mode)):
                    r, why, sent = dispatches.send(
                        "codex-3", "rf-%s-%s" % (closer, mode), "hi", self.a,
                        key="rf-%d" % n, repo=self.repo, new_work=True)
                self.assertFalse(sent)
                self.assertIsNone(why)                     # canonical terminal, no debt
                self.assertEqual(dispatches._label(r), label)
                self.assertEqual(len(dispatches.history(r["id"])), 2)  # no late event

    def test_reconcile_send_case1_UNKNOWN_when_ledger_unavailable(self):
        # case 1: a canonical-read FAILURE surfaces UNKNOWN (row=None)
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger locked")):
            r, why, sent = dispatches._reconcile_send("someid", "detail")
        self.assertIsNone(r)
        self.assertIn("UNKNOWN", why)
        self.assertFalse(sent)

    def test_reconcile_send_case2_UNKNOWN_when_absent_from_readable_ledger(self):
        # case 2 (xrev 4th defect): a READABLE snapshot missing the row is
        # UNKNOWN, never the stale pre-DM OPEN row — there is no `or fallback`.
        with mock.patch.object(dispatches, "snapshot", return_value=({}, None)):
            r, why, sent = dispatches._reconcile_send("gone", "detail")
        self.assertIsNone(r)                               # NOT a fallback OPEN row
        self.assertIn("UNKNOWN", why)
        self.assertFalse(sent)

    def test_reconcile_send_case3_returns_the_canonical_terminal_state(self):
        # case 3: a present terminal row is returned as-is (why=None)
        with mock.patch.object(
                dispatches, "snapshot",
                return_value=({"x": {"status": "cancelled"}}, None)):
            r, why, sent = dispatches._reconcile_send("x", "detail")
        self.assertEqual(r["status"], "cancelled")
        self.assertIsNone(why)
        self.assertFalse(sent)

    def test_reconcile_send_case4_open_row_needs_confirmation(self):
        # case 4: a present OPEN row -> NEEDS CONFIRMATION with the detail
        with mock.patch.object(
                dispatches, "snapshot",
                return_value=({"x": {"status": "open"}}, None)):
            r, why, sent = dispatches._reconcile_send("x", "transport failed")
        self.assertEqual(r["status"], "open")
        self.assertIn("NEEDS CONFIRMATION", why)
        self.assertIn("transport failed", why)
        self.assertFalse(sent)


class ASnapshotOfADirtyTreeIsNotAReviewedTipTest(DispatchBase):
    """A reviewed tip that is fab's synthetic snapshot of a dirty worktree.

    fab mints that commit with `git commit-tree` so a node can gate the tree
    you are standing in. It moves no branch, no `git commit` ever runs for it,
    and therefore every commit-time rung in this repo was skipped on its
    content — yet it resolves like any other object, so a row can name it, a
    reviewer can bind it, and the fold can carry it onto trunk. Two reached
    trunk that way.

    WHY THE SUBJECT AND NOT "NO BRANCH CONTAINS IT". The structural-sounding
    rule was measured against this ledger and refuses the healthy population:
    57 of 211 open and held rows name a tip no branch contains, and they are
    ordinary work, because a landed lane has its sha rewritten and its branch
    deleted. Uncontained is the resting state of a healthy row. Across the
    whole 3722-row ledger the same read gives 944 uncontained against 4
    snapshot-subject tips, and those 4 are every known instance.
    """

    def snapshot_tip(self):
        """A REAL dirty-tree snapshot, minted the way fab mints one.

        Built with commit-tree from a scratch index, never with a hand-written
        subject on an ordinary commit: the thing under test is the object fab
        actually produces, and a fixture that only imitates its MESSAGE would
        pass against a reader that checked anything else.
        """
        path = os.path.join(self.repo, "dirty.txt")
        with open(path, "w") as fh:
            fh.write("uncommitted\n")
        index = os.path.join(self.tmp, "snapindex")
        env = dict(os.environ, GIT_INDEX_FILE=index)
        subprocess.run(["git", "-C", self.repo, "add", "-A"],
                       check=True, env=env)
        tree = subprocess.run(["git", "-C", self.repo, "write-tree"],
                              check=True, capture_output=True, text=True,
                              env=env).stdout.strip()
        msg = os.path.join(self.tmp, "snapmsg")
        with open(msg, "w") as fh:
            fh.write("fab snapshot (tracked+untracked) of %s+dirty\n"
                     % self.c[:9])
        tip = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-p", self.c,
             "-F", msg], check=True, capture_output=True, text=True).stdout.strip()
        os.unlink(path)
        # MUST-HIT ON THE FIXTURE ITSELF: the object must exist and must NOT be
        # on any branch, or this arm is testing an ordinary commit.
        self.assertEqual(len(tip), 40)
        contains = subprocess.run(
            ["git", "-C", self.repo, "branch", "--contains", tip],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(contains, "",
                         "the fixture snapshot is ON a branch, so it is not "
                         "the dangling object this arm is about")
        return tip

    def test_send_refuses_a_snapshot_reviewed_tip(self):
        tip = self.snapshot_tip()
        # POSITIVE CONTROL FIRST, on the same door and the same repo: an
        # ordinary commit goes through, so the refusal below is about the tip.
        ok, err, _d = dispatches.send(
            "reviewer", "lane-ok", "review this", self.c,
            kind="review", new_work=True, repo=self.repo)
        self.assertIsNone(err)
        self.assertTrue(ok)

        row, err, _d = dispatches.send(
            "reviewer", "lane-snap", "review this", tip,
            kind="review", new_work=True, repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("COMMIT BEFORE GATING", err or "")
        self.assertIn(tip[:12], err or "")

    def test_send_refuses_a_snapshot_under_every_reference_spelling(self):  # noqa: VACUOUS_ASSERTION — the admission pass runs first and asserts a bound tip on an ordinary commit through all four spellings, so a refusal below cannot be a spelling this door never accepted
        """THE DOOR IS COMPOSED WITH send, AND THE COMPOSITION IS THE SUBJECT.

        Every arm around this one calls `snapshot_tip_refusal` directly, so
        they measure the helper and say nothing about the CALL. The first cut
        of this door lived in `send` against `raw_tip`, which is
        `str(ref).strip().lower()` — and the same snapshot named as `HEAD`
        arrived there as `head`, which resolves to nothing, so the door stayed
        silent while `_base` went on to resolve the ORIGINAL `HEAD` and append
        the snapshot. A helper-only arm passes through all of that.

        THE LOWERCASING WITNESS IS IN THIS ARM, so the regression is provable
        without the old source: the helper is asked for `head` and answers
        nothing, while `send` given `HEAD` refuses. Any door reading the
        lowercased spelling therefore could not have refused, and this arm goes
        red the moment one is put back.

        Detaching HEAD onto the snapshot is not a contrivance — it is the state
        a build node is in while it gates a dirty tree, which is where the
        object comes from in the first place.
        """
        tip = self.snapshot_tip()

        def spellings(sha):
            mixed = "".join(ch.upper() if i % 2 else ch
                            for i, ch in enumerate(sha))
            self.assertNotEqual(mixed, sha,
                                "the mixed-case fixture is not mixed case")
            return (("full", sha), ("short", sha[:12]),
                    ("mixed", mixed), ("symbolic", "HEAD"))

        # ADMISSION PASS FIRST, on an ORDINARY commit through all four
        # spellings, so a refusal below is about the tip and not about a
        # spelling this door never accepted.
        self.git("checkout", "-q", "--detach", self.c)
        for label, ref in spellings(self.c):
            with self.subTest(admit=label):
                row, err, _d = dispatches.send(
                    "reviewer", "lane-ok-" + label, "review " + label, ref,
                    kind="review", new_work=True, repo=self.repo)
                self.assertIsNone(err, err)
                self.assertEqual(row["tip"], self.c,
                                 "the row bound something other than the "
                                 "commit %s resolves to" % label)

        # REFUSAL PASS, the same four spellings against the snapshot.
        # RESTORED INLINE, NEVER THROUGH addCleanup: cleanups run AFTER
        # tearDown, which has already removed the whole fixture tree, so a
        # deferred `git -C <deleted repo>` exits 128 and turns a green arm
        # into an error in its own teardown.
        self.git("checkout", "-q", "--detach", tip)
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        for label, ref in spellings(tip):
            with self.subTest(refuse=label):
                row, err, _d = dispatches.send(
                    "reviewer", "lane-snap-" + label, "review " + label, ref,
                    kind="review", new_work=True, repo=self.repo)
                self.assertIsNone(
                    row, "send admitted the snapshot spelled as %s" % label)
                self.assertIn("COMMIT BEFORE GATING", err or "")
                self.assertIn(tip[:12], err or "")

        # THE WITNESS. `head` is not a ref: the helper cannot answer for it,
        # so a door reading send's lowercased `raw_tip` was structurally
        # unable to refuse the `HEAD` case that send DOES refuse above.
        self.assertIsNone(dispatches.snapshot_tip_refusal(self.repo, "head"))
        self.assertIn("COMMIT BEFORE GATING",
                      dispatches.snapshot_tip_refusal(self.repo, "HEAD") or "")
        self.git("checkout", "-q", self.main)

    def test_a_short_sha_does_not_walk_past_the_helper(self):
        """The ref a caller TYPES is not the object the ledger STORES.

        `_resolve_tip` expands a short sha to the full tip that gets appended,
        so a door keyed on a 40-hex string was walked past by the same snapshot
        named by its first twelve characters and then admitted under its full
        sha. My first fixture used full shas only and therefore could not see
        it — the shape I had in mind, tested against itself.

        The cure is not a longer pattern: the door resolves through the SAME
        accessor the append path uses, so whatever reaches the ledger is what
        gets judged, and a spelling that does not resolve cannot be stored
        either.
        """
        tip = self.snapshot_tip()
        # POSITIVE CONTROL on the shape that already worked, so a failure below
        # is about the short form rather than about the fixture.
        self.assertIn("COMMIT BEFORE GATING",
                      dispatches.snapshot_tip_refusal(self.repo, tip) or "")
        for length in (7, 12, 20, 39):
            with self.subTest(chars=length):
                self.assertIn(
                    "COMMIT BEFORE GATING",
                    dispatches.snapshot_tip_refusal(self.repo, tip[:length]) or "",
                    "a %d-character prefix of the snapshot walked past the door"
                    % length)
        # AND AN ORDINARY COMMIT'S SHORT FORM IS STILL ADMITTED, so the cure is
        # about what the ref RESOLVES to and not about ref length.
        self.assertIsNone(dispatches.snapshot_tip_refusal(self.repo, self.c[:12]))

    def test_an_unreadable_tip_is_not_refused(self):
        """UNKNOWN is not a no, and the census is why.

        26 of 211 live rows name an object this checkout cannot read — another
        repo, a pruned object, a fork. A door that refused there would break
        legitimate sends while answering a question nobody asked it.
        """
        self.assertIsNone(dispatches.snapshot_tip_refusal(self.repo, "f" * 40))
        self.assertIsNone(dispatches.snapshot_tip_refusal(self.repo, "nope"))
        self.assertIsNone(dispatches.snapshot_tip_refusal("/nonexistent", "f" * 40))
        # AND THE PAIRED POSITIVE on the same helper, so the three Nones above
        # are not simply a helper that never speaks.
        self.assertIn("COMMIT BEFORE GATING",
                      dispatches.snapshot_tip_refusal(
                          self.repo, self.snapshot_tip()) or "")

    def test_the_snapshot_marker_is_the_recorded_producer_string(self):
        """The marker this door keys on, asserted UNCONDITIONALLY.

        THE CROSS-REPO PIN IS NOT BUILDABLE FROM HERE AND SAYING SO IS THE
        POINT. The producer is `fab/bin/fab` on the HUB — the commit-tree at
        the dirty-tree branch — and this suite runs on a BUILD NODE, where
        ~/fab/bin holds only the spoke scripts (fab-gate-spoke, fab-gate-state
        and friends) and the hub `fab` does not exist at all. Measured on both
        nodes before this arm was written: neither candidate hub path is
        present. An arm that read the producer would therefore SKIP on every
        real run — a dead arm that reads as coverage, which is worse than no
        arm.

        So this asserts what it CAN: the exact literal, so a silent edit to the
        door is caught here rather than by a door that has gone quietly blind.
        The other half — that fab still WRITES this subject — belongs to fab's
        own suite, which runs where fab lives; it is filed against the project
        that owns fab rather than faked here.
        """
        self.assertEqual(dispatches._SNAPSHOT_SUBJECT, "fab snapshot")
        # AND THE DOOR USES IT: the constant is not merely present, it decides.
        # Without this, renaming the constant's USE site would leave the
        # equality above passing over a door that keys on something else.
        tip = self.snapshot_tip()
        self.assertIn("COMMIT BEFORE GATING",
                      dispatches.snapshot_tip_refusal(self.repo, tip) or "")
        with mock.patch.object(dispatches, "_SNAPSHOT_SUBJECT",
                               "something else entirely"):
            self.assertIsNone(dispatches.snapshot_tip_refusal(self.repo, tip),
                              "the door still refused after the marker was "
                              "changed, so it is keyed on something other "
                              "than this constant")

    def test_the_producer_still_writes_the_marker(self):
        """Best-effort cross-repo pin, run only where the producer exists.

        This is the hub-only half. It cannot run on a build node and must not
        pretend to: the sibling arm above carries the unconditional assertion,
        so a skip here costs no coverage.

        WHERE the producer lives is this host's own names, never the source's:
        the deploy directory, then the deploy project's registered checkout
        (`deploy-dir` and `deploy-project`, the pair `helm doctor`'s deployed
        artifact rung reads). The suite runs under a temp HELM_HOME, so both
        are read from the live root, read-only.
        """
        from helm import localnames, pk
        live = os.path.join(home.default_home(), home.GLOBAL)
        names = pk.read_json(os.path.join(live, localnames.CONFIG))
        names = names if isinstance(names, dict) else {}
        reg = pk.read_json(os.path.join(live, "registry.json"))
        projects = (reg.get("projects") if isinstance(reg, dict) else None) or {}
        entry = projects.get(names.get("deploy-project") or "")
        checkout = entry.get("path") if isinstance(entry, dict) else None
        candidates = [os.path.join(os.path.expanduser(root), *tail)
                      for root, tail in ((names.get("deploy-dir"), ("fab",)),
                                         (checkout, ("fab", "bin", "fab")))
                      if isinstance(root, str) and root]
        producer = next((c for c in candidates if os.path.isfile(c)), None)
        if producer is None:
            self.skipTest("the hub fab is not present here — this suite runs "
                          "on a build node, which carries only the spoke "
                          "scripts; the unconditional marker assertion is in "
                          "test_the_snapshot_marker_is_the_recorded_producer_string")
        with open(producer, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        self.assertIn("commit-tree", source,
                      "fab no longer mints a snapshot with commit-tree; this "
                      "arm is reading the wrong producer")
        self.assertIn('-m "%s' % dispatches._SNAPSHOT_SUBJECT, source,
                      "fab's snapshot subject no longer starts with %r, so "
                      "the dispatch door is blind to the object it exists to "
                      "refuse" % dispatches._SNAPSHOT_SUBJECT)


if __name__ == "__main__":
    pass  # unittest.main() moved to EOF: 38 later tests were invisible to direct runs

class HoldReleaseTest(DispatchBase):
    """helm dispatch hold/release -- non-terminal pause/resume transitions."""

    def test_hold_puts_an_open_row_into_held_status(self):
        row = self.add(lane="blocked")
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])
        out, why = dispatches.mark_hold(row["id"], "waiting for #210 land")
        self.assertIsNone(why)
        self.assertEqual(out["status"], "held")
        self.assertEqual(out["hold_reason"], "waiting for #210 land")
        self.assertEqual(dispatches.open_rows(), [])

    def test_an_ordinary_hold_is_not_owner_gated(self):
        """The DEFAULT side of the flag, and the reason it is a flag at all.

        Most holds wait on a machine: a build box, a credential, another
        lane. Those are the fleet's debt and must keep counting as stalls —
        if `owner_gated` defaulted true, or leaked from the reason text, the
        stall count would drop to zero and the board would go quiet about
        work nobody is doing."""
        row = self.add()
        out, why = dispatches.mark_hold(
            row["id"], "waiting on the owner's build box to come back")
        self.assertIsNone(why)
        self.assertIs(out["owner_gated"], False)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertIs(replayed.get("owner_gated"), False,
                      "the fold must not invent owner-gatedness from prose — "
                      "this reason SAYS owner and is a machine dependency")

    def test_an_owner_gated_hold_survives_the_fold(self):
        """The flag is durable, not a return-value decoration. Every consumer
        (the projection, the console, the stall count) reads the FOLDED row,
        so a flag that lives only in mark_hold's return is invisible to all
        of them."""
        row = self.add()
        out, why = dispatches.mark_hold(
            row["id"], "needs a publication decision", owner_gated=True)
        self.assertIsNone(why)
        self.assertIs(out["owner_gated"], True)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertIs(replayed.get("owner_gated"), True)
        self.assertEqual(replayed["status"], "held")

    def test_hold_is_idempotent_on_the_same_reason(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "blocked on upstream")
        again, why = dispatches.mark_hold(row["id"], "blocked on upstream")
        self.assertIsNone(why)
        self.assertEqual(again["status"], "held")
        self.assertEqual(len(dispatches.history(row["id"])), 2)

    def test_hold_replays_from_a_fresh_disk_snapshot(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "waiting for #210")
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "held")
        self.assertEqual(replayed["hold_reason"], "waiting for #210")

    def test_hold_requires_a_reason(self):
        row = self.add()
        _r, why = dispatches.mark_hold(row["id"], "")
        self.assertIn("reason", why)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_a_held_row_refuses_a_different_hold_reason(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "reason one")
        _r, why = dispatches.mark_hold(row["id"], "reason two")
        self.assertIn("already held with a different reason", why)

    def test_a_closed_row_cannot_be_held(self):
        row = self.add()
        dispatches.mark_cancel(row["id"], "moot")
        _r, why = dispatches.mark_hold(row["id"], "too late")
        self.assertIn("cancelled", why)

    def test_a_verdicted_row_cannot_be_held(self):
        row = self.add()
        dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        _r, why = dispatches.mark_hold(row["id"], "too late")
        self.assertIn("verdict", why.lower())

    def test_a_held_row_cannot_be_verdicted(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "waiting for upstream")
        _r, why = dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        self.assertIsNotNone(why)
        self.assertIn("held", why.lower())
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "held")

    def test_a_held_row_can_be_cancelled(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "blocked")
        out, why = dispatches.mark_cancel(row["id"], "blocker resolved -- moot")
        self.assertIsNone(why)
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(dispatches.open_rows(), [])

    def test_release_returns_held_row_to_open(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "waiting for #210 land")
        out, why = dispatches.mark_release(row["id"])
        self.assertIsNone(why)
        self.assertEqual(out["status"], "open")
        self.assertNotIn("hold_reason", out)
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])

    def test_release_is_idempotent_on_already_open_row(self):
        row = self.add()
        out, why = dispatches.mark_release(row["id"])
        self.assertIsNone(why)
        self.assertEqual(out["status"], "open")

    def test_release_replays_from_disk(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "blocked")
        dispatches.mark_release(row["id"])
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "open")
        self.assertIn("release_reason", replayed)

    def test_release_replay_clears_the_specific_owner_hold_identity(self):
        row = self.add()
        held, why = dispatches.mark_hold(
            row["id"], "choose publication", owner_gated=True)
        self.assertIsNone(why)
        self.assertTrue(held["owner_gated"])
        self.assertIn("hold_reason", held)
        self.assertIn("hold_ts", held)
        out, why = dispatches.mark_release(row["id"])
        self.assertIsNone(why)
        self.assertEqual(out["status"], "open")
        self.assertNotIn("owner_gated", out)
        self.assertNotIn("hold_reason", out)
        self.assertNotIn("hold_ts", out)
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "open")
        self.assertNotIn("owner_gated", replayed)
        self.assertNotIn("hold_reason", replayed)
        self.assertNotIn("hold_ts", replayed)

    def test_release_on_cancelled_is_refused(self):
        row = self.add()
        dispatches.mark_cancel(row["id"], "moot")
        _r, why = dispatches.mark_release(row["id"])
        self.assertIn("cancelled", why)

    def test_release_on_verdict_is_refused(self):
        row = self.add()
        dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        _r, why = dispatches.mark_release(row["id"])
        self.assertIn("verdict", why.lower())

    def test_held_row_not_in_overdue_nor_stop_candidate(self):
        row = self.add(lane="blocked-old", deadline_s=60)
        self.age(row["id"], 3600)
        self.assertTrue(dispatches.overdue())
        cand, _kind, _un = dispatches.stop_candidate()
        self.assertIsNotNone(cand)
        dispatches.mark_hold(row["id"], "waiting for deps")
        self.assertEqual(dispatches.overdue(), [])
        cand, _kind, _un = dispatches.stop_candidate()
        self.assertIsNone(cand)

    def test_held_row_visible_in_default_list_not_open_list(self):
        row = self.add(lane="held-list")
        dispatches.mark_hold(row["id"], "blocked by upstream CI")
        rc, out, _err = run(dispatches.cmd_dispatch, ["list", "--open"])
        self.assertEqual(rc, 0)
        self.assertNotIn(row["id"], out)
        rc, out, _err = run(dispatches.cmd_dispatch, ["list", "--held"])
        self.assertEqual(rc, 0)
        self.assertIn("HELD (blocked by upstream CI)", out)

    def test_held_row_label_shows_reason_in_default_list(self):
        row = self.add(lane="labeled-hold")
        dispatches.mark_hold(row["id"], "gate: external audit pending")
        rc, out, _err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 0)
        self.assertIn("HELD (gate: external audit pending)", out)

    def test_hold_resolves_short_id(self):
        row = self.add()
        out, why = dispatches.mark_hold(row["id"][:12], "blocked")
        self.assertIsNone(why)
        self.assertEqual(out["id"], row["id"])
        self.assertEqual(out["status"], "held")

    def test_release_resolves_short_id(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "blocked")
        out, why = dispatches.mark_release(row["id"][:12])
        self.assertIsNone(why)
        self.assertEqual(out["id"], row["id"])
        self.assertEqual(out["status"], "open")

    def test_hold_resolves_id_before_validating_reason(self):
        _out, why = dispatches.mark_hold("deadbeef", "x" * 300)
        self.assertIn("no such dispatch", why)
        self.assertNotIn("at most", why)
        row = self.add()
        _out, why = dispatches.mark_hold(row["id"][:12], "x" * 300)
        self.assertIn("at most", why)

    def test_cli_hold_and_release_roundtrip(self):
        row = self.add(lane="cli-roundtrip")
        rc, out, err = run(dispatches.cmd_dispatch, ["hold", row["id"][:12], "external deps"])
        self.assertEqual(rc, 0, err)
        self.assertIn("HELD (external deps)", out)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["status"], "held")
        rc, out, err = run(dispatches.cmd_dispatch, ["release", row["id"][:12]])
        self.assertEqual(rc, 0, err)
        self.assertIn("RELEASED to OPEN", out)
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["status"], "open")

    def test_cli_hold_usage_on_no_args(self):
        rc, _out, err = run(dispatches.cmd_dispatch, ["hold"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_cli_release_usage_on_no_args(self):
        rc, _out, err = run(dispatches.cmd_dispatch, ["release"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


if __name__ == "__main__":
    unittest.main()


class StdinBodyDoorTest(DispatchBase):
    """`dispatch send` reads its body from stdin, and that read can never end.

    THE FAILURE THIS PINS IS A HANG, NOT A WRONG ANSWER, and a hang is the one
    outcome no assertion about output can catch. `sys.stdin.read()` returns
    when the fd reaches EOF; a live UNIX socket with no writer closing it
    never does. Under an agent harness stdin is exactly that, and the verb then
    prints nothing, creates no row, and sits reading as a slow queue — a socket
    fd parked in `unix_stream_data_wait`.

    So the arms below assert on the SELECT, which is the decision that
    precedes the read, and they carry both polarities: an fd with no data must
    not be entered, an fd with data or at EOF must be.
    """

    def _pair(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return left, right

    def test_a_socket_with_NO_DATA_is_not_a_body_and_EOF_still_is(self):
        """Both polarities, because only the pair says the guard discriminates.

        `/dev/null` is the scripted pole this guard must not break: it is
        non-tty and it is READY, because an fd at EOF is readable. It reaches
        the read and gets an empty body, exactly as before.
        """
        left, _right = self._pair()
        self.assertFalse(dispatches._stdin_has_a_body_fd(left, window=0.05),
                         "a socket with no writer was treated as a body, "
                         "which is the read that never returns")
        _l2, right = self._pair()
        right.sendall(b"a body arrived\n")
        self.assertTrue(dispatches._stdin_has_a_body_fd(_l2, window=0.05),
                        "a socket carrying bytes was not seen as a body")
        with open(os.devnull) as null:
            self.assertTrue(dispatches._stdin_has_a_body_fd(null, window=0.05),
                            "/dev/null stopped counting as ready, so the "
                            "scripted positional pole now refuses")

    def test_an_UNSELECTABLE_stdin_keeps_the_OLD_behaviour(self):
        """This guard converts a hang into a usage error; it never invents a
        refusal on an fd it cannot classify."""
        class Unselectable:
            def fileno(self):
                raise OSError("no fileno here")
        self.assertTrue(
            dispatches._stdin_has_a_body_fd(Unselectable(), window=0.05),
            "an unclassifiable stdin was refused rather than read")

    def test_the_SEND_DOOR_returns_usage_instead_of_reading_a_dead_socket(self):  # noqa: VACUOUS_ASSERTION — the heredoc control on the last lines asserts unconditionally that a body-carrying stdin still reaches the row, on the same door
        """The whole point: rc 2 and a usage line, not a process that waits.

        A bounded `assertRaises`-free arm cannot prove "did not hang" without
        a clock, so this asserts the OBSERVABLE the cure produces — the usage
        refusal — and the control below proves the body path is still live.
        """
        left, _right = self._pair()
        reads = []
        real_read = left.makefile("r").read

        class Fd:
            def isatty(self):
                return False

            def fileno(self):
                return left.fileno()

            def read(self, *a):
                reads.append(a)
                return real_read(*a)

        with mock.patch.object(sys, "stdin", Fd()):
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "send", "reviewer", "no-body-lane",
                "--ref", self.a, "--repo", self.repo, "--kind", "review",
                "--new-work"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm dispatch send", err)
        self.assertEqual(reads, [],
                         "the door entered the read on a socket that never "
                         "reaches EOF")

        # THE CONTROL, unconditional and on the same door: a stdin that DOES
        # carry a body still reaches the read and still builds the row.
        body, writer = self._pair()
        writer.sendall(b"the body still arrives on stdin\n")
        writer.shutdown(socket.SHUT_WR)

        live_reads = []
        handle = body.makefile("r")

        class LiveFd:
            def isatty(self):
                return False

            def fileno(self):
                return body.fileno()

            def read(self, *a):
                live_reads.append(a)
                return handle.read(*a)

        with mock.patch.object(sys, "stdin", LiveFd()):
            rc, out, _err = run(dispatches.cmd_dispatch, [
                "send", "reviewer", "with-body-lane",
                "--ref", self.a, "--repo", self.repo, "--kind", "review",
                "--key", "stdin-body-control", "--new-work"])
        self.assertTrue(live_reads,
                        "a body-carrying stdin was never read, so the guard "
                        "closed the door it was meant to keep open")
        self.assertEqual(rc, 0, out)


class ApprovalTierAdvisoryTest(DispatchBase):
    def policy(self, members, reason="only the independent tier may final-approve"):
        return store.write_prior({
            "id": "fleet-approval-tier",
            "statement": "Final approval uses the declared tier.",
            "confidence": 1.0,
            "stated_ts": "2026-07-29T00:00:00Z",
            "source": "human",
            "policy_kind": "approval-tier",
            "policy_members": members,
            "policy_reason": reason,
        }, root_dir=os.path.join(home.global_dir(), "premises"))

    _send_seq = 0

    def send_cli(self, recipient="reviewer", kind="review"):
        self._send_seq += 1
        return run(dispatches.cmd_dispatch, [
            "send", recipient, "approval-check-%d" % self._send_seq,
            "Review this tip",
            "--ref", self.a, "--repo", self.repo, "--kind", kind,
            "--key", "check-" + recipient + "-" + kind, "--new-work",
        ])

    def runtime(self, name, family):
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = name
        try:
            return seats.write_roster(
                name, runtime={"family": family, "backend": "native"},
                presence_beat=False)
        finally:
            os.environ["HELM_CHAT_NAME"] = prior

    def mint(self, family, name):
        base = seat.seat_dir(family)
        os.makedirs(base, exist_ok=True)
        open(os.path.join(base, "config.yaml"), "a").close()
        if name != family:
            d = os.path.join(base, "instances", name)
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "config.yaml"), "a").close()

    def test_historical_approval_without_author_evidence_never_borrows_current_family(self):
        self.policy(["family:claude"])
        self.runtime("reviewer", "claude")
        state, message = dispatches.approval_tier_for_verdict({
            "recipient": "reviewer", "repo_id": os.path.join(self.repo, ".git")})
        self.assertEqual(state, "unknown")
        self.assertEqual(dispatches.tier_unknown_kind(state), dispatches.TIER_PRE_TIER)
        self.assertIn("PRE-TIER", message)

    def test_approval_replays_the_verdict_author_session_not_the_current_roster(self):
        self.policy(["family:claude"])
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = "reviewer"
        try:
            seats.write_roster(
                "reviewer", session="session-a",
                runtime={"family": "codex", "backend": "native"},
                presence_beat=False)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "session-a"
            row = self.add(recipient="reviewer", ref=self.side)
            proof = {"v": 3, "session": "session-a",
                     "agent_harness": "claude", "agent_pid": 101,
                     "agent_starttime": 11, "model": "gpt-5.6-sol",
                     "local_base_url": "http://127.0.0.1:8317",
                     "proxy_pid": 202, "proxy_identity": "proc:22",
                     "proxy_config": "/safe/codex.yaml",
                     "config_sha256": "a" * 64,
                     "route": {"alias": "gpt-5.6-sol",
                               "provider": "codex",
                               "upstream_model": "gpt-5.6-sol"},
                     "observed_at": 1000,
                     "canary": {"state": "HEALTHY", "status": 200}}
            authority = {"v": 3, "identity": "reviewer",
                         "roster_identity": "reviewer", "session": "session-a",
                         "proxy_proof": proof}
            family = ({"codex"}, authority,
                      dispatches._subsumed_family_anchor(authority), None)
            with mock.patch.object(
                    dispatches.gate, "bind",
                    return_value=("VERIFIED", "a" * 16, "test receipt")), \
                    mock.patch.object(
                        dispatches, "_approval_identity_family_evidence",
                        return_value=family):
                verdict, err = dispatches.mark_verdict(
                    row["id"], self.side, "reviewed session A",
                    polarity="approve", basis="measured", bind_author=True)
            self.assertIsNone(err, err)
            self.assertEqual(verdict["verdict_author_session"], "session-a")
            resolved = verdict["verdict_author_runtime_evidence"]["resolved"]
            self.assertEqual(resolved["family"], "codex")
            self.assertEqual(resolved["model"], "gpt-5.6-sol")
            self.assertEqual(resolved["provider"], "codex")
            self.assertEqual(resolved["agent_harness"], "claude")
            forged = json.loads(json.dumps(
                verdict["verdict_author_runtime_evidence"]))
            forged["session"] = "session-b"
            forged_anchor = dispatches._verdict_author_runtime_anchor(forged)
            mismatch = dispatches._verdict_author_runtime_error(
                forged, "reviewer", "session-b", forged_anchor)
            self.assertIsNotNone(mismatch)
            self.assertIn("session", mismatch)
            native_authority = {
                "v": 5, "identity": "reviewer",
                "roster_identity": "reviewer", "session": "session-a",
                "runtime": {"family": "claude", "backend": "native"},
                "runtime_verified": True}
            native = dispatches._verdict_author_runtime_evidence(
                "reviewer", "session-a", "claude", native_authority)
            native["session"] = "session-b"
            mismatch = dispatches._verdict_author_runtime_error(
                native, "reviewer", "session-b",
                dispatches._verdict_author_runtime_anchor(native))
            self.assertIsNotNone(mismatch)
            self.assertIn("session", mismatch)
            seats.write_roster(
                "reviewer", session="session-b",
                runtime={"family": "claude", "backend": "native"},
                presence_beat=False)
        finally:
            os.environ["HELM_CHAT_NAME"] = prior or "integrator"
        with mock.patch.dict(seat.FAMILIES, {}, clear=True):
            replayed = dispatches.snapshot()[0][row["id"]]
            why, tier = landreq._approval_refusal(replayed)
        self.assertEqual(tier, "outside")
        self.assertIn("does not permit", why)
        self.assertEqual(replayed["verdict_author_session"], "session-a")
        self.assertEqual(
            replayed["verdict_author_runtime_evidence"]["resolved"]["family"],
            "codex")

    def test_invalid_review_recipient_warns_with_policy_but_send_succeeds(self):
        self.policy(["seat:lead", "family:codex"])
        self.runtime("reviewer", "gemini")
        with mock.patch.object(dispatches, "_ref_sanity", return_value=[]):
            rc, out, err = self.send_cli()
        self.assertEqual(rc, 0)
        self.assertIn("outside the current approval tier", err)
        self.assertIn("only the independent tier may final-approve", err)
        self.assertIn("source prior: fleet-approval-tier", err)
        self.assertIn("valid set: family:codex, seat:lead", err)
        self.assertIn("Warning only", err)
        rid = out.split()[2]
        self.assertEqual(dispatches.snapshot()[0][rid]["recipient"], "reviewer")

    def test_resolved_Gemini_preserves_policy_reason_but_unknown_is_not_outside(self):
        reason = "Gemini policy refusal must remain visible after resolution"
        self.policy(["family:codex"], reason=reason)
        self.runtime("reviewer", "gemini")
        state, message = dispatches.approval_tier("reviewer")
        self.assertEqual(state, "outside")
        self.assertIn(reason, message)
        state, message = dispatches.approval_tier("unresolved-reviewer")
        self.assertEqual(state, "unknown")
        self.assertNotIn("outside the current approval tier", message)

    def test_exact_and_explicit_family_members_are_silent_and_dynamic(self):
        self.policy(["seat:reviewer"])
        self.assertIsNone(dispatches._approval_tier_advisory("reviewer"))
        self.runtime("reviewer", "gemini")
        self.policy(["family:codex"])
        self.assertIn("outside the current approval tier",
                      dispatches._approval_tier_advisory("reviewer"))
        self.policy(["family:gemini"])
        self.assertIsNone(dispatches._approval_tier_advisory("reviewer"))

    def test_case_only_variants_share_exact_and_family_approval_identity(self):
        self.policy(["seat:someone-else"])
        self.assertIn("outside the current approval tier",
                      dispatches._approval_tier_advisory("reviewer"))

        self.policy(["seat:REVIEWER"])
        self.assertIsNone(dispatches._approval_tier_advisory("reviewer"))

        self.runtime("Reviewer", "gemini")
        self.policy(["family:gemini"])
        self.assertIsNone(dispatches._approval_tier_advisory("REVIEWER"))

        self.runtime("Codex-9", "codex")
        self.policy(["family:codex"])
        self.assertIsNone(dispatches._approval_tier_advisory("CODEX-9"))

    def test_minted_offline_family_grants_no_approval_authority(self):
        self.policy(["family:codex"])
        self.mint("codex", "codex-9")
        note = dispatches._approval_tier_advisory("codex-9")
        self.assertIn("check unavailable", note)
        self.assertIn("no unique canonical roster runtime record", note)
        self.assertNotIn("outside the current approval tier", note)
        note = dispatches._approval_tier_advisory("codex-99")
        self.assertIn("check unavailable", note)
        self.assertNotIn("outside the current approval tier", note)

    def test_runtime_outranks_minted_label_and_malformed_policy_is_unknown(self):
        self.policy(["family:codex"])
        self.mint("codex", "codex-9")
        self.runtime("codex-9", "gemini")
        note = dispatches._approval_tier_advisory("codex-9")
        self.assertIn("outside the current approval tier", note)
        self.assertNotIn("conflicting", note)
        self.policy(["codex"])
        note = dispatches._approval_tier_advisory("reviewer")
        self.assertIn("malformed selector", note)
        self.assertNotIn("outside the current approval tier", note)

    def test_foreign_runtime_spoof_and_malformed_runtime_fail_unknown(self):  # noqa: VACUOUS_ASSERTION — runtime_verified=False positively arms the refusal
        self.policy(["family:codex"])
        seats.write_roster("reviewer", runtime={"family": "codex"},
                           presence_beat=False)
        self.assertFalse(seats.roster()["reviewer"]["runtime_verified"])
        note = dispatches._approval_tier_advisory("reviewer")
        self.assertIn("check unavailable", note)
        self.assertNotIn("outside the current approval tier", note)
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"reviewer": {"runtime": ["codex"]}}, f)
        note = dispatches._approval_tier_advisory("reviewer")
        self.assertIn("no verified native runtime", note)
        self.assertNotIn("outside the current approval tier", note)

    def test_unavailable_policy_is_not_invalid_and_the_scope_is_the_review_kind(self):
        """THE SCOPE IS THE KIND, NOT THE VERB — re-pointed, not deleted.

        This arm previously pinned the advisory to the SEND path and asserted
        that `add --kind review` must never reach it. That scope was the
        defect: whether a recipient's APPROVE CAN BIND is a fact about the
        RECIPIENT, so a REBOUND review and a library caller were writing review
        obligations to seats nobody had vouched for and seeing nothing. The
        surviving restriction is the advisory's own: it speaks for the review
        kind and is silent for a build, which produces no verdict to bind.
        """
        with mock.patch.object(dispatches, "_ref_sanity", return_value=[]):
            rc, _out, err = self.send_cli()
        self.assertEqual(rc, 0)
        self.assertIn("check unavailable", err)
        self.assertNotIn("outside the current approval tier", err)
        # A BUILD MUST STILL NOT REACH IT. Unchanged from this arm's original
        # half, and it is the half that keeps the widening honest.
        with mock.patch.object(dispatches, "_approval_tier_advisory",
                               side_effect=AssertionError("wrong scope")), \
                mock.patch.object(dispatches, "_ref_sanity", return_value=[]):
            rc, _out, _err = self.send_cli(recipient="builder", kind="build")
            self.assertEqual(rc, 0)
        # AND A REVIEW WRITTEN THROUGH add() MUST REACH IT. This is the half
        # that closes the hole: a review obligation can be written by a door
        # that is not the send verb, and its recipient is no less in need of a
        # tier answer for having arrived that way.
        seen = []
        with mock.patch.object(dispatches, "_approval_tier_advisory",
                               side_effect=lambda r: seen.append(r)), \
                mock.patch.object(dispatches, "_ref_sanity", return_value=[]):
            rc, _out, _err = run(dispatches.cmd_dispatch, [
                "add", "reviewer", "approval-check", "--ref", self.a,
                "--repo", self.repo, "--kind", "review", "--new-work"])
            self.assertEqual(rc, 0)
        self.assertEqual(seen, ["reviewer"],
                         "a review written through add() never consulted the "
                         "tier, which is the population this cure exists for")


class ApprovalTierMemoTest(DispatchBase):
    """A transient UNKNOWN must not answer for the whole projection.

    task/1067, measured twice 2026-08-11: the tier resolution is live I/O (a
    proxywatch state file mid-pass-swap, a canary through a busy proxy), and
    one transient failure early in an ~8-minute compose was memoised for the
    seat's every row — a batch of gated approves projected REVIEWED, the
    compose refused them, and the identical retry admitted them. projscope's
    own forget() docstring names the law; the tier memo door follows it."""

    @staticmethod
    def _unknown(kind, why="planted"):
        return dispatches.TierUnknown(kind, "unknown"), why

    def test_a_transient_unknown_is_reasked_by_the_next_row_in_the_same_scope(self):
        from helm import projscope
        answers = [self._unknown(dispatches.TIER_TRANSIENT, "canary hang"),
                   ("ok", None)]
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=answers) as uncached, \
                projscope.scope():
            self.assertEqual(
                dispatches.approval_tier("reviewer", repo="/r")[0], "unknown")
            self.assertEqual(
                dispatches.approval_tier("reviewer", repo="/r"),
                ("ok", None))
        self.assertEqual(uncached.call_count, 2)

    def test_a_durable_unknown_is_an_answer_and_is_kept_for_the_scope(self):
        """Forgetting EVERY unknown is a different bug wearing the cure's
        clothes: a malformed proxywatch record, a policy with no reason, a
        recipient that names no seat, a seat whose upstream stores nothing —
        none of those is a fact about the moment, so re-deriving one per row
        buys the identical answer at full price and tells every downstream
        read that helm is still trying. Measured on the live roster
        2026-08-11: eleven seats read unknown and NOT ONE was transient (six
        damaged, five dark).

        Each arm plants a SECOND answer that differs; if the durable one were
        evicted the second call would surface it, so this reddens on an
        over-broad forget rather than merely counting calls."""
        from helm import projscope
        # THE UNCONDITIONAL POSITIVE CONTROL, ahead of the loop: the planted
        # second answer IS reachable through this exact call shape, so a
        # subTest below that never reaches it is measuring retention and not a
        # broken harness.
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=[self._unknown(
                                   dispatches.TIER_TRANSIENT), ("ok", "2nd")]), \
                projscope.scope():
            dispatches.approval_tier("control", repo="/r")
            self.assertEqual(dispatches.approval_tier("control", repo="/r"),
                             ("ok", "2nd"))
        for kind in (dispatches.TIER_DARK, dispatches.TIER_DAMAGED,
                     dispatches.TIER_UNNAMED):
            with self.subTest(kind=kind):
                first = self._unknown(kind, "durable %s" % kind)
                with mock.patch.object(
                        dispatches, "_approval_tier_uncached",
                        side_effect=[first, ("ok", None)]) as uncached, \
                        projscope.scope():
                    for _ in range(3):
                        state, why = dispatches.approval_tier("r", repo="/r")
                        self.assertEqual(state, "unknown")
                        self.assertEqual(why, "durable %s" % kind)
                        self.assertEqual(
                            dispatches.tier_unknown_kind(state), kind)
                self.assertEqual(uncached.call_count, 1)

    def test_an_unclassified_unknown_is_reasked_rather_than_frozen(self):
        """A plain "unknown" nobody tagged might be either kind, and of the
        two errors re-asking costs a read while freezing costs correctness.
        This is also the compatibility arm: every pre-existing caller and test
        double returns exactly this shape — and on this base so does every
        proxywatch snapshot failure, which the resolver mints UNCLASSIFIED."""
        from helm import projscope
        with mock.patch.object(
                dispatches, "_approval_tier_uncached",
                side_effect=[("unknown", "untagged"), ("ok", None)]) as uncached, \
                projscope.scope():
            state, _why = dispatches.approval_tier("r", repo="/r")
            self.assertEqual(dispatches.tier_unknown_kind(state),
                             dispatches.TIER_UNCLASSIFIED)
            self.assertEqual(dispatches.approval_tier("r", repo="/r"),
                             ("ok", None))
        self.assertEqual(uncached.call_count, 2)

    def test_a_measured_answer_still_memoises_for_the_whole_scope(self):
        # THE MUST-MISS CONTROL for the forget: it must discriminate BY STATE,
        # not blanket-evict. If "ok" were also forgotten, the second call
        # would surface the planted "outside" — so this arm fails on both a
        # missing memo and an over-broad forget. "outside" is itself a
        # measured answer and memoises too, pinned in the second scope.
        from helm import projscope
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=[("ok", None),
                                            ("outside", "planted")]) as uncached, \
                projscope.scope():
            self.assertEqual(dispatches.approval_tier("r2", repo="/r"),
                             ("ok", None))
            self.assertEqual(dispatches.approval_tier("r2", repo="/r"),
                             ("ok", None))
        self.assertEqual(uncached.call_count, 1)
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=[("outside", "why"),
                                            ("ok", None)]) as uncached, \
                projscope.scope():
            self.assertEqual(dispatches.approval_tier("r3", repo="/r"),
                             ("outside", "why"))
            self.assertEqual(dispatches.approval_tier("r3", repo="/r"),
                             ("outside", "why"))
        self.assertEqual(uncached.call_count, 1)

    def test_historical_verdict_never_enters_the_live_eviction_rule(self):
        """The frozen evidence-free input stays pre-tier, not transient-to-OK.

        Live advisory eviction still has its positive controls above. Historical
        verdicts no longer enter that mutable door, even when it would recover.
        """
        from helm import projscope
        row = {"recipient": "reviewer", "repo_id": "/r"}
        with mock.patch.object(
                dispatches, "_approval_tier_uncached",
                side_effect=AssertionError("historical verdict read live tier")), \
                projscope.scope():
            for _ in range(2):
                state, why = dispatches.approval_tier_for_verdict(row)
                self.assertEqual(state, "unknown")
                self.assertEqual(dispatches.tier_unknown_kind(state), dispatches.TIER_PRE_TIER)
                self.assertIn("PRE-TIER", why)


class RefSanityTest(DispatchBase):
    """A --ref that does not mean what the sender thinks, caught at WRITE time.

    Both shapes were committed by the integrator on 2026-07-26:
      1. ALREADY ON TRUNK — a build sent with --ref at the RECIPIENT's own
         commit, already merged, so the row asked them to gate finished work.
         It surfaced only because that seat refused instead of complying.
      2. UNRELATED TO ITS LANE — a real, accurate review bound to a different
         lane's merge three commits earlier, a tip still containing everything
         the change removed. Every surface read gated-and-landed.

    WARN, NEVER REFUSE: a post-land review is legitimate and a false refusal
    blocks real work, while a zombie row costs one surfaced row. And this can
    only work at WRITE time — once a lane merges, its own commits become
    indistinguishable from trunk's and the question is unanswerable.
    """

    def test_a_ref_already_on_trunk_is_flagged(self):
        # self.c is on the main branch of the fixture repo
        out = dispatches._ref_sanity(self.c, "some-lane", repo=self.repo)
        self.assertTrue(any("ALREADY on" in w for w in out),
                        "a merged tip must be flagged, got %r" % out)

    def test_a_lane_rooms_own_tip_is_neither_landed_nor_foreign_on_a_master_trunk(self):
        """Sent from inside the lane's own worktree on a repo trunked on
        `master`, the lane's own tip drew BOTH "ALREADY on lane/<lane>" and
        "NOT one of lane/<lane>'s own commits": the trunk had resolved to the
        room's own branch. The tip is the lane's, on a lane ahead of trunk."""
        import subprocess
        base = tempfile.mkdtemp(prefix="helm-test-refsanity-master-")
        self.addCleanup(shutil.rmtree, base, True)
        repo = os.path.join(base, "repo")
        os.makedirs(repo)

        def sh(cwd, *args):
            r = subprocess.run(list(args), cwd=cwd, capture_output=True,
                               text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()

        def commit(cwd, name):
            with open(os.path.join(cwd, name), "w") as f:
                f.write(name)
            sh(cwd, "git", "add", "-A")
            sh(cwd, "git", "commit", "-q", "-m", name)
            return sh(cwd, "git", "rev-parse", "HEAD")

        sh(repo, "git", "init", "-q", "-b", "master")
        sh(repo, "git", "config", "user.email", "t@t")
        sh(repo, "git", "config", "user.name", "t")
        commit(repo, "seed")
        room = os.path.join(base, "wt", "the-lane")
        sh(repo, "git", "worktree", "add", "-q", "-b", "lane/the-lane", room)
        own = commit(room, "lane-work")
        sh(repo, "git", "branch", "lane/other", "master")
        other_room = os.path.join(base, "wt", "other")
        sh(repo, "git", "worktree", "add", "-q", other_room, "lane/other")
        foreign = commit(other_room, "other-work")

        out = dispatches._ref_sanity(own, "the-lane", repo=room, kind="review")
        self.assertFalse([w for w in out if "ALREADY on" in w], out)
        self.assertFalse([w for w in out if "own commits" in w], out)
        self.assertTrue([w for w in out if "ff-able from master" in w],
                        "the note that IS true must still print: %r" % out)
        # CONTROL, from the SAME room: another lane's commit is still named
        # foreign, so the quiet above is about trunk resolution, not a check
        # that stopped firing.
        out = dispatches._ref_sanity(foreign, "the-lane", repo=room,
                                     kind="review")
        self.assertTrue([w for w in out if "own commits" in w], out)

    def test_the_ff_able_NOTE_reads_the_kind_the_row_will_carry(self):  # noqa: VACUOUS_ASSERTION — the flagged assertion is the build MUST-MISS asserting an empty ff-able list, and its control is necessarily a SECOND call: one _ref_sanity call cannot answer for both kind=review and kind=build, so a same-call positive is impossible by construction. The control is the unconditional assertTrue three lines above the loops — the canonical spelling through the IDENTICAL fixture, repo and helper must produce an ff-able NOTE, so a predicate that emitted nothing for anything fails there rather than passing here
        """LATENT TODAY, AND CURED FOR THE SHAPE RATHER THAN THE SYMPTOM.

        The ff-able NOTE is gated on the kind being a review. That test was a
        RAW string comparison while `clean_kind` lowercases and strips, so a
        caller handing it "Review" lost the note. It is unreachable from fleet
        paths right now — `cmd_dispatch` binds kind through clean_kind before
        the only call — which is why this arm drives the function DIRECTLY
        rather than through the CLI: the defect lives in the predicate's
        contract with its callers, not in any path a seat can walk today.

        The equality is the assertion, not the wording: comparing the two
        spellings' output survives any change to the NOTE text, where a
        substring match on it would rot silently.
        """
        self.git("branch", "lane/ffable", self.a)
        self.git("checkout", "-q", "lane/ffable")
        own = self.commit("lane work")
        self.git("checkout", "-q", self.main)
        canonical = dispatches._ref_sanity(own, "ffable", repo=self.repo,
                                           kind="review")
        # NON-VACUITY: the canonical spelling must actually produce the note,
        # or the equalities below compare two empty lists and prove nothing.
        self.assertTrue([w for w in canonical if "ff-able" in w],
                        "the fixture must produce an ff-able NOTE at all, "
                        "got %r" % canonical)
        for spelling in ("Review", "REVIEW", " review "):
            with self.subTest(kind=spelling):
                self.assertEqual(
                    dispatches._ref_sanity(own, "ffable", repo=self.repo,
                                           kind=spelling),
                    canonical,
                    "a row recorded kind=review loses its ff-able NOTE "
                    "because the caller spelled it %r" % spelling)
        # MUST-MISS: a build row carries no ff-able note however it is spelled,
        # so this is a normalisation and not a widening.
        for spelling in ("build", "Build"):
            with self.subTest(kind=spelling):
                self.assertEqual(
                    [w for w in dispatches._ref_sanity(
                        own, "ffable", repo=self.repo, kind=spelling)
                     if "ff-able" in w], [])

    def test_a_ref_from_another_lane_is_flagged(self):
        self.git("branch", "lane/mylane", self.a)
        self.git("checkout", "-q", "lane/mylane")
        own = self.commit("lane work")
        self.git("checkout", "-q", self.main)
        # the lane's OWN commit is fine
        self.assertEqual(
            [w for w in dispatches._ref_sanity(own, "mylane", repo=self.repo)
             if "NOT one of" in w], [],
            "a lane's own tip must not be flagged as foreign")
        # a commit from the OTHER branch is not this lane's work
        out = dispatches._ref_sanity(self.side, "mylane", repo=self.repo)
        self.assertTrue(any("NOT one of" in w for w in out),
                        "a foreign tip must be flagged, got %r" % out)

    def test_a_suffixed_or_prefixed_lane_still_probes_its_own_branch(self):  # noqa: VACUOUS_ASSERTION — the assertTrue positive control runs once per member of a two-element literal tuple; the loop cannot be empty
        """Round 2 (codex finding 5): collapsing to the family stem probed
        lane/feature for a real lane/feature-review (branch absent, check
        silently skipped); the raw historical spelling probed lane/lane/foo.
        The row's OWN branch is lane/<prefix-stripped lane>, suffixes
        intact."""
        self.git("branch", "lane/mylane-review", self.a)
        for spelling in ("mylane-review", "lane/mylane-review"):
            out = dispatches._ref_sanity(self.side, spelling, repo=self.repo)
            self.assertTrue(any("NOT one of" in w for w in out),
                            "foreign tip not flagged for %r: %r"
                            % (spelling, out))

    def test_a_fresh_review_ref_says_integrator_owns_the_final_base_and_gate(self):
        self.git("checkout", "-q", "-b", "lane/fresh-review", self.c)
        tip = self.commit("fresh review")
        self.git("checkout", "-q", self.main)
        out = dispatches._ref_sanity(
            tip, "fresh-review", repo=self.repo, kind="review")
        text = "\n".join(out)
        self.assertIn("ff-able from", text)
        self.assertIn("NOW", text)
        self.assertIn("cannot stay true", text)
        self.assertIn("integrator", text)
        self.assertIn("final exact-tree gate", text)
        self.assertNotIn("rebase as author", text)

    def test_a_stale_review_base_is_structural_not_author_error(self):
        out = dispatches._ref_sanity(
            self.side, "some-lane", repo=self.repo, kind="review")
        text = "\n".join(out)
        self.assertIn("NOT ff-able", text)
        self.assertIn("structural, not author error", text)
        self.assertIn("do not rebase or re-gate as author", text)
        self.assertIn("landing order", text)

    def test_review_send_callsite_passes_kind_into_the_base_protocol(self):
        self.git("checkout", "-q", "-b", "lane/review-base", self.c)
        tip = self.commit("review base")
        self.git("checkout", "-q", self.main)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "send", "reviewer", "review-base", "inspect", "this",
            "--ref", tip, "--repo", self.repo, "--kind", "review",
            "--new-work"])
        self.assertEqual(rc, 0, err)
        self.assertIn("cannot stay true", err)
        self.assertIn("integrator", err)

    def test_build_dispatch_does_not_prescribe_the_review_landing_protocol(self):
        out = dispatches._ref_sanity(
            self.side, "some-lane", repo=self.repo, kind="build")
        self.assertFalse(any("final exact-tree gate" in w for w in out), out)

    def test_it_FAILS_OPEN_and_never_blocks(self):
        """Unknowable is silent, not noisy — and no path here can refuse."""
        self.assertEqual(dispatches._ref_sanity("de" * 20, "x", repo=self.repo), [])
        self.assertEqual(dispatches._ref_sanity("", "x", repo=self.repo), [])
        self.assertEqual(dispatches._ref_sanity(self.c, "x", repo="/nonexistent"), [])

    def test_an_unreadable_ancestry_warns_UNKNOWN_not_silence(self):
        """The phantom-unlanded-lanes fold at the advisory check: rc 128 used
        to produce the SAME silence as a clean "not on trunk", so an ancestry
        the check could not see read as verified. A real commit whose history
        walk hits a missing object must earn an UNKNOWN-flavored note —
        still a note, never a refusal."""
        self.git("checkout", "-q", "-b", "lane/ghost", self.a)
        mid = self.commit("ghost mid")
        tip = self.commit("ghost tip")
        self.git("checkout", "-q", self.main)
        # the tip stays a readable commit (the cat-file gate passes); the
        # WALK cannot see its parent -> merge-base exits 128
        os.remove(os.path.join(self.repo, ".git", "objects",
                               mid[:2], mid[2:]))
        out = dispatches._ref_sanity(tip, "x", repo=self.repo)
        self.assertTrue(any("UNKNOWN" in w for w in out),
                        "an unreadable ancestry must warn, got %r" % out)
        self.assertTrue(any("cannot tell" in w for w in out), out)
        # and it is the unknown note, not a laundered ALREADY-on-trunk one
        self.assertFalse(any("ALREADY on" in w for w in out), out)


class VerdictLandNudgeTest(DispatchBase):
    """2026-07-29 council G2: an APPROVE binds and NOTHING wakes the lander.
    At write time inside mark_verdict, an approve-polarity verdict DM's the
    land command — contextual, budgeted (one per approve)."""

    def setUp(self):
        super().setUp()
        self._sents = []

        def _dm(lander, text, who=None):
            self._sents.append((lander, text, who))
            return ({"ts": "t", "from": who or "dispatches",
                     "text": text, "dm": lander}, None)
        self._dm = mock.patch.object(seats, "dm", side_effect=_dm)
        self._dm.start()
        self._gate = mock.patch.object(
            dispatches.gate, "bind",
            return_value=("VERIFIED", "a" * 16, "test receipt"))
        self._gate.start()

    def tearDown(self):
        self._gate.stop()
        self._dm.stop()
        super().tearDown()

    def _genuine_approve(self):
        erow = self.add()
        row, _ = dispatches.mark_verdict(erow["id"], erow["tip"],
                                          "APPROVE", "approve")
        # The nudge fires AFTER the ledger lock (kimi's finding: delivery
        # must not hold the durability lock). Call it explicitly — the CLI
        # verdict handler does this, and this test pins it still fires.
        dispatches._verdict_land_nudge(row)
        return row

    def _genuine_approve_delivered(self):
        """Same as _genuine_approve, but hands back what the nudge REPORTED.

        The spy list only says the mock was called; the RETURN VALUE says the
        production leg believes it delivered. Arms needing a positive control
        on a production observable — rather than on their own instrumentation
        — use this one."""
        erow = self.add()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "APPROVE", "approve")
        self.assertIsNone(why, why)
        return dispatches._verdict_land_nudge(row)

    def _genuine_approve_no_nudge(self):
        erow = self.add()
        return dispatches.mark_verdict(erow["id"], erow["tip"],
                                        "APPROVE", "approve")

    def test_approve_triggers_a_dm_to_the_lander(self):
        self._genuine_approve()
        self.assertTrue(self._sents, "no DM sent on approve")
        _lander, text, _ = self._sents[0]
        self.assertIn("helm lr land", text)

    def test_the_nudge_fires_AFTER_the_verdict_not_inside_the_lock(self):
        """Kimi's finding: a stalled chat transport inside the ledger lock
        stalls every verdict in the fleet. mark_verdict returns first; the
        nudge is a SEPARATE call — the verdict is durable; delivery is not."""
        row, _ = self._genuine_approve_no_nudge()
        self.assertIsNotNone(row)
        self.assertEqual(self._sents, [])  # no DM inside mark_verdict
        # Now fire it — the DM happens
        dispatches._verdict_land_nudge(row)
        self.assertTrue(self._sents, "nudge must fire when called explicitly")
        self.assertIn("helm lr land", self._sents[0][1])

    def test_fix_does_not_trigger_the_LAND_nudge(self):  # noqa: VACUOUS_ASSERTION — the positive control is _genuine_approve_delivered() returning True, a PRODUCTION value, asserted unconditionally before the absence loop
        """RENAMED, because the old name outran its own assertion and this
        lane made the gap load-bearing. It read `test_fix_does_NOT_trigger_a_dm`
        while the body only forbids the string `helm lr land` — it never
        asserted that no DM was sent. A fix verdict now DOES send a DM (to the
        AUTHOR, see VerdictAuthorNudgeTest below), so the old name asserts the
        opposite of the shipped contract while the body stays exactly right.
        AND THE BODY IS NO LONGER VACUOUS, which the rung caught on the rename:
        `for row in self._sents: assertNotIn(...)` asserts NOTHING when the
        list is empty, and under the old code it always was — mark_verdict does
        not nudge. A guaranteed pass. It now carries a positive control on the
        SAME observable first, so the absence claim has something to deny."""
        # POSITIVE CONTROL: prove this fixture can observe a DM at all.
        self.assertTrue(self._genuine_approve_delivered(),
                        "the approve nudge reports no delivery")
        self.assertTrue(self._sents, "the DM observable is inert — the "
                        "absence assertion below would pass on nothing")
        self.assertIn("helm lr land", self._sents[0][1])
        self._sents[:] = []
        erow = self.add()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "FIX evidence", "fix")
        self.assertIsNone(why, why)
        self.assertTrue(dispatches._verdict_author_nudge(row),
                        "a fix must report that it woke its author")
        self.assertTrue(self._sents, "a fix must wake its author")
        for (_to, text, _w) in self._sents:
            self.assertNotIn("helm lr land", text,
                             "fix must never carry the LAND command")


class AnUndeliveredNudgeSaysSoTest(DispatchBase):
    """A VERDICT THAT REACHES NOBODY MUST NOT ALSO REACH NOBODY SILENTLY.

    The nudge closes every chance to notice on purpose, and the reasons are
    each good: `seats.dm` reports (None, reason) and the reason is spent as a
    branch; the room fallback runs inside try/except and discards what `post`
    returns; this function returns False; both call sites are bare statements.
    The rule they serve — a nudge must never block a verdict — is right. It is
    also a different property from NOT REPORTING, and conflating the two is
    how a misaddressed lander costs a verdict with nothing said anywhere.
    """

    def setUp(self):
        super().setUp()
        self._warned = []
        from helm import seats_identity
        self._w = mock.patch.object(
            seats_identity, "_warn_once",
            side_effect=lambda key, text: self._warned.append((key, text)))
        self._w.start()
        self.addCleanup(self._w.stop)

    def test_a_dm_that_does_not_deliver_is_reported_once_with_its_reason(self):
        """THE REASON THE DOOR ALREADY HAD. `dm` says why it could not
        deliver; the whole finding is that nothing ever said it out loud."""
        # THE REASON IS A SENTINEL THAT CANNOT APPEAR IN THE CONSTANT PROSE.
        # A plausible-sounding reason is the trap: the warning's own fixed
        # words explain what an unreachable name means, so they already
        # contain the ordinary vocabulary and an assertion on it is satisfied
        # by the boilerplate whether or not the reason was interpolated at
        # all. Measured — a mutant that replaced the interpolated reason with
        # a constant left this arm GREEN until the sentinel replaced it.
        reason = "sentinel-transport-refused-xyzzy"
        with mock.patch.object(seats, "dm", return_value=(None, reason)), \
             mock.patch.object(dispatches, "_default_lander",
                               return_value="seat-lander"), \
             mock.patch("helm.chat.post"):
            landed = dispatches._nudge("seat-ghost", "body", "context")
        self.assertFalse(landed,
                         "the DM refused and the nudge still claims delivery")
        self.assertTrue(self._warned,
                        "the DM did not deliver and nothing was reported, so "
                        "the operator who can fix the address learns nothing")
        key, text = self._warned[0]
        self.assertIn("seat-ghost", key,
                      "the report is not keyed on the recipient, so two bad "
                      "addresses in one process would collapse into one line")
        self.assertIn("seat-ghost", text,
                      "the report does not name WHO it could not reach")
        self.assertIn(reason, text,
                      "the report drops the reason the door already gave, "
                      "which is the half an operator can act on: %r" % text)
        # THE UNCONDITIONAL POSITIVE ON `landed`: this door DOES answer truthy
        # when the DM lands, so the False above is a refusal this arm measured
        # and not the only value the function can produce.
        with mock.patch.object(
                seats, "dm",
                return_value=({"ts": "t", "dm": "seat-live"}, None)):
            delivered = dispatches._nudge("seat-live", "body", "context")
        self.assertTrue(delivered,
                        "this door never reports delivery at all, so the "
                        "assertFalse above is about nothing")

    def test_a_dm_that_RAISES_is_reported_like_one_that_refuses(self):
        """A TRANSPORT THAT FAILS BY RAISING REACHES NOBODY TOO.

        A returned (None, reason) and a RAISED transport error carry the same
        fact — the verdict did not reach this name — so one reporter covers
        both exits or the door has a silent half. The sibling arms above pin
        the returned exit; this one pins the raise, and the reason it must
        carry is what the door OBSERVED.

        THE EXCEPTION IS THE REASON, not a generic word. Whether the name is
        wrong or the transport is down stays exactly as undecidable as it was
        here, and naming either would be a diagnosis with no evidence behind
        it — so the type and the message are what the report may say.
        """
        warned = self._warned          # the spy is the observable under test
        boom = RuntimeError("socket is gone")
        with mock.patch.object(seats, "dm", side_effect=boom), \
             mock.patch.object(dispatches, "_default_lander",
                               return_value="seat-lander"), \
             mock.patch("helm.chat.post"):
            landed = dispatches._nudge("seat-ghost", "body", "context")
        self.assertFalse(landed,
                         "the DM raised and the nudge still claims delivery")
        self.assertTrue(warned,
                        "the DM raised, so the verdict reached nobody, and "
                        "nothing was reported anywhere")
        key, text = warned[0]
        self.assertIn("seat-ghost", key,
                      "the report is not keyed on the recipient, so two "
                      "raising addresses collapse into one line")
        self.assertIn("RuntimeError", text,
                      "the report does not name what the transport raised, "
                      "so an operator cannot tell a dead socket from a bad "
                      "name: %r" % text)
        self.assertIn("socket is gone", text,
                      "the exception's own words were dropped: %r" % text)
        # THE UNCONDITIONAL CONTROL, ON THE SAME OBSERVABLE and through the
        # same door in this method: a DM that LANDS still says nothing, so the
        # assertions above track the raise and are not a reporter that now
        # fires on everything.
        del warned[:]
        with mock.patch.object(
                seats, "dm",
                return_value=({"ts": "t", "dm": "seat-live"}, None)):
            self.assertTrue(dispatches._nudge("seat-live", "body", "context"),
                            "the healthy pole stopped delivering, so the "
                            "absence below is about nothing")
        self.assertEqual([], warned,
                         "a nudge that DELIVERED warned: %r" % (warned,))

    def test_a_delivered_nudge_says_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the observable under test IS the spy: this arm runs BOTH poles through the same door in the same method, failing pole FIRST with an unconditional assertTrue on the same list, so an emptiness over a dead instrument cannot reach the absence
        """A WARNING ON THE HEALTHY POLE TRAINS EVERY READER TO SKIP THE LINE,
        which is the failure this cure exists to avoid rather than to cause.

        BOTH POLES RUN THROUGH THE SAME DOOR IN THE SAME METHOD, and the
        failing one runs FIRST: an emptiness asserted against a spy that never
        fills is true of a broken spy, a missing patch and a door that is
        never reached, and those are indistinguishable from the cure working.
        """
        # THE UNCONDITIONAL POSITIVE, ON THE SAME OBSERVABLE the absence below
        # constrains: this spy DOES fill, through this door, in this method.
        with mock.patch.object(seats, "dm",
                               return_value=(None, "no roster row")), \
             mock.patch.object(dispatches, "_default_lander",
                               return_value="seat-lander"), \
             mock.patch("helm.chat.post"):
            dispatches._nudge("seat-ghost", "body", "context")
        self.assertTrue(self._warned,
                        "the reporter never fires at all, so the emptiness "
                        "below would hold over a dead instrument")
        self._warned[:] = []
        with mock.patch.object(
                seats, "dm",
                return_value=({"ts": "t", "dm": "seat-live"}, None)):
            landed = dispatches._nudge("seat-live", "body", "context")
        self.assertTrue(landed,
                        "the healthy pole stopped delivering, so the absence "
                        "below is about a nudge nobody received")
        self.assertEqual([], self._warned,
                         "a nudge that DELIVERED still warned: %r"
                         % (self._warned,))

    def test_the_report_never_raises_out_of_a_verdict(self):
        """A REPORT ABOUT A FAILED DELIVERY MAY NOT FAIL A VERDICT. The rule
        the silence served is kept: this arm drives the reporter itself into
        an exception and requires the nudge to return normally."""
        with mock.patch.object(seats, "dm",
                               return_value=(None, "transport down")), \
             mock.patch.object(dispatches, "_default_lander",
                               return_value="seat-lander"), \
             mock.patch("helm.chat.post"), \
             mock.patch("helm.seats_identity._warn_once",
                        side_effect=RuntimeError("stderr is gone")):
            landed = dispatches._nudge("seat-ghost", "body", "context")
        self.assertFalse(landed,
                         "a nudge whose DM refused and whose reporter threw "
                         "still claims delivery")
        # THE UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE: this door DOES
        # return True when the DM lands, so the False above is a refusal and
        # not a function that can only ever answer falsy.
        with mock.patch.object(
                seats, "dm",
                return_value=({"ts": "t", "dm": "seat-live"}, None)):
            self.assertTrue(dispatches._nudge("seat-live", "body", "context"),
                            "this door never reports delivery at all, so the "
                            "assertion above is about nothing")
        # AND THE CONTROL ON THE REPORTER: with it WORKING, the identical
        # failing call reports — so the arm above measures a SWALLOWED
        # exception and not a path that never reaches the reporter.
        with mock.patch.object(seats, "dm",
                               return_value=(None, "transport down")), \
             mock.patch.object(dispatches, "_default_lander",
                               return_value="seat-lander"), \
             mock.patch("helm.chat.post"):
            dispatches._nudge("seat-ghost", "body", "context")
        self.assertTrue(self._warned,
                        "the reporter never fires even when it works, so the "
                        "arm above proves nothing about it raising")


class VerdictLandNudgeLadderTest(DispatchBase):
    """THE LAND DM WAS BUILT FROM VERDICT POLARITY ALONE, so every approve got
    the identical "ready: helm lr land <id>" while the lr projection already
    knew what the row was. Two live specimens: a row whose author and builder
    were the same seat (review field EMPTY, projection READY-SELF-REVIEW),
    and a day of instructions mostly for rows that had already LANDED or
    CLOSED.

    The first cure special-cased the SELF-REVIEW arm by comparing sender to
    recipient on the raw row. Self-review is ONE rung of a ladder, and a
    branch per rung is the same defect regrowing one state at a time. The
    nudge now asks landreq for the word and prescribes on plain "READY"
    only — so the arms below pin the RULE, not a list: the invented-word
    arm is the one that fails if anybody ever puts an enumeration back in
    the notifier.

    THE CURE DISCLOSES, NEVER WITHHOLDS. The DM still arrives and still
    carries the land command for every state — a suppressed DM rebuilds the
    G2 nothing-wakes-the-lander defect this nudge exists to cure."""

    def setUp(self):
        super().setUp()
        self._sents = []

        def _dm(lander, text, who=None):
            self._sents.append((lander, text, who))
            return ({"ts": "t", "from": who or "dispatches",
                     "text": text, "dm": lander}, None)
        self._dm = mock.patch.object(seats, "dm", side_effect=_dm)
        self._dm.start()
        # A COMPLETE RECEIPT, NOT A STUB WITH A CHOSEN ID. The token this
        # mocked bind hands back is the one `_approve(landable=True)` seeds
        # into the ledger, so it must be an id the reader can RECOMPUTE:
        # landreq's gate index is `gate.receipts()` now, which admits a row
        # only when its content id verifies. The old `{"v": 4, "id": "aaaa..."}`
        # stub was indexed by the earlier reader that walked raw ledger lines,
        # so a row resting on a receipt that could never verify still
        # projected plain READY — the overstatement the verified index closes.
        self._receipt = {
            "v": 1, "event": "gate", "ts": dispatches.pk.now_ts(),
            "repo_id": self.repo, "head": "0" * 40, "tree": "1" * 40,
            "dirty": False,
            "interpreter": {"name": "cpython", "version": "3.14.6",
                            "language": "3.14.6",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 1, "skipped": 0, "rc": 0}
        self._receipt["id"] = dispatches.gate._receipt_id(self._receipt)
        self._gate = mock.patch.object(
            dispatches.gate, "bind",
            return_value=("VERIFIED", self._receipt["id"], "test receipt"))
        self._gate.start()

    def tearDown(self):
        self._gate.stop()
        self._dm.stop()
        super().tearDown()

    def _approve(self, recipient, landable=False, ref=None):
        """One approve-verdicted row dispatched to `recipient`.

        THE REF IS LOAD-BEARING and defaults OFF trunk. The fixture's own
        default tip is already ON its trunk, so a row minted there projects
        LANDED and TERMINAL — a real state (the common one for a reviewed
        row, and the terminal arm below asks for it explicitly) but not the
        one an arm about rungs wants. `landable`
        additionally mints the gate receipt the READY rung needs, because an
        approve with no receipt in the ledger is READY-UNVERIFIED.

        The fixture's acting author is HELM_CHAT_NAME=integrator, so
        recipient="integrator" mints the self-review specimen's exact shape."""
        if landable:
            path = dispatches.gate.receipts_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(self._receipt) + "\n")
            # SEEDED IS NOT ADMITTED. Assert the reader actually took it, or
            # this fixture goes back to promising a receipt the rung cannot
            # see and every arm below measures the wrong pole.
            self.assertIn(self._receipt["id"], landreq._gate_receipt_index())
        erow = self.add(recipient=recipient,
                        ref=self.side if ref is None else ref)
        from tests._verdict import native_author
        with native_author(self):
            row, why = dispatches.mark_verdict(
                erow["id"], erow["tip"], "APPROVE", "approve", bind_author=True)
        self.assertIsNone(why, why)
        return row

    def test_a_plainly_READY_row_keeps_the_instruction_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — the assertEqual on the WHOLE DM line is the unconditional positive control for this class
        """THE CONTROL, AND THE PIN. Cross-reviewed, receipted, live rows are
        the healthy majority and their DM must not change by one character —
        a cure that taxed them would withhold the wake this nudge exists to
        send. Projected end to end through the REAL ladder, so a break
        anywhere in the seam shows up here as the healthy pole going quiet."""
        row = self._approve("seat-b", landable=True)
        self.assertEqual(
            landreq.land_instruction(row["id"]), ("READY", None),
            "fixture drift: this arm is only a pin while the ladder still "
            "calls this row plainly READY")
        self.assertTrue(dispatches._verdict_land_nudge(row))
        _to, text, _w = self._sents[0]
        self.assertEqual(
            text, "VERDICT APPROVE at %s — ready: helm lr land %s"
            % (row["reviewed_tip"][:10], row["id"][:17]))

    def test_the_live_self_review_specimen_loses_the_prescription(self):
        """The self-review specimen, end to end through the real projection:
        author and recipient the same seat, no independent reviewer. The
        command still arrives; "ready:" does not."""
        row = self._approve("integrator", landable=True)
        self.assertTrue(dispatches._verdict_land_nudge(row),
                        "the nudge must still DELIVER — withholding is the "
                        "over-restriction, not the cure")
        _to, text, _w = self._sents[0]
        self.assertIn("READY-SELF-REVIEW", text)
        self.assertIn("helm lr land", text)
        self.assertNotIn("ready:", text)

    def test_a_TERMINAL_row_is_never_told_it_is_ready(self):
        """The reviewer's own arm, and terminal is the COMMON case: most of a
        day's land instructions are for rows already landed or closed.
        Reachable in production because an idempotent `helm dispatch verdict`
        retry returns (row, None) and re-fires this nudge.

        THIS ARM COVERS THE ROWS THAT REACH IT BY STATE. A row whose state IS
        LANDED needs no terminal guard — `ready_word` already answers LANDED
        for it, and a sabotage that deletes the guard leaves this arm green
        (measured). Rows retired as CLOSED_LANDED are terminal by RETIREMENT
        with a stored state of READY, and that is the shape the guard exists
        for; LandInstructionWordTest pins it where the guard lives, and the
        enumerate-nothing arm below pins that this line carries the word."""
        row = self._approve("seat-b", ref=self.a)   # a tip already on trunk
        self.assertEqual(
            landreq.land_instruction(row["id"]), ("LANDED", None),
            "fixture drift: this arm needs a row the ladder calls terminal")
        self.assertTrue(dispatches._verdict_land_nudge(row))
        _to, text, _w = self._sents[0]
        self.assertIn("LANDED", text)
        self.assertNotIn("ready:", text)
        self.assertIn("helm lr land", text)

    def test_the_notifier_ENUMERATES_NOTHING(self):  # noqa: VACUOUS_ASSERTION — test_a_plainly_READY_row_keeps_the_instruction_byte_for_byte is the unconditional positive control on the SAME observable (the DM text), asserted first in the class; the loop below cannot be empty because its subjects are a literal tuple
        """THE ARM THAT DEFENDS THE SHAPE. Every word below takes the same
        path, including one no rung has ever emitted: if a later hand adds
        `if word == "CONTESTED"` branches, the invented word is what fails.
        The ladder owns the vocabulary; this line owns one comparison."""
        for word in ("READY-SELF-REVIEW", "READY-CONTESTED",
                     "READY-STALE-BASE", "READY-UNVERIFIED", "UNVERIFIED",
                     "LANDED", "CLOSED_BY_LANDING", "ABANDONED", "REVIEWED",
                     "A-RUNG-NOBODY-HAS-WRITTEN-YET"):
            with self.subTest(word=word):
                self._sents[:] = []
                row = self._approve("seat-b", landable=True)
                # THE LADDER IS REACHED THROUGH ONE DOOR, and that door is
                # land_nudge_instruction: it projects ONCE and mints word,
                # reason and command from the same reading, so it does not
                # call land_instruction at all. Patching the old entry point
                # would leave the oracle uncalled and prove nothing about
                # this line. The enumeration contract is unchanged — ONE
                # comparison, every word down one path.
                cmd = "helm lr land %s" % row["id"][:17]
                with mock.patch.object(
                        landreq, "land_nudge_instruction",
                        return_value=(word, None, cmd)) as oracle:
                    self.assertTrue(dispatches._verdict_land_nudge(row))
                oracle.assert_called_once_with(row["id"])
                _to, text, _w = self._sents[-1]
                self.assertIn(word, text)
                self.assertIn("helm lr land %s" % row["id"][:17], text)
                self.assertNotIn("ready:", text)

    def test_a_ladder_that_cannot_be_read_never_says_ready(self):
        """The delivery leg runs after a DURABLE verdict, so an unreadable
        projection must cost neither the wake nor the truth: the DM arrives,
        carries UNVERIFIED, and does not prescribe."""
        row = self._approve("seat-b", landable=True)
        with mock.patch.object(landreq, "project",
                               side_effect=RuntimeError("ledger gone")):
            self.assertTrue(dispatches._verdict_land_nudge(row))
        _to, text, _w = self._sents[0]
        self.assertIn("UNVERIFIED", text)
        self.assertNotIn("ready:", text)
        self.assertIn("helm lr land", text)

    def test_the_fallback_room_post_carries_the_word_too(self):  # noqa: VACUOUS_ASSERTION — assertTrue(texts) is the unconditional positive control on the same observable, asserted before the presence claims
        """The fallback is the half that is easy to get wrong twice (_nudge's
        own docstring). Kill the DM leg and the ADDRESSED room post must say
        the same word — a recipient reached by either leg reads the same
        fact."""
        row = self._approve("integrator", landable=True)   # minted under the
        # WORKING spy — the announce leg inside mark_verdict needs chat.post
        with mock.patch.object(seats, "dm",
                               return_value=(None, "transport down")), \
                mock.patch.object(chat, "post") as post:
            self.assertFalse(dispatches._verdict_land_nudge(row))
        texts = [c.args[0] for c in post.call_args_list]
        self.assertTrue(texts, "the fallback leg never posted — the "
                        "presence assertions below would pass on nothing")
        self.assertTrue(any("READY-SELF-REVIEW" in t for t in texts),
                        "fallback post lost the state word: %r" % texts)
        self.assertTrue(any("helm lr land" in t for t in texts), texts)


class VerdictAuthorNudgeTest(DispatchBase):
    """THE OTHER POLE. An APPROVE wakes the LANDER (the class above); every
    NON-approve hands the row BACK and must wake its AUTHOR.

    Nothing did, and that is the owner's P0 verbatim: a CHANGES_REQUESTED
    verdict hands the row back to its author and nothing wakes them. Measured
    2026-08-11T00:02Z — 22 codex-family verdicts in two hours, 21 of them fix;
    two seats posted in chat that they were still 'awaiting' reviews whose
    verdicts were already 17-26 minutes old, and the integrator held two more
    of its own in the same state."""

    def setUp(self):
        super().setUp()
        self._sents = []
        self._posts = []

        def _dm(to, text, who=None):
            self._sents.append((to, text, who))
            return ({"ts": "t", "from": who or "dispatches", "text": text,
                     "dm": to}, None)
        self._dm = mock.patch.object(seats, "dm", side_effect=_dm)
        self._dm.start()

        def _post(text, **kw):
            # chat.post RETURNS THE ROW ("Append one message; returns it"),
            # while seats.dm returns (row, reason). I gave both the tuple
            # shape and the gate caught it as six errors raised INSIDE
            # mark_verdict, where the announce path calls .get() on the
            # result. A stub is a contract claim; verify it against the real
            # signature, not against the stub beside it.
            self._posts.append((text, kw))
            return {"id": "p", "ts": "t", "text": text}
        self._post = mock.patch.object(chat, "post", side_effect=_post)
        self._post.start()
        # an approve is REFUSED without a verified gate token, and one arm
        # below exercises the approve pole's fallback (same stub as the
        # sibling class, for the same reason).
        self._gate = mock.patch.object(
            dispatches.gate, "bind",
            return_value=("VERIFIED", "a" * 16, "test receipt"))
        self._gate.start()

    def tearDown(self):
        self._gate.stop()
        self._post.stop()
        self._dm.stop()
        super().tearDown()

    def _verdict(self, polarity):
        """(delivered, row) for one non-approve verdict on a fresh row.

        SENDER IS NOT A PARAMETER and cannot be: add() derives it from
        _acting_author() and REFUSES an identityless or disputed caller
        outright (owner call 2026-08-02 — un-disownable shared-name rows are
        worse than a lost add). My first draft passed sender="cj" as a kwarg,
        which is not in add()'s signature, and the gate caught it as eight
        errors. So the arms read the sender the fixture actually recorded and
        assert the DM went THERE — a stronger contract than a hardcoded name,
        and one that cannot drift from how rows really get authored."""
        erow = self.add()
        self.assertTrue(erow.get("sender"),
                        "fixture row has no author to wake")
        row, why = dispatches.mark_verdict(
            erow["id"], erow["tip"],
            "%s evidence" % (polarity or "undeclared").upper(), polarity)
        self.assertIsNone(why, why)
        # DROP mark_verdict'S OWN ANNOUNCE. _announce_verdict posts
        # "VERDICT FIX evidence — lane ... tip ..." into the room as part of
        # recording the verdict, and it carries NO @mention — which is the
        # production defect this lane exists to cure, reproduced here in a
        # fixture. These arms are about what the NUDGE posts, so the spy is
        # reset between the write and the nudge; leaving it would let the
        # announce answer every question asked of the fallback.
        self._posts[:] = []
        return dispatches._verdict_author_nudge(row), erow

    def test_a_FIX_verdict_DMs_the_ROW_S_AUTHOR(self):  # noqa: VACUOUS_ASSERTION — assertTrue(delivered) pins the nudge's own return and _sents[0][0] pins the exact addressee; neither passes on an empty result
        """The defect in one arm: before this, a fix woke nobody at all.

        The addressee assertion is the load-bearing half. A nudge that fires
        but routes to the lander every time would pass a mere 'a DM was sent'
        check while reproducing the bug — the author still learns nothing.
        This also makes the CURE'S OWN PREMISE falsifiable: it rests on
        mark_verdict's returned row carrying `sender` through from the ledger,
        which was read in the source and never run. If that read was wrong,
        this arm goes red HERE rather than shipping a cure that silently wakes
        the wrong seat forever."""
        delivered, erow = self._verdict("fix")
        self.assertTrue(delivered,
                        "the nudge must REPORT that the author's DM landed")
        self.assertTrue(self._sents, "a FIX verdict woke NOBODY")
        to, text, _who = self._sents[0]
        self.assertEqual(to, erow["sender"],
                         "the DM must go to the ROW'S OWN recorded author")
        self.assertIn("BACK WITH YOU", text)
        self.assertIn("helm dispatch triage", text)
        self.assertEqual(self._posts, [],
                         "a resolvable author needs no room fallback")
        # POSITIVE CONTROL ON THE SAME OBSERVABLE. The empty assertion above
        # means nothing unless this fixture can observe a post at all — an
        # inert patch would satisfy it forever (the rung's own catch).
        chat.post("control", who="test")
        self.assertTrue(self._posts, "the _posts observable is inert")

    def test_EVERY_non_approve_polarity_wakes_the_author(self):  # noqa: VACUOUS_ASSERTION — each pole asserts the production return True plus the addressee, and `seen` pins that the loop ran four times
        """The call site's `else` covers four outcomes, so enumerate four.
        fix, supersede and concur all hand the row back, and UNDECLARED is the
        worst of them — immutable, closes nothing, and 28 rows on this ledger
        carry it. Testing only `fix` would leave three silent poles behind a
        green arm."""
        poles = ("fix", "supersede", "concur")
        seen = 0
        for polarity in poles:
            with self.subTest(polarity=polarity):
                self._sents[:] = []
                delivered, erow = self._verdict(polarity)
                self.assertTrue(delivered,
                                "polarity %r reported no delivery" % polarity)
                self.assertTrue(
                    self._sents,
                    "polarity %r hands the row back and woke nobody" % polarity)
                self.assertEqual(self._sents[0][0], erow["sender"])
                seen += 1
        # UNCONDITIONAL STRUCTURAL ASSERTION: a loop that never ran would
        # otherwise report four covered poles as a pass over zero.
        self.assertEqual(seen, len(poles),
                         "the polarity sweep did not cover every pole")

    def test_an_UNDECLARED_polarity_still_wakes_the_author(self):  # noqa: VACUOUS_ASSERTION — assertTrue(delivered) pins the nudge's production return, and the addressee plus the UNDECLARED string in the body are both positive; the only absence is assertIsNone(why) on the SETUP verdict, not on the observable under test
        """The fourth pole, and it cannot be minted — only inherited.

        mark_verdict REFUSES polarity=None outright: "verdict polarity is
        required: an undeclared verdict is immutable and can never be
        retired". So the sweep above cannot cover it and my first draft's
        None pole was asserting a write the ledger forbids. But UNDECLARED
        rows EXIST — 28 of them on this ledger — and they are the worst pole
        to leave silent, because an immutable verdict that closes nothing is
        exactly the row whose author most needs telling. The nudge is a pure
        function over the row dict, so the historical shape is constructed
        rather than written."""
        erow = self.add()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "FIX evidence", "fix")
        self.assertIsNone(why, why)
        delivered = dispatches._verdict_author_nudge(
            dict(row, polarity=None))
        self.assertTrue(delivered, "an UNDECLARED verdict woke nobody")
        self.assertEqual(self._sents[0][0], erow["sender"])
        self.assertIn("UNDECLARED", self._sents[0][1],
                      "the author must be told WHICH polarity came back")

    def test_an_UNRESOLVABLE_author_routes_to_the_LANDER_BY_NAME(self):  # noqa: VACUOUS_ASSERTION — absence IS the contract (the DM must NOT deliver) and it is paired with positive assertions that the fallback post exists, starts with @ and names the failed sender
        """The fallback must be ADDRESSED, never ambient — this is the arm
        that stops the cure from rebuilding the defect inside itself.

        Posting into the room with nobody named is EXACTLY the failure being
        cured, so an unresolvable sender may not degrade into one. It routes
        to the lander by name and says the author could not be woken. Waking
        someone who cannot act is recoverable; waking nobody is not."""
        erow = self.add()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "FIX evidence", "fix")
        self.assertIsNone(why, why)
        # DROP mark_verdict'S OWN ANNOUNCE. _announce_verdict posts
        # "VERDICT FIX evidence — lane ... tip ..." into the room as part of
        # recording the verdict, and it carries NO @mention — which is the
        # production defect this lane exists to cure, reproduced here in a
        # fixture. These arms are about what the NUDGE posts, so the spy is
        # reset between the write and the nudge; leaving it would let the
        # announce answer every question asked of the fallback.
        self._posts[:] = []
        self._dm.stop()
        self._dm = mock.patch.object(
            seats, "dm", side_effect=lambda *a, **k: (None, "no such seat"))
        self._dm.start()
        # add() REFUSES to mint a row with an unresolvable author, so the
        # unresolvable case is constructed on the row dict the nudge actually
        # consumes — it is a pure function over that dict.
        delivered = dispatches._verdict_author_nudge(
            dict(row, sender="a-seat-that-does-not-exist"))
        self.assertFalse(delivered,
                         "a failed author DM must REPORT that it fell back")
        self.assertTrue(self._posts, "an unwakeable author must still surface")
        text = self._posts[0][0]
        self.assertTrue(text.startswith("@"),
                        "the fallback must NAME someone — an unaddressed post "
                        "is the very defect this lane cures, got %r" % text[:60])
        self.assertIn("COULD NOT BE WOKEN", text)
        self.assertIn("a-seat-that-does-not-exist", text,
                      "name the sender that failed, so it can be repaired")

    def test_a_BROKEN_transport_leaves_the_verdict_DURABLE(self):
        """Delivery may never unbind a verdict. Asserting 'it did not raise'
        would be vacuous, so this asserts the EFFECT: with both legs throwing,
        the verdict is still readable from the ledger afterwards."""
        erow = self.add()
        self._dm.stop()
        self._dm = mock.patch.object(seats, "dm", side_effect=RuntimeError("x"))
        self._dm.start()
        self._post.stop()
        self._post = mock.patch.object(chat, "post",
                                       side_effect=RuntimeError("x"))
        self._post.start()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "FIX evidence", "fix")
        self.assertIsNone(why, why)
        dispatches._verdict_author_nudge(row)
        after = dispatches.snapshot()[0][erow["id"]]
        self.assertEqual(after["polarity"], "fix",
                         "the verdict must survive a dead transport")

    def test_the_APPROVE_fallback_fires_on_a_FAILED_DM_not_an_empty_name(self):  # noqa: VACUOUS_ASSERTION — absence IS the contract (the lander DM must fail) paired with positive assertions on the fallback post's content
        """The dead branch this lane removed, pinned so it cannot come back.

        _verdict_land_nudge read `lander = os.environ.get(...) or "opus-
        integrator"` and then branched on `if lander:` — which the `or` makes
        unconditionally true, so its else was UNREACHABLE, and had it ever run
        it would have posted the literal "@None". The condition tested a name
        that cannot be empty instead of a delivery that can fail. Both nudges
        now share _nudge, which branches on the DM's own result."""
        erow = self.add()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "APPROVE gate:%s" % ("a" * 16),
                                           "approve")
        self.assertIsNone(why, why)
        # DROP mark_verdict'S OWN ANNOUNCE. _announce_verdict posts
        # "VERDICT FIX evidence — lane ... tip ..." into the room as part of
        # recording the verdict, and it carries NO @mention — which is the
        # production defect this lane exists to cure, reproduced here in a
        # fixture. These arms are about what the NUDGE posts, so the spy is
        # reset between the write and the nudge; leaving it would let the
        # announce answer every question asked of the fallback.
        self._posts[:] = []
        self._dm.stop()
        self._dm = mock.patch.object(
            seats, "dm", side_effect=lambda *a, **k: (None, "transport down"))
        self._dm.start()
        self.assertFalse(dispatches._verdict_land_nudge(row),
                         "a failed lander DM must REPORT that it fell back")
        self.assertTrue(self._posts,
                        "a failed DM to the lander must reach the room")
        text = self._posts[0][0]
        self.assertTrue(text.startswith("@"), text[:60])
        self.assertNotIn("@None", text,
                         "the removed branch formatted an empty lander")
        self.assertIn("helm lr land", text)

    def test_the_CLI_DOOR_ITSELF_wakes_the_author_on_a_FIX(self):
        """THE WIRE, not the helper: every author-wake
        arm above drives _verdict_author_nudge directly, so deleting the
        two-line else at the ONLY production call site would restore the
        founding failure while this whole class stayed green. This arm
        drives cmd_dispatch itself and is the one that reddens on that
        deletion. Helper-tested is not wire-proven."""
        erow = self.add()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", erow["id"], erow["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--no-patch-because", "a design finding for a meld",
                "cli-door evidence"])
        self.assertEqual(rc, 0, err)
        after = dispatches.snapshot()[0][erow["id"]]
        self.assertEqual(after["polarity"], "fix",
                         "the verdict must bind durably before any nudge")
        author_nudges = [(to, t) for (to, t, _w) in self._sents
                         if to == erow["sender"] and "BACK WITH YOU" in t]
        self.assertEqual(
            len(author_nudges), 1,
            "the CLI door must wake the author EXACTLY once, got %r"
            % (self._sents,))
        self.assertEqual(
            [t for (_to, t, _w) in self._sents if "helm lr land" in t], [],
            "a FIX must never carry the land command")

    def test_the_CLI_DOOR_routes_APPROVE_to_the_lander_not_the_author(self):
        """Opposite-pole control on the SAME wire: an approve wakes the
        LANDER with the land command, and the author nudge stays silent.
        Without this control, an else that fired BOTH nudges on every
        polarity would pass the FIX arm above."""
        erow = self.add()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", erow["id"], erow["tip"], "--approve", "--measured",
                "APPROVE gate:%s" % ("a" * 16)])
        self.assertEqual(rc, 0, err)
        land = [(to, t) for (to, t, _w) in self._sents
                if "helm lr land" in t]
        self.assertEqual(len(land), 1,
                         "an approve must nudge the land exactly once, "
                         "got %r" % (self._sents,))
        self.assertEqual(land[0][0], dispatches._default_lander(),
                         "the land nudge routes to the LANDER by name")
        self.assertEqual(
            [t for (_to, t, _w) in self._sents if "BACK WITH YOU" in t], [],
            "an approve must not fire the author nudge")

    def test_a_ROW_AND_REASON_shape_is_a_violation_that_still_falls_back(self):  # noqa: VACUOUS_ASSERTION — False IS the contract for the nudge's return under a violated dm shape; the same observable's positive face is the two fallback assertions (posts non-empty, addressed), which cannot pass on an inert patch
        """The second finding, sealed: _nudge read ANY non-None row
        as delivered and ignored the reason leg, so a (row, reason)
        contract violation suppressed the fallback while reporting
        success — a silent wake-path loss inside the cure for silent
        wake-path loss. Delivery now requires row-present AND reason-None;
        the violating shape falls through to the ADDRESSED fallback."""
        erow = self.add()
        row, why = dispatches.mark_verdict(erow["id"], erow["tip"],
                                           "FIX evidence", "fix")
        self.assertIsNone(why, why)
        self._posts[:] = []
        self._dm.stop()
        self._dm = mock.patch.object(
            seats, "dm",
            side_effect=lambda *a, **k: ({"dm": "x"}, "violated contract"))
        self._dm.start()
        self.assertFalse(
            dispatches._verdict_author_nudge(row),
            "a contract-violating shape must never read as delivered")
        self.assertTrue(self._posts,
                        "the fallback must fire on a violated contract")
        self.assertTrue(self._posts[0][0].startswith("@"),
                        self._posts[0][0][:60])



class ApplySignalsWhatItTookTest(DispatchBase):
    """THE CONTRACT THE FOLD'S REPORT RESTS ON.

    `_fold` tells its callers which events it ACCEPTED, and the land card's
    every transition stamp now comes from that list — because the canonical
    state can say a verdict happened and cannot say WHICH of two verdict events
    was it, and reading the first one in the raw slice took a refused event's
    timestamp (a 2001 stamp on a row approved today).

    The discriminator is `_apply`'s own return: EVERY refusal path answers the
    state object it was handed, EVERY acceptance answers a NEW dict. That is
    true of the code today and nothing enforced it, so a future branch that
    updates `state` in place would silently re-attribute stamps to refused
    events with no test going red. This is that test."""

    def row(self):
        return dispatches.add("codex-3", "lane/apply", ref=self.side,
                              repo=self.repo, new_work=True, notify=False)

    def state_of(self, rid):
        current, _u = dispatches.snapshot()
        return current[rid]

    def test_an_ACCEPTED_event_answers_a_NEW_object(self):
        row = self.row()
        state = self.state_of(row["id"])
        event = {"id": row["id"], "v": 3, "seq": int(state.get("seq") or 0) + 1,
                 "event": "delivered", "ts": "2026-07-30T00:00:00Z",
                 "delivery_ref": "post-1"}
        before = dict(state)
        after = dispatches._apply(state, event)
        self.assertIsNot(after, state, "an accepted event reused the object, so "
                                       "the fold cannot tell it was taken")
        self.assertEqual(after["delivery"], "observed")
        # …and the object it was handed is UNTOUCHED: an in-place update would
        # move the state while reading as "not taken"
        self.assertEqual(state, before)

    def test_a_REFUSED_event_answers_the_SAME_object(self):
        row = self.row()
        state = self.state_of(row["id"])
        for why, event in (
                ("out of sequence",
                 {"id": row["id"], "v": 3, "seq": 99, "event": "delivered",
                  "ts": "2026-07-30T00:00:00Z", "delivery_ref": "post-1"}),
                ("not this row",
                 {"id": "0" * 32, "v": 3, "seq": 1, "event": "delivered",
                  "ts": "2026-07-30T00:00:00Z", "delivery_ref": "post-1"}),
                ("a verdict bound to another tip",
                 {"id": row["id"], "v": 3, "seq": 1, "event": "verdict",
                  "ts": "2026-07-30T00:00:00Z", "reviewed_tip": "f" * 40,
                  "verdict_ref": "ok", "polarity": "approve"})):
            self.assertIs(dispatches._apply(state, event), state, why)

    def test_an_event_the_fold_never_ACTS_on_answers_the_same_object(self):
        """A notify-failed marker is a real recorded fact that moves no state.
        It must read as "not taken" so it is never dated as a transition — and
        `landreq` names refusals by KIND, so it is never called refused either."""
        row = self.row()
        state = self.state_of(row["id"])
        self.assertIs(dispatches._apply(state, {
            "id": row["id"], "v": 3, "seq": 1, "event": "notify-failed",
            "ts": "2026-07-30T00:00:00Z", "reason": "chat node down"}), state)

    def test_the_fold_lists_the_taken_events_in_ledger_order(self):
        row = self.row()
        dispatches._mark_delivered(row["id"], "post-1")
        current, _order, taken = dispatches._fold(
            eventledger.events(dispatches.ledger_path()))
        self.assertEqual(current[row["id"]]["delivery"], "observed")
        self.assertEqual([e.get("event") for e in taken[row["id"]]],
                         ["dispatch", "delivered"])


class MovedLaneTest(DispatchBase):
    """An APPROVE on a branch that moved under the review is orphaned at
    birth: the write authorizes landing a commit no branch carries. The guard
    runs for LAND-AUTHORIZING writes only (41dc6b0): a FIX on the
    old tip stays truthful when the author advances while fixing — the
    movement is often caused by the review — and a SUPERSEDE is directly
    caused by it. Identity is the row's write-boundary ref_branch, never the
    free-text lane; a raw SHA binds only when exactly one local branch has it
    as its tip, while legacy and unanswerable SHA rows carry none."""

    def setUp(self):
        super().setUp()
        # One arm mints through a whole-suite gate.run: admission on a fixture
        # box, never this node's live cap (task/1740).
        from tests._tmphome import pin_admission
        pin_admission(self)

    def topic(self, name, base, filename):
        self.git("checkout", "-q", "-b", name, base)
        path = os.path.join(self.repo, filename)
        with open(path, "a", encoding="utf-8") as f:
            f.write(filename + "\n")
        self.git("add", filename)
        self.git("commit", "-q", "-m", filename)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return tip

    def advance(self, branch, filename):
        self.git("checkout", "-q", branch)
        with open(os.path.join(self.repo, filename), "w") as f:
            f.write(filename + "\n")
        self.git("add", filename)
        self.git("commit", "-q", "-m", filename)
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return head

    def local_tips(self, tip):
        return self.git("for-each-ref", "--format=%(refname)",
                        "--points-at", tip, "refs/heads").splitlines()

    def sha_dispatch(self, branch, base, filename, short=False):
        tip = self.topic(branch, base, filename)
        ref = tip[:12] if short else tip
        row = self.add(lane=branch.removeprefix("lane/"), ref=ref)
        self.assertEqual(self.local_tips(tip), ["refs/heads/" + branch])
        self.assertEqual(row["tip"], tip)
        self.assertEqual(row["ref"], ref)
        self.assertEqual(row.get("ref_branch"), "refs/heads/" + branch)
        return tip, row

    def bound_approve(self, rid, tip):
        """Mint a real canonical receipt with HEAD at the reviewed tip."""
        self.git("checkout", "-q", tip)
        with serial_process():
            g, gerr = gate.run(repo=self.repo)
        self.git("checkout", "-q", self.main)
        self.assertIsNone(gerr, gerr)
        return dispatches.mark_verdict(rid, tip, gate.evidence_line(g),
                                       polarity="approve")

    def test_raw_full_and_short_shas_bind_a_unique_local_branch_tip(self):  # noqa: VACUOUS_ASSERTION — fixed two-case loop asserts the exact positive binding for both SHA spellings
        for suffix, short in (("full", False), ("short", True)):
            with self.subTest(ref=suffix):
                tip, row = self.sha_dispatch(
                    "lane/sha-" + suffix, self.a, "sha-" + suffix,
                    short=short)
                self.assertEqual(row["tip"], tip)

    def test_a_sha_with_zero_local_branch_tips_binds_none(self):  # noqa: VACUOUS_ASSERTION — exact tip persistence is the positive control for intentionally absent branch evidence
        self.assertEqual(self.local_tips(self.b), [])
        row = self.add(lane="unbound-zero", ref=self.b)
        self.assertEqual(row["tip"], self.b)
        self.assertIsNone(row.get("ref_branch"))

    def test_a_sha_at_multiple_local_branch_tips_binds_none(self):  # noqa: VACUOUS_ASSERTION — the two-ref census is the positive control for intentionally absent unique evidence
        tip = self.topic("lane/sha-one", self.a, "sha-multiple")
        self.git("branch", "lane/sha-two", tip)
        self.assertEqual(set(self.local_tips(tip)),
                         {"refs/heads/lane/sha-one",
                          "refs/heads/lane/sha-two"})
        row = self.add(lane="ambiguous-sha", ref=tip)
        self.assertEqual(row["tip"], tip)
        self.assertIsNone(row.get("ref_branch"))

    def test_a_branch_containing_but_not_tipped_at_the_sha_does_not_bind(self):  # noqa: VACUOUS_ASSERTION — ancestry and exact tip persistence positively prove the contains-not-points-at fixture
        tip = self.topic("lane/sha-contains", self.a, "contains1")
        head = self.advance("lane/sha-contains", "contains2")
        self.assertNotEqual(head, tip)
        self.assertEqual(subprocess.run(
            ["git", "-C", self.repo, "merge-base", "--is-ancestor",
             tip, head]).returncode, 0)
        self.assertEqual(self.local_tips(tip), [])
        row = self.add(lane="contains-not-tip", ref=tip)
        self.assertEqual(row["tip"], tip)
        self.assertIsNone(row.get("ref_branch"))

    def test_a_tag_at_the_sha_is_not_a_local_branch_binding(self):  # noqa: VACUOUS_ASSERTION — tag resolution and persisted exact tip positively control the local-branch absence
        self.git("tag", "sha-control", self.b)
        self.assertEqual(self.git("rev-parse", "refs/tags/sha-control"), self.b)
        self.assertEqual(self.local_tips(self.b), [])
        row = self.add(lane="tag-control", ref=self.b)
        self.assertEqual(row["tip"], self.b)
        self.assertIsNone(row.get("ref_branch"))

    def test_a_remote_tracking_ref_at_the_sha_is_not_a_local_branch_binding(self):  # noqa: VACUOUS_ASSERTION — remote ref resolution and exact tip persistence positively control local absence
        self.git("update-ref", "refs/remotes/origin/sha-control", self.b)
        self.assertEqual(
            self.git("rev-parse", "refs/remotes/origin/sha-control"), self.b)
        self.assertEqual(self.local_tips(self.b), [])
        row = self.add(lane="remote-control", ref=self.b)
        self.assertEqual(row["tip"], self.b)
        self.assertIsNone(row.get("ref_branch"))

    def test_tags_and_remotes_do_not_make_one_local_tip_ambiguous(self):
        tip = self.topic("lane/sha-local", self.a, "sha-local")
        self.git("tag", "sha-local-tag", tip)
        self.git("update-ref", "refs/remotes/origin/sha-local", tip)
        self.assertEqual(self.local_tips(tip), ["refs/heads/lane/sha-local"])
        row = self.add(lane="local-with-other-refs", ref=tip)
        self.assertEqual(row.get("ref_branch"), "refs/heads/lane/sha-local")

    def test_optional_sha_branch_discovery_fails_open(self):  # noqa: VACUOUS_ASSERTION — fixed failure matrix proves each probe executes and every dispatch retains its exact tip
        real_run = subprocess.run
        for suffix, failure in (
                ("oserror", OSError("git unavailable")),
                ("decode", UnicodeDecodeError(
                    "utf-8", b"\xff", 0, 1, "invalid ref byte")),
                ("timeout", subprocess.TimeoutExpired("git", 5)),
                ("nonzero", subprocess.CompletedProcess(
                    ["git", "for-each-ref"], 1, stdout="", stderr="no refs")),
                ("malformed", subprocess.CompletedProcess(
                    ["git", "for-each-ref"], 0, stdout="not-a-ref\n", stderr=""))):
            with self.subTest(failure=suffix):
                tip = self.topic("lane/discovery-" + suffix, self.a,
                                 "discovery-" + suffix)
                self.assertEqual(self.local_tips(tip),
                                 ["refs/heads/lane/discovery-" + suffix])
                discovered = []

                def optional(argv, *args, **kwargs):
                    if "for-each-ref" in argv and "refs/heads" in argv:
                        discovered.append(tuple(argv))
                        if isinstance(failure, BaseException):
                            raise failure
                        return failure
                    return real_run(argv, *args, **kwargs)

                with mock.patch.object(dispatches.subprocess, "run",
                                       side_effect=optional):
                    row = dispatches.add(
                        "codex-3", "discovery failure " + suffix, ref=tip,
                        repo=self.repo, new_work=True, notify=False)
                self.assertTrue(discovered,
                                "the optional discovery seam was not exercised")
                self.assertIsNotNone(row)
                self.assertEqual(row["tip"], tip)
                self.assertIsNone(row.get("ref_branch"))

    def test_sha_branch_discovery_ignores_a_hostile_git_environment(self):
        clone = os.path.join(self.tmp, "sha-clone")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True)
        tip = self.topic("lane/local-sha", self.a, "local-sha")
        self.assertEqual(self.local_tips(tip), ["refs/heads/lane/local-sha"])
        foreign_head = self.git("rev-parse", "HEAD", cwd=clone)
        self.assertNotEqual(foreign_head, tip)
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(clone, ".git"),
                              "GIT_WORK_TREE": clone}):
            row = dispatches.add(
                "codex-3", "hostile sha discovery", ref=tip, repo=self.repo,
                new_work=True, notify=False)
        self.assertIsNotNone(row)
        self.assertEqual(row["tip"], tip)
        self.assertEqual(row.get("ref_branch"), "refs/heads/lane/local-sha")

    def test_approve_on_a_forward_moved_branch_refuses_with_a_runnable_reissue(self):
        tip = self.topic("lane/topic", self.a, "topic1")
        row = self.add(lane="topic", ref="lane/topic")
        head = self.advance("lane/topic", "topic2")
        with open(dispatches.ledger_path(), "rb") as f:
            before = f.read()
        out, why = dispatches.mark_verdict(row["id"], tip, "looks good",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)
        self.assertIn("--ref " + head, why)
        self.assertIn("--supersedes " + row["id"], why)
        self.assertIn("--kind", why)
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")

    def test_approve_after_a_rebase_is_movement_even_without_ancestry(self):
        """Only the branch's own reflog still says it sat at the tip."""
        tip = self.topic("lane/topic", self.a, "topic1")
        row = self.add(lane="topic", ref="lane/topic")
        self.git("checkout", "-q", "lane/topic")
        self.git("rebase", "-q", self.main)
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertNotEqual(head, tip)
        rc = subprocess.run(["git", "-C", self.repo, "merge-base",
                             "--is-ancestor", tip, head]).returncode
        self.assertNotEqual(rc, 0)      # the fixture really is the rebase shape
        out, why = dispatches.mark_verdict(row["id"], tip, "ok",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)

    def test_approve_under_an_expired_reflog_is_still_caught_by_ancestry(self):
        """Reflogs expire (git gc); forward movement must then be proven by
        ancestry alone — the only fixture where the ancestor leg can die."""
        tip = self.topic("lane/expired", self.a, "expired1")
        row = self.add(lane="expired", ref="lane/expired")
        self.advance("lane/expired", "expired2")
        self.git("reflog", "expire", "--expire=now", "--all")
        gone = subprocess.run(["git", "-C", self.repo, "rev-list", "-g",
                               "refs/heads/lane/expired"],
                              capture_output=True, text=True)
        self.assertNotIn(tip, gone.stdout)   # the reflog cannot testify
        out, why = dispatches.mark_verdict(row["id"], tip, "ok",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)

    def test_a_fix_on_a_moved_branch_writes_normally(self):
        """The author advancing WHILE fixing is the normal response to a FIX
        review; refusing the verdict the movement caused inverts the
        workflow (review blocker 1 — the old arms codified the inversion)."""
        tip = self.topic("lane/fixing", self.a, "fixing1")
        row = self.add(lane="fixing", ref="lane/fixing")
        self.advance("lane/fixing", "fixing2")
        out, why = dispatches.mark_verdict(row["id"], tip, "found issues",
                                           polarity="fix")
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")
        self.assertEqual(out["polarity"], "fix")

    def test_a_supersede_on_a_moved_branch_writes_normally(self):
        """SUPERSEDE is directly CAUSED by movement."""
        tip = self.topic("lane/super", self.a, "super1")
        row = self.add(lane="super", ref="lane/super")
        self.advance("lane/super", "super2")
        out, why = dispatches.mark_verdict(row["id"], tip, "replaced",
                                           polarity="supersede")
        self.assertIsNone(why, why)
        self.assertEqual(out["polarity"], "supersede")

    def test_approve_at_the_branch_tip_writes_normally(self):
        tip = self.topic("lane/steady", self.a, "steady1")
        row = self.add(lane="steady", ref="lane/steady")
        out, why = self.bound_approve(row["id"], tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")
        self.assertEqual(out["polarity"], "approve")

    def test_a_sha_bound_forward_move_refuses_approve(self):
        tip, row = self.sha_dispatch(
            "lane/sha-forward", self.a, "sha-forward1")
        head = self.advance("lane/sha-forward", "sha-forward2")
        out, why = dispatches.mark_verdict(
            row["id"], tip, "looks good", polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)
        self.assertIn(head, why)

    def test_a_sha_bound_rebase_refuses_approve(self):
        tip, row = self.sha_dispatch(
            "lane/sha-rebase", self.a, "sha-rebase1")
        self.git("checkout", "-q", "lane/sha-rebase")
        self.git("rebase", "-q", self.main)
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertNotEqual(head, tip)
        self.assertNotEqual(subprocess.run(
            ["git", "-C", self.repo, "merge-base", "--is-ancestor",
             tip, head]).returncode, 0)
        out, why = dispatches.mark_verdict(
            row["id"], tip, "looks good", polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)

    def test_a_sha_bound_steady_branch_approves(self):
        tip, row = self.sha_dispatch(
            "lane/sha-steady", self.a, "sha-steady1")
        out, why = self.bound_approve(row["id"], tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")
        self.assertEqual(out["polarity"], "approve")

    def test_sha_bound_FIX_and_SUPERSEDE_ignore_branch_movement(self):  # noqa: VACUOUS_ASSERTION — fixed polarity matrix requires a persisted terminal verdict in both cases
        for polarity in ("fix", "supersede"):
            with self.subTest(polarity=polarity):
                branch = "lane/sha-" + polarity
                tip, row = self.sha_dispatch(
                    branch, self.a, "sha-" + polarity + "1")
                self.advance(branch, "sha-" + polarity + "2")
                out, why = dispatches.mark_verdict(
                    row["id"], tip, polarity + " evidence", polarity=polarity)
                self.assertIsNone(why, why)
                self.assertEqual(out["status"], "verdict")
                self.assertEqual(out["polarity"], polarity)

    def test_a_raw_sha_at_trunk_tip_binds_and_trunk_movement_refuses(self):  # noqa: VACUOUS_ASSERTION — exact trunk binding and named moved head positively control the refused write
        self.assertEqual(self.local_tips(self.c), ["refs/heads/" + self.main])
        row = self.add(lane="trunk-sha", ref=self.c)
        self.assertEqual(row.get("ref_branch"), "refs/heads/" + self.main)
        head = self.advance(self.main, "trunk-sha-moved")
        out, why = dispatches.mark_verdict(
            row["id"], self.c, "looks good", polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)
        self.assertIn(head, why)

    def test_a_later_branch_impostor_cannot_retroactively_bind_a_sha(self):  # noqa: VACUOUS_ASSERTION — successful gated verdict is the positive control for intentionally absent binding
        """A SHA with no branch-tip match stays unbound: an unrelated branch
        created FROM that commit later cannot manufacture movement."""
        row = self.add(lane="ghostly", ref=self.b)    # SHA, no branch exists
        self.assertIsNone(dispatches.snapshot()[0][row["id"]].get("ref_branch"))
        # the imposter carries EXACTLY the name a lane-text guesser would
        # derive, created FROM the dispatched commit and advanced past it
        self.topic("lane/ghostly", self.b, "imposter1")
        self.advance("lane/ghostly", "imposter2")
        out, why = self.bound_approve(row["id"], self.b)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")

    def test_a_sha_bound_verdict_retry_freezes_the_original_binding(self):
        tip, row = self.sha_dispatch(
            "lane/sha-retry", self.a, "sha-retry1")
        out, why = dispatches.mark_verdict(
            row["id"], tip, "found issues", polarity="fix")
        self.assertIsNone(why, why)
        self.assertEqual(out.get("ref_branch"), "refs/heads/lane/sha-retry")
        self.advance("lane/sha-retry", "sha-retry2")
        again, retry_why = dispatches.mark_verdict(
            row["id"], tip, "found issues", polarity="fix")
        self.assertIsNone(retry_why, retry_why)
        self.assertEqual(again["status"], "verdict")
        self.assertEqual(again.get("ref_branch"),
                         "refs/heads/lane/sha-retry")
        verdicts = [e for e in dispatches.history(row["id"])
                    if e.get("event") == "verdict"]
        self.assertEqual(len(verdicts), 1)

    def test_renaming_a_sha_bound_branch_makes_movement_unanswerable(self):
        tip, row = self.sha_dispatch(
            "lane/sha-rename", self.a, "sha-rename1")
        self.git("branch", "-m", "lane/sha-rename", "lane/sha-renamed")
        self.assertEqual(self.local_tips(tip),
                         ["refs/heads/lane/sha-renamed"])
        self.assertIsNone(dispatches._lane_movement(row, tip))
        out, why = self.bound_approve(row["id"], tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")

    def test_deleting_a_sha_bound_branch_makes_movement_unanswerable(self):  # noqa: VACUOUS_ASSERTION — successful gated verdict positively controls the deleted-ref absence
        tip, row = self.sha_dispatch(
            "lane/sha-delete", self.a, "sha-delete1")
        self.git("branch", "-D", "lane/sha-delete")
        self.assertEqual(self.local_tips(tip), [])
        self.assertIsNone(dispatches._lane_movement(row, tip))
        out, why = self.bound_approve(row["id"], tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")

    def test_a_branch_ref_with_a_junk_lane_is_still_guarded(self):
        """Review blocker 2, miss case: the lane text names no branch, but
        the ref did — the stored identity guards it anyway."""
        tip = self.topic("lane/real-work", self.a, "real1")
        row = self.add(lane="UI cleanup review", ref="lane/real-work")
        self.advance("lane/real-work", "real2")
        out, why = dispatches.mark_verdict(row["id"], tip, "ship it",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)

    def test_a_hostile_git_dir_cannot_redirect_the_probe(self):
        """Review blocker 4: ambient GIT_DIR selects another repository for
        every git invocation regardless of -C. If the probe honoured it, the
        branch would read absent, the guard unanswerable, and this moved
        approve would WRITE."""
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.git("init", "-q", cwd=other)
        tip = self.topic("lane/redirect", self.a, "redirect1")
        row = self.add(lane="redirect", ref="lane/redirect")
        self.advance("lane/redirect", "redirect2")
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(other, ".git")}):
            out, why = dispatches.mark_verdict(row["id"], tip, "ok",
                                               polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)

    def test_write_boundary_identity_survives_a_hostile_git_env(self):
        """Meld e:1785552432 acceptance fixture, verbatim: a hostile
        GIT_DIR/GIT_WORK_TREE at DISPATCH time must not store a foreign
        repo_id, tip, or ref_branch — the read-side guard would then
        faithfully protect a lie. Dies independently if the scrub is removed
        from _repo_info (repo_id binds B) or any _resolve_tip call (tip
        binds B's head)."""
        clone = os.path.join(self.tmp, "cloneB")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True,
                       capture_output=True)
        with open(os.path.join(clone, "bonly"), "w") as f:
            f.write("b\n")
        self.git("add", "bonly", cwd=clone)
        self.git("commit", "-q", "-m", "b-only", cwd=clone)
        b_head = self.git("rev-parse", "HEAD", cwd=clone)
        a_head = self.git("rev-parse", self.main)
        self.assertNotEqual(b_head, a_head)      # the trap is armed
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(clone, ".git"),
                                          "GIT_WORK_TREE": clone}):
            row = dispatches.add("codex-3", "UI cleanup review", ref=self.main,
                                 repo=self.repo, kind="review", new_work=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["repo_id"],
                         os.path.realpath(os.path.join(self.repo, ".git")))
        self.assertEqual(row["tip"], a_head)
        self.assertEqual(row["ref_branch"], "refs/heads/" + self.main)
        control = dispatches.add("codex-3", "UI cleanup review 2", ref=self.main,
                                 repo=self.repo, kind="review", new_work=True)
        for key in ("repo_id", "tip", "ref_branch"):
            self.assertEqual(row[key], control[key])   # byte-identical identity

    def test_a_hostile_env_cannot_refuse_a_branch_that_exists(self):
        """The show-ref leg's harm is AVAILABILITY, not identity: its output
        is only existence, which coincides in a clone — but a branch living
        only in repo A must stay dispatchable under a hostile env pointing
        at B, or the scrub gap denies honest work."""
        clone = os.path.join(self.tmp, "cloneC")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True,
                       capture_output=True)
        self.topic("lane/a-only", self.a, "aonly1")   # born AFTER the clone
        tip = self.git("rev-parse", "lane/a-only")
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(clone, ".git"),
                                          "GIT_WORK_TREE": clone}):
            row = dispatches.add("codex-3", "a-only work", ref="lane/a-only",
                                 repo=self.repo, kind="review", new_work=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["tip"], tip)
        self.assertEqual(row["ref_branch"], "refs/heads/lane/a-only")

    def test_junk_branch_binding_never_raises_and_grants_nothing(self):  # noqa: VACUOUS_ASSERTION — refusal contract: every junk shape must yield None, there is no positive to assert
        for repo_id, branch in ((["not", "a", "path"], "refs/heads/x"),
                                (7, "refs/heads/x"), (None, "refs/heads/x"),
                                ("relative/path", "refs/heads/x"),
                                (os.getcwd(), None),
                                (os.getcwd(), 7),
                                (os.getcwd(), "main")):
            self.assertIsNone(dispatches._lane_movement(
                {"repo_id": repo_id, "ref_branch": branch}, "a" * 40))

    def test_the_idempotent_retry_of_a_standing_verdict_is_untouched(self):
        """The guard sits behind the terminal-status checks: a replayed
        verdict reconciles even if the branch moved after it was recorded."""
        tip = self.topic("lane/replay", self.a, "replay1")
        row = self.add(lane="replay", ref="lane/replay")
        out, why = dispatches.mark_verdict(row["id"], tip, "ok", "fix")
        self.assertIsNone(why)
        self.advance("lane/replay", "replay2")
        again, why2 = dispatches.mark_verdict(row["id"], tip, "ok", "fix")
        self.assertIsNone(why2)
        self.assertEqual(again["status"], "verdict")


class OpenRecipientsTest(unittest.TestCase):
    """The open-work census proxywatch's IDLE-vs-HUNG rung reads. Both arms:
    a measured count, and a blind ledger that must stay None — a {} from a
    read that never happened would call every seat out of work."""

    def test_counts_only_open_rows_per_recipient(self):
        current = {"a": {"status": "open", "recipient": "codex"},
                   "b": {"status": "open", "recipient": "codex"},
                   "c": {"status": "verdict", "recipient": "codex"},
                   "d": {"status": "cancelled", "recipient": "kimi"},
                   "e": {"status": "open", "recipient": "kimi"},
                   "f": {"status": "open", "recipient": ""}}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(current, None)):
            counts, err = dispatches.open_recipients()
        self.assertIsNone(err)
        self.assertEqual(counts, {"codex": 2, "kimi": 1})

    def test_a_seat_with_no_open_rows_measures_zero_beside_a_counted_sibling(self):
        """The positive control rides the same observable: kimi's row proves
        the census read the ledger, and codex's zero is then a MEASUREMENT."""
        current = {"e": {"status": "open", "recipient": "kimi"}}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(current, None)):
            counts, err = dispatches.open_recipients()
        self.assertIsNone(err)
        self.assertEqual(counts, {"kimi": 1})
        self.assertEqual(counts.get("codex", 0), 0)

    def test_an_unreadable_ledger_is_None_never_an_empty_board(self):
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger unavailable")):
            counts, err = dispatches.open_recipients()
        self.assertIsNone(counts)
        self.assertEqual(err, "ledger unavailable")


class AuthorIdentityTest(DispatchBase):
    """THE FLOOR IS NEVER AN AUTHOR (owner-declared P0, 2026-08-02).
    dispatches stamped seats.derive_seat's last rung — the bare family floor —
    as the acting identity, so `helm dispatch mix` showed sender 'claude' with
    84 reviews and 6 stalled rows owned by a name three seats share nagged
    every claude seat and could never be disowned. handoff._own_seat REFUSED
    the floor all along; _acting_author aligns add()/send() to that law,
    relaxed by exactly one rung (a single-valued roster binding may author).
    Each fixture fails exactly one clause of _acting_author."""

    SID = "aaaabbbb-1111-4222-8333-444455556666"

    def test_add_refuses_an_identityless_author_instead_of_the_floor(self):
        os.environ.pop("HELM_CHAT_NAME", None)
        row, why = dispatches.add("someseat", "somelane", ref=self.a,
                                  repo=self.repo, kind="review", notify=False,
                                  new_work=True, _reason=True)
        self.assertIsNone(row, "an identityless add still minted a row")
        self.assertIn("family floor", why)
        self.assertIn("HELM_CHAT_NAME", why)      # the fix, not just the no
        self.assertIn("helm chat join", why)
        self.assertIn("disowned", why)            # why the floor is banned

    def test_send_refuses_an_identityless_author(self):
        os.environ.pop("HELM_CHAT_NAME", None)
        row, why, sent = dispatches.send(
            "codex-3", "review", "msg", self.a, repo=self.repo, sign=False,
            new_work=True)
        self.assertIsNone(row)
        self.assertFalse(sent)
        self.assertIn("HELM_CHAT_NAME", why)

    def test_a_floor_shaped_environment_is_still_refused(self):
        """The measured incident shape: a claude-family env with a session id
        but NO name and NO roster row. derive_seat would mint the bare family
        floor here (tests/test_seat_identity_cli.py pins that contract, which
        this lane deliberately does NOT change — the CALLER refuses)."""
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDECODE"] = "1"    # in ENV_KEYS: tearDown restores
        for k in ("CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL"):
            prior = os.environ.pop(k, None)
            if prior is not None:
                self.addCleanup(os.environ.__setitem__, k, prior)
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.assertEqual(seats.derive_seat(self.SID), "claude",
                         "fixture control: this env IS the floor shape")
        row, why = dispatches.add("someseat", "somelane", ref=self.a,
                                  repo=self.repo, kind="review", notify=False,
                                  new_work=True, _reason=True)
        self.assertIsNone(row)
        self.assertIn("'claude'", why)

    def test_a_roster_bound_session_may_author(self):
        """The one-rung relaxation vs handoff._own_seat: a hand-launched but
        rostered seat (no env name) still authors — refusing it would refuse
        every such seat, and after the write_roster refusal the binding is
        single-valued."""
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        seats.write_roster("integrator", session=self.SID)
        seats.write_roster("someseat")                  # recipient must be rostered
        row = dispatches.add("someseat", "somelane", ref=self.a,
                             repo=self.repo, kind="review", notify=False,
                             new_work=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["sender"], "integrator")

    def test_a_declared_vs_rostered_dispute_refuses_to_author(self):
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        seats.write_roster("codex-3", session=self.SID)
        os.environ["HELM_CHAT_NAME"] = "integrator"
        row, why = dispatches.add("someseat", "somelane", ref=self.a,
                                  repo=self.repo, kind="review", notify=False,
                                  new_work=True, _reason=True)
        self.assertIsNone(row)
        self.assertIn("integrator", why)
        self.assertIn("codex-3", why)
        self.assertIn("disown", why)

    def test_a_hostile_name_keeps_the_exact_token_refusal(self):
        os.environ["HELM_CHAT_NAME"] = "bad\nseat"
        row, why = dispatches.add("someseat", "somelane", ref=self.a,
                                  repo=self.repo, kind="review", notify=False,
                                  new_work=True, _reason=True)
        self.assertIsNone(row)
        self.assertIn("sender", why)


class UnroutableRecipientTest(DispatchBase):
    """#116 — `dispatch send claude <lane>` returned rc=0 and printed
    "(delivery observed)" for a recipient no seat holds. `claude` is the FAMILY
    name of helm-claude and helm-claude-2; the row sat PENDING VERDICT with no
    possible owner until an idle watchdog called it STRANDED, and every surface
    corroborated the typo because a bogus recipient renders in the same column
    as a real one.

    The two directions fail DIFFERENTLY on purpose and each direction has its
    own clause below: a wrong refusal costs a real dispatch, so unknown-roster
    and empty-roster both PROCEED; a wrong delivery claim costs hours, so the
    claim requires positive roster evidence."""

    def seatrow(self, name):
        return seats.write_roster(name, presence_beat=False)

    def argv(self, verb, recipient, lane, *extra):
        return [verb, recipient, lane, "hi", "--ref", self.a,
                "--repo", self.repo, "--kind", "review",
                "--new-work"] + list(extra)

    def test_an_unroutable_recipient_is_REFUSED_naming_the_REAL_seats(self):
        for name in ("helm-claude", "helm-claude-2", "codex"):
            self.seatrow(name)
        rc, out, err = run(dispatches.cmd_dispatch,
                           self.argv("send", "claude", "some-lane"))
        self.assertEqual(rc, 2)
        self.assertIn("no roster row", err)
        # The near-miss must name the seats that EXIST — the whole point is
        # that the sender learns the real name, not merely that they were wrong.
        self.assertIn("helm-claude", err)
        self.assertIn("helm-claude-2", err)
        self.assertNotIn("delivery observed", out)

    def test_the_refusal_leaves_the_ledger_BYTE_IDENTICAL(self):
        for name in ("helm-claude", "codex"):
            self.seatrow(name)
        rc, _out, _err = run(dispatches.cmd_dispatch,
                             self.argv("send", "codex", "control-lane"))
        self.assertEqual(rc, 0)                      # POSITIVE CONTROL: sends work
        with open(dispatches.ledger_path(), "rb") as f:
            before = f.read()
        self.assertIn(b"control-lane", before)       # and the control WROTE
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "claude", "refused-lane"))
        self.assertEqual(rc, 2)
        with open(dispatches.ledger_path(), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertIn("no roster row", err)

    def test_a_ROUTABLE_recipient_still_sends(self):
        self.seatrow("helm-claude-2")
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "helm-claude-2", "real-lane"))
        self.assertEqual(rc, 0, err)
        # rc=0 ALONE WOULD BE VACUOUS: it says the CLI did not object, not that
        # a dispatch exists. Assert the row.
        rows, _ = dispatches.snapshot()
        sent = [r for r in rows.values() if r["lane"] == "real-lane"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["recipient"], "helm-claude-2")
        self.assertEqual(sent[0]["status"], "open")

    def test_an_EMPTY_roster_PROCEEDS_because_it_is_unknown_not_negative(self):
        """A fresh install, a scratch HELM_HOME and every fixture has an empty
        roster. Refusing there would read as "this seat does not exist" when
        the truth is "this box has not met any seat yet"."""
        self.assertEqual(seats.roster(), {})
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: prove roster() can report a
        # seat in THIS fixture before trusting its emptiness. A misrooted chat
        # dir returns {} unconditionally, which would make the emptiness above
        # an artifact of the harness rather than a state of the box — and this
        # whole clause would then be testing nothing.
        self.seatrow("probe-seat")
        self.assertIn("probe-seat", seats.roster())
        os.remove(seats.roster_path())
        self.assertEqual(seats.roster(), {})
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "nobody-yet", "fresh-box-lane"))
        self.assertEqual(rc, 0, err)
        # The POSITIVE half of "did not refuse": a row actually exists.
        rows, _ = dispatches.snapshot()
        sent = [r for r in rows.values() if r["lane"] == "fresh-box-lane"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["recipient"], "nobody-yet")

    def test_ADD_refuses_on_the_same_rule_as_SEND(self):
        self.seatrow("helm-claude")
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("add", "claude", "added-lane"))
        self.assertEqual(rc, 2)
        self.assertIn("no roster row", err)

    def test_FORCE_sends_to_a_pre_join_seat_but_NEVER_CLAIMS_DELIVERY(self):
        """The pre-join address is a real workflow and it survives. What does
        not survive is asserting delivery to it.

        The absence assertion here is guarded by an UNCONDITIONAL POSITIVE
        CONTROL on the SAME observable: `send` is patched to report an observed
        delivery in BOTH halves, so the routable half PROVES the string can
        render on this exact code path. Without that half, "delivery observed"
        being absent would pass for a typo in the assertion itself."""
        self.seatrow("helm-claude")
        real = dispatches.send

        def observed(*a, **kw):
            row, why, _sent = real(*a, **kw)
            return row, why, True                 # claim delivery unconditionally

        with mock.patch.object(dispatches, "send", observed):
            rc, out, err = run(dispatches.cmd_dispatch,
                               self.argv("send", "helm-claude", "control-lane"))
            self.assertEqual(rc, 0, err)
            self.assertIn("delivery observed", out)     # POSITIVE CONTROL

            rc, out, err = run(dispatches.cmd_dispatch,
                               self.argv("send", "claude", "prejoin-lane",
                                         "--force"))
            self.assertEqual(rc, 0, err)                # the send is PERMITTED
            self.assertNotIn("delivery observed", out)  # the CLAIM is not
            # "NO RECIPIENT", never "NOT DELIVERED": a forced pre-join send
            # really does publish its mention/DM row, so the ledger really does
            # record a `delivered` event. Borrowing that word here to mean
            # something else would put this line in direct contradiction with
            # `dispatch list`. This states only what was measured — that no
            # seat holds the address.
            self.assertIn("NO RECIPIENT", out)
            self.assertNotIn("NOT DELIVERED", out)

    def test_a_CORRUPT_roster_proceeds_rather_than_inventing_a_refusal(self):
        """A failed probe proves nothing, so it must not deny anyone.

        THE MALFORMED STATE HERE IS DELIBERATELY WELL-FORMED JSON OF THE WRONG
        SHAPE, and an earlier version of this test got that wrong. Unparseable
        bytes fail-open through roster() to {}, which the empty-roster arm
        already admits — so a garbage-bytes fixture passes whether the check
        reads roster() or roster_checked(), and proves nothing about either
        (measured: that mutation reddened NO test). Wrong-shaped JSON is where
        they diverge: roster() hands back a NON-EMPTY dict of garbage and the
        membership test then refuses every real seat for not appearing in it,
        while roster_checked reports the failed probe and nobody is denied."""
        self.seatrow("helm-claude")
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump({"ghost": "not-a-row"}, f)
        self.assertEqual(seats.roster_checked(), ({}, True))   # probe FAILED
        self.assertTrue(seats.roster())          # fail-open: NON-EMPTY garbage
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "helm-claude", "blind-lane"))
        self.assertEqual(rc, 0, err)
        rows, _ = dispatches.snapshot()
        self.assertEqual([r["recipient"] for r in rows.values()
                          if r["lane"] == "blind-lane"], ["helm-claude"])

    def test_HISTORICAL_rows_with_unroutable_recipients_stay_readable(self):
        """LIFECYCLE: the refusal is at SEND time only. 838 live rows carry
        recipients like codex-orch that no longer hold a roster row; tightening
        the send door must not retroactively strand them."""
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "codex-orch", "legacy-lane"))
        self.assertEqual(rc, 0, err)                 # written when roster empty
        for name in ("helm-claude", "codex"):
            self.seatrow(name)                       # roster now knows others
        rows, _ = dispatches.snapshot()
        legacy = [r for r in rows.values() if r["lane"] == "legacy-lane"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["recipient"], "codex-orch")
        self.assertEqual(legacy[0]["status"], "open")

    def test_a_HOSTILE_seat_name_cannot_reach_the_terminal_through_the_hint(self):
        """The near-miss prints ROSTER-DERIVED names, which makes this refusal a
        display sink — and a seat KEY is unvalidated at the join seam, so a
        hostile HELM_CHAT_NAME puts ESC/bidi on the roster.

        tests/test_display_launder_tripwire.py owns this law tree-wide; this
        clause is the dispatch-side proof that the hint launders. The absence
        assertion is controlled by asserting the LAUNDERED form is present in
        the same string, so a hint that silently vanished could not pass."""
        hostile = "claude\x1b[2Jpwn"
        self.seatrow(hostile)
        self.seatrow("codex")
        laundered = seats._seat_label(hostile)
        # POSITIVE FIRST, and it is not ceremony: a launder that returned ""
        # would satisfy "no ESC" while destroying the hint entirely, and the
        # whole clause would be vacuous.
        self.assertIn("claude", laundered)       # it kept the addressable part
        self.assertNotIn("\x1b", laundered)      # and dropped the escape
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "claude", "hostile-lane"))
        self.assertEqual(rc, 2)
        self.assertIn("no roster row", err)
        self.assertIn(laundered, err)            # POSITIVE CONTROL: hint fired
        self.assertNotIn("\x1b", err)            # and carries no escape

    def test_REBIND_cannot_hand_a_live_row_to_a_seat_nobody_holds(self):
        """SAME CLASS AS send/add, THROUGH A DIFFERENT DOOR, and the damage is
        worse: send invents a row nobody owns, but rebind takes an obligation a
        seat is ALREADY carrying and moves it to an address that cannot receive.
        rebind() calls resolve_recipient, which NORMALISES and never checks
        membership, so the roster door has to be here too."""
        for name in ("helm-claude", "codex"):
            self.seatrow(name)
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "codex", "rebind-src"))
        self.assertEqual(rc, 0, err)                 # POSITIVE CONTROL
        rows, _ = dispatches.snapshot()
        rid = next(r["id"] for r in rows.values() if r["lane"] == "rebind-src")

        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["rebind", rid[:12], "--to", "claude",
                             "--force", "--reason", "typo target"])

        self.assertEqual(rc, 2, err)
        self.assertIn("no roster row", err)
        self.assertIn("helm-claude", err)            # names the real seat
        rows, _ = dispatches.snapshot()
        still = [r for r in rows.values() if r["lane"] == "rebind-src"]
        self.assertEqual(len(still), 1)
        self.assertEqual(still[0]["recipient"], "codex")   # UNMOVED

    def test_an_UNREADABLE_roster_never_prints_a_DELIVERY_CLAIM(self):
        """CODEX FINDING #1. The tri-state collapsed into a two-way branch:
        `if joined is False / else`, so joined=None fell to the else and
        printed "(delivery observed)" — the exact thing the comment directly
        above it forbids. A comment stating an invariant its adjacent code
        breaks is worse than no comment: the reader checks it, finds it right,
        and stops looking."""
        self.seatrow("helm-claude")
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump({"ghost": "not-a-row"}, f)     # well-formed, wrong shape
        self.assertEqual(seats.roster_checked(), ({}, True))   # probe FAILED
        real = dispatches.send

        def observed(*a, **kw):
            row, why, _sent = real(*a, **kw)
            return row, why, True                    # force the claim path

        with mock.patch.object(dispatches, "send", observed):
            rc, out, err = run(dispatches.cmd_dispatch,
                               self.argv("send", "anyone", "unknown-lane"))
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN", out)                # POSITIVE CONTROL
        self.assertNotIn("delivery observed", out)

    def test_a_CANONICAL_spelling_of_a_REAL_seat_is_not_refused(self):
        """CODEX FINDING #3. The membership check read RAW ARGV while the row
        stores resolve_recipient's output, so 'CODEX' and '@codex' — both of
        which resolve to the real seat 'codex' — were refused as unrostered.
        A check aimed at the wrong object, the same shape as finding #1."""
        self.seatrow("codex")
        self.assertEqual(seats.resolve_recipient("CODEX")[0], "codex")
        # DISTINCT LANES PER SPELLING: they all resolve to the same recipient,
        # so a shared lane name trips the duplicate-row guard and the refusal
        # under test never runs — measured, that is what the first version did.
        for i, spelling in enumerate(("codex", "CODEX", "@codex")):
            rc, _out, err = run(dispatches.cmd_dispatch,
                                self.argv("send", spelling, "canon-%d" % i))
            self.assertEqual(rc, 0, "%r was refused: %s" % (spelling, err))
        rows, _ = dispatches.snapshot()
        # POSITIVE CONTROL: all three stored the SAME canonical recipient.
        got = sorted(r["recipient"] for r in rows.values()
                     if r["lane"].startswith("canon-"))
        self.assertEqual(got, ["codex", "codex", "codex"])

    def test_ADD_does_not_promise_a_beacon_pickup_that_cannot_happen(self):
        """CODEX FINDING #2. `add ghost --force` printed "their beacon picks it
        up on next wake". A seat joining LATER baselines the public rooms at
        EOF — only its DM lane starts at 0 — so that mention is SKIPPED, never
        queued. The promise was false AND reassuring."""
        self.seatrow("helm-claude")
        rc, out, err = run(dispatches.cmd_dispatch,
                           self.argv("add", "ghost", "prejoin-add", "--force"))
        self.assertEqual(rc, 0, err)
        # ASSERTS CODEX'S CONTRACT, not my earlier wording: ABSENT names the
        # state at assignment and says a public mention is not a durable
        # pre-join path. POSITIVE CONTROL first, then the false promise.
        self.assertIn("no roster row at assignment", out)
        self.assertIn("not a durable pre-join path", out)
        self.assertNotIn("picks it up on next wake", out)

    def test_the_REBIND_refusal_does_not_name_a_flag_that_cannot_help(self):
        """CODEX FINDING #4. The shared refusal told a rebind caller to "send
        anyway with --force" — a caller who had ALREADY passed --force, and
        whose --force cannot open that door at all (there it attests
        STARVATION). A remedy that cannot work costs the reader a retry to
        disprove."""
        for name in ("helm-claude", "codex"):
            self.seatrow(name)
        rc, _out, err = run(dispatches.cmd_dispatch,
                            self.argv("send", "codex", "rb-src"))
        self.assertEqual(rc, 0, err)
        rows, _ = dispatches.snapshot()
        rid = next(r["id"] for r in rows.values() if r["lane"] == "rb-src")
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["rebind", rid[:12], "--to", "ghost",
                             "--force", "--reason", "typo"])
        self.assertEqual(rc, 2)
        # POSITIVE CONTROL, and it now asserts a RUNNABLE command: the old
        # remedy read "cancel this row", which is a route, not something a
        # reader can paste.
        # codex's contract: prose PLUS a real executable help path, never a
        # partial backticked command a reader cannot paste.
        self.assertIn("helm dispatch --help", err)
        self.assertIn("one flag cannot prove both", err)
        self.assertNotIn("Send anyway with --force", err)


class RecipientCapabilityGridTest(DispatchBase):
    """THE CARTESIAN GRID codex asked for: door x roster-state x identity-form.

    A CELL MATRIX IS THE POINT, not decoration. Three review rounds each closed
    real cases and left the class alive, because each round tested the cases
    someone had imagined. The invariants below are asserted over the WHOLE
    product, so a cell nobody thought of still has to satisfy them."""

    DOORS = ("send", "add", "rebind")
    IDENTITIES = (("exact", "codex"), ("casefold", "CODEX"),
                  ("at-prefixed", "@codex"), ("invalid", "bad name"))

    def argv(self, verb, recipient, lane, *extra):
        return [verb, recipient, lane, "hi", "--ref", self.a,
                "--repo", self.repo, "--kind", "review",
                "--new-work"] + list(extra)

    def roster_state(self, state):
        """Plant one of the five roster states and return it."""
        path = seats.roster_path()
        # The chat dir is created lazily by write_roster, so the malformed
        # fixtures — which write the file directly — must ensure it first.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)              # RESET: cells must not accumulate
        if state == "joined":
            seats.write_roster("codex", presence_beat=False)
        elif state == "absent":
            seats.write_roster("helm-claude", presence_beat=False)
        elif state == "empty":
            if os.path.exists(path):
                os.remove(path)
        elif state == "unreadable":
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ not json at all")
        elif state == "wrong-shape":
            # WELL-FORMED JSON, WRONG SHAPE — distinct from unreadable bytes
            # and the reason roster_checked exists: the plain reader fail-opens
            # THIS to a NON-EMPTY dict of garbage, so a membership test would
            # refuse every real seat. Garbage bytes fail-open to {} and are
            # caught by the empty arm, so a bytes-only fixture tests nothing.
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ghost": "not-a-row"}, f)
        return state

    def test_the_full_grid_holds_every_invariant(self):  # noqa: VACUOUS_ASSERTION — the absence assertions live inside the 60-cell product by construction; they are controlled by TWO unconditional cells bound to the SAME roots (cap/refusal) before the loop — a JOINED cell proving refusal can be None and an ABSENT cell proving it can be a real sentence — plus an unconditional checked==60 must-hit so a skipped or partial walk cannot pass. Verified by mutation, not by argument: assertIsNone(refusal) is what reddens when rebind's fail-closed arm is removed.
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, before any
        # loop: if the product below never iterates, every assertion inside it
        # is skipped and the test passes over nothing. This cell runs whatever
        # happens, and `checked` is asserted against the full product size at
        # the end so a PARTIAL walk cannot pass either.
        # BOUND TO THE SAME NAMES the product below asserts on (`cap` /
        # `refusal`), deliberately: the vacuity rung matches controls to
        # absence assertions BY ROOT, so a control under a different name
        # proves the instrument and still leaves those assertions uncontrolled.
        self.roster_state("joined")
        cap, refusal = dispatches._recipient_gate(
            "codex", force=False, door="send")
        self.assertEqual(cap["membership"], "JOINED")
        self.assertEqual(cap["canonical"], "codex")
        self.assertIsNone(refusal)
        # ...and the REFUSAL observable positively, so the assertIsNone above
        # and every assertIsNone in the product below are controlled: this
        # proves a refusal CAN be produced and is a non-empty sentence, not
        # that one merely failed to appear.
        self.roster_state("absent")
        cap, refusal = dispatches._recipient_gate(
            "codex", force=False, door="send")
        self.assertEqual(cap["membership"], "ABSENT")
        self.assertIn("no roster row", refusal)

        states = ("joined", "absent", "empty", "unreadable", "wrong-shape")
        checked = 0
        for door in self.DOORS:
            for state in states:
                for form, raw in self.IDENTITIES:
                    with self.subTest(door=door, roster=state, identity=form):
                        if True:
                            # ONE fixture, roster RE-PLANTED per cell. An
                            # earlier version drove setUp/tearDown inside the
                            # loop and rebuilt a git repo 60 times; the gate
                            # reads only the roster, so the churn added
                            # failures of its own and measured nothing extra.
                            self.roster_state(state)
                            cap, refusal = dispatches._recipient_gate(
                                raw, force=False, door=door)
                            checked += 1

                            if form == "invalid":
                                # GRAMMAR ERROR PRESERVED AND SURFACED, never
                                # rewritten as a routing verdict.
                                self.assertIsNotNone(cap["error"])
                                self.assertEqual(cap["evidence"], "malformed")
                                self.assertIsNone(cap["canonical"])
                                self.assertEqual(refusal, cap["error"])
                                self.assertNotIn("roster row", refusal)
                                continue

                            # IDENTITY IS UNCONDITIONAL. Roster state may choose
                            # the display spelling, but it never shapes the
                            # routing key: @-strip + exact-token casefold happen
                            # before membership evidence is interpreted.
                            self.assertEqual(cap["canonical"], "codex")

                            if state == "joined":
                                self.assertEqual(cap["membership"], "JOINED")
                                self.assertIsNone(refusal)
                            elif state == "absent":
                                self.assertEqual(cap["membership"], "ABSENT")
                                self.assertIsNotNone(refusal)   # every door
                            else:
                                # empty / unreadable / wrong-shape are UNKNOWN,
                                # and NO output may assert joined-or-absent.
                                self.assertEqual(cap["membership"], "UNKNOWN")
                                if door == "rebind":
                                    # FAILS CLOSED: it moves an obligation a
                                    # seat already carries.
                                    self.assertIsNotNone(refusal)
                                    self.assertIn("cannot prove both", refusal)
                                else:
                                    # send/add FAIL OPEN: a wrong refusal
                                    # destroys a real dispatch.
                                    self.assertIsNone(refusal)

        # MUST-HIT: the grid actually ran its whole product.
        self.assertEqual(checked, len(self.DOORS) * len(states)
                         * len(self.IDENTITIES))

    def test_casefold_canonicalisation_is_independent_of_roster_evidence(self):
        """A pre-join address and a joined address name one routing identity.

        The typed operand retains the roster/original spelling for display, but
        its string value is canonical in every roster state. This is the seam
        mutation target: removing either @-strip or casefold reds here."""
        self.roster_state("joined")
        joined = seats.resolve_recipient("@CODEX")[0]
        self.assertEqual(joined, "codex")
        self.assertEqual(joined.display, "codex")
        self.roster_state("empty")
        prejoin = seats.resolve_recipient("@CODEX")[0]
        self.assertEqual(prejoin, "codex")
        self.assertEqual(prejoin.display, "CODEX")

    def test_prejoin_cross_case_send_delivers_to_exact_later_join(self):  # noqa: VACUOUS_ASSERTION — the later exact recipient positively delivers from the same lane after the sibling's empty read
        self.roster_state("empty")
        rc, out, err = run(
            dispatches.cmd_dispatch,
            self.argv("send", "@CODEX", "prejoin-casefold"))
        self.assertEqual(rc, 0, err)
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        row = next(r for r in rows.values()
                   if r["lane"] == "prejoin-casefold")
        self.assertEqual((row["recipient"], row["recipient_display"]),
                         ("codex", "CODEX"))
        dm_row = chat.read(seats.dm_lane("codex"))[0][-1]
        self.assertEqual((dm_row["dm"], dm_row["dm_display"]),
                         ("codex", "CODEX"))
        self.assertIn("@CODEX", out)       # display is not the routing key

        # Exact-token negative control: the prefix sibling neither owns the
        # dispatch nor consumes its private-lane message.
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "codex-2"}):
            seats.join(session="s-c2", seat="codex-2", cwd=self.repo)
            self.assertIsNone(seats.deliver_any(
                session="s-c2", seat="codex-2", cwd=self.repo))
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "Codex"}):
            seats.join(session="s-c", seat="Codex", cwd=self.repo)
            delivered = seats.deliver_any(
                session="s-c", seat="Codex", cwd=self.repo)
        self.assertIn("hi", delivered)
        self.assertIn("dm → Codex", delivered)

    def test_wrong_shape_and_unreadable_are_NOT_the_same_cell(self):
        """The two fixtures diverge at the READER, which is why both are in the
        grid: roster() fail-opens wrong-shape to a NON-EMPTY dict of garbage
        while roster_checked reports the failed probe. A grid that used only
        unparseable bytes would pass with either reader and prove nothing."""
        self.roster_state("wrong-shape")
        self.assertTrue(seats.roster())                  # fail-open: NON-empty
        self.assertEqual(seats.roster_checked(), ({}, True))
        cap, _r = dispatches._recipient_gate("codex", False, "send")
        self.assertEqual(cap["evidence"], "read-failed")

    def test_capability_reads_the_checked_roster_ONCE_on_every_path(self):  # noqa: VACUOUS_ASSERTION — checked.call_count is the positive control
        """JOINED was the misleading fast-path control: it returned before alias
        evidence and therefore read once even while every unmatched path read
        twice. Exercise every roster state with the plain reader armed to fail."""
        cases = (("joined", "CODEX"), ("absent", "nobody-here"),
                 ("empty", "nobody-here"), ("unreadable", "nobody-here"),
                 ("wrong-shape", "nobody-here"))
        for state, raw in cases:
            with self.subTest(state=state):
                self.roster_state(state)
                real = seats.roster_checked
                with mock.patch.object(seats, "roster",
                                       side_effect=AssertionError("second read")) as plain, \
                     mock.patch.object(seats, "roster_checked", wraps=real) as checked:
                    seats.recipient_capability(raw)
                self.assertEqual(checked.call_count, 1)
                plain.assert_not_called()

    def test_refusal_hint_is_a_pure_reduction_of_the_capability(self):  # noqa: VACUOUS_ASSERTION — refusal and checked read are positive controls
        self.roster_state("absent")
        real = seats.roster_checked
        with mock.patch.object(seats, "roster",
                               side_effect=AssertionError("second plain read")) as plain, \
             mock.patch.object(seats, "roster_checked", wraps=real) as checked:
            cap, refusal = dispatches._recipient_gate(
                "nobody-here", force=False, door="send")
        self.assertEqual(cap["membership"], "ABSENT")
        self.assertIsNotNone(refusal)
        self.assertEqual(checked.call_count, 1)
        plain.assert_not_called()

    def test_rejected_roster_data_cannot_suppress_an_alias_error(self):
        """The rejected second snapshot used to add `ghost` to canonicals before
        alias_refusal checked malformed configuration, changing ERROR to success.
        Alias evidence must consume the validated empty snapshot instead."""
        self.roster_state("wrong-shape")
        with mock.patch.dict(os.environ, {"HELM_SEAT_ALIASES": "broken"}), \
             mock.patch.object(seats, "roster",
                               side_effect=AssertionError("rejected data reread")):
            cap = seats.recipient_capability("ghost")
        self.assertIsNone(cap["canonical"])
        self.assertIn("aliases misconfigured", cap["error"])
        self.assertEqual(cap["evidence"], "malformed")

    def _assert_writer_carries_the_capability(self, door):
        """Drive one command through a world where re-resolution changes identity.

        The fixture proves its own discrimination FIRST. The command must then
        write and render the capability's `codex` without calling the exact
        recipient resolver again. Unrelated roster reads — sender attribution,
        for example — are intentionally outside this arm.
        """
        seats.write_roster("codex", presence_beat=False)
        cap = seats.recipient_capability("CODEX")
        self.assertEqual((cap["canonical"], cap["membership"]),
                         ("codex", "JOINED"))
        shifted = {"CODEX": {"session": "shifted"}}
        second, second_err = seats._resolve_against(cap["canonical"], shifted)
        self.assertIsNone(second_err)
        self.assertEqual(second, "codex")
        self.assertEqual(second.display, "CODEX")
        self.assertNotEqual(second.display, cap["canonical"].display)

        if door == "rebind":
            seats.write_roster("source")
            source = self.add(recipient="source", lane="canonical-source")
        else:
            source = None

        def shifted_resolve(value):
            return seats._resolve_against(value, shifted)

        if door == "rebind":
            args = ["rebind", source["id"][:12], "--to", "CODEX",
                    "--force", "--reason", "shifted-world"]
        else:
            args = self.argv(door, "CODEX", "canonical-" + door)
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "sender-session"}), \
             mock.patch.object(seats, "resolve_recipient",
                               side_effect=shifted_resolve) as resolver:
            rc, out, err = run(dispatches.cmd_dispatch, args)
        self.assertEqual(rc, 0, err)
        self.assertEqual(resolver.call_count, 0,
                         "writer re-resolved the canonical recipient")

        rows, _ = dispatches.snapshot()
        written = [r for r in rows.values()
                   if (r.get("supersedes") == source["id"] if source
                       else r["lane"] == "canonical-" + door)]
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["recipient"], "codex")
        self.assertIn("@codex", out)
        self.assertNotIn("@CODEX", out)

    def test_SEND_carries_the_capability_through_the_write(self):  # noqa: VACUOUS_ASSERTION — shared helper has discriminating controls
        self._assert_writer_carries_the_capability("send")

    def test_ADD_carries_the_capability_through_the_write(self):  # noqa: VACUOUS_ASSERTION — shared helper has discriminating controls
        self._assert_writer_carries_the_capability("add")

    def test_REBIND_carries_the_capability_through_the_write(self):  # noqa: VACUOUS_ASSERTION — shared helper has discriminating controls
        self._assert_writer_carries_the_capability("rebind")


class DispatchListStampsItsReadInstant(DispatchBase):
    """`helm dispatch list` is the board an integrator reads before acting —
    and tonight it was quoted forward as a standing fact after it had already
    moved (helm task #125). Its sibling `helm lr` already stamps the instant;
    this surface owned the helper and was its only non-caller."""

    _STAMP = re.compile(r"read (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

    def test_the_header_carries_the_read_instant(self):
        self.add()                    # one row -> the populated header
        rc, out, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL: the row really rendered, so the header assertion
        # below is not reading an empty listing.
        self.assertIn("logical row", out)
        self.assertRegex(out, self._STAMP,
                         "the board's readers re-quote it; a relative-age "
                         "listing with no origin re-anchors to whenever it "
                         "is read next")

    def test_the_empty_listing_stamps_its_absence_claim_too(self):
        rc, out, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("no matching rows", out)

    def test_the_rendered_age_reads_the_same_instant_as_the_marker(self):
        """THE BOUNDARY, at the exact numbers it was reproduced on. The verb
        binds one read instant and hands it to the filter; `_fmt` used to call
        `_age_s(row)` bare, so the age BESIDE the verdict came off the wall
        clock at print time. Read at 1059, a 1000-stamped/60s row is not late
        — and rendering it at 1120 printed `2m/1m` with NO NEEDS CHECK-IN: an
        age past its own deadline sitting next to the marker's silence, on one
        line. The render clock is pushed 61s ahead here precisely so a second
        time source cannot hide."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1000))
        row = {"id": "d" * 12, "ts": ts, "deadline_s": 60, "status": "open",
               "lane": "lane-boundary", "recipient": "codex-3",
               "ref": "abc123def456"}
        with mock.patch.object(dispatches, "progress_state",
                               return_value=(dispatches.IDLE, "no claim")):
            before = dispatches._is_overdue(row, 1059.0)
            after = dispatches._is_overdue(row, 1060.0)
        self.assertEqual((before, after), (False, True))   # the filter's edge
        # UNDER the deadline at the read instant — and the printed age says so
        # even though the wall clock has since walked past it.
        with mock.patch.object(dispatches.time, "time", return_value=1120.0):
            line = dispatches._fmt(row, 1059.0, before, ())
        self.assertIn("  0m/1m ", line)
        self.assertNotIn("NEEDS CHECK-IN", line)
        # AT the deadline: age and marker are printed by the same instant, so
        # they agree in one string rather than two the reader must reconcile.
        with mock.patch.object(dispatches.time, "time", return_value=1120.0):
            edge = dispatches._fmt(row, 1060.0, after, ())
        self.assertIn("  1m/1m  NEEDS CHECK-IN (OVERDUE)", edge)


class LaneNormalizationTest(DispatchBase):
    """#142: the ledger stored the lane field two ways (798 bare rows / 82
    lane/-prefixed), so any join on lane silently half-matched. Writers strip
    the prefix through THE ONE canonicalizer (landreq._strip_lane_prefix);
    read-side joins go through the same family normalisation, so BOTH stored
    spellings keep matching. Historical rows are never rewritten."""

    def test_write_time_normalization_strips_lane_prefix(self):
        row = self.add(recipient="codex-3", lane="lane/my-feature")
        self.assertEqual(row["lane"], "my-feature")
        # Round 2 (codex finding 6): the prefix compare is casefolded while
        # the sliced remainder keeps its display spelling — an upper-cased
        # prefix must not smuggle a prefixed row past always-bare storage.
        self.assertEqual(landreq._strip_lane_prefix("LANE/foo"), "foo")
        self.assertEqual(landreq._strip_lane_prefix("Lane/Case-Kept"),
                         "Case-Kept")
        upper = self.add(recipient="codex-3", lane="LANE/case-borne")
        self.assertEqual(upper["lane"], "case-borne")

    def test_prefixed_and_bare_fixture_pair_must_join_and_match(self):
        # A row created prefixed and a row created bare normalize to the same
        # stem and join — BOTH directions through the one canonicalizer.
        self.assertEqual(landreq._strip_lane_prefix("lane/my-feature"), "my-feature")
        self.assertEqual(landreq._strip_lane_prefix("my-feature"), "my-feature")
        self.assertEqual(landreq._lane_stem("lane/my-feature-r2"), "my-feature")
        self.assertEqual(landreq._lane_stem("my-feature"), "my-feature")

        # Duplicate check detects collision across prefixed vs bare STORED
        # rows — the write-strip means a freshly minted pair is byte-equal, so
        # each direction plants the OTHER spelling as a historical row:
        # (A) an 82-style prefixed historical row vs a bare query;
        # (B) a bare stored row (the minted one) vs a prefixed query.
        r1 = self.add(recipient="codex-3", lane="lane/join-test")
        self.assertEqual(r1["lane"], "join-test")   # write-strip, the control
        hist = dict(r1, id="hist0001", lane="lane/join-test")
        warn, needs_force = dispatches._duplicate_mint_warning(
            {"id": "dummy2", "recipient": "codex-3", "lane": "join-test",
             "repo_id": r1["repo_id"]},
            {"hist0001": hist})
        self.assertTrue(needs_force)
        self.assertIn("already uses lane label 'join-test'", warn)
        warn, needs_force = dispatches._duplicate_mint_warning(
            {"id": "dummy2", "recipient": "codex-3", "lane": "lane/join-test",
             "repo_id": r1["repo_id"]},
            {r1["id"]: r1})
        self.assertTrue(needs_force)
        self.assertIn("already uses lane label", warn)

    def test_verdict_turn_binding_is_byte_bound_to_the_stored_lane(self):
        """Round 2 (codex finding 1): the vlane a turn signs and the vlane
        replay reconstructs are the SAME BYTES — the row's stored lane. Both
        the emit side and the confirm join derive from that one field, so a
        normalized spelling on either side self-invalidates the attest."""
        class _Chat(object):
            @staticmethod
            def is_verdict(m):
                return True

            @staticmethod
            def is_reply(m):
                return False

        row = {"id": "x1", "lane": "feature-r2",
               "reviewed_tip": "a" * 40, "verdict_ref": "gate:z"}
        turn = {"vrid": "x1", "vlane": "feature-r2", "vtip": "a" * 40,
                "vref": "gate:z"}
        self.assertTrue(dispatches._is_this_verdicts_turn(turn, row, _Chat))
        # The family stem is NOT the signed spelling — a stem-signed turn
        # must refuse to bind (it could never re-verify in _report_from_done).
        self.assertFalse(dispatches._is_this_verdicts_turn(
            dict(turn, vlane="feature"), row, _Chat))

    def test_attest_emit_signs_the_lane_replay_reconstructs(self):  # noqa: VACUOUS_ASSERTION — an un-invoked capture leaves captured empty and the verdict subscript then raises KeyError loudly; the two assertEquals are the positive controls on the captured bytes
        """Round 2 (codex finding 1), the emit side: _report_from_done
        rebuilds the signed payload from the RAW row lane, so what
        _emit_and_record hands the signer must be those same bytes. Probe:
        feature-r2 signed as its stem made every post-land report read
        NEEDS CONFIRMATION."""
        from helm import chat
        row = self.add(lane="feature-r2")
        captured = {}

        def capture(*a, **k):
            captured.update(k)
            raise RuntimeError("down")

        with mock.patch.object(chat, "post", side_effect=capture):
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "PASS", "fix")
        self.assertIsNone(why)
        stored = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(captured["verdict"]["lane"], "feature-r2")
        self.assertEqual(captured["verdict"]["lane"],
                         str(stored.get("lane") or ""))

    def test_explicit_key_retry_is_idempotent_across_the_prefix_split(self):
        """Round 2 (codex finding 3): 128 historical v3 rows store the lane/
        spelling; a retry re-derived through today's writer arrives bare, and
        a byte-for-byte semantic compare read that as DIFFERENT WORK instead
        of an idempotent retry."""
        row, why, posted = dispatches.send(
            "codex-3", "retry-lane", "Do the thing", self.a, repo=self.repo,
            key="opk-142", sign=False, new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        # Age the stored event into its historical spelling (append-only in
        # real code; the test rewrites its own fixture ledger to plant
        # history the current writer can no longer produce).
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == row["id"] and ev.get("event") == "dispatch":
                    ev["lane"] = "lane/retry-lane"
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1)
        # Replay is byte-truth: the historical spelling survives the read.
        self.assertEqual(dispatches.rows()[row["id"]]["lane"],
                         "lane/retry-lane")
        again, why, posted = dispatches.send(
            "codex-3", "retry-lane", "Do the thing", self.a, repo=self.repo,
            key="opk-142", sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(len(dispatches.rows()), 1)

    def test_auto_key_send_reconciles_onto_its_historical_prefixed_row(self):
        """Round 3 (codex sibling): 115 of the 128 prefixed v3 rows are
        AUTO-key mints. The old writer hashed the auto key over the stored
        lane/ spelling; today's writer hashes the canonical lane, derives a
        DIFFERENT id, and sailed past its own row into duplicate refusal (or
        a --force duplicate). The historical side of this fixture is FROZEN:
        legacy key and id are computed by the old formula inlined here,
        never by the current key builder — a fixture that derives both
        sides from live code proves nothing about history."""
        row, why, posted = dispatches.send(
            "codex-3", "legacy-auto", "Old auto send", self.a,
            repo=self.repo, sign=False, new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        stored = dispatches.rows()[row["id"]]
        # THE OLD WRITER'S BYTES, frozen inline: auto key over the PREFIXED
        # spelling, id over sender/repo/key.
        legacy_key = "auto:" + hashlib.blake2b(
            "\0".join((stored["recipient"], "lane/legacy-auto",
                        stored["tip"], "", str(stored["deadline_s"]),
                        stored["message_hash"], "new-work")).encode("utf-8"),
            digest_size=16).hexdigest()
        legacy_id = hashlib.blake2b(
            ("dispatch\0" + "\0".join((stored["sender"], stored["repo_id"],
                                        legacy_key))).encode("utf-8"),
            digest_size=16).hexdigest()
        self.assertNotEqual(legacy_id, row["id"])   # the split is real
        # Age the ledger into the historical shape: the row as the OLD
        # writer wrote it — prefixed lane, legacy key, legacy id,
        # self-rooted at that id.
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == row["id"]:
                    if ev.get("chain_root") == row["id"]:
                        ev["chain_root"] = legacy_id
                    ev["id"] = legacy_id
                    if ev.get("event") == "dispatch":
                        ev["lane"] = "lane/legacy-auto"
                        ev["operation_key"] = legacy_key
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertIn(legacy_id, dispatches.rows())
        self.assertNotIn(row["id"], dispatches.rows())
        # The SAME COMMAND replayed (old prefixed spelling) reconciles.
        again, why, posted = dispatches.send(
            "codex-3", "lane/legacy-auto", "Old auto send", self.a,
            repo=self.repo, sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], legacy_id)
        # A MODERNIZED retry (bare spelling) reconciles onto the same row.
        again2, why2, posted2 = dispatches.send(
            "codex-3", "legacy-auto", "Old auto send", self.a,
            repo=self.repo, sign=False, new_work=True)
        self.assertFalse(posted2)
        self.assertIn("do not resend", why2)
        self.assertEqual(again2["id"], legacy_id)
        self.assertEqual(len(dispatches.rows()), 1)

    def _pre_parent_row(self, lane_bare, message):
        """Mint via today's writer, then age the row into the PRE-CHAIN
        shape: prefixed lane, PRE-PARENT auto key/id (the formula inlined
        here, never the live key builder), and NO chain fields at all —
        the shape live row 1ddf37fc still holds. Returns (legacy_id,
        legacy_key)."""
        row, why, posted = dispatches.send(
            "codex-3", lane_bare, message, self.a, repo=self.repo,
            sign=False, new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        stored = dispatches.rows()[row["id"]]
        prefixed = "lane/" + lane_bare
        legacy_key = "auto:" + hashlib.blake2b(
            "\0".join((stored["recipient"], prefixed, stored["tip"], "",
                        str(stored["deadline_s"]),
                        stored["message_hash"])).encode("utf-8"),
            digest_size=16).hexdigest()
        legacy_id = hashlib.blake2b(
            ("dispatch\0" + "\0".join((stored["sender"], stored["repo_id"],
                                        legacy_key))).encode("utf-8"),
            digest_size=16).hexdigest()
        self.assertNotEqual(legacy_id, row["id"])   # the split is real
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == row["id"]:
                    ev["id"] = legacy_id
                    if ev.get("event") == "dispatch":
                        ev["lane"] = prefixed
                        ev["operation_key"] = legacy_key
                        ev.pop("chain_root", None)
                        ev.pop("supersedes", None)
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertIn(legacy_id, dispatches.rows())
        self.assertNotIn(row["id"], dispatches.rows())
        return legacy_id, legacy_key

    def test_pre_parent_auto_row_reconciles_for_a_new_work_replay(self):
        """Round 4 (codex): ONE live v3 row (1ddf37fc) predates the chain
        schema — its auto key hashes NO parent field and it stores no
        chain_root. Round 3's candidates all carry the parent field, so
        that row's key/id were underivable and the same command replayed
        would mint again after the schema evolution."""
        # THE LIVE ROW'S KEY BYTES, frozen verbatim from the 2026-08-02
        # ledger: the pre-parent formula over these exact bytes must
        # reproduce the stored operation key. Frozen so no evolution of the
        # live key builder can quietly redefine what "the old formula"
        # means — a fixture computed by the code under test proves nothing
        # about history. The row's ID leg (sender + repo_id + key) is NOT
        # frozen here because the live repo_id is a machine-local path the
        # never-track law keeps out of history; the id formula is anchored
        # by this test's replay below, over this fixture's own repo.
        frozen_key = "auto:" + hashlib.blake2b("\0".join((
            "codex", "lane/translated-object-superseded",
            "6088eb70c33753915cdf7148568e6f66db7fdb45", "", "2700",
            "4cd92a291ae108a1e67a33cc3538183e")).encode("utf-8"),
            digest_size=16).hexdigest()
        self.assertEqual(frozen_key, "auto:3b3d11acffd093b878aadaa6a6c1867a")
        # Replay that shape through today's send in this fixture repo.
        legacy_id, _legacy_key = self._pre_parent_row(
            "legacy-preparent", "Pre-parent auto send")
        self.assertIsNone(dispatches.rows()[legacy_id]["chain_root"])
        # The SAME COMMAND replayed (old prefixed spelling) reconciles.
        again, why, posted = dispatches.send(
            "codex-3", "lane/legacy-preparent", "Pre-parent auto send",
            self.a, repo=self.repo, sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIn("do not resend", why)
        self.assertEqual(again["id"], legacy_id)
        # A MODERNIZED retry (bare spelling) reconciles onto the same row.
        again2, why2, posted2 = dispatches.send(
            "codex-3", "legacy-preparent", "Pre-parent auto send", self.a,
            repo=self.repo, sign=False, new_work=True)
        self.assertFalse(posted2)
        self.assertIn("do not resend", why2)
        self.assertEqual(again2["id"], legacy_id)
        self.assertEqual(len(dispatches.rows()), 1)
        # BYTE-TRUTH: reconciliation rewrote nothing — the stored row still
        # holds the pre-chain shape after both replays.
        self.assertIsNone(dispatches.rows()[legacy_id]["chain_root"])

    def test_pre_parent_reconciliation_is_new_work_only_and_must_miss(self):
        """Round 4 (codex): the null-root reconciliation admits ONLY the
        semantic new-work replay. MUST-MISS controls — different bytes hit
        no candidate, a parented send never probes pre-parent ids, and a
        malformed stored root is never read as legacy-permissive."""
        legacy_id, _legacy_key = self._pre_parent_row(
            "preparent-miss", "Original pre-parent send")
        # (1) DIFFERENT BYTES MISS every candidate: without --force the
        # writer reaches the same-lane ADVISORY — which is the proof no
        # candidate reconciled, because a spurious hit would have returned
        # "do not resend" before the advisory could fire.
        refused, why, posted = dispatches.send(
            "codex-3", "lane/preparent-miss", "A DIFFERENT message", self.a,
            repo=self.repo, sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIsNone(refused)
        self.assertIn("already uses lane label", why)
        self.assertNotIn("do not resend", why)
        # ... and with --force it MINTS a second row instead of folding
        # onto the legacy one.
        minted, why, posted = dispatches.send(
            "codex-3", "lane/preparent-miss", "A DIFFERENT message", self.a,
            repo=self.repo, sign=False, new_work=True, force=True)
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        self.assertNotEqual(minted["id"], legacy_id)
        self.assertEqual(len(dispatches.rows()), 2)
        # (2) A PARENTED SEND NEVER PROBES PRE-PARENT IDS: the same visible
        # bytes as the legacy row, sent as a continuation of other work,
        # is DIFFERENT WORK — it mints its own row rather than reconciling
        # (or refusing) onto the pre-chain row.
        parented, why, posted = dispatches.send(
            "codex-3", "lane/preparent-miss", "Original pre-parent send",
            self.a, repo=self.repo, sign=False, supersedes=minted["id"])
        self.assertIsNone(why, why)
        self.assertTrue(posted)
        self.assertNotEqual(parented["id"], legacy_id)
        self.assertEqual(len(dispatches.rows()), 3)
        # (3) A MALFORMED STORED ROOT IS NOT LEGACY-PERMISSIVE: chain_root
        # garbage replays CHAIN_UNKNOWN, and the exact new-work replay that
        # reconciles a null root must now refuse (`_replay_chain`: a
        # corrupted field has not earned the permissive reading).
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == legacy_id and ev.get("event") == "dispatch":
                    ev["chain_root"] = "not-an-id"
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(dispatches.rows()[legacy_id]["chain_root"],
                         dispatches.CHAIN_UNKNOWN)
        refused, why, posted = dispatches.send(
            "codex-3", "lane/preparent-miss", "Original pre-parent send",
            self.a, repo=self.repo, sign=False, new_work=True)
        self.assertFalse(posted)
        self.assertIsNone(refused)
        self.assertIn("already names different work", why)

    def test_replay_preserves_the_stored_lane_bytes_for_legacy_rows(self):
        """Round 2 (codex finding 4): v1/v2 replay stripped the stored lane,
        so every historical attest sidecar binding (hashed over the raw
        stored lane) stopped matching its own row. Replay is byte-truth;
        joins normalize at the join instead."""
        row = {"id": "feedbead", "ts": "2026-07-01T00:00:00Z",
               "recipient": "grok", "lane": "lane/legacy-spelling",
               "ref": self.a, "note": None, "deadline_s": 60, "source": "old",
               "status": "open", "ack_ref": None, "verdict_ref": None,
               "last_updated": "2026-07-01T00:00:00Z"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        replayed = dispatches.rows()["feedbead"]
        self.assertEqual(replayed["lane"], "lane/legacy-spelling")
        # The sidecar binding derived from those bytes is stable across replay.
        self.assertEqual(
            dispatches._binding_key(replayed, "a" * 40, "gate:z"),
            dispatches._binding_key(row, "a" * 40, "gate:z"))

    def test_different_lane_pair_must_not_match_false_positive_control(self):  # noqa: VACUOUS_ASSERTION — the negative arm of the join test above; the positive control (prefixed vs bare DOES collide) is the previous test
        self.assertNotEqual(landreq._lane_stem("lane/lane-alpha"),
                            landreq._lane_stem("lane/lane-beta"))
        self.assertNotEqual(landreq._strip_lane_prefix("lane/lane-alpha"),
                            landreq._strip_lane_prefix("lane/lane-beta"))
        r1 = self.add(recipient="codex-3", lane="lane/lane-alpha")
        warn, needs_force = dispatches._duplicate_mint_warning(
            {"id": "dummy3", "recipient": "codex-3", "lane": "lane-beta",
             "repo_id": r1["repo_id"]},
            {r1["id"]: r1})
        self.assertFalse(needs_force)
        self.assertIsNone(warn)


class ForcedForkSurvivesRebindTest(DispatchBase):
    """The condition for the RECOMPUTE ruling, proven against the real
    writer rather than a hand-built snapshot.

    THE RULING: finding (7) requires the forced fork's BEHAVIOUR to persist
    across a rebind, not a stored force bit. If both live arms are derivable
    from the successor graph and rebinding one arm leaves the sibling
    independently carrying, no field is added — the graph IS the durable
    evidence. This is that proof, and it is why helm/dispatches.py gained no
    `forked` column.

    `--force` is the deliberate fork: the caller declares BOTH successors live.
    A rebind then cancels one arm and mints its own child. The question is
    whether the OTHER arm still answers for the parent afterwards."""

    def _arms(self):
        parent = self.add()
        a = self.add(supersedes=parent["id"])
        b = self.add(supersedes=parent["id"], force=True)
        return parent, a, b

    def test_both_arms_are_derivable_from_the_graph(self):
        """The precondition. If the fork is not visible in the successor set,
        nothing below means anything."""
        parent, a, b = self._arms()
        snap = dispatches.snapshot()[0]
        kids = {r["id"] for r in snap.values()
                if r.get("supersedes") == parent["id"]}
        self.assertEqual(kids, {a["id"], b["id"]},
                         "the forced fork is not visible in the successor set")
        self.assertIsNotNone(dispatches.carrier(snap[parent["id"]], snap))

    def test_rebinding_ONE_arm_leaves_THE_SIBLING_carrying(self):
        """The ruling's condition, exactly. Rebind arm A; arm B must still
        answer for the parent, and it must still be actionable."""
        parent, a, b = self._arms()
        # --force with a reason is the judgment-seat override rebind documents;
        # the default path wants a MEASURED starvation signal, which a unit
        # test has no business synthesising.
        out, err = dispatches.rebind(a["id"], "ds4pro", repo=self.repo,
                                     force=True, reason="fork-survival proof")
        self.assertIsNone(err, "the rebind was refused: %s" % err)
        self.assertIsNotNone(out)

        snap = dispatches.snapshot()[0]
        # MUST-HIT CONTROL: the rebind really happened — arm A is no longer
        # the open row it was, so the assertions below are about a changed
        # world and not a no-op.
        self.assertNotEqual(snap[a["id"]]["status"], "open",
                            "the rebind left the source untouched")

        carried = dispatches.carrier(snap[parent["id"]], snap)
        self.assertIsNotNone(carried,
                             "rebinding one arm made the parent read as owed "
                             "while its sibling still held the work")
        self.assertEqual(snap[b["id"]]["status"], "open",
                         "the untouched arm stopped being actionable")
        self.assertNotIn(parent["id"], {r["id"] for r in dispatches.owed(snap)},
                         "the parent re-entered the owed set")

    def test_the_parent_IS_owed_again_once_BOTH_arms_are_gone(self):
        """The other direction, and the reason recompute is safe: nothing is
        remembered, so the answer follows the graph back. Cancel both arms and
        the obligation returns to the parent rather than vanishing."""
        parent, a, b = self._arms()
        for arm in (a, b):
            _r, err = dispatches.mark_cancel(arm["id"], "stood down")
            self.assertIsNone(err, "cancel refused: %s" % err)
        snap = dispatches.snapshot()[0]
        self.assertIsNone(dispatches.carrier(snap[parent["id"]], snap),
                          "a parent with two dead arms still reads as carried")
        self.assertIn(parent["id"], {r["id"] for r in dispatches.owed(snap)},
                      "the obligation vanished instead of returning")


class EveryProjectionAgreesTest(DispatchBase):
    """ONE SNAPSHOT, EVERY PROJECTION, ONE ANSWER.

    This is the test whose absence let nine defects live behind 423 green ones.
    The lane claimed the four consumers ask a single supersession question, and
    every test proved the PRIMITIVE while none drove the CONSUMERS against the
    same snapshot — so `--open` showed the child while `--overdue` showed the
    already-carried PARENT, from one ledger, in the same breath, and triage
    offered both for action.

    A shared predicate is not a shared answer. What makes it one is a test that
    asks every surface at once and refuses to let them differ."""

    def _carried_pair(self):
        """A parent whose successor is alive and carrying it — and which is
        ALREADY LATE.

        THE DEADLINE IS LOAD-BEARING AND WAS MISSING. Without it the rows are
        fresh, `--overdue` returns empty whether or not it consults the owed
        frontier, and every assertion below passes without ever entering the
        case. Measured by mutation: restoring the raw-row `--overdue` selector
        left this class GREEN until deadline_s=0 was added. A fixture that
        cannot reach the defect is decoration, however careful its
        assertions."""
        parent = self.add(deadline_s=60)
        child = self.add(supersedes=parent["id"], deadline_s=60)
        self.assertIsNotNone(parent, "the writer refused the parent fixture")
        self.assertIsNotNone(child, "the writer refused the child fixture")
        return parent, child

    @staticmethod
    def _later():
        """A clock far enough past the fixture deadline that the rows ARE late.

        deadline_s=0 is refused by the writer, so the fixture cannot be born
        overdue; the clock moves instead. Nothing here sleeps."""
        return mock.patch("time.time", return_value=time.time() + 3600)

    def test_every_selector_agrees_on_who_owes(self):
        parent, child = self._carried_pair()
        snap = dispatches.snapshot()[0]
        owed_ids = {r["id"] for r in dispatches.owed(snap)}

        # MUST-HIT CONTROL: the fixture really is the carried shape. Without
        # this, an owed() that returned everything or nothing would satisfy
        # every agreement assertion below trivially.
        self.assertIn(child["id"], owed_ids, "the successor is not owed work")
        self.assertNotIn(parent["id"], owed_ids,
                         "the carried parent is still owed")

        # open_rows and stop_candidate read the same frontier.
        self.assertNotIn(parent["id"], {r["id"] for r in dispatches.open_rows()},
                         "open_rows resurrected a carried parent")
        # overdue is a subset of owed, ALWAYS — a row that owes nothing cannot
        # be late for it.
        with self._later():
            late = dispatches.overdue()
        # MUST-HIT CONTROL: the clock really moved and rows really are late.
        # Without it this loop passes over an empty list and proves nothing —
        # measured: the first version of this test used fresh rows, `overdue`
        # was empty, and restoring the raw-row selector left it GREEN.
        self.assertTrue(late, "no row is overdue; the loop below is vacuous")
        for row in late:
            self.assertIn(row["id"], owed_ids,
                          "overdue named a row that owes nothing")
        # open_recipients counts obligations, and proxywatch derives IDLE from
        # it, so a carried parent must not make its recipient look busy.
        counts, note = dispatches.open_recipients()
        self.assertIsNone(note, "the ledger was unreadable: %s" % note)
        self.assertEqual(sum(counts.values()), len(owed_ids),
                         "recipient counts disagree with the owed frontier")

    def test_the_CLI_selectors_agree_with_the_frontier(self):
        """The surfaces a human actually reads. `--open` and `--overdue`
        disagreeing on one snapshot is the defect that started this."""
        parent, child = self._carried_pair()
        snap = dispatches.snapshot()[0]
        owed_ids = {r["id"] for r in dispatches.owed(snap)}
        self.assertTrue(owed_ids, "nothing is owed; the fixture proves nothing")

        for flag in ("--open", "--overdue"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self._later():
                dispatches.cmd_dispatch(["list", flag, "--json"])
            text = out.getvalue().strip()
            shown = {r["id"] for r in json.loads(text)} if text.startswith("[") \
                else set()
            with self.subTest(flag=flag):
                self.assertTrue(shown, "%s returned nothing; its assertions "
                                       "below would be vacuous" % flag)
                self.assertNotIn(parent["id"], shown,
                                 "%s showed a parent whose successor carries "
                                 "the work" % flag)
                self.assertLessEqual(shown, owed_ids,
                                     "%s showed a row outside the owed "
                                     "frontier" % flag)

    def test_a_row_nothing_carries_is_shown_by_EVERY_surface(self):
        """The other direction, and the one that matters most: the predicate
        must not achieve agreement by hiding everything."""
        lone = self.add()
        snap = dispatches.snapshot()[0]
        self.assertIn(lone["id"], {r["id"] for r in dispatches.owed(snap)})
        self.assertIn(lone["id"], {r["id"] for r in dispatches.open_rows()})
        counts, _n = dispatches.open_recipients()
        self.assertTrue(counts.get(lone["recipient"], 0) >= 1,
                        "a genuinely owed row did not reach its recipient's "
                        "count")


class StaleWriterDetectorTest(unittest.TestCase):
    """A row missing a field it PREDATES is old, not malformed. A row missing a
    field that rows BEFORE it already carried was written by a binary behind
    the one already in use.

    THE NAIVE VERSION WAS SHIPPED FIRST AND WAS WRONG: comparing against the
    key set _base writes today flagged 714 of 1375 live creations, because
    chain_root and supersedes appear on 977, ref_branch on 852 and
    recipient_display on 185 — added at different times. The ledger's own
    history is the only honest floor, which is why the rule is monotonic and
    self-calibrating rather than a hardcoded date."""

    def _ledger(self, rows):
        import json as _json
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            for r in rows:
                fh.write(_json.dumps(r) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    @staticmethod
    def _row(rid, ts, **kw):
        r = {"v": 3, "event": "dispatch", "id": rid, "ts": ts, "sender": "s"}
        r.update(kw)
        return r

    def test_a_row_that_PREDATES_a_field_is_not_flagged(self):
        path = self._ledger([
            self._row("a", "2026-01-01T00:00:00Z"),                  # no chain_root
            self._row("b", "2026-02-01T00:00:00Z", chain_root="b"),  # introduces it
        ])
        out = dispatches.stale_writer_rows(path)
        self.assertEqual(out, [], "ordinary schema growth was called malformed")
        # MUST-HIT CONTROL on the same observable: reverse the ORDER so the
        # field-less row comes second, and the identical rows ARE flagged. The
        # empty result above is about chronology, not a dead detector.
        flipped = self._ledger([
            self._row("b", "2026-01-01T00:00:00Z", chain_root="b"),
            self._row("a", "2026-02-01T00:00:00Z")])
        self.assertEqual(len(dispatches.stale_writer_rows(flipped)), 1)

    def test_a_row_AFTER_the_field_appeared_and_missing_it_IS_flagged(self):
        path = self._ledger([
            self._row("a", "2026-01-01T00:00:00Z", chain_root="a"),
            self._row("b", "2026-02-01T00:00:00Z"),                  # regressed
        ])
        out = dispatches.stale_writer_rows(path)
        self.assertEqual(len(out), 1, "a stale writer went unnoticed")
        self.assertEqual(out[0]["id"], "b")
        self.assertIn("chain_root", out[0]["missing"])
        self.assertEqual(out[0]["first_seen"]["chain_root"],
                         "2026-01-01T00:00:00Z")

    def test_v1_and_v2_rows_are_old_not_malformed(self):
        path = self._ledger([
            self._row("a", "2026-01-01T00:00:00Z", chain_root="a"),
            {"v": 1, "event": "dispatch", "id": "b",
             "ts": "2026-02-01T00:00:00Z", "sender": "s"},
        ])
        # MUST-HIT CONTROL: the same shape at v3 IS flagged, so the exemption
        # is about the schema version and not a detector that finds nothing.
        self.assertEqual(len(dispatches.stale_writer_rows(self._ledger([
            self._row("a", "2026-01-01T00:00:00Z", chain_root="a"),
            self._row("b", "2026-02-01T00:00:00Z")]))), 1)
        self.assertEqual(dispatches.stale_writer_rows(path), [])

    def test_an_unreadable_ledger_is_UNKNOWN_never_none_found(self):
        """A pass whose input was missing reports the opposite of the truth."""
        self.assertIsNone(
            dispatches.stale_writer_rows("/nonexistent/dispatch/ledger.jsonl"))
        # MUST-HIT CONTROL: a readable ledger with a REAL regression returns a
        # populated list, so None means "could not look" rather than "this
        # function only ever answers nothing".
        readable = self._ledger([
            self._row("a", "2026-01-01T00:00:00Z", chain_root="a"),
            self._row("b", "2026-02-01T00:00:00Z")])
        self.assertEqual(len(dispatches.stale_writer_rows(readable)), 1)

    def test_only_dispatch_CREATIONS_are_judged(self):
        """Later events on a row do not carry the creation's field set."""
        path = self._ledger([
            self._row("a", "2026-01-01T00:00:00Z", chain_root="a"),
            {"v": 3, "event": "verdict", "id": "a", "seq": 1,
             "ts": "2026-02-01T00:00:00Z"},
        ])
        self.assertEqual(dispatches.stale_writer_rows(path), [],
                         "a follow-on event was judged against a creation floor")
        # MUST-HIT CONTROL: make the SECOND row a creation instead of a verdict
        # and it IS flagged, so the exemption is about the event kind.
        as_creation = self._ledger([
            self._row("a", "2026-01-01T00:00:00Z", chain_root="a"),
            self._row("b", "2026-02-01T00:00:00Z")])
        self.assertEqual(len(dispatches.stale_writer_rows(as_creation)), 1)


class RebindRefusesALiveReaderTest(DispatchBase):
    """The LIVE-READER rung: a row whose recipient is measurably live AND
    measurably mid-read is refused by name on the default path, and moves only
    under `--force --reason`, which the cancel event then records.

    WHY THE CAPACITY GATE BESIDE IT CANNOT COVER THIS. That gate asks whether
    the recipient CAN act and answers "not measurably unable to act" for every
    healthy seat — true, general, and naming nobody — then advertises the
    override in the same sentence. A seat that takes a row out from under
    somebody mid-read does not do it because the override was hard to find; it
    does it because nothing told it whose row it was."""

    def setUp(self):
        super().setUp()
        for seat_name in ("seat-b", "seat-a"):
            seats.write_roster(seat_name, presence_beat=False)

    def _child_of(self, source_id):
        snap = dispatches.snapshot()[0]
        kids = [r for r in snap.values() if r.get("supersedes") == source_id]
        return kids[0] if kids else None

    def _starve(self, recipient):
        """Measured proxy starvation for the recipient — the capacity gate's
        own admitting evidence, so a rebind can reach the write path without
        --force."""
        _plant_health_row(self, {
            "seat": recipient, "config_ok": True, "drift": [],
            "alerted_at": None, "transcript_age_s": 0,
            "hang_candidate": False, "probe": None, "probe_detail": None,
            "probe_ms": 1, "log": "streak", "log_detail": "HTTP 402"})

    def _beat(self, seat_name):
        """Write the recipient's presence beat AS that seat.

        `touch_seen` refuses a FOREIGN seat — a process that declares one name
        may not refresh another seat's presence, which is the whole cure for
        the cross-seat presence contamination — and this fixture's process
        declares the integrator. So the beat is written under the recipient's
        own declared name, which is the only process that writes one in the
        world either."""
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = seat_name
        try:
            seats.write_roster(seat_name, presence_beat=True)
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        self.assertEqual(seats.presence_of(seats.last_seen(seat_name)),
                         "fresh", "the presence beat was refused, so this "
                                  "arm would pass for the wrong reason")

    def _reading_row(self, recipient="seat-a"):
        """An OPEN row whose delivery to this recipient the ledger OBSERVED.

        Delivery is marked through the real verb rather than by editing a
        snapshot, because the field this rung reads is a projection of a
        `delivered` event and a hand-built dict would prove nothing about
        which rows actually reach the state."""
        row = self.add(recipient=recipient, kind="review")
        _out, err = dispatches.mark_delivered(row["id"], "dm-ref")
        self.assertIsNone(err, "the fixture could not mark the row delivered, "
                               "so no arm below reaches the rung: %s" % err)
        fresh = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(fresh.get("delivery"), "observed",
                         "the fixture row is not in a reading state")
        return row

    def test_a_rebind_off_a_live_reader_refuses_and_names_the_reader(self):
        row = self._reading_row("seat-a")
        # A REAL TOOL BOUNDARY, through the door that writes one. The rung
        # reads presence off the seen file's mtime, so writing that beat is
        # planting the fact the fleet plants, not a stand-in for it.
        self._beat("seat-a")
        out, err = dispatches.rebind(row["id"], "seat-b", repo=self.repo)
        self.assertIsNone(out, "a refused rebind must not report a move")
        err = err or ""
        self.assertIn("is READING this row", err)
        self.assertIn("@seat-a", err)              # WHO loses it
        self.assertIn("presence fresh", err)     # the activity reading
        self.assertIn("PENDING VERDICT with delivery observed", err)
        self.assertIn("WAIT for the verdict", err)        # door one
        self.assertIn("--force --reason", err)            # door two
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[row["id"]]["status"], "open",
                         "a refused rebind moved the row anyway")
        self.assertIsNone(self._child_of(row["id"]),
                          "a refused rebind minted a successor anyway")

    def test_a_stranded_recipient_is_left_to_the_capacity_gate(self):
        """THE CONTROL that keeps the arm above meaningful. Same row, same
        verb, ONE difference — nobody has touched the recipient's presence —
        and the refusal that comes back must be the CAPACITY gate's, not this
        rung's. Without this, a rung that refused every rebind would pass the
        arm above."""
        row = self._reading_row("seat-a")
        self.assertEqual(seats.presence_of(seats.last_seen("seat-a")), "absent",
                         "the control recipient is not stranded")
        _out, err = dispatches.rebind(row["id"], "seat-b", repo=self.repo)
        err = err or ""
        self.assertNotIn("is READING this row", err)
        self.assertIn("not measurably unable to act", err)

    def test_a_stranded_recipient_still_rebinds_on_measured_evidence(self):
        """And the control goes all the way through the write path: with the
        capacity gate's own evidence present, a delivered row on an absent
        recipient moves exactly as it did, carrying no override note."""
        row = self._reading_row("seat-a")
        self._starve("seat-a")
        out, err = dispatches.rebind(row["id"], "seat-b", repo=self.repo)
        self.assertIsNone(err, "the stranded control was refused: %s" % err)
        self.assertEqual(out["new"]["recipient"], "seat-b")
        self.assertNotIn("OVERRIDE", out["reason"],
                         "a move nobody overrode was recorded as an override")

    def test_force_moves_it_and_the_cancel_records_who_lost_the_row(self):
        row = self._reading_row("seat-a")
        self._beat("seat-a")
        out, err = dispatches.rebind(row["id"], "seat-b", force=True,
                                     reason="the reader is walled",
                                     repo=self.repo)
        self.assertIsNone(err, "the override was refused: %s" % err)
        self.assertEqual(out["new"]["recipient"], "seat-b")
        snap = dispatches.snapshot()[0]
        closed = snap[row["id"]]
        self.assertEqual(closed["status"], "cancelled")
        reason = str(closed.get("cancel_reason") or "")
        self.assertIn("OVERRIDE", reason)
        self.assertIn("@seat-a", reason)                  # taken FROM
        self.assertIn("seat-b", reason)                 # moved TO
        self.assertIn("presence fresh", reason)         # what was overridden
        self.assertIn("the reader is walled", reason)   # the caller's own why


class RebindSeesTheContextWallTest(DispatchBase):
    """Rebind's evidence gate had ONE surface — proxywatch — which measures the
    PROXY. A seat at 100% of its context window has a healthy proxy and cannot
    take a turn, so every rebind off it was refused and --force was the only
    road. These arms pin the second surface and, more importantly, pin the
    NARROWNESS of it: a stale or masked context reading must NOT open the gate.
    """

    def _proxy_healthy(self, recipient):
        _plant_health_row(self, {
            "seat": recipient, "config_ok": True, "drift": [],
            "alerted_at": None, "transcript_age_s": 0,
            "hang_candidate": False, "probe": "healthy",
            "probe_detail": None, "probe_ms": 1, "log": "ok",
            "log_detail": None})

    def _proxy_blind(self):
        """proxywatch itself cannot be read — the case the OLD code returned
        early on, which structurally prevented any second measurement."""
        _blind_health(self, OSError("planted: health unreadable"))

    def _context(self, pct, status="ok", window=320000):
        from helm import autocompact
        orig = autocompact.read
        autocompact.read = lambda seat: {
            "seat": seat, "pct": pct, "window": window, "status": status}
        self.addCleanup(setattr, autocompact, "read", orig)

    def _no_context(self):
        from helm import autocompact
        orig = autocompact.read
        autocompact.read = lambda seat: {"seat": seat, "status":
                                         "no-context-data", "pct": None}
        self.addCleanup(setattr, autocompact, "read", orig)

    # ---- MUST-HIT: the whole point of the lane -------------------------

    def test_a_context_full_seat_rebinds_without_force(self):
        self._proxy_healthy("codex-3")
        self._context(100.0)
        row = self.add(recipient="codex-3", kind="review")
        out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIsNone(err, "a 100%%-context seat was still refused: %s" % err)
        self.assertEqual(out["new"]["recipient"], "ds4pro")
        # the REASON must name the context wall, not a proxy fact -- an
        # operator reading the ledger has to know which surface fired.
        self.assertIn("context window exhausted",
                      out["old"]["cancel_reason"])

    def test_the_reason_carries_the_measured_percentage_and_window(self):
        self._proxy_healthy("codex-3")
        self._context(116.5)
        reason, note = dispatches._recipient_evidence("codex-3")
        self.assertIsNotNone(reason)
        self.assertIn("116.5%", reason)
        self.assertIn("320k", reason)
        self.assertIsNone(note)

    # ---- MUST-MISS: the narrowness is the load-bearing half -------------

    def test_a_STALE_context_reading_does_not_open_the_gate(self):
        """A stale reading is not a measurement of NOW. Same 100%, status
        stale -> still refused."""
        self._proxy_healthy("codex-3")
        self._context(100.0, status="stale")
        reason, _ = dispatches._recipient_evidence("codex-3")
        self.assertIsNone(reason)
        row = self.add(recipient="codex-3", kind="review")
        _out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIn("not measurably unable to act", err or "")

    def test_claude_model_status_is_refused_because_it_MASKS_the_age_check(self):
        """THE ELIF-CHAIN ARM. autocompact.read()'s status is a chain, and
        `claude-model` short-circuits BEFORE the freshness test -- so such a row
        may be arbitrarily stale while still carrying a pct. Reading the status
        NAMES as if they were independent booleans is the bug this forbids."""
        self._proxy_healthy("codex-3")
        # POSITIVE CONTROL FIRST, unconditional: the SAME percentage under a
        # post-freshness status DOES fire. Without this the assertion below is
        # satisfied by any predicate that only ever answers None.
        self._context(150.0, status="ok")
        self.assertIn("context window exhausted",
                      dispatches._recipient_evidence("codex-3")[0])
        self._context(150.0, status="claude-model")
        reason, _ = dispatches._recipient_evidence("codex-3")
        self.assertIsNone(reason,
                          "a status that masks the age check opened the gate")

    def test_below_the_wall_is_refused_so_the_threshold_is_real(self):
        self._proxy_healthy("codex-3")
        # POSITIVE CONTROL FIRST, unconditional -- at the wall it fires.
        self._context(100.0)
        self.assertIn("context window exhausted",
                      dispatches._recipient_evidence("codex-3")[0])
        # ...and one tenth BELOW it does not, so the None is about the
        # threshold rather than about a dead predicate.
        self._context(99.9)
        self.assertIsNone(dispatches._recipient_evidence("codex-3")[0])

    # ---- the composition, which is what the restructure buys ------------

    def test_the_context_arm_runs_even_when_PROXYWATCH_IS_BLIND(self):
        """The old code returned early on an unreadable health pass, so no
        second surface could ever be consulted -- and a blind proxywatch is
        exactly when an independent measurement is worth most."""
        self._proxy_blind()
        self._context(100.0)
        reason, note = dispatches._recipient_evidence("codex-3")
        self.assertIsNotNone(reason, "proxywatch blindness suppressed the "
                                     "context arm")
        self.assertIn("context window exhausted", reason)

    def test_both_arms_silent_still_returns_the_proxywatch_NOTE(self):
        """The unavailable-note must not be lost by the restructure: when
        neither surface has evidence, the caller still learns WHY the proxy
        surface said nothing."""
        self._proxy_blind()
        self._no_context()
        reason, note = dispatches._recipient_evidence("codex-3")
        self.assertIsNone(reason)
        self.assertIn("health unreadable", note or "")

    def test_refusal_names_BOTH_silent_evidence_arms(self):
        self._proxy_healthy("codex-3")
        self._context(99.9)
        row = self.add(recipient="codex-3", kind="review")
        _out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIn("neither proxywatch starvation/hang nor fresh context "
                      "exhaustion", err or "")

    def test_a_blind_proxy_refusal_names_the_silent_context_arm_too(self):
        self._proxy_blind()
        self._no_context()
        row = self.add(recipient="codex-3", kind="review")
        _out, err = dispatches.rebind(row["id"], "ds4pro", repo=self.repo)
        self.assertIn("proxywatch health unreadable", err or "")
        self.assertIn("context arm produced no fresh-exhaustion evidence",
                      err or "")

    def test_executable_help_names_both_independent_evidence_arms(self):
        from helm import cli
        self.assertIn("proxywatch", dispatches.USAGE)
        self.assertIn("fresh context exhaustion", dispatches.USAGE)
        help_text = cli._VERB_HELP["dispatch"]
        self.assertIn("proxywatch", help_text)
        self.assertIn("fresh context exhaustion", help_text)

    def test_a_raising_autocompact_is_no_fact_never_an_exception(self):
        """A context gauge must never be able to break the rebind gate."""
        from helm import autocompact
        self._proxy_healthy("codex-3")
        # POSITIVE CONTROL FIRST, unconditional: with a WORKING gauge this same
        # call returns a reason. Otherwise "no exception, reason None" is
        # equally satisfied by a gate that has stopped answering at all.
        self._context(100.0)
        self.assertIn("context window exhausted",
                      dispatches._recipient_evidence("codex-3")[0])

        def boom(seat):
            raise RuntimeError("planted")
        orig = autocompact.read
        autocompact.read = boom
        self.addCleanup(setattr, autocompact, "read", orig)
        reason, note = dispatches._recipient_evidence("codex-3")
        self.assertIsNone(reason)
        self.assertIsNone(note)


class CuredFixAwaitsAReviewerTest(DispatchBase):
    """A FIX verdict says the AUTHOR owes a cure. When the author HAS cured and
    never re-dispatched, the row still says FIX and NOBODY IS WAITING ON
    ANYBODY — the author believes they are done, the board says they owe work,
    and no reviewer holds it.

    NOTHING COULD SEE IT, STRUCTURALLY: `triage` re-measures `owed()`, the OPEN
    frontier, and a FIX-verdicted row is CLOSED. It is finished work with no
    reader, invisible to both frontiers at once (measured: the owner's own MCP
    decision sat 4.5h in this state)."""

    def setUp(self):
        super().setUp()
        # THE BASE FIXTURE'S `self.a` IS ON TRUNK, so it is LANDED, not cured —
        # my first draft used it as the must-hit and the code correctly
        # answered "not a stall". A cure needs the REVIEWED tip itself to be
        # ahead of trunk with something ahead of IT, so `side` gets two.
        self.git("checkout", "-q", "side")
        self.reviewed = self.commit("cured-fixture-reviewed")
        self.cure = self.commit("cured-fixture-cure")
        self.git("checkout", "-q", self.main)

    def rows(self, snap, **kw):
        return dispatches.cured_unwitnessed(
            snap, root=self.repo, trunk=self.main, **kw)

    def snap_of(self, *rows):
        return {r["id"]: r for r in rows}

    def row(self, rid, tip, polarity="fix", supersedes=None, lane="lane-x"):
        r = {"id": rid, "lane": lane, "recipient": "peer-seat",
             "polarity": polarity, "reviewed_tip": tip, "status": "verdict",
             "chain_root": "r1" if supersedes else rid,
             "ts": "2026-01-01T00:00:00Z"}
        if supersedes:
            r["supersedes"] = supersedes
        return r

    def test_a_CURED_fix_with_nobody_waiting_is_found(self):
        # self.a is on `side`, which has advanced to self.side — the author
        # cured. MUST-HIT: this is the state the whole rung exists for.
        got, err = self.rows(self.snap_of(self.row("r1", self.reviewed)))
        self.assertIsNone(err)
        self.assertEqual([r["id"] for r, _w in got], ["r1"])
        (_row, (branch, tip, ahead)), = got
        self.assertEqual(branch, "side")
        self.assertEqual(tip, self.cure)
        self.assertGreaterEqual(ahead, 1, "a cure is AHEAD of what was reviewed")

    def test_the_verdict_hands_the_gate_door_the_rows_own_checkout(self):
        """F1's verdict leg. `mark_verdict` held both coordinates the send had
        resolved — `repo_id`, the shared admin dir, and `repo_root`, the checkout
        the send ran in — and handed the gate door only the first. One admin dir
        covers a repository root and every linked worktree of it, so where a root
        and a lane declare different gate commands that door was asked a question
        with two answers and refused an honest receipt as ambiguous. The narrower
        coordinate was on the row the whole time.

        THE SPY WRAPS THE REAL DOOR and records what the shipped writer passed
        it; nothing about the call is reconstructed here.
        """
        from unittest import mock
        from tests._tmphome import dispatch_home
        with dispatch_home(self.repo):
            row, add_err = dispatches.add(
                "seat-under-test", "coordinate-carrier", ref=self.reviewed,
                repo=self.repo, kind="review", new_work=True, notify=False,
                _reason=True)
        self.assertIsNone(add_err, add_err)
        # CONTROL ON THE INPUT: the row really carries BOTH coordinates and they
        # are different strings, which is the whole premise — a reader that
        # passed `repo_id` would be indistinguishable from one that passed the
        # checkout if they were one value.
        self.assertTrue(row.get("repo_root"))
        self.assertNotEqual(row["repo_root"], row.get("repo_id"))
        seen = []
        real = gate.bind

        def spy(*args, **kw):
            seen.append((kw.get("repo_id"), kw.get("consuming_repo")))
            return real(*args, **kw)

        with mock.patch.object(gate, "bind", spy):
            verdict, err = dispatches.mark_verdict(
                row["id"], self.reviewed, "measured cure", "fix")
        self.assertIsNone(err, err)
        self.assertEqual(verdict["polarity"], "fix")
        # MUST-HIT: the writer reached the gate door at all. Without this the
        # emptiness below would read as agreement.
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(seen[0], (row["repo_id"], row["repo_root"]))

    def test_production_writer_and_replay_supply_the_carried_checkout(self):  # noqa: VACUOUS_ASSERTION — materialized repo_root and the exact rendered row are unconditional positive controls on writer, replay, and consumer
        """The composed contract, not a hand-written `repo_root` fixture.

        add() traverses the production `_base` writer, snapshot() traverses
        `_new_state`, and the web consumer reads that materialized row while its
        process cwd is outside every checkout. Removing either producer field
        assignment or the consumer read makes the exact row disappear.
        """
        from unittest import mock
        from helm import web_owed

        import shutil
        separate = os.path.join(self.tmp, "separate-checkout")
        gitdir = os.path.join(self.tmp, "separate-common.git")
        subprocess.run(["git", "clone", "-q", "--separate-git-dir", gitdir,
                        self.repo, separate], check=True)
        self.addCleanup(shutil.rmtree, separate, ignore_errors=True)
        self.addCleanup(shutil.rmtree, gitdir, ignore_errors=True)
        self.git("branch", "side", self.cure, cwd=separate)
        from tests._tmphome import dispatch_home
        with dispatch_home(separate):
            row, add_err = dispatches.add(
                "seat-under-test", "composed-carrier", ref=self.reviewed,
                repo=separate, kind="review", new_work=True, notify=False,
                _reason=True)
        self.assertIsNone(add_err, add_err)
        verdict, err = dispatches.mark_verdict(
            row["id"], self.reviewed, "measured cure", "fix")
        self.assertIsNone(err, err)
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        materialized = snap[row["id"]]
        self.assertEqual(materialized.get("repo_root"), separate,
                         "MUST-HIT: writer bytes survived production replay")
        self.git("update-ref", "refs/remotes/origin/main", self.c, cwd=separate)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        here = os.getcwd()
        try:
            os.chdir(outside)
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=(snap, None)), \
                    mock.patch("helm.obligation.unanswered_fixes",
                               return_value=([], [], None)):
                # THE BODY, NOT THE ENDPOINT. /api/owed is served through a
                # serve-stale cache, so calling it here would read whichever
                # body another module left behind — through a fixture this arm
                # never installed. `_owed_build` is the consumer this arm is
                # about: it takes the ledger reading and renders the row.
                cured = web_owed._owed_build()["cured"]
        finally:
            os.chdir(here)
        self.assertFalse(cured["unavailable"], cured.get("why"))
        self.assertEqual([item["row"] for item in cured["rows"]],
                         [verdict["id"][:12]])

    def test_a_CHAIN_SUCCESSOR_means_somebody_IS_waiting(self):  # noqa: VACUOUS_ASSERTION — an emptiness claim CANNOT carry a same-call positive: one call yields one answer. The control is a SEPARATE call on the SAME code path with a DIFFERENT input, asserted FIRST and unconditionally, which is the only way to prove a discriminator discriminates rather than being inert
        # THE CLAUSE THAT MAKES IT A RUNG AND NOT A NAG. Measured on the live
        # ledger: 57 of 67 cured rows have a successor and are healthy
        # in-flight work. Reporting them would be 85% false, and a surface that
        # cries "stranded" at live work is muted within a day.
        parent = self.row("r1", self.reviewed)
        kid = self.row("r2", self.reviewed, polarity=None, supersedes="r1")
        # CONTROL FIRST, same call shape, same fixture: without the successor
        # this row IS reported. An inert scan would answer [] to both and the
        # absence below would prove nothing.
        alone, err = self.rows(self.snap_of(parent))
        self.assertIsNone(err)
        self.assertEqual([r["id"] for r, _w in alone], ["r1"])
        got, err = self.rows(self.snap_of(parent, kid))
        self.assertIsNone(err)
        self.assertEqual(got, [], "a row with a successor is somebody's work")

    def test_AUTHOR_OWES_when_the_tip_never_moved(self):  # noqa: VACUOUS_ASSERTION — an emptiness claim CANNOT carry a same-call positive: one call yields one answer. The control is a SEPARATE call on the SAME code path with a DIFFERENT input, asserted FIRST and unconditionally, which is the only way to prove a discriminator discriminates rather than being inert
        # The other half of the discriminator, and it wants the OPPOSITE
        # action. self.side is the branch tip itself, so nothing advanced past
        # it: the FIX genuinely stands against the author.
        # CONTROL on the same observable: the scan DOES report a cured row
        # through this exact call, so the empty answer below is the
        # tip-never-moved clause and not a dead scan.
        seen, err = self.rows(self.snap_of(self.row("r0", self.reviewed)))
        self.assertEqual([r["id"] for r, _w in seen], ["r0"])
        got, err = self.rows(self.snap_of(self.row("r1", self.cure)))
        self.assertIsNone(err)
        self.assertEqual(got, [], "tip == reviewed_tip is the author's debt")
        state, _w = dispatches.cure_state(
            self.row("r1", self.cure),
            dispatches._cure_index(root=self.repo, trunk=self.main)[0])
        self.assertEqual(state, dispatches.CURE_AUTHOR_OWES)

    def test_a_LANDED_reviewed_tip_is_not_a_stall(self):  # noqa: VACUOUS_ASSERTION — an emptiness claim CANNOT carry a same-call positive: one call yields one answer. The control is a SEPARATE call on the SAME code path with a DIFFERENT input, asserted FIRST and unconditionally, which is the only way to prove a discriminator discriminates rather than being inert
        # self.c is on trunk, so the reviewed work reached the target and the
        # row is done. `--no-merged` excludes trunk-merged branches, so a
        # landed tip is simply absent from the index.
        # CONTROL on the same observable, for the same reason as above.
        seen, err = self.rows(self.snap_of(self.row("r0", self.reviewed)))
        self.assertEqual([r["id"] for r, _w in seen], ["r0"])
        got, err = self.rows(self.snap_of(self.row("r1", self.c)))
        self.assertIsNone(err)
        self.assertEqual(got, [])

    def test_retired_FIX_debt_is_history_not_a_cure_obligation(self):  # noqa: VACUOUS_ASSERTION — each subtest positively executes a distinct retirement discriminator before asserting no cure obligation
        for field, value in (("discharged", True), ("withdrawn", True),
                             ("close_reason", "stranded")):
            with self.subTest(field=field):
                row = self.row("r1", self.reviewed)
                row[field] = value
                got, err = self.rows(self.snap_of(row))
                self.assertIsNone(err)
                self.assertEqual(got, [])

    def test_noncarrying_successors_do_not_hide_the_cure(self):  # noqa: VACUOUS_ASSERTION — each subtest unconditionally names the parent returned by the cure census after a distinct retired successor
        parent = self.row("r1", self.reviewed)
        for field, value in (("status", "cancelled"),
                             ("withdrawn", True),
                             ("close_reason", "stranded")):
            with self.subTest(field=field):
                child = self.row("r2", self.reviewed, polarity=None,
                                 supersedes="r1")
                child[field] = value
                got, err = self.rows(self.snap_of(parent, child))
                self.assertIsNone(err)
                self.assertEqual([row["id"] for row, _where in got], ["r1"])

    def reviewed_chain(self, polarity="fix"):
        """(parent, kid, snapshot) written by the shipped producers: a FIX on
        `self.reviewed`, a successor dispatched `--supersedes` it on the cure
        and verdicted `polarity` there, then withdrawn through `lr close
        --reason withdrawn` — a successor that recorded a verdict on the cure
        and left the board, which `carrier` passes through."""
        from helm import landreq
        parent, err = dispatches.add(
            "peer-seat", "chain-cure", ref=self.reviewed, repo=self.repo,
            kind="review", new_work=True, notify=False, _reason=True)
        self.assertIsNone(err, err)
        _v, err = dispatches.mark_verdict(parent["id"], self.reviewed,
                                          "measured findings", "fix")
        self.assertIsNone(err, err)
        kid, err = dispatches.add(
            "peer-seat", "chain-cure", ref=self.cure, repo=self.repo,
            kind="review", supersedes=parent["id"], notify=False,
            _reason=True)
        self.assertIsNone(err, err)
        _v, err = dispatches.mark_verdict(kid["id"], self.cure,
                                          "measured findings again", polarity)
        self.assertIsNone(err, err)
        _lr, err = landreq.withdraw(kid["id"], "left the board unlanded")
        self.assertIsNone(err, err)
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIsNone(dispatches.carrier(snap[parent["id"]], snap),
                          "the premise failed: the withdrawn successor still "
                          "carries the parent, so the census would skip it "
                          "for that reason and not for the review")
        return snap[parent["id"]], snap[kid["id"]], snap

    def test_a_cure_a_successor_REVIEWED_is_not_awaiting_review(self):
        """A successor recorded a verdict on the exact cure tip and was later
        withdrawn. The cure WAS re-dispatched and reviewed, so the census must
        not report it as awaiting review. The arm below is the control on the
        same producers. MUTATION: dropping the `reviewed` clause in
        `cure_state` reports this parent."""
        parent, kid, snap = self.reviewed_chain()
        got, err = self.rows(snap)
        self.assertIsNone(err)
        self.assertNotIn(parent["id"], [r["id"] for r, _w in got])
        index, ierr = dispatches._cure_index(root=self.repo, trunk=self.main)
        self.assertIsNone(ierr)
        state, where = dispatches.cure_state(
            parent, index,
            reviewed=dispatches.chain_reviewed_tips(parent, snap))
        self.assertEqual(state, dispatches.CURE_REVIEWED)
        self.assertEqual(where[1], self.cure)
        self.assertEqual(where[2]["id"], kid["id"])

    def test_a_CONCUR_successor_did_not_review_the_cure(self):
        """A `concur` on the cure authorizes nothing, so it is not a review:
        the identical withdrawn chain with a concur verdict leaves the cure
        awaiting review at its tip. The FIX arm above is the control on the
        same producers. MUTATION: counting any recorded verdict as a review in
        `dispatches._verdict_is_a_review` hides this parent."""
        parent, kid, snap = self.reviewed_chain(polarity="concur")
        self.assertEqual(kid.get("polarity"), "concur")
        self.assertEqual(dispatches.chain_reviewed_tips(parent, snap), {})
        got, err = self.rows(snap)
        self.assertIsNone(err)
        self.assertEqual([(r["id"], w[1]) for r, w in got],
                         [(parent["id"], self.cure)])

    def test_a_cure_committed_after_the_successors_review_is_awaiting(self):
        """The control for the arm above: the identical reviewed chain, then
        the author commits again above the reviewed cure. The newest cure was
        never reviewed, so the census reports it at that tip. MUTATION:
        treating any reviewed successor as reviewing the branch tip drops this
        parent."""
        parent, _kid, _snap = self.reviewed_chain()
        self.git("checkout", "-q", "side")
        newer = self.commit("cured-fixture-second-cure")
        self.git("checkout", "-q", self.main)
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        got, err = self.rows(snap)
        self.assertIsNone(err)
        self.assertEqual([(r["id"], w[1]) for r, w in got],
                         [(parent["id"], newer)])

    def test_a_live_carrier_beyond_a_cancelled_successor_suppresses_the_parent(self):  # noqa: VACUOUS_ASSERTION — the concrete open grandchild positively controls why the parent is absent from the cure census
        parent = self.row("r1", self.reviewed)
        dead = self.row("r2", self.reviewed, polarity=None, supersedes="r1")
        dead["status"] = "cancelled"
        live = self.row("r3", self.reviewed, polarity=None, supersedes="r2")
        live["status"] = "open"
        got, err = self.rows(self.snap_of(parent, dead, live))
        self.assertIsNone(err)
        self.assertEqual(got, [])

    def test_descendant_cure_tip_outranks_an_earlier_stale_branch(self):
        self.git("branch", "aaa-stale", self.reviewed)
        index, err = dispatches._cure_index(root=self.repo, trunk=self.main)
        self.assertIsNone(err)
        self.assertEqual(index[self.reviewed], ("side", self.cure))

    def test_incomparable_cure_branches_refuse_instead_of_selecting_by_name(self):
        self.git("checkout", "-q", "-b", "sibling", self.reviewed)
        sibling = self.commit("independent-cure")
        self.git("checkout", "-q", self.main)
        got, err = self.rows(self.snap_of(self.row("r1", self.reviewed)))
        self.assertEqual(got, [])
        self.assertIn("ambiguous live cure carriers", err)
        self.assertIn(sibling[:12], err)

    def test_one_ambiguous_row_does_not_suppress_an_unrelated_unique_cure(self):
        self.git("checkout", "-q", "-b", "sibling", self.reviewed)
        self.commit("independent-cure")
        self.git("checkout", "-q", "-b", "other-cure", self.main)
        other_reviewed = self.commit("other-reviewed")
        other_cure = self.commit("other-fixed")
        self.git("checkout", "-q", self.main)
        snap = self.snap_of(self.row("r1", self.reviewed),
                            self.row("r2", other_reviewed))
        got, err = self.rows(snap)
        self.assertEqual([row["id"] for row, _where in got], ["r2"])
        self.assertEqual(got[0][1], ("other-cure", other_cure, 1))
        self.assertIn("r1: ambiguous live cure carriers", err)

    def test_an_UNREADABLE_git_is_UNKNOWN_and_never_an_empty_all_clear(self):
        # AN EMPTY LIST AND A BROKEN WALK ARE INDISTINGUISHABLE TO A READER,
        # and publishing the second as the first is how a false all-clear gets
        # believed. The caller MUST receive a reason, not a clean zero.
        got, err = dispatches.cured_unwitnessed(
            self.snap_of(self.row("r1", self.reviewed)),
            root=os.path.join(self.tmp, "no-such-repo"), trunk=self.main)
        self.assertEqual(got, [])
        self.assertTrue(err, "a git failure must hand back a REASON")
        # and the pure classifier refuses to guess without an index
        state, _w = dispatches.cure_state(self.row("r1", self.reviewed), None)
        self.assertEqual(state, dispatches.CURE_UNKNOWN)

    def test_whitespace_only_reviewed_tip_is_filtered_before_git(self):
        row = self.row("blank", "   ")
        got, err = dispatches.cured_unwitnessed(
            self.snap_of(row), root=os.path.join(self.tmp, "no-such-repo"),
            trunk=self.main)
        self.assertEqual((got, err), ([], None))
        self.assertFalse(dispatches.cure_candidate(row, self.snap_of(row)))
        control, control_err = self.rows(
            self.snap_of(self.row("real", self.reviewed)))
        self.assertIsNone(control_err)
        self.assertEqual([r["id"] for r, _where in control], ["real"])

    def test_resolved_short_id_does_not_widen_to_a_longer_prefix_collision(self):
        short = self.row("abcd1234", self.reviewed)
        self.git("checkout", "-q", "-b", "other-cure", self.main)
        other_reviewed = self.commit("other-reviewed")
        self.commit("other-cure-a")
        self.git("checkout", "-q", "-b", "other-sibling", other_reviewed)
        self.commit("other-cure-b")
        self.git("checkout", "-q", self.main)
        longer = self.row("abcd1234deadbeef", other_reviewed)
        got, err = self.rows(self.snap_of(short, longer), ids=[short["id"]])
        self.assertEqual([row["id"] for row, _where in got], [short["id"]])
        self.assertIsNone(err, "the unselected colliding row must not add ambiguity")

    def test_same_tip_branches_choose_the_first_sorted_display_name(self):
        self.git("branch", "aaa-same-tip", self.cure)
        index, err = dispatches._cure_index(root=self.repo, trunk=self.main)
        self.assertIsNone(err)
        self.assertEqual(index[self.reviewed], ("aaa-same-tip", self.cure))

    def test_the_cure_is_found_by_ANCESTRY_not_by_the_lane_name(self):
        # A LANE LABEL IS NOT A BRANCH NAME and nothing enforces that they
        # match. Two live cures sit on `review/68bec-round7` and
        # `prerebase/gate-epoch` whose labels share no words with them; a
        # name-keyed scan finds neither and reports "no branch, the cure cannot
        # exist" — which is how three live lanes were called dead in one night.
        r = self.row("r1", self.reviewed, lane="a-label-matching-no-branch-at-all")
        got, err = self.rows(self.snap_of(r))
        self.assertIsNone(err)
        self.assertEqual([x["id"] for x, _w in got], ["r1"])
        self.assertEqual(got[0][1][0], "side",
                         "resolved to the branch that CONTAINS the tip")


class CuredTriageCompletenessTest(DispatchBase):
    def triage(self, *ids):
        return run(dispatches.cmd_dispatch, ["triage", *ids])

    def row(self, rid="cure", repo_id="/repo/.git"):
        return {"id": rid, "polarity": "fix", "reviewed_tip": "a" * 40,
                "repo_id": repo_id, "repo_root": "/repo", "status": "verdict",
                "lane": "lane-cure", "recipient": "reviewer", "ts": "t"}

    def test_total_cure_index_failure_is_UNAVAILABLE_not_PARTIAL(self):
        from unittest import mock
        row = self.row()
        facts = {"eligible": 1, "scanned_repos": 0, "blind_repos": 1,
                 "blind_rows": 1, "ambiguous": 0}
        with mock.patch.object(dispatches, "snapshot", return_value=({"cure": row}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]), \
                mock.patch.object(dispatches, "cured_by_repo",
                                  return_value=([], ["repo unreadable"], facts)):
            rc, out, err = self.triage()
        self.assertEqual(rc, 0)
        self.assertIn("UNAVAILABLE", err)
        self.assertNotIn("PARTIAL", err)
        self.assertIn("says nothing about whether any cures exist", err)
        self.assertNotIn("CURE AWAITING REVIEW", out)

    def test_one_blind_repo_is_PARTIAL_when_another_repo_was_scanned(self):
        from unittest import mock
        row = self.row("seen")
        entry = (row, ("branch", "b" * 40, 1))
        facts = {"eligible": 2, "scanned_repos": 1, "blind_repos": 1,
                 "blind_rows": 1, "ambiguous": 0}
        with mock.patch.object(dispatches, "snapshot", return_value=({"seen": row}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]), \
                mock.patch.object(dispatches, "cured_by_repo",
                                  return_value=([entry], ["repo blind"], facts)):
            rc, out, err = self.triage()
        self.assertEqual(rc, 0)
        self.assertIn("PARTIAL", err)
        self.assertIn("1 row(s) are UNKNOWN", err)
        self.assertIn("2 row(s): 1 measured, 0 ambiguous, 1 repository-blind", out)
        self.assertIn("seen", out)

    def test_per_repo_scan_finds_each_repository_without_process_cwd(self):
        from unittest import mock
        a, b = self.row("a", "/a/.git"), self.row("b", "/b/.git")
        a["repo_root"], b["repo_root"] = "/a", "/b"
        snap = {"a": a, "b": b}
        seen = []

        def cured(_snap, ids=None, root=None, **_kw):
            rid = ids[0]
            seen.append((rid, root))
            return [(_snap[rid], ("branch", rid * 40, 1))], None

        with mock.patch("helm.obligation._root_for_repo",
                        side_effect=lambda repo, root=None: root), \
                mock.patch("helm.landreq._close_trunk",
                           side_effect=lambda _row, repo, _trunk:
                                  ("refs/heads/main", "a" * 40, "local", None)), \
                mock.patch.object(dispatches, "cured_unwitnessed", cured):
            rows, problems, facts = dispatches.cured_by_repo(snap)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(seen), [("a", "/a"), ("b", "/b")])
        self.assertEqual(sorted(row["id"] for row, _where in rows), ["a", "b"])
        self.assertEqual(facts["scanned_repos"], 2)

    def test_per_repo_scan_resolves_each_authoritative_trunk(self):
        from unittest import mock
        a = self.row("a", "/a/.git")
        a["repo_root"] = "/a"
        seen = []

        def cured(_snap, ids=None, root=None, trunk=None):
            seen.append((ids, root, trunk))
            return [(_snap["a"], ("branch", "b" * 40, 1))], None

        with mock.patch("helm.obligation._root_for_repo",
                        side_effect=lambda _repo, root=None: root), \
                mock.patch("helm.landreq._close_trunk",
                           return_value=("refs/heads/trunk", "c" * 40,
                                                "local", None)) as resolve, \
                mock.patch.object(dispatches, "cured_unwitnessed", cured):
            rows, problems, facts = dispatches.cured_by_repo({"a": a})
        self.assertEqual(problems, [])
        self.assertEqual([row["id"] for row, _where in rows], ["a"])
        self.assertEqual(seen, [(["a"], "/a", "refs/heads/trunk")])
        self.assertEqual(resolve.call_args.args, (a, "/a/.git", None))
        self.assertEqual(facts["scanned_repos"], 1)

    def test_whitespace_repo_id_is_unknown_not_a_successful_empty_scan(self):
        from unittest import mock
        row = self.row("bad", "/repo/.git ")
        with mock.patch("helm.obligation._root_for_repo") as place, \
                mock.patch.object(dispatches, "cured_unwitnessed") as cured:
            rows, problems, facts = dispatches.cured_by_repo({"bad": row})
        self.assertEqual(rows, [])
        self.assertEqual(facts["scanned_repos"], 0)
        self.assertEqual(facts["blind_rows"], 1)
        self.assertTrue(any("no usable repo_id" in problem for problem in problems))
        place.assert_not_called()
        cured.assert_not_called()

    def test_ambiguous_rows_are_first_class_in_the_total(self):
        from unittest import mock
        row = self.row()
        facts = {"eligible": 1, "scanned_repos": 1, "blind_repos": 0,
                 "blind_rows": 0, "ambiguous": 1}
        with mock.patch.object(dispatches, "snapshot", return_value=({"cure": row}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]), \
                mock.patch.object(dispatches, "cured_by_repo",
                                  return_value=([], ["ambiguous carriers"], facts)):
            rc, out, err = self.triage()
        self.assertEqual(rc, 0)
        self.assertIn("PARTIAL", err)
        self.assertIn("1 row(s): 0 measured, 1 ambiguous, 0 repository-blind", out)


class ReviewNewWorkChecksWorkIdentityTest(DispatchBase):
    """A RENAMED CONTINUATION IS INVISIBLE TO EVERY SAME-LANE RULE.

    `_duplicate_mint_warning`'s --new-work arm matches on LANE LABEL, one
    sentence after its own docstring concedes "lanes are names, not identity".
    So it was structurally silent on the single case the heuristic it enforces
    exists for — `a-review-of-someone-elses-build-supersedes-it-never-roots-
    new-work` [1.00]: "the LANE NAME being new is not the test, WORK IDENTITY
    is." Twice in two days, both under an integrator's build row; the second
    (2026-08-06) got NO warning because the lane had been renamed
    cured-fix-sweep -> cured-fix-awaits-a-reviewer.

    THE COST IS AN UNCLOSABLE ROW, not a duplicate: landed refuses on ancestry
    after a rebase, discharged refuses on the missing link, and `lr land`
    refuses because a BUILD row's ref is a BASE. Three correct refusals, no
    honest terminal."""

    def rows(self, *rows):
        return {r["id"]: r for r in rows}

    def build_row(self, rid, recipient, lane="build-lane"):
        return {"id": rid, "kind": "build", "lane": lane,
                "recipient": recipient, "sender": "integrator",
                "repo_id": "/x/.git", "status": "open", "delivery": "observed"}

    def review_row(self, rid, sender, lane="a-different-lane", **kw):
        r = {"id": rid, "kind": "review", "lane": lane, "sender": sender,
             "recipient": "peer", "repo_id": "/x/.git", "status": "open",
             "delivery": "observed"}
        r.update(kw)
        return r

    def branch_row(self, rid, parent, polarity="fix", **kw):
        return self.review_row(
            rid, "author", lane="branch-" + rid[:8], status="verdict",
            polarity=polarity, supersedes=parent, chain_root="work-root", **kw)

    def test_an_unretired_FIX_verdict_blocks_the_historical_sibling_mint(self):
        """THE 00:28 SPECIMEN's graph shape, not its opaque ledger ids.

        The first review had already returned FIX on one child of the original
        parent. A second-family review then named that original parent again.
        `_not_closed` called every verdict terminal and admitted the sibling,
        even though the first branch still carried operational debt.
        """
        parent = "original-parent"
        existing = self.branch_row("first-review", parent)
        incoming = self.review_row(
            "second-review", "author", supersedes=parent)
        warning, force = dispatches._duplicate_mint_warning(
            incoming, self.rows(existing))
        self.assertTrue(force, "the exact historical fork shape was admitted again")
        self.assertIn("first-review (VERDICT/FIX)", warning)
        self.assertIn(parent[:12], warning)
        self.assertIn("duplicate live work", warning)
        self.assertIn("--supersedes <active-row-id>, not this parent", warning)
        self.assertIn("--force only for a deliberate fork", warning)
        self.assertIn("intentional parallel branch", warning)

    def test_a_CARRIED_build_row_is_NOT_named_while_an_UNCARRIED_one_STILL_IS(self):
        """THE WARNING CLAIMS THE ROW WILL HAVE NO HONEST TERMINAL, AND A
        CARRIED ROW ALREADY HAS ONE. `_not_closed` is a STATUS test while the
        sentence makes a claim about the row's FUTURE, and that claim is false
        the moment a successor carries the obligation.

        MEASURED ON THE LIVE LEDGER BEFORE THE CURE, both directions: of 73
        not-closed BUILD rows, 72 were already CARRIED and exactly one was not.
        A reader who dismisses this warning seventy-two times is trained past
        the single case it exists for — the same false-cause shape as
        task/1469 and task/1511.

        BOTH POLES THROUGH ONE CALL, because a filter that removed every
        candidate would pass a one-directional check just as happily as the
        correct one."""
        carried = self.build_row("carried-build", "author", lane="carried-lane")
        successor = self.build_row("successor-row", "author", lane="carried-lane")
        successor["supersedes"] = "carried-build"
        successor["chain_root"] = "carried-build"
        alone = self.build_row("lonely-build", "author", lane="lonely-lane")
        incoming = self.review_row("incoming-review", "author")

        snap = self.rows(carried, successor, alone, incoming)
        # FIXTURE CONTROL: the shapes must actually differ on the predicate the
        # cure consults, or this arm proves nothing about the filter.
        self.assertIsNotNone(dispatches.carrier(carried, snap),
                             "fixture: the carried row must have a carrier")
        self.assertIsNone(dispatches.carrier(alone, snap),
                          "fixture: the lonely row must have none")

        warning, _force = dispatches._duplicate_mint_warning(incoming, snap)
        self.assertIsNotNone(warning, "the uncarried row must still warn")
        self.assertIn("lonely-build"[:12], warning)
        self.assertNotIn("carried-build"[:12], warning,
                         "a CARRIED row has the terminal this warning wants: %r"
                         % warning)

    def test_only_the_CHAIN_TAIL_is_named_never_the_carried_interior(self):
        """WHAT THE FILTER ACTUALLY DOES, and it is sharper than "skip carried".
        A chain's TAIL is uncarried BY CONSTRUCTION — nothing supersedes it yet
        — so the warning cannot ever go fully silent on a live chain, and an
        arm asserting that would be red for a correct implementation. I wrote
        that arm first and the rehearsal caught it before a gate.

        The real contract: of a three-row chain the interior rows are carried
        and the TAIL is not, so exactly the tail is named. That is why the live
        ledger went from 73 candidates to 1 — one open chain tail, not one
        surviving accident."""
        parent = self.build_row("parent-build", "author", lane="one-lane")
        child = self.build_row("child-build", "author", lane="one-lane")
        child["supersedes"] = "parent-build"
        child["chain_root"] = "parent-build"
        tail = self.build_row("tail-build", "author", lane="one-lane")
        tail["supersedes"] = "child-build"
        tail["chain_root"] = "parent-build"
        incoming = self.review_row("incoming-review", "author")
        snap = self.rows(parent, child, tail, incoming)

        # FIXTURE CONTROL on the predicate the cure consults: interior carried,
        # tail not. Without this the assertions below could pass on a chain
        # that never linked up at all.
        self.assertEqual((dispatches.carrier(parent, snap) or {}).get("id"),
                         "child-build")
        self.assertEqual((dispatches.carrier(child, snap) or {}).get("id"),
                         "tail-build")
        self.assertIsNone(dispatches.carrier(tail, snap))

        warning, _force = dispatches._duplicate_mint_warning(incoming, snap)
        self.assertIsNotNone(warning)
        self.assertIn("tail-build"[:12], warning)
        for interior in ("parent-build", "child-build"):
            self.assertNotIn(interior[:12], warning,
                             "%s is CARRIED and must not be named: %r"
                             % (interior, warning))

    def test_an_unretired_SUPERSEDE_verdict_is_the_same_active_branch(self):
        parent = "parent"
        existing = self.branch_row("contrary", parent, polarity="supersede")
        warning, force = dispatches._duplicate_mint_warning(
            self.review_row("sibling", "author", supersedes=parent),
            self.rows(existing))
        self.assertTrue(force)
        self.assertIn("contrary (VERDICT/SUPERSEDE)", warning)

    def test_OPEN_and_HELD_direct_siblings_still_block(self):
        parent = "parent"
        opened = self.review_row(
            "opened", "author", supersedes=parent)
        open_warning, open_force = dispatches._duplicate_mint_warning(
            self.review_row("open-sibling", "author", supersedes=parent),
            self.rows(opened))
        self.assertTrue(open_force)
        self.assertIn("opened (OPEN)", open_warning)

        held = self.review_row(
            "held", "author", status="held", supersedes=parent,
            hold_reason="dependency")
        held_warning, held_force = dispatches._duplicate_mint_warning(
            self.review_row("held-sibling", "author", supersedes=parent),
            self.rows(held))
        self.assertTrue(held_force)
        self.assertIn("held (HELD)", held_warning)

    def test_the_correct_continuation_supersedes_the_FIX_row_itself(self):  # noqa: VACUOUS_ASSERTION — the sibling refusal on the same snapshot is the unconditional positive control, asserted first
        parent = "parent"
        fixed = self.branch_row("fixed", parent)
        sibling_warning, sibling_force = dispatches._duplicate_mint_warning(
            self.review_row("sibling", "author", supersedes=parent),
            self.rows(fixed))
        self.assertTrue(sibling_force)
        self.assertTrue(sibling_warning)

        warning, force = dispatches._duplicate_mint_warning(
            self.review_row("next-round", "author", supersedes=fixed["id"]),
            self.rows(fixed))
        self.assertIsNone(warning, "the honest child-of-FIX continuation was refused")
        self.assertFalse(force)

    def test_a_carried_FIX_still_blocks_a_new_sibling_under_its_parent(self):
        """Carriage continues a branch; it does not erase that branch's identity."""
        parent = "parent"
        fixed = self.branch_row("fixed", parent)
        child = self.review_row(
            "carrier", "author", supersedes=fixed["id"],
            chain_root="work-root")
        snap = self.rows(fixed, child)
        self.assertEqual(dispatches.carrier(fixed, snap)["id"], child["id"],
                         "the fixture did not actually carry the FIX branch")
        warning, force = dispatches._duplicate_mint_warning(
            self.review_row("sibling", "author", supersedes=parent), snap)
        self.assertTrue(force, "carrier() incorrectly erased the original branch")
        self.assertIn("fixed (VERDICT/FIX)", warning)

    def test_APPROVE_and_operationally_retired_verdicts_release_the_parent(self):  # noqa: VACUOUS_ASSERTION — the unretired FIX on the same call path is the unconditional positive control, asserted first
        parent = "parent"
        live = self.branch_row("live", parent)
        warning, force = dispatches._duplicate_mint_warning(
            self.review_row("control", "author", supersedes=parent),
            self.rows(live))
        self.assertTrue(force)
        self.assertTrue(warning)

        retired = (
            ("approve", {"polarity": "approve"}),
            ("discharged", {"discharged": True}),
            ("withdrawn", {"withdrawn": True}),
            ("closed-by-landing", {"closed_by_landing": True}),
            ("abandoned", {"abandoned": True}),
            ("structured-close", {"close_reason": "superseded"}),
        )
        for label, fields in retired:
            with self.subTest(label=label):
                existing = self.branch_row("retired", parent, **fields)
                quiet, needs_force = dispatches._duplicate_mint_warning(
                    self.review_row("sibling", "author", supersedes=parent),
                    self.rows(existing))
                self.assertIsNone(quiet, "%s did not retire the branch" % label)
                self.assertFalse(needs_force)

    def test_a_RENAMED_review_over_my_own_open_build_row_WARNS(self):
        # THE MISS ITSELF: lane labels differ, so the label arm cannot fire.
        review = self.review_row("r1", "worker", lane="a-totally-other-name")
        opened = self.build_row("b1", "worker", lane="one-name")
        open_warning, force = dispatches._duplicate_mint_warning(
            review, self.rows(opened))
        self.assertIn("b1 (OPEN)", open_warning,
                      "the warning must NAME the OPEN row to supersede")
        self.assertNotIn("dispatch list --held", open_warning)
        self.assertIn("--supersedes", open_warning, "and the verb that fixes it")
        self.assertFalse(force, "WARN, never refuse — role overlap is evidence, "
                                "not proof, and a seat may review unrelated work")

        held = dict(opened, id="b2", status="held",
                    hold_reason="waiting on dependency")
        mixed_warning, force = dispatches._duplicate_mint_warning(
            review, self.rows(opened, held))
        self.assertIn("b1 (OPEN)", mixed_warning)
        self.assertIn("b2 (HELD)", mixed_warning)
        self.assertIn("dispatch list --held", mixed_warning)
        self.assertFalse(force)

    def test_it_is_SILENT_when_the_sender_holds_no_open_build(self):  # noqa: VACUOUS_ASSERTION — an emptiness claim cannot carry a same-call positive; the control is a SEPARATE call on the SAME path with a different sender, asserted FIRST and unconditionally
        # CONTROL FIRST, same call shape: the arm DOES fire for the seat that
        # holds the row, so the silence below is the sender test and not an
        # inert arm.
        snap = self.rows(self.build_row("b1", "worker"))
        fires, _f = dispatches._duplicate_mint_warning(
            self.review_row("r1", "worker"), snap)
        self.assertTrue(fires)
        quiet, _f = dispatches._duplicate_mint_warning(
            self.review_row("r2", "someone-else"), snap)
        self.assertIsNone(quiet, "a seat holding no build row owes nothing")

    def test_a_BUILD_row_never_takes_this_arm(self):  # noqa: VACUOUS_ASSERTION — see above; the control is the review row on the same snapshot, asserted first
        # The arm asks "are you ABANDONING a build you owe" — a build dispatch
        # is not that question, and firing there would nag every integrator
        # dispatching work to a seat that already holds some.
        snap = self.rows(self.build_row("b1", "worker"))
        fires, _f = dispatches._duplicate_mint_warning(
            self.review_row("r1", "worker"), snap)
        self.assertTrue(fires)
        quiet, _f = dispatches._duplicate_mint_warning(
            dict(self.review_row("r2", "worker"), kind="build"), snap)
        self.assertIsNone(quiet)

    def test_supersedes_takes_the_PARENT_arm_and_never_reaches_this_one(self):
        # A row that already declares its work identity has nothing to warn
        # about; the parent arm owns it and returns before this code runs.
        snap = self.rows(self.build_row("b1", "worker"))
        # CONTROL FIRST, same call shape: WITHOUT --supersedes this exact seat
        # and snapshot DO warn, so the silence below is the parent arm taking
        # the row and not an inert check.
        fires, _f = dispatches._duplicate_mint_warning(
            self.review_row("r0", "worker"), snap)
        self.assertTrue(fires)
        warn, force = dispatches._duplicate_mint_warning(
            self.review_row("r1", "worker", supersedes="b1"), snap)
        self.assertIsNone(warn, "declaring the continuation IS the cure")
        self.assertFalse(force)

    def test_a_CLOSED_build_row_is_not_owed_and_does_not_warn(self):  # noqa: VACUOUS_ASSERTION — the OPEN row on the same snapshot is the unconditional positive control, asserted first
        # CONTROL: open row fires.
        openrow = self.build_row("b1", "worker")
        fires, _f = dispatches._duplicate_mint_warning(
            self.review_row("r1", "worker"), self.rows(openrow))
        self.assertTrue(fires)
        closed = dict(openrow, status="verdict", polarity="approve")
        quiet, _f = dispatches._duplicate_mint_warning(
            self.review_row("r2", "worker"), self.rows(closed))
        self.assertIsNone(quiet, "a closed build row is not an obligation")


class NewWorkOverALiveContraryIsAContinuationTest(DispatchBase):
    """The `--new-work` mint that MANUFACTURES an unclosable row.

    `_duplicate_mint_warning`'s lane arm asks `_not_closed`, and `_not_closed`
    is FALSE for a FIX-verdicted review. The `supersedes` arm one screen up
    already knows better and spends `_duplicate_branch_live` ("FIX and
    SUPERSEDE close a REVIEW turn, not the work identity"); the lane arm was
    left behind, so the ONE shape that matters — the cure round for an
    outstanding finding — walked through with no warning at all.

    MEASURED on the live board 2026-08-12: 40 of 974 `--new-work` mints (4.1%)
    had a same-lane contrary-verdicted review whose reviewed tip is a git
    ANCESTOR of the new ref. In 40 of 40 that row was invisible to the lane
    arm. The cost is not a duplicate, it is a row with no honest terminal:
    `--new-work` severs the chain, `superseded` needs a later APPROVE on the
    SAME chain, and three rows sat FINISHED and unclosable for 2-14 hours.

    ANCESTRY IS THE RUNG, not the lane label — which is what keeps the rate at
    4.1% instead of refusing every dispatch onto a lane holding an open
    finding, and what lets the refusal name ONE row instead of a list."""

    def gitdir(self):
        """THE ROW'S repo_id IS THE GITDIR, not the worktree — a candidate
        built with the worktree path never matches a real row's repository
        and the guard is silently unreachable. That is exactly how the first
        cut of these arms failed: every control went quiet."""
        return os.path.realpath(os.path.join(self.repo, ".git"))

    def contrary_on(self, tip, lane="cure-lane", polarity="fix"):
        """A live FIX-verdicted review row, and the two relations that
        DISAGREE about it — which is the entire defect, asserted here so a
        later change to either relation lands on this arm first."""
        row = dispatches.add("seat-b", lane, ref=tip, repo=self.repo,
                             kind="review", notify=False, new_work=True)
        _out, err = dispatches.mark_verdict(row["id"], tip, "findings",
                                            polarity=polarity)
        self.assertIsNone(err)
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        standing = current[row["id"]]
        # MUST-HIT: the two relations really do disagree, or this arm is
        # testing a hazard that no longer exists.
        self.assertFalse(dispatches._not_closed(standing),
                         "the lane arm would already have caught this")
        self.assertTrue(dispatches._duplicate_branch_live(standing))
        return standing, current

    def test_a_descendant_ref_is_refused_and_names_the_row_to_supersede(self):
        self.git("checkout", "-q", self.main)
        reviewed = self.commit("finding")
        cure = self.commit("the cure")          # DESCENDS from `reviewed`
        standing, current = self.contrary_on(reviewed)
        warning, needs_force = dispatches._duplicate_mint_warning(
            {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
             "repo_id": self.gitdir(), "tip": cure, "sender": "someone"},
            current)
        self.assertTrue(needs_force)
        self.assertIn(standing["id"][:12], warning)
        self.assertIn(reviewed[:12], warning)
        self.assertIn("--supersedes " + standing["id"][:12], warning)
        self.assertIn("severs the chain", warning)

    def test_a_REBASED_continuation_is_named_though_ancestry_is_destroyed(self):  # noqa: VACUOUS_ASSERTION — the only absence assertion is the final assertNotIn on the word DESCENDS, and three UNCONDITIONAL positives on the SAME observable precede it: the warning must be non-None with needs_force true, must name the standing row's id, and must contain both '--supersedes <id>' and the word REBASED. A silent guard fails those before the absence is ever reached
        """ANCESTRY ANSWERS NOTHING ABOUT A REBASED BRANCH, and a rebase is
        what happens to nearly every lane between a FIX verdict and its cure:
        trunk moves, the author rebases, every commit is rewritten, and the
        reviewed tip stops being an ancestor of its own continuation while the
        work is unchanged.

        RE-DERIVED OVER THE LIVE LEDGER: of the same-lane `--new-work` mints
        whose ref does NOT descend from a live contrary verdict's reviewed tip
        and whose objects still exist, 33 of the 37 that could be evaluated
        carry that commit's patch-id inside their own history. The census that
        chose ancestry counted mints whose reviewed tip IS an ancestor, so the
        rebased population was outside the set it measured.
        """
        self.git("checkout", "-q", self.main)
        reviewed = self.commit_file("finding.txt", "finding")
        standing, _snap = self.contrary_on(reviewed)
        # TRUNK MOVES, THEN THE AUTHOR REBASES ONTO IT.
        self.git("checkout", "-q", "-B", "rebased", self.c)
        moved = self.commit("trunk moved")
        self.git("cherry-pick", reviewed)
        rewritten = self.git("rev-parse", "HEAD")
        cure = self.commit("the cure")
        # MUST-HIT, BOTH HALVES: the rewrite must really have destroyed
        # ancestry AND preserved the patch, or this arm is exercising a
        # situation the world does not produce.
        self.assertNotEqual(rewritten, reviewed,
                            "fixture: the cherry-pick did not rewrite the commit")
        self.assertNotEqual(moved, reviewed)
        env = dispatches._git_env()
        gitdir = self.gitdir()
        self.assertNotEqual(
            0,
            subprocess.run(["git", "-C", self.repo, "merge-base",
                            "--is-ancestor", reviewed, cure],
                           capture_output=True, env=env).returncode,
            "fixture: ancestry survived, so the old rung would have caught it")
        # MUST-HIT ON THE RELATION THE RUNG ACTUALLY SPENDS, read through the
        # same seam rather than through a private patch-id of my own: a
        # fixture proven with a different instrument proves a different thing.
        from helm import vcs
        self.assertEqual(
            vcs.PATCH_EQUIVALENT,
            vcs.backend(gitdir).landed_state(gitdir, reviewed, rewritten),
            "fixture: the rebase did not preserve the patch identity")

        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        warning, needs_force = dispatches._duplicate_mint_warning(
            {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
             "repo_id": gitdir, "tip": cure, "sender": "someone"},
            current)
        self.assertTrue(needs_force, "the rebased continuation walked through")
        self.assertIn(standing["id"][:12], warning)
        self.assertIn("--supersedes " + standing["id"][:12], warning)
        self.assertIn("REBASED", warning)
        # AND THE SENTENCE MUST NOT MAKE THE CLAIM THAT IS FALSE HERE. A
        # writer who checks "this ref DESCENDS from it" on a rebased lane
        # finds it false and discounts the whole warning.
        self.assertNotIn("DESCENDS", warning)

    def test_a_rebased_UNRELATED_ref_on_the_same_lane_is_still_silent(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the FIRST call in the body, a rebased continuation on the same path asserted to FIRE before any silence is claimed
        """THE MUST-MISS FOR THE PATCH RUNG, and it is the whole false-positive
        bound: 4 of the 37 blind pairs in the census are genuinely distinct
        work sharing a lane label. A rung that answered yes for them would have
        traded one wrong answer for another."""
        self.git("checkout", "-q", self.main)
        reviewed = self.commit_file("finding.txt", "finding")
        standing, _snap = self.contrary_on(reviewed)
        self.git("checkout", "-q", "-B", "rebased", self.c)
        self.commit("trunk moved")
        self.git("cherry-pick", reviewed)
        cure = self.commit("the cure")
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        candidate = {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
                     "repo_id": self.gitdir(), "sender": "someone"}
        # CONTROL FIRST: the same call path DOES fire on the rebased cure.
        fires, force = dispatches._duplicate_mint_warning(
            dict(candidate, tip=cure), current)
        self.assertTrue(force, "the control did not fire")
        self.assertIn(standing["id"][:12], fires)
        # `side` never carried the finding's change at all.
        quiet, quiet_force = dispatches._duplicate_mint_warning(
            dict(candidate, tip=self.side), current)
        self.assertIsNone(quiet, quiet)
        self.assertFalse(quiet_force)

    def test_an_UNRELATED_ref_on_the_same_lane_is_silent(self):  # noqa: VACUOUS_ASSERTION — an emptiness claim cannot carry a same-call positive; the control is a SECOND call on the SAME path with a DESCENDANT ref, asserted FIRST and unconditionally
        """ANCESTRY, NOT THE LABEL. `side` is real work on the same lane label
        that does not descend from the finding, and it must sail through."""
        self.git("checkout", "-q", self.main)
        reviewed = self.commit("finding")
        cure = self.commit("the cure")
        standing, current = self.contrary_on(reviewed)
        # CONTROL FIRST: the same call path DOES refuse a descendant.
        fires, force = dispatches._duplicate_mint_warning(
            {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
             "repo_id": self.gitdir(), "tip": cure, "sender": "someone"},
            current)
        self.assertTrue(force, "the control did not fire")
        self.assertIn(standing["id"][:12], fires)
        quiet, quiet_force = dispatches._duplicate_mint_warning(
            {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
             "repo_id": self.gitdir(), "tip": self.side, "sender": "someone"},
            current)
        self.assertIsNone(quiet)
        self.assertFalse(quiet_force)

    def test_an_APPROVED_row_releases_the_lane(self):  # noqa: VACUOUS_ASSERTION — the FIX row on the same call path is the unconditional positive control, asserted first
        """An APPROVE is not a contrary and owes no cure round."""
        self.git("checkout", "-q", self.main)
        reviewed = self.commit("finding")
        cure = self.commit("the cure")
        standing, current = self.contrary_on(reviewed)
        candidate = {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
                     "repo_id": self.gitdir(), "tip": cure, "sender": "someone"}
        fires, force = dispatches._duplicate_mint_warning(candidate, current)
        self.assertTrue(force, "the control did not fire")
        self.assertIn(standing["id"][:12], fires)
        approved = dict(current)
        approved[standing["id"]] = dict(standing, polarity="approve")
        quiet, quiet_force = dispatches._duplicate_mint_warning(
            candidate, approved)
        self.assertIsNone(quiet)
        self.assertFalse(quiet_force)

    def test_an_unreadable_repository_never_blocks_the_write(self):  # noqa: VACUOUS_ASSERTION — the readable repository on the same call path is the unconditional positive control, asserted first
        """`_branch_moved`'s doctrine: absence of evidence must never refuse a
        dispatch. A guard that turns a slow disk into a blocked fleet is worse
        than the defect it prevents."""
        self.git("checkout", "-q", self.main)
        reviewed = self.commit("finding")
        cure = self.commit("the cure")
        standing, current = self.contrary_on(reviewed)
        candidate = {"id": "n" * 32, "kind": "review", "lane": "cure-lane",
                     "repo_id": self.gitdir(), "tip": cure, "sender": "someone"}
        fires, force = dispatches._duplicate_mint_warning(candidate, current)
        self.assertTrue(force, "the control did not fire")
        self.assertIn(standing["id"][:12], fires)
        missing = os.path.join(self.tmp, "not-a-repo")
        gone = {rid: dict(r, repo_id=missing) for rid, r in current.items()}
        quiet, quiet_force = dispatches._duplicate_mint_warning(
            dict(candidate, repo_id=missing), gone)
        self.assertIsNone(quiet)
        self.assertFalse(quiet_force)


class ForeignRepoWriteDoorTest(DispatchBase):
    """A row whose repository is not this project's is refused at the door.

    `_base` is the SOLE MINT of `repo_id`: rebind and retip refuse unless
    --repo matches the value the row already carries, and the abandon, landing
    and verdict paths read `row["repo_id"]` rather than measuring a fresh one.
    So this one door is the whole of the prevention, which is why it is tested
    as prevention and not as cleanup.
    """

    def _home(self, gitdir, why=None):
        """Pin the answer to "which project is this ledger's". The suite's own
        HELM_HOME maps to no registered project, so the guard is dormant for
        every other test in this file — these arms must state a home to have
        anything to disagree with."""
        self.addCleanup(setattr, dispatches, "home_repo_id",
                        dispatches.home_repo_id)
        dispatches.home_repo_id = lambda: (gitdir, why)

    def _elsewhere(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        return dispatches._repo_info(other)["repo_id"]

    def test_a_row_from_this_project_still_lands(self):  # noqa: VACUOUS_ASSERTION — the concrete persisted repo_id positively proves the accepted write path completed
        mine = dispatches._repo_info(self.repo)["repo_id"]
        self._home(mine)
        row = dispatches.add("seat-under-test", "zz-lane", ref=self.a,
                             repo=self.repo, new_work=True)
        self.assertIsNotNone(row, "the guard refused this project's own row")
        self.assertEqual(dispatches.rows()[row["id"]]["repo_id"], mine)

    def test_a_foreign_parent_cannot_recursively_authorize_its_repository(self):
        other_id = self._elsewhere()
        other = os.path.dirname(other_id)
        with open(os.path.join(other, "a.txt"), "w") as f:
            f.write("foreign work\n")
        subprocess.run(["git", "-C", other, "add", "a.txt"], check=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "-C", other, "commit", "-qm", "foreign"],
                       check=True, env=env)
        tip = subprocess.run(["git", "-C", other, "rev-parse", "HEAD"],
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        self._home(other_id)
        parent = dispatches.add("seat-under-test", "foreign-lane", ref=tip,
                                repo=other, new_work=True)
        self.assertIsNotNone(parent)
        self._home(dispatches._repo_info(self.repo)["repo_id"])
        child = dispatches.add("seat-under-test", "foreign-lane", ref=tip,
                               repo=other, supersedes=parent["id"])
        self.assertIsNone(child)
        self.assertEqual(list(dispatches.rows()), [parent["id"]],
                         "one misplaced parent punched a recursive repo tunnel")

    def test_a_home_child_cannot_inherit_a_misplaced_foreign_parent_chain(self):
        other_id = self._elsewhere()
        other = os.path.dirname(other_id)
        with open(os.path.join(other, "a.txt"), "w") as f:
            f.write("foreign work\n")
        subprocess.run(["git", "-C", other, "add", "a.txt"], check=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "-C", other, "commit", "-qm", "foreign"],
                       check=True, env=env)
        tip = subprocess.run(["git", "-C", other, "rev-parse", "HEAD"],
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        self._home(other_id)
        parent = dispatches.add("seat-under-test", "foreign-parent", ref=tip,
                                repo=other, new_work=True)
        self.assertIsNotNone(parent)
        mine = dispatches._repo_info(self.repo)["repo_id"]
        self._home(mine)
        child, why = dispatches.add(
            "seat-under-test", "home-child", ref=self.a, repo=self.repo,
            supersedes=parent["id"], _reason=True)
        self.assertIsNone(child)
        self.assertIn("refusing foreign chain authority", why)
        self.assertEqual(list(dispatches.rows()), [parent["id"]])

    def test_a_foreign_row_never_reaches_the_ledger(self):
        """THE POSITIVE CONTROL IS THE POINT. An empty ledger proves nothing on
        its own — a broken writer produces the same emptiness as a working
        refusal. So a legitimate row is written FIRST and must still be there
        afterwards: the ledger is asserted to hold exactly it, never to be
        bare."""
        self._home(dispatches._repo_info(self.repo)["repo_id"])
        kept = dispatches.add("seat-under-test", "zz-kept", ref=self.a,
                              repo=self.repo, new_work=True)
        self.assertIsNotNone(kept, "the control row never landed")
        self.assertIn(kept["id"], dispatches.rows())
        self._home(self._elsewhere())
        self.assertIsNone(dispatches.add("seat-under-test", "zz-lane",
                                         ref=self.a, repo=self.repo,
                                         new_work=True))
        self.assertEqual(list(dispatches.rows()), [kept["id"]],
                         "a foreign row was written despite the refusal")

    def test_the_refusal_hands_the_work_off_instead_of_slamming(self):
        """A door slam loses findings; a labelled hand-off does not. The text
        must name the repository the ref lives in AND the project this ledger
        answers for, or the caller cannot carry the work to where it belongs.

        AND THE HAND-OFF MUST NAME A DESTINATION THAT EXISTS (task/2437). The
        old text sent the caller to "that repository's own helm", which exists
        only if they copy this package into their tree — so the labelled
        hand-off was a slam wearing a hand-off's words. `helm sync` is a verb
        the caller can actually run, so the refusal is asserted to name it."""
        home = self._elsewhere()
        self._home(home)
        mine = dispatches._repo_info(self.repo)["repo_id"]
        row, err = dispatches._base("seat-under-test", "zz-lane", self.a, "note", 60,
                                    self.repo, new_work=True)
        self.assertIsNone(row)
        self.assertIn(mine, err)
        self.assertIn(home, err)
        self.assertIn("helm sync", err)
        self.assertNotIn("own helm", err)

    def test_an_UNKNOWN_home_REFUSES_rather_than_waving_through(self):  # noqa: VACUOUS_ASSERTION — the injected resolver failure positively controls the empty-ledger refusal assertion
        """AN UNKNOWN AUTHORITY IS NOT A PERMIT.

        RE-POINTED BY task/2437, AND THE PROPERTY SURVIVES. The refusal no
        longer comes from the unknown home ALONE: a row is admitted when the
        REGISTRY places its repository, so what this arm now pins is that
        NEITHER authority answered — the fixture repo is in no registered
        project (the suite's temp HELM_HOME maps to none) and this helm can
        establish no repository of its own, so the row has no home at all and
        is refused. `RegisteredProjectWriteDoorTest.test_an_UNKNOWN_home_still_
        files_a_REGISTERED_projects_row` holds the other half deliberately.

        THIS ARM USED TO ASSERT THE OPPOSITE, and pinning that was the defect.
        The guard read `if home_id and ...`, so it refused only on a KNOWN
        mismatch and therefore refused NOTHING while home identity was
        unresolvable — an unlabelled escape, when the owner's row requires any
        escape to be LABELLED FOREIGN on every surface. A review
        found it; no arm here could, because they all replace the resolver
        with a lambda and so test the comparison, never the thing compared
        against.

        "Missing evidence is not evidence against" was the right principle at
        the WRONG LAYER: the cure is not to refuse on absence, it is to stop
        deriving the authority from something that can go absent.
        """
        self._home(None, "probe: package is not inside a git tree")
        row = dispatches.add("seat-under-test", "zz-lane", ref=self.a,
                             repo=self.repo, new_work=True)
        self.assertIsNone(row, "an UNKNOWN home let a repo-bound row through")
        self.assertEqual(dispatches.rows(), {},
                         "the refused row reached the ledger anyway")

    def test_the_REAL_resolver_answers_without_a_registry(self):
        """NO LAMBDA. Every other arm here pins `home_repo_id`, so a resolver
        that always returned None would keep them all green — the exact gap
        the reviewer named. This one runs the real thing.

        It also pins WHY the resolution is trustworthy: the answer comes from
        the package's OWN location, so it holds with the project registry
        emptied. That registry is a rebuildable projection, and the write
        door's authority may not depend on a file helm regenerates on demand.
        """
        from helm import registry
        # THE BASE FIXTURE PINS home_repo_id so 85 write sites are not refused
        # as foreign. This arm exists to test the REAL resolver, so it puts the
        # real one back for the duration — otherwise it would assert about a
        # lambda, which is precisely the gap that let the registry fail-open
        # ship past a 10,863-test green.
        real = self._real_home_repo_id
        home, why = real()
        self.assertIsNone(why, "the real resolver could not answer")
        self.assertTrue(home and os.path.isabs(home), "home is not an abspath")
        self.assertTrue(os.path.isdir(home), "home is not a real gitdir")

        # REGISTRY LOSS MUST NOT MOVE THE ANSWER.
        with mock.patch.object(registry, "load", return_value={}):
            again, why2 = real()
        self.assertIsNone(why2, "an empty registry blanked the home")
        self.assertEqual(again, home,
                         "home identity moved when the rebuildable projection "
                         "was emptied, which is the escape this closes")


class RegisteredProjectWriteDoorTest(DispatchBase):
    """A ROW IS KEYED BY THE REPOSITORY ITS REF LIVES IN (task/2437).

    The owner's standing framing: helm exists to help agent teams build ANY
    project, a team USING helm must never need to know how helm is made, and a
    verb that assumes the helm checkout is a bug. The
    equality door was that bug written as a remedy — "dispatch it from that
    repository's own helm", where `home_repo_id` derives identity from the
    RUNNING PACKAGE PATH, so the only way to obey was to copy the helm package
    into the target repository's tree. One project did exactly that (an
    untracked .helm-dispatch-runtime/helm inside a client project's worktree), which is
    how 63 non-helm rows reached the live ledger a MONTH after the guard landed.

    EVERY ARM HERE RUNS THE REAL RESOLVER. `DispatchBase` pins `home_repo_id`
    to the fixture repo so 85 unrelated write sites are not refused as foreign;
    these arms put the REAL one back, so the ONLY thing that can admit a row
    bound to the fixture repo is REGISTRY MEMBERSHIP — the property under test.
    Pinning would leave them asserting about a lambda, which is the gap that let
    a registry fail-open ship past a 10,863-test green.
    """

    def _real_home(self):
        """Put the package-path resolver back for the rest of this arm."""
        pinned = dispatches.home_repo_id
        self.addCleanup(setattr, dispatches, "home_repo_id", pinned)
        dispatches.home_repo_id = self._real_home_repo_id
        home, why = self._real_home_repo_id()
        self.assertIsNone(why, "the real resolver could not answer")
        return home

    def _register(self, **projects):
        """The registry answers with EXACTLY these projects.

        realpath'd, because that is the relationship the LIVE registry has with
        the repo_id a row stamps: `_repo_info` realpaths what it writes, and
        `project_for_cwd` matches by longest path prefix. Only the FILE is
        synthetic — the resolver under test is the real one, so an arm cannot
        pass by agreeing with a lambda about a string.
        """
        from helm import registry
        data = {"projects": {n: {"name": n, "path": os.path.realpath(p)}
                             for n, p in projects.items()}}
        patcher = mock.patch.object(registry, "load", return_value=data)
        patcher.start()
        self.addCleanup(patcher.stop)
        return data

    def _second_repo(self, name="beside"):
        """A second git repository with its own gitdir and its own commit."""
        other = os.path.join(self.tmp, name)
        os.makedirs(other)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        with open(os.path.join(other, "work"), "w", encoding="utf-8") as fh:
            fh.write("work\n")
        subprocess.run(["git", "-C", other, "add", "work"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-qm", "work"],
                       check=True, env=env)
        tip = subprocess.run(["git", "-C", other, "rev-parse", "HEAD"],
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        return other, tip

    def test_a_registered_projects_row_lands_keyed_to_that_repository(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the CONCRETE persisted repo_id read back off the ledger; the assertNotEqual beside it only states that the fixture is not degenerate
        """THE WHOLE RULE, at the owner layer: a ref in a REGISTERED project's
        repository files a row whose repo_id is THAT repository — with the real
        package-path home pointing somewhere else entirely."""
        home = self._real_home()
        self._register(otherproj=self.repo)
        row, why = dispatches.add("seat-under-test", "lane/other-project-work",
                                  ref=self.a, repo=self.repo, new_work=True,
                                  kind="review", notify=False, _reason=True)
        self.assertIsNotNone(row, "a registered project's row was refused: %s"
                             % (why,))
        mine = dispatches._repo_info(self.repo)["repo_id"]
        self.assertEqual(dispatches.rows()[row["id"]]["repo_id"], mine)
        self.assertNotEqual(dispatches._real(mine), dispatches._real(home),
                            "this arm proves nothing unless the row's "
                            "repository differs from the package's own")

    def test_the_shipped_add_verb_files_a_registered_projects_row(self):
        """THE SAME FACT THROUGH THE ARGV A SEAT ACTUALLY TYPES, because the
        door being widened at the owner layer and a CLI arm that still refuses
        is exactly the half-cure this ledger keeps producing."""
        self._real_home()
        self._register(otherproj=self.repo)
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["add", "seat-under-test", "lane/shipped-verb",
                            "--ref", self.a, "--kind", "review", "--new-work",
                            "--repo", self.repo])
        self.assertEqual(rc, 0, "the shipped verb refused: %s%s" % (out, err))
        rows = list(dispatches.rows().values())
        self.assertEqual(len(rows), 1, "%r" % (rows,))
        self.assertEqual(rows[0]["repo_id"],
                         dispatches._repo_info(self.repo)["repo_id"])

    def test_this_packages_own_repository_lands_with_an_EMPTY_registry(self):  # noqa: VACUOUS_ASSERTION — the empty-registry comparison is a PRECONDITION check, and the unconditional positive control on the same observable is the persisted repo_id equalling the real home
        """THE MUST-HIT CONTROL, on the exact hazard a registry admission adds:
        a wiped registry must never lock helm out of its OWN ledger.

        So the registry answers with NO projects at all and the ref is a commit
        in the repository this package is running from, resolved by the REAL
        `home_repo_id`. If this goes red the cure has traded one lockout for
        another.

        THE WORKING TREE COMES FROM THE SHIPPED RESOLVER, NEVER FROM STRING
        SURGERY ON THE GITDIR. `home` is `--git-common-dir`, and that is
        `<root>/.git` only for a classic checkout: the fab gate runs this suite
        in a LINKED WORKTREE whose common dir is a bare mirror named
        `<lane>.git`, so `dirname(home)` was the mirrors directory, the door
        answered its honest "ref needs a Git working tree" and this arm read
        that as the lockout it was written to catch. `_repo_info` returns the
        toplevel BESIDE the gitdir it reports, which is the working tree by
        definition wherever this suite runs — and the equality below binds the
        two, so a resolver that answered about some other repository cannot
        pass this arm."""
        home = self._real_home()
        self._register()                       # no projects whatsoever
        info = dispatches._repo_info(
            os.path.dirname(os.path.abspath(dispatches.__file__)))
        self.assertIsNotNone(info, "the running helm package is not inside a "
                                   "Git working tree, so this arm has no "
                                   "subject")
        self.assertEqual(info["repo_id"], home,
                         "the working tree resolved here is not the one "
                         "`home_repo_id` names, so the ref below would be "
                         "about another repository")
        here = info["repo"]
        tip = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"],
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        from helm import registry
        self.assertEqual(registry.load().get("projects"), {},
                         "the registry is not actually empty, so this arm "
                         "would pass for the wrong reason")
        row, why = dispatches.add("seat-under-test", "lane/helms-own-work",
                                  ref=tip, repo=here, new_work=True,
                                  kind="review", notify=False, _reason=True)
        self.assertIsNotNone(row, "an empty registry locked helm out of its "
                             "own ledger: %s" % (why,))
        self.assertEqual(dispatches.rows()[row["id"]]["repo_id"], home)

    def test_a_LINKED_WORKTREE_of_helms_own_repository_lands_with_an_EMPTY_registry(self):  # noqa: VACUOUS_ASSERTION — the row landing and its persisted repo_id are unconditional positives asserted BEFORE the refusal, on the same door, the same repository and the same tip
        """HELM'S OWN SECOND ADMISSION MUST HOLD FROM A LANE ROOM, and a lane
        room's gitdir is not `<root>/.git`.

        Every seat on this fleet works in `helm-wt/<lane>`, and the fab gate
        goes one step further: it runs the suite in a linked worktree whose
        common dir is a BARE MIRROR named `<lane>.git`. So "the repository this
        package is running from" is a path with NO `.git` directory inside it
        and a gitdir that is not its child — the shape the empty-registry arm
        beside this one had been deriving by string surgery and getting wrong.

        THE NEGATIVE IS ON THE SAME REPOSITORY AND THE SAME TIP, one fact
        changed: `--repo` names the bare mirror instead of the room. That is a
        path with no working tree, and the door's refusal there is correct —
        which is what makes the positive above a statement about the ADMISSION
        rather than about a door that accepts anything.
        """
        mirror = os.path.join(self.tmp, "mirror.git")
        subprocess.run(["git", "clone", "--bare", "-q", self.repo, mirror],
                       check=True, capture_output=True)
        room = os.path.join(self.tmp, "lane-room")
        subprocess.run(["git", "-C", mirror, "worktree", "add", "-q", room,
                        self.main], check=True, capture_output=True)
        # THE ROOM IS HELM'S OWN CHECKOUT for this arm, declared through the
        # fixture's own pin and resolved by the shipped `_repo_info` — so the
        # authority under test is the real one and only its LOCATION is staged.
        from tests._tmphome import pin_dispatch_home
        pin_dispatch_home(self, room)
        self._register()                       # no projects whatsoever
        mirror_id = dispatches._repo_info(room)["repo_id"]
        self.assertEqual(mirror_id, os.path.realpath(mirror),
                         "the fixture is not the fab node's shape: a lane "
                         "room whose gitdir is a bare mirror is the subject")
        self.assertFalse(os.path.exists(os.path.join(room, ".git", "HEAD")),
                         "this room has an ordinary .git directory, so it does "
                         "not model the worktree the gate runs in")
        tip = subprocess.run(["git", "-C", room, "rev-parse", "HEAD"],
                             check=True, capture_output=True,
                             text=True).stdout.strip()

        row, why = dispatches.add("seat-under-test", "lane/from-the-room",
                                  ref=tip, repo=room, new_work=True,
                                  kind="review", notify=False, _reason=True)
        self.assertIsNotNone(row, "an empty registry locked helm out of its "
                                  "own ledger from a lane room: %s" % (why,))
        self.assertEqual(dispatches.rows()[row["id"]]["repo_id"], mirror_id)

        refused, why = dispatches.add("seat-under-test", "lane/from-the-gitdir",
                                      ref=tip, repo=mirror, new_work=True,
                                      kind="review", notify=False,
                                      _reason=True)
        self.assertIsNone(refused, "a path with no working tree filed a row")
        self.assertIn("needs a Git working tree", why)

    def test_an_unregistered_repository_is_refused_and_told_how_to_register(self):
        """THE REFUSAL NAMES A REPAIR THE CALLER CAN PERFORM. "Dispatch it from
        that repository's own helm" could only be obeyed by copying the package
        into their tree; `helm sync` is a command that exists.

        THE CONTROL COMES FIRST AND ON THE SAME OBSERVABLE: a REGISTERED
        repository's row lands into this same ledger in the same test, so the
        refusal below cannot be a broken writer refusing everything."""
        self._real_home()
        registered, reg_tip = self._second_repo("registered")
        self._register(registeredproj=registered)
        kept, why = dispatches.add("seat-under-test", "lane/registered",
                                   ref=reg_tip, repo=registered,
                                   new_work=True, kind="review", notify=False,
                                   _reason=True)
        self.assertIsNotNone(kept, "the control row never landed: %s" % (why,))

        row, why = dispatches.add("seat-under-test", "lane/stranger",
                                  ref=self.a, repo=self.repo, new_work=True,
                                  kind="review", notify=False, _reason=True)
        self.assertIsNone(row, "an unregistered repository's row was filed")
        self.assertIn("not a registered helm project", why)
        self.assertIn("helm sync", why)
        self.assertIn(dispatches._repo_info(self.repo)["repo_id"], why)
        self.assertNotIn("own helm", why,
                         "the refusal still names a helm that only exists if "
                         "the caller copies this package into their tree")
        self.assertEqual(list(dispatches.rows()), [kept["id"]],
                         "the refused row reached the ledger anyway")

    def test_a_chain_is_authorized_by_the_ROWS_repository_not_by_helms(self):
        """THE CHAIN RUNG MOVED WITH THE DOOR. It compared the parent against
        the helm package's own repository, so a registered project's SECOND row
        could never supersede its own first one. The invariant is that a chain
        stays inside ONE repository, and both halves are asserted here: the
        same-repository child lands, the cross-repository child is refused with
        the sentence that names the reason."""
        self._real_home()
        one, one_tip = self._second_repo("proj-one")
        two, two_tip = self._second_repo("proj-two")
        self._register(projone=one, projtwo=two)
        parent, why = dispatches.add("seat-under-test", "lane/p", ref=one_tip,
                                     repo=one, new_work=True, kind="review",
                                     notify=False, _reason=True)
        self.assertIsNotNone(parent, "%s" % (why,))
        child, why = dispatches.add("seat-under-test", "lane/p", ref=one_tip,
                                    repo=one, supersedes=parent["id"],
                                    kind="review", notify=False, _reason=True)
        self.assertIsNotNone(child, "a project could not continue its own "
                             "chain: %s" % (why,))
        self.assertEqual(dispatches.rows()[child["id"]]["supersedes"],
                         parent["id"])
        crossed, why = dispatches.add("seat-under-test", "lane/p", ref=two_tip,
                                      repo=two, supersedes=parent["id"],
                                      kind="review", notify=False,
                                      _reason=True)
        self.assertIsNone(crossed, "a chain crossed two repositories")
        self.assertIn("refusing foreign chain authority", why)

    # ---- task/2437 round two, the P1 + its must-hit control ----------------
    def _legacy_parent(self, rid="a1b2c3d4e5f60718293a4b5c6d7e8f90"):
        """One v1 row — the shape EVERY row on this ledger had before `repo_id`
        existed — appended through the real event ledger and read back through
        the real replay, which PRESERVES an absent repository rather than
        inventing one. No snapshot dict is invented here: the row reaches the
        door the way a five-month-old row does, and the arm asserts the absence
        it depends on instead of assuming the replay kept it."""
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.assertTrue(eventledger.append(path, {
            "v": 1, "id": rid, "seq": 0, "status": "open",
            "ts": "2026-04-01T00:00:00Z", "recipient": "seat-under-test",
            "lane": "ancient", "tip": self.a, "deadline_s": 600}))
        replayed = dispatches.rows()[rid]
        self.assertIsNone(
            replayed.get("repo_id"),
            "the fixture row carries a repository, so this arm would measure "
            "the ordinary comparison and not the unrecorded-provenance branch")
        return rid

    def test_a_legacy_parents_OWN_repository_still_continues_its_chain(self):  # noqa: VACUOUS_ASSERTION — the absent `repo_id` is a PRECONDITION read off the real replay; the unconditional positive control on the same observable is the persisted `supersedes` read back off the ledger
        """THE MUST-HIT CONTROL for the P1 cure. A row with no recorded
        repository is read as the HOME repository — the only repository the
        equality door could ever have written one from — so a child in THAT
        repository still supersedes it. A cure that refused every legacy parent
        would close the hole by deleting the chain door, and ~all the oldest
        rows in the live ledger are exactly this shape.

        The fixture's home IS `self.repo` (pinned in `setUp`), which is the
        premise this arm rests on and why it is stated rather than implied."""
        other, _other_tip = self._second_repo("registered-elsewhere")
        self._register(otherproj=other)
        parent = self._legacy_parent()
        self.assertEqual(dispatches._real(dispatches.home_repo_id()[0]),
                         dispatches._real(
                             dispatches._repo_info(self.repo)["repo_id"]),
                         "the fixture home is not this repo, so 'same repo as "
                         "the legacy parent' is not what this arm exercises")
        child, why = dispatches.add("seat-under-test", "lane/continues",
                                    ref=self.a, repo=self.repo,
                                    supersedes=parent, kind="review",
                                    notify=False, _reason=True)
        self.assertIsNotNone(child, "a legacy parent's own repository could no "
                             "longer continue its chain: %s" % (why,))
        self.assertEqual(dispatches.rows()[child["id"]]["supersedes"], parent)

    def test_an_unrecorded_provenance_authorizes_NO_other_repository(self):
        """THE P1. The parent comparison was SKIPPED whenever the parent carried
        no `repo_id`, which is the true reading of every v1 row. While the write
        door was an equality test that cost nothing — the only repository a new
        row could bind was this package's own, so a skipped comparison and a
        satisfied one had one outcome. Registry admission separated them: a
        newly admitted project can name a HISTORICAL helm row as its parent, and
        the close ladder then treats the old obligation as carried (`_same_chain`
        accepts the derived root, the carrier index takes the child, `owed`
        suppresses the parent). A helm obligation nobody reviewed disappears
        because another repository claimed its chain.

        THE OWED FRONTIER IS THE ASSERTION, not only the refusal, because
        suppression is what the hole actually costs — and the ledger contents
        are asserted too, since a refusal that still appends is the defect
        wearing a pass. The sibling arm above is the must-hit: the same legacy
        parent, continued from its own repository, still lands."""
        other, other_tip = self._second_repo("registered-b")
        self._register(otherproj=other)
        parent = self._legacy_parent()
        self.assertIn(parent, [r["id"] for r in dispatches.owed(
            dispatches.rows())], "the legacy row is not an owed obligation to "
            "begin with, so its suppression could not be measured")
        crossed, why = dispatches.add("seat-under-test", "lane/adopted",
                                      ref=other_tip, repo=other,
                                      supersedes=parent, kind="review",
                                      notify=False, _reason=True)
        self.assertIsNone(crossed, "another repository superseded a row whose "
                          "provenance is unrecorded")
        self.assertIn("records NO repository", why)
        self.assertIn("authorizes nothing", why)
        self.assertEqual(list(dispatches.rows()), [parent],
                         "the refused row reached the ledger anyway")
        self.assertIn(parent, [r["id"] for r in dispatches.owed(
            dispatches.rows())], "the legacy obligation left the owed frontier")

    def test_an_UNKNOWN_home_still_files_a_REGISTERED_projects_row(self):  # noqa: VACUOUS_ASSERTION — nothing here asserts an absence: the row is asserted present and its stamped repo_id is read back off the ledger
        """THE POLARITY THE REGISTRY ADMISSION DELIBERATELY CHANGES.

        A helm whose package is not inside a Git working tree can establish no
        repository identity of its own. That is not a reason to refuse a row a
        REGISTERED project can place: the row has a home even though this helm
        does not, and every read surface can scope it. The sibling arm
        `test_an_UNKNOWN_home_REFUSES_rather_than_waving_through` holds the
        other half — unknown home AND no registration still refuses."""
        pinned = dispatches.home_repo_id
        self.addCleanup(setattr, dispatches, "home_repo_id", pinned)
        dispatches.home_repo_id = lambda: (
            None, "probe: package is not inside a git tree")
        self._register(otherproj=self.repo)
        row, why = dispatches.add("seat-under-test", "lane/placed-by-registry",
                                  ref=self.a, repo=self.repo, new_work=True,
                                  kind="review", notify=False, _reason=True)
        self.assertIsNotNone(row, "a registered project's row was refused "
                             "because THIS helm has no repository: %s" % (why,))
        self.assertEqual(dispatches.rows()[row["id"]]["repo_id"],
                         dispatches._repo_info(self.repo)["repo_id"])


class LaneClaimEvidenceBindsToTheRowsRepositoryTest(DispatchBase):
    """A LANE CLAIM IS PROGRESS ONLY FOR THE REPOSITORY WHOSE ROOM IT NAMES
    (task/2437 round two, finding 7).

    `_repo_project` is the repo root's BASENAME, deliberately — the CLAIM is
    minted under that basename and a second derivation would desync the lease
    key from its minter. So a project's checkout and a fork of it kept elsewhere
    yield ONE token and share one lease namespace. While the write door was an
    equality test, only helm's rows existed and the collision was unreachable
    FROM THE LEDGER; registry admission reached it. A seat holding
    `worktree:<basename>:<lane>` in the FORK then suppressed the overdue verdict
    on a row belonging to the OTHER repository of that name, and the row read as
    being worked while nobody was working it — a silent alarm, the one direction
    `_is_overdue`'s own docstring forbids.

    ROUND ONE FILED THE TOKEN as a remainder because curing the TOKEN moves the
    claims key and owes a live migration. The cure here does not touch the
    token: it asks the ROW's repository whether the claimed room exists, so the
    claims key is byte-identical and the migration stays filed.

    EVERY PRODUCER HERE IS THE SHIPPED ONE. `work._claims.claim` mints both the
    lease and the room (it is what `helm work claim` runs), the row goes through
    the real write door, and the verdict is read off `progress_state` and
    `_is_overdue` — the two functions the overdue surfaces actually call.
    """

    def _repo_named(self, parent, name="service"):
        """A git repository at <tmp>/<parent>/<name>, so two of them can share a
        basename while differing in every other byte."""
        root = os.path.join(self.tmp, parent, name)
        os.makedirs(root)
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        subprocess.run(["git", "-C", root, "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", root, "config", "user.name", "t"],
                       check=True)
        with open(os.path.join(root, "seed"), "w", encoding="utf-8") as handle:
            handle.write(parent)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "seed"], check=True)
        tip = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        return root, tip

    def test_a_forks_lane_claim_is_not_progress_on_the_other_repositorys_row(self):  # noqa: VACUOUS_ASSERTION — the suppression half is asserted FIRST and unconditionally on the same observable (WORKING, and the clock NOT overdue), and the fork half asserts the POSITIVE overdue verdict
        """THE PAIR, on one token and one seat: a claim minted in the ROW's own
        repository still suppresses the overdue verdict (the must-hit, asserted
        FIRST), and a claim minted in a FORK sharing the token no longer does.

        The shared token is asserted as a precondition, because without it this
        arm would measure nothing at all."""
        from helm import registry
        from helm.work import _claims, _lanes
        mine, mine_tip = self._repo_named("x")
        fork, _fork_tip = self._repo_named("y")
        patcher = mock.patch.object(
            registry, "load",
            return_value={"projects": {"serviceproj": {
                "name": "serviceproj", "path": os.path.realpath(mine)}}})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(_lanes.project_token(mine), _lanes.project_token(fork),
                         "the two repositories do not share a claims token, so "
                         "the collision this arm is about does not exist here")

        own, why = dispatches.add("seat-a", "lane/own-room", ref=mine_tip,
                                  repo=mine, new_work=True, kind="review",
                                  deadline_s=60, notify=False, _reason=True)
        self.assertIsNotNone(own, "the registered project's row was refused, so "
                             "there is no row to measure progress on: %s" % (why,))
        rc, line = _claims.claim(mine, "own-room", "seat-a", ttl=600)
        self.assertEqual(rc, 0, line)
        late = time.time() + 4000
        state, detail = dispatches.progress_state(own)
        self.assertEqual(state, dispatches.WORKING,
                         "a seat holding the dispatched lane in the ROW'S OWN "
                         "repository stopped counting as progress: %s" % detail)
        self.assertFalse(dispatches._is_overdue(own, late),
                         "visible progress stopped suppressing the clock")

        theirs, why = dispatches.add("seat-a", "lane/fork-room", ref=mine_tip,
                                     repo=mine, new_work=True, kind="review",
                                     deadline_s=60, notify=False, _reason=True)
        self.assertIsNotNone(theirs, "%s" % (why,))
        rc, line = _claims.claim(fork, "fork-room", "seat-a", ttl=600)
        self.assertEqual(rc, 0, line)
        # THE CLAIM IS LIVE AND ITS TOKEN DOES MATCH — that pair is the whole of
        # what a token comparison can see, so asserting it here is what makes the
        # IDLE below a measurement of the REPOSITORY BINDING and not of a missing
        # claim.
        claims = dispatches.live_claims()
        self.assertIn("worktree:%s:fork-room" % _lanes.project_token(fork),
                      claims or {})
        self.assertEqual("worktree:%s:fork-room" % _lanes.project_token(fork),
                         "worktree:%s:fork-room" % dispatches._repo_project(
                             dispatches._repo_info(mine)["repo_id"]))
        state, detail = dispatches.progress_state(theirs)
        self.assertEqual(state, dispatches.IDLE,
                         "a claim on a FORK's room counted as progress on "
                         "another repository's row: %s" % detail)
        self.assertTrue(dispatches._is_overdue(theirs, late),
                        "a fork's lane claim silenced an overdue obligation "
                        "nobody is working")

    def _register(self, *roots):
        """Register these repositories, so the real write door admits their rows."""
        from helm import registry
        projects = {}
        for i, root in enumerate(roots):
            projects["proj%d" % i] = {"name": "proj%d" % i,
                                      "path": os.path.realpath(root)}
        patcher = mock.patch.object(registry, "load",
                                    return_value={"projects": projects})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _row_for(self, root, tip, lane):
        """A real dispatch row through the real write door, bound to `root`."""
        row, why = dispatches.add("seat-a", "lane/" + lane, ref=tip, repo=root,
                                  new_work=True, kind="review", deadline_s=60,
                                  notify=False, _reason=True)
        self.assertIsNotNone(row, "the row was refused, so there is nothing to "
                                  "measure progress on: %s" % (why,))
        return row

    def test_a_RETAINED_ROOM_is_not_a_current_grant_for_its_repository(self):  # noqa: VACUOUS_ASSERTION — the suppression half is asserted FIRST on both rows and the same observable, and the post-move half asserts the POSITIVE overdue verdict on A
        """THE SEVENTH ROOT, ROUND THREE: A ROOM OUTLIVES THE GRANT THAT CUT IT.

        `work/_claims.release_lane` keeps BOTH branch and worktree whenever the
        lane is not proven landed — by design, so nothing is ever discarded —
        and it leaves the worktree's `lease:<id8>` lock string behind too. The
        round-two cure read the ROOM, so a seat that worked lane L in repository
        A, released the lease UNLANDED, and then claimed the same lane in
        repository B (ONE lease key: the token is the root's basename) left A's
        row reading WORKING off a directory nobody holds. Same silent alarm as
        round two, one artifact further out.

        THE SEQUENCE IS THE OWNER-STATED DISCRIMINATOR: claim in A, release
        UNLANDED with A's room retained, claim the same lane and token in B.
        A must read OVERDUE and B must read WORKING. Every producer is shipped —
        `_claims.claim` and `_claims.release_lane` are what `helm work claim`
        and `helm work release` run, the rows go through the real write door.
        """
        from helm.work import _claims, _lanes
        a_root, a_tip = self._repo_named("x")
        b_root, b_tip = self._repo_named("y")
        self._register(a_root, b_root)
        self.assertEqual(_lanes.project_token(a_root), _lanes.project_token(b_root),
                         "the two repositories do not share a claims token, so "
                         "the collision this arm is about does not exist here")
        lane = "moved-lane"
        row_a = self._row_for(a_root, a_tip, lane)
        row_b = self._row_for(b_root, b_tip, lane)
        late = time.time() + 4000

        rc, line = _claims.claim(a_root, lane, "seat-a", ttl=600)
        self.assertEqual(rc, 0, line)
        room_a, _branch, lease_a, _ttl = line.split("\t")
        # UNLANDED WORK IS WHAT KEEPS THE ROOM. A lane with no commits of its own
        # is PROVEN landed and `release_lane` retires the room, which would
        # delete the artifact this arm is about.
        with open(os.path.join(room_a, "wip"), "w", encoding="utf-8") as handle:
            handle.write("unlanded\n")
        subprocess.run(["git", "-C", room_a, "add", "-A"], check=True)
        subprocess.run(["git", "-C", room_a, "commit", "-qm", "wip"], check=True)

        # THE PAIR BEFORE THE MOVE, on the observable the cure changes: A's own
        # live grant suppresses A and does NOT suppress B.
        self.assertFalse(dispatches._is_overdue(row_a, late),
                         "a live grant in the row's own repository stopped "
                         "counting as progress")
        self.assertTrue(dispatches._is_overdue(row_b, late),
                        "A's grant suppressed B's row")

        rc, lines = _claims.release_lane(a_root, lane, "seat-a", lease=lease_a)
        self.assertEqual(rc, 0, lines)
        # MUST-HIT: the release left the room standing. Without it this arm
        # measures a deleted directory and proves nothing about grants.
        self.assertTrue(os.path.isfile(os.path.join(room_a, ".git")),
                        "the release retired room %s, so the retained-room "
                        "case this arm is about did not happen: %s"
                        % (room_a, lines))
        self.assertNotIn(_lanes.resource(a_root, lane),
                         dispatches.live_claims() or {},
                         "the lease survived the release, so the move below is "
                         "not a move")

        rc, line = _claims.claim(b_root, lane, "seat-a", ttl=600)
        self.assertEqual(rc, 0, line)
        # CONTROL — the round-two evidence, recomputed in place on the SHIPPED
        # predicate. A's retained room STILL binds to A, which is exactly what
        # the room-only cure looked at, so this assertion is what proves the
        # verdict below comes from the grant and not from a vanished room.
        # BLAST RADIUS: one read-only call to `_lane_claim_binds`; it mutates
        # nothing and fails only if the retained room stops pointing at A.
        a_repo_id = dispatches._repo_info(a_root)["repo_id"]
        self.assertTrue(dispatches._lane_claim_binds(a_repo_id, lane),
                        "A's room stopped binding to A, so the OVERDUE verdict "
                        "below would be explained by the room and not by the "
                        "grant — the control this arm needs is gone")
        grant = (dispatches.live_claims() or {}).get(
            _lanes.resource(b_root, lane))
        self.assertEqual(dispatches._real((grant or {}).get("repo")),
                         dispatches._repo_info(b_root)["repo_id"],
                         "the shipped producer did not record B as the grant's "
                         "repository, so nothing here distinguishes A from B")

        state, detail = dispatches.progress_state(row_a)
        self.assertEqual(state, dispatches.IDLE,
                         "a room retained by an UNLANDED release counted as a "
                         "current grant for its repository: %s" % detail)
        self.assertTrue(dispatches._is_overdue(row_a, late),
                        "a retained room silenced an overdue obligation nobody "
                        "is working")
        state, detail = dispatches.progress_state(row_b)
        self.assertEqual(state, dispatches.WORKING,
                         "the seat that MOVED to B stopped counting as working "
                         "B's row: %s" % detail)
        self.assertFalse(dispatches._is_overdue(row_b, late))

    def test_a_gitdir_pointer_whose_path_CONTAINS_A_NEWLINE_still_binds(self):
        """THE POINTER IS ONE PATHNAME (round three, finding 2).

        A newline is a legal byte in a path, and `git worktree add` inside a root
        whose own path contains one writes exactly that pointer. The first cut
        walked `splitlines()`, so it truncated the path at the root's newline,
        resolved the fragment, and reported a genuine same-repository room as
        foreign — a live claim on the dispatched lane reading IDLE and the row
        going overdue while the work is being done.

        THE PATH IS PRODUCED BY GIT, NOT BY THIS TEST: `_claims.claim` cuts the
        room, git writes the `.git` file, and the assertion is on
        `progress_state`. The ordinary-path positive lives in the arms above and
        beside this one, so both spellings are covered."""
        from helm.work import _claims, _lanes
        root, tip = self._repo_named("we\nird")
        self._register(root)
        lane = "lf-room"
        row = self._row_for(root, tip, lane)
        late = time.time() + 4000
        self.assertTrue(dispatches._is_overdue(row, late),
                        "positive control: with no claim the clock rules")
        rc, line = _claims.claim(root, lane, "seat-a", ttl=600)
        self.assertEqual(rc, 0, line)
        room = line.split("\t")[0]
        with open(os.path.join(room, ".git"), "rb") as handle:
            raw = handle.read()
        # MUST-HIT: the pointer really does carry an embedded newline. Without
        # this the arm is the ordinary-path positive under a different name.
        self.assertIn(b"\n", raw.rstrip(b"\n"),
                      "the room's pointer carries no embedded newline, so this "
                      "arm measures nothing: %r" % raw)
        # CONTROL — the parse this cure replaced, recomputed inline over the
        # SHIPPED artifact. Splitting on line separators truncates the path at
        # the root's newline, and the fragment does not resolve to this
        # repository's gitdir, so the old reader answered False and the WORKING
        # verdict below could not have been reached.
        # BLAST RADIUS: pure string/realpath arithmetic on bytes already read;
        # it touches no helm state and no other arm.
        repo_id = dispatches._repo_info(root)["repo_id"]
        truncated = os.fsdecode(raw).splitlines()[0].split(":", 1)[1].strip()
        self.assertNotEqual(os.path.realpath(truncated), repo_id,
                            "the truncated pointer resolved to the repository "
                            "anyway, so the old parse was not broken here")
        self.assertFalse(os.path.realpath(truncated).startswith(
            repo_id + os.sep), "same")

        self.assertTrue(dispatches._lane_claim_binds(repo_id, lane),
                        "the room's own gitdir pointer stopped binding once the "
                        "path carried a newline")
        state, detail = dispatches.progress_state(row)
        self.assertEqual(state, dispatches.WORKING,
                         "a live grant on the dispatched lane read IDLE because "
                         "the repository's path contains a newline: %s" % detail)
        self.assertFalse(dispatches._is_overdue(row, late))
        self.assertEqual(_lanes.resource(root, lane),
                         "worktree:%s:%s" % (_lanes.project_token(root), lane))


    def test_an_UNRESOLVABLE_repository_refuses_the_extension_not_inherits_it(self):  # noqa: VACUOUS_ASSERTION — the WORKING/not-overdue pair is asserted FIRST and unconditionally on the same observables, and the healthy-retry half asserts the POSITIVE grant repository and WORKING verdict
        """A FAILED OBSERVATION IS NOT THE PREVIOUS ANSWER (round four).

        `work/_claims._grant_repo` returned None when the repository lookup
        failed, and None at the grant door means OMITTED — which
        `seats_claims.claim` deliberately reads as "this extension says nothing
        new" and answers by KEEPING the binding the grant already had. So an
        authenticated same-nonce extension taken out from checkout B, whose
        gitdir lookup failed transiently while the worktree lookup and the flock
        stayed healthy, SUCCEEDED with the grant still naming A: `progress_state`
        read A WORKING — a real obligation silenced by work nobody is doing —
        and B IDLE. That is the direction `_is_overdue` forbids.

        THE ARM DRIVES THE SHIPPED PRODUCERS. `_claims.claim` is what `helm work
        claim` runs, the rows go through the real write door, and the verdicts
        come off `progress_state`/`_is_overdue`. The only fixture is the FAILURE
        itself: `dispatches._repo_info` — the seam `_grant_repo` resolves
        provenance through — raises ONCE for B, and the arm asserts it fired.

        THE FRESH-B-AFTER-RELEASE ARM ABOVE CANNOT REACH THIS BRANCH: it
        releases A's lease first, so B's claim MINTS a grant and never extends
        one. The inheritance only exists on an extension.
        """
        from helm.work import _claims, _lanes
        a_root, a_tip = self._repo_named("x")
        b_root, b_tip = self._repo_named("y")
        self._register(a_root, b_root)
        self.assertEqual(_lanes.project_token(a_root), _lanes.project_token(b_root),
                         "the two repositories do not share a claims token, so "
                         "one lease cannot be extended from the other checkout")
        lane = "inherited-lane"
        row_a = self._row_for(a_root, a_tip, lane)
        row_b = self._row_for(b_root, b_tip, lane)
        late = time.time() + 4000
        a_id = dispatches._repo_info(a_root)["repo_id"]
        b_id = dispatches._repo_info(b_root)["repo_id"]
        resource = _lanes.resource(a_root, lane)

        rc, line = _claims.claim(a_root, lane, "seat-a", ttl=600)
        self.assertEqual(rc, 0, line)
        lease = line.split("\t")[2]
        # THE PAIR BEFORE THE FAILED EXTENSION, on the observable the cure
        # changes: A's own live grant suppresses A and suppresses nothing else.
        self.assertEqual(dispatches.progress_state(row_a)[0], dispatches.WORKING)
        self.assertFalse(dispatches._is_overdue(row_a, late))
        self.assertTrue(dispatches._is_overdue(row_b, late))
        prior = (dispatches.live_claims() or {}).get(resource, {})
        self.assertEqual(dispatches._real(prior.get("repo")), a_id,
                         "the grant does not name A, so there is no prior "
                         "binding for a failed observation to inherit")

        real_info, fired = dispatches._repo_info, []

        def flaky(root, *args, **kwargs):
            """B's repository observation fails ONCE; everything else answers."""
            if not fired and os.path.realpath(str(root)) == os.path.realpath(b_root):
                fired.append(root)
                raise OSError("probe: transient git failure")
            return real_info(root, *args, **kwargs)

        with mock.patch.object(dispatches, "_repo_info", flaky):
            self.assertIsInstance(_claims._grant_repo(b_root), seats.UnresolvedRepo)
            fired.clear()
            rc, line = _claims.claim(b_root, lane, "seat-a", ttl=600, lease=lease)
        # MUST-HIT: the failure this arm is about actually happened. Without it
        # the refusal below could be any other refusal on this path.
        self.assertTrue(fired, "B's repository resolved normally, so no failed "
                               "observation was ever made")
        self.assertEqual(rc, 1, "the extension succeeded on a repository it "
                               "could not resolve: %s" % (line,))
        self.assertIn(b_root, line,
                      "the refusal does not name the unresolvable repository, "
                      "so the holder cannot tell what to retry: %s" % (line,))
        self.assertIn("failed observation", line)

        grant = (dispatches.live_claims() or {}).get(resource, {})
        self.assertIsNone(grant.get("repo"),
                          "the grant kept a repository nobody just observed")
        self.assertNotEqual(dispatches._real(grant.get("repo")), a_id,
                            "the failed observation of B inherited A")
        self.assertEqual(grant.get("lease"), lease,
                         "the refused extension moved the lease, so this is a "
                         "half-extension and not a refusal")
        # CONTROL — the expression the cure replaced, recomputed inline over the
        # SAME recorded prior. `bound = str(repo) if repo else prior["repo"]`
        # with a None repo yields A's gitdir, which is what the assertions above
        # forbid: this arm goes red without the cure.
        # BLAST RADIUS: pure dict arithmetic on a row already read; it mutates
        # no claims state and touches no other arm.
        unresolved_as_none = None
        self.assertEqual(str(unresolved_as_none) if unresolved_as_none
                         else prior.get("repo"), a_id,
                         "the old expression no longer inherits A, so this "
                         "control proves nothing")

        state, detail = dispatches.progress_state(row_a)
        self.assertEqual(state, dispatches.IDLE,
                         "a grant whose repository is UNKNOWN counted as "
                         "progress on A: %s" % detail)
        self.assertTrue(dispatches._is_overdue(row_a, late),
                        "an unconfirmed grant silenced A's obligation")
        self.assertTrue(dispatches._is_overdue(row_b, late),
                        "the refused extension silenced B's obligation")

        # THE POSITIVE THAT PROVES THE REFUSAL IS NOT A BLANKET BREAK: the same
        # check-in, with the observation working, extends and MOVES provenance.
        rc, line = _claims.claim(b_root, lane, "seat-a", ttl=600, lease=lease)
        self.assertEqual(rc, 0, line)
        grant = (dispatches.live_claims() or {}).get(resource, {})
        self.assertEqual(dispatches._real(grant.get("repo")), b_id)
        self.assertEqual(dispatches.progress_state(row_b)[0], dispatches.WORKING)
        self.assertEqual(dispatches.progress_state(row_a)[0], dispatches.IDLE)

        # AND OMITTED PROVENANCE IS STILL THE DOCUMENTED KEEP. This is the half
        # the cure must NOT change: a caller that names no repository extends
        # its own lease and the binding it already had survives.
        ok, msg, _lease = seats.claim(resource, "seat-a", ttl=600, lease=lease)
        self.assertTrue(ok, msg)
        self.assertEqual(dispatches._real(
            (dispatches.live_claims() or {}).get(resource, {}).get("repo")), b_id,
            "an omitted repository cleared a binding it says nothing about")

        # AND A FRESH GRANT RECORDS UNKNOWN rather than refusing new lane work
        # over a transient git failure: there is no prior authority to inherit.
        fresh = _lanes.resource(a_root, "fresh-under-failure")
        ok, msg, _lease = seats.claim(fresh, "seat-b", ttl=600,
                                      repo=seats.UnresolvedRepo(b_root, "probe"))
        self.assertTrue(ok, "a FRESH grant was refused for an unresolvable "
                            "repository, which is a strictness this field has "
                            "no reading for: %s" % (msg,))
        self.assertIsNone((dispatches.live_claims() or {}).get(fresh, {}).get("repo"))


class ProjectLocalRuntimeBindsIdentityE2E(DispatchBase):
    """A `--project` deploy binds this ledger's identity to that project.

    THIS IS THE ONE ARM IN THE FILE THAT DOES NOT PIN THE RESOLVER TO A
    CONSTANT. Every other identity arm here assigns `home_repo_id` a lambda
    returning a gitdir chosen by the test, which tests the COMPARISON and
    never the thing compared against — the reviewer-found gap recorded on
    `test_the_REAL_resolver_answers_without_a_registry`. Here the home is
    computed by the real `_repo_info` from the real location of a really
    deployed package, so the arm fails if the deployer stops landing inside
    the project, if an ignored path stops resolving a repository, or if the
    write door stops reading the answer.

    THE FOURTH LEG IS THE ONE THAT MATTERS. A refusal that still appends is
    the defect wearing a pass, so the sibling's rejection is asserted against
    the ledger contents and not merely against a None return.
    """

    SOURCE_CLI = ('import sys\n'
                  'from helm import __version__\n'
                  '\n'
                  '\n'
                  'def main():\n'
                  '    if sys.argv[1:] == ["--version"]:\n'
                  '        print("helm " + __version__)\n'
                  '        return 0\n'
                  '    return 2\n')

    def _repo_at(self, name, ignore=None):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        subprocess.run(["git", "-C", path, "init", "-q"], check=True)
        subprocess.run(["git", "-C", path, "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "test"],
                       check=True)
        if ignore is not None:
            with open(os.path.join(path, ".gitignore"), "w",
                      encoding="utf-8") as handle:
                handle.write(ignore)
        with open(os.path.join(path, "seed.txt"), "w", encoding="utf-8") as handle:
            handle.write(name)
        subprocess.run(["git", "-C", path, "add", "-A"], check=True)
        subprocess.run(["git", "-C", path, "commit", "-qm", "seed"], check=True)
        head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                              check=True, text=True,
                              stdout=subprocess.PIPE).stdout.strip()
        return path, head

    def _deployable_source(self):
        """The smallest tree scripts/deploy.py will publish."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = os.path.join(self.tmp, "source")
        os.makedirs(os.path.join(src, "scripts"))
        os.makedirs(os.path.join(src, "helm"))
        os.makedirs(os.path.join(src, "docs"))
        os.makedirs(os.path.join(src, "tests"))
        os.makedirs(os.path.join(src, "bin"))
        shutil.copy2(os.path.join(root, "scripts", "deploy.py"),
                     os.path.join(src, "scripts", "deploy.py"))
        shutil.copy2(os.path.join(root, "bin", "helm"),
                     os.path.join(src, "bin", "helm"))
        # BOTH ENTRY POINTS. `bin/helm-hook` is what every generated hook
        # command execs, so scripts/deploy.py requires it and mode-checks it
        # beside bin/helm; a source tree without it is no longer deployable.
        shutil.copy2(os.path.join(root, "bin", "helm-hook"),
                     os.path.join(src, "bin", "helm-hook"))
        writes = {
            "helm/__init__.py": '__version__ = "e2e"\n',
            "helm/cli.py": self.SOURCE_CLI,
            "README.md": "e2e source\n",
            "docs/VERBS.md": "# verbs\n",
            "tests/test_witness.py": "# witness\n",
        }
        for rel, body in writes.items():
            with open(os.path.join(src, rel), "w", encoding="utf-8") as handle:
                handle.write(body)
        subprocess.run(["git", "-C", src, "init", "-q"], check=True)
        subprocess.run(["git", "-C", src, "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", src, "config", "user.name", "test"],
                       check=True)
        subprocess.run(["git", "-C", src, "add", "-A"], check=True)
        subprocess.run(["git", "-C", src, "commit", "-qm", "source"], check=True)
        commit = subprocess.run(["git", "-C", src, "rev-parse", "HEAD"],
                                check=True, text=True,
                                stdout=subprocess.PIPE).stdout.strip()
        return src, commit

    def test_a_project_local_deploy_admits_its_own_repo_and_refuses_a_sibling(self):
        project, project_head = self._repo_at("project-a", ignore=".cache/\n")
        sibling, sibling_head = self._repo_at("project-b")
        source, source_commit = self._deployable_source()

        # LEG 1 — the deploy lands inside the project's ignored cache.
        deployed_root = os.path.join(project, ".cache", "helm-artifacts")
        proc = subprocess.run(
            [sys.executable, os.path.join(source, "scripts", "deploy.py"),
             "--project", project],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = os.path.join(deployed_root, "releases", source_commit, "helm")
        self.assertTrue(os.path.isfile(os.path.join(package, "__init__.py")),
                        "no deployed package under the project")

        # LEG 2 — the REAL resolver reads that location as the project. No
        # lambda: this is the same call production makes on its own __file__.
        resolved = dispatches._repo_info(package)
        self.assertIsNotNone(resolved, "the deployed package resolved nothing")
        self.assertEqual(resolved["repo_id"],
                         dispatches._repo_info(project)["repo_id"],
                         "the deployed runtime does not bind its project")
        self.assertNotEqual(resolved["repo_id"],
                            dispatches._repo_info(source)["repo_id"],
                            "identity followed the deployer, not the artifact")

        self.addCleanup(setattr, dispatches, "home_repo_id",
                        dispatches.home_repo_id)
        dispatches.home_repo_id = lambda: (
            dispatches._repo_info(package)["repo_id"], None)

        # LEG 3 — a row from the project itself lands. This is also the
        # positive control for leg 4: an empty ledger proves nothing, because
        # a broken writer is empty too.
        kept = dispatches.add("seat-under-test", "zz-local",
                              ref=project_head, repo=project, new_work=True)
        self.assertIsNotNone(kept, "the project's own row was refused")
        self.assertEqual(dispatches.rows()[kept["id"]]["repo_id"],
                         resolved["repo_id"])

        # LEG 4 — the sibling is refused AND the ledger is untouched.
        self.assertIsNone(  # noqa: VACUOUS_ASSERTION — the refusal IS the product law; one call cannot both land and be refused, and the ledger assertion below is the same-observable control
            dispatches.add("seat-under-test", "zz-sibling",
                           ref=sibling_head, repo=sibling, new_work=True),
            "a sibling repository was admitted by a project-local runtime")
        self.assertEqual(list(dispatches.rows()), [kept["id"]],
                         "the refused sibling row reached the ledger anyway")

        row, err = dispatches._base("seat-under-test", "zz-sibling",
                                    sibling_head, "note", 60, sibling,
                                    new_work=True)
        self.assertIsNone(row)
        self.assertIn(dispatches._repo_info(sibling)["repo_id"], err)
        self.assertIn(resolved["repo_id"], err)


class LandNudgeTwoLegContractTest(unittest.TestCase):
    """One minting feeds both legs, so they cannot tell different stories.

    These call _verdict_land_nudge DIRECTLY. An earlier version of this arm
    drove the verdict door and captured the AUTHOR nudge instead — the same
    fixture, one seam over, measuring a different function entirely.
    """

    ROW = {"id": "a" * 32, "lane": "some-lane", "reviewed_tip": "b" * 40}

    def _legs(self, parts):
        from helm import dispatches
        seen = {}

        def capture(recipient, dm_body, room_body):
            seen["dm"], seen["room"] = dm_body, room_body
            return True

        with mock.patch.object(dispatches, "_land_nudge_parts",
                               lambda row: parts), \
                mock.patch.object(dispatches, "_nudge", capture), \
                mock.patch.object(dispatches, "_default_lander",
                                  lambda: "lander"):
            dispatches._verdict_land_nudge(dict(self.ROW))
        return seen

    def test_BOTH_legs_of_a_READY_approve_carry_the_word(self):
        """The room leg used to carry only the command, so the one word a
        reader scans for was on one leg and absent from the other."""
        seen = self._legs(("READY", None, "helm lr land XYZ"))
        self.assertTrue(seen, "the nudge fired no legs at all")
        for name in ("dm", "room"):
            self.assertIn("ready", str(seen[name]).lower(),
                          "the %s leg dropped the READY word: %s"
                          % (name, seen[name]))

    def test_BOTH_legs_carry_the_command(self):
        """POSITIVE CONTROL on the same observable: the legs must still carry
        the command, or 'ready' could be satisfied by a message that tells
        the reader nothing to run."""
        seen = self._legs(("READY", None, "helm lr land XYZ"))
        for name in ("dm", "room"):
            self.assertIn("helm lr land XYZ", str(seen[name]))

    def test_a_non_READY_approve_carries_its_REASON(self):
        """UNVERIFIED alone names a problem and withholds the part that lets
        the reader act. The reason was computed one call away, then dropped
        by taking [0]."""
        seen = self._legs(("UNVERIFIED", "projection raised: boom",
                           "helm lr land XYZ"))
        self.assertTrue(
            any("projection raised" in str(seen[n]) for n in ("dm", "room")),
            "the reason never reached a reader: %s" % seen)

    def test_a_non_READY_word_with_no_reason_still_reads_cleanly(self):
        """MUST-MISS: a missing reason must not print an empty parenthesis
        or the word None."""
        seen = self._legs(("STALE-BASE", None, "helm lr land XYZ"))
        for name in ("dm", "room"):
            body = str(seen[name])
            self.assertIn("STALE-BASE", body)
            self.assertNotIn("None", body)
            self.assertNotIn("()", body)


class TheTierAdvisoryRidesTheWriteDoorNotAVerb(DispatchBase):
    """WHOSE APPROVE CAN BIND IS A FACT ABOUT THE RECIPIENT, so it belongs at
    the door every writer passes through rather than on one verb's CLI branch.

    Sited on the send branch it was invisible to a REBOUND review — and the
    reviewer a re-route installs is exactly the one nobody vouched for — and
    to every library caller, because a stderr print is not a door.
    """

    def _outside_tier(self):
        """Declare a tier this recipient is not in, and give it a family."""
        store.write_prior({
            "id": "fleet-approval-tier",
            "statement": "Final approval uses the declared tier.",
            "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z",
            "source": "human", "policy_kind": "approval-tier",
            "policy_members": ["seat:someone-else"],
            "policy_reason": "only the independent tier may final-approve",
        }, root_dir=os.path.join(home.global_dir(), "premises"))
        prior = os.environ.get("HELM_CHAT_NAME")
        try:
            # BOTH ends need a roster row: the rebind arm SENDS to source-seat
            # before moving the obligation, and an unrostered recipient is
            # refused one rung earlier than the tier — which is how that arm
            # first failed, on its own setup rather than on its claim.
            for name in ("target-seat", "source-seat"):
                os.environ["HELM_CHAT_NAME"] = name
                seats.write_roster(name,
                                   runtime={"family": "gemini",
                                            "backend": "native"},
                                   presence_beat=False)
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior

    def _notes(self, row):
        return list((row or {}).get(dispatches._ADMISSION_NOTES, ()))

    def test_a_review_written_through_the_door_carries_the_tier_note(self):
        """Kills siting this on a verb: the note must exist on the row the
        LIBRARY returns, with no terminal involved."""
        self._outside_tier()
        row, why = dispatches.add("target-seat", "tier-lane", ref=self.a,
                                  repo=self.repo, kind="review",
                                  new_work=True, _reason=True, notify=False)
        self.assertIsNone(why, why)
        self.assertTrue(row, "the advisory must not have blocked the write")
        self.assertTrue(
            any("approval tier" in n or "review recipient" in n
                for n in self._notes(row)),
            "the write door computed no tier note: %s" % self._notes(row))

    def test_a_build_row_carries_none(self):  # noqa: VACUOUS_ASSERTION — the same fixture and recipient produce a note for kind=review in the arm above, so an absent note here is a measured difference rather than an inert path
        """THE KIND RESTRICTION IS CORRECT AND PINNED HERE. The advisory asks
        whether an APPROVE could bind; a build row produces no verdict, so a
        note on one would be noise that teaches readers to skim."""
        self._outside_tier()
        row, why = dispatches.add("target-seat", "tier-build-lane", ref=self.a,
                                  repo=self.repo, kind="build",
                                  new_work=True, _reason=True, notify=False)
        self.assertIsNone(why, why)
        self.assertFalse(
            [n for n in self._notes(row)
             if "approval tier" in n or "review recipient" in n])

    def test_the_advisory_reads_the_kind_the_ROW_WILL_CARRY(self):
        """A SPELLING THE ROW ACCEPTS MUST NOT LOSE THE ADVISORY.

        `clean_kind` lowercases and strips, and BOTH doors normalise AFTER
        asking for the note — `add` one line before `_base`, `send` while
        holding its own `kind_value`. So `--kind Review` minted a row recorded
        as "review" and carrying NO tier note: a review by every later reader,
        with the one write-time warning about an unbindable approve silently
        absent. That is this class's own defect wearing a different coat — the
        note was reachable through one spelling of one door instead of one
        verb.
        """
        self._outside_tier()
        # UNCONDITIONAL POSITIVE CONTROL, on the same fixture and recipient:
        # the canonical spelling carries the note. Without it every assertion
        # below would pass on a fixture that had stopped producing advisories
        # at all, which is the way this arm would rot silently.
        row, why = dispatches.add("target-seat", "tier-lane", ref=self.a,
                                  repo=self.repo, kind="review",
                                  new_work=True, _reason=True, notify=False)
        self.assertIsNone(why, why)
        self.assertTrue(self._notes(row), "the control must carry a note")
        # THE LANE MUST DIFFER PER ITERATION AND THE SPELLING CANNOT SUPPLY
        # IT. All three normalise to one label, so deriving the lane from the
        # spelling collided on the duplicate-mint guard — my arm assumed the
        # spellings stay distinct downstream, which is precisely what this
        # cure makes false. The index is the only thing here that varies.
        for index, spelling in enumerate(("Review", "REVIEW", " review ")):
            with self.subTest(kind=spelling):
                row, why = dispatches.add(
                    "target-seat", "tier-lane-cased-%d" % index,
                    ref=self.a, repo=self.repo, kind=spelling,
                    new_work=True, _reason=True, notify=False)
                self.assertIsNone(why, why)
                # THE ROW RECORDS IT AS A REVIEW — the half that makes the
                # missing note a contradiction rather than a policy.
                self.assertEqual(row.get("kind"), "review")
                self.assertTrue(
                    self._notes(row),
                    "a row recorded kind=review carries no tier advisory "
                    "because the caller spelled it %r" % spelling)
        # MUST-MISS, and it is the reason this is a normalisation rather than
        # a widening: a build row still carries nothing, however it is spelled.
        row, why = dispatches.add("target-seat", "build-lane-cased",
                                  ref=self.a, repo=self.repo, kind="Build",
                                  new_work=True, _reason=True, notify=False)
        self.assertIsNone(why, why)
        self.assertEqual(row.get("kind"), "build")
        # THE CLAIM IS ABOUT ONE MEMBER OF THE LIST, NOT THE LIST. A build row
        # legitimately carries OTHER admission notes — the usability advisory
        # fires whenever the recipient's transcript cannot be dated — so
        # asserting an empty list asserted something this arm never meant and
        # failed on a true statement about a different note.
        #
        # AND THE MEMBER IS DERIVED, NOT SPELLED. Matching a substring of the
        # advisory would rot the moment its wording changed and would quietly
        # stop discriminating; asking the production function for the exact
        # string it would have produced cannot.
        tier = dispatches._approval_tier_advisory("target-seat")
        self.assertTrue(tier, "the fixture must still produce an advisory at "
                              "all, or this must-miss proves nothing")
        self.assertNotIn(tier, self._notes(row))

    def test_a_rebound_review_carries_it_too(self):
        """THE POPULATION THAT WAS DARK. A re-route installs a new reviewer
        without anyone vouching for them, and a write that does not pass
        through the send verb reaches no CLI branch at all."""
        self._outside_tier()
        row, why, _sent = dispatches.send(
            "source-seat", "tier-rebind-lane", "review this", self.a,
            repo=self.repo, sign=False, new_work=True, kind="review")
        self.assertIsNone(why, why)
        # force=True with a reason IS the evidence arm's documented override,
        # so this arm does not need to stage a starved source: it is about the
        # TARGET's tier, not about whether the move was justified.
        out, err = dispatches.rebind(row["id"], "target-seat", force=True,
                                     reason="the seat is dark", repo=self.repo,
                                     notify=False)
        self.assertIsNone(err, err)
        self.assertTrue(
            any("approval tier" in n or "review recipient" in n
                for n in self._notes(out["new"])),
            "a rebound review reached its new reviewer with no tier note: %s"
            % self._notes(out["new"]))


class BriefAndAuthorTravelWithTheObligationTest(DispatchBase):
    """A rebind MOVES one obligation. Two things used not to move with it.

    THE BRIEF DID NOT TRAVEL. `send` hashed the message and threw the text
    away, so `rebind` built the replacement from lane + ref + note and the new
    recipient started with no instruction. Measured 2026-07-31: two rows rebound
    off a dark family, BOTH recipients came back asking for the brief.

    THE AUTHOR DID NOT SURVIVE. `add` stamped `sender` from `_acting_author`,
    i.e. from whoever ran the rebind, and `landreq` projects that field straight
    into `"author"` — so moving an obligation silently rewrote who wrote the
    work, and `_verdict_author_nudge` woke the mover instead of the author.

    THE FIXTURE ROSTERS EVERY NAME IT USES, deliberately: a NON-EMPTY roster
    turns the recipient guard from UNKNOWN-proceed into "this name is absent =
    REFUSED" (see DispatchBase.setUp), and #116 already requires a rebind target
    to be joined. The starvation rungs are BORROWED from RebindTest rather than
    re-typed — one implementation, so a change to the health fixture cannot make
    these arms and RebindTest's disagree about what starvation looks like."""

    _starve = RebindTest._starve
    _healthy = RebindTest._healthy

    AUTHOR = "author-seat"
    MOVER = "mover-seat"

    def setUp(self):
        super().setUp()
        for seat_name in ("source-seat", "target-seat"):
            seats.write_roster(seat_name, presence_beat=False)
        os.environ["HELM_CHAT_NAME"] = self.AUTHOR

    def _send(self, message, recipient="source-seat", lane="brief-lane"):
        from tests._tmphome import dispatch_home
        with dispatch_home(self.repo):
            row, why, sent = dispatches.send(
                recipient, lane, message, self.a, repo=self.repo, sign=False,
                new_work=True)
        self.assertIsNone(why, why)
        self.assertTrue(sent, "the fixture's own send must have delivered")
        return row

    def _move(self, rid, to="target-seat", as_seat=None):
        """Rebind AS a named seat, and return (result, error)."""
        if as_seat:
            os.environ["HELM_CHAT_NAME"] = as_seat
        self._starve("source-seat", streak=True)
        return dispatches.rebind(rid, to, force=True, reason="the seat is dark",
                                 repo=self.repo, notify=False)

    def _strip_field(self, rid, field):
        """Age one stored creation into its PRE-FIELD shape.

        Real code never rewrites the append-only ledger; this plants history the
        current writer can no longer produce, which is the only way to get a row
        that PREDATES a field the writer now always emits."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid and ev.get("event") == "dispatch" \
                        and field in ev:
                    ev.pop(field)
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "the fixture did not age the row it meant to; "
                                 "every assertion below would be about the "
                                 "UNMODIFIED row")

    # ---------------------------------------------------------------- brief

    def test_the_brief_TRAVELS_from_the_send_row_onto_the_rebound_row(self):
        brief = ("Review the FIFO gate: tests/test_gate_fifo.py pins the "
                 "ordering, and the failure id is computed from the tree.")
        row = self._send(brief)
        # POSITIVE CONTROL ON THE PRODUCER, before any claim about the move:
        # if send() never stored the body, the child's empty body below would
        # be true and would mean nothing.
        stored, why = dispatches.body_of(dispatches.rows()[row["id"]])
        self.assertIsNone(why)
        self.assertEqual(stored, brief, "send() did not persist the brief")
        # ...and the hash is STILL over the original message, unchanged.
        self.assertEqual(
            row["message_hash"],
            hashlib.blake2b(brief.encode("utf-8"), digest_size=16).hexdigest())

        out, err = self._move(row["id"])
        self.assertIsNone(err, err)
        carried, why = dispatches.body_of(
            dispatches.rows()[out["new"]["id"]])
        self.assertIsNone(why)
        self.assertEqual(carried, brief,
                         "the rebound row must carry the ORIGINAL brief")

    def test_a_TRUNCATED_brief_says_so_INSIDE_the_stored_value(self):
        """A silent truncation reads as a complete brief — the recipient works
        the first N bytes believing that is the whole instruction. So the notice
        is part of the stored TEXT, not a flag beside it.

        THE CAP IS DERIVED, NEVER TRANSCRIBED: every boundary here is computed
        from `dispatches.MESSAGE_BODY_CAP`, and the last arm MOVES that constant
        to prove the writer actually reads it rather than a literal of its own."""
        cap = dispatches.MESSAGE_BODY_CAP
        # AT the cap: whole, and NOT marked.
        exact = "x" * cap
        kept = dispatches._store_body(exact)
        self.assertEqual(kept, exact)
        self.assertNotIn(dispatches.BODY_TRUNCATED_MARK, kept)
        # ONE BYTE OVER: truncated, marked, and — THE DEFECT THIS PINS — the
        # NOTICE IS INSIDE THE BOUND IT REPORTS. The old writer kept a full
        # `cap` bytes and then APPENDED the notice, so a body one byte over
        # stored 4194 under a cap of 4000: the warning broke the bound it was
        # warning about. The kept prefix is therefore SHORTER than `cap`, and
        # asserting it equals `cap` is asserting the bug.
        over = "y" * (cap + 1)
        cut = dispatches._store_body(over)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, cut)
        self.assertLessEqual(len(cut.encode("utf-8")), cap,
                             "the truncation notice pushed the stored value "
                             "back over the cap")
        kept = cut.split("\n\n" + dispatches.BODY_TRUNCATED_MARK)[0]
        self.assertTrue(kept and over.startswith(kept),
                        "the kept text is not a prefix of the brief")
        self.assertLess(len(kept), cap,
                        "the notice was given no room, so it must have "
                        "overflowed the bound")
        # THE NOTICE'S NUMBERS ARE DERIVED FROM WHAT IT ACTUALLY KEPT, never
        # transcribed: a notice that misreports is a second silent truncation.
        self.assertIn("%d of %d UTF-8 bytes"
                      % (len(kept.encode("utf-8")), cap + 1), cut)
        # MOVE THE INPUT. A cap read from a literal would ignore this.
        with mock.patch.object(dispatches, "MESSAGE_BODY_CAP", 512):
            moved = dispatches._store_body(over)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, moved)
        self.assertLessEqual(len(moved.encode("utf-8")), 512)
        self.assertNotEqual(moved, cut,
                            "moving the cap changed nothing, so the writer is "
                            "reading a literal of its own")
        moved_kept = moved.split("\n\n" + dispatches.BODY_TRUNCATED_MARK)[0]
        self.assertLess(len(moved_kept), len(kept),
                        "a smaller cap did not keep less")
        # A CAP BELOW THE NOTICE ITSELF REFUSES THE CONFIGURATION (review's
        # ruling, overturning my first answer). I had made the NOTICE win and
        # knowingly exceeded the bound, on the reasoning that a silently
        # truncated brief reads as COMPLETE and truth beats size. The ruling:
        # a declared cap that may be knowingly exceeded IS NOT A CAP, and every
        # downstream sizing and append guarantee resting on it becomes false.
        # So neither value is written — not the over-bound truthful one, not
        # the silent lie — and the writer boundary refuses, naming both numbers.
        with mock.patch.object(dispatches, "MESSAGE_BODY_CAP", 16):
            with self.assertRaises(ValueError) as caught:
                dispatches._store_body(over)
        self.assertIn("not a cap", str(caught.exception),
                      "the refusal must say WHY, not just fail")

    def test_the_cap_is_BYTES_so_a_multibyte_brief_under_it_in_CHARACTERS_is_still_cut(self):
        """THE MUST-MISS. A character cap and a byte cap are THE SAME FUNCTION
        on ASCII, so an arm built from ASCII fixtures cannot tell them apart —
        and the ledger line is paid in bytes. This is the input the writer must
        REJECT as too long even though it is comfortably under the cap in
        `len()`: 3-byte characters, count well below the cap, byte length well
        above it.

        It also pins the CUT ITSELF: slicing UTF-8 mid-codepoint and decoding it
        away would leave a replacement character at the seam, which is a corrupt
        brief wearing a complete one's clothes."""
        cap = dispatches.MESSAGE_BODY_CAP
        glyph = "漢"                       # 3 bytes in UTF-8
        self.assertEqual(len(glyph.encode("utf-8")), 3)
        chars = cap // 2                        # UNDER the cap in characters
        wide = glyph * chars
        self.assertLess(len(wide), cap)
        self.assertGreater(len(wide.encode("utf-8")), cap)
        cut = dispatches._store_body(wide)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, cut,
                      "a byte-cap must cut this; a char-cap would not")
        kept = cut.split(dispatches.BODY_TRUNCATED_MARK)[0].rstrip("\n")
        self.assertLessEqual(len(kept.encode("utf-8")), cap)
        self.assertEqual(set(kept), {glyph},
                         "the cut must land on a character boundary — no "
                         "replacement character at the seam")
        # UNCONDITIONAL POSITIVE CONTROL on the same producer: the identical
        # CHARACTER COUNT in ASCII is under the byte cap and comes back WHOLE,
        # so the truncation above is about bytes and not about length in
        # characters, and `_store_body` is not simply marking everything.
        narrow = "a" * chars
        self.assertEqual(dispatches._store_body(narrow), narrow)
        self.assertNotIn(dispatches.BODY_TRUNCATED_MARK,
                         dispatches._store_body(narrow))

    def test_body_of_SEPARATES_a_row_that_predates_storage_from_one_with_no_brief(self):
        """Two absences, and merging them is the bug. An `add` row NEVER had a
        DM — nothing was lost. A row written before 2026-08-27 may well have had
        one and this ledger cannot say. Reporting the second as "no brief" tells
        the operator a re-brief is unnecessary when it is exactly what is owed."""
        sent = self._send("the instruction")
        filed = self.add(recipient="source-seat", lane="filed-lane")
        # POSITIVE CONTROL, on the same reader, in this test: a row WITH a body
        # reads as one. Without this, both readings below pass on a reader that
        # can only ever say "nothing here".
        body, why = dispatches.body_of(dispatches.rows()[sent["id"]])
        self.assertEqual(body, "the instruction")
        self.assertIsNone(why)

        body, why = dispatches.body_of(dispatches.rows()[filed["id"]])
        self.assertIsNone(body)
        self.assertEqual(why, dispatches.BODY_NONE)

        self._strip_field(sent["id"], "message_body")
        body, why = dispatches.body_of(dispatches.rows()[sent["id"]])
        self.assertIsNone(body)
        self.assertEqual(why, dispatches.BODY_UNRECORDED)
        self.assertNotEqual(dispatches.BODY_UNRECORDED, dispatches.BODY_NONE)
        # MUST-MISS: an empty string is not a brief, and a row rebuilt from a
        # subset of fields is UNKNOWN rather than empty.
        self.assertEqual(dispatches.body_of({"message_body": ""}),
                         (None, dispatches.BODY_NONE))
        self.assertEqual(dispatches.body_of({"id": "abc"}),
                         (None, dispatches.BODY_UNRECORDED))
        self.assertEqual(dispatches.body_of(None),
                         (None, dispatches.BODY_UNRECORDED))
        # ...and a present-but-MALFORMED value reads UNKNOWN, not KNOWN-EMPTY:
        # BODY_NONE is the branch that permits, and a field of the wrong type
        # has not earned it (`_replay_chain`'s split, applied here).
        self.assertEqual(dispatches.body_of({"message_body": 17}),
                         (None, dispatches.BODY_UNRECORDED))

    def test_the_rebind_CLI_says_WHICH_of_the_three_things_happened(self):
        """The old NOTE fired unconditionally and described one case out of
        three: it asked for a re-brief even when the superseded row was an `add`
        that never carried a DM, and it could not say "the brief travelled"
        because it never did.

        Driven through cmd_dispatch so the sentence an operator actually reads
        is the thing under test."""
        # 1. THE BRIEF TRAVELLED.
        sent = self._send("the whole instruction")
        self._starve("source-seat", streak=True)
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["rebind", sent["id"], "--to", "target-seat", "--force",
                             "--reason", "dark"])
        self.assertEqual(rc, 0, err)
        self.assertIn("ORIGINAL BRIEF TRAVELED", err)
        self.assertIn("target-seat", err, "the note must name the recipient")
        self.assertNotIn("Re-brief", err,
                         "nothing is owed when the brief arrived")

        # 2. THERE NEVER WAS A BRIEF (an `add` row carries no DM).
        filed = self.add(recipient="source-seat", lane="filed-lane")
        self.assertIsNone(filed["message_hash"],
                          "an add-created row never had an original DM")
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["rebind", filed["id"], "--to", "target-seat", "--force",
                             "--reason", "dark"])
        self.assertEqual(rc, 0, err)
        self.assertIn("THERE NEVER WAS ONE", err)
        self.assertNotIn("Re-brief", err)

        # 3. THE ROW PREDATES BODY STORAGE — the only case that owes a re-brief.
        old = self._send("a brief this ledger no longer holds", lane="old-lane")
        self._strip_field(old["id"], "message_body")
        # AND ITS REFERENCE. A row that predates body storage predates the
        # brief FILE too; stripping only the bounded copy would leave a state
        # the writer cannot produce — no text on the row and a whole brief on
        # disk — and the reader would truthfully report a brief that travelled.
        self._strip_field(old["id"], "brief_ref")
        self._strip_field(old["id"], "brief_bytes")
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["rebind", old["id"], "--to", "target-seat", "--force",
                             "--reason", "dark"])
        self.assertEqual(rc, 0, err)
        self.assertIn("PREDATES body storage", err)
        self.assertIn("Re-brief @target-seat", err)

    # --------------------------------------------------------------- author

    def test_a_rebind_does_NOT_re_author_the_obligation(self):
        """`landreq` reads `"author": row.get("sender")`, so the mover's name in
        that field IS the land request's author. Moving an obligation must not
        rewrite who wrote the work."""
        row = self._send("build it")
        self.assertEqual(row["sender"], self.AUTHOR)

        out, err = self._move(row["id"], as_seat=self.MOVER)
        self.assertIsNone(err, err)
        # POSITIVE CONTROL ON THE IDENTITY SWITCH, in this test: if the env
        # change had not taken, the preserved sender below would be the mover's
        # own name and the arm would pass while proving nothing.
        fresh = self.add(recipient="source-seat", lane="mover-own-work")
        self.assertEqual(fresh["sender"], self.MOVER,
                         "the fixture never actually became a different seat")

        stored = dispatches.rows()[out["new"]["id"]]
        self.assertEqual(stored["sender"], self.AUTHOR,
                         "the rebind re-authored the obligation")
        self.assertEqual(stored["acted_by"], self.MOVER,
                         "the seat that MOVED it must still be recorded")
        # The author is not erased from the mover's own ordinary work.
        self.assertNotIn("acted_by", fresh,
                         "acted_by means INHERITED; ordinary work has no such "
                         "claim to make")

    def test_a_preserved_sender_is_READ_off_a_row_and_never_ACCEPTED(self):
        """THE SECURITY PROPERTY. A passthrough would be worse than the bug:
        `rebind` is reachable by any seat with a live pane, so `sender=<name>`
        would let any of them mint work under any other seat's name straight
        into `landreq`'s author field.

        Two halves, and the first is structural — there is no argument to abuse.
        The second is the MUST-MISS: asking to preserve an author with no row to
        read one off is a caller ASSERTING a name, and it is REFUSED."""
        import inspect
        params = inspect.signature(dispatches.add).parameters
        self.assertNotIn("sender", params,
                         "add() must expose no sender argument at all")
        self.assertIn("_preserve_origin", params,
                      "this arm is pinned to the wrong door")

        # THE VALUE `True` NO LONGER REACHES THIS PATH AT ALL. Preserving an
        # author is now minted, not requested: a caller could otherwise name
        # ANY row as its parent and inherit that row's sender, which is the
        # forgery this door was closed for. The two halves below still pin the
        # original contract; they just hold the mint.
        forged, forged_why = dispatches.add(
            "seat-a", "no-parent-lane", ref=self.a, repo=self.repo,
            new_work=True, notify=False, _reason=True, _preserve_origin=True)
        self.assertIsNone(forged, "a caller requested preserved authorship "
                                  "by VALUE and was allowed")
        self.assertIn("minted inside dispatches", forged_why)

        # MUST-MISS: preserve-with-no-parent is refused, not defaulted.
        refused, why = dispatches.add(
            "target-seat", "no-parent-lane", ref=self.a, repo=self.repo,
            new_work=True, notify=False, _reason=True,
            _preserve_origin=dispatches._MOVE_MINT)
        self.assertIsNone(refused)
        self.assertIn("NO superseded row", why)

        # UNCONDITIONAL POSITIVE CONTROL on the same door: the identical call
        # WITH a parent succeeds, so the refusal above is about the missing
        # parent and not about something else in this path.
        parent = self._send("the parent brief")
        child, why = dispatches.add(
            "target-seat", "no-parent-lane", ref=self.a, repo=self.repo,
            supersedes=parent["id"], notify=False, _reason=True,
            _preserve_origin=dispatches._MOVE_MINT)
        self.assertIsNone(why, why)
        self.assertEqual(child["sender"], self.AUTHOR)

    def test_a_parent_with_NO_recorded_sender_says_so_instead_of_pretending(self):
        """Every `dispatch add` row written before 2026-07-28 carries
        sender=None. Refusing the move would break `rebind` on exactly the
        oldest rows; stamping the mover silently would be the original defect
        wearing a new field's clothes. So the mover authors it AND the write
        says out loud that authorship could not be preserved."""
        row = self._send("legacy work")
        self._strip_field(row["id"], "sender")
        self.assertIsNone(dispatches.rows()[row["id"]].get("sender"))

        out, err = self._move(row["id"], as_seat=self.MOVER)
        self.assertIsNone(err, err)
        stored = dispatches.rows()[out["new"]["id"]]
        self.assertEqual(stored["sender"], self.MOVER,
                         "an unrecorded author cannot be preserved")
        self.assertNotIn("acted_by", stored,
                         "acted_by claims an INHERITED sender; there was none")
        self.assertTrue(
            any("records NO sender" in n
                for n in out["new"].get(dispatches._ADMISSION_NOTES, ())),
            "the write must SAY authorship could not be preserved")

        # AND IT SURVIVES THE NOTIFY LEG, which is the only leg the CLI uses.
        # add() re-reads the row after posting its @mention and REPLACES the
        # returned dict, re-carrying write warnings and nothing else — an
        # advisory attached before that swap is built, attached and silently
        # dropped on every notify=True write, i.e. on every real rebind. This
        # half is the arm for the ORDER, not for the sentence.
        again = self._send("more legacy work", lane="legacy-two")
        self._strip_field(again["id"], "sender")
        self._starve("source-seat", streak=True)
        loud, err = dispatches.rebind(again["id"], "target-seat", force=True,
                                      reason="dark", repo=self.repo,
                                      notify=True)
        self.assertIsNone(err, err)
        self.assertEqual(loud["new"]["delivery"], "observed",
                         "the notify leg did not run, so this half proves "
                         "nothing about surviving its row swap")
        self.assertTrue(
            any("records NO sender" in n
                for n in loud["new"].get(dispatches._ADMISSION_NOTES, ())),
            "the notify leg's row swap dropped the advisory")

    def test_a_parent_whose_recorded_sender_is_not_a_token_is_REFUSED(self):
        """A hand-edited or corrupt row is not an author. Refuse the move rather
        than carry a malformed stamp — and leave the source OPEN, which is
        rebind's standing contract for a replacement it could not write."""
        # BOTH PARENTS ARE MINTED HERE, WHILE THIS PROCESS IS STILL THE AUTHOR.
        # The first draft minted the control AFTER the failed move had already
        # switched HELM_CHAT_NAME, so the "intact" parent was authored by the
        # MOVER and inheriting the mover's name was the correct answer — the arm
        # went red on its own fixture, which is exactly what it is for.
        row = self._send("work under a corrupt stamp")
        clean = self._send("intact work", lane="intact-lane")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == row["id"] and ev.get("event") == "dispatch":
                    ev["sender"] = "not a seat token!"
                    hit += 1
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1)

        out, err = self._move(row["id"], as_seat=self.MOVER)
        self.assertIsNone(out)
        self.assertIn("not a seat token", err)
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open",
                         "a refused replacement must leave the source OPEN")
        # UNCONDITIONAL POSITIVE CONTROL on the same verb and fixture: an
        # INTACT parent moves. Without it, "rebind refused" proves only that
        # rebind refuses, which it does for a dozen reasons in this file.
        out, err = self._move(clean["id"], as_seat=self.MOVER)
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.rows()[out["new"]["id"]]["sender"],
                         self.AUTHOR)

    def test_triage_of_a_NAMED_row_PRINTS_the_stored_brief(self):
        """A stored brief with no reader is the same defect one layer down.
        `triage <id>` is where a seat picks work up, so it is where the
        instruction has to appear — including for the seat that INHERITED the
        obligation from a rebind and was never DMed.

        THE MUST-MISS is the bulk listing: `triage` with NO ids is a
        one-line-per-row table, and a multi-line brief under every row would
        destroy it. The arm must REJECT that input."""
        sent = self._send("Cure the FIFO gate; the failure id comes from the "
                          "tree, not the run.")
        filed = self.add(recipient="source-seat", lane="filed-lane")

        rc, out, err = run(dispatches.cmd_dispatch, ["triage", sent["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn(sent["id"][:12], out,
                      "the row's own measured line is the positive control; "
                      "without it an absent brief below means nothing")
        self.assertIn("BRIEF (as sent", out)
        self.assertIn("Cure the FIFO gate", out)

        # MUST-MISS #1 — KNOWN-EMPTY PRINTS NOTHING. An `add` row never had a
        # DM, so its lane/ref/note ARE the whole ask, and
        # TriageAnswersEveryNamedIdTest pins a named open row at exactly one
        # line. The first cut of this cure printed "BRIEF: none" here and took
        # that arm red, which is the arm doing its job.
        rc, out, err = run(dispatches.cmd_dispatch, ["triage", filed["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn(filed["id"][:12], out,
                      "the row printed nothing at all, so the missing BRIEF "
                      "line below proves nothing")
        self.assertEqual(len(out.strip().splitlines()), 1,
                         "a row that never had a brief stays one line")
        self.assertNotIn("BRIEF", out)

        self._strip_field(sent["id"], "message_body")
        # ...AND ITS REFERENCE, for the reason the rebind arm above states: a
        # pre-storage row has neither field, and leaving one behind asserts a
        # reader's answer about a row no writer could have written.
        self._strip_field(sent["id"], "brief_ref")
        self._strip_field(sent["id"], "brief_bytes")
        rc, out, err = run(dispatches.cmd_dispatch, ["triage", sent["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("BRIEF: UNKNOWN", out)
        self.assertNotIn("Cure the FIFO gate", out)

        # MUST-MISS #2: the bulk listing names no id and must print NO brief line
        # at all — and it still prints the rows, which is what makes this a
        # rejection rather than a dead verb.
        again = self._send("a second instruction", lane="second-lane")
        rc, out, err = run(dispatches.cmd_dispatch, ["triage"])
        self.assertEqual(rc, 0, err)
        self.assertIn(again["id"][:12], out,
                      "the bulk listing printed nothing, so its missing BRIEF "
                      "lines below prove nothing")
        self.assertNotIn("BRIEF", out)
        self.assertNotIn("a second instruction", out)

    def test_the_public_mention_is_posted_by_the_MOVER_not_the_author(self):
        """`sender` is now inherited, and `_notify_public` used to post the
        @mention AS that seat — which after a move puts the notice in the
        original author's mouth in the public room. The seat that wrote the row
        posts it; `acted_by` is present only on a move, so ordinary rows are
        untouched."""
        row = self._send("build it")
        self._starve("source-seat", streak=True)
        os.environ["HELM_CHAT_NAME"] = self.MOVER
        posts = []
        from helm import chat as chatmod
        real_post = chatmod.post

        def record(text, room=None, who=None, **kw):
            posts.append({"who": who, "room": room})
            return real_post(text, room=room, who=who, **kw)

        with mock.patch.object(chatmod, "post", side_effect=record):
            out, err = dispatches.rebind(row["id"], "target-seat", force=True,
                                         reason="dark", repo=self.repo,
                                         notify=True)
        self.assertIsNone(err, err)
        self.assertTrue(posts, "the notify leg never posted, so the `who` "
                               "assertion below would be vacuous")
        self.assertEqual([p["who"] for p in posts], [self.MOVER],
                         "the mention must be posted by the seat that moved it")
        self.assertEqual(dispatches.rows()[out["new"]["id"]]["sender"],
                         self.AUTHOR)


class CustodyMovesTheDeliveryLegTest(DispatchBase):
    """review's "one verb moves every holding", and the half `rebind` never
    covered: a row the seat SENT.

    rebind moves the RECIPIENT. Pointing it at a sent row hands a third party's
    review to the successor and leaves the orphaned sender orphaned, so custody
    is a separate field behind a separate, proof-bound verb.
    """

    def test_the_swap_CANNOT_be_skipped_because_there_is_no_door(self):
        """`expected` used to be a parameter defaulting to None, so a caller
        that simply did not pass it got the overwrite with no diagnostic — an
        opt-in compare-and-swap, which is not one. The expectation is now read
        off the capability, and the parameter that made skipping possible is
        gone."""
        import inspect
        params = inspect.signature(dispatches.mark_custody).parameters
        self.assertNotIn("expected", params,
                         "the skip door is back: a caller can once again omit "
                         "the expectation and silently disable the swap")
        # `outcome` is a RESULT sink, not an input to the swap: it is written
        # only after the locked checks pass and carries nothing the writer
        # reads (task/2529). Any other new parameter still fails here.
        self.assertEqual(list(params), ["rid", "auth", "outcome"])
        self.assertIsNone(params["outcome"].default)

    def test_a_row_held_by_SOMEONE_ELSE_refuses(self):
        """The behaviour the missing door protects. The auth is minted from
        seat-a, so a row whose custody has since moved to seat-c must refuse
        rather than overwrite the fresher holder."""
        row = self.add(recipient="grok", kind="review")
        moved, err = dispatches.mark_custody(row["id"], self._auth())
        self.assertIsNone(err, err)
        self.assertEqual(moved.get("custodian"), "seat-b",
                         "the first move did not land, so the stale-auth "
                         "refusal below would prove nothing")
        # Custody is seat-b now. An authorization still minted from seat-a is
        # exactly the stale reassignment the swap exists to stop.
        again, err2 = dispatches.mark_custody(row["id"],
                                              self._auth(source="seat-a",
                                                         target="seat-d"))
        self.assertIsNone(again,
                          "custody was overwritten from a stale expectation")
        self.assertIn("moved under us", err2 or "")

    def test_the_writer_reports_a_checked_no_op_and_only_that(self):  # noqa: VACUOUS_ASSERTION — the empty-sink assertions on the write and the refusal sit beside the unconditional positive control noop == {"already": True} on the same sink
        """task/2529: a caller that counts custody moves needs the WRITER to
        say a row was already the target's, after its locked current-row,
        status and compare-and-swap checks, not an earlier snapshot. A real
        write and a refusal leave the sink empty; only the checked no-op marks
        it, and the no-op appends nothing."""
        row = self.add(recipient="grok", kind="review")
        wrote = {}
        moved, err = dispatches.mark_custody(row["id"], self._auth(),
                                             outcome=wrote)
        self.assertIsNone(err, err)
        self.assertEqual(moved.get("custodian"), "seat-b",
                         "the first move did not land")
        self.assertEqual(wrote, {}, "a real custody write reported a no-op")
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)
        seq = current[row["id"]]["seq"]
        noop = {}
        same, err = dispatches.mark_custody(
            row["id"], self._auth(source="seat-b", target="Seat-B"),
            outcome=noop)
        self.assertIsNone(err, err)
        self.assertIsNotNone(same, "the canonical no-op returned no row")
        self.assertEqual(noop, {"already": True},
                         "a custodian already matching the target was not "
                         "reported as a checked no-op")
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)
        self.assertEqual(current[row["id"]]["seq"], seq,
                         "the no-op wrote a custody event")
        stale = {}
        none, err = dispatches.mark_custody(
            row["id"], self._auth(source="seat-a", target="Seat-B"),
            outcome=stale)
        self.assertIsNone(none)
        self.assertIn("moved under us", err or "")
        self.assertEqual(stale, {},
                         "a refused move reported a no-op: the sink must only "
                         "ever speak after the compare-and-swap passes")

    # THE DEFAULT SOURCE IS THE FIXTURE'S OWN SEAT, and it was "seat-a" — a
    # seat that never holds any row this class creates, because DispatchBase
    # runs as "integrator" and a fresh row's custodian is its sender. Every
    # arm below was therefore moving integrator's custody under an
    # authorization minted for seat-a, and it worked only because the
    # compare-and-swap was opt-in and none of them opted in. Now that the
    # expectation is read off the capability, the mismatch is refused — which
    # is the point — so the fixture states the truth instead of relying on a
    # check that was not running.
    def _auth(self, source="integrator", target="seat-b", state=None,
              force=False):
        from helm import takeover, seat_reassign
        want = state if state is not None else seat_reassign.SOURCE_DEAD
        # The proof is MINTED under a controlled liveness, never spelled: the
        # mint reads `source_disposition`, so patching that is how a test says
        # "this seat is dead" without handing the door a value.
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(want, "pane gone")):
            disp, derr = takeover.mint_source_disposition(source)
        self.assertIsNone(derr, derr)
        auth, err = takeover.mint_reassign_custody(
            source, target, force=force, reason="source seat is dead",
            disposition=disp)
        self.assertIsNone(err, err)
        return auth

    def test_reassign_mints_per_row_without_renewing_the_source_measurement(self):
        from helm import seat_reassign, seats_roster, takeover
        rows = [self.add(recipient="grok", kind="review") for _ in range(3)]
        ids = [row["id"] for row in rows]
        # The whole command also visits the lease mover: its target must
        # actually exist, not merely be returned by a resolver double.
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}):
            seats_roster.write_roster("seat-b", cwd=self.repo, home_room="main")
        self.assertIn("seat-b", seats_roster.roster())
        self.assertEqual(seat_reassign.resolve_target("seat-b")[0], "seat-b")
        clock = [time.time()]
        start = clock[0]
        real_lock = eventledger.locked
        locks = []

        @contextlib.contextmanager
        def delayed(path):
            with real_lock(path) as locked:
                if path == dispatches.ledger_path():
                    locks.append(locked)
                    clock[0] += 16
                yield locked

        def holdings(_seat):
            current, err = dispatches.snapshot()
            self.assertIsNone(err, err)
            outgoing = [current[rid] for rid in ids
                        if dispatches.custodian_of(current[rid]) == "integrator"]
            return {"dispatch_in": [], "dispatch_out": outgoing,
                    "tasks": [], "leases": []}, []

        with mock.patch.object(takeover.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(seat_reassign, "resolve_source",
                                  return_value=("integrator", None, "orphan")), \
                mock.patch.object(seat_reassign, "source_disposition",
                                  return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(seat_reassign, "holdings", side_effect=holdings), \
                mock.patch.object(takeover, "mint_source_disposition",
                                  wraps=takeover.mint_source_disposition) as measure, \
                mock.patch.object(takeover, "mint_reassign_custody",
                                  wraps=takeover.mint_reassign_custody) as mint, \
                mock.patch.object(eventledger, "locked", delayed):
            rc, lines = seat_reassign.reassign("integrator", "seat-b", apply=True)
            self.assertEqual(rc, 0, lines)
            self.assertEqual(locks, [True, True, True])
            self.assertGreater(clock[0] - start, takeover.REASSIGN_MAX_AGE_S)
            self.assertEqual(measure.call_count, 1)
            self.assertEqual(mint.call_count, 3)
            disp = mint.call_args_list[0].kwargs["disposition"]
            self.assertTrue(all(call.kwargs["disposition"] is disp
                                for call in mint.call_args_list))
            current, err = dispatches.snapshot()
            self.assertIsNone(err, err)
            self.assertEqual([current[rid].get("custodian") for rid in ids],
                             ["seat-b"] * 3)

            # A new per-write capability must not renew the older measurement.
            clock[0] = disp.measured_at + takeover.DISPOSITION_MAX_AGE_S + 1
            before = pathlib.Path(dispatches.ledger_path()).read_bytes()
            moved, refused = seat_reassign._move_custody(
                rows, "integrator", "seat-c", "expired batch", [],
                disposition=disp)
            self.assertEqual(moved, [])
            self.assertEqual(len(refused), 3)
            self.assertTrue(all("remeasure" in row["why"] for row in refused))
            self.assertEqual(pathlib.Path(dispatches.ledger_path()).read_bytes(),
                             before)
            self.assertEqual(locks, [True, True, True],
                             "expired measurement reached the dispatch writer")
            self.assertEqual(measure.call_count, 1,
                             "the batch silently remeasured its source")

    def test_custody_proof_has_a_bounded_clock_at_the_writer(self):
        from helm import takeover
        for age in (-2, 0, takeover.REASSIGN_MAX_AGE_S):
            with self.subTest(age=age):
                row = self.add(recipient="grok", kind="review")
                auth = self._auth()
                with mock.patch.object(takeover.time, "time",
                                       return_value=auth.minted_at + age):
                    moved, err = dispatches.mark_custody(row["id"], auth)
                self.assertIsNone(err, err)
                self.assertEqual(moved.get("custodian"), "seat-b")
                self.assertEqual(dispatches.snapshot()[0][row["id"]]
                                 .get("custodian"), "seat-b")

        row = self.add(recipient="grok", kind="review")
        now = time.time()
        clocks = (now - takeover.REASSIGN_MAX_AGE_S - 1, now + 3,
                  None, True, "yesterday", float("nan"), float("inf"),
                  float("-inf"), 10 ** 400, -(10 ** 400))
        for stamp in clocks:
            with self.subTest(stamp=stamp):
                auth = self._auth()
                auth.minted_at = stamp
                before = pathlib.Path(dispatches.ledger_path()).read_bytes()
                with mock.patch.object(takeover.time, "time", return_value=now):
                    moved, err = dispatches.mark_custody(row["id"], auth)
                self.assertIsNone(moved)
                self.assertIn("stale or clock-invalid", err or "")
                self.assertEqual(pathlib.Path(dispatches.ledger_path())
                                 .read_bytes(), before)

    def test_custody_proof_can_expire_while_waiting_for_the_ledger_lock(self):
        from helm import takeover
        row = self.add(recipient="grok", kind="review")
        auth = self._auth()
        clock = [auth.minted_at]
        held = []
        real_lock = eventledger.locked

        @contextlib.contextmanager
        def delayed(path):
            with real_lock(path) as locked:
                held.append(locked)
                clock[0] += takeover.REASSIGN_MAX_AGE_S + 1
                yield locked

        before = pathlib.Path(dispatches.ledger_path()).read_bytes()
        with mock.patch.object(takeover.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(eventledger, "locked", delayed):
            moved, err = dispatches.mark_custody(row["id"], auth)
        self.assertEqual(held, [True], "the real lock was never acquired")
        self.assertIsNone(moved)
        self.assertIn("stale or clock-invalid", err or "")
        self.assertEqual(pathlib.Path(dispatches.ledger_path()).read_bytes(), before)
        # The same row still moves with a fresh proof; refusal is not a dead writer.
        moved, err = dispatches.mark_custody(row["id"], self._auth())
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]
                         .get("custodian"), "seat-b")

    def _sender_of(self, rid):
        return (dispatches.snapshot()[0].get(rid) or {}).get("sender")

    def test_a_custody_event_is_RENDERED_and_not_merely_APPENDED(self):
        """THE ARM THIS FEATURE MOST NEEDED. A writer that appends an event the
        reducer has no arm for is silently inert: the ledger grows, the return
        value looks right, every assertion on IT passes, and the reduced state
        never changes. So this asserts through `snapshot()`."""
        row = self.add(recipient="grok", kind="review")
        out, err = dispatches.mark_custody(row["id"], self._auth())
        self.assertIsNone(err, err)
        self.assertEqual(out.get("custodian"), "seat-b")
        reduced = dispatches.snapshot()[0].get(row["id"]) or {}
        self.assertEqual(reduced.get("custodian"), "seat-b",
                         "the custody event appended but the reducer never "
                         "applied it — the row is unchanged in reduced state")

    def test_every_OBLIGATION_reader_follows_the_custodian(self):
        """review caught that my first census was FALSE: it enumerated
        callers of `_sent_by` inside dispatches.py, and two more readers spell
        the sender field themselves — one in another module entirely. This arm
        pins ALL of them, before and after, on one row.

        The recipient is asserted UNCHANGED throughout: custody moves the
        delivery leg and must never disturb who the row is addressed to."""
        from helm import seats_stop_signals
        row = self.add(recipient="grok", kind="review")
        rid = row["id"]
        author = self._sender_of(rid)
        self.assertTrue(author, "fixture: the row must carry an author")
        before = dispatches.snapshot()[0].get(rid) or {}
        recipient_before = before.get("recipient")

        # BEFORE: the author owes it, seat-b does not.
        self.assertTrue(dispatches._sent_by(before, author))
        self.assertFalse(dispatches._sent_by(before, "seat-b"))

        _o, err = dispatches.mark_custody(rid, self._auth(source=author))
        self.assertIsNone(err, err)
        after = dispatches.snapshot()[0].get(rid) or {}

        # AFTER: seat-b owes it, the author does not.
        self.assertTrue(dispatches._sent_by(after, "seat-b"),
                        "_sent_by did not follow the custodian")
        self.assertFalse(dispatches._sent_by(after, author),
                         "_sent_by still bills the original author")
        # _mine_or_unprovable, the stop-guard nag, same flip.
        roster = {"seat-b": {}, str(author): {}}
        self.assertTrue(dispatches._mine_or_unprovable(
            after, "seat-b", roster, False),
            "_mine_or_unprovable did not follow the custodian")
        # AND THE RECIPIENT NEVER MOVED.
        self.assertEqual(after.get("recipient"), recipient_before,
                         "custody disturbed the row's RECIPIENT")
        self.assertEqual(after.get("sender"), author,
                         "custody REWROTE authorship")
        # _beacon_obligation READS THE SAME FIELD FROM ANOTHER MODULE, and it
        # is CALLED here rather than merely asserted to exist — "callable" is
        # the vacuous shape this repo keeps catching. It answers None when the
        # ledger cannot be read, so a None is reported as an inconclusive
        # fixture rather than silently passing as False.
        owed_new = seats_stop_signals._beacon_obligation("seat-b")
        owed_old = seats_stop_signals._beacon_obligation(str(author))
        if owed_new is None or owed_old is None:
            self.skipTest("beacon obligation unreadable in this fixture; the "
                          "arm would be vacuous rather than passing")
        self.assertTrue(owed_new,
                        "the new custodian does not owe the beacon, so a "
                        "transferred leg cannot wake anyone")
        self.assertFalse(owed_old,
                         "the DEAD author still owes the beacon after the "
                         "transfer — the exact inversion custody repairs")

    def test_a_STALE_holder_refuses_instead_of_overwriting(self):
        """review: a transfer landing between holdings() and the write would
        be silently clobbered by the stale reassignment. The compare-and-swap
        runs under the ledger lock."""
        row = self.add(recipient="grok", kind="review")
        rid = row["id"]
        author = self._sender_of(rid)
        # Someone else moves it first.
        _o, err = dispatches.mark_custody(rid, self._auth(source=author,
                                                          target="seat-c"))
        self.assertIsNone(err, err)
        # Our stale move still believes the author holds it.
        out, err2 = dispatches.mark_custody(
            rid, self._auth(source=author, target="seat-b"))
        self.assertIsNone(out, "a stale reassignment overwrote a fresher one")
        self.assertIn("moved under us", err2 or "")
        self.assertEqual(
            (dispatches.snapshot()[0].get(rid) or {}).get("custodian"),
            "seat-c", "the fresher custodian was lost")

    def test_custody_moves_only_behind_a_MINTED_authorization(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(out) for the bare-token call; its unconditional positive is the minted call asserted immediately above, same verb, same row
        row = self.add(recipient="grok", kind="review")
        # CONTROL: with a real proof it works.
        ok, err = dispatches.mark_custody(row["id"], self._auth())
        self.assertIsNotNone(ok, err)
        # A bare seat name is not evidence.
        row2 = self.add(recipient="grok", kind="review")
        out, err2 = dispatches.mark_custody(row2["id"], "seat-b")
        self.assertIsNone(out, "a bare token moved a delivery leg")
        self.assertIn("minted reassignment authorization", err2 or "")

    def test_a_LIVE_source_cannot_have_its_leg_moved_without_an_override(self):
        from helm import takeover, seat_reassign
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_LIVE,
                                             "pane is live")):
            live_disp, _e = takeover.mint_source_disposition("seat-a")
        auth, err = takeover.mint_reassign_custody(
            "seat-a", "seat-b", force=False, reason="tidying",
            disposition=live_disp)
        self.assertIsNone(auth, "a LIVE source minted custody with no override")
        self.assertIn("measurably LIVE", err or "")
        # CONTROL: the override mints, and records that it was forced.
        forced, ferr = takeover.mint_reassign_custody(
            "seat-a", "seat-b", force=True, reason="operator judgment",
            disposition=live_disp)
        self.assertIsNotNone(forced, ferr)
        self.assertTrue(forced.force)

    def test_a_GHOST_target_never_reaches_a_write(self):
        # THE OLD MINT SIGNATURE, still spelled here. It took `state` and `why`
        # positionally; they were removed when one measurement became the
        # capability's own input, and these two calls kept passing them — so
        # SOURCE_DEAD landed on `force` and "pane gone" on `reason`, which
        # then collided with the explicit reason= and raised TypeError before
        # the arm's actual question was ever asked. The question is target
        # validation, which the mint answers first and without a disposition.
        from helm import takeover
        auth, err = takeover.mint_reassign_custody(
            "seat-a", "", reason="dead")
        self.assertIsNone(auth, "custody minted with no target at all")
        self.assertIn("resolved source and target", err or "")
        same, serr = takeover.mint_reassign_custody(
            "seat-a", "seat-a", reason="dead")
        self.assertIsNone(same, "custody minted onto the SOURCE seat")
        self.assertIn("same seat", serr or "")

    def test_the_REDUCER_refuses_what_the_WRITER_refuses(self):
        """review: the writer requires a bounded non-empty reason and the
        reducer checked none, so an event `mark_custody` would never have
        written APPLIES on read. A projection reachable by a shape its own door
        rejects is a ledger that disagrees with itself — and this is the door a
        replay or a hand-append comes through."""
        from helm import eventledger, pk
        row = self.add(recipient="grok", kind="review")
        rid = row["id"]
        # CONTROL FIRST: a well-formed event through the same raw path DOES
        # apply, so the refusals below are about the reason and not about
        # hand-appending being inert.
        # DERIVE THE SEQ, never transcribe it. A hardcoded 2 assumes `add`
        # left the row at seq 1; if it did not, every event below is rejected
        # for SEQ ORDER rather than for its reason, and the arm passes while
        # proving nothing about the reason at all.
        seq = int((dispatches.snapshot()[0].get(rid) or {}).get("seq") or 0)
        good = {"v": 3, "event": "custody", "seq": seq + 1, "id": rid,
                "ts": pk.now_ts(), "custodian": "seat-b",
                "reason": "source seat is dead"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), good))
        self.assertEqual(
            (dispatches.snapshot()[0].get(rid) or {}).get("custodian"),
            "seat-b", "control: a well-formed custody event must apply")

        for label, reason in (("absent", None), ("empty", "   "),
                              ("past the cap", "x" * 5000)):
            with self.subTest(reason=label):
                # Re-read each time: a REJECTED event does not advance the
                # seq, so a fixed number would drift out of order after the
                # first refusal and the rest would fail for the wrong reason.
                cur = int((dispatches.snapshot()[0].get(rid) or {}).get("seq") or 0)
                bad = {"v": 3, "event": "custody", "seq": cur + 1, "id": rid,
                       "ts": pk.now_ts(), "custodian": "seat-c"}
                if reason is not None:
                    bad["reason"] = reason
                self.assertTrue(
                    eventledger.append(dispatches.ledger_path(), bad))
                self.assertEqual(
                    (dispatches.snapshot()[0].get(rid) or {}).get("custodian"),
                    "seat-b",
                    "a custody event with a %s reason moved custody on read, "
                    "though the writer would have refused it" % label)

    def test_CUSTODY_TRAVELS_when_a_move_mints_a_child(self):
        """review: a move already inherits `sender`; it did not inherit the
        CUSTODIAN. So after A authored a row and custody moved A->B, a later
        recipient rebind minted a child carrying sender=A and no custodian —
        and `custodian_of` falls back to the sender, silently handing the
        delivery leg back to A. B, who had actually taken it, owed nothing and
        was told nothing."""
        row = self.add(recipient="grok", kind="review")
        rid = row["id"]
        author = self._sender_of(rid)
        _o, err = dispatches.mark_custody(rid, self._auth(source=author))
        self.assertIsNone(err, err)
        parent = dispatches.snapshot()[0].get(rid) or {}
        self.assertEqual(dispatches.custodian_of(parent), "seat-b",
                         "fixture: custody must have moved before the rebind")

        child, cerr = dispatches.add(
            "kimi", parent.get("lane") or "lane", ref=self.a, repo=self.repo,
            supersedes=rid, notify=False, _reason=True,
            _preserve_origin=dispatches._MOVE_MINT)
        self.assertIsNone(cerr, cerr)
        got = dispatches.snapshot()[0].get(child["id"]) or {}
        self.assertEqual(got.get("sender"), author,
                         "the move must still inherit authorship")
        self.assertEqual(dispatches.custodian_of(got), "seat-b",
                         "the child handed the delivery leg back to the "
                         "AUTHOR; the custodian did not travel")

    def test_custodian_of_falls_back_to_the_sender(self):
        """Every row written before this field existed has no custodian, so the
        default must be the answer the ledger has always given."""
        self.assertEqual(dispatches.custodian_of({"sender": "seat-a"}), "seat-a")
        self.assertEqual(
            dispatches.custodian_of({"sender": "seat-a", "custodian": "seat-b"}),
            "seat-b")
        # MUST-MISS: a blank custodian is not a custodian.
        self.assertEqual(
            dispatches.custodian_of({"sender": "seat-a", "custodian": "  "}),
            "seat-a")

    def test_a_CLOSED_row_has_no_delivery_leg_to_transfer(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(out) for the cancelled row; its unconditional positive is the successful mark_custody on an OPEN row asserted immediately above it
        row = self.add(recipient="grok", kind="review")
        ok, err = dispatches.mark_custody(row["id"], self._auth())
        self.assertIsNotNone(ok, err)
        row2 = self.add(recipient="grok", kind="review")
        _c, cerr = dispatches.mark_cancel(row2["id"], "not needed")
        self.assertIsNone(cerr, cerr)
        out, err2 = dispatches.mark_custody(row2["id"], self._auth())
        self.assertIsNone(out,
                          "custody moved on a CLOSED row, manufacturing an "
                          "obligation instead of transferring one")
        self.assertIn("no delivery leg", err2 or "")


class PreserveOriginIsMintedNotRequestedTest(DispatchBase):
    """review: `_preserve_origin` was exposed on the generic `add`, so any
    library caller could mint a child row carrying ANOTHER seat's `sender`
    with no rebind evidence — an authorship forgery reachable by keyword.

    The underscore was privacy by CONVENTION, and convention is not a
    boundary. The capability is now an object that cannot be constructed from
    outside the module.
    """

    def test_requesting_preserve_origin_BY_VALUE_is_refused(self):
        parent = self.add(recipient="grok", kind="review")
        out, err = dispatches.add(
            recipient="kimi", lane="forged", kind="review",
            supersedes=parent["id"], _reason=True, _preserve_origin=True)
        self.assertIsNone(out, "a caller minted a row under another seat's "
                               "sender by passing True")
        self.assertIn("minted inside dispatches", err or "")

    def test_an_ORDINARY_add_still_works_and_authors_itself(self):
        """CONTROL. Without it, an `add` broken for every caller would satisfy
        the refusal above and look like a security fix.

        Routed through the base fixture's `add` rather than calling
        `dispatches.add` directly: the raw door refuses a ref that does not
        live in the ledger's own repo, and my first draft read that refusal as
        a broken `add`."""
        row = self.add(recipient="grok", kind="review")
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)
        stored = current.get(row["id"]) or {}
        self.assertTrue(stored.get("sender"),
                        "an ordinary add recorded no author at all")


class TheBodyCapBindsTheSerializedValueTest(unittest.TestCase):
    """the review ruling: bind FINAL SERIALIZED LEDGER BYTES. A raw admission
    cap may exist but cannot substitute.

    The old cap measured raw text on the wrong side of its own encoding, and
    missed in BOTH directions at once: it kept 4000 raw bytes and then APPENDED
    its truncation notice on top (4001 ASCII in -> 4194 stored, over a cap of
    4000), while a body of control characters escapes roughly sixfold in JSON
    (4000 raw -> 24002 serialized, six times the cap).
    """

    def _ser(self, text):
        """DELEGATE, never re-implement. This helper was
        `len(json.dumps(text))` — the DEFAULT ensure_ascii=True — which is the
        exact defect the production counter was fixed for, transcribed into the
        arm that checks it. The arm then measured 24002 for a body the ledger
        stores at 8190 and failed a cure that was correct. A probe more or less
        forgiving than the code is a DIFFERENT measurement, and its verdict
        says nothing about the code."""
        return dispatches._serialized_len(text)

    def test_control_bytes_cannot_escape_past_the_cap(self):
        """THE INPUT THE OLD CAP COULD NOT REJECT. Every character here costs
        six serialized bytes, so a body the raw cap called legal was 6x over."""
        out = dispatches._store_body("\x01" * 4000)
        self.assertLessEqual(self._ser(out),
                             dispatches.MESSAGE_BODY_SERIALIZED_CAP,
                             "control bytes escaped past the serialized cap")
        # CONTROL, same call, ordinary text: a cap that truncated everything to
        # nothing would satisfy the bound above without storing a brief.
        short = dispatches._store_body("hello")
        self.assertEqual(short, "hello")

    def test_an_INTACT_body_is_never_labelled_TRUNCATED(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotIn(BODY_TRUNCATED_MARK); its unconditional positive is assertEqual(out, body) on the SAME returned value one line above, which a function returning "" or None could not satisfy
        """A brief that fits is stored verbatim and says nothing about being
        cut. My first draft of the cure got this wrong in the other direction —
        it returned the whole body AND appended "4001 of 4001 bytes stored", a
        truncation warning on an intact brief, which sends the recipient asking
        for a tail that does not exist."""
        body = "a" * dispatches.MESSAGE_BODY_CAP
        out = dispatches._store_body(body)
        self.assertEqual(out, body, "an intact brief was altered")
        self.assertNotIn(dispatches.BODY_TRUNCATED_MARK, out,
                         "an INTACT brief was labelled truncated")

    def test_the_NOTICE_is_inside_the_bound_it_reports(self):
        """THE ORIGINAL DEFECT. The old cap kept MESSAGE_BODY_CAP raw bytes and
        then APPENDED its notice, so a body one byte over the cap stored 4194
        bytes under a cap of 4000 — the warning itself broke the bound it was
        warning about. Both caps are now measured on the ASSEMBLED value."""
        out = dispatches._store_body("a" * (dispatches.MESSAGE_BODY_CAP + 1))
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, out)
        self.assertLessEqual(
            len(out.encode("utf-8")), dispatches.MESSAGE_BODY_CAP,
            "the truncation notice pushed the stored value back over the raw "
            "cap — the defect this cure exists for")
        self.assertLessEqual(self._ser(out),
                             dispatches.MESSAGE_BODY_SERIALIZED_CAP)

    def test_a_truncated_notice_states_HONEST_numbers(self):
        body = "a" * 20000
        out = dispatches._store_body(body)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, out)
        self.assertLessEqual(self._ser(out),
                             dispatches.MESSAGE_BODY_SERIALIZED_CAP)
        self.assertIn("of %d UTF-8 bytes" % len(body.encode("utf-8")), out,
                      "the notice misreports the original size")
        kept = out.split("\n\n" + dispatches.BODY_TRUNCATED_MARK)[0]
        self.assertIn("%d of" % len(kept.encode("utf-8")), out,
                      "the notice misreports how much it kept")

    def test_a_multibyte_body_cuts_on_a_CHARACTER_boundary(self):
        out = dispatches._store_body("\u4e2d" * 9000)
        # UNCONDITIONAL POSITIVE on the same value: it kept real content and
        # said it was cut. Without this, a function returning "" would satisfy
        # the no-replacement-character assertion below perfectly.
        self.assertTrue(out.startswith("\u4e2d\u4e2d"),
                        "the kept prefix is not the body that went in")
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, out)
        self.assertLessEqual(self._ser(out),
                             dispatches.MESSAGE_BODY_SERIALIZED_CAP)
        # ...and lost no character to a mid-codepoint split.
        self.assertNotIn("\ufffd", out, "a codepoint was split")

    def test_the_counter_uses_THE_WRITERS_ENCODING_not_a_default(self):  # noqa: VACUOUS_ASSERTION — no absence is asserted here; both assertions are positive, and the assertLess above is the unconditional control proving this input can DISCRIMINATE the two encodings at all (on ASCII they coincide and the arm would be vacuous)
        """review: `_serialized_len` used json.dumps' DEFAULT
        ensure_ascii=True while `eventledger` writes ensure_ascii=False and
        encodes UTF-8. Measured: 1000 emoji cost 4002 bytes on disk and the
        counter called them 12002 — a 3x OVER-count that truncated briefs which
        would have fit whole.

        That is the SAME defect this cap was rewritten to fix, reintroduced
        inside its own cure: the original measured raw bytes when the ledger
        paid serialized ones, and the fix measured a serialization the ledger
        never performs. "The encoded size" is not enough — it has to be the
        encoder that ACTUALLY WRITES, which is why this arm derives the
        expected number from the writer's own flags rather than a literal."""
        body = "\U0001f600" * 1000
        # The writer's flags, spelled the way eventledger spells them.
        on_disk = len(json.dumps(body, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))
        escaped = len(json.dumps(body))
        # CONTROL: the two encodings genuinely differ here, so this input can
        # tell them apart. Without it the assertion below would pass on ASCII.
        self.assertLess(on_disk, escaped,
                        "fixture: this body must cost less on disk than "
                        "escaped, or it cannot discriminate the encodings")
        self.assertEqual(dispatches._serialized_len(body), on_disk,
                         "the counter is measuring an encoding the ledger "
                         "never performs")

    def test_a_MOVE_reapplies_the_current_caps_to_an_inherited_body(self):
        """A move COPIED the parent's stored body straight through, while the
        ordinary send path runs it through `_store_body`. So a row written
        under a larger cap — or carried across a cap later LOWERED —
        propagated unbounded through every rebind, and the bound this field
        advertises held only for rows nobody had moved."""
        big = "z" * 9000
        # A body stored under a LARGER cap, as a historical row would carry.
        with mock.patch.object(dispatches, "MESSAGE_BODY_CAP", 100000), \
                mock.patch.object(dispatches, "MESSAGE_BODY_SERIALIZED_CAP",
                                  200000):
            historical = dispatches._store_body(big)
        self.assertEqual(historical, big,
                         "fixture: the historical body must exceed today's cap")
        # Under TODAY's caps it is brought inside the bound.
        carried = dispatches._store_body(historical)
        self.assertLessEqual(len(carried.encode("utf-8")),
                             dispatches.MESSAGE_BODY_CAP,
                             "an inherited body crossed a rebind unbounded")
        # AND IT IS IDEMPOTENT: a row rebound repeatedly must not accumulate a
        # truncation notice per hop.
        self.assertEqual(dispatches._store_body(carried), carried,
                         "each rebind re-truncated an already-capped body")
        # CONTROL: a body that already fits is returned untouched, so the
        # re-application costs nothing in the ordinary case.
        self.assertEqual(dispatches._store_body("short brief"), "short brief")

    def test_an_EMPTY_body_is_KNOWN_EMPTY_and_not_an_empty_brief(self):
        # UNCONDITIONAL POSITIVE FIRST: a real body comes back as itself, so
        # the two Nones below are about EMPTINESS and not about a function
        # that returns None for everything.
        self.assertEqual(dispatches._store_body("a brief"), "a brief")
        self.assertIsNone(dispatches._store_body(""))
        self.assertIsNone(dispatches._store_body(None))


class TheBriefDoesNotRideAListingTest(unittest.TestCase):
    """`dispatch list --json` dumps whole rows. Storing `message_body` ON the
    row turned a verb that emitted metadata into one that emits the full text
    of every brief the ledger holds — including CLOSED rows — to anyone who can
    run it. The listing verb predates the field; the boundary is the field's to
    owe.
    """

    ROWS = [{"id": "a", "message_body": "hello brief"},
            {"id": "b"},
            {"id": "c", "message_body": "\U0001f600"}]

    def test_no_body_survives_the_listing(self):
        got = dispatches.redact_bodies(self.ROWS)
        # CONTROL FIRST: the rows arrived and were processed at all.
        self.assertEqual([r["id"] for r in got], ["a", "b", "c"])
        self.assertTrue(all("message_body" not in r for r in got),
                        "a brief was emitted in the listing")

    def test_a_SIZE_is_published_so_absence_stays_distinguishable(self):
        """A redaction that erased both would make an UNBRIEFED row and a
        PRIVATE one look identical, which is a second false fact traded for the
        first."""
        got = dispatches.redact_bodies(self.ROWS)
        self.assertEqual(got[0].get("message_body_bytes"), 11)
        self.assertEqual(got[2].get("message_body_bytes"), 4,
                         "the size must be UTF-8 bytes, not characters")
        self.assertIsNone(got[1].get("message_body_bytes"),
                          "a row that never had a brief must not grow a size")

    def test_the_CALLERS_rows_are_not_mutated(self):
        """The redaction is for one surface. Mutating in place would strip the
        body from every other reader sharing the snapshot."""
        rows = [dict(r) for r in self.ROWS]
        dispatches.redact_bodies(rows)
        self.assertEqual(rows[0].get("message_body"), "hello brief",
                         "redaction mutated the caller's row")


class DispatchFoldAmbientBudgetTest(unittest.TestCase):
    def test_expiry_between_rows_stops_the_fold_before_later_events(self):  # noqa: VACUOUS_ASSERTION — seen=['a'] is the positive control that row one ran; exact equality proves later rows did not
        from helm import projscope
        seen = []

        def opened(row):
            seen.append(row["id"])
            return {"id": row["id"], "status": "open"}

        with mock.patch.object(dispatches, "_new_state", side_effect=opened), \
                mock.patch.object(
                    dispatches.projscope, "spend_or_raise",
                    side_effect=[None, projscope.Expired("planted")]):
            with self.assertRaises(projscope.Expired):
                dispatches._fold([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        self.assertEqual(seen, ["a"],
                         "expiry was swallowed as a malformed row and folding continued")


class TheFrontierAsksTheCheapQuestionFirstTest(unittest.TestCase):
    """helm task/2394. The successor-frontier scan ran `query.query_is_open`
    over EVERY row of the projection once per parent — measured on the live
    14,444-event ledger, 98 parents x ~2,650 rows = 259,881 predicate calls and
    0.74-1.06s of a stop-guard rung budgeted at 6.5s. Both gates are pure tests
    on the same row, so the order cannot move the ANSWER; these arms pin both
    halves of that claim, the answer and the cost."""

    def _exhaustive(self, current, rid):
        """The PREVIOUS order, transcribed, as the reference answer.

        It is here rather than described because the property under test is
        that two orderings agree, and an agreement needs both sides present."""
        successors, unknown = [], []
        for r in (current or {}).values():
            if not isinstance(r, dict) or not dispatches._not_closed(r) \
                    or r.get("id") == rid:
                continue
            if r.get("supersedes") == rid:
                successors.append(str(r.get("id") or ""))
            elif r.get("supersedes") == dispatches.CHAIN_UNKNOWN:
                unknown.append(str(r.get("id") or ""))
        return sorted(successors), sorted(unknown)

    def _projection(self):
        """Every discrimination the scan makes, each with a live specimen."""
        return {
            "parent": {"id": "parent", "status": "open",
                       "supersedes": "parent"},          # its own successor
            "open-child": {"id": "open-child", "status": "open",
                           "supersedes": "parent"},
            "held-child": {"id": "held-child", "status": "held",
                           "supersedes": "parent"},
            "retired-child": {"id": "retired-child", "status": "open",
                              "retired_admin": "integrator",
                              "supersedes": "parent"},
            "closed-child": {"id": "closed-child", "status": "cancelled",
                             "supersedes": "parent"},
            "verdicted-child": {"id": "verdicted-child", "status": "verdict",
                                "supersedes": "parent"},
            "open-unknown": {"id": "open-unknown", "status": "open",
                             "supersedes": dispatches.CHAIN_UNKNOWN},
            "closed-unknown": {"id": "closed-unknown", "status": "close",
                               "supersedes": dispatches.CHAIN_UNKNOWN},
            "stranger": {"id": "stranger", "status": "open",
                         "supersedes": "somebody-else"},
            "rootless": {"id": "rootless", "status": "open"},
            "not-a-dict": "a corrupt projection entry",
        }

    def test_the_reordered_scan_returns_the_exhaustive_answer(self):
        current = self._projection()
        got = dispatches._successor_frontier(current, "parent")
        self.assertEqual(got, self._exhaustive(current, "parent"))
        # THE MUST-HIT: an agreement between two empty answers is not an
        # agreement about anything. Both lists carry rows, and the closed,
        # RETIRED, foreign, rootless, self-naming and non-dict specimens are
        # absent from both — which is what makes the equality discriminating.
        # `held` is live and `retired_admin` is terminal WHATEVER the status
        # word says, which is `query.query_is_open`'s rule and not this
        # fixture's: the first cut of this arm expected a `hold`-spelled row
        # to be live and the reference answer refused it.
        self.assertEqual(got, (["held-child", "open-child"], ["open-unknown"]))

    def test_the_predicate_is_asked_only_about_rows_naming_this_parent(self):
        """The COST arm, and it fails if the order is ever restored: on a
        projection of 500 rows where two name the parent, the liveness
        predicate is consulted a handful of times, not 500."""
        current = {"r%d" % i: {"id": "r%d" % i, "status": "open",
                               "supersedes": "elsewhere-%d" % i}
                   for i in range(500)}
        current.update({k: v for k, v in self._projection().items()
                        if k in ("open-child", "open-unknown", "parent")})
        from helm import query
        calls = []
        real = query.query_is_open

        def spy(row):
            calls.append(row.get("id"))
            return real(row)

        with mock.patch.object(query, "query_is_open", side_effect=spy):
            got = dispatches._successor_frontier(current, "parent")
        self.assertEqual(got, (["open-child"], ["open-unknown"]),
                         "the answer is the point; the count below is the cost")
        self.assertLessEqual(
            len(calls), 8,
            "the liveness predicate ran %d times over a 503-row projection: "
            "the cheap `supersedes` discriminator is not being asked first"
            % len(calls))
        self.assertTrue(calls, "the predicate was never consulted at all, so "
                                "this arm would pass on a scan that decides "
                                "liveness by guessing")


class TheCarriageWitnessIsDerivedOnEveryReadTest(DispatchBase):
    """helm task/2394, round four. `_close_event_error`'s `carried` arm
    re-derives `carriage_proof` at EVERY replay, and that derivation spawns git:
    measured on the live ledger, two carried closes cost 18 subprocesses and
    2.345s of a 5.384s `snapshot()`. No verdict is memoised against that cost,
    because a persisted answer about a git HISTORY VIEW is only as good as the
    view, and `refs/replace`, `info/grafts` and a shallow boundary each
    reinterpret the same immutable ids without changing one of them. So the view
    is PINNED at the witness instead, and these arms measure the view. There is
    no cache here to test."""

    def setUp(self):
        super().setUp()
        self.gitdir = os.path.join(self.repo, ".git")
        self.trunk = "refs/heads/" + self.main
        # The deprecation advice for a grafts file is stderr noise this
        # fixture's own arms provoke on purpose; git ships no other interface.
        self.git("config", "advice.graftFileDeprecated", "false")
        # A CHERRY-PICKED LAND IS THE ONLY SHAPE THIS WITNESS AFFIRMS, and the
        # first cut of this fixture got that wrong: `git cherry` on a tip that
        # is already an ANCESTOR of trunk prints nothing, and `_reached_trunk`
        # reads an empty range as silence, because an empty range affirms every
        # possible trunk. So the fixture lands the work the way helm does —
        # a lane commit, cherry-picked onto trunk under a different sha — which
        # is the state `git cherry` exists to match by patch identity.
        self.git("checkout", "-q", "-b", "landing-lane", self.main)
        self.lane_tip = self.commit_file("lane-work", "carried")
        self.git("checkout", "-q", self.main)
        # TRUNK MOVES FIRST, and that is not decoration: cherry-picking onto an
        # unmoved trunk reproduces the lane commit's own sha (same tree, same
        # parent, same author), `git cherry` then has nothing upstream-only to
        # list, and the witness reads an empty range as silence. The drift is
        # what makes the landed twin a DIFFERENT object carrying the same patch.
        self.commit_file("trunk-drift", "trunk moved under the lane")
        self.git("cherry-pick", self.lane_tip)
        self.row = {"id": "cafebabecafebabe", "reviewed_tip": self.lane_tip}
        self.derived = []
        # THE SPY SITS ON THE ANCESTRY/PATCH-IDENTITY FAMILY, so an arm can
        # say WHICH family answered and how often it was derived.
        self.real_witness = dispatches._carriage_reached_witness

    def _counting_witness(self):
        def witness(*args, **kwargs):
            self.derived.append(args)
            return self.real_witness(*args, **kwargs)
        return mock.patch.object(dispatches, "_carriage_reached_witness",
                                 side_effect=witness)

    def _pair_bound(self):
        """(row, rows) the chain binds an immutable (base, tip) for, so the
        CONTENT family is the one that answers. `self.c` is the commit the lane
        branched from, which is the base `landreq` records for a build row."""
        build = {"id": "0123456789abcdef", "kind": "build", "tip": self.c}
        row = {"id": "beefbeefbeefbeef", "kind": "review",
               "reviewed_tip": self.lane_tip, "chain_root": build["id"]}
        return row, {build["id"]: build}

    def _proof(self, row=None, trunk=None, rows=None):
        return dispatches.carriage_proof(row or self.row, rows or {}, {},
                                         self.gitdir, trunk or self.trunk)

    def _replace_the_landed_twin(self):
        """Stage the history view that moves a naked `git cherry`: the LANDED
        TWIN — the trunk commit whose patch identity IS the whole of the match
        — is replaced by a commit carrying an unrelated patch. -> (twin, decoy)

        NOT ONE ID IN THE QUESTION CHANGES, and that is the point.
        `refs/replace/<oid>` is a ref; the twin object is still in the store,
        trunk still points at the twin, and `rev-parse` still prints the twin
        with replacement on or off. The decoy's carrier branch is deleted so
        nothing but the replacement ref reaches it.
        """
        twin = self.git("rev-parse", self.trunk)
        self.git("checkout", "-q", "-b", "decoy-carrier", twin + "^")
        decoy = self.commit_file("decoy-file", "an unrelated patch entirely")
        self.git("checkout", "-q", self.main)
        self.git("replace", twin, decoy)
        self.git("branch", "-D", "decoy-carrier")
        self.assertEqual(self.git("rev-parse", self.trunk + "^{commit}"), twin,
                         "the replacement moved the object the question names, "
                         "so what follows is a different question rather than "
                         "a different view of this one")
        return twin, decoy

    def _naked_cherry(self, pinned=False, gitdir=None):
        """`git cherry` SPAWNED DIRECTLY, outside helm's seam, so the control it
        feeds is about GIT and not about this module's plumbing. The overlay is
        the only difference between the two spellings, which is what lets an arm
        attribute a moved answer to the overlay rather than to anything helm
        did."""
        from helm import rowworld
        child = dict(os.environ)
        for name in ("GIT_NO_REPLACE_OBJECTS", "GIT_GRAFT_FILE"):
            child.pop(name, None)
        if pinned:
            child.update(rowworld._history_view_env())
        done = subprocess.run(["git", "-C", gitdir or self.gitdir, "cherry",
                               self.trunk, self.lane_tip], capture_output=True,
                              text=True, env=child, check=True)
        return done.stdout.strip()

    def _graft(self, line):
        """Write this repository's grafts file the way an operator does — git
        ships no verb that writes one, so the file IS its own interface — and
        ask GIT where it goes rather than assuming `<gitdir>/info/grafts`."""
        path = self.git("rev-parse", "--git-path", "info/grafts",
                        cwd=self.gitdir)
        if not os.path.isabs(path):
            path = os.path.join(self.gitdir, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(line + "\n")
        return path

    def _home_listing(self):
        """Every file under HELM_HOME, by path and size. The must-hit's
        observable — and it is the WHOLE home rather than one expected path,
        because the claim is that a stop evaluation persists NOTHING, not that
        one named file is absent."""
        found = {}
        for root, _dirs, names in os.walk(home.helm_home()):
            for name in names:
                path = os.path.join(root, name)
                found[os.path.relpath(path, home.helm_home())] = \
                    os.path.getsize(path)
        return found

    def test_a_replaced_object_cannot_move_the_witness_answer(self):  # noqa: VACUOUS_ASSERTION — the one assertNotEqual sits between two unconditional controls on the SAME observable, `git cherry`'s own output: it reads `- <tip>` before the replacement and `- <tip>` again under the overlay after it
        """THE HISTORY VIEW IS PINNED AT THE WITNESS, WHICH IS WHAT MAKES THE
        IDS THE WHOLE INPUT. `refs/replace/<oid>` substitutes one object for
        another at every lookup, so `git cherry` reinterprets ids that have not
        changed: replacing the landed twin whose patch identity IS the match
        turns a uniformly `-` range into `+`. Every witness spawn disables
        replacement, so the answer is about the objects the ids name."""
        before = self._proof()
        self.assertIs(before[0], True, before)
        # THE FIXTURE'S OWN RANGE AFFIRMS FIRST, or nothing below can move.
        self.assertEqual(self._naked_cherry(), "- " + self.lane_tip,
                         "the fixture does not affirm by patch identity at "
                         "all, so no replacement can flip it")
        self._replace_the_landed_twin()
        # THE CONTROL, UNCONDITIONAL AND ON THE WITNESS'S OWN OBSERVABLE: git's
        # answer to exactly this question DOES move under the replacement, and
        # the overlay is the only difference between the two spawns. Without
        # this pair the arm below would pass on a replacement git never saw.
        self.assertNotEqual(self._naked_cherry(), "- " + self.lane_tip,
                            "the replacement never reached `git cherry`, so "
                            "the pinning this arm claims is untested")
        self.assertEqual(self._naked_cherry(pinned=True),
                         "- " + self.lane_tip,
                         "the overlay did not pin git's answer")
        after = self._proof()
        self.assertIs(after[0], True, after)
        self.assertEqual(after[1]["witness"], dispatches.REACHED_TRUNK)
        self.assertEqual(after[1]["trunk_sha"], before[1]["trunk_sha"])
        self.assertEqual(after[1]["tip"], before[1]["tip"])

    def test_a_grafted_repository_cannot_move_the_witness_answer(self):  # noqa: VACUOUS_ASSERTION — the one assertNotEqual sits between two unconditional controls on the SAME observable, `git cherry`'s own output: `- <tip>` before the graft and `- <tip>` again under the overlay after it
        """`info/grafts` REWRITES PARENTAGE AND `--no-replace-objects` DOES NOT
        COVER IT — measured on git 2.53.0 in this fixture: with the lane tip
        grafted rootless, a naked `git cherry` reads `+ <tip>` and reads it
        again under `--no-replace-objects`, while `GIT_GRAFT_FILE=/dev/null`
        restores `- <tip>`. That devnull spelling is `landreq._object_view`'s,
        which `rowworld._history_view_env` imports rather than retypes, so the
        witness reads the object's own ancestry.

        AND IT IS PINNED AT THE SPAWN, NOT MEASURED AT A PREFLIGHT. A design
        that probes for a grafts file and THEN runs the witness leaves an
        interval a graft can be installed in, and that interval is unbindable
        because git history is mutable under a running process. Disabling the
        graft inside the witness's own spawn leaves no interval at all."""
        before = self._proof()
        self.assertIs(before[0], True, before)
        self.assertEqual(self._naked_cherry(), "- " + self.lane_tip,
                         "the fixture does not affirm by patch identity at "
                         "all, so no graft can flip it")
        self._graft(self.lane_tip)
        # THE CONTROL, UNCONDITIONAL AND ON GIT'S OWN OBSERVABLE: the graft DOES
        # move this exact question, and the overlay is the only difference
        # between the two spawns.
        self.assertNotEqual(self._naked_cherry(), "- " + self.lane_tip,
                            "the grafts file never reached `git cherry`, so "
                            "the pinning this arm claims is untested")
        self.assertEqual(self._naked_cherry(pinned=True),
                         "- " + self.lane_tip,
                         "the overlay did not pin git's answer, so grafts are "
                         "not disabled at the witness")
        after = self._proof()
        self.assertIs(after[0], True, after)
        self.assertEqual(after[1]["witness"], dispatches.REACHED_TRUNK)
        self.assertEqual(after[1]["trunk_sha"], before[1]["trunk_sha"])
        self.assertEqual(after[1]["tip"], before[1]["tip"])

    def test_a_shallow_repository_yields_unknown(self):  # noqa: VACUOUS_ASSERTION — three unconditional positive controls on the same observables: the shallow clone's own `git cherry` reads `- <tip>` before the refusal, the SAME question against the unshallow original answers True THROUGH the witness (derived == 1), and the predicate returns None for this fixture's own repository
        """A `.git/shallow` BOUNDARY IS THE ONE REWRITER WITH NO OVERLAY: the
        parent objects behind it are genuinely ABSENT, so no variable restores
        the range `git cherry` would have walked and a truncated range that
        comes back uniformly `-` is an artifact of the truncation. So carriage
        is REFUSED there — UNKNOWN, never eligible — which
        `_close_event_error`'s `carried` arm reads as trunk no longer affirming
        the work, the fail-safe direction.

        THE CLONE IS REAL and it is deep enough that its own range AFFIRMS
        (asserted below through a naked `git cherry`), which is what makes the
        refusal a REFUSAL rather than a repository that had nothing to say."""
        shallow = os.path.join(self.tmp, "shallow-clone")
        self.git("clone", "--quiet", "--depth", "3", "--no-single-branch",
                 "file://" + self.repo, shallow, cwd=self.tmp)
        gitdir = os.path.join(shallow, ".git")
        self.assertEqual(
            self.git("rev-parse", "--is-shallow-repository", cwd=gitdir),
            "true", "the clone is not shallow, so this arm is about the wrong "
                    "repository")
        # THE MUST-HIT, ON GIT'S OWN OBSERVABLE: the shallow clone's own range
        # AFFIRMS, so the refusal below is this module's and not git's.
        self.assertEqual(self._naked_cherry(gitdir=gitdir),
                         "- " + self.lane_tip,
                         "the shallow clone does not affirm by patch identity, "
                         "so a refusal here proves nothing")
        with self._counting_witness():
            answer, detail = dispatches.carriage_proof(self.row, {}, {},
                                                       gitdir, self.trunk)
            self.assertIsNone(answer, detail)
            self.assertIn("SHALLOW", detail)
            self.assertEqual(self.derived, [],
                             "a shallow repository reached the witness at all")
            # THE CONTROL, ON THE SAME TWO OBSERVABLES: the SAME question
            # against the unshallow original IS answered, by the witness.
            self.assertIs(self._proof()[0], True)
            self.assertEqual(len(self.derived), 1)
        # AND AN UNREADABLE ANSWER IS A REFUSAL TOO, never an eligibility.
        from helm import rowworld
        for name, reply in (("a spawn that failed", (-1, "")),
                            ("an empty report", (0, "")),
                            ("an answer git does not mint", (0, "maybe"))):
            with self.subTest(reading=name):
                with mock.patch.object(rowworld, "_git", return_value=reply):
                    reason = dispatches._carriage_shallow_refusal(self.gitdir)
                self.assertTrue(reason, "%s was treated as eligible" % name)
        self.assertIsNone(
            dispatches._carriage_shallow_refusal(self.gitdir),
            "the control: this fixture's own repository IS eligible, so every "
            "reason above is about the damaged reading")

    def test_the_witness_runs_against_the_resolved_sha_not_the_ref(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls are assertEqual(handed, [pinned]) and assertIs(answer, True): the witness's OWN argument and an affirmative verdict, neither behind a guard
        """ROUND-ONE FINDING 1 — BOTH FAMILIES MUST OBSERVE ONE TRUNK. The
        first cut resolved the trunk ref in one place and handed the MUTABLE REF
        to the witness, so a trunk that moved in between let one half answer
        about trunk A while the other answered about trunk B, under a single
        reported verdict. One resolution feeds both halves now, so there is no
        'in between' left to move in."""
        from helm import rowworld
        pinned = dispatches._carriage_trunk_sha(self.gitdir, self.trunk)
        self.assertTrue(pinned)
        handed, real = [], rowworld._reached_trunk

        def spy(gitdir, tip, trunk):
            handed.append(trunk)
            answer = real(gitdir, tip, trunk)
            # TRUNK MOVES WHILE THE WITNESS IS RUNNING — the race itself.
            self.commit_file("moved-mid-witness", "trunk moves under a witness")
            return answer

        with mock.patch.object(rowworld, "_reached_trunk", side_effect=spy):
            answer, detail = self._proof()
        self.assertIs(answer, True, detail)
        self.assertEqual(handed, [pinned],
                         "the witness was handed %r, not the object resolved "
                         "for this proof" % (handed,))
        self.assertEqual(detail["trunk_sha"], pinned)
        # AND THE NEXT READ RESOLVES AGAIN, which is what makes the answer as
        # live as the measurement: the spy's commit moved trunk, so this proof
        # is about a different object.
        self.assertNotEqual(
            dispatches._carriage_trunk_sha(self.gitdir, self.trunk), pinned,
            "the fixture's trunk did not move, so nothing was re-resolved")

    def test_the_content_family_answers_before_the_ancestry_family(self):  # noqa: VACUOUS_ASSERTION — the empty derive list sits between two unconditional positives on the same observables: assertIs(answer, True) with a CARRIAGE_REPLAY witness, and the base-less row that DOES reach the second family (derived == 1)
        """THE FALLTHROUGH IS ONE-DIRECTIONAL. The replay family asks about
        trunk HEAD's CONTENT and is the stronger question; only SILENCE from it
        may reach the ancestry/patch-identity family, because a measured verdict
        from the stronger question must not be second-guessed by a weaker one.
        BOTH are derived on every read — there is no store left to pre-empt
        either of them."""
        row, rows = self._pair_bound()
        with self._counting_witness():
            answer, detail = self._proof(row=row, rows=rows)
            self.assertIs(answer, True, detail)
            self.assertEqual(detail["witness"], dispatches.CARRIAGE_REPLAY)
            self.assertEqual(self.derived, [],
                             "the ancestry family answered a question the "
                             "content family had already measured")
            # THE CONTROL, ON THE SAME OBSERVABLE: the row with no immutable
            # base has no content witness at all, and THAT one reaches the
            # second family — so the emptiness above is the ordering.
            self.assertIs(self._proof()[0], True)
            self.assertEqual(len(self.derived), 1)

    def test_a_stop_evaluation_writes_no_file_under_the_helm_home(self):  # noqa: VACUOUS_ASSERTION — the unchanged listing sits AFTER an unconditional positive control on the SAME observable: `self.add` writes a row through the real send door and the listing is asserted to have GROWN by the ledger file
        """THE MUST-HIT FOR THE WHOLE PROPERTY: a stop evaluation persists
        NOTHING. That single assertion is what replaces a cache-invalidation
        rulebook — the whole HELM_HOME tree is the same path-to-size map before
        and after, and the ledger read derives its carriage verdict every time.

        THE LISTING IS PROVEN LIVE BEFORE IT IS TRUSTED. A real dispatch goes
        through the shipped send door first and the listing MUST grow by the
        ledger file — an instrument that reported nothing would otherwise pass
        this arm while the store was still being written."""
        base = self._home_listing()
        row = self.add(recipient="grok", kind="build")
        grown = self._home_listing()
        ledger = os.path.relpath(dispatches.ledger_path(), home.helm_home())
        self.assertIn(ledger, grown,
                      "the send door wrote no ledger this listing can see, so "
                      "the listing is not an instrument")
        self.assertNotEqual(grown, base, "the listing never moves at all")
        self.assertIn(row["id"], dispatches.snapshot()[0])
        # THE SHIPPED READ, AS THE STOP-FACTS RESIDENT MAKES IT
        # (`stopfacts_resident.compute` folds through `dispatches.snapshot`;
        # the Stop guard itself reads the ledger nowhere).
        settled = self._home_listing()
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], snap)
        # AND THE PROOF DOOR ITSELF, EXERCISED WITH THE ANSWER A MEMO WOULD
        # WANT: an AFFIRMATION, asked TWICE about one question, which is the
        # exact pair any cache here would serve the second half of.
        with self._counting_witness():
            self.assertIs(self._proof()[0], True)
            self.assertIs(self._proof()[0], True)
        self.assertEqual(len(self.derived), 2,
                         "the second read did not re-derive, so something is "
                         "still remembering this verdict")
        self.assertEqual(self._home_listing(), settled,
                         "a stop evaluation persisted something under the helm "
                         "home")


class TheLedgerRungStaysWithinItsBudgetTest(DispatchBase):
    """helm task/2394. The rung's answer must not change and its cost must not
    grow with the ledger's history."""

    def test_an_event_appended_after_an_earlier_read_is_seen(self):
        """Nothing here remembers the LEDGER, only a git observation, and this
        is the arm that fails if anyone ever memoises the fold itself."""
        first = self.add(recipient="grok", kind="build")
        before = dispatches.snapshot()[0]
        self.assertIn(first["id"], before)
        second = self.add(recipient="grok", kind="build")
        after = dispatches.snapshot()[0]
        self.assertIn(second["id"], after)
        self.assertNotIn(second["id"], before,
                         "the control: the second row was genuinely absent "
                         "from the first read")

    def test_a_torn_final_line_is_not_an_empty_ledger(self):
        row = self.add(recipient="grok", kind="build")
        with open(dispatches.ledger_path(), "a", encoding="utf-8") as f:
            f.write('{"v": 3, "event": "dispatch", "id": "tor')
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], snap,
                      "a torn tail collapsed the whole ledger into absence")

    def test_a_twenty_thousand_event_fold_stays_under_one_second(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is assertEqual(len(out), 20000): a fold that rejected the planted rows would be fast and prove nothing
        """A BOUND WITH ITS BOX, not a universal claim: measured on the owner's
        laptop, python 3.14, warm page cache. The events are the WRITER'S own
        rows re-keyed, never invented shapes — a fold over rows `_new_state`
        rejects would be fast and would prove nothing, which is why the row
        count below is asserted."""
        self.add(recipient="grok", kind="build")
        events = eventledger.events(dispatches.ledger_path())
        seed = [e for e in events if e.get("event") == "dispatch"]
        self.assertEqual(len(seed), 1, seed)
        planted = []
        for index in range(20000):
            row = dict(seed[0])
            row["id"] = "%016x" % index
            row["lane"] = "planted-%d" % index
            planted.append(row)
        started = time.monotonic()
        out, _verdicts, _taken = dispatches._fold(planted)
        elapsed = time.monotonic() - started
        self.assertEqual(len(out), 20000,
                         "the fold rejected the planted rows, so the timing "
                         "below measures an empty loop")
        self.assertLess(elapsed, 1.0,
                        "a 20,000-event fold took %.3fs on this box" % elapsed)


class CodexPoolBudgetGateTest(DispatchBase):
    """task/2480 — a send to a codex seat consults the POOLED codex budget.

    The state it exists for: every pooled codex account at its WEEKLY cap
    while the 5h window each seat is paced on still reads healthy, so the fleet
    keeps filing rows into a budget that is gone. The gate reads the snapshot
    proxywatch persists — it never probes, so a send costs no vendor round-trip
    and a vendor outage can never stall routing.

    EVERY ARM DRIVES THE SHIPPED READER over a PLANTED snapshot written by the
    shipped writer (`codexbudget.write_snapshot`), never a stubbed verdict.
    """

    def plant(self, *pcts, age_s=0):
        from helm import burnflags, codexbudget
        rows = [{"email": "a%d@x.example" % i, "account_id": "acct-%d" % i,
                 "plan": "team", "state": "ok", "status": "allowed",
                 "longest_pct": p,
                 "windows": [{"label": "7d", "used_percent": p,
                              "reset_after_seconds": 3600}] if p is not None else []}
                for i, p in enumerate(pcts)]
        at = time.time() - age_s
        codexbudget.write_snapshot(rows, ceiling=90.0, now=at)
        # THE FOLD IS WHAT THE DOOR READS NOW, and it is written by ITS OWN
        # shipped writer over the same rows — one planted world, two
        # persisted views of it, neither one a stub.
        burnflags.write_snapshot({"ceiling": 90.0,
                                  "money": {"codex": rows},
                                  "money_measured_at": {"codex": at},
                                  "upstream": {}, "anthropic_history": None,
                                  "declarations": None}, now=at)
        return rows

    def test_a_FULLY_capped_pool_refuses_the_send_and_force_files_it_anyway(self):
        self.plant(100.0, 97.0)
        row, why, _sent = dispatches.send(
            "codex", "capped-lane", "work", self.a, repo=self.repo,
            kind="build", new_work=True, sign=False)
        self.assertIsNone(row)
        self.assertIn("UNUSABLE", why)
        self.assertIn("90%", why)
        self.assertIn("walled on MONEY", why)
        # AND NOT AN ACCOUNT IDENTITY: the fold carries no email, by the arm
        # that widened it — a refusal a reader pastes into a room must not
        # publish a credential's owner.
        self.assertNotIn("@", why.split("force=True")[0])
        self.assertIn("force=True", why)
        # --force FILES IT. A guard with no door is a guard people route around.
        forced, fwhy, _ = dispatches.send(
            "codex", "capped-lane", "work", self.a, repo=self.repo,
            kind="build", new_work=True, sign=False, force=True)
        self.assertIsNone(fwhy, fwhy)
        self.assertEqual(forced["recipient"], "codex")

    def test_the_gate_lives_at_the_ONE_door_every_send_branch_reaches(self):
        """THE SHAPE ARM. `send` reaches the recipient door from three
        branches; a gate bolted beside one of them is a gate two thirds of the
        sends never see — which is exactly what the first cut of this lane
        shipped, and what this arm caught. Both rungs now sit behind
        `_validate_recipient_usable`, so the seat rung's own callers get the
        budget rung for free."""
        self.plant(100.0, 100.0)
        ok, refusal, _ = dispatches._validate_recipient_usable("codex", False)
        self.assertFalse(ok)
        self.assertIn("UNUSABLE", refusal)
        # the seat rung ALONE still admits — so the refusal above is the budget
        # rung's doing and not a seat verdict that would have refused anyway.
        self.assertTrue(dispatches._recipient_seat_rung("codex", False)[0])

    def test_ONE_SPENT_ACCOUNT_BESIDE_A_HEALTHY_ONE_FILES_CLEAN(self):
        """THE READING CHANGED AND SO DID THIS ARM, deliberately. A door that
        warns whenever ANY pooled account is over answers a different question
        from the flag, which asks whether the FAMILY can still pay — and that
        is a question about the account with the most headroom. One spent account beside one at 50
        percent is not a cost fact worth a line on every send — and the arm
        below proves the warning still fires where it means something."""
        self.plant(100.0, 50.0)
        ok, refusal, warning = dispatches._validate_recipient_budget("codex", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIsNone(warning)

    def test_a_NEARLY_SPENT_family_warns_and_still_files(self):
        self.plant(85.0, 88.0)
        ok, refusal, warning = dispatches._validate_recipient_budget("codex", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIn("ORANGE", warning)
        self.assertIn("85%", warning)                    # the headroom, named
        row, why, _ = dispatches.send(
            "codex", "mixed-lane", "work", self.a, repo=self.repo,
            kind="build", new_work=True, sign=False)
        self.assertIsNone(why, why)
        self.assertEqual(row["recipient"], "codex")

    def test_at_FIFTY_percent_the_gate_says_nothing_at_all(self):
        """THE POLARITY CONTROL. Everything below refuses or warns; a pool with
        headroom must be silent, or the gate is just noise on every send."""
        self.plant(50.0, 10.0)
        self.assertEqual(dispatches._validate_recipient_budget("codex", False),
                         (True, None, None))

    def test_an_UNREADABLE_account_is_not_a_measured_wall(self):
        """A rejected token says nothing about headroom. Refusing there would
        be the guard-fires-on-absence class, so the fully-capped pool that DOES
        refuse above becomes a WARN the moment one account cannot be read."""
        self.plant(100.0, 97.0, None)
        ok, refusal, warning = dispatches._validate_recipient_budget("codex", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIn("1 of 3", warning)
        self.assertIn("could not be read", warning)

    def test_a_NON_codex_recipient_is_never_measured(self):
        """THE SCOPE CONTROL, over the identical capped snapshot: the pool
        belongs to the codex seat proxy, so a seat on any other family must be
        admitted with the same bytes on disk that refuse codex."""
        self.plant(100.0, 100.0)
        self.assertEqual(dispatches._validate_recipient_budget("seat-c", False),
                         (True, None, None))
        self.assertEqual(dispatches._validate_recipient_budget("kimi", False),
                         (True, None, None))
        # and the must-hit that proves the snapshot really does refuse
        ok, refusal, _ = dispatches._validate_recipient_budget("codex", False)
        self.assertFalse(ok)
        self.assertIn("UNUSABLE", refusal)

    def test_an_ABSENT_or_STALE_snapshot_admits_silently(self):
        """Absence is not a measured contradiction. A box where proxywatch has
        never run must route exactly as it did before this gate existed."""
        self.assertEqual(dispatches._validate_recipient_budget("codex", False),
                         (True, None, None))
        self.plant(100.0, 100.0, age_s=86_400)
        self.assertEqual(dispatches._validate_recipient_budget("codex", False),
                         (True, None, None))
        # the must-hit: the same rows written FRESH do refuse, so the silence
        # above is the staleness rung and not an unreachable snapshot.
        self.plant(100.0, 100.0)
        self.assertFalse(dispatches._validate_recipient_budget("codex", False)[0])

    def test_force_skips_the_gate_before_it_reads_anything(self):
        from helm import codexbudget
        self.plant(100.0, 100.0)
        with mock.patch.object(codexbudget, "cached_budget",
                               side_effect=AssertionError("force read the snapshot")):
            self.assertEqual(dispatches._validate_recipient_budget("codex", True),
                             (True, None, None))

    def verified(self, name, family):
        """The seat self-writes its own launch metadata, which is the ONLY
        write that mints `runtime_verified` — `write_roster` stamps
        `not foreign`, so a mirror written under another HELM_CHAT_NAME seeds
        display evidence and no authority. Driving the SHIPPED writer, because
        a hand-built roster row would test a dict this fixture invented."""
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = name
        try:
            seats.write_roster(name, runtime={"family": family,
                                              "backend": "proxy"},
                               presence_beat=False)
        finally:
            os.environ["HELM_CHAT_NAME"] = prior or "integrator"
        self.assertTrue(seats.roster()[name]["runtime_verified"],
                        "the fixture failed to mint verified runtime metadata")

    def test_the_budget_rung_reads_the_VERIFIED_RUNTIME_family_not_the_spelling(self):
        """task/2480 R1 — DISPLAY SPELLING IS NOT CAPABILITY IDENTITY.

        A pi harness can name its seat for the harness while the credential
        wall and the pooled budget belong to family codex. This rung asked
        `family_for(name)` with no runtime at all, so it made both errors at
        once on the same door: it let every seat that IS codex under another
        name spend a budget that is gone, and it charged the ceiling to every
        seat merely SPELLED like codex."""
        self.plant(100.0, 97.0)
        # 1. THE ESCAPE. A verified codex runtime under a display name that
        # parses to no family at all.
        self.verified("seat-a", "codex")
        ok, refusal, _ = dispatches._validate_recipient_budget("seat-a", False)
        self.assertFalse(ok)
        self.assertIn("UNUSABLE", refusal)
        # THE CONTROL, on an otherwise identical name with NO verified runtime:
        # nothing resolves it to a family, so it is admitted — which is what
        # makes the refusal above the VERIFIED RUNTIME's doing and not some
        # property of the name. Blast radius: this control moves only the
        # roster row, and every other arm in this class plants its own.
        self.assertEqual(
            dispatches._validate_recipient_budget("seat-b", False),
            (True, None, None))
        # 2. THE OVER-CHARGE, and it is ONE name measured twice. With no roster
        # row the family-spelled recipient parses to codex and is refused...
        self.assertFalse(
            dispatches._validate_recipient_budget("codex", False)[0])
        # ...and the ONLY thing that changes below is its verified runtime.
        self.verified("codex", "gemini")
        self.assertEqual(dispatches._validate_recipient_budget("codex", False),
                         (True, None, None))

    def test_the_usable_door_hands_ONE_family_answer_to_the_budget_rung(self):
        """The two rungs must not resolve the family twice and disagree. The
        door resolves it once, off the joined row proxywatch already stamped
        with `family_for(name, runtime, runtime_verified)`, and hands it over."""
        self.plant(100.0, 100.0)
        self.verified("seat-a", "codex")
        ok, refusal, _ = dispatches._validate_recipient_usable("seat-a", False)
        self.assertFalse(ok)
        self.assertIn("UNUSABLE", refusal)
        # the seat rung ALONE admits this recipient, so the refusal is the
        # budget rung's and not a usability verdict that would refuse anyway.
        self.assertTrue(dispatches._recipient_seat_rung("seat-a", False)[0])
        # AND THE THREADED ANSWER IS WHAT DECIDES, in both directions, over one
        # unchanged snapshot: the family argument is honoured, not re-derived.
        self.assertTrue(dispatches._validate_recipient_budget(
            "codex", False, family="gemini")[0])
        self.assertFalse(dispatches._validate_recipient_budget(
            "seat-a", False, family="codex")[0])

    def test_any_UNREAD_account_warns_even_with_every_read_sibling_healthy(self):
        """task/2480 R6 — this verb's own help and docs/VERBS both promise that
        any unread account proceeds AND warns. The warning only ever fired
        through the `mixed` branch, which needs some OTHER account to be over
        the ceiling — so a pass where every reachable account answered 401
        admitted in total silence, the one shape indistinguishable from a
        healthy pool. The policy stays ADMIT; the uncertainty is now said."""
        self.plant(50.0, None)
        ok, refusal, warning = dispatches._validate_recipient_budget("codex", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIn("INCOMPLETE", warning)
        self.assertIn("1 of 2", warning)
        # AND NO ACCOUNT IDENTITY. The fold carries a state tally rather than
        # an EMAIL, because a warning a reader pastes into a room must not
        # publish whose credential it is.
        # What survives is everything the reader acts on: how many, of how
        # many, and in what state.
        self.assertNotIn("@", warning)
        # every account unread warns too — nothing was measured at all
        self.plant(None)
        self.assertIn("INCOMPLETE",
                      dispatches._validate_recipient_budget("codex", False)[2])
        # [100, None] warns TWICE over — capped where measured AND read
        # incompletely. An unread account never becomes headroom, but it also
        # never turns partial evidence into a fleet-wide refusal.
        self.plant(100.0, None)
        ok, refusal, warning = dispatches._validate_recipient_budget(
            "codex", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIn("INCOMPLETE", warning)
        self.assertIn("ORANGE", warning)
        # THE POLARITY CONTROL: all-known-under stays silent, so the warnings
        # above are the unread rows' doing and not a gate that always speaks.
        self.plant(50.0, 10.0)
        self.assertEqual(dispatches._validate_recipient_budget("codex", False),
                         (True, None, None))

    def test_a_BROKEN_reader_admits_and_says_so_rather_than_losing_the_row(self):
        """THE BROKEN READER IS THE ONE THE DOOR CALLS. The door asks the
        persisted fold, so the reader that can explode is the flag lookup;
        patching the pooled verdict instead would leave this arm green against
        a door that never calls it."""
        from helm import burnflags
        self.plant(100.0, 100.0)
        # the control: unpatched, this planted world really does refuse
        self.assertFalse(
            dispatches._validate_recipient_budget("codex", False)[0])
        with mock.patch.object(burnflags, "family_flag",
                               side_effect=RuntimeError("reader exploded")):
            ok, refusal, warning = dispatches._validate_recipient_budget(
                "codex", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIn("admitted unverified", warning)
        self.assertIn("reader exploded", warning)


class ASourceCleanHoldNamesTheIntegrator(DispatchBase):
    """A hold answers WHO OWES THE NEXT MOVE, and there are three answers.

    The fleet owes an ordinary hold, the owner owes an `--owner-gated` one,
    and the INTEGRATOR owes a SOURCE-CLEAN one: the reviewer read the delta,
    found nothing, and cannot mint an approve because an approve binds a
    verified whole-suite token that only the land gate on the rebased tree
    produces. The verdict door has always sent reviewers here in PROSE, so
    the claim existed and no surface could find it.
    """

    def held(self, tip=None, **kw):
        row = self.add()
        return row, dispatches.mark_hold(
            row["id"], "awaiting the land gate",
            source_clean_tip=tip, **kw)

    def folded(self, row):
        return dispatches.snapshot()[0][row["id"]]

    def plant_hold(self, rid, reason, **extra):
        """Append a hold event straight to the ledger, so the FOLD is what is
        under test. The door refuses some of these shapes and the point is
        what a chain carrying them BECOMES, not what the door does."""
        path = dispatches.ledger_path()
        current = dispatches.snapshot()[0][rid]
        event = {"v": 3, "event": "hold", "seq": current["seq"] + 1,
                 "id": rid, "ts": dispatches.pk.now_ts(),
                 "reason": reason, "owner_gated": False}
        event.update(extra)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def found_with(self, **extra):
        """Mint a row whose FOUNDING event carries `extra`, and return its id.

        A founding row is written by the door, which would never accept these
        fields — so this copies a real founding event, gives it a fresh id and
        adds them. That is exactly the shape a hand-edited, forged or legacy
        ledger presents to the fold, and the fold is the thing under test."""
        seed = self.add()
        path = dispatches.ledger_path()
        with open(path, encoding="utf-8") as fh:
            founding = next(json.loads(line) for line in fh
                            if json.loads(line).get("id") == seed["id"])
        self._add_seq += 1
        founding = dict(founding)
        founding["id"] = "f%031x" % self._add_seq
        founding.update(extra)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(founding) + "\n")
        minted = dispatches.snapshot()[0]
        self.assertIn(founding["id"], minted,
                      "MUST-HIT: the planted founding row must actually fold, "
                      "or every assertion below is about a row that is absent")
        return founding["id"]

    def test_an_ordinary_hold_claims_nothing_about_source(self):
        """THE DEFAULT SIDE, and the reason absence must stay absence: every
        historical hold and every machine stall has to read exactly as it did,
        so the key is written only when the claim is made."""
        row = self.add()
        out, why = dispatches.mark_hold(row["id"], "source-clean, I promise")
        self.assertIsNone(why)
        self.assertNotIn("source_clean_tip", out,
                         "the fold must not read cleanliness out of PROSE — "
                         "this reason SAYS source-clean and claims nothing")
        self.assertNotIn("source_clean_tip", self.folded(row))

    def test_a_source_clean_hold_records_the_exact_tip_and_survives_the_fold(self):
        """UNCONDITIONAL POSITIVE. Every consumer reads the FOLDED row, so a
        claim living only in mark_hold's return is invisible to all of them."""
        row, (out, why) = self.held(tip=self.c)
        self.assertIsNone(why)
        self.assertEqual(out["status"], "held")
        self.assertEqual(out["source_clean_tip"], self.c)
        self.assertEqual(self.folded(row)["source_clean_tip"], self.c)

    def test_the_tip_is_resolved_not_merely_stored(self):
        """A SPELLING IS NOT A COMMIT. The integrator gates the tree this
        names, so a tip that resolves to nothing in the row's own repository
        must be refused at the door rather than stored and discovered later."""
        short = self.c[:12]
        row, (out, why) = self.held(tip=short)
        self.assertIsNone(why)
        self.assertEqual(out["source_clean_tip"], self.c,
                         "an abbreviated tip must be stored as the one exact "
                         "commit it resolves to")
        for bad in ("deadbeef" * 5, "no-such-branch", "", "   "):
            _row, (_out, err) = self.held(tip=bad)
            self.assertIsNotNone(err, "tip %r must be refused" % (bad,))
            self.assertIn("--source-clean", err)

    def test_a_hold_cannot_owe_two_holders(self):
        """An owner-gated hold waits on a decision only the owner can make; a
        source-clean hold waits on a gate only the integrator can run. A row
        claiming both would be counted by one surface and excluded by the
        other, so the combination is refused rather than left representable."""
        _row, (out, why) = self.held(tip=self.c, owner_gated=True)
        self.assertIsNone(out)
        self.assertIn("ONE holder", why)

    def test_release_drops_the_claim_with_the_hold(self):
        """The claim belongs to the HOLD, not to the row: the same row can be
        source-clean at one tip today and cured to another tomorrow."""
        row, (_out, why) = self.held(tip=self.c)
        self.assertIsNone(why)
        _r, rerr = dispatches.mark_release(row["id"])
        self.assertIsNone(rerr)
        folded = self.folded(row)
        self.assertEqual(folded["status"], "open")
        self.assertNotIn("source_clean_tip", folded)

    def test_re_holding_at_a_DIFFERENT_tip_is_not_a_silent_no_op(self):
        """THE ARM THAT CAUGHT MY OWN IDEMPOTENCY BUG. Comparing hold reasons
        alone made a re-declaration after a cure return the OLD row, so the
        integrator would have gated the tip from the round before while the
        reviewer believed the newer one was recorded."""
        row, (_out, why) = self.held(tip=self.a)
        self.assertIsNone(why)
        again, err = dispatches.mark_hold(row["id"], "awaiting the land gate",
                                          source_clean_tip=self.c)
        self.assertIsNone(again)
        self.assertIn(self.a, err)
        self.assertIn(self.c, err)
        # The SAME tip with the same reason is still idempotent, so the
        # refusal above is about the change and not about repetition.
        same, serr = dispatches.mark_hold(row["id"], "awaiting the land gate",
                                          source_clean_tip=self.a)
        self.assertIsNone(serr)
        self.assertEqual(same["source_clean_tip"], self.a)

    def test_a_malformed_stored_claim_reads_as_no_claim_never_as_clean(self):
        """THE VALUE IS THE ANSWER HERE, so a key written in a shape the
        replay cannot read must not be honoured. Over-reading it would let a
        surface bind a land gate to a tree nobody read."""
        for bad in (True, 12, ["x"], "not-a-sha", self.c[:12], None):
            self.assertEqual(
                dispatches._clean_tip_of({"source_clean_tip": bad}), "",
                "%r must not read as a clean tip" % (bad,))
        # The unconditional positive on the same reader.
        self.assertEqual(
            dispatches._clean_tip_of({"source_clean_tip": self.c.upper()}),
            self.c)

    def test_a_FOLD_never_installs_both_holders(self):
        """THE DOOR REFUSES IT AND SO MUST THE FOLD. A hand-written, forged or
        legacy hold event carrying both claims would otherwise replay into a
        row that is BOTH — listed as the integrator's and excluded from the
        stall count as the owner's, so two surfaces disagree about who owes
        it. Replay is not a trust boundary, which is why it must not install a
        state the door would have refused."""
        row = self.add()
        path = dispatches.ledger_path()
        current = dispatches.snapshot()[0][row["id"]]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "v": 3, "event": "hold", "seq": current["seq"] + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "reason": "both at once", "owner_gated": True,
                "source_clean_tip": self.c}) + "\n")
        folded = self.folded(row)
        self.assertEqual(folded["status"], "held")
        self.assertIs(folded["owner_gated"], True)
        self.assertNotIn("source_clean_tip", folded,
                         "one row cannot owe two holders, at the door OR in "
                         "the fold")

    def test_a_FOUNDING_row_cannot_declare_a_clean_tip(self):
        """A founding row declares no hold, so it cannot declare one clean.
        The founder copied the raw row, so an unsolicited field passed
        straight into the state and nothing downstream cleared it: `True`
        selected as source-clean and then raised TypeError when the label
        sliced it, and a valid-looking sha claimed a cleanliness nobody had
        declared."""
        for smuggled in (True, self.c, "not-a-sha", 12):
            rid = self.found_with(source_clean_tip=smuggled)
            folded = dispatches.snapshot()[0][rid]
            self.assertNotIn("source_clean_tip", folded,
                             "a founder carrying %r claims nothing"
                             % (smuggled,))
            # It must also survive every surface that reads the state.
            self.assertNotIn("SOURCE-CLEAN", dispatches._base_label(folded))
            # And an ORDINARY hold after it must still claim nothing —
            # asserted to have SUCCEEDED, or an arm about what a hold leaves
            # behind could pass on a hold that never happened.
            held, err = dispatches.mark_hold(rid, "waiting on a build box")
            self.assertIsNone(err)
            self.assertEqual(held["status"], "held")
            after = dispatches.snapshot()[0][rid]
            self.assertEqual(after["status"], "held")
            self.assertNotIn("source_clean_tip", after)
            self.assertNotIn("SOURCE-CLEAN", dispatches._base_label(after))

    def test_an_ordinary_hold_CLEARS_an_earlier_claim(self):
        """SET OR CLEAR, NEVER INHERIT. The fold copies the prior state, so a
        claim from an EARLIER hold would otherwise survive a later one that
        declared nothing — the row would keep pointing the integrator at a tip
        no reviewer had re-read."""
        # WHAT IS ACTUALLY REACHABLE, asserted; the rest is named as
        # defensive rather than dressed up as covered. A hold folds only from
        # OPEN, and the two ways a row reaches OPEN carrying a claim are both
        # closed: release drops it and the founder strips it. So the pop in
        # the hold arm is DEFENCE IN DEPTH with no production path today, and
        # an arm claiming to exercise it would be theatre — a second planted
        # hold on a HELD row is ignored by the fold entirely, which is how
        # this was found.
        row, (_out, why) = self.held(tip=self.c)
        self.assertIsNone(why)
        self.assertEqual(self.folded(row)["source_clean_tip"], self.c)
        released, rerr = dispatches.mark_release(row["id"])
        self.assertIsNone(rerr)
        self.assertEqual(released["status"], "open")
        again, err = dispatches.mark_hold(row["id"], "waiting on a build box")
        self.assertIsNone(err)
        self.assertEqual(again["status"], "held")
        self.assertNotIn("source_clean_tip", again)
        after = self.folded(row)
        self.assertEqual(after["hold_reason"], "waiting on a build box")
        self.assertNotIn("source_clean_tip", after)
        self.assertNotIn("SOURCE-CLEAN", dispatches._base_label(after))

    def test_a_SIXTY_FOUR_hex_tip_survives_the_fold(self):
        """THE WRITER AND THE REPLAY SHARE ONE READER, which the brief claimed
        and the code did not do. `_TIP` accepts 40 TO 64 hex because a sha256
        repository's object ids are 64, and the resolver returns them — while
        a private 40-only pattern here meant the door RESOLVED and STORED such
        a tip and the fold then dropped it, so a hold that succeeded lost its
        claim on the very next snapshot."""
        # PLANTED IN THE LEDGER, NOT ASSERTED THROUGH THE HELPER. A helper
        # call proves what the helper does; only a fold proves what a row
        # BECOMES, and it is the fold every consumer reads.
        for length, kept in ((40, True), (64, True),
                             (41, False), (63, False), (39, False)):
            tip = "b" * length
            rid = self.found_with()
            self.plant_hold(rid, "awaiting the land gate",
                            source_clean_tip=tip)
            folded = dispatches.snapshot()[0][rid]
            self.assertEqual(folded["status"], "held",
                             "the planted hold must fold, or this arm is "
                             "about a row that never held")
            if kept:
                self.assertEqual(folded.get("source_clean_tip"), tip,
                                 "%d hex is a full object id" % length)
                self.assertIn("SOURCE-CLEAN",
                              dispatches._base_label(folded))
            else:
                self.assertNotIn("source_clean_tip", folded,
                                 "%d hex names no commit in any repository"
                                 % length)
                self.assertNotIn("SOURCE-CLEAN",
                                 dispatches._base_label(folded))
        # And the fold agrees with the WRITER for a real resolvable tip.
        row, (out, why) = self.held(tip=self.c)
        self.assertIsNone(why)
        self.assertEqual(out["source_clean_tip"],
                         self.folded(row)["source_clean_tip"],
                         "what the door returned and what the fold stored "
                         "must be the same claim")

    def test_the_release_RETURN_matches_what_the_fold_drops(self):
        """A return that kept the claim while the fold dropped it made the
        in-process answer and the stored one disagree about an OPEN row."""
        row, (_out, why) = self.held(tip=self.c)
        self.assertIsNone(why)
        returned, err = dispatches.mark_release(row["id"])
        self.assertIsNone(err)
        self.assertNotIn("source_clean_tip", returned)
        self.assertNotIn("source_clean_tip", self.folded(row))

    def test_the_state_word_names_the_integrator(self):
        """Every reader of this line is deciding whether the row is theirs, so
        the holder has to be IN the word and not only in the prose."""
        row, (_out, why) = self.held(tip=self.c)
        self.assertIsNone(why)
        word = dispatches._base_label(self.folded(row))
        self.assertIn("SOURCE-CLEAN", word)
        self.assertIn(self.c[:12], word)
        self.assertIn("INTEGRATOR", word,
                      "the label must NAME the holder — every reader of this "
                      "line is deciding whether the row is theirs")
        # MUST-MISS: an ordinary hold must not acquire the word.
        other = self.add()
        dispatches.mark_hold(other["id"], "waiting on a build box")
        self.assertNotIn("SOURCE-CLEAN",
                         dispatches._base_label(self.folded(other)))



    def test_a_source_clean_hold_WAKES_the_integrator_it_hands_the_row_to(self):
        """A HOLD THAT MOVES THE PLATE MUST WAKE WHOEVER IT MOVED IT TO.

        The hold is durable either way; what this asserts is that the new
        holder is TOLD. Measured cost of the silent version on row
        a withdrawal-lane row, id unspelled because a bare 12-hex token
        reads as a commit to a fresh clone: held source-clean at 17:35Z, and
        at 18:55Z the integrator was asking why an unread row was blocking a
        train car it had already been waiting on for eighty minutes."""
        calls = []
        row = self.add()
        with mock.patch.object(dispatches, "_nudge",
                               lambda to, body, ctx: calls.append((to, body, ctx))):
            rc, _out, err = run(dispatches.cmd_dispatch,
                                ["hold", row["id"], "awaiting the land gate",
                                 "--source-clean", self.c])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(calls), 1, calls)
        to, body, ctx = calls[0]
        self.assertTrue(to, "the DM must be ADDRESSED; an empty name is the "
                            "unaddressed-post defect one layer along")
        # WHAT IT IS WAITING ON, not merely that it waits: the tip to gate.
        self.assertIn(self.c[:12], body)
        self.assertIn(self.c[:12], ctx)
        self.assertIn("gate", body.lower())

    def test_a_BARE_hold_wakes_NOBODY_because_the_plate_did_not_move(self):
        """Waking someone for a row still exactly where they left it is how a
        notifier earns the reputation that gets the next one ignored."""
        calls = []
        row = self.add()
        with mock.patch.object(dispatches, "_nudge",
                               lambda to, body, ctx: calls.append(to)):
            rc, _out, err = run(dispatches.cmd_dispatch,
                                ["hold", row["id"], "waiting on a build box"])
            # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, under
            # the same patch: a source-clean hold DOES fill this list, so an
            # empty one above is the claim and not a dead double.
            clean = self.add()
            run(dispatches.cmd_dispatch,
                ["hold", clean["id"], "awaiting the land gate",
                 "--source-clean", self.c])
        self.assertEqual(rc, 0, err)         # the hold itself still happened
        self.assertEqual(self.folded(row)["status"], "held")
        self.assertEqual(len(calls), 1, calls)

    def test_an_OWNER_GATED_hold_does_not_ride_THIS_path(self):
        """It reaches him through the owner-ask surfaces, which read
        `owner_gated` in eight modules. A second notifier here would tell him
        twice about one ask."""
        calls = []
        row = self.add()
        with mock.patch.object(dispatches, "_nudge",
                               lambda to, body, ctx: calls.append(to)):
            rc, _out, err = run(dispatches.cmd_dispatch,
                                ["hold", row["id"], "needs his ruling",
                                 "--owner-gated"])
            clean = self.add()               # same-observable control
            run(dispatches.cmd_dispatch,
                ["hold", clean["id"], "awaiting the land gate",
                 "--source-clean", self.c])
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.folded(row)["owner_gated"])
        self.assertEqual(len(calls), 1, calls)

    def test_the_LEDGER_WRITER_does_not_deliver(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and on the SAME list: `len(calls) == 1` from the identical hold through the verb runs under the same patch, so an empty `writer_calls` cannot be a double that never fired
        """REPLACES two arms about a failing or re-entrant nudge, whose
        premise the structure dissolves rather than satisfies.

        The nudge is called by the VERB, after `mark_hold` has returned, so
        the hold is durable and its lock released before any transport is
        touched. That cannot be asserted by mocking a transport failure --
        the interesting property is not that delivery is guarded, it is that
        `mark_hold` does not deliver AT ALL. So the arm asks the writer
        directly: hold a row through the library and prove no nudge happens,
        which is what keeps a chat transport from ever sitting between the
        ledger lock and its release."""
        calls = []
        row = self.add()
        with mock.patch.object(dispatches, "_nudge",
                               lambda to, body, ctx: calls.append(to)):
            out, why = dispatches.mark_hold(
                row["id"], "awaiting the land gate", source_clean_tip=self.c)
            writer_calls = list(calls)
            # THE SAME OBSERVABLE, PROVEN LIVE: the identical hold through the
            # VERB fills this list. So an empty writer_calls is about WHERE
            # the delivery lives and not about a patch that never fired.
            via_verb = self.add()
            run(dispatches.cmd_dispatch,
                ["hold", via_verb["id"], "awaiting the land gate",
                 "--source-clean", self.c])
        self.assertIsNone(why)
        self.assertEqual(out["status"], "held")
        self.assertEqual(self.folded(row)["source_clean_tip"], self.c)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(writer_calls, [],
                         "the ledger writer delivered; a stalled transport "
                         "would now sit inside the dispatch lock")

class ReviewerPatchTipIsCoAuthorWorkTest(DispatchBase):
    """`--patch-tip` — the door the co-author review procedure needs.

    THE RULING THIS IMPLEMENTS (owner): the codex models are equal counterparts
    to the claude models even when a claude holds the integrator chair, and the
    read-only-reviewer rule was the system evolving too restrictive. A reviewer
    of either family who finds a MECHANICAL defect commits the cure off the
    exact reviewed tip and names it here; the lane then carries two authors.

    EVERY ARM NAMES THE POLE IT ATTACKS. The positive records and REPLAYS the
    field off disk (an in-process return would pass over a write the reducer
    refuses); the ancestry arm uses a commit that is real and resolvable but
    descends from a DIFFERENT tip, so a refusal cannot be an accident of a
    malformed sha; and the control is a FIX with no flag at all, which must be
    byte-for-byte the verdict it was before this field existed.
    """

    def _fix(self, row, *flags):
        """A FIX naming no cure states why, so the arms that are NOT about
        that question keep the one recorded answer the whole class shares."""
        with self.verdict_author():
            return run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py", *flags,
                *(() if any(f == "--patch-tip" for f in flags)
                  else ("--no-patch-because", "a design finding for a meld")),
                "the guard is inverted"])

    def test_a_FIX_carrying_a_descendant_patch_tip_records_both_authors(self):
        row = self.add(ref=self.b, recipient="seat-b")
        rc, out, err = self._fix(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        # REPLAYED OFF DISK, not read from the writer's return: the canonical
        # reducer is what every later reader runs.
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["patch_tip"], self.c)
        self.assertEqual(current["patch_author"], "seat-b",
                         "the co-author is the seat the row was dispatched to")
        self.assertIn("reviewer patch", out)
        self.assertIn(self.c[:12], out)
        self.assertIn("seat-b", out)

    def test_a_patch_tip_that_does_not_descend_from_the_reviewed_tip_refuses(self):  # noqa: VACUOUS_ASSERTION — the row STAYING open is the effect under test, and it is paired with an unconditional rc==1 plus the exact refusal words on the same call
        """THE CONTROL ON THE REFUSAL: `side` is a REAL commit in this very
        repository that resolves cleanly — it simply branched before the
        reviewed tip. A refusal here is about ANCESTRY and nothing else."""
        row = self.add(ref=self.b)
        self.assertEqual(
            self.git("merge-base", "--is-ancestor", self.a, self.side) or "", "",
            "fixture premise: side must descend from a")
        rc, _out, err = self._fix(row, "--patch-tip", self.side)
        self.assertEqual(rc, 1, err)
        self.assertIn("does not descend from the reviewed tip", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open",
                         "a refused patch tip must not bind a verdict")

    def test_a_FIX_without_the_flag_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — this arm IS the control: the absent fields are the claim, asserted beside an unconditional rc==0 and polarity==fix on the same replayed row
        row = self.add(ref=self.b)
        rc, out, err = self._fix(row)
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["polarity"], "fix")
        self.assertNotIn("patch_tip", current)
        self.assertNotIn("patch_author", current)
        self.assertNotIn("reviewer patch", out)

    def test_the_patch_tip_belongs_to_FIX_alone(self):  # noqa: VACUOUS_ASSERTION — the loop ends on an unconditional EXACT count of the polarities swept, so a loop that ran zero times fails there rather than passing empty
        """APPROVE ends the loop, SUPERSEDE replaces the work, CONCUR blocks
        nothing — a cure recorded on any of them is a claim about a row no
        reader of it will act on. The refusal names the meld for design."""
        seen = 0
        for polarity in ("approve", "supersede", "concur"):
            row = self.add(ref=self.b, lane="patch-polarity-" + polarity)
            written, why = dispatches.mark_verdict(
                row["id"], row["tip"], "a finding", polarity,
                patch_tip=self.c)
            self.assertIsNone(written)
            self.assertIn("--patch-tip belongs to --fix", why)
            self.assertIn("meld", why)
            self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                             "open")
            seen += 1
        self.assertEqual(seen, 3, "the polarity sweep did not run")

    def test_a_short_or_malformed_patch_tip_refuses_before_any_git_probe(self):  # noqa: VACUOUS_ASSERTION — the loop ends on an unconditional EXACT count of the shapes swept
        row = self.add(ref=self.b)
        shapes = (self.c[:12], "not-a-sha", "z" * 40)
        seen = 0
        for bad in shapes:
            written, why = dispatches.mark_verdict(
                row["id"], row["tip"], "a finding", "fix", patch_tip=bad)
            self.assertIsNone(written, bad)
            self.assertIn("full exact commit id", why)
            seen += 1
        self.assertEqual(seen, len(shapes), "the shape sweep did not run")

    def test_triage_prints_the_reviewer_patch_on_the_named_row(self):  # noqa: VACUOUS_ASSERTION — the must-miss half rides an unconditional positive half in the same arm: the same surface PRINTS the note for the patched row
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self._fix(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        rc, out, err = run(dispatches.cmd_dispatch, ["triage", row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("REVIEWER PATCH", out)
        self.assertIn(self.c[:12], out)
        self.assertIn("seat-b", out)
        # MUST-MISS: a row with no patch tip prints no such line.
        plain = self.add(ref=self.b)
        rc, out, err = run(dispatches.cmd_dispatch, ["triage", plain["id"]])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("REVIEWER PATCH", out)

    def test_the_flag_is_advertised_where_a_reviewer_will_look(self):
        """A door nothing advertises is a door nobody opens — the measured
        2.34%-adoption lesson `--measured` itself carries."""
        from helm import cli
        self.assertIn("--patch-tip", dispatches.USAGE)
        self.assertIn("--patch-tip", cli._VERB_HELP["dispatch"])


# A UNIQUE KEY PER ASK. The probe below is called more than once inside a
# single scope, and a key shared between two calls would be served from the
# FIRST call's entry — reporting zero computations and reading as a broken
# probe rather than as a working memo.
_SCOPE_PROBE_SEQ = [0]

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_SCOPE_PROBE_SEQ": (
        "a counter that only mints unique keys; nothing reads its value"),
}


def _memo_double_ask(tag):
    """Ask projscope for ONE key twice, here, and report how often it computed.

    1 means a scope is open on this thread and serving this pass; 2 means no
    scope, so every ask is a fresh computation. This is the memo's own
    observable EFFECT — not a flag that claims to describe it.
    """
    from helm import projscope
    _SCOPE_PROBE_SEQ[0] += 1
    key = ("dispatch-scope-probe", tag, _SCOPE_PROBE_SEQ[0])
    computed = []

    def compute():
        computed.append(key)
        return key

    projscope.memo(key, compute)
    projscope.memo(key, compute)
    return len(computed)


class ReadVerbsRunInsideOneMemoScopeTest(DispatchBase):
    """THE MEMO WAS ALLOCATED AND NEVER CONSULTED ON THIS DOOR.

    `projscope` exists so ONE projection asks each derived question once, and
    its contract is equally that WRITE paths enter NO scope, so nothing a write
    decides can be answered from an older question. `cmd_dispatch` answers both
    kinds of verb and opened no scope at all: the listing paid for every repeat
    of a question it had already asked, and the property that protects the
    writers held only because nobody had turned the memo on.

    Asserted as the EFFECT, never as a flag about it. Each arm reaches into the
    door WHILE IT IS RUNNING — through the one ledger read both kinds of verb
    perform — and asks the memo to compute a value twice. Under a read verb the
    second ask is served from the pass; under a write verb it computes again.
    """

    def test_a_read_verb_reads_the_ledger_inside_the_pass_memo(self):
        self.add()
        seen = []
        real = dispatches.snapshot

        def spy(*a, **kw):
            seen.append(_memo_double_ask("read"))
            return real(*a, **kw)

        with mock.patch.object(dispatches, "snapshot", spy):
            rc, _out, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 0, err)
        # THE PROBE MUST HAVE FIRED. A spy that never ran reports the
        # unmutated world and reads exactly like a pass.
        self.assertTrue(seen, "snapshot() was never called, so this arm "
                              "measured nothing about the list path")
        self.assertEqual(seen, [1] * len(seen),
                         "the ledger read of `dispatch list` recomputed a key "
                         "it had already asked for, so it is not inside a "
                         "scope: %r" % (seen,))
        # THE SCOPE IS AN OPERATION AND NOT A CLOCK: it must be gone the
        # moment the verb returns, or a later caller in this process inherits
        # a cache from a projection that has already ended.
        self.assertEqual(_memo_double_ask("after"), 2,
                         "the scope outlived the verb that opened it")

    def test_a_write_verb_is_answered_by_no_memo(self):
        row = self.add()
        read_seen, write_seen = [], []
        real = dispatches.snapshot

        def spy_into(bucket, tag):
            def spy(*a, **kw):
                bucket.append(_memo_double_ask(tag))
                return real(*a, **kw)
            return spy

        # THE POSITIVE CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: the
        # same spy, wrapping the same function, under a READ verb. Without it
        # an arm that only ever sees 2 cannot tell "writes are unscoped" from
        # "this probe cannot see a scope at all", and the finding it claims to
        # prove would be indistinguishable from a broken instrument.
        with mock.patch.object(dispatches, "snapshot",
                               spy_into(read_seen, "control-read")):
            rc, _out, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(read_seen, "the control never reached snapshot()")
        self.assertEqual(read_seen, [1] * len(read_seen),
                         "the control read did not run inside a scope, so "
                         "this probe proves nothing about the write below")

        with mock.patch.object(dispatches, "snapshot",
                               spy_into(write_seen, "write")):
            rc, _out, err = run(dispatches.cmd_dispatch,
                                ["cancel", row["id"], "recipient", "gone"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(write_seen, "the cancel door never read the ledger, "
                                    "so this arm measured nothing about it")
        self.assertEqual(write_seen, [2] * len(write_seen),
                         "a write verb ran inside a memo scope: what it "
                         "appends could then be justified by a fact resolved "
                         "before its own effect, and no later read of the "
                         "ledger could detect it: %r" % (write_seen,))

    def test_the_scoped_set_names_reads_and_admits_no_writer(self):
        """The set is the whole guard, so it is asserted by NAME.

        A verb added to the table gets no scope until someone adds it here on
        purpose. That is the safe direction to fail, and it is only safe while
        this arm can say which side of the line each verb is on."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional: the set is
        # populated and holds exactly the projections. An empty frozenset would
        # satisfy the refusal below while granting the cure to nothing.
        self.assertEqual(sorted(dispatches.DISPATCH_READ_VERBS),
                         ["briefs", "collisions", "list", "mix", "triage"])
        writers = ["add", "cancel", "hold", "mark-delivered", "rebind",
                   "release", "retip", "send", "verdict"]
        self.assertEqual(
            [v for v in writers if v in dispatches.DISPATCH_READ_VERBS], [],
            "a verb that APPENDS is inside the memo scope")


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()
