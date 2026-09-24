#!/usr/bin/env python3
"""helm.planprompt — the plan-prompt ACTUATOR, the rung that ADVANCES a seat
frozen at an approval prompt.

Detection existed (seat.seat_liveness mints BLOCKED_ON_HUMAN with the plan path
attached), and two rungs correctly refused to resume/compact over it because
that discards the pending plan. Nothing ANSWERED. These tests pin the four
rungs and — the part that makes them worth running — the two MUST-HIT controls:

  * a genuinely OWNER-driven pane still reports BLOCKED_ON_HUMAN and is never
    sent a keystroke. A cure that unblocks everything is the failure mode.
  * a pane that does NOT leave BLOCKED_ON_HUMAN after the choice is reported
    `unproven` and surfaced. The send's own success is the RPC, not the effect.

Every assertion is an EFFECT: the exact keystroke that reached the adapter, the
outcome on the row, or the chat row read back out of a temp room. Hermetic —
HELM_HOME/HELM_CHAT_DIR are temp dirs, liveness is injected, and NO pane is ever
opened. Seat names and plan paths are synthetic fixtures.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, harness, planprompt, seat  # noqa: E402
from helm import seat_lifecycle  # noqa: E402

SEAT = "fixture-seat"
OWNER = "owner-fixture"
CLEAN_PLAN = ("# Plan\n\nAdd a regression test for the delivery lane, then run "
              "the suite and report the token.\n")
GATED_PLAN = ("# Plan\n\nRebase the lane, then git push to origin so the "
              "reviewer can see it.\n")

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_NAME", "MELD_CHAT_NAME")


def blocked(plan=None, options=(("1", "Yes, and bypass permissions"),
                                ("2", "No, keep planning")),
            prompt_type=seat.PLAN_EXECUTION_PROMPT):
    """A BLOCKED_ON_HUMAN liveness row exactly as seat_liveness mints one."""
    return {"seat": SEAT, "state": "BLOCKED_ON_HUMAN",
            "blocked_on": plan or "Do you want to proceed?",
            "evidence": "pane-tail", "detail": None,
            "prompt_type": prompt_type,
            "options": [list(o) for o in options]}


def prompt(plan, options=(("1", "Yes, and bypass permissions"),
                          ("2", "No, keep planning")),
           question=("Claude has written up a plan and is ready to execute. "
                     "Would you like to proceed?")):
    choices = "\n".join("  %s. %s" % o for o in options)
    return ("Ready to code?\nReview the plan at %s\n\n%s\n%s\n"
            "  ⏵⏵ bypass permissions on · 1 monitor"
            % (plan, question, choices))


def running():
    return {"seat": SEAT, "state": "RUNNING", "blocked_on": None,
            "evidence": "pane-tail", "detail": None}


def sequence(*rows):
    """A liveness callable that walks `rows` and then repeats the last one —
    the shape of a real pane observed across a send."""
    box = {"i": 0}

    def live(_name):
        i = min(box["i"], len(rows) - 1)
        box["i"] += 1
        return rows[i]
    return live


class PlanPromptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-planprompt-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""      # set-but-empty: v1, hermetic
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = OWNER
        self.plans = os.path.join(self.tmp, "seats", "plans")
        os.makedirs(self.plans)

    def tearDown(self):
        for k, v in self.prior.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, text=CLEAN_PLAN, name="quiet-hopping-otter.md"):
        p = os.path.join(self.plans, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def room(self):
        return chat.read(room=planprompt.ROOM)[0]


class PlanGateTest(PlanPromptBase):
    """R3: the answer comes from reading the plan, or there is no answer."""

    def test_a_permission_dialog_with_no_plan_is_the_owners(self):
        path, err = planprompt.plan_of("Do you want to proceed?")
        self.assertIsNone(path)
        self.assertIn("named no plan file", err)

    def test_an_arbitrary_md_path_is_not_a_plan(self):
        """seat._PLAN_PATH_RE accepts any `~/….md` in the tail, so a transcript
        that merely mentioned a README would hand this rung an arbitrary file
        to read aloud into chat. Only a plans/ path is a plan."""
        readme = os.path.join(self.tmp, "README.md")
        open(readme, "w").close()
        path, err = planprompt.plan_of(readme)
        self.assertIsNone(path)
        self.assertIn("not a plan path", err)

    def test_a_real_plans_path_is_accepted(self):
        """MUST-HIT control for the refusal above: the SAME shape under a
        plans/ segment resolves, so the refusal is discriminating rather than
        blanket."""
        p = self.plan()
        self.assertEqual(planprompt.plan_of(p), (p, None))

    def test_a_named_plan_that_is_not_on_disk_is_refused(self):
        path, err = planprompt.plan_of(
            os.path.join(self.plans, "never-written.md"))
        self.assertIsNone(path)
        self.assertIn("does not exist", err)

    def test_an_empty_plan_is_not_a_read_plan(self):
        text, err = planprompt.read_plan(self.plan(text="   \n"))
        self.assertIsNone(text)
        self.assertIn("is empty", err)

    def test_every_gated_act_is_refused_by_name(self):
        ok, why = planprompt.plan_verdict("# Plan\n\nthen git push to origin\n")
        self.assertFalse(ok)
        self.assertIn("pushes to a remote", why)   # the refusal NAMES the act
        for text, want in (("finish with helm land", "lands to trunk"),
                           ("create a local commit", "creates a local delivery commit"),
                           ("rm -rf the stale worktree", "destroys working state"),
                           ("then deploy it", "deploys"),
                           ("read the ANTHROPIC_API_KEY", "touches credentials")):
            with self.subTest(text=text):
                ok, why = planprompt.plan_verdict("# Plan\n\n%s\n" % text)
                self.assertFalse(ok)
                self.assertIn(want, why)

    def test_delivery_commit_language_is_owner_gated(self):
        for act in ("git commit -m 'ship it'", "create a local commit",
                    "commit these changes"):
            with self.subTest(act=act):
                ok, why = planprompt.plan_verdict("# Plan\n\n%s\n" % act)
                self.assertFalse(ok)
                self.assertIn("local delivery commit", why)
        self.assertTrue(planprompt.plan_verdict(
            "# Plan\n\nrecord the commit hash in the report\n")[0])

    def test_a_plan_without_the_gated_verb_passes(self):
        """MUTATION: strip the gated act from the same plan and the verdict
        must flip, or the table is decoration."""
        self.assertFalse(planprompt.plan_verdict(GATED_PLAN)[0])
        self.assertTrue(planprompt.plan_verdict(
            GATED_PLAN.replace("git push to origin", "post the diff"))[0])


class AssessTest(PlanPromptBase):
    """R1/R2/R3/R4 as one read-only verdict per seat."""

    def test_a_running_seat_is_not_this_rungs_business(self):
        row = planprompt.assess(SEAT, liveness=lambda _n: running())
        self.assertEqual(row["outcome"], "not-blocked")
        self.assertIsNone(row["choice"])

    def test_an_owner_driven_pane_stays_blocked_and_is_never_answered(self):
        """MUST-HIT CONTROL (#124 is canon: complying destroys his work). The
        refusal must not clear the flag either — the pane still REPORTS
        BLOCKED_ON_HUMAN, it just does not get a keystroke."""
        row = planprompt.assess(OWNER, liveness=lambda _n: dict(
            blocked(plan=self.plan()), seat=OWNER))
        self.assertEqual(row["outcome"], "refused-owner")
        self.assertEqual(row["state"], "BLOCKED_ON_HUMAN")
        self.assertIsNone(row["choice"])
        self.assertIn("OWNER name", row["detail"])

    def test_an_agent_seat_with_the_same_prompt_IS_answerable(self):
        """The discriminator for the control above: identical prompt, identical
        plan, a name that is not the owner's -> answerable."""
        row = planprompt.assess(SEAT, liveness=lambda _n: blocked(self.plan()))
        self.assertEqual(row["outcome"], "answer")
        self.assertEqual(row["choice"], "1")

    def test_a_permission_dialog_quoting_the_same_plan_is_never_answered(self):
        """MUTATION: delete the prompt-type guard and this permission question
        becomes `answer` because its real path, clean plan and Yes choice all
        pass. Path location is evidence, not permission to impersonate a human.
        The genuine built-in dialog with the SAME path is the must-hit control.
        """
        p = self.plan()
        permission = planprompt.assess(SEAT, liveness=lambda _n: blocked(
            p, prompt_type=seat.PERMISSION_PROMPT))
        genuine = planprompt.assess(SEAT, liveness=lambda _n: blocked(p))
        self.assertEqual(permission["outcome"], "surface")
        self.assertIsNone(permission["choice"])
        self.assertIn("human permission prompt", permission["detail"])
        self.assertEqual((genuine["outcome"], genuine["choice"]),
                         ("answer", "1"))

    def test_an_untyped_blocked_row_fails_closed(self):
        """Old/corrupt cached rows cannot inherit answer authority by omission."""
        p = self.plan()
        row = blocked(p)
        row.pop("prompt_type")
        got = planprompt.assess(SEAT, liveness=lambda _n: row)
        self.assertEqual(got["outcome"], "surface")
        self.assertIn("not typed by the pane classifier", got["detail"])

    def test_a_prompt_with_no_plan_surfaces_instead_of_guessing(self):
        row = planprompt.assess(SEAT, liveness=lambda _n: blocked())
        self.assertEqual(row["outcome"], "surface")
        self.assertIsNone(row["choice"])

    def test_a_gated_plan_surfaces_and_carries_its_text(self):
        row = planprompt.assess(
            SEAT, liveness=lambda _n: blocked(self.plan(text=GATED_PLAN)))
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("pushes to a remote", row["detail"])
        self.assertIn("git push to origin", row["plan_text"])

    def test_a_crashing_probe_blinds_one_seat_and_not_the_sweep(self):
        """A rescue that dies on seat A never reaches seat B — the same shape
        as a watchdog going blind at exactly its target state."""
        def live(name):
            if name == "exploding-seat":
                raise RuntimeError("pane inventory exploded")
            return blocked(self.plan())

        rows = [planprompt.assess(n, liveness=live)
                for n in ("exploding-seat", SEAT)]
        self.assertEqual([r["outcome"] for r in rows], ["blind", "answer"])
        self.assertIn("probe crashed", rows[0]["detail"])

    def test_a_dialog_with_no_readable_yes_is_never_guessed_at(self):
        """helm does not type a digit it did not read: a dialog whose options
        did not parse is surfaced, not answered with a hopeful `1`."""
        row = planprompt.assess(SEAT, liveness=lambda _n: dict(
            blocked(self.plan()), options=[]))
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("no readable `Yes` choice", row["detail"])
        self.assertIsNone(row["choice"])


class CleanBillTest(unittest.TestCase):
    """A no-findings line is only as wide as what the pass could see."""

    def test_a_clean_bill_names_the_panes_it_could_not_read(self):
        rows = [{"seat": "a", "state": "RUNNING", "outcome": "not-blocked"},
                {"seat": "b", "state": "UNKNOWN", "outcome": "not-blocked"},
                {"seat": "c", "state": "UNKNOWN", "outcome": "blind"}]
        line = planprompt.clean_bill(rows)
        self.assertIn("not a clean bill", line)
        self.assertIn("b, c", line)
        self.assertIn("1 readable", line)

    def test_an_all_readable_pass_says_so_plainly(self):
        """The discriminator: with nothing unread the qualifier must NOT
        appear, or the warning is noise nobody reads."""
        line = planprompt.clean_bill(
            [{"seat": "a", "state": "RUNNING", "outcome": "not-blocked"}])
        self.assertEqual(line, "no seat is blocked on a human prompt (1 scanned)")


class ActuateTest(PlanPromptBase):
    """R4: the keystroke reaches the registered pane, and the EFFECT is proved
    by re-measuring the state — never by the send returning."""

    def pane(self):
        ad = mock.Mock()
        ad.name = "orca"
        return ad

    def actuate(self, ad, live, tail=None, mutate_plan=None):
        p = self.plan()
        row = planprompt.assess(SEAT, liveness=lambda _n: blocked(p))
        self.assertEqual(row["outcome"], "answer")     # control: R1-R3 passed
        if mutate_plan:
            mutate_plan(p)
        ad.read.return_value = prompt(p) if tail is None else \
            (tail(p) if callable(tail) else tail)
        with mock.patch.object(seat, "_seat_family", return_value=("codex", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_fixture", "registered")):
            return planprompt.act(row, liveness=live, sleep=lambda _s: None)

    def test_the_read_choice_reaches_the_registered_pane(self):
        ad = self.pane()
        row = self.actuate(ad, sequence(blocked(), running()))
        ad.send.assert_called_once_with("term_fixture", "1", enter=True)
        self.assertEqual(row["outcome"], "answered")
        self.assertIn("left BLOCKED_ON_HUMAN", row["detail"])

    def test_a_prompt_that_advanced_before_the_lock_receives_no_keystroke(self):
        ad = self.pane()
        row = self.actuate(
            ad, sequence(running()),
            tail="done, parked\n❯\n  ⏵⏵ bypass permissions on · 1 monitor")
        ad.send.assert_not_called()
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("changed before send", row["detail"])

    def test_a_permission_dialog_replacing_the_plan_prompt_gets_no_keystroke(self):
        ad = self.pane()
        row = self.actuate(
            ad, sequence(running()),
            tail=lambda p: prompt(
                p, question="May I create a local commit after reading this plan?"))
        ad.send.assert_not_called()
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("not Claude Code's plan-execution prompt", row["detail"])

    def test_a_changed_affirmative_choice_gets_no_keystroke(self):
        ad = self.pane()
        row = self.actuate(
            ad, sequence(running()),
            tail=lambda p: prompt(
                p, options=(("1", "No, keep planning"),
                            ("2", "Yes, and bypass permissions"))))
        ad.send.assert_not_called()
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("affirmative choice", row["detail"])

    def test_a_plan_file_changed_after_assessment_gets_no_keystroke(self):
        ad = self.pane()

        def rewrite(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(GATED_PLAN)

        row = self.actuate(ad, sequence(running()), mutate_plan=rewrite)
        ad.send.assert_not_called()
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("plan contents no longer match", row["detail"])

    def test_a_pane_that_never_advances_is_unproven_not_answered(self):
        """MUST-HIT CONTROL / MUTATION: hold the pane at BLOCKED_ON_HUMAN and
        the SAME successful send must stop reading as success. This is the
        whole difference between actuating and hoping."""
        ad = self.pane()
        row = self.actuate(ad, sequence(blocked()))
        ad.send.assert_called_once_with("term_fixture", "1", enter=True)
        self.assertEqual(row["outcome"], "unproven")
        self.assertIn("the send is not the effect", row["detail"])

    def test_an_unreadable_pane_after_the_send_never_counts_as_advanced(self):
        """UNKNOWN is not proof: a pane we cannot read is not a pane that
        moved."""
        unknown = {"seat": SEAT, "state": "UNKNOWN", "blocked_on": None,
                   "evidence": "read-failed", "detail": None}
        row = self.actuate(self.pane(), sequence(unknown))
        self.assertEqual(row["outcome"], "unproven")

    def test_a_refused_send_surfaces_rather_than_claiming_an_answer(self):
        ad = self.pane()
        ad.send.side_effect = harness.HarnessError("pane is gone")
        row = self.actuate(ad, sequence(running()))
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("could not be delivered", row["detail"])

    def test_an_unresolvable_register_stops_the_keystroke(self):
        """The spawn register is the authority every pane actuator shares; no
        register, no write."""
        with mock.patch.object(seat, "_seat_family", return_value=("codex", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(None, None, "stale handle")):
            ok, detail = planprompt._send_choice(SEAT, "1")
        self.assertFalse(ok)
        self.assertEqual(detail, "stale handle")


class StaleMenuTest(PlanPromptBase):
    """The pane-changed guard at R4 re-reads the pane through the SAME helper
    the assessment used (`seat._prompt_options` via `affirmative_choice`), so a
    helper that resurrected an answered menu out of scrollback made the guard
    AGREE with the stale reading — and the digit went into whatever owned input
    now. Measured before the cure, end to end: a real plan-execution prompt with
    a newer non-qualifying numbered list below it still classified
    BLOCKED_ON_HUMAN with the same plan path, and `1` plus Enter was sent.

    BOTH ARMS DRIVE THE SHIPPED PRODUCER: the liveness row is minted by
    `seat_lifecycle._seat_liveness_row` FROM THE PANE TEXT, never hand-written,
    and the only double is the adapter, which RECORDS what would be typed. No
    pane is opened.
    """

    NEWER_LIST = ("\n...work happened...\n\nPick a target to inspect:\n"
                  "  1. src/main.py\n  2. src/util.py\n  3. tests/\n")

    def pane(self, tail):
        ad = mock.Mock()
        ad.name = "orca"
        ad.read.return_value = tail
        return ad

    def minted(self, ad):
        """The liveness row seat_lifecycle mints from the adapter's pane text."""
        with mock.patch.object(seat_lifecycle, "_seat_family",
                               return_value=("codex", None)), \
             mock.patch.object(seat_lifecycle, "_spawn_record",
                               return_value={"harness": "orca"}), \
             mock.patch.object(seat_lifecycle, "_resolve_registered_pane",
                               return_value=(ad, "term_fixture", "registered")):
            return seat_lifecycle._seat_liveness_row(SEAT)

    def registered(self, ad):
        return (mock.patch.object(seat, "_seat_family",
                                  return_value=("codex", None)),
                mock.patch.object(seat, "_resolve_registered_pane",
                                  return_value=(ad, "term_fixture",
                                                "registered")))

    def test_the_newest_run_being_the_menu_is_what_sends_the_digit(self):
        """THE SIBLING CONTROL, and it is what makes the refusal below mean
        something: the same fixture, the same minted row, the same send path —
        with the plan prompt as the NEWEST numbered run the digit IS typed."""
        p = self.plan()
        ad = self.pane(prompt(p))
        lv = self.minted(ad)
        self.assertEqual(lv["state"], planprompt.BLOCKED)
        self.assertEqual(lv["prompt_type"], seat.PLAN_EXECUTION_PROMPT)
        row = planprompt.assess(SEAT, liveness=lambda _n: lv)
        self.assertEqual(row["outcome"], "answer")
        fam, reg = self.registered(ad)
        with fam, reg:
            row = planprompt.act(row, liveness=sequence(running()),
                                 sleep=lambda _s: None)
        ad.send.assert_called_once_with("term_fixture", "1", enter=True)
        self.assertEqual(row["outcome"], "answered")

    def test_a_newer_numbered_list_below_the_prompt_gets_no_keystroke(self):
        """THE DEFECT ARM. The pane still classifies BLOCKED_ON_HUMAN with the
        same plan path — that is why this was reachable — but the menu is no
        longer the newest numbered run, so the producer hands the actuator NO
        options, assess surfaces instead of answering, and the guard refuses the
        stale choice even when a caller arrives holding it. Asserted at the
        adapter: nothing was typed."""
        p = self.plan()
        ad = self.pane(prompt(p) + self.NEWER_LIST)
        lv = self.minted(ad)
        # Reachability, pinned: the state and the plan authority are UNCHANGED
        # by the newer list. Only the options are gone.
        self.assertEqual(lv["state"], planprompt.BLOCKED)
        self.assertEqual(lv["prompt_type"], seat.PLAN_EXECUTION_PROMPT)
        self.assertEqual(lv["blocked_on"], p)
        self.assertEqual(lv["options"], [])
        row = planprompt.assess(SEAT, liveness=lambda _n: lv)
        self.assertEqual(row["outcome"], "surface")
        self.assertIn("no readable `Yes` choice", row["detail"])
        # And the guard refuses the stale choice even when a caller arrives
        # holding it, instead of agreeing with the reading that produced it.
        fam, reg = self.registered(ad)
        with fam, reg:
            ok, detail = planprompt._send_choice(
                SEAT, "1", plan=p, plan_text=CLEAN_PLAN,
                choice_label="Yes, and bypass permissions")
        self.assertFalse(ok)
        self.assertIn("changed before send", detail)
        self.assertIn("affirmative choice", detail)
        ad.send.assert_not_called()
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional and on the
        # same adapter object: drop the newer list from the pane and the SAME
        # call types the digit. So the silence above is the rule firing, not a
        # send path that is inert on this harness.
        ad.read.return_value = prompt(p)
        with fam, reg:
            ok, detail = planprompt._send_choice(
                SEAT, "1", plan=p, plan_text=CLEAN_PLAN,
                choice_label="Yes, and bypass permissions")
        self.assertTrue(ok, detail)
        ad.send.assert_called_once_with("term_fixture", "1", enter=True)


class PassTest(PlanPromptBase):
    """One pass, end to end, asserted on the chat rows a human would read."""

    def run_pass(self, live, seats=(SEAT,), pane_plan=None, **kw):
        ad = mock.Mock()
        ad.name = "orca"
        ad.read.return_value = prompt(pane_plan) if pane_plan else ""
        with mock.patch.object(seat, "_seat_family", return_value=("codex", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_fixture", "registered")):
            res = planprompt.check(seats=list(seats), liveness=live,
                                   sleep=lambda _s: None, **kw)
        return res, ad

    def test_a_surfaced_block_reaches_the_owner_carrying_the_plan(self):  # noqa: VACUOUS_ASSERTION — `ad.send` is proven to record on this exact harness by test_an_answered_prompt_leaves_an_audit_row, which drives the same run_pass with the same mock; its silence HERE is the product law
        """The row a human reads is the deliverable — without the plan text he
        still has to open the pane, which is the manual step this deletes."""
        p = self.plan(text=GATED_PLAN)
        res, ad = self.run_pass(lambda _n: blocked(p))
        ad.send.assert_not_called()
        rows = self.room()
        self.assertEqual(len(rows), 1, rows)
        text = rows[0]["text"]
        self.assertIn(SEAT, text)
        self.assertIn("git push to origin", text)      # the plan itself
        self.assertIn("@", text)                       # addressed to the owner
        self.assertEqual([r["seat"] for r in res["surfaced"]], [SEAT])

    def test_an_answered_prompt_leaves_an_audit_row(self):
        p = self.plan()
        res, ad = self.run_pass(sequence(blocked(p), running()), pane_plan=p)
        ad.send.assert_called_once_with("term_fixture", "1", enter=True)
        self.assertEqual([r["seat"] for r in res["answered"]], [SEAT])
        text = self.room()[0]["text"]
        self.assertIn(SEAT, text)
        self.assertIn(p, text)
        self.assertIn("choice 1", text)

    def test_the_owner_pane_is_skipped_while_the_agent_pane_is_answered(self):  # noqa: VACUOUS_ASSERTION — the discriminating control is IN this test and unconditional: the agent pane in the same sweep records exactly one send and one answered row, so the owner pane's silence is a difference this pass measured, not an absence nothing produced
        """MUST-HIT CONTROL at pass scope: both panes are blocked on the same
        plan in the same sweep, and exactly one keystroke is sent."""
        p = self.plan()
        agent = sequence(blocked(p), running())

        def live(name):
            if name == OWNER:                     # his pane never advances
                return dict(blocked(p), seat=OWNER)
            return dict(agent(name), seat=SEAT)

        res, ad = self.run_pass(live, seats=(OWNER, SEAT), pane_plan=p)
        self.assertEqual(ad.send.call_count, 1)
        self.assertEqual([r["seat"] for r in res["answered"]], [SEAT])
        owner_row = next(r for r in res["rows"] if r["seat"] == OWNER)
        self.assertEqual(owner_row["outcome"], "refused-owner")
        self.assertEqual(owner_row["state"], "BLOCKED_ON_HUMAN")
        self.assertNotIn(OWNER, "".join(m["text"] for m in self.room()))

    def test_the_same_block_surfaces_once_and_a_new_plan_surfaces_again(self):
        """The latch stops a frozen seat re-crying the SAME block; it must not
        hide a new one."""
        p = self.plan()
        self.run_pass(lambda _n: blocked(p))
        self.run_pass(lambda _n: blocked(p))
        latched = self.room()
        self.assertEqual([m["from"] for m in latched], [planprompt.WHO])
        p2 = self.plan(name="brisk-tumbling-vole.md")
        self.run_pass(lambda _n: blocked(p2))
        after = self.room()
        self.assertEqual([m["from"] for m in after],
                         [planprompt.WHO, planprompt.WHO])
        self.assertIn(p2, after[1]["text"])

    def test_a_dry_run_writes_nothing_and_does_not_spend_the_latch(self):  # noqa: VACUOUS_ASSERTION — the positive control is IN this test and unconditional: the identical REAL pass at the end posts the row and writes the latch path this dry run left absent, so every absence above is a measured difference
        """A simulation that stamps the latch suppresses the next REAL alert —
        the defect silent_drop already paid for."""
        p = self.plan()
        res, ad = self.run_pass(lambda _n: blocked(p), dry=True)
        ad.send.assert_not_called()
        self.assertEqual(self.room(), [])
        self.assertEqual(res["would"], [SEAT])
        self.assertFalse(os.path.exists(planprompt._state_path()))
        self.run_pass(lambda _n: blocked(p))           # the real pass still speaks
        self.assertEqual([m["from"] for m in self.room()], [planprompt.WHO])
        self.assertTrue(os.path.exists(planprompt._state_path()))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# THE PROMPT-STALL WATCH — a native seat parked at a Claude Code prompt
# ---------------------------------------------------------------------------

NATIVE = "native-fixture-2"
INTEGRATOR = "integrator-fixture"
STALL_SID = "cccccccc-1111-2222-3333-444444444444"
T0 = 1790232425.0          # the incident's shape: parked at 06:47:05Z


def native_row(pid=4242, name=NATIVE, child=False):
    return {"pid": pid, "root": "/homes/fixture", "start": "1000",
            "child": child,
            "environ": {"HELM_CHAT_NAME": name} if name else {}}


def presence(status="waiting", waiting_for="permission prompt", since=T0):
    return {"pid": 4242, "sessionId": STALL_SID, "procStart": "1000",
            "status": status, "waitingFor": waiting_for,
            "statusUpdatedAt": int(since * 1000)}


def stalls_at(now, rec, rows=None, sids=None):
    return planprompt.prompt_stalls(
        census={"rows": rows or [native_row()], "listing_failed": False},
        now=now, read=lambda _r, _p, _s: (rec, "record-ok"),
        sids=sids or {}, owners={OWNER})


class PromptStallFindTest(unittest.TestCase):
    """The detector reads the vendor's own presence record, so a NATIVE seat —
    which `seats_to_scan` never lists — is seen."""

    def test_the_proxy_scan_set_is_blind_to_the_native_seat_this_watch_sees(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn is the miss itself; the stall row naming the same seat below is the positive control
        """THE MISS, pinned from both sides: the actuator's scan set is the
        proxy seats, and the watch names the native seat it cannot reach."""
        with mock.patch("helm.autocompact.proxy_seats", return_value=["codex"]):
            self.assertNotIn(NATIVE, planprompt.seats_to_scan())
        got = stalls_at(T0 + 300, presence())
        self.assertEqual([r["seat"] for r in got["stalls"]], [NATIVE])
        self.assertEqual(got["stalls"][0]["waiting_for"], "permission prompt")
        self.assertEqual(got["stalls"][0]["waited_s"], 300)

    def test_a_short_wait_and_a_busy_seat_are_not_stalls(self):
        """CONTROLS on the empty answer: the same seat inside the threshold,
        and a seat parked just as long but BUSY, both read clean — while the
        arm above proves this reader does emit a stall."""
        self.assertEqual(stalls_at(T0 + 60, presence())["stalls"], [])
        self.assertEqual(stalls_at(T0 + 3600, presence(status="busy"))["stalls"],
                         [])
        self.assertEqual(stalls_at(T0 + 300, presence())["sessions"], 1)

    def test_a_seat_the_roster_holds_by_session_is_named(self):
        got = stalls_at(T0 + 900, presence(), rows=[native_row(name=None)],
                        sids={"roster-named": STALL_SID})
        self.assertEqual([r["seat"] for r in got["stalls"]], ["roster-named"])

    def test_an_unnamed_session_is_counted_and_the_owner_is_not_paged(self):
        got = stalls_at(T0 + 900, presence(), rows=[native_row(name=None),
                                                    native_row(pid=7, name=OWNER)])
        self.assertEqual((got["stalls"], got["unnamed"]), ([], 1))
        self.assertEqual(got["sessions"], 2)     # both records were READ

    def test_a_junk_stamp_is_never_a_stall(self):
        rec = dict(presence(), statusUpdatedAt=True)
        self.assertEqual(stalls_at(T0 + 900, rec)["stalls"], [])
        self.assertIn("statusUpdatedAt", stalls_at(T0 + 900, rec)["blind"][0]["why"])
        # CONTROL: the same record with a real stamp IS a stall.
        self.assertEqual(len(stalls_at(T0 + 900, presence())["stalls"]), 1)

    def test_a_failed_listing_is_unknown_not_clean(self):
        got = planprompt.prompt_stalls(census={"rows": [], "listing_failed": True},
                                       now=T0, sids={}, owners=set())
        self.assertFalse(got["read"])
        self.assertIn("enumeration failed", got["why"])


class PromptStallBlindTest(unittest.TestCase):
    """UNREADABLE IS NOT EMPTY. A presence record that cannot answer is a
    BLIND session with its reason — never not-stalled, never uncounted — and
    never a stall either. The real-file arms go through the REAL bracketed
    reader; the reason sweep injects each refusal the reader can return."""

    REASONS = ("record-missing", "record-unreadable", "record-unsafe",
               "record-replaced", "record-corrupt", "record-schema",
               "record-stale")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-presence-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "sessions"))
        self.path = os.path.join(self.root, "sessions", "4242.json")

    def write(self, body):
        import json
        with open(self.path, "w") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def real(self, now=T0 + 300):
        row = dict(native_row(), root=self.root)
        return planprompt.prompt_stalls(
            census={"rows": [row], "listing_failed": False}, now=now,
            sids={}, owners={OWNER})

    def test_MUST_HIT_a_readable_waiting_file_still_stalls(self):
        self.write(presence())
        got = self.real()
        self.assertEqual([r["seat"] for r in got["stalls"]], [NATIVE])
        self.assertEqual((got["sessions"], got["blind"]), (1, []))

    def test_a_missing_file_is_blind(self):
        got = self.real()
        self.assertEqual((got["stalls"], got["sessions"]), ([], 0))
        self.assertEqual(got["blind"], [{"seat": NATIVE, "pid": 4242,
                                         "why": "record-missing"}])

    def test_a_stale_generation_is_blind(self):
        self.write(dict(presence(), procStart="999"))
        self.assertEqual(self.real()["blind"][0]["why"], "record-stale")

    def test_a_corrupt_file_is_blind(self):
        self.write("{not json")
        self.assertEqual(self.real()["blind"][0]["why"], "record-corrupt")

    def test_a_schema_changed_file_is_blind(self):
        rec = presence()
        del rec["sessionId"]
        self.write(rec)
        self.assertEqual(self.real()["blind"][0]["why"], "record-schema")

    def test_an_unsafe_file_is_blind(self):
        self.write(presence())
        os.chmod(self.path, 0o666)
        self.assertEqual(self.real()["blind"][0]["why"], "record-unsafe")

    def test_every_reader_refusal_is_blind_with_its_reason(self):  # noqa: VACUOUS_ASSERTION — the loop is over a fixed non-empty tuple and each subTest asserts the named reason; test_MUST_HIT_a_readable_waiting_file_still_stalls is the positive control
        for why in self.REASONS:
            with self.subTest(reason=why):
                got = planprompt.prompt_stalls(
                    census={"rows": [native_row()], "listing_failed": False},
                    now=T0 + 300, read=lambda _r, _p, _s, w=why: (None, w),
                    sids={}, owners={OWNER})
                self.assertEqual((got["stalls"], got["sessions"]), ([], 0))
                self.assertEqual(got["blind"][0]["why"], why)

    def test_a_status_this_watch_does_not_know_is_blind(self):  # noqa: VACUOUS_ASSERTION — a fixed two-value loop, each asserting the blind reason; the MUST-HIT arm is the positive control
        for status in ("needs_user", None):
            with self.subTest(status=status):
                got = stalls_at(T0 + 900, dict(presence(), status=status))
                self.assertEqual(got["stalls"], [])
                self.assertIn("status", got["blind"][0]["why"])

    def test_the_pass_names_blind_sessions_UNKNOWN_and_never_clean(self):  # noqa: VACUOUS_ASSERTION — a fixed two-value loop whose every pass asserts the UNKNOWN line's text
        found = {"read": True, "why": None, "sessions": 0, "stalls": [],
                 "unnamed": 0,
                 "blind": [{"seat": NATIVE, "pid": 4242, "why": "record-missing"},
                           {"seat": None, "pid": 7, "why": "record-stale"}]}
        for dry in (True, False):
            with self.subTest(dry=dry):
                res = planprompt.stall_pass(now=T0, found=found, dry=dry,
                                            post=False, memory={})
                text = " ".join(res["lines"])
                self.assertIn("2 session(s) UNKNOWN", text)
                self.assertIn("seat %s (record-missing)" % NATIVE, text)
                self.assertIn("pid 7 (record-stale)", text)

    def test_unblock_prints_UNKNOWN_instead_of_the_clean_bill(self):
        import io
        import contextlib
        found = {"read": True, "why": None, "sessions": 0, "stalls": [],
                 "unnamed": 0,
                 "blind": [{"seat": NATIVE, "pid": 4242, "why": "record-missing"}]}
        clean = {"rows": [{"seat": "codex", "state": "IDLE",
                           "outcome": "not-blocked", "detail": "state is IDLE"}],
                 "answered": [], "surfaced": [], "would": []}
        out = io.StringIO()
        with mock.patch.object(planprompt, "check", return_value=clean), \
             mock.patch.object(planprompt, "prompt_stalls", return_value=found), \
             contextlib.redirect_stdout(out):
            planprompt.cmd_unblock(["--dry-run"])
        self.assertIn("UNKNOWN", out.getvalue())
        self.assertNotIn("no seat is blocked", out.getvalue())
        # CONTROL: the same world with a readable fleet DOES print the bill.
        out = io.StringIO()
        with mock.patch.object(planprompt, "check", return_value=clean), \
             mock.patch.object(planprompt, "prompt_stalls",
                               return_value=dict(found, blind=[])), \
             contextlib.redirect_stdout(out):
            planprompt.cmd_unblock(["--dry-run"])
        self.assertIn("no seat is blocked", out.getvalue())


class PromptStallPassTest(PlanPromptBase):
    """End to end, asserted on the chat rows a human reads and on the push."""

    def run_stall(self, now, rec=None, dry=False, post_ok=True, memory=None):
        found = stalls_at(now, rec or presence())
        with mock.patch("helm.notify.owner_push", return_value=True) as push, \
             mock.patch("helm.seats.owner_name", return_value=OWNER), \
             mock.patch("helm.seats_integrator.integrator_seat",
                        return_value=(INTEGRATOR, None)), \
             mock.patch.object(planprompt, "_post_stall",
                               wraps=planprompt._post_stall) as posted:
            if not post_ok:
                posted.side_effect = lambda *_a: False
            res = planprompt.stall_pass(now=now, found=found, dry=dry,
                                        memory=memory or {})
        return res, push

    def test_a_stall_reaches_the_owner_and_integrator_loudly(self):
        res, push = self.run_stall(T0 + 300)
        rows = self.room()
        self.assertEqual(len(rows), 1, rows)
        text = rows[0]["text"]
        for want in ("@%s" % OWNER, "@%s" % INTEGRATOR, NATIVE, "FROZEN",
                     "permission prompt", "06:47Z", "answer it"):
            self.assertIn(want, text)
        self.assertEqual(rows[0]["from"], planprompt.STALL_WHO)
        push.assert_called_once()
        self.assertIn(NATIVE, push.call_args[0][0])
        self.assertEqual([r["seat"] for r in res["posted"]], [NATIVE])

    def test_one_episode_posts_once_then_again_only_after_the_repeat(self):
        self.run_stall(T0 + 300)
        self.run_stall(T0 + 600)
        self.assertEqual(len(self.room()), 1)
        self.run_stall(T0 + 300 + planprompt.STALL_REPEAT_S)
        self.assertEqual(len(self.room()), 2)

    def test_a_new_episode_posts_at_once(self):
        self.run_stall(T0 + 300)
        self.run_stall(T0 + 700, rec=presence(since=T0 + 400))
        self.assertEqual(len(self.room()), 2)

    def test_a_failed_post_is_not_latched_and_re_surfaces(self):  # noqa: VACUOUS_ASSERTION — the push silence is the claim; the re-post read back from the room is the positive control
        res, push = self.run_stall(T0 + 300, post_ok=False)
        self.assertEqual(res["posted"], [])
        push.assert_not_called()
        self.assertTrue(any("FAILED" in line for line in res["lines"]))
        self.run_stall(T0 + 600)
        self.assertEqual(len(self.room()), 1)

    def test_a_dry_run_posts_and_latches_nothing(self):  # noqa: VACUOUS_ASSERTION — the silence is the claim; the dry line naming the seat and the later real post are the positive controls
        res, push = self.run_stall(T0 + 300, dry=True)
        push.assert_not_called()
        self.assertEqual(self.room(), [])
        self.assertTrue(any(NATIVE in line for line in res["lines"]))
        self.run_stall(T0 + 600)                 # nothing latched: it posts
        self.assertEqual(len(self.room()), 1)

    def test_the_relaunch_rides_the_row_when_the_session_predates_the_base(self):
        self.run_stall(T0 + 300, memory={4242: "RELAUNCH-LINE-FIXTURE"})
        text = self.room()[0]["text"]
        self.assertIn("RELAUNCH-LINE-FIXTURE", text)
        self.assertIn("--resume", text)


class PresenceReaderTest(unittest.TestCase):
    """The bracketed reader returns the WHOLE record, still bound to the pid
    generation, and the session-id door built on it is unchanged."""

    def test_the_whole_record_comes_back_and_a_stale_generation_does_not(self):
        import json
        from helm import session
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "sessions"))
            with open(os.path.join(root, "sessions", "4242.json"), "w") as f:
                json.dump(presence(), f)
            uid = os.geteuid()
            rec, why = session.read_session_presence(root, 4242, uid, "1000")
            self.assertEqual((why, rec["status"], rec["waitingFor"]),
                             ("record-ok", "waiting", "permission prompt"))
            self.assertEqual(session.read_session_presence(root, 4242, uid, "9"),
                             (None, "record-stale"))
            self.assertEqual(session._read_session_record(root, 4242, uid, "1000"),
                             (STALL_SID, "record-ok"))
