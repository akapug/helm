#!/usr/bin/env python3
"""Fleet notes are headlines first; detail and destination stay one click away.

Runs the ACTUAL note renderer helpers lifted from the assembled web UI under node, so a
browser edit that puts the body inline or admits an active URL scheme fails in
the gate rather than on the owner's homepage. Node is optional on non-web hosts.
"""
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

SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const noteAgo = _iso => "now";
"""

DRIVER = r"""
const out = {
  collapsed: noteRowHTML({key:"landed", by:"codex-2", ts:"x", headline:"Gate green", detail:"Open the full receipt", goto:"ledger"}),
  board: noteRowHTML({key:"board", headline:"Landed lane", detail:"Exact result", goto:"board"}),
  unsafe: noteRowHTML({key:"bad", headline:"Do not run", goto:"javascript:alert(1)"}),
  external: noteRowHTML({key:"runbook", headline:"Owner action", goto:"https://example.test/runbook?a=1&b=2"}),
};
console.log(JSON.stringify(out));
"""


SEC_SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const noteAgo = _iso => "some time ago";
let CARD = null;
const $ = _sel => CARD;
const showView = () => {};
function freshCard() {
  return {innerHTML: "", shown: false,
          classList: {add(){CARD.shown = true;}, remove(){CARD.shown = false;}},
          querySelectorAll: () => []};
}
"""

SEC_DRIVER = r"""
const N = (key, stale) => ({key, by: "seat", ts: "2026-08-05T00:00:00Z",
                           headline: key + " headline", stale});
function render(notes, cap) {
  CARD = freshCard();
  notesSec({notes, cap});
  return {html: CARD.innerHTML, shown: CARD.shown};
}
const out = {
  mixed: render([N("live", false), N("old_a", true), N("old_b", true)], 64),
  all_fresh: render([N("live", false), N("also", false)], 64),
  all_stale: render([N("old_a", true), N("old_b", true)], 64),
  empty: render([], 64),
};
console.log(JSON.stringify(out));
"""


class FleetNotesFreshnessRenderTest(unittest.TestCase):
    """#234: notesSec run VERBATIM under node with a stub DOM. The owner asked
    twice why a live note was invisible among week-old ones; these assert the
    WORDS and the STRUCTURE he ends up looking at, not that the function runs."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n)
                          for n in ("noteGoto", "noteRowHTML", "notesSec"))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-notesec-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(SEC_SUPPORT + "\n" + fns + "\n" + SEC_DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        p = subprocess.run([cls.node, cls.path], capture_output=True,
                           text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        cls.r = json.loads(p.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_a_live_note_is_not_buried_among_older_ones(self):
        """The owner's actual complaint. The fresh note renders ABOVE the fold
        and the header counts what is NEW, not the total."""
        html = self.r["mixed"]["html"]
        self.assertIn("1 new note", html)
        self.assertIn("2 older", html)
        self.assertNotIn("3 left for you", html)
        # the live note is outside the fold, the old ones inside it
        fold = html.index("<details")
        self.assertLess(html.index("live headline"), fold,
                        "the live note was rendered inside the older fold")
        self.assertGreater(html.index("old_a headline"), fold)

    def test_older_notes_are_kept_and_reachable_never_deleted(self):
        html = self.r["mixed"]["html"]
        for key in ("old_a", "old_b"):
            self.assertIn(key + " headline", html,
                          "%s vanished from the card entirely" % key)
        self.assertIn("kept, not deleted", html)

    def test_a_card_with_only_old_notes_opens_the_fold(self):
        """Collapsing EVERY note would leave a card reading empty while notes
        exist — the same disappearance this lane exists to end, arrived at from
        the other side."""
        html = self.r["all_stale"]["html"]
        self.assertIn("<details", html)
        self.assertIn(" open>", html.replace('"', ""),
                      "every note was stale and the fold stayed shut")
        self.assertIn("nothing new", html)
        self.assertTrue(self.r["all_stale"]["shown"], "the card was hidden")

    def test_an_all_fresh_card_has_no_fold_at_all(self):
        html = self.r["all_fresh"]["html"]
        self.assertIn("2 new notes", html)
        self.assertNotIn("<details", html)
        self.assertNotIn("older", html)

    def test_an_empty_payload_still_hides_the_card(self):
        # POSITIVE CONTROL FIRST, on the SAME observable: a card WITH notes is
        # shown and non-empty. Without it, a driver that silently produced
        # nothing would satisfy both assertions below and read as a pass.
        self.assertTrue(self.r["mixed"]["shown"], "no card is ever shown")
        self.assertNotEqual(self.r["mixed"]["html"], "")
        self.assertFalse(self.r["empty"]["shown"])
        self.assertEqual(self.r["empty"]["html"], "")


class FleetNotesRendererTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        cls.source = web_ui_loader.read_text()
        functions = "\n\n".join(
            _extract_fn(cls.source, name) for name in ("noteGoto", "noteRowHTML"))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-fleetnotes-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(SUPPORT + "\n" + functions + "\n" + DRIVER)
        check = subprocess.run([cls.node, "--check", cls.path],
                               capture_output=True, text=True)
        assert check.returncode == 0, "node --check failed:\n" + check.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        assert cls.proc.returncode == 0, cls.proc.stderr
        cls.rendered = json.loads(cls.proc.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_detail_is_inside_native_details_never_inline(self):
        html = self.rendered["collapsed"]
        self.assertIn('<div class="nheadline">Gate green</div>', html)
        self.assertIn("<details><summary>details</summary>", html)
        self.assertGreater(html.index("Open the full receipt"),
                           html.index("<details>"))
        self.assertLess(html.index("Open the full receipt"),
                        html.index("</details>"))

    def test_board_pointer_routes_to_the_kanban_on_the_work_page(self):
        html = self.rendered["board"]
        self.assertIn('href="#work"', html)
        self.assertIn('data-note-tab="board"', html)
        self.assertIn("go to board", html)
        self.assertIn('$("#tierpipeline").scrollIntoView', self.source)

    def test_active_scheme_is_not_rendered_as_a_pointer(self):  # noqa: VACUOUS_ASSERTION — rendered headline positively proves only the unsafe pointer disappeared
        html = self.rendered["unsafe"]
        self.assertIn('<div class="nheadline">Do not run</div>', html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("ngoto", html)

    def test_external_pointer_is_escaped_and_opens_separately(self):
        html = self.rendered["external"]
        self.assertIn("https://example.test/runbook?a=1&amp;b=2", html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)


if __name__ == "__main__":
    unittest.main()
