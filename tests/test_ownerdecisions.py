#!/usr/bin/env python3
"""helm ownerasks DECISION CARDS — the ask primitive generalized to
rulings-among-options (owner steer 2026-08-04: a durable queue the owner
reviews at leisure; the verdict flows back to the FILING SEAT).

Hermetic: tmp HELM_HOME/HELM_CHAT_DIR/HELM_BOARD, transport killed, ambient
session ids scrubbed (the seats-test law). Covers the four contract legs the
feature names: card file/verdict round-trip (append-only snapshots),
verdict-notifies-asker (the DM lane), board owner_gated_queue derivation
(single source), and the one-compact-line room announcement.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, ownerasks, pk, seats  # noqa: E402

ENV_KEYS = (
            # FILING A CARD NOW POSTS TO THE OWNER'S PHONE. Leaving the topic
            # in the environment would make this suite buzz a real person once
            # per fixture card — the loudest possible test-hygiene leak, and
            # the only one whose blast radius is outside the machine. Every
            # arm below that wants a push sets the topic itself, under mock.
            "HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC",
            "HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER",
            "HELM_BOARD", "HELM_SCRATCH_GC", "HELM_CACHE_DIR",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR", "HELM_ACTOR",
            # WHO IS WRITING is now read from these (task/2997): an inherited
            # actor-store override would point a fixture at a real identity
            # file, and an inherited harness stamp would make the "owner's own
            # shell" arms run as an agent.
            "HELM_ACTORS", "MELD_ACTORS", "PI_CODING_AGENT",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")

BODY = ("The store holds 400 retired entries; scans cost 2s per resolve.\n"
        "* Archive to cold file :: resolves fast; history one file away\n"
        "*! Leave in place :: zero risk; scans stay slow until indexed\n")


def ruling(rid, choice, **kw):
    """The owner's verdict through HIS door, the only way one is recorded
    (task/2997): production mints the OwnerDoor only in web_core's owner
    handlers, and a fixture standing in for the owner mints it the same way."""
    return ownerasks.decide(rid, choice, by=ownerasks.owner_door("web"), **kw)


class DecideBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-decide-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["HELM_BOARD"] = os.path.join(self.tmp, "board.json")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        # room derivation reads cwd — run from tmp so the default stays 'main'
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": []})

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decide_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        prior = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ownerasks.cmd_decide(list(args))
        finally:
            sys.stdin = prior
        return rc, out.getvalue(), err.getvalue()

    def file_card(self, asker="builder-1"):
        ctx, opts, err = ownerasks.parse_card_body(BODY)
        self.assertIsNone(err)
        row, problem = ownerasks.file_decision(
            "store GC policy", ctx, opts, asker, refs=["lane/store-gc"])
        self.assertIsNotNone(row, problem)
        return row

    def dm_lane(self, seat):
        p = chat.room_path(chat.dm_room(seat))
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f]


class BodyGrammarTest(DecideBase):
    def test_context_then_options_with_one_recommend(self):
        ctx, opts, err = ownerasks.parse_card_body(BODY)
        self.assertIsNone(err)
        self.assertIn("400 retired entries", ctx)
        self.assertEqual([o["label"] for o in opts],
                         ["Archive to cold file", "Leave in place"])
        self.assertEqual([o["recommended"] for o in opts], [False, True])
        self.assertEqual([o["key"] for o in opts], ["1", "2"])
        self.assertIn("one file away", opts[0]["consequence"])

    def test_no_option_lines_materializes_approve_reject(self):
        ctx, opts, err = ownerasks.parse_card_body("just a yes/no question\n")
        self.assertIsNone(err)
        self.assertEqual([o["key"] for o in opts], ["approve", "reject"])

    def test_mechanical_refusals(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control IS the first statement (assertIsNone on the good body's err through the same parser); the loop then walks a literal non-empty tuple
        # UNCONDITIONAL POSITIVE CONTROL on the same observable first: the
        # parser ACCEPTS a good body, so every refusal below is the rule
        # firing, never a parser that refuses everything.
        self.assertIsNone(ownerasks.parse_card_body(BODY)[2])
        for body, want in (
                ("", "CONTEXT"),                       # no context at all
                ("ctx\n* bare option no separator", "::"),
                ("ctx\n* label ::   ", "BOTH halves"),
                ("ctx\n*! a :: b\n*! c :: d", "ONE option"),
                ("ctx\n* a :: b\ntrailing prose", "BEFORE")):
            _c, _o, err = ownerasks.parse_card_body(body)
            self.assertIsNotNone(err, body)
            self.assertIn(want, err)


class LedgerRoundTripTest(DecideBase):
    def test_file_verdict_deliver_round_trip_append_only(self):
        row = self.file_card()
        rid = row["id"]
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["asker"], "builder-1")
        self.assertEqual(row["refs"], ["lane/store-gc"])
        # the ruling: recorded but NOT closed — delivery is the closer
        dec, err = ruling(rid, "Leave in place", comment="simple wins")
        self.assertIsNone(err)
        self.assertEqual(dec["status"], "decided")
        self.assertEqual(dec["verdict"]["choice"], "2")
        self.assertEqual(dec["verdict"]["comment"], "simple wins")
        deliv, derr = ownerasks.deliver_verdict(rid)
        self.assertIsNone(derr)
        self.assertEqual(deliv["status"], "delivered")
        self.assertTrue(deliv["delivered_ref"])
        with open(ownerasks.decisions_path(), encoding="utf-8") as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual([l["status"] for l in lines],
                         ["open", "decided", "delivered"])  # never rewritten

    def test_choice_resolves_by_key_or_exact_label_never_substring(self):
        rid = self.file_card()["id"]
        _r, err = ruling(rid, "Leave")        # substring: refused
        self.assertIn("matches no option", err)
        dec, err = ruling(rid, "1")           # key: resolves
        self.assertIsNone(err)
        self.assertEqual(dec["verdict"]["label"], "Archive to cold file")

    def test_decided_is_terminal_for_verdicts(self):
        rid = self.file_card()["id"]
        ruling(rid, "1")
        _r, err = ruling(rid, "2")
        self.assertIn("already decided", err)

    def test_deliver_refuses_open_and_is_idempotent_after_delivery(self):
        rid = self.file_card()["id"]
        _r, err = ownerasks.deliver_verdict(rid)
        self.assertIn("no verdict yet", err)
        ruling(rid, "1")
        first, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        again, err = ownerasks.deliver_verdict(rid)     # retry: no second DM
        self.assertIsNone(err)
        self.assertEqual(again["delivered_ref"], first["delivered_ref"])
        self.assertEqual(len(self.dm_lane("builder-1")), 1)

    def test_refusals_missing_pieces(self):
        self.assertIn("title", ownerasks.file_decision("", "c", None, "a")[1])
        self.assertIn("context", ownerasks.file_decision("t", " ", None, "a")[1])
        self.assertIn("asker", ownerasks.file_decision("t", "c", None, "")[1])
        self.assertIn("no such", ruling("ghost", "1")[1])
        self.assertIn("no such", ownerasks.deliver_verdict("ghost")[1])

    def test_malformed_asker_is_refused_AT_FILE_TIME(self):
        """The card has no address-edit verb, so a bad return address filed
        today is a verdict undeliverable forever (codex blocker 2 hardening):
        refuse at the door, where the filer can still fix it."""
        for bad in ("bad name!", "seat/with/slash", "a" * 65):
            row, why = ownerasks.file_decision("t", "c", None, bad)
            self.assertIsNone(row, bad)
            self.assertIn("not a deliverable seat address", why)
        # all-@ strips to NOTHING and is the missing-address refusal, not the
        # malformed one — two doors, each with its own teaching sentence
        row, why = ownerasks.file_decision("t", "c", None, "@@")
        self.assertIsNone(row)
        self.assertIn("--asker", why)
        # positive control on the same gate: the exact-token shape files
        self.assertIsNotNone(ownerasks.file_decision("t", "c", None,
                                                     "team.a-1")[0])


class DeliveryDurabilityTest(DecideBase):
    """SENT is not SEEN (codex blocker 2): chat is tmpfs — a reboot wipes the
    dir, rotation drops rows, seat GC deletes lane files — so `delivered`
    binds to the LEDGER (the verdict is always pullable) and a retry
    RE-VERIFIES the lane, reopening delivery on loss instead of no-opping
    on a terminal state."""

    def delivered_card(self):
        rid = self.file_card(asker="builder-7")["id"]
        ruling(rid, "2")
        row, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        return rid, row

    def lane_path(self):
        return chat.room_path(chat.dm_room("builder-7"))

    def ledger_statuses(self):
        with open(ownerasks.decisions_path(), encoding="utf-8") as f:
            return [json.loads(l)["status"] for l in f]

    def test_reboot_wipes_the_chat_dir_and_retry_recovers(self):
        rid, first = self.delivered_card()
        shutil.rmtree(os.environ["HELM_CHAT_DIR"])          # the tmpfs reboot
        again, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        self.assertEqual(again["status"], "delivered")
        self.assertNotEqual(again["delivered_ref"], first["delivered_ref"],
                            "a fresh DM, never a claim about the dead one")
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 1)
        self.assertIn("Leave in place", lane[0]["text"])    # re-sent FROM the ledger
        # SNAPSHOT-BEFORE-RESEND, visible in the durable sequence: the reopen
        # (decided, ref cleared) precedes the fresh delivered even on the
        # SUCCESS path — a crash mid-resend leaves a visible decided card.
        self.assertEqual(self.ledger_statuses(),
                         ["open", "decided", "delivered", "decided",
                          "delivered"],
                         "the reopen is its own appended snapshot, BEFORE "
                         "the resend's delivered one")

    def test_loss_plus_resend_failure_stays_visible_and_retryable(self):
        """ROUND 3'S CELL: evidence gone AND the resend fails. The reopen
        snapshot must land BEFORE the send attempt, so the failure leaves a
        durably-decided card — visible on --open and the web queue, retry
        reachable — never a terminal 'delivered' on a dead ref. THE ORDERING
        IS THE MUTATION CHECK: an implementation that resends first (or only
        appends on success) appends nothing when the send fails, and the
        decided tail asserted below goes red."""
        rid, first = self.delivered_card()
        os.unlink(self.lane_path())                          # evidence gone
        with mock.patch.object(seats, "dm",
                               return_value=(None, "transport down")):
            row, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(row)
        self.assertIn("visible", err)                        # says what is true
        self.assertIn("helm decide deliver %s" % rid, err)   # retry pointer
        folded = ownerasks.decision_rows()[rid]
        self.assertEqual(folded["status"], "decided",
                         "NEVER terminally delivered on dead evidence")
        self.assertIsNone(folded["delivered_ref"], "the dead ref is cleared")
        self.assertEqual(self.ledger_statuses(),
                         ["open", "decided", "delivered", "decided"],
                         "the durable reopen landed despite the failed send")
        # visible again on the CLI queue surface…
        rc, out, _e = self.decide_cli("list", "--open")
        self.assertEqual(rc, 0)
        self.assertIn(rid, out)
        # …and the retry completes once the transport heals
        healed, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        self.assertEqual(healed["status"], "delivered")
        self.assertNotEqual(healed["delivered_ref"], first["delivered_ref"])
        self.assertEqual(len(self.dm_lane("builder-7")), 1)

    def test_reopen_refusal_never_resends(self):  # noqa: VACUOUS_ASSERTION — sent==[] has its positive control at the SAME patch seam one test up: test_loss_plus_resend_failure proves a seats.dm patch intercepts this exact call path (its injected error propagates into the result)
        """The remaining cell: evidence gone but the ledger cannot record the
        reopen — the side effect must NOT happen (a resend nothing recorded
        would be delivery the ledger denies)."""
        rid, _first = self.delivered_card()
        os.unlink(self.lane_path())
        sent = []
        with mock.patch.object(seats, "dm",
                               side_effect=lambda *a, **k: sent.append(1)), \
                mock.patch.object(ownerasks.eventledger, "append_unlocked",
                                  return_value=False):
            row, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(row)
        self.assertIn("NOT resending", err)
        self.assertEqual(sent, [], "no DM without the durable record first")
        self.assertEqual(ownerasks.decision_rows()[rid]["status"], "delivered",
                         "nothing was recorded, nothing pretended")

    def test_show_warns_on_lost_evidence_read_only(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn(WARNING) intact-lane control precedes the unconditional assertIn(WARNING) on the same observable after the loss; both branches are exercised in this one test
        """The per-card observation surface: anyone holding the id discovers
        the loss from `show`, without mutating state."""
        rid, _row = self.delivered_card()
        rc, out, _e = self.decide_cli("show", rid)           # intact: control
        self.assertEqual(rc, 0)
        self.assertNotIn("WARNING", out)
        os.unlink(self.lane_path())
        rc, out, _e = self.decide_cli("show", rid)
        self.assertEqual(rc, 0)
        self.assertIn("delivered evidence GONE", out)
        self.assertIn("helm decide deliver %s" % rid, out)
        self.assertEqual(ownerasks.decision_rows()[rid]["status"], "delivered",
                         "show is READ-ONLY — observation is not mutation")

    def test_rotation_drops_the_row_and_retry_recovers(self):  # noqa: VACUOUS_ASSERTION — delivered_card() asserts the first delivery succeeded (err is None) before the loss is injected, and the recovery is asserted PRESENT (assertTrue(any(verdict text in lane)))
        rid, first = self.delivered_card()
        with open(self.lane_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "other0row0000", "ts": "t",
                                "from": "x", "text": "unrelated"}) + "\n")
        again, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        self.assertNotEqual(again["delivered_ref"], first["delivered_ref"])
        texts = [m["text"] for m in self.dm_lane("builder-7")]
        self.assertTrue(any("Leave in place" in t for t in texts), texts)

    def test_gc_deletes_the_lane_file_and_retry_recovers(self):  # noqa: VACUOUS_ASSERTION — delivered_card() is the unconditional positive control (first delivery proven), and recovery is asserted PRESENT (len(lane)==1 after the unlink)
        rid, _first = self.delivered_card()
        os.unlink(self.lane_path())                          # the seat GC
        _again, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        self.assertEqual(len(self.dm_lane("builder-7")), 1)

    def test_intact_lane_stays_verified_idempotent(self):  # noqa: VACUOUS_ASSERTION — both observables are asserted PRESENT: the refs are equal non-empty strings from a proven delivery and len(lane)==1 fails on an empty lane
        """The other half of re-verification: the recorded row still present
        means NO duplicate DM — retry answers from the verified state, not
        from the terminal status."""
        rid, first = self.delivered_card()
        again, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        self.assertEqual(again["delivered_ref"], first["delivered_ref"])
        self.assertEqual(len(self.dm_lane("builder-7")), 1)

    def test_the_dm_carries_the_durable_pull_path(self):
        """The DM is ATTENTION, the ledger is AVAILABILITY: every verdict DM
        names the always-pullable record, so even a lost DM leaves the asker
        one command from the ruling."""
        rid, _row = self.delivered_card()
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 1)              # the structural must-hit
        self.assertIn("helm decide show %s" % rid, lane[0]["text"])


class VerdictNotifiesAskerTest(DecideBase):
    def test_verdict_lands_in_the_askers_dm_lane(self):  # noqa: VACUOUS_ASSERTION — the observable is asserted PRESENT: assertEqual(len(lane), 1) fails on an empty/missing lane before any content assertIn runs, so no vacuous pass exists
        rid = self.file_card(asker="builder-7")["id"]
        ruling(rid, "2", comment="do the simple thing")
        _r, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 1, "exactly one verdict DM")
        text = lane[0]["text"]
        self.assertIn(rid, text)
        self.assertIn("Leave in place", text)           # the chosen option
        self.assertIn("do the simple thing", text)      # the owner's comment
        self.assertIn("store GC policy", text)          # names the card

    def test_comment_is_non_closing_and_reaches_the_asker(self):
        rid = self.file_card(asker="builder-7")["id"]
        row, problem = ownerasks.comment_decision(
            rid, "what does indexing cost?", by=ownerasks.owner_door("web"))
        self.assertIsNone(problem)
        self.assertEqual(row["status"], "open")         # comment never closes
        self.assertEqual(len(row["comments"]), 1)
        self.assertIn("indexing cost", self.dm_lane("builder-7")[0]["text"])


class RoomAnnouncementTest(DecideBase):
    def test_filing_drops_one_compact_line_in_the_room(self):
        row = self.file_card()
        with open(chat.room_path("main"), encoding="utf-8") as f:
            lines = [json.loads(l) for l in f]
        hits = [l for l in lines if row["id"] in l.get("text", "")]
        self.assertEqual(len(hits), 1, "ONE line — the attention budget")
        text = hits[0]["text"]
        self.assertIn("store GC policy", text)
        self.assertNotIn("400 retired entries", text,
                         "the line never carries the card BODY")

    def test_announcement_failure_never_eats_the_card(self):
        os.environ["HELM_CHAT_DIR"] = "/proc/no-such/chat"   # unwritable
        ctx, opts, _e = ownerasks.parse_card_body(BODY)
        row, problem = ownerasks.file_decision("t", ctx, opts, "builder-1")
        self.assertIsNotNone(row, "the durable card outranks the room line")
        self.assertIn("announcement failed", problem)
        self.assertIn(row["id"], ownerasks.decision_rows())


class _Resp:
    """urlopen's context-manager shape — notify.py only enters and exits."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class OwnerReachTest(DecideBase):
    """A DECISION ONLY THE OWNER CAN MAKE MUST NOT DEPEND ON HIM LOOKING.

    Before this, filing a card announced it to #helm — a room read only by
    seats — and then waited. Overnight that is a queue of rulings nobody is
    blocked on because nobody knows it filled up. These arms bind the reach
    fact itself, and specifically that it is never claimed when nothing left
    the box: `owner_push` returns True for BOTH a delivery and a deliberate
    opt-out, which is the exact confusion helm/beacons.py:1646 was fixed for.
    """

    def room_lines(self):
        with open(chat.room_path("main"), encoding="utf-8") as f:
            return [json.loads(l)["text"] for l in f]

    def ledger_row(self, rid):
        return ownerasks.decision_rows()[rid]

    def test_a_filed_card_pushes_its_headline_and_never_its_body(self):
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as urlopen:
            row = self.file_card()
        self.assertEqual(urlopen.call_count, 1, "one push per card, not per option")
        req = urlopen.call_args[0][0]
        body = req.data.decode("utf-8")
        self.assertEqual(req.get_header("Title"), "helm decision")
        self.assertIn("store GC policy", body)
        self.assertIn(row["id"], body)
        # HE MUST BE ABLE TO ANSWER FROM THE DEVICE THIS ARRIVED ON. The push
        # names no console: the owner reads ntfy and uses orca mobile, and
        # helm web binds 127.0.0.1, which on a phone is the phone.
        self.assertNotIn("helm web", body)
        self.assertIn("reply", body, "he needs to know HOW to answer")
        for opt in row["options"]:
            self.assertIn(opt["label"], body, "the choices are the headline")
            # the KEY travels too — a one-character reply is what a phone can
            # realistically carry, and _pick_option resolves it
            self.assertIn("%s. %s" % (opt["key"], opt["label"]), body)
        # THE ATTENTION BUDGET IS THE POINT, so this is not a style assertion:
        # the context paragraph and the per-option consequences live on the web
        # queue. A phone notification carrying the whole card is the firehose
        # this surface exists to avoid.
        self.assertNotIn("400 retired entries", body)
        self.assertTrue(self.ledger_row(row["id"])["owner_pushed_ts"],
                        "a delivered push is recorded on the durable row")

    def test_an_opted_out_owner_is_never_recorded_as_told(self):
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME TWO OBSERVABLES
        # this arm then asserts empty. Without it a zero call_count and a None
        # stamp would also be exactly what a version with no push code at all
        # produces, and the arm would pass while measuring nothing.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as armed:
            control = self.file_card(asker="builder-9")
        self.assertEqual(armed.call_count, 1)
        self.assertTrue(self.ledger_row(control["id"])["owner_pushed_ts"])
        control_line = [t for t in self.room_lines() if control["id"] in t][0]
        self.assertIn("pushed to his phone", control_line)

        # THE CASE: no HELM_NTFY_TOPIC — setUp scrubbed it, the opt-out world.
        with mock.patch("urllib.request.urlopen") as urlopen:
            row = self.file_card()
        self.assertEqual(urlopen.call_count, 0, "opt-out makes NO network call")
        self.assertIn(row["id"], ownerasks.decision_rows(),
                      "the card still files — the None below is about REACH")
        self.assertIsNone(self.ledger_row(row["id"])["owner_pushed_ts"])
        line = [t for t in self.room_lines() if row["id"] in t][0]
        self.assertIn("NOT PUSHED", line)
        self.assertIn("HELM_NTFY_TOPIC", line,
                      "the room learns WHY he was not reached")

    def test_a_failed_push_is_not_recorded_as_told(self):
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route to host")):
            row = self.file_card()
        self.assertIn(row["id"], ownerasks.decision_rows(),
                      "a notifier must never eat the durable card")
        self.assertIsNone(self.ledger_row(row["id"])["owner_pushed_ts"])
        line = [t for t in self.room_lines() if row["id"] in t][0]
        self.assertIn("NOT PUSHED", line)
        self.assertIn("FAILED", line)

    def test_the_next_push_carries_the_cards_he_was_never_told_about(self):
        """A card is filed ONCE and has no edge of its own to re-fire on, so
        an unreachable moment would strand it forever. The next card's push
        carries the backlog — one extra sentence, never one buzz per row."""
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("phone off")):
            stranded = self.file_card(asker="builder-1")
        self.assertIsNone(self.ledger_row(stranded["id"])["owner_pushed_ts"])
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as urlopen:
            later = self.file_card(asker="builder-2")
        self.assertEqual(urlopen.call_count, 1, "ONE push, not one per card")
        body = urlopen.call_args[0][0].data.decode("utf-8")
        self.assertIn(later["id"], body)
        self.assertIn(stranded["id"], body, "he learns the backlog exists")
        self.assertTrue(self.ledger_row(stranded["id"])["owner_pushed_ts"],
                        "the carried card is stamped by the push that carried it")
        # CONTROL: a stamped card is NOT carried again. Without this, every
        # future push would re-list every card ever stranded and the sentence
        # that makes the backlog visible would become the noise that hides it.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as urlopen:
            self.file_card(asker="builder-3")
        third = urlopen.call_args[0][0].data.decode("utf-8")
        self.assertNotIn(stranded["id"], third)

    def test_the_list_marks_an_open_card_the_owner_never_saw(self):
        # ORDER IS LOAD-BEARING and the first draft of this arm had it
        # backwards: filing the unreached card FIRST let the reached card's
        # push carry it, so both ended up stamped and the assertion failed
        # against correct code. The reached one goes first precisely because
        # nothing after it should un-strand the one that follows.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen", return_value=_Resp()):
            reached = self.file_card(asker="builder-2")
        with mock.patch("urllib.request.urlopen"):
            unreached = self.file_card(asker="builder-1")
        rc, out, _err = self.decide_cli("list")
        self.assertEqual(rc, 0)
        rows = {l.split()[0]: l for l in out.splitlines() if l.split()}
        self.assertIn("NOT PUSHED", rows[unreached["id"]],
                      "an open card he was never told about is not waiting on him")
        self.assertNotIn("NOT PUSHED", rows[reached["id"]])

    def test_a_card_the_push_did_not_NAME_is_not_stamped_as_told(self):
        """A FIX. The body named stale[:6] and the stamp took the
        WHOLE list, so a seventh stranded card was recorded as told-about
        having appeared in NO message — silent loss of exactly the thing the
        carry exists to prevent. What the push SAYS is now what it CLAIMS."""
        stranded = []
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("off")):
            for i in range(ownerasks.STALE_NAMED + 2):      # 8 with cap 6
                stranded.append(self.file_card(asker="builder-%d" % i))
        for r in stranded:
            self.assertIsNone(self.ledger_row(r["id"])["owner_pushed_ts"])

        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as urlopen:
            self.file_card(asker="carrier")
        body = urlopen.call_args[0][0].data.decode("utf-8")
        named = [r for r in stranded if r["id"] in body]
        unnamed = [r for r in stranded if r["id"] not in body]
        # MUST-HIT CONTROLS: the cap actually bit, in both directions — some
        # were named and some were not. Either list being empty would make the
        # assertion below pass for the wrong reason.
        self.assertEqual(len(named), ownerasks.STALE_NAMED)
        self.assertTrue(unnamed)
        # THE COUNT IS HONEST even though the ids are capped
        self.assertIn("+ %d earlier cards" % len(stranded), body)
        self.assertIn("and %d more" % len(unnamed), body)
        for r in named:
            self.assertTrue(self.ledger_row(r["id"])["owner_pushed_ts"])
        for r in unnamed:
            self.assertIsNone(self.ledger_row(r["id"])["owner_pushed_ts"],
                              "a card the push never named was stamped as told")

    def test_a_channel_that_disappears_mid_send_is_UNPROVEN_not_delivered(self):
        """A FIX. `configured()` and `owner_push` are two reads and
        they are not atomic: a topic unset between them makes owner_push return
        its OPT-OUT True while nothing left the box — the beacons latch defect
        surviving as a TOCTOU. Unproven delivery must not be stamped."""
        # POSITIVE CONTROL FIRST, same observable: a channel present for BOTH
        # reads does stamp. Otherwise "not stamped" is also what a build that
        # never stamps anything produces.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen", return_value=_Resp()):
            ok = self.file_card(asker="steady")
        self.assertTrue(self.ledger_row(ok["id"])["owner_pushed_ts"])

        # THE CASE: armed at the first read, gone by the second.
        seen = []

        def vanishing():
            seen.append(1)
            return len(seen) == 1          # True once, then False
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("helm.notify.configured", side_effect=vanishing), \
                mock.patch("urllib.request.urlopen", return_value=_Resp()):
            row = self.file_card(asker="vanishing")
        self.assertEqual(len(seen), 2, "the channel is read on BOTH sides")
        self.assertIsNone(self.ledger_row(row["id"])["owner_pushed_ts"])
        line = [t for t in self.room_lines() if row["id"] in t][0]
        self.assertIn("NOT PUSHED", line)
        self.assertIn("UNPROVEN", line)

    def test_the_LAST_card_gets_a_real_retry_edge_not_a_list_marker(self):
        """A SECOND pass, and it was right that stating the bound
        was not curing it: the carry rides the NEXT card's push, so the last
        card ever filed had nothing behind it, and marking it in `decide list`
        tells an AGENT — leaving the owner exactly where task/232 found him,
        dependent on somebody noticing. A list marker is evidence, not
        delivery."""
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("off")):
            last = self.file_card(asker="the-last-one")
        self.assertIsNone(self.ledger_row(last["id"])["owner_pushed_ts"])

        # NO NEW CARD IS FILED. The recovery must come from the periodic pass.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as urlopen:
            sent, why = ownerasks.flush_unreached()
        self.assertTrue(sent, why)
        self.assertEqual(urlopen.call_count, 1, "ONE batched push, not one per card")
        body = urlopen.call_args[0][0].data.decode("utf-8")
        self.assertIn(last["id"], body)
        self.assertIn("store GC policy", body)
        self.assertTrue(self.ledger_row(last["id"])["owner_pushed_ts"])
        # AND IT GOES QUIET once nothing is owed — otherwise a periodic pass
        # would buzz him every cycle forever, which is the opposite cure.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Resp()) as again:
            sent2, _why2 = ownerasks.flush_unreached()
        self.assertFalse(sent2)
        self.assertEqual(again.call_count, 0)

    def test_the_flush_is_WIRED_into_the_periodic_pass(self):
        """A recovery edge nothing runs is a list marker with extra steps. The
        pass is `helm beacons --post`, the fleet's owner-reachability watchdog:
        already scheduled, already pushing through notify, already re-arming a
        failed edge. Pinning the WIRE, because the behaviour above proves the
        function and proves nothing about whether anything calls it."""
        import inspect
        from helm import beacons
        src = inspect.getsource(beacons.cmd_beacons)
        # MUST-HIT CONTROL: the source really is the posting pass, so a missing
        # call below means missing, not mis-scoped.
        self.assertIn("escalate(", src)
        self.assertIn("flush_unreached", src)

    def test_every_card_filing_suite_scrubs_the_push_topic(self):
        """THE CLASS, NOT THE INSTANCE. Filing a card is now an OUTBOUND
        NETWORK ACT, so any suite that files one and leaves HELM_NTFY_TOPIC in
        the environment buzzes a real person's phone once per fixture card.
        That is the only test-hygiene leak whose blast radius is off the
        machine, and it must not be re-introducible by writing a new module."""
        here = os.path.dirname(os.path.abspath(__file__))
        filers = []
        for name in sorted(os.listdir(here)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(here, name), encoding="utf-8") as f:
                src = f.read()
            if "file_decision(" in src or "cmd_decide(" in src:
                filers.append((name, src))
        # MUST-HIT: this scan is worthless if it finds nothing, and an empty
        # list would pass every assertion below.
        found = {n for n, _s in filers}
        self.assertIn("test_ownerdecisions.py", found)
        self.assertIn("test_web_decisions.py", found)
        for name, src in filers:
            self.assertIn("HELM_NTFY_TOPIC", src,
                          "%s files decision cards and does not scrub the "
                          "owner's push topic" % name)


class BoardDerivationTest(DecideBase):
    def test_owner_gated_queue_is_derived_from_open_cards_and_asks(self):
        rid = self.file_card()["id"]
        ownerasks.add("relogin codex", needs="owner device-auth")
        ok, err = ownerasks.sync_board()
        self.assertTrue(ok, err)
        q = pk.read_json(os.environ["HELM_BOARD"])["owner_gated_queue"]
        self.assertEqual([r["state"] for r in q],
                         ["waiting-owner-decision", "waiting-owner"])
        self.assertEqual([r["hold_kind"] for r in q],
                         ["decision", "owner-input"])
        self.assertIn("store GC policy", q[0]["ask"])
        self.assertIn("Leave in place*", q[0]["why"])    # options + recommend
        self.assertEqual(q[1]["ask"], "relogin codex")
        self.assertIn("owner device-auth", q[1]["why"])
        # the verdict removes the card from the projection — single source
        ruling(rid, "1")
        ownerasks.deliver_verdict(rid)
        ok, err = ownerasks.sync_board()
        self.assertTrue(ok, err)
        q = pk.read_json(os.environ["HELM_BOARD"])["owner_gated_queue"]
        self.assertEqual([r["state"] for r in q], ["waiting-owner"])

    def test_usage_reset_kind_is_typed_at_the_ask_writer(self):
        row, err = ownerasks.add("relogin codex", needs="owner device-auth",
                                 hold_kind="usage-reset")
        self.assertIsNone(err)
        self.assertEqual(row["hold_kind"], "usage-reset")
        ok, err = ownerasks.sync_board()
        self.assertTrue(ok, err)
        q = pk.read_json(os.environ["HELM_BOARD"])["owner_gated_queue"]
        self.assertEqual(q[0]["hold_kind"], "usage-reset")

    # THE LIVE-SHAPED DIFFERENTIAL (codex blocker 1): the real board holds
    # hand-written rows the ledgers know nothing about — a device-auth owner
    # gate whose `state` is the SAME "waiting-owner" the projection emits,
    # and an informational FYI. The first cut's wholesale replace deleted
    # both while reporting success, and every fixture started [] so nothing
    # could catch it. This one starts SEEDED and fails if sync drops a row
    # it did not derive.
    LEGACY = [
        {"ask": "codex relogin (device-auth) for the owner account",
         "why": "grant dead — only the owner holds the device",
         "state": "waiting-owner", "since": "2026-08-04T19:15Z"},
        {"ask": "informational: ~/.bashrc exports a machine-wide profile",
         "why": "no change requested — context only", "state": "fyi"},
    ]

    def test_sync_preserves_rows_it_did_not_derive(self):
        pk.write_json(os.environ["HELM_BOARD"],
                      {"owner_gated_queue": [dict(r) for r in self.LEGACY]})
        rid = self.file_card()["id"]
        ok, err = ownerasks.sync_board()
        self.assertTrue(ok, err)
        q = pk.read_json(os.environ["HELM_BOARD"])["owner_gated_queue"]
        self.assertEqual(q[-2:], self.LEGACY,
                         "hand-written rows pass through VERBATIM, after "
                         "the derived ones")
        self.assertEqual(q[0]["src"], "ledger")     # ours carry provenance
        self.assertIn("store GC policy", q[0]["ask"])
        # the full cycle re-syncs twice more — legacy rows survive UNCHANGED
        # and are never duplicated, while the derived row comes and goes
        ruling(rid, "1")
        ownerasks.deliver_verdict(rid)
        ok, err = ownerasks.sync_board()
        self.assertTrue(ok, err)
        ok, err = ownerasks.sync_board()            # idempotent re-run
        self.assertTrue(ok, err)
        q = pk.read_json(os.environ["HELM_BOARD"])["owner_gated_queue"]
        self.assertEqual(q, self.LEGACY,
                         "delivered card gone, BOTH legacy rows intact, "
                         "no duplication across repeated syncs")

    def test_sync_refuses_when_the_key_is_not_rendered(self):
        pk.write_json(os.environ["HELM_BOARD"], {"tasks": []})
        ok, err = ownerasks.sync_board()
        self.assertFalse(ok)
        self.assertIn("owner_gated_queue", err)

    def test_sync_refuses_a_non_list_key(self):
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": "done"})
        ok, err = ownerasks.sync_board()
        self.assertFalse(ok)
        self.assertIn("not a list", err)
        # and the refusal did not write: the scalar survives untouched
        self.assertEqual(pk.read_json(os.environ["HELM_BOARD"])
                         ["owner_gated_queue"], "done")

    def test_sync_survives_a_missing_board(self):
        os.environ["HELM_BOARD"] = os.path.join(self.tmp, "nowhere.json")
        ok, err = ownerasks.sync_board()
        self.assertFalse(ok)
        self.assertTrue(err)


class CliTest(DecideBase):
    def test_cli_file_heredoc_verdict_deliver_cycle(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn(rid, list --open) absence has its unconditional positive control on the SAME observable above: assertIn(rid, out) on the pre-verdict list, and the DM-lane len==1 proves the closing effect
        rc, out, err = self.decide_cli("file", "store", "GC", "policy",
                                       "--asker", "builder-1",
                                       "--ref", "lane/store-gc", stdin=BODY)
        self.assertEqual(rc, 0, err)
        rid = out.split()[1]
        rc, out, _e = self.decide_cli("list", "--open")
        self.assertIn(rid, out)
        rc, out, _e = self.decide_cli("show", rid)
        self.assertIn("Leave in place (recommended)", out)
        # THE CLI IS NEVER THE OWNER'S DOOR (task/2997): the ruling lands on
        # his web door, and the CLI's own legs deliver and project it.
        rc, _o, err = self.decide_cli("verdict", rid, "2")
        self.assertEqual(rc, 1, err)
        _r, rerr = ruling(rid, "2", comment="simple wins")
        self.assertIsNone(rerr)
        rc, out, err = self.decide_cli("deliver", rid)
        self.assertEqual(rc, 0, err)
        self.assertIn("delivered to builder-1", out)
        self.assertEqual(len(self.dm_lane("builder-1")), 1)
        rc, out, _e = self.decide_cli("list", "--open")
        self.assertNotIn(rid, out)                      # delivered = closed
        # and the board projection moved in the same cycle
        self.assertEqual(pk.read_json(os.environ["HELM_BOARD"])
                         ["owner_gated_queue"], [])

    def test_cli_file_refuses_without_return_address(self):
        rc, _o, err = self.decide_cli("file", "a", "title", stdin="ctx\n")
        self.assertEqual(rc, 2)
        self.assertIn("--asker", err)

    def test_cli_file_teaches_the_grammar_on_empty_body(self):
        rc, _o, err = self.decide_cli("file", "a", "title",
                                      "--asker", "b", stdin="")
        self.assertEqual(rc, 2)
        self.assertIn("CONTEXT", err)

    def test_cli_usage_floors(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a LITERAL non-empty tuple, so its body cannot be skipped, and each iteration asserts both the rc floor and the taught usage text
        for args in ((), ("bogus",), ("verdict", "x"), ("show",)):
            rc, _o, err = self.decide_cli(*args)
            self.assertEqual(rc, 2, args)
            # the structural assert: a floor TEACHES, it never exits mute
            self.assertIn("usage: helm decide", err, args)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("decide", cli.VERBS)


class AuthorshipTest(DecideBase):
    """task/2997: WHO WROTE THIS ROW. A seat ran `helm decide comment` on a
    card; the ledger recorded `"by": "owner"` and the relay DM'd the asker
    "owner comment on ..." from the owner's handle. Any seat's words became
    the owner's, on the one record that exists to carry HIS voice.

    THE THREAT MODEL IS THE SAME UNIX USER: nothing on the box proves the
    owner, so the owner is never INFERRED. The CLI records the acting seat
    or refuses UNKNOWN; "owner" is written only through an OwnerDoor, which
    only the web queue's handlers mint."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def seat(self, name):
        """Make THIS process the seat `name` the way a live seat is one: a
        declared name AND a harness session the roster binds to it. A
        declared name alone is not an identity (helm.actors refuses it)."""
        sid = "sid-%s" % name
        os.environ["HELM_CHAT_NAME"] = name
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        seats.write_roster(name, session=sid, cwd=self.tmp)

    def comments(self, rid):
        return ownerasks.decision_rows()[rid].get("comments") or []

    def ledger_lines(self):
        with open(ownerasks.decisions_path(), encoding="utf-8") as f:
            return f.read().splitlines()

    def child(self, *args):
        """`bin/helm decide ...` as a CHILD PROCESS whose harness stamps
        and seat name are all unset: the shape helm-codex measured, a seat
        spawning helm with its inherited identity stripped. It keeps this
        fixture's temp homes, so nothing live is touched."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("HELM_CHAT_NAME", "MELD_CHAT_NAME", "CLAUDECODE",
                            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                            "CODEX_SESSION_ID", "PI_CODING_AGENT",
                            "HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC")}
        p = subprocess.run(
            [sys.executable, os.path.join(self.REPO, "bin", "helm"),
             "decide"] + list(args),
            env=env, cwd=self.tmp, capture_output=True, text=True,
            timeout=120)
        return p.returncode, p.stdout, p.stderr

    def test_a_seat_comment_records_the_seat_and_relays_as_the_seat(self):  # noqa: VACUOUS_ASSERTION — the one absence (no "owner comment" in the seat's DM) sits beside assertIn("builder-2 comment on") on the same text, after the owner-door control proved that text can say "owner comment"
        rid = self.file_card(asker="builder-7")["id"]
        # POSITIVE CONTROL FIRST, on the same observables: the owner's door
        # records the owner and relays from his handle. Without it the seat
        # half below would also pass on a writer that never says "owner".
        _row, problem = ownerasks.comment_decision(
            rid, "what does indexing cost?", by=ownerasks.owner_door("web"))
        self.assertIsNone(problem)
        self.assertEqual((self.comments(rid)[-1]["by"],
                          self.comments(rid)[-1]["door"]), ("owner", "web"))
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 1)
        self.assertIn("owner comment on", lane[0]["text"])
        self.assertEqual(lane[0]["from"], seats.owner_name())

        self.seat("builder-2")
        rc, _o, err = self.decide_cli("comment", rid, "indexing is a day of work")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.comments(rid)[-1]["by"],
                          self.comments(rid)[-1]["door"]), ("builder-2", None))
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 2)
        self.assertIn("builder-2 comment on", lane[1]["text"])
        self.assertNotIn("owner comment", lane[1]["text"])
        self.assertEqual(lane[1]["from"], "builder-2")
        # APPEND-ONLY: the owner's earlier comment is still his
        self.assertEqual(self.comments(rid)[0]["by"], "owner")

    def test_a_seat_commenting_on_its_own_card_is_not_relayed_to_itself(self):  # noqa: VACUOUS_ASSERTION — the empty lane is the contract (a DM to yourself never delivers); the recorded by == builder-7 on the same card is the unconditional positive half
        rid = self.file_card(asker="builder-7")["id"]
        self.seat("builder-7")
        rc, _o, err = self.decide_cli("comment", rid, "correction: scans are 3s")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.comments(rid)[-1]["by"], "builder-7")
        self.assertEqual(self.dm_lane("builder-7"), [])
        self.assertNotIn("failed", err, "an author reading his own words is "
                                        "not a failed delivery")

    def test_a_child_with_its_identity_stripped_is_UNKNOWN_never_the_owner(self):  # noqa: VACUOUS_ASSERTION — the unchanged ledger and empty lane are the refusal contract; the arm ends with an unconditional positive control on the SAME observables (an admitted seat's comment grows the ledger and fills the lane)
        """helm-codex's first door: the owner is never INFERRED from the
        absence of a seat name and harness stamps, because a seat's child
        with those variables unset has exactly that absence."""
        rid = self.file_card(asker="builder-7")["id"]
        before = self.ledger_lines()
        for verb in (("comment", rid, "hello"), ("verdict", rid, "2")):
            rc, _o, err = self.child(*verb)
            self.assertEqual(rc, 1, (verb, err))
            self.assertIn("UNKNOWN", err, verb)
            self.assertIn("web queue", err, "the refusal names his doors")
        # and in-process, with only a harness stamp and no seat behind it
        for stamp in ({"CLAUDE_CODE_SESSION_ID": "sid-nobody"},
                      {"CLAUDECODE": "1"}, {}):
            with mock.patch.dict(os.environ, stamp):
                rc, _o, err = self.decide_cli("comment", rid, "hello")
            self.assertEqual(rc, 1, (stamp, err))
            self.assertIn("UNKNOWN", err, stamp)
        self.assertEqual(self.ledger_lines(), before, "nothing was recorded")
        self.assertEqual(ownerasks.decision_rows()[rid]["status"], "open")
        self.assertEqual(self.dm_lane("builder-7"), [])
        # POSITIVE CONTROL, same verb, same card, same observables
        self.seat("builder-2")
        rc, _o, err = self.decide_cli("comment", rid, "hello")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.ledger_lines()), len(before) + 1)
        self.assertEqual(len(self.dm_lane("builder-7")), 1)

    def test_only_an_owner_door_casts_a_verdict(self):  # noqa: VACUOUS_ASSERTION — the unchanged ledger, open status and empty lane are the refusal contract; the arm ends with an unconditional positive control on the same card and lane (the OwnerDoor verdict is recorded as the owner's and delivered)
        rid = self.file_card(asker="builder-7")["id"]
        before = self.ledger_lines()
        self.seat("builder-2")
        rc, out, err = self.decide_cli("verdict", rid, "2")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("OWNER", err)
        self.assertIn("web queue", err, "the refusal names the owner's doors")
        self.assertIn("helm decide comment", err,
                      "and the one thing a seat CAN do on the card")
        # helm-codex's second door: a caller-stated string is never the
        # owner, whichever of his spellings it uses, nor a seat
        for forged in ("owner", "daria", "builder-2"):
            _r, lerr = ownerasks.decide(rid, "2", by=forged)
            self.assertIn("never a caller-stated string", lerr, forged)
        self.assertEqual(self.ledger_lines(), before, "no verdict was recorded")
        self.assertEqual(ownerasks.decision_rows()[rid]["status"], "open")
        self.assertEqual(self.dm_lane("builder-7"), [])
        # POSITIVE CONTROL on the same card and lane: the owner's door rules,
        # the row says so and names the door, and the DM leaves as him
        row, err = ruling(rid, "2")
        self.assertIsNone(err)
        self.assertEqual((row["verdict"]["by"], row["verdict"]["door"]),
                         ("owner", "web"))
        _d, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 1)
        self.assertEqual(lane[0]["from"], seats.owner_name())

    def test_a_string_owner_cannot_comment_either(self):  # noqa: VACUOUS_ASSERTION — the unchanged ledger is the refusal contract; the OwnerDoor comment at the end grows the SAME ledger, the unconditional positive control
        rid = self.file_card(asker="builder-7")["id"]
        before = self.ledger_lines()
        for forged in ("owner", "daria", "builder-2", ""):
            row, err = ownerasks.comment_decision(rid, "x", by=forged)
            self.assertIsNone(row, forged)
            self.assertIn("never a caller-stated string", err, forged)
        self.assertEqual(self.ledger_lines(), before)
        row, err = ownerasks.comment_decision(rid, "x",
                                              by=ownerasks.owner_door("web"))
        self.assertIsNone(err)
        self.assertEqual(len(self.ledger_lines()), len(before) + 1)

    def test_an_owner_door_is_minted_never_constructed(self):
        with self.assertRaises(TypeError):
            ownerasks.OwnerDoor("web")
        door = ownerasks.owner_door("web")
        self.assertEqual(door.door, "web")
        with self.assertRaises(AttributeError):
            door.door = "cli"

    def test_only_the_web_owner_handlers_mint_an_owner_door(self):  # noqa: VACUOUS_ASSERTION — assertEqual against a literal three-site set is exact, so an empty census fails it
        """THE CENSUS behind "only the web door constructs it": every call to
        owner_door() or OwnerDoor() in production code, found by parsing the
        tree rather than by remembering it."""
        import ast
        sites = set()
        for base, dirs, files in os.walk(os.path.join(self.REPO, "helm")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(base, f)
                rel = os.path.relpath(path, self.REPO)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())

                def walk(node, enclosing, rel=rel):
                    for child in ast.iter_child_nodes(node):
                        here = child.name if isinstance(
                            child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            else enclosing
                        if isinstance(child, ast.Call):
                            name = getattr(child.func, "attr", None) \
                                or getattr(child.func, "id", None)
                            if name in ("owner_door", "OwnerDoor"):
                                sites.add((rel, here, name))
                        walk(child, here)
                walk(tree, "<module>")
        # MUST-HIT: the two web handlers are found, so an empty census is a
        # broken probe and never a pass
        # The away card's press handler is the third web door (task/3018):
        # his away flag and his fleet notice are his word, like a verdict.
        want = {("helm/web_core.py", "_api_decisions_verdict", "owner_door"),
                ("helm/web_core.py", "_api_decisions_comment", "owner_door"),
                ("helm/web_core.py", "_api_posture_post", "owner_door"),
                ("helm/ownerasks.py", "owner_door", "OwnerDoor")}
        self.assertEqual(sites, want,
                         "an OwnerDoor is the owner's authority on the "
                         "decision ledger and his away card; only his web "
                         "handlers mint one")

    def test_delivery_relays_the_verdicts_recorded_author(self):
        """deliver_verdict sent every verdict FROM the owner's handle,
        whatever the row said. Rows are append-only and older writers took a
        caller string, so the relay reads the author off the record."""
        rid = self.file_card(asker="builder-7")["id"]
        planted = dict(ownerasks.decision_rows()[rid])
        planted.update(status="decided", verdict={
            "choice": "1", "label": "Archive to cold file",
            "consequence": "resolves fast", "comment": "",
            "by": "builder-3", "ts": pk.now_ts()})
        self.assertTrue(ownerasks.eventledger.append(
            ownerasks.decisions_path(), planted))
        row, err = ownerasks.deliver_verdict(rid)
        self.assertIsNone(err)
        lane = self.dm_lane("builder-7")
        self.assertEqual(len(lane), 1)
        self.assertEqual(lane[0]["from"], "builder-3")
        self.assertIn("verdict by builder-3", lane[0]["text"])
        # CONTROL: an owner verdict still relays from the owner, worded as before
        other = self.file_card(asker="builder-8")["id"]
        ruling(other, "1")
        _row, err = ownerasks.deliver_verdict(other)
        self.assertIsNone(err)
        lane = self.dm_lane("builder-8")
        self.assertEqual(lane[0]["from"], seats.owner_name())
        self.assertIn("] verdict on ", lane[0]["text"])

    def test_a_card_cannot_name_the_owner_as_its_asker(self):
        """The room line announcing a card is posted AS the asker, and the
        verdict is DM'd back to him. With the owner's own name as the asker,
        filing put a line in the room in HIS voice and addressed his ruling
        to himself, which never delivers."""
        ctx, opts, _e = ownerasks.parse_card_body(BODY)
        for name in ("daria", "owner"):
            row, why = ownerasks.file_decision("t", ctx, opts, name)
            self.assertIsNone(row, name)
            self.assertIn("owner", why)
        # POSITIVE CONTROL on the same door: a seat's return address files
        self.assertIsNotNone(ownerasks.file_decision("t", ctx, opts,
                                                     "builder-1")[0])


if __name__ == "__main__":
    unittest.main()
