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
import shutil
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



def _stdlib_names(case):
    """sys.stdlib_module_names, or skip the calling test below 3.10."""
    if not hasattr(sys, "stdlib_module_names"):
        case.skipTest("python < 3.10 has no stdlib_module_names")
    return set(sys.stdlib_module_names)


def _scan_third_party(pkg, stdlib_ok, snapshots=None):
    """Third-party import findings for every .py directly in pkg.

    Guarded imports are exempted BY NODE: an import inside a try whose
    except catches ImportError/ModuleNotFoundError is the documented
    optional pattern (tomllib pre-3.11). The exemption never travels by
    NAME — an unguarded import of the same module elsewhere still counts.

    Installed scanners may also require a sibling snapshot. That is an
    INTERNAL dependency, not an optional third-party import: both files must
    be actual sources in this package and in the installer's snapshot map.

    A PATHNAME FALLBACK PAIR is exempt, and it is recognised as a PAIR. A
    module that is also a documented script (`python3 helm/physics.py ...`)
    has no package parent when run that way, so `from . import pk` raises and
    the handler reaches the same file as a bare `import pk`. Every one of
    these must hold, and any one failing is a finding:

      * the try body is EXACTLY ONE STATEMENT and it is `from . import N` —
        level 1, no module, one name, no alias. EVERY statement counts, not
        just the qualifying ones: an unrelated import standing beside the
        relative attempt raises first under an ordinary package import, so
        the handler's bare import is not that attempt's fallback at all;
      * the ImportError/ModuleNotFoundError handler is EXACTLY ONE STATEMENT
        and it is `import N` for that same N, undotted and unaliased;
      * both are DIRECT statements of their block, never nested in a function
        or class, because a deferred import is not this idiom;
      * `<pkg>/N.py` exists.

    THE SIBLING FILE IS NECESSARY AND NEVER SUFFICIENT. A bare name does not
    resolve to a sibling under an ordinary package import — only the package
    PARENT is on sys.path, so a bare name needs a TOP-LEVEL module — and it
    resolves only in the pathname case. Exempting on ancestry plus a
    same-named file would clear `try: import optional / except ImportError:
    import requests` the moment any `requests.py` sat in this package.

    SCOPE: this is a CONSERVATIVE SYNTACTIC RECOGNITION of the pathname
    fallback idiom as this tree writes it. It is NOT proof that Python
    resolves the same module both ways, and NOT a claim that an arbitrary
    ImportError means a missing package parent.
    """
    import ast
    installed = {os.path.basename(dest): os.path.abspath(source)
                 for dest, source in (snapshots or {}).items()}
    bad = []
    for fn in sorted(os.listdir(pkg)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(pkg, fn), encoding="utf-8") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue
        guarded_nodes = set()
        sibling_fallbacks = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            catches_import = any(
                isinstance(h.type, ast.Name)
                and h.type.id in ("ImportError", "ModuleNotFoundError")
                for h in node.handlers)
            if not catches_import:
                continue
            for inner in node.body:
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    guarded_nodes.add(id(inner))
            # THE PATHNAME FALLBACK PAIR, and it is recognised as a PAIR.
            # Handler ancestry plus a same-named file establishes NOTHING: a
            # try whose body imports an unrelated optional module and whose
            # handler imports `requests` would be exempted the moment some
            # `requests.py` happened to sit in this package. What earns the
            # exemption is CORRESPONDENCE — one relative attempt and its own
            # bare fallback, for the same name — and the sibling file is a
            # NECESSARY condition, never a sufficient one. A bare name does
            # not resolve to a sibling under an ordinary package import at
            # all (only the package PARENT is on sys.path, so a bare name
            # needs a TOP-LEVEL module); it resolves only when the file is
            # run by pathname, which is the single case this recognises.
            # WHOLE BODIES, NOT QUALIFYING STATEMENTS. Counting only the
            # statements that qualify let an unrelated import sit beside the
            # relative attempt and still be called a pair:
            #     try:
            #         import definitely_optional
            #         from . import requests
            #     except ImportError:
            #         import requests
            # Under an ordinary package import the missing optional raises
            # BEFORE the relative attempt runs, so the bare fallback is not
            # the fallback for it — and a `requests.py` in the package would
            # have laundered a real dependency. One statement each side.
            if len(node.body) != 1:
                continue
            rel = [i for i in node.body
                   if isinstance(i, ast.ImportFrom)
                   and i.level == 1 and i.module is None
                   and len(i.names) == 1 and not i.names[0].asname]
            if len(rel) != 1:
                continue
            want = rel[0].names[0].name
            for handler in node.handlers:
                if not (isinstance(handler.type, ast.Name)
                        and handler.type.id in ("ImportError",
                                                "ModuleNotFoundError")):
                    continue
                # The handler is counted the same way: EXACTLY ONE
                # statement, and it must be the matching bare import. An
                # import deferred inside a nested function or class is not
                # this idiom, and neither is a pair with anything else
                # standing beside it.
                if len(handler.body) != 1:
                    continue
                bare = [i for i in handler.body
                        if isinstance(i, ast.Import)
                        and len(i.names) == 1 and not i.names[0].asname
                        and i.names[0].name == want]
                if len(bare) == 1:
                    sibling_fallbacks.add(id(bare[0]))
        for node in ast.walk(tree):
            if id(node) in guarded_nodes:
                continue
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                mods = [(node.module or "").split(".")[0]]
            for m in mods:
                sibling = os.path.abspath(os.path.join(pkg, m + ".py"))
                if installed.get(fn) == os.path.abspath(os.path.join(pkg, fn)) \
                        and installed.get(m + ".py") == sibling \
                        and os.path.isfile(sibling):
                    continue
                if id(node) in sibling_fallbacks and os.path.isfile(sibling):
                    continue
                if m and m != "helm" and m not in stdlib_ok:
                    bad.append("%s imports %s" % (fn, m))
    return bad


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
        stdlib_ok = _stdlib_names(self)
        from helm.work import _guard
        bad = _scan_third_party(os.path.join(ROOT, "helm"), stdlib_ok,
                                _guard._scanner_assets(ROOT))
        self.assertEqual(bad, [], "the README promises zero dependencies: %s"
                         % bad)

    def _pkg(self, scanner_src, extra=("shared",)):
        """A throwaway package: scanner.py plus the named sibling files."""
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name in extra:
            with open(os.path.join(d, name + ".py"), "w") as f:
                f.write("VALUE = 1\n")
        with open(os.path.join(d, "scanner.py"), "w") as f:
            f.write(scanner_src)
        return d

    def test_a_pathname_fallback_PAIR_is_not_a_dependency(self):
        """A module that is ALSO a documented script has no package parent
        when run by pathname, so its relative import raises and the handler
        reaches the same file by its bare name. That names helm's own module.

        THE PAIR EARNS IT, NOT THE FILE. Every pole below reaches this same
        predicate — each one has a real `from . import` in the try body, so
        none of them can pass through the earlier optional-import skip — and
        each asserts the finding TEXT, so a pole cannot look cured by failing
        somewhere else for an unrelated reason."""
        d = self._pkg("try:\n"
                      "    from . import shared\n"
                      "except ImportError:\n"
                      "    import shared\n")
        self.assertEqual(_scan_third_party(d, set()), [])

    def test_an_unrelated_optional_guard_does_not_launder_a_dependency(self):
        """THE COUNTEREXAMPLE VERBATIM, and its try body is part of the
        witness rather than an accident of fixture style: the optional import
        is skipped by the inherited try-body rule, and an ancestry-plus-file
        exemption would then clear `requests` in the handler purely because a
        `requests.py` happens to sit in this package. The decisive assertion
        is the HANDLER finding text."""
        d = self._pkg("try:\n"
                      "    import definitely_optional\n"
                      "except ImportError:\n"
                      "    import requests\n",
                      extra=("shared", "requests"))
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports requests"])

    def test_only_the_CORRESPONDING_bare_fallback_is_exempt(self):
        """The same hole reached through the NEW predicate instead of the
        inherited skip: here the try body really does hold a relative import,
        so the pair rule is what has to refuse the mismatched handler name."""
        d = self._pkg("try:\n"
                      "    from . import shared\n"
                      "except ImportError:\n"
                      "    import requests\n",
                      extra=("shared", "requests"))
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports requests"])

    def test_a_wrong_relative_module_or_level_earns_nothing(self):
        """`from .other import N` and `from .. import N` can name a different
        object than the bare `import N` beside them."""
        for rel in ("from .other import shared", "from .. import shared"):
            with self.subTest(rel=rel):
                d = self._pkg("try:\n"
                              "    %s\n"
                              "except ImportError:\n"
                              "    import shared\n" % rel,
                              extra=("shared", "other"))
                self.assertEqual(_scan_third_party(d, set()),
                                 ["scanner.py imports shared"])

    def test_a_nested_or_deferred_half_earns_nothing(self):
        """Both halves must be DIRECT statements of their block: a deferred
        import inside a nested function is not this idiom."""
        deferred_handler = ("try:\n"
                            "    from . import shared\n"
                            "except ImportError:\n"
                            "    def _late():\n"
                            "        import shared\n"
                            "        return shared\n")
        nested_relative = ("try:\n"
                           "    def _late():\n"
                           "        from . import shared\n"
                           "except ImportError:\n"
                           "    import shared\n")
        for src in (deferred_handler, nested_relative):
            with self.subTest(src=src.splitlines()[1].strip()):
                d = self._pkg(src)
                self.assertEqual(_scan_third_party(d, set()),
                                 ["scanner.py imports shared"])

    def test_a_dotted_name_earns_nothing_when_only_the_top_file_exists(self):
        d = self._pkg("try:\n"
                      "    from . import shared\n"
                      "except ImportError:\n"
                      "    import shared.missing\n")
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports shared"])

    def test_an_alias_on_either_half_earns_nothing(self):
        """Refused until something needs it: an exemption with no live
        consumer is how the previous hole got in."""
        for src in ("try:\n    from . import shared as s\n"
                    "except ImportError:\n    import shared as s\n",
                    "try:\n    from . import shared\n"
                    "except ImportError:\n    import shared as s\n"):
            with self.subTest(src=src.splitlines()[1].strip()):
                d = self._pkg(src)
                self.assertEqual(_scan_third_party(d, set()),
                                 ["scanner.py imports shared"])

    def test_an_alias_on_the_RELATIVE_half_alone_earns_nothing(self):
        """The alias controls beside this one cover both-halves-aliased and
        handler-only-aliased. Without this third spelling, deleting only the
        relative half's alias check is a survivor, and the per-clause kill
        claim for that check has no arm behind it."""
        d = self._pkg("try:\n"
                      "    from . import shared as s\n"
                      "except ImportError:\n"
                      "    import shared\n")
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports shared"])

    def test_a_missing_sibling_file_earns_nothing(self):
        """File existence is NECESSARY, and these arms are what keep it from
        looking sufficient."""
        d = self._pkg("try:\n"
                      "    from . import elsewhere\n"
                      "except ImportError:\n"
                      "    import elsewhere\n", extra=())
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports elsewhere"])

    def test_TWO_relative_attempts_in_one_try_earn_nothing(self):
        """SOLE on both sides. With two relative attempts in one try body,
        nothing says WHICH one the single bare fallback corresponds to, and
        picking the first is exactly the inference the pair rule refuses."""
        d = self._pkg("try:\n"
                      "    from . import shared\n"
                      "    from . import other\n"
                      "except ImportError:\n"
                      "    import shared\n", extra=("shared", "other"))
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports shared"])

    def test_a_MIXED_handler_body_earns_nothing_for_either_import(self):
        """THIS ARM IS THE INVERSE OF THE ONE IT REPLACES, deliberately. An
        earlier round read SOLE as sole-for-that-name and pinned the pair as
        still exempt with an unrelated import beside it. The agreed rule
        counts EVERY statement, so a mixed handler body is not the idiom and
        BOTH imports are reported. A surviving arm that contradicts the rule
        is worse than a missing one, so it is rewritten rather than kept."""
        d = self._pkg("try:\n"
                      "    from . import shared\n"
                      "except ImportError:\n"
                      "    import shared\n"
                      "    import other\n", extra=("shared", "other"))
        self.assertEqual(sorted(_scan_third_party(d, set())),
                         ["scanner.py imports other",
                          "scanner.py imports shared"])

    def test_a_MIXED_try_body_earns_nothing_and_reports_the_dependency(self):
        """THE WITNESS, verbatim: an unrelated optional import standing beside
        the relative attempt. Under an ordinary package import the missing
        optional raises BEFORE the relative attempt runs, so the handler's
        bare import is not that attempt's fallback — and a same-named file in
        the package would otherwise launder a real dependency."""
        d = self._pkg("try:\n"
                      "    import definitely_optional\n"
                      "    from . import requests\n"
                      "except ImportError:\n"
                      "    import requests\n",
                      extra=("shared", "requests"))
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports requests"])

    def test_TWO_bare_fallbacks_for_the_SAME_name_earn_nothing(self):
        """Two `import shared` statements in one handler is not the idiom, and
        the rule requires exactly one."""
        d = self._pkg("try:\n"
                      "    from . import shared\n"
                      "except ImportError:\n"
                      "    import shared\n"
                      "    import shared\n")
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports shared",
                          "scanner.py imports shared"])

    def test_an_unguarded_bare_sibling_import_is_unchanged(self):
        """The pre-existing rule: it would fail under an ordinary package
        import, so it stays a finding."""
        d = self._pkg("import shared\n")
        self.assertEqual(_scan_third_party(d, set()),
                         ["scanner.py imports shared"])

    def test_required_sibling_import_needs_BOTH_real_snapshot_sources(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            importer = os.path.join(d, "scanner.py")
            sibling = os.path.join(d, "shared.py")
            with open(importer, "w") as f:
                f.write("import shared\n")
            with open(sibling, "w") as f:
                f.write("VALUE = 1\n")
            snapshots = {os.path.join(d, "installed", "scanner.py"): importer,
                         os.path.join(d, "installed", "shared.py"): sibling}
            expected = ["scanner.py imports shared"]
            self.assertEqual(_scan_third_party(d, set()), expected)
            self.assertEqual(_scan_third_party(d, set(), snapshots), [])
            importer_only = {dest: source for dest, source in snapshots.items()
                             if source == importer}
            self.assertEqual(_scan_third_party(d, set(), importer_only), expected)
            sibling_only = {dest: source for dest, source in snapshots.items()
                            if source == sibling}
            self.assertEqual(_scan_third_party(d, set(), sibling_only), expected)
            os.remove(sibling)
            self.assertEqual(_scan_third_party(d, set(), snapshots), expected)

    def test_guarded_import_exemption_is_per_node_not_per_name(self):
        """A guarded import elsewhere in the file must not amnesty an
        UNGUARDED import of the same module — the exemption follows the
        exact AST node, or a real dependency hides behind its optional
        twin."""
        stdlib_ok = _stdlib_names(self)
        import tempfile
        guard = ("try:\n    import definitely_third_party\n"
                 "except ImportError:\n    definitely_third_party = None\n")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "both.py"), "w") as f:
                f.write(guard + "import definitely_third_party\n")
            with open(os.path.join(d, "guarded.py"), "w") as f:
                f.write(guard)
            with open(os.path.join(d, "unguarded.py"), "w") as f:
                f.write("import definitely_third_party\n")
            self.assertEqual(
                _scan_third_party(d, stdlib_ok),
                ["both.py imports definitely_third_party",
                 "unguarded.py imports definitely_third_party"])

if __name__ == "__main__":
    unittest.main()
