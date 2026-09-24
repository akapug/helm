#!/usr/bin/env python3
"""helm — a NON-PANE session may not redefine a seat's row.

WHY THIS FILE EXISTS, and why it is shaped the way it is.

The first attempt at this (reverted the same day; later rebases erased both
the attempt and its revert from main's history) treated
CLAUDE_CODE_CHILD_SESSION=1 as "I am a subagent" and switched the process's
SEAT NAME on it. It self-locked the whole fleet: that marker is set in EVERY
SEAT'S TOOL-CALL SHELL, so every seat's every `helm` CLI call resolved to
`agent-<sid8>`. Real damage: chat rows posted under `agent-00000000` and
`agent-00000000` instead of their seat names, falsifying the review record, and
`helm chat` refusing verbs with "this session is agent-00000000".

MEASURED GROUND TRUTH afterwards (six representative seats) — the two facts
that shape this design:
  1. A seat's own tool-call shell and its subagent's tool-call shell have
     BYTE-IDENTICAL environments: same CLAUDE_CODE_CHILD_SESSION=1, same
     CLAUDE_CODE_SESSION_ID, no key present in one and absent in the other.
     So NO environment test can tell them apart.
  2. Binding the env session id against the roster row does not save it either:
     4 of 6 live seats were bound, but console-design and opus-integrator were
     NOT (console-design's real id had been evicted from its own row at
     SESSIONS_KEPT; opus-integrator's row held eight other ids). Both would have
     become `agent-<sid8>` — i.e. that predicate reproduces the outage on the
     two seats the incident was about, including the coordinator's own.

So this file pins a design with a much smaller blast radius: the seat NAME is
never switched, and the only thing a non-pane session loses is the right to
redefine the row's CURRENT session binding. A wrong verdict can leave one row's
`session` un-refreshed by one join; it can never change authorship, refuse a
verb, or lock a seat out of its own row.

SCOPE, stated up front because the sentence keeps drifting wider than the
behaviour. `nonpane_session()` detects DIFFERENT-SID HOOK PROCESSES — not
"subagents". A seat measured from inside its own turn-loop that an Agent-tool
SIDECHAIN carries the PARENT's sessionId (isSidechain:true, own agentId, same
sessionId on every row, same env sid in its Bash tools), so that class is
invisible to this predicate; SameSidSidechainLimitTest pins that as a KNOWN
LIMIT and pins that it is harmless. Two guards here are class-INDEPENDENT and
cover every join regardless (the temp-cwd refusal and the eviction order), which
is what makes the gap survivable. A lingering same-name child keeping a dead
parent's presence fresh remains OPEN — the only real discriminator is
isSidechain/agentId in the TRANSCRIPT, and until a hook payload carries
equivalent provenance that is a harness-layer gap, not a helm bug.
"""
import contextlib
import io
import json
import glob
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, pk, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "HELM_CHAT_ROOM",
            "HELM_CHAT_ROOM_SOURCE", "HELM_CHAT_OWNER_NAMES", "HELM_PROC",
            "HELM_ADOPTED_DIR", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
            "CODEX_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE",
            "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")

SEAT = "codex-3"
BOUND = "32285620-9279-4516-a44c-1a1081294e88"     # the seat's OWN live sid
CHILD = "aaaabbbb-cccc-dddd-eeee-ffff00001111"     # a fan-out subagent's sid
WORK = "/home/tester/dev/example/helm"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-nonpane-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # The fixture homes its posts EXPLICITLY through the same env seam
        # launched seats use — the old accidental "main" default, now stated.
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        chat._ensure_dir()

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the REAL live shape the unit tests never modelled ------------------
    def seat_shell(self, sid=BOUND):
        """A SEAT's own tool-call shell, exactly as measured on the live fleet:
        the declared name, the child marker SET, and the pane's own session
        id. Every `helm` CLI call a seat makes runs in this shape."""
        os.environ["HELM_CHAT_NAME"] = SEAT
        os.environ["CLAUDE_CODE_CHILD_SESSION"] = "1"
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid

    def seated(self, cwd=WORK):
        pk.write_json(seats.roster_path(), {SEAT: {
            "session": BOUND, "sessions": [BOUND], "cwd": cwd,
            "project": os.path.basename(cwd), "home_room": "helm-dogfood",
            "home_room_source": "explicit"}})

    def cmd(self, verb, args=(), room="main"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), room)
        return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 1. THE REGRESSION PIN — the shape that broke the fleet
# ---------------------------------------------------------------------------

class SeatShellIsTheSeatTest(Base):
    """Pin 1 + 3 + 4 from the correction. Each of these FAILS against the
    reverted CHILD_SESSION attempt, which answered `agent-32285620` to all
    of them."""

    def test_a_seat_shell_with_the_child_marker_is_still_the_seat(self):
        self.seated()
        self.seat_shell()
        self.assertEqual(seats.acting_seat(BOUND), SEAT)
        self.assertEqual(seats.own_name(), SEAT)
        self.assertFalse(seats.foreign_seat(SEAT))

    def test_the_child_marker_alone_never_changes_a_seat_name(self):
        """The marker is present in every seat's tool-call shell, so it must
        have NO bearing on identity — with or without a bound row, with or
        without a session id at all."""
        self.seat_shell()
        for roster in ({}, {SEAT: {"session": "something-else",
                                   "sessions": ["something-else"]}}):
            pk.write_json(seats.roster_path(), roster)
            for sid in (BOUND, CHILD, ""):
                if sid:
                    os.environ["CLAUDE_CODE_SESSION_ID"] = sid
                else:
                    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                self.assertEqual(seats.own_name(), SEAT)
                self.assertEqual(seats.acting_seat(sid or None), SEAT)

    def test_an_unbound_session_is_still_the_seat(self):
        """The measured killer: console-design and opus-integrator were both
        running session ids ABSENT from their own rows. Resolution must not
        depend on the row remembering the id."""
        pk.write_json(seats.roster_path(), {SEAT: {
            "session": "0000aaaa-0000-0000-0000-000000000000",
            "sessions": ["0000aaaa-0000-0000-0000-000000000000"]}})
        self.seat_shell(CHILD)
        self.assertEqual(seats.acting_seat(CHILD), SEAT)

    def test_first_join_with_nothing_bound_does_not_lock_the_seat_out(self):
        """Pin 3: an empty roster, a brand-new seat, the child marker set."""
        self.seat_shell()
        seat, _line = seats.join(session=BOUND, cwd=WORK)
        self.assertEqual(seat, SEAT)
        row = seats.roster()[SEAT]
        self.assertEqual(row["session"], BOUND)
        self.assertEqual(row["cwd"], WORK)
        self.assertEqual(row["project"], "helm")
        self.assertTrue(seats.touch_seen(SEAT))
        self.assertEqual(seats.presence_of(seats.last_seen(SEAT, {})), "fresh")

    def test_a_seat_shell_posts_under_the_seat_name(self):
        """Pin 4 — AUTHORSHIP, the damage the fleet actually saw. Rows landed
        as `agent-0fa7c4ed` / `agent-32285620` and falsified the review
        record, so pin the author, not only the resolver."""
        self.seated()
        self.seat_shell()
        self.assertEqual(chat.whoname(), SEAT)
        row = chat.post("VCS SEAM RE-GATE = FIX")
        self.assertEqual(row["from"], SEAT)
        rows, _total = chat.read("main")
        self.assertEqual(rows[-1]["from"], SEAT)

    def test_a_seat_shell_can_still_run_every_verb(self):
        """The other half of the outage: verbs REFUSED with 'this session is
        agent-<sid8>'. status / claim / pending must all work."""
        self.seated()
        self.seat_shell()
        rc, _out, err = self.cmd("status", ["re-gating the vcs seam"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(seats.roster()[SEAT]["status"], "re-gating the vcs seam")
        rc, _out, err = self.cmd("claim", ["worktree:helm:vcs"])
        self.assertEqual(rc, 0, err)
        self.assertIn(SEAT, {c["holder"] for c in seats.claims_list()})
        rc, _out, err = self.cmd("pending")
        self.assertEqual(rc, 0, err)


# ---------------------------------------------------------------------------
# 2. what a NON-PANE session may not redefine
# ---------------------------------------------------------------------------

class NonPaneFieldsTest(Base):
    """Pin 2, at FIELD level rather than identity level."""

    def child_shell(self):
        """A subagent's tool-call shell: byte-identical env to the seat's
        (same name, same marker, same env sid) — the hook payload is the only
        thing that differs, and it carries the subagent's OWN new id."""
        self.seat_shell()          # identical environment, deliberately

    def test_nonpane_session_needs_positive_evidence(self):
        self.seat_shell()
        self.assertFalse(seats.nonpane_session(BOUND), "the pane's own id")
        self.assertFalse(seats.nonpane_session(None), "no payload => the seat")
        self.assertTrue(seats.nonpane_session(CHILD), "a different session")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertFalse(seats.nonpane_session(CHILD),
                         "no env id => no evidence => fail open to the seat")

    def test_a_nonpane_join_cannot_redefine_session_cwd_or_project(self):
        """The live corruption: a seat working in the helm checkout read
        session=<dead child>, cwd=/tmp, project=tmp on every owner surface."""
        self.seated()
        self.child_shell()
        seat, _line = seats.join(session=CHILD, cwd="/tmp")
        self.assertEqual(seat, SEAT, "the NAME is never switched")
        row = seats.roster()[SEAT]
        self.assertEqual(row["session"], BOUND, "current binding was hijacked")
        self.assertEqual(row["cwd"], WORK, "cwd was downgraded to a temp dir")
        self.assertEqual(row["project"], "helm")
        self.assertIn(CHILD, row["sessions"],
                      "the id must stay ADDRESSABLE so its own rows deliver")

    def test_a_temp_cwd_never_overwrites_a_real_one(self):
        for temp in ("/tmp/x", "/var/tmp/y", "/dev/shm/z"):
            self.seated()
            self.child_shell()
            seats.join(session=CHILD, cwd=temp)
            self.assertEqual(seats.roster()[SEAT]["cwd"], WORK, temp)

    def test_a_real_cwd_still_rehomes_the_seat(self):
        """No over-reach: an identity-bearing join re-homes freely."""
        self.seated()
        self.seat_shell()
        seats.join(session=BOUND, cwd="/home/tester/dev/example/other")
        self.assertEqual(seats.roster()[SEAT]["project"], "other")

    def test_a_brand_new_row_records_even_a_temp_cwd(self):
        """Never lock a new seat out of its own row: with nothing recorded, a
        temp cwd is still better than no cwd."""
        self.child_shell()
        seats.join(session=CHILD, cwd="/tmp/scratch")
        self.assertEqual(seats.roster()[SEAT]["cwd"], "/tmp/scratch")

    def test_a_nonpane_boundary_does_not_repoint_the_row(self):
        """deliver's cursor self-heal is a write_roster too."""
        self.seated()
        self.child_shell()
        seats.deliver(session=CHILD, room="main", seat=SEAT)
        self.assertEqual(seats.roster()[SEAT]["session"], BOUND)


# ---------------------------------------------------------------------------
# 2b. THE KNOWN LIMIT — pinned, because a test that pins a boundary is worth
#     more than one that implies coverage we do not have
# ---------------------------------------------------------------------------

class SameSidSidechainLimitTest(Base):
    """An Agent-tool SIDECHAIN carries the PARENT's sessionId.

    Measured from inside a seat's own turn-loop:
    subagents/agent-<id>.jsonl is `isSidechain: true` with its own `agentId`,
    yet EVERY row carries the parent's sessionId, and its Bash tools inherit the
    parent's CLAUDE_CODE_SESSION_ID and CLAUDE_CODE_CHILD_SESSION unchanged. So
    payload/env inequality cannot see this class at all.

    These tests assert the HONEST outcome: the guard does NOT catch it, and NOT
    catching it is survivable — the damage is confined to row fields that any
    later identity-bearing join refreshes, while authorship, verb access and
    seat identity are untouched. The one real discriminator is
    isSidechain/agentId in the TRANSCRIPT; until a hook payload carries
    equivalent agent provenance this class is UNOBSERVABLE to helm — a
    harness-layer gap, not a helm bug."""

    def sidechain_shell(self):
        """A sidechain's tool-call shell: byte-identical to its parent's, and
        the payload sid is the PARENT's too."""
        self.seat_shell(BOUND)

    def test_a_same_sid_sidechain_is_NOT_detected_and_that_is_recorded(self):
        self.seated()
        self.sidechain_shell()
        self.assertFalse(
            seats.nonpane_session(BOUND),
            "KNOWN LIMIT: a sidechain carrying the parent's sessionId is "
            "indistinguishable from the seat's own hook. If this ever starts "
            "returning True, the harness began exposing agent provenance — "
            "widen the predicate deliberately, do not let it drift here.")

    def test_not_catching_it_costs_only_a_refreshable_row_field(self):
        """It CAN re-point the current session binding — bounded, and undone by
        the seat's next own join."""
        self.seated()
        self.sidechain_shell()
        seats.join(session=BOUND, cwd="/home/tester/dev/example/helm/wt")
        self.assertEqual(seats.roster()[SEAT]["session"], BOUND)
        self.assertEqual(seats.roster()[SEAT]["project"], "wt")
        self.seat_shell(BOUND)                       # the seat's own next join
        seats.join(session=BOUND, cwd=WORK)
        self.assertEqual(seats.roster()[SEAT]["project"], "helm",
                         "an identity-bearing join must heal the row")

    def test_it_cannot_touch_authorship_verbs_or_seat_identity(self):
        """The three things the reverted commit broke stay untouched even for
        the class we cannot detect — that is the containment, and it is what
        makes the gap survivable rather than fatal."""
        self.seated()
        self.sidechain_shell()
        self.assertEqual(seats.acting_seat(BOUND), SEAT)
        self.assertEqual(chat.whoname(), SEAT)
        self.assertEqual(chat.post("from a sidechain")["from"], SEAT)
        rc, _out, err = self.cmd("status", ["scanning"])
        self.assertEqual(rc, 0, err)

    def test_the_class_independent_guards_still_cover_it(self):
        """Both damage-directed guards run on EVERY join, so the undetectable
        class still cannot do the two things that made the live corruption
        unrecoverable: downgrade a real cwd to a throwaway, or evict the row's
        live session id."""
        self.seated()
        self.sidechain_shell()
        seats.join(session=BOUND, cwd="/tmp/sidechain-scratch")
        self.assertEqual(seats.roster()[SEAT]["cwd"], WORK,
                         "a temp cwd downgraded a real one")
        for i in range(12):
            seats.join(session="sc%05d-0000-0000-0000-000000000000" % i,
                       cwd="/tmp")
        row = seats.roster()[SEAT]
        self.assertEqual(row["session"], BOUND)
        self.assertIn(BOUND, row["sessions"])


# ---------------------------------------------------------------------------
# 3. the eviction that made the corruption unrecoverable
# ---------------------------------------------------------------------------

class SessionEvictionTest(Base):
    """console-design took eight transient joins and its REAL session id — still
    running as pid 2789564 — was pushed out of its own row by plain FIFO, so
    seat_for_session could no longer find the live pane at all. That is also
    exactly why the roster-binding predicate failed on it."""

    def fake_proc(self, sids):
        """A /proc whose processes reference `sids` (HELM_PROC redirects the
        liveness probe, the same seam the hooks tests use)."""
        root = os.path.join(self.tmp, "proc")
        for i, sid in enumerate(sids, start=100):
            d = os.path.join(root, str(i))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "cmdline"), "w") as f:
                f.write("claude\0--resume\0%s\0" % sid)
            with open(os.path.join(d, "environ"), "w") as f:
                f.write("HELM_CHAT_NAME=%s\0" % SEAT)
        os.environ["HELM_PROC"] = root
        return root

    def test_a_live_session_is_never_evicted_by_transient_joins(self):
        self.fake_proc([BOUND])
        self.seated()
        self.seat_shell()
        for i in range(12):
            sid = "dead%04d-0000-0000-0000-000000000000" % i
            os.environ["CLAUDE_CODE_SESSION_ID"] = BOUND   # the pane's own
            seats.join(session=sid, cwd="/tmp")
        row = seats.roster()[SEAT]
        self.assertIn(BOUND, row["sessions"],
                      "the seat's LIVE session id was evicted from its own row")
        self.assertEqual(row["session"], BOUND)
        self.assertEqual(seats.seat_for_session(BOUND), SEAT,
                         "the live pane became unresolvable")
        self.assertLessEqual(len(row["sessions"]), seats.SESSIONS_KEPT)

    def test_the_current_binding_is_never_evicted(self):
        self.fake_proc([])                      # no liveness evidence at all
        self.seated()
        self.seat_shell()
        for i in range(12):
            seats.join(session="dead%04d-0000-0000-0000-000000000000" % i,
                       cwd="/tmp")
        self.assertIn(BOUND, seats.roster()[SEAT]["sessions"])

    def test_the_cap_still_holds(self):
        self.fake_proc([])
        self.seated()
        self.seat_shell()
        for i in range(20):
            seats.join(session="s%05d-0000-0000-0000-000000000000" % i,
                       cwd="/tmp")
        self.assertLessEqual(len(seats.roster()[SEAT]["sessions"]),
                             seats.SESSIONS_KEPT)

    def test_the_probe_fails_open_to_fifo(self):
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "no-such-proc")
        self.seated()
        self.seat_shell()
        seats.join(session=CHILD, cwd="/tmp")     # must not raise
        self.assertIn(CHILD, seats.roster()[SEAT]["sessions"])

    def test_liveness_probe_finds_a_referenced_session(self):
        self.fake_proc([BOUND])
        got = seats._sessions_with_a_process([BOUND, CHILD])
        self.assertEqual(got, {BOUND})


# ---------------------------------------------------------------------------
# 4. the live-shape guard itself — a fleet-wide self-lockout tripwire
# ---------------------------------------------------------------------------

class NoSelfLockoutTest(Base):
    """A blunt sweep over representative seat shapes. If any future
    change makes a seat's own tool-call shell resolve to anything but its seat
    name, this fires — the check nobody had when the fleet burned."""

    LIVE = {"kimi": "00000000-0000-4000-a000-000000000001",
            "ds4pro": "00000000-0000-4000-a000-000000000002",
            "codex": "00000000-0000-4000-a000-000000000003",
            "codex-3": "00000000-0000-4000-a000-000000000004",
            "console-design": "00000000-0000-4000-a000-000000000005",
            "opus-integrator": "00000000-0000-4000-a000-000000000006"}
    UNBOUND = ("console-design", "opus-integrator")

    def test_no_live_seat_shell_resolves_to_an_ephemeral_row(self):
        rows = {}
        for seat, sid in self.LIVE.items():
            rows[seat] = ({"session": "evicted-0000-0000-0000-000000000000",
                           "sessions": ["evicted-0000-0000-0000-000000000000"]}
                          if seat in self.UNBOUND
                          else {"session": sid, "sessions": [sid]})
        pk.write_json(seats.roster_path(), rows)
        for seat, sid in self.LIVE.items():
            os.environ["HELM_CHAT_NAME"] = seat
            os.environ["CLAUDE_CODE_CHILD_SESSION"] = "1"
            os.environ["CLAUDE_CODE_SESSION_ID"] = sid
            self.assertEqual(seats.acting_seat(sid), seat, seat)
            self.assertEqual(seats.own_name(), seat, seat)
            self.assertEqual(chat.whoname(), seat, seat)
            self.assertFalse(seats.foreign_seat(seat), seat)
            rc, _out, err = self.cmd("status", ["working"])
            self.assertEqual(rc, 0, "%s: %s" % (seat, err))

    def test_the_identity_law_never_reads_the_child_marker(self):
        """A SOURCE tripwire, because the wrong premise is re-derivable from
        the env var's NAME alone: `CLAUDE_CODE_CHILD_SESSION` sounds like "I am
        a subagent" and is in fact set in EVERY seat's tool-call shell. Any
        identity decision taken on it self-locks the fleet (the reverted
        first attempt this file's module docstring describes).
        Prose ABOUT it is welcome — a READ of it is not."""
        pkg = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm")
        reads = ('environ.get("CLAUDE_CODE_CHILD_SESSION")',
                 "environ.get('CLAUDE_CODE_CHILD_SESSION')",
                 'environ["CLAUDE_CODE_CHILD_SESSION"]',
                 "child_session(")
        # THE SEATS FAMILY IS A GLOB, NOT A FILENAME. This scanned
        # seats.py by name, so moving an identity read into seats_identity.py
        # would make it INVISIBLE — the same class as the grammar rung above,
        # in the file that guards the identity law itself.
        # ALL THREE FAMILIES ARE GLOBS. I converted only seats*
        # and left chat.py and home.py as exact pins, even though the guard
        # spans all three — so this scan would go blind on chat.py the day it
        # splits, and @codex holds bigfile-split-chat-py RIGHT NOW. Half a
        # cure for a class is the class, still live, in the two files I did
        # not look at.
        families = ("seats*.py", "chat*.py", "home*.py")
        mods = sorted({os.path.basename(q)
                       for pat in families
                       for q in glob.glob(os.path.join(pkg, pat))})
        # EVERY FAMILY IS A GLOB — structural, not a count. A pattern that is
        # an exact path stops covering a module's siblings the day it gains
        # them, which is the whole defect this test was rewritten to close.
        for pat in families:
            self.assertIn("*", pat,
                          "family %s is an exact path — a sibling of it would "
                          "go unscanned the day a split creates one" % pat)
        # CONTROLS, AND THEY ARE STRUCTURAL RATHER THAN FILESYSTEM FACTS.
        # I first wrote assertGreaterEqual(len(mods), 5) here and it failed on
        # this very lane — which branches from trunk, where the seats split
        # has not landed, so seats*.py matches exactly one file. That is the
        # SECOND time in ten minutes I demanded a fact about today's tree when
        # the property I wanted was that the PATTERN keeps working: the day a
        # sibling appears it is scanned, without anyone editing this line.
        # What must hold in every tree is that the scan is non-empty and that
        # the three modules this test was originally written against are
        # still in it.
        self.assertTrue(mods, "the identity-module scan found nothing, so "
                              "every assertion below is vacuously true")
        for original in ("seats.py", "chat.py", "home.py"):
            self.assertIn(original, mods,
                          "%s dropped out of the identity scan" % original)
        for mod in mods:
            with open(os.path.join(pkg, mod), encoding="utf-8") as fh:
                body = fh.read()
            for probe in reads:
                self.assertNotIn(
                    probe, body,
                    "%s reads the child marker (%s). It is set in EVERY seat's "
                    "tool-call shell — a seat's own shell and its subagent's "
                    "have byte-identical environments — so no identity or "
                    "presence decision may be taken on it." % (mod, probe))


if __name__ == "__main__":
    unittest.main()
