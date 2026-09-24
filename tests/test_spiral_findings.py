#!/usr/bin/env python3
"""Three rounds about ONE defect and three rounds about THREE read alike.

The spiral rung counts distinct reviewed tips and, at three, prescribes a
MELD — converge every open finding in one live exchange. Measured on the
`the-quota-page-puts-what-he-uses-on-top` chain, six rounds read 1 finding,
CLEAN, CLEAN, 4, 2, 1; the guard said "meld" twice and at each firing ZERO
findings stood open, because every round found a defect the previous round's
arms were structurally unable to see. A meld converges open findings and
there were none; the lane was UNDER-ARMED and was being armed.

So these arms ask the question the round count could not: does this round
accuse a file an earlier round already accused? The chain-level arms run the
REAL writer, the REAL fold and the REAL stop rung over a temp home, because
the finding key is derived from what a reviewer actually wrote through
`helm dispatch verdict` — a key that only ever survives a hand-built fixture
row is a key about nothing.
"""
import contextlib
import io
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches as D, eventledger  # noqa: E402
from helm import seats_stop_signals as signals  # noqa: E402
from helm import spiral_findings as SF  # noqa: E402

SEAT = "seat-a"
PEER = "seat-b"

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_NAME",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_ROOM", "HELM_SCRATCH_GC",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_SPIRAL",
            "HELM_STOP_GUARD_WHISPER", "HELM_STOP_GUARD_WIRING",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


class FindingKeyTest(unittest.TestCase):
    """What a round ACCUSES, read out of what the reviewer already wrote."""

    def test_a_full_path_and_a_bare_basename_are_the_same_file(self):
        """The one direction this key may not fail in.

        A reviewer writes `helm/web_quota.py` in round one and
        `web_quota.py:582` in round two. As plain strings those are disjoint,
        so a RE-RAISED finding would read as a new defect and the meld the
        chain needs would be withdrawn."""
        first = SF._paths_in("helm/web_quota.py post-write re-read unguarded")
        later = SF._paths_in("web_quota.py:582 still unguarded")
        self.assertTrue(first & later, (sorted(first), sorted(later)))
        # THE CONTROL: a genuinely different file still reads as different.
        other = SF._paths_in("helm/accounts.py drops the subscription")
        self.assertTrue(other, "the control read no path at all")
        self.assertFalse(first & other, (sorted(first), sorted(other)))

    def test_a_dotted_attribute_reference_is_not_a_file(self):
        """`vcs.probe` and `helm.seat` are how reviewers name CODE, and two
        rounds that merely discussed one module are not one finding."""
        self.assertEqual(SF._paths_in("vcs.probe and helm.seat disagree"),
                         set())
        # THE CONTROL: the same sentence naming a real file does key.
        self.assertIn("helm/vcs.py",
                      SF._paths_in("vcs.probe in helm/vcs.py disagrees"))

    def test_a_truncated_dotted_name_never_becomes_a_key(self):
        """`tests.test_web_accounts` matched as `tests.test` before the token
        carried a trailing boundary — a string naming no file that two
        unrelated rounds would both have produced."""
        self.assertEqual(SF._paths_in("tests.test_web_accounts imports it"),
                         set())
        self.assertIn("tests/test_web_accounts.py",
                      SF._paths_in("tests/test_web_accounts.py asserts a stub"))

    def test_a_version_number_is_not_a_file(self):
        self.assertEqual(SF._paths_in("0.2 of 2.34% e.g. i.e. vs. 1.0"), set())
        self.assertIn("a.py", SF._paths_in("0.2 of 2.34%, see a.py"))

    def test_a_fan_out_at_one_tip_unions_into_one_round_key(self):
        """Two families reviewing ONE tip is one round by this tree's own law,
        and two reviewers naming two files found two things."""
        key = SF.finding_key([
            {"verdict_ref": "helm/a.py is wrong"},
            {"worse_than_main_paths": ["helm/b.py"]}])
        self.assertTrue(key, "neither row contributed a path")
        self.assertEqual(key, frozenset({"helm/a.py", "a.py",
                                         "helm/b.py", "b.py"}))

    def test_the_first_keyed_round_is_new_and_a_repeat_is_spiral(self):
        kinds = SF.classify([frozenset({"a.py"}), frozenset({"b.py"}),
                             frozenset({"a.py"})])
        self.assertEqual(kinds, [SF.NEW_DEFECT, SF.NEW_DEFECT, SF.SPIRAL])

    def test_a_keyless_round_counts_toward_neither(self):
        """Never folded into SPIRAL: a round whose finding identity could not
        be derived is not evidence that it re-raised anything. A CLEAN round
        lands here by construction — it accuses no file."""
        kinds = SF.classify([frozenset({"a.py"}), frozenset(),
                             frozenset({"a.py"})])
        self.assertEqual(kinds, [SF.NEW_DEFECT, SF.UNKNOWN, SF.SPIRAL])


class SpiralKeyChainTest(unittest.TestCase):
    """The real writer, the real fold and the real stop rung, over a temp home."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-spiralkey-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"
        os.environ["HELM_STOP_GUARD_WIRING"] = "0"
        os.makedirs(os.path.dirname(D.ledger_path()), exist_ok=True)
        # AUTHOR BINDING AND OUTWARD NOTIFICATION ARE NOT THIS CONTRACT, and
        # leaving them live made the verdict door refuse for a reason about
        # session runtime rather than about a finding key. The parser, the
        # writer, the ledger replay and the whole stop rung stay real.
        writer = D.mark_verdict

        def write(*args, **kwargs):
            kwargs["bind_author"] = False
            return writer(*args, **kwargs)

        for name, value in (("mark_verdict", write),
                            ("_announce_verdict", lambda *a: "isolated"),
                            ("_reconcile_announce", lambda *a: "isolated"),
                            ("_verdict_author_nudge", lambda *a: None)):
            patch = mock.patch.object(D, name, side_effect=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.repo = os.path.join(self.tmp, "repo", ".git")
        self.chain = None
        self.rows = []

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def observe(self, path=None):
        """One round: a dispatch the seat sent, then a FIX verdict on it.

        `deadline_s` is not decoration — `_valid_identity` throws a row
        without it away, so a fixture that omits it proves nothing about the
        real reader. `path` None plants a round nobody has decided yet, which
        is the KEYLESS case and the shape a still-open round really has."""
        tip = os.urandom(20).hex()
        rid = os.urandom(16).hex()
        self.chain = self.chain or rid
        row = {"v": 3, "seq": 0, "status": "open", "tip": tip,
               "event": "dispatch", "id": rid, "kind": "review",
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(time.time() - 3600
                                               + len(self.rows) * 60)),
               "sender": SEAT, "recipient": PEER, "lane": "one-lane",
               "deadline_s": 2700, "chain_root": self.chain,
               "repo_id": self.repo}
        if self.rows:
            row["supersedes"] = self.rows[-1]
        self.assertTrue(eventledger.append(D.ledger_path(), row))
        self.rows.append(rid)
        if path is not None:
            argv = ["verdict", rid, tip, "--fix", "--measured",
                    "--worse-than-main", path,
                    "--no-patch-because", "a design finding for a meld",
                    "Reviewer observation naming %s." % path]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = D.cmd_dispatch(argv)
            # THE DOOR'S OWN WORDS, or a refusal reads as a bare 1 != 0 and
            # names nothing about why the round was never recorded.
            self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
            folded, err = D.snapshot()
            self.assertIsNone(err)
            self.assertEqual(folded[rid]["status"], "verdict")
        return rid

    def read(self, session):
        info, err = D.review_spiral(SEAT)
        self.assertIsNone(err)
        self.assertIsNotNone(info, "the rung went silent")
        block, warn = signals._spiral_gate(session, "main", SEAT)
        return info, block, warn

    def test_three_rounds_re_raising_one_path_fire_meld(self):
        for _ in range(3):
            self.observe("helm/dispatches.py")
        info, block, warn = self.read("meld")
        self.assertEqual(info["rounds"], 3)
        self.assertEqual(info["prescription"], "MELD")
        self.assertIsNotNone(block, "a real spiral must still be walled")
        self.assertIn("review spiral", block)
        self.assertNotIn(SF.UNDER_ARMED, block)

    def test_three_disjoint_path_sets_fire_under_armed_and_do_not_block(self):
        """Every round found a defect the previous arms could not see. There
        is nothing open to converge, so the meld prescription is withdrawn —
        and the seat is not walled for the behaviour a review exists to
        produce."""
        for path in ("helm/a.py", "helm/b.py", "helm/c.py"):
            self.observe(path)
        info, block, warn = self.read("underarmed")
        self.assertEqual(info["rounds"], 3)
        self.assertEqual(info["prescription"], SF.UNDER_ARMED)
        self.assertIsNone(block)
        self.assertIn(SF.UNDER_ARMED, warn)
        self.assertIn("3 disjoint finding path sets", warn)
        self.assertIn("ask the author for the generating cause", warn)

    def test_two_rounds_fire_nothing(self):
        """Two disjoint rounds are the ordinary shape of a review, not a
        diagnosis — and the third is the control that the arm above is not
        passing for some reason of its own."""
        for path in ("helm/a.py", "helm/b.py"):
            self.observe(path)
        info, block, warn = self.read("two")
        self.assertEqual(info["rounds"], 2)
        self.assertEqual(info["prescription"], "MELD")
        self.assertIsNone(block)
        self.assertNotIn(SF.UNDER_ARMED, warn)
        self.observe("helm/c.py")
        self.assertEqual(self.read("three")[0]["prescription"], SF.UNDER_ARMED)

    def test_a_keyless_round_is_unknown_and_the_render_says_so(self):
        """An undecided round yields no finding identity. It counts toward
        neither bucket, so two keyed rounds cannot reach UNDER-ARMED — and
        the block that stands instead says which rounds it could not read."""
        self.observe("helm/a.py")
        self.observe(None)
        self.observe("helm/b.py")
        info, block, warn = self.read("keyless")
        self.assertEqual(info["prescription"], "MELD")
        self.assertIn("1 round(s) keyless", info["finding_evidence"])
        self.assertIn("finding identity UNKNOWN", block)


if __name__ == "__main__":
    unittest.main()
