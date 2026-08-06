#!/usr/bin/env python3
"""The README makes CHECKABLE claims. These bind them to the code.

WHY THIS EXISTS. On 2026-07-29 the owner reviewed the 0.2 candidate and found
the README describing a product that no longer existed: it never mentioned
dregg, cv or CLIProxyAPI — "all of those are basically required to do anything
useful" — never explained that Claude Code is the harness of expertise because
of hooks, and never mentioned orca or herdr at all. `dregg` and `cv` appeared
exactly once each, in the LICENSE line. Every pillar was projects/store/drain/
sessions, while the product had become a multi-family agent-fleet substrate.

Rewriting it fixed the instance. This file is the attempt at the CLASS, and the
distinction matters: nothing-stale-ever is the right law, but it is enforced by
NOTICING, and noticing does not scale past the surfaces someone happens to look
at. A README goes stale silently, in the direction of flattering the past, and
the only reader who catches it is the one it was written for.

So: every claim the README makes that CODE can adjudicate gets adjudicated
here. The question asked of each sentence was "what would have to be true for
this to be a lie, and can I assert it?"

THE MOST VALUABLE ONE IS THE DENIAL. The README says tmux and cmux are NOT
supported — which is true today and is exactly the kind of sentence that
becomes false the moment someone adds an adapter, silently, in a commit that
has no reason to touch the README. A test that fails the day the code grows
past the doc is worth more than one that only checks today's truth.

WHAT THESE DELIBERATELY DO NOT DO: adjudicate prose. Whether the README
EXPLAINS well is a human question, and a test that tried would either be
vacuous or would fossilise the wording. These pin the FACTS a sentence asserts,
and leave the sentence alone.
"""
import os
import re
import sys
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import harness, hooks  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def readme():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        return f.read()


class MetaharnessClaimsTest(unittest.TestCase):
    """The README's metaharness section names exactly two adapters."""

    def test_every_adapter_the_README_claims_ACTUALLY_EXISTS(self):
        """The owner's review asserted tmux and cmux were supported. They are
        not — `ADAPTERS` is a two-entry dict — so the README says so instead of
        repeating it. A false capability claim in a public README is worse than
        an absent one: it is discovered by the person who trusted it."""
        text = readme()
        for name in harness.ADAPTERS:
            self.assertIn(name, text,
                          "%s is a real adapter and the README never names it "
                          "— a capability nobody can find is a capability "
                          "nobody uses" % name)

    def test_the_DENIAL_fails_the_day_the_code_outgrows_it(self):
        """THE SHARP ONE. The README states tmux/cmux are not supported today.
        That sentence becomes a lie the moment an adapter is added — in a
        commit with no reason to touch the README, by someone who has never
        read this paragraph. This is the test that notices instead of us."""
        text = readme()
        for absent in ("tmux", "cmux"):
            if absent in harness.ADAPTERS:
                self.fail(
                    "helm now ships a %s adapter and the README still says it "
                    "is NOT supported. Update the metaharness section — the "
                    "denial is load-bearing for a reader deciding whether helm "
                    "fits their setup." % absent)
            self.assertIn(absent, text,
                          "the README's not-supported list should still name "
                          "%s, or drop the claim entirely" % absent)

    def test_the_README_does_not_promise_an_adapter_that_is_not_wired(self):
        """The inverse staleness: prose naming a metaharness helm cannot
        actually drive. Only names presented as adapters are checked, so
        mentioning a tool in passing stays free."""
        text = readme()
        claimed = set(re.findall(r"\*\*\[?([a-z]+)\]?[^*]*\*\*\s+—\s+", text))
        for name in claimed & {"orca", "herdr", "tmux", "cmux", "zellij", "screen"}:
            self.assertIn(
                name, harness.ADAPTERS,
                "the README presents %s as an adapter but ADAPTERS has no such "
                "entry" % name)


class HookTableTest(unittest.TestCase):
    """The README explains WHY Claude Code is the harness of expertise with a
    table of hook events. Every row is a factual claim about helm's own specs."""

    def test_every_hook_event_in_the_table_is_one_helm_actually_installs(self):
        """The table is the README's answer to "why Claude Code" — it is the
        load-bearing paragraph of the whole how-it-works section. A row naming
        an event helm does not wire would be a mechanism that does not exist,
        described as the reason for an architecture."""
        text = readme()
        rows = set(re.findall(r"^\| `([A-Za-z]+)` \|", text, re.M))
        self.assertTrue(rows, "the hook table vanished — it is the README's "
                              "explanation of why Claude Code is first-class")
        real = {s["event"] for s in hooks.SPECS}
        self.assertEqual(rows - real, set(),
                         "the README's hook table names events helm does not "
                         "install: %s" % sorted(rows - real))

    def test_the_table_is_allowed_to_be_a_SAMPLE_not_the_full_set(self):
        """helm wires SessionEnd and UserPromptSubmit too. The README's table
        is illustrative and does not claim completeness, so this asserts the
        direction of the check rather than equality — pinning equality would
        force a README edit for every internal hook helm ever adds."""
        real = {s["event"] for s in hooks.SPECS}
        rows = set(re.findall(r"^\| `([A-Za-z]+)` \|", readme(), re.M))
        self.assertTrue(rows <= real)


class DependencyClaimsTest(unittest.TestCase):
    """The substrate table names what helm composes with."""

    def test_the_public_dependencies_are_all_named(self):
        """The owner's specific complaint, in one assertion: "it doesnt mention
        dregg, cv or cliproxy, when all of those are basically required to do
        anything useful". They are named now, and this fails if a future edit
        genericises them away — which is exactly how docs/ATTRIBUTION.md became
        eleven lines naming zero projects."""
        text = readme()
        for dep in ("dregg", "cv", "CLIProxyAPI"):
            self.assertIn(dep, text, "the README must name %s — it is "
                                     "load-bearing and was missing at 0.2" % dep)

    def test_the_LICENSE_the_README_claims_is_the_LICENSE_that_ships(self):
        """Added after finding docs/LICENSE-TODO.md still saying "this repo
        currently ships no license — owner decision required before any public
        flip", eight days after AGPL-3.0 shipped in e202b0b and while the owner
        was reading those very docs to decide on a public flip. He would have
        read an open licensing question that had been closed for a week.

        The badge, the License section and the LICENSE file are three surfaces
        asserting one fact, and nothing was checking they agreed. This is the
        gap the rest of this file left: I bound adapters, hooks, dependencies
        and the zero-dep promise, and not the single claim with legal weight."""
        text = readme()
        path = os.path.join(ROOT, "LICENSE")
        self.assertTrue(os.path.exists(path),
                        "the README badges a license; the file must exist")
        with open(path, encoding="utf-8") as f:
            head = f.read(4000).upper()
        claimed_agpl = "AGPL" in text
        ships_agpl = "AFFERO GENERAL PUBLIC LICENSE" in head
        self.assertEqual(
            claimed_agpl, ships_agpl,
            "the README says AGPL=%s but LICENSE is AGPL=%s — badge, prose and "
            "file must agree, and this is the one claim with legal weight"
            % (claimed_agpl, ships_agpl))

    def test_no_doc_still_calls_the_license_UNDECIDED(self):
        """The stale-planning class, bound. A doc asserting the licence is open
        while LICENSE ships is not a harmless leftover — it is read by the
        person deciding whether to publish, and it tells him the opposite of
        the truth at exactly the moment it matters."""
        docs = os.path.join(ROOT, "docs")
        offenders = []
        for fn in sorted(os.listdir(docs)):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(docs, fn), encoding="utf-8",
                      errors="replace") as f:
                body = f.read().lower()
            if "ships **no license**" in body or "ships no license" in body:
                offenders.append(fn)
        self.assertEqual(offenders, [],
                         "these docs still claim helm ships no license, which "
                         "stopped being true when LICENSE landed: %s" % offenders)

    def test_the_stdlib_only_promise_is_still_true(self):
        """The README promises zero dependencies. A third-party import in the
        package would make the badge, the Requirements section and the
        substrate table simultaneously false."""
        import ast
        stdlib_ok = (set(sys.stdlib_module_names)
                     if hasattr(sys, "stdlib_module_names") else None)
        if stdlib_ok is None:
            self.skipTest("python < 3.10 has no stdlib_module_names")
        pkg = os.path.join(ROOT, "helm")
        bad = []
        for fn in os.listdir(pkg):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(pkg, fn), encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    mods = [(node.module or "").split(".")[0]]
                for m in mods:
                    if m and m != "helm" and m not in stdlib_ok:
                        bad.append("%s imports %s" % (fn, m))
        self.assertEqual(bad, [], "the README promises zero dependencies: %s"
                         % bad)

if __name__ == "__main__":
    unittest.main()
