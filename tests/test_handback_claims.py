#!/usr/bin/env python3
"""The hand-back claim check (task/3540).

A builder hands work back with a review row, and its brief carries claims:
"fab: Ran 52 tests in 1.102s OK", "gate:<id> whole-suite OK Ran 26377",
"LOG: host:~/fab/logs/<job>.log", "tip <sha>". Until this check they were
prose, and the laundered-gate case read exactly like a truthful one: a
receipt minted on ANOTHER lane's tree, cited as this lane's proof.

WHAT THESE ARMS PIN:
  P  the parser finds each claim kind, and pairs a Ran count with the gate
     token in its own clause;
  C  every claim binds to a gate receipt for the row's OWN tree or reads
     UNBOUND with the reason named: a truthful hand-back is all CHECKED, a
     borrowed receipt is UNBOUND and named, a Ran count its receipt does not
     record is UNBOUND, and testimony nothing binds (a bare Ran line, a LOG
     path) is never CHECKED;
  A  a receipt vouches only when the land path's own row checks admit it
     (a custom command's echoed count, a moved or dirty worktree, OK over a
     nonzero exit and a nameless host all read UNBOUND), a whole-suite claim
     ("whole suite" or "full suite") only on a whole-suite kind, every
     CHECKED reason names the kind, and a claim that says FAILED never binds;
  S`helm dispatch triage <id>` and `helm lr show <id>` print one line per
     claim on a review row, and nothing on a build row; beside a cancelled
     round's carried reads (task/3081) they are the shown row's and print
     above the FROM header.
Each UNBOUND arm has a CHECKED control on the same fixture, so a red arm is
not a blind one.
"""
import json
import os
import unittest

from helm import dispatches, gate, gateauthority, handback_claims, landreq, pk
from tests import test_dispatches as td
from tests._tmphome import pin_live_seats


def setUpModule():
    pin_live_seats()


def _receipt(head, tree, ran, suite=True, status="OK", dirty=False,
             repo="/lanes/room", custom=False, **over):
    """One receipt whose content id recomputes, in the shape `helm gate run`
    mints: the post-run bracket, the host, and the kind's own argv. A whole
    suite is v4 on the frozen serial discovery argv, a focused run is v6 with
    its scope, and `custom` is `helm gate run -- <cmd>`: no interpreter helm
    chose, the command's own argv, suite false. `over` sets any field."""
    exe = "/usr/bin/python3"
    row = {"v": 6 if not suite and not custom else 4, "event": "gate",
           "ts": pk.now_ts(), "repo_id": repo,
           "head": head, "tree": tree, "dirty": dirty,
           "head_after": head, "tree_after": tree, "dirty_after": False,
           "interpreter": None if custom else {
               "name": "cpython", "version": "3.14.6", "language": "3.14.6",
               "executable": exe},
           "host": {"node": "testbox", "system": "Linux", "release": "1",
                    "id": "ab" * 8},
           "argv": ([exe] + list(gateauthority.SERIAL_ARGV) if suite
                    else [exe, "-m", "unittest", "tests.test_x"]),
           "suite": suite, "label": None,
           "status": status, "ran": ran, "skipped": 0, "detail": "",
           "rc": 0 if status == "OK" else 1, "failures": [],
           "failures_unreadable": False, "base_check": None}
    if row["v"] == 6:
        row["focus"] = {"selected": ["tests.test_x"], "universe": 900}
    row.update(over)
    row["id"] = gate._receipt_id(row)
    path = gate.receipts_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


class ParseTest(unittest.TestCase):
    """P. The claim grammar, on the shapes hand-back briefs actually use."""

    BRIEF = ("Built the lane.\n"
             "fab: Ran 52 tests in 1.102s OK (tests.test_actsteer)\n"
             "gate:5cfcdde75393dc28 whole-suite OK Ran 26377\n"
             "LOG: build-1:~/fab/logs/job-17.log\n"
             "tip " + "a" * 40 + "\n"
             "a reviewer may pass --patch-tip " + "b" * 40 + "\n")

    def test_each_claim_kind_is_found(self):
        from helm import handback_claims
        claims = handback_claims.parse(self.BRIEF)
        kinds = [c["kind"] for c in claims]
        self.assertEqual(sorted(kinds), ["gate", "log", "ran", "tip"], claims)
        by = {c["kind"]: c for c in claims}
        self.assertEqual(by["gate"]["receipt"], "5cfcdde75393dc28")
        self.assertEqual(by["gate"]["ran"], 26377,
                         "the Ran count in the gate token's clause is its claim")
        self.assertTrue(by["gate"]["whole"])
        self.assertEqual(by["ran"]["ran"], 52)
        self.assertEqual(by["log"]["log"], "build-1:~/fab/logs/job-17.log")
        self.assertEqual(by["tip"]["tip"], "a" * 40,
                         "`--patch-tip` names a cure, not the hand-back's tip")

    def test_a_comma_grouped_count_reads_whole(self):
        """"Ran 1,234 tests" claims 1234, never 1."""
        from helm import handback_claims
        self.assertEqual([c["ran"] for c in
                          handback_claims.parse("Ran 1,234 tests in 9s")],
                         [1234])

    def test_a_brief_with_no_claim_parses_to_none(self):
        from helm import handback_claims
        self.assertEqual(handback_claims.parse("please read this lane"), [])
        self.assertEqual(handback_claims.parse(None), [])


class ClaimBase(td.DispatchBase):
    """A lane tip (`side`), another lane's tip (`c`), and receipts on each."""

    def setUp(self):
        super().setUp()
        self.side_tree = self.git("rev-parse", self.side + "^{tree}")
        self.c_tree = self.git("rev-parse", self.c + "^{tree}")

    def check(self, brief):
        from helm import handback_claims
        return handback_claims.check(brief, self.side, repo=self.repo)

    def states(self, brief):
        return [(c["kind"], c["state"]) for c in self.check(brief)]


class HoldReceiptTest(unittest.TestCase):
    """The fab receipt a source-clean hold carries (task/4103), on the job
    names fab writes: `<branch>-<git rev-parse --short>-<epoch>-<nonce>-<pid>`.
    git sizes the short sha to the repository's object count, so a smaller
    repository's run on exactly the held tip names fewer than 11 hex chars
    of it, and that LOG is this tip's receipt, never another tip's."""

    TIP = "148cd929b0e1f2a3b4c5d6e7f8091a2b3c4d5e6f"
    OTHER = "9160a632aa0e1f2a3b4c5d6e7f8091a2b3c4d5e6"

    def log(self, short):
        return "LOG node-a:~/fab/logs/lane-x-%s-1790920203-44782e44-2454.log" \
            % short

    def test_a_short_sha_of_the_tip_is_its_receipt(self):
        for n in (7, 9, 11, 12):
            receipt, foreign = handback_claims.hold_receipt(
                "read clean, " + self.log(self.TIP[:n]), self.TIP)
            self.assertEqual((bool(receipt), foreign), (True, []), n)
        # beside a Ran line, the same LOG is no longer read as a foreign run
        receipt, foreign = handback_claims.hold_receipt(
            "fab Ran 63 tests OK, " + self.log(self.TIP[:9]), self.TIP)
        self.assertTrue(receipt)
        self.assertEqual(foreign, [])

    def test_a_short_sha_of_another_tip_is_foreign(self):  # noqa: VACUOUS_ASSERTION — the unconditional first assertion is the positive control: the same LOG shape on the held tip is its receipt
        receipt, _foreign = handback_claims.hold_receipt(
            "fab Ran 63 tests OK, " + self.log(self.TIP[:9]), self.TIP)
        self.assertEqual(receipt, "LOG " + self.log(self.TIP[:9])[4:])
        for n in (7, 9, 11):
            receipt, foreign = handback_claims.hold_receipt(
                "fab Ran 63 tests OK, " + self.log(self.OTHER[:n]), self.TIP)
            self.assertIsNone(receipt, n)
            self.assertEqual(len(foreign), 1, n)

    def test_a_hex_part_shorter_than_a_short_sha_names_no_tip(self):
        receipt, foreign = handback_claims.hold_receipt(
            "read clean, " + self.log(self.TIP[:6]), self.TIP)
        self.assertIsNone(receipt)
        self.assertEqual(len(foreign), 1)

    def test_a_zero_or_failed_count_is_no_receipt(self):
        for text in ("fab Ran 0 tests in 0.0s OK",
                     "fab Ran 63 tests in 4.2s FAILED (failures=1)",
                     "fab Ran 63 tests in 4.2s\n\nFAILED (errors=2)"):
            self.assertEqual(handback_claims.hold_receipt(text, self.TIP),
                             (None, []), text)
        self.assertEqual(handback_claims.hold_receipt(
            "fab Ran 63 tests in 4.2s\n\nOK", self.TIP), ("Ran 63", []))


class ClaimBindingTest(ClaimBase):
    """C. Every claim binds to a receipt for the row's own tree, or says why
    it does not."""

    def test_a_truthful_hand_back_is_all_CHECKED(self):
        whole = _receipt(self.side, self.side_tree, 26377)
        focused = _receipt(self.side, self.side_tree, 52, suite=False)
        brief = ("fab: Ran 52 tests in 1.102s OK (tests.test_x)\n"
                 "gate:%s whole-suite OK Ran 26377\n"
                 "tip %s" % (whole["id"], self.side))
        got = self.check(brief)
        self.assertEqual([c["state"] for c in got], ["CHECKED"] * 3, got)
        ran = [c for c in got if c["kind"] == "ran"][0]
        self.assertIn(focused["id"], ran["why"],
                      "a bare Ran line CHECKs only by naming the receipt that "
                      "records its count on this tree")

    def test_a_receipt_on_the_same_TREE_under_another_head_is_CHECKED(self):
        """The lane's own TREE is the subject, not its sha: a fab snapshot or
        a gate-then-commit run carries another head over identical bytes."""
        twin = self.git("commit-tree", self.side_tree, "-p", self.side,
                        "-m", "same bytes")
        receipt = _receipt(twin, self.side_tree, 26377)
        self.assertEqual(self.states("gate:%s OK Ran 26377" % receipt["id"]),
                         [("gate", "CHECKED")])

    def test_a_BORROWED_receipt_is_UNBOUND_and_named(self):
        """THE LAUNDERED-GATE CASE: a green receipt minted on another lane's
        tree, cited as this lane's proof. Control: the same count on this
        lane's tree binds."""
        mine = _receipt(self.side, self.side_tree, 26377)
        theirs = _receipt(self.c, self.c_tree, 26377, repo="/lanes/other")
        self.assertEqual(self.states("gate:%s OK Ran 26377" % mine["id"]),
                         [("gate", "CHECKED")])
        got = self.check("gate:%s whole-suite OK Ran 26377" % theirs["id"])
        self.assertEqual([c["state"] for c in got], ["UNBOUND"], got)
        why = got[0]["why"]
        self.assertIn(theirs["id"], why)
        self.assertIn(self.c[:12], why, "the tree it WAS minted on is named")
        self.assertIn("other", why, "the room it ran in is named")
        self.assertIn(self.side[:12], why)

    def test_a_Ran_count_its_receipt_does_not_record_is_UNBOUND(self):
        receipt = _receipt(self.side, self.side_tree, 26377)
        got = self.check("gate:%s whole-suite OK Ran 26400" % receipt["id"])
        self.assertEqual([c["state"] for c in got], ["UNBOUND"], got)
        self.assertIn("26400", got[0]["why"])
        self.assertIn("26377", got[0]["why"])

    def test_a_FOCUSED_receipt_cited_as_a_whole_suite_is_UNBOUND(self):
        receipt = _receipt(self.side, self.side_tree, 52, suite=False)
        self.assertEqual(self.states("gate:%s OK Ran 52" % receipt["id"]),
                         [("gate", "CHECKED")])
        self.assertEqual(
            self.states("gate:%s whole-suite OK Ran 52" % receipt["id"]),
            [("gate", "UNBOUND")])

    def test_a_RED_or_DIRTY_or_UNKNOWN_receipt_is_UNBOUND(self):
        red = _receipt(self.side, self.side_tree, 26377, status="FAILED")
        dirty = _receipt(self.side, self.side_tree, 26377, dirty=True)
        for rid in (red["id"], dirty["id"], "0123456789abcdef"):
            self.assertEqual(self.states("gate:%s OK Ran 26377" % rid),
                             [("gate", "UNBOUND")], rid)

    def test_TESTIMONY_nothing_binds_is_never_CHECKED(self):
        """A bare Ran line with no receipt on this tree recording its count,
        and a LOG path: testimony. It reads UNBOUND, saying so."""
        _receipt(self.c, self.c_tree, 52, suite=False)   # another tree's 52
        got = self.check("fab: Ran 52 tests in 1.1s OK\n"
                         "LOG: build-1:~/fab/logs/job-17.log")
        self.assertEqual([c["state"] for c in got], ["UNBOUND", "UNBOUND"],
                         got)
        for claim in got:
            self.assertIn("testimony", claim["why"])

    def test_a_tip_the_row_does_not_carry_is_UNBOUND(self):
        self.assertEqual(self.states("tip %s" % self.side[:12]),
                         [("tip", "CHECKED")])
        self.assertEqual(self.states("tip %s" % self.c),
                         [("tip", "UNBOUND")])


class ClaimAdmissionTest(ClaimBase):
    """A. A receipt vouches for a claim only when the land path's own row
    checks admit it (`gate.row_refusal`, then OK, an interpreter helm chose,
    a named host), and a whole-suite claim only on a whole-suite KIND. Every
    CHECKED reason names that kind; a claim whose own text says FAILED is
    never CHECKED."""

    ECHO = ["sh", "-c", "echo 'Ran 26377 tests in 1.0s'; echo; echo OK"]

    def test_an_ECHOED_Ran_line_from_a_custom_command_is_never_CHECKED(self):
        """THE FALSE ACCEPT: `helm gate run -- <any cmd>` mints an honest row
        on the lane tip whose count is whatever the command printed. Control:
        the same claims bind once a serial receipt records that count."""
        echo = _receipt(self.side, self.side_tree, 26377, suite=False,
                        custom=True, argv=self.ECHO)
        briefs = ("gate:%s OK Ran 26377" % echo["id"],
                  "full suite green gate:%s Ran 26377" % echo["id"],
                  "Ran 26377 tests in 812s\nOK")
        for brief in briefs:
            got = self.check(brief)
            self.assertEqual([c["state"] for c in got], ["UNBOUND"], got)
            self.assertIn("custom", got[0]["why"], brief)
        _receipt(self.side, self.side_tree, 26377)
        got = self.check(briefs[2])
        self.assertEqual([c["state"] for c in got], ["CHECKED"], got)

    def test_a_receipt_the_land_path_refuses_is_UNBOUND_with_its_refusal(self):
        """The worktree MOVED or went DIRTY after the run, OK over a nonzero
        exit, no host named: `gate.row_refusal` and `gate.bind` refuse each,
        so no claim reads CHECKED on it, cited or bare."""
        cases = (
            (dict(head_after=self.c, tree_after=self.c_tree), "MOVED"),
            (dict(dirty_after=True), "DIRTY"),
            (dict(rc=1), "exited 1"),
            (dict(host=None), "HOST"),
        )
        for over, word in cases:
            bad = _receipt(self.side, self.side_tree, 26377, **over)
            got = self.check("gate:%s OK Ran 26377" % bad["id"])
            self.assertEqual([c["state"] for c in got], ["UNBOUND"], over)
            self.assertIn(word, got[0]["why"], over)
        got = self.check("Ran 26377 tests in 9s\nOK")
        self.assertEqual([c["state"] for c in got], ["UNBOUND"], got)
        good = _receipt(self.side, self.side_tree, 26377)
        self.assertEqual(self.states("gate:%s OK Ran 26377" % good["id"]),
                         [("gate", "CHECKED")])

    def test_every_CHECKED_reason_names_the_receipt_kind(self):
        serial = _receipt(self.side, self.side_tree, 26377)
        focused = _receipt(self.side, self.side_tree, 52, suite=False)
        got = self.check("gate:%s OK Ran 26377\ngate:%s OK Ran 52\n"
                         "Ran 52 tests in 1.1s OK" % (serial["id"],
                                                     focused["id"]))
        self.assertEqual([c["state"] for c in got], ["CHECKED"] * 3, got)
        self.assertIn("serial", got[0]["why"])
        self.assertIn("focused v6", got[1]["why"])
        self.assertIn("focused v6", got[2]["why"])

    def test_a_FOCUSED_receipt_cited_as_a_FULL_suite_is_UNBOUND(self):
        """"full suite" is the whole-suite word too, and so is a heading line
        above the token. Control: the same receipt cited as focused binds."""
        receipt = _receipt(self.side, self.side_tree, 26377, suite=False)
        self.assertEqual(self.states("focused: gate:%s OK Ran 26377"
                                     % receipt["id"]), [("gate", "CHECKED")])
        for brief in ("full suite: gate:%s OK Ran 26377" % receipt["id"],
                      "Whole suite:\ngate:%s OK Ran 26377" % receipt["id"],
                      "full suite green, Ran 26377 tests"):
            got = self.check(brief)
            self.assertEqual([c["state"] for c in got], ["UNBOUND"], brief)
            self.assertIn("focused v6", got[0]["why"], brief)

    def test_a_claim_whose_own_text_says_FAILED_is_never_CHECKED(self):
        """Whatever OK receipt that tree holds. Control: the same count said
        OK binds."""
        receipt = _receipt(self.side, self.side_tree, 26377)
        self.assertEqual(self.states("Ran 26377 tests in 3s\n\nOK"),
                         [("ran", "CHECKED")])
        for brief in ("Ran 26377 tests in 3s\nFAILED (failures=2)",
                      "Ran 26377 tests in 3s\n\nFAILED (failures=2)",
                      "gate:%s FAILED (failures=2) Ran 26377" % receipt["id"]):
            got = self.check(brief)
            self.assertEqual([c["state"] for c in got], ["UNBOUND"], brief)
            self.assertIn("FAILED", got[0]["why"], brief)

    def test_a_comma_grouped_count_binds_its_whole_number(self):
        """"Ran 1,234 tests" is not a claim of Ran 1."""
        _receipt(self.side, self.side_tree, 1)
        self.assertEqual(self.states("Ran 1,234 tests in 9s OK"),
                         [("ran", "UNBOUND")])
        _receipt(self.side, self.side_tree, 1234)
        self.assertEqual(self.states("Ran 1,234 tests in 9s OK"),
                         [("ran", "CHECKED")])


class ClaimSurfaceTest(ClaimBase):
    """S. The lines ride the named-row triage and `lr show`."""

    def send(self, brief, kind="review"):
        from tests._tmphome import dispatch_home
        with dispatch_home(self.repo):
            row, why, _sent = dispatches.send(
                "codex-3", "claims-lane-" + kind, brief, self.side,
                repo=self.repo, sign=False, new_work=True, kind=kind,
                task=self.review_task["id"] if kind == "review" else None)
        self.assertIsNone(why, why)
        return row

    def brief(self):
        mine = _receipt(self.side, self.side_tree, 26377)
        theirs = _receipt(self.c, self.c_tree, 26377, repo="/lanes/other")
        return mine, theirs, ("hand-back\n"
                              "gate:%s whole-suite OK Ran 26377\n"
                              "gate:%s whole-suite OK Ran 26377\n"
                              "tip %s" % (mine["id"], theirs["id"], self.side))

    def test_triage_of_a_named_review_row_prints_each_claim(self):
        mine, theirs, brief = self.brief()
        row = self.send(brief)
        rc, out, err = td.run(dispatches.cmd_dispatch, ["triage", row["id"]])
        self.assertEqual(rc, 0, err)
        lines = [line for line in out.splitlines()
                 if "CHECKED" in line or "UNBOUND" in line]
        self.assertEqual(len(lines), 3, out)
        self.assertTrue(any("CHECKED" in l and mine["id"] in l
                            for l in lines), out)
        self.assertTrue(any("UNBOUND" in l and theirs["id"] in l
                            for l in lines), out)

    def test_lr_show_prints_each_claim(self):
        mine, theirs, brief = self.brief()
        row = self.send(brief)
        rc, out, err = td.run(landreq.cmd_lr, ["show", row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertTrue(any("CHECKED" in l and mine["id"] in l
                            for l in out.splitlines()), out)
        self.assertTrue(any("UNBOUND" in l and theirs["id"] in l
                            for l in out.splitlines()), out)

    def test_claims_are_the_shown_rows_and_print_above_a_carried_read(self):
        """Beside task/3081: a cancelled round's read rides the round that
        continues it under a FROM header, and `lr show` of the cancelled id
        opens that round. The claims printed are the OPENED row's (the
        cancelled brief claims nothing), and none sits under the header."""
        from unittest import mock
        mine, _theirs, brief = self.brief()
        gone = self.send("hand-back, nothing claimed")["id"]
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("codex-3", None)):
            _row, why = dispatches.mark_hold(gone, "read clean; fab Ran 5 tests OK",
                                             source_clean_tip=self.side)
        self.assertIsNone(why, why)
        _row, why = dispatches.mark_cancel(gone, "moved to a new round")
        self.assertIsNone(why, why)
        from tests._tmphome import dispatch_home
        with dispatch_home(self.repo):
            succ, why, _sent = dispatches.send(
                "codex-3", "claims-lane-review", brief, self.side,
                repo=self.repo, sign=False, kind="review", supersedes=gone)
        self.assertIsNone(why, why)
        shown = td.run(landreq.cmd_lr, ["show", gone])
        triaged = td.run(dispatches.cmd_dispatch, ["triage", succ["id"]])
        for rc, out, err in (shown, triaged):
            self.assertEqual(rc, 0, err)
            lines = [line.strip() for line in out.splitlines()]
            head = next((i for i, line in enumerate(lines)
                         if line.startswith("FROM %s (CANCELLED" % gone[:12])),
                        None)
            self.assertIsNotNone(head, out)
            claims = [i for i, line in enumerate(lines)
                      if line.startswith("claim ")]
            self.assertEqual(len(claims), 3, out)
            self.assertLess(max(claims), head, out)
            self.assertTrue(any(mine["id"] in lines[i] and "CHECKED"
                                in lines[i] for i in claims), out)
        self.assertIn("showing %s" % succ["id"][:12], shown[1])

    def test_a_BUILD_row_prints_no_claim_line(self):
        """A build brief is the integrator's instruction, not a hand-back:
        its numbers are not claims about the row's tree."""
        _mine, _theirs, brief = self.brief()
        row = self.send(brief, kind="build")
        rc, out, err = td.run(dispatches.cmd_dispatch, ["triage", row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("claim ", out)


if __name__ == "__main__":
    unittest.main()
