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
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, inject, pk, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR",
            # the shadow backend's activation env — popped so the WHOLE suite is
            # hermetic (a stray HELM_CF_ENDPOINT must never let a test reach out)
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_CF_TOKEN", "MELD_CF_TOKEN")


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


class SessionCooldownTest(InjectBase):
    """The habituation guard extended to the JIT lane: per-session suppression
    at _global/.state/inject-seen/<session>.json, COOLDOWN_TURNS window, 2x
    score escape, pinned/reflex exempt, freed cap slots, fail-open."""

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
        with open(inject._ledger_path(), encoding="utf-8") as f:
            return [json.loads(l) for l in f.read().splitlines()]

    def seen(self, session="s1"):
        with open(inject._seen_path(session), encoding="utf-8") as f:
            return json.load(f)

    def test_fires_then_cools_then_refires_after_window(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        first = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(first["jit"], ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 1)
        for i in range(inject.COOLDOWN_TURNS):  # turns 2..16: cooled
            self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [],
                             "turn %d must be cooled" % (i + 2))
        r = self.rows()[-1]  # a suppressed-to-silence turn is still measurable
        self.assertIs(r["silent"], True)
        self.assertEqual(r["suppressed"], ["jit-a"])
        self.assertEqual(r["session"], "s1")
        again = inject.gather(self.PROMPT, session="s1")  # turn 17: window past
        self.assertEqual(again["jit"], first["jit"])
        self.assertNotIn("suppressed", self.rows()[-1])
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 17)  # re-recorded

    def test_cooldown_counts_turns_not_fires(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        inject.gather(self.PROMPT, session="s1")  # fires, turn 1
        for _ in range(inject.COOLDOWN_TURNS - 1):  # turns 2..15: silent, still counted
            self.assertEqual(inject.gather("unrelated words", session="s1")["jit"], [])
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [],
                         "turn 16 is inside the window")
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"],
                         ["PRIOR 0.80 jit-a: a flux fact"], "turn 17 refires")
        self.assertEqual(self.seen()["turn"], 17)

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

    def test_2x_score_escape_refires_through_the_window(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap,quantum")
        inject.gather(self.PROMPT, session="s1")  # fires: score 0.8 (fluxcap df=1)
        self.assertEqual(inject.gather(self.PROMPT, session="s1")["jit"], [])
        # both keywords hit -> score 1.6 = 2.0x the recorded 0.8 -> escapes
        got = inject.gather("tune the fluxcap quantum", session="s1")["jit"]
        self.assertEqual(got, ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertEqual(self.seen()["fired"]["jit-a"], [3, 1.6])  # re-recorded
        # and the refreshed record cools it again, even at the higher score
        self.assertEqual(inject.gather("tune the fluxcap quantum", session="s1")["jit"], [])

    def test_pinned_and_reflex_lanes_exempt(self):
        from helm import reflex
        self.plant_pinned("pin-a", "always truth")
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        reflex.write({"id": "flux-reflex", "steer": "flux steer",
                      "signal": "prompt", "pattern": "fluxcap"})
        first = inject.gather(self.PROMPT, session="s1")
        second = inject.gather(self.PROMPT, session="s1")
        self.assertEqual(second["pinned"], first["pinned"])  # exempt
        self.assertEqual(second["reflex"], ["REFLEX: flux steer"])  # exempt
        self.assertEqual((first["jit"], second["jit"]),
                         (["PRIOR 0.80 jit-a: a flux fact"], []))

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

    def test_unwritable_state_hook_contract_rc0(self):
        g = os.path.join(os.environ["HELM_HOME"], "_global")
        os.makedirs(g)
        with open(os.path.join(g, ".state"), "w") as f:
            f.write("x")  # .state is a FILE: seen-state AND ledger writes raise
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        for _ in range(2):  # no cooldown possible -> fires every turn, rc 0
            rc, out, err = self.run_inject(["--hook-json"],
                                           stdin_text=self.hook_stdin(self.PROMPT))
            self.assertEqual(rc, 0)
            self.assertIn("jit-a", out)
            self.assertEqual(err, "")

    def test_stale_files_pruned_and_expired_window_refires(self):
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        os.makedirs(inject._seen_dir())
        stale = os.path.join(inject._seen_dir(), "old-session.json")
        with open(stale, "w") as f:
            f.write("{}")
        os.utime(stale, (1, 1))  # epoch-old: beyond SEEN_TTL
        pk.write_json(inject._seen_path("s1"),
                      {"v": 1, "ts": pk.now_ts(), "turn": 40,
                       "fired": {"jit-a": [1, 0.8]}})  # fired 39 turns ago
        got = inject.gather(self.PROMPT, session="s1")["jit"]
        self.assertEqual(got, ["PRIOR 0.80 jit-a: a flux fact"])
        self.assertFalse(os.path.exists(stale), "stale session file must be pruned")
        self.assertEqual(self.seen()["fired"]["jit-a"][0], 41)

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
        with open(inject._ledger_path(), encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
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
        with open(inject._ledger_path(), encoding="utf-8") as f:
            return [json.loads(l) for l in f.read().splitlines()]

    def test_digest_leads_the_pinned_lane(self):
        self.plant_profile()
        self.plant_pinned("pin-a", "always truth")
        got = inject.gather("anything")["pinned"]
        self.assertEqual(got, ["WHO operator: expert operator",
                               "WHO guidance: keep it short; batch deploys",
                               "PREMISE pin-a: always truth"])
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["pinned"], ["who:operator", "pin-a"])
        self.assertEqual(r["bytes"]["pinned"], sum(len(l) for l in got))
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
        with mock.patch.object(inject, "PINNED_BUDGET", 40):
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

    def test_explain_renders_who_counts_candidate_no_ledger(self):
        self.plant_profile()
        self.plant_pinned("pin-a", "always truth")
        rc, out, _ = self.run_inject(["--explain"], stdin_text="anything")
        self.assertEqual(rc, 0)
        self.assertIn("pinned (2 candidates, budget %dB):" % inject.PINNED_BUDGET, out)
        self.assertIn("+ WHO operator: expert operator", out)
        self.assertIn("+ WHO guidance: keep it short; batch deploys", out)
        self.assertFalse(os.path.exists(inject._ledger_path()),
                         "--explain must never write the ledger")
        with mock.patch.object(inject, "PINNED_BUDGET", 40):
            rc, out, _ = self.run_inject(["--explain"], stdin_text="anything")
        self.assertIn("- who:operator (over budget)", out)
        self.assertNotIn("+ WHO", out)

    def test_cooldown_exempt_fires_every_turn(self):
        self.plant_profile()
        self.plant_jit("jit-a", "a flux fact", "fluxcap")
        first = inject.gather("tune the fluxcap", session="s1")
        second = inject.gather("tune the fluxcap", session="s1")
        self.assertEqual(second["pinned"], first["pinned"])  # WHO exempt
        self.assertIn("WHO operator: expert operator", second["pinned"])
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
            f.writelines(json.dumps(r) + "\n" for r in rotated)
        with open(path, "w", encoding="utf-8") as f:
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
        with open(inject._ledger_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before,
                             "--lane-report must never write the ledger")
        self.assertFalse(os.path.exists(inject._seen_dir()))
        self.assertFalse(os.path.exists(inject._coinage_path()))

    def test_empty_ledger_reports_unfired(self):
        rc, out, _ = self.run_inject(["--lane-report"])
        self.assertEqual(rc, 0)
        self.assertIn("no ledger rows yet", out)


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
             "waiting": ["interview", "2 queued"]}
    QUIET = {"sessions": {"total": 0},
             "knowledge": {"added": [], "updated": [], "retired": [], "drained": 0},
             "waiting": []}

    def hook(self, prompt="what's the plan today?", session="sid-1"):
        return json.dumps({"prompt": prompt, "session_id": session,
                           "hook_event_name": "UserPromptSubmit"})

    def rows(self):
        with open(inject._ledger_path(), encoding="utf-8") as f:
            return [json.loads(l) for l in f.read().splitlines()]

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

    def test_whisper_rides_the_ledger_ids_never_text(self):
        with mock.patch("helm.brief.compose", return_value=self.BRIEF):
            inject.gather("hello", session="s1")
        r = self.rows()[-1]
        self.assertEqual(r["fired"]["whisper"], [inject.WHISPER_ID])
        self.assertGreater(r["bytes"]["whisper"], 0)
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


class _FakeBackend:
    """A shadow-backend test double — the honest injection seam (no network, no
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


class ShadowBackendTest(InjectBase):
    """The pluggable shadow-resolver seam (cf-shadow-backend card): interface
    dispatch, the CF stub's env gate (no network), divergence logging with an
    injected fake, the shadow-off zero-cost path, fail-open on a raising backend,
    --shadow-report both states, and the byte-identical local lane law."""

    def test_interface_local_is_authority(self):
        # the local backend behind the interface reproduces the resolver output
        self.plant_jit("jit-a", "alpha fact", "alpha")
        self.plant_jit("jit-b", "beta fact", "beta")
        self.assertEqual(inject.LOCAL_BACKEND.resolve("tune the alpha now"), ["jit-a"])
        self.assertEqual(inject.LOCAL_BACKEND.name, "local")
        # law 3 (name your source): a mandatory declaration on every backend
        self.assertTrue(inject.LOCAL_BACKEND.source)
        self.assertTrue(inject.CFShadowBackend().source)

    def test_active_shadow_off_by_default_cf_when_configured(self):
        self.assertIsNone(inject._active_shadow())  # unconfigured => OFF
        os.environ["HELM_CF_ENDPOINT"] = "https://example.invalid/query"
        b = inject._active_shadow()
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "cf")

    def test_cf_stub_empty_without_endpoint_no_network(self):
        # the zero-network guard: unconfigured resolve returns [] before urllib
        cf = inject.CFShadowBackend()
        self.assertFalse(cf.configured())
        self.assertEqual(cf.resolve("anything at all"), [])
        # configured-but-empty-prompt also short-circuits before any network
        os.environ["HELM_CF_ENDPOINT"] = "https://example.invalid/query"
        self.assertEqual(cf.resolve("   "), [])

    def test_divergence_logged_with_fake_backend(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        self.plant_jit("jit-b", "beta fact", "beta")
        fake = _FakeBackend(ids=["jit-a", "cf-x"])  # local finds [jit-a]
        inject.gather("tune the alpha now", shadow=fake)
        rows = inject._shadow_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["backend"], "fake")
        self.assertEqual(r["agreed"], ["jit-a"])
        self.assertEqual(r["shadow_only"], ["cf-x"])
        self.assertEqual(r["local_only"], [])
        self.assertEqual(r["local_n"], 1)
        self.assertEqual(r["shadow_n"], 2)

    def test_shadow_ledger_holds_ids_never_prompt_text(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        inject.gather("tune the alpha now", shadow=_FakeBackend(ids=["cf-x"]))
        with open(inject._shadow_ledger_path(), encoding="utf-8") as f:
            self.assertNotIn("tune the alpha", f.read())  # ids ride, not the prompt

    def test_shadow_off_writes_no_ledger_zero_cost(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        inject.gather("tune the alpha now")  # no shadow param, no env => OFF
        self.assertFalse(os.path.exists(inject._shadow_ledger_path()))

    def test_shadow_skips_empty_prompt(self):
        fake = _FakeBackend(ids=["cf-x"])
        inject.gather("   ", shadow=fake)
        self.assertEqual(fake.calls, [])  # never queried on an empty turn
        self.assertFalse(os.path.exists(inject._shadow_ledger_path()))

    def test_fail_open_on_backend_raise(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        sections = inject.gather("tune the alpha now", shadow=_FakeBackend(raises=True))
        self.assertTrue(sections["jit"], "the local lane must survive a shadow raise")
        rows = inject._shadow_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["error"])
        self.assertEqual(rows[0]["backend"], "fake")

    def test_local_lane_byte_identical_with_and_without_shadow(self):
        self.plant_pinned("pin-1", "a pinned truth")
        self.plant_jit("jit-a", "alpha fact", "alpha")
        off = inject.gather("tune the alpha now")
        on = inject.gather("tune the alpha now", shadow=_FakeBackend(ids=["totally", "other"]))
        self.assertEqual(off, on)
        boom = inject.gather("tune the alpha now", shadow=_FakeBackend(raises=True))
        self.assertEqual(off, boom)

    def test_shadow_report_off_state(self):
        rc, out, err = self.run_inject(["--shadow-report"])
        self.assertEqual(rc, 0)
        self.assertIn("shadow off", out)
        self.assertIn("HELM_CF_ENDPOINT", out)
        self.assertEqual(err, "")

    def test_shadow_report_renders_divergence(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        fake = _FakeBackend(ids=["jit-a", "cf-x", "cf-y"])
        inject.gather("tune the alpha now", shadow=fake)
        inject.gather("tune the alpha again", shadow=fake)
        rc, out, err = self.run_inject(["--shadow-report"])
        self.assertEqual(rc, 0)
        self.assertIn("agreed", out)
        self.assertIn("shadow-only", out)
        self.assertIn("cf-x", out)  # the concrete hot id, not a vibe
        self.assertIn("2 comparison turns", out)

    def test_shadow_report_no_ledger_row_written(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        inject.gather("tune the alpha now", shadow=_FakeBackend(ids=["cf-x"]))
        before = len(inject._shadow_rows())
        self.run_inject(["--shadow-report"])
        self.assertEqual(len(inject._shadow_rows()), before)  # read-only

    def test_explain_surfaces_active_shadow_read_only(self):
        self.plant_jit("jit-a", "alpha fact", "alpha")
        # off => no shadow line (salience)
        _, off_out, _ = self.run_inject(["--explain"], stdin_text="tune the alpha now")
        self.assertNotIn("shadow: backend", off_out)
        os.environ["HELM_CF_ENDPOINT"] = "https://example.invalid/query"
        _, on_out, _ = self.run_inject(["--explain"], stdin_text="tune the alpha now")
        self.assertIn("shadow: backend cf active", on_out)
        # --explain is a dry look: it never queries and never writes the ledger
        self.assertFalse(os.path.exists(inject._shadow_ledger_path()))


if __name__ == "__main__":
    unittest.main()
