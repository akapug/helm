#!/usr/bin/env python3
"""A signing failure resolves itself when its cause is measurably gone, is
scoped to its seat when that seat is dark, and pages the seat that owns fleet
maintenance while it is live — so the owner never has to find it on a web
panel and ask.

The measured case: the board read "signing DEGRADED · ds4pro · chat node
unreachable at http://127.0.0.1:8898 · first 2026-09-24T19:55:46Z · last
2026-09-24T21:51:21Z (19h ago)" while the node answered 200, because the
record could only be retired by a signed turn from ds4pro (dark since) or a
manual `helm chat transport ack`.

Hermetic: the node probe, the signer and the seat-liveness read are seams; no
arm reaches the live chat node, the live roster or the live sign-failure dir.
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
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-signheal-", var="HELM_HOME")

from helm import cell as cellmod  # noqa: E402
from helm import chat, human, seat_usability  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM_SOURCE",
            "HELM_CHAT_LOG", "MELD_CHAT_LOG", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "HELM_LANDER", "HELM_SCRATCH_GC",
            "HELM_CACHE_DIR")

NODE = "http://127.0.0.1:1"
READY_SIGNER = {"configured": True, "usable": True, "state": "ready",
                "reason": "signer ready"}
UNSET_SIGNER = {"configured": False, "usable": False, "state": "unset",
                "reason": "HELM_CELL_BIN is unset"}
FIRST_TS = "2026-09-24T19:55:46Z"
DARK_SINCE = 1790000000.0            # 2026-09-21T14:13:20Z
DARK = {"dark": True, "why": "no live beacon", "since": DARK_SINCE,
        "seat": "ds4pro"}
LIVE = {"dark": False, "why": "beating now", "since": None, "seat": "seat-a"}
UNPROVEN = {"dark": None, "why": "not a roster seat", "since": None,
            "seat": None}


class HealBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-signheal-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = NODE
        os.environ["HELM_CELL_PROFILE"] = "reader-profile"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        getattr(chat, "_SEAT_DARK_MEMO", {}).clear()
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        failure_dir = chat.sign_failures_dir()
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        getattr(chat, "_SEAT_DARK_MEMO", {}).clear()
        shutil.rmtree(failure_dir, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def record(self, profile, code, reason, epoch=100.0, ts=FIRST_TS):
        return chat._record_sign_failure(profile, chat._diag(
            code, reason, event_epoch=epoch, event_ts=ts))

    @contextlib.contextmanager
    def world(self, head=None, head_error=None, signer=READY_SIGNER,
              dark=None, now=1000.0):
        """The probe world: what the node, the signer and the seat read say."""
        node = (mock.patch.object(chat, "node_head", side_effect=head_error)
                if head_error is not None else
                mock.patch.object(chat, "node_head", return_value=head))
        darkness = dark if callable(dark) else (lambda p, now=None: dark or UNPROVEN)
        with node, \
             mock.patch.object(cellmod, "bin_status", return_value=signer), \
             mock.patch.object(chat, "_seat_darkness", side_effect=darkness,
                               create=True), \
             mock.patch.object(chat.time, "time", return_value=now):
            yield

    def cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["transport", "status"])
        return rc, out.getvalue()


# ---------------------------------------------------------------------------
# 1. SELF-RESOLVE: a record whose cause is measurably gone resolves itself
# ---------------------------------------------------------------------------

class SelfResolveTest(HealBase):
    def test_a_node_unreachable_record_resolves_when_the_node_answers_now(self):
        """THE MEASURED CASE. The node answers, so its unreachability is gone;
        the record resolves with evidence and nobody types an ack."""
        self.record("ds4pro", "node_unreachable",
                  "chat node unreachable at %s" % NODE)
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         ["ds4pro"])
        with self.world(head={"chain_index": 42}):
            st = chat.transport_status(fleet=True)
            rc, out = self.cli()
        self.assertEqual((st["mode"], st.get("state")), ("ready", "READY"))
        self.assertEqual(st["head"], 42)
        self.assertEqual(chat.sign_failures(), [])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "helm chat transport: READY (UNPROVEN) — chain #42\n")
        # WHO, WHAT PROBE, WHEN — and the failure it retired, kept as history.
        events = chat.sign_resolutions()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev["profile"], ev["code"]), ("ds4pro", "node_unreachable"))
        self.assertIn("pid %d" % os.getpid(), ev["by"])
        self.assertIn("fleet status", ev["by"])
        self.assertIn("%s/api/receipts" % NODE, ev["probe"])
        self.assertIn("chain head #42", ev["probe"])
        self.assertEqual(ev["resolved_epoch"], 1000.0)
        self.assertTrue(ev["resolved_at"])
        self.assertEqual((ev["first_failure"], ev["failure_count"]),
                         (FIRST_TS, 1))
        self.assertIn("unreachable", ev["reason"])
        self.assertEqual(st["resolved"][0]["profile"], "ds4pro")

    def test_a_resolution_is_history_not_a_receipt_and_a_later_failure_reopens(self):
        chat._clear_sign_failure("ds4pro", succeeded_at=50.0)     # a real receipt
        self.record("ds4pro", "node_unreachable",
                  "chat node unreachable at %s" % NODE)
        self.assertEqual(chat._signed_success_epoch("ds4pro"), 50.0)
        with self.world(head={"chain_index": 7}):
            chat.transport_status(fleet=True)
        # The receipt survives the resolution and the resolution is not one.
        self.assertEqual(chat._signed_success_epoch("ds4pro"), 50.0)
        self.assertEqual(chat.fleet_signed(), ("ds4pro", 50.0))
        # A delayed failure measured BEFORE the resolution cannot resurrect it.
        self.record("ds4pro", "node_unreachable", "late", epoch=500.0, ts="late")
        self.assertEqual(chat.sign_failures(), [])
        # A genuinely later failure reopens it, and the history stays.
        self.record("ds4pro", "send_failed", "new failure", epoch=2000.0,
                  ts="2026-09-25T20:00:00Z")
        self.assertEqual([f["reason"] for f in chat.sign_failures()],
                         ["new failure"])
        self.assertEqual([e["code"] for e in chat.sign_resolutions()],
                         ["node_unreachable"])

    def test_an_ack_before_the_failure_is_not_laundered_into_a_receipt_by_the_resolution(self):
        """ack -> a later failure -> the cause measured gone. The ack's
        watermark rode into the failure record as `_success_epoch` with its
        `acknowledged_at` marker dropped, so the resolution kept it as the
        profile's receipt and the profile read SIGNED, then led the fleet as
        its newest signer, having never signed."""
        self.record("seat-b", "node_unreachable",
                  "chat node unreachable at %s" % NODE, epoch=100.0)
        with mock.patch.object(chat.time, "time", return_value=200.0):
            self.assertEqual(chat.acknowledge_sign_failures("seat-b"),
                             (["seat-b"], None))
        self.assertEqual(chat._signed_success_epoch("seat-b"), 0.0)
        self.record("seat-b", "node_unreachable",
                  "chat node unreachable at %s" % NODE, epoch=300.0,
                  ts="2026-09-24T20:00:00Z")
        self.assertEqual(chat._signed_success_epoch("seat-b"), 0.0)
        self.assertIsNone(chat.fleet_signed())
        os.environ["HELM_CELL_PROFILE"] = "seat-b"
        with self.world(head={"chain_index": 4}, now=400.0):
            st = chat.transport_status(fleet=True)
            own = chat.transport_status()
        self.assertEqual(chat.sign_failures(), [])
        self.assertEqual(chat._signed_success_epoch("seat-b"), 0.0)
        self.assertIsNone(chat.fleet_signed())
        self.assertEqual((st["mode"], own["mode"]), ("ready", "ready"))

    def test_a_record_whose_cause_is_still_present_stays_DEGRADED_and_says_so(self):
        self.record("ds4pro", "node_unreachable",
                  "chat node unreachable at %s" % NODE)
        with self.world(head=None):
            st = chat.transport_status(fleet=True)
            rc, out = self.cli()
        self.assertEqual((st["mode"], st["state"], st["profile"]),
                         ("degraded", "DEGRADED", "ds4pro"))
        self.assertEqual(st["cause_state"], "PRESENT")
        self.assertIn("still does not answer at %s" % NODE, st["cause"])
        self.assertEqual(rc, 1)
        self.assertIn("helm chat transport: DEGRADED profile 'ds4pro'", out)
        self.assertIn("cause: the chat node still does not answer", out)
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         ["ds4pro"])
        self.assertEqual(chat.sign_resolutions(), [])

    def test_an_unreadable_probe_is_UNKNOWN_never_healthy(self):
        """A reader with no signer of its own (the timer's shape) cannot say
        whether the node answers when the probe raises, so the record is
        UNKNOWN. A reader WITH a usable signer keeps the older contract: its
        own posts ride that node, so an exploding probe is its own live
        node_unreachable, DEGRADED, with the record beside it as UNKNOWN."""
        self.record("ds4pro", "node_unreachable",
                  "chat node unreachable at %s" % NODE)
        with self.world(head_error=OSError("probe exploded"),
                        signer=UNSET_SIGNER):
            st = chat.transport_status(fleet=True)
            rc, out = self.cli()
        with self.world(head_error=OSError("probe exploded")):
            own = chat.transport_status(fleet=True)
        self.assertEqual((own["mode"], own["code"], own["cause_state"]),
                         ("degraded", "node_unreachable", "PRESENT"))
        self.assertEqual([(f["profile"], f["cause_state"])
                          for f in own["failed_profiles"]][1:],
                         [("ds4pro", "UNKNOWN")])
        self.assertEqual((st["mode"], st["state"], st["cause_state"]),
                         ("unknown", "UNKNOWN", "UNKNOWN"))
        self.assertIn("probe exploded", st["cause"])
        self.assertEqual(chat.transport_label(st), "unknown")
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith(
            "helm chat transport: UNKNOWN profile 'ds4pro': chat node "
            "unreachable at %s — first %s" % (NODE, FIRST_TS)), out)
        self.assertIn("probe exploded", out)
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         ["ds4pro"])

    def test_a_record_naming_another_node_is_UNKNOWN_where_that_node_is_not_probed(self):
        self.record("ds4pro", "node_unreachable",
                  "chat node unreachable at http://10.9.9.9:8898")
        with self.world(head={"chain_index": 3}):
            st = chat.transport_status(fleet=True)
        self.assertEqual((st["mode"], st["cause_state"]), ("unknown", "UNKNOWN"))
        self.assertIn("http://10.9.9.9:8898", st["cause"])
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         ["ds4pro"])

    def test_a_signer_unavailable_record_resolves_when_the_signer_is_usable(self):
        self.record("seat-b", "signer_unavailable",
                  "HELM_CELL_BIN is set but the signer path does not exist")
        with self.world(head={"chain_index": 9}):
            st = chat.transport_status(fleet=True)
        self.assertEqual(st["mode"], "ready")
        self.assertEqual(chat.sign_failures(), [])
        ev = chat.sign_resolutions()[0]
        self.assertEqual((ev["profile"], ev["code"]),
                         ("seat-b", "signer_unavailable"))
        self.assertIn("signer", ev["probe"])
        self.assertIn("usable", ev["probe"])

    def test_a_signer_unavailable_record_is_UNKNOWN_where_no_signer_is_configured(self):
        self.record("seat-b", "signer_unavailable",
                  "HELM_CELL_BIN is set but the signer path does not exist")
        with self.world(head={"chain_index": 9}, signer=UNSET_SIGNER):
            st = chat.transport_status(fleet=True)
        self.assertEqual((st["mode"], st["cause_state"]), ("unknown", "UNKNOWN"))
        self.assertIn("no signer configured", st["cause"])
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         ["seat-b"])

    def test_an_unprobeable_failure_stays_DEGRADED_and_is_named_as_such(self):
        self.record("seat-a", "send_failed", "send failed")
        with self.world(head={"chain_index": 5}, dark=LIVE):
            st = chat.transport_status(fleet=True)
            rc, out = self.cli()
        self.assertEqual((st["mode"], st["cause_state"]),
                         ("degraded", "UNPROBED"))
        self.assertIn("not re-probeable", st["cause"])
        self.assertEqual(rc, 1)
        self.assertIn("cause: not re-probeable", out)
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         ["seat-a"])


# ---------------------------------------------------------------------------
# 2. SCOPE AND AGE: a dark seat's failure is its own line, not the fleet's
# ---------------------------------------------------------------------------

class ScopeTest(HealBase):
    SCOPED = ("ds4pro (dark since 2026-09-21T14:13:20Z): last signing attempt "
              "failed 15m ago (send_failed: send failed) — no live beacon")

    def test_a_dark_seats_failure_is_a_scoped_line_not_fleet_DEGRADED(self):
        self.record("ds4pro", "send_failed", "send failed")
        with self.world(head={"chain_index": 11}, dark=DARK):
            st = chat.transport_status(fleet=True)
            rc, out = self.cli()
        self.assertEqual((st["mode"], st["state"]), ("ready", "READY"))
        self.assertEqual([f["profile"] for f in st["dark_failures"]],
                         ["ds4pro"])
        self.assertEqual(st["scoped"], [self.SCOPED])
        self.assertEqual(st["head"], 11)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "helm chat transport: READY (UNPROVEN) — chain #11\n"
                              "helm chat transport: scoped: %s\n" % self.SCOPED)

    def test_a_dark_seats_acked_then_reopened_failure_never_leads_the_fleet_as_its_signer(self):
        """ack -> a later failure from a seat now dark. Scoping that failure
        away let fleet_signed read the record, whose `_success_epoch` was the
        ack's watermark with the marker dropped: the fleet read SIGNED as a
        seat that never signed."""
        self.record("ds4pro", "send_failed", "send failed", epoch=100.0)
        with mock.patch.object(chat.time, "time", return_value=200.0):
            self.assertEqual(chat.acknowledge_sign_failures("ds4pro"),
                             (["ds4pro"], None))
        self.record("ds4pro", "send_failed", "send failed again", epoch=300.0,
                  ts="2026-09-24T20:00:00Z")
        with self.world(head={"chain_index": 11}, dark=DARK, now=400.0):
            st = chat.transport_status(fleet=True)
        self.assertEqual([f["profile"] for f in st["dark_failures"]],
                         ["ds4pro"])
        self.assertIsNone(chat.fleet_signed())
        self.assertEqual((st["mode"], st.get("fleet_signer")), ("ready", None))

    def test_a_seat_asking_about_itself_is_never_scoped_away(self):
        os.environ["HELM_CELL_PROFILE"] = "ds4pro"
        self.record("ds4pro", "send_failed", "send failed")
        with self.world(head={"chain_index": 11}, dark=DARK):
            own = chat.transport_status()
            fleet = chat.transport_status(fleet=True)
        self.assertEqual((own["mode"], own["profile"]), ("degraded", "ds4pro"))
        self.assertEqual(fleet["mode"], "ready")
        self.assertEqual(fleet["scoped"], [self.SCOPED])

    def test_unproven_liveness_keeps_the_failure_loud(self):
        self.record("ds4pro", "send_failed", "send failed")
        with self.world(head={"chain_index": 11}, dark=UNPROVEN):
            st = chat.transport_status(fleet=True)
        self.assertEqual((st["mode"], st["profile"]), ("degraded", "ds4pro"))
        self.assertEqual(st.get("dark_failures") or [], [])
        self.assertEqual(st["liveness"], "unknown")

    def test_a_live_seat_stays_DEGRADED_beside_a_scoped_dark_one(self):
        self.record("ds4pro", "send_failed", "send failed", epoch=100.0)
        self.record("seat-a", "join_failed", "join failed", epoch=200.0,
                  ts="2026-09-24T20:00:00Z")
        by = {"ds4pro": DARK, "seat-a": LIVE}
        with self.world(head={"chain_index": 12},
                        dark=lambda p, now=None: by[p]):
            st = chat.transport_status(fleet=True)
        self.assertEqual((st["mode"], st["profile"]), ("degraded", "seat-a"))
        self.assertEqual([f["profile"] for f in st["failed_profiles"]],
                         ["seat-a"])
        self.assertEqual([f["profile"] for f in st["dark_failures"]],
                         ["ds4pro"])
        self.assertEqual(st["liveness"], "live")

    def test_a_hostile_profile_is_laundered_on_the_scoped_line(self):
        raw = "ds4pro\x1b[2J‮"
        self.record(raw, "send_failed", "send\x1b[31m failed")
        with self.world(head={"chain_index": 11}, dark=DARK):
            st = chat.transport_status(fleet=True)
        self.assertEqual(len(st["scoped"]), 1)
        self.assertIn("ds4pro", st["scoped"][0])
        for ch in ("\x1b", "‮"):
            self.assertNotIn(ch, st["scoped"][0])
            self.assertNotIn(ch, json.dumps(st["dark_failures"],
                                            ensure_ascii=False))


class OwnerWordsTest(HealBase):
    """Console walk 4, P1 2. History's top card and the Chat view printed a
    failure's `reason`, `cause` and `remediation` verbatim, so the owner read
    "Relaunch through `helm launch`" and "`helm chat transport ack --profile
    meta-claude`". Those fields are the agents' and keep their verbs. Every
    failure the transport publishes also carries `owner_say`, the same failure
    in plain words that names who repairs it, and each scoped line has its
    owner copy in `owner_scoped`."""

    CONFLICT = ("identity conflict: this process resolves to seat 'ds4pro' "
                "but the environment names profile 'seat-a'. Relaunch through "
                "`helm launch` (which sets both vars to the seat)")

    def test_every_failure_code_has_owner_words_that_name_no_helm_verb(self):
        from tests._ownerverbs import owner_verbs
        codes = sorted(chat._REMEDIATION) + ["a_code_helm_never_wrote"]
        states = ({"cause_state": "PRESENT"}, {"cause_state": "UNPROBED"},
                  {"cause_state": "UNKNOWN"},
                  {"cause_state": "UNPROBED", "liveness": "dark"})
        # CONTROL: the agents' text for these codes names helm verbs, so the
        # sweep below reads a copy that differs from it
        self.assertEqual(owner_verbs(chat._diag("identity_conflict", "x")
                                     ["remediation"]), ["launch", "chat"])
        for code in codes:
            for extra in states:
                d = chat._diag(code, "failed")
                row = {"from": "seat-a", "text": "x", "transport": dict(
                    extra, state="DEGRADED", profile="seat-a", code=code,
                    reason=self.CONFLICT, remediation=d["remediation"])}
                say = chat.public_rows([row])[0]["transport"].get(
                    "owner_say") or ""
                self.assertIn("seat-a", say, (code, extra))
                self.assertIn("lead", say, (code, extra, say))
                self.assertEqual(owner_verbs(say), [], (code, extra, say))

    def test_the_status_carries_owner_words_beside_the_agents_text(self):  # noqa: VACUOUS_ASSERTION — each no-verb absence is on a sentence first asserted to name its seat and who repairs it, and the agents' fields on the same read are asserted to carry their verbs
        from tests._ownerverbs import owner_verbs
        self.record("seat-a", "send_failed", "send failed", epoch=200.0,
                    ts="2026-09-24T20:00:00Z")
        self.record("ds4pro", "identity_conflict", self.CONFLICT)
        by = {"seat-a": LIVE, "ds4pro": DARK}
        with self.world(head={"chain_index": 12},
                        dark=lambda p, now=None: by[p]):
            st = chat.transport_status(fleet=True)
        self.assertEqual((st["mode"], st["profile"]), ("degraded", "seat-a"))
        # THE AGENTS' FIELDS KEEP THEIR VERBS
        self.assertIn("helm chat", st["remediation"])
        self.assertIn("helm launch", st["scoped"][0])
        # the owner's copy of the lead failure, and of the scoped line
        say = st.get("owner_say") or ""
        self.assertIn("seat-a", say)
        self.assertIn("its lead", say)
        self.assertEqual(owner_verbs(say), [], say)
        scoped = st.get("owner_scoped") or [""]
        self.assertEqual(len(scoped), 1, scoped)
        self.assertIn("ds4pro is not running", scoped[0])
        self.assertEqual(owner_verbs(scoped[0]), [], scoped[0])
        for f in st["failed_profiles"] + st["dark_failures"]:
            self.assertEqual(owner_verbs(f.get("owner_say") or "helm chat"),
                             [], f)

    def test_a_status_with_no_failure_carries_no_owner_words(self):
        """A SIGNED or READY read has nothing to say: no key is added, so the
        wire's exact shape for a healthy read is unchanged."""
        with self.world(head={"chain_index": 3}):
            st = chat.transport_status(fleet=True)
        self.assertEqual(st["mode"], "ready")
        self.assertFalse({"owner_say", "owner_scoped"} & set(st), st)


class SigningDarkPredicateTest(HealBase):
    """seat_usability.signing_dark composes the readers helm already has —
    presence, the attendance register re-proven by a live beacon probe, and
    the family burn flag — for the seat behind one signing profile."""

    def roster(self, **row):
        base = {"last_seen": 1000.0 - 3600,
                "runtime": {"family": "ds4pro", "agent_harness": "claude"},
                "runtime_verified": True}
        base.update(row)
        return lambda: {"ds4pro": base}

    def att(self, state, at=990.0):
        return {"state": state, "at": at, "since": 500.0,
                "why": "no live beacon: helm cannot wake it"}

    def ask(self, register, probe=([], None), flag=None, name="ds4pro"):
        with mock.patch.object(seat_usability.time, "time", return_value=1000.0):
            return seat_usability.signing_dark(
                name, now=1000.0, register=register,
                probe=lambda n: probe, flag=lambda fam: flag)

    def test_absent_and_deaf_with_no_live_beacon_is_dark_since_its_last_beat(self):
        got = self.ask(self.roster(attendance=self.att("DEAF")))
        self.assertIs(got["dark"], True)
        self.assertIn("no live beacon", got["why"])
        self.assertEqual((got["since"], got["seat"]), (1000.0 - 3600, "ds4pro"))

    def test_a_seat_that_beats_now_is_live_whatever_the_register_says(self):
        got = self.ask(self.roster(last_seen=990.0,
                                   attendance=self.att("DEAF")))
        self.assertEqual(got["seat"], "ds4pro")
        self.assertIn("beat", got["why"])
        self.assertIs(got["dark"], False)

    def test_a_deaf_register_overturned_by_a_live_beacon_is_live(self):
        got = self.ask(self.roster(attendance=self.att("DEAF")),
                       probe=([4242], None))
        self.assertIs(got["dark"], False)
        self.assertIn("re-armed", got["why"])

    def test_a_RED_family_with_no_beat_is_dark(self):
        got = self.ask(self.roster(), flag={"colour": "RED",
                                            "cause": "credit exhausted"})
        self.assertIs(got["dark"], True)
        self.assertIn("RED", got["why"])
        self.assertIn("credit exhausted", got["why"])

    def test_no_roster_row_and_no_proof_are_UNKNOWN_never_dark(self):
        missing = self.ask(self.roster(), name="helm-agent")
        self.assertIsNone(missing["dark"])
        self.assertIn("helm-agent", missing["why"])
        unproven = self.ask(self.roster())
        self.assertEqual(unproven["seat"], "ds4pro")
        self.assertTrue(unproven["why"])
        self.assertIsNone(unproven["dark"])


# ---------------------------------------------------------------------------
# 3. PAGE: a live degradation reaches the maintenance seat without the owner
# ---------------------------------------------------------------------------

def _st(state, profile="seat-a", code="send_failed", reason="send failed",
        cause="not re-probeable — clears when seat-a next commits a signed turn",
        **extra):
    if state == "ok":
        return dict({"mode": "signed", "state": "SIGNED", "head": 7}, **extra)
    row = {"profile": profile, "code": code, "reason": reason,
           "first_failure": FIRST_TS, "last_failure": FIRST_TS,
           "failure_count": 2, "age_s": 600, "last_age_s": 60,
           "remediation": "inspect the node, then retry",
           "cause_state": "UNKNOWN" if state == "UNKNOWN" else "UNPROBED",
           "cause": cause}
    return dict(row, mode=state.lower(), state=state, failed_profiles=[row],
                **extra)


class PageTest(HealBase):
    LANDER = "signing-probe-lander"

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NODE_URL"] = ""   # the alert posts stay unsigned
        os.environ["HELM_CHAT_NAME"] = "tester"
        os.environ["HELM_LANDER"] = self.LANDER
        self.clock = 10000.0

    def alerts(self):
        rows, _total = chat.read(chat.dm_room(self.LANDER))
        return [r.get("text") or "" for r in rows]

    def room_alerts(self):
        rows, _total = chat.read(chat.FLUSH_ALERT_ROOM)
        return [r.get("text") or "" for r in rows
                if r.get("from") == getattr(chat, "SIGN_WATCHDOG_WHO", "?")]

    def check(self, state, **kw):
        self.clock += 180.0
        with mock.patch.object(chat, "transport_status",
                               return_value=_st(state, **kw)):
            return chat.signing_watchdog(now=self.clock)

    def test_the_second_consecutive_live_failure_pages_the_maintenance_seat(self):
        self.check("DEGRADED")
        after_one = len(self.alerts())
        self.check("DEGRADED")
        got = self.alerts()
        self.assertEqual(len(got), 1)
        self.assertEqual(after_one, 0)
        self.assertIn("CHAT SIGNING DEGRADED", got[0])
        self.assertIn("seat-a", got[0])
        self.assertIn("2 consecutive checks", got[0])
        self.assertIn("helm chat transport status", got[0])
        self.assertIn("not re-probeable", got[0])
        # ADDRESSED TO THE MAINTENANCE SEAT, and to nobody else.
        shouted = self.room_alerts()
        self.assertEqual(len(shouted), 1)
        self.assertTrue(shouted[0].startswith("@%s " % self.LANDER))
        self.assertEqual(shouted[0].count("@"), 1)
        # A MACHINE LABEL: its plain rows are pulled, never pushed to every
        # seat; the page reaches the maintenance seat because it is addressed.
        from helm import machine_senders
        self.assertTrue(machine_senders.is_machine(chat.SIGN_WATCHDOG_WHO))

    def test_a_persistent_failure_re_pages_on_doubling_and_says_how_old(self):
        for _ in range(8):
            self.check("DEGRADED")
        got = self.alerts()
        self.assertEqual(len(got), 3, "expected pages at checks 2, 4 and 8")
        self.assertIn("2 consecutive checks over 3m", got[0])
        self.assertIn("4 consecutive checks over 9m", got[1])
        self.assertIn("8 consecutive checks over 21m", got[2])

    def test_recovery_says_RESTORED_and_re_arms(self):
        self.check("DEGRADED")
        self.check("DEGRADED")
        self.assertEqual(len(self.alerts()), 1)
        self.check("ok", resolved=[{"profile": "seat-a", "code": "send_failed",
                                  "probe": "the chat node answered"}])
        closed = self.alerts()
        self.assertEqual(len(closed), 2)
        self.assertIn("CHAT SIGNING RESTORED", closed[1])
        self.assertIn("the chat node answered", closed[1])
        self.check("DEGRADED")
        self.check("DEGRADED")
        self.assertEqual(len(self.alerts()), 3)
        self.assertIn("CHAT SIGNING DEGRADED", self.alerts()[2])

    def test_a_RESTORED_page_over_an_EXPECTED_state_says_it_is_still_unsigned(self):
        """Walk 4 cure, reviewer arm: a live failure reclassified as a seat's
        expected state (a seat run outside helm launch under a shell's
        profile) closes the episode, but nothing was restored, and the page
        says that seat still posts unsigned. RED on 92d76ce9051: "no live
        signing failure remains" and nothing more."""
        self.check("DEGRADED")
        self.check("DEGRADED")
        self.check("ok", expected_failures=[{
            "profile": "seat-a", "code": "identity_conflict",
            "inherited_profile": "shell-profile"}])
        closed = self.alerts()
        self.assertEqual(len(closed), 2)
        self.assertIn("CHAT SIGNING RESTORED", closed[1])
        self.assertIn("1 now scoped as the expected state", closed[1])
        self.assertIn("still go out unsigned", closed[1])

    def test_an_undelivered_page_is_never_recorded_and_is_retried(self):
        with mock.patch.object(chat, "_deliver_flush_alert",
                               return_value=False) as dead:
            self.check("DEGRADED")
            self.check("DEGRADED")
            while_down = len(self.alerts())
        self.check("DEGRADED")
        landed = self.alerts()
        self.assertEqual(len(landed), 1, "a failed page must be retried")
        self.assertIn("3 consecutive checks", landed[0])
        self.assertEqual(dead.call_count, 1)
        self.assertEqual(while_down, 0)

    def test_an_UNKNOWN_cause_pages_and_a_DEGRADED_one_escalates_at_once(self):
        self.check("UNKNOWN", cause="the node probe itself failed")
        self.check("UNKNOWN", cause="the node probe itself failed")
        first = self.alerts()
        self.assertEqual(len(first), 1)
        self.assertIn("CHAT SIGNING UNKNOWN", first[0])
        self.assertIn("the node probe itself failed", first[0])
        # A WORSENING fires on its own next check, not at the doubled one.
        self.check("DEGRADED")
        got = self.alerts()
        self.assertEqual(len(got), 2)
        self.assertIn("CHAT SIGNING DEGRADED", got[1])

    def test_a_new_profile_failing_mid_episode_pages_at_once(self):
        self.check("DEGRADED")
        self.check("DEGRADED")
        self.assertEqual(len(self.alerts()), 1)
        self.check("DEGRADED", profile="seat-c")
        got = self.alerts()
        self.assertEqual(len(got), 2)
        self.assertIn("seat-c", got[1])

    def test_a_dark_seats_scoped_failure_never_pages(self):
        scoped = {"dark_failures": [{"profile": "ds4pro"}],
                  "scoped": [ScopeTest.SCOPED]}
        for _ in range(5):
            self.check("ok", **scoped)
        quiet = len(self.alerts())
        self.check("DEGRADED")
        self.check("DEGRADED")
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(quiet, 0)

    def test_the_timers_all_rooms_log_flush_runs_the_signing_watchdog(self):
        with mock.patch.object(chat, "signing_watchdog", create=True,
                               return_value={"state": "ok"}) as dog:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                chat.cmd_chat(["log-flush"])
            self.assertEqual(dog.call_count, 1)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                chat.cmd_chat(["log-flush", "--room", "helm"])
            self.assertEqual(dog.call_count, 1, "a scoped repair run is not the fleet's check")


# ---------------------------------------------------------------------------
# 4. SURFACES: every reader of the one transport truth, in all four states
# ---------------------------------------------------------------------------

class SurfaceTest(HealBase):
    def test_the_tui_line_says_UNKNOWN_with_its_cause(self):
        self.record("ds4pro", "node_unreachable",
                  "chat node unreachable at %s" % NODE)
        with self.world(head_error=OSError("probe exploded"),
                        signer=UNSET_SIGNER):
            st = chat.transport_status()
        line = human.status_line(dict(human.model_new(), status=st), 400)
        self.assertIn("UNKNOWN", line)
        self.assertIn("ds4pro", line)
        self.assertIn("probe exploded", line)

    def test_doctor_warns_on_UNKNOWN_and_reports_a_scoped_line_without_warning(self):
        from helm import doctor
        unknown = _st("UNKNOWN", cause="the node probe itself failed")
        scoped = _st("ok", dark_failures=[{"profile": "ds4pro"}],
                     scoped=[ScopeTest.SCOPED])
        with mock.patch.object(chat, "node_url", return_value=None), \
             mock.patch.object(chat, "transport_status", return_value=unknown):
            warn = [m for lvl, m in doctor.check_chat_node()
                    if lvl == doctor.WARN]
        self.assertTrue(any("UNKNOWN profile 'seat-a'" in m for m in warn), warn)
        with mock.patch.object(chat, "node_url", return_value=None), \
             mock.patch.object(chat, "transport_status", return_value=scoped):
            rows = doctor.check_chat_node()
        said = [m for _lvl, m in rows]
        self.assertTrue(any(ScopeTest.SCOPED in m for m in said), said)
        self.assertFalse(any(ScopeTest.SCOPED in m for lvl, m in rows
                             if lvl == doctor.WARN), rows)


    def test_node_status_exits_1_on_UNKNOWN_and_prints_a_scoped_line_without_failing(self):
        from helm import chatnode
        unknown = _st("UNKNOWN", cause="the node probe itself failed")
        scoped = _st("ok", dark_failures=[{"profile": "ds4pro"}],
                     scoped=[ScopeTest.SCOPED])
        runs = {}
        for name, st in (("unknown", unknown), ("scoped", scoped)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(chat, "transport_status", return_value=st), \
                 mock.patch.object(chatnode, "_systemctl",
                                   return_value=(0, "active")), \
                 mock.patch.object(cellmod, "get_json", return_value=[]), \
                 contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                rc = chatnode._status([])
            runs[name] = (rc, out.getvalue(), err.getvalue())
        self.assertEqual(runs["unknown"][0], 1)
        self.assertIn("UNKNOWN profile 'seat-a'", runs["unknown"][2])
        self.assertIn("helm chat node: signing scoped: " + ScopeTest.SCOPED,
                      runs["scoped"][1])
        self.assertEqual(runs["scoped"][0], 0)


class WebBoardRuntimeTest(unittest.TestCase):
    """The ledger strip the owner read, driven by the real function in node."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        from helm import web_ui_loader
        from tests.test_web_chat_client_runtime import _extract_fn
        cls.fn = _extract_fn(web_ui_loader.read_text(), "ledgerStrip")

    def render(self, transport):
        code = """
const out = {html: ""};
function $() { return out; }
function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;")
  .replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function ledgerHTML(el, html) { el.html = html; }
function ledgerAgeHTML(ts) { return "AGE"; }
let NATIVE_COUNT = 1;
""" + self.fn + """
ledgerStrip({status: {healthy: true, dag_height: 3, latest_height: 3},
             transport: %s});
console.log(JSON.stringify(out.html));
""" % json.dumps(transport)
        p = subprocess.run([self.node, "-e", code], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_healthy_scoped_degraded_and_unknown_render_as_four_states(self):
        """Each state over the wire's real shape: the owner's copy
        `_owner_words` adds beside the agents' text, which the strip draws in
        place of the reason, the cause and the agents' scoped line (console
        walk 4, P1 2)."""
        dark = {"profile": "ds4pro", "code": "send_failed",
                "cause_state": "UNPROBED", "liveness": "dark"}
        healthy = self.render({"mode": "signed", "state": "SIGNED"})
        scoped_st = chat._owner_words({"mode": "signed", "state": "SIGNED",
                                       "dark_failures": [dark],
                                       "scoped": [ScopeTest.SCOPED]})
        degraded_st = chat._owner_words(_st("DEGRADED"))
        unknown_st = chat._owner_words(
            _st("UNKNOWN", cause="the node probe itself failed"))
        scoped, degraded, unknown = (self.render(st) for st in (
            scoped_st, degraded_st, unknown_st))
        self.assertIn('signing <span class="lbadge final">live</span>', healthy)
        self.assertNotIn("DEGRADED", healthy)
        self.assertIn('signing <span class="lbadge final">live</span>', scoped)
        self.assertIn(scoped_st["owner_scoped"][0], scoped)
        self.assertIn("ds4pro is not running now", scoped)
        self.assertNotIn(ScopeTest.SCOPED, scoped)
        self.assertNotIn("DEGRADED", scoped)
        self.assertIn('<span class="lbadge tent">DEGRADED</span> · '
                      + degraded_st["owner_say"], degraded)
        self.assertIn("seat-a", degraded_st["owner_say"])
        self.assertIn('<span class="lbadge tent">UNKNOWN</span> · '
                      + unknown_st["owner_say"], unknown)
        self.assertIn("could not be checked from here", unknown)
        self.assertNotIn("the node probe itself failed", unknown)
        self.assertNotIn("DEGRADED", unknown)


if __name__ == "__main__":
    unittest.main()
