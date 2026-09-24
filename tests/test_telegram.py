"""The owner's phone as a two-way surface — the laws, not the API.

Every arm here doubles `telegram._api`, so nothing reaches the network and
nothing needs a token. The double is ASSERTED IN EFFECT wherever a call is
expected: a patch that silently failed to bind would let an arm "pass" while
proving only that an unconfigured module stays quiet, which is exactly the
vacuous shape this module's opt-out path makes easy to write.
"""
import os
import tempfile
import unittest
from unittest import mock

from helm import telegram

# A SYNTHETIC routing key, deliberately not any live seat's name. A fixture
# that names a real seat reads as a claim about that seat and goes stale the
# moment it is renamed; the routing law under test is indifferent to which
# seat asked.
KEY = "seat-under-test"


class TelegramBase(unittest.TestCase):
    TOKEN = "123:FAKE-NOT-A-REAL-TOKEN"
    CHAT = "999111"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-tg-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self._offset = os.path.join(self.tmp, "telegram-offset.json")
        p = mock.patch.object(telegram, "_offset_path",
                              lambda: self._offset)
        p.start()
        self.addCleanup(p.stop)

    def configure(self, token=None, chat=None):
        """Bind both halves unless a test is deliberately half-configuring."""
        env = {"TELEGRAM_TOKEN": self.TOKEN if token is None else token,
               "TELEGRAM_CHAT_ID": self.CHAT if chat is None else chat}
        return mock.patch.object(telegram.home, "env",
                                 lambda k: env.get(k, ""))

    def api(self, result, ok=True):
        """Double `_api` and record every call so an arm can prove it ran."""
        calls = []

        def fake(method, payload, timeout=None):
            calls.append((method, payload))
            return ok, result
        return mock.patch.object(telegram, "_api", fake), calls


class OptOutTest(TelegramBase):
    def test_unconfigured_makes_NO_network_call_and_is_not_a_failure(self):
        boom, calls = self.api([{"update_id": 1, "message": {
            "message_id": 1, "text": "hi",
            "chat": {"id": int(self.CHAT)}}}])
        # POSITIVE CONTROL FIRST, on the same double and the same fixture: an
        # empty `calls` proves nothing unless this exact wiring is shown able
        # to produce a non-empty one. Without it the arm below passes just as
        # happily when the patch never bound.
        with self.configure(), boom:
            self.assertTrue(telegram.owner_send("x"))
            self.assertEqual(len(telegram.poll()), 1)
        self.assertEqual(len(calls), 2, "the control never reached the API — "
                                        "the absence assertion below would be "
                                        "vacuous")
        calls.clear()
        with mock.patch.object(telegram.home, "env", lambda k: ""), boom:
            self.assertFalse(telegram.configured())
            self.assertTrue(telegram.owner_send("x"),
                            "opt-out is deliberate, never a delivery failure "
                            "— a False here would arm an outbox forever")
            self.assertEqual(telegram.poll(), [])
        self.assertEqual(calls, [], "an opted-out bridge must not touch the "
                                    "network at all")

    def test_HALF_configured_is_opted_out_not_half_working(self):
        # A token with no chat id can neither deliver nor route a reply. The
        # founding rule of notify.py is that a half-working alarm reads as
        # delivered and wakes nobody, so this must answer False, not True.
        with self.configure():
            self.assertTrue(telegram.configured(),
                            "control: both halves present must read True, or "
                            "the False assertions below mean nothing")
        for tok, chat in ((self.TOKEN, ""), ("", self.CHAT)):
            with self.configure(token=tok, chat=chat):
                self.assertFalse(telegram.configured())


class SendTest(TelegramBase):
    def test_the_reply_footer_carries_the_routing_key(self):
        send, calls = self.api({"message_id": 1})
        with self.configure(), send:
            self.assertTrue(telegram.owner_send("body", reply_key=KEY))
        self.assertEqual(len(calls), 1, "the _api double never ran — this arm "
                                        "would otherwise prove nothing")
        method, payload = calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("reply to answer " + KEY, payload["text"],
                      "without the footer a free-text reply has no route")
        self.assertEqual(payload["chat_id"], self.CHAT)

    def test_an_overlong_body_is_truncated_not_dropped(self):
        send, calls = self.api({"message_id": 1})
        with self.configure(), send:
            telegram.owner_send("x" * 9000)
        self.assertEqual(len(calls), 1)
        text = calls[0][1]["text"]
        self.assertLessEqual(len(text), telegram.MAX_BODY + 32)
        self.assertIn("truncated", text,
                      "silent truncation would let a card lose its options "
                      "with no sign on the owner's phone")

    def test_a_failed_send_reports_False_so_an_outbox_stays_armed(self):
        ok_send, _ = self.api({"message_id": 1}, ok=True)
        with self.configure(), ok_send:
            self.assertTrue(telegram.owner_send("body"),
                            "control: the identical call must report True when "
                            "the API succeeds — otherwise the False below "
                            "could be any unrelated refusal")
        send, calls = self.api(None, ok=False)
        with self.configure(), send:
            self.assertFalse(telegram.owner_send("body"))
        self.assertEqual(len(calls), 1, "False came from somewhere other than "
                                        "the failing API call")


class PollTest(TelegramBase):
    def _update(self, uid, text, chat=None, parent=None):
        msg = {"message_id": uid, "text": text,
               "chat": {"id": int(chat or self.CHAT)}}
        if parent:
            msg["reply_to_message"] = {"text": parent}
        return {"update_id": uid, "message": msg}

    def test_a_reply_routes_by_its_parents_footer(self):
        parent = "some question\n\n⤷ reply to answer " + KEY
        got, calls = self.api([self._update(7, "yes do it", parent=parent)])
        with self.configure(), got:
            rows = telegram.poll()
        self.assertEqual(len(calls), 1, "the _api double never ran")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "yes do it")
        self.assertEqual(rows[0]["reply_key"], KEY)

    def test_free_text_with_no_parent_still_arrives_unrouted(self):
        # "pretty much ALL responses need the option of a free-text reply" —
        # an unrouted message is the common case (he just types), and must
        # never be discarded for lacking a key.
        got, _ = self.api([self._update(8, "how is the gate going")])
        with self.configure(), got:
            rows = telegram.poll()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["reply_key"])

    def test_a_message_from_another_chat_is_never_accepted(self):
        # The bot token can be added to any chat. Input is authority here —
        # a routed reply becomes a post AS the owner — so the chat id is the
        # only thing that says this came from him.
        #
        # The control is the SAME update with ONLY the chat id changed, so a
        # rejection cannot be credited to a malformed fixture.
        mine, _ = self.api([self._update(9, "do something")])
        with self.configure(), mine:
            self.assertEqual(len(telegram.poll()), 1,
                             "control: this exact row from HIS chat must be "
                             "accepted, or the rejection below proves nothing")
        theirs, _ = self.api([self._update(9, "do something", chat="123456")])
        with self.configure(), theirs:
            self.assertEqual(telegram.poll(), [])

    def test_the_offset_advances_past_what_was_returned(self):
        got, calls = self.api([self._update(41, "a"), self._update(42, "b")])
        with self.configure(), got:
            rows = telegram.poll()
        self.assertEqual(len(rows), 2)
        from helm import pk
        self.assertEqual(pk.read_json(self._offset, None), {"offset": 43},
                         "the cursor must clear exactly the delivered set")

    def test_a_failed_getUpdates_does_NOT_advance_the_cursor(self):
        # At-least-once is the right side to err on: a duplicated reply is
        # visible and annoying, a lost one is invisible — and it is lost from
        # the one person who cannot see that it failed.
        bad, _ = self.api(None, ok=False)
        with self.configure(), bad:
            self.assertEqual(telegram.poll(), [])
        self.assertFalse(os.path.exists(self._offset),
                         "a failed poll wrote a cursor — the owner's next "
                         "reply would be skipped with no trace")
        # The control comes SECOND so the absence above is about a file that
        # had not been written yet, and proves the missing file means "refused
        # to advance" rather than "this path is never written at all".
        good, _ = self.api([self._update(60, "ok")])
        with self.configure(), good:
            self.assertEqual(len(telegram.poll()), 1)
        self.assertTrue(os.path.exists(self._offset),
                        "control: a SUCCESSFUL poll must write this same path, "
                        "or the assertion above was about a path nothing uses")


if __name__ == "__main__":
    unittest.main()
