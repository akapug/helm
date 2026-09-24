"""Reference parity pins — the anti-regrowth half of the reference truing.

Each pin binds a reference surface (docs/HOOKS.md's hook-estate table, the
`helm <verb>` one-liners in cli.py, the lr close-reason grammar in cli.py +
docs/VERBS.md) to the code register it restates — the grep-pin idiom
tests/test_vcs.py established for ARCHITECTURE.md's git-spawn count: the
number/set the doc states must BE the measured one, so the next drift fails
a test instead of waiting for the next audit sweep.

Every sweep here carries a MUST-HIT positive control: a regex that rots to
zero matches must fail loudly, never pass vacuously (absence of a match is
not a finding).
"""
import os
import re
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import chat, cli, hooks, landreq  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS = os.path.join(_ROOT, "docs")
_WORDS = {2: "two", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
          11: "eleven", 12: "twelve", 13: "thirteen"}


def _doc(name):
    with open(os.path.join(_DOCS, name), encoding="utf-8") as f:
        return f.read()


def _sweep_verbs(relpath):
    """Every string literal a dispatcher compares its `verb` against —
    `verb == "x"` plus the `verb in ("x", "y")` grouped form."""
    with open(os.path.join(_ROOT, relpath), encoding="utf-8") as f:
        src = f.read()
    verbs = set(re.findall(r'\bverb == "([a-z][a-z0-9-]*)"', src))
    for group in re.findall(r'\bverb in \(([^)]*)\)', src):
        verbs |= set(re.findall(r'"([a-z][a-z0-9-]*)"', group))
    return verbs


def _one_liner_tokens(verb):
    """The word tokens of a one-liner's GRAMMAR half (before the first
    ` — ` description dash). Flag words ride along harmlessly — the pin only
    asks that every required subverb APPEARS, never that every token is one."""
    line = cli._VERB_HELP[verb]
    grammar = line.split(" — ", 1)[0]
    return set(re.findall(r"[a-z][a-z0-9-]*", grammar))


class HooksEstatePinTest(unittest.TestCase):
    """docs/HOOKS.md restates hooks.SPECS — count and rows, pinned."""

    def test_the_stated_estate_count_is_the_specs_length(self):
        # the number the doc states must BE the measured number (test_vcs.py's
        # law, verbatim): the stated count moves when SPECS does.
        #
        # THE COUNT IS PINNED, THE NOUN IS NOT. The first version matched the
        # literal "N entries per home", so a doc edit clarifying "per
        # CREDENTIAL home" — true, and the exact distinction the seat-parity
        # work made worth drawing — read as a broken pin. A parity test that
        # forbids improving the sentence around the number is guarding its own
        # phrasing rather than the fact; what must not drift is the NUMBER, so
        # that is what this matches.
        doc = _doc("HOOKS.md")
        stated = re.search(r"(\d+) entries per (?:credential )?home", doc)
        self.assertIsNotNone(
            stated, "the estate-count sentence moved out of HOOKS.md — the "
            "count is no longer stated anywhere this pin can read")
        self.assertEqual(int(stated.group(1)), len(hooks.SPECS),
                         "the doc states an estate size that SPECS does not")

    def test_the_estate_table_has_one_row_per_spec(self):
        text = _doc("HOOKS.md")
        lines = text.splitlines()
        heads = [i for i, ln in enumerate(lines)
                 if ln.replace(" ", "") == "|event|command|whatitcarries|"]
        self.assertEqual(len(heads), 1, "the estate table header moved — "
                         "re-anchor this pin on the new header")
        rows = []
        for ln in lines[heads[0] + 2:]:      # skip header + |---| separator
            if not ln.startswith("|"):
                break
            rows.append(ln)
        # MUST-HIT control first: an empty scrape is a broken pin, not a
        # zero-row estate.
        self.assertGreater(len(rows), 0, "table scrape found nothing")
        self.assertEqual(
            len(rows), len(hooks.SPECS),
            "the hook-estate table has %d rows but hooks.SPECS has %d "
            "entries — a spec was added/removed without its doc row "
            "(rewrite the docs/HOOKS.md table from SPECS)"
            % (len(rows), len(hooks.SPECS)))
        table = "\n".join(rows)
        for s in hooks.SPECS:
            self.assertIn("helm %s" % s["args"], table,
                          "spec %r has no table row naming its command"
                          % s["name"])
            self.assertIn("`%s`" % s["event"], table,
                          "spec %r: its event is not in the table" % s["name"])

    def test_the_gate_paragraph_names_every_gate_and_the_rc124_leg(self):
        text = _doc("HOOKS.md")
        gates = [s for s in hooks.SPECS if s.get("gate")]
        self.assertGreater(len(gates), 0, "SPECS lost its gates?")  # must-hit
        para = text[text.index("rows are GATES"):]
        para = para[:para.index("\n\n")]
        for s in gates:
            self.assertIn(s["name"], para,
                          "gate %r is not named in the GATES paragraph"
                          % s["name"])
        word = _WORDS[len(hooks.SPECS) - len(gates)]
        self.assertIn("%s other hooks" % word, para,
                      "the non-gate count moved — restate it")
        # THE QUOTED SHAPE MUST CARRY EVERY ARM. A quote that omits one
        # re-documents the bug that arm exists for: 124 is a `timeout` kill
        # that prints nothing, 127 is a helm path that no longer exists (the
        # 2026-08-04 outage — eight settings files pointed at a deleted lane
        # room and four homes ran unguarded in silence), and `*)` is the
        # default that keeps the NEXT unhandled code from being silent too.
        self.assertIn('2) exit 2', para)
        self.assertIn('124)', para)
        self.assertIn('127)', para)
        self.assertIn('*)', para)
        for arm, said in (("124", "TIMED OUT"), ("127", "IS MISSING"),
                          ("*", "FAILED")):
            self.assertIn(said, para,
                          "the rc-%s arm's message is not documented" % arm)
        # and the doc's arms are the CODE's arms — a leg added without touching
        # this paragraph fails here. THE ARMS ARE IN bin/helm-hook, not in the
        # generated command: the ladder moved out of the command string because
        # Claude Code echoes that string on every blocked stop, and a text
        # search over the command would now pass on a ladder that had lost an
        # arm entirely. The command is checked for the SHAPE that reaches the
        # script instead.
        import os as _os
        ladder = open(_os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "bin", hooks.HOOK_WRAPPER), encoding="utf-8").read()
        for arm in ("2)", "124)", "127)", "*)"):
            self.assertIn(arm, ladder,
                          "the doc pins an arm the shipped ladder lost")
        import shlex as _shlex
        self.assertEqual(_shlex.split(hooks.spec_command(gates[0]))[1], "gate",
                         "the gate is no longer rendered as one, so the arm "
                         "that propagates rc 2 is never reached")


class OneLinerParityPinTest(unittest.TestCase):
    """cli.py's per-dispatcher one-liners name every PUBLIC subverb the real
    dispatcher accepts. A deliberately internal/hook-facing verb lives in
    _INTERNAL below WITH its reason — adding a public subverb without touching
    the one-liner fails here."""

    # Verbs a dispatcher accepts that the global one-liner deliberately does
    # not advertise. Every entry needs a reason; a stale entry (verb gone from
    # the code) fails the sweep-consistency assertion. Spelled as ONE split
    # string, not quoted names: test_scratch's host-mutation tripwire reads a
    # literal quoted stop-guard-with-comma as a stop-hook INVOCATION (the
    # prose-as-call-site class, #128) — this register is data, so it must not
    # wear that shape.
    _INTERNAL = {
        "chat": set(
            "roster "           # alias of `seats`, normalized before dispatch
            "transport "        # transport plumbing probe, not an agent verb
            "verify "           # room-integrity probe; surfaced by chat's own
                                # usage, not the global table
            "restore-journal "  # disaster-recovery leg of log-flush
            "argv-guard "       # PreToolUse hook verb (hooks.SPECS), not typed
            "delegation-stop "  # SubagentStop hook verb (hooks.SPECS)
            "stop-guard "       # Stop hook verb (hooks.SPECS)
            "verdict "          # council blind-verdict internals — reached
            "reveal "           # through the meld/council preset, documented
            "council-status "   # with it in VERBS.md, not as chat verbs
            "council-abort".split()),
        "seat": set(
            "silent-drop "      # proxy-watchdog internals (silent_drop.py),
            "idle-dispatch "    # fired by monitors — not operator verbs
            "rebind".split()),  # spawn-register repair; surfaced by cmd_seat's
                                # own usage/docstring, deliberately not the
                                # global table
        "work": set(),
    }

    _SOURCES = {
        "chat": os.path.join("helm", "chat.py"),
        "seat": os.path.join("helm", "seat.py"),
        "work": os.path.join("helm", "work", "_cli.py"),
    }

    # MUST-HIT seeds: if the sweep stops seeing these, the REGEX rotted — the
    # zero is a broken probe, never a shrunken dispatcher.
    _SEEDS = {"chat": "post", "seat": "spawn", "work": "claim"}

    def _code_verbs(self, name):
        verbs = _sweep_verbs(self._SOURCES[name])
        if name == "chat":
            # chat delegates whole verb groups to tables, not if-chains
            verbs |= set(chat.SEAT_VERBS) | set(chat.MELD_VERBS)
        return verbs

    def test_every_public_dispatcher_verb_is_in_its_one_liner(self):  # noqa: VACUOUS_ASSERTION — the per-dispatcher must-hit seed assertIn is the unconditional positive control; the loop iterates a literal 3-key dict and cannot run zero times
        for name in sorted(self._SOURCES):
            code = self._code_verbs(name)
            self.assertIn(self._SEEDS[name], code,
                          "%s: the dispatcher sweep lost its seed verb — "
                          "fix the sweep, do not trust its zero" % name)
            stale = sorted(self._INTERNAL[name] - code)
            self.assertEqual(stale, [], "%s: these _INTERNAL entries no "
                             "longer exist in the dispatcher — drop them: %r"
                             % (name, stale))
            tokens = _one_liner_tokens(name)
            missing = sorted(code - self._INTERNAL[name] - tokens)
            self.assertEqual(
                missing, [],
                "%s: dispatcher subverbs missing from cli.py's one-liner: %r "
                "— name them in _VERB_HELP[%r], or (only if genuinely "
                "internal) register them in _INTERNAL here with a reason"
                % (name, missing, name))

    def test_the_one_liners_still_carry_a_grammar_half(self):  # noqa: VACUOUS_ASSERTION — assertIn on each of a literal 3-key dict's one-liners; the loop cannot run zero times
        # the token parse keys on the first ` — `; a reworded line that drops
        # it would silently shrink the token set to everything (or nothing).
        for name in sorted(self._SOURCES):
            self.assertIn(" — ", cli._VERB_HELP[name], name)


class StopGuardSwitchRegisterPinTest(unittest.TestCase):
    """ENVIRONMENT.md's HELM_STOP_GUARD_* rows are COMPLETE against the code:
    every per-check kill switch the guard actually reads (an
    `_off("STOP_GUARD_X")` call anywhere under helm/) has its own documented
    row. The 4-of-11 table this truing replaced is exactly the regrowth this
    pin forbids."""

    def _swept(self):
        names = set()
        for base, _dirs, files in os.walk(os.path.join(_ROOT, "helm")):
            for fn in files:
                if fn.endswith(".py"):
                    with open(os.path.join(base, fn), encoding="utf-8") as f:
                        names |= set(re.findall(
                            r'_off\("STOP_GUARD_([A-Z_]+)"\)', f.read()))
        return names

    def test_every_swept_switch_has_an_environment_row(self):
        swept = self._swept()
        self.assertIn("BEACON", swept,
                      "the switch sweep lost its seed — fix the regex, "
                      "do not trust its zero")
        text = _doc("ENVIRONMENT.md")
        missing = sorted(n for n in swept
                         if "`HELM_STOP_GUARD_%s`" % n not in text)
        self.assertEqual(
            missing, [],
            "per-check stop-guard switches with no ENVIRONMENT.md row: %r — "
            "every kill switch the guard reads gets its own documented row"
            % missing)

    def test_hooks_md_states_the_switch_count(self):
        self.assertIn("the %d per-check" % len(self._swept()),
                      _doc("HOOKS.md"),
                      "the per-check switch count HOOKS.md states must BE "
                      "the swept number")


class CloseReasonRegisterPinTest(unittest.TestCase):
    """The lr close-reason lists restate landreq.CLOSE_CLI_REASONS — exact
    membership AND order, in both reference surfaces."""

    def test_the_register_is_nonempty_and_still_has_landed(self):
        # must-hit: the pin is meaningless against an empty/renamed register
        self.assertIn("landed", landreq.CLOSE_CLI_REASONS)

    def test_the_cli_one_liner_states_the_register(self):
        want = "--reason " + "|".join(landreq.CLOSE_CLI_REASONS)
        self.assertIn(want, cli._VERB_HELP["lr"],
                      "cli.py's lr one-liner close-reason list drifted from "
                      "landreq.CLOSE_CLI_REASONS — restate it: %s" % want)

    def test_verbs_md_states_the_register_and_its_count(self):  # noqa: VACUOUS_ASSERTION — assertIn on the exact register string is the positive control; the register's non-emptiness is pinned by test_the_register_is_nonempty_and_still_has_landed
        text = _doc("VERBS.md")
        want = "--reason " + "|".join(landreq.CLOSE_CLI_REASONS)
        self.assertIn(want, text,
                      "docs/VERBS.md's lr close grammar drifted from "
                      "landreq.CLOSE_CLI_REASONS — restate it: %s" % want)
        n = len(landreq.CLOSE_CLI_REASONS)
        word = _WORDS.get(n)
        self.assertIsNotNone(word, "extend _WORDS for %d" % n)
        self.assertIn("%s reasons" % word.capitalize(), text,
                      "the stated reason count moved — it must be the "
                      "register's length (%d)" % n)


if __name__ == "__main__":
    unittest.main()
