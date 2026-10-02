#!/usr/bin/env python3
"""helm goals — the owner's goals, carried to DONE on criteria he approved.

The owner: "i state a goal yall figure out how to get there and i
click yes on your well thought out acceptance criteria and/or add comment just
like already there". Each class below carries one clause of that loop:

  DoorRefusalTest       every refusal at `helm goal add` has its own arm, each
                        with the ledger unchanged and a positive control
  CaptureTest           a goal is a task row plus a goal record, and its
                        criteria reach him as ONE decision card with one option
  PromoteTest           an owner-asked row is promoted in place, on a
                        live-shaped copy of a real row
  CloseGuardTest        no door but `report` / `supersede` closes a goal; a
                        child closes freely and the goal stays open
  StateWalkTest         the surface-by-state matrix: every derived state, on
                        `helm goal list/show`, `helm task list`, and the card
  ReportTest            DONE needs his yes, every criterion passing, and the
                        why re-run by a seat other than the accountable one
  ReviseTest            a comment is answered on the same card at its next
                        rev, whose refs name the new criteria version
  CustodyTest           the goal's open card answers to its accountable seat,
                        through every change of that seat
  ApprovalBindsRevTest  his yes counts only on the card rev the row carries
  OwnerWordTest         a goal closes as superseded, or a done goal reopens,
                        only on a ref that resolves to a record he authored
  GoalRowPinsTest       a goal row is never unowned and its origin stays owner
  ProjectionTest        the projection matches a fresh fold, and says STALE
                        from the ledgers' tails when it is not
  CycleTest             X of Y done, and the first-wave yield
  RegistryTest          the registries a new module and verb owe

Hermetic: tmp HELM_HOME/HELM_CHAT_DIR/HELM_BOARD, chat transport killed, the
push topic scrubbed, ambient session ids scrubbed. Seats are declared through
tests/_tmphome.declare, so the identity layer resolves them the way it
resolves a live seat.
"""
import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import declare, home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-goals-home-", var="HELM_HOME")
_tmp_home(prefix="helm-test-goals-adopted-", var="HELM_ADOPTED_DIR")
from helm import (chat, cli, eventledger, goals, ownerasks, pk,  # noqa: E402
                  registry, tasks)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_KEYS = ("HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC",
            "HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_OWNER_NAMES",
            "HELM_CHAT_DELIVER", "HELM_BOARD", "HELM_SCRATCH_GC",
            "HELM_CACHE_DIR", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_ACTOR", "HELM_ACTORS", "MELD_ACTORS", "PI_CODING_AGENT",
            "HELM_CLARITY_ADVISE_OFF",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")

ACCOUNTABLE = "builder-1"
OTHER = "builder-2"

WORDS = ("my goals are captured and then always at best partially executed\n"
         "i state a goal yall figure out how to get there")
BODY = ("WORDS:\n" + WORDS + "\n"
        "CRITERIA:\n"
        "-! Every goal reports X of Y done each day :: `helm goal cycle`\n"
        "-  A goal closes only on measured criteria :: `helm task close` "
        "refuses on the live ledger\n")


def criteria(*lines):
    """Parsed criteria from `- text :: proof` lines."""
    _w, got, err = goals.parse_body("\n".join(lines), words=False)
    assert err is None, err
    return got


def lines_naming(text, tid):
    """The lines of `text` that name `tid` as a whole token."""
    return [ln for ln in text.splitlines()
            if (" %s " % tid) in (" %s " % " ".join(ln.split()))]


# HIS WORD POSTDATES THE CARD: a goal closes or reopens only on an owner
# record dated strictly after its criteria card's current revision, and clocks tick in
# whole seconds, so a fixture's owner post is stamped past any card by default.
LATER = "2099-01-01T00:00:00Z"
EARLIER = "2000-01-01T00:00:00Z"

TWO = ("-! Every goal reports X of Y done each day :: `helm goal cycle`",
       "-  A goal closes only on measured criteria :: `helm task close` "
       "refuses on the live ledger")


class GoalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-goals-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["HELM_BOARD"] = os.path.join(self.tmp, "board.json")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": []})
        declare(self, ACCOUNTABLE)
        # A GOAL MUST NAME ITS PROJECT (task/3745), so `goal add` refuses
        # from a cwd no project claims. A placeholder project is registered
        # at its own directory and `run_goal("add", ...)` files from inside
        # it while the cwd resolves to none; every other verb keeps the
        # unscoped cwd these arms were written against.
        self.home_dir = os.path.realpath(os.path.join(self.tmp,
                                                      "goalproj-repo"))
        os.makedirs(self.home_dir)
        pk.write_json(os.path.join(os.path.dirname(tasks.ledger_path()),
                                   "registry.json"),
                      {"version": 1, "projects": {"goalproj": {
                          "name": "goalproj", "path": self.home_dir}}})

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    HOME_ADDS = True

    def run_goal(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        prior = sys.stdin
        sys.stdin = io.StringIO(stdin)
        here = None
        if (self.HOME_ADDS and args[:1] == ("add",)
                and tasks.current_project() is None):
            here = os.getcwd()
            os.chdir(self.home_dir)
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = goals.cmd_goal(list(args))
        finally:
            sys.stdin = prior
            if here is not None:
                os.chdir(here)
        return rc, out.getvalue(), err.getvalue()

    def run_task(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.cmd_task(list(args))
        return rc, out.getvalue(), err.getvalue()

    def add_goal(self, title="Goals reach done on approved criteria",
                 lines=TWO, why="80% of what I ask done in one cycle",
                 words_ref="chat:post-1"):
        row, notes, err = goals.add(title, ACCOUNTABLE, WORDS, words_ref,
                                    why, criteria(*lines))
        self.assertIsNone(err, err)
        self.assertTrue(row["goal"].get("card"), notes)
        return row

    def row(self, tid):
        return tasks.rows()[tid]

    def card(self, cid):
        return ownerasks.decision_rows()[cid]

    def ledger_lines(self):
        p = tasks.ledger_path()
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return f.read().splitlines()

    def yes(self, tid):
        """The owner's Yes through HIS door (the web handler's mint), on the
        rev his page draws: the card's current one, as the web queue sends."""
        cid = self.row(tid)["goal"]["card"]
        got, err = ownerasks.decide(cid, goals.YES_KEY,
                                    by=ownerasks.owner_door("web"),
                                    rev=ownerasks.card_rev(self.card(cid)))
        self.assertIsNone(err, err)
        return got

    def state(self, tid):
        known = tasks.rows()
        row = known[tid]
        card = ownerasks.decision_rows().get(str(row["goal"].get("card")))
        return goals.state(row, card, goals.children_of(known)[tid])["state"]

    def measure(self, tid, key, passed=True, seat=None, value="ok", **kw):
        if seat:
            declare(self, seat)
        row, notes, err = goals.measure(tid, key, passed, value,
                                        kw.pop("how", "helm goal cycle"), **kw)
        if seat:
            declare(self, ACCOUNTABLE)
        return row, notes, err

    def child(self, tid, title):
        row, err = tasks.add(title, ACCOUNTABLE, continues=tid,
                             posture_na="test fixture", force_new=True)
        self.assertIsNone(err, err)
        return row

    def owner_post(self, text, ts=LATER):
        """A chat row the owner posted through his web door: one of his names
        and an owner-rail origin, the two parts `owner_rail` reads. It is
        stamped `ts`, by default after any card a fixture files."""
        with mock.patch.object(pk, "now_ts", return_value=ts):
            return chat.post(text, room="main", who="daria", origin="web")

    def seat_post(self, text, seat=ACCOUNTABLE):
        return chat.post(text, room="main", who=seat)

    def reassign_auth(self, tid, to):
        """The seat-reassign capability `helm seat reassign` mints, moving the
        goal to `to` from a seat measured dead."""
        from helm import seat_reassign, takeover
        before = self.row(tid)
        incumbent = tasks.owner_of(before)
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "no pane")):
            disp, derr = takeover.mint_source_disposition(incumbent)
        self.assertIsNone(derr, derr)
        auth, err = takeover.mint_seat_reassign(
            tid, before, incumbent, to,
            {"kind": "seat-reassign", "from": incumbent, "to": to},
            disposition=disp)
        self.assertIsNone(err, err)
        return auth


class GoalNeedsAProjectTest(GoalBase):
    """`helm goal add` refuses a goal with no project, the way `helm task
    add` does: the cwd's project or --project NAME, and --project none is
    not a way out (task/3745)."""

    HOME_ADDS = False
    GOOD = ("add", "A goal that names its home", "--owner", ACCOUNTABLE,
            "--words-ref", "p1")

    def test_a_cwd_in_no_project_refuses_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the same door filed from the project's checkout is the positive control: rc 0 and the stamped project equals the literal 'goalproj'
        self.assertEqual(tasks.current_project(), None)
        rc, out, err = self.run_goal(*self.GOOD, stdin=BODY)
        self.assertEqual(rc, 2, err)
        self.assertIn("a task must name its project", err)
        self.assertIn("--project NAME", err)
        self.assertIn("goalproj", err)
        # THE DOOR IS NAMED ONCE. The project sentence already starts with
        # "helm goal add:"; a second "helm goal:" in front of it is the
        # double prefix (task/3994).
        self.assertNotIn("helm goal: helm goal", err)
        self.assertTrue(err.startswith("helm goal add:"), err)
        self.assertEqual(err.count("Nothing was filed"), 1)
        self.assertEqual(self.ledger_lines(), [])
        self.assertEqual(ownerasks.decision_rows(), {})
        for spelling in (("--project", "none"), ("--project=NULL",)):
            rc, _out, err = self.run_goal(*self.GOOD + spelling, stdin=BODY)
            self.assertEqual(rc, 2, err)
            self.assertIn("is not a way out", err)
        self.assertEqual(self.ledger_lines(), [])
        os.chdir(self.home_dir)
        rc, out, err = self.run_goal(*self.GOOD, stdin=BODY)
        self.assertEqual(rc, 0, err)
        row = [r for r in tasks.rows().values()
               if r.get("title") == "A goal that names its home"][0]
        self.assertEqual(row["project"], "goalproj")

    def test_the_flag_names_the_project_from_anywhere(self):
        rc, _out, err = self.run_goal(*self.GOOD + ("--project", "goalproj"),
                                      stdin=BODY)
        self.assertEqual(rc, 0, err)
        row = [r for r in tasks.rows().values()
               if r.get("title") == "A goal that names its home"][0]
        self.assertEqual(row["project"], "goalproj")


    def test_an_unknown_project_and_a_broken_registry_name_the_door_once(self):  # noqa: VACUOUS_ASSERTION — each refusal is paired with an empty ledger, so the single door prefix is a refusal that wrote nothing and not a prefix check on a success
        rc, _out, err = self.run_goal(
            *self.GOOD + ("--project", "not-a-project"), stdin=BODY)
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.startswith("helm goal add:"), err)
        self.assertNotIn("helm goal: helm goal", err)
        self.assertIn("not a registered project", err)
        self.assertEqual(err.count("Nothing was filed"), 1)
        self.assertEqual(self.ledger_lines(), [])
        reg = os.path.join(os.path.dirname(tasks.ledger_path()),
                           "registry.json")
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write("{ this is not registry json\n")
        rc, _out, err = self.run_goal(
            *self.GOOD + ("--project", "goalproj"), stdin=BODY)
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.startswith("helm goal add:"), err)
        self.assertNotIn("helm goal: helm goal", err)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(err.count("Nothing was filed"), 1)
        self.assertEqual(self.ledger_lines(), [])

class DoorRefusalTest(GoalBase):
    """Each refusal at the door has its own sentence, and each writes nothing:
    no task row, no card."""

    GOOD = ("add", "A goal the door accepts", "--owner", ACCOUNTABLE,
            "--words-ref", "p1")

    def refuse_then_cure(self, bad, stdin, good=GOOD, cure=BODY):
        """(refused rc, stderr, cured rc, ledger lines before, after, cards
        before, after): the refused add, then the SAME door given what the
        refusal named. The cure writing one row and one card is what makes
        the refusal's unchanged ledger a refusal and not a door that writes
        nothing at all."""
        got, _o, err = self.run_goal(*bad, stdin=stdin)
        lines, cards = self.ledger_lines(), ownerasks.decision_rows()
        cured, _o, cerr = self.run_goal(*good, stdin=cure)
        return (got, err, cured, cerr, lines, self.ledger_lines(), cards,
                ownerasks.decision_rows())

    def test_a_well_formed_goal_passes_the_door(self):
        row = self.add_goal()
        self.assertEqual(row["origin"], "owner")
        self.assertEqual(len(ownerasks.decision_rows()), 1)

    def test_no_owner_refuses(self):
        rc, err, cured, cerr, lines, after, cards, cards_after = \
            self.refuse_then_cure(("add", "A goal", "--words-ref", "p1"),
                                  BODY)
        self.assertEqual((rc, lines, cards), (1, [], {}))
        self.assertIn("ONE accountable seat", err)
        self.assertEqual((cured, len(cards_after)), (0, 1), cerr)
        self.assertGreater(len(after), len(lines))

    def test_no_words_refuses(self):
        rc, err, cured, cerr, lines, after, cards, cards_after = \
            self.refuse_then_cure(self.GOOD, "CRITERIA:\n" + "\n".join(TWO))
        self.assertEqual((rc, lines, cards), (2, [], {}))
        self.assertIn("no WORDS", err)
        self.assertEqual((cured, len(cards_after)), (0, 1), cerr)
        self.assertGreater(len(after), len(lines))

    def test_no_words_ref_refuses(self):
        rc, err, cured, cerr, lines, after, cards, cards_after = \
            self.refuse_then_cure(self.GOOD[:4], BODY)
        self.assertEqual((rc, lines, cards), (1, [], {}))
        self.assertIn("--words-ref", err)
        self.assertIn("testimony", err)
        self.assertEqual((cured, len(cards_after)), (0, 1), cerr)
        self.assertGreater(len(after), len(lines))

    def test_a_criterion_with_no_proof_refuses(self):
        rc, err, cured, cerr, lines, after, cards, cards_after = \
            self.refuse_then_cure(
                self.GOOD, "WORDS:\nhis words\nCRITERIA:\n-! It works\n")
        self.assertEqual((rc, lines, cards), (2, [], {}))
        self.assertIn("no `:: <proof>`", err)
        self.assertEqual((cured, len(cards_after)), (0, 1), cerr)
        self.assertGreater(len(after), len(lines))

    def test_no_why_criterion_refuses_and_two_refuse(self):
        err = goals.check_criteria(criteria(
            "- Every goal reports X of Y done each day :: `helm goal cycle`"))
        self.assertIn("mark the one that tests his WHY", err)
        err = goals.check_criteria(criteria(
            "-! Every goal reports X of Y done each day :: `helm goal cycle`",
            "-! A goal closes only on measured criteria :: a refusal"))
        self.assertIn("mark exactly one", err)
        self.assertIsNone(goals.check_criteria(criteria(*TWO)))

    def test_more_than_seven_criteria_refuses(self):
        lines = ["-! Criterion zero holds :: proof zero"] + [
            "- Criterion %s holds :: proof %s" % (w, w) for w in
            ("one", "two", "three", "four", "five", "six", "seven")]
        err = goals.check_criteria(criteria(*lines))
        self.assertIn("8 criteria", err)
        self.assertIn("checklist", err)
        self.assertIsNone(goals.check_criteria(criteria(*lines[:7])))

    def test_an_unreadable_criterion_refuses_and_a_target_number_passes(self):
        """The clarity die in owner mode refuses a semicolon; the provenance
        rule is skipped, because a criterion's number is the bar he approves,
        not a claim that owes a MEASURED tier."""
        err = goals.check_criteria(criteria(
            "-! The brief is fast; the report is short :: `helm brief`"))
        self.assertIn("not readable for the owner", err)
        self.assertIsNone(goals.check_criteria(criteria(
            "-! The brief prints in 6 seconds :: `time helm brief`")))

    def test_the_owner_is_never_the_accountable_seat(self):
        row, _n, err = goals.add("A goal", "daria", WORDS, "p1", None,
                                 criteria(*TWO))
        self.assertEqual((row, self.ledger_lines()), (None, []))
        self.assertIn("owner's own names", err)
        row, _n, err = goals.add("A goal", ACCOUNTABLE, WORDS, "p1", None,
                                 criteria(*TWO))
        self.assertEqual(row["owner"], ACCOUNTABLE, err)
        self.assertEqual(len(ownerasks.decision_rows()), 1)

    def test_an_unresolved_actor_writes_nothing(self):
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        row, _n, err = goals.add("A goal", ACCOUNTABLE, WORDS, "p1", None,
                                 criteria(*TWO))
        self.assertEqual((row, self.ledger_lines()), (None, []))
        self.assertIn("UNKNOWN", err)
        declare(self, ACCOUNTABLE)
        row, _n, err = goals.add("A goal", ACCOUNTABLE, WORDS, "p1", None,
                                 criteria(*TWO))
        self.assertEqual(row["goal"]["captured_by"], ACCOUNTABLE, err)
        self.assertEqual(len(ownerasks.decision_rows()), 1)

    def test_add_revise_body_rejects_words(self):
        _w, _c, err = goals.parse_body("WORDS:\nnew words\n", words=False)
        self.assertIn("fixed at capture", err)


class CaptureTest(GoalBase):

    def test_a_goal_is_a_task_row_with_a_goal_record(self):
        rc, out, err = self.run_goal(
            "add", "Goals reach done on approved criteria", "--owner",
            ACCOUNTABLE, "--words-ref", "chat:post-1", "--why",
            "80% of what I ask done in one cycle", stdin=BODY)
        self.assertEqual(rc, 0, err)
        tid = out.split()[1]
        row = self.row(tid)
        self.assertEqual((row["origin"], row["owner"], row["priority"],
                          row["status"]), ("owner", ACCOUNTABLE, "P1", "open"))
        self.assertIsNone(row["continues"])
        goal = row["goal"]
        self.assertEqual(goal["words"], WORDS)          # verbatim, multi-line
        self.assertEqual(goal["words_ref"], "chat:post-1")
        self.assertEqual(goal["why"], "80% of what I ask done in one cycle")
        self.assertEqual([c["key"] for c in goal["criteria"]], ["c1", "c2"])
        self.assertEqual([c["why_test"] for c in goal["criteria"]],
                         [True, False])
        self.assertEqual(goal["captured_by"], ACCOUNTABLE)
        self.assertEqual(self.state(tid), "proposed")

    def test_the_criteria_reach_him_as_one_card_with_one_option(self):
        row = self.add_goal()
        cards = ownerasks.decision_rows()
        self.assertEqual(list(cards), [row["goal"]["card"]])
        card = cards[row["goal"]["card"]]
        self.assertEqual(card["asker"], ACCOUNTABLE)
        self.assertEqual(card["refs"], [row["id"], "goal-criteria:v1"])
        self.assertEqual([(o["key"], o["label"], o["recommended"])
                          for o in card["options"]],
                         [("1", "Yes, done means these", True)])
        self.assertIn("Goal %s:" % row["id"], card["title"])
        for piece in (WORDS, "chat:post-1", "80% of what I ask",
                      "Every goal reports X of Y done each day",
                      "proof: `helm goal cycle`", "[tests your why]"):
            self.assertIn(piece, card["context"])
        self.assertLessEqual(len(card["context"]), goals.CARD_CONTEXT_MAX)

    def test_long_words_are_cut_on_the_card_and_the_cut_is_said(self):  # noqa: VACUOUS_ASSERTION — the stored words, the cut notice and the criterion are each asserted PRESENT on the card context
        long_words = "word " * 1100
        row, _n, err = goals.add("Long testimony goal", ACCOUNTABLE,
                                 long_words, "p1", None, criteria(*TWO))
        self.assertEqual(row["goal"]["words"], long_words.strip("\n"), err)
        ctx = self.card(row["goal"]["card"])["context"]
        self.assertLessEqual(len(ctx), goals.CARD_CONTEXT_MAX)
        self.assertIn("characters shown; the whole text is on %s" % row["id"],
                      ctx)
        self.assertIn("Every goal reports X of Y done each day", ctx)

    def test_a_failed_card_leaves_the_row_and_propose_files_it(self):
        with mock.patch("helm.ownerasks.file_decision",
                        return_value=(None, "ledger unwritable")):
            row, notes, err = goals.add("Card failure goal", ACCOUNTABLE,
                                        WORDS, "p1", None, criteria(*TWO))
        self.assertIsNone(err)
        self.assertIsNone(row["goal"]["card"])
        self.assertIn("NOT FILED", notes[0])
        known = tasks.rows()
        st = goals.state(known[row["id"]], None, [])
        self.assertEqual((st["state"], st["flags"]),
                         ("proposed", ["card-not-filed"]))
        rc, out, err = self.run_goal("propose", row["id"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.row(row["id"])["goal"]["card"])
        rc, _o, err = self.run_goal("propose", row["id"])
        self.assertEqual(rc, 1)
        self.assertIn("already carries", err)

    def test_propose_adopts_a_card_already_in_his_queue(self):
        with mock.patch("helm.goals._record_card",
                        side_effect=lambda row, card, path, notes:
                        (row, ["not recorded"])):
            row = goals.add("Orphan card goal", ACCOUNTABLE, WORDS, "p1",
                            None, criteria(*TWO))[0]
        self.assertIsNone(self.row(row["id"])["goal"]["card"])
        orphan = list(ownerasks.decision_rows())
        self.assertEqual(len(orphan), 1)
        new, notes, err = goals.propose(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(new["goal"]["card"], orphan[0])
        self.assertIn("adopted", notes[0])
        self.assertEqual(len(ownerasks.decision_rows()), 1)

    def test_a_card_is_never_recorded_on_a_row_whose_criteria_moved_on(self):
        """The card filed for criteria v1 is being recorded on the row when a
        revise lands first: the row is at v2 with its own card. The retry
        that re-reads the moved row must NOT bind the v1 card, at its rev 1,
        to the v2 row — his Yes on that card would then approve criteria he
        never read. The v2 card stays, and the v1 card is told it needs no
        answer."""
        real = tasks.update
        fired = []

        def revise_first(*a, **kw):
            door = kw.get("goal_door")
            if door is not None and door.act == "card" and not fired:
                fired.append(1)
                with mock.patch.object(tasks, "update", real):
                    row, _n, err = goals.revise(a[0],
                                                criteria(*ReviseTest.THREE))
                self.assertIsNone(err, err)
            return real(*a, **kw)
        with mock.patch.object(tasks, "update", side_effect=revise_first):
            row, notes, err = goals.add("Goals reach done on approved criteria",
                                        ACCOUNTABLE, WORDS, "chat:post-1",
                                        "why", criteria(*TWO))
        self.assertIsNone(err, err)
        self.assertEqual(fired, [1])
        tid = row["id"]
        goal = self.row(tid)["goal"]
        cards = ownerasks.decision_rows()
        self.assertEqual(len(cards), 2)
        card = cards[goal["card"]]
        self.assertEqual((goal["criteria_version"], goal["card_rev"]), (2, 1))
        self.assertIn("goal-criteria:v2", card["refs"])
        self.assertTrue(goals._carries(card, self.row(tid)))
        self.assertEqual(self.state(tid), "proposed")
        stale = [c for c in cards.values() if c["id"] != goal["card"]][0]
        self.assertIn("goal-criteria:v1", stale["refs"])
        self.assertIn("needs no answer",
                      " ".join(c["text"] for c in stale["comments"]))
        self.assertIn("needs no answer", "\n".join(notes))
        # the returned row is the one on the ledger, not the one add judged
        self.assertEqual(row["goal"]["card"], goal["card"])


class PromoteTest(GoalBase):
    """`--from` promotes an owner-asked row IN PLACE: same id, note and
    comments. The fixture is the shape of a real owner P0 row as the live
    ledger stores it: written by older doors, carrying rank provenance, a
    reported session, a project, and a comment with no author."""

    def live_shaped_row(self, **over):
        now = time.time()
        row = {"id": "task/2435", "ts": now - 86400 * 12,
               "last_updated": now - 3600, "continues": None,
               "priority": "P0",
               "title": "Canon injection is scoped by project, so a seat on "
                        "another project reads only its own premises",
               "status": "open", "owner": "builder-3",
               "note": "OWNER P0 2026-09-13 10:28 PDT: canon injection scoped "
                       "by project (verbatim: that makes total sense, clearly "
                       "this was not planned for)",
               "refs": [], "source": "builder-4", "origin": "owner",
               "closed_reason": None, "project": "helm",
               "reported_session": "sess-synthetic-0001",
               "ranked_by": "builder-4", "ranked_at": now - 86000,
               "rank_action": "manual", "ranked_from": "P1",
               "comments": [
                   {"ts": now - 7200, "by": "builder-4",
                    "text": "the lane LANDED and its ACCEPTANCE IS NOT MET"},
                   {"ts": now - 3600, "by": None,
                    "text": "omg get this live!!!"}]}
        row.update(over)
        self.assertTrue(eventledger.append(tasks.ledger_path(), row))
        return row

    def promote(self, tid="task/2435", owner="builder-3", title=None):
        declare(self, owner)
        return goals.add(title, owner, WORDS, "chat:post-2435", None,
                         criteria(*TWO), from_task=tid)

    def test_promote_keeps_the_id_note_comments_and_rank(self):
        before = self.live_shaped_row()
        row, notes, err = self.promote()
        self.assertIsNone(err, err)
        self.assertEqual(row["id"], "task/2435")
        for key in ("title", "note", "comments", "priority", "owner",
                    "project", "ts", "ranked_by"):
            self.assertEqual(row[key], before[key], key)
        self.assertEqual(row["goal"]["words_ref"], "chat:post-2435")
        card = self.card(row["goal"]["card"])
        self.assertEqual(card["asker"], "builder-3")
        self.assertEqual(card["refs"][0], "task/2435")
        rc, out, _e = self.run_task("show", "task/2435")
        self.assertEqual(rc, 0)
        self.assertIn("omg get this live!!!", out)
        self.assertIn("[goal 0/2]", out)

    def test_promote_ranks_an_unranked_row_p1_with_its_actor(self):  # noqa: VACUOUS_ASSERTION — an exact pin of the rank and the ranking actor on the promoted row
        self.live_shaped_row(priority=None)
        row, _n, err = self.promote()
        self.assertEqual((row["priority"], row["ranked_by"]),
                         ("P1", "builder-3"), err)

    def test_promote_refuses_what_is_not_an_open_owner_root(self):  # noqa: VACUOUS_ASSERTION — each refused case is followed, outside the loop, by an accepted promotion whose goal record is asserted PRESENT
        cases = (({"status": "closed", "closed_reason": "landed"}, "CLOSED"),
                 ({"origin": "agent"}, "agent work"),
                 ({"continues": "task/9"}, "story root"))
        tasks.add("A parent row for the fixture", "builder-4", tid="task/9",
                  posture_na="test fixture", force_new=True)
        for n, (over, words) in enumerate(cases):
            tid = "task/%d" % (500 + n)
            self.live_shaped_row(id=tid, **over)
            row, _n, err = self.promote(tid=tid)
            self.assertIsNone(row, over)
            self.assertIn(words, err, over)
            self.assertNotIn("goal", self.row(tid))
        self.live_shaped_row(id="task/600")
        row, _n, err = self.promote(tid="task/600")
        self.assertEqual(row["goal"]["words_ref"], "chat:post-2435", err)
        again, _n, err = self.promote(tid="task/600")
        self.assertIsNone(again)
        self.assertIn("already a goal", err)

    def test_promote_by_another_seat_meets_the_incumbent_guard(self):  # noqa: VACUOUS_ASSERTION — the refused promotion is followed by the incumbent's accepted one, whose goal record is asserted PRESENT
        self.live_shaped_row()
        row, _n, err = self.promote(owner=OTHER)
        self.assertIsNone(row)
        self.assertIn("held by builder-3", err)
        self.assertNotIn("goal", self.row("task/2435"))
        row, _n, err = self.promote()           # the incumbent may
        self.assertEqual(row["goal"]["words_ref"], "chat:post-2435", err)


class CloseGuardTest(GoalBase):

    def test_task_close_on_a_goal_is_refused_naming_the_goal_verbs(self):
        tid = self.add_goal()["id"]
        rc, _o, err = self.run_task("close", tid, "LANDED as LAND 999")
        self.assertEqual(rc, 2)
        for piece in ("is a GOAL", "helm goal measure %s" % tid,
                      "helm goal report %s" % tid, "never the goal"):
            self.assertIn(piece, err)
        self.assertEqual(self.row(tid)["status"], "open")

    def test_every_api_closer_is_refused(self):
        tid = self.add_goal()["id"]
        got, err = tasks.close(tid, "landed")
        self.assertIsNone(got)
        self.assertIn("is a GOAL", err)
        got, err = tasks.update(tid, status="closed", closed_reason="landed")
        self.assertIsNone(got)
        self.assertIn("is a GOAL", err)
        self.assertEqual(self.row(tid)["status"], "open")
        # the positive control: an ordinary row closes through the same door
        plain, _e = tasks.add("An ordinary engineering row", ACCOUNTABLE,
                              posture_na="test fixture", force_new=True)
        got, err = tasks.close(plain["id"], "done")
        self.assertIsNone(err)
        self.assertEqual(got["status"], "closed")

    def test_a_child_closes_and_the_goal_stays_open(self):
        tid = self.add_goal()["id"]
        kid = self.child(tid, "Build the cycle report")
        got, err = tasks.close(kid["id"], "LANDED as LAND 999")
        self.assertIsNone(err)
        self.assertEqual(got["status"], "closed")
        self.assertEqual(self.row(tid)["status"], "open")

    def test_the_goal_record_is_written_only_through_the_door(self):  # noqa: VACUOUS_ASSERTION — every refused write ends with the same write holding the door landing, its edited field asserted PRESENT
        tid = self.add_goal()["id"]
        goal = dict(self.row(tid)["goal"], report_ref="post-forged")
        got, err = tasks.update(tid, goal=goal)
        self.assertIsNone(got)
        self.assertIn("written only by the `helm goal` verbs", err)
        got, err = tasks.update(tid, goal=goal, status="closed",
                                closed_reason="forged")
        self.assertIsNone(got)
        self.assertIsNone(self.row(tid)["goal"]["report_ref"])
        with self.assertRaises(TypeError):
            goals.GoalDoor("report")
        got, err = tasks.add("Goal born outside the door", ACCOUNTABLE,
                             origin="owner", goal=goal, force_new=True)
        self.assertIsNone(got)
        self.assertIn("only by `helm goal add`", err)
        # the positive control: the same write, holding the door, lands
        got, err = tasks.update(tid, goal=dict(goal, report_ref=None,
                                               why="edited by a goal verb"),
                                goal_door=goals._door("measure"))
        self.assertEqual(got["goal"]["why"], "edited by a goal verb", err)

    def test_even_the_door_cannot_close_a_goal_without_a_terminal_fact(self):
        tid = self.add_goal()["id"]
        got, err = tasks.update(tid, status="closed", closed_reason="x",
                                goal_door=goals._door("report"))
        self.assertIsNone(got)
        self.assertIn("is a GOAL", err)
        got, err = tasks.update(tid, status="closed", closed_reason="x",
                                goal_door=goals._door("measure"))
        self.assertIsNone(got)
        self.assertEqual(self.row(tid)["status"], "open")

    def test_a_goal_is_a_story_root(self):
        tid = self.add_goal()["id"]
        other = self.add_goal(title="A second owner goal about the brief")
        got, err = tasks.update(tid, continues=other["id"])
        self.assertIsNone(got)
        self.assertIn("story root", err)


class StateWalkTest(GoalBase):
    """The surface-by-state matrix, walked: each derived state, as `helm goal
    list`, `helm goal show`, `helm task list` and the card show it."""

    def list_line(self, tid, *flags):
        rc, out, err = self.run_goal("list", *flags)
        self.assertEqual(rc, 0, err)
        return lines_naming(out, tid)

    def assertSurfaces(self, tid, state, badge):
        self.assertEqual(self.state(tid), state)
        self.assertIn(state, self.list_line(tid, "--all")[0])
        rc, out, _e = self.run_goal("show", tid)
        self.assertEqual(rc, 0)
        self.assertIn("state        %s" % state, out)
        rc, out, _e = self.run_task("list", "--all")
        self.assertIn(badge, lines_naming(out, tid)[0])

    def test_proposed_through_done(self):
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.assertSurfaces(tid, "proposed", "[goal 0/2]")
        # commented: he comments on the card, not a second card
        ownerasks.comment_decision(first, "add a latency bar",
                                   by=ownerasks.owner_door("web"))
        self.assertSurfaces(tid, "commented", "[goal 0/2]")
        self.assertIn("helm goal revise %s" % tid, self.list_line(tid)[0])
        self.assertIn("he said      add a latency bar",
                      self.run_goal("show", tid)[1])
        # revised: the accountable seat answers with criteria v2, on THE SAME
        # card at its next rev (his card is open), never a second card
        rc, out, err = self.run_goal(
            "revise", tid, stdin="CRITERIA:\n" + "\n".join(TWO) + "\n"
            "- The brief prints in 6 seconds :: `time helm brief`\n")
        self.assertEqual(rc, 0, err)
        goal = self.row(tid)["goal"]
        self.assertEqual((goal["criteria_version"], goal["card"],
                          goal["cards"]), (2, first, []))
        self.assertEqual([c["key"] for c in goal["criteria"]],
                         ["c1", "c2", "c3"])
        self.assertEqual(list(ownerasks.decision_rows()), [first])
        card = self.card(first)
        self.assertEqual((card["status"], card["rev"]), ("open", 2))
        self.assertIn("criteria v2", card["context"])
        self.assertIn("The brief prints in 6 seconds", card["context"])
        self.assertEqual([r["rev"] for r in card["revisions"]], [1])
        self.assertIn("criteria v1", card["revisions"][0]["context"])
        self.assertEqual(card["answering_comment_ts"],
                         card["comments"][0]["ts"])
        self.assertSurfaces(tid, "revised", "[goal 0/3]")
        self.assertIn("rev 2", self.run_goal("show", tid)[1])
        # his page drawn before the revision cannot say yes to it
        got, err = ownerasks.decide(first, goals.YES_KEY, rev=1,
                                    by=ownerasks.owner_door("web"))
        self.assertIsNone(got)
        self.assertIsInstance(err, ownerasks.StaleRev)
        self.assertEqual(err.rev, 2)
        self.assertEqual(self.state(tid), "revised")
        # criteria-approved: his Yes on the rev the card now shows
        self.yes(tid)
        self.assertSurfaces(tid, "criteria-approved", "[goal 0/3]")
        # in-progress: work hangs off the goal
        self.child(tid, "Write the cycle report")
        self.assertSurfaces(tid, "in-progress", "[goal 0/3]")
        # criteria-measured: every criterion passes, the why by another seat
        for key in ("c1", "c2", "c3"):
            self.assertIsNone(self.measure(tid, key)[2])
        self.assertIsNone(self.measure(tid, "c1", seat=OTHER)[2])
        self.assertSurfaces(tid, "criteria-measured", "[goal 3/3]")
        self.assertIn("report owed", self.list_line(tid)[0])
        self.assertEqual(self.list_line(tid, "--done"), [])
        # done: the report is the only closer
        rc, out, err = self.run_goal("report", tid, "chat:post-done")
        self.assertEqual(rc, 0, err)
        self.assertSurfaces(tid, "done", "[done 3/3]")
        self.assertIn("reported in post chat:post-done",
                      self.list_line(tid, "--done")[0])
        self.assertEqual(self.list_line(tid), [])        # open list drops it

    def test_superseded(self):  # noqa: VACUOUS_ASSERTION — every observable is asserted PRESENT: the closed status, the reason naming the post and the successor, the surface lines, and the comment on the card
        tid = self.add_goal()["id"]
        other = self.add_goal(title="A second owner goal about the brief")
        post = self.owner_post("the brief goal replaces this one")
        rc, _o, err = self.run_goal("supersede", tid, "--owner-ref",
                                    post["id"], "--by", other["id"])
        self.assertEqual(rc, 0, err)
        row = self.row(tid)
        self.assertEqual(row["status"], "closed")
        self.assertIn(post["id"], row["closed_reason"])
        self.assertIn(other["id"], row["closed_reason"])
        self.assertSurfaces(tid, "superseded", "[superseded 0/2]")
        card = self.card(row["goal"]["card"])
        self.assertIn("needs no answer", card["comments"][-1]["text"])
        rc, _o, err = self.run_goal("supersede", tid, "--owner-ref", "p")
        self.assertEqual(rc, 1)
        self.assertIn("superseded", err)

    def test_a_comment_on_an_earlier_revision_is_answered(self):
        """A card revised in place carries `rev`, and each comment the rev it
        was made on: his comment on rev 1 is answered by rev 2, and a comment
        on rev 2 is open again."""
        tid = self.add_goal()["id"]
        row = self.row(tid)
        card = dict(self.card(row["goal"]["card"]), rev=2, comments=[
            {"ts": pk.now_ts(), "text": "add a bar", "by": "owner",
             "door": "web", "rev": 1}])
        self.assertEqual(goals.state(row, card, [])["state"], "revised")
        card["comments"].append({"ts": pk.now_ts(), "text": "and another",
                                 "by": "owner", "door": "web", "rev": 2})
        self.assertEqual(goals.state(row, card, [])["state"], "commented")

    def test_the_card_not_pushed_flag_shows(self):
        tid = self.add_goal()["id"]
        line = self.list_line(tid)[0]
        self.assertIn("card NOT PUSHED to his phone", line)


class ReportTest(GoalBase):

    def ready(self):
        tid = self.add_goal()["id"]
        return tid

    def test_report_before_his_yes_is_refused(self):
        tid = self.ready()
        for key in ("c1", "c2"):
            self.measure(tid, key)
        self.measure(tid, "c1", seat=OTHER)
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIsNone(row)
        self.assertIn("has not said yes", err)
        self.yes(tid)
        self.assertIsNone(goals.report(tid, "chat:post-x")[2])

    def test_evidence_before_the_yes_is_labelled_and_counts(self):
        tid = self.ready()
        row, _n, err = self.measure(tid, "c2")
        self.assertIsNone(err)
        self.assertTrue(goals._latest(row["goal"]["criteria"][1])[
            "pre_approval"])
        self.yes(tid)
        row, _n, _e = self.measure(tid, "c1")
        self.assertFalse(goals._latest(row["goal"]["criteria"][0])[
            "pre_approval"])

    def test_a_criterion_short_of_passing_refuses_by_key(self):
        tid = self.ready()
        self.yes(tid)
        self.measure(tid, "c1", seat=OTHER)
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIsNone(row)
        self.assertIn("c2 has no evidence", err)
        self.measure(tid, "c2", passed=False, value="close went through")
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIn("c2 FAILED last (close went through)", err)
        self.measure(tid, "c2")
        self.assertIsNone(goals.report(tid, "chat:post-x")[2])

    def test_the_accountable_seat_never_certifies_its_own_why(self):
        tid = self.ready()
        self.yes(tid)
        self.measure(tid, "c1")
        self.measure(tid, "c2")
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIsNone(row)
        self.assertIn("must be re-run by a seat other than %s" % ACCOUNTABLE,
                      err)
        # an independent pass older than a later fail does not count
        self.measure(tid, "c1", seat=OTHER)
        self.measure(tid, "c1", passed=False, value="regressed")
        self.measure(tid, "c1")
        self.assertIn("seat other than", goals.report(tid, "chat:post-x")[2])
        self.measure(tid, "c1", seat=OTHER)
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")

    def test_report_is_the_closer_and_its_reason_carries_the_evidence(self):
        tid = self.ready()
        self.yes(tid)
        self.measure(tid, "c1", value="2 of 3 done", seat=OTHER)
        self.measure(tid, "c2", value="refused rc 2")
        row, _n, err = goals.report(tid, "chat:post-told")
        self.assertIsNone(err, err)
        self.assertEqual(row["goal"]["report_ref"], "chat:post-told")
        self.assertEqual(row["goal"]["report"]["by"], ACCOUNTABLE)
        for piece in ("DONE: 2 of 2", "c1 pass (2 of 3 done, by %s)" % OTHER,
                      "c2 pass (refused rc 2", "post chat:post-told"):
            self.assertIn(piece, row["closed_reason"])
        self.assertIn("is DONE", goals.report(tid, "again")[2])

    def test_a_dispute_reopens_a_done_goal_only_on_his_word(self):
        tid = self.ready()
        self.yes(tid)
        self.measure(tid, "c1", seat=OTHER)
        self.measure(tid, "c2")
        self.assertIsNone(goals.report(tid, "chat:post-told")[2])
        row, _n, err = self.measure(tid, "c2", passed=False, value="he saw it")
        self.assertIsNone(row)
        self.assertIn("--ref <his post", err)
        self.assertEqual(self.row(tid)["status"], "closed")
        post = self.owner_post("no, the cycle line is still wrong")
        row, notes, err = self.measure(tid, "c2", passed=False,
                                       value="he saw it", ref=post["id"])
        self.assertIsNone(err, err)
        self.assertEqual((row["status"], row["closed_reason"],
                          row["goal"]["report_ref"]), ("open", None, None))
        self.assertEqual(row["goal"]["reopened"][-1]["owner_said"],
                         {"kind": "chat", "id": post["id"], "ts": post["ts"],
                          "room": "main"})
        self.assertIn("REOPENED", "\n".join(notes))
        self.assertEqual(self.state(tid), "in-progress")
        self.assertIn("is a GOAL", tasks.close(tid, "landed again")[1])
        # a closed goal is not reopened by a task door either
        self.measure(tid, "c2")
        self.assertIsNone(goals.report(tid, "chat:post-told-2")[2])
        got, err = tasks.update(tid, status="open", closed_reason=None)
        self.assertIsNone(got)
        self.assertIn("reopens only when the owner disputes it", err)

    def test_measure_refuses_what_is_not_evidence(self):
        tid = self.ready()
        for args, words in ((("c9", True, "v", "h"), "no criterion 'c9'"),
                            (("c1", True, "", "h"), "no value"),
                            (("c1", True, "v", ""), "no --how")):
            row, _n, err = goals.measure(tid, *args)
            self.assertIsNone(row)
            self.assertIn(words, err)
        row, notes, err = goals.measure(tid, "c1", True, "3 of 4",
                                        "an eyeball of the page")
        self.assertIsNone(err)
        self.assertIn("does not contain the proof's command", notes[0])
        rc, _o, err = self.run_goal("measure", tid, "c1", "--pass", "x",
                                    "--fail", "y", "--how", "h")
        self.assertEqual(rc, 2)
        self.assertIn("exactly one of --pass", err)

    def test_evidence_on_the_row_is_bounded_and_the_ledger_keeps_it(self):  # noqa: VACUOUS_ASSERTION — the row's evidence count is an exact non-zero pin, and the ledger text is asserted to CONTAIN the oldest value
        tid = self.ready()
        for n in range(goals.EVIDENCE_KEEP + 3):
            self.measure(tid, "c2", value="run %d" % n)
        crit = self.row(tid)["goal"]["criteria"][1]
        self.assertEqual(len(crit["evidence"]), goals.EVIDENCE_KEEP)
        self.assertEqual(crit["evidence"][-1]["value"],
                         "run %d" % (goals.EVIDENCE_KEEP + 2))
        text = "\n".join(self.ledger_lines())
        self.assertIn('"value":"run 0"', text)


class ReviseTest(GoalBase):

    def test_an_unchanged_criterion_keeps_its_key_and_evidence(self):
        tid = self.add_goal()["id"]
        self.measure(tid, "c1", value="kept")
        row, _n, err = goals.revise(tid, criteria(
            TWO[0], "- A goal closes only on its approved criteria :: "
                    "`helm task close` refuses"))
        self.assertIsNone(err, err)
        c1, c3 = row["goal"]["criteria"]
        self.assertEqual((c1["key"], c1["version"]), ("c1", 1))
        self.assertEqual(goals._latest(c1)["value"], "kept")
        self.assertEqual((c3["key"], c3["version"], c3["evidence"]),
                         ("c3", 2, []))

    def test_nothing_changed_refuses_and_a_done_goal_refuses(self):
        tid = self.add_goal()["id"]
        self.assertIn("nothing changed",
                      goals.revise(tid, criteria(*TWO))[2])
        self.assertEqual(len(ownerasks.decision_rows()), 1)

    THREE = TWO + ("- The brief prints in 6 seconds :: `time helm brief`",)

    def test_an_open_card_is_revised_in_place(self):  # noqa: VACUOUS_ASSERTION — the empty `cards` list sits in one tuple with the kept card id and criteria v2, both PRESENT, and the single-card ledger is an exact pin
        """His card is open: the revision is the same card at its next rev,
        with the replaced body kept and his thread kept."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        ownerasks.comment_decision(first, "add a latency bar",
                                   by=ownerasks.owner_door("web"))
        filed = self.card(first)
        self.assertEqual(self.state(tid), "commented")
        row, notes, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((row["goal"]["card"], row["goal"]["cards"],
                          row["goal"]["criteria_version"]), (first, [], 2))
        self.assertEqual(list(ownerasks.decision_rows()), [first])
        card = self.card(first)
        self.assertEqual((card["status"], card["rev"], card["asker"]),
                         ("open", 2, ACCOUNTABLE))
        self.assertEqual([c["text"] for c in card["comments"]],
                         ["add a latency bar"])
        self.assertEqual([(r["rev"], r["context"]) for r in card["revisions"]],
                         [(1, filed["context"])])
        self.assertEqual(card["context"], goals.card_context(row))
        self.assertEqual(card["answering_comment_ts"],
                         filed["comments"][0]["ts"])
        self.assertEqual(self.state(tid), "revised")

    def test_a_decided_card_gets_a_new_card(self):  # noqa: VACUOUS_ASSERTION — the untouched decided card is an equality with its non-empty pre-revise snapshot, beside the new card id, refs and rev asserted PRESENT
        """After his verdict the card is never re-ruled: a changed scope is a
        new card, and the decided one moves to `cards` untouched."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.yes(tid)
        self.assertEqual(self.state(tid), "criteria-approved")
        decided = self.card(first)
        row, notes, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        second = row["goal"]["card"]
        self.assertNotEqual(second, first)
        self.assertEqual(row["goal"]["cards"], [first])
        self.assertEqual(sorted(ownerasks.decision_rows()),
                         sorted([first, second]))
        self.assertEqual(self.card(first), decided)
        card = self.card(second)
        self.assertEqual((card["status"], card["rev"], card["refs"]),
                         ("open", 1, [tid, "goal-criteria:v2"]))
        self.assertEqual(self.state(tid), "revised")
        self.yes(tid)
        self.assertEqual(self.state(tid), "criteria-approved")

    def test_only_the_cards_asker_revises_the_open_card(self):
        """The open card is revised by its asker only (the decision ledger's
        law), and a refused revision writes nothing: no row, no card."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        before = (self.ledger_lines(), ownerasks.decision_rows())
        declare(self, OTHER)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        declare(self, ACCOUNTABLE)
        self.assertIsNone(row)
        self.assertIn("only its asker revises it", err)
        self.assertEqual((self.ledger_lines(), ownerasks.decision_rows()),
                         before)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((row["goal"]["card"], self.card(first)["rev"]),
                         (first, 2))

    def test_a_row_write_lost_after_the_card_moved_is_repaired_by_rerunning(self):
        """The card is revised first and the row second. When the row write
        fails, the card already carries the revision, and the same revise run
        again writes the row only: no second rev of the card."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        real = tasks.update

        def lose_the_revise(*a, **kw):
            door = kw.get("goal_door")
            if door is not None and door.act == "revise":
                return None, "planted: ledger unwritable"
            return real(*a, **kw)
        with mock.patch("helm.tasks.update", side_effect=lose_the_revise):
            row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(row)
        self.assertIn("planted: ledger unwritable", err)
        self.assertIn("helm goal revise %s" % tid, err)
        self.assertEqual(self.row(tid)["goal"]["criteria_version"], 1)
        self.assertEqual(self.card(first)["rev"], 2)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual(row["goal"]["criteria_version"], 2)
        self.assertEqual((self.card(first)["rev"], row["goal"]["card"]),
                         (2, first))
        self.assertEqual(self.card(first)["context"], goals.card_context(row))

    def test_a_revised_cards_refs_name_the_new_criteria_version(self):
        """The card's refs say which criteria version its body carries, so
        after an in-place revise they name the new one; the replaced refs
        ride the replaced body on `revisions`."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.assertEqual(self.card(first)["refs"], [tid, "goal-criteria:v1"])
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual(row["goal"]["criteria_version"], 2)
        card = self.card(first)
        self.assertEqual(card["refs"], [tid, "goal-criteria:v2"])
        self.assertEqual(card["revisions"][0]["refs"],
                         [tid, "goal-criteria:v1"])


class CustodyTest(GoalBase):
    """CARD CUSTODY FOLLOWS THE GOAL. When a goal's accountable seat changes,
    its OPEN card answers to the new seat in the same write, and the move is
    recorded on the card. Every door that changes a task's owner reaches
    `tasks.update`; the seat reassignment of a dead seat's holdings is driven
    here the way production drives it (the measured disposition is the only
    fixture). A decided card keeps its asker: the verdict is that seat's."""

    THREE = ReviseTest.THREE

    def reassign(self, tid, to):
        """Move the goal to `to` through the seat-reassign capability, the
        door `helm seat reassign` takes, against a seat measured dead."""
        row, err = tasks.update(tid, takeover_auth=self.reassign_auth(tid, to),
                                owner=to)
        self.assertIsNone(err, err)
        self.assertEqual(tasks.owner_of(row), to)
        return row

    def test_the_open_card_follows_and_the_new_seat_revises_it_in_place(self):  # noqa: VACUOUS_ASSERTION — the empty `cards` list sits in one tuple with the kept card id and criteria v2, both PRESENT, and the single-card ledger is an exact pin
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        ownerasks.comment_decision(first, "add a latency bar",
                                   by=ownerasks.owner_door("web"))
        self.reassign(tid, OTHER)
        card = self.card(first)
        self.assertEqual(card["asker"], OTHER)
        self.assertEqual([(c["from"], c["to"], c["goal"])
                          for c in card["custody"]],
                         [(ACCOUNTABLE, OTHER, tid)])
        self.assertTrue(card["custody"][0]["ts"])
        # the body he reads did not change, so his page is not made stale
        self.assertEqual((card["rev"], card["status"]), (1, "open"))
        declare(self, OTHER)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((row["goal"]["card"], row["goal"]["cards"],
                          row["goal"]["criteria_version"]), (first, [], 2))
        self.assertEqual(list(ownerasks.decision_rows()), [first])
        card = self.card(first)
        self.assertEqual((card["rev"], card["asker"], card["status"]),
                         (2, OTHER, "open"))
        self.assertEqual([c["text"] for c in card["comments"]],
                         ["add a latency bar"])

    def test_the_previous_seat_can_no_longer_revise_it(self):
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.reassign(tid, OTHER)
        before = (self.ledger_lines(), ownerasks.decision_rows())
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(row)
        self.assertIn("only its asker revises it", err)
        self.assertIn(OTHER, err)
        self.assertEqual((self.ledger_lines(), ownerasks.decision_rows()),
                         before)
        declare(self, OTHER)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((row["goal"]["card"], self.card(first)["rev"]),
                         (first, 2))

    def test_a_decided_card_keeps_its_asker_and_the_open_one_moves(self):  # noqa: VACUOUS_ASSERTION — the untouched decided card is an equality with its non-empty pre-reassign snapshot, beside the open card's moved asker and custody event asserted PRESENT
        """After his Yes a changed scope is a new, open card beside the
        decided one. A reassignment moves the open card only; the decided
        card is untouched, row for row."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.yes(tid)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        second = row["goal"]["card"]
        self.assertNotEqual(second, first)
        decided = self.card(first)
        self.reassign(tid, OTHER)
        self.assertEqual(self.card(first), decided)
        self.assertEqual(self.card(first)["asker"], ACCOUNTABLE)
        card = self.card(second)
        self.assertEqual((card["asker"], card["status"]), (OTHER, "open"))
        self.assertEqual([(c["from"], c["to"]) for c in card["custody"]],
                         [(ACCOUNTABLE, OTHER)])

    def test_a_lost_card_move_is_said_and_healed_by_the_new_seats_revise(self):  # noqa: VACUOUS_ASSERTION — the flag's absence after the heal follows its PRESENCE on the same `goal show` surface earlier in the arm, and the ledgers left unchanged by the refusal are then grown by the healing revise, asserted PRESENT
        """The row is the custody record and the card follows it. A card
        move that fails after the row moved leaves the card naming the old
        seat: `helm goal show` says so, the old seat is still refused, and
        the new seat's revise moves the card before revising it."""
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        with mock.patch.object(ownerasks, "move_asker", return_value=(
                None, "planted: decision ledger unwritable")) as lost:
            self.reassign(tid, OTHER)
        # the double fired: the reassignment asked for the move, and lost it
        self.assertEqual(lost.call_count, 1)
        self.assertEqual((lost.call_args[0][0], lost.call_args[0][1],
                          lost.call_args[0][3]), (first, OTHER, tid))
        self.assertEqual(self.card(first)["asker"], ACCOUNTABLE)
        rc, out, _e = self.run_goal("show", tid)
        self.assertEqual(rc, 0)
        self.assertIn("still answers to its previous seat", out)
        before = (self.ledger_lines(), ownerasks.decision_rows())
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(row)
        self.assertIn("only its asker revises it", err)
        self.assertEqual((self.ledger_lines(), ownerasks.decision_rows()),
                         before)
        declare(self, OTHER)
        row, notes, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        card = self.card(first)
        self.assertEqual((card["asker"], card["rev"]), (OTHER, 2))
        self.assertEqual([(c["from"], c["to"]) for c in card["custody"]],
                         [(ACCOUNTABLE, OTHER)])
        self.assertNotIn("still answers to its previous seat",
                         self.run_goal("show", tid)[1])

    def test_a_why_pass_the_accountable_seat_recorded_stays_its_own_after_custody_moves(self):
        """The accountable seat records its own why-pass, then custody moves
        to another seat. That pass was self-certified when it was recorded
        and does not become independent because a different seat is
        accountable NOW: report is still refused. The control: the previous
        seat, no longer accountable, re-runs the proof after the move, and
        that pass is another seat's and counts."""
        tid = self.add_goal()["id"]
        self.yes(tid)
        self.measure(tid, "c1")
        self.measure(tid, "c2")
        self.assertIn("seat other than", goals.report(tid, "chat:post-x")[2])
        self.reassign(tid, OTHER)
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIsNone(row)
        self.assertIn("seat other than", err)
        self.assertEqual(self.row(tid)["status"], "open")
        self.measure(tid, "c1")
        row, _n, err = goals.report(tid, "chat:post-x")
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(goals._latest(row["goal"]["criteria"][0])["by"],
                         ACCOUNTABLE)


class ApprovalBindsRevTest(GoalBase):
    """HIS YES BINDS THE CARD REV THE ROW CARRIES. The goal row stores
    `card_rev`, the rev whose body carries its current criteria, in the same
    write as the criteria, and a Yes counts only when it was ruled on that
    rev. A Yes on the card after a lost row write approved a body the row
    does not carry; the same revise, run again, reconciles them."""

    THREE = ReviseTest.THREE

    def lose_the_revise(self, tid):
        real = tasks.update

        def lose(*a, **kw):
            door = kw.get("goal_door")
            if door is not None and door.act == "revise":
                return None, "planted: ledger unwritable"
            return real(*a, **kw)
        with mock.patch("helm.tasks.update", side_effect=lose):
            row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(row)
        self.assertIn("planted: ledger unwritable", err)

    def test_the_row_names_the_rev_that_carries_its_criteria(self):
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.assertEqual(self.row(tid)["goal"]["card_rev"], 1)
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((row["goal"]["card"], row["goal"]["card_rev"]),
                         (first, 2))
        self.yes(tid)
        self.assertEqual(self.state(tid), "criteria-approved")
        # after his verdict a changed scope is a new card, bound at its rev 1
        row, _n, err = goals.revise(tid, criteria(*TWO))
        self.assertIsNone(err, err)
        self.assertNotEqual(row["goal"]["card"], first)
        self.assertEqual(self.card(row["goal"]["card"])["status"], "open")
        self.assertEqual((row["goal"]["card_rev"],
                          row["goal"]["criteria_version"]), (1, 3))

    def test_a_yes_on_a_rev_the_row_does_not_carry_is_not_approval(self):
        tid = self.add_goal()["id"]
        first = self.row(tid)["goal"]["card"]
        self.lose_the_revise(tid)
        self.assertEqual((self.card(first)["rev"],
                          self.row(tid)["goal"]["criteria_version"]), (2, 1))
        # his page shows rev 2, and he says yes to it: it approves the body
        # the card carries, which is not the row's criteria v1
        self.yes(tid)
        self.assertEqual(self.card(first)["verdict"]["rev"], 2)
        self.assertEqual(self.state(tid), "revised")
        self.assertEqual(self.row(tid)["goal"]["card_rev"], 1)
        rc, out, err = self.run_goal("show", tid)
        self.assertEqual(rc, 0, err)
        self.assertIn("approved a different revision", out)
        rc, out, err = self.run_goal("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("helm goal revise %s" % tid, lines_naming(out, tid)[0])
        row, _n, err = goals.report(tid, "chat:post-told")
        self.assertIsNone(row)
        self.assertIn("different revision", err)
        # the recovery: the same revise, run again, writes the row only, and
        # binds it to the rev he approved; no second card, no second rev
        row, _n, err = goals.revise(tid, criteria(*self.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((row["goal"]["card"], row["goal"]["cards"],
                          row["goal"]["card_rev"],
                          row["goal"]["criteria_version"]),
                         (first, [], 2, 2))
        self.assertEqual(list(ownerasks.decision_rows()), [first])
        self.assertEqual(self.card(first)["rev"], 2)
        self.assertEqual(self.state(tid), "criteria-approved")
        self.assertNotIn("approved a different revision",
                         self.run_goal("show", tid)[1])


class ProjectionTest(GoalBase):

    def fresh(self):
        return goals.compute(tasks.rows(), ownerasks.decision_rows())["goals"]

    def test_the_projection_matches_a_fresh_fold_through_every_write(self):
        tid = self.add_goal()["id"]
        proj, problem = goals.read_projection()
        self.assertIsNone(problem)
        self.assertEqual(proj["goals"], self.fresh())
        self.yes(tid)
        self.child(tid, "Write the cycle report")
        self.measure(tid, "c1", seat=OTHER)
        kid = [k for k in tasks.rows().values() if k.get("continues") == tid]
        tasks.close(kid[0]["id"], "LANDED")
        proj, problem = goals.read_projection()
        self.assertIsNone(problem)
        self.assertEqual(proj["goals"], self.fresh())
        self.assertEqual(proj["goals"][tid]["children"]["closed"], 1)
        self.assertEqual(proj["goals"][tid]["state"], "in-progress")

    def test_an_unrelated_task_write_leaves_it_current(self):
        self.add_goal()
        tasks.add("An unrelated engineering row", ACCOUNTABLE,
                  posture_na="test fixture", force_new=True)
        proj, problem = goals.read_projection()
        self.assertIsNone(problem)
        self.assertEqual(proj["goals"], self.fresh())
        # the control on the same read: a row that DOES belong to the goal's
        # story, appended behind the writer's back, makes it stale
        tid = list(proj["goals"])[0]
        self.assertTrue(eventledger.append(tasks.ledger_path(), {
            "id": "task/77", "title": "a child nobody refreshed",
            "status": "open", "continues": tid}))
        proj, problem = goals.read_projection()
        self.assertIn("has a newer row for task/77", problem)

    def test_a_verdict_it_has_not_seen_reads_stale_and_sync_cures_it(self):
        tid = self.add_goal()["id"]
        self.assertIsNone(goals.read_projection()[1])
        self.yes(tid)
        proj, problem = goals.read_projection()
        self.assertIn("stale: the decisions ledger has a newer row", problem)
        self.assertEqual(proj["goals"][tid]["state"], "proposed")
        rc, out, err = self.run_goal("sync")
        self.assertEqual(rc, 0, err)
        proj, problem = goals.read_projection()
        self.assertIsNone(problem)
        self.assertEqual(proj["goals"][tid]["state"], "criteria-approved")

    def test_a_goal_row_written_behind_its_back_reads_stale(self):
        tid = self.add_goal()["id"]
        row = dict(self.row(tid), title="retitled by hand")
        self.assertTrue(eventledger.append(tasks.ledger_path(), row))
        self.assertIn("has a newer row for %s" % tid,
                      goals.read_projection()[1])

    def test_absent_is_not_empty(self):
        proj, problem = goals.read_projection()
        self.assertIsNone(proj)
        self.assertIn("absent", problem)

    def test_a_read_verb_never_certifies_bytes_it_did_not_fold(self):
        """`helm goal list` refreshes the projection from the fold it holds,
        OUTSIDE the ledger lock. A goal row appended after that fold and
        before the refresh measured the ledger is inside the size the
        projection is dated to and outside the fold: the projection must
        then read STALE naming that row, never current, until a refresh
        that folded it. The control: a refresh from a fresh fold covers it."""
        self.add_goal()
        real = tasks.snapshot
        landed = []

        def racing(*a, **kw):
            known, unavailable = real(*a, **kw)
            if not landed:
                with mock.patch.object(tasks, "snapshot", real):
                    row, _n, err = goals.add(
                        "A goal that landed mid-fold", ACCOUNTABLE, WORDS,
                        "chat:post-2", "why", criteria(*TWO), force_new=True)
                self.assertIsNone(err, err)
                landed.append(row["id"])
            return known, unavailable
        with mock.patch.object(tasks, "snapshot", side_effect=racing):
            rc, _o, err = self.run_goal("list")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(landed), 1)
        proj, problem = goals.read_projection()
        self.assertNotIn(landed[0], proj["goals"])
        self.assertIn("stale: the tasks ledger has a newer row for %s"
                      % landed[0], problem or "current")
        proj, err = goals.refresh_projection()
        self.assertIsNone(err, err)
        self.assertIn(landed[0], proj["goals"])
        self.assertIsNone(goals.read_projection()[1])


class CycleTest(GoalBase):

    def test_x_of_y_done_and_first_wave_yield(self):
        done = self.add_goal()["id"]
        self.yes(done)
        self.measure(done, "c1", seat=OTHER)
        self.measure(done, "c2")
        self.assertIsNone(goals.report(done, "chat:post-told")[2])
        moving = self.add_goal(title="A second owner goal about the brief")["id"]
        self.yes(moving)
        kid = self.child(moving, "Write the brief section")
        tasks.close(kid["id"], "LANDED")
        self.measure(moving, "c1", seat=OTHER)
        waiting = self.add_goal(title="A third owner goal on the web page")["id"]
        later = time.time() + goals.FIRST_WAVE_GRACE_S + 60
        rep, err = goals.cycle(since=0, now=later)
        self.assertIsNone(err, err)
        self.assertEqual((rep["done"], rep["counted"], rep["waiting_on_yes"]),
                         (1, 2, 1))
        self.assertEqual(rep["criteria"], [3, 4])
        self.assertEqual(rep["first_wave"], [1, 2, 1])
        lines = {g["id"]: g for g in rep["goals"]}
        self.assertEqual(lines[moving]["first_wave"]["passing"], 1)
        self.assertIn("pending", lines[waiting]["first_wave"])
        text = goals.render_cycle(rep)
        self.assertIn("1 of 2 done (50%)", text)
        self.assertIn("first-wave yield: 50%", text)
        self.assertIn("waiting on the owner's yes: 1", text)
        # before the grace window the wave is pending, never a zero
        rep, _e = goals.cycle(since=0)
        self.assertIn("pending", {g["id"]: g for g in rep["goals"]}[moving][
            "first_wave"])
        self.assertIn("no goal's first wave closed", goals.render_cycle(rep))

    def test_a_done_goal_outside_the_window_is_not_counted(self):  # noqa: VACUOUS_ASSERTION — the (0, 0) window is paired with the (1, 1) window on the same report fields in the same arm
        done = self.add_goal()["id"]
        self.yes(done)
        self.measure(done, "c1", seat=OTHER)
        self.measure(done, "c2")
        goals.report(done, "chat:post-told")
        rep, _e = goals.cycle(since=time.time() + 60)
        self.assertEqual((rep["done"], rep["counted"]), (0, 0))
        rep, _e = goals.cycle(since=0)
        self.assertEqual((rep["done"], rep["counted"]), (1, 1))

    def test_first_wave_is_the_first_time_every_child_so_far_closed(self):
        kids = [{"id": "task/1", "ts": 10}, {"id": "task/2", "ts": 20},
                {"id": "task/3", "ts": 50}]
        closed = {"task/1": 30, "task/2": 40, "task/3": 60}
        self.assertEqual(goals.first_wave_closed(kids, closed), 40)
        self.assertIsNone(goals.first_wave_closed(kids, {"task/1": 30}))
        self.assertIsNone(goals.first_wave_closed([], {}))

    def test_the_cli_prints_the_cycle(self):
        self.add_goal()
        rc, out, err = self.run_goal("cycle", "--days", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("goals this cycle", out)
        rc, out, _e = self.run_goal("cycle", "--json")
        self.assertEqual(json.loads(out)["waiting_on_yes"], 1)
        rc, _o, err = self.run_goal("cycle", "--since", "yesterday-ish")
        self.assertEqual(rc, 2)
        self.assertIn("is not a time", err)


class DispatchTest(GoalBase):

    def test_unknown_subverb_and_junk_before_help_refuse(self):
        rc, _o, err = self.run_goal("zz-no-such")
        self.assertEqual(rc, 2)
        self.assertIn("unknown subverb", err)
        rc, _o, err = self.run_goal("list", "--bogus", "--help")
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)
        rc, out, _e = self.run_goal("list", "--json", "--help")
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm goal", out)
        rc, _o, err = self.run_goal("show", "task/1", "--bogus")
        self.assertEqual(rc, 2)

    def test_show_and_list_json(self):
        tid = self.add_goal()["id"]
        rc, out, _e = self.run_goal("show", tid, "--json")
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual((got["id"], got["derived"]["state"]),
                         (tid, "proposed"))
        rc, out, _e = self.run_goal("list", "--json")
        self.assertEqual(list(json.loads(out)), [tid])
        rc, _o, err = self.run_goal("show", "task/99999")
        self.assertEqual(rc, 1)
        self.assertIn("not a goal", err)


class OwnerWordTest(GoalBase):
    """A GOAL CLOSES OR REOPENS ON THE OWNER'S WORD, AND HIS WORD IS A RECORD.
    `supersede --owner-ref` and a reopening `measure --fail --ref` resolve
    the ref to something helm can read and he authored: a chat row he posted
    through one of his own doors, by id or unambiguous prefix across every
    room and DM lane, or his door-bearing comment on this goal's criteria card
    (`card:<id>` or `card:<id>@<ts>`, as `helm decide show` prints it). The
    card's Yes verdict approves criteria and cannot replace/dispute the goal.
    The record is dated strictly after the goal's criteria card was filed and
    is never the post its words_ref names: the words that created a goal
    cannot replace or reopen it.
    The resolved record (kind, id, ts) is written on the row. A
    seat's row, a row under his name that none of his doors stamped, an id
    nothing matches, and a store that cannot be read each refuse, naming what
    was looked up, and write nothing."""

    NOT_HIS = "2099-01-01T00:00:00Z"

    def done_goal(self):
        tid = self.add_goal()["id"]
        self.yes(tid)
        self.measure(tid, "c1", seat=OTHER)
        self.measure(tid, "c2")
        self.assertIsNone(goals.report(tid, "chat:post-told")[2])
        return tid

    def test_supersede_on_his_post_records_the_post_it_resolved(self):
        tid = self.add_goal()["id"]
        post = self.owner_post("drop this goal, the brief work replaces it")
        # an 8-character prefix: never read as a row number, and resolved to
        # the whole id on the row
        rc, _o, err = self.run_goal("supersede", tid, "--owner-ref",
                                    post["id"][:8])
        self.assertEqual(rc, 0, err)
        row = self.row(tid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["goal"]["superseded"]["owner_said"],
                         {"kind": "chat", "id": post["id"], "ts": post["ts"],
                          "room": "main"})
        self.assertIn(post["id"], row["closed_reason"])

    def test_a_ref_that_is_not_his_word_refuses_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the unchanged ledger is an exact pin against its non-empty filing snapshot, and the control closes the same goal through the same verb, its closed status asserted PRESENT
        tid = self.add_goal()["id"]
        before = self.ledger_lines()
        mine = self.seat_post("the owner wants this goal dropped")
        # his name, and no door of his stamped it
        forged = chat.post("drop this goal", room="main", who="daria")
        for ref, words in (
                (mine["id"], ("posted by %s" % ACCOUNTABLE, "not the owner")),
                (forged["id"], ("not through one of his doors",)),
                ("feedfacecafe", ("looked up as a chat row id",
                                  "no message matches"))):
            rc, _o, err = self.run_goal("supersede", tid, "--owner-ref", ref)
            self.assertEqual(rc, 1, (ref, err))
            self.assertIn(ref, err)
            for w in words:
                self.assertIn(w, err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "open")
        # the control: his own post, through the same verb, closes it
        post = self.owner_post("drop it")
        rc, _o, err = self.run_goal("supersede", tid, "--owner-ref", post["id"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.row(tid)["status"], "closed")

    def test_an_unreadable_store_refuses(self):
        tid = self.add_goal()["id"]
        post = self.owner_post("drop this goal")
        with mock.patch.object(chat, "list_rooms", side_effect=OSError(
                "planted: the chat directory will not list")):
            rc, _o, err = self.run_goal("supersede", tid, "--owner-ref",
                                        post["id"])
        self.assertEqual(rc, 1, err)
        self.assertIn("could not be read", err)
        self.assertIn(post["id"], err)
        self.assertEqual(self.row(tid)["status"], "open")
        cid = self.row(tid)["goal"]["card"]
        ownerasks.comment_decision(cid, "drop it",
                                   by=ownerasks.owner_door("web"))
        with mock.patch.object(ownerasks, "decisions_snapshot", return_value=(
                {}, "planted: decision ledger unreadable")):
            rc, _o, err = self.run_goal("supersede", tid, "--owner-ref",
                                        "card:" + cid)
        self.assertEqual(rc, 1, err)
        self.assertIn("decision ledger unreadable", err)
        self.assertIn("card:" + cid, err)
        self.assertEqual(self.row(tid)["status"], "open")
        # the control: the same refs, each store readable
        rc, _o, err = self.run_goal("supersede", tid, "--owner-ref", post["id"])
        self.assertEqual(rc, 0, err)

    def test_his_comment_on_the_goals_card_resolves(self):
        tid = self.add_goal()["id"]
        other = self.add_goal(title="A second owner goal about the brief")
        cid = self.row(tid)["goal"]["card"]
        ownerasks.comment_decision(cid, "a seat's note on the card")
        seat_ts = self.card(cid)["comments"][-1]["ts"]
        for ref, words in (
                ("card:" + cid, ("no comment or verdict of the owner",)),
                ("card:%s@%s" % (cid, seat_ts), ("by %s" % ACCOUNTABLE,
                                                 "not the owner")),
                ("card:" + other["goal"]["card"],
                 ("not one of %s's criteria cards" % tid,)),
                ("card:ffffffff", ("not one of %s's criteria cards" % tid,))):
            row, _n, err = goals.supersede(tid, ref)
            self.assertIsNone(row, ref)
            self.assertIn(ref, err)
            for w in words:
                self.assertIn(w, err)
        self.assertEqual(self.row(tid)["status"], "open")
        with mock.patch.object(pk, "now_ts", return_value=LATER):
            ownerasks.comment_decision(cid, "drop it, the brief goal replaces "
                                       "it", by=ownerasks.owner_door("web"))
        his = self.card(cid)["comments"][-1]
        row, _n, err = goals.supersede(tid, "card:" + cid, by_task=other["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["goal"]["superseded"]["owner_said"],
                         {"kind": "card-comment", "id": cid, "ts": his["ts"],
                          "rev": 1})

    def test_the_goals_yes_verdict_is_not_a_replacement_or_dispute(self):
        tid = self.add_goal()["id"]
        cid = self.row(tid)["goal"]["card"]
        self.yes(tid)
        v = self.card(cid)["verdict"]
        said, err = goals.owner_word("card:%s@%s" % (cid, v["ts"]),
                                     self.row(tid)["goal"], "--owner-ref")
        self.assertIsNone(said)
        self.assertIn("Yes approves the goal's criteria", err)
        self.assertIn("does not replace or dispute the goal", err)

    def test_a_done_goal_reopens_only_on_a_ref_that_resolves_to_him(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal two-row tuple, and the reopen that follows is unconditional, its status, owner_said record and note asserted PRESENT
        tid = self.done_goal()
        mine = self.seat_post("he says the cycle line is wrong")
        for ref, words in ((mine["id"], ("posted by %s" % ACCOUNTABLE,
                                         "not the owner")),
                           ("feedfacecafe", ("no message matches",))):
            row, _n, err = self.measure(tid, "c2", passed=False,
                                        value="he saw it", ref=ref)
            self.assertIsNone(row, ref)
            self.assertIn(ref, err)
            for w in words:
                self.assertIn(w, err)
            self.assertEqual(self.row(tid)["status"], "closed")
        cid = self.row(tid)["goal"]["card"]
        yes_ts = self.card(cid)["verdict"]["ts"]
        with mock.patch.object(pk, "now_ts", return_value=self.NOT_HIS):
            ownerasks.comment_decision(cid, "the cycle line is wrong",
                                       by=ownerasks.owner_door("web"))
        # his Yes approves criteria and never disputes them, so it is no
        # candidate: the bare form names his one comment, with no ambiguity
        # whose other choice always refuses
        row, notes, err = self.measure(tid, "c2", passed=False,
                                       value="he saw it", ref="card:" + cid)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["goal"]["reopened"][-1]["owner_said"],
                         {"kind": "card-comment", "id": cid,
                          "ts": self.NOT_HIS, "rev": 1})
        self.assertIn("REOPENED", "\n".join(notes))
        # two comments of his ARE ambiguous, and the times offered are theirs
        second = "2099-01-02T00:00:00Z"
        with mock.patch.object(pk, "now_ts", return_value=second):
            ownerasks.comment_decision(cid, "and the header is wrong",
                                       by=ownerasks.owner_door("web"))
        said, err = goals.owner_word("card:" + cid, self.row(tid)["goal"],
                                     "--ref", tid)
        self.assertIsNone(said)
        self.assertIn("ambiguous", err)
        for ts in (self.NOT_HIS, second):
            self.assertIn(ts, err)
        self.assertNotIn(yes_ts, err)
        # a comment in the same second as his Yes is what that time names
        with mock.patch.object(pk, "now_ts", return_value=yes_ts):
            ownerasks.comment_decision(cid, "and the footer",
                                       by=ownerasks.owner_door("web"))
        said, err = goals._card_word("card:%s@%s" % (cid, yes_ts),
                                     self.row(tid)["goal"], "--ref", tid)
        self.assertIsNone(err, err)
        self.assertEqual((said["kind"], said["ts"]), ("card-comment", yes_ts))

    def test_the_yes_that_authorized_done_cannot_be_reused_as_a_dispute(self):  # noqa: VACUOUS_ASSERTION — the exact Yes ref passes the owner-word resolver before the cure; unchanged ledger plus closed state pin that the refused reopen writes nothing
        """The owner's Yes authorized DONE; it is not a later dispute of it."""
        tid = self.add_goal()["id"]
        self.yes(tid)
        cid = self.row(tid)["goal"]["card"]
        yes = self.card(cid)["verdict"]
        self.measure(tid, "c1", seat=OTHER)
        self.measure(tid, "c2")
        self.assertIsNone(goals.report(tid, "chat:post-told")[2])
        before = self.ledger_lines()
        row, _notes, err = self.measure(
            tid, "c2", passed=False, value="a seat disputes it",
            ref="card:%s@%s" % (cid, yes["ts"]))
        self.assertIsNone(row)
        self.assertIn("Yes that authorized DONE", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "closed")

    def test_his_word_postdates_the_goals_card_and_is_not_its_words_ref(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal three-row tuple, the unchanged ledger is an exact pin, and the control closes the same goal through the same verb, its owner_said record asserted PRESENT
        """THE WORDS THAT CREATED A GOAL CANNOT REPLACE OR REOPEN IT. The ref
        must be dated STRICTLY AFTER the goal's criteria card was filed, and
        is never the post its words_ref names, whatever that post's stamp:
        here it is stamped after the card, so only the words_ref test can
        refuse it. Each time refusal names the cited post's time and the
        card's."""
        asked = self.owner_post("i state a goal yall figure out how to get "
                                "there")
        tid = self.add_goal(words_ref="chat:" + asked["id"])["id"]
        filed = self.card(self.row(tid)["goal"]["card"])["ts"]
        older = self.owner_post("drop the goals work", ts=EARLIER)
        equal = self.owner_post("drop it, the brief work replaces the goals "
                                "work", ts=filed)
        before = self.ledger_lines()
        for ref, words in (
                (asked["id"][:8], ("the post that asked for this goal",
                                   "chat:" + asked["id"])),
                (older["id"], (EARLIER, filed, "strictly after")),
                (equal["id"], (filed, "strictly after"))):
            rc, _o, err = self.run_goal("supersede", tid, "--owner-ref", ref)
            self.assertEqual(rc, 1, (ref, err))
            self.assertIn(ref, err)
            for w in words:
                self.assertIn(w, err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "open")
        # the control: his post dated after the card closes it
        later = self.owner_post("drop it, the brief work replaces it")
        rc, _o, err = self.run_goal("supersede", tid, "--owner-ref",
                                    later["id"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.row(tid)["goal"]["superseded"]["owner_said"],
                         {"kind": "chat", "id": later["id"], "ts": LATER,
                          "room": "main"})

    def test_an_older_word_cannot_reopen_a_done_goal(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal two-row tuple, the unchanged ledger is an exact pin, and the control reopens the same goal through the same verb, its status asserted PRESENT
        """The same bound on a reopen, and on his card comment's time."""
        tid = self.done_goal()
        cid = self.row(tid)["goal"]["card"]
        filed = self.card(cid)["ts"]
        older = self.owner_post("the cycle line is wrong", ts=EARLIER)
        with mock.patch.object(pk, "now_ts", return_value=EARLIER):
            ownerasks.comment_decision(cid, "the cycle line is wrong",
                                       by=ownerasks.owner_door("web"))
        before = self.ledger_lines()
        for ref in (older["id"], "card:%s@%s" % (cid, EARLIER)):
            row, _n, err = self.measure(tid, "c2", passed=False,
                                        value="he saw it", ref=ref)
            self.assertIsNone(row, ref)
            for w in (ref, EARLIER, filed, "strictly after"):
                self.assertIn(w, err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "closed")
        # the control: his post dated after the card reopens it
        later = self.owner_post("the cycle line is still wrong")
        row, _n, err = self.measure(tid, "c2", passed=False, value="he saw it",
                                    ref=later["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "open")

    def test_a_word_before_the_done_report_cannot_reopen_it(self):
        """A DISPUTE OF DONE POSTDATES THE DONE. A reopen cites his word
        dated strictly after the report that marked the goal done, as well as
        after its card: a post he made while the work was in flight disputes
        no done he had yet been told of. Here the card is filed in 2000, the
        post in 2010 and the report now, so only the report bound can refuse
        it. The refusal names the post's time and the report's."""
        with mock.patch.object(pk, "now_ts", return_value=EARLIER):
            tid = self.add_goal()["id"]
        self.yes(tid)
        self.measure(tid, "c1", seat=OTHER)
        self.measure(tid, "c2")
        self.assertIsNone(goals.report(tid, "chat:post-told")[2])
        done = pk.epoch_ts(self.row(tid)["goal"]["report"]["ts"])
        midway = "2010-01-01T00:00:00Z"
        between = self.owner_post("the cycle line is wrong", ts=midway)
        before = self.ledger_lines()
        row, _n, err = self.measure(tid, "c2", passed=False, value="he saw it",
                                    ref=between["id"])
        self.assertIsNone(row)
        self.assertIn(between["id"], err)
        self.assertIn(midway, err)
        self.assertIn("reported DONE at %s" % done, err)
        self.assertIn("strictly after", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "closed")
        # the control: his post dated after the report reopens it
        later = self.owner_post("the cycle line is still wrong")
        row, _n, err = self.measure(tid, "c2", passed=False, value="he saw it",
                                    ref=later["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["goal"]["reopened"][-1]["owner_said"]["ts"], LATER)

    def test_his_word_refuses_when_the_cards_filing_time_cannot_be_read(self):  # noqa: VACUOUS_ASSERTION — each refusal's words are asserted PRESENT, the unchanged ledger is an exact pin, and the control closes the same goal with the same post, its closed status asserted PRESENT
        """THE BOUND FAILS CLOSED. His word is dated against the goal's
        criteria card, so a goal with no card recorded, a decision ledger
        that cannot be read, a card the ledger does not hold and a card whose
        filing time does not parse each refuse his later post, naming what
        could not be read, and write nothing. The same post resolves once the
        card reads."""
        tid = self.add_goal()["id"]
        goal = self.row(tid)["goal"]
        cid = goal["card"]
        card = self.card(cid)
        post = self.owner_post("drop it, the brief work replaces it")
        said, err = goals.owner_word(post["id"], dict(goal, card=None),
                                     "--owner-ref", tid)
        self.assertIsNone(said)
        self.assertIn("none is recorded on it", err)
        before = self.ledger_lines()
        with mock.patch.object(ownerasks, "decisions_snapshot", return_value=(
                {}, "planted: decision ledger unreadable")):
            row, _n, err = goals.supersede(tid, post["id"])
        self.assertIsNone(row)
        self.assertIn("cannot be shown to postdate", err)
        self.assertIn("planted: decision ledger unreadable", err)
        with mock.patch.object(ownerasks, "decisions_snapshot",
                               return_value=({}, None)):
            row, _n, err = goals.supersede(tid, post["id"])
        self.assertIsNone(row)
        self.assertIn("%s took its current revision at unknown" % cid, err)
        with mock.patch.object(ownerasks, "decisions_snapshot", return_value=(
                {cid: dict(card, rev_ts="last tuesday")}, None)):
            row, _n, err = goals.supersede(tid, post["id"])
        self.assertIsNone(row)
        self.assertIn("%s took its current revision at last tuesday" % cid,
                      err)
        self.assertIn("does not read", err)
        self.assertNotIn("anything older", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "open")
        # the control: the same post, the card readable, closes it
        row, _n, err = goals.supersede(tid, post["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")

    def test_a_comment_a_revision_answered_is_no_word_on_the_goal(self):  # noqa: VACUOUS_ASSERTION — the same ref resolves before the revision, the unchanged ledger is an exact pin, and the control closes the goal, its closed status asserted PRESENT
        """THE BOUND IS THE CARD'S CURRENT REVISION: its rev_ts, or its
        filing time when it was never revised. His "not yet, add X" on rev 1 is
        answered by rev 2, so once rev 2 exists it is no word on the goal as
        it stands, and cannot replace or reopen it."""
        tid = self.add_goal()["id"]
        cid = self.row(tid)["goal"]["card"]
        with mock.patch.object(pk, "now_ts", return_value=LATER):
            ownerasks.comment_decision(cid, "not yet, add a latency bar",
                                       by=ownerasks.owner_door("web"))
        ref = "card:%s@%s" % (cid, LATER)
        said, err = goals.owner_word(ref, self.row(tid)["goal"],
                                     "--owner-ref", tid)
        self.assertIsNone(err, err)            # his word while rev 1 stands
        revised = "2099-02-01T00:00:00Z"
        with mock.patch.object(pk, "now_ts", return_value=revised):
            _row, _n, err = goals.revise(tid, criteria(*ReviseTest.THREE))
        self.assertIsNone(err, err)
        self.assertEqual((self.card(cid)["rev"], self.card(cid)["rev_ts"]),
                         (2, revised))
        before = self.ledger_lines()
        row, _n, err = goals.supersede(tid, ref)
        self.assertIsNone(row)
        for w in (ref, LATER, revised, "strictly after"):
            self.assertIn(w, err)
        self.assertEqual(self.ledger_lines(), before)
        # the control: his post after rev 2 closes it
        post = self.owner_post("drop it", ts="2099-03-01T00:00:00Z")
        row, _n, err = goals.supersede(tid, post["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")

    def test_a_revision_with_no_time_or_an_earlier_rev_answers_the_comment(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal two-row tuple, the unchanged ledger is an exact pin, and the control closes the same goal, its closed status asserted PRESENT
        """A REVISION WITH NO TIME IS AN UNREAD TIME, and a comment the ledger
        records on an earlier rev was answered whatever the clocks say. A
        card at rev 2 whose rev_ts is gone took its current body at a time
        nobody recorded: its filing is when rev 1 took effect, so reading the
        filing as the bound admits his rev-1 comment that rev 2 answered. A
        clock that stepped back stamps rev 2 before that comment. Both refuse
        and write nothing."""
        tid = self.add_goal()["id"]
        cid = self.row(tid)["goal"]["card"]
        with mock.patch.object(pk, "now_ts", return_value=LATER):
            ownerasks.comment_decision(cid, "not yet, add a latency bar",
                                       by=ownerasks.owner_door("web"))
        ref = "card:%s@%s" % (cid, LATER)
        with mock.patch.object(pk, "now_ts",
                               return_value="2099-02-01T00:00:00Z"):
            _row, _n, err = goals.revise(tid, criteria(*ReviseTest.THREE))
        self.assertIsNone(err, err)
        card = self.card(cid)
        self.assertEqual(card["rev"], 2)
        self.assertEqual(card["comments"][-1]["rev"], 1)
        timeless = {k: v for k, v in card.items() if k != "rev_ts"}
        stepped_back = dict(card, rev_ts="2098-01-01T00:00:00Z")
        before = self.ledger_lines()
        for planted in (timeless, stepped_back):
            with mock.patch.object(ownerasks, "decisions_snapshot",
                                   return_value=({cid: planted}, None)):
                row, _n, err = goals.supersede(tid, ref)
            self.assertIsNone(row, planted.get("rev_ts"))
            self.assertIn(ref, err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.row(tid)["status"], "open")
        # the control: his post after rev 2 closes it
        post = self.owner_post("drop it", ts="2099-03-01T00:00:00Z")
        row, _n, err = goals.supersede(tid, post["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")

    def test_a_report_whose_time_does_not_read_reopens_on_nothing(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal five-row tuple, and the control passes the same word against the real report, asserted None
        """THE REPORT BOUND FAILS CLOSED: a report with no time, a boolean or
        a string where the epoch belongs, or no report at all, refuses his
        later word, naming the time as unknown."""
        tid = self.done_goal()
        goal = self.row(tid)["goal"]
        post = self.owner_post("the cycle line is wrong")
        said, err = goals.owner_word(post["id"], goal, "--ref", tid)
        self.assertIsNone(err, err)
        for bad in (None, {}, {"ts": None}, {"ts": True},
                    {"ts": "2026-09-25T00:00:00Z"}):
            err = goals._after_report(said, post["id"], dict(goal, report=bad),
                                      "--ref", tid)
            self.assertIn("reported DONE at unknown", err or "", bad)
            self.assertIn("does not read", err or "", bad)
        self.assertIsNone(goals._after_report(said, post["id"], goal, "--ref",
                                              tid))


class GoalRowPinsTest(GoalBase):
    """THE GOAL GUARD PINS WHAT A GOAL ROW IS, in `tasks._goal_guard` itself
    and not only through the incumbent guard beside it: a goal row is never
    unowned (a change of owner names a successor through the seat-reassign
    or BUILD takeover door), and its origin stays `owner`."""

    def test_a_goal_is_never_left_unowned(self):  # noqa: VACUOUS_ASSERTION — every refusal ends with the unchanged ledger as an exact pin and the named successor's write landing, its owner asserted PRESENT
        from helm import takeover
        tid = self.add_goal()["id"]
        before = self.ledger_lines()
        for owner in ("", None, "UNOWNED"):
            got, err = tasks.update(tid, owner=owner)
            self.assertIsNone(got, owner)
            self.assertIn("never unowned", err)
        rc, _o, err = self.run_task("update", tid, "--owner", "")
        self.assertEqual(rc, 2, err)
        self.assertIn("never unowned", err)
        # A CUSTODY CAPABILITY CANNOT LEAVE IT UNOWNED EITHER: a seat-reassign
        # proof minted for an empty successor satisfies the incumbent guard,
        # and the goal guard still refuses, naming the doors from their table
        got, err = tasks.update(tid, takeover_auth=self.reassign_auth(tid, ""),
                                owner="")
        self.assertIsNone(got)
        self.assertIn("never unowned", err)
        for door in takeover.task_owner_doors(ACCOUNTABLE):
            self.assertIn(door, err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(tasks.owner_of(self.row(tid)), ACCOUNTABLE)
        prev = self.row(tid)
        err, _reopen = tasks._goal_guard(prev, {"owner": ""},
                                         dict(prev, owner=""), None)
        self.assertIsNotNone(err)
        self.assertIn("never unowned", err)
        # the control: a named successor, through the same door, lands
        row, err = tasks.update(tid, takeover_auth=self.reassign_auth(
            tid, OTHER), owner=OTHER)
        self.assertIsNone(err, err)
        self.assertEqual(tasks.owner_of(row), OTHER)

    def test_a_goals_origin_stays_owner(self):  # noqa: VACUOUS_ASSERTION — every refusal ends with the unchanged ledger as an exact pin and the ordinary row's corrected origin asserted PRESENT
        tid = self.add_goal()["id"]
        before = self.ledger_lines()
        rc, _o, err = self.run_task("update", tid, "--origin", "agent")
        self.assertEqual(rc, 2, err)
        self.assertIn("origin stays owner", err)
        got, err = tasks.update(tid, origin="agent",
                                goal_door=goals._door("measure"))
        self.assertIsNone(got)
        self.assertIn("origin stays owner", err)
        prev = self.row(tid)
        err, _reopen = tasks._goal_guard(prev, {"origin": "agent"},
                                         dict(prev, origin="agent"), None)
        self.assertIsNotNone(err)
        self.assertIn("origin stays owner", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(tasks.origin_of(self.row(tid)), "owner")
        # the control: an ordinary row's origin is corrected through this door
        plain, _e = tasks.add("An ordinary engineering row", ACCOUNTABLE,
                              origin="owner", posture_na="test fixture",
                              force_new=True)
        got, err = tasks.update(plain["id"], origin="agent")
        self.assertIsNone(err, err)
        self.assertEqual(got["origin"], "agent")


class RegistryTest(unittest.TestCase):

    def test_the_verb_is_wired_and_documented(self):
        from helm import cli_help
        self.assertIn("goal", cli.VERBS)
        self.assertIn("goal", cli_help._VERB_HELP)
        for sub in goals.SUBVERBS:
            self.assertIn(sub, cli_help._VERB_HELP["goal"])
        with open(os.path.join(REPO, "docs", "VERBS.md"),
                  encoding="utf-8") as f:
            self.assertIn("### `helm goal add", f.read())

    def test_the_card_fits_what_the_web_queue_serves(self):  # noqa: VACUOUS_ASSERTION — an equality between two non-zero constants, the web cap and the card cap
        from helm import web_core
        self.assertEqual(goals.CARD_CONTEXT_MAX, web_core.DECISION_CTX_CAP)

    def test_the_projection_is_a_declared_store(self):
        rows = {r["name"]: r for r in registry.projections()}
        row = rows["goals-projection"]
        self.assertEqual((row["kind"], row["root"], row["rebuild"]),
                         ("projection", "home", "helm goal sync"))
        self.assertIn("_global/" + goals.PROJECTION, row["globs"])

    def test_only_this_module_mints_a_goal_door(self):
        """The census behind "only the goal verbs write a goal record or close
        one": every GoalDoor construction in production code, found by
        parsing the tree."""
        sites = set()
        for base, dirs, files in os.walk(os.path.join(REPO, "helm")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(base, f)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for fn in ast.walk(tree):
                    if not isinstance(fn, ast.FunctionDef):
                        continue
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and (
                                getattr(node.func, "id", None)
                                or getattr(node.func, "attr", None)) in (
                                    "GoalDoor", "_door"):
                            sites.add((os.path.relpath(path, REPO), fn.name,
                                       getattr(node.func, "id", None)
                                       or node.func.attr))
        mints = {s for s in sites if s[2] == "GoalDoor"}
        self.assertEqual(mints, {("helm/goals.py", "_door", "GoalDoor")})
        self.assertEqual({s[0] for s in sites}, {"helm/goals.py"})


if __name__ == "__main__":
    unittest.main()
