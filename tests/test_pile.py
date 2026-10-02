#!/usr/bin/env python3
"""helm.pile tests — the Lego-reflex read-only screen (task/3868).

HERMETIC BY CONSTRUCTION: every section reads one real helm module, so each
test injects a stub for that module into the pile's `from . import <mod>`
path BEFORE the section calls it. No real ~/.helm, no real ledger, and the
git-log section runs against a throwaway temp repo with a real `origin`.
The point is the section-to-line mapping and the degrade contract, not the
source's own correctness (which its own module tests cover).
"""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import pile  # noqa: E402


class TestPure(unittest.TestCase):
    def test_human_seconds(self):
        self.assertEqual(pile._human_seconds(30), "30s")
        self.assertEqual(pile._human_seconds(120), "2m")
        self.assertEqual(pile._human_seconds(7200), "2.0h")

    def test_git_log_since_midnight(self):
        # Real `origin/main` so the verb's own `git log origin/main` command
        # resolves; a bare `origin` with no ref would make the test see empty.
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess
            repo = os.path.join(tmp, "r")
            os.mkdir(repo)
            origin = os.path.join(tmp, "bare.git")
            os.mkdir(origin)
            subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo)
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
            with open(os.path.join(repo, "f"), "w") as f:
                f.write("x")
            env = os.environ.copy()
            subprocess.run(["git", "add", "f"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "merge task/777 landing today"],
                cwd=repo, check=True, env=env)
            # the verb queries `--merges`, so seed a real two-parent merge:
            # create a side branch, commit on it, then merge it into main.
            with open(os.path.join(repo, "g"), "w") as g:
                g.write("y")
            subprocess.run(["git", "add", "g"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "branch", "-q", "side"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "checkout", "-q", "side"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "side task/777 work"],
                cwd=repo, check=True, env=env)
            subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "merge", "-q", "--no-ff", "-m",
                 "merge task/777 landing today", "side"],
                cwd=repo, check=True, env=env)
            subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True, env=env)
            # read it through the vcs seam against the throwaway repo, so the
            # merge we just made is what `--since=midnight` returns.
            with mock.patch("helm.pile._repo_root", return_value=repo):
                out = pile._git_log_since_midnight("--merges --format=%s")
            self.assertIn("task/777", "".join(out))


class TestDegrade(unittest.TestCase):
    def test_cut_by_deadline(self):
        def slow():
            time.sleep(2)
            return ([pile._piece("x")], None, None)
        pieces, reason = pile._section(slow, secs=1)
        self.assertEqual(pieces, [])
        self.assertIn("cut at", reason)

    def test_unknown_on_raise(self):
        def boom():
            raise ValueError("nope")
        pieces, reason = pile._section(boom)
        self.assertEqual(pieces, [])
        self.assertIn("unknown:", reason)

    def test_empty_section_reports_nothing(self):
        pieces, reason = pile._section(lambda: ([], "nothing to show"))
        self.assertEqual(pieces, [])
        self.assertEqual(reason, "unknown: nothing to show")

    def test_unknown_reason_is_prefixed_once(self):
        # A source that already says `unknown: ...` is not stamped again.
        _pieces, reason = pile._section(
            lambda: ([], "unknown: ledger unreadable"))
        self.assertEqual(reason, "unknown: ledger unreadable")


class TestOutputShape(unittest.TestCase):
    def _run(self, sections):
        buf = io.StringIO()
        with mock.patch.object(pile, "_SECTIONS", sections):
            with mock.patch("builtins.print", side_effect=buf.write):
                pile.cmd_pile([])
        return buf.getvalue()

    def test_section_none(self):
        def empty():
            return ([], "nothing to show")
        out = self._run([("ONE (title)", empty)])
        # an unreadable section is still shown, with its reason, and its
        # count is `?`: no placeholder piece is counted as a found one
        self.assertIn("== ONE (title) (?) ==", out)
        self.assertNotIn("(1)", out)
        self.assertIn("unknown: nothing to show", out)

    def test_raising_section_counts_unknown_not_one(self):
        def boom():
            raise OSError("gone")
        out = self._run([("ONE (title)", boom)])
        self.assertIn("== ONE (title) (?) ==", out)
        self.assertIn("unknown: OSError", out)
        self.assertNotIn("(1)", out)

    def test_section_lines(self):
        def two():
            return ([pile._piece("a"), pile._piece("b")], None)
        out = self._run([("ONE (title)", two)])
        self.assertIn("== ONE (title) (2) ==", out)
        self.assertIn("  a", out)

    def test_unknown_reason_rendered(self):
        def dead():
            return ([], "unknown: source failed at 12:00:00")
        out = self._run([("ONE (title)", dead)])
        self.assertIn("  unknown: source failed at 12:00:00", out)

    def test_json_shape(self):
        # HERMETIC: stub sections, so the test never reads the real fleet
        # (the real creds source probes vendor usage endpoints).
        sections = [("ONE", lambda: ([pile._piece("a")], None)),
                    ("TWO", lambda: ([], "ledger unreadable"))]
        buf = io.StringIO()
        with mock.patch.object(pile, "_SECTIONS", sections):
            with mock.patch("builtins.print", side_effect=buf.write):
                pile.cmd_pile(["--json"])
        import json
        data = json.loads(buf.getvalue())
        self.assertIn("sections", data)
        self.assertIn("cut_at", data)
        self.assertEqual(len(data["sections"]), len(sections))
        self.assertEqual(data["sections"][1]["pieces"], [])
        self.assertEqual(data["sections"][1]["reason"],
                         "unknown: ledger unreadable")
        for sec in data["sections"]:
            self.assertIn("reason", sec)


class TestRealSectionsAgainstStubs(unittest.TestCase):
    def _stub(self, name, obj, attr):
        """Patch ONE attribute of ONE helm submodule (`name.<attr>` -> obj),
        restoring it on exit so no module is rebound and the parallel gate
        workers stay isolated. `attr` is the exact function the section calls
        (e.g. "join", "_rows", "cached_snapshot", "owed")."""
        import importlib
        mod = importlib.import_module("helm." + name)
        original = getattr(mod, attr, None)
        patcher = mock.patch.object(mod, attr, obj, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        if original is not None:
            self.addCleanup(setattr, mod, attr, original)
        return patcher

    def test_seats_section(self):
        join = {"gemini": {"can_take_work": True, "holding": 0, "reason": ""}}
        self._stub("seat_usability", lambda: join, "join")
        pieces, _ = pile._seats()
        self.assertEqual([p["name"] for p in pieces], ["gemini"])

    def test_seats_holding_is_excluded(self):  # noqa: VACUOUS_ASSERTION
        # A positive control for the same join shape is test_seats_section;
        # the source filter here is the contract, not a vacuous pass.
        join = {"codex": {"can_take_work": True, "holding": 1, "reason": "work"}}
        self._stub("seat_usability", lambda: join, "join")
        pieces, _ = pile._seats()
        # holding>0 filters the seat out at the source
        self.assertEqual(pieces, [])

    def test_seats_unknown_holding_is_not_called_idle(self):
        # holding None is an UNREAD count, never zero: the seat is named
        # with `holding UNKNOWN`, not presented as free.
        join = {"gemini": {"can_take_work": True, "holding": None,
                           "reason": "usable"}}
        self._stub("seat_usability", lambda: join, "join")
        pieces, _ = pile._seats()
        self.assertEqual([(p["name"], p["detail"]) for p in pieces],
                         [("gemini", "holding UNKNOWN")])

    def test_creds_section(self):
        # Real data (F5): list an account only when it is usable, has at
        # least 50% headroom, AND resets within the next 6 hours — room that
        # will actually be lost. A full account far from its reset strands
        # nothing; a dark family has no room at all.
        now = time.time()
        rows = [
            {
                "usable": True, "headroom": 0.8, "provider": "claude",
                "account": "claude-pro", "resets_at_ms": int((now + 2 * 3600) * 1000),
            },
            {
                "usable": True, "headroom": 0.8, "provider": "codex",
                "account": "codex-pro", "resets_at_ms": int((now + 30 * 3600) * 1000),
            },
            {
                "usable": True, "headroom": 0.2, "provider": "gemini",
                "account": "gemini-pro", "resets_at_ms": int((now + 2 * 3600) * 1000),
            },
        ]
        self._stub("creds", lambda: rows, "_rows")
        pieces, _ = pile._creds()
        names = [(p["name"], p["detail"]) for p in pieces]
        self.assertTrue(any("claude-pro" in n and "80%" in d for n, d in names))
        self.assertFalse(any("codex-pro" in n for n, d in names))
        self.assertFalse(any("gemini-pro" in n for n, d in names))

    def test_resets_section(self):
        now = time.time()
        rows = [
            {"usable": True, "provider": "codex", "account": "a",
             "resets_at_ms": int((now + 60) * 1000)},
            {"usable": True, "provider": "claude", "account": "b",
             "resets_at_ms": int((now + 7200) * 1000)},
        ]
        self._stub("creds", lambda: rows, "_rows")
        pieces, _ = pile._resets()
        self.assertEqual(len(pieces), 1)
        self.assertIn("codex", pieces[0]["name"])

    def test_landed_today_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess
            repo = os.path.join(tmp, "r")
            os.mkdir(repo)
            origin = os.path.join(tmp, "bare.git")
            os.mkdir(origin)
            subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo)
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
            with open(os.path.join(repo, "f"), "w") as f:
                f.write("x")
            env = os.environ.copy()
            subprocess.run(["git", "add", "f"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "merge task/444 landed"],
                cwd=repo, check=True, env=env)
            # the verb queries `--merges`, so seed a real two-parent merge:
            # create a side branch, commit on it, then merge it into main.
            with open(os.path.join(repo, "g"), "w") as g:
                g.write("y")
            subprocess.run(["git", "add", "g"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "branch", "-q", "side"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "checkout", "-q", "side"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "side task/444 work"],
                cwd=repo, check=True, env=env)
            subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "merge", "-q", "--no-ff", "-m",
                 "train500: merge lane task-444-board (task/444: landing "
                 "today)", "side"],
                cwd=repo, check=True, env=env)
            subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True, env=env)
            with mock.patch("helm.pile._repo_root", return_value=repo):
                self._stub("tasks", lambda: [{"id": "task/444", "status": "open"}], "open_rows")
                pieces, _ = pile._landed_today_open()
                self.assertTrue(any("444" in p["name"] for p in pieces))

    def test_landed_today_section_matches_real_subject_and_open_id(self):
        # Real data (F1): a merge subject reads
        # `... (task/3856: ...)` with the token after `task/` inside the
        # subject, and `tasks.open_rows()` ids look like `task/56`, so a
        # found ref `3856` must match the id `task/3856`. The old `startswith`
        # parser never saw the real token and never compared `task/`.
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess
            repo = os.path.join(tmp, "r")
            os.mkdir(repo)
            origin = os.path.join(tmp, "bare.git")
            os.mkdir(origin)
            subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo)
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
            with open(os.path.join(repo, "f"), "w") as f:
                f.write("x")
            env = os.environ.copy()
            subprocess.run(["git", "add", "f"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "merge task/3856 landed"],
                cwd=repo, check=True, env=env)
            with open(os.path.join(repo, "g"), "w") as g:
                g.write("y")
            subprocess.run(["git", "add", "g"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "branch", "-q", "side"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "checkout", "-q", "side"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "side task/3856 work"],
                cwd=repo, check=True, env=env)
            subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True, env=env)
            subprocess.run(
                ["git", "merge", "-q", "--no-ff", "-m",
                 "train521: merge lane effort-high-default-3856 (task/3856: "
                 "every agent starts at HIGH effort ...; P1, ...)", "side"],
                cwd=repo, check=True, env=env)
            subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True, env=env)
            with mock.patch("helm.pile._repo_root", return_value=repo):
                # task/3856 IS open -> listed
                self._stub("tasks", lambda: [{"id": "task/3856", "status": "open"}], "open_rows")
                pieces, _ = pile._landed_today_open()
                self.assertTrue(any("3856" in p["name"] for p in pieces))
                # task/3856 NOT open -> nothing listed
                self._stub("tasks", lambda: [], "open_rows")
                pieces, _ = pile._landed_today_open()
                self.assertEqual(pieces, [])

    def test_landed_today_lists_a_task_once_across_two_merges(self):
        # Real data (F1): two merge subjects naming task/444 today list the
        # task once PER merge; keep one piece per task id, first-seen.
        with mock.patch(
                "helm.pile._git_log_since_midnight",
                return_value=["train500: merge lane task-444-board (task/444: "
                              "landed)",
                              "train501: merge lane task-444-board (task/444: "
                              "again)"]):
            self._stub("tasks", lambda: [{"id": "task/444", "status": "open"}], "open_rows")
            pieces, _ = pile._landed_today_open()
            self.assertEqual([p["name"] for p in pieces], ["task/444"])

    def test_landed_today_attributes_a_lane_only_train_merge(self):
        # task/4119b: a lane-only train merge is attributed to the task its
        # lane serves (`parse_train` reads it from the lane's name), a
        # back-merge (no `trainNNN:` car) is not counted, and an ordinary
        # train merge is unchanged. All through taskhygiene, never the regex.
        with mock.patch(
                "helm.pile._git_log_since_midnight",
                return_value=[
                    "train522: merge lane task-444-board",
                    "merge origin/main into lane/foo (task/5: back into trunk)",
                    "train521: merge lane lane-3856 (task/3856: every agent "
                    "starts at HIGH effort)",
                ]):
            self._stub("tasks",
                       lambda: [{"id": "task/444", "status": "open"},
                                {"id": "task/3856", "status": "open"}],
                       "open_rows")
            pieces, _ = pile._landed_today_open()
            names = {p["name"] for p in pieces}
            self.assertIn("task/444", names)
            self.assertNotIn("task/5", names)
            self.assertIn("task/3856", names)

    def test_landed_today_does_not_count_a_back_merge(self):  # noqa: VACUOUS_ASSERTION — the positive lane-only car asserts the section still lists
        # A back-merge into trunk (`merge origin/main into lane/X (task/N)`)
        # is not a train car, so parse_train returns None and the merge's
        # task is never read as landed today.
        with mock.patch(
                "helm.pile._git_log_since_midnight",
                return_value=[
                    "merge origin/main into lane/foo (task/5: back into trunk)",
                ]):
            self._stub("tasks",
                       lambda: [{"id": "task/5", "status": "open"}],
                       "open_rows")
            pieces, _ = pile._landed_today_open()
            self.assertEqual(pieces, [])

    def _landed(self, lines, open_ids, lanes=({}, None)):
        """`_landed_today_open` over these merge subjects, these open rows
        and this lane-records read, so no real ledger or repository is read."""
        self._stub("tasks", lambda: [{"id": t, "status": "open"}
                                     for t in open_ids], "open_rows")
        self._stub("taskkey", lambda _repo, *a, **k: lanes, "lane_records")
        with mock.patch("helm.pile._git_log_since_midnight",
                        return_value=lines):
            return pile._landed_today_open()

    def test_landed_today_reads_lane_records_as_stale_sweep_does(self):
        # An inferred car (task-N in the lane's name) whose lane now records
        # another task links nothing, as `land_link` refuses it for the
        # sweep; a car that records its own task stands either way.
        pieces, reason = self._landed(
            ["train600: merge lane task-444-board",
             "train601: merge lane task-555-board (task/555: its own)"],
            ["task/444", "task/555"],
            lanes=({"task-444-board": frozenset({"task/9"}),
                    "task-555-board": frozenset({"task/9"})}, None))
        self.assertEqual([p["name"] for p in pieces], ["task/555"])
        self.assertIsNone(reason)

    def test_landed_today_unreadable_lane_records_are_unknown(self):
        # Lane records that cannot be read leave an inferred car's task
        # unknown: the section says so instead of reading a clean empty.
        pieces, reason = self._landed(
            ["train600: merge lane task-444-board"], ["task/444"],
            lanes=({}, "config could not be read"))
        self.assertEqual(pieces, [])
        self.assertTrue(reason.startswith("unknown:"), reason)
        self.assertIn("config could not be read", reason)

    def test_landed_today_unreadable_trunk_log_is_unknown(self):
        pieces, reason = self._landed(None, ["task/444"])
        self.assertEqual(pieces, [])
        self.assertIn("trunk log unreadable", reason)
        _p, shown = pile._section(lambda: (pieces, reason))
        self.assertTrue(shown.startswith("unknown:"), shown)

    def test_git_log_since_midnight_unreadable_is_none(self):  # noqa: VACUOUS_ASSERTION — test_git_log_since_midnight is the positive control on the same read
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("helm.pile._repo_root", return_value=tmp):
                self.assertIsNone(
                    pile._git_log_since_midnight("--merges --format=%s"))

    def test_landed_today_reads_a_lettered_train(self):
        # A recomposed train (train555b) is a land like any other.
        pieces, _ = self._landed(
            ["train555b: merge lane some-lane (task/12: the thing, P1)"],
            ["task/12"])
        self.assertEqual([p["name"] for p in pieces], ["task/12"])

    def test_landed_today_names_a_part_land(self):
        # A car that carried only part of its task leaves the row open for
        # the rest; the piece says a part landed, not that the row is done.
        pieces, _ = self._landed(
            ["train600: merge lane some-lane-4119b (task/4119: part b)"],
            ["task/4119"])
        self.assertEqual([p["detail"] for p in pieces],
                         ["a part landed today, the rest still open"])

    def test_reviewer_patches_section(self):
        # A verdict row with fix polarity is listed; a non-verdict row is not.
        snap = {"r1": {"id": "1", "status": "verdict", "polarity": "fix",
                       "patch_tip": "ab" * 20},
                "r2": {"id": "2", "kind": "build", "verdict": "FIX"}}
        def _owedsnap():
            return snap, False
        def _owe(_s):
            return [r for r in snap.values()]
        self._stub("dispatches", _owedsnap, "snapshot")
        self._stub("dispatches", _owe, "owed")
        pieces, _ = pile._reviewer_patches()
        self.assertEqual([p["name"] for p in pieces], ["row 1"])

    def test_reviewer_patches_section_uses_real_verdict_shape(self):
        # Real data (F2): a snapshot is {id: folded_row}; a FIX ruling is a
        # row with `status == "verdict"` and `polarity == "fix"` (a folded row
        # carries no `verdict` field, so checking one never matches),
        # is only listed while it is the newest row of its chain (a later row
        # in the same chain_root supersedes it), is within the last 7 days,
        # and its name/detail carry lane, recipient, sender, id[:12], task.
        now = time.time()
        def row(r_id, extra):
            return {"id": r_id, "lane": "lane-x", "sender": "seat-a",
                    "recipient": "seat-b", "task": "task/3863",
                    "ts": extra.get("ts", None), "chain_root": extra.get("cr"),
                    "status": extra.get("status", "open"),
                    "polarity": extra.get("pol", None),
                    "patch_tip": "cd" * 20}
        day = 86400
        snap = {
            "r1": row("r1", {"ts": now - day, "cr": "chain1", "status": "verdict",
                             "pol": "fix"}),
            "r2": row("r2", {"ts": now - 2 * day, "cr": "chain1", "status": "held",
                             "pol": "approve"}),
            "r3": row("r3", {"ts": now - day, "cr": "chain2", "status": "verdict",
                             "pol": "fix"}),
            "r4": row("r4", {"ts": now - 8 * day, "cr": "chain3", "status": "verdict",
                             "pol": "fix"}),
        }
        self._stub("dispatches", lambda: (snap, False), "snapshot")
        self._stub("dispatches", lambda _s: list(snap.values()), "owed")
        pieces, _ = pile._reviewer_patches()
        names = {p["name"] for p in pieces}
        details = {p["detail"] for p in pieces}
        # Only the newest verdict in chain1 (r1) is listed; r2 is not a fix,
        # r3 is its own chain but r3 is not the newest — wait, r3 has no newer
        # sibling so it IS the newest of its chain: listed too. r4 is 8 days
        # old -> excluded by the 7-day window.
        self.assertIn("lane-x", names)
        self.assertTrue(any("seat-b" in d for d in details))
        self.assertTrue(any("seat-a" in d for d in details))

    def test_a_fix_with_no_patch_is_not_a_reviewer_patch(self):
        # A FIX verdict that committed no cure (`patch_tip` absent) is a
        # finding: there is nothing for the lane to take.
        now = time.time()
        snap = {
            "with": {"id": "with", "lane": "lane-p", "sender": "seat-a",
                     "recipient": "seat-b", "ts": now - 3600,
                     "chain_root": "c1", "status": "verdict",
                     "polarity": "fix", "patch_tip": "ef" * 20},
            "without": {"id": "without", "lane": "lane-q", "sender": "seat-a",
                        "recipient": "seat-b", "ts": now - 3600,
                        "chain_root": "c2", "status": "verdict",
                        "polarity": "fix"},
        }
        self._stub("dispatches", lambda: (snap, False), "snapshot")
        pieces, _ = pile._reviewer_patches()
        self.assertEqual([p["name"] for p in pieces], ["lane-p"])
        self.assertIn("efefefefefef", pieces[0]["detail"])

    def test_reviewer_patches_section_skips_aged(self):  # noqa: VACUOUS_ASSERTION — the empty result is the whole assertion (both rows filtered); no positive control lives on this test
        # A FIX that a newer row in the same chain supersedes is not listed;
        # a FIX older than 7 days is not listed.
        now = time.time()
        day = 86400
        snap = {
            "old": {"id": "old", "lane": "lane-x", "sender": "seat-c",
                    "recipient": "seat-b", "task": "task/3863",
                    "ts": now - 3 * day, "chain_root": "chainA",
                    "status": "verdict", "polarity": "fix"},
            "new": {"id": "new", "lane": "lane-x", "sender": "seat-c",
                    "recipient": "seat-b", "task": "task/3863",
                    "ts": now - day, "chain_root": "chainA", "status": "held",
                    "polarity": "supersede"},
            "aged": {"id": "aged", "lane": "lane-y", "sender": "seat-c",
                     "recipient": "seat-b", "task": "task/3863",
                     "ts": now - 8 * day, "chain_root": "chainB",
                     "status": "verdict", "polarity": "fix"},
        }
        self._stub("dispatches", lambda: (snap, False), "snapshot")
        self._stub("dispatches", lambda _s: list(snap.values()), "owed")
        pieces, _ = pile._reviewer_patches()
        self.assertEqual(pieces, [])

    def test_today_rulings_only_lists_today(self):
        # Real data (F3): cards carry an ISO timestamp (`%Y-%m-%dT%H:%M:%SZ`,
        # UTC); only cards at or after local midnight today are listed. The
        # "today" card is stamped at `now` (UTC) and the "old" one two days
        # before — local midnight always falls between them, so the assertion
        # holds in any timezone.
        now = time.time()
        day = 86400
        def iso(t):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        cards = {
            "rid_today": {"ts": iso(now), "kind": "decision",
                          "verdict": {"choice": "1", "ts": iso(now)}},
            "rid_old": {"ts": iso(now - 2 * day), "kind": "decision",
                        "verdict": {"choice": "1", "ts": iso(now - 2 * day)}},
        }
        self._stub("ownerasks", lambda: (cards, False), "decisions_snapshot")
        pieces, _ = pile._today_rulings()
        names = {p["name"] for p in pieces}
        self.assertTrue(any("rid_today" in n for n in names))
        self.assertFalse(any("rid_old" in n for n in names))

    def test_empty_section_reports_none_not_unknown(self):
        # Real data (F4): a source that returns no pieces is a MEASURED empty,
        # not an unreadable one — it prints `(0)` and `none`, never `unknown`.
        pieces, reason = pile._section(lambda: ([], None))
        self.assertEqual(pieces, [])
        self.assertFalse("unknown" in reason)

    def test_cut_section_still_prints_unknown(self):
        pieces, reason = pile._section(lambda: ([], "unknown: source failed"))
        self.assertIn("unknown", reason)

    def test_today_rulings_section(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
        cards = {"rid1": {"ts": stamp, "kind": "decision",
                          "verdict": {"choice": "1", "label": "Yes",
                                      "ts": stamp}}}
        def _dsnap():
            return cards, False
        self._stub("ownerasks", _dsnap, "decisions_snapshot")
        pieces, _ = pile._today_rulings()
        self.assertTrue(any("rid1" in p["name"] for p in pieces))

    def test_today_rulings_read_the_verdict_time_not_the_filing_time(self):
        # A card's top-level `ts` is when it was FILED. Filed today and still
        # open is a question, not a ruling; filed days ago and ruled today is
        # today's ruling.
        now = time.time()
        def iso(t):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        cards = {
            "open_today": {"ts": iso(now), "kind": "decision",
                           "status": "open", "verdict": None},
            "ruled_today": {"ts": iso(now - 3 * 86400), "kind": "decision",
                            "status": "decided",
                            "verdict": {"choice": "2", "label": "Text only",
                                        "ts": iso(now)}},
        }
        self._stub("ownerasks", lambda: (cards, False), "decisions_snapshot")
        pieces, _ = pile._today_rulings()
        self.assertEqual([p["name"] for p in pieces],
                         ["owner ruling ruled_today"])
        self.assertIn("Text only", pieces[0]["detail"])


class TestTodayBoundary(unittest.TestCase):
    def test_midnight_is_local_midnight_west_of_utc(self):
        # In a zone west of UTC, local midnight must read back as 00:00 local
        # on today's date; reading the local wall clock as UTC moved it to the
        # evening of the day before.
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "EST+5"
        time.tzset()
        lt = time.localtime(pile._midnight_s())
        today = time.localtime()
        self.assertEqual((lt.tm_hour, lt.tm_min, lt.tm_sec), (0, 0, 0))
        self.assertEqual((lt.tm_year, lt.tm_yday),
                         (today.tm_year, today.tm_yday))


if __name__ == "__main__":
    unittest.main()
