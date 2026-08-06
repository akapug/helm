#!/usr/bin/env python3
"""Dispatch ledger: durable handoff, exact-tip verdict, and hostile storage tests.
All writes use scratch HELM_HOME/HELM_CHAT_DIR; the real ledger is read-only."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
import textwrap
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from helm import (chat, dispatches, eventledger, gate, home, landreq, seat,
                  seats, store, vcs, verdicts)
from tests import subsumption_property

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
    (An independent review of the cure that bound the instant in the first
    place: the ORDER was the residual half.)"""

    def test_read_now_is_bound_before_the_snapshot(self):
        """ASSERTED ON THE AST, NOT ON THE TEXT — and the first cut of this
        test proves why. Scanning the source string for "snapshot()" matched
        the WORD INSIDE THE COMMENT that explains the fix, so the ordering
        check compared a comment against code and failed on correct code. A
        text probe's domain includes prose; the AST's does not."""
        import ast, inspect
        src = inspect.getsource(dispatches.cmd_dispatch)
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
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "snapshot"):
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
    """The owner asked for doubt to be legible and it was captured
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
    """A blast-radius review finding. The parser scanned the WHOLE argv
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
        """A T1 cross-family review, measured at the exact tip — the FOURTH
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
        self.a = self.commit("a")
        self.git("branch", "side", self.a)
        self.b = self.commit("b")
        self.c = self.commit("c")
        self.git("checkout", "-q", "side")
        self.side = self.commit("side")
        self.git("checkout", "-q", self.main)

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
        row = dispatches.add(**defaults)
        self.assertIsNotNone(row)
        return row

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


class AddDeliveryLegTest(DispatchBase):
    """add() could never mark a row delivered, so every row it minted sat at
    needs-confirmation FOREVER and the delivery-confirmation nag chased a state
    no code path could reach. Measured cost: four seats each spent a check-in on
    one such row, and the same nag misbilled an uninvolved seat twice
    (the incident add()'s own docstring records).

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
    """A reviewed defect: no verb could set delivery_ref after send.  A dispatch row
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
    """A live seven-close drain: the close-door grammar
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

        with mock.patch.object(dispatches, "mark_verdict", side_effect=capture):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], self.a, "--fix", "--measured", "safe"])
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
        with mock.patch.object(dispatches, "mark_verdict", side_effect=capture):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], self.a, "--fix", "--measured", "safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT (FIX/MEASURED)", out)
        again, why = returned.pop()
        self.assertIsNone(why)
        self.assertEqual(again, first)
        self.assertEqual(len(dispatches.history(row["id"])), before)

        rc, _out, err = run(dispatches.cmd_dispatch, [
            "verdict", row["id"], self.a, "--fix", "--unverified", "safe"])
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


class RebindRoomFenceTest(DispatchBase):
    """A rebind moves the OBLIGATION and leaves the WORKTREE LEASE with
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
        correct rebind; this is explicitly a warn rung."""
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
    """`--supersedes` left the parent looking ACTIONABLE, so
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
        live, two checked-out copies of one upstream sit at different
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
                              # (caught in review)
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
        kid = self.add(lane=lane, supersedes=parent["id"], force=True,
                       ref=osha, repo=other)               # the TWIN repo
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
    """WHO ACTUALLY HOLDS THIS ROW'S OBLIGATION.

    `add` stamps superseded_by with the FIRST successor and keeps it forever, by
    design and correctly for idempotency. It is the wrong answer to "who carries
    this debt" the moment that first successor dies and a SIBLING takes over —
    a reviewer stands down, the row is re-dispatched. Measured live:
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
                         {"withdrawn", "abandoned"})
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
        for flag in ("withdrawn", "abandoned"):
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


class RetipTest(DispatchBase):
    """retip = re-point one OPEN row at a NEW TIP in place, one strict
    seq-ordered event — same row, same recipient, same chain. The mirror of
    rebind for the case where the BASE moved rather than the reviewer,
    measured six times in one day, once per land that moved trunk under an
    already-dispatched lane.

    DELIBERATELY NOT the cancel+add pair the first build of this verb was:
    a retip is not a ROUND, so it must not burn a chain hop, must not retire
    the row id the recipient is watching, and must not own a two-write crash
    window. One event, every refusal before it, and the seq-0 event keeps the
    original tip forever.

    EVERY ARM HERE MINTS ITS OWN ROW. The old verb's first exercise was run
    against a LIVE review row to see it refuse, and it did not refuse — it
    cancelled a real obligation. A destructive verb is exercised against a
    fixture or not at all."""

    def raw_events(self, rid, kind=None):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if isinstance(e, dict) and e.get("id") == rid
                and (kind is None or e.get("event") == kind)]

    def historical_proof(self, event):
        """An earlier build's `proof` recipe, inlined AS THE FORGER'S TOOL:
        blake2b-16 over canonical JSON minus the proof field, domain
        retip-proof-v1. It lives only in this test file now — the shipped cure
        is that computing it grants nothing, and these arms prove that by
        wielding it."""
        raw = json.dumps({k: v for k, v in event.items() if k != "proof"},
                         sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"))
        return hashlib.blake2b(("retip-proof-v1\0" + raw).encode("utf-8"),
                               digest_size=16).hexdigest()

    def corrupt_supersedes(self, rid):
        """Malform one row's STORED supersedes on disk — the reviewer's
        unreadable-successor fixture. Real code never rewrites the append-only
        ledger; after this, `_replay_chain` reads the field as CHAIN_UNKNOWN.
        Every caller asserts that MUST-HIT itself before trusting a refusal."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for row in events:
                if row.get("id") == rid and row.get("event") == "dispatch":
                    row["supersedes"] = "not-a-chain!"
                f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def test_retip_moves_the_ref_appends_the_event_and_keeps_history(self):
        """THE CONTRACT, all three halves on one move: the ref MOVES (same row
        id, new tip), the EVENT is appended (a strict v3 retip row on the
        ledger), and the OLD ref is PRESERVED — both in the projection's
        `retips` history and in the untouched seq-0 event. Chain identity does
        not move: no successor, no new chain hop, same root. BUILD kind,
        deliberately: a plain a->b trunk hop is a legitimate FORWARD base move
        for a build, while for a review it is different code — the first cut
        of this test rode the vacuous empty/empty "verified" (a cross-family
        P1) by retipping a default-kind row between two trunk commits."""
        row = self.add(recipient="grok", kind="build")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["id"], row["id"], "retip must not mint a new row")
        self.assertEqual(out["tip"], self.b)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["status"], "open")
        self.assertEqual(live["tip"], self.b)
        self.assertEqual([h["old_tip"] for h in live["retips"]], [self.a])
        self.assertEqual(live["retips"][0]["reason"], "trunk moved")
        self.assertEqual(live["retips"][0]["identity"], "verified",
                         "identity must survive a FRESH snapshot — a stamp "
                         "that lives only in the writer's return is transient")
        self.assertEqual(live.get("chain_root"), row["id"],
                         "a base move is not a round: the chain must not move")
        self.assertIsNone(live.get("supersedes"))
        events = self.raw_events(row["id"], "retip")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["old_tip"], self.a)
        self.assertEqual(events[0]["tip"], self.b)
        self.assertEqual(events[0]["identity"], "verified")
        seq0 = self.raw_events(row["id"], "dispatch")
        self.assertEqual([e["tip"] for e in seq0], [self.a],
                         "history is append-only: the original tip stays")

    def test_a_row_carrying_a_VERDICT_refuses_to_be_retipped(self):
        """A verdict BINDS the tip it was written against; re-pointing a
        verdicted row would retarget a reviewer's binding onto code they never
        read — the thing a reviewer refused a countersign over. The status check
        IS the verdict check; this arm stops a later reader relaxing it as
        tidiness. And the binding runs BOTH directions: after a retip, a
        verdict against the OLD tip is refused as stale."""
        row = self.add(recipient="grok", kind="build")
        # CONTROL, unconditional and first: while OPEN the verb DOES move it.
        out, err = dispatches.retip(row["id"], self.b, reason="base moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        # The verdict must bind the CURRENT tip: the old one is stale now.
        _v, stale = dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        self.assertIn("stale", stale)
        # FIX polarity, not approve: an approve requires a verified gate token
        # and this arm is about the TIP BINDING, not gate machinery.
        _v, why = dispatches.mark_verdict(row["id"], self.b, "reviewed", "fix")
        self.assertIsNone(why, why)
        out2, err2 = dispatches.retip(row["id"], self.c, reason="try to move",
                                      repo=self.repo, notify=False)
        self.assertIsNone(out2)
        self.assertIn("only an OPEN row", err2)
        self.assertIn("verdict", err2)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], self.b)

    def test_a_CANCELLED_row_refuses_too(self):
        row = self.add(recipient="grok")
        _c, cerr = dispatches.mark_cancel(row["id"], "work moot")
        self.assertIsNone(cerr, cerr)
        out, err = dispatches.retip(row["id"], self.b, reason="x",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("only an OPEN row", err)

    def test_a_hand_appended_retip_event_after_a_verdict_is_INERT(self):
        """The replay arm enforces the same gate as the writer: a well-shaped
        retip event appended around the writer (hand edit, forged append)
        lands after the verdict and must not move the reviewed tip."""
        row = self.add(recipient="grok")
        _v, why = dispatches.mark_verdict(row["id"], self.a, "reviewed", "fix")
        self.assertIsNone(why, why)
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
            "old_tip": self.a, "reason": "forged", "identity": "verified"}))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["status"], "verdict")
        self.assertEqual(live["tip"], self.a)
        self.assertNotIn("retips", live)

    def test_a_retip_event_that_misnames_the_old_tip_is_INERT(self):
        """The event must NAME the tip it moves. Strict seq already orders
        events; binding each hop to its predecessor makes a spliced or
        out-of-context retip inert instead of silently applied."""
        row = self.add(recipient="grok")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
            "old_tip": self.c, "reason": "spliced", "identity": "verified"}))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.a, "a hop from a tip the row is "
                         "not at must not apply")
        self.assertEqual(live["status"], "open")

    def test_retip_to_the_SAME_tip_refuses_rather_than_churning(self):
        row = self.add(recipient="grok")
        out, err = dispatches.retip(row["id"], self.a, reason="x",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("already names", err)

    def test_retip_without_a_reason_refuses(self):
        """The recipient is being asked to RE-READ. 'Why' is the difference
        between a rebase and a rewrite, and it travels on the event."""
        row = self.add(recipient="grok")
        out, err = dispatches.retip(row["id"], self.b, reason="  ",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("reason", err)

    def test_a_ref_THAT_DOES_NOT_RESOLVE_changes_NOTHING(self):
        """THE DESTRUCTIVE BUG of the verb's first build, kept as the pin: a
        typo'd ref must be indistinguishable from a no-op, never from a
        cancel. In the one-event design there is nothing to strand — but this
        arm still proves the refusal happens before the write."""
        row = self.add(recipient="grok")
        out, err = dispatches.retip(row["id"], "deadbeefdeadbeefdeadbeef",
                                    reason="typo", repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("does not resolve", err)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a))
        # The MUST-HIT positive on the same read that proves the absence: the
        # seq-0 dispatch event is the ONLY event this row carries.
        self.assertEqual([e["event"] for e in self.raw_events(row["id"])],
                         ["dispatch"])

    def test_an_UNRELATED_commit_cannot_inherit_the_brief(self):
        """retip moves an obligation to a new BASE, never to different code.
        `side` carries a patch trunk does not, so its per-commit patch-id
        sequence differs from a trunk commit's, and the recipient's brief —
        written about the old sequence — cannot be silently pointed at it."""
        row = self.add(recipient="grok", kind="review")
        out, err = dispatches.retip(row["id"], self.side,
                                    reason="wrong commit", repo=self.repo,
                                    notify=False)
        self.assertIsNone(out)
        self.assertIn("not the same work", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["tip"], self.a)

    def test_the_CONTROL_a_genuine_REBASE_is_verified_and_accepted(self):
        """The half that must NOT change, and the reason the identity is
        patch-id rather than sha: a rebase changes every sha and no patch. If
        this arm went red the check would have broken the exact case retip
        exists for while looking like it had tightened something."""
        def lane_tip(base, name):
            self.git("checkout", "-q", "-b", name, base)
            with open(os.path.join(self.repo, "lane.txt"), "w",
                      encoding="utf-8") as f:
                f.write("the lane's own work\n")
            self.git("add", "lane.txt")
            self.git("commit", "-q", "-m", "lane work")
            tip = self.git("rev-parse", "HEAD")
            self.git("checkout", "-q", self.main)
            self.git("clean", "-qfd")
            return tip

        old_tip = lane_tip(self.a, "lane-before")
        rebased = lane_tip(self.c, "lane-after")
        self.assertTrue(old_tip and rebased, "fixture must mint both tips")
        self.assertNotEqual(rebased, old_tip, "fixture must actually rebase")
        row = self.add(recipient="grok", kind="review", ref=old_tip)
        out, err = dispatches.retip(row["id"], rebased, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["identity"], "verified")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], rebased)
        self.assertEqual(live["retips"][0]["old_tip"], old_tip)

    def test_a_BUILD_base_moves_only_FORWARD(self):
        """A build row's ref is a BASE, not a review subject: the honest move
        is to a DESCENDANT (trunk advanced), and a non-descendant is rewritten
        or unrelated work that needs a fresh dispatch."""
        row = self.add(recipient="grok", kind="build")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["identity"], "verified")
        sideways = self.add(recipient="grok", kind="build", ref=self.b)
        out2, err2 = dispatches.retip(sideways["id"], self.side,
                                      reason="hop lanes", repo=self.repo,
                                      notify=False)
        self.assertIsNone(out2)
        self.assertIn("FORWARD", err2)

    def test_an_OPEN_successor_blocks_retip_and_a_CLOSED_one_does_not(self):
        """THE COMPOSITION WITH THE DUPLICATE-SUCCESSOR LAW. A row whose OPEN
        successor already supersedes it has demonstrably handed its obligation
        to the child; re-pointing the parent would stand up a second live
        frontier for the same work — the precise duplicate the mint guard
        refuses. A CLOSED successor releases the block: the continuation
        ended and the still-open parent is again the one frontier."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("supersede", err)
        self.assertIn(child["id"][:12], err)
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        out2, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err2, err2)
        self.assertEqual(out2["tip"], self.b)

    def test_the_chain_still_works_ON_TOP_of_a_retipped_row(self):
        """The other direction of composing: a retipped row is still a
        legitimate PARENT. A first successor minted --supersedes it links
        cleanly (inheriting the root), and a second live successor is refused
        by the duplicate-successor guard exactly as on an untouched row."""
        row = self.add(recipient="grok", kind="build")
        _o, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                   repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        child = self.add(recipient="grok", supersedes=row["id"], ref=self.b)
        live = dispatches.snapshot()[0]
        self.assertEqual(live[child["id"]].get("chain_root"), row["id"])
        self.assertEqual(live[row["id"]].get("superseded_by"), child["id"])
        dup, why = dispatches.add("grok", "lane-dup", ref=self.b,
                                  repo=self.repo, supersedes=row["id"],
                                  notify=False, _reason=True)
        self.assertIsNone(dup)
        self.assertIn("duplicate live work", why)

    def test_the_cli_arm_refuses_junk_before_writing_anything(self):
        row = self.add(recipient="grok")
        rc, _out, err = run(dispatches.cmd_dispatch, ["retip", row["id"][:12]])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a),
                         "a usage refusal must write NOTHING")

    def test_the_cli_arm_moves_a_row_and_reports_the_hop(self):
        row = self.add(recipient="grok", kind="build")
        rc, out, _err = run(dispatches.cmd_dispatch,
                            ["retip", row["id"][:12], "--ref", self.b,
                             "--reason", "trunk moved", "--json"])
        self.assertEqual(rc, 0, _err)
        got = json.loads(out)
        self.assertEqual((got["id"], got["tip"]), (row["id"], self.b))
        self.assertEqual((got["retips"][0]["old_tip"], got["identity"]),
                         (self.a, "verified"))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live["status"]), (self.b, "open"))

    def test_replay_enforces_the_successor_frontier_law_the_writer_does(self):
        """A cross-family P1, reproduced verbatim: the writer refuses to
        retip a parent whose OPEN child supersedes it, but a hand-appended
        well-shaped retip at parent.seq+1 APPLIED at replay, because replay
        checked only status/seq/old_tip — replay accepted a transition the
        writer refuses. Now replay reads the successor frontier off the same
        one coherent projection the fold builds: the forged event is inert
        while the child lives, and the SAME-SHAPED event applies once the
        child is closed (the control that proves the refusal is the frontier,
        not the event shape). Identity is `unverified`, deliberately: a
        hand-appended hop that cannot claim the writer verified anything is
        the honest forgery shape, and it keeps this arm pinned to the
        frontier law alone."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "old_tip": self.a, "reason": "adversarial",
                  "identity": "unverified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "an open successor is the one live frontier: the "
                         "parent's tip must not move at replay either")
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(forged)))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual([h["old_tip"] for h in live["retips"]], [self.a],
                         "the control: frontier clear, same event applies")
        self.assertEqual(live["tip"], self.b)

    def test_replay_requires_a_strict_identity_and_keeps_it_durable(self):
        """A cross-family P1: replay neither validated event.identity nor
        copied it anywhere durable — a strict retip with identity OMITTED
        applied, and a verified stamp existed only in the writer's transient
        return value. Identity is now a strict accepted enum (omitted and
        junk are both inert) AND a durable hop field a fresh snapshot still
        carries. The verified half of durability is pinned by the happy-path
        arm; this one pins unverified."""
        row = self.add(recipient="grok", kind="build")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        base = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                "old_tip": self.a, "reason": "adversarial"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(base)))          # omitted
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(base, identity="definitely")))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "an event that cannot say whether identity was "
                         "verified has not earned application")
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(base, identity="unverified")))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.b)
        self.assertEqual([h["identity"] for h in live["retips"]],
                         ["unverified"],
                         "the stamp must survive a fresh snapshot ON the hop")

    def test_two_tips_already_on_trunk_can_never_verify_vacuously(self):
        """A cross-family P1, reproduced verbatim: linear a->b->c with c on
        trunk — both post-merge-base sequences are [], and []==[] blessed
        a->b as verified although b carries a whole commit a does not. When
        both tips sit on trunk the reviewed patch itself decides, and the two
        differ, so this now REFUSES as different work."""
        row = self.add(recipient="grok", kind="review")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("not the same work", err)
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a))

    def test_an_identical_retry_after_a_committed_write_reconciles(self):
        """A cross-family P2: a committed retip whose RESPONSE was lost
        gets re-run, and the exact re-run used to refuse with 'already names'
        — the one sibling mutation that punished the retry pattern every
        other writer reconciles. The identical retry now returns the achieved
        row without another append (and the receipt proves it: one event,
        same seq); a DIFFERENT reason at the same tip is a genuine no-op and
        still refuses."""
        row = self.add(recipient="grok", kind="build")
        first, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        again, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                       repo=self.repo, notify=False)
        self.assertIsNone(err2, err2)
        self.assertEqual((again["id"], again["tip"], again["seq"]),
                         (row["id"], self.b, first["seq"]))
        self.assertEqual(again["identity"], "verified",
                         "the reconciled retry carries the hop's durable stamp")
        self.assertEqual(len(self.raw_events(row["id"], "retip")), 1,
                         "reconciling is a READ: no second event")
        other, err3 = dispatches.retip(row["id"], self.b, reason="different",
                                       repo=self.repo, notify=False)
        self.assertIsNone(other)
        self.assertIn("already names", err3)

    def test_an_UNREADABLE_successor_state_refuses_at_the_writer(self):
        """A cross-family P1, reproduced verbatim: the frontier was an
        equality against the parent id, and a not-closed child whose
        supersedes replays CHAIN_UNKNOWN satisfies no equality — so the one
        state that means 'this check could not look' read as 'no open
        successor' and the writer retipped a parent whose successor state was
        unreadable. UNKNOWN now REFUSES with a named reason and writes
        nothing; closing the unreadable row clears the frontier (the control
        that proves the refusal was the unreadable state, nothing else)."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        self.corrupt_supersedes(child["id"])
        self.assertEqual(
            dispatches.snapshot()[0][child["id"]].get("supersedes"),
            dispatches.CHAIN_UNKNOWN,
            "fixture MUST-HIT: the child must actually replay chain-UNKNOWN")
        before = self.raw_events(row["id"])
        self.assertEqual([e["event"] for e in before],
                         ["dispatch", "superseded"],
                         "fixture MUST-HIT: the parent enters carrying its "
                         "seq-0 row plus the mint-time annotation")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("UNREADABLE", err)
        self.assertIn(child["id"][:12], err,
                      "the refusal must NAME the unreadable row")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["status"], live["tip"]), ("open", self.a))
        self.assertEqual(self.raw_events(row["id"]), before,
                         "a refusal writes NOTHING")
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        out2, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err2, err2)
        self.assertEqual(out2["tip"], self.b,
                         "the control: a CLOSED unreadable row no longer "
                         "occupies the frontier")

    def test_an_UNREADABLE_successor_state_is_inert_at_replay_too(self):
        """The replay half of the same P1: a hand-appended well-shaped retip
        past a child whose supersedes replays CHAIN_UNKNOWN must be inert —
        replay enforces what the writer refuses, and a frontier that could
        not be read never reads as clear. Identity is `unverified` to keep
        the frontier the only law in play (see the open-successor arm)."""
        row = self.add(recipient="grok", kind="build")
        child = self.add(recipient="grok", supersedes=row["id"])
        self.corrupt_supersedes(child["id"])
        self.assertEqual(
            dispatches.snapshot()[0][child["id"]].get("supersedes"),
            dispatches.CHAIN_UNKNOWN,
            "fixture MUST-HIT: the child must actually replay chain-UNKNOWN")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "old_tip": self.a, "reason": "adversarial",
                  "identity": "unverified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "an unreadable frontier must refuse at replay: the "
                         "forged hop applied past a child whose successor "
                         "state could not be read")
        _c, cerr = dispatches.mark_cancel(child["id"], "round abandoned")
        self.assertIsNone(cerr, cerr)
        self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                           dict(forged)))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.b,
                         "the control: frontier readable again, same event "
                         "applies")

    def test_a_MISANCHORED_forge_dies_on_the_derived_tip_binding(self):  # noqa: VACUOUS_ASSERTION — the forge-inert absences are controlled: the writer's retip then moves the SAME row (tip positive) and the SAME event list yields the appended hop (retips/ev positives)
        """SCOPE CORRECTED after a cross-family review measured this arm honestly.

        What this test proves is NARROWER than its old name claimed. It
        forges an old_tip the row was never at, so replay refuses it at the
        DERIVED-TIP BINDING — a guard that predates this lane. It says
        nothing about a forge that anchors correctly; see the arm below,
        which measures that one and finds it APPLIES.

        The self-hashed `proof` is still the right thing to have removed:
        an unkeyed recipe is recomputable by exactly the forger it would
        need to stop, so a better hash was never the cure. The control: the
        writer's real retip still moves the row afterwards, replays clean
        end-to-end, and the event it appends carries NO proof field."""
        row = self.add(recipient="grok", kind="build")
        seq = dispatches.snapshot()[0][row["id"]]["seq"]
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.side,
                  "ref": self.side, "old_tip": self.c,
                  "reason": "unrelated work", "identity": "verified"}
        forged["proof"] = self.historical_proof(forged)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((live["tip"], live.get("retips")), (self.a, None),
                         "the ledger is the witness: a forge that hashes its "
                         "own fields still cannot make the fold's derived "
                         "state say the row was at its old_tip")
        out, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                    repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        ev = self.raw_events(row["id"], "retip")[-1]
        self.assertEqual((ev["old_tip"], ev["tip"], ev["identity"]),
                         (self.a, self.b, "verified"),
                         "MUST-HIT: this is the writer's own appended event, "
                         "not the forge — the absence check below is about "
                         "ITS schema")
        self.assertNotIn("proof", ev,
                         "the writer stamps nothing the fold is pinned to "
                         "ignore")
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(
            (live["tip"], [h["identity"] for h in live["retips"]]),
            (self.b, ["verified"]),
            "the control: the legitimate retip replays clean end-to-end")

    def test_a_CORRECTLY_ANCHORED_forge_APPLIES_and_that_is_the_trust_boundary(self):
        """KNOWN ACCEPTED RISK, recorded so nobody re-derives it and nobody
        believes a defense we do not have. A cross-family review said the
        correctly anchored forge still applies; probed, the review is right.

        An attacker who can APPEND TO THE LEDGER, and who anchors the hop at
        the row's true derived tip, moves the row to any tip it likes and has
        `identity: verified` recorded durably beside it. This test asserts
        that it APPLIES — it does not pretend otherwise.

        WHY NO REPLAY-SIDE ARM CAN CLOSE IT, which is the whole point: replay's
        only inputs ARE ledger events. A writer with append access and a forger
        with append access submit byte-identical evidence, so every check
        available to the fold sees one indistinguishable population. The
        removed `proof` field is the proof of that — an unkeyed recipe is
        recomputable by exactly the forger it would need to stop, and any
        replacement computed from event fields inherits the same defect. A
        stronger check here would be theatre: it would raise the cost of the
        forge by zero and raise our confidence by a lot, which is the worst
        possible trade.

        WHERE THE REAL CURE LIVES: upstream, at the WRITE boundary — dregg
        writer-identity signing, so an append carries a key the forger does
        not hold and the ledger can tell the two populations apart. Filed as
        follow-up work. Until that lands, ledger-append access is root here and
        this test is the honest record of it.

        THE CONTROL that keeps this from excusing everything: the MIS-anchored
        forge above still dies at the derived-tip binding. The boundary is
        exactly `can append AND anchors correctly`, not `can append`."""
        row = self.add(recipient="grok", kind="build")
        snap = dispatches.snapshot()[0][row["id"]]
        seq, derived_tip = snap["seq"], snap["tip"]
        self.assertEqual(derived_tip, self.a,
                         "fixture MUST-HIT: the row starts at its own tip, so "
                         "anchoring below is genuinely CORRECT, not accidental")
        forged = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.side,
                  "ref": self.side, "old_tip": derived_tip,
                  "reason": "unrelated work", "identity": "verified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(live["tip"], self.side,
                         "MEASURED, NOT ASPIRATIONAL: the anchored forge moves "
                         "the row. If this ever starts failing, a real cure "
                         "landed — read the follow-up and rewrite this test "
                         "rather than deleting it")
        self.assertEqual([h["identity"] for h in live["retips"]], ["verified"],
                         "and the forger's own self-asserted stamp is what the "
                         "durable record now carries — the field is testimony, "
                         "never authority")

    def test_the_proof_field_is_inert_on_the_acceptance_path(self):
        """THE MUTATION PIN: nothing in the retip replay arm may
        read `proof`, in either direction. Three same-shaped events, bound
        to the row's true derived tip, differing ONLY in the proof field —
        absent, junk, and the historical recipe computed correctly — must
        produce IDENTICAL outcomes. Mutate replay to consult the field and
        one diverges: requiring a valid proof kills the absent/junk arms
        (the earlier law this pin retires — its acceptance is the
        behavioral delta), honoring it as authority is killed by the
        misbound-forge arm above. The residual this pins as LAW: a bound
        verified stamp is the writer's TESTIMONY, recorded durably, not
        re-proven at fold — the forger who could exploit that holds append
        access and is outside every ledger-resident scheme."""
        exercised = []
        for variant in ("absent", "junk", "self-consistent"):
            row = self.add(recipient="grok", kind="build")
            seq = dispatches.snapshot()[0][row["id"]]["seq"]
            ev = {"v": 3, "event": "retip", "seq": seq + 1, "id": row["id"],
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "old_tip": self.a, "reason": "hand-appended",
                  "identity": "verified"}
            if variant == "junk":
                ev["proof"] = "deadbeef" * 4
            if variant == "self-consistent":
                ev["proof"] = self.historical_proof(ev)
            self.assertTrue(eventledger.append(dispatches.ledger_path(), ev))
            live = dispatches.snapshot()[0][row["id"]]
            self.assertEqual(
                (live["tip"], [h["identity"] for h in live["retips"]]),
                (self.b, ["verified"]),
                "outcome must not depend on the proof field (%s): replay "
                "reads its witness, never the hash" % variant)
            exercised.append(variant)
        self.assertEqual(exercised, ["absent", "junk", "self-consistent"],
                         "the unconditional control: all three variants ran "
                         "and applied — a skipped arm is a vacuous pin")

    def test_a_row_with_no_derivable_tip_anchors_no_retip(self):
        """The underivable half of the same review: a legacy needs-redispatch row has
        NO derivable current tip, and the old binding check string-matched
        absence against absence — '' == '' — so a hand-appended hop APPLIED
        to a row whose state replay could not witness. Underivable now
        anchors nothing: the writer refuses with the named reason (measured:
        it did NOT already refuse — None == None passed its under-lock tip
        re-check), and the same-shaped hop is inert at fold."""
        rid = "ab" * 12
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "id": rid, "recipient": "grok", "lane": "lane-legacy",
            "deadline_s": 3600, "status": "open"}))
        live = dispatches.snapshot()[0][rid]
        self.assertEqual((live["status"], live.get("tip")), ("open", None),
                         "fixture MUST-HIT: an OPEN legacy row with no "
                         "derivable tip")
        out, err = dispatches.retip(rid, self.b, reason="anchor it",
                                    repo=self.repo, notify=False)
        self.assertIsNone(out)
        self.assertIn("derivable", err,
                      "the refusal must NAME the underivable anchor")
        self.assertIn("re-dispatch", err)
        forged = {"v": 3, "event": "retip", "seq": 1, "id": rid,
                  "ts": dispatches.pk.now_ts(), "tip": self.b, "ref": self.b,
                  "reason": "anchor it", "identity": "unverified"}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        live = dispatches.snapshot()[0][rid]
        self.assertIsNone(live.get("tip"),
                          "underivable never reads as anchored: the hop must "
                          "not apply to a row whose tip the fold cannot "
                          "witness")
        self.assertNotIn("retips", live)

    def test_an_exact_retry_reconciles_even_after_the_row_went_terminal(self):
        """A cross-family P2, reproduced verbatim: reconciliation ran
        AFTER the OPEN-only gate, so a delayed retry of a COMMITTED retip
        failed the moment the row went terminal — the gate refused the exact
        retry it protects nothing from. Reconciliation is a READ of achieved
        state and now runs first: the retry returns the achieved row
        (terminality included), appends nothing, and everything that is NOT
        the exact retry still meets the gate."""
        row = self.add(recipient="grok", kind="build")
        first, err = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        _c, cerr = dispatches.mark_cancel(row["id"], "work moot")
        self.assertIsNone(cerr, cerr)
        again, err2 = dispatches.retip(row["id"], self.b, reason="trunk moved",
                                       repo=self.repo, notify=False)
        self.assertIsNone(err2, "a delayed exact retry of a committed retip "
                          "must reconcile, not refuse: %s" % err2)
        self.assertEqual((again["id"], again["tip"]), (row["id"], self.b))
        self.assertEqual(again["identity"], "verified",
                         "the reconciled retry carries the hop's durable stamp")
        self.assertEqual(again["status"], "cancelled",
                         "the reconciled return is the ACHIEVED state, "
                         "terminality included — never a resurrected row")
        self.assertEqual(len(self.raw_events(row["id"], "retip")), 1,
                         "reconciling is a READ: no second event")
        other, err3 = dispatches.retip(row["id"], self.b, reason="different",
                                       repo=self.repo, notify=False)
        self.assertIsNone(other)
        self.assertIn("only an OPEN row", err3,
                      "the control: a non-exact request on a terminal row "
                      "still meets the gate")


class RebindTest(DispatchBase):
    """rebind = cancel-as-rebound + re-add to a new recipient, one operation,
    EVIDENCE-GATED: proxy starvation/hang OR fresh context
    exhaustion suffices; only no evidence across both independent arms refuses.
    --force carries a mandatory reason."""

    def setUp(self):
        super().setUp()
        # REBIND NOW REQUIRES A JOINED TARGET (it moves an obligation a
        # seat already carries, so ABSENT/EMPTY/UNREADABLE all refuse). These
        # tests were green under an EMPTY roster because no such check
        # existed; joining the targets is what the fixture always meant, not a
        # workaround. Any test here that wants the REFUSAL asserts it directly.
        for seat_name in ("ds4pro", "grok"):
            seats.write_roster(seat_name, presence_beat=False)

    def _starve(self, recipient, probe=None, streak=False, hang=False):
        from helm import proxywatch
        row = {"seat": recipient, "config_ok": True, "drift": [],
               "alerted_at": None, "transcript_age_s": 4000 if hang else 0,
               "hang_candidate": hang, "probe": probe,
               "probe_detail": "refused" if probe else None, "probe_ms": 1,
               "log": "streak" if streak else "ok", "log_detail": "HTTP 402"}
        orig = proxywatch.health
        proxywatch.health = lambda seats=None, include_upstream=True, prior_state=None: {"seats": [row]}
        self.addCleanup(setattr, proxywatch, "health", orig)

    def _healthy(self, recipient):
        from helm import proxywatch
        row = {"seat": recipient, "config_ok": True, "drift": [],
               "alerted_at": None, "transcript_age_s": 0,
               "hang_candidate": False, "probe": "healthy",
               "probe_detail": None, "probe_ms": 1, "log": "ok",
               "log_detail": None}
        orig = proxywatch.health
        proxywatch.health = lambda seats=None, include_upstream=True, prior_state=None: {"seats": [row]}
        self.addCleanup(setattr, proxywatch, "health", orig)

    def _child_of(self, source_id):
        """The successor row a rebind wrote for this source, or None."""
        snap = dispatches.snapshot()[0]
        kids = [r for r in snap.values() if r.get("supersedes") == source_id]
        return kids[0] if kids else None

    def test_a_verdict_landing_mid_rebind_cancels_the_child(self):
        """The child is appended under the WRITER lock and the source is
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
        """A review HIGH finding: cancelling the child was not
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
        """An adversarial review sweep: the first version read ONE hop and
        called it an answer. A(open) -> B(cancelled) -> C(open) resurrected A
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
        """A review finding: a 64-hop cap truncated VALID long chains. The visited set
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
        """A review blocker, and the third instance of ONE shape:
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
        it (a review correction to the original proposal in the meld)."""
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
        """A review finding: the cap lived as a named constant in the writer and as TWO
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
        """An adversarial review sweep: if the child was itself rebound before
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
        """A second review blocker. My other nested test plants a VERDICT,
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
        """A second review HIGH finding: the residual path interpolated the
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

    def test_the_rebind_CLI_SAYS_the_brief_did_not_travel(self):  # noqa: VACUOUS_ASSERTION — assertIn binds non-empty stderr
        """MEASURED LIVE: two rows were rebound off a family that had gone
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
        self.assertIn("no recoverable DM message body", said,
                      "the rebinder was never told the brief was unavailable")
        self.assertNotIn("the original DM", said,
                         "add-created rows never had an original DM")
        self.assertIn("ds4pro", said,
                      "the warning must name WHO to re-brief")

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
        from helm import proxywatch
        orig = proxywatch.health
        proxywatch.health = lambda seats=None, include_upstream=True, prior_state=None: (
            _ for _ in ()).throw(RuntimeError("tmpfs gone"))
        self.addCleanup(setattr, proxywatch, "health", orig)
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
        """A review blocker: --repo forwarding let a same-tip
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
        all there is and must still flow (a meld-review coverage
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
        self.assertIn("Re-brief @ds4pro", err,
                      "the no-brief NOTE must stay on stderr beside --json")
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
        separate, why, _ = dispatches.send(
            "codex-3", "key-ns-separate", "one", self.a, repo=clone, key="shared",
            note="first", deadline_s=60, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertNotEqual(separate["id"], first["id"])

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
        # A review HIGH finding: a retarget appended TODAY must be inert — the
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
        # A cross-family adversarial review: ts=1 stringifies below the boundary — the
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
        # A cross-family adversarial review (LOW): a legacy-shaped FIRST row appended after
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
        # empty-string ts is NOT an honest pre-boundary stamp (a review delta)
        empty = dict(fab, id="4d" * 16, ts="", last_updated="")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), empty))
        self.assertEqual(dispatches.rows()["4d" * 16]["status"], "open")
        # the same genesis stamped BEFORE the boundary is honest history: closed
        old = dict(fab, id="2b" * 16, ts=OLD_TS, last_updated=OLD_TS)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), old))
        self.assertEqual(dispatches.rows()["2b" * 16]["status"], "verdict")

    def test_closed_rows_never_carry_needs_redispatch(self):
        # a live row read verdict+needs-redispatch simultaneously —
        # contradictory in --json even though every consumer filtered right.
        base = self._legacy_open("3c" * 4, self.a[:7])
        closed = dict(base, status="verdict", verdict_ref="safe",
                      last_updated=OLD_TS)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), closed))
        got = dispatches.rows()["3c" * 4]
        self.assertEqual(got["status"], "verdict")
        self.assertIsNone(got["migration"])

    def test_garbage_seq_rows_never_crash_replay_or_blind_good_rows(self):
        # A review HIGH finding: a valid legacy row with seq='not-an-int' made
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
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")):
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
    progressing normally — one seat read overdue at 3h while holding a 3h50m
    lease and actively editing, another at 1h42m of lease remaining."""

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

    def claims(self, *rows):
        """(resource, holder, ttl_s) … -> planted live claims."""
        blob = {"_fence": 1}
        for res, holder, ttl in rows:
            blob[res] = {"holder": holder, "lease": "0" * 16, "fence": 1,
                         "exp_mono": time.monotonic() + ttl,
                         "exp_wall": time.time() + ttl, "ts": "2026-08-01T00:00:00Z"}
        os.makedirs(os.path.dirname(seats.claims_path()), exist_ok=True)
        with open(seats.claims_path(), "w", encoding="utf-8") as f:
            json.dump(blob, f)

    def elapsed(self, **kwargs):
        """An open row whose clock has definitely run out."""
        row = self.add(deadline_s=60, **kwargs)
        self.age(row["id"], 3600)
        return dispatches.rows()[row["id"]]

    def lane_resource(self, row):
        """The exact worktree resource encoded by this fixture's row."""
        repo_id = row["repo_id"]
        self.assertEqual(os.path.basename(repo_id), ".git")
        return "worktree:%s:%s" % (
            os.path.basename(os.path.dirname(repo_id)), row["lane"])

    def test_a_recipient_holding_the_DISPATCHED_LANE_is_working_not_late(self):
        row = self.elapsed()
        self.assertTrue(dispatches._is_overdue(row),
                        "positive control: with no claim the clock rules")
        self.claims((self.lane_resource(row), "codex-3", 3600))
        self.assertFalse(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.WORKING)
        self.assertEqual([r["id"] for r in dispatches.overdue()], [])

    def test_case_only_claim_holder_is_the_same_recipient(self):
        row = self.elapsed()
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

    def test_ANOTHER_seats_claim_on_the_same_lane_shields_nothing(self):
        """The claim must belong to the RECIPIENT. A lane held by someone else
        is evidence about them, not about the seat that owes this row."""
        row = self.elapsed()
        self.claims((self.lane_resource(row), "kimi", 3600))
        self.assertTrue(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.IDLE)

    def test_an_EXPIRED_claim_shields_nothing(self):
        """Liveness is seats._sweep's rule, not a key's presence — a lease that
        ran out is exactly the stalled case the alarm exists for."""
        row = self.elapsed()
        self.claims((self.lane_resource(row), "codex-3", -1))
        self.assertTrue(dispatches._is_overdue(row))
        self.assertEqual(dispatches.progress_state(row)[0], dispatches.IDLE)

    def test_a_LANE_SUFFIX_never_counts_as_the_lane(self):
        """A differently-named lane cannot shield the dispatched one."""
        row = self.elapsed()
        self.claims((self.lane_resource(row) + "-followup", "codex-3", 3600))
        self.assertTrue(dispatches._is_overdue(row))

    def test_a_same_named_lane_outside_this_project_shields_nothing(self):  # noqa: VACUOUS_ASSERTION — exact-resource positive control runs before the loop
        """A lane label is not a global identity. Claims are fleet-global, so
        another project's same-named lane — or a non-worktree resource with
        the same suffix — proves nothing about this dispatch."""
        row = self.elapsed()
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
        with mock.patch.object(
                dispatches.gate, "bind",
                return_value=("VERIFIED", "a" * 16, "test receipt")):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "verdict", rid, self.a, "--approve", "--measured",
                "gate:" + "a" * 16, "safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT", out)
        self.assertIn("APPROVE", out.upper())

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

        MEASURED LIVE: a re-gate row minted with `add`, announced as
        minted, and another seat had to read the recipient's pane to discover it
        was never in their task list. The obligation existed; its holder did not
        know. The fix is not to make `add` send — that deletes the primitive —
        it is to make the silence loud."""
        rc, out, err = run(dispatches.cmd_dispatch,
                           ["add", "someseat", "somelane", "--ref", self.a,
                            "--kind", "review", "--repo", self.repo,
                            "--new-work"])
        self.assertEqual(rc, 0)
        # add() NOW NOTIFIES (landed in the same merge), so the
        # honest report is that the mention posted and DELIVERY is what remains
        # unconfirmed. The unconditional "sends NOTHING" from an earlier build was true
        # against a main where add never notified and became a LIE at that merge
        # — pinned here so code and surface can never drift apart again.
        self.assertIn("was mentioned in #main", out)
        self.assertIn("someseat", out)
        self.assertNotIn("sends NOTHING", out + err)

    def test_add_records_its_sender_so_the_row_can_be_attributed(self):
        """A row that cannot name its sender cannot be chased, cancelled, or
        credited — and the stop-guard bills its delivery nag to WHOEVER STOPS
        NEXT. Measured live: two rows minted by one seat landed with
        sender=None and nagged an uninvolved seat, ten minutes apart, to confirm
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
        and its holder ignorant — the live case where a re-gate row sat
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
        # THE RACE (a cross-family review finding): send() releases the lock for the DM; a
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
        # A review HIGH finding (HIGH1): once terminal, NO later event — including a
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
        # A review HIGH finding (HIGH2): bool is an int subclass and == let True==1 /
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
        # never be flagged stranded (review guidance: exercise the real scan)
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
        """REGRESSION for a live misread. The closed label was the
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
        # A cross-family review finding: a resend of an already-CLOSED operation must report the
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
        # A cross-family review finding: when the DM fails OR raises while a cancel/verdict lands,
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
        # case 2 (a fourth reviewed defect): a READABLE snapshot missing the row is
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


if __name__ == "__main__":
    pass  # unittest.main() moved to EOF: 38 later tests were invisible to direct runs

class HoldReleaseTest(DispatchBase):
    """helm dispatch hold/release -- non-terminal pause/resume transitions."""

    def test_hold_puts_an_open_row_into_held_status(self):
        row = self.add(lane="blocked")
        self.assertIn(row["id"], [r["id"] for r in dispatches.open_rows()])
        out, why = dispatches.mark_hold(row["id"], "waiting for upstream land")
        self.assertIsNone(why)
        self.assertEqual(out["status"], "held")
        self.assertEqual(out["hold_reason"], "waiting for upstream land")
        self.assertEqual(dispatches.open_rows(), [])

    def test_hold_is_idempotent_on_the_same_reason(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "blocked on upstream")
        again, why = dispatches.mark_hold(row["id"], "blocked on upstream")
        self.assertIsNone(why)
        self.assertEqual(again["status"], "held")
        self.assertEqual(len(dispatches.history(row["id"])), 2)

    def test_hold_replays_from_a_fresh_disk_snapshot(self):
        row = self.add()
        dispatches.mark_hold(row["id"], "waiting for upstream")
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(replayed["status"], "held")
        self.assertEqual(replayed["hold_reason"], "waiting for upstream")

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
        dispatches.mark_hold(row["id"], "waiting for upstream land")
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


class VerdictTurnTest(DispatchBase):
    """mark_verdict emits the verdict as a chat turn carrying the STRUCTURED
    binding: row fields vlane/vtip/vrid/vref, signed via the
    disjoint verdict digest when signing is on (unsigned here: node URL empty),
    ambient (non-waking), and fail-open — announce failure never fails the
    verdict the ledger already recorded."""

    def _verdict(self, row):
        return dispatches.mark_verdict(
            row["id"], row["tip"], "PASS by codex", "fix")

    def _rows(self, room="main"):
        from helm import chat
        msgs, _total = chat.read(room)
        return msgs

    def test_verdict_announces_a_turn_with_the_structured_binding(self):
        row = self.add(lane="verdict-lane")
        out, why = self._verdict(row)
        self.assertIsNone(why)
        turns = [r for r in self._rows() if r.get("vrid") == row["id"]]
        self.assertEqual(len(turns), 1)
        t = turns[0]
        self.assertEqual(t["vlane"], "verdict-lane")
        self.assertEqual(t["vtip"], row["tip"])
        self.assertEqual(t["vref"], "PASS by codex")
        self.assertEqual(t.get("ambient"), 1)

    def test_announce_failure_never_fails_the_verdict(self):
        from helm import chat
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            row = self.add()
            out, why = self._verdict(row)
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertEqual(out["status"], "verdict")

    def test_idempotent_reverdict_does_not_double_announce(self):
        row = self.add(lane="once")
        self._verdict(row)
        dispatches.mark_verdict(
            row["id"], row["tip"], "PASS by codex", "fix")  # same
        turns = [r for r in self._rows() if r.get("vrid") == row["id"]]
        self.assertEqual(len(turns), 1)

    def test_verdict_room_env_routes_the_announce(self):
        os.environ["HELM_VERDICT_ROOM"] = "gates"
        try:
            row = self.add(lane="routed")
            self._verdict(row)
        finally:
            os.environ.pop("HELM_VERDICT_ROOM", None)
        self.assertEqual([r for r in self._rows("main") if r.get("vrid")], [])
        self.assertEqual(len([r for r in self._rows("gates")
                              if r.get("vrid") == row["id"]]), 1)

    def test_doubt_never_resigns_but_pre_intent_verdicts_heal_once(self):
        # Lifecycle finding A (cross-family review): after an intent exists, a crash window may hide
        # a COMPLETED remote signing — the retry must never re-emit (doubt),
        # only read-only-confirm. Heal-by-emit is reserved for the one
        # provably sign-free state: no intent on record.
        from helm import chat, eventledger
        calls = []
        orig = chat.post
        def crashing(*a, **k):
            calls.append(1)
            raise RuntimeError("down")
        chat.post = crashing
        try:
            row = self.add(lane="doubt", notify=False)
            first, why = self._verdict(row)
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertIn("NEEDS CONFIRMATION", first["announce"])
        self.assertEqual(len(calls), 1)
        spy = []
        chat.post = lambda *a, **k: spy.append(1) or orig(*a, **k)
        try:
            again, _ = self._verdict(row)          # retry: DOUBT, no re-sign
        finally:
            chat.post = orig
        self.assertIn("in doubt", again["announce"])
        self.assertEqual(spy, [])                  # post never called again
        # pre-intent (legacy) verdicts DO heal: erase the attest ledger
        os.remove(dispatches.attest_path())
        healed, _ = self._verdict(row)
        self.assertIn("cite-tier", healed["announce"])
        turns = [r for r in self._rows() if r.get("vrid") == row["id"]]
        self.assertEqual(len(turns), 1)

    def test_doubt_upgrades_read_only_from_the_INTENT_room_not_env(self):
        # Lifecycle finding C: the intent's STORED room is authoritative —
        # an env change never redirects the search, and the upgrade is
        # strictly read-only (found turn -> done, no emit).
        from helm import chat, eventledger
        os.environ["HELM_VERDICT_ROOM"] = "gates"
        try:
            row = self.add(lane="stored-room")
            first, _ = self._verdict(row)          # emits into 'gates'
        finally:
            os.environ.pop("HELM_VERDICT_ROOM", None)
        self.assertIn("cite-tier", first["announce"])
        # wipe DONE so the retry enters the doubt cell with intent standing
        events = list(eventledger.events(dispatches.attest_path()))
        os.remove(dispatches.attest_path())
        for e in events:
            if e.get("event") == "intent":
                eventledger.append(dispatches.attest_path(), e)
        os.environ["HELM_VERDICT_ROOM"] = "elsewhere"   # hostile env change
        spy = []
        orig = chat.post
        chat.post = lambda *a, **k: spy.append(1) or orig(*a, **k)
        try:
            again, _ = self._verdict(row)
        finally:
            chat.post = orig
            os.environ.pop("HELM_VERDICT_ROOM", None)
        self.assertIn("cite-tier", again["announce"])   # found in STORED room
        self.assertEqual(spy, [])                       # read-only upgrade

    def test_doubt_rejects_an_incompletely_signed_matching_row(self):
        # Lifecycle finding E: a matching row with a bare truthy `turn` (no
        # receipt/chain) is NOT committed-signed — doubt stands, nothing emits.
        from helm import chat, eventledger
        row = self.add(lane="e-guard")
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            self._verdict(row)                     # intent stands, no turn
        finally:
            chat.post = orig
        os.makedirs(chat.chat_dir(), exist_ok=True)
        forged = {"ts": "t", "from": "mallory", "text": "VERDICT PASS by codex",
                  "vlane": "e-guard", "vtip": row["tip"], "vrid": row["id"],
                  "vref": "PASS by codex", "turn": "x" * 64}
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "a", encoding="utf-8") as f:
            f.write(json.dumps(forged) + "\n")
        again, _ = self._verdict(row)
        self.assertIn("in doubt", again["announce"])


    def test_reconcile_ignores_forged_bindings_and_heals_the_true_turn(self):
        # A cross-family review finding: same-rid signed rows with WRONG lane/tip/ref
        # (or a plain row merely carrying vrid) must never read as attested.
        from helm import chat
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            row = self.add(lane="strict")
            first, _ = self._verdict(row)          # committed, unemitted
        finally:
            chat.post = orig
        self.assertIn("NEEDS CONFIRMATION", first["announce"])
        # plant forgeries: wrong tip; and a plain row carrying only vrid
        chat.post("fake", who="mallory",
                  verdict={"lane": "strict", "tip": "e" * 40,
                           "rid": row["id"], "ref": "PASS by codex"})
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "t", "from": "mallory", "text": "hi",
                                "vrid": row["id"]}) + "\n")
        again, _ = self._verdict(row)              # retry reconciles
        self.assertNotIn("attested", again["announce"])   # forgeries ignored
        self.assertIn("in doubt", again["announce"])      # and doubt STANDS:
        # intent exists, so the retry must never re-sign — even though only
        # forged rows are on record (lifecycle finding A over the second digest)
        valid = [r for r in self._rows()
                 if r.get("vrid") == row["id"] and r.get("vtip") == row["tip"]
                 and r.get("vref") == "PASS by codex"]
        self.assertEqual(len(valid), 0)


class AttestReducerMatrixTest(DispatchBase):
    """The FULL sidecar partition as pinned regressions (a review bar) —
    file-level x schema x duplicates x live-path, each cell asserting the
    reported state AND whether the emit cell is reachable. UNKNOWN can never
    reach emit; only provably-empty states may heal."""

    def _verdicted(self):
        from helm import chat
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            row = self.add(lane="matrix")
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "PASS", "fix")
        finally:
            chat.post = orig
        self.assertIsNone(why)
        return dispatches.snapshot()[0][row["id"]]

    def _retry(self, row, expect_posts):
        from helm import chat
        spy = []
        orig = chat.post
        chat.post = lambda *a, **k: spy.append(1) or orig(*a, **k)
        try:
            out, why = dispatches.mark_verdict(
                row["id"], row["reviewed_tip"], row["verdict_ref"],
                row["polarity"])
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertEqual(len(spy), expect_posts,
                         "emit-cell reachability violated: %s" % out["announce"])
        return out["announce"]

    def _sidecar(self, row, content_rows, raw_suffix=b""):
        import json as J
        path = dispatches.attest_path()
        with open(path, "wb") as f:
            for r in content_rows:
                f.write(J.dumps(r).encode() + b"\n")
            f.write(raw_suffix)

    def _intent(self, row, **over):
        base = {"v": 1, "event": "intent", "id": row["id"], "ts": "t",
                "room": "main",
                "binding": dispatches._binding_key(
                    row, row["reviewed_tip"], row["verdict_ref"])}
        base.update(over)
        return base

    def _done(self, row, kind="cite-tier", **over):
        from helm import chat
        base = {"v": 1, "event": "done", "id": row["id"], "ts": "t",
                "room": "main",
                "binding": dispatches._binding_key(
                    row, row["reviewed_tip"], row["verdict_ref"]),
                "payload": "", "turn": "", "receipt": "", "chain": None,
                "kind": kind, "text": "VERDICT turn"}
        if kind == "attested":
            # a VALID attested done recomputes over the ledger-truth binding
            payload = chat.verdict_digest(
                str(row.get("lane") or ""), row["reviewed_tip"],
                str(row["id"]), str(row["verdict_ref"] or ""), "VERDICT turn")
            base.update(payload=payload, turn="t" * 64, receipt="r" * 64,
                        chain=3)
        base.update(over)
        return base

    # ---- surface: the split must be VISIBLE, not just detected ---------------
    def test_a_REMINTED_row_is_reported_UNVERIFIABLE_and_a_MATCHING_one_is_not(self):
        """The pair is the point: the same row, same code, only the ledger's
        binding differs — so a pass cannot come from the probe simply saying
        'unverifiable' to everything. Measured live: five rows sat
        in exactly this state reading as ordinary decided rows."""
        row = self._verdicted()
        # MUST-NOT-HIT: ledger bound to the evidence the row actually carries
        self._sidecar(row, [self._intent(row),
                            self._done(row, kind="attested")])
        self.assertEqual(dispatches.attest_unverifiable([row]), frozenset(),
                         "a row whose binding MATCHES must never be flagged")
        # MUST-HIT: the same row re-minted with different evidence. Nothing is
        # corrupt — the ledger describes evidence E1, the row now carries E2.
        stale = dict(row)
        stale["verdict_ref"] = str(row["verdict_ref"]) + " (first draft)"
        self._sidecar(row, [self._intent(stale),
                            self._done(stale, kind="attested")])
        flagged = dispatches.attest_unverifiable([row])
        self.assertIn(str(row["id"]), flagged)
        # and it must reach the SURFACE, which is the entire defect: the
        # detector already existed and printed to a terminal nobody reads.
        self.assertIn("ATTEST UNVERIFIABLE", dispatches._label(row, flagged))
        # the verdict itself still shows — composed, never replaced
        self.assertIn(dispatches._base_label(row),
                      dispatches._label(row, flagged))

    def test_the_unverifiable_probe_NEVER_WRITES_TO_THE_LEDGER(self):
        """A probe that writes would MINT the rows it was asked to count:
        _announce_verdict appends an intent when none exists, so the read-only
        path must go through _attest_state. Asserted by BYTES, because 'no
        exception raised' would pass even if the file were rewritten."""
        row = self._verdicted()
        self._sidecar(row, [self._intent(row)])
        with open(dispatches.attest_path(), "rb") as fh:
            before = fh.read()
        # POSITIVE CONTROL: two reads of an EMPTY file are also equal, so
        # equality alone would pass on a ledger that never existed.
        self.assertTrue(before, "fixture wrote no ledger — equality is vacuous")
        dispatches.attest_unverifiable([row])
        with open(dispatches.attest_path(), "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after, "the probe mutated the attest ledger")

    def test_an_UNREADABLE_ledger_yields_EMPTY_rather_than_a_guess(self):
        """A false ATTEST UNVERIFIABLE on the fleet's main surface teaches
        people to ignore the true ones, so an unreadable ledger says nothing."""
        row = self._verdicted()
        # POSITIVE CONTROL FIRST: with a mismatched ledger present, this exact
        # row IS flagged. Without it, an empty answer proves nothing — a probe
        # that always returned frozenset() would pass.
        stale = dict(row)
        stale["verdict_ref"] = str(row["verdict_ref"]) + " (first draft)"
        self._sidecar(row, [self._intent(stale),
                            self._done(stale, kind="attested")])
        self.assertIn(str(row["id"]), dispatches.attest_unverifiable([row]))
        os.remove(dispatches.attest_path())
        self.assertEqual(dispatches.attest_unverifiable([row]), frozenset())

    def test_a_row_that_was_NEVER_ATTESTED_is_not_flagged(self):  # noqa: VACUOUS_ASSERTION — the absence (reviewed_tip is None) is a PRECONDITION, not the claim; the claim is the exact set equality below, which pins attest_unverifiable to a NON-EMPTY expected set {other}, so it can only hold if the probe both FOUND `other` and OMITTED `row`. A probe returning frozenset() for everything fails it. The rung cannot see this because assertEqual against a literal is an "exact scalar pin" it discards, so no control on `row` is countable here.
        """reviewed_tip is None on a row with no verdict: there is no signed
        record for a missing one to contradict, so it is not a split."""
        other = self._verdicted()
        # POSITIVE CONTROL ON reviewed_tip ITSELF: a misspelled key would read
        # None on every row and make the assertion below pass for the wrong
        # reason, so prove the field is populated when it should be.
        self.assertIsNotNone(other.get("reviewed_tip"))
        row = self.add(lane="never-verdicted")
        # POSITIVE CONTROL ON `row` ITSELF: without it, assertIsNone would pass
        # just as happily on an empty dict or a misspelled key — the absence
        # has to be a fact about a REAL populated row. assertTrue rather than
        # assertEqual deliberately: an equality against a literal is an exact
        # scalar pin, which the vacuous-assertion rung classifies as "neither
        # proof nor claim" and discards, so it would control nothing.
        self.assertTrue(row["id"])
        self.assertTrue(row["lane"])
        self.assertIsNone(row.get("reviewed_tip"))
        # AND on the probe: a genuinely split row passed in alongside IS
        # flagged, so the exemption is about THIS row rather than the probe
        # answering empty to everything.
        stale = dict(other)
        stale["verdict_ref"] = str(other["verdict_ref"]) + " (first draft)"
        self._sidecar(other, [self._intent(stale),
                              self._done(stale, kind="attested")])
        flagged = dispatches.attest_unverifiable([row, other])
        self.assertEqual(flagged, frozenset({str(other["id"])}))

    def test_the_label_WITHOUT_the_set_is_unchanged_for_every_row_shape(self):
        """Regression guard for the rename: _label defaults to the empty set,
        so every existing caller keeps byte-identical output."""
        row = self._verdicted()
        # UNCONDITIONAL, outside the loop: an emptied shape tuple would skip
        # every assertion below and still pass.
        self.assertTrue(dispatches._base_label(row))
        self.assertEqual(dispatches._label(row), dispatches._base_label(row))
        self.assertEqual(
            dispatches._label(row, frozenset({str(row["id"])})),
            dispatches._base_label(row) + " / ATTEST UNVERIFIABLE")
        for shape in ({}, {"abandoned": True}, {"closed_by_landing": True},
                      {"discharged": True}, {"close_reason": "superseded"},
                      {"status": "cancelled"}, {"migration": "x"},
                      {"delivery": "unobserved"}):
            probe = dict(row)
            probe.update(shape)
            # POSITIVE CONTROL: the label must be non-empty, or "unchanged"
            # would be satisfied by two empty strings for every shape.
            self.assertTrue(dispatches._base_label(probe))
            self.assertEqual(dispatches._label(probe),
                             dispatches._base_label(probe))
            # and the marker composes onto whatever that shape rendered
            self.assertEqual(
                dispatches._label(probe, frozenset({str(probe["id"])})),
                dispatches._base_label(probe) + " / ATTEST UNVERIFIABLE")

    # ---- A. file level -------------------------------------------------------
    def test_A1_missing_file_is_provably_empty_and_heals_once(self):
        row = self._verdicted()
        os.remove(dispatches.attest_path())
        self.assertIn("cite-tier", self._retry(row, expect_posts=1))

    def test_A2_unreadable_file_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        os.chmod(dispatches.attest_path(), 0)
        try:
            self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        finally:
            os.chmod(dispatches.attest_path(), 0o600)

    def test_A3_empty_file_is_provably_empty_and_heals_once(self):
        row = self._verdicted()
        self._sidecar(row, [])
        self.assertIn("cite-tier", self._retry(row, 1))

    def test_A4_torn_tail_is_not_durable_and_is_ignored(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b'{"v":1,"event":"intent"')
        self.assertIn("cite-tier", self._retry(row, 1))   # torn != event

    def test_A4b_historical_blank_separator_is_not_corruption(self):
        row = self._verdicted()
        intent = self._intent(row)
        with open(dispatches.attest_path(), "wb") as f:
            f.write(b"\n" + json.dumps(intent).encode() + b"\n\n")
        self.assertIn("in doubt", self._retry(row, 0))

    def test_A5_malformed_terminated_line_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b"not json\n")
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_A6_non_object_row_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b'["list"]\n')
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    # ---- B. intent schema ----------------------------------------------------
    def test_B1_valid_intent_routes_to_doubt_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row)])
        self.assertIn("in doubt", self._retry(row, 0))

    def test_B2_unknown_event_name_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [{"v": 1, "event": "garbage", "id": row["id"]}])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B3_intent_missing_key_is_UNKNOWN(self):
        row = self._verdicted()
        bad = self._intent(row); del bad["room"]
        self._sidecar(row, [bad])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B4_intent_extra_key_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, extra="x")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B5_intent_v_wrong_type_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, v="1")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B6_intent_empty_ts_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, ts="")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B7_intent_binding_mismatch_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, binding="f" * 32)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B8_identical_duplicate_intent_is_idempotent_doubt(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._intent(row)])
        self.assertIn("in doubt", self._retry(row, 0))

    def test_B9_different_duplicate_intent_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._intent(row, room="evil")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    # ---- C. done schema ------------------------------------------------------
    def test_C1_done_without_intent_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._done(row)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C2_valid_attested_done_reports_attested(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, "attested")])
        self.assertIn("attested", self._retry(row, 0))

    def test_C3_valid_cite_done_reports_cite(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row)])
        self.assertIn("cite-tier", self._retry(row, 0))

    def test_C4_done_room_mismatch_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, room="other")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C5_done_binding_mismatch_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, binding="e" * 32)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C6_attested_done_missing_receipt_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, "attested", receipt="")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C7_cite_done_carrying_turn_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, turn="x" * 64)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C8_done_unknown_kind_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, kind="wat")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C9_conflicting_duplicate_done_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row),
                            self._done(row, ts="t2")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C10_identical_duplicate_done_reports_once(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row), self._done(row)])
        self.assertIn("cite-tier", self._retry(row, 0))

    def test_C11_foreign_id_rows_are_ignored(self):
        row = self._verdicted()
        foreign = self._intent(row, id="other-rid", binding="a" * 32)
        self._sidecar(row, [foreign, self._intent(row)])
        self.assertIn("in doubt", self._retry(row, 0))

    # ---- D. live path --------------------------------------------------------
    def test_D1_emit_failure_keeps_intent_and_reports_doubtful(self):
        row = self._verdicted()          # emit already failed at verdict time
        intent, done, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNotNone(intent)
        self.assertIsNone(done)
        self.assertIsNone(unknown)

    def test_D2_invalid_live_turn_never_persists_done(self):
        # cell 6: a forged/partial turn must not append a done a retry could
        # upgrade — doubt both times, zero valid turns.
        from helm import chat
        row = self._verdicted()
        forged = {"vlane": "matrix", "vtip": row["reviewed_tip"],
                  "vrid": row["id"], "vref": "PASS", "turn": "x" * 64}
        orig = chat.post
        chat.post = lambda *a, **k: dict(forged)
        try:
            first = self._retry_raw(row)
        finally:
            chat.post = orig
        self.assertIn("in doubt", first)
        intent, done, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNone(done)          # nothing persisted to upgrade later

    def _retry_raw(self, row):
        out, why = dispatches.mark_verdict(
            row["id"], row["reviewed_tip"], row["verdict_ref"],
            row["polarity"])
        self.assertIsNone(why)
        return out["announce"]

    def test_D3_done_append_failure_reports_not_stronger_than_durable(self):
        from helm import chat, eventledger
        row = self._verdicted()
        os.remove(dispatches.attest_path())      # provably empty: heal path
        orig = eventledger.append_unlocked
        def flaky(path, r):
            if r.get("event") == "done":
                return False
            return orig(path, r)
        eventledger.append_unlocked = flaky
        try:
            state = self._retry(row, 1)          # emit happens once
        finally:
            eventledger.append_unlocked = orig
        self.assertIn("NEEDS CONFIRMATION", state)

    def test_D4_doubt_upgrade_appends_validated_done(self):
        row = self._verdicted()                  # intent stands, turn absent
        again = self._retry(row, 0)              # doubt (room has no turn)
        self.assertIn("in doubt", again)
        # deliver the true unsigned turn out-of-band, then reconcile upgrades
        from helm import chat
        chat.post("VERDICT PASS — manual", room="main", ambient=True,
                  verdict={"lane": row.get("lane"), "tip": row["reviewed_tip"],
                           "rid": row["id"], "ref": row["verdict_ref"]})
        upgraded = self._retry(row, 0)
        self.assertIn("cite-tier", upgraded)
        _i, done, _u = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNotNone(done)


class AttestReducerRound5Test(DispatchBase):
    """Cross-family counterexample pins: replay re-verification with
    verifier-call reachability, bool-proof types, unscopable rows, and
    concurrent done-append discrimination."""

    _verdicted = AttestReducerMatrixTest._verdicted
    _retry = AttestReducerMatrixTest._retry
    _sidecar = AttestReducerMatrixTest._sidecar
    _intent = AttestReducerMatrixTest._intent
    _done = AttestReducerMatrixTest._done

    def test_R1_forged_attested_done_fails_reverification_with_verifiers_called(self):
        from helm import chat
        row = self._verdicted()
        forged = self._done(row, "attested", payload="chat:b2b:" + "f" * 64)
        self._sidecar(row, [self._intent(row), forged])
        calls = {"payload_for": 0, "committed_signed": 0}
        op, oc = chat.payload_for, chat.committed_signed
        chat.payload_for = lambda *a, **k: calls.__setitem__(
            "payload_for", calls["payload_for"] + 1) or op(*a, **k)
        chat.committed_signed = lambda *a, **k: calls.__setitem__(
            "committed_signed", calls["committed_signed"] + 1) or oc(*a, **k)
        try:
            state = self._retry(row, 0)
        finally:
            chat.payload_for, chat.committed_signed = op, oc
        self.assertIn("NEEDS CONFIRMATION", state)      # never attested/cite
        self.assertGreater(calls["committed_signed"], 0)
        # a VALID done then re-verifies attested, with the verifier consulted
        self._sidecar(row, [self._intent(row), self._done(row, "attested")])
        calls["payload_for"] = 0
        chat.payload_for = lambda *a, **k: calls.__setitem__(
            "payload_for", calls["payload_for"] + 1) or op(*a, **k)
        try:
            state = self._retry(row, 0)
        finally:
            chat.payload_for = op
        self.assertIn("attested", state)
        self.assertGreater(calls["payload_for"], 0)     # verified, not trusted

    def test_R2_bool_v_and_bool_chain_are_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, v=True)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        i, d, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNone(i)
        self.assertIsNone(d)
        self.assertIn("schema violation (v)", unknown or "")
        self._sidecar(row, [self._intent(row),
                            self._done(row, "attested", chain=True)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        i, d, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIn("evidence incomplete", unknown or "")

    def test_R3_unscopable_complete_row_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b'{"v":1,"event":"garbage"}\n')
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        self._sidecar(row, [], raw_suffix=b'{"v":1,"event":"intent"}\n')
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_R4_concurrent_done_writers_append_exactly_once(self):
        from helm import chat
        row = self._verdicted()
        turn = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "VERDICT turn", "turn": "", "receipt": "",
                "chain": None, "payload": ""}
        first = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main", dict(turn), chat)
        second = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main", dict(turn), chat)
        self.assertIn("cite-tier", first)
        self.assertEqual(first, second)                  # idempotent report
        import json as J
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(len([r for r in rows if r.get("event") == "done"]), 1)

    def test_R5_forged_live_turn_reaches_record_done_and_is_refused(self):
        from helm import chat
        row = self._verdicted()          # intent stands
        os.remove(dispatches.attest_path())              # heal cell
        reached = []
        orig_rd = dispatches._record_done
        def spy_rd(*a, **k):
            reached.append(1)
            return orig_rd(*a, **k)
        dispatches._record_done = spy_rd
        forged = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                  "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                  "text": "x", "turn": ["not", "a", "hash"], "receipt": {},
                  "chain": "9", "payload": "junk"}
        orig_post = chat.post
        chat.post = lambda *a, **k: dict(forged)
        try:
            out, why = dispatches.mark_verdict(
                row["id"], row["reviewed_tip"], row["verdict_ref"],
                row["polarity"])
        finally:
            chat.post = orig_post
            dispatches._record_done = orig_rd
        self.assertIsNone(why)
        self.assertEqual(len(reached), 1)                # verifier REACHED
        self.assertIn("NEEDS CONFIRMATION", out["announce"])
        intent, done, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNone(done)
        self.assertIsNone(unknown)                       # file still VALID
        self.assertIsNotNone(intent)
        import json as J
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            raw_rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(                                 # raw truth: 0 done
            len([r for r in raw_rows if r.get("event") == "done"]), 0)

    def test_R6_forged_nonempty_payload_on_unsigned_turn_is_refused(self):
        from helm import chat
        row = self._verdicted()
        turn = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "x", "turn": "", "receipt": "", "chain": None,
                "payload": "forged-nonempty"}
        state = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main", turn, chat)
        self.assertIsNone(state)
        import json as J
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            raw_rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(
            len([r for r in raw_rows if r.get("event") == "done"]), 0)

    def test_R7_ack_or_react_overlap_is_not_a_verdict_turn(self):
        from helm import chat
        row = self._verdicted()
        base = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "x", "turn": "", "receipt": "", "chain": None}
        self.assertTrue(dispatches._is_this_verdicts_turn(dict(base), row, chat))
        self.assertFalse(dispatches._is_this_verdicts_turn(
            dict(base, ack="foreign-shape"), row, chat))
        self.assertFalse(dispatches._is_this_verdicts_turn(
            dict(base, react="👍|1|x"), row, chat))

    def test_R8_numeric_id_row_is_unscopable_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=(
            b'{"v":1,"event":"intent","id":123456,"ts":"t","room":"main",'
            b'"binding":"x"}\n'))
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        _i, _d, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIn("unscopable", unknown or "")

    def test_R9_falsey_wrong_type_wire_values_are_refused_zero_write(self):
        # The FULL wire-value partition — truthiness must never
        # decide shape. NON-VACUOUS by construction: the intent STAYS on
        # file, so the only refusal path is the partition itself, and the
        # exactly-empty valid shape RECORDS at the end (reachability).
        from helm import chat
        row = self._verdicted()                          # intent stands
        base = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "x", "turn": "", "receipt": "", "chain": None,
                "payload": ""}
        cases = ([dict(base, turn=v) for v in (0, False, [])]
                 + [dict(base, receipt=v) for v in (0, False, [])]
                 + [dict(base, payload=v) for v in (0, False, [], {})]
                 + [dict(base, chain=v) for v in (0.0, False, "3")])
        import json as J
        for turn in cases:
            state = dispatches._record_done(
                row, row["reviewed_tip"], row["verdict_ref"], "main",
                turn, chat)
            self.assertIsNone(state, "laundered: %r" % (turn,))
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            raw_rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(
            len([r for r in raw_rows if r.get("event") == "done"]), 0)
        # reachability: the exactly-empty unsigned shape RECORDS cite-tier
        state = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main",
            dict(base), chat)
        self.assertIn("cite-tier", state or "")


if __name__ == "__main__":
    unittest.main()


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

    def test_unavailable_policy_is_not_invalid_and_scope_is_send_review_only(self):
        with mock.patch.object(dispatches, "_ref_sanity", return_value=[]):
            rc, _out, err = self.send_cli()
        self.assertEqual(rc, 0)
        self.assertIn("check unavailable", err)
        self.assertNotIn("outside the current approval tier", err)
        with mock.patch.object(dispatches, "_approval_tier_advisory",
                               side_effect=AssertionError("wrong scope")), \
                mock.patch.object(dispatches, "_ref_sanity", return_value=[]):
            rc, _out, _err = self.send_cli(recipient="builder", kind="build")
            self.assertEqual(rc, 0)
            rc, _out, _err = run(dispatches.cmd_dispatch, [
                "add", "reviewer", "approval-check", "--ref", self.a,
                "--repo", self.repo, "--kind", "review", "--new-work"])
            self.assertEqual(rc, 0)


class RefSanityTest(DispatchBase):
    """A --ref that does not mean what the sender thinks, caught at WRITE time.

    Both shapes were committed live by the integrator:
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
        """A review finding: collapsing to the family stem probed
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
    """A reviewed gap: an APPROVE binds and NOTHING wakes the lander.
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
        # The nudge fires AFTER the ledger lock (a review finding: delivery
        # must not hold the durability lock). Call it explicitly — the CLI
        # verdict handler does this, and this test pins it still fires.
        dispatches._verdict_land_nudge(row)
        return row

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
        """A review finding: a stalled chat transport inside the ledger lock
        stalls every verdict in the fleet. mark_verdict returns first; the
        nudge is a SEPARATE call — the verdict is durable; delivery is not."""
        row, _ = self._genuine_approve_no_nudge()
        self.assertIsNotNone(row)
        self.assertEqual(self._sents, [])  # no DM inside mark_verdict
        # Now fire it — the DM happens
        dispatches._verdict_land_nudge(row)
        self.assertTrue(self._sents, "nudge must fire when called explicitly")
        self.assertIn("helm lr land", self._sents[0][1])

    def test_fix_does_NOT_trigger_a_dm(self):
        erow = self.add()
        dispatches.mark_verdict(erow["id"], erow["tip"],
                                "FIX evidence", "fix")
        for (_lander, _t, _w) in self._sents:
            self.assertNotIn("helm lr land", _t,
                             "fix must not trigger land nudge")


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
    runs for LAND-AUTHORIZING writes only (a review finding): a FIX on the
    old tip stays truthful when the author advances while fixing — the
    movement is often caused by the review — and a SUPERSEDE is directly
    caused by it. Identity is the row's write-boundary ref_branch, never the
    free-text lane; a raw SHA binds only when exactly one local branch has it
    as its tip, while legacy and unanswerable SHA rows carry none."""

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
        """A real receipt minted with HEAD at the reviewed tip (the
        SUITE seam — a custom argv records no interpreter and bind refuses
        it by design), then the approve itself."""
        self.git("checkout", "-q", tip)
        with mock.patch.object(gate, "SUITE", (
                "-c", "import sys; print('Ran 1 test in 0.0s\\n\\nOK', "
                "file=sys.stderr)")):
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
        workflow (a review blocker — the old arms codified the inversion)."""
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
        """A review blocker, miss case: the lane text names no branch, but
        the ref did — the stored identity guards it anyway."""
        tip = self.topic("lane/real-work", self.a, "real1")
        row = self.add(lane="UI cleanup review", ref="lane/real-work")
        self.advance("lane/real-work", "real2")
        out, why = dispatches.mark_verdict(row["id"], tip, "ship it",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("moved under this review", why)

    def test_a_hostile_git_dir_cannot_redirect_the_probe(self):
        """codex-3 blocker 4: ambient GIT_DIR selects another repository for
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
        """codex-3 meld e:1785552432 acceptance fixture, verbatim: a hostile
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


class VerdictListAttestProjectionTest(AttestReducerMatrixTest):
    """#135: durable verdict/attest truth reaches both dispatch list surfaces."""

    def test_mismatch_is_visible_in_text_and_json_without_mutating_sidecar(self):  # noqa: VACUOUS_ASSERTION — fixture sidecar bytes are asserted non-empty, then exact text + typed JSON state prove the mismatch reached both surfaces
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, binding="f" * 32)])
        with open(dispatches.attest_path(), "rb") as f:
            before = f.read()
        self.assertTrue(before, "mismatch fixture wrote no attest sidecar")
        rc, text, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("verdict polarity: dispatch store", text)
        self.assertIn("attestation: attest sidecar", text)
        self.assertIn("attest: UNVERIFIABLE", text)
        rc, raw, err = run(dispatches.cmd_dispatch, ["list", "--json"])
        self.assertEqual((rc, err), (0, ""))
        listed = {r["id"]: r for r in json.loads(raw)}[row["id"]]
        self.assertEqual(listed["polarity"], row["polarity"])
        self.assertEqual(listed["polarity_source"], "dispatch-store")
        self.assertEqual(listed["attest_state"], "unverifiable")
        self.assertEqual(listed["attest_source"], "attest-sidecar")
        self.assertEqual(listed["attest_detail"],
                         "attest intent binding mismatch")
        with open(dispatches.attest_path(), "rb") as f:
            self.assertEqual(f.read(), before)

    def test_a_whole_list_reads_the_attest_sidecar_once(self):
        first = self._verdicted()
        second = self._verdicted()
        real = dispatches._attest_rows
        with mock.patch.object(dispatches, "_attest_rows", wraps=real) as read:
            rows = dispatches.with_verdict_projections([first, second])
        self.assertEqual(read.call_count, 1)
        self.assertEqual([r["attest_state"] for r in rows], ["doubt", "doubt"])


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
    """codex-2's condition for the RECOMPUTE ruling, proven against the real
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


class RebindSeesTheContextWallTest(DispatchBase):
    """Rebind's evidence gate had ONE surface — proxywatch — which measures the
    PROXY. A seat at 100% of its context window has a healthy proxy and cannot
    take a turn, so every rebind off it was refused and --force was the only
    road. These arms pin the second surface and, more importantly, pin the
    NARROWNESS of it: a stale or masked context reading must NOT open the gate.
    """

    def _proxy_healthy(self, recipient):
        from helm import proxywatch
        row = {"seat": recipient, "config_ok": True, "drift": [],
               "alerted_at": None, "transcript_age_s": 0,
               "hang_candidate": False, "probe": "healthy",
               "probe_detail": None, "probe_ms": 1, "log": "ok",
               "log_detail": None}
        orig = proxywatch.health
        proxywatch.health = (
            lambda seats=None, include_upstream=True, prior_state=None:
            {"seats": [row]})
        self.addCleanup(setattr, proxywatch, "health", orig)

    def _proxy_blind(self):
        """proxywatch itself cannot be read — the case the OLD code returned
        early on, which structurally prevented any second measurement."""
        from helm import proxywatch
        def boom(seats=None, include_upstream=True, prior_state=None):
            raise OSError("planted: health unreadable")
        orig = proxywatch.health
        proxywatch.health = boom
        self.addCleanup(setattr, proxywatch, "health", orig)

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
