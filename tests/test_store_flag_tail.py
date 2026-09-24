#!/usr/bin/env python3
"""`helm store` verbs refuse a flag they do not parse, instead of eating it.

THE DEFECT (task/1933, measured 2026-09-09 against the live store). `revise`
built its statement as `[a for a in rest if not a.startswith("--")]`. An
unknown flag was therefore SILENTLY DROPPED and its orphaned VALUE SILENTLY
JOINED THE PROSE:

    helm store revise <id> "CORRECTED" --source SRC --rationale RAT
    -> pending_revision.statement == "CORRECTED SRC RAT"

No error, no warning. A canon entry was corrupted this way and then confirmed
live, so the polluted rule fired at the whole fleet. `add` had a correct guard
of its own (`_add_args`) the entire time — the store had one guarded verb and
sixteen that inherited nothing from it.

WHY THE ARMS LOOK THE WAY THEY DO. Asserting only `rc == 2` would pass under a
mutation that refuses for an unrelated reason at an earlier gate, so the
incident arm asserts THE ABSORBED VALUE IS ABSENT from the stored statement —
the observable the operator actually lost. And the parity arm exists because
the dangerous failure of a flag TABLE is not a missed refusal but a FALSE one:
a verb that grows a flag nobody adds to the table starts refusing legitimate
calls for every seat. That arm reads the branches, not this file's prose.

Hermetic: HELM_HOME and friends are tmp dirs; no arm reads the live store.
"""
import ast
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import store  # noqa: E402
from helm.store import cli as store_cli  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR",
            "MELD_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME")

TS = "2026-09-09T12:00:00Z"
EID = "a-synthetic-entry-for-flag-tail-arms"


def run_store(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


class FlagTailBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-flagtail-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for var, leaf in (("HELM_HOME", "helm"),
                          ("HELM_ADOPTED_DIR", "adopted"),
                          ("HELM_CACHE_DIR", "cache"),
                          ("HELM_CHAT_DIR", "chat")):
            os.environ[var] = os.path.join(self.tmp, leaf)
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        # 0.60, NOT certain: `evidence` deliberately refuses to move a
        # certain prior's confidence (it logs the contradiction as drift
        # instead), so a 1.0 fixture would make the signed-delta arm's
        # positive control unfalsifiable — it would read "the verb ran" off
        # a number the verb is documented never to change.
        store.write_prior({"id": EID, "statement": "AN ORIGINAL STATEMENT",
                           "confidence": "0.60", "keywords": "alpha,beta",
                           "stated_ts": TS, "source": "human"})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self):
        hits = [e for e in store.load_all() if e["id"] == EID]
        self.assertEqual(len(hits), 1, EID)
        return hits[0]


class TheIncidentTest(FlagTailBase):
    """The exact live invocation that corrupted a canon entry."""

    def test_the_incident_command_REFUSES_because_revise_writes_canon(self):
        # THE DEFECT: `revise` filtered every --token out of argv, so the flag
        # VANISHED and its orphaned VALUE joined the prose — the staged
        # statement became "CORRECTED SRCVALUE RATVALUE" and nothing said so.
        # A revise tail is a CANON STATEMENT, so accepting the tokens as
        # ordinary text would leave the same corruption reachable, merely
        # visible instead of silent (a review superseding one clause of its
        # own meld ruling). GUARDED tails refuse until `--` is given.
        rc, _out, err = run_store(
            ["revise", EID, "CORRECTED", "--source", "SRCVALUE",
             "--project", "p"])
        self.assertEqual(rc, 2, err)
        self.assertIn("--source", err)
        self.assertIsNone(self.entry().get("pending_revision"))
        self.assertEqual(self.entry()["statement"], "AN ORIGINAL STATEMENT")

    def test_a_guarded_tail_takes_literal_text_AFTER_the_escape(self):
        # ...and the escape is the expression path, which is why the refusal
        # above is not a dead end.
        rc, _out, err = run_store(
            ["revise", EID, "--", "CORRECTED", "--source", "SRCVALUE",
             "--project", "p"])
        self.assertEqual(rc, 0, err)
        staged = str((self.entry().get("pending_revision") or {})
                     .get("statement") or "")
        self.assertIn("--source", staged)
        self.assertIn("SRCVALUE", staged)

    def test_a_legitimate_revise_is_untouched(self):
        rc, out, err = run_store(["revise", EID, "A CLEAN CORRECTION",
                                  "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertIn("STAGED", out)
        staged = self.entry().get("pending_revision") or {}
        self.assertEqual(staged.get("statement"), "A CLEAN CORRECTION")

    def test_a_recognized_flag_is_still_a_flag_in_the_tail(self):
        rc, _out, err = run_store(["revise", EID, "A STATEMENT",
                                   "--type", "prior", "--project", "p"])
        self.assertEqual(rc, 0, err)
        staged = str((self.entry().get("pending_revision") or {})
                     .get("statement") or "")
        self.assertEqual(staged, "A STATEMENT")
        self.assertNotIn("--type", staged)


class TheGuardRefusesOnlyWhereAFlagIsMeantTest(FlagTailBase):
    def test_an_unknown_flag_on_a_TAILLESS_verb_refuses(self):
        # `keywords` has no free-text tail, so a stray --token can only be a
        # flag it does not take. THIS is where refusing is right.
        rc, _out, err = run_store(
            ["keywords", EID, "--bogus", "--project", "p"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)
        self.assertIn("--add", err)            # what it DOES take

    def test_a_flag_shaped_VALUE_is_not_a_flag(self):
        # ROUND-1 REGRESSION: `--set` takes whatever follows as its
        # payload, so `--force` here is a VALUE. Works on main; a name-only
        # scan refused it.
        rc, _out, err = run_store(
            ["keywords", EID, "--set", "--force", "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertIn("--force", self.entry()["keywords"])

    def test_a_dash_leading_token_in_a_free_text_TAIL_is_prose(self):
        # ROUND-2 REGRESSION: `evidence <ts> <id> <delta>
        # <reason...>` stores "--because" as reason text on main; the arity
        # walk refused it because it modelled flags but not positionals.
        before = float(self.entry()["confidence"])
        rc, _out, err = run_store(
            ["evidence", TS, EID, "-0.10", "--because", "it", "drifted",
             "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertLess(float(self.entry()["confidence"]), before)
        log = self.entry()["evidence_log"][-1]
        self.assertIn("--because", log["reason"])

    def test_a_SIGNED_delta_is_not_a_flag(self):
        before = float(self.entry()["confidence"])
        rc, _out, err = run_store(
            ["evidence", TS, EID, "-0.15", "a", "reason", "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertLess(float(self.entry()["confidence"]), before)

    def test_a_duplicate_flag_REFUSES_rather_than_guessing(self):
        rc, _out, err = run_store(
            ["revise", EID, "S", "--type", "prior", "--type", "lexicon",
             "--project", "p"])
        self.assertEqual(rc, 2)
        self.assertIn("only once", err)

    def test_a_valued_flag_with_nothing_after_it_REFUSES(self):
        rc, _out, err = run_store(["keywords", EID, "--add", "--project", "p"])
        self.assertEqual(rc, 2)
        self.assertIn("needs a value", err)

    def test_the_ATTACHED_spelling_is_parsed_not_ignored(self):
        # `--flag=value` used to pass the name check and then be dropped by
        # the branch, so `list --type=prior` silently listed everything.
        #
        # TWO FIXTURES OF DIFFERENT TYPES, because with only the prior in the
        # store "the filter works" and "the flag was ignored, list everything"
        # RENDER IDENTICALLY — an oracle that cannot disagree is decoration.
        # The heuristic must be absent from the filtered listing
        # and present in the unfiltered one, or this arm proves nothing.
        store.write_heuristic({"id": "a-synthetic-heuristic-for-the-filter",
                               "move": "A MOVE", "keywords": "gamma",
                               "stated_ts": TS, "source": "human"})
        rc, out, err = run_store(["list", "--type=prior", "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertIn(EID, out)                       # the prior IS listed
        self.assertNotIn("a-synthetic-heuristic-for-the-filter", out)
        self.assertNotIn("--type=prior", out)
        # POSITIVE CONTROL: unfiltered, the heuristic DOES appear — so the
        # absence above is the filter and not a fixture that never landed.
        rc, allout, err = run_store(["list", "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertIn("a-synthetic-heuristic-for-the-filter", allout)

    def test_a_TAILLESS_verb_never_discards_an_extra_token(self):
        # `get` has no free-text tail, so a second token cannot be prose. It
        # used to land in an unread bucket and vanish: `get foo bar` resolved
        # "foo" and DROPPED "bar", where main joined both into the id. The
        # silent drop this lane exists to close, one layer down in its own
        # parser (a FIX on the previous tip).
        ns, err = store_cli.parse_argv("get", ["foo", "bar"])
        self.assertIsNone(err)
        self.assertEqual(ns.pos, ["foo", "bar"])
        self.assertEqual(ns.tail, "")

    def test_a_delimiter_inside_a_rest_tail_stops_flag_lifting(self):
        # `--edit` takes the remaining argv; a `--` inside it makes everything
        # after LITERAL, so a sibling flag is no longer hoisted out of the
        # operator's own text — and the delimiter is consumed, not stored.
        ns, err = store_cli.parse_argv(
            "confirm", ["an-id", "--edit", "replacement", "--", "--type",
                        "literal"])
        self.assertIsNone(err)
        self.assertEqual(ns.get("--edit"), "replacement --type literal")
        self.assertIsNone(ns.get("--type"))
        # POSITIVE CONTROL: WITHOUT the delimiter, lifting still happens —
        # which is what keeps `gloss --set x --clear` refusing.
        ns2, err2 = store_cli.parse_argv(
            "confirm", ["an-id", "--edit", "replacement", "--type", "prior"])
        self.assertIsNone(err2)
        self.assertEqual(ns2.get("--type"), "prior")

    def test_the_literal_escape_reaches_the_tail(self):
        rc, _out, err = run_store(
            ["revise", EID, "--", "--looks-like-a-flag", "--project", "p"])
        self.assertEqual(rc, 0, err)
        staged = str((self.entry().get("pending_revision") or {})
                     .get("statement") or "")
        self.assertIn("--looks-like-a-flag", staged)
        self.assertNotIn("--project", staged)      # the separator ends flags
        # ...and the separator is CONSUMED: no bare `--` token survives into
        # the stored text (substring would match --looks-like-a-flag itself).
        self.assertNotIn("--", staged.split())


class ThereIsOnlyOneFlagConsumerTest(FlagTailBase):
    """A prose-consuming flag must not get its own, weaker flag parser.

    The rest-arity used to hand off to a NESTED scanner that re-implemented
    flag handling, so the two paths disagreed and the weaker one won inside a
    tail. A probe measured the worst case: `--force-new=false` SET FORCE TRUE
    there, turning a false-looking token into AUTHORIZATION, while the main
    path refuses `=` on a switch outright. These arms pin the three ways they
    diverged; the cure was deleting the second consumer, not patching it.
    """

    def test_a_switch_with_an_attached_value_REFUSES_inside_a_tail(self):
        # THE MAIN-VS-TIP ARM a review asked for. force-new is a SWITCH:
        # `--force-new=false` is not "false", it is a value on a flag that
        # takes none. Accepting it silently authorized.
        rc, _out, err = run_store(
            ["confirm", EID, "--edit", "corrected", "--force-new=false",
             "--project", "p"])
        self.assertEqual(rc, 2, err)
        self.assertIn("takes no value", err)
        # POSITIVE CONTROL, unconditional: the same flag WITHOUT a value is
        # accepted, so the refusal is about the `=` and not about the flag.
        ns, perr = store_cli.parse_argv(
            "confirm", [EID, "--edit", "corrected", "--force-new"])
        self.assertIsNone(perr)
        self.assertIs(ns.get("--force-new"), True)

    def test_a_duplicate_flag_REFUSES_inside_a_tail_too(self):
        _ns, err = store_cli.parse_argv(
            "confirm", ["an-id", "--edit", "one", "--edit", "two"])
        self.assertEqual(err, "--edit may appear only once")

    def test_an_EMPTY_attached_value_refuses(self):
        _ns, err = store_cli.parse_argv("revise", ["an-id", "--type=", "text"])
        self.assertIn("needs a value", err or "")
        # POSITIVE CONTROL: a non-empty attached value is accepted.
        ns, perr = store_cli.parse_argv("revise", ["an-id", "--type=prior", "t"])
        self.assertIsNone(perr)
        self.assertEqual(ns.get("--type"), "prior")

    def test_a_known_flag_is_still_consumed_after_a_rest_flag(self):
        # The property that keeps a do-not-combine refusal reachable: a
        # RECOGNISED flag stays a flag inside a tail. Only UNRECOGNISED dash
        # tokens are the operator's prose there. `confirm --edit` is the
        # remaining rest-arity verb, since `gloss` is exempt.
        ns, err = store_cli.parse_argv(
            "confirm", ["an-id", "--edit", "some text", "--force-new"])
        self.assertIsNone(err)
        self.assertIs(ns.get("--force-new"), True)
        self.assertEqual(ns.get("--edit"), "some text")


class GlossKeepsItsOwnParseTest(FlagTailBase):
    """`gloss` is EXEMPT, and this is the main-parity arm that says why.

    Its `--type` is read from ANYWHERE for its value, yet stripped from the
    `--set` payload ONLY when it is the immediate pair after it — the same
    token both consumed and kept. Routing that through the shared loop
    consumed `--type` anywhere and SILENTLY DELETED payload text (a
    FIX/MEASURED on the previous tip). Uniformity is not worth deleting an
    operator's words.
    """

    def setUp(self):
        super(GlossKeepsItsOwnParseTest, self).setUp()
        store.write_heuristic({"id": "a-synthetic-heuristic-for-gloss",
                               "move": "A MOVE", "keywords": "delta",
                               "stated_ts": TS, "source": "human"})

    def test_type_AFTER_the_payload_stays_in_the_text(self):
        # MAIN'S CONTRACT: `--set x --type prior` -> text "x --type prior".
        rc, _out, err = run_store(
            ["gloss", "a-synthetic-heuristic-for-gloss", "--set", "x",
             "--type", "heuristic", "--project", "p"])
        self.assertEqual(rc, 0, err)
        hits = [e for e in store.load_all()
                if e["id"] == "a-synthetic-heuristic-for-gloss"]
        self.assertEqual(hits[0].get("gloss"), "x --type heuristic")

    def test_type_as_the_IMMEDIATE_pair_is_stripped(self):
        # ...and the placement contract's other half, which is what makes the
        # arm above a CONTRACT rather than an accident.
        rc, _out, err = run_store(
            ["gloss", "a-synthetic-heuristic-for-gloss", "--set", "--type",
             "heuristic", "just the words", "--project", "p"])
        self.assertEqual(rc, 0, err)
        hits = [e for e in store.load_all()
                if e["id"] == "a-synthetic-heuristic-for-gloss"]
        self.assertEqual(hits[0].get("gloss"), "just the words")

    def test_an_unknown_flag_in_the_STRUCTURAL_region_refuses(self):
        # THE EXACT PROBE: this verb must not succeed, clear the gloss,
        # and DISCARD the unknown pair in silence. Exempting the verb was
        # right for its PAYLOAD and left this hole in its STRUCTURE.
        rc, _out, err = run_store(
            ["gloss", "a-synthetic-heuristic-for-gloss", "--clear", "--bogus",
             "value", "--project", "p"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)
        self.assertIn("--set", err)          # names what it DOES take
        # ...and it did not act: POSITIVE CONTROL that the refusal came
        # BEFORE the write, not after it.
        hits = [e for e in store.load_all()
                if e["id"] == "a-synthetic-heuristic-for-gloss"]
        self.assertEqual(hits[0].get("gloss"), "")

    def test_a_valueless_type_in_the_structural_region_refuses(self):
        rc, _out, err = run_store(
            ["gloss", "a-synthetic-heuristic-for-gloss", "--type",
             "--project", "p"])
        self.assertEqual(rc, 2)
        self.assertIn("--type needs a type", err)

    def test_the_guard_STOPS_at_the_payload(self):
        # A dash token AFTER --set is the operator's text and must survive the
        # guard untouched — otherwise the structural fix re-breaks placement,
        # which is the trade this whole round was about.
        rc, _out, err = run_store(
            ["gloss", "a-synthetic-heuristic-for-gloss", "--set", "x",
             "--bogus", "y", "--project", "p"])
        self.assertEqual(rc, 0, err)
        hits = [e for e in store.load_all()
                if e["id"] == "a-synthetic-heuristic-for-gloss"]
        self.assertEqual(hits[0].get("gloss"), "x --bogus y")

    def test_gloss_is_absent_from_the_grammar_on_purpose(self):
        self.assertNotIn("gloss", store_cli._GRAMMAR)
        self.assertNotIn("add", store_cli._GRAMMAR)
        # POSITIVE CONTROL: the grammar is populated, so the absences mean
        # EXEMPT rather than empty.
        self.assertIn("revise", store_cli._GRAMMAR)
        self.assertIn("confirm", store_cli._GRAMMAR)


class AddKeepsItsOwnGuardTest(FlagTailBase):
    """`add` is exempt from the grammar — prove the exemption is not a hole."""

    def test_add_still_refuses_an_unknown_option(self):
        rc, _out, err = run_store(
            ["add", "prior", "x | y | 0.6 | k | d", "--bogus", "--project", "p"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)

    def test_add_is_deliberately_absent_from_the_grammar(self):
        self.assertNotIn("add", store_cli._GRAMMAR)
        # POSITIVE CONTROL: the grammar is real, so the absence means exempt.
        self.assertIn("revise", store_cli._GRAMMAR)
        self.assertIn("--type", store_cli._GRAMMAR["revise"]["flags"])


class TheGrammarMatchesTheBranchesTest(unittest.TestCase):
    """The grammar is derived from the verbs; drift refuses REAL calls."""

    def _branch_flags(self):
        path = store_cli.__file__.replace(".pyc", ".py")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        starts = [(i, m.group(1)) for i, l in enumerate(lines)
                  if (m := re.match(r'    if cmd == "([^"]+)"', l))]
        self.assertGreater(len(starts), 10, "verb scan found nothing")
        starts.append((len(lines), None))
        return {verb: set(re.findall(r'"(--[a-z][a-z-]*)"',
                                     "\n".join(lines[a:b])))
                for (a, verb), (b, _) in zip(starts, starts[1:])}

    # EXEMPT BY IDENTITY AND BY NAME, never by a shape rule. Each verb here
    # owns a grammar the shared loop provably cannot reproduce, and each was
    # added only after a MEASURED regression proved it — so a future verb
    # cannot drift into the exemption by resembling one of them.
    EXEMPT = {
        "add": "_add_args owns its unquoted pipe grammar and already refuses "
               "unknown options",
        "gloss": "--type is read from anywhere for its value but stripped "
                 "from the --set payload only as the immediate pair; the "
                 "shared loop consumed it anywhere and deleted operator text",
    }

    def test_every_verb_except_the_named_exemptions_is_modelled(self):
        branch_verbs = set(self._branch_flags())
        self.assertIn("revise", branch_verbs)
        self.assertIn("add", branch_verbs)
        untabled = branch_verbs - set(store_cli._GRAMMAR) - set(self.EXEMPT)
        self.assertFalse(untabled,
                         "unmodelled verbs skip the parser: %s"
                         % sorted(untabled))
        # ...and an exemption must be REAL: a name listed here that no longer
        # exists as a verb is a stale licence for nothing.
        stale = set(self.EXEMPT) - branch_verbs
        self.assertFalse(stale, "exemption names a dead verb: %s"
                         % sorted(stale))

    def test_every_arity_is_one_the_parser_understands(self):
        shapes = {sh for g in store_cli._GRAMMAR.values()
                  for sh in g["flags"].values()}
        self.assertTrue(shapes)
        self.assertEqual(shapes - {"switch", "one", "rest"}, set(),
                         "an unknown arity falls through the parser silently")

    def test_every_modelled_verb_declares_its_positional_shape(self):
        for verb, spec in store_cli._GRAMMAR.items():
            self.assertIsInstance(spec["pos"], int, verb)
            self.assertIn(spec["tail"], (False, "literal", "guarded"), verb)
        # POSITIVE CONTROL on the two shapes the regressions turned on.
        self.assertEqual(store_cli._GRAMMAR["evidence"]["pos"], 3)
        self.assertEqual(store_cli._GRAMMAR["evidence"]["tail"], "literal")
        self.assertEqual(store_cli._GRAMMAR["revise"]["tail"], "guarded")
        self.assertFalse(store_cli._GRAMMAR["keywords"]["tail"])


if __name__ == "__main__":
    unittest.main()
