#!/usr/bin/env python3
"""inject tests — the ONE live per-turn surface (UserPromptSubmit hook on every
cred-home). Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR are tmp
dirs; the real ~/.helm, ~/.claude and ~/.cache are never touched.

Pins the load-bearing constants (PINNED_BUDGET / JIT_CAP / LINE_CAP), the
salience law (no match -> empty stdout, rc 0), the --json shape, inline-arg vs
stdin precedence, the fail-open law (a raising store must never block a turn),
the parsed-entry cache added for the twice-per-prompt parse fix (including the
entries= seam short-circuiting ALL store parsing), the fire-ledger (row
shape, ids-never-text, rotation, fail-open, --explain writes no row), the
per-session JIT cooldown (fires/cools/refires, 2x score escape, cross-session
independence, freed cap slots, explain rendering, state fail-open), and the
coinage 3-strikes recorder (K=3 distinct turns, offer-once-latch-forever,
narrowest detector's structural stoplist, one nudge per turn, fail-open)."""
import contextlib
import io
import json
import os
import re
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
            "HELM_CACHE_DIR", "MELD_CACHE_DIR",
            # the chat dir: popped AND set per-test, so the council-streak
            # reflex counts this suite's rooms and never the live fleet's
            "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            # the comparison backend's activation env — popped so the WHOLE suite
            # is hermetic (a stray HELM_CF_ENDPOINT must never let a test reach out)
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_CF_TOKEN", "MELD_CF_TOKEN",
            # the seat identity env — popped so a suite run INSIDE a codex seat
            # never smuggles the SA whisper or v2 context into assertions
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_AGENT_HARNESS",
            "PI_CODING_AGENT", "PI_CODING_AGENT_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_PID",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            # the seat-name AUTHORITY: deliberately NOT resolved through
            # HELM_HOME (it describes the fleet, not one estate), so without
            # this key the store's scope derivation would read the real
            # ~/.helm/_global/seat-names.txt and a suite result would depend on
            # who happens to be seated while it runs
            "HELM_SEAT_NAMES", "MELD_SEAT_NAMES")


class InjectBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-inject-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        # THE CHAT DIR TOO, or these tests read the LIVE fleet's rooms. The
        # council-streak reflex counts REAL ping-pong, so the moment a real
        # seat had three async rounds with a peer, gather() began emitting
        # "REFLEX: 3 async rounds with <seat>..." into tests asserting on empty
        # output — a suite whose result depends on what the fleet happened to
        # be doing while it ran. Found 2026-07-24 when my own review ping-pong
        # turned three inject tests red; same class as the ambient-identity
        # leak in test_web_chat, and it had been latent all along.
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        # An ABSENT authority is a proven-empty one (read_authority), so the
        # default here names no identity at all and every test that does not
        # write it gets the same answer on any machine.
        os.environ["HELM_SEAT_NAMES"] = os.path.join(self.tmp, "seat-names.txt")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant_pinned(self, pid, statement, conf="1.0"):
        store.write_prior({"id": pid, "statement": statement,
                           "confidence": conf, "pin": "true"})

    def plant_jit(self, pid, statement, keywords, conf="0.8"):
        store.write_prior({"id": pid, "statement": statement,
                           "confidence": conf, "keywords": keywords})

    def run_inject(self, args, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        stdin_prior = sys.stdin
        sys.stdin = io.StringIO(stdin_text if stdin_text is not None else "")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = inject.cmd_inject(list(args))
        finally:
            sys.stdin = stdin_prior
        return rc, out.getvalue(), err.getvalue()


def rule_lines(pinned_section):
    """The lines PINNED_BUDGET actually governs.

    Per the 2026-08-28 ruling (chat 2044, clause 3 amended by row
    64f555da38e6): PINNED_BUDGET is the budget FOR THE RULES ALONE. The WHO
    digest walks AFTER them against WHO_CAP as its whole budget, and the
    loud-drop alarm is deliberately UNCHARGED -- an announcement that a rule
    was starved must never itself be starved. Summing the whole rendered
    section against PINNED_BUDGET is the PRE-ruling accounting, and it is what
    made these arms red: they were measuring a law that no longer exists.
    The lane FOOTER (task/2980) is uncharged for the alarm's reason: it is
    about the rules, not one of them."""
    return [l for l in pinned_section
            if not l.startswith("WHO ") and not l.startswith("[helm pinned]")
            and l != inject.FOOTER]


class BudgetTest(InjectBase):
    def test_pinned_lane_stops_at_budget(self):
        # DERIVED from the budget, never pinned to its value: this said
        # "6 entries ... only ~4 fit under 1200" and silently stopped
        # overflowing when PINNED_BUDGET moved to 1540. A fixture that encodes
        # the constant tests the constant, not the behaviour.
        n = inject.PINNED_BUDGET // 240 + 2      # always MORE than can fit
        for i in range(n):
            self.plant_pinned("pin-%d" % i, ("truth %d " % i) + "x" * 240)
        sections = inject.gather("anything at all")
        got = sections["pinned"]
        self.assertTrue(got, "pinned lane must fire")
        self.assertLess(len(got), n, "budget must exclude some entries")
        rules = rule_lines(got)
        self.assertTrue(rules, "must-hit: some rule rendered, else the sum "
                               "below is vacuously under any budget")
        self.assertLessEqual(sum(len(l) for l in rules), inject.PINNED_BUDGET)
        # greedy in store.pinned order: adding back the first excluded line
        # would break the budget (the cap is the reason it stopped)
        entries = store.pinned()
        next_line = inject._entry_line(entries[len(got)])
        self.assertGreater(sum(len(l) for l in got) + len(next_line),
                           inject.PINNED_BUDGET)

    def test_jit_lane_capped_at_four(self):
        for i in range(6):
            self.plant_jit("jit-%d" % i, "flux fact %d" % i, "fluxcap")
        sections = inject.gather("tune the fluxcap now")
        self.assertEqual(len(sections["jit"]), inject.JIT_CAP)
        self.assertEqual(inject.JIT_CAP, 4)

    def test_a_cut_pinned_line_fits_the_cap_and_its_lane_names_the_route(self):
        """The cap is a CEILING, not an equality: a cut line backs off to a
        word boundary and ends in an ellipsis, and the LANE'S FOOTER names
        `helm store get`, so the reader can reach what the cap removed
        (task/2980: once per lane, where every cut line carried its own).
        Pinning == LINE_CAP pinned the old mid-word chop, which is the defect,
        not the contract."""
        self.plant_pinned("long-one", "y " * 400)
        sections = inject.gather("anything")
        line = sections["pinned"][0]
        self.assertLessEqual(len(line), inject.LINE_CAP)
        self.assertGreater(len(line), inject.LINE_CAP // 2,
                           "control: the line must still be substantive")
        self.assertNotIn("helm store get", line)
        self.assertTrue(line.endswith("…"), line)
        self.assertEqual(sections["pinned"][1], inject.FOOTER)
        # a short line is untouched
        self.plant_pinned("short-one", "small truth")
        lines = inject.gather("anything")["pinned"]
        short = next(l for l in lines if "short-one" in l)
        self.assertEqual(short, "PREMISE short-one: small truth")


class ProvisionalTagTest(InjectBase):
    """The resolver renders a provisional (xrev-cleared) entry WITH a visible
    [provisional] prefix so an agent can weight it; a candidate fires NOTHING;
    a live entry is untagged (unchanged byte-shape). All three pinned here."""

    def plant_provisional(self, pid, statement, keywords):
        store.write_prior({"id": pid, "statement": statement, "confidence": "0.8",
                           "keywords": keywords, "status": "provisional",
                           "xrev_by": "codex-seat", "xrev_ts": "2026-07-20"})

    def plant_candidate(self, pid, statement, keywords):
        store.write_prior({"id": pid, "statement": statement, "confidence": "0.8",
                           "keywords": keywords, "status": "candidate"})

    def test_three_states_in_the_jit_lane(self):
        self.plant_jit("live-a", "a flux fact", "fluxcap")
        self.plant_provisional("prov-a", "a cleared flux fact", "fluxcap")
        self.plant_candidate("cand-a", "a raw flux guess", "fluxcap")
        jit = inject.gather("tune the fluxcap")["jit"]
        self.assertIn("PRIOR 0.80 live-a: a flux fact", jit)
        self.assertIn("[provisional] PRIOR 0.80 prov-a: a cleared flux fact", jit)
        self.assertFalse(any("cand-a" in l for l in jit),
                         "a candidate must fire NOTHING through inject")

    def test_provisional_prefix_survives_line_truncation(self):  # noqa: VACUOUS_ASSERTION — the line is asserted to start with the tag and end in the ellipsis, and the lane to end in the footer
        # the tag is a PREFIX so the cut can never eat it. The JIT lane cuts
        # at JIT_LINE_CAP, not LINE_CAP, and spends its tail on the route to
        # the full entry rather than on a bare ellipsis.
        self.plant_provisional("long-one", "z " * 400, "fluxcap")
        jit = inject.gather("tune the fluxcap")["jit"]
        line = next(l for l in jit if "long-one" in l)
        from helm.inject import _common
        self.assertLessEqual(len(line), _common.JIT_LINE_CAP)
        self.assertTrue(line.startswith("[provisional] "))
        self.assertTrue(line.endswith("…"), line)
        self.assertEqual(jit[-1], inject.FOOTER)


class FooterTest(InjectBase):
    """ONE ROUTE PER LANE (task/2980). A route on every cut line,
    ` (helm store get <type>:<id>)`, cost about 64 B on 63% of store lines,
    22% of their bytes, repeating the id its own prefix names (MEASURED,
    injection-quality eval e4). A lane ends in ONE footer when any line in it
    carries less than its entry — a cut, a gloss or a first sentence — and no
    line carries a pointer of its own."""

    def gloss_jit(self, pid, statement, keywords, gloss):
        store.write_prior({"id": pid, "statement": statement,
                           "confidence": "0.8", "keywords": keywords,
                           "gloss": gloss})

    def test_the_cut_line_is_the_old_line_less_its_route(self):  # noqa: VACUOUS_ASSERTION — every leg asserts byte-equality with the old renderer's non-empty line; the pointer's absence is the property under test
        # kills: the cut WIDENED into the freed bytes (nothing saved) or
        # NARROWED (rule text lost). The old renderer's arithmetic, spelled
        # out here, is the oracle: the text before the ellipsis is unchanged.
        body = " ".join("w%d" % i for i in range(200))
        for tid, e in (("prior:route-arm", {"type": "prior", "id": "route-arm",
                                            "class": "certain",
                                            "statement": body}),
                       ("heuristic:a-much-longer-route-arm-id-with-many-words",
                        {"type": "heuristic", "statement": body,
                         "id": "a-much-longer-route-arm-id-with-many-words"}),
                       # a 60-character slug ending in a dash: the reserve is
                       # the key's width, not typed_id's longer spelling
                       ("prior:review-independence-is-a-different-model-and"
                        "-no-reviewer-is-",
                        {"type": "prior", "class": "certain", "statement": body,
                         "id": "review-independence-is-a-different-model-and"
                               "-no-reviewer-is-never-a-blocker"})):
            for cap in (inject.JIT_LINE_CAP, inject.LINE_CAP):
                full = inject._entry_line_full(e)
                route = " (helm store get %s)" % tid
                room = cap - len(route) - 1
                cut = full[:room]
                cut = cut[:cut.rfind(" ")] if cut.rfind(" ") > room // 2 else cut
                old = cut.rstrip().rstrip(",;:") + "…" + route
                line = inject._entry_line(e, cap=cap)
                self.assertEqual(line + route, old, (tid, cap))
                self.assertNotIn("helm store get", line)

    def test_each_shorter_line_owes_the_lane_one_footer(self):
        # kills: a footer keyed on the ellipsis alone (the gloss and the
        # first sentence would go uncovered), and a footer per line. Each
        # shape gets its own word, so each lane holds exactly its own pair.
        cases = (
            ("fluxcut", lambda w: self.plant_jit(
                "cut-one", w + " " + "long words " * 40, w)),
            ("fluxgloss", lambda w: self.gloss_jit(
                "gloss-one", "the full %s record, never delivered" % w, w,
                "the %s gloss" % w)),
            ("fluxfirst", lambda w: self.plant_jit(
                "first-one", "The first %s sentence is long enough. A second "
                "one stays in the store." % w, w)),
        )
        for word, plant in cases:
            with self.subTest(shape=word):
                self.plant_jit("whole-" + word, "a whole %s fact" % word, word)
                plant(word)
                jit = inject.gather("tune the " + word)["jit"]
                self.assertEqual(len(jit), 3, jit)
                self.assertEqual(jit.count(inject.FOOTER), 1, jit)
                self.assertEqual(jit[-1], inject.FOOTER, jit)
                self.assertFalse(any("helm store get" in l for l in jit[:-1]),
                                 jit)
        # all three shapes in ONE lane: still one footer
        jit = inject.gather("tune the fluxcut fluxgloss fluxfirst")["jit"]
        self.assertEqual(jit.count(inject.FOOTER), 1, jit)
        self.assertEqual(jit[-1], inject.FOOTER, jit)

    def test_a_lane_of_whole_lines_owes_no_footer(self):
        # kills: an unconditional footer. The control on the SAME lane: one
        # glossed entry beside it and the footer appears
        self.plant_jit("whole-a", "a whole fluxcap fact", "fluxcap")
        self.assertEqual(inject.gather("tune the fluxcap")["jit"],
                         ["PRIOR 0.80 whole-a: a whole fluxcap fact"])
        self.gloss_jit("glossed-b", "a longer fluxcap record", "fluxcap",
                       "the fluxcap gloss")
        self.assertEqual(inject.gather("tune the fluxcap")["jit"][-1],
                         inject.FOOTER)

    def test_the_footer_verb_reads_back_the_line_it_covers(self):  # noqa: VACUOUS_ASSERTION — every leg asserts rc 0 and the entry's own text in the get output
        # kills: a footer naming a verb the line's own tag cannot feed —
        # MOVE, TERM and REF are not store types (store.find_typed LINE_TAGS)
        long = "the full record of this entry. " + "detail " * 60
        store.write_prior({"id": "tag-prior", "statement": long,
                           "confidence": "1.0", "keywords": "tagword"})
        store.write_heuristic({"id": "tag-move", "move": long,
                               "trigger": "tagword"})
        store.write_reference({"id": "tag-ref", "statement": long,
                               "keywords": "tagword"})
        store.write_lexicon({"term": "tag-term", "definition": long,
                             "keywords": "tagword"})
        es = {e["id"]: e for e in store.load_all()}
        for eid in ("tag-prior", "tag-move", "tag-ref", "tag-term"):
            line = inject._entry_line(es[eid], cap=inject.JIT_LINE_CAP,
                                      short=True)
            self.assertTrue(inject._abridged(es[eid], line), line)
            tag, rest = line.split(" ", 1)
            spelled = "%s:%s" % (tag.lower(), rest.split(":", 1)[0])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = store.cmd_store(["get", spelled])
            self.assertEqual(rc, 0, (spelled, out.getvalue()))
            self.assertIn(long.strip()[:40], out.getvalue(), spelled)

    def test_a_capability_line_owes_no_footer(self):
        # `helm store get` cannot read a capability, so the footer would
        # route to nothing; the control is the same line on a prior
        line = "CAP you have a verb: " + "x " * 20 + "…"
        self.assertFalse(inject._abridged({"type": "capability",
                                           "statement": "y"}, line))
        self.assertTrue(inject._abridged({"type": "prior", "id": "p",
                                          "class": "certain",
                                          "statement": "y"}, line))

    def test_the_pinned_footer_is_delivered_once_and_explained(self):  # noqa: VACUOUS_ASSERTION — the first delivery on the same session is asserted to end in the footer; the empty second one is the suppression under test
        # kills: gather and --explain disagreeing about the pinned lane's
        # bytes (the fingerprint covers the footer), and the footer charged
        self.plant_pinned("long-pin", "y " * 400)
        pinned = inject.gather("anything", session="s-foot")["pinned"]
        self.assertEqual(pinned[-1], inject.FOOTER)
        self.assertEqual(inject.gather("anything", session="s-foot")["pinned"],
                         [], "the pinned lane, footer included, fires once")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inject._explain("anything")
        self.assertIn("  + %s ← footer" % inject.FOOTER, out.getvalue())


class SalienceTest(InjectBase):
    def test_no_match_empty_stdout_rc0(self):
        # JIT-only store (empty pinned lane), prompt matches nothing
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, err = self.run_inject([], stdin_text="completely unrelated words")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_empty_store_empty_prompt_rc0(self):
        rc, out, _ = self.run_inject([], stdin_text="")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_match_fires(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject([], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        self.assertIn("PRIOR 0.80 jit-a: a flux fact", out)


class GlossTest(InjectBase):
    """A GLOSS FIRES; THE FULL ENTRY STAYS ON DISK.

    LINE_CAP's comment claimed that for as long as it has existed, and there
    was no gloss — the renderer read `statement` and truncated it. A truncation
    is not a gloss, it is a severed sentence. MEASURED 2026-07-30 on the live
    store: three of the owner's five always-on rules fired at exactly 400 bytes,
    dropping 46-57% of each and ending mid-clause, while the other two could not
    fire at all because 3 x LINE_CAP == PINNED_BUDGET. His only way to make a
    rule fit was to REWRITE HIS OWN CANON shorter — trading the durable record
    for the firing line."""

    def test_the_gloss_fires_and_the_full_statement_survives(self):
        long = "L" * 900
        store.write_prior({"id": "g1", "statement": long, "confidence": "1.0",
                           "pin": "true", "gloss": "the short line that fires"})
        e = next(x for x in store.load_all() if x["id"] == "g1")
        self.assertEqual(len(e["statement"]), 900,
                         "the full statement is the durable record and must survive")
        line = inject._entry_line(e)
        self.assertEqual(line, "PREMISE g1: the short line that fires")
        self.assertFalse(line.endswith("…"), "a gloss fires WHOLE, never cut")

    def test_without_a_gloss_nothing_changes(self):
        """The un-gloss'd path is untouched — a long statement still truncates,
        so this cannot silently alter every entry that has no gloss."""
        long = "L" * 900
        store.write_prior({"id": "g2", "statement": long, "confidence": "1.0",
                           "pin": "true"})
        e = next(x for x in store.load_all() if x["id"] == "g2")
        line = inject._entry_line(e)
        # the cut stays where the old route put it (task/2980): the line
        # keeps its rule text and only the route's bytes leave it
        self.assertEqual(len(line),
                         inject.LINE_CAP - len(" (helm store get prior:g2)"))
        self.assertTrue(line.endswith("…"), line)
        self.assertNotIn("helm store get", line)

    def test_an_oversized_gloss_never_reaches_the_renderer_but_is_cut_if_it_does(self):
        """TWO RAILS, and the first one changed on review.

        This test used to assert that an oversized gloss was silently CUT, and
        codex refuted the premise: accepting a gloss the injector then truncates
        mid-clause recreates the exact severed-sentence failure the gloss exists
        to prevent, one remove away and quietly. So the WRITER now REFUSES it
        with the limit measured on the real rendered line.

        The renderer still truncates defensively, because a hand-authored file
        or an entry written before the refusal existed can still carry one, and
        the budget is the budget."""
        with self.assertRaises(ValueError) as cm:      # rail 1: never written
            store.write_prior({"id": "g3", "statement": "short",
                               "confidence": "1.0", "pin": "true",
                               "gloss": "G" * 900})
        self.assertIn("gloss too long", str(cm.exception))
        e = {"type": "prior", "id": "g3b", "class": "certain",
             "statement": "short", "gloss": "G" * 900}   # rail 2: got in anyway
        line = inject._entry_line(e)
        self.assertEqual(len(line),
                         inject.LINE_CAP - len(" (helm store get prior:g3b)"))
        self.assertTrue(line.endswith("…"), line)
        self.assertNotIn("helm store get", line)

    def test_an_oversized_steer_is_truncated_at_STEER_CAP_with_an_ellipsis(self):
        """#reflex-steers-are-uncapped: a 943B steer wall reaches the inject
        verbatim every turn (pack norm 101-137B; reflex.py:44 'steers are
        terse FACTS'). The renderer caps it at STEER_CAP with an ellipsis —
        over the cap it is cut, under it it passes through untouched
        (mutation: over -> truncated, under -> untouched)."""
        import sys as _sys
        from helm.inject import _common
        import helm.inject._whisper as _wi  # noqa: F401 — binds the module in sys.modules
        _whisper_mod = _sys.modules["helm.inject._whisper"]  # the package's
        # `from ._whisper import _whisper` shadows the module attr with the fn
        over = "REFLEX: " + ("a null from a scan is a fact about your query. " * 40)
        under = "REFLEX: a null from a scan is a fact about your query, not the world."
        self.assertGreater(len(over), _common.STEER_CAP)
        self.assertLessEqual(len(under), _common.STEER_CAP)
        capped_over = _whisper_mod._cap_steer(over)
        self.assertLessEqual(len(capped_over), _common.STEER_CAP)
        self.assertTrue(capped_over.endswith("…"))
        self.assertEqual(_whisper_mod._cap_steer(under), under)

    def test_a_glossed_lane_fits_where_the_truncated_one_could_not(self):
        """The whole point, end to end: five rules that cannot all fire when
        each renders at LINE_CAP, all firing once glossed."""
        for i in range(5):
            store.write_prior({"id": "p%d" % i, "statement": "S" * 900,
                               "confidence": "1.0", "pin": "true"})
        ungloss = [inject._entry_line(e) for e in store.pinned()]
        self.assertGreater(sum(len(l) for l in ungloss), inject.PINNED_BUDGET,
                           "un-glossed, the lane cannot hold all five")
        for i in range(5):
            e = next(x for x in store.load_all() if x["id"] == "p%d" % i)
            e["gloss"] = "rule %d in one short line" % i
            store.write_prior(e)
        glossed = [inject._entry_line(e) for e in store.pinned()]
        self.assertEqual(len(glossed), 5)
        self.assertLessEqual(sum(len(l) for l in glossed), inject.PINNED_BUDGET,
                             "glossed, every rule reaches the seat")


class ShapeTest(InjectBase):
    def test_json_shape(self):
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject(["--json"], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(sorted(d), ["jit", "pinned", "reflex", "whisper"])
        for k in ("pinned", "jit", "reflex"):
            self.assertIsInstance(d[k], list)
        self.assertEqual(d["pinned"], ["PREMISE pin-a: always truth"])
        self.assertEqual(d["jit"], ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertEqual(d["reflex"], [])

    def test_json_no_match_is_empty_lists_not_silence(self):
        rc, out, _ = self.run_inject(["--json"], stdin_text="nothing")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out),
                         {"whisper": [], "pinned": [], "jit": [], "reflex": []})

    def test_inline_arg_beats_stdin(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject(["tune the fluxcap"],
                                     stdin_text="no matching words here")
        self.assertEqual(rc, 0)
        self.assertIn("jit-a", out)
        # and the reverse: inline no-match wins over a matching stdin
        rc, out, _ = self.run_inject(["unrelated"], stdin_text="tune the fluxcap")
        self.assertEqual(out, "")

    def test_project_flag_scopes_the_store(self):
        store.write_prior({"id": "p-pin", "statement": "project truth",
                           "confidence": "1.0", "pin": "true"},
                          root_dir=os.path.join(home.project_dir("p1"), "premises"))
        self.assertEqual(inject.gather("x", project="p1")["pinned"],
                         ["PREMISE p-pin: project truth"])
        self.assertEqual(inject.gather("x")["pinned"], [])


class FailOpenTest(InjectBase):
    def test_store_raise_injects_nothing_rc0(self):
        with mock.patch.object(store, "load_all",
                               side_effect=RuntimeError("store exploded")):
            rc, out, err = self.run_inject([], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_reflex_raise_keeps_store_lanes(self):
        self.plant_pinned("pin-a", "always truth")
        from helm import reflex
        with mock.patch.object(reflex, "fire",
                               side_effect=RuntimeError("reflex exploded")):
            rc, out, _ = self.run_inject([], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn("PREMISE pin-a: always truth", out)

    def test_corrupt_entry_is_skipped_not_fatal(self):
        self.plant_pinned("pin-a", "always truth")
        bad = os.path.join(os.environ["HELM_ADOPTED_DIR"], "prior-garbled.md")
        with open(bad, "w") as f:
            f.write("---\nname: prior-garbled\n")  # unterminated frontmatter
        rc, out, _ = self.run_inject([], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn("pin-a", out)


class CacheTest(InjectBase):
    def test_second_call_serves_from_cache_without_reparsing(self):
        self.plant_pinned("pin-a", "always truth")
        first = inject.gather("anything")
        cache = inject._cache_file()
        self.assertTrue(os.path.exists(cache))
        # disk parse forbidden -> the warm cache must carry the call alone
        with mock.patch.object(store, "load_all",
                               side_effect=AssertionError("second parse!")):
            self.assertEqual(inject.gather("anything"), first)

    def test_a_cache_written_by_the_old_classifier_is_not_replayed(self):
        """task/2547 r1 P2: the cure changed the DERIVATION, and a
        warm persistent cache carries the pre-cure answer. `_store_sig` covers
        store paths, mtimes and sizes only, so after an upgrade with no store
        mutation the version and signature still matched and load_entries
        returned the stale filtered list — the UUID-path foreign row stayed
        withheld until something touched the store. The cache version is the
        derivation's identity, so it moves with it."""
        import json as _json
        from helm.inject import _entries
        pre_cure_version = 4      # the version the pre-cure derivation wrote
        self.plant_pinned("uuid-row", "verify the checkout at /tmp/c/p/"
                          "1953843d-c02a-498e-b8ca-47dce667fcb3/clientproj-wt/room")
        fresh = _entries.load_entries(project="clientproj")
        ids = [e.get("id") for e in fresh]
        # MUST-HIT: the cured derivation admits the row at all, so the stale
        # replay below is about the CACHE and not about the classifier.
        self.assertIn("uuid-row", ids)
        path = _entries._cache_file(project="clientproj")
        sig = _entries._store_sig(project="clientproj")
        stale = [e for e in fresh if e.get("id") != "uuid-row"]
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"v": pre_cure_version, "sig": sig,
                        "entries": stale}, f)
        self.assertEqual(
            [e.get("id") for e in _entries.load_entries(project="clientproj")], ids,
            "a cache written by the previous derivation was replayed")
        # CONTROL on the same observable: a cache at the CURRENT version with
        # the same signature is still served, so the arm above is about the
        # version and not about caching being off.
        from helm.inject import _common
        self.assertGreater(_common._CACHE_VERSION, pre_cure_version,
                           "the derivation changed, so its cache identity must")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"v": _common._CACHE_VERSION, "sig": sig,
                        "entries": stale}, f)
        self.assertEqual(
            [e.get("id") for e in _entries.load_entries(project="clientproj")],
            [e.get("id") for e in stale])

    def test_lane_df_is_the_same_map_the_direct_pass_computes(self):
        """The df cache must be the df, cold and warm, or the JIT lane re-ranks.

        `helm inject` is CPU-bound (measured on the live tree: 98% CPU, ~0.6s
        per turn, no blocking IO), so its WALL cost is that figure
        times how oversubscribed the box is — which is why the installed hook's
        10s `timeout` fires in fleet-wide bursts and a seat silently loses its
        whole brief. _df_map was one full-corpus pass paid TWICE per turn for an
        answer that changes only when the store does; it is now computed once
        and cached. A df map that is merely CLOSE is worse than none: every
        matched probe scores 1/df, so a wrong denominator silently reorders the
        cap-4 winners. Both arms therefore compare against the direct pass."""
        from helm.inject import _entries
        self.plant_jit("jit-a", "alpha rule", "alpha,shared")
        self.plant_jit("jit-b", "beta rule", "beta,shared")
        entries = _entries._lane_entries()
        cand = store._jit_candidates(entries)
        truth = store._df_map(cand)
        # MUST-HIT control on the same observable: the corpus really is loaded
        # and really does carry the shared probe, so the equalities below are
        # over a populated map and not two empty dicts agreeing.
        self.assertGreaterEqual(len(cand), 2)
        self.assertEqual(truth.get("shared"), 2)
        cold = _entries.lane_df(entries)          # writes the cache
        warm = _entries.lane_df(entries)          # reads it back
        self.assertEqual(cold, truth, "cold lane_df diverged from _df_map")
        self.assertEqual(warm, truth, "the cached df diverged from _df_map")
        self.assertTrue(os.path.exists(_entries._df_cache_file()))

    def test_a_changed_corpus_is_not_served_the_old_df(self):
        """The key is the df's own inputs, so a new entry moves it.

        The parsed-entry cache keys on the store SIGNATURE; this one cannot,
        because the candidate set is store entries PLUS the live capability
        index, which never enters that signature. Keying on the inputs is what
        makes the two corpora impossible to confuse."""
        from helm.inject import _entries
        self.plant_jit("jit-a", "alpha rule", "alpha,shared")
        before = _entries.lane_df(_entries._lane_entries())
        self.assertEqual(before.get("shared"), 1)   # control: the probe is there
        self.plant_jit("jit-b", "beta rule", "beta,shared")
        after = _entries.lane_df(_entries._lane_entries())
        self.assertEqual(after.get("shared"), 2,
                         "a stale df was replayed over a changed corpus")
        self.assertNotEqual(
            _entries._df_key(store._jit_candidates(_entries._lane_entries())),
            _entries._df_key([]),
            "the key must distinguish corpora")

    def test_resolve_prompt_ranks_identically_with_a_supplied_df(self):
        """df= is a threaded value, never a different weighting."""
        from helm.inject import _entries
        self.plant_jit("jit-rare", "the rare one", "quokka")
        self.plant_jit("jit-common", "the common one", "shared,quokka")
        entries = _entries._lane_entries()
        plain = store.resolve_prompt("quokka", cap=4, entries=entries)
        threaded = store.resolve_prompt("quokka", cap=4, entries=entries,
                                        df=_entries.lane_df(entries))
        # MUST-HIT: the lane actually fired, so equal-and-empty cannot pass.
        self.assertTrue(plain, "control: the probe matched nothing")
        self.assertEqual([e["id"] for e in plain], [e["id"] for e in threaded])

    def test_lanes_threads_one_df_to_both_consumers(self):
        """_lanes returns the map gather's cooldown scorer reuses."""
        from helm.inject import _entries
        self.plant_jit("jit-a", "alpha rule", "alpha,shared")
        pinned_entries, jit, entries, df = _entries._lanes("alpha")
        self.assertEqual(df, store._df_map(store._jit_candidates(entries)))
        self.assertTrue(df, "control: the df map is populated")

    def test_store_change_invalidates_cache(self):
        self.plant_pinned("pin-a", "always truth")
        inject.gather("anything")  # warm the cache
        self.plant_pinned("pin-b", "newer truth")
        lines = inject.gather("anything")["pinned"]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("pin-b" in l for l in lines))

    def test_gather_is_one_parse_per_call(self):
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        calls = []
        real = store.load_all

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with mock.patch.object(store, "load_all", counting):
            sections = inject.gather("tune the fluxcap")
        self.assertEqual(len(calls), 1,
                         "gather must parse the store ONCE (was twice pre-fix)")
        self.assertTrue(sections["pinned"] and sections["jit"])

    def test_warm_cache_short_circuits_store_parse(self):
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        first = inject.gather("tune the fluxcap")
        self.assertTrue(first["pinned"] and first["jit"])
        # sentinel: ANY real parse now explodes. The warm cache + the entries=
        # seam must carry the whole call; a regression (a lane reaching for
        # load_all/disk again) trips fail-open -> EMPTY lanes -> loud inequality.
        with mock.patch.object(store, "_load_root",
                               side_effect=AssertionError("store parsed twice")), \
                mock.patch.object(store, "load_all",
                                  side_effect=AssertionError("load_all called twice")):
            self.assertEqual(inject.gather("tune the fluxcap"), first)

    def test_unwritable_cache_dir_still_serves(self):
        blocker = os.path.join(self.tmp, "not-a-dir")
        with open(blocker, "w") as f:
            f.write("x")
        os.environ["HELM_CACHE_DIR"] = os.path.join(blocker, "cache")  # mkdir fails
        self.plant_pinned("pin-a", "always truth")
        self.assertEqual(inject.gather("x")["pinned"], ["PREMISE pin-a: always truth"])


class LedgerTest(InjectBase):
    def rows(self):
        return list(inject._ledger_rows())

    def test_fired_row_shape_ids_never_prompt_text(self):  # noqa: VACUOUS_ASSERTION — the non-empty pinned and JIT fixtures positively control the raw-prompt absence check
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, _, _ = self.run_inject([], stdin_text="tune the fluxcap now")
        self.assertEqual(rc, 0)
        self.assertEqual(inject._ledger_path(), os.path.join(
            os.environ["HELM_HOME"], "_global", ".state", "inject-ledger.jsonl"))
        r, = self.rows()
        self.assertEqual(r["v"], 2)
        self.assertTrue(r["ts"])
        self.assertIsNone(r["project"])
        self.assertEqual(r["fired"], {"pinned": ["pin-a"], "jit": ["jit-a"], "reflex": []})
        self.assertEqual(r["sample"]["encoding"], "utf-8")
        self.assertGreater(r["sample"]["lane_bytes"]["pinned"], 0)
        self.assertGreater(r["sample"]["lane_bytes"]["jit"], 0)
        self.assertEqual(r["sample"]["lane_bytes"]["reflex"], 0)
        self.assertGreater(r["sample"]["rendered_bytes"],
                           r["sample"]["lane_bytes"]["pinned"])
        self.assertNotIn("bytes", r, "v1 character counts must not masquerade as v2 bytes")
        self.assertEqual(r["candidates"], 2)
        self.assertGreaterEqual(r["elapsed_ms"], 0)
        self.assertNotIn("silent", r)
        # entry IDS only — the raw line must never carry the prompt
        with open(inject._ledger_path(), encoding="utf-8") as f:
            self.assertNotIn("tune the fluxcap", f.read())
        # jsonl append: a second call adds exactly one more row
        self.run_inject([], stdin_text="tune the fluxcap now")
        self.assertEqual(len(self.rows()), 2)

    def test_v3_sample_matches_stdout_and_only_agent_runtime_mints_context(self):  # noqa: VACUOUS_ASSERTION — the owner-positive exact UTF-8 row controls the owner-unavailable negative on the same producer path
        self.plant_pinned("pin-unicode", "snowman ☃ and café")
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        config_home = os.path.join(self.tmp, "explicit-home")
        runtime = {"pid": 77, "start": "123", "session": "session-v3",
                   "cwd": cwd, "harness": "claude",
                   "config_home": config_home,
                   "config_home_source": "CLAUDE_CONFIG_DIR"}
        with mock.patch.dict(os.environ, {
                "HELM_AGENT_HARNESS": "claude", "CLAUDE_PID": "77",
                "CLAUDE_CONFIG_DIR": "/hook-must-not-win"}, clear=False), \
                mock.patch("helm.session.runtime_config_for_session",
                           return_value=(runtime, None)):
            sections = inject.gather("anything", session="session-v3", cwd=cwd)
        rendered = "\n".join(line for lane in
                             ("whisper", "pinned", "jit", "reflex")
                             for line in sections[lane])
        stdout = rendered + ("\n" if rendered else "")
        r = self.rows()[-1]
        self.assertEqual(r["v"], 3)
        self.assertEqual(r["sample"]["rendered_bytes"], len(stdout.encode("utf-8")))
        self.assertGreater(r["sample"]["rendered_bytes"], len(stdout),
                           "non-ASCII output distinguishes UTF-8 bytes from characters")
        self.assertEqual(r["context"], {
            "session": "session-v3", "cwd": cwd, "harness": "claude",
            "config_home": config_home})
        self.assertEqual(r["context_sources"]["config_home"],
                         "CLAUDE_CONFIG_DIR")
        self.assertEqual(r["runtime"], {"pid": 77, "proc_start": "123"})
        self.assertNotEqual(r["context"]["config_home"], "/hook-must-not-win")

        with mock.patch.dict(os.environ, {
                "HELM_AGENT_HARNESS": "claude", "CLAUDE_PID": "77",
                "CLAUDE_CONFIG_DIR": "/hook-must-not-win", "HOME": "/ambient"},
                clear=False), mock.patch(
                    "helm.session.runtime_config_for_session",
                    return_value=(None, "environ-unreadable")):
            inject.gather("anything", session="session-unknown", cwd=cwd)
        unknown = self.rows()[-1]
        self.assertEqual(unknown["v"], 3)
        self.assertEqual(unknown["context"], {})
        self.assertEqual(unknown["context_unavailable"], "environ-unreadable")
        self.assertNotEqual(unknown["context"].get("config_home"),
                            os.path.expanduser("~/.claude"))

        for pid in (None, "not-a-pid", "0", "-7"):
            env = {"HELM_AGENT_HARNESS": "claude",
                   "CLAUDE_CONFIG_DIR": "/hook-must-not-win",
                   "HOME": "/ambient"}
            if pid is not None:
                env["CLAUDE_PID"] = pid
            with self.subTest(pid=pid), mock.patch.dict(
                    os.environ, env, clear=False), mock.patch(
                        "helm.session.runtime_config_for_session") as owner:
                if pid is None:
                    os.environ.pop("CLAUDE_PID", None)
                inject.gather("anything", session="session-no-pid", cwd=cwd)
            refused = self.rows()[-1]
            self.assertEqual(refused["v"], 3)
            self.assertEqual(refused["context"], {})
            self.assertEqual(refused["context_unavailable"],
                             "agent-pid-unavailable")
            owner.assert_not_called()

    def test_provenance_timeout_consumes_no_injection_latches(self):  # noqa: VACUOUS_ASSERTION — empty output and three untouched mutation seams are the required fail-open result
        with mock.patch("helm.inject._whisper._sample_context",
                        side_effect=TimeoutError), \
                mock.patch("helm.inject._whisper._seen_save") as seen, \
                mock.patch("helm.inject._whisper.reflex.fire") as reflex_fire, \
                mock.patch("helm.inject._whisper._ledger_finish") as finish:
            sections = inject.gather("anything", session="timeout", cwd="/turn")
        self.assertEqual(sections, {lane: [] for lane in
                                   ("whisper", "pinned", "jit", "reflex")})
        seen.assert_not_called()
        reflex_fire.assert_not_called()
        finish.assert_not_called()

    def test_pi_explicit_agent_dir_is_recorded_without_defaulting(self):
        pi_home = os.path.join(self.tmp, "pi-home")
        with mock.patch.dict(os.environ, {
                "PI_CODING_AGENT": "true",
                "PI_CODING_AGENT_DIR": pi_home}, clear=False):
            inject.gather("anything", session="pi-session")
        row = self.rows()[-1]
        self.assertEqual(row["context"], {
            "session": "pi-session", "harness": "pi",
            "config_home": os.path.realpath(pi_home)})
        self.assertEqual(row["context_sources"], {
            "session": "hook-json", "harness": "PI_CODING_AGENT",
            "config_home": "PI_CODING_AGENT_DIR"})

    def test_malformed_explicit_harness_blocks_ambient_fallback(self):  # noqa: VACUOUS_ASSERTION — a valid explicit harness first proves the same ledger context observable can carry harness authority before each malformed ambient arm proves its absence
        with mock.patch.dict(os.environ, {
                "HELM_AGENT_HARNESS": "pi",
                "PI_CODING_AGENT_DIR": os.path.join(self.tmp, "valid-home")},
                clear=False):
            inject.gather("anything", session="explicit-valid")
        self.assertEqual(self.rows()[-1]["context"]["harness"], "pi")
        ambient = (
            {"PI_CODING_AGENT": "true",
             "PI_CODING_AGENT_DIR": os.path.join(self.tmp, "pi-home")},
            {"CLAUDE_CODE_SESSION_ID": "ambient-session",
             "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "claude-home")},
        )
        for n, env in enumerate(ambient):
            with self.subTest(env=env), mock.patch.dict(
                    os.environ, dict(env, HELM_AGENT_HARNESS="not a harness!"),
                    clear=False):
                inject.gather("anything", session="explicit-invalid-%d" % n)
            row = self.rows()[-1]
            self.assertEqual(row["context"], {
                "session": "explicit-invalid-%d" % n})
            self.assertEqual(row["context_sources"], {"session": "hook-json"})

    def test_silent_turn_logs_silent_row(self):  # noqa: VACUOUS_ASSERTION — the written v2 zero-byte row positively controls absence of fired and legacy byte fields
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject([], stdin_text="completely unrelated words")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        r, = self.rows()
        self.assertIs(r["silent"], True)
        self.assertEqual(r["v"], 2)
        self.assertEqual(r["sample"]["rendered_bytes"], 0)
        self.assertNotIn("fired", r)
        self.assertNotIn("bytes", r)

    def test_rotation_at_5mb_one_generation(self):
        self.assertEqual(inject.LEDGER_MAX, 5 * 1024 * 1024)
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path))
        legacy_x = json.dumps({"legacy": "x" * inject.LEDGER_MAX}) + "\n"
        with open(path, "w") as f:
            f.write(legacy_x)
        self.plant_pinned("pin-a", "always truth")
        self.run_inject([], stdin_text="anything")
        self.assertEqual(len(self.rows()), 1)  # fresh file: this turn's row only
        with open(path + ".1", encoding="utf-8") as f:
            self.assertIn('"legacy": "xxx', f.read())
        # ONE generation: the next rotation replaces .1, never mints .2
        legacy_y = json.dumps({"legacy": "y" * inject.LEDGER_MAX}) + "\n"
        with open(path, "a") as f:
            f.write(legacy_y)
        self.run_inject([], stdin_text="anything")
        rows = self.rows()
        self.assertEqual(len(rows), 2,
                         "the rotated .1 keeps its prior canonical row")
        self.assertTrue(all(row["v"] == 2 for row in rows),
                        "the hostile legacy row is not promoted to telemetry")
        self.assertFalse(os.path.exists(path + ".2"))
        with open(path + ".1", encoding="utf-8") as f:
            self.assertIn('"legacy": "yyy', f.read())

    def test_unwritable_ledger_fails_open(self):
        # a FILE where the .state dir belongs -> every ledger write raises
        g = os.path.join(os.environ["HELM_HOME"], "_global")
        os.makedirs(g)
        with open(os.path.join(g, ".state"), "w") as f:
            f.write("x")
        self.plant_pinned("pin-a", "always truth")
        rc, out, err = self.run_inject([], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "",
                         "no durable intent means no Helm delivery")
        self.assertEqual(err, "")

    def test_explain_names_the_root_per_line(self):
        """Discovery attribution: every explain line says which STORE ROOT it
        came from (adopted / helm-global / project) — the owner can trace a
        fired line to its file's home at a glance."""
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        from helm import store
        root = store.pinned()[0]["root"]
        rc, out, _ = self.run_inject(["--explain"], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        self.assertIn("+ PREMISE pin-a: always truth \u2190 %s" % root, out)
        self.assertRegex(out, r"\+ jit-a \[matched: .*\] score [0-9.]+ \u2190 " + root)

    def test_explain_prints_why_and_writes_no_ledger(self):
        rc, out, _ = self.run_inject(["--explain"], stdin_text="nothing here")
        self.assertEqual(rc, 0)
        self.assertIn("silent turn", out)
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject(["--explain"], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        self.assertIn("pinned (1 candidate, budget %dB):" % inject.PINNED_BUDGET, out)
        self.assertIn("+ PREMISE pin-a: always truth", out)
        self.assertIn("jit (1 hit, cap %d):" % inject.JIT_CAP, out)
        # per-hit DF contributions: fluxcap is unique among candidates (df=1,
        # weight 1.000); score = confidence 0.8 * 1.0
        self.assertIn("+ jit-a [matched: fluxcap=1.000] score 0.800", out)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")
        # over-cap hits are shown, marked, and still not fired
        for i in range(6):
            self.plant_jit("jit-%d" % i, "flux fact %d" % i, "fluxcap")
        rc, out, _ = self.run_inject(["--explain"], stdin_text="tune the fluxcap")
        self.assertEqual(out.count("(over cap)"), 3)  # 7 hits, cap 4


class HookJsonTest(InjectBase):
    """--hook-json: the harness hook payload on stdin + cwd->project scope
    derivation (longest-prefix over registry paths, global fallback)."""

    def setUp(self):
        super().setUp()
        # these session turns are not the day's first — hold the whisper latched
        # so no real brief compose rides them (WhisperTest owns that lane)
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def seed_registry(self, **paths):
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            n: {"name": n, "path": p, "kind": "git", "status": "active",
                "sessions": {}} for n, p in paths.items()}})

    def hook_stdin(self, prompt, cwd=None, session="sid-1", **extra):
        d = {"prompt": prompt, "cwd": cwd, "session_id": session,
             "hook_event_name": "UserPromptSubmit", **extra}
        return json.dumps({k: v for k, v in d.items() if v is not None})

    def ledger_rows(self):
        return list(inject._ledger_rows())

    def test_parse_extracts_and_tolerates_unknown_keys(self):
        p, c, s = inject.parse_hook_json(self.hook_stdin(
            "tune the fluxcap", cwd="/tmp/x", transcript_path="/t.jsonl"))
        self.assertEqual((p, c, s), ("tune the fluxcap", "/tmp/x", "sid-1"))
        self.assertEqual(inject.parse_hook_json("{}"), ("", None, None))

    def test_malformed_json_fails_open_empty_rc0(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        for bad in ("not json{", "[1, 2]", '"a string"', ""):
            rc, out, err = self.run_inject(["--hook-json"], stdin_text=bad)
            self.assertEqual((rc, out, err), (0, "", ""), bad)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "a garbled payload must not ledger a turn")

    def test_hook_json_fires_like_plain_stdin(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject(["--hook-json"],
                                     stdin_text=self.hook_stdin("tune the fluxcap"))
        self.assertEqual(rc, 0)
        self.assertIn("PRIOR 0.80 jit-a: a flux fact", out)

    def test_cwd_derives_project_longest_prefix_wins(self):
        outer = os.path.join(self.tmp, "repos", "outer")
        inner = os.path.join(outer, "inner")
        self.seed_registry(outer=outer, inner=inner)
        for name in ("outer", "inner"):
            store.write_prior(
                {"id": name + "-pin", "statement": name + " truth",
                 "confidence": "1.0", "pin": "true"},
                root_dir=os.path.join(home.project_dir(name), "premises"))
        rc, out, _ = self.run_inject(
            ["--hook-json"],
            stdin_text=self.hook_stdin("x", cwd=os.path.join(inner, "sub")))
        self.assertEqual(rc, 0)
        self.assertIn("inner-pin", out)   # deepest registered path wins
        self.assertNotIn("outer-pin", out)
        r = self.ledger_rows()[-1]
        self.assertEqual(r["project"], "inner")
        self.assertEqual(r["session"], "sid-1")
        # prefix is path-boundary, not string-boundary: outerX is NOT outer
        self.assertIsNone(inject.project_for_cwd(outer + "X"))
        self.assertEqual(inject.project_for_cwd(outer), "outer")

    def test_a_LANE_WORKTREE_resolves_to_its_repos_project(self):
        """`helm work claim` mints rooms at <repo>-wt/<lane>, a SIBLING of the
        repo path, so no registered prefix matched and the nearest ANCESTOR
        project won instead.

        Measured live 2026-08-05: every helm lane worktree resolved to the
        UMBRELLA project, which registers the parent directory holding every
        repo. Four surfaces read this
        one derivation — turn premises and reflexes, `helm store add` project
        inference, the handoff journal shelf, and dispatch scoping — so a seat
        working in the room it is REQUIRED to claim got another project's
        answer for all four, and `helm handoff check` reported "contract
        satisfied" against the wrong shelf. It fails toward SILENCE, which is
        why it survived so long."""
        repo = os.path.join(self.tmp, "repos", "proj")
        parent = os.path.join(self.tmp, "repos")
        self.seed_registry(proj=repo, umbrella=parent)
        # MUST-HIT CONTROL: the repo itself still resolves, so a None below
        # would mean "not matched" rather than "registry unreadable".
        self.assertEqual(inject.project_for_cwd(repo), "proj")
        for room in (repo + "-wt",
                     os.path.join(repo + "-wt", "some-lane"),
                     os.path.join(repo + "-wt", "peeks", "abc123")):
            with self.subTest(room=room):
                self.assertEqual(inject.project_for_cwd(room), "proj",
                                 "a lane worktree fell through to the "
                                 "ancestor project")

    def test_a_LOOKALIKE_sibling_is_not_captured_by_the_worktree_rule(self):
        """The suffix is matched with its separator. A sibling repo whose name
        merely starts the same must not be swallowed, or the fix would trade a
        silent mis-scope for a louder one."""
        repo = os.path.join(self.tmp, "repos", "proj")
        parent = os.path.join(self.tmp, "repos")
        self.seed_registry(proj=repo, umbrella=parent)
        self.assertEqual(inject.project_for_cwd(repo + "-wt"), "proj")
        for stranger in (repo + "-wtx", repo + "ect", repo + "-widget"):
            with self.subTest(stranger=stranger):
                self.assertEqual(inject.project_for_cwd(stranger), "umbrella",
                                 "a lookalike sibling was captured")

    def test_a_REGISTERED_project_outranks_a_derived_worktree_root(self):
        """A repo genuinely NAMED "<x>-wt" beside "<x>" matches both rules at
        the same depth, and the row someone actually registered is the one that
        means it.

        I found this by writing the case down as untested in a review request
        and then measuring it instead of handing it over: before the rank, the
        winner depended on registry ITERATION ORDER, which is the kind of
        answer that is right until someone adds a project."""
        real = os.path.join(self.tmp, "repos", "proj")
        twin = real + "-wt"                       # a real repo, really named that
        self.seed_registry(proj=real, twin=twin)
        # MUST-HIT CONTROL: both rows resolve at all.
        self.assertEqual(inject.project_for_cwd(real), "proj")
        for cwd in (twin, os.path.join(twin, "sub")):
            with self.subTest(cwd=cwd):
                self.assertEqual(inject.project_for_cwd(cwd), "twin",
                                 "a derived worktree root outranked a "
                                 "registered project at the same depth")

    def test_the_derived_rule_still_applies_where_nothing_is_registered(self):
        """The other direction: without a twin, the worktree rule must still
        carry the room to its repo — otherwise the precedence fix would have
        quietly undone the thing it was added to."""
        real = os.path.join(self.tmp, "repos", "solo")
        parent = os.path.join(self.tmp, "repos")
        self.seed_registry(solo=real, umbrella=parent)
        self.assertEqual(
            inject.project_for_cwd(os.path.join(real + "-wt", "lane")), "solo")

    def test_a_THREE_DEEP_nest_composes_without_a_special_case(self):
        """Longest-prefix and the registration rank together, on the shape a
        real machine has: an umbrella over repos, a repo, and a repo nested
        inside it — each with its own worktree root.

        Pinned rather than probed. On the sibling lane my mutation matrix
        killed three of five and TWO SURVIVED, because I had proven those
        behaviours in a shell probe and never put them in the suite.
        Correct-but-untested is how a fix gets undone by someone with no way
        to know it was deliberate."""
        umbrella = os.path.join(self.tmp, "d")
        outer = os.path.join(umbrella, "outer")
        inner = os.path.join(outer, "inner")
        self.seed_registry(umbrella=umbrella, outer=outer, inner=inner)
        # MUST-HIT CONTROL: the deepest registered path still wins outright,
        # so the worktree arms below are additions and not a replacement.
        self.assertEqual(inject.project_for_cwd(os.path.join(inner, "sub")),
                         "inner")
        cases = (
            (inner + "-wt", "inner"),                       # worktree root
            (os.path.join(inner + "-wt", "lane"), "inner"),  # deepest project's
            (os.path.join(outer + "-wt", "lane"), "outer"),  # not the umbrella
            # a path that merely LOOKS nested, inside outer's worktree
            (os.path.join(outer + "-wt", "inner", "sub"), "outer"),
            (os.path.join(umbrella, "elsewhere"), "umbrella"),
        )
        for cwd, want in cases:
            with self.subTest(cwd=cwd):
                self.assertEqual(inject.project_for_cwd(cwd), want)

    def test_unregistered_cwd_falls_back_to_global(self):
        self.seed_registry(p1=os.path.join(self.tmp, "repos", "p1"))
        self.plant_pinned("g-pin", "global truth")
        rc, out, _ = self.run_inject(
            ["--hook-json"],
            stdin_text=self.hook_stdin("x", cwd=os.path.join(self.tmp, "elsewhere")))
        self.assertEqual(rc, 0)
        self.assertIn("g-pin", out)
        r = self.ledger_rows()[-1]
        self.assertIsNone(r["project"])
        self.assertEqual(r["session"], "sid-1")

    def test_explicit_project_flag_beats_derivation(self):
        outer = os.path.join(self.tmp, "repos", "outer")
        self.seed_registry(outer=outer)
        store.write_prior({"id": "p2-pin", "statement": "p2 truth",
                           "confidence": "1.0", "pin": "true"},
                          root_dir=os.path.join(home.project_dir("p2"), "premises"))
        rc, out, _ = self.run_inject(
            ["--hook-json", "--project", "p2"],
            stdin_text=self.hook_stdin("x", cwd=outer))
        self.assertIn("p2-pin", out)
        self.assertEqual(self.ledger_rows()[-1]["project"], "p2")

    def test_explain_shows_scope_line_only_when_derived(self):
        outer = os.path.join(self.tmp, "repos", "outer")
        self.seed_registry(outer=outer)
        rc, out, _ = self.run_inject(
            ["--hook-json", "--explain"], stdin_text=self.hook_stdin("x", cwd=outer))
        self.assertEqual(rc, 0)
        self.assertIn("[scope: outer via %s]" % outer, out)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")
        rc, out, _ = self.run_inject(
            ["--hook-json", "--explain"],
            stdin_text=self.hook_stdin("x", cwd=os.path.join(self.tmp, "elsewhere")))
        self.assertNotIn("[scope:", out)

    # ------------------------------------------------------------------
    # A PREMISE IS INJECTED ONLY INTO THE PROJECT IT IS ABOUT (task/2435).
    #
    # The owner's sentence: "an entry carries the PROJECT IT IS ABOUT — a
    # project name from the registry, or the value fleet for owner policy that
    # applies everywhere. A seat receives only fleet entries plus entries of
    # its own project."
    #
    # MEASURED PRODUCER (helm/store/load.py:92-93): roots() returns the
    # ("helm-global", "global", home.global_dir()) triple UNCONDITIONALLY
    # ahead of any project root, load_all merges every file it holds, and
    # nothing downstream ever compares an entry's scope to the requested
    # project. So the 641-file _global store — where helm's OWN seat-routing
    # knowledge lives, because `store add` deliberately homes to _global
    # without --project (cli.py:143-152) — was fed to every project's inject
    # lane. THE FAILURE MODE: a premise about ONE project's seat routing —
    # which seats a review may go to while the owner is away — fired into a
    # turn in an unrelated project, as authoritative canon.
    #
    # These arms plant through the SHIPPED PRODUCER — store.write_prior into
    # the real global/project dirs, the real registry file, the real
    # --hook-json CLI — never a dict handed to the filter.
    # ------------------------------------------------------------------
    def _two_projects(self):
        helm_dir = os.path.join(self.tmp, "repos", "helm")
        clientproj = os.path.join(self.tmp, "repos", "clientproj")
        self.seed_registry(helm=helm_dir, clientproj=clientproj)
        return helm_dir, clientproj

    def _plant_global(self, pid, statement, owner=None, **fields):
        """A pinned premise in the GLOBAL root — the root every project is
        admitted to — optionally RECORDING the project it is about."""
        e = {"id": pid, "statement": statement, "confidence": "1.0",
             "pin": "true", **fields}
        if owner is not None:
            e["project"] = owner
        store.write_prior(e)

    def _plant_adopted(self, pid, statement, owner=None, **fields):
        """A pinned premise in the ADOPTED root — the owner's own live claude
        memory store, which is FLAT (no per-type subdir) — optionally RECORDING
        the project it is about. This is the root that carries the canon every
        seat is meant to hold."""
        e = {"id": pid, "statement": statement, "confidence": "1.0",
             "pin": "true", **fields}
        if owner is not None:
            e["project"] = owner
        store.write_prior(e, root_dir=store.adopted_dir())

    def _seat_authority(self, *names):
        """Arm the ONE seat-name authority with these identities and PROVE the
        reader agrees they are armed there.

        THE DERIVATION NO LONGER ASKS IT. This helper exists so the arms below
        can establish that the seat-name input EXISTS IN THE WORLD, armed with
        the very identity a statement carries, and that the row is scoped
        exactly as if it did not: an input that is gone is proven gone by
        arming it, never by leaving it empty."""
        from helm import seatname_guard
        path, invalid = seatname_guard.authority_path()
        self.assertIsNone(invalid)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(names) + "\n")
        authority, why = seatname_guard.read_authority(path)
        self.assertIsNone(why)
        for n in names:
            self.assertIn(n, authority,
                          "must-hit: the authority the derivation reads does "
                          "not refuse this identity, so an arm below would be "
                          "measuring an empty list")

    def test_a_helm_entry_is_not_injected_into_another_project(self):
        helm_dir, clientproj = self._two_projects()
        self._plant_global("seat-routing", "a review may go to one named seat",
                           owner="helm")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertEqual(self.ledger_rows()[-1]["project"], "clientproj",
                         "must-hit: the scope really derived to clientproj, so a "
                         "missing entry below is the fence and not a "
                         "registry that failed to load")
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: this very stdout renders a
        # pinned premise, so the absence below is the fence and not an empty
        # lane, an unwritten registry or a store that never loaded.
        self.assertIn("fleet-row", out)
        self.assertNotIn("seat-routing", out)

    def test_the_same_helm_entry_still_reaches_a_helm_cwd(self):
        """THE CONTROL for the arm above: the fence must subtract nothing
        from the project the entry is about."""
        helm_dir, clientproj = self._two_projects()
        self._plant_global("seat-routing", "a review may go to one named seat",
                           owner="helm")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir))
        self.assertEqual(rc, 0)
        self.assertIn("seat-routing", out)

    def test_a_fleet_entry_is_injected_into_every_project(self):
        helm_dir, clientproj = self._two_projects()
        self._plant_global("one-tla-pair", "numbered seats are not a "
                           "review pool", owner="fleet")
        # UNCONDITIONAL, ahead of the loop: the entry is admitted for one named
        # project, so a loop that never ran could not read as a pass.
        self.assertIn("one-tla-pair",
                      [e["id"] for e in store.load_all(project="clientproj",
                                                       scope_fence=True)])
        for cwd in (helm_dir, clientproj):
            with self.subTest(cwd=cwd):
                rc, out, _ = self.run_inject(
                    ["--hook-json", "--explain"],
                    stdin_text=self.hook_stdin("x", cwd=cwd, session="s-" + os.path.basename(cwd)))
                self.assertEqual(rc, 0)
                self.assertIn("one-tla-pair", out)

    def test_an_unrecorded_row_naming_a_lane_stays_in_the_helm_project(self):
        """No project recorded = the project it was WRITTEN UNDER, and a
        helm-global row was written under none. Its STATEMENT decides: a
        statement naming an identity that exists only inside helm's own fleet
        — here a lane room — is about helm and reaches no other project, while
        a project-HOMED unrecorded row keeps reaching its own project."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("unscoped-truth",
                           "a claim is released at lane/some-room, "
                           "never before")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE — see the arm above.
        self.assertIn("fleet-row", out)
        self.assertNotIn("unscoped-truth", out)
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir,
                                                        session="sid-2"))
        self.assertIn("unscoped-truth", out,
                      "must-hit: the entry exists and fires; only the "
                      "cross-project door is closed")
        # and a project-HOMED entry with no field recorded is derived from
        # the root it was written into, so it keeps reaching its own project
        store.write_prior({"id": "clientproj-row", "statement": "clientproj truth",
                           "confidence": "1.0", "pin": "true"},
                          root_dir=os.path.join(home.project_dir("clientproj"),
                                                "premises"))
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj,
                                                        session="sid-3"))
        self.assertIn("clientproj-row", out)

    def test_only_helms_own_worktree_root_is_a_helm_artifact(self):
        """A worktree path is a helm identity ONLY when the root it hangs off is
        helm's own. EVERY fleet mints its lanes at `<its repo>-wt/<lane>`, so a
        pattern matching any `-wt/` path read another project's worktree as
        helm's: a general rule citing that project's own checkout was scoped to
        helm and WITHHELD FROM THE ONE PROJECT THE PATH BELONGS TO, which is the
        opposite of what the path says. The worktree root name is derived from
        the same constant that names the answer, so the two cannot drift.

        AND THE COMPONENT IS PROVEN BY THE SLASH ON BOTH SIDES OF IT, never by
        guessing what a NAME may contain. Two earlier shapes are refuted here by
        measurement: a negative lookbehind over an ASCII class matched inside
        caféhelm-wt/ because é was outside the class, and a whitespace tokenizer
        would cut `/work/acme helm-wt/room` into a fresh `helm-wt/room` token —
        a space in a basename is DATA. Every foreign room below is minted by the
        shipped lane producer from a registered project, and the positives (four
        foreign rooms: plain, hyphenated, non-ASCII, internally spaced) sit on
        the SAME observable as the negative (helm's own room), so neither can
        pass alone."""
        from helm.work import _lanes
        helm_dir, clientproj = self._two_projects()
        # FOUR FOREIGN FLEETS, each one a face of the same defect, each
        # REGISTERED and each room minted by the SHIPPED producer so no arm can
        # test a room shape helm does not mint:
        #   acme-helm  — the project word as a hyphenated basename SUFFIX
        #   caféhelm   — a non-ASCII code point immediately before the word, the
        #                character every denial list forgets
        #   acme helm  — an INTERNAL SPACE, which a tokenizer would treat as a
        #                delimiter and turn into a fresh `helm-wt/` token
        acme = os.path.join(self.tmp, "repos", "acme-helm")
        cafe = os.path.join(self.tmp, "repos", "caféhelm")
        spaced = os.path.join(self.tmp, "repos", "acme helm")
        self.seed_registry(**{"helm": helm_dir, "clientproj": clientproj,
                              "acme-helm": acme, "caféhelm": cafe,
                              "acme helm": spaced})
        acme_room = _lanes.lane_path(acme, "review-room")
        cafe_room = _lanes.lane_path(cafe, "review-room")
        spaced_room = _lanes.lane_path(spaced, "review-room")
        clientproj_room = _lanes.lane_path(clientproj, "review-room")
        helm_room = _lanes.lane_path(helm_dir, "some-lane")
        for room, tail in ((acme_room, "-helm-wt"), (cafe_room, "éhelm-wt"),
                           (spaced_room, " helm-wt")):
            self.assertTrue(os.path.basename(os.path.dirname(room))
                            .endswith(tail),
                            "must-hit: the shipped producer really mints the "
                            "basename %r this arm is about, so a pass below "
                            "cannot come from a room shape that ends in "
                            "something else" % tail)
        # THE CONTROL, both classifiers measured on the same two inputs. The
        # round-six lookbehind is rebuilt here exactly as it shipped: it calls
        # the caféhelm room helm's, which is the regression, and the shipped
        # rule does not — while BOTH still call helm's own room helm's, so the
        # cure narrowed the one input and nothing else. Blast radius: these four
        # assertions only; the old regex is a local object no other arm reads.
        old = re.compile(r"(?<![0-9A-Za-z_.~-])helm-wt/")
        self.assertTrue(old.search("/work/caféhelm-wt/review-room"),
                        "must-hit: the refuted regex really does match here, "
                        "so the next assertion is a cure and not a tautology")
        self.assertFalse(store.names_helm_artifact(
            "/work/caféhelm-wt/review-room"),
            "a foreign basename ending in the project word is never helm's, "
            "whatever code point precedes it")
        self.assertTrue(old.search("/work/helm-wt/some-lane"))
        self.assertTrue(store.names_helm_artifact("/work/helm-wt/some-lane"),
                        "must-hit: the genuine component still classifies, so "
                        "the cure did not simply stop matching")
        self._plant_global("foreign-wt-row",
                           "before editing, verify the checkout at "
                           + clientproj_room)
        self._plant_global("hyphenated-foreign-wt-row",
                           "before editing, verify the checkout at "
                           + acme_room)
        self._plant_global("unicode-foreign-wt-row",
                           "before editing, verify the checkout at "
                           + cafe_room)
        self._plant_global("spaced-foreign-wt-row",
                           "before editing, verify the checkout at "
                           + spaced_room)
        self._plant_global("helm-wt-row",
                           "the gate runs in " + helm_room)
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        # UNCONDITIONAL POSITIVE CONTROL, ahead of the loop: every foreign row
        # is admitted for one named project and the fleet row is too, so a
        # loop that never ran — or a subTest whose absence assertion passed on
        # an empty lane — could not read as a pass.
        admitted = [e["id"] for e in store.load_all(project="clientproj",
                                                    scope_fence=True)]
        for pid in ("fleet-row", "hyphenated-foreign-wt-row",
                    "unicode-foreign-wt-row", "spaced-foreign-wt-row"):
            self.assertIn(pid, admitted)
        self.assertNotIn("helm-wt-row", admitted)
        # EACH FOREIGN LANE, read from INSIDE that fleet's own room — the
        # project the ledger maps that room to is the one the path names, and
        # that row must reach it.
        for room, project, pid, sid in (
                (acme_room, "acme-helm", "hyphenated-foreign-wt-row", "sid-a"),
                (cafe_room, "caféhelm", "unicode-foreign-wt-row", "sid-c"),
                (spaced_room, "acme helm", "spaced-foreign-wt-row", "sid-s")):
            with self.subTest(room=room):
                rc, out, _ = self.run_inject(
                    ["--hook-json"],
                    stdin_text=self.hook_stdin("x", cwd=room, session=sid))
                self.assertEqual(rc, 0)
                self.assertEqual(
                    self.ledger_rows()[-1]["project"], project,
                    "must-hit: the lane room really resolved to its own "
                    "repo's project, so a missing row below is the artifact "
                    "rule and not a registry that lost the room")
                self.assertIn("fleet-row", out)
                self.assertIn(pid, out,
                              "this fleet's own rule about its own checkout "
                              "must reach this fleet")
                self.assertNotIn("helm-wt-row", out,
                                 "must-hit: helm's own room is still helm's, "
                                 "so the component rule did not disarm it")
        # ONE OBSERVABLE, all four faces at once: a third project's turn renders
        # the fleet row and every foreign-room rule, and withholds helm's.
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        for pid in ("hyphenated-foreign-wt-row", "unicode-foreign-wt-row",
                    "spaced-foreign-wt-row"):
            self.assertIn(pid, out,
                          "a FOREIGN fleet's worktree path is ambiguous canon, "
                          "never helm's artifact, so the rule citing it stays "
                          "general — %s" % pid)
        # POSITIVE CONTROL ON THIS EXACT OBSERVABLE: a fleet row does render in
        # this stdout, so an absent id below is the fence and not a lane that
        # rendered nothing at all.
        self.assertIn("fleet-row", out)
        self.assertIn("foreign-wt-row", out,
                      "a FOREIGN project's worktree path is that project's "
                      "artifact, never helm's, so the rule citing it stays "
                      "general and reaches this project")
        self.assertNotIn("helm-wt-row", out,
                         "must-hit: helm's OWN worktree root is still an "
                         "identity that exists only inside helm's fleet, so "
                         "the narrowing did not disarm the rule")
        # THE OTHER DIRECTION OF THAT MUST-HIT: the withheld row must still
        # reach helm. Without this, the assertNotIn above would also pass for
        # an entry that fires nowhere.
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir,
                                                        session="sid-wt"))
        self.assertEqual(rc, 0)
        self.assertIn("helm-wt-row", out,
                      "must-hit: the helm-worktree row is admitted for helm")
        # EVERY OTHER SPELLING OF THE SAME MISTAKE, each room minted by the
        # shipped producer and classified by the shipped classifier. A prefixed
        # or suffixed basename is a DIFFERENT project, so its room is never
        # helm's artifact; helm's own room still is.
        for root, is_helm in (("helm", True), ("acme-helm", False),
                              ("caféhelm", False), ("acme helm", False),
                              ("myhelm", False), ("helmet", False),
                              ("helm-wt-old", False)):
            room = _lanes.lane_path(os.path.join(self.tmp, "repos", root),
                                    "review-room")
            with self.subTest(room=room):
                self.assertEqual(store.names_helm_artifact(room), is_helm)

    def test_a_bare_worktree_reference_is_helm_only_where_it_is_unambiguous(self):
        """THE POLICY THE COMPONENT RULE BUYS, stated as inputs. `/helm-wt/`
        carries the slash on both sides, so it is a whole component wherever it
        appears — absolute or relative. A BARE `helm-wt/<lane>` has no left
        slash at all, so it is helm's only where nothing can precede it: the
        START of the statement. Anywhere else in prose it is AMBIGUOUS — it
        could as easily be the tail of a foreign basename someone typed without
        its parent — and ambiguous canon stays FLEET under existing law, where
        its author can scope it with `helm store rescope`.

        The mid-prose negative is the deliberate cost of the rule and the one
        input that separates it from an unanchored substring match, so it is
        pinned rather than left to the reader."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, ahead of the
        # loop: the classifier does say True for the genuine component and
        # False for the ambiguous one, so neither a loop that never ran nor a
        # classifier that answers False for everything reads as a pass.
        self.assertTrue(store.names_helm_artifact(
            "the gate runs in /x/helm-wt/some-lane"))
        self.assertFalse(store.names_helm_artifact(
            "see helm-wt/some-lane for the gate"))
        for text, is_helm in (
                ("helm-wt/some-lane is where the gate runs", True),
                ("the gate runs in /x/helm-wt/some-lane", True),
                ("the gate runs in ../helm-wt/some-lane", True),
                ("see helm-wt/some-lane for the gate", False),
                ("see acme-helm-wt/some-lane for the gate", False)):
            with self.subTest(text=text):
                self.assertEqual(store.names_helm_artifact(text), is_helm)
        # MUST-HIT ON THE CLASSIFIER ITSELF: the same statements go through the
        # shipped derivation, so the readings above cannot be true of a helper
        # nothing consults. A statement-start reference derives helm; the
        # mid-prose one derives fleet.
        self.assertEqual(
            store.entry_scope({"statement": "helm-wt/some-lane holds the gate",
                               "scope": "global"})[0], store.HOME_PROJECT)
        self.assertEqual(
            store.entry_scope({"statement": "see helm-wt/some-lane for it",
                               "scope": "global"})[0], store.FLEET)

    def test_a_uuid_in_a_path_is_not_a_helm_row_id(self):  # noqa: VACUOUS_ASSERTION — the helm row ids asserted True and the helm-own row asserted withheld are unconditional controls on the same classifier and the same admitting door
        """task/2547. The row-id shape is 12 to 32 hex digits, which is also
        the last group of every UUID, and a UUID sits in every Claude session
        scratch path (/tmp/claude-<uid>/<project>/<session-uuid>/...). So a
        general rule citing such a path was read as naming a dispatch row and
        scoped to helm, and a test run under a TMPDIR carrying a session UUID
        withheld a foreign room from its own project. A UUID's group is not a
        row id: the run is refused when a dash joins it to more hex."""
        from helm.work import _lanes
        uuid = "1953843d-c02a-498e-b8ca-47dce667fcb3"
        # UNCONDITIONAL POSITIVE CONTROLS: helm's own row ids still classify,
        # bare and in prose, so the cure did not stop matching hex.
        for text in ("dispatch 5f1fc24c8d2a203a9fd95fe6ebc8fefc is open",
                     "row 5f1fc24c8d2a has a verdict",
                     "receipt 9a91e1cd6c793a53 is OK"):
            self.assertTrue(store.names_helm_artifact(text), text)
        for text in ("keep notes under /tmp/claude-1000/p/%s/scratchpad" % uuid,
                     "session %s resumed" % uuid,
                     "session %s resumed" % uuid.upper()):
            with self.subTest(text=text):
                self.assertFalse(store.names_helm_artifact(text))
        self.assertEqual(store.entry_scope(
            {"statement": "keep notes under /tmp/c/p/%s/scratchpad" % uuid,
             "scope": "global"})[0], store.FLEET)
        # THE ADMITTING DOOR, with a foreign room minted by the shipped lane
        # producer under a directory carrying a session UUID, as a TMPDIR does.
        base = os.path.join(self.tmp, uuid, "repos")
        helm_dir = os.path.join(base, "helm")
        clientproj = os.path.join(base, "clientproj")
        self.seed_registry(helm=helm_dir, clientproj=clientproj)
        self._plant_global("uuid-foreign-wt-row",
                           "before editing, verify the checkout at "
                           + _lanes.lane_path(clientproj, "review-room"))
        self._plant_global("uuid-helm-wt-row",
                           "the gate runs in "
                           + _lanes.lane_path(helm_dir, "some-lane"))
        admitted = [e["id"] for e in store.load_all(project="clientproj",
                                                    scope_fence=True)]
        self.assertIn("uuid-foreign-wt-row", admitted)
        self.assertNotIn("uuid-helm-wt-row", admitted)

    # ------------------------------------------------------------------
    # THE TWO GLOBAL ROOTS DERIVE DIFFERENTLY, and the reason is a MEASUREMENT
    # of what each one holds rather than a symmetry in the code.
    #
    # THE ADOPTED ROOT gets NO content test at all. Hand-labelling its 135 rows
    # against a statement classifier found the classifier wrong 8 of 14 times,
    # and EVERY error was in one direction: it stripped fleet policy or the
    # owner's own pinned canon from every non-helm seat. Only 6 rows are
    # genuinely about helm alone, and the recorded field is how those 6 become
    # project-scoped — `store rescope` is that door.
    #
    # THE HELM-GLOBAL ROOT keeps a classifier because it holds 4770 rows and
    # hand-labelling does not scale; the statement rule marks about 2 percent
    # of them helm-specific. So it FAILS TOWARD FLEET: an unclassified row
    # keeps reaching every seat instead of silently vanishing from all of them.
    #
    # The classifier reads the STATEMENT LINE ALONE — never the body, the
    # keywords, the source or the rationale — because those carry PROVENANCE. A
    # rule learned while working one lane cites that lane in its source and is
    # still general engineering canon.
    # ------------------------------------------------------------------
    def test_an_adopted_row_is_fleet_even_when_it_names_a_lane(self):
        """The owner's adopted canon reaches every seat with no content test.
        This row's STATEMENT names a lane room — the one token shape that WOULD
        scope a helm-global row, and carries both again in the fields a classifier would be
        tempted to read — and it is still fleet, because the adopted root is
        not classified at all."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_adopted(
            "adopted-canon",
            "qq-probe-identity holds lane/some-room until the claim releases",
            source="observed while working lane/some-room",
            keywords="qq-probe-identity")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertEqual(self.ledger_rows()[-1]["project"], "clientproj",
                         "must-hit: the scope really derived to clientproj")
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: a pinned row does render in
        # this very stdout, so a present id below is the derivation and not a
        # lane that fired everything it was handed.
        self.assertIn("fleet-row", out)
        self.assertIn("adopted-canon", out,
                      "the adopted root is the owner's canon: it is fleet "
                      "always, and no statement in it is a scope test")

    def test_a_general_rule_containing_the_bare_word_is_still_fleet(self):
        """The bare project word in prose is NOT an identity. A rule about
        working under a guard is general engineering canon whichever fleet's
        guard taught it, so it must reach every seat."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("guard-canon",
                           "a helm guard refusing me is evidence about the "
                           "world, so read the state it reports on")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out)
        self.assertIn("guard-canon", out,
                      "the bare word is prose, not a seat, lane, train or row")

    def test_only_the_statement_line_decides_not_the_provenance_fields(self):
        """The other half of the same law: an identity in the SOURCE or the
        KEYWORDS is provenance — where the rule was learned — and must not
        scope the rule to the lane that taught it."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("provenance-canon",
                           "derive a check, never transcribe it",
                           source="qq-probe-identity in lane/some-room, "
                                  "task/2435",
                           keywords="qq-probe-identity")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out)
        self.assertIn("provenance-canon", out)

    def test_a_statement_naming_a_lane_room_is_helm_only(self):
        """THE MUST-HIT CONTROL for the two arms above: a statement that names a
        POSITIVELY HELM ARTIFACT — here a lane room — is still withheld from
        another project, so the arms about seat names and model words are
        measuring the dropped input and not a fence that stopped working.

        The authority is armed anyway, which is what makes the pair
        discriminating: one statement carries a seat name and renders, this one
        carries a lane and does not."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("seat-row",
                           "a review goes to the seat holding "
                           "lane/some-room while the owner is away")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out)
        self.assertNotIn("seat-row", out)
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir,
                                                        session="sid-2"))
        self.assertEqual(rc, 0)
        self.assertIn("seat-row", out,
                      "must-hit: the row exists and fires; only the "
                      "cross-project door is closed")

    def test_a_statement_naming_a_task_row_or_a_row_id_is_helm_only(self):
        """The same clause through its other spellings: a task row, a
        dispatch or land-request row id (a long lowercase hex token), a train,
        and a source path under the project's own package."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rows = {
            "task-row": "task/2435 is the owner ruling this answers",
            "rowid-row": "the verdict landed at 485891c80a5e",
            "train-row": "compose-train 4 pipelines on the second node",
            "srcpath-row": "the fence lives in helm/store/load.py",
        }
        for pid, statement in rows.items():
            self._plant_global(pid, statement)
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out)
        for pid in rows:
            with self.subTest(pid=pid):
                self.assertNotIn(pid, out)
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir,
                                                        session="sid-2"))
        self.assertEqual(rc, 0)
        for pid in rows:
            with self.subTest(pid=pid):
                self.assertIn(pid, out,
                              "must-hit: the row fires somewhere, so the "
                              "absence above is the derivation")

    def test_a_seat_attribution_in_a_statement_does_not_scope_the_rule(self):
        """A SEAT NAME NEVER SCOPES BY ITSELF, and the reason is a measurement of
        the corpus rather than a preference: of the 283 rows the seat-name input
        held to one project, 170 were seat-name matches, and those were mostly
        ATTRIBUTION carried inside otherwise general engineering rules — "who
        measured this" is provenance wearing a statement's clothes. A general
        rule is canon for every seat, so an ambiguous statement fails toward
        fleet and the seat-name input is gone from the derivation entirely.

        THE AUTHORITY IS ARMED WITH THE VERY NAME THIS STATEMENT CARRIES, and
        the row still reaches both projects: the input exists in the world and
        is not consulted."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("attributed-canon",
                           "independently verify generated code before "
                           "shipping, measured by qq-probe-identity")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        for cwd, sid in ((clientproj, "sid-clientproj"), (helm_dir, "sid-helm")):
            with self.subTest(cwd=cwd):
                rc, out, _ = self.run_inject(
                    ["--hook-json"],
                    stdin_text=self.hook_stdin("x", cwd=cwd, session=sid))
                self.assertEqual(rc, 0)
                # POSITIVE CONTROL ON THE SAME OBSERVABLE: a pinned row renders
                # in this very stdout, so a present id is the derivation and an
                # absent one is not a spent budget.
                self.assertIn("fleet-row", out)
                self.assertIn("attributed-canon", out,
                              "attribution is provenance: a general rule "
                              "carrying a seat name is still every seat's")
        self.assertEqual(self.ledger_rows()[-1]["project"], "helm",
                         "must-hit: the second cwd really derived to helm, so "
                         "the clientproj render above was a different project")

    def test_an_authority_token_that_is_also_a_model_word_scopes_nothing(self):
        """THE COLLISION THE OLD INPUT COULD NOT SEE: a roster carries tokens
        that are also MODEL FAMILY words, and a rule about a model family's
        output is a rule about that model — never about the fleet whose roster
        happens to spell a seat the same way. The fixture arms the authority
        with the house-convention identity and has the statement name it as the
        subject of a general checking rule, which is the shape that collided."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("seat-b")
        self._plant_global("model-canon",
                           "check seat-b output against the producer before "
                           "quoting it")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out)
        self.assertIn("model-canon", out)

    def test_a_fleet_recorded_row_renders_everywhere_and_rescope_restores_one(self):  # noqa: VACUOUS_ASSERTION — each absence has an unconditional positive twin on the SAME stdout: a fleet-recorded row renders beside it, and the withheld row renders for the helm cwd
        """THE EXPLICIT FLEET RESTORATION: a RECORDED project wins over every
        derivation, in BOTH directions. A row recorded to helm is withheld from
        clientproj even though its statement is general; rescoped to fleet through
        the shipped verb it renders in clientproj again, which is what makes the
        record a door rather than a trapdoor."""
        helm_dir, clientproj = self._two_projects()
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        self._plant_global("held-row", "a general looking sentence",
                           owner="helm")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out, "the fleet record reaches clientproj")
        self.assertNotIn("held-row", out)
        rc, helm_out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir,
                                                        session="sid-h"))
        self.assertEqual(rc, 0)
        self.assertIn("held-row", helm_out,
                      "POSITIVE TWIN for the absence above: the row is "
                      "renderable, so what clientproj lost was the record")
        e, err = store.rescope("held-row", "2026-07-18T00:00:00Z", store.FLEET)
        self.assertIsNone(err)
        self.assertEqual(e["project"], store.FLEET)
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj,
                                                        session="sid-2"))
        self.assertEqual(rc, 0)
        self.assertIn("held-row", out,
                      "rescope to fleet RESTORES the clientproj render — the "
                      "parsed-entry cache is keyed on the file the verb wrote")

    def test_a_recorded_project_on_an_adopted_row_still_scopes_it(self):
        """THE RESCOPE DOOR: the recorded field is the ONLY way an adopted row
        becomes project-scoped, and it still wins over the fleet default."""
        helm_dir, clientproj = self._two_projects()
        self._plant_adopted("adopted-scoped", "a general looking sentence",
                            owner="helm")
        self._plant_adopted("adopted-open", "another general sentence")
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=clientproj))
        self.assertEqual(rc, 0)
        # POSITIVE CONTROL: the adopted root loads and fires in this stdout.
        self.assertIn("adopted-open", out)
        self.assertNotIn("adopted-scoped", out)
        rc, out, _ = self.run_inject(
            ["--hook-json"], stdin_text=self.hook_stdin("x", cwd=helm_dir,
                                                        session="sid-2"))
        self.assertEqual(rc, 0)
        self.assertIn("adopted-scoped", out)

    def test_the_census_splits_the_derived_rows_from_the_recorded_ones(self):
        """The derived split is a NUMBER someone can work: how many rows reach
        every seat only because nothing is recorded, and how many are held to
        one project only because a statement named an identity."""
        helm_dir, clientproj = self._two_projects()
        self._seat_authority("qq-probe-identity")
        self._plant_global("fleet-row", "owner policy everywhere",
                           owner="fleet")
        self._plant_global("clientproj-recorded", "a clientproj truth",
                           owner="clientproj")
        self._plant_global("derived-fleet", "derive a check, never transcribe")
        self._plant_global("derived-helm",
                           "a review goes to the seat holding lane/some-room")
        self._plant_adopted("adopted-open", "another general sentence")
        cen = store.scope_census(project="clientproj")
        self.assertEqual(cen["fleet"], 1)
        self.assertEqual(cen["own"], 1)
        self.assertEqual(cen["unscoped_fleet"], 2)   # one global, one adopted
        self.assertEqual(cen["unscoped_helm"], 1)
        self.assertEqual(cen["foreign"], 0)

    def test_plain_stdin_contract_unchanged_no_session(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject([], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        self.assertIn("jit-a", out)
        self.assertNotIn("session", self.ledger_rows()[-1])


class SessionCooldownTest(InjectBase):
    """The habituation guard extended to the JIT lane: per-session suppression
    at _global/.state/inject-seen/<session>.json, session-long (NO turn
    window — a seat forgets at compaction), 2x score escape,
    pinned/reflex exempt, freed cap slots, fail-open."""

    PROMPT = "tune the fluxcap"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()  # keep the cooldown turns off the first-turn-whisper path
        self.addCleanup(p.stop)

    def hook_stdin(self, prompt, session="sid-1"):
        return json.dumps({"prompt": prompt, "session_id": session,
                           "hook_event_name": "UserPromptSubmit"})

    def rows(self):
        return list(inject._ledger_rows())

    def seen(self, session="s1"):
        with open(inject._seen_path(session), encoding="utf-8") as f:
            return json.load(f)

    def test_fires_then_stays_sent_for_the_whole_session(self):
        """WAS test_fires_then_cools_then_refires_after_window, and the change
        of name IS the change of contract. The 15-turn window is gone: a fired
        entry stays suppressed for the life of the session, because a seat
        forgets at COMPACTION and not on a timer.

        Measured on the fire-ledger before this lane: 6,949 of 7,375 JIT
        re-deliveries were that window merely expiring (94.2%), against 426
        real score escapes — 66.9% of the entire JIT lane, ~622k tokens of
        content the seats already had in context. The old test asserted turn
        17 re-fires; that re-fire was the defect."""
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        first = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(first["jit"], ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 1)
        for i in range(40):  # far past where the old window reopened
            self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [],
                             "turn %d must stay suppressed" % (i + 2))
        r = self.rows()[-1]  # a suppressed-to-silence turn is still measurable
        self.assertIs(r["silent"], True)
        self.assertEqual(r["suppressed"], ["jit-a"])
        self.assertEqual(r["session"], "s1")
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 1,
                         "the record names the ORIGINAL fire, not a rolling one")

    def test_the_turn_counter_still_advances_on_suppressed_turns(self):
        """WAS test_cooldown_counts_turns_not_fires. The turn counter no longer
        gates suppression, but it still has to COUNT — it dates every record
        and drives the '--explain: fired Nt ago' line."""
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        # POSITIVE CONTROL: the entry provably fires here, so the empty lanes
        # below are suppression and an off-topic prompt — not an unplanted store.
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"],
                         ["PRIOR 0.80 jit-a: a flux fact"])
        for _ in range(16):
            self.assertEqual(inject.gather("unrelated words", session="s1")["jit"], [])
        self.assertEqual(self.seen()["turn"], 17)
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 1)

    def test_only_compaction_or_a_score_escape_brings_a_jit_entry_back(self):
        """THE TWO DOORS THAT REMAIN, and the reason removing the window is
        safe. forget_session is the compaction leg — the seat provably lost
        the content, so it must get it back. Without this arm a session-long
        suppression would go permanently silent for a seat that just lost
        everything it was suppressing, which is strictly worse than the waste
        it replaces."""
        from helm.inject import _ledger
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        first = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(first["jit"], ["PRIOR 0.80 jit-a: a flux fact"])
        for _ in range(20):
            self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [])
        _ledger.forget_session("s1")          # what the compact hook calls
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"],
                         first["jit"],
                         "a compacted seat lost it and must get it back")

    def test_cross_session_independence(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        inject.gather(self.PROMPT, session="s1")
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [])
        self.assertEqual(inject.gather(self.PROMPT, session="s2")["jit"],
                         ["PRIOR 0.80 jit-a: a flux fact"],
                         "another session must have its own cooldown state")

    def test_stdin_mode_no_session_no_cooldown_no_state(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        for _ in range(3):  # re-fires every turn, exactly the pre-cooldown law
            rc, out, _ = self.run_inject([], stdin_text=self.PROMPT)
            self.assertEqual(rc, 0)
            self.assertIn("jit-a", out)
        self.assertFalse(os.path.exists(inject._seen_dir()),
                         "plain stdin must write no seen-state")

    def test_the_fire_map_is_never_evicted(self):
        """A review of 66a58b1c: every record in this map is ACTIVELY
        SUPPRESSING content the seat still has, so ANY eviction silently
        re-enables delivery of whatever it drops. My first version capped it at
        2000 and its own test asserted the oldest fires were evicted — which is
        the same defect this lane removes, moved to the Nth distinct id.

        Nothing needs to bound it: the keys are STORE ENTRY IDS, so the map
        cannot outgrow the store's cardinality, and forget_session drops the
        whole file at every context boundary."""
        from helm.inject import _ledger
        fired = {"id-%05d" % i: [i + 1, 0.5] for i in range(5000)}
        _ledger._seen_save("s1", {"turn": 5000, "fired": fired, "pinned": None})
        kept = self.seen()["fired"]
        self.assertEqual(len(kept), 5000, "a save must never drop a live record")
        self.assertIn("id-00000", kept, "the OLDEST fire is still suppressing")
        self.assertIn("id-04999", kept)

    def test_2x_score_escape_refires_through_suppression(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap,quantum")
        inject.gather(self.PROMPT, session="s1")  # fires: score 0.8 (fluxcap df=1)
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [])
        # both keywords hit -> score 1.6 = 2.0x the recorded 0.8 -> escapes
        got = inject.gather("tune the fluxcap quantum", session="s1")["jit"]
        self.assertEqual(got, ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertEqual(self.seen()["fired"]["jit-a"], [3, 1.6])  # re-recorded
        # and the refreshed record cools it again, even at the higher score
        self.assertEqual(inject.gather("tune the fluxcap quantum", session="s1")["jit"], [])

    def test_the_pinned_lane_is_sent_once_and_reflex_stays_exempt(self):
        """CONTRACT CHANGE 2026-08-04, owner-asked: the pinned lane was
        cooldown-EXEMPT ("so every agent warms from the profile on every
        turn") and that exemption is what made it 41.0% of helm's injection at
        1,147 B/turn — 1,157 of those bytes byte-identical every prompt,
        re-sent 444 times in one measured session.

        It now fires ONCE per session-with-context. REFLEX STAYS EXEMPT: a
        reflex fires on a signal live THIS turn, so suppressing it would
        silence the steer at the moment it applies — a different lane with a
        different contract."""
        from helm import reflex
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        reflex.write({"id": "flux-reflex", "steer": "flux steer",
                      "signal": "prompt", "pattern": "fluxcap"})
        first = inject.gather(self.PROMPT, session="s1")
        second = inject.gather(self.PROMPT, session="s1")
        # POSITIVE CONTROL, unconditional: the lane really did fire on turn 1,
        # so the empty second turn is suppression and not an unplanted store.
        self.assertTrue(first["pinned"])
        self.assertEqual(second["pinned"], [], "sent once, not every turn")
        row = self.rows()[-1]
        self.assertEqual(row["suppressed_pinned"], ["pin-a"])
        self.assertEqual(row["suppressed_utf8_bytes"]["pinned"],
                         len("\n".join(first["pinned"]).encode("utf-8")))
        self.assertEqual(second["reflex"], ["REFLEX: flux steer"],
                         "a reflex fires on a LIVE signal — still exempt")
        self.assertEqual((first["jit"], second["jit"]),
                         (["PRIOR 0.80 jit-a: a flux fact"], []))

    def test_changed_pinned_content_refires_within_the_same_session(self):
        self.plant_pinned("pin-a", "first truth")
        first = inject.gather(self.PROMPT, session="s1")
        self.assertIn("first truth", first["pinned"][0])
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["pinned"], [])
        self.plant_pinned("pin-a", "changed truth")
        changed = inject.gather(self.PROMPT, session="s1")
        self.assertTrue(any("changed truth" in line for line in changed["pinned"]),
                        "new rendered guidance must re-fire without compaction")
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["pinned"], [],
                         "the changed content warms exactly once")

    def test_pinned_identity_outlives_the_JIT_cooldown_window(self):
        self.plant_pinned("pin-a", "always truth")
        self.assertTrue(inject.gather(self.PROMPT, session="s1")["pinned"])
        for turn in range(17):  # far past where the old JIT window reopened
            self.assertEqual(
                inject.gather(self.PROMPT, session="s1")["pinned"], [],
                "pinned content re-fired on cooldown turn %d" % turn)

    def test_explain_reports_the_suppressed_pinned_lane(self):
        """--explain is the ONE surface whose job is to say what reaches the
        seat, and it was blind to the pinned dedup: it printed "+" for every
        pinned line on every turn while gather was sending none of them. An
        operator asking "why is my premise not in context" read "+" and
        concluded it HAD been delivered. Measured identically at 5c805008 and
        at its parent d3300985, so the misreport predates the content-identity
        marker — the dedup only made a standing lie load-bearing.

        THE FIRST ASSERTION IS THE CONTROL: an explain that rendered no pinned
        lane at all would satisfy the "no +" check below vacuously."""
        self.plant_pinned("pin-a", "always truth")
        rc, before, _ = self.run_inject(
            ["--hook-json", "--explain"], stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        self.assertIn("+ PREMISE pin-a: always truth", before)  # CONTROL
        self.assertTrue(inject.gather(self.PROMPT, session="sid-1")["pinned"])
        self.assertEqual(inject.gather(self.PROMPT, session="sid-1")["pinned"], [])
        rc, after, _ = self.run_inject(
            ["--hook-json", "--explain"], stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        self.assertIn("- PREMISE pin-a: always truth "
                      "(already delivered this session)", after)
        self.assertNotIn("+ PREMISE pin-a", after)

    def test_explain_follows_content_identity_not_a_warmed_flag(self):
        """The explain rung must mirror gather's SEMANTICS, not just its
        boolean: changed guidance re-fires, so the diagnostic must show it
        firing again — otherwise the surface tells a seat its stale premise is
        still current."""
        self.plant_pinned("pin-a", "first truth")
        inject.gather(self.PROMPT, session="sid-1")
        rc, out, _ = self.run_inject(
            ["--hook-json", "--explain"], stdin_text=self.hook_stdin(self.PROMPT))
        self.assertIn("(already delivered this session)", out)
        self.plant_pinned("pin-a", "changed truth")
        rc, out, _ = self.run_inject(
            ["--hook-json", "--explain"], stdin_text=self.hook_stdin(self.PROMPT))
        self.assertIn("+ PREMISE pin-a: changed truth", out)
        self.assertNotIn("(already delivered this session)", out)

    def test_explain_never_stamps_the_pinned_marker(self):
        """READ-ONLY, exactly like the cooldown rung beside it. An explain that
        stamped the marker it reports on would silence the seat's real next
        turn — strictly worse than the misreport this fixes, because the seat
        would lose guidance to a DIAGNOSTIC it ran to check that guidance."""
        self.plant_pinned("pin-a", "always truth")
        for _ in range(3):
            self.run_inject(["--hook-json", "--explain"],
                            stdin_text=self.hook_stdin(self.PROMPT))
        self.assertTrue(inject.gather(self.PROMPT, session="sid-1")["pinned"],
                        "an explain burned the pinned lane's first delivery")

    def test_explain_returns_the_budget_when_the_lane_is_suppressed(self):
        """A finding against a826ce87, on the exact arm I asked a review to
        attack. gather's suppression does TWO things — it empties the pinned
        lane AND resets used=0 — so a suppressed turn hands the whole budget
        to the codex nudges walking last. An explain that reported the lane
        suppressed while STILL charging its bytes against the tail called
        those nudges over-budget on the very turns gather was sending them.
        Half-modelling a suppression is its own kind of lie."""
        os.environ["HELM_CHAT_NAME"] = "codex"
        self.addCleanup(os.environ.pop, "HELM_CHAT_NAME", None)
        # A budget where the arithmetic is exact rather than assumed: the base
        # line renders to 195B, the two nudges are 84B and 127B. At 250B the
        # base fits and crowds BOTH nudges out; with the base suppressed the
        # returned budget seats both. (My first attempt sized the statement
        # against PINNED_BUDGET directly and LINE_CAP truncated it to 195
        # anyway, so the nudges fit on turn 1 and the control was vacuous —
        # the test caught its own fixture.)
        self.plant_pinned("pin-a", "y" * 180)
        with mock.patch.object(inject, "PINNED_BUDGET", 250):
            first = inject.gather(self.PROMPT, session="sid-1")["pinned"]
            # CONTROL: the nudges are ABSENT while the base lane holds the
            # budget, so their arrival below is the budget returning and not
            # them fitting all along.
            self.assertTrue(any("pin-a" in l for l in first),
                            "base lane must fire on the warming turn")
            self.assertNotIn(inject.SA_WHISPER, first)
            second = inject.gather(self.PROMPT, session="sid-1")["pinned"]
            self.assertNotIn("pin-a", " ".join(second), "base lane suppressed")
            self.assertIn(inject.SA_WHISPER, second,
                          "returned budget permits the pair's first delivery")
            third = inject.gather(self.PROMPT, session="sid-1")["pinned"]
            self.assertNotIn(inject.SA_WHISPER, third,
                             "delivery, not the first attempt, stamps suppression")
            rc, out, _ = self.run_inject(
                ["--hook-json", "--explain"],
                stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        self.assertIn("already delivered this context", out)
        self.assertNotIn("(over budget) ← codex-only nudge", out)

    def test_the_row_says_WHY_each_jit_entry_fired(self):
        """A fired id that ALREADY had a record cleared the 2x escape; one that
        did not is a first delivery. Without the distinction the ledger cannot
        tell a working dedup from a leaking one — measured 2026-08-04, the
        landed change read 28.5% post-land repeats against an age-matched 12.5%
        pre-land, and that excess was equally consistent with legitimate
        escapes, correct post-boundary re-fires, and suppression leaking. Three
        verdicts on one number, because the row said WHICH ids fired and never
        WHY."""
        self.plant_jit("jit-a", "a flux fact", "fluxcap,warpcore")
        inject.gather(self.PROMPT, session="s1")
        self.assertEqual(self.rows()[-1]["jit_why"], {"escape": 0, "new": 1},
                         "a first delivery is NEW, never an escape")
        # both keywords -> score 1.6 = 2.0x the recorded 0.8 -> escapes
        inject.gather("tune the fluxcap and the warpcore", session="s1")
        self.assertEqual(self.rows()[-1]["jit_why"], {"escape": 1, "new": 0},
                         "a 2x re-fire is an ESCAPE, and must not read as new")
        # and a suppressed turn carries no why at all — it fired nothing
        inject.gather(self.PROMPT, session="s1")
        last = self.rows()[-1]
        self.assertNotIn("jit_why", last)
        self.assertEqual(last["suppressed"], ["jit-a"])

    def test_the_row_carries_the_turn_so_a_boundary_reset_is_visible(self):
        """THE OTHER HALF: after forget_session the counter restarts, so a NEW
        fire at turn 1 of a long-running session is the compaction/clear leg
        working, while the same fire at turn 40 is an entry never matched
        before. One int, and it is the difference between "the dedup reset" and
        "the dedup leaked" — indistinguishable in every row written before."""
        from helm.inject import _ledger
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        for _ in range(4):
            inject.gather(self.PROMPT, session="s1")
        self.assertEqual(self.rows()[-1]["turn"], 4)      # CONTROL: it counts
        _ledger.forget_session("s1")
        inject.gather(self.PROMPT, session="s1")
        after = self.rows()[-1]
        self.assertEqual(after["turn"], 1, "a boundary restarts the counter")
        self.assertEqual(after["jit_why"], {"escape": 0, "new": 1})

    def test_the_reset_row_says_why_the_epoch_reset(self):
        """THE INT SAYS THAT, THIS SAYS WHY. `turn` back at 1 proves a
        boundary happened and names neither the SessionStart that caused it
        nor whether anything vouched for that — so answering "was this
        boundary real?" meant cross-referencing the fire ledger against the
        resume state and the hook's stderr. Both facts are on hand where the
        drop is made, and this is them arriving on the row that shows it."""
        from helm.inject import _ledger
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        inject.gather(self.PROMPT, session="s1")
        # UNCONDITIONAL CONTROL on the SAME observable: a drop with no
        # provenance leaves the key OFF the row entirely, so its presence
        # below is the marker and not a row shape that always carries one.
        _ledger.forget_session("s1")
        inject.gather(self.PROMPT, session="s1")
        bare = self.rows()[-1]
        self.assertEqual(bare["turn"], 1, "CONTROL: a real boundary row")
        self.assertNotIn("epoch", bare)
        _ledger.forget_session("s1", {"source": "clear",
                                      "vouch": _ledger.EPOCH_UNVOUCHED})
        inject.gather(self.PROMPT, session="s1")
        row = self.rows()[-1]
        self.assertEqual(row["turn"], 1)
        self.assertEqual(row["epoch"], {"source": "clear",
                                        "vouch": _ledger.EPOCH_UNVOUCHED})

    def test_the_provenance_rides_the_reset_row_and_not_the_epoch_behind_it(self):
        """It describes the BOUNDARY, so it belongs to the rows that show the
        reset and to no others. Carrying it forward would stamp the same two
        fields on every row of the epoch — bytes on every turn to answer a
        question asked once."""
        from helm.inject import _ledger
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        _ledger.forget_session("s1", {"source": "compact",
                                      "vouch": _ledger.EPOCH_VOUCHED})
        inject.gather(self.PROMPT, session="s1")
        self.assertIn("epoch", self.rows()[-1], "CONTROL: the reset row has it")
        inject.gather(self.PROMPT, session="s1")
        later = self.rows()[-1]
        self.assertEqual(later["turn"], 2, "CONTROL: the epoch really moved on")
        self.assertNotIn("epoch", later)

    def test_an_unreadable_marker_is_an_absence_and_never_a_fourth_value(self):
        """Every reader below this has arms for a marker and for none. A
        marker that cannot be read must land on the second, because inventing
        a value nobody has an arm for is the failure this field removes."""
        from helm.inject import _ledger
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        _ledger.forget_session("s1", {"source": "clear",
                                      "vouch": _ledger.EPOCH_UNVOUCHED})
        inject.gather(self.PROMPT, session="s1")
        self.assertIn("epoch", self.rows()[-1], "CONTROL: a readable one lands")
        for junk in ("{not json", '{"source": "clear", "vouch": "maybe"}',
                     '{"vouch": "unvouched"}', '["clear"]'):
            with self.subTest(junk=junk):
                _ledger.forget_session("s1")
                with open(_ledger._epoch_path("s1"), "w", encoding="utf-8") as f:
                    f.write(junk)
                inject.gather(self.PROMPT, session="s1")
                self.assertNotIn("epoch", self.rows()[-1])

    def test_a_sessionless_turn_carries_neither(self):  # noqa: VACUOUS_ASSERTION — a session-bearing gather runs first and asserts BOTH keys present on the same observable, so their absence on the sessionless row is the arm under test rather than a row shape that never carries them
        """No session means no seen-state to reason from, so claiming a WHY
        would be inventing one."""
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        # UNCONDITIONAL CONTROL on the SAME observable: with a session both
        # keys ARE written, so their absence below is the sessionless arm and
        # not a row shape that never carries them at all.
        inject.gather(self.PROMPT, session="s1")
        warm = self.rows()[-1]
        self.assertIn("jit_why", warm)
        self.assertIn("turn", warm)
        got = inject.gather(self.PROMPT)
        self.assertTrue(got["jit"], "CONTROL: the sessionless turn really fired")
        last = self.rows()[-1]
        self.assertNotIn("jit_why", last)
        self.assertNotIn("turn", last)

    def test_a_compaction_brings_the_pinned_lane_back(self):
        """THE ARM THE WHOLE DESIGN TURNS ON. The session id SURVIVES a
        compaction and the context does not, so without this the dedup goes
        permanently silent for a seat that just lost every premise it was
        suppressing — strictly worse than the waste it replaces."""
        from helm.inject import _ledger
        self.plant_pinned("pin-a", "always truth")
        first = inject.gather(self.PROMPT, session="s1")
        self.assertTrue(first["pinned"])
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["pinned"], [])
        _ledger.forget_session("s1")          # what the compact hook calls
        again = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(again["pinned"], first["pinned"],
                         "a compacted seat must get its premises back")

    def test_a_sessionless_turn_always_gets_the_pinned_lane(self):
        """Plain stdin has no session, so there is nothing to remember with —
        it must never be suppressed by another session's record."""
        self.plant_pinned("pin-a", "always truth")
        a = inject.gather(self.PROMPT, session="s1")
        self.assertTrue(a["pinned"])
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["pinned"], [])
        self.assertTrue(inject.gather(self.PROMPT)["pinned"],
                        "no session = no suppression")

    def test_freed_cap_slots_reach_lower_candidates(self):
        for i, conf in enumerate(("0.9", "0.8", "0.7", "0.6", "0.5")):
            self.plant_jit("jit-%d" % i, "flux fact %d" % i, "fluxcap", conf=conf)
        first = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(len(first["jit"]), inject.JIT_CAP)
        self.assertFalse(any("jit-4" in l for l in first["jit"]))
        second = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(second["jit"], ["PRIOR 0.50 jit-4: flux fact 4"],
                         "suppression is pre-cap: the crowded-out 5th fires")
        self.assertEqual(sorted(self.rows()[-1]["suppressed"]),
                         ["jit-0", "jit-1", "jit-2", "jit-3"])

    def test_explain_renders_cooldown_read_only(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, _, _ = self.run_inject(["--hook-json"],
                                   stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        before = self.seen("sid-1")
        n_rows = len(self.rows())
        rc, out, _ = self.run_inject(["--hook-json", "--explain"],
                                     stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        self.assertIn("- jit-a (cooldown, fired 1t ago)", out)
        self.assertNotIn("+ jit-a", out)
        self.assertEqual(self.seen("sid-1"), before,
                         "--explain must never mutate seen-state")
        self.assertEqual(len(self.rows()), n_rows,
                         "--explain must never write the ledger")
        # another session's explain sees it hot
        rc, out, _ = self.run_inject(
            ["--hook-json", "--explain"],
            stdin_text=self.hook_stdin(self.PROMPT, session="sid-9"))
        self.assertIn("+ jit-a", out)
        self.assertNotIn("cooldown", out)

    def test_garbled_seen_state_reads_fresh_never_crashes(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        inject.gather(self.PROMPT, session="s1")
        for garbage in ("{not json", '{"turn": "x", "fired": []}',
                        '{"turn": -3, "fired": {"jit-a": ["a"]}}'):
            with open(inject._seen_path("s1"), "w") as f:
                f.write(garbage)
            self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"],
                             ["PRIOR 0.80 jit-a: a flux fact"], garbage)

    def test_unwritable_state_hook_contract_rc0(self):  # noqa: VACUOUS_ASSERTION — the same nonempty jit-a hook is the writable positive before durable admission is blocked
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, err = self.run_inject(["--hook-json"],
                                       stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        self.assertIn("jit-a", out,
                      "the same hook input delivers when admission is durable")
        self.assertEqual(err, "")
        g = os.path.join(os.environ["HELM_HOME"], "_global")
        shutil.rmtree(os.path.join(g, ".state"))
        with open(os.path.join(g, ".state"), "w") as f:
            f.write("x")  # .state is a FILE: durable admission cannot begin
        for _ in range(2):
            rc, out, err = self.run_inject(["--hook-json"],
                                           stdin_text=self.hook_stdin(self.PROMPT))
            self.assertEqual(rc, 0)
            self.assertEqual(out, "",
                             "no durable intent means no unaccounted delivery")
            self.assertEqual(err, "")

    def test_stale_sibling_files_pruned_and_an_old_fire_still_suppresses(self):
        """WAS test_stale_files_pruned_and_expired_window_refires. The SIBLING
        pruning is by wall-clock TTL and is unchanged; what changed is the
        second half — a fire 39 turns back no longer re-fires, because age in
        turns was never evidence the seat forgot. The stale-file arm keeps a
        positive control on the same turn (the pruning must still happen while
        the entry stays suppressed)."""
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        os.makedirs(inject._seen_dir())
        stale = os.path.join(inject._seen_dir(), "old-session.json")
        with open(stale, "w") as f:
            f.write("{}")
        os.utime(stale, (1, 1))  # epoch-old: beyond SEEN_TTL
        # POSITIVE CONTROL on the FILE observable: a FRESH sibling must SURVIVE
        # the same sweep, or "the stale one is gone" would also be satisfied by
        # a prune that deleted everything (or by a sweep that never ran and a
        # file that was never really there).
        fresh = os.path.join(inject._seen_dir(), "fresh-session.json")
        with open(fresh, "w") as f:
            f.write("{}")
        pk.write_json(inject._seen_path("s1"),
                      {"v": 1, "ts": pk.now_ts(), "turn": 40,
                       "fired": {"jit-a": [1, 0.8]}})  # fired 39 turns ago
        # POSITIVE CONTROL FIRST, on the same observable: a session with NO
        # seen-record fires this exact line, so the empty result below is the
        # 39-turn-old record suppressing and not an unplanted store.
        self.assertEqual(inject.gather(self.PROMPT, session="control")["jit"],
                         ["PRIOR 0.80 jit-a: a flux fact"])
        got = inject.gather(self.PROMPT, session="s1")["jit"]
        self.assertEqual(got, [], "39 turns is not evidence the seat forgot")
        self.assertFalse(os.path.exists(stale), "stale session file must be pruned")
        self.assertTrue(os.path.exists(fresh),
                        "the sweep is TTL-scoped: a fresh sibling survives it")
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 1,
                         "a suppressed entry keeps its ORIGINAL fire record")

    def test_induced_errors_preserve_hook_contract(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        with mock.patch.object(inject, "_seen_load",
                               side_effect=RuntimeError("seen exploded")), \
                mock.patch.object(inject, "_coinage",
                                  side_effect=RuntimeError("coinage exploded")):
            rc, out, err = self.run_inject(["--hook-json"],
                                           stdin_text=self.hook_stdin(self.PROMPT))
        self.assertEqual(rc, 0)
        self.assertIn("jit-a", out)
        self.assertEqual(err, "")


class CoinageTest(InjectBase):
    """3-strikes coinage recorder: K distinct turns -> ONE define nudge ->
    permanent latch. Narrowest detector (quoted + hyphenated), structural
    code/path stoplist, one nudge per turn, O(1) state, fail-open."""

    def state(self):
        with open(inject._coinage_path(), encoding="utf-8") as f:
            return json.load(f)

    def test_three_strikes_nudges_once_then_latched_forever(self):
        for i in range(inject.COINAGE_STRIKES - 1):
            got = inject.gather("the fire-ledger idea again %d" % i)
            self.assertEqual(got["reflex"], [], "strike %d must be silent" % (i + 1))
        nudged = inject.gather("more fire-ledger talk")["reflex"]
        self.assertEqual(len(nudged), 1)
        self.assertIn("coinage 'fire-ledger'", nudged[0])
        self.assertIn("helm coach", nudged[0])
        self.assertIn("owner present", nudged[0])       # offer-if-present
        self.assertIn("lexicon candidate", nudged[0])   # write-if-away
        last = inject._ledger_rows()[-1]
        self.assertEqual(last["fired"]["reflex"], ["coinage:fire-ledger"])
        d = self.state()
        self.assertEqual(d["offered"], ["fire-ledger"])  # the latch
        self.assertNotIn("fire-ledger", d["terms"])
        for i in range(4):  # forever means forever
            self.assertEqual(inject.gather("fire-ledger yet again %d" % i)["reflex"],
                             [], "latched term must never re-nudge")

    def test_quoted_phrase_counts_and_merges_with_hyphenated(self):
        inject.gather('we should call it "fire ledger"')
        inject.gather("the fire-ledger grows on me")
        got = inject.gather('one more vote for "fire ledger"')["reflex"]
        self.assertEqual(len(got), 1)
        self.assertIn("coinage 'fire-ledger'", got[0])

    def test_same_turn_repeats_count_once(self):
        inject.gather("fire-ledger fire-ledger \"fire-ledger\" fire-ledger")
        self.assertEqual(self.state()["terms"]["fire-ledger"][0], 1,
                         "distinct TURNS, not occurrences")

    def test_store_known_term_never_nudges(self):
        store.write_lexicon({"term": "fire-ledger",
                             "definition": "the inject measurement spine"})
        for i in range(inject.COINAGE_STRIKES + 1):
            got = inject.gather("the fire-ledger idea %d" % i)["reflex"]
            self.assertEqual(got, [], "a term already in the store never nudges")

    def test_detector_structural_stoplist(self):
        c = inject._coinage_candidates
        self.assertEqual(c("check store.load_all in helm/store.py via --hook-json "
                           "plus camelCase-name, cap-4 and snake_case-thing"), set())
        self.assertEqual(c("a so-called well-known long-term idea"), set())
        self.assertEqual(c('the fire-ledger, "flux capacitor" and '
                           "offer-once-latch-forever"),
                         {"fire-ledger", "flux-capacitor",
                          "offer-once-latch-forever"})
        self.assertEqual(c(""), set())
        self.assertEqual(c(None), set())

    def test_tag_context_terms_never_counted(self):
        # live-estate tuning: 'task-id'/'output-file'/'task-notification' were
        # harness tags in the prompt, not owner coinages
        prompt = ('per <task-id>42</task-id> write the <output-file/> — the '
                  'task-id and output-file machinery aside, I call this '
                  '"flux capacitor"')
        self.assertEqual(inject._coinage_candidates(prompt), {"flux-capacitor"},
                         "a tagged term is disqualified even where it rides "
                         "prose; the genuine coinage in the SAME prompt counts")
        for i in range(inject.COINAGE_STRIKES + 1):
            got = inject.gather(prompt + " %d" % i)["reflex"]
            self.assertTrue(all("task-id" not in l and "output-file" not in l
                                for l in got))
        self.assertNotIn("task-id", self.state()["terms"])
        self.assertIn("flux-capacitor", self.state()["offered"])

    def test_system_notification_prompts_skipped_entirely(self):
        for machine in ("[SYSTEM NOTIFICATION] agent done, see fire-ledger",
                        "<task-notification>fire-ledger done</task-notification>",
                        'note [system notification: "flux capacitor" fired]'):
            self.assertEqual(inject._coinage_candidates(machine), set(), machine)
        for i in range(inject.COINAGE_STRIKES + 1):  # machine text never counts
            got = inject.gather(
                "[SYSTEM NOTIFICATION] the fire-ledger run %d" % i)["reflex"]
            self.assertEqual(got, [])
        self.assertFalse(os.path.exists(inject._coinage_path()),
                         "machine prompts must not touch coinage state")

    def test_one_nudge_per_turn_second_term_waits(self):
        for i in range(inject.COINAGE_STRIKES - 1):
            inject.gather("alpha-coin and beta-coin, take %d" % i)
        both_hot = inject.gather("alpha-coin and beta-coin at K together")["reflex"]
        self.assertEqual(len(both_hot), 1, "max ONE nudge per turn")
        self.assertIn("coinage 'alpha-coin'", both_hot[0])
        nxt = inject.gather("beta-coin once more")["reflex"]
        self.assertEqual(len(nxt), 1)
        self.assertIn("coinage 'beta-coin'", nxt[0])
        self.assertEqual(self.state()["offered"], ["alpha-coin", "beta-coin"])

    def test_no_candidates_no_state_write(self):
        inject.gather("plain words with no quotes or neologisms")
        self.assertFalse(os.path.exists(inject._coinage_path()),
                         "a candidate-free prompt must not touch coinage state")

    def test_latch_survives_alien_terms_state(self):
        for i in range(inject.COINAGE_STRIKES):
            inject.gather("the fire-ledger idea %d" % i)
        d = self.state()
        d["terms"] = {"fire-ledger": "garbage", "other": 7}  # alien shapes
        pk.write_json(inject._coinage_path(), d)
        got = inject.gather("fire-ledger after corruption")["reflex"]
        self.assertEqual(got, [], "the offered latch must hold through torn counts")

    def test_unwritable_state_fails_open_silent(self):
        g = os.path.join(os.environ["HELM_HOME"], "_global")
        os.makedirs(g)
        with open(os.path.join(g, ".state"), "w") as f:
            f.write("x")
        for i in range(inject.COINAGE_STRIKES + 1):
            rc, out, err = self.run_inject([], stdin_text="fire-ledger turn %d" % i)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertNotIn("coinage", out,
                             "counts that cannot persist must never nudge")


class WhoLaneTest(InjectBase):
    """The WHO leg: the operator digest (whoami profile) rides the pinned
    budget as the lane's FIRST entry — <=2 lines jointly WHO_CAP-terse, atomic
    against PINNED_BUDGET, ledgered as who:operator, cooldown-exempt,
    fail-open on any profile trouble."""

    def plant_profile(self, level="expert operator",
                      guidance=("keep it short", "batch deploys")):
        pk.write_json(
            os.path.join(home.global_dir(), "know-your-user", "profile.json"),
            {"schema_version": 2, "technical_level": level,
             "guidance": list(guidance), "interview_status": "done",
             "updated_at": "2026-07-19T00:00:00Z", "source": "test"})

    def rows(self):
        return list(inject._ledger_rows())

    def test_the_digest_follows_the_rules_it_used_to_lead(self):
        """ORDER FLIPPED by integrator ruling 2026-08-28. WHO is a digest
        ABOUT the operator; the premises are rules FROM him. While WHO led AND
        was charged to PINNED_BUDGET it structurally outranked every rule, and
        four ratified owner premises went dark behind it. Rules lead now, and
        if a future squeeze drops anything it is the digest."""
        self.plant_profile()
        self.plant_pinned("pin-a", "always truth")
        got = inject.gather("anything")["pinned"]
        self.assertEqual(got, ["PREMISE pin-a: always truth",
                               "WHO operator: expert operator",
                               "WHO guidance: keep it short; batch deploys"])
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["pinned"], ["pin-a", "who:operator"])
        self.assertEqual(r["sample"]["lane_bytes"]["pinned"],
                         len("\n".join(got).encode("utf-8")))
        self.assertEqual(r["candidates"], 2)  # the digest + one pin

    def test_no_profile_absent(self):
        self.plant_pinned("pin-a", "always truth")
        got = inject.gather("anything")["pinned"]
        self.assertEqual(got, ["PREMISE pin-a: always truth"])
        self.assertEqual(self.rows()[-1]["fired"]["pinned"], ["pin-a"])
        self.assertEqual(self.rows()[-1]["candidates"], 1)

    def test_who_cap_two_terse_lines(self):
        self.plant_profile(level="x" * 500, guidance=("y" * 500,))
        got = inject.gather("anything")["pinned"]
        self.assertEqual(len(got), 1)  # level ate WHO_CAP; no room for guidance
        self.assertEqual(len(got[0]), inject.WHO_CAP)
        self.assertTrue(got[0].endswith("…"))
        self.plant_profile(level="expert", guidance=("g" * 500,))
        got = inject.gather("anything")["pinned"]
        self.assertEqual(len(got), 2)
        self.assertLessEqual(sum(len(l) for l in got), inject.WHO_CAP)
        self.assertTrue(got[1].endswith("…"))  # top guidance survives, terse

    def test_budget_drop_is_atomic_lane_survives(self):
        self.plant_profile()  # digest ~70B
        self.plant_pinned("pin-a", "tiny")
        # WHO is charged to WHO_CAP now, not PINNED_BUDGET, so the digest is
        # squeezed by ITS OWN budget. The invariant is unchanged and is the
        # point of the arm: an over-budget digest drops WHOLE and the rules
        # survive it.
        with mock.patch.object(inject, "WHO_CAP", 40):
            got = inject.gather("anything")["pinned"]
        self.assertEqual(got, ["PREMISE pin-a: tiny"],
                         "an over-budget digest drops WHOLE; the lane lives on")
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["pinned"], ["pin-a"])
        self.assertEqual(r["candidates"], 2)  # the dropped digest still counted

    def test_fail_open_garbled_and_raising(self):
        from helm import whoami
        self.plant_pinned("pin-a", "always truth")
        pk.atomic_write(whoami.profile_path(), "{not json")
        rc, out, err = self.run_inject([], stdin_text="anything")
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn("WHO", out)
        self.assertIn("pin-a", out)
        with mock.patch.object(whoami, "load_profile",
                               side_effect=RuntimeError("profile exploded")):
            rc, out, err = self.run_inject([], stdin_text="anything")
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn("WHO", out)
        self.assertIn("pin-a", out)

    def test_explain_renders_who_counts_candidate_no_ledger(self):  # noqa: VACUOUS_ASSERTION — the two assertIn lines ("+ WHO operator:" and "(over budget)") are unconditional positive controls on the same output; the assertNotIn is the ruled inversion of the pre-ruling law.
        self.plant_profile()
        self.plant_pinned("pin-a", "always truth")
        rc, out, _ = self.run_inject(["--explain"], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn("pinned (2 candidates, budget %dB):" % inject.PINNED_BUDGET, out)
        self.assertIn("+ WHO operator: expert operator", out)
        self.assertIn("+ WHO guidance: keep it short; batch deploys", out)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")
        # THE RULING (chat 2044 clauses 1-2, amended by row 64f555da38e6):
        # WHO no longer competes for PINNED_BUDGET. Squeezing the RULE budget
        # to 40B must therefore starve every RULE and leave the digest
        # STANDING -- it walks last, against WHO_CAP as its whole budget. The
        # pre-ruling arm asserted the exact opposite ("- who:operator (over
        # budget)", "+ WHO" absent), which is the accounting this lane removed:
        # a digest ABOUT the operator outranking every rule FROM him.
        # THE SQUEEZE HAS TO BE SMALLER THAN THE RULE'S OWN LINE, and 40B
        # was not. 40B bit only while WHO was CHARGED to PINNED_BUDGET: the
        # digest's ~70B ate the 40 and starved the rule behind it. THIS LANE'S
        # OWN RULING moved WHO out of that budget -- so the sole planted rule
        # ("PREMISE pin-a: always truth", 27B) now fits 40B with 13B to spare,
        # nothing is dropped, and the must-hit control below stopped biting.
        # The control's teeth WERE the accounting this lane removed, which is
        # why updating the two WHO assertions was not enough: a must-hit whose
        # bite depends on the behaviour under change retires itself silently
        # at the exact moment it is needed most.
        # So DERIVE the squeeze from the live renderer rather than naming a
        # number -- the house idiom for precisely this case
        # (test_store_gates `len(rule_line) - 1`; `rendered - 1` further down
        # this file). One byte under the FIRST rule's own line starves that
        # rule, and the greedy law ends the lane there, so every rule starves
        # however the fixture's gloss text later changes.
        rules = store.pinned()
        self.assertEqual(len(rules), 1,
                         "the derivation below spends the FIRST rule's line as "
                         "the whole rule budget; more rules would need the "
                         "running total instead")
        squeeze = len(inject._entry_line(rules[0])) - 1
        with mock.patch.object(inject, "PINNED_BUDGET", squeeze):
            rc, out, _ = self.run_inject(["--explain"], stdin_text="anything")
        self.assertIn("+ WHO operator:", out)
        self.assertNotIn("- who:operator (over budget)", out)
        # must-hit: the squeeze really bit, so WHO's survival is the LAW and
        # not an artefact of a budget that never excluded anything.
        self.assertIn("(over budget)", out)
        self.assertIn("did NOT fit the %dB rule budget" % squeeze, out)

    def test_the_WHO_digest_warms_the_session_once_not_every_turn(self):
        """WAS test_cooldown_exempt_fires_every_turn. The WHO digest is the
        pinned lane's declared FIRST entry and rode the same exemption; it is
        now the MARKER for whether a session has been warmed at all, so it
        fires on the warming turn and is silent until a context boundary."""
        self.plant_profile()
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        first = inject.gather("tune the fluxcap", session="s1")
        second = inject.gather("tune the fluxcap", session="s1")
        # POSITIVE CONTROL on the same observable: it really did arrive once.
        self.assertIn("WHO operator: expert operator", first["pinned"])
        self.assertEqual(second["pinned"], [], "warmed once per session")
        self.assertEqual((first["jit"], second["jit"]),
                         (["PRIOR 0.80 jit-a: a flux fact"], []))  # jit cools


class LaneReportTest(InjectBase):
    """--lane-report: the lane-split eval's read-only instrument — cohort
    classification (facts = lexicon/certain-prior/reference/profile; judgment
    = heuristic/belief-prior), the table off planted ledger fixtures, rotated
    generation included, zero mutation."""

    def plant_store(self):
        store.write_lexicon({"term": "term-a", "definition": "a fact of naming"})
        store.write_prior({"id": "cert-a", "statement": "a settled decision",
                           "confidence": "1.0", "keywords": "certkw"})
        store.write_prior({"id": "bel-a", "statement": "a held belief",
                           "confidence": "0.8", "keywords": "belkw"})
        store.write_heuristic({"id": "heur-a", "move": "a judgment move",
                               "keywords": "heurkw"})
        store.write_reference({"id": "ref-a", "statement": "a pointer",
                               "url": "https://x", "keywords": "refkw"})

    def plant_ledger(self, rotated, rows):
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".1", "w", encoding="utf-8") as f:
            f.write("# helm-inject-ledger protocol=1 generation=%s\n" %
                    ("1" * 32))
            f.writelines(json.dumps(r) + "\n" for r in rotated)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# helm-inject-ledger protocol=1 generation=%s\n" %
                    ("2" * 32))
            f.writelines(json.dumps(r) + "\n" for r in rows)

    FIXTURE_OLD = [{"v": 1, "ts": "t1", "session": "s1", "fired": {
        "pinned": ["cert-a", "who:operator"], "jit": ["term-a", "bel-a"],
        "reflex": ["punt-tell"]}}]
    FIXTURE = [
        {"v": 1, "ts": "t2", "session": "s2",
         "fired": {"pinned": ["cert-a"], "jit": ["heur-a", "ref-a"], "reflex": []},
         "suppressed": ["bel-a"]},
        {"v": 1, "ts": "t3", "silent": True},
        {"v": 1, "ts": "t4", "silent": True, "session": "s2"},
    ]

    def test_cohort_classification(self):
        self.plant_store()
        by_id = {e["id"]: e for e in store.load_all()}
        self.assertEqual(inject._cohort(by_id["term-a"]), "facts")
        self.assertEqual(inject._cohort(by_id["cert-a"]), "facts")
        self.assertEqual(inject._cohort(by_id["ref-a"]), "facts")
        self.assertEqual(inject._cohort(by_id["bel-a"]), "judgment")
        self.assertEqual(inject._cohort(by_id["heur-a"]), "judgment")
        self.assertIsNone(inject._cohort({"type": "episodic"}))

    def test_report_numbers_from_planted_fixtures(self):
        self.plant_store()
        self.plant_ledger(self.FIXTURE_OLD, self.FIXTURE)
        r = inject.lane_report()
        self.assertEqual((r["rows"], r["fired_rows"], r["silent"],
                          r["sessions"]), (4, 2, 2, 2))
        f, j, o = (r["cohorts"][k] for k in ("facts", "judgment", "other"))
        # facts: cert-a x2 + who:operator + term-a + ref-a = 5 fires, 4 ids
        self.assertEqual((f["fires"], f["ids"], f["sessions"], f["suppressed"]),
                         (5, 4, 2, 0))
        self.assertGreater(f["bytes"], 0)
        # judgment: bel-a + heur-a fired once each; bel-a suppressed once
        self.assertEqual((j["fires"], j["ids"], j["suppressed"]), (2, 2, 1))
        # other: the reflex id — machinery, outside the A/B question
        self.assertEqual((o["fires"], o["ids"]), (1, 1))
        self.assertEqual(r["halves"], [[0, 2], [2, 2]])  # silent-rate trend

    def test_table_renders_and_is_read_only(self):
        self.plant_store()
        self.plant_ledger(self.FIXTURE_OLD, self.FIXTURE)
        with open(inject._ledger_path(), encoding="utf-8") as fh:
            before = fh.read()
        rc, out, err = self.run_inject(["--lane-report"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("lane-split cohorts — 4 rows (2 fired, 2 silent), 2 sessions", out)
        facts = next(l for l in out.splitlines() if l.startswith("facts"))
        judgment = next(l for l in out.splitlines() if l.startswith("judgment"))
        self.assertIn(" 5 ", facts.replace("%", " "))
        self.assertIn("33.3%", judgment)  # 1 suppressed of 2+1 seen
        self.assertIn("first half 0.0% -> second half 100.0%", out)
        self.assertIn("fires are not heeds", out)
        self.assertIn("per-turn outcome markers", out)
        self.assertNotIn("evals/", out)   # names no file the tree does not ship
        with open(inject._ledger_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before,
                             "--lane-report must never write the ledger")
        self.assertFalse(os.path.exists(inject._seen_dir()))
        self.assertFalse(os.path.exists(inject._coinage_path()))

    def test_empty_ledger_reports_unfired(self):
        self.plant_ledger([], [])
        rc, out, _ = self.run_inject(["--lane-report"])
        self.assertEqual(rc, 0)
        self.assertIn("no ledger rows yet", out)
        self.assertNotIn("UNKNOWN", out)

    def test_no_protocol_marker_reports_unknown_not_unfired(self):
        rc, out, _ = self.run_inject(["--lane-report"])
        self.assertEqual(rc, 0)
        self.assertIn("source census incomplete", out)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("instrument is unfired", out)


class WhisperTest(InjectBase):
    """The first-turn whisper: once per calendar day the day's first
    session-bearing, non-empty turn LEADS with a one-line brief digest, latched
    in _global/.state/greeted.json; later turns stay silent (and pay no brief
    read), day-rollover re-fires, brief trouble fails open, --explain renders it
    read-only, and the empty-output rc-0 hook contract is preserved. brief is
    mocked at helm.brief.compose so the lane is exercised hermetically."""

    BRIEF = {"sessions": {"total": 4},
             "knowledge": {"added": ["a", "b"], "updated": [], "retired": [],
                           "drained": 0},
             "waiting": ["interview"],
             "owner_asks": [{"ask": "relogin"}]}
    QUIET = {"sessions": {"total": 0},
             "knowledge": {"added": [], "updated": [], "retired": [], "drained": 0},
             "waiting": [], "owner_asks": []}
    UNKNOWN = dict(QUIET, owner_asks_unavailable="queue unreadable")

    def hook(self, prompt="what's the plan today?", session="sid-1"):
        return json.dumps({"prompt": prompt, "session_id": session,
                           "hook_event_name": "UserPromptSubmit"})

    def rows(self):
        return list(inject._ledger_rows())

    def test_fires_once_per_day_then_silent(self):
        with mock.patch("helm.brief.compose", return_value=self.BRIEF) as m:
            line, = inject.gather("hello", session="s1")["whisper"]
            self.assertTrue(line.startswith("BRIEF: "))
            self.assertIn("4 sessions since you left", line)
            self.assertIn("+2 knowledge", line)
            self.assertIn("2 need you", line)
            self.assertLessEqual(len(line), inject.WHISPER_CAP)
            self.assertEqual(m.call_count, 1)
            for _ in range(3):  # every later turn that day: silent, no brief read
                self.assertEqual(inject.gather("hello", session="s1")["whisper"], [])
            self.assertEqual(m.call_count, 1, "only the first turn reads the brief")
        self.assertEqual(pk.read_json(inject._greeted_path())["day"], inject._today())

    def test_leads_the_rendered_output(self):
        self.plant_pinned("pin-a", "always truth")
        with mock.patch("helm.brief.compose", return_value=self.BRIEF):
            rc, out, _ = self.run_inject(["--hook-json"], stdin_text=self.hook())
        self.assertEqual(rc, 0)
        self.assertTrue(out.splitlines()[0].startswith("BRIEF: "),
                        "the whisper must lead the injected output")
        self.assertIn("PREMISE pin-a: always truth", out)  # the lanes still ride

    def test_day_rollover_refires(self):
        with mock.patch("helm.brief.compose", return_value=self.BRIEF):
            with mock.patch.object(inject, "_today", return_value="2026-07-19"):
                self.assertTrue(inject.gather("hi", session="s1")["whisper"])
                self.assertFalse(inject.gather("hi", session="s1")["whisper"])
            with mock.patch.object(inject, "_today", return_value="2026-07-20"):
                self.assertTrue(inject.gather("hi", session="s1")["whisper"],
                                "a new calendar day re-greets")

    def test_quiet_day_whispers_nothing_but_still_latches(self):
        with mock.patch("helm.brief.compose", return_value=self.QUIET) as m:
            self.assertEqual(inject.gather("hi", session="s1")["whisper"], [])
            self.assertEqual(inject.gather("hi", session="s1")["whisper"], [])
            self.assertEqual(m.call_count, 1, "the quiet first turn still latches")
        self.assertTrue(inject._greeted_today())

    def test_unreadable_owner_queue_whispers_unknown_not_quiet(self):
        with mock.patch("helm.brief.compose", return_value=self.UNKNOWN):
            line, = inject.gather("hello", session="s1")["whisper"]
        self.assertIn("owner asks UNKNOWN", line)

    def test_fail_open_when_brief_unavailable(self):
        with mock.patch("helm.brief.compose",
                        side_effect=RuntimeError("brief exploded")):
            rc, out, err = self.run_inject(["--hook-json"], stdin_text=self.hook())
        self.assertEqual((rc, out, err), (0, "", ""),
                         "brief trouble -> no whisper, never a blocked hook")

    def test_no_session_or_empty_prompt_never_whispers(self):
        with mock.patch("helm.brief.compose", return_value=self.BRIEF) as m:
            self.assertEqual(inject.gather("hello")["whisper"], [])       # no session
            self.assertEqual(inject.gather("   ", session="s1")["whisper"], [])  # blank
            self.assertEqual(m.call_count, 0, "the gate pays no brief compose")
        self.assertFalse(os.path.exists(inject._greeted_path()))

    def test_whisper_rides_the_ledger_ids_never_text(self):  # noqa: VACUOUS_ASSERTION — the non-empty whisper id and byte sample positively control absence of rendered whisper text
        with mock.patch("helm.brief.compose", return_value=self.BRIEF):
            inject.gather("hello", session="s1")
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["whisper"], [inject.WHISPER_ID])
        self.assertGreater(r["sample"]["lane_bytes"]["whisper"], 0)
        with open(inject._ledger_path(), encoding="utf-8") as f:
            self.assertNotIn("since you left", f.read())  # the id rides, not the line

    def test_explain_renders_it_read_only(self):
        with mock.patch("helm.brief.compose", return_value=self.BRIEF):
            rc, out, _ = self.run_inject(["--hook-json", "--explain"],
                                         stdin_text=self.hook())
        self.assertEqual(rc, 0)
        self.assertIn("whisper (first turn today):", out)
        self.assertIn("BRIEF: helm morning", out)
        self.assertFalse(inject._greeted_today(),
                         "--explain must never stamp the greeted latch")
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")


class SaWhisperTest(InjectBase):
    """The codex-only whisper pair: the exact lines that fit walk last, fire
    once per context, and re-arm on fresh identity or context loss. Claude
    families never see them; pressure delays delivery rather than evicting a
    premise; suppression is measurable and fail-open."""

    def seat(self, name):
        if name is None:
            os.environ.pop("HELM_CHAT_NAME", None)
        else:
            os.environ["HELM_CHAT_NAME"] = name

    def rows(self):
        return list(inject._ledger_rows())

    def test_codex_seat_gets_the_lines_last_in_pinned(self):
        self.plant_pinned("pin-a", "always truth")
        self.seat("codex")
        got = inject.gather("anything at all")["pinned"]
        self.assertEqual(got[-2:], [inject.SA_WHISPER, inject.CLAIM_WHISPER],
                         "the nudges ride LAST in SA_LINES order — premises lead")
        self.assertIn("PREMISE pin-a: always truth", got[0])
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["pinned"][-2:],
                         [inject.SA_WHISPER_ID, inject.CLAIM_WHISPER_ID],
                         "ledger honesty: each fired nudge under its own id")

    def test_pair_fires_once_per_context_and_rearms_on_identity_or_loss(self):
        from helm.inject import _ledger
        self.seat("codex")
        pair = [inject.SA_WHISPER, inject.CLAIM_WHISPER]
        first = inject.gather("ordinary work", session="s1")["pinned"]
        later = [inject.gather("ordinary work", session="s1")["pinned"]
                 for _ in range(39)]
        self.assertEqual(first, pair, "turn 1 teaches both codex nudges")
        self.assertTrue(all(not any(line in got for line in pair) for got in later),
                        "turn 40 must not repay the unchanged pair")
        self.assertEqual(inject.gather("ordinary work", session="s2")["pinned"],
                         pair, "a fresh session has not received the pair")
        self.assertTrue(_ledger.forget_session("s1"))
        self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"],
                         pair, "context loss must re-arm the pair")

    def test_partial_delivery_frees_budget_for_the_undelivered_line(self):
        self.seat("codex")
        with mock.patch.object(inject, "PINNED_BUDGET", 150):
            self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"],
                             [inject.SA_WHISPER])
            rc, out, _ = self.run_inject(
                ["--hook-json", "--explain"],
                stdin_text=json.dumps({"prompt": "ordinary work", "cwd": self.tmp,
                                       "session_id": "s1"}))
            self.assertEqual(rc, 0)
            self.assertIn(inject.SA_WHISPER + " (already delivered", out)
            self.assertIn("+ " + inject.CLAIM_WHISPER, out,
                          "explain must return the suppressed line's budget too")
            self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"],
                             [inject.CLAIM_WHISPER],
                             "a delivered line cannot crowd out its unseen sibling")
            self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"], [],
                             "each line is paid exactly once per context")

    def test_pair_suppression_is_not_laundered_as_absence(self):
        self.seat("codex")
        pair = [inject.SA_WHISPER, inject.CLAIM_WHISPER]
        self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"], pair)
        self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"], [])
        row = self.rows()[-1]
        self.assertEqual(row["suppressed_nudges"],
                         [inject.SA_WHISPER_ID, inject.CLAIM_WHISPER_ID])
        self.assertEqual(row["suppressed_utf8_bytes"]["nudges"],
                         len((inject.SA_WHISPER + "\n" +
                              inject.CLAIM_WHISPER).encode("utf-8")))

    def test_old_seen_state_rearms_the_pair_once(self):
        from helm.inject import _ledger
        self.seat("codex")
        _ledger._seen_save("s1", {"turn": 7, "fired": {}, "pinned": "old"})
        self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"],
                         [inject.SA_WHISPER, inject.CLAIM_WHISPER])
        self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"], [],
                         "the rolling migration may cost exactly one delivery")

    def test_changed_wording_rearms_only_the_changed_line(self):
        from helm.inject import _entries
        self.seat("codex")
        inject.gather("ordinary work", session="s1")
        changed = inject.SA_WHISPER + " Act on this version."
        with mock.patch.object(
                _entries, "SA_LINES",
                ((changed, inject.SA_WHISPER_ID),
                 (inject.CLAIM_WHISPER, inject.CLAIM_WHISPER_ID))):
            self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"],
                             [changed],
                             "content mutation re-arms only its own ledger id")
            self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"], [])

    def test_explain_reports_suppression_without_mutating_it(self):
        self.seat("codex")
        self.assertEqual(inject.gather("ordinary work", session="s1")["pinned"],
                         [inject.SA_WHISPER, inject.CLAIM_WHISPER])
        before = inject._seen_load("s1")
        rc, out, _ = self.run_inject(
            ["--hook-json", "--explain"],
            stdin_text=json.dumps({"prompt": "ordinary work", "cwd": self.tmp,
                                   "session_id": "s1"}))
        self.assertEqual(rc, 0)
        self.assertIn("already delivered this context", out)
        self.assertEqual(inject._seen_load("s1"), before,
                         "explain must remain read-only")

    def test_line_is_terse_and_justification_free(self):
        self.assertIn("subagents", inject.SA_WHISPER)
        self.assertLessEqual(len(inject.SA_WHISPER), inject.LINE_CAP)
        for word in ("because", "compaction", "context window", "justif"):
            self.assertNotIn(word, inject.SA_WHISPER.lower(),
                             "the justification is deliberately left out")

    def test_codex_instance_seat_resolves_through_the_one_resolver(self):
        self.seat("codex-3")  # slice-6 instance: _seat_family's law, not ours
        self.assertIn(inject.SA_WHISPER, inject.gather("hi")["pinned"])

    def test_claude_family_seats_never_get_it(self):
        self.plant_pinned("pin-a", "always truth")
        for name in (None, "helm-fable", "claude", "opus-integrator", "fable-2"):
            self.seat(name)
            got = inject.gather("anything")["pinned"]
            self.assertNotIn(inject.SA_WHISPER, got,
                             "claude-family seat %r must never see the nudge" % name)
            self.assertNotIn(inject.CLAIM_WHISPER, got,
                             "claude-family seat %r must never see claim-start" % name)

    def test_codexes_only_kimi_excluded(self):
        self.seat("kimi")  # a real seat family, but the ask is codexes-only
        got = inject.gather("hi")["pinned"]
        self.assertNotIn(inject.SA_WHISPER, got)
        self.assertNotIn(inject.CLAIM_WHISPER, got)

    def test_full_budget_drops_the_nudges_never_a_premise(self):  # noqa: VACUOUS_ASSERTION — assertEqual(got, baseline) is the unconditional positive control; the assertNotIn pair names the two nudges a full budget must shed.
        # PRESSURE PINNED, like its two siblings above. The invariant here is
        # that a FULL budget sheds the nudges and never a premise — a property
        # of the greedy walk, not of PINNED_BUDGET's value. Deriving the fill
        # from the constant does NOT work: the walk stops at the first line
        # that does not fit, so 3x395B leaves 15B at 1200 and 355B at 1540, and
        # 355B is room enough for both nudges. The squeeze has to be pinned to
        # be a squeeze.
        for i in range(4):  # 3 x 395B rendered lines fill a 1200B budget
            self.plant_pinned("big-%d" % i, "x" * 380)
        with mock.patch.object(inject, "PINNED_BUDGET", 1200):
            squeezed = inject.PINNED_BUDGET      # the budget the lane was BUILT under
            self.seat(None)
            baseline = inject.gather("anything")["pinned"]
            self.seat("codex")
            got = inject.gather("anything")["pinned"]
            # ASSERT INSIDE THE PATCH, or against the captured value. These
            # assertions used to run after the with-block exited, so the last
            # one compared a lane built at 1200 against the RESTORED 1540 and
            # passed for free. A patch that ends before its own comparison is
            # a fixture that never reached the state it set up.
            self.assertEqual(got, baseline,
                             "a full budget drops the nudges and nothing else")
            self.assertNotIn(inject.SA_WHISPER, got)
            self.assertNotIn(inject.CLAIM_WHISPER, got)
            self.assertLessEqual(sum(len(l) for l in rule_lines(got)), squeezed)

    def test_family_derivation_is_the_existing_resolver(self):
        # the resolver is the AUTHORITY: its verdict, not the raw name, gates
        with mock.patch("helm.seat._seat_family",
                        return_value=("codex", None)) as m:
            self.seat("anything-at-all")
            self.assertIn(inject.SA_WHISPER, inject.gather("hi")["pinned"])
            m.assert_called_with("anything-at-all")
        with mock.patch("helm.seat._seat_family", return_value=(None, "nope")):
            self.seat("codex")
            self.assertNotIn(inject.SA_WHISPER, inject.gather("hi")["pinned"])

    def test_resolver_trouble_fails_open(self):
        self.seat("codex")
        with mock.patch("helm.seat._seat_family",
                        side_effect=RuntimeError("seat exploded")):
            rc, out, err = self.run_inject([], stdin_text="unmatched words")
        self.assertEqual((rc, out, err), (0, "", ""),
                         "resolver trouble -> no nudge, never a blocked turn")

    def test_explain_renders_the_nudges(self):
        self.seat("codex")
        rc, out, _ = self.run_inject(["--explain"], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn(inject.SA_WHISPER, out)
        self.assertIn(inject.SA_WHISPER_ID, out)
        self.assertIn(inject.CLAIM_WHISPER, out)
        self.assertIn(inject.CLAIM_WHISPER_ID, out)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")


class ClaimWhisperTest(InjectBase):
    """The codex-only claim-start whisper (owner ask 2026-07-23): a codex
    claimed two lanes then STOPPED without starting the writers — law-5's
    'end turns at bounded milestones' read as 'posted my claim'. The second
    SA_LINES line teaches claim-is-a-start-not-a-stop, terse, justification
    left out; it rides the SAME budget walk AFTER the SA-delegation line
    (pressure drops it first, never a premise), ledgered under its own id.
    Claude-family + kimi seats never see it (SaWhisperTest covers those)."""

    def seat(self, name):
        if name is None:
            os.environ.pop("HELM_CHAT_NAME", None)
        else:
            os.environ["HELM_CHAT_NAME"] = name

    def rows(self):
        return list(inject._ledger_rows())

    def test_line_is_terse_and_justification_free(self):
        self.assertEqual(inject.CLAIM_WHISPER,
                         "Claiming a lane is a START, not a milestone — "
                         "launch the writer this same turn; end your turn "
                         "only when work is visibly moving.")
        self.assertLessEqual(len(inject.CLAIM_WHISPER), inject.LINE_CAP)
        for word in ("because", "law-5", "milestone-discipline", "justif"):
            self.assertNotIn(word, inject.CLAIM_WHISPER.lower(),
                             "the justification is deliberately left out")

    def test_codex_seat_gets_both_lines_delegation_first(self):
        self.seat("codex-3")  # instance names ride the one resolver too
        got = inject.gather("anything at all")["pinned"]
        self.assertIn(inject.SA_WHISPER, got)
        self.assertIn(inject.CLAIM_WHISPER, got)
        self.assertLess(got.index(inject.SA_WHISPER),
                        got.index(inject.CLAIM_WHISPER),
                        "SA-delegation leads, claim-start follows")

    def test_pressure_drops_claim_start_first_never_a_premise(self):
        # THE PRESSURE IS PINNED, NOT INHERITED. This arm tests the DEGRADATION
        # ORDER under a squeeze — claim-start goes before SA, and neither ever
        # evicts a premise — which is a property of the walk, not of whatever
        # PINNED_BUDGET happens to be. Riding the production constant made it
        # break when the budget moved to 1540 for reasons that have nothing to
        # do with what it asserts.
        for i in range(3):  # 3 x 350B lines leave 150B: SA (84B) fits,
            self.plant_pinned("big-%d" % i, "x" * 335)  # claim (127B) drops
        self.seat("codex")
        with mock.patch.object(inject, "PINNED_BUDGET", 1200):
            squeezed = inject.PINNED_BUDGET      # the budget the lane was BUILT under
            got = inject.gather("anything")["pinned"]
            # inside the patch: the last assertion used to compare a lane built
            # at 1200 against the RESTORED budget and passed for free
            for i in range(3):
                self.assertTrue(any(("big-%d" % i) in l for l in got),
                                "a nudge never evicts a premise")
            self.assertIn(inject.SA_WHISPER, got,
                          "the earlier SA_LINES line survives the squeeze")
            self.assertNotIn(inject.CLAIM_WHISPER, got,
                             "the later SA_LINES line degrades first")
            self.assertLessEqual(sum(len(l) for l in got), squeezed)

    def test_ledger_honesty_for_both_ids(self):
        self.seat("codex")
        inject.gather("anything at all")
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["pinned"],
                         [inject.SA_WHISPER_ID, inject.CLAIM_WHISPER_ID])
        self.assertEqual(r["candidates"], 2,
                         "each nudge counts as its own candidate")

    def test_explain_parity_under_pressure(self):
        # pressure pinned for the same reason as its sibling above: --explain
        # must agree with gather UNDER A SQUEEZE, whatever the budget is.
        for i in range(3):  # gather's exact squeeze: SA fires, claim drops
            self.plant_pinned("big-%d" % i, "x" * 335)
        self.seat("codex")
        with mock.patch.object(inject, "PINNED_BUDGET", 1200):
            rc, out, _ = self.run_inject(["--explain"], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn("+ %s" % inject.SA_WHISPER, out)
        self.assertIn("- %s (over budget)" % inject.CLAIM_WHISPER_ID, out)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")

    def test_resolver_trouble_fails_open(self):
        self.seat("codex")
        with mock.patch("helm.seat._seat_family",
                        side_effect=RuntimeError("seat exploded")):
            rc, out, err = self.run_inject([], stdin_text="unmatched words")
        self.assertEqual((rc, out, err), (0, "", ""),
                         "resolver trouble -> no nudges, never a blocked turn")


class _FakeBackend:
    """A comparison-backend test double — the honest injection seam (no network, no
    monkeypatch). resolve returns the planted ids (or raises for the fail-open
    path); records its calls so the zero-cost / empty-prompt gates are provable."""
    name = "fake"
    source = "test double"

    def __init__(self, ids=None, raises=False):
        self.ids = list(ids or [])
        self.raises = raises
        self.calls = []

    def configured(self):
        return True

    def resolve(self, text, project=None):
        self.calls.append((text, project))
        if self.raises:
            raise RuntimeError("boom")
        return list(self.ids)


class CompareBackendTest(InjectBase):
    """The pluggable comparison-resolver seam: interface dispatch, the CF
    stub's env gate (no network), divergence logging with an injected fake,
    the comparison-off zero-cost path, fail-open on a raising backend,
    --compare-report both states, and the byte-identical local lane law."""

    def test_interface_local_is_authority(self):
        # the local backend behind the interface reproduces the resolver output
        self.plant_jit("jit-a", "alpha fact", "alpha")
        self.plant_jit("jit-b", "beta fact", "beta")
        self.assertEqual(inject.LOCAL_BACKEND.resolve("tune the alpha now"), ["jit-a"])
        self.assertEqual(inject.LOCAL_BACKEND.name, "local")
        # law 3 (name your source): a mandatory declaration on every backend
        self.assertTrue(inject.LOCAL_BACKEND.source)
        self.assertTrue(inject.CFCompareBackend().source)

    def test_active_compare_off_by_default_cf_when_configured(self):
        self.assertIsNone(inject._active_compare())  # unconfigured => OFF
        os.environ["HELM_CF_ENDPOINT"] = "https://example.invalid/query"
        b = inject._active_compare()
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "cf")

    def test_cf_stub_empty_without_endpoint_no_network(self):
        # the zero-network guard: unconfigured resolve returns [] before urllib
        cf = inject.CFCompareBackend()
        self.assertFalse(cf.configured())
        self.assertEqual(cf.resolve("anything at all"), [])
        # configured-but-empty-prompt also short-circuits before any network
        os.environ["HELM_CF_ENDPOINT"] = "https://example.invalid/query"
        self.assertEqual(cf.resolve("   "), [])

    def test_divergence_logged_with_fake_backend(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        self.plant_jit("jit-b", "beta fact", "beta")
        fake = _FakeBackend(ids=["jit-a", "cf-x"])  # local finds [jit-a]
        inject.gather("tune the alpha now", compare=fake)
        rows = inject._compare_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["backend"], "fake")
        self.assertEqual(r["agreed"], ["jit-a"])
        self.assertEqual(r["compare_only"], ["cf-x"])
        self.assertEqual(r["local_only"], [])
        self.assertEqual(r["local_n"], 1)
        self.assertEqual(r["compare_n"], 2)

    def test_compare_ledger_holds_ids_never_prompt_text(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        inject.gather("tune the alpha now", compare=_FakeBackend(ids=["cf-x"]))
        with open(inject._compare_ledger_path(), encoding="utf-8") as f:
            self.assertNotIn("tune the alpha", f.read())  # ids ride, not the prompt

    def test_compare_off_writes_no_ledger_zero_cost(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        inject.gather("tune the alpha now")  # no compare param, no env => OFF
        self.assertFalse(os.path.exists(inject._compare_ledger_path()))

    def test_compare_skips_empty_prompt(self):
        fake = _FakeBackend(ids=["cf-x"])
        inject.gather("   ", compare=fake)
        self.assertEqual(fake.calls, [])  # never queried on an empty turn
        self.assertFalse(os.path.exists(inject._compare_ledger_path()))

    def test_fail_open_on_backend_raise(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        sections = inject.gather("tune the alpha now", compare=_FakeBackend(raises=True))
        self.assertTrue(sections["jit"], "the local lane must survive a comparison raise")
        rows = inject._compare_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["error"])
        self.assertEqual(rows[0]["backend"], "fake")

    def test_local_lane_byte_identical_with_and_without_compare(self):
        self.plant_pinned("pin-1", "a pinned truth")
        self.plant_jit("jit-a", "alpha fact", "alpha")
        off = inject.gather("tune the alpha now")
        on = inject.gather("tune the alpha now", compare=_FakeBackend(ids=["totally", "other"]))
        self.assertEqual(off, on)
        boom = inject.gather("tune the alpha now", compare=_FakeBackend(raises=True))
        self.assertEqual(off, boom)

    def test_compare_report_off_state(self):
        rc, out, err = self.run_inject(["--compare-report"])
        self.assertEqual(rc, 0)
        self.assertIn("comparison backend off", out)
        self.assertIn("HELM_CF_ENDPOINT", out)
        self.assertEqual(err, "")

    def test_compare_report_renders_divergence(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        fake = _FakeBackend(ids=["jit-a", "cf-x", "cf-y"])
        inject.gather("tune the alpha now", compare=fake)
        inject.gather("tune the alpha again", compare=fake)
        rc, out, err = self.run_inject(["--compare-report"])
        self.assertEqual(rc, 0)
        self.assertIn("agreed", out)
        self.assertIn("compare-only", out)
        self.assertIn("cf-x", out)  # the concrete hot id, not a vibe
        self.assertIn("2 comparison turns", out)

    def test_compare_report_no_ledger_row_written(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        inject.gather("tune the alpha now", compare=_FakeBackend(ids=["cf-x"]))
        before = len(inject._compare_rows())
        self.run_inject(["--compare-report"])
        self.assertEqual(len(inject._compare_rows()), before)  # read-only

    def test_explain_surfaces_active_compare_read_only(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        # off => no comparison line (salience)
        _, off_out, _ = self.run_inject(["--explain"], stdin_text="tune the alpha now")
        self.assertNotIn("comparison: backend", off_out)
        os.environ["HELM_CF_ENDPOINT"] = "https://example.invalid/query"
        _, on_out, _ = self.run_inject(["--explain"], stdin_text="tune the alpha now")
        self.assertIn("comparison: backend cf active", on_out)
        # --explain is a dry look: it never queries and never writes the ledger
        self.assertFalse(os.path.exists(inject._compare_ledger_path()))


class CouncilReachTest(InjectBase):
    """The council reach rung (premise council-is-the-number-one-feature +
    feature-and-rsh-must-both-be-wired): the recorded predecessor-harness failure was
    SALIENCE — the meld verb existed and agents never reached for it. >= 3
    ping-pong rounds with ONE peer in the home room -> one latched nudge
    naming the exact council invite command; a new streak re-arms."""
    EXTRA = ("HELM_CHAT_DIR", "MELD_CHAT_DIR", "HELM_CHAT_ROOM",
             "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE", "HELM_CHAT_NAME",
             "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
             "HELM_CHAT_OWNER_NAMES", "CLAUDE_CODE_SESSION_ID",
             "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

    def setUp(self):
        super().setUp()
        self.extra_prior = {k: os.environ.get(k) for k in self.EXTRA}
        for k in self.EXTRA:
            os.environ.pop(k, None)
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_ROOM"] = "workroom"
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"

    def tearDown(self):
        for k, v in self.extra_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _pingpong(self, rounds=3, peer="seat-b", room="workroom"):
        from helm import chat
        for i in range(rounds):
            chat.post("q%d" % i, room=room, who="seat-a")
            chat.post("a%d" % i, room=room, who=peer)

    def _review_pingpong(self, rounds=3, peer="seat-b", room="workroom"):
        """A streak where BOTH sides speak review vocabulary."""
        from helm import chat
        for i in range(rounds):
            chat.post("gate request %d, please xrev" % i, room=room, who="seat-a")
            chat.post("FIX %d — one more blocker" % i, room=room, who=peer)

    def test_the_percase_cure_rides_the_streak_on_a_REVIEW(self):
        """The cure for a 3+-round spiral sat in the store keyed on its own
        CONCLUSION — per-case, whole-object, enumeration — the words you have
        AFTER you understand it. Nobody mid-spiral types those. The
        condition is a MEASURABLE EVENT this reflex already detects, so the
        lesson rides the detector instead of waiting to be searched for."""
        self._review_pingpong(3)
        got = inject._council_reach(None, None)
        self.assertIsNotNone(got)
        line, _wid = got
        self.assertIn("async rounds", line)          # the streak nudge itself
        self.assertIn("PER-CASE", line)              # and the cure
        self.assertIn("whole-object", line)
        self.assertIn("honest refusal", line)

    def test_the_cure_stays_SILENT_on_an_ordinary_streak(self):
        """Pinned as hard as the firing case. A cure line about per-case
        handling on ordinary back-and-forth is wallpaper, and wallpaper is how
        a guard stops being read — the same noise failure the tree warning
        taught us the same day."""
        self._pingpong(3)                            # plain q/a, no review words
        got = inject._council_reach(None, None)
        self.assertIsNotNone(got)                    # the streak STILL nudges
        line, _wid = got
        self.assertIn("async rounds", line)
        self.assertNotIn("PER-CASE", line)           # but carries no cure
        self.assertNotIn("whole-object", line)

    def test_one_sided_review_vocabulary_is_not_a_review_streak(self):
        """Both sides must use it: one person saying 'fix' in a design chat is
        a conversation, not a review."""
        from helm import chat
        for i in range(3):
            chat.post("we should fix the gate design %d" % i,
                      room="workroom", who="seat-a")
            chat.post("agreed, thinking about it %d" % i,
                      room="workroom", who="seat-b")
        got = inject._council_reach(None, None)
        self.assertIsNotNone(got)
        self.assertNotIn("PER-CASE", got[0])

    def test_reach_fires_once_per_streak_then_rearms(self):
        from helm import chat
        self._pingpong(3)
        got = inject._council_reach(None, None)
        self.assertIsNotNone(got)
        line, wid = got
        self.assertIn("helm chat council invite seat-b", line)
        self.assertEqual(wid, inject.COUNCIL_WHISPER_ID)
        self.assertIsNone(inject._council_reach(None, None))   # latched
        self._pingpong(1)                                      # SAME streak
        self.assertIsNone(inject._council_reach(None, None))   # grows silent
        chat.post("third voice", room="workroom", who="seat-c")
        self._pingpong(3)                                      # NEW streak
        self.assertIsNotNone(inject._council_reach(None, None))

    def test_latch_holds_past_the_tail_cap_and_over_reactions(self):
        """Live-probed 2026-07-23: fp = room|peer|(total-len(suffix)) mixed
        coordinate systems — total counted ALL rows while suffix capped at
        COUNCIL_TAIL attributed rows — so the rung RE-FIRED every turn once
        a streak outgrew the cap, and re-fired on any reaction row: wallpaper
        on exactly the agents deepest in ping-pong."""
        from helm import chat
        self._pingpong(3)
        self.assertIsNotNone(inject._council_reach(None, None))   # latches
        self._pingpong(6)      # 18 attributed rows — past COUNCIL_TAIL=16
        self.assertIsNone(inject._council_reach(None, None))
        self._pingpong(1)      # deeper still: every extra round re-fired
        self.assertIsNone(inject._council_reach(None, None))
        _row, err = chat.react(-1, "👍", room="workroom", who="seat-c")
        self.assertIsNone(err)
        self.assertIsNone(inject._council_reach(None, None))      # no drift
        chat.post("break", room="workroom", who="seat-c")
        self._pingpong(3)                                         # NEW streak
        self.assertIsNotNone(inject._council_reach(None, None))   # re-arms

    def test_reaction_mid_streak_before_the_cap_keeps_the_latch(self):
        from helm import chat
        self._pingpong(3)
        self.assertIsNotNone(inject._council_reach(None, None))
        _row, err = chat.react(-1, "👍", room="workroom", who="seat-c")
        self.assertIsNone(err)
        self._pingpong(1)                                         # SAME streak
        self.assertIsNone(inject._council_reach(None, None))

    def test_deep_streak_with_unknown_start_latches_the_pair(self):
        """First observed already past the cap (start hidden by the window):
        one fire, then the pair itself stays latched — no wallpaper even
        when the streak's start offset is unknowable."""
        self._pingpong(9)      # 18 rows: suffix saturates on first look
        self.assertIsNotNone(inject._council_reach(None, None))
        self._pingpong(1)
        self.assertIsNone(inject._council_reach(None, None))

    def test_under_threshold_owner_and_monologue_stay_silent(self):
        from helm import chat
        self._pingpong(2)
        self.assertIsNone(inject._council_reach(None, None))   # 2 < 3 rounds
        chat.post("q", room="workroom", who="seat-a")
        chat.post("from the human", room="workroom", who="daria")
        self.assertIsNone(inject._council_reach(None, None))   # owner talk
        for i in range(5):
            chat.post("mono%d" % i, room="workroom", who="seat-b")
        chat.post("one reply", room="workroom", who="seat-a")
        self.assertIsNone(inject._council_reach(None, None))   # no ping-pong

    def test_meld_room_and_mid_meld_pair_stay_silent(self):
        from helm import meld
        self._pingpong(3)
        meld.invite("seat-b", "the topic", seat="seat-a")      # verb reached
        self.assertIsNone(inject._council_reach(None, None))

    def test_rides_the_reflex_lane_and_ledger(self):
        self._pingpong(3)
        sections = inject.gather("carry on", session="sid-r", cwd=None)
        joined = "\n".join(sections["reflex"])
        self.assertIn("helm chat council invite seat-b", joined)
        rows = inject._ledger_rows()
        self.assertIn(inject.COUNCIL_WHISPER_ID, rows[-1]["fired"]["reflex"])


if __name__ == "__main__":
    unittest.main()


class PinnedDropIsAnnounced(InjectBase):
    """A pinned rule the budget cannot carry must SAY it was dropped.

    The pinned lane read `if not it[6]: continue` — a plain skip with no
    marker, no counter, nothing. So a rule the budget could not carry and a
    rule that does not exist produced the SAME observable: absence. Measured
    2026-08-28: three owner premises carried no gloss, truncated at LINE_CAP,
    and consumed the whole budget between them; the greedy walk then dropped
    the remaining FIVE in silence, every turn, on every seat.
    """

    def plant_profile(self, level="expert operator",
                      guidance=("keep it short", "batch deploys")):
        """The WHO digest fixture — BYTE-IDENTICAL to the sibling class's
        planter, deliberately.

        Without a profile the WHO assertions skipped conditionally, so this
        had to exist. My first version wrote the profile through a DIFFERENT
        call, which would have made two fixture builders for one artifact —
        the exact duplication this lane cures in production. Same shape, same
        keys, same schema_version: if the profile format moves, both move
        together or both break together."""
        pk.write_json(
            os.path.join(home.global_dir(), "know-your-user", "profile.json"),
            {"schema_version": 2, "technical_level": level,
             "guidance": list(guidance), "interview_status": "done",
             "updated_at": "2026-07-19T00:00:00Z", "source": "test"})

    def _pinned(self, text="anything at all"):
        return inject.gather(text)["pinned"]

    def _alarm(self, lines):
        return next((l for l in lines if l.startswith("[helm pinned]")), None)

    def test_a_dropped_rule_is_named_and_counted(self):
        for i in range(9):
            self.plant_pinned("pin-%d" % i, ("truth %d " % i) + "x" * 240)
        lines = self._pinned()
        alarm = self._alarm(lines)
        self.assertIsNotNone(alarm, "rules were dropped and nothing said so")
        rules = [l for l in lines if not l.startswith("[helm pinned]")
                 and not l.startswith("WHO ")]
        dropped_n = 9 - len(rules)
        self.assertGreater(dropped_n, 0, "the fixture must actually overflow")
        # EXACT count, EXACT typed ids, EXACT tail — "any pin-*" passed on a
        # single bare slug and proved nothing about identity or arithmetic
        self.assertIn("%d always-on rule(s) did NOT fit" % dropped_n, alarm)
        named = min(dropped_n, inject.PINNED_DROP_NAMES)
        for e in store.pinned()[len(rules):len(rules) + named]:
            self.assertIn(inject.store_typed_id(e), alarm,
                          "the alarm must name the TYPED id, never the slug")
        if dropped_n > inject.PINNED_DROP_NAMES:
            self.assertIn("(+%d more)" % (dropped_n - inject.PINNED_DROP_NAMES),
                          alarm)
        else:
            self.assertNotIn("more)", alarm)

    def test_a_lane_that_fits_says_nothing(self):
        """MUST-MISS, the not-overzealous half: an alarm on every turn is how a
        reader learns to skip the one that matters."""
        self.plant_pinned("pin-small", "short truth")
        lines = self._pinned()
        self.assertTrue(lines, "the pinned lane must still fire")
        self.assertIsNone(self._alarm(lines),
                          "nothing was dropped, so nothing may be announced")

    def test_the_alarm_is_not_charged_to_the_budget(self):
        """A full lane must not be able to silence its own alarm. The marker is
        a diagnostic ABOUT the budget, not a rule competing for it."""
        for i in range(9):
            self.plant_pinned("pin-%d" % i, ("truth %d " % i) + "x" * 240)
        lines = self._pinned()
        alarm = self._alarm(lines)
        self.assertIsNotNone(alarm)
        # ASSERT ON THE CHARGE, NOT ON THE RENDERED TEXT. Two earlier versions
        # of this arm read the rendered lines and were both VACUOUS: charging
        # the alarm does not change what is rendered, so a mutation that spent
        # 200 bytes of budget on the alarm left every rendered-text assertion
        # green. Mutation-proven, twice — the second time is why this reads the
        # admission record's own `used`.
        from helm import store as _store
        rec = inject.pinned_admission(_store.pinned(), _store.load_all())
        rule_bytes = sum(len(l) for l in rec["lines"])
        self.assertEqual(rec["used"], rule_bytes,
                         "`used` must count the RULES and nothing else — an "
                         "alarm inside the charge lets a full lane silence "
                         "its own alarm")
        self.assertLessEqual(rec["used"], inject.PINNED_BUDGET)
        # and the alarm really is outside that charge
        self.assertNotIn(alarm, rec["lines"])

    def test_one_line_however_many_drop_and_the_cap_is_exact(self):
        """An alarm that scales with the fault teaches readers to skip it — and
        the CAP must be the constant, with the remainder COUNTED not dropped."""
        for i in range(20):
            self.plant_pinned("pin-%d" % i, ("truth %d " % i) + "x" * 240)
        lines = self._pinned()
        alarms = [l for l in lines if l.startswith("[helm pinned]")]
        self.assertEqual(len(alarms), 1)
        rules = [l for l in lines if not l.startswith("[helm pinned]")
                 and not l.startswith("WHO ")]
        dropped_n = 20 - len(rules)
        self.assertGreater(dropped_n, inject.PINNED_DROP_NAMES,
                           "the fixture must overflow the NAME cap, not just "
                           "the byte budget")
        self.assertEqual(inject.PINNED_DROP_NAMES, 6)
        # exactly PINNED_DROP_NAMES ids named, and the remainder counted
        named = [tok for tok in alarms[0].split(": ", 1)[1].split(" — ")[0]
                 .split(" (+")[0].split(", ") if tok]
        self.assertEqual(len(named), inject.PINNED_DROP_NAMES)
        self.assertIn("(+%d more)" % (dropped_n - inject.PINNED_DROP_NAMES),
                      alarms[0])

    def test_who_is_charged_to_its_own_cap_and_walks_last(self):
        """WHO used to take its bytes off the top of the rules' budget AND walk
        first, so a digest ABOUT the operator outranked every rule FROM him —
        which is how four owner premises went dark while the arithmetic said
        they fit (integrator ruling 2026-08-28)."""
        self.plant_profile()         # UNCONDITIONAL: without this the WHO
        self.plant_pinned("pin-a", "a short owner rule")   # asserts silently skip
        lines = self._pinned()
        who = [l for l in lines if l.startswith("WHO ")]
        self.assertTrue(who, "the profile must produce a WHO digest")
        self.assertTrue(lines[-1].startswith("WHO "),
                        "WHO must walk after the rules")
        self.assertLessEqual(sum(len(l) for l in who), inject.WHO_CAP)
        rules = [l for l in lines if not l.startswith("WHO ")
                 and not l.startswith("[helm pinned]")]
        self.assertTrue(rules, "rules must render")
        self.assertTrue(lines[0] == rules[0], "a rule must come first")

    def test_the_budget_is_the_measured_render_not_a_guess(self):
        """PINNED_BUDGET was 1200 while the eight ratified rules RENDER at 1339
        — gloss plus a 33-43B `PREMISE <id>: ` prefix per line. Two authors'
        arithmetic said they fit; the instrument said otherwise, twice."""
        # MEASURED, not transcribed: render the specimen and require the
        # budget to clear its actual total. Hardcoding 1339/1540 pins two
        # numbers that stop being related the moment either moves.
        #
        # THE SPECIMEN MUST BE LIVE-SIZED. The first cut planted
        # "specimen rule 0" -- about 15 bytes -- so `rendered` came to a few
        # hundred and `PINNED_BUDGET >= rendered` held against ANY budget
        # including the 1200 this lane exists to correct. An arm that cannot
        # fail for the reason it was written is not an arm. The ratified
        # glosses run 120-140B of statement plus a 33-43B `PREMISE <id>: `
        # prefix, which is what produces the measured 1339 over eight rules.
        for i in range(8):
            self.plant_pinned("spec-%d" % i,
                              ("specimen rule %d, sized like a ratified "
                               "gloss so the render total lands in the same "
                               "range as the live estate " % i) + "s" * 50)
        rendered = sum(len(inject._entry_line(e)) for e in store.pinned())
        # BOUNDED BOTH WAYS, not just above 1000. A `> 1000` floor lets the
        # specimen drift to ~1050, where 1.10x is 1155 and the 1200 this lane
        # exists to correct would PASS the headroom assertion below. I had
        # verified 1200-fails for THIS specimen by hand and left the arm free
        # to stop being that specimen — a control that lives in my probe and
        # not in the test is not a control.
        self.assertGreater(rendered, 1250,
                           "specimen too small (%dB): it must sit near the "
                           "ratified 1339B render" % rendered)
        self.assertLess(rendered, 1500,
                        "specimen too large (%dB): it must sit near the "
                        "ratified 1339B render" % rendered)
        # THE DISCRIMINATOR, asserted rather than hand-checked: at this size
        # the pre-amendment 1200 CANNOT satisfy the headroom law below.
        self.assertLess(1200, int(rendered * 1.10),
                        "the specimen must be sized so the pre-amendment 1200 "
                        "fails the headroom assertion — otherwise this arm "
                        "passes against the very budget it exists to reject")
        self.assertGreaterEqual(
            inject.PINNED_BUDGET, rendered,
            "the budget must clear the rendered specimen (%dB)" % rendered)
        # THE RATIFIED DERIVATION (row 64f555da38e6): the budget is the
        # measured render PLUS headroom, not a number that merely happens to
        # clear it. 1200 would fail this against a live-sized specimen, which
        # is the whole point of the amendment.
        self.assertGreaterEqual(
            inject.PINNED_BUDGET, int(rendered * 1.10),
            "the budget carries the rendered lane with real headroom "
            "(rendered %dB, budget %dB)" % (rendered, inject.PINNED_BUDGET))
        # and the specimen really is at the boundary: one byte under its own
        # render total must starve a rule and fire the alarm.
        with mock.patch.object(inject, "PINNED_BUDGET", rendered - 1):
            squeezed = inject.gather("anything at all")["pinned"]
        self.assertTrue(any(l.startswith("[helm pinned]") for l in squeezed),
                        "a budget under the render total must announce a drop")


class TheAlarmNamesTypedIdsBecauseSlugsCollide(InjectBase):
    """A dropped rule must be named by its TYPED id, never its bare slug.

    The ambiguity is not inside the pinned lane — store.pinned() carries at
    most one entry per slug. It is in the REMEDIATION the alarm prints:
    `helm store gloss <id> --set TEXT` resolves across EVERY type, so a bare
    slug sends the reader to a verb that cannot tell prior:twin from
    heuristic:twin. An alarm whose fix-it command is ambiguous is an alarm
    that cannot be acted on. A review asked for this arm by name.
    """

    def test_a_dropped_rule_is_named_prior_twin_not_twin(self):
        # the collision: one slug, two types — both real, both resolvable
        # conf 0.5 puts twin LAST in the walk order (-confidence, -recency,
        # id asc) whatever the ids around it, so it is DETERMINISTICALLY the
        # dropped one. The first cut left it at 1.0 and guarded the assertion
        # with `if "twin" in alarm:` -- a conditional that skips itself the
        # moment the walk order changes, which is a vacuous arm wearing a
        # check's clothes.
        store.write_prior({"id": "twin", "statement": "prior twin " + "y" * 300,
                           "confidence": "0.5", "pin": "true"})
        store.write_heuristic({"id": "twin",
                               "statement": "heuristic twin " + "z" * 80,
                               "confidence": "1.0"})
        allrows = store.load_all()
        collide = sorted(inject.store_typed_id(e) for e in allrows
                         if str(e.get("id")) == "twin")
        # MUST-HIT: the collision is real, so the typed form is load-bearing
        self.assertEqual(collide, ["heuristic:twin", "prior:twin"])
        # THE ORDERING CONTROL, now IN the arm. A previous commit message
        # claimed this control existed; it only ever existed in a probe I ran
        # by hand, which is a claim about verification that the artifact did
        # not carry. Both directions, so the conf-0.5 placement below is a
        # property and not a coincidence of these ids.
        def _row(i, conf):
            return {"id": i, "confidence": float(conf), "load_class": "always",
                    "statement": "s", "stated_ts": "2026-08-28T00:00:00Z"}
        low = [_row("big-%d" % i, 1.0) for i in range(3)] + [_row("twin", 0.5)]
        self.assertEqual([r["id"] for r in store.pinned(entries=low)][-1],
                         "twin", "conf 0.5 must sort LAST — the fixture's "
                                 "determinism rests on it")
        same = [_row("big-%d" % i, 1.0) for i in range(3)] + [_row("aaa", 1.0)]
        self.assertEqual([r["id"] for r in store.pinned(entries=same)][0],
                         "aaa", "at EQUAL confidence an early id sorts FIRST "
                                "and would never be dropped — which is why "
                                "the old conf-1.0 fixture was order-dependent")
        # overflow so `twin` is dropped and named by the alarm
        for i in range(9):
            self.plant_pinned("big-%d" % i, ("t%d " % i) + "x" * 300)
        lines = inject.gather("anything at all")["pinned"]
        alarm = next((l for l in lines if l.startswith("[helm pinned]")), None)
        self.assertIsNotNone(alarm, "the fixture must overflow")
        self.assertIn("prior:twin", alarm,
                      "a dropped rule must carry its TYPE — the alarm's own "
                      "remediation verb resolves across types")
        self.assertNotIn(" twin,", alarm)     # never the bare slug
        self.assertFalse(alarm.rstrip().endswith(" twin"), alarm)
        # and NO id in the alarm may be bare
        named = alarm.split(": ", 1)[1].split(" — ")[0].split(" (+")[0]
        for tok in [t.strip() for t in named.split(",") if t.strip()]:
            self.assertIn(":", tok, "bare slug %r in the alarm" % tok)



class NoticeTurnTest(InjectBase):
    """task/2972: the JIT keyword match and the prompt reflex regexes read a
    notice's CONTENT, never its fixed envelope.

    MEASURED over 1,923 inject turns (41h) joined to their prompts: 54%
    were chat wakes delivered as Monitor events, 26% background-task notices,
    17% typed, and 80% of JIT fires matched machine-generated text. An entry
    keyed on a word the harness writes into every envelope ("benign", the
    Monitor's own description) fired on every wake whatever the wake said.

    The prompts are tests/_notices.py: the harness's real envelope shape,
    invented content."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def jit(self, text, **kw):
        return inject.gather(text, **kw)["jit"]

    def fires(self, text, pid):
        return any((" %s:" % pid) in l for l in self.jit(text))

    def test_an_entry_keyed_on_envelope_words_never_fires_on_a_notice(self):  # noqa: VACUOUS_ASSERTION — the unconditional typed control asserts jit-benign PRESENT through the same gather observable (fires() reads jit())
        from tests import _notices as N
        self.plant_jit("jit-benign", "a fact about benign output", "benign")
        self.plant_jit("jit-beacon", "a fact about inbox beacons", "beacon")
        self.plant_jit("jit-suite", "a fact about the suite", "suite")
        # UNCONDITIONAL CONTROL on the same observable: typed, the word fires.
        self.assertTrue(self.fires("is that output benign?", "jit-benign"))
        for pid, typed in (("jit-benign", "is that output benign?"),
                           ("jit-beacon", "re-arm the beacon"),
                           ("jit-suite", "run the suite")):
            self.assertTrue(self.fires(typed, pid), pid)
        for label, text in (("chat wake", N.wake("the cap is ready")),
                            ("agent done", N.agent_done("green",
                                                        desc="suite review")),
                            ("command done", N.command_done("run the suite"))):
            with self.subTest(kind=label):
                self.assertEqual(self.jit(text), [])

    def test_the_content_of_a_notice_still_matches(self):
        """A peer's wake is matched on its content. The seat's OWN background
        results (an agent's report, its Monitor's lines) carry routes only
        since trigger design lane 2: 0 of 55 task-notice pairs were relevant
        in the E2 gold (tests/test_inject_arrival.py)."""
        from tests import _notices as N
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        self.assertTrue(self.fires(N.wake("the fluxcap rewrite is ready"),
                                   "jit-a"))
        self.assertTrue(self.fires(N.handback("tuned the fluxcap"), "jit-a"))
        self.assertFalse(self.fires(N.agent_done("tuned the fluxcap"), "jit-a"))
        self.assertFalse(self.fires(
            N.monitor("gate: fluxcap 12 OK", desc="gate watcher"), "jit-a"))

    def test_a_prompt_reflex_reads_the_same_substance(self):
        from tests import _notices as N
        from helm import reflex
        reflex.write({"id": "benign-words", "steer": "fixture steer",
                      "signal": "prompt", "pattern": r"\bbenign\b"})
        line = "REFLEX: fixture steer"
        self.assertIn(line, inject.gather("is it benign?")["reflex"])  # control
        self.assertNotIn(line, inject.gather(N.wake("the cap is ready"))["reflex"])
        self.assertIn(line, inject.gather(N.wake("the drift is benign"))["reflex"])

    def test_a_typed_prompt_is_matched_whole(self):
        """THE CONTROL: a typed prompt that quotes an envelope is typed, and
        every word in it — wrapper words included — still matches."""
        from tests import _notices as N
        self.plant_jit("jit-benign", "a fact about benign output", "benign")
        self.assertTrue(self.fires("why did this fire? " + N.wake("x"),
                                   "jit-benign"))

    def test_the_ledger_row_marks_a_notice_turn(self):  # noqa: VACUOUS_ASSERTION — the notice row's field is asserted IS True, which an absent field fails, on the same two-row read the typed absence uses
        from tests import _notices as N
        inject.gather("typed words")
        inject.gather(N.wake("the cap is ready"))
        typed, notice = list(inject._ledger_rows())
        self.assertNotIn("notice", typed)
        self.assertIs(notice.get("notice"), True)

    def test_explain_matches_what_gather_matches(self):  # noqa: VACUOUS_ASSERTION — the typed --explain run asserts the entry PRESENT first, on the same verb and store
        from tests import _notices as N
        self.plant_jit("jit-benign", "a fact about benign output", "benign")
        rc, out, _ = self.run_inject(["--explain"], stdin_text="is it benign?")
        self.assertEqual(rc, 0)
        self.assertIn("jit-benign", out)                    # control
        rc, out, _ = self.run_inject(["--explain"],
                                     stdin_text=N.wake("the cap is ready"))
        self.assertEqual(rc, 0)
        self.assertNotIn("jit-benign", out)

    def test_coinage_still_reads_the_raw_envelope(self):  # noqa: VACUOUS_ASSERTION — the recorded argument is asserted EQUAL to the non-empty raw prompt, which a missed call or a stripped body fails
        """The coinage recorder reads the RAW prompt, never a stripped body
        that would count an agent's quoted phrases as the owner coining
        terms; and since trigger design lane 2 it runs on typed turns only,
        so a machine-begun turn never reaches it at all."""
        from tests import _notices as N
        text = 'why did this fire? ' + N.agent_done('the "fluxcap drift" is back')
        with mock.patch.object(inject, "_coinage", return_value=None) as c:
            inject.gather(text)
        self.assertEqual(c.call_args[0][0], text)
        with mock.patch.object(inject, "_coinage", return_value=None) as c:
            inject.gather(N.agent_done('the "fluxcap drift" is back'))
        self.assertIsNone(c.call_args)


class NoticeRemainderTest(InjectBase):
    """task/2978: a notice whose remainder has no substance injects no JIT
    entry. MEASURED on the E2 replay: 13.2% of JIT bytes landed on turns
    with no substance, and the fixed result sentences ("This agent's report
    was delivered ...") carried the words that fired them."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def fires(self, text, pid, **kw):
        return any((" %s:" % pid) in l for l in inject.gather(text, **kw)["jit"])

    def test_the_fixed_result_sentences_fire_nothing(self):  # noqa: VACUOUS_ASSERTION — the typed control asserts jit-delivered PRESENT through the same gather observable first
        from tests import _notices as N
        self.plant_jit("jit-delivered", "a fact about delivered reports",
                       "delivered, subagenthandback")
        self.assertTrue(self.fires("was the report delivered?", "jit-delivered"))
        for text in (N.agent_done(N.DELIVERED), N.agent_done(N.NOT_YET),
                     N.monitor(N.SUPPRESSED)):
            with self.subTest(text=text[-80:]):
                self.assertEqual(inject.gather(text)["jit"], [])

    def test_a_handback_matches_its_report_never_its_frame(self):
        from tests import _notices as N
        self.plant_jit("jit-frame", "a fact about model output", "authority")
        self.plant_jit("jit-report", "a fact about the fluxcap", "fluxcap")
        text = N.handback("the fluxcap moved to six")
        self.assertTrue(self.fires(text, "jit-report"))
        self.assertFalse(self.fires(text, "jit-frame"))


class NoticeRouteTest(InjectBase):
    """task/2978: a Monitor expiry is the fleet's check-in tick; its rule
    (inbox-beacon-can-die-rearm) was relevant on 2 of 2 expiry turns in the
    E2 gold. The expiry line is fixed text, so its substance is empty and no
    keyword can match it. The route is by the notice's KIND: an entry
    declares `notice:monitor-expired` among its keywords, and that cell is a
    route, never a word probe."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def jit_ids(self, text, **kw):
        return [l.split(":", 1)[0].split(" ")[-1]
                for l in inject.gather(text, **kw)["jit"]]

    def test_an_expiry_surfaces_the_declared_entry_only(self):
        from tests import _notices as N
        self.plant_jit("beacon-rule", "re-arm on every expiry",
                       "beacon, notice:monitor-expired")
        self.plant_jit("expiry-words", "a fact about monitors",
                       "monitor expired, re-arm, expired")
        got = self.jit_ids(N.monitor(N.EXPIRED))
        self.assertEqual(got, ["beacon-rule"])

    def test_a_route_cell_is_never_a_word_probe(self):
        self.plant_jit("beacon-rule", "re-arm on every expiry",
                       "beacon, notice:monitor-expired")
        self.assertEqual(self.jit_ids("is the beacon up?"), ["beacon-rule"])
        self.assertEqual(self.jit_ids("see notice:monitor-expired please"), [])

    def test_the_route_is_scope_fenced(self):
        from tests import _notices as N
        store.write_prior({"id": "other-beacon", "statement": "another project's rule",
                           "confidence": "0.8", "project": "other-project",
                           "keywords": "notice:monitor-expired"})
        self.plant_jit("fleet-beacon", "re-arm on every expiry",
                       "notice:monitor-expired")
        self.assertEqual(sorted(self.jit_ids(N.monitor(N.EXPIRED),
                                             project="helm")),
                         ["fleet-beacon"])
        self.assertIn("other-beacon",
                      self.jit_ids(N.monitor(N.EXPIRED),
                                   project="other-project"))

    def test_a_routed_entry_cools_like_any_delivery(self):  # noqa: VACUOUS_ASSERTION — the first call on the same session asserts the entry PRESENT through the same jit_ids observable, and a fresh session asserts it again
        from tests import _notices as N
        self.plant_jit("beacon-rule", "re-arm on every expiry",
                       "beacon, notice:monitor-expired")
        first = self.jit_ids(N.monitor(N.EXPIRED), session="s-route")
        second = self.jit_ids(N.monitor(N.EXPIRED), session="s-route")
        self.assertEqual(first, ["beacon-rule"])
        self.assertEqual(second, [])
        self.assertEqual(self.jit_ids(N.monitor(N.EXPIRED), session="s-other"),
                         ["beacon-rule"])

    def test_explain_names_the_route(self):
        from tests import _notices as N
        self.plant_jit("beacon-rule", "re-arm on every expiry",
                       "beacon, notice:monitor-expired")
        rc, out, _ = self.run_inject(["--explain"],
                                     stdin_text=N.monitor(N.EXPIRED))
        self.assertEqual(rc, 0)
        self.assertIn("beacon-rule", out)
        self.assertIn("route: arrival.monitor-expired", out)


class PromptCensusTurnTest(InjectBase):
    """task/2978: the prompt census is recorded by the turn that matches, from
    the SAME substance the keyword lane reads, so a word the harness writes
    into every envelope never becomes 'common' by being in the envelope."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_gather_records_the_substance(self):
        from helm import promptcensus
        from tests import _notices as N
        inject.gather(N.wake("the fluxcap rewrite is ready"))
        inject.gather("typed words about the fluxcap")
        got = promptcensus.load()
        self.assertEqual(got.turns, 2)
        self.assertEqual(got.df.get("fluxcap"), 2)
        for fixed in ("pushnotification", "benign", "monitor", "beacon"):
            with self.subTest(word=fixed):
                self.assertNotIn(fixed, got.df)

    def test_a_turn_with_no_substance_is_not_counted(self):
        from helm import promptcensus
        from tests import _notices as N
        inject.gather("one typed turn")
        inject.gather(N.agent_done(N.DELIVERED))
        self.assertEqual(promptcensus.load().turns, 1)

    def test_explain_records_nothing(self):  # noqa: VACUOUS_ASSERTION — test_gather_records_the_substance is the positive twin on the same census file
        from helm import promptcensus
        rc, _out, _ = self.run_inject(["--explain"], stdin_text="the fluxcap")
        self.assertEqual(rc, 0)
        self.assertEqual(promptcensus.load().turns, 0)


class SeatWithoutProjectTest(InjectBase):
    """task/2978: 23 of 4,333 ledger rows in the E2 window came from a cwd no
    project claims (a scratch dir, /tmp, the home dir), and on those turns
    every project's entries fired. The owner's law: a seat receives fleet
    entries plus entries of its own project, so a seat in NO project
    receives fleet entries only."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_no_project_gets_fleet_only(self):
        store.write_prior({"id": "helm-only", "statement": "a helm rule",
                           "confidence": "0.8", "project": "helm",
                           "keywords": "flumpet"})
        store.write_prior({"id": "fleet-rule", "statement": "a fleet rule",
                           "confidence": "0.8", "project": "fleet",
                           "keywords": "flumpet"})
        jit = inject.gather("the flumpet broke", project=None)["jit"]
        self.assertTrue(any(" fleet-rule:" in l for l in jit))
        self.assertFalse(any(" helm-only:" in l for l in jit))
        jit = inject.gather("the flumpet broke", project="helm")["jit"]
        self.assertTrue(any(" helm-only:" in l for l in jit))   # its project
