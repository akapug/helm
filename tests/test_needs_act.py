#!/usr/bin/env python3
"""helm needs_act — the wake classifier's laws, pinned (task/4019 slice A).

The classifier did not exist before this lane, so every arm is RED on the
old tree. Each FYI shape is paired with the ACT twins the spec names as
falsifiers: the owner, a deadline, a failure, an unclassifiable row, and every
direct address of the reader.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import needs_act  # noqa: E402

ENV_KEYS = ("HELM_CHAT_DIR", "HELM_HOME", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_OWNER_NAMES")
_PRIOR = {}


def setUpModule():
    _PRIOR.update({k: os.environ.get(k) for k in ENV_KEYS})
    os.environ["HELM_CHAT_DIR"] = tempfile.mkdtemp()
    os.environ["HELM_HOME"] = tempfile.mkdtemp()
    os.environ["HELM_CHAT_NODE_URL"] = ""
    os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"


def tearDownModule():
    for k in ENV_KEYS:
        if _PRIOR.get(k) is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = _PRIOR[k]

NAMES = ["bee"]


def cap(name, membership="JOINED", error=None):
    """One chat.post addressee capability, the shape the row carries."""
    return {"raw": name, "canonical": None if error else name,
            "error": error, "membership": membership,
            "evidence": "malformed" if error else "populated"}


def to_other(text="@seat-b 9a5c2ff9: meld-diff-applied-3937", **extra):
    row = {"from": "seat-a", "text": text,
           "addressees": [cap("seat-b")]}
    row.update(extra)
    return row


def sweep(*terminals, header=None):
    lines = ["@bee [stale-bot] %d aged or cure-awaiting row(s) on your name "
             "— PROPOSED dispositions below. Reply CONCUR/OVERRULE per line."
             % (len(terminals) if header is None else header)]
    lines += ["%d. abc%d (dispatch, 2d old, past its 3600s deadline, no "
              "visible progress) PROPOSED %s: evidence" % (i, i, t)
              for i, t in enumerate(terminals, 1)]
    return {"from": "stale-bot", "text": "\n".join(lines),
            "addressees": [cap("bee")]}


class AddressedElsewhereTest(unittest.TestCase):
    """Cure 1b: a row stamped for other joined seats only is FYI here."""

    def test_a_row_addressed_to_another_seat_is_fyi(self):
        self.assertFalse(needs_act.needs_act(to_other(), NAMES))

    def test_unclassifiable_rows_are_act(self):
        for row in (None, {}, {"text": None}, {"text": ""}, {"text": 42},
                    {"from": "x", "text": "plain chatter, no stamp"},
                    {"from": "x", "text": "@x", "addressees": "garbage"},
                    {"from": "x", "text": "@x", "addressees": ["garbage"]}):
            self.assertTrue(needs_act.needs_act(row, NAMES), row)

    def test_an_addressee_that_did_not_resolve_to_a_joined_seat_is_act(self):
        for c in (cap("ghost", membership="ABSENT"),
                  cap("ghost", membership="UNKNOWN"),
                  cap("bad name", membership="UNKNOWN", error="malformed")):
            row = to_other("@ghost hi", addressees=[c])
            self.assertTrue(needs_act.needs_act(row, NAMES), c)

    def test_an_addressee_naming_this_seat_or_its_alias_is_act(self):
        self.assertTrue(needs_act.needs_act(
            to_other(addressees=[cap("seat-b"), cap("bee")]), NAMES))
        self.assertTrue(needs_act.needs_act(
            to_other(addressees=[cap("bee-old")]), ["bee", "bee-old"]))

    def test_a_later_mention_of_this_seat_is_act(self):
        self.assertTrue(needs_act.needs_act(
            to_other("@seat-b abc: diff applied — @bee sanity-check it"),
            NAMES))

    def test_a_reply_to_this_seat_is_act(self):
        self.assertTrue(needs_act.needs_act(to_other(rfrom="bee"), NAMES))

    def test_an_all_broadcast_is_act(self):
        self.assertTrue(needs_act.needs_act(
            to_other("@all and @seat-b: standup"), NAMES))

    def test_an_owner_row_is_act(self):
        self.assertTrue(needs_act.needs_act(
            to_other("@seat-b hold all lands", **{
                "from": "daria", "origin": "web"}), NAMES))

    def test_deadline_and_failure_rows_are_act(self):
        for text in ("@seat-b the deadline is 17:00 for this land",
                     "@seat-b land due by noon",
                     "@seat-b the gate FAILED on 3926",
                     "@seat-b train REFUSED, main is RED",
                     "@seat-b lease expires in 5 min"):
            self.assertTrue(needs_act.needs_act(to_other(text), NAMES), text)

    def test_the_row_is_never_mutated(self):
        row = to_other()
        before = repr(row)
        self.assertFalse(needs_act.needs_act(row, NAMES))
        self.assertEqual(repr(row), before)


class StalebotSweepTest(unittest.TestCase):
    """Cure 1c: an all-keep sweep is FYI; any other proposal is ACT."""

    def test_an_all_keep_sweep_is_fyi(self):
        self.assertFalse(needs_act.needs_act(
            sweep("still-live-keep", "still-live-keep"), NAMES))

    def test_a_sweep_with_any_terminal_proposal_is_act(self):
        for word in ("supersede-candidate", "cancel-with-reason",
                     "retip-candidate", "reanchor-needed",
                     "redispatch-candidate", "redispatch-by-proxy",
                     "source-unavailable", "source-clean-rehold",
                     "source-clean-close", "some-future-word"):
            self.assertTrue(needs_act.needs_act(
                sweep("still-live-keep", word), NAMES), word)

    def test_a_capped_sweep_with_unshown_rows_is_act(self):
        """The header counts 14 rows, 2 are shown: the rest are unknown."""
        self.assertTrue(needs_act.needs_act(
            sweep("still-live-keep", "still-live-keep", header=14), NAMES))

    def test_an_unparseable_sweep_is_act(self):
        self.assertTrue(needs_act.needs_act(
            {"from": "stale-bot", "text": "@bee [stale-bot] <garbled>"},
            NAMES))

    def test_the_sweep_shape_from_another_sender_is_act(self):
        row = sweep("still-live-keep")
        row["from"] = "not-the-bot"
        self.assertTrue(needs_act.needs_act(row, NAMES))

    def test_the_stalebot_classifier_matches_the_real_digest(self):
        """Pinned against stalebot.digest_text itself, not a hand copy."""
        from helm import stalebot
        it = {"id": "abc123", "kind": "task", "why_aged": "untouched 4d",
              "evidence": "still live", "door": None}
        keep = dict(it, terminal=stalebot.KEEP)
        close = dict(it, terminal=stalebot.SUPERSEDE, id="def456")
        row = {"from": stalebot.BOT}
        row["text"] = stalebot.digest_text("bee", [keep, dict(keep)])
        self.assertFalse(needs_act.needs_act(row, NAMES), row["text"])
        row["text"] = stalebot.digest_text("bee", [keep, close])
        self.assertTrue(needs_act.needs_act(row, NAMES), row["text"])


if __name__ == "__main__":
    unittest.main()
