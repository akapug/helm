#!/usr/bin/env python3
"""Every Claude-Code-vocabulary env var helm exports must be one CC READS.

WHY THIS FILE EXISTS (#182). A seat launch line sets CLAUDE_* and ANTHROPIC_*
variables. An env var Claude Code does not read is not an error anywhere: no
warning, no exit code, no log line. It silently does nothing, forever, and
every test that asserts the launch line CONTAINS the string still passes. Nine
assertions in test_seat.py pinned CLAUDE_CODE_MAX_CONTEXT_TOKENS and not one of
them could tell a working variable from a decorative one.

THE INVESTIGATION THAT PRODUCED THIS GUARD ALSO PRODUCED ITS SHAPE. #182 was
opened on the finding that CLAUDE_CODE_MAX_CONTEXT_TOKENS "is not a Claude Code
variable — it does not appear in the official env-var reference." The premise
was checked against the DOCUMENTATION, which answers "is this published?" The
question that decides whether a launch line works is "does the program read
it?", and those differ: the variable is UNDOCUMENTED AND READ. Reading the
shipped binary settled in one command what two days of doc-checking could not,
and the prescribed fix — swap it for the documented name — would have removed
the only knob that sets capacity for non-claude seats.

So the allowlist is NOT a copy of Anthropic's published table. A copy would rot
within a release, and worse, it would re-enshrine the wrong oracle: a name
absent from the docs would fail this test while working perfectly in
production. The list is the vars WE export, each carrying the evidence that CC
reads it, and the test fails CLOSED on any name nobody has checked.

TWO ARMS, because they answer different questions and can fail apart:
  * membership  — always runs. Every exported CC-vocabulary name is on the list.
                  This is the one that stops a new invention at build time.
  * grounding   — runs only where the shipped binary is readable, and SKIPS
                  (never passes) elsewhere. It is the anti-rot arm: it proves
                  the list still describes the Claude Code we actually launch.
A skip is not a pass. If this file only ever skips the grounding arm on the
machine that gates, the list is unverified there and the membership arm is
doing all the work — which is the honest state, stated, rather than a green
that means nothing.
"""
import os
import re
import shutil
import subprocess
import unittest

from helm import seat
from helm.seat_catalog import taught_window

# Name -> why we believe Claude Code reads it. VERIFIED 2026-08-04 against the
# shipped binary at claude 2.1.221 (`strings -a | grep -x`), exact-match counts
# in parentheses. "docs" means it also appears in the published env-var
# reference; its ABSENCE from the docs is not evidence of anything, which is
# the whole lesson of #182.
CC_ENV_ALLOWLIST = {
    "ANTHROPIC_API_KEY":
        "binary (21) + docs. helm only ever UNSETS it — never written "
        "anywhere by seat.py, per the Claude-API house rule.",
    "ANTHROPIC_AUTH_TOKEN":
        "binary (11) + docs. The proxy bearer, exported by a shell builtin so "
        "it never lands in any process's argv.",
    "ANTHROPIC_BASE_URL":
        "binary (7) + docs. Points a seat at its local CLIProxyAPI.",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE":
        "binary (3) + docs. Percentage of the window at which auto-compaction "
        "fires. Clamped downward only (Math.min), so it can lower a threshold "
        "and never raise one.",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW":
        "binary (4) + docs. The auto-compact window itself, and CLAMPED BY the "
        "capacity below: SJ() returns {window: Math.min(o, c)}. Setting this "
        "alone cannot widen a capacity nobody configured.",
    "CLAUDE_CODE_BRIDGE_SESSION_ID":
        "binary (5) + docs. Stripped on launch so an inherited Remote Control "
        "stamp cannot bind the seat's own session.",
    "CLAUDE_CODE_CHILD_SESSION":
        "binary (8) + docs. Stripped on launch — the child stamp that killed "
        "seat persistence (upstream fix(pty) in the orca fork).",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS":
        "binary (3), UNDOCUMENTED AND READ — the #182 variable. NEu() ends "
        "`if (n > 0 && !model.startsWith(\"claude-\")) return n`, so it sets "
        "capacity for exactly the non-claude proxy seats helm mints it for. "
        "Do not delete it because a docs search comes back empty.",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS":
        "binary (1 exact, 8 total) at claude 2.1.273, DOCUMENTED AND READ — "
        "it is in the bundle's own Environment-variables settings list AND in "
        "a user-facing sentence (\"To configure this behavior, set the "
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable\"), and the read "
        "site is explicit: `Ire(\"CLAUDE_CODE_MAX_OUTPUT_TOKENS\", "
        "process.env.CLAUDE_CODE_MAX_OUTPUT_TOKENS, n.default, "
        "n.upperLimit).effective`. It caps the output a seat may request, "
        "which on the openrouter family is a SAFETY knob rather than a "
        "preference: those are reasoning models, and under a low cap they "
        "spend the whole budget thinking and return an EMPTY answer that "
        "reads exactly like a broken family. HONEST LIMIT, because the "
        "fragment above states it: the value is CLAMPED to an upperLimit the "
        "binary derives from the MODEL (`fQ(e)`), so what a non-claude proxy "
        "model resolves to is not settled from here — `/context` and a live "
        "turn are, the same unsettled note MAX_CONTEXT_TOKENS carries.",
    "CLAUDE_CODE_SESSION_ID":
        "binary (9). Stripped on launch alongside the other child stamps.",
    "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS":
        "binary (1 exact, 5 total) at claude 2.1.273, UNDOCUMENTED AND READ: "
        "the Workflow tool reads it as the per-run concurrent-agent cap "
        "(`Wt=a.CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS??Pr`), which is "
        "how a proxy seat admits a bounded Workflow (seat_catalog.WORKFLOW_CAP_VAR).",
    "CLAUDE_CODE_SUBAGENT_MODEL":
        "binary (4). Pins the subagent model on a single-model seat; dropped "
        "entirely under --multi so frontmatter can route per-subagent.",
    "CLAUDE_CONFIG_DIR":
        "binary (6). The seat's isolated config dir, so CC discovers no "
        "skills, agents or MCP servers from the minting host.",
}

# Anything matching this is spoken TO Claude Code and must be on the list.
# HELM_*, DREGG_*, MELD_* are our own vocabulary and are not CC's business.
_CC_VOCABULARY = re.compile(r"\A(?:CLAUDE|ANTHROPIC)[A-Z0-9_]*\Z")
_ASSIGNED = re.compile(r"(?:\A|\s)(?:-u\s+)?([A-Z][A-Z0-9_]{2,})=")
_UNSET = re.compile(r"-u\s+([A-Z][A-Z0-9_]{2,})")


def _exported(line):
    """Every env var name a launch line sets OR unsets."""
    return set(_ASSIGNED.findall(line)) | set(_UNSET.findall(line))


def _claude_binary():
    """The shipped Claude Code this box would actually launch, or None."""
    found = shutil.which("claude")
    if not found:
        return None
    real = os.path.realpath(found)
    return real if os.path.isfile(real) else None


class SeatEnvAllowlistTest(unittest.TestCase):
    def _lines(self):
        """The COMPOSED launcher, not just launch_line().

        What a seat actually receives is `_token_export(...) + launch_line(...)`
        — both the printed launch (seat.py ~6634) and the generated launch.sh
        (~2710) prepend the exporter, and it is a REAL exporter: it puts
        ANTHROPIC_AUTH_TOKEN into the environ with shell builtins. Enumerating
        only launch_line left a whole exporter outside the guard, so an invented
        CLAUDE_CODE_* var added THERE would have passed while the same name in
        launch_line reddened. A review found it on the lane whose
        entire point is that an unrecognised env var fails silently — the guard
        had the same blind spot as the bug.

        The enumeration is the thing to keep honest: a scan is only as good as
        the SET it walks, and a set that excludes a producer reports health it
        never measured."""
        out = {}
        for family in seat.FAMILIES:
            try:
                out[family] = (seat._token_export(family)
                               + seat.launch_line(family))
            except Exception:      # noqa: BLE001 — a family that cannot mint a
                continue           # line here is another test's failure, not ours
        return out

    def test_every_exported_cc_variable_is_on_the_allowlist(self):
        lines = self._lines()
        self.assertTrue(lines, "no family produced a launch line to inspect")
        seen = set()
        for family, line in lines.items():
            for name in _exported(line):
                if not _CC_VOCABULARY.match(name):
                    continue
                seen.add(name)
                self.assertIn(
                    name, CC_ENV_ALLOWLIST,
                    "%s exports %s to Claude Code and nobody has checked that "
                    "CC READS it. An unread env var fails SILENTLY — no "
                    "warning, no exit code — so this test is the only place it "
                    "can ever fail. Verify it against the shipped binary "
                    "(`strings -a $(realpath $(which claude)) | grep -x %s`), "
                    "then add it to CC_ENV_ALLOWLIST with that evidence. Do "
                    "NOT decide from the docs alone: %s is undocumented and "
                    "read, which is exactly how #182 happened."
                    % (family, name, name, "CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
        # POSITIVE CONTROL. Without this the loop above passes perfectly when
        # the extractor matches nothing at all — the shape that let a decorative
        # variable live for days.
        self.assertIn("ANTHROPIC_BASE_URL", seen,
                      "the extractor found no ANTHROPIC_BASE_URL, so it is not "
                      "reading launch lines and every assertion above was "
                      "vacuous")
        # AND THE COMPOSED HALF IS IN THE SET TOO. ANTHROPIC_AUTH_TOKEN is
        # exported ONLY by _token_export, never by launch_line, so seeing it
        # here is the proof that the prefix is being scanned. Without this the
        # enumeration could silently narrow back to launch_line alone and every
        # assertion above would still pass — which is exactly how the gap
        # a review found got in.
        self.assertIn("ANTHROPIC_AUTH_TOKEN", seen,
                      "the scan is not reading the composed launcher: "
                      "_token_export's own export is invisible to it, so a "
                      "var invented THERE would never fail this test")
        self.assertGreaterEqual(len(seen), 5)

    def test_the_two_context_knobs_are_minted_together(self):  # noqa: VACUOUS_ASSERTION — the observable IS per-family and so is its control (assertIn HELM_MODEL_FAMILY on the same line, immediately above each assertNotIn); an unconditional control cannot exist for a line that only exists inside the loop. assertGreater(checked, 0) covers the empty-iteration case.
        """#182's actual defect, pinned. Capacity and window are separate
        knobs and the window is clamped by the capacity, so a family that
        mints one without the other silently keeps CC's 200k default."""
        checked = 0
        for family, line in self._lines().items():
            if not seat.FAMILIES[family].get("max_context"):
                # POSITIVE CONTROL ON THIS FAMILY'S OWN LINE before asserting
                # what is absent from it: an empty string satisfies every
                # assertNotIn ever written.
                self.assertIn("HELM_MODEL_FAMILY=%s" % family, line)
                self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", line)
                continue
            checked += 1
            # THE TAUGHT window: max_context narrowed by a declared
            # context_budget (kimi, task/2944), the one number both knobs and
            # the autocompact gauge carry.
            fam = seat.FAMILIES[family]
            window = taught_window(fam, fam["max_context"])
            self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % window, line,
                          "%s lost its capacity knob" % family)
            self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % window, line,
                          "%s sets a capacity but no auto-compact window; the "
                          "window then falls back to CC's default and the "
                          "capacity does nothing visible" % family)
        self.assertGreater(checked, 0,
                           "no family declares max_context, so this test "
                           "proved nothing")

    @unittest.skipIf(_claude_binary() is None,
                     "no shipped claude binary on this box — the grounding arm "
                     "SKIPS rather than passes, because 'I could not look' and "
                     "'I looked and it was fine' are different results")
    def test_the_allowlist_still_matches_the_shipped_binary(self):
        binary = _claude_binary()
        if binary is None:
            # The decorator answered at IMPORT time; this re-asks at RUN time,
            # because a full-suite run puts minutes of other tests between the
            # two and one of them clobbering PATH turned this arm into a
            # TypeError (measured 2026-08-04: test_cell's bare-name tests
            # overwrote PATH without restore — fixed there too). A skip stays
            # the honest verdict either way.
            self.skipTest("claude binary resolvable at import but not at run "
                          "time — an earlier test mutated PATH")
        found = subprocess.run(["strings", "-a", binary],
                               capture_output=True, text=True, timeout=180)
        if found.returncode != 0:
            self.skipTest("strings could not read %s" % binary)
        names = set(found.stdout.splitlines())
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional: prove we
        # actually read strings out of this binary before concluding anything
        # from what is missing. An empty `names` would make the emptiness
        # assertion below read as a clean bill of health.
        self.assertIn("ANTHROPIC_BASE_URL", names)
        # NEGATIVE CONTROL: if an obviously fake name "matches", the
        # comparison is broken and every positive above is meaningless.
        self.assertNotIn("CLAUDE_CODE_TOTALLY_INVENTED_XYZ", names)
        self.assertNotIn("HELM_CHAT_NAME", names)
        missing = sorted(n for n in CC_ENV_ALLOWLIST if n not in names)
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — `missing` is derived from `names`, whose unconditional positive control (assertIn ANTHROPIC_BASE_URL) is eight lines above; the rung cannot follow provenance through the comprehension. An unread binary yields empty `names`, which makes EVERY allowlist name missing and fails this loudly — it cannot go green on a dead read.
            missing, [],
            "these names are on the allowlist but the shipped Claude Code (%s) "
            "does not contain them — either the list has rotted or a variable "
            "was removed upstream, and any seat still exporting one is "
            "configuring nothing" % os.path.basename(binary))

    def test_every_allowlist_entry_carries_its_evidence(self):
        """A name with an empty reason is a name nobody checked."""
        # UNCONDITIONAL FIRST: every assertion below lives inside the loop, so
        # an EMPTY allowlist would satisfy all of them and report perfect
        # health. That is the same shape as the bug this file exists for.
        self.assertGreaterEqual(len(CC_ENV_ALLOWLIST), 5)
        for name, why in CC_ENV_ALLOWLIST.items():
            self.assertTrue(why and why.strip(), name)
            self.assertIn("binary", why,
                          "%s cites no binary verification; the docs alone "
                          "cannot answer whether CC reads a variable" % name)


if __name__ == "__main__":
    unittest.main()
