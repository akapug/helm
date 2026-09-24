#!/usr/bin/env python3
"""The web chat names the owner from configuration, never from the page.

THE DEFECT THIS FILE PINS: the chat page shipped a person's first name as the
default for its name box, its reactions and the ledger's seat messages. A
fresh install posted every unnamed web message under someone else's name,
and the owner-recognition rails (seats.owner_names) did not know that name,
so his own posts counted as unread to him.

The page now sends a name only when the owner typed one, and the server's
post handlers name every other post with seats.owner_name(). The poll's
reset open reports that name (`owner_name`) so the page can show it and mark
his rows. These arms are the SERVER half, across the three states of the
configuration: an authored owner name, none (derived from git or the login),
and an authored layer that cannot be read. The page half runs under node in
tests/test_web_chat_name_runtime.py.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, registry, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CELL_PROFILE",
            "MELD_AGENT_PROFILE", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "HELM_CHAT_OWNER_NAMES", "MELD_CHAT_OWNER_NAMES",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM")


class OwnerNameBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ownername-")
        prior = {k: os.environ.get(k) for k in ENV_KEYS}
        pin = seats._OWNER_NAME
        cwd = os.getcwd()

        def restore():
            os.chdir(cwd)
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            seats._OWNER_NAME = pin
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.addCleanup(restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""      # transport off: hermetic
        os.chdir(self.tmp)                         # no repository's git identity
        seats._OWNER_NAME = None                   # derive, never a pin
        web._CHAT_OLDER_CACHE.clear()

    def configured(self, name):
        """An authored owner name: the host block of the registry."""
        return mock.patch("helm.registry.authored_host",
                          return_value={"owner_name": name})

    def unconfigured(self, login):
        """Nothing authored and no git user name: the login names him."""
        return (mock.patch("helm.registry.authored_host", return_value={}),
                mock.patch.object(seats, "_git_owner_handle", return_value=None),
                mock.patch("getpass.getuser", return_value=login))

    def unreadable(self, git_handle):
        """The authored layer cannot be read: derivation still answers."""
        return (mock.patch("helm.registry.authored_host",
                           side_effect=registry.AuthoredUnreadable("torn file")),
                mock.patch.object(seats, "_git_owner_handle",
                                  return_value=git_handle))

    def open_(self):
        body, status = web._api_chat({"since": ["0"]})
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", body, body)
        return body

    def posted_names(self):
        rows, _total = chat.read("main")
        return [r.get("from") for r in rows]


class TheResetOpenSaysWhoUnnamedPostsLandUnderTest(OwnerNameBase):

    def test_a_CONFIGURED_name_is_announced_and_every_unnamed_post_lands_under_it(self):
        with self.configured("Harbor"):
            got = self.open_().get("owner_name")
            msg = web._api_chat_post({"text": "first", "room": "main"})[0]["msg"]
            web._api_chat_post({"text": "second", "room": "main",
                                "reply_to": msg.get("id")})
            web._api_chat_react({"emoji": ":tada:", "tts": msg["ts"],
                                 "tfrom": msg["from"], "room": "main"})
        self.assertEqual(got, "harbor", "the reset open did not name the "
                                        "configured owner")
        # THE SEAM, not two facts side by side: the post, the reply and the
        # reaction each named nobody, and each landed under the name the open
        # announced, so the page's mark and the stored rows agree.
        self.assertEqual(self.posted_names(), [got, got, got])

    def test_with_NOTHING_configured_the_derived_login_is_announced(self):
        a, b, c = self.unconfigured("dockhand")
        with a, b, c:
            got = self.open_().get("owner_name")
            web._api_chat_post({"text": "hello", "room": "main"})
        self.assertEqual(got, "dockhand")
        self.assertEqual(self.posted_names(), ["dockhand"])

    def test_an_UNREADABLE_config_degrades_to_derivation_never_to_nobody(self):
        a, b = self.unreadable("quay")
        with a, b, mock.patch("sys.stderr"):
            got = self.open_().get("owner_name")
            web._api_chat_post({"text": "hello", "room": "main"})
        self.assertEqual(got, "quay")
        self.assertEqual(self.posted_names(), ["quay"])

    def test_a_resolver_that_RAISES_leaves_the_key_out_and_the_room_still_reads(self):
        chat.post("a row", "main", who="seat-a")
        # POSITIVE CONTROL FIRST, same room, same call: with a resolver that
        # answers, the key is there. So its absence below is the raise, not a
        # server that never sends it.
        with self.configured("harbor"):
            self.assertEqual(self.open_().get("owner_name"), "harbor")
        with mock.patch.object(seats, "owner_name",
                               side_effect=RuntimeError("resolver down")):
            body = self.open_()
        self.assertNotIn("owner_name", body,
                         "a failed resolver must leave the name out, never "
                         "fill in a guess")
        self.assertEqual([r.get("from") for r in body["lines"]], ["seat-a"])

    def test_only_the_reset_open_carries_it_and_the_incremental_poll_is_untouched(self):
        chat.post("one", "main", who="seat-a")
        with self.configured("harbor"):
            opened = self.open_()
            self.assertEqual(opened.get("owner_name"), "harbor")   # control
            chat.post("two", "main", who="seat-a")
            body, status = web._api_chat({"since": [str(opened["total"])]})
        self.assertEqual(status, 200)
        self.assertEqual([r.get("text") for r in body["lines"]], ["two"])
        self.assertNotIn("owner_name", body,
                         "the incremental poll carries rows[since:] and no "
                         "extra field")
        self.assertNotIn("base", body)

    def test_the_announced_name_is_display_laundered(self):
        with self.configured("harbor\x1b[2J"):
            got = self.open_().get("owner_name")
        self.assertTrue(got, "the control: a name was announced")
        self.assertNotIn("\x1b", got)


if __name__ == "__main__":
    unittest.main()
