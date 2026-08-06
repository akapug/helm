#!/usr/bin/env python3
"""The compaction resume leg for a seat helm's SPAWN REGISTER does not know.

THE BUG, measured on this integrator's own pane 2026-07-29: `_registered`
called `seat._seat_family()` and returned its error, so every seat without a
multimodel family name answered its own compaction with `unknown seat
'opus-integrator' (families: codex, ds4pro, gemini, grok, kimi)` and then sat
idle until the OWNER typed into its pane. That is 53 of the 59 seats in the
live chat roster — the entire claude family, including the integrator.

The capability was never missing. `orcaadopt.resolve()` answered correctly for
the same seat in the same minute, returning the exact pane handle a human had
just injected `/compact` into by hand. `seat._resume` already falls through to
it; this leg did not. Built is not wired, and the only thing that finds an
unwired path is using it.

THE POLARITY INVERSION worth keeping in view: a relaunch-resume treats a LIVE
process as a REFUSAL (it would duplicate the seat), while a turn restart treats
LIVE as the precondition (the process survived the compaction and is sitting at
an empty prompt). Same module, opposite meaning, which is why the two paths are
separate functions rather than a flag.

MOST OF WHAT IS PINNED HERE IS REFUSAL, because an over-eager resume injects
text into a live agent: two processes sharing a seat name must never be guessed
between, a contradicting session must refuse, and a pane relaunched during the
settle delay must not receive a directive addressed to its predecessor.
"""
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import harness, orcaadopt, resumeturn  # noqa: E402

# A pane whose composer is EMPTY — one that took its turn. Synthetic, but a
# real frame's shape: `submit` proves delivery by READING THE COMPOSER BACK,
# so a double returning "" models an UNREADABLE pane (UNKNOWN), not a
# working one.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


def setUpModule():
    """Zero `submit`'s type->Enter settle for this module.

    Patches the MODULE CONSTANT rather than exporting HELM_SUBMIT_SETTLE_S: an
    env var set at import time and never restored is exactly what
    test_env_hygiene catches, and it caught this.
    """
    global _settle
    _settle = mock.patch.object(harness, "SUBMIT_SETTLE_S", 0)
    _settle.start()


def tearDownModule():
    _settle.stop()

SID = "0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"
OTHER = "b4cdb8c0-7371-45b3-a250-e6adfe9ba5a1"
SEAT = "opus-integrator"

_KEEP = object()        # "this fixture says nothing about it", never None


STAMP = "88401"         # a /proc/<pid>/stat field-22 birth tick


def proc(pid=103056, seat=SEAT, pane_key="71e79820:7a1349f7", resume_sid=SID,
         start=STAMP):
    return {"pid": pid, "start": start, "seat": seat, "pane_key": pane_key,
            "worktree_id": None, "resume_sid": resume_sid}


def ident(pid=103056, start=STAMP):
    """The token an identity ladder hands the delivery child: pid AND the
    birth stamp that says which incarnation of it was decided about."""
    return orcaadopt.ProcIdent(pid, start)


class IdentityTest(unittest.TestCase):
    """turn_restart_identity — the parent-side check, /proc reads only."""

    def _id(self, procs, unreadable=(), sids=(SID,), failed=False, session=SID,
            current=_KEEP):
        """-> (pid_or_None, reason). pid is the WINNER, not a boolean: the
        delivery leg must re-prove that exact process, never re-derive it from
        the seat name — the name is the ambiguous part.

        THE PINNED SEAM IS `roster_identity`, NOT `roster_sessions`, and the
        move is the point rather than a refactor. `roster_sessions` is the
        LIVENESS half and its own docstring says addressing callers must not
        use it; this function resolves an ADDRESS. Pinning the liveness half
        was how a history sid reached an addressing rung unchallenged — and
        when the production call moved, these tests fell through to the LIVE
        HOST'S roster and started asserting against whatever this box happened
        to be running (measured: they read opus-integrator's real current sid).
        `current` defaults to the newest known sid, so a fixture that says
        nothing about history describes a seat sitting in its current session.
        """
        if current is _KEEP:
            current = sids[0] if sids else None
        with mock.patch.object(orcaadopt, "roster_identity",
                               return_value=(current, list(sids), failed)):
            return orcaadopt.turn_restart_identity(
                SEAT, session, procs=procs, unreadable=list(unreadable))

    def test_the_seat_that_broke_it_now_resolves(self):
        """The regression itself: one live process, a pane key, and an argv
        session that matches. This exact shape answered 'unknown seat'."""
        pid, why = self._id([proc()])
        self.assertEqual(pid, 103056, why)
        self.assertIn("argv --resume", why)

    def test_a_SUBAGENT_sharing_the_inherited_name_does_not_block_the_resume(self):
        """FOUND BY DOGFOODING, not by review. The first draft refused the
        moment two processes shared the seat name — and running it against this
        integrator's own pane refused instantly, because a second claude
        process was carrying the same inherited HELM_CHAT_NAME for a few
        seconds. Everything a seat spawns inherits its name, so a bare name
        collision is the NORMAL state of a busy seat; a guard keyed on it
        refuses precisely the seats doing the most work."""
        sub = proc(pid=999, pane_key=None, resume_sid=None)   # no pane: a child
        pid, why = self._id([proc(), sub])
        self.assertEqual(pid, 103056, why)

    def test_a_SECOND_REAL_PANE_on_the_same_session_is_still_refused(self):
        """The negative control for the narrowing above. Once both candidates
        are addressable AND both claim this session, the evidence is spent and
        guessing is the only thing left — so refuse."""
        pid, why = self._id([proc(), proc(pid=999)])
        self.assertIsNone(pid)
        self.assertIn("must never guess", why)

    def test_a_contradicting_session_refuses(self):
        """Positive disproof: this pid demonstrably holds a DIFFERENT session,
        so the pane that compacted is gone."""
        pid, why = self._id([proc(resume_sid=OTHER)])
        self.assertIsNone(pid)
        self.assertIn("is gone", why)

    def test_a_missing_session_record_is_NOT_a_contradiction(self):
        """Asymmetry on purpose. A process with no `--resume` in argv and a
        roster wiped by a reboot are both ordinary; refusing them reinstates
        the idleness this closes. Absence never refuses — only disproof does."""
        pid, why = self._id([proc(resume_sid=None)], sids=(), failed=False)
        self.assertEqual(pid, 103056, why)
        self.assertIn("not a contradiction", why)

    def test_a_roster_that_names_other_sessions_only_DOES_refuse(self):
        pid, why = self._id([proc(resume_sid=None)], sids=(OTHER,))
        self.assertIsNone(pid)
        self.assertIn("not among them", why)

    def test_a_HISTORY_sid_does_not_authorize_an_address(self):
        """@codex's repro 1/2 on dispatch 1b4039cc, reproduced at exact tip
        761e20f before this rung existed.

        THE SECOND PATH THE ROUND-1 SPLIT MISSED. Round 1 introduced
        `roster_identity` to separate "liveness may use history" from
        "addressing uses the current session only" — and applied it to the
        resolve()/`_session_joined` path while THIS function, which also
        returns an address, kept reading the whole history through
        `roster_sessions`. So a compaction carrying a RETIRED sid was
        authorized here, and resolve()'s current-only rung ran later and could
        not undo a pid that was already authorized.

        WHY IT IS A WRONG PANE AND NOT MERELY A STALE ONE. The seat name is
        NOT the question — both sids belong to this seat. What differs is
        WHICH SESSION'S handoff this is. The live pane holds the CURRENT
        session; the directive belongs to a transcript the seat retired. At
        761e20f this returned (101, 'chat roster names session sid-old…') and
        the delivery leg then injected that old session's directive into the
        current pane and reported "resumed".

        The pane here is session-SILENT (no `--resume` in argv), which is what
        makes rung 2 fall through to the roster at all — the shape no other
        rung catches, since there is no contradiction to spot and nothing
        ambiguous to count.
        """
        pid, why = self._id([proc(resume_sid=None)], sids=(OTHER, SID),
                            current=OTHER, session=SID)
        self.assertIsNone(pid, "a retired sid authorized an address: %s" % why)
        self.assertIn("HISTORY", why)
        self.assertIn(OTHER[:8], why, "the refusal must name the CURRENT sid "
                      "so an operator can see which session actually holds it")

    def test_the_CURRENT_sid_still_authorizes_through_the_roster(self):
        """The positive control for the rung above, and it is the reason that
        rung refuses on `current` rather than simply deleting the roster rung:
        a pane too old to have written an argv sid is ORDINARY, and refusing it
        would reinstate exactly the idleness this whole path closes."""
        pid, why = self._id([proc(resume_sid=None)], sids=(SID, OTHER),
                            current=SID, session=SID)
        self.assertEqual(pid, 103056, why)
        self.assertIn("current session", why)

    def test_a_roster_with_HISTORY_BUT_NO_CURRENT_sid_authorizes_nothing(self):
        """@codex's round-3 finding 2 (dispatch a4051c69), reproduced at
        6c279fe: `roster_identity` -> (None, ['sid-old']) walked straight past
        the mismatch guard — which is written `if current_sid and …` — and
        announced the compaction as the seat's "current session". The roster
        had never said that. A row files the live session under `session` and
        everything retired under `sessions`, so a sid found ONLY in the second
        is history by the register's own data model, and history is liveness
        evidence and never an address.

        THIS FIXTURE IS THE REAL ROW SHAPE, not the seam: `roster_checked` is
        pinned rather than `roster_identity`, because the defect lived in how
        a row with no `session` key becomes (current=None, sids=[...]) and a
        mocked splitter would have hidden exactly that.
        """
        roster = {SEAT: {"sessions": ["sid-old"]}}         # no `session` key
        with mock.patch("helm.seats.roster_checked",
                        return_value=(roster, False)):
            self.assertEqual(orcaadopt.roster_identity(SEAT),
                             (None, ["sid-old"], False))
            pid, why = orcaadopt.turn_restart_identity(
                SEAT, "sid-old", procs=[proc(resume_sid=None)], unreadable=[])
        self.assertIsNone(pid, "history alone authorized an address: %s" % why)
        self.assertIn("HISTORY", why)
        self.assertIn("names no current session", why)

    def test_and_the_REBOOT_case_with_no_roster_row_at_all_still_resumes(self):
        """THE CONTROL THAT MAKES THE ONE ABOVE MEAN SOMETHING, because the two
        are one careless line apart. An EMPTY roster is no evidence — tmpfs was
        wiped, or the seat never joined chat — and this file's standing
        asymmetry is that absence never refuses, only disproof does. Refusing
        here would reinstate the idleness the whole path closes, and it would
        look like a correct fix."""
        with mock.patch("helm.seats.roster_checked", return_value=({}, False)):
            pid, why = orcaadopt.turn_restart_identity(
                SEAT, "sid-old", procs=[proc(resume_sid=None)], unreadable=[])
        self.assertEqual(pid, 103056, why)
        self.assertIn("not a contradiction", why)

    def test_no_pane_key_means_no_address_to_send_to(self):
        pid, why = self._id([proc(pane_key=None)])
        self.assertIsNone(pid)
        self.assertIn("no address", why)

    def test_unreadable_processes_refuse_rather_than_report_absent(self):
        """CANNOT LOOK != absent — orcaadopt's standing law. A probe that was
        blind must not clear the way for an injection."""
        pid, why = self._id([], unreadable=[42])
        self.assertIsNone(pid)
        self.assertIn("could not be identified", why)

    def test_a_genuine_typo_still_refuses(self):
        """The negative control for the whole feature. If an unknown name were
        accepted, the fix would have replaced a false refusal with a false
        acceptance."""
        pid, why = self._id([proc(seat="something-else")])
        self.assertIsNone(pid)
        self.assertIn("no live claude process names", why)


def nameless(pid=14632, pane_key="94880e82:55095a54", resume_sid=SID,
             seat=None, start=STAMP):
    """The nameless-pane specimen shape, measured live 2026-07-29: no
    HELM_CHAT_NAME in environ, the sid in argv, an ORCA_PANE_KEY present."""
    return {"pid": pid, "start": start, "seat": seat, "pane_key": pane_key,
            "worktree_id": None, "resume_sid": resume_sid}


class NamelessIdentityTest(unittest.TestCase):
    """turn_restart_identity(None, sid) — WHICH PANE, never WHO AM I.

    The security frame these pins protect: own_name() stays strict (acting-as
    is impersonation territory), while THIS ladder answers only "which pane do
    I type this session's own handoff into", from the session's own sid
    matched against argv `--resume <sid>` — evidence no other seat's roster
    row supplies, and strictly less ambiguous than a name (a name is inherited
    by every child a seat spawns; an argv sid is unique to the resumed pane).

    The meta-law (vcs.ancestry / proxywatch-HUNG, landed 2026-07-29): a check
    that cannot see a case returns UNKNOWN, never a verdict. Most cases here
    are refusals whose REASON must say what was actually seen."""

    def _id(self, procs, unreadable=(), session=SID):
        return orcaadopt.turn_restart_identity(
            None, session, procs=procs, unreadable=list(unreadable))

    def test_the_specimen_resolves(self):
        """The bug itself: sid in argv, name nowhere, pane addressable — and
        the pre-fix consumer never asked, because it filtered by name first."""
        pid, why = self._id([nameless()])
        self.assertEqual(pid, 14632, why)
        self.assertIn("argv --resume", why)
        self.assertIn("nameless", why)

    def test_positive_argv_evidence_is_REQUIRED(self):
        """The named ladder treats an absent record as not-a-contradiction;
        the nameless ladder must NOT inherit that asymmetry — with no name and
        no sid there is nothing tying any pane to this session, and the
        pre-fix named ladder handed exactly this proc back as a winner (None
        == None matched every nameless proc, then absence waved it through)."""
        pid, why = self._id([nameless(resume_sid=None)])
        self.assertIsNone(pid)
        self.assertIn("no live claude argv", why)
        self.assertIn("no process evidence", why)

    def test_two_panes_on_one_sid_refuse(self):
        pid, why = self._id([nameless(), nameless(pid=999)])
        self.assertIsNone(pid)
        self.assertIn("never guess", why)

    def test_an_unreadable_environ_is_UNKNOWN_never_not_found(self):
        """The environ is where the pane address lives; a probe that could
        not read it must say "could not read", never "has none"."""
        pid, why = self._id([nameless(pane_key=None)], unreadable=[14632])
        self.assertIsNone(pid)
        self.assertIn("could not be read", why)
        self.assertNotIn("no address", why)

    def test_a_readable_match_with_no_pane_key_has_no_address(self):
        """The honest negative, for contrast with the unreadable case: helm
        COULD look, and there demonstrably is no ORCA_PANE_KEY."""
        pid, why = self._id([nameless(pane_key=None)])
        self.assertIsNone(pid)
        self.assertIn("no address", why)

    def test_a_named_holder_of_the_sid_is_not_the_nameless_pane(self):
        """A hook inherits its pane's environ, so a nameless hook whose
        sid-holder DECLARES a name is looking at another seat double-opened
        on this session — refuse, never inject into a named seat's pane."""
        pid, why = self._id([nameless(seat="some-other-seat")])
        self.assertIsNone(pid)
        self.assertIn("some-other-seat", why)

    def test_no_session_id_refuses(self):
        pid, why = self._id([nameless()], session=None)
        self.assertIsNone(pid)
        self.assertIn("no session id", why)


class NamelessRegisteredTest(unittest.TestCase):
    """_registered(None, sid) — the third rung, wired."""

    def test_a_nameless_session_resolves_through_its_own_sid(self):
        """MUST FAIL pre-fix: the old guard returned "cannot address its
        pane" at the missing name, BEFORE any process evidence was consulted."""
        with mock.patch.object(orcaadopt, "turn_restart_identity",
                               return_value=(14632, "pid 14632, argv "
                                             "--resume")) as tri:
            why, pids = resumeturn._registered(None, SID)
        self.assertIsNone(why)
        self.assertEqual(pids, [14632], "the child must re-prove this holder")
        tri.assert_called_once_with(None, SID)

    def test_an_unproven_nameless_session_reports_what_was_seen(self):
        with mock.patch.object(orcaadopt, "turn_restart_identity",
                               return_value=(None, "no live claude argv "
                                             "carries --resume 0fa7c4ed…")):
            why, pids = resumeturn._registered(None, SID)
        self.assertIsNone(pids)
        self.assertIn("HELM_CHAT_NAME", why)
        self.assertIn("no live claude argv", why)


class NamelessDeliveryTest(unittest.TestCase):
    """send_to_sid_pane — the nameless twin of send_to_pane. Every case pins
    `claude_processes` (the DeliveryTest lesson: an unpinned test reads the
    live host's process table)."""

    class Adapter(harness._CLIAdapter):
        """Inherits the REAL `submit` — split send plus read-back — from the
        base, so the double cannot agree with a broken implementation."""
        name = "fake"

        def __init__(self, panes=None):
            self.sent, self.panes = [], panes or {}
            self.resolve_pane = lambda key: self.panes.get(key)

        def read(self, handle, limit=3000, timeout=60):
            return ADVANCED_PANE

        def send(self, handle, text, enter=False):
            self.sent.append((handle, text, enter))

    def test_the_directive_reaches_the_sid_resolved_pane(self):
        ad = self.Adapter({"94880e82:55095a54": {"handle": "term_gtp"}})
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([nameless()], [])):
            mode, detail = orcaadopt.send_to_sid_pane(
                SID, "go", expect_pids=[ident(14632)], adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(ad.sent, [("term_gtp", "go", False),
                                   ("term_gtp", "", True)])

    def test_a_pane_relaunched_during_the_settle_is_refused(self):
        """The TOCTOU guard: a relaunch resumed on the SAME sid re-resolves
        cleanly — to a different pid, which is a different agent."""
        ad = self.Adapter({"94880e82:55095a54": {"handle": "term_gtp"}})
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([nameless(pid=777)], [])):
            mode, detail = orcaadopt.send_to_sid_pane(
                SID, "go", expect_pids=[ident(14632)], adapter=ad)
        self.assertEqual(mode, "manual")
        self.assertIn("relaunched during the settle", detail)
        self.assertEqual(ad.sent, [], "nothing may be injected after a refusal")

    def test_ambiguity_at_send_time_refuses(self):
        ad = self.Adapter({"94880e82:55095a54": {"handle": "term_gtp"}})
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([nameless(),
                                              nameless(pid=999)], [])):
            mode, detail = orcaadopt.send_to_sid_pane(
                SID, "go", expect_pids=[ident(14632)], adapter=ad)
        self.assertEqual(mode, "manual")
        self.assertIn("never guess", detail)
        self.assertEqual(ad.sent, [])

    def test_deliver_routes_a_nameless_seat_to_the_sid_path(self):
        """The branch: pids WITH a name is the adopted path (name-keyed,
        roster-aware); pids WITHOUT one must never touch the name-keyed path."""
        with mock.patch.object(orcaadopt, "send_to_sid_pane",
                               return_value=("resumed", "ok")) as sidp, \
                mock.patch.object(orcaadopt, "send_to_pane") as named:
            mode, _ = resumeturn.deliver(None, "go", SID, pids=[14632])
        self.assertEqual(mode, "resumed")
        sidp.assert_called_once()
        named.assert_not_called()


class RegisteredTest(unittest.TestCase):
    """_registered — which register proves which seat."""

    def test_an_adopted_seat_passes_and_carries_its_holder(self):
        with mock.patch.object(orcaadopt, "turn_restart_identity",
                               return_value=(103056, "pid 103056")):
            why, pids = resumeturn._registered(SEAT, SID)
        self.assertIsNone(why)
        self.assertEqual(pids, [103056], "the child must re-prove this holder")

    def test_an_unadoptable_name_reports_BOTH_registers(self):
        """A genuine typo must not lose the original message — the family list
        is what tells an operator they misspelled a family seat."""
        with mock.patch.object(orcaadopt, "turn_restart_identity",
                               return_value=(None, "no live claude process")):
            why, pids = resumeturn._registered("codx-2", SID)
        self.assertIsNone(pids)
        self.assertIn("unknown seat", why)
        self.assertIn("not an adopted pane either", why)

    def test_a_known_native_seat_failure_never_prints_proxy_family_choices(self):
        """The live opus-integrator case: process evidence recognizes the native
        Claude seat, but every addressable pane holds another session. The alert
        names that measured failure, not a proxy-family list that could never
        have contained the seat."""
        procs = [proc(resume_sid=OTHER)]
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=(procs, [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({SEAT: {"session": OTHER}}, False)):
            why, pids = resumeturn._registered(SEAT, SID)
        self.assertIsNone(pids)
        self.assertIn("native Claude seat", why)
        self.assertIn("different session", why)
        self.assertNotIn("families:", why)

    def test_roster_known_native_seat_with_no_process_is_not_called_a_typo(self):
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({SEAT: {"session": SID}}, False)):
            why, pids = resumeturn._registered(SEAT, SID)
        self.assertIsNone(pids)
        self.assertIn("native Claude seat", why)
        self.assertIn("no live claude process", why)
        self.assertNotIn("families:", why)

    def test_blind_native_evidence_is_UNKNOWN_not_a_proxy_family_verdict(self):
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [42])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, True)):
            why, pids = resumeturn._registered(SEAT, SID)
        self.assertIsNone(pids)
        self.assertIn("UNKNOWN", why)
        self.assertIn("unreadable", why)
        self.assertNotIn("families:", why)

    def test_a_family_seat_still_uses_the_spawn_register(self):
        """Nothing about the adopted path may reroute a helm-spawned seat."""
        from helm import seat
        with mock.patch.object(seat, "_spawn_record",
                               return_value={"seat": "codex-2",
                                             "session": SID}), \
                mock.patch.object(seat, "_instance_dir", return_value="/x"), \
                mock.patch.object(orcaadopt, "turn_restart_identity") as ad:
            why, pids = resumeturn._registered("codex-2", SID)
        self.assertIsNone(why)
        self.assertIsNone(pids, "a registered seat needs no pid guard")
        ad.assert_not_called()


class DeliveryTest(unittest.TestCase):
    """send_to_pane — the adopted seat's transaction.

    EVERY case here pins `claude_processes`, because the strict
    pane-resolution path reads it and an unpinned test therefore reads
    the LIVE HOST'S process table. Not hypothetical: three of these
    passed in isolation and FAILED in the full suite, because a subagent
    running concurrently inherits the seat's HELM_CHAT_NAME and
    ORCA_PANE_KEY and becomes a second pane-carrying candidate — the
    exact ambiguity this code was written to handle, arriving through the
    test harness instead of through production. A test whose result
    depends on what else is running on the box is not a test."""

    class Adapter(harness._CLIAdapter):
        """A metaharness that RESOLVES PANE KEYS, because the delivery leg now
        always re-resolves the authorized process's own key at send time.

        It used to be able to get away with no resolver at all: `resolve()`'s
        handle was handed down as a `prior` and spent directly. That reuse was
        @codex's round-3 finding 1 — it rested on pid equality across two
        observations — so the handle is re-derived from the ORCA_PANE_KEY of
        the process that is alive NOW, and a fixture without a resolver is no
        longer modelling anything real.
        """

        name = "fake"

        def __init__(self, panes=None):
            self.sent = []
            self.panes = ({"71e79820:7a1349f7": {"handle": "term_13a40f4c"}}
                          if panes is None else panes)

        def resolve_pane(self, key):
            return self.panes.get(key)

        def read(self, handle, limit=3000, timeout=60):
            return ADVANCED_PANE

        def send(self, handle, text, enter=False):
            self.sent.append((handle, text, enter))

    def test_the_directive_reaches_the_resolved_pane(self):
        ad = self.Adapter()
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "term_13a40f4c",
                                             "pids": [103056]}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([proc()], [])):
            mode, detail = orcaadopt.send_to_pane(SEAT, "go", expect_pids=[ident(103056)],
                                                  adapter=ad)
        self.assertEqual(mode, "resumed")
        self.assertEqual(ad.sent, [("term_13a40f4c", "go", False),
                                   ("term_13a40f4c", "", True)])

    def test_a_pane_relaunched_during_the_settle_is_refused(self):
        """THE TOCTOU GUARD. A helm-spawned seat gets this from the lifecycle
        lock plus a spawn-register re-read; an adopted seat has neither, so the
        equivalent proof is that the process the hook decided about is still
        holding the seat."""
        ad = self.Adapter()
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "term_new",
                                             "pids": [777]}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([proc(pid=777)], [])):
            mode, detail = orcaadopt.send_to_pane(SEAT, "go", expect_pids=[ident(103056)],
                                                  adapter=ad)
        self.assertEqual(mode, "manual")
        self.assertIn("is gone", detail)
        self.assertEqual(ad.sent, [], "nothing may be injected after a refusal")

    def test_a_subagent_STARTING_during_the_settle_does_not_refuse(self):
        """Survival, not set equality — the delivery-side half of the same
        lesson. A seat that spawns a helper between the compaction and the send
        has not changed its pane, and refusing it would strand the resume."""
        ad = self.Adapter()
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "term_13a40f4c",
                                             "pids": [103056, 999]}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([proc(), proc(pid=999, pane_key=None)], [])):
            mode, _ = orcaadopt.send_to_pane(SEAT, "go", expect_pids=[ident(103056)],
                                             adapter=ad)
        self.assertEqual(mode, "resumed")
        self.assertEqual(ad.sent, [("term_13a40f4c", "go", False),
                                   ("term_13a40f4c", "", True)])

    def test_an_unresolvable_pane_is_manual_not_a_crash(self):
        ad = self.Adapter(panes={})       # orca knows the key no longer
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"pids": [103056], "state": "LIVE",
                                             "evidence": "named by 1 process"}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([proc()], [])):
            mode, detail = orcaadopt.send_to_pane(SEAT, "go", expect_pids=[ident(103056)],
                                                  adapter=ad)
        self.assertEqual(mode, "manual")
        self.assertIn("no live pane resolves", detail)


    def test_it_injects_into_the_DECIDED_pid_not_the_first_name_match(self):
        """THE ONE CROSS-REVIEW CAUGHT. `resolve()` returns the FIRST named
        process with a resolvable pane, and turn_restart_identity's own
        docstring says the caller must never re-derive the holder from the seat
        NAME "because the name is exactly the ambiguous part" — then
        send_to_pane did precisely that. With a stale pane and a relaunched one
        sharing a seat name, the survival check passed on the RIGHT pid while
        the injection went to the WRONG pane, and it returned "resumed"."""
        ad = self.Adapter()
        stale = proc(pid=100, pane_key="STALE", resume_sid=OTHER)
        live = proc(pid=200, pane_key="LIVE")
        panes = {"STALE": {"handle": "term_stale"}, "LIVE": {"handle": "term_live"}}
        ad.resolve_pane = lambda key: panes.get(key)
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "term_stale",
                                             "pids": [100, 200]}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([stale, live], [])):
            mode, detail = orcaadopt.send_to_pane(SEAT, "go", expect_pids=[ident(200)],
                                                  adapter=ad)
        self.assertEqual(mode, "resumed")
        self.assertEqual(ad.sent, [("term_live", "go", False),
                                   ("term_live", "", True)],
                         "the DECIDED pid's pane, never the name's first hit; "
                         "and BOTH acts of the split submit go to that one "
                         "pane — a bare Enter aimed elsewhere would submit "
                         "whatever that pane's composer happened to hold")

    def test_a_decided_pid_with_no_resolvable_pane_REFUSES(self):
        """Rather than falling back to some other pane that answers to the same
        seat name — which is the bug above wearing a helpful face."""
        ad = self.Adapter()
        ad.resolve_pane = lambda key: None
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "term_stale",
                                             "pids": [100, 200]}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([proc(pid=100), proc(pid=200)], [])):
            mode, detail = orcaadopt.send_to_pane(SEAT, "go", expect_pids=[ident(200)],
                                                  adapter=ad)
        self.assertEqual(mode, "manual")
        self.assertIn("refusing to inject into another", detail)
        self.assertEqual(ad.sent, [])

    def test_deliver_routes_to_the_adopted_path_only_when_pids_are_given(self):
        """The branch itself. With pids it is an adopted seat; without them the
        spawn-register transaction must still own the send."""
        with mock.patch.object(orcaadopt, "send_to_pane",
                               return_value=("resumed", "ok")) as adopted:
            mode, _ = resumeturn.deliver(SEAT, "go", SID, pids=[103056])
        self.assertEqual(mode, "resumed")
        adopted.assert_called_once()

        from helm import autocompact
        with mock.patch.object(autocompact, "_pane_action",
                               return_value=(("resumed", "reg"), None)) as reg, \
                mock.patch.object(orcaadopt, "send_to_pane") as adopted2:
            resumeturn.deliver("codex-2", "go", SID)
        reg.assert_called_once()
        adopted2.assert_not_called()


class ChildArgvTest(unittest.TestCase):
    def test_pids_survive_the_fork(self):
        """The decision and the send happen in different processes; an argv
        that drops the holder drops the TOCTOU guard with it."""
        argv = resumeturn._child_argv(SEAT, SID, "/tmp/t", 4.5, pids=[103056])
        self.assertIn("--pids", argv)
        self.assertEqual(argv[argv.index("--pids") + 1], "103056")

    def test_the_BIRTH_STAMP_survives_the_fork_too(self):
        """A pid that crosses the fork without its stamp arrives as a slot
        number, and `authorized_handle` refuses a slot number — so dropping it
        here would not mis-deliver, it would silently disarm every adopted
        resume. The decision and the send are separated by the settle delay,
        which is exactly long enough for the kernel to hand the number on."""
        argv = resumeturn._child_argv(SEAT, SID, "/tmp/t", 4.5,
                                      pids=[ident(103056)])
        self.assertEqual(argv[argv.index("--pids") + 1], "103056:" + STAMP)

    def test_the_child_parses_the_stamp_back_out_of_argv(self):
        """The other half of the round trip, driven through `cmd_resume_turn` rather
        through the parser: an argv that carries the stamp and a child that
        throws it away are indistinguishable from here otherwise."""
        import tempfile
        seen = {}

        def fake_child(seat_name, session, text, delay, adapter=None, pids=None):
            seen["pids"] = pids
            return "resumed", "ok"

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("go")
            path = fh.name
        with mock.patch.object(resumeturn, "child", fake_child):
            rc = resumeturn.cmd_resume_turn(["--deliver", "--session", SID,
                                          "--seat", SEAT, "--text-file", path,
                                          "--delay", "0", "--pids", "103056:" + STAMP])
        self.assertEqual(rc, 0)
        self.assertEqual([int(p) for p in seen["pids"]], [103056])
        self.assertEqual([p.start for p in seen["pids"]], [STAMP],
                         "the child got a bare pid — the TOCTOU guard cannot "
                         "tell a survivor from a successor without the stamp")

    def test_a_registered_seat_carries_no_pids_flag(self):
        argv = resumeturn._child_argv("codex-2", SID, "/tmp/t", 4.5)
        self.assertNotIn("--pids", argv)

    def test_a_named_argv_is_byte_identical_to_the_pre_nameless_form(self):
        """The regression pin the task demands: a NAMED pane's behavior is
        untouched, down to flag order."""
        from helm import hooks
        argv = resumeturn._child_argv("codex-2", SID, "/tmp/t", 4.5,
                                      pids=[103056])
        self.assertEqual(argv, [hooks.helm_bin(), "seat", "resume-turn",
                                "--deliver", "--seat", "codex-2",
                                "--session", SID, "--text-file", "/tmp/t",
                                "--delay", "4.500", "--pids", "103056"])

    def test_a_nameless_argv_carries_no_seat_flag(self):
        """No proven name, no --seat — the delivery child must never run
        under an identity the hook did not prove (not even the roster's)."""
        argv = resumeturn._child_argv(None, SID, "/tmp/t", 0.0, pids=[14632])
        self.assertNotIn("--seat", argv)
        self.assertEqual(argv[argv.index("--pids") + 1], "14632")
        self.assertEqual(argv[argv.index("--session") + 1], SID)


if __name__ == "__main__":
    unittest.main()
