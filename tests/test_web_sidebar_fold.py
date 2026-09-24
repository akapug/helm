#!/usr/bin/env python3
"""The sidebar's QUIET-FOLD decisions are pure functions — test them headless.

The owner's ask (2026-08-01): "the meld list needs to be collapsable" and
"the sidebars need to be adjustable width". The load-bearing halves are
decisions, not paint: WHICH room folds into the "quiet melds/channels (N)"
group (chatRoomQuiet/chatSplitQuiet — a meld sleeps at >1h, a channel at
>24h, and unread/current/home can NEVER fold, because folding those hides
exactly what the sidebar exists to surface), HOW a section orders itself
(chatByRecency — most recent conversation first, no-last last), and WHERE a
dragged rail may land (railClamp — min a readable 180px, max half the
viewport).

Like test_web_chat_client_runtime.py these run the ACTUAL source lifted
verbatim from the assembled web UI under node, so a JS edit that flips a
threshold or drops the unread override fails HERE, not in the owner's
browser. Requires node; skipped (not failed) where unavailable."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import web_ui_loader  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

EXTRACT = ["roomType", "chatQuietCut", "chatRoomQuiet", "chatSplitQuiet",
           "chatByRecency", "railClamp"]

DRIVER = r"""
const NOW = Date.parse("2026-08-01T12:00:00Z") / 1e3;
const iso = agoS => new Date((NOW - agoS) * 1e3).toISOString();
const out = [];
const t = (name, fn) => {
  try { out.push({name, pass: true, detail: fn() || {}}); }
  catch (e) { out.push({name, pass: false, detail: String(e && e.stack || e)}); }
};

t("meld_cut_is_1h_channel_cut_is_24h", () => ({
  meldCut: chatQuietCut("meld"), chanCut: chatQuietCut("project"),
  meld2h: chatRoomQuiet({room: "meld-x", last: iso(2 * 3600)}, NOW, []),
  meld30m: chatRoomQuiet({room: "meld-x", last: iso(1800)}, NOW, []),
  chan2h: chatRoomQuiet({room: "build", last: iso(2 * 3600)}, NOW, []),
  chan25h: chatRoomQuiet({room: "build", last: iso(25 * 3600)}, NOW, []),
}));

t("unread_outranks_any_age", () => ({
  meld: chatRoomQuiet({room: "meld-x", last: iso(9e6), owner_unread: 1}, NOW, []),
  chan: chatRoomQuiet({room: "old", last: iso(9e6), owner_unread: 3}, NOW, []),
  aged: chatRoomQuiet({room: "meld-x", last: iso(9e6)}, NOW, []),  // positive control
}));

t("current_main_and_home_never_fold", () => {
  const keep = ["meld-live", "main", "helm"];
  return {folded: keep.map(room => chatRoomQuiet({room, last: iso(9e6)}, NOW, keep))};
});

t("a_room_with_no_last_is_quiet_unless_kept", () => ({
  bare: chatRoomQuiet({room: "meld-empty"}, NOW, []),
  kept: chatRoomQuiet({room: "meld-empty"}, NOW, ["meld-empty"]),
}));

t("split_preserves_every_row", () => {
  const list = [
    {room: "meld-a", last: iso(60)}, {room: "meld-b", last: iso(7200)},
    {room: "build", last: iso(60)}, {room: "attic", last: iso(30 * 3600)},
  ];
  const s = chatSplitQuiet(list, NOW, []);
  return {active: s.active.map(r => r.room), quiet: s.quiet.map(r => r.room),
          total: s.active.length + s.quiet.length};
});

t("recency_sorts_newest_first_no_last_last_input_untouched", () => {
  const list = [{room: "b", last: iso(600)}, {room: "empty"}, {room: "a", last: iso(60)}];
  return {order: chatByRecency(list).map(r => r.room), input: list.map(r => r.room)};
});

t("rail_clamps_180_to_half_viewport", () => ({
  below: railClamp(50, 1200), inRange: railClamp(300, 1200), above: railClamp(5000, 1200),
}));

console.log(JSON.stringify(out));
"""


class TestSidebarFold(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, name) for name in EXTRACT)
        cls.tmp = tempfile.mkdtemp(prefix="helm-sidebar-fold-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(fns + "\n" + DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, cls.path], capture_output=True, text=True, timeout=60)
        cls.proc = proc
        try:
            cls.results = {r["name"]: r for r in json.loads(proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _detail(self, name):
        """The measured values from the node run — every test asserts the
        EFFECT (the concrete decision the function returned), never merely the
        harness's absence of a complaint."""
        self.assertIn(name, self.results,
                      "harness produced no result for %s\nstdout=%r\nstderr=%r"
                      % (name, self.proc.stdout, self.proc.stderr))
        r = self.results[name]
        self.assertTrue(r["pass"], name + ": " + json.dumps(r["detail"]))
        return r["detail"]

    def test_meld_cut_is_1h_channel_cut_is_24h(self):
        d = self._detail("meld_cut_is_1h_channel_cut_is_24h")
        self.assertEqual(d["meldCut"], 3600)
        self.assertEqual(d["chanCut"], 86400)
        self.assertTrue(d["meld2h"], "a meld silent 2h must fold")
        self.assertFalse(d["meld30m"], "a meld silent 30m must stay")
        self.assertFalse(d["chan2h"], "a channel silent 2h must stay")
        self.assertTrue(d["chan25h"], "a channel silent 25h must fold")

    def test_unread_outranks_any_age(self):
        d = self._detail("unread_outranks_any_age")
        # positive control FIRST: the identical room without unread DOES fold,
        # so the two Falses below measure the unread override, not a dead check
        self.assertTrue(d["aged"], "the control room did not fold — age never bit")
        self.assertFalse(d["meld"], "an unread meld folded")
        self.assertFalse(d["chan"], "an unread channel folded")

    def test_current_main_and_home_never_fold(self):
        d = self._detail("current_main_and_home_never_fold")
        self.assertEqual(d["folded"], [False, False, False])

    def test_a_room_with_no_last_is_quiet_unless_kept(self):
        d = self._detail("a_room_with_no_last_is_quiet_unless_kept")
        self.assertTrue(d["bare"], "a room nobody ever spoke in must fold")
        self.assertFalse(d["kept"], "the kept room folded anyway")

    def test_split_preserves_every_row(self):
        d = self._detail("split_preserves_every_row")
        self.assertEqual(d["active"], ["meld-a", "build"])
        self.assertEqual(d["quiet"], ["meld-b", "attic"])
        self.assertEqual(d["total"], 4, "rows lost or invented by the split")

    def test_recency_sorts_newest_first_no_last_last_input_untouched(self):
        d = self._detail("recency_sorts_newest_first_no_last_last_input_untouched")
        self.assertEqual(d["order"], ["a", "b", "empty"])
        self.assertEqual(d["input"], ["b", "empty", "a"], "sort mutated its input")

    def test_rail_clamps_180_to_half_viewport(self):
        d = self._detail("rail_clamps_180_to_half_viewport")
        self.assertEqual(d["below"], 180)
        self.assertEqual(d["inRange"], 300)
        self.assertEqual(d["above"], 600)


if __name__ == "__main__":
    unittest.main()
