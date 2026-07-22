#!/usr/bin/env python3
"""helm chat replies — the parent pointer, the parent-bound signed digest, the
one-level render, and the law that threading changes NOTHING about who a
message wakes. Hermetic: tmp HELM_HOME + HELM_CHAT_DIR, transport disabled
(the signing seam is mocked, like test_chat_v2)."""
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

from helm import chat, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_LOG", "MELD_CHAT_LOG", "HELM_CELL_BIN", "MELD_CELL_BIN")

SENT = {"sent": True, "turn_hash": "t" * 64, "receipt_hash": "r" * 64,
        "chain_index": 7}


class ReplyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-reply-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # hermetic: no signing probe

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, room="main"):
        return chat.read(room)[0]

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(list(args))
        return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 1. the row: an additive parent pointer that reuses the stable row id
# ---------------------------------------------------------------------------

class ReplyRowTest(ReplyBase):
    def test_reply_to_roundtrips_the_parent_id_and_identity(self):
        p = chat.post("the parent", who="alice")
        r = chat.post("the child", who="bob", reply_to=p["id"])
        self.assertEqual(r["reply_to"], p["id"])
        self.assertEqual((r["rts"], r["rfrom"]), (p["ts"], "alice"))
        # it SURVIVES the file: one read gets the same pointer back
        on_disk = self.rows()[-1]
        self.assertEqual(on_disk["reply_to"], p["id"])
        self.assertTrue(chat.is_reply(on_disk))
        self.assertFalse(chat.is_reply(self.rows()[0]))

    def test_plain_post_row_is_untouched(self):
        """ADDITIVE: a non-reply row gains no key at all (old readers, old
        cursors, old fingerprints all see exactly the v2 row)."""
        m = chat.post("plain", who="alice")
        self.assertEqual(sorted(m), ["from", "id", "text", "ts"])

    def test_reply_by_ordinal_and_by_id_prefix(self):
        a = chat.post("first", who="alice")
        b = chat.post("second", who="alice")
        by_ord = chat.post("re first", who="bob", reply_to="1")
        by_pre = chat.post("re first again", who="bob", reply_to=a["id"][:6])
        by_last = chat.post("re the newest", who="bob", reply_to="-1")
        self.assertEqual(by_ord["reply_to"], a["id"])
        self.assertEqual(by_pre["reply_to"], a["id"])
        self.assertEqual(by_last["reply_to"], by_pre["id"])   # -1 = latest
        self.assertEqual(chat.post("re second", who="bob",
                                   reply_to="2")["reply_to"], b["id"])

    def test_an_all_digit_id_prefix_is_an_id_not_an_ordinal(self):
        """Row ids are hex — ~6% of 6-char prefixes are all digits. Identity
        resolves FIRST, so copying one can never silently mean 'message N'."""
        chat._ensure_dir()
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "w", encoding="utf-8") as f:
            for i, rid in enumerate(("658511145301", "aa11bb22cc33")):
                f.write(json.dumps({"ts": "2026-07-01T00:00:0%dZ" % i,
                                    "from": "alice", "text": "row %d" % i,
                                    "id": rid}) + "\n")
        r = chat.post("re", who="bob", reply_to="658511")
        self.assertEqual(r["reply_to"], "658511145301")
        self.assertEqual(r["rfrom"], "alice")
        # a plain small number is still the ordinal it has always been
        self.assertEqual(chat.post("re2", who="bob",
                                   reply_to="2")["reply_to"], "aa11bb22cc33")

    def test_reply_to_a_pre_id_row_binds_ts_and_from(self):
        """A row written before the id law has no id — the (ts, from) pair is
        the fallback identity, exactly what reaction rows already use."""
        chat._ensure_dir()
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "w", encoding="utf-8") as f:   # a TRUE v1 row: no id
            f.write(json.dumps({"ts": "2026-07-01T00:00:00Z", "from": "old",
                                "text": "ancient"}) + "\n")
        r = chat.post("answering the ancestor", who="bob", reply_to="1")
        self.assertEqual((r["reply_to"], r["rfrom"], r["rtext"]),
                         ("", "old", "ancient"))
        idx = chat.index_rows(self.rows())
        self.assertEqual(chat.quote_of(r, idx), ("old", "ancient"))

    def test_unresolvable_ref_still_lands_as_an_orphan(self):
        r = chat.post("shouting into the void", who="bob", reply_to="deadbeef99")
        self.assertEqual(r["reply_to"], "deadbeef99")
        self.assertNotIn("rts", r)
        idx = chat.index_rows(self.rows())
        self.assertIsNone(chat.parent_of(r, idx))
        self.assertEqual(chat.quote_of(r, idx), ("?", "(parent rotated out)"))
        self.assertIn("(parent rotated out)", chat._fmt(r, idx=idx))

    def test_a_reaction_is_never_a_reply(self):
        chat.post("target", who="alice")
        row, err = chat.react(1, ":fire:", who="bob")
        self.assertIsNone(err)
        self.assertFalse(chat.is_reply(row))
        self.assertIsNone(chat.quote_of(row, chat.index_rows(self.rows())))


# ---------------------------------------------------------------------------
# 2. the signature: the digest BINDS the parent — and old rows still verify
# ---------------------------------------------------------------------------

class ReplyDigestTest(ReplyBase):
    def test_plain_post_payload_is_byte_identical_to_v2(self):
        """The backward-compat proof: every signed row already on disk
        recomputes to EXACTLY the payload it signed."""
        legacy = {"ts": "2026-07-01T00:00:00Z", "from": "a1", "text": "hello",
                  "chain": 3}
        self.assertEqual(chat.payload_for(legacy), chat.digest_payload("hello"))
        self.assertTrue(chat.payload_for(legacy).startswith(chat.CHAT_TAG))
        legacy_react = {"ts": "t", "from": "a2", "react": "🎉",
                        "tts": "T", "tfrom": "a1", "chain": 4}
        self.assertEqual(chat.payload_for(legacy_react),
                         chat.digest_payload("react|T|a1|🎉|a2"))

    def test_signed_reply_binds_the_parent(self):
        p = chat.post("the parent", who="alice")
        with mock.patch.object(chat, "_sign_send",
                               return_value=(SENT, None)) as ss:
            r = chat.post("the child", who="bob", profile="bob", sign=True,
                          reply_to=p["id"])
        want = chat.reply_digest(p["id"], p["ts"], "alice", "the child")
        ss.assert_called_once_with(want, "bob")
        self.assertEqual(r["payload"], want)
        self.assertEqual(r["chain"], 7)
        # the SAME text unparented signs a DIFFERENT payload — re-parenting a
        # signed reply is impossible without re-signing
        self.assertNotEqual(want, chat.digest_payload("the child"))

    def test_reply_payload_space_is_disjoint_from_a_plain_post(self):
        """The forgery this design exists to stop: no attacker-chosen TEXT can
        make a plain post produce a reply's digest (what an in-band
        `reply|<id>|…` prefix on the shared tag would have allowed)."""
        d = chat.reply_digest("abc", "T", "alice", "hi")
        self.assertTrue(d.startswith(chat.REPLY_TAG))
        for evil in ("reply|abc|hi", "abc\x1eT\x1ealice\x1ehi",
                     chat.REPLY_TAG + "hi", "chat:reply:b2b:hi"):
            self.assertNotEqual(chat.digest_payload(evil), d)

    def test_field_sliding_cannot_forge_a_parent(self):
        a = chat.reply_digest("ab", "cT", "alice", "hi")
        b = chat.reply_digest("abc", "T", "alice", "hi")
        self.assertNotEqual(a, b)   # the RS separator keeps the fields apart

    def test_literal_separator_cannot_slide_between_author_and_text(self):
        """RS itself is attacker-controlled input too. Without field escaping,
        these two tuples serialize to the same joined bytes and a text edit can
        keep verify green by moving content into rfrom."""
        a = chat.reply_digest("abc", "T", "alice\x1eadmin", "approved")
        b = chat.reply_digest("abc", "T", "alice", "admin\x1eapproved")
        self.assertNotEqual(a, b)

    def test_verify_rejects_separator_field_sliding(self):
        p = chat.post("parent", who="alice\x1eadmin")
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)):
            chat.post("approved", who="bob", profile="bob", sign=True,
                      reply_to=p["id"])
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, encoding="utf-8") as f:
            lines = f.read().split("\n")
        row = json.loads(lines[1])
        row["rfrom"] = "alice"
        row["text"] = "admin\x1eapproved"
        lines[1] = json.dumps(row)
        with open(raw, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.assertEqual(chat.verify()[1]["state"], "MISMATCH")

    def test_verify_flags_a_re_parented_row_and_passes_the_rest(self):
        chat.post("parent one", who="alice")
        p2 = chat.post("parent two", who="alice")
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)):
            chat.post("signed plain", who="bob", profile="bob", sign=True)
            chat.post("signed reply", who="bob", profile="bob", sign=True,
                      reply_to=p2["id"])
        rep = {r["n"]: r for r in chat.verify()}
        self.assertEqual([rep[n]["state"] for n in (1, 2, 3, 4)],
                         ["unsigned", "unsigned", "ok", "ok"])
        # a hand-edited parent pointer no longer recomputes to what was signed
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, encoding="utf-8") as f:
            lines = f.read().split("\n")
        row = json.loads(lines[3])
        row["reply_to"] = "0" * 12          # re-parent under the signature
        lines[3] = json.dumps(row)
        with open(raw, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.assertEqual(chat.verify()[3]["state"], "MISMATCH")

    def test_signed_pre_id_reply_binds_the_exact_parent_text(self):
        chat._ensure_dir()
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-01T00:00:00Z", "from": "old",
                                "text": "first twin"}) + "\n")
            f.write(json.dumps({"ts": "2026-07-01T00:00:00Z", "from": "old",
                                "text": "second twin"}) + "\n")
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)):
            r = chat.post("answer", who="bob", profile="bob", sign=True,
                          reply_to="2")
        self.assertEqual(r["rtext"], "second twin")
        self.assertEqual(chat.verify()[-1]["state"], "ok")
        r["rtext"] = "first twin"
        self.assertNotEqual(chat.payload_for(r), r["payload"])

    def test_stripping_the_payload_off_a_signed_reply_is_a_MISMATCH(self):
        """The downgrade attack: `legacy` is the ONE state that never alarms,
        so a forger's cheapest move is to delete the recorded payload and
        re-parent freely. It cannot work — reply_to and {payload} shipped in
        the same change, so a signed row that IS a reply and carries no
        payload is forged by construction, never legacy."""
        p1 = chat.post("parent one", who="alice")
        p2 = chat.post("parent two", who="alice")
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)):
            chat.post("signed reply", who="bob", profile="bob", sign=True,
                      reply_to=p2["id"])
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, encoding="utf-8") as f:
            lines = f.read().split("\n")
        row = json.loads(lines[2])
        row.pop("payload")                       # the downgrade
        row.update(reply_to=p1["id"], rts=p1["ts"], rfrom=p1["from"])
        lines[2] = json.dumps(row)
        with open(raw, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.assertEqual(chat.verify()[2]["state"], "MISMATCH")
        rc, _out, err = self.cli("verify")
        self.assertEqual(rc, 1)                  # and the verb exits non-zero
        self.assertIn("STRIPPED", err)

    def test_verify_calls_a_pre_payload_signed_row_legacy_not_broken(self):
        """Rows signed before the recorded-payload field are UNVERIFIABLE, not
        invalid — the honest state, never a false alarm."""
        chat._append({"ts": "2026-07-01T00:00:00Z", "from": "a1",
                      "text": "old signed", "turn": "t" * 64, "chain": 1},
                     "main")
        self.assertEqual(chat.verify()[0]["state"], "legacy")

    def test_unsigned_reply_still_lands_visibly_unattested(self):
        p = chat.post("parent", who="alice")
        r = chat.post("child", who="bob", reply_to=p["id"])
        self.assertNotIn("chain", r)
        self.assertNotIn("payload", r)
        self.assertIn("[unsigned]", chat._fmt(r))


# ---------------------------------------------------------------------------
# 3. the render: one level, a quote up, a count down, orphans graceful
# ---------------------------------------------------------------------------

class ReplyRenderTest(ReplyBase):
    def test_read_renders_the_quote_and_the_parent_count(self):
        p = chat.post("the original ledger post", who="alice")
        chat.post("first answer", who="bob", reply_to=p["id"])
        chat.post("second answer", who="carol", reply_to=p["id"])
        rc, out, _ = self.cli("read")
        self.assertEqual(rc, 0)
        lines = out.strip().split("\n")
        self.assertIn("↩2", lines[0])                       # the parent's count
        self.assertIn('↳alice "the original ledger post"', lines[1])
        self.assertIn("first answer", lines[1])
        self.assertNotIn("↩", lines[1].split(":")[-1])      # a reply is a leaf

    def test_reply_counts_never_leak_onto_a_same_second_sibling(self):
        """ts|from is NOT a row identity — one seat posting twice inside a
        second shares it. Threading keys on the row id."""
        a = chat.post("first", who="alice")
        b = dict(chat.post("second", who="alice"))
        self.assertEqual(a["ts"], b["ts"])   # same second, same author
        chat.post("re first", who="bob", reply_to=a["id"])
        idx = chat.index_rows(self.rows())
        self.assertEqual(idx["replies"].get(chat.tkey(a)), 1)
        self.assertIsNone(idx["replies"].get(chat.tkey(b)))

    def test_one_level_only_a_reply_to_a_reply_quotes_the_reply(self):
        p = chat.post("root", who="alice")
        r1 = chat.post("mid", who="bob", reply_to=p["id"])
        r2 = chat.post("leaf", who="carol", reply_to=r1["id"])
        idx = chat.index_rows(self.rows())
        self.assertEqual(chat.quote_of(r2, idx), ("bob", "mid"))
        self.assertEqual(idx["replies"][chat.tkey(r1)], 1)   # flat, not nested

    def test_quote_is_one_clipped_line(self):
        p = chat.post("x" * 400 + "\nsecond line", who="alice")
        r = chat.post("re", who="bob", reply_to=p["id"])
        who, snip = chat.quote_of(r, chat.index_rows(self.rows()))
        self.assertEqual(who, "alice")
        self.assertNotIn("\n", snip)
        self.assertLessEqual(len(snip), chat.QUOTE_CHARS)

    def test_render_survives_a_rotated_out_parent(self):
        p = chat.post("doomed parent", who="alice")
        chat.post("child", who="bob", reply_to=p["id"])
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, encoding="utf-8") as f:
            kept = f.read().split("\n")[1]      # the parent is gone (rotation)
        with open(raw, "w", encoding="utf-8") as f:
            f.write(kept + "\n")
        rc, out, _ = self.cli("read")
        self.assertEqual(rc, 0)
        self.assertIn("(parent rotated out)", out)
        self.assertIn("child", out)

    def test_a_rotated_parent_never_resolves_to_its_same_second_twin(self):
        """MIS-ATTRIBUTION, the class rotation actually produces: one seat
        posting twice inside a second shares ts|from, and rotation drops the
        oldest half — which can split exactly that pair. The reply names its
        parent by ID, so a gone parent is an ORPHAN, never the surviving twin
        (quoting the twin would put words the author never wrote under the
        reply, and hang a phantom ↩N on an innocent row)."""
        a1 = chat.post("SECRET: approve the wire transfer", who="alice")
        a2 = chat.post("lunch?", who="alice")
        self.assertEqual(a1["ts"], a2["ts"])          # the precondition
        chat.post("agreed, doing it", who="bob", reply_to=a1["id"])
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, encoding="utf-8") as f:
            kept = f.read().split("\n")[1:]           # a1 rotates out
        with open(raw, "w", encoding="utf-8") as f:
            f.write("\n".join(kept))
        rows = self.rows()
        idx = chat.index_rows(rows)
        self.assertIsNone(chat.parent_of(rows[-1], idx))
        self.assertEqual(chat.quote_of(rows[-1], idx),
                         ("alice", "(parent rotated out)"))
        self.assertEqual(idx["replies"], {})          # no phantom count
        rc, out, _ = self.cli("read")
        self.assertEqual(rc, 0)
        self.assertNotIn("lunch?\"", out)             # never quoted as parent
        self.assertIn("(parent rotated out)", out)

    def test_a_pre_id_parent_still_resolves_by_ts_from_and_text(self):
        """The id guard must not strand an old parent, but ts|from alone is
        never enough: exact text is the third, signed discriminator."""
        chat._ensure_dir()
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-01T00:00:00Z", "from": "old",
                                "text": "ancient"}) + "\n")
        r = chat.post("answering the ancestor", who="bob", reply_to="1")
        idx = chat.index_rows(self.rows())
        self.assertEqual((r["reply_to"], r["rtext"]), ("", "ancient"))
        self.assertIsNotNone(chat.parent_of(r, idx))
        self.assertEqual(chat.quote_of(r, idx), ("old", "ancient"))

    def test_pre_id_same_second_twins_resolve_exactly_or_orphan(self):
        """The prior fallback still mis-attributed PRE-ID parents: selecting
        twin two quoted twin one immediately, and rotating the selected twin
        could quote its survivor. rtext makes both states exact."""
        chat._ensure_dir()
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        twins = [{"ts": "2026-07-01T00:00:00Z", "from": "old",
                  "text": "SECRET: approve the wire"},
                 {"ts": "2026-07-01T00:00:00Z", "from": "old",
                  "text": "lunch?"}]
        with open(raw, "w", encoding="utf-8") as f:
            for row in twins:
                f.write(json.dumps(row) + "\n")
        r = chat.post("answering lunch", who="bob", reply_to="2")
        idx = chat.index_rows(self.rows())
        self.assertEqual(chat.quote_of(r, idx), ("old", "lunch?"))
        self.assertNotIn(chat.tkey(twins[0]), idx["replies"])
        self.assertEqual(idx["replies"][chat.tkey(twins[1])], 1)
        rc, out, _ = self.cli("read")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertNotIn("↩", lines[0])
        self.assertIn("↩1", lines[1])
        # The chosen parent rotates out while its same-second sibling survives.
        with open(raw, "w", encoding="utf-8") as f:
            f.write(json.dumps(twins[0]) + "\n")
            f.write(json.dumps(r) + "\n")
        rows = self.rows()
        idx = chat.index_rows(rows)
        self.assertIsNone(chat.parent_of(rows[-1], idx))
        self.assertEqual(chat.quote_of(rows[-1], idx),
                         ("old", "(parent rotated out)"))
        self.assertEqual(idx["replies"], {})

    def test_the_journal_keeps_the_thread(self):
        p = chat.post("parent", who="alice")
        chat.post("child", who="bob", reply_to=p["id"])
        chat.log_flush(rooms=["main"])
        logs = [n for n in os.listdir(chat.journal_dir()) if n.endswith(".log")]
        path = os.path.join(chat.journal_dir(), sorted(logs)[0])
        with open(path) as f:
            body = f.read()
        self.assertIn("bob ↳alice@%s: child" % p["ts"], body)

    def test_fmt_without_an_index_is_unchanged(self):
        """Every existing caller (single-row echoes) renders byte-identically."""
        m = chat.post("hi", who="alice")
        self.assertEqual(chat._fmt(m),
                         "%s alice: hi [unsigned]" % m["ts"][11:16])


# ---------------------------------------------------------------------------
# 4. the CLI verbs
# ---------------------------------------------------------------------------

class ReplyCliTest(ReplyBase):
    def test_reply_verb_threads_and_echoes_the_quote(self):
        chat.post("the parent", who="alice")
        rc, out, _ = self.cli("reply", "1", "answering", "--seat", "bob")
        self.assertEqual(rc, 0)
        self.assertIn('↳alice "the parent"', out)
        r = self.rows()[-1]
        self.assertEqual((r["from"], r["rfrom"], r["text"]),
                         ("bob", "alice", "answering"))

    def test_post_reply_to_flag(self):
        p = chat.post("the parent", who="alice")
        rc, _out, _ = self.cli("post", "flagged answer", "--seat", "bob",
                               "--reply-to", p["id"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.rows()[-1]["reply_to"], p["id"])

    def test_reply_wants_a_ref_and_text(self):
        rc, _out, err = self.cli("reply", "1")
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm chat reply", err)

    def test_reply_to_flag_requires_its_own_value(self):
        for args in (("post", "hello", "--reply-to"),
                     ("post", "hello", "--reply-to", "--dm", "codex")):
            rc, _out, err = self.cli(*args)
            self.assertEqual(rc, 2)
            self.assertIn("--reply-to wants", err)
        self.assertEqual(self.rows(), [])

    def test_reply_rides_the_room_flag(self):
        chat.post("team parent", room="team-z", who="alice")
        rc, _out, _ = self.cli("--room", "team-z", "reply", "1", "in-team",
                               "--seat", "bob")
        self.assertEqual(rc, 0)
        self.assertEqual(self.rows("team-z")[-1]["rfrom"], "alice")
        self.assertEqual(chat.read("main")[1], 0)      # never leaked to main

    def test_dm_reply_stays_in_the_lane(self):
        seats.write_roster("recv", session="s-recv")
        first, err = seats.dm("recv", "opening", who="alice")
        self.assertIsNone(err)
        second, err = seats.dm("recv", "threaded", who="alice",
                               reply_to=first["id"])
        self.assertIsNone(err)
        self.assertEqual(second["reply_to"], first["id"])
        self.assertEqual(chat.read("main")[1], 0)      # no room fanout, ever

    def test_verify_verb_reports_and_exits_clean(self):
        chat.post("plain", who="alice")
        rc, out, _ = self.cli("verify")
        self.assertEqual(rc, 0)
        self.assertIn("verify: 1 row", out)
        self.assertIn("1 unsigned", out)


# ---------------------------------------------------------------------------
# 5. THE LAW: threading does not touch beacon-wake semantics
# ---------------------------------------------------------------------------

class ReplyWakeTest(ReplyBase):
    """A reply wakes EXACTLY what its text alone would have woken. Asserted
    against seats.deliverable() — the beacon's own decision function — not a
    proxy for it."""

    def deliv(self, row, seat, room="main"):
        return seats.deliverable(row, seat, room)

    def test_replying_to_a_seat_does_not_wake_it(self):
        seats.write_roster("codex", session="s-codex")
        p = chat.post("codex's own words", who="codex")
        r = chat.post("thanks, noted", who="alice", reply_to=p["id"])
        self.assertFalse(self.deliv(r, "codex"))
        # ...and the identical text without the thread is equally silent
        plain = chat.post("thanks, noted", who="alice")
        self.assertEqual(self.deliv(r, "codex"), self.deliv(plain, "codex"))

    def test_a_reply_that_mentions_still_wakes(self):
        seats.write_roster("codex", session="s-codex")
        p = chat.post("parent", who="codex")
        r = chat.post("@codex what about this", who="alice", reply_to=p["id"])
        self.assertTrue(self.deliv(r, "codex"))

    def test_wake_is_identical_with_and_without_the_pointer_everywhere(self):
        """The whole matrix: for every scope the beacon knows, the reply row
        and the same text unparented decide the SAME way."""
        seats.write_roster("codex", session="s-codex", home_room="team-z")
        p = chat.post("parent", room="team-z", who="alice")
        cases = [("plain chatter", "main"), ("plain chatter", "team-z"),
                 ("@all hands", "main"), ("@all hands", "side"),
                 ("@codex ping", "side"), ("nothing for you", "side")]
        for text, room in cases:
            r = dict(chat.post(text, room=room, who="alice",
                               reply_to=p["id"]))
            plain = dict(r)
            for k in ("reply_to", "rts", "rfrom"):
                plain.pop(k, None)
            self.assertEqual(self.deliv(r, "codex", room),
                             self.deliv(plain, "codex", room),
                             "%r in #%s decided differently once threaded"
                             % (text, room))

    def test_deliverable_never_reads_the_pointer(self):
        """Structural proof, not just behavioural: the row's thread fields are
        invisible to the wake decision by construction."""
        seats.write_roster("codex", session="s-codex")
        row = {"ts": "T", "from": "alice", "text": "@codex hi",
               "reply_to": "x", "rts": "T0", "rfrom": "codex"}
        self.assertTrue(seats.deliverable(row, "codex", "main"))
        row["reply_to"] = "totally-different"
        row["rfrom"] = "someone-else"
        self.assertTrue(seats.deliverable(row, "codex", "main"))

    def test_deliver_any_end_to_end_stays_text_driven(self):
        """Integration proof at the actual boundary hook: replying to a seat's
        own row is silent, then the same threaded path wakes once its text
        contains the exact mention."""
        seats.join(session="s-codex", seat="codex", cwd="/tmp/reply-wake")
        p = chat.post("codex parent", who="codex")
        chat.post("quiet threaded answer", who="alice", reply_to=p["id"])
        self.assertIsNone(seats.deliver_any(session="s-codex", seat="codex"))
        chat.post("@codex threaded ping", who="alice", reply_to=p["id"])
        line = seats.deliver_any(session="s-codex", seat="codex")
        self.assertIn("@codex threaded ping", line)


if __name__ == "__main__":
    unittest.main()
