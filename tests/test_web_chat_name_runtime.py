#!/usr/bin/env python3
"""The chat page names nobody: runtime arms for WHO the owner's posts carry.

A page that fills in a name makes a fresh install post under someone else's
name. So the page sends a name only when the owner TYPED one, shows the
server's owner name in the empty box, and marks his rows with it. A post
that names nobody is named by the server with seats.owner_name() (the
server half is tests/test_web_chat_owner_name.py).

The matrix: three states of the configuration as the page meets them
(configured, unnamed, unread) against three paths (the composer, the reply,
the ledger's message-a-seat card). The functions run verbatim under node
(tests/chat_name_runtime_harness.js). Requires node; skipped where node is
unavailable, like every runtime harness here.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "chat_name_runtime_harness.js")
EXTRACT = ["chatTypedName", "chatPostAs", "chatNamed", "chatOwnerStamp",
           "chatRow", "initChat", "sendChat", "sendReact", "sendSeatMsg"]
# the two declarations the functions read, lifted as shipped so the stored
# key under test is the page's own
DECLS = [r"^const CHAT_NAME_KEY = [^\n]*;$", r"^let CHAT_OWNER = [^\n]*;$"]

POSTS = 5     # composer, threaded reply, reaction, seat DM, broadcast


class TheChatPageNamesNobodyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        decls = []
        for rx in DECLS:
            m = re.search(rx, src, re.M)
            assert m, "declaration not found in the assembled web UI: " + rx
            decls.append(m.group(0))
        fns = "\n\n".join(_extract_fn(src, n) for n in EXTRACT)
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        assert "/*__INJECT__*/" in template
        cls.tmp = tempfile.mkdtemp(prefix="helm-chat-name-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(template.replace("/*__INJECT__*/",
                                     "\n".join(decls) + "\n\n" + fns))
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.results = {r["name"]: r["detail"]
                           for r in json.loads(cls.proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _r(self, name):
        self.assertIn(name, self.results,
                      "harness produced no result for %s\nstdout=%r\nstderr=%r"
                      % (name, self.proc.stdout, self.proc.stderr))
        return self.results[name]

    def _no_name_sent(self, d):
        # EVERY PATH RAN, so an empty list cannot pass: five posts reached the
        # recorder, the reply kept its parent, and none of them carried a name.
        self.assertEqual(d["urls"], ["/api/chat", "/api/chat", "/api/chat/react",
                                     "/api/chat/dm", "/api/chat"], d)
        self.assertEqual(d["replyTo"], "row-1", d)
        self.assertEqual(d["sent"], [None] * POSTS,
                         "a post the owner did not name carried a name: %r"
                         % (d["sent"],))
        self.assertEqual(d["stored"], {}, "a name nobody typed was stored")

    # -- the three states, each across all three paths ----------------------

    def test_CONFIGURED_the_box_shows_the_servers_name_and_every_path_leaves_it_to_the_server(self):
        d = self._r("state_configured")
        self.assertEqual(d["shown"], {"value": "", "placeholder": "harbor"})
        self._no_name_sent(d)
        self.assertTrue(d["ownerMarked"], "his own row is not marked as his")
        self.assertFalse(d["wordMarked"])

    def test_UNNAMED_the_box_says_owner_and_no_row_is_claimed(self):
        d = self._r("state_unnamed")
        self.assertEqual(d["shown"], {"value": "", "placeholder": "owner"})
        self._no_name_sent(d)
        # WITH NO NAME THE PAGE MARKS NOTHING. A seat that happens to be
        # called "owner", or a person the page guessed, is not him.
        self.assertFalse(d["ownerMarked"])
        self.assertFalse(d["wordMarked"])

    def test_UNREAD_a_failed_poll_leaves_the_word_and_sends_no_guess(self):
        d = self._r("state_unread")
        self.assertEqual(d["shown"], {"value": "", "placeholder": "owner"})
        self._no_name_sent(d)
        self.assertFalse(d["ownerMarked"])
        self.assertFalse(d["wordMarked"])

    # -- a typed name, the one name the page may send ------------------------

    def test_a_TYPED_name_rides_every_path_is_kept_and_is_dropped_when_cleared(self):
        d = self._r("typed")
        self.assertEqual(d["sent"], ["skipper"] * 4 + [None],
                         "composer, reply, reaction and seat DM carry the "
                         "typed name; the post after clearing carries none")
        self.assertEqual(d["typedStore"], {"helm.postas": "skipper"})
        self.assertEqual(d["kept"], "skipper")
        self.assertTrue(d["typedMarked"])
        self.assertFalse(d["serverNameMarked"],
                         "while he posts as a typed name, that is the name "
                         "his rows carry")
        self.assertEqual(d["afterClear"], {})

    def test_the_old_key_is_dropped_because_it_cannot_say_who_typed_it(self):
        d = self._r("legacy")
        self.assertEqual(d["value"], "")
        self.assertEqual(d["left"], {}, "the old key survived initChat")
        self.assertEqual(d["sent"], [None])


if __name__ == "__main__":
    unittest.main()
