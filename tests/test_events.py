#!/usr/bin/env python3
"""events-journal tests — mutation receipts through ONE chokepoint (pk.event):
every store/drain writer appends one {v, ts, actor, verb, target, summary}
line to _global/.state/events.jsonl AFTER its write lands. The fire-ledger
laws apply (O(1) append, 5MB one-generation rotation, fail-open — journal
trouble never fails the write it describes); receipts, never truth. Hermetic:
HELM_HOME / HELM_ADOPTED_DIR are tmp dirs; actor env is cleared so a real
CLAUDE_SESSION_ID from the harness never leaks into assertions."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import drain, home, pk, store  # noqa: E402

TS = "2026-07-19T00:00:00Z"
ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_ACTOR", "CLAUDE_SESSION_ID")


class EventsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-events-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        try:
            with open(pk.events_path(), encoding="utf-8") as f:
                return [json.loads(l) for l in f.read().splitlines()]
        except OSError:
            return []

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()


class SeamTest(EventsBase):
    def test_row_shape_summary_cap_and_append(self):
        self.assertTrue(pk.event("store.add", "x-law", "s" * 300, actor="sess-1"))
        pk.event("store.retire", "x-law", "done")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual((r["v"], r["actor"], r["verb"], r["target"]),
                         (1, "sess-1", "store.add", "x-law"))
        self.assertEqual(r["summary"], "s" * 200)  # capped, never a blob
        self.assertTrue(r["ts"])

    def test_actor_env_fallback_chain(self):
        pk.event("v", "t", "s")
        os.environ["CLAUDE_SESSION_ID"] = "claude-sess"
        pk.event("v", "t", "s")
        os.environ["HELM_ACTOR"] = "agent-z"  # HELM_ACTOR beats the session id
        pk.event("v", "t", "s")
        self.assertEqual([r["actor"] for r in self.rows()],
                         ["cli", "claude-sess", "agent-z"])

    def test_rotation_at_5mb_one_generation(self):
        path = pk.events_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("x" * (pk.EVENTS_MAX + 1))
        pk.event("store.add", "y", "post-rotation")
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertEqual(os.path.getsize(path + ".1"), pk.EVENTS_MAX + 1)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "post-rotation")

    def test_fail_open_unwritable_state_dir(self):
        state = os.path.dirname(pk.events_path())
        os.makedirs(state)
        os.chmod(state, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: os.path.isdir(state) and os.chmod(state, 0o755))
        self.assertFalse(pk.event("store.add", "x", "s"))  # no raise, ever
        # the write the receipt describes still succeeds end-to-end
        rc, out, _ = self.run_cli(["add", "prior", "x-law | statement | 0.7"])
        self.assertEqual(rc, 0)
        self.assertIn("LIVE 'x-law'", out)


class ChokepointTest(EventsBase):
    """One receipt per writer verb — the chokepoint is adopted, not optional."""

    def test_store_writer_verbs_each_leave_one_receipt(self):
        for spec in (["add", "prior", "x-law | belief | 0.7 | xkw"],
                     ["add", "premise", "y-truth | certain | ykw"],
                     ["add", "lexicon", "youable | able to be you"],
                     ["add", "heuristic", "swarmify | split it | swarm"],
                     ["add", "reference", "dregg | crown | https://x.test"],
                     ["evidence", TS, "x-law", "0.1", "held up"],
                     ["supersede", TS, "x-law", "y-truth", "sharper"],
                     ["retire", TS, "y-truth", "over"]):
            rc, _, err = self.run_cli(spec)
            self.assertEqual(rc, 0, err)
        self.assertEqual([(r["verb"], r["target"]) for r in self.rows()],
                         [("store.add", "x-law"), ("store.add", "y-truth"),
                          ("store.add", "youable"), ("store.add", "swarmify"),
                          ("store.add", "dregg"),
                          ("store.evidence", "x-law"),
                          ("store.supersede", "x-law"),
                          ("store.retire", "y-truth")])

    def test_demote_and_undo_receipts(self):
        store.write_prior({"id": "pin-a", "statement": "always on",
                           "confidence": 1.0, "pin": "true", "stated_ts": TS})
        rc, _, _ = self.run_cli(["demote", "pin-a", "starved 130/130 rows"])
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_cli(["demote", "pin-a", "--undo", "needed after all"])
        self.assertEqual(rc, 0)
        self.assertEqual([(r["verb"], r["target"]) for r in self.rows()],
                         [("store.demote", "pin-a"), ("store.undemote", "pin-a")])
        self.assertIn("always -> jit", self.rows()[0]["summary"])

    def test_drain_apply_and_rekey_receipts(self):
        mem = os.environ["HELM_ADOPTED_DIR"]
        pk.atomic_write(os.path.join(mem, "feedback-atomic.md"),
                        '---\nname: feedback-atomic\ndescription: "torn writes '
                        'never land"\nmetadata:\n  node_type: memory\n'
                        '  type: feedback\n---\n\nbody\n')
        pk.write_json(home.registry_path(), {"version": 1, "projects": {}})
        drain.apply(drain.classify(mem), mem)
        rows = self.rows()
        self.assertEqual(rows[-1]["verb"], "drain.apply")
        self.assertIn("1 action routed", rows[-1]["summary"])
        # dry-run rekey: no receipt; --apply with changes: one receipt
        drain.rekey()
        self.assertEqual(len(self.rows()), len(rows))
        r = drain.rekey(apply=True)
        self.assertEqual(r["rekeyed"], 1)
        self.assertEqual(self.rows()[-1]["verb"], "drain.rekey")


class ReadSideTest(EventsBase):
    def test_events_renders_recent_trail_with_limit(self):
        for i in range(4):
            pk.event("store.add", "id-%d" % i, "statement %d" % i, actor="s-%d" % i)
        rc, out, _ = self.run_cli(["events", "--limit", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("helm store events (last 2):", out)
        self.assertNotIn("id-1", out)
        self.assertIn("id-2", out)
        self.assertIn("id-3", out)
        self.assertIn("store.add", out)
        self.assertIn("s-3", out)
        rc, out, _ = self.run_cli(["events"])  # default window
        self.assertIn("(last 4)", out)

    def test_read_spans_rotated_generation(self):
        path = pk.events_path()
        os.makedirs(os.path.dirname(path))
        with open(path + ".1", "w") as f:
            f.write(json.dumps({"v": 1, "ts": TS, "actor": "cli",
                                "verb": "store.add", "target": "old-row",
                                "summary": "pre-rotation"}) + "\n")
        pk.event("store.retire", "new-row", "post-rotation")
        got = pk.read_events(5)
        self.assertEqual([r["target"] for r in got], ["old-row", "new-row"])

    def test_empty_journal_and_garbled_lines(self):
        rc, out, _ = self.run_cli(["events"])
        self.assertEqual(rc, 0)
        self.assertIn("no mutation receipts yet", out)
        path = pk.events_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("not json\n" + json.dumps({"v": 1, "ts": TS, "actor": "cli",
                                               "verb": "v", "target": "t",
                                               "summary": "s"}) + "\n")
        self.assertEqual(len(pk.read_events(10)), 1)  # garbled skipped, never fatal
        rc, _, err = self.run_cli(["events", "--limit", "nope"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
