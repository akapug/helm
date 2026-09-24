#!/usr/bin/env python3
"""A RULE ARRIVES WITH ITS GATE (task/1346) — the `gates:` link.

THE DEFECT: the gate premise ("two gates must pass before a PR is considered")
was injected at 03:43 on unrelated vocabulary and did NOT surface at 11:30
when "upstream PR" was typed into a task note; the fork-is-PR-staging default
fired as a conclusion with its precondition in a different entry keyed on
different words. Keyword injection fires on vocabulary, decisions on moments.

THE MECHANISM under test: a premise names its PRECONDITIONS in `gates:`;
(a) when the injector selects it, each gate's gloss rides in the SAME whisper
marked "GATE of <rule>", budget permitting, and a gate the budget cannot carry
is dropped LOUDLY with a marker; (b) rule and gate share probe vocabulary, so
either side's words pull both; the verb `helm store gates <id> --add CSV` is
the keywords verb's sibling.

Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR / HELM_CHAT_DIR are tmp
dirs — the real store is never read or written. Each arm names the mutation
it kills.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import home, inject, pk, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_CF_TOKEN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_AGENT_HARNESS",
            "PI_CODING_AGENT", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
            "CLAUDE_PID", "CLAUDE_CONFIG_DIR", "CODEX_HOME")

TS = "2026-08-22T12:00:00Z"
RULE = "fork-is-pr-staging"
GATE = "two-gates-before-a-pr"
GATE2 = "orca-stays-a-black-box"
ROUTE = "t"   # the notice kind these arms route a rule by (task/2980)


@contextlib.contextmanager
def routed_turn():
    """The turn arrives as a notice of kind ROUTE, so an entry declaring
    `notice:ROUTE` is a DETERMINISTIC delivery: the only line a rider rides on
    (task/2980). A keyword line names its gates in the lane footer instead."""
    from helm import promptshape
    with mock.patch.object(promptshape, "notice_kinds",
                           lambda _prompt: frozenset({ROUTE})):
        yield


def run_store(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gates-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, pid, statement, keywords, conf="1.0", **kw):
        e = {"id": pid, "statement": statement, "confidence": conf,
             "keywords": keywords, "stated_ts": TS, "source": "human"}
        e.update(kw)
        return store.write_prior(e)

    def plant_pair(self):
        """The specimen, shrunk: the rule on the fork/staging words, the gate
        on upstream/pr/culture — disjoint vocabularies, linked by gates:."""
        self.plant(RULE, "a fork is a PR staging ground, not a discard",
                   "fork,staging,ember")
        self.plant(GATE, "two gates pass before a PR is even considered: "
                   "proven in our use, and the project welcomes it",
                   "upstream,pr,culture")
        e, err = store.regate(RULE, TS, add=GATE)
        self.assertIsNone(err)
        return e

    def plant_outranked(self):
        """The pair plus three stronger entries, so the RULE sits at rank 4
        (newer than the gate on the tie) and the GATE at rank 5 — a gate that
        did NOT earn a slot and rides only as the rule's rider. -> the prompt.
        A gate that shares the rule's vocabulary ranks beside it, so this is
        the only shape in which the JIT rider budget is exercised."""
        self.plant(RULE, "a fork is a PR staging ground, not a discard",
                   "fork,staging,ember")
        self.plant(GATE, "two gates pass before a PR is even considered: "
                   "proven in our use, and the project welcomes it",
                   "upstream,pr,culture")
        _e, err = store.regate(RULE, "2026-08-22T13:00:00Z", add=GATE)
        self.assertIsNone(err)
        words = []
        for i in range(3):
            kws = ["w%d%d" % (i, j) for j in range(3)]
            words += kws
            self.plant("strong-%d" % i, "strong fact %d" % i, ",".join(kws))
        return "time to fork the staging repo " + " ".join(words)

    def entry(self, eid):
        hits = [e for e in store.load_all() if e["id"] == eid]
        self.assertEqual(len(hits), 1, eid)
        return hits[0]

    def route(self, eid):
        """Declare `notice:ROUTE` on a prior, past the verb: under
        routed_turn() it is then a deterministic delivery."""
        e = self.entry(eid)
        e["keywords"] = e["keywords"] + ",notice:" + ROUTE
        store.write_prior(e, path=e["path"])


class VocabularyTest(GateBase):
    def test_the_rules_words_pull_the_gate_as_strongly_as_the_rule(self):
        # kills: link_gates one-directional (rule borrows, gate does not), or
        # _probes not folding gate_probes at all
        self.plant_pair()
        got = [e["id"] for e in store.resolve_prompt("time to fork the staging repo")]
        self.assertIn(RULE, got)
        self.assertIn(GATE, got, "the gate must be reached from the RULE's words")
        # and the gate's words pull the rule — symmetric on purpose
        got = [e["id"] for e in store.resolve_prompt("an upstream pr and its culture")]
        self.assertIn(GATE, got)
        self.assertIn(RULE, got)
        # as strongly: identical per-probe weights, identical score
        entries = store.load_all()
        df = store._df_map(store._jit_candidates(entries))
        low = "time to fork the staging repo"
        r, g = self.entry(RULE), self.entry(GATE)
        self.assertEqual(store._probe_hits(r, low)[2], store._probe_hits(g, low)[2])
        self.assertEqual(inject._jit_score(r, low, df), inject._jit_score(g, low, df))

    def test_an_unlinked_entry_has_no_borrowed_probes(self):
        # must-miss control on the fold: without gates:, nothing changes
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        got = [e["id"] for e in store.resolve_prompt("time to fork the staging repo")]
        self.assertEqual(got, [RULE])
        self.assertNotIn("gate_probes", self.entry(RULE))
        store.regate(RULE, TS, add=GATE)                # the positive control
        self.assertEqual(self.entry(RULE)["gate_probes"], GATE + ",upstream,pr,culture")

    def test_linking_does_not_dilute_the_df_weight(self):
        # kills: _df_map counting the folded probes — the rule's rare word
        # would read df=2 and every linked pair would rank itself down
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        before = store._df_map(store._jit_candidates(store.load_all()))
        store.regate(RULE, TS, add=GATE)
        after = store._df_map(store._jit_candidates(store.load_all()))
        self.assertEqual(before["staging"], 1)
        self.assertEqual(after["staging"], 1, "a borrowed probe is not a second document")
        self.assertEqual(after["culture"], 1)

    def test_a_retired_gate_lends_no_vocabulary(self):
        # kills: link_gates ignoring status — a retired gate would still
        # steer the rule's retrieval while never being able to ride
        self.plant_pair()
        self.assertIn("gate_probes", self.entry(RULE))   # linked while live
        store.retire(GATE, TS, "superseded", project=None)
        self.assertNotIn("gate_probes", self.entry(RULE))


class WhisperTest(GateBase):
    def test_a_rule_with_a_gate_injects_both_glosses_in_one_whisper(self):
        # kills: _gate_plan without riders; _jit_lane rendering entries only;
        # gather ignoring the plan
        self.plant_pair()
        sections = inject.gather("time to fork the staging repo")
        jit = sections["jit"]
        self.assertEqual(len(jit), 2, jit)
        self.assertTrue(jit[0].startswith("PREMISE %s:" % RULE), jit[0])
        self.assertEqual(jit[1], "GATE of %s: PREMISE %s: two gates pass before a "
                         "PR is even considered: proven in our use, and the "
                         "project welcomes it" % (RULE, GATE))
        # the CLI renders the same two lines, rule above gate
        out, err = io.StringIO(), io.StringIO()
        stdin_prior = sys.stdin
        sys.stdin = io.StringIO("time to fork the staging repo")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = inject.cmd_inject([])
        finally:
            sys.stdin = stdin_prior
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual([l.split(":")[0] for l in lines],
                         ["PREMISE " + RULE, "GATE of " + RULE])

    def test_a_rule_without_gates_injects_alone(self):
        # must-miss: no gates:, no rider, no marker
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        sections = inject.gather("time to fork the staging repo")
        self.assertEqual(len(sections["jit"]), 1)
        self.assertNotIn("GATE", "\n".join(sections["jit"]))

    def test_budget_exhaustion_drops_the_gate_loudly_never_silently(self):
        # kills: a rider past the ceiling skipped with `continue` (silent);
        # a marker that does not name the gate; the marker dropping the
        # fetch command
        prompt = self.plant_outranked()
        ranked = [e["id"] for e in store.resolve_prompt(prompt, cap=9)]
        self.assertEqual(ranked[3:5], [RULE, GATE], ranked)  # rule 4, gate 5
        self.route(RULE)            # a rider rides only on a routed line
        with mock.patch.object(inject, "GATE_BUDGET", 60), routed_turn():
            sections = inject.gather(prompt)
        jit = sections["jit"]
        self.assertEqual(len(jit), 5, jit)
        self.assertEqual(jit[4], "GATE of prior:%s DROPPED (gate budget): prior:%s "
                         "— helm store get prior:%s" % (RULE, GATE, GATE))
        # the gate's id is in the lane EITHER way — the never-silent law
        self.assertIn(GATE, "\n".join(jit))
        # and at the real ceiling the gloss rides whole
        with routed_turn():
            sections = inject.gather(prompt)
        self.assertNotIn("DROPPED", "\n".join(sections["jit"]))
        self.assertTrue(sections["jit"][4].startswith(
            "GATE of %s: PREMISE %s: two gates" % (RULE, GATE)), sections["jit"])

    def test_an_earned_gate_keeps_its_slot_under_its_rule(self):
        # THE LIVE REGRESSION (2026-08-22): the specimen's number-one hit was
        # a gate of the rank-4 rule; demoted to a rider it was budget-dropped
        # and the seat lost the entry it would have had alone. kills: earned
        # gates budgeted as riders
        self.plant_pair()
        with mock.patch.object(inject, "GATE_BUDGET", 10):
            sections = inject.gather("time to fork the staging repo")
        jit = sections["jit"]
        self.assertEqual([l.split(":")[0] for l in jit],
                         ["PREMISE " + RULE, "GATE of " + RULE])
        self.assertNotIn("DROPPED", "\n".join(jit))
        self.assertIn("two gates pass before a PR", jit[1])

    def test_the_allowance_is_spent_across_riders_not_per_rider(self):
        # kills: a per-rider check (each rider under GATE_BUDGET alone), which
        # would let N gates add N x GATE_BUDGET; and riders charged against
        # the base lines (the first cut's cap ceiling, measured unable to
        # carry the live specimen even fully glossed)
        prompt = self.plant_outranked()
        self.plant(GATE2, "orca is a black box", "orca,upgrade")
        store.regate(RULE, "2026-08-22T13:00:00Z", add=GATE2)
        entries = store.load_all()
        top = store.resolve_prompt(prompt, cap=inject.JIT_CAP, entries=entries)
        self.assertEqual([e["id"] for e in top],
                         ["strong-0", "strong-1", "strong-2", RULE])
        r1 = len(inject._gate_line(RULE, self.entry(GATE)))
        r2 = len(inject._gate_line(RULE, self.entry(GATE2)))
        base = sum(len(inject._entry_line(e)) for e in top)
        # both fit alone, together they do not: the second must drop LOUDLY
        # (the rule is ROUTED here: a rider rides only on a routed line)
        with mock.patch.object(inject, "GATE_BUDGET", max(r1, r2) + 1):
            lines, ids, riders = inject._jit_lane(top, entries, {RULE})
        self.assertEqual([g["id"] for g, _r in riders], [GATE])
        self.assertIn("gate-dropped:prior:" + GATE2, ids)
        # and the base lines are not what the allowance is charged against
        with mock.patch.object(inject, "GATE_BUDGET", r1 + r2):
            lines, ids, riders = inject._jit_lane(top, entries, {RULE})
        self.assertEqual([g["id"] for g, _r in riders], [GATE, GATE2])
        self.assertGreater(sum(len(l) for l in lines), base + r1 + r2 - 1)

    def test_a_gate_that_is_not_injectable_is_said_to_be_missing(self):
        # kills: a retired/absent gate skipped silently
        self.plant_pair()
        store.retire(GATE, TS, "superseded", project=None)
        sections = inject.gather("time to fork the staging repo")
        self.assertEqual(sections["jit"][1],
                         "GATE of prior:%s MISSING from the store: %s" % (RULE, GATE))

    def test_a_gate_cooled_on_its_own_words_still_rides_with_the_rule(self):
        # THE SPECIMEN REPLAY: the gate fired at 03:43 on its own vocabulary
        # (now cooled for the session); at 11:30 the rule's words arrive and
        # the gate must arrive WITH the rule. kills: riders filtered by the
        # cooldown; the rider not recorded as fired
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging,ember")
        self.plant(GATE, "two gates pass before a PR is even considered",
                   "upstream,pr,culture")
        for session in ("s1", "s2"):
            first = inject.gather("an upstream pr and its culture",
                                  session=session)
            self.assertEqual([l.split(":")[0] for l in first["jit"]],
                             ["PREMISE " + GATE])      # the gate, alone, early
        store.regate(RULE, TS, add=GATE)                # the link lands later
        self.route(RULE)
        # ON A KEYWORD LINE the gate arrives BY NAME, in the lane's footer
        # (task/2980): it is not delivered, so it records no cooldown
        second = inject.gather("time to fork the staging repo", session="s1")
        jit = second["jit"]
        self.assertEqual(jit[-1], inject.FOOTER + inject.FOOTER_GATES
                         + "prior:" + GATE, jit)
        self.assertFalse(any(l.startswith("GATE of ") for l in jit), jit)
        self.assertNotIn("prior:" + GATE, inject._seen_load("s1")["fired"])
        # ON A ROUTED LINE it rides whole, and the ride is a delivery
        with routed_turn():
            third = inject.gather("time to fork the staging repo", session="s2")
        jit = third["jit"]
        # on its own the gate is COOLED now (score 2.0 against 3.0 at fire):
        self.assertNotIn("PREMISE %s:" % GATE, [l[:len("PREMISE %s:" % GATE)]
                                                for l in jit])
        self.assertTrue(any(l.startswith("GATE of %s: PREMISE %s" % (RULE, GATE))
                            for l in jit), jit)
        seen = inject._seen_load("s2")
        self.assertIn("prior:" + GATE, seen["fired"])   # the rider's typed key
        self.assertIn(RULE, seen["fired"])              # a selected entry's bare key

    def test_the_fire_ledger_records_the_rider_and_the_loud_drop(self):  # noqa: VACUOUS_ASSERTION — both ledger rows are asserted equal to non-empty id lists
        # kills: fired["jit"] built from jit_entries instead of the lane ids
        prompt = self.plant_outranked()
        self.route(RULE)
        with routed_turn():
            inject.gather(prompt)
            with mock.patch.object(inject, "GATE_BUDGET", 60):
                inject.gather(prompt)
        rows = inject._ledger_rows()
        strong = ["strong-0", "strong-1", "strong-2"]
        # the routed rule leads the lane (the route is prepended)
        self.assertEqual(rows[0]["fired"]["jit"], [RULE] + strong + ["prior:" + GATE])
        self.assertEqual(rows[1]["fired"]["jit"],
                         [RULE] + strong + ["gate-dropped:prior:" + GATE])

    def test_a_pinned_rule_carries_its_jit_gate_in_the_pinned_lane(self):
        # kills: the pinned walk ignoring the plan
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging",
                   pin="true")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        store.regate(RULE, TS, add=GATE)
        sections = inject.gather("anything at all")
        pinned = sections["pinned"]
        self.assertEqual([l.split(":")[0] for l in pinned],
                         ["PREMISE " + RULE, "GATE of " + RULE])
        with mock.patch.object(inject, "PINNED_BUDGET", 60):
            sections = inject.gather("anything at all")
        self.assertIn("GATE of prior:%s DROPPED (pinned budget): prior:%s"
                      % (RULE, GATE), "\n".join(sections["pinned"]))

    def test_explain_reports_the_rider_the_seat_receives(self):
        # kills: --explain not reading the same lane walk as gather
        self.plant_pair()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inject._explain("time to fork the staging repo")
        text = out.getvalue()
        self.assertIn("gates (1, rider allowance %dB):" % inject.GATE_BUDGET, text)
        self.assertIn("  + GATE of %s: PREMISE %s:" % (RULE, GATE), text)


class KeywordLineGateTest(GateBase):
    """A RIDER RIDES ONLY ON A DETERMINISTIC LINE (task/2980). On a keyword
    line a gate rider tripled one block to 1,648 B (MEASURED, trigger design
    D8), for a lane whose keyword lines are about 1 in 8 relevant.
    A keyword rule's gate is NAMED in the lane footer instead, by an id that
    `helm store get` reads back."""

    def test_a_keyword_rules_gate_is_named_in_the_footer_never_rendered(self):
        # kills: riders_for ignored (every rule rides), and a mention counted
        # as a delivery (a ledger id or a cooldown record)
        prompt = self.plant_outranked()
        sections = inject.gather(prompt, session="s-kw")
        jit = sections["jit"]
        self.assertFalse(any(l.startswith("GATE of ") for l in jit), jit)
        self.assertEqual(jit[-1], inject.FOOTER + inject.FOOTER_GATES
                         + "prior:" + GATE)
        row = inject._ledger_rows()[-1]
        self.assertEqual(row["fired"]["jit"],
                         ["strong-0", "strong-1", "strong-2", RULE])
        self.assertNotIn("prior:" + GATE, inject._seen_load("s-kw")["fired"])
        # the control on the SAME store: a routed turn carries the rider
        self.route(RULE)
        with routed_turn():
            jit = inject.gather(prompt)["jit"]
        self.assertTrue(any(l.startswith("GATE of %s: PREMISE %s" % (RULE, GATE))
                            for l in jit), jit)
        self.assertFalse(any(inject.FOOTER_GATES in l for l in jit), jit)

    def test_explain_names_the_mention_the_footer_carries(self):
        # kills: --explain walking a plan with riders the seat never receives
        prompt = self.plant_outranked()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inject._explain(prompt)
        self.assertIn("  ~ prior:%s (gate of %s, named in the footer)"
                      % (GATE, RULE), out.getvalue())
        self.assertNotIn("  + GATE of %s" % RULE, out.getvalue())

    def test_every_id_the_footer_names_reads_back(self):  # noqa: VACUOUS_ASSERTION — every leg asserts the footer names the gate, rc 0 and the gate's text on get
        # THE DEAD-POINTER CLASS (trigger design defect 2, MEASURED on the
        # live store: 26 rows, 23 live). _slug cuts at 60 characters and
        # strips an edge dash, so a typed id whose cut landed on a dash
        # re-slugged one character short and `helm store get` said "not
        # found". Both a single and a double dash at the cut. kills: typed_id
        # printing the raw 60-character slug
        for gid in ("review-independence-is-a-different-model-and-no-reviewer-"
                    "is-never-a-blocker",
                    "a" * 59 + "--" + "tail-of-the-id"):
            with self.subTest(gid=gid[:20]):
                self.assertTrue(store._slug(gid).endswith("-"), gid)
                prompt = self.plant_outranked()
                self.plant(gid, "the gate with the long id", "longgateword")
                _e, err = store.regate(RULE, "2026-08-22T13:00:00Z",
                                       replace=gid)
                self.assertIsNone(err, err)
                footer = inject.gather(prompt)["jit"][-1]
                self.assertTrue(footer.startswith(inject.FOOTER
                                                  + inject.FOOTER_GATES), footer)
                named = footer[len(inject.FOOTER + inject.FOOTER_GATES):]
                rc, out, _err = run_store(["get", named])
                self.assertEqual(rc, 0, named)
                self.assertIn("the gate with the long id", out)
                self.assertEqual(store.split_gate(named),
                                 store.gate_key(self.entry(gid)))


class VerbTest(GateBase):
    def test_gates_add_writes_and_round_trips_through_the_parser(self):
        # kills: the writer not emitting gates:; a parser default missing
        # (the allowlist law — the key would be dropped on the next rewrite)
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        self.plant(GATE2, "orca is a black box", "orca,upgrade")
        rc, out, err = run_store(["gates", RULE, "--add", GATE + "," + GATE2])
        self.assertEqual(rc, 0, err)
        self.assertIn("now carries 2 gates", out)
        self.assertEqual(self.entry(RULE)["gates"], GATE + "," + GATE2)
        with open(self.entry(RULE)["path"], encoding="utf-8") as f:
            self.assertIn("  gates: %s,%s\n" % (GATE, GATE2), f.read())
        # a later unrelated rewrite keeps it (the dropped-field class)
        store.retag(RULE, TS, add="pr-staging")
        self.assertEqual(self.entry(RULE)["gates"], GATE + "," + GATE2)
        rc, out, _err = run_store(["gates", RULE])
        self.assertEqual(rc, 0)
        self.assertIn("2 gates", out)
        self.assertIn("  " + GATE2, out)
        rc, out, _err = run_store(["gates", RULE, "--remove", GATE2])
        self.assertEqual(rc, 0)
        self.assertEqual(self.entry(RULE)["gates"], GATE)

    def test_a_gate_that_resolves_to_nothing_is_refused(self):
        # kills: regate accepting any CSV — a rule that believes it has a
        # gate and arrives alone, the exact silent shape
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        rc, _out, err = run_store(["gates", RULE, "--add", "no-such-entry"])
        self.assertEqual(rc, 1)
        self.assertIn("resolves to no injectable store entry", err)
        self.assertEqual(self.entry(RULE).get("gates", ""), "")
        rc, _out, err = run_store(["gates", RULE, "--add", RULE])
        self.assertEqual(rc, 1)
        self.assertIn("cannot gate itself", err)
        # the positive control: a gate that resolves is accepted by the same door
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        rc, _out, err = run_store(["gates", RULE, "--add", GATE])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(RULE)["gates"], GATE)

    def test_the_other_three_types_carry_gates_too(self):
        # kills: one writer missing _gates_lines (the 3-of-4 class)
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        store.write_heuristic({"id": "h1", "move": "ask the three questions",
                               "trigger": "seam,strategy", "stated_ts": TS})
        store.write_reference({"id": "r1", "statement": "the orca CLI surface",
                               "keywords": "orca-cli", "stated_ts": TS})
        store.write_lexicon({"term": "seam", "definition": "a dependency edge",
                             "keywords": "seam-word", "updated_ts": TS})
        for eid, ctype in (("h1", "heuristic"), ("r1", "reference"),
                           ("seam", "lexicon")):
            e, err = store.regate(eid, TS, add=GATE, ctype=ctype)
            self.assertIsNone(err, (eid, err))
            self.assertEqual(self.entry(eid)["gates"], GATE, eid)
        # and the gate rides with whichever of them fires first
        sections = inject.gather("a strategy at the seam")
        self.assertTrue(any(l.startswith("GATE of ") and GATE in l
                            for l in sections["jit"]), sections["jit"])

    def test_gates_is_idempotent_and_writes_no_event_for_no_change(self):
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        with mock.patch.object(pk, "event") as ev:
            store.regate(RULE, TS, add=GATE)
        ev.assert_called_once_with("store.regate", RULE, "0 -> 1 gates")
        with mock.patch.object(pk, "event") as ev:
            e, err = store.regate(RULE, TS, add=GATE)
        self.assertIsNone(err)
        ev.assert_not_called()

    def test_help_names_the_verb(self):
        # a synopsis omission reads as absence
        rc, out, err = run_store(["--help"])
        self.assertIn("gates <id>", out + err)


class CycleTest(GateBase):
    """P1 (a), dispatch fff5cef99aec: write.py accepted a<->b gate
    cycles and _gate_plan then returned [], erasing both selected rules."""

    def test_a_cycle_is_refused_at_the_verb(self):
        # kills: regate without the graph walk (_gate_cycle); the 1-cycle
        # self-gate alone is not the cure
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture")
        self.plant(GATE2, "orca is a black box", "orca,upgrade")
        _e, err = store.regate(RULE, TS, add=GATE)
        self.assertIsNone(err)
        rc, _out, err = run_store(["gates", GATE, "--add", RULE])
        self.assertEqual(rc, 1)
        self.assertIn("CYCLE", err)
        self.assertIn("%s -> %s -> %s" % (GATE, RULE, GATE), err)
        self.assertEqual(self.entry(GATE).get("gates", ""), "", "nothing written")
        # the longer cycle: RULE -> GATE -> GATE2 -> RULE
        _e, err = store.regate(GATE, TS, add=GATE2)
        self.assertIsNone(err, err)
        _e, err = store.regate(GATE2, TS, add=RULE)
        self.assertIn("CYCLE", err or "")
        # the positive control on the same walk: a chain that does not
        # return is accepted
        self.plant("leaf", "a leaf premise", "leafword")
        _e, err = store.regate(GATE2, TS, add="leaf")
        self.assertIsNone(err, err)

    def test_a_cycle_already_on_disk_still_renders_both_rules(self):
        # kills: the old deferral (each side steps aside for the other and
        # the plan is empty); the fail-open must render BOTH selected ids
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging",
                   gates=GATE)                      # written past the verb
        self.plant(GATE, "two gates before a PR", "upstream,pr,culture",
                   gates=RULE)
        self.assertEqual(store.gate_ids(self.entry(GATE)), [RULE])
        plan = inject._gate_plan(
            [self.entry(RULE), self.entry(GATE)], store.load_all())
        self.assertEqual([k for k, *_ in plan], ["entry", "gate-earned"])
        sections = inject.gather("time to fork the staging repo")
        joined = "\n".join(sections["jit"])
        self.assertIn("PREMISE %s:" % RULE, joined)
        self.assertIn("GATE of %s: PREMISE %s:" % (RULE, GATE), joined)
        # the 3-cycle, where the fixpoint alone would strand the first side:
        # every selected id still renders (the no-erasure floor)
        self.plant(GATE2, "orca is a black box", "orca,upgrade", gates=RULE)
        e = self.entry(GATE)
        e["gates"] = GATE2
        store.write_prior(e, path=e["path"])
        ids = [str(x["id"]) for x in store.resolve_prompt("time to fork the staging repo")]
        self.assertEqual(set(ids), {RULE, GATE, GATE2})
        plan = inject._gate_plan([self.entry(i) for i in ids], store.load_all())
        self.assertEqual({str(it[1]["id"]) for it in plan if it[1] is not None},
                         {RULE, GATE, GATE2})

    def test_a_non_selected_gate_that_names_its_rule_back_is_a_loud_cycle_marker(self):
        # kills: a cycle rider rendered as if sound, or skipped silently
        prompt = self.plant_outranked()             # GATE rides, not earned
        e = self.entry(GATE)
        e["gates"] = RULE                            # the cycle, past the verb
        store.write_prior(e, path=e["path"])
        self.route(RULE)                             # riders ride routed lines
        with routed_turn():
            sections = inject.gather(prompt)
        joined = "\n".join(sections["jit"])
        # (d) the cycle marker and its remediation carry the TYPED spelling
        self.assertIn("GATE of prior:%s CYCLE: prior:%s names prior:%s as its own "
                      "gate — helm store gates prior:%s --remove prior:%s"
                      % (RULE, GATE, RULE, GATE, RULE), joined)
        self.assertIn("PREMISE %s:" % RULE, joined, "the rule still renders")
        self.assertNotIn("GATE of %s: PREMISE %s" % (RULE, GATE), joined)


class AmbiguousSlugTest(GateBase):
    """P1 (b): a bare slug shared by a prior and a lexicon validated the
    prior (typed-first) and injected whichever entry the list yielded."""

    def test_a_shared_slug_is_refused_and_the_typed_id_injects_that_type(self):
        # kills: regate resolving bare slugs typed-first; _gate_plan keying
        # by bare slug; the two resolving through different functions
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        self.plant("seam", "the prior about a seam", "seamprior")
        store.write_lexicon({"term": "seam", "definition": "a dependency edge",
                             "keywords": "seamword", "updated_ts": TS})
        rc, _out, err = run_store(["gates", RULE, "--add", "seam"])
        self.assertEqual(rc, 1)
        self.assertIn("AMBIGUOUS", err)
        self.assertIn("lexicon:seam", err)
        self.assertIn("prior:seam", err)
        rc, _out, err = run_store(["gates", RULE, "--add", "lexicon:seam"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(RULE)["gates"], "lexicon:seam")
        sections = inject.gather("time to fork the staging repo")
        joined = "\n".join(sections["jit"])
        self.assertIn("GATE of %s: TERM seam: a dependency edge" % RULE, joined)
        self.assertNotIn("the prior about a seam", joined)
        # the typed form of the other type injects the other entry
        store.regate(RULE, TS, replace="prior:seam")
        sections = inject.gather("time to fork the staging repo")
        joined = "\n".join(sections["jit"])
        self.assertIn("GATE of %s: PREMISE seam: the prior about a seam" % RULE, joined)
        self.assertNotIn("a dependency edge", joined)
        # and the vocabulary fold followed the same resolution
        self.assertIn("seamprior", self.entry(RULE)["gate_probes"])
        self.assertNotIn("seamword", self.entry(RULE)["gate_probes"])


class PinnedOrderTest(GateBase):
    """P1 (c): a pinned rider used to sit between two pins and take the
    budget the later pin would have had."""

    def test_a_later_pin_survives_a_turn_with_riders(self):
        # kills: riders walked in plan order (between base lines) instead of
        # after every base line
        self.plant("a-rule", "the pinned rule", "fork,staging", pin="true")
        self.plant("b-pin", "the later pin that must survive", "otherword",
                   pin="true")
        self.plant(GATE, "G" * 380, "upstream,pr,culture")
        self.plant(GATE2, "H" * 380, "orca,upgrade")
        store.regate("a-rule", TS, add=GATE + "," + GATE2)
        self.assertEqual([e["id"] for e in store.pinned()], ["a-rule", "b-pin"])
        with mock.patch.object(inject, "PINNED_BUDGET", 600):
            sections = inject.gather("anything at all")
        pinned = sections["pinned"]
        self.assertEqual([l.split(":")[0] for l in pinned[:2]],
                         ["PREMISE a-rule", "PREMISE b-pin"])
        self.assertTrue(pinned[2].startswith("GATE of a-rule: PREMISE %s:" % GATE))
        self.assertEqual(pinned[3], "GATE of prior:a-rule DROPPED (pinned budget): "
                         "prior:%s — helm store get prior:%s" % (GATE2, GATE2))
        # and explain reports the same four lines in the same order
        out = io.StringIO()
        with mock.patch.object(inject, "PINNED_BUDGET", 600), \
                contextlib.redirect_stdout(out):
            inject._explain("anything at all")
        text = out.getvalue()
        self.assertLess(text.index("PREMISE b-pin"), text.index("GATE of a-rule"))
        self.assertIn("DROPPED (pinned budget): prior:%s" % GATE2, text)


class OneLaneModelTest(GateBase):
    """Dispatch 0810a8fbe4e9, converged in a meld: rendered-base
    membership is the single model, keyed by typed identity; whisper and
    --explain both consume it; riders exist only for rendered bases; markers
    and remediation carry type:slug."""

    def plant_small_pair(self):
        """A pinned rule whose two JIT gates are SHORT — so if a rider is
        absent, the only possible reason is its base."""
        self.plant("a-rule", "the pinned rule with a long enough statement "
                   + "r" * 60, "fork,staging", pin="true")
        self.plant(GATE, "gate one", "upstream,pr,culture")
        self.plant(GATE2, "gate two", "orca,upgrade")
        store.regate("a-rule", TS, add=GATE + "," + GATE2)

    def test_a_rule_the_base_budget_omits_renders_no_rider_and_no_marker(self):
        # (a) kills: riders generated from the selected set instead of the
        # rendered set — a gate without its rule, or a marker that is one
        self.plant_small_pair()
        rule_line = inject._entry_line(self.entry("a-rule"))
        rider = inject._gate_line("a-rule", self.entry(GATE))
        self.assertLess(len(rider), len(rule_line),
                        "the rider must fit where the rule does not")
        with mock.patch.object(inject, "PINNED_BUDGET", len(rule_line) - 1):
            sections = inject.gather("anything at all")
            plan = inject._gate_plan(store.pinned(), store.load_all(),
                                     base_budget=inject.PINNED_BUDGET)
        # THE ARM'S POINT IS UNCHANGED and is the plan assertion below: an
        # omitted rule contributes NO RIDER and no rider-marker, because a
        # marker for a gate whose rule never rendered would itself be a gate
        # without its rule. That still holds exactly.
        #
        # What changed is the LANE: an omitted rule is now ANNOUNCED by a
        # single lane-level line (integrator ruling 2026-08-28) — a rule the
        # budget could not carry and a rule that does not exist used to
        # produce the identical observable, which is how five ratified owner
        # premises were dropped silently every turn. The lane is therefore no
        # longer EMPTY here; it carries the alarm and NO rule and NO rider.
        rendered = sections["pinned"]
        self.assertEqual(len(rendered), 1, rendered)
        self.assertTrue(rendered[0].startswith("[helm pinned]"), rendered)
        self.assertNotIn(rule_line, rendered)
        self.assertNotIn(rider, rendered)
        self.assertEqual([(it[0], it[6]) for it in plan], [("entry", False)])
        # the positive control: room for the rule and both riders renders all
        with mock.patch.object(inject, "PINNED_BUDGET",
                               len(rule_line) + 2 * len(rider) + 10):
            sections = inject.gather("anything at all")
        self.assertEqual([l.split(":")[0] for l in sections["pinned"]],
                         ["PREMISE a-rule", "GATE of a-rule", "GATE of a-rule"])

    def test_explain_consumes_the_same_plan_object_as_gather(self):
        # (b) kills: --explain re-deriving the lane from text or from a
        # second walk — asserted on the plan calls and their items, not text
        self.plant_small_pair()
        self.plant(RULE, "a fork is a PR staging ground", "fork,staging")
        store.regate(RULE, TS, add=GATE)
        import importlib
        _whisper = importlib.import_module("helm.inject._whisper")  # the
        # package re-exports a FUNCTION named _whisper over the module name
        # _gate_plan is DEFINED in _entries and RE-EXPORTED into _whisper, so
        # the two modules hold INDEPENDENT bindings. The pinned walk now runs
        # inside _entries.pinned_admission against _entries' own global, and
        # the jit walk still runs through _whisper's. Patching one binding
        # therefore observes ONE of the two walks and silently counts 1 where
        # the property is about 2 -- the probe loses its subject while the
        # property it tests still holds. Patch BOTH; the assertion is that the
        # SAME plan objects reach gather and --explain, not that they were
        # reached through any particular module attribute.
        _entries = importlib.import_module("helm.inject._entries")
        real = _entries._gate_plan
        calls = []

        def recorder(selected, entries, **kw):
            out = real(selected, entries, **kw)
            calls.append(([store.gate_key(e) for e in selected], kw,
                          [(it[0], it[4], it[5], it[6]) for it in out]))
            return out
        with mock.patch.object(inject, "PINNED_BUDGET", 150), \
                mock.patch.object(_whisper, "_gate_plan", recorder), \
                mock.patch.object(_entries, "_gate_plan", recorder):
            sections = inject.gather("time to fork the staging repo")
            gathered = list(calls)
            calls.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                inject._explain("time to fork the staging repo")
            explained = list(calls)
        self.assertEqual(len(gathered), 2, "one pinned plan, one jit plan")
        self.assertEqual(gathered, explained)
        # and what gather rendered is exactly the plan's rendered items
        pinned_plan, jit_plan = gathered
        self.assertEqual(sections["pinned"],
                         [line for _k, line, _lid, ok in pinned_plan[2] if ok])
        self.assertEqual(sections["jit"],
                         [line for _k, line, _lid, ok in jit_plan[2] if ok])
        self.assertIn("DROPPED (pinned budget)", "\n".join(sections["pinned"]))

    def test_a_dropped_same_slug_gate_is_named_by_type_in_the_marker_and_the_command(self):  # noqa: VACUOUS_ASSERTION — the marker is asserted equal to one named line and the typed remove to rc 0 before the field is asserted empty
        # (c) kills: the marker flattening to the bare slug, which names the
        # prior of the same slug; the remediation must fetch the lexicon
        prompt = self.plant_outranked()
        self.plant("seam", "the prior about a seam", "seamprior")
        store.write_lexicon({"term": "seam", "definition": "a dependency edge",
                             "keywords": "seamword",
                             "updated_ts": "2026-08-22T11:00:00Z"})
        _e, err = store.regate(RULE, "2026-08-22T13:00:00Z", replace="lexicon:seam")
        self.assertIsNone(err, err)
        ranked = [store.typed_id(e) for e in store.resolve_prompt(prompt, cap=9)]
        self.assertEqual(ranked[3], "prior:" + RULE)
        self.assertIn("lexicon:seam", ranked[4:])      # a rider, not earned
        self.route(RULE)                               # riders ride routed lines
        with mock.patch.object(inject, "GATE_BUDGET", 10), routed_turn():
            sections = inject.gather(prompt)
        marker = [l for l in sections["jit"] if "DROPPED" in l]
        self.assertEqual(marker, ["GATE of prior:%s DROPPED (gate budget): "
                                  "lexicon:seam — helm store get lexicon:seam" % RULE])
        rc, out, _err = run_store(["get", "lexicon:seam"])
        self.assertEqual(rc, 0)
        self.assertIn("a dependency edge", out)
        self.assertNotIn("the prior about a seam", out)
        # and the typed removal the markers print reaches the right entry
        rc, _out, err = run_store(["gates", "prior:" + RULE, "--remove", "lexicon:seam"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(RULE).get("gates", ""), "")



class TypedRiderIdentityTest(GateBase):
    """Dispatch 0fb12823851d: rendered rider ledger/cooldown ids were
    bare, so same-slug types collapsed; a typed --remove matched a stored
    bare same-slug gate."""

    def plant_same_slug_pair(self):
        """The rule at rank 4 (three stronger entries), gating a lexicon and a
        reference that share the slug `x` — both outranked, so both RIDE."""
        prompt = self.plant_outranked()
        store.regate(RULE, "2026-08-22T13:00:00Z", remove=GATE)
        store.write_lexicon({"term": "x", "definition": "the lexicon x",
                             "keywords": "xlex", "updated_ts": TS})
        store.write_reference({"id": "x", "statement": "the reference x",
                               "keywords": "xref", "stated_ts": TS})
        _e, err = store.regate(RULE, "2026-08-22T13:00:00Z",
                               add="lexicon:x,reference:x")
        self.assertIsNone(err, err)
        return prompt

    def test_two_same_slug_riders_get_two_ledger_ids_and_two_cooldown_keys(self):
        # (1) kills: typed_id dropped at the rider's ledger id; the cooldown
        # writer or reader keyed on the bare id
        prompt = self.plant_same_slug_pair()
        self.route(RULE)                               # riders ride routed lines
        with routed_turn():
            sections = inject.gather(prompt, session="s-typed")
        gated = [l for l in sections["jit"] if l.startswith("GATE of ")]
        self.assertEqual(len(gated), 2, sections["jit"])
        row = inject._ledger_rows()[-1]
        self.assertEqual(row["fired"]["jit"][-2:], ["lexicon:x", "reference:x"])
        seen = inject._seen_load("s-typed")
        self.assertIn("lexicon:x", seen["fired"])
        self.assertIn("reference:x", seen["fired"])
        self.assertNotIn("x", seen["fired"])
        # and the reader finds the typed record: both are cooled next turn
        # while a prompt matching the lexicon on its own word arrives
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inject._explain("xlex xref", session="s-typed")
        self.assertEqual(out.getvalue().count("- x (cooldown, fired 1t ago)"), 2,
                         out.getvalue())

    def test_a_typed_remove_leaves_the_other_typed_same_slug_gate(self):
        # (2) kills: --remove matching on the bare slug
        self.plant_same_slug_pair()
        rc, _out, err = run_store(["gates", RULE, "--remove", "lexicon:x"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(RULE)["gates"], "reference:x")
        rc, _out, err = run_store(["gates", RULE, "--remove", "reference:x"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(RULE).get("gates", ""), "")

    def test_a_bare_stored_gate_with_two_types_refuses_to_be_removed_by_either(self):
        # (2) kills: a typed operand matching a stored bare spelling whose
        # slug has two injectable types — the wrong identity could go
        self.plant_same_slug_pair()
        e = self.entry(RULE)
        e["gates"] = "x"                         # a bare spelling, past the verb
        store.write_prior(e, path=e["path"])
        for operand in ("lexicon:x", "x"):
            rc, _out, err = run_store(["gates", RULE, "--remove", operand])
            self.assertEqual(rc, 1, operand)
            self.assertIn("AMBIGUOUS", err)
            self.assertIn("lexicon:x", err)
            self.assertIn("reference:x", err)
            self.assertEqual(self.entry(RULE)["gates"], "x", "nothing removed")
        # the positive control: retire the reference and the bare `x`
        # resolves to one type, so the typed remove reaches it
        store.retire("x", TS, "gone", project=None)
        self.assertIsNone(store.resolve_gate("reference:x", [
            z for z in store.load_all() if z.get("status") in store.INJECTABLE_STATUSES]))
        rc, _out, err = run_store(["gates", RULE, "--remove", "lexicon:x"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(RULE).get("gates", ""), "")


class NewestCooldownRecordTest(GateBase):
    """Dispatch c7897ed62f4a: _cool_rec read typed-then-bare by ORDER,
    so a rider's stale typed record at score S stayed authoritative over a
    later selected escape at 2S — repeated escapes, and --explain aging the
    fire from the wrong turn. The newer record wins, whichever key it is under."""

    def plant_riding_gate(self):
        """plant_outranked, with one rare word of the gate's own so a
        gate-only prompt can score 2x the rule score it rides at."""
        prompt = self.plant_outranked()
        store.retag(GATE, "2026-08-22T13:30:00Z", add="gateword")
        # the retag made the GATE the newest entry, which would win the
        # rank-4 tie and make it SELECTED on the rule prompt — keep the rule
        # newest so the gate stays the rider these arms are about
        store.retag(RULE, "2026-08-22T14:00:00Z", add="rulefresh")
        self.route(RULE)            # a rider rides only on a routed line
        return prompt

    def test_a_selected_escape_after_a_rider_is_the_record_that_counts(self):
        # kills: typed-first ordering (the stale rider record authoritative)
        rule_prompt = self.plant_riding_gate()
        with routed_turn():                    # the rider's turn is routed
            first = inject.gather(rule_prompt, session="s-newest")
        self.assertTrue(any(l.startswith("GATE of ") for l in first["jit"]),
                        first["jit"])          # the rider: typed record at S=2.0
        gate_prompt = "an upstream pr culture gateword"   # 4.0 = 2S: the escape
        second = inject.gather(gate_prompt, session="s-newest")
        # the gate is SELECTED this turn (the rule escapes beside it and the
        # plan still marks the pairing), so its record lands under the BARE key
        self.assertTrue(any("PREMISE %s: two gates" % GATE in l
                            for l in second["jit"]), second["jit"])
        seen = inject._seen_load("s-newest")
        self.assertEqual(seen["fired"]["prior:" + GATE][0], 1)  # the stale rider
        self.assertEqual(seen["fired"][GATE][0], 2)             # the escape, newer
        # --explain ages the fire from the ESCAPE turn (1t ago on the
        # would-be turn 3), through the same read — before the next gather
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inject._explain(gate_prompt, session="s-newest")
        self.assertIn("- %s (cooldown, fired 1t ago)" % GATE, out.getvalue())
        self.assertNotIn("fired 2t ago", out.getvalue())
        # and the SAME prompt next turn does NOT re-escape: 4.0 < 2 x 4.0
        third = inject.gather(gate_prompt, session="s-newest")
        self.assertFalse(any(GATE in l for l in third["jit"]), third["jit"])

    def test_the_mirror_a_rider_after_a_selected_fire_is_the_record_that_counts(self):
        # the control against the OPPOSITE ordering (bare-first): selected at
        # S, then a rider at a later turn — the typed record is newer and wins
        rule_prompt = self.plant_riding_gate()
        inject.gather("the culture question", session="s-mirror")  # selected, S=1.0
        seen = inject._seen_load("s-mirror")
        self.assertEqual(seen["fired"][GATE][0], 1)
        with routed_turn():
            second = inject.gather(rule_prompt, session="s-mirror")  # rides at 2.0
        self.assertTrue(any(l.startswith("GATE of ") and "PREMISE %s" % GATE in l
                            for l in second["jit"]), second["jit"])
        seen = inject._seen_load("s-mirror")
        self.assertEqual(seen["fired"]["prior:" + GATE][0], 2)     # typed, newer
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inject._explain("the culture question", session="s-mirror")
        self.assertIn("- %s (cooldown, fired 1t ago)" % GATE, out.getvalue())
        self.assertNotIn("fired 2t ago", out.getvalue())


if __name__ == "__main__":
    unittest.main()
