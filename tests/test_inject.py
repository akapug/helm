#!/usr/bin/env python3
"""inject tests — the ONE live per-turn surface (UserPromptSubmit hook on every
cred-home). Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR are tmp
dirs; the real ~/.helm, ~/.claude and ~/.cache are never touched.

Pins the load-bearing constants (PINNED_BUDGET / JIT_CAP / LINE_CAP), the
salience law (no match -> empty stdout, rc 0), the --json shape, inline-arg vs
stdin precedence, the fail-open law (a raising store must never block a turn),
and the parsed-entry cache added for the twice-per-prompt parse fix."""
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

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, inject, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR")


class InjectBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-inject-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
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


class BudgetTest(InjectBase):
    def test_pinned_lane_stops_at_budget(self):
        # 6 pinned entries, each line ~260 bytes -> only ~4 fit under 1200
        for i in range(6):
            self.plant_pinned("pin-%d" % i, ("truth %d " % i) + "x" * 240)
        sections = inject.gather("anything at all")
        got = sections["pinned"]
        self.assertTrue(got, "pinned lane must fire")
        self.assertLess(len(got), 6, "budget must exclude some entries")
        self.assertLessEqual(sum(len(l) for l in got), inject.PINNED_BUDGET)
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

    def test_line_truncation_at_400_with_ellipsis(self):
        self.plant_pinned("long-one", "y" * 600)
        sections = inject.gather("anything")
        line = sections["pinned"][0]
        self.assertEqual(len(line), inject.LINE_CAP)
        self.assertTrue(line.endswith("…"))
        # a short line is untouched
        self.plant_pinned("short-one", "small truth")
        lines = inject.gather("anything")["pinned"]
        short = next(l for l in lines if "short-one" in l)
        self.assertEqual(short, "PREMISE short-one: small truth")


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


class ShapeTest(InjectBase):
    def test_json_shape(self):
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject(["--json"], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(sorted(d), ["jit", "pinned", "reflex"])
        for k in ("pinned", "jit", "reflex"):
            self.assertIsInstance(d[k], list)
        self.assertEqual(d["pinned"], ["PREMISE pin-a: always truth"])
        self.assertEqual(d["jit"], ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertEqual(d["reflex"], [])

    def test_json_no_match_is_empty_lists_not_silence(self):
        rc, out, _ = self.run_inject(["--json"], stdin_text="nothing")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"pinned": [], "jit": [], "reflex": []})

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

    def test_unwritable_cache_dir_still_serves(self):
        blocker = os.path.join(self.tmp, "not-a-dir")
        with open(blocker, "w") as f:
            f.write("x")
        os.environ["HELM_CACHE_DIR"] = os.path.join(blocker, "cache")  # mkdir fails
        self.plant_pinned("pin-a", "always truth")
        self.assertEqual(inject.gather("x")["pinned"], ["PREMISE pin-a: always truth"])


if __name__ == "__main__":
    unittest.main()
