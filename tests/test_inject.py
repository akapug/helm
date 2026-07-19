#!/usr/bin/env python3
"""inject tests — the ONE live per-turn surface (UserPromptSubmit hook on every
cred-home). Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR are tmp
dirs; the real ~/.helm, ~/.claude and ~/.cache are never touched.

Pins the load-bearing constants (PINNED_BUDGET / JIT_CAP / LINE_CAP), the
salience law (no match -> empty stdout, rc 0), the --json shape, inline-arg vs
stdin precedence, the fail-open law (a raising store must never block a turn),
the parsed-entry cache added for the twice-per-prompt parse fix (including the
entries= seam short-circuiting ALL store parsing), and the fire-ledger (row
shape, ids-never-text, rotation, fail-open, --explain writes no row)."""
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

from helm import home, inject, pk, store  # noqa: E402

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
        with open(inject._ledger_path(), encoding="utf-8") as f:
            return [json.loads(l) for l in f.read().splitlines()]

    def test_fired_row_shape_ids_never_prompt_text(self):
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, _, _ = self.run_inject([], stdin_text="tune the fluxcap now")
        self.assertEqual(rc, 0)
        self.assertEqual(inject._ledger_path(), os.path.join(
            os.environ["HELM_HOME"], "_global", ".state", "inject-ledger.jsonl"))
        r, = self.rows()
        self.assertEqual(r["v"], 1)
        self.assertTrue(r["ts"])
        self.assertIsNone(r["project"])
        self.assertEqual(r["fired"], {"pinned": ["pin-a"], "jit": ["jit-a"], "reflex": []})
        self.assertGreater(r["bytes"]["pinned"], 0)
        self.assertGreater(r["bytes"]["jit"], 0)
        self.assertEqual(r["bytes"]["reflex"], 0)
        self.assertEqual(r["candidates"], 2)
        self.assertGreaterEqual(r["elapsed_ms"], 0)
        self.assertNotIn("silent", r)
        # entry IDS only — the raw line must never carry the prompt
        with open(inject._ledger_path(), encoding="utf-8") as f:
            self.assertNotIn("tune the fluxcap", f.read())
        # jsonl append: a second call adds exactly one more row
        self.run_inject([], stdin_text="tune the fluxcap now")
        self.assertEqual(len(self.rows()), 2)

    def test_silent_turn_logs_silent_row(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject([], stdin_text="completely unrelated words")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        r, = self.rows()
        self.assertIs(r["silent"], True)
        self.assertEqual(r["v"], 1)
        self.assertNotIn("fired", r)
        self.assertNotIn("bytes", r)

    def test_rotation_at_5mb_one_generation(self):
        self.assertEqual(inject.LEDGER_MAX, 5 * 1024 * 1024)
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("x" * (inject.LEDGER_MAX + 1))
        self.plant_pinned("pin-a", "always truth")
        self.run_inject([], stdin_text="anything")
        self.assertEqual(len(self.rows()), 1)  # fresh file: this turn's row only
        with open(path + ".1", encoding="utf-8") as f:
            self.assertEqual(len(f.read()), inject.LEDGER_MAX + 1)
        # ONE generation: the next rotation replaces .1, never mints .2
        with open(path, "a") as f:
            f.write("y" * (inject.LEDGER_MAX + 1))
        self.run_inject([], stdin_text="anything")
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(os.path.exists(path + ".2"))
        with open(path + ".1", encoding="utf-8") as f:
            self.assertTrue(f.read().endswith("y"))

    def test_unwritable_ledger_fails_open(self):
        # a FILE where the .state dir belongs -> every ledger write raises
        g = os.path.join(os.environ["HELM_HOME"], "_global")
        os.makedirs(g)
        with open(os.path.join(g, ".state"), "w") as f:
            f.write("x")
        self.plant_pinned("pin-a", "always truth")
        rc, out, err = self.run_inject([], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn("PREMISE pin-a: always truth", out)
        self.assertEqual(err, "")

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
        self.assertIn("+ jit-a [matched: fluxcap]", out)
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

    def seed_registry(self, **paths):
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            n: {"name": n, "path": p, "kind": "git", "status": "active",
                "sessions": {}} for n, p in paths.items()}})

    def hook_stdin(self, prompt, cwd=None, session="sid-1", **extra):
        d = {"prompt": prompt, "cwd": cwd, "session_id": session,
             "hook_event_name": "UserPromptSubmit", **extra}
        return json.dumps({k: v for k, v in d.items() if v is not None})

    def ledger_rows(self):
        with open(inject._ledger_path(), encoding="utf-8") as f:
            return [json.loads(l) for l in f.read().splitlines()]

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

    def test_plain_stdin_contract_unchanged_no_session(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        rc, out, _ = self.run_inject([], stdin_text="tune the fluxcap")
        self.assertEqual(rc, 0)
        self.assertIn("jit-a", out)
        self.assertNotIn("session", self.ledger_rows()[-1])


if __name__ == "__main__":
    unittest.main()
