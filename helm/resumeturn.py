#!/usr/bin/env python3
"""helm seat resume-turn — the leg that restarts the turn loop AFTER a compaction.

THE HOLE (owner, live 2026-07-28): "you will also want to set some way to get
your next message or monitor coming out of compaction so i dont have to type
like this to queue you to start automatically again".

Nothing here was individually broken, which is exactly the shape of the bug
(guard-composition, decision-spirit #20): `autocompact` reads context% and
injects /compact at 90% — correct. The PreCompact hook writes a typed handoff
journal entry — correct. JOINTLY they produce a seat that compacts and then
sleeps forever, because a compaction ends a turn and nothing starts the next
one. CC's own native autocompaction continues the turn it interrupted; a
DELIBERATE /compact does not. So the more helm automated the compaction, the
more reliably it parked the seat. Bug class `compaction-has-no-resume-leg`.

THE LEG: a SessionStart hook that fires ONLY on `source == "compact"` and
injects the seat's own next directive back into its pane.

TWO KINDS OF COMPACTION REACH THAT HOOK AND ONLY ONE NEEDS THE LEG. The
SessionStart payload says `source == "compact"` for both, and nothing else
in it tells them apart; for two months this module read only that field and
treated every compaction as the DELIBERATE one (owner report: 240
"could NOT be auto-resumed" rows in room helm, ~85 KB, every one of them
about a seat that had already resumed by itself). The distinction lives one
hook EARLIER: Claude Code's PreCompact payload carries `trigger`, "auto" for
its native autocompaction (the turn continues by itself afterwards) and
"manual" for a typed or injected /compact (the turn ends and the seat
parks). `helm handoff check --hook-json` rides PreCompact, so it records
{session, trigger, agent, at} under this session's key in the resume state
(`note_precompact`), and the SessionStart leg reads it back
(`native_autocompaction`): a RECENT record with trigger "auto", written by
the SAME thread (agent None for the seat's main thread; a subagent shares
the key and its record vouches only for a subagent), and not yet consumed,
means the harness is continuing the turn, so nothing is injected, nothing is
alerted, nobody is DMed — one stderr line, one `native-auto` entry in the
state and one event in the ledger say so. A "manual" record, an unknown
trigger, or NO record at all is today's leg, unchanged: absence of the
record is not proof of a native compaction (the PreCompact hook may be
uninstalled, may have timed out, or may have given up on the state lock),
and the loud path is the one whose cost is a spurious row rather than a
parked seat.

THE INVALIDATION PROTOCOL — how a record stops vouching, in four clauses,
each written where the entry's bookkeeping already lives:
  1. ONE RECORD VOUCHES FOR EXACTLY ONE SESSIONSTART. The decision that
     acts on it marks it consumed in the same locked write
     (`_consume_native_autocompaction`); a consumed record never vouches,
     and a lock the consumer cannot take is a record it may not act on (the
     loud path, said on stderr).
  2. THE PRODUCER WAITS, BOUNDED. `note_precompact` takes the same state
     lock as every other writer (`_mutate_entry`), retrying the nonblocking
     take for up to PRECOMPACT_WAIT_S — well under the PreCompact hook's own
     timeout in hooks.SPECS — so ordinary contention no longer loses the
     record. Past the bound it writes NOTHING, lockless or otherwise: one
     stderr line, and the record standing before it stays as it was.
  3. A PRESENT-INVALID FIELD NEVER VOUCHES. A precompact member that is not
     a dict, a null, a non-finite or non-numeric `at`, a missing or
     non-string `trigger`, a `session` or `agent` of the wrong type or
     absent outright — each declines, and absence is told apart from null
     where the two must differ (`agent` is None on the main thread).
  4. A RECORD OLDER THAN THE ENTRY'S LAST DECISION IS OBSOLETE BY
     CONSTRUCTION. Every resume-turn decision stamps `last_at` on the same
     entry (`_stamp`, and the consuming write itself), the record's `at` is
     taken under the same lock, and a record stamped at or before `last_at`
     describes a compaction whose leg already ran, so it declines.
  5. THE RECORD IS BOUND TO A TRANSCRIPT POSITION -- the per-compaction
     identity clauses 1-4 lacked. The PreCompact payload carries
     `transcript_path`; the producer records that file's byte length, inode
     and device at that instant (`transcript_len`, `transcript_ino`,
     `transcript_dev`). The SessionStart payload carries the same path, and
     the consumer (`_transcript_binding`) vouches only when the file it
     OPENS there -- one regular-file descriptor, opened nonblocking and
     fstat'ed, read for the whole interval past the position, before the
     state lock is taken -- is the SAME inode and device, has not shrunk,
     has grown by at most
     TRANSCRIPT_GROWTH_CAP, and every record appended since the recorded
     position is one the harness writes DURING a compaction: a
     `compact_boundary` record in that region means the record's compaction
     already ended (its boundary and summary are on disk), a `user` or
     `assistant` turn record means it never ended and the session moved on,
     and either way the record is not this compaction's. Every failure to
     PROVE the binding -- no transcript_path in either payload, a record
     without a position, a file that cannot be stat'ed or read, a replaced
     inode, a shrunk file, growth past the cap, an unparseable record in the
     region -- declines with one stderr line naming which, and declining is
     the loud path: absence of evidence is never silence.

WHY THE POSITION DISCRIMINATES -- measured on this host's transcript corpus
(431 compact_boundary records across 14 transcripts; byte offsets computed
per record, no transcript read whole):
  * The boundary is created AFTER the SessionStart hook has finished on
    every harness build from 2.1.204 on (397 boundaries; the running build
    is 2.1.273): the boundary record's timestamp is 0.02-1.2s LATER than the
    SessionStart hook_success attachment's. In file order the boundary
    always precedes the summary, and a record cannot be written before it
    is created, so neither the boundary nor the summary is on disk while
    the hook runs. On builds 2.1.166-2.1.198 (31 boundaries) the boundary
    was created 0.26-1.12s BEFORE the hook finished, which cannot exclude a
    write before the hook started; there the binder declines and the leg
    is loud over a native compaction, never silent over a parked seat.
  * What lands between PreCompact and the boundary is never a turn. Over
    420 boundaries whose PreCompact instant is recoverable (the boundary
    timestamp minus compactMetadata.durationMs), the records stamped during
    the compaction and on disk before the boundary are queue-operation
    (911), pr-link (184), frame-link (56), file-history-delta (8) and one
    non-boundary system record; ZERO user/assistant records stamped before
    the instant landed on disk after a record stamped during it, so a turn
    record in the region is never a late flush of this compaction's own
    history.
  * Size alone cannot discriminate. The region from the last turn record
    to the boundary is a median 7.6 KB but reaches 655 KB on a native
    autocompaction (a file-history-snapshot of 124 KB, a `file` attachment
    of 550 KB), while the smallest compaction summary is 11.5 KB (largest
    30.9 KB). The length is therefore only a READ bound:
    TRANSCRIPT_GROWTH_CAP is 4 MiB, over six times the largest
    same-compaction region measured, and a region past it is unprovable
    and declines.
  * Three transcripts in detail -- the bytes appended between the
    PreCompact instant and the boundary on each native autocompaction: a
    seat on this repo, three autos, 1.2-2.9 KB; a seat on a second
    project, seven autos, 0.8-3.4 KB; a seat on a third project,
    twenty-two autos, 0.7-6.7 KB with one 18.7 KB carrying a
    file-history-snapshot. Every one of them metadata records, every one
    far under its own summary.

BOUNDARY, what remains: the binding is unprovable when the transcript is
unreadable on BOTH sides -- the producer could not stat it, or the consumer
cannot -- and an unprovable binding declines, so that case costs one
spurious resume alert over a native compaction, never a silent parked
seat. The round-3 residual (a stale auto record read by a later manual
compaction's SessionStart after the consumer, the recovery stamp and the
manual producer were each refused the lock) is closed by clause 5: the
stale record's own boundary and summary lie between its position and the
current end of the transcript, and the arm named RESIDUAL in the module's
own suite drives exactly that sequence and asserts the loud path.

DETACHED, NEVER INLINE. A SessionStart hook runs BEFORE the session resumes,
so anything it emits inline lands ahead of the composer. The hook therefore
forks a detached child that waits out the composer settle and then injects
through the metaharness seam. The parent does file reads only and returns in
milliseconds — a hook that blocks is a hook that wedges every compaction.

SETTLE — MEASURED, not guessed (see SETTLE_S).

PANE IDENTITY is autocompact's, unchanged and un-duplicated: the actuation
runs inside `autocompact._pane_action`, which re-proves the spawn register
INSIDE the per-seat lifecycle lock at send time (identity can change during
the settle wait) and resolves through the one registered-pane resolver. A pane
that cannot be authoritatively identified NEVER gets a guessed injection: it
gets a loud chat alert naming the seat, so a human types one key instead of
discovering a dead seat hours later. A pane with NO seat name at all is still
addressable — from its own sid in a live argv (`_registered`, third rung) —
because a check that cannot see a case must return UNKNOWN, never the verdict
"helm cannot address its pane" about a pane it never looked for.

LOOP GUARD — a compact→resume→compact spiral must be impossible by
construction, because the resume text itself costs context:
  * DEBOUNCE_S  — a second SessionStart(compact) this soon is the SAME
    compaction re-firing (a double-wired hook), not a new one. Silent no-op.
  * SPIRAL_S    — a genuinely new compaction this soon after a resume means
    the resume is feeding the spiral. Stop injecting, alert loudly.
  * MAX_RESUMES in WINDOW_S — the rolling cap; it re-arms with time instead of
    latching a seat off forever.
Every constant is sized against this estate's real compaction record (461
compact_boundary transcript records): a compaction takes 100–190s of wall
clock, and consecutive compactions of one session sit 6–16h apart.

THE TEXT is the seat's OWN fresh handoff (`handoff.attribute_entry`), so the
seat resumes on its actual directive rather than a generic "continue" that
invites it to invent work. OWN is proven, not assumed: the shelf is shared by
every seat on the project, so an entry it cannot attribute to THIS caller is
refused and the reader is told a handoff exists that is not its own. No fresh
handoff -> the generic line, which says re-ground first. compaction_floor
decides "fresh" — one definition, shared.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import stat as stat_mod
import sys
import tempfile
import threading
import time

from . import home

SETTLE_S = 3.0
# The composer-settle grace, and the honest story of the number.
#
# MEASURED 2026-07-28 against claude 2.1.220 driven on a real pty (its own
# CLAUDE_CONFIG_DIR seeded the way seat.py seeds a seat, no credential, a dead
# API endpoint, so no model call): inject at SessionStart-hook + D for D in
# {0, 0.25, 0.5, 1, 2, 3}s, three reps, for BOTH free SessionStart sources
# (process startup and `/clear`, the in-place session remount `/compact` also
# performs). Success = the message was SUBMITTED, read off a force-repainted
# frame, not merely echoed.
#
# THE FLOOR IS ZERO: 36 of 36 landed and submitted, including D=0.02s. The pty
# buffers what the TUI has not read yet, so nothing is lost by being early.
# Two earlier readings said otherwise and both were DETECTOR bugs — kernel echo
# read as a composer, then a submitted message read as "still in the composer"
# because CC renders a sent message with the same `❯` glyph. A positive control
# is why they were caught; the number below is only worth what the control was.
#
# So 3.0s is MARGIN, not a measured requirement, and it is cheap because a
# detached child waiting costs nobody anything: the real injection does not go
# through a raw pty write but through the metaharness seam (orca `terminal
# send`, herdr `pane run`), whose own timing was not measurable here, on a host
# under real load, into a session re-initializing tools. helm already carries
# the same shape of grace for the same class of act — seat.SPAWN_SEND_DELAY_S
# is 5s before onboarding keystrokes.
#
# NOTE the delay is NOT why the injection is detached. Even 3s inline would eat
# most of the hook's 5s `timeout` budget and hold up every session start on the
# host. HELM_RESUME_TURN_SETTLE_S overrides (tests set 0).
WITHDRAW_DEADLINE_S = 30.0
# HOW LONG THE CHILD WAITS FOR PROOF THE SEAT ALREADY WOKE, before typing.
#
# THIS IS NOT SETTLE_S AND CONFLATING THEM SHIPS THE BUG. SETTLE_S is the
# composer-settle grace and its measured floor is ZERO. This is a different
# quantity: how long after a compaction the resumed session first SPEAKS.
# MEASURED 2026-09-09 over 359 boundary->first-`assistant` pairs, 12 files,
# 7 project directories, plus the two >500MB sessions a naive size cut would
# have dropped — and dropping them would have been a BIASED filter, because
# the largest sessions are exactly the ones that compact (they carried 28% of
# all pairs). Fleet: min 2s, p50 12s, p90 133s, max 38949s.
#
# THE FLEET NUMBER IS NOT THE ONE THIS CONSTANT IS SIZED ON, and the reason is
# the whole finding: the distribution is BIMODAL and the split is a HUMAN. In
# 265 of 359 pairs a real user message (not a tool result) sits between the
# compaction and the first assistant record — the seat was waiting on a person,
# not failing to wake. That subset carries 100% of the pairs over 300s, 97%
# over 120s and 89% over 60s, and it is irrelevant here: a seat a human is
# typing to is not a stranded seat, and its pane copy is exactly the one that
# lands in his way. The 94 pairs with NO human turn in the gap are the real
# post-compaction latency, and they are hard-bounded: min 2s, p50 7s, p90 28s,
# p99 = max = 133s.
#
# So 30s covers ~p90 of the population this leg is actually about. The tail
# past it is 10 pairs, and the cost there is a duplicate line, never a lost
# directive — the fallback still fires.
#
# ZERO of the 69 pairs in the first (helm-only) census fell inside
# SETTLE_S=3.0, and one fleet pair sits at 2s; timestamps are integer-second,
# so read that floor as "about 3 seconds, +/-1", not as a law. It is still the
# point: a withdrawal check run AT the settle could only ever answer no —
# green in a fixture, inert in production, duplicate still delivered.
#
# PER-REPOSITORY p90 SPANS ROUGHLY 90x across the checkouts on this host, but
# that spread is the human-in-the-gap contamination rather than machine
# variance and the no-human subset does not show it — so it is not a reason to
# make this per-repo. HELM_RESUME_TURN_WITHDRAW_S overrides (tests set 0).
#
# THE COST OF BEING WRONG IS ASYMMETRIC AND THAT IS WHY WAITING IS SAFE: too
# short re-delivers a duplicate into the owner's own composer (the defect);
# too long delays a STRANDED seat's rescue by at most this many seconds, and
# a detached child waiting costs nobody anything.
WITHDRAW_POLL_S = 1.0

DEBOUNCE_S = 120      # re-fire of the SAME compaction (measured: a compaction
                      # itself takes 100–190s; refilling a window inside two
                      # minutes is not physically possible)
SPIRAL_S = 900        # a NEW compaction this soon after a resume = a spiral
                      # (measured floor between real compactions: hours)
MAX_RESUMES = 3       # per rolling WINDOW_S
WINDOW_S = 3600
TEXT_BYTES = 600      # the directive stored for the resumed seat
WIRE_TEXT_BYTES = 56  # measured 60-col frame minus prompt/chrome margin
INJECTION_TTL_S = 3600
PRECOMPACT_WAIT_S = 2.0   # the producer's bounded wait on the state lock:
                          # hooks.SPECS gives the PreCompact hook 5s, and the
                          # handoff capture and contract read run after it
LOCK_RETRY_S = 0.05       # the nonblocking take is retried this often
TRANSCRIPT_GROWTH_CAP = 4 * 1024 * 1024   # the consumer reads at most this
                          # much of the transcript past the record's position;
                          # a READ bound, not an identity (module docstring:
                          # over six times the largest same-compaction region
                          # measured, 655 KB), and a region past it declines
# A FRESHNESS HORIZON, NEVER AN AUTHORIZATION ONE, and the distinction is the
# whole of task/2117. Identity is proven by four checks that a clock cannot
# see past -- digest, handle, text, and a generation whose hash already
# contains the recording pid and time_ns -- so pane reuse trips handle or pid,
# text drift trips text, and a second record trips generation. Nothing is left
# for age to catch. What age DOES answer is whether a record is still the
# CURRENT intent for that composer, which is a question about listings and
# prompts and not about whether Helm may press Enter on text it can prove is
# its own. Gating recovery on it produced the one state with no exit: the
# composer held Helm's text, autocompact refused on unsent input, submit
# refused on the lapsed clock, and a live seat climbed to 91.7% of its context
# window with every guard behaving correctly. The value stays 3600 because
# nothing has measured a better one -- that is a gap, and saying so is the
# point of this comment.
RECOVERY_PERSIST_S = 30
RECOVERY_ATTEMPTS = 2
RECOVERY_BACKOFF_S = 5

_USAGE = """usage: helm seat resume-turn --hook-json [--dry-run] [--json]
       helm seat resume-turn --status | --show DIGEST
       helm seat resume-turn --nudge --seat S [--session SID] [--dry-run]
       helm seat resume-turn --deliver --session SID [--seat S]
                             [--delay SEC] [--text-file PATH] [--pids P,P]
                             [--record-key KEY] [--attempt ID]
                             [--owed-room ROOM]
  The SessionStart(source=compact) resume leg: restart the turn loop after a
  DELIBERATE compaction (PreCompact trigger=manual, or no PreCompact record);
  if injection cannot be proven, alert with the measured self-wake route
  (armed inbox beacon, pane fallback, or UNKNOWN). A NATIVE autocompaction
  (a recent PreCompact record with trigger=auto, written by `helm handoff
  check --hook-json`) continues its turn by itself: no injection, no alert,
  one stderr line and a `native-auto` state entry. The printed line reaches
  the thread that compacted, which can be a subagent: only a compaction
  proven to be the seat's main conversation prints the handoff NEXT; a proven
  subagent gets a `lead handoff suppressed` notice and nothing is armed, and
  an unproven one gets the same notice while the pane leg still carries it.
  --status prints the leg's recorded state lines and exits;
  --hook-json is the hook form (fail-open, rc 0 always, silent unless it acts;
  --json prints its structured result);
  --nudge is THE MANUAL REPAIR for a DEAF-IN-EFFECT seat, and the one door
  `helm beacons` advertises: it reads the seat's current session from the
  roster when --session is absent, re-proves the pane, and forks the same
  child the alarm would. --dry-run prints the directive without typing it.
  --deliver is the DETACHED CHILD it forks — it waits out the composer settle,
  re-proves pane identity, and injects. Never call --deliver by hand unless you
  are reproducing the child.
  --record-key names the PARENT'S episode namespace so the child's
  bookkeeping lands where the parent counted it; omitted, this leg keeps the
  compaction key, which is right only for the compaction child.
  --attempt names the repair attempt the child was launched for, and
  --owed-room the room its alarm measured; every act door re-asks both.
  --pids marks an ORCA-ADOPTED seat (no spawn register, no family name) and
  carries the process the hook decided against, so the child can refuse a pane
  that was relaunched during the settle. A NAMELESS pane (no HELM_CHAT_NAME
  anywhere) omits --seat and MUST carry --pids: the pane was resolved from the
  session's own sid in a live argv, and the send re-proves that, never a name.
"""


# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------

def _num(name, default):
    try:
        return float(home.env(name, default))
    except (TypeError, ValueError):
        return float(default)


def _withdraw_deadline_s():
    return max(0.0, _num("RESUME_TURN_WITHDRAW_S", WITHDRAW_DEADLINE_S))


def settle_s():
    return max(0.0, _num("RESUME_TURN_SETTLE_S", SETTLE_S))


def _debounce_s():
    return _num("RESUME_TURN_DEBOUNCE_S", DEBOUNCE_S)


def _spiral_s():
    return _num("RESUME_TURN_SPIRAL_S", SPIRAL_S)


def _max_resumes():
    return int(_num("RESUME_TURN_MAX", MAX_RESUMES))


def _window_s():
    return _num("RESUME_TURN_WINDOW_S", WINDOW_S)


_RECOVERY_KNOBS = {
    "injection_ttl": ("RESUME_TURN_INJECTION_TTL_S", INJECTION_TTL_S),
    "persistence": ("RESUME_TURN_RECOVERY_PERSIST_S", RECOVERY_PERSIST_S),
    "backoff": ("RESUME_TURN_RECOVERY_BACKOFF_S", RECOVERY_BACKOFF_S),
}


def _recovery_knob(name):
    env, default = _RECOVERY_KNOBS[name]
    return max(0.0, _num(env, default))


def injection_ttl_s():
    return _recovery_knob("injection_ttl")


def recovery_persist_s():
    return _recovery_knob("persistence")


def recovery_backoff_s():
    return _recovery_knob("backoff")


def enabled():
    """The kill switch. A wedged resume leg must be disarmable without an
    edit — every other helm guard carries one (HELM_STOP_GUARD's law)."""
    return str(home.env("RESUME_TURN", "1")).lower() not in ("0", "off", "no")


# ---------------------------------------------------------------------------
# the latch + loop guard (one resume per episode, spiral-proof)
# ---------------------------------------------------------------------------

def state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "resumeturn.json")


def _decide(entry, now):
    """(action, detail) from the prior entry — resume, debounce, spiral,
    capped or unknown.

    Reads a ROLLING list of resume stamps rather than a single latch: a latch
    keyed on one event either sticks forever or re-arms on a clock, and this
    has to survive both a hook that double-fires and a session that legitimately
    compacts again tomorrow.

    An `Unreadable` entry is "unknown": none of the four answers can be
    judged from a store that cannot be read, and "resume" is the one that
    acts."""
    if isinstance(entry, Unreadable):
        return "unknown", (
            "the resume state %s could not be read (%s), so debounce, spiral "
            "and cap cannot be judged -- nothing is injected until it is "
            "repaired (`helm doctor` names it)" % (state_path(), entry.why))
    at = sorted(t for t in (entry or {}).get("at", [])
                if isinstance(t, (int, float)) and now - t < _window_s())
    if not at:
        return "resume", ""
    gap = now - at[-1]
    if gap < _debounce_s():
        return "debounce", ("same compaction episode — a resume fired %.0fs ago"
                            % gap)
    if gap < _spiral_s():
        return "spiral", ("a NEW compaction landed %.0fs after the last resume; "
                          "injecting again would feed the spiral" % gap)
    if len(at) >= _max_resumes():
        return "capped", ("%d resumes already in the last %.0fm"
                          % (len(at), _window_s() / 60))
    return "resume", ""


def _attempt_standing(seat_name, attempt):
    """(standing, born) of a presented attempt id, read from the episode that
    minted it -> "current" with its birth, "legacy" (the current id, minted
    with no birth), "settled" (the current id, whose episode reached a
    terminal outcome), "retired" (the episode names another attempt or spell),
    "absent" (no episode names any attempt), or "unknown" (the roster could
    not be read).

    THE EPISODE IS THE AUTHORITY AND IT KEEPS ONE ATTEMPT. An id is presented
    by whichever process was launched for it, for as long as that process
    lives, so "may this id still be accounted for" cannot be answered from the
    id itself or from a clock. It is answered by whether the episode still
    names it; once a newer attempt is minted the old id is retired for good,
    because an episode never goes back to an attempt it replaced."""
    _key, row, _rows, err = _roster_row(seat_name)
    if err:
        return "unknown", None
    return _standing_of(row, attempt)


def _standing_of(row, attempt):
    """(standing, born) of `attempt` in ONE roster row already read -- the
    pure half of `_attempt_standing`, so an act door's final capture judges the
    attempt from the same read as the rest of its authorization. An episode
    that has reached a TERMINAL outcome is "settled": the attempt it names has
    finished, and nothing may act for it again."""
    from .beacons import _REPAIR_SETTLED
    att = (row or {}).get("attendance")
    ep = att.get("repair") if isinstance(att, dict) else None
    if not isinstance(ep, dict) or not ep.get("attempt"):
        return "absent", None
    if ep.get("attempt") != str(attempt) or ep.get("since") != att.get("since"):
        return "retired", None
    if ep.get("outcome") in _REPAIR_SETTLED:
        return "settled", None
    born = ep.get("born")
    if isinstance(born, bool) or not isinstance(born, (int, float)):
        return "legacy", None
    return "current", float(born)


def _charge_entry(entry, now, count_it, attempt, seat_name, launching=False):
    """Apply ONE writer's charge to one resume-state entry, in place -> the
    refusal ("" when the writer may account, and when `launching`, launch).

    THE RATE STAMP AND ITS RECEIPT ARE ONE WRITE. Both land in this entry and
    the caller writes the entry once under the store's lock, so there is no
    instant at which a receipt says "charged" while the stamp is missing, or
    the reverse: a crash before the write loses both and the other writer
    charges; a crash after it keeps both and the other writer finds the
    receipt. Either writer may arrive first.

    A NAMED ATTEMPT IS CHARGED ONCE, BY RECEIPT, FOR AS LONG AS IT IS CURRENT.
    The receipt is kept while the episode still names the attempt and pruned
    the moment a writer sees a different current one, so the store holds the
    receipts of at most the current attempt. An attempt that is retired or
    absent is refused without accounting: a delayed writer presenting an old
    id, including after its episode was settled and a new spell installed,
    adds nothing. Age plays no part in a charge; a fresh retry of an old spell
    is a fresh attempt and is charged like any other.

    AN UNREADABLE STANDING KEEPS THE LIMITER. When the roster cannot say
    whether the attempt is current, a writer that is not launching charges it
    once, stamp and receipt, and still returns the refusal. Refusing the stamp
    would leave `_decide` with nothing to debounce or cap against and admit a
    fresh mint on every pass; the receipt keeps a writer that later reads the
    attempt as current from charging it a second time. Receipts written while
    the standing is unreadable cannot be pruned to "the current attempt", so
    they are pruned to the stamps still inside the rate window instead.

    THE LAUNCHING WRITER'S CHARGE IS ITS LAST LAUNCH GATE. With `launching`,
    launch eligibility is judged from the SAME roster read as the charge: a
    refusal accounts nothing and the caller does not launch. An admitted launch
    re-dates the attempt's one stamp to this instant, so the debounce the
    charge imposes runs from the launch and not from an earlier mint.

    NO ATTEMPT NAMED is the compaction leg's legacy receipt: a stamp inside the
    debounce window is taken as paid, because `_decide` refuses a second
    episode inside that window regardless."""
    at = [t for t in entry.get("at", [])
          if isinstance(t, (int, float)) and now - t < _window_s()]
    receipts = entry.get("receipts")
    receipts = {str(k): v for k, v in (receipts or {}).items()
                if isinstance(v, (int, float))} \
        if isinstance(receipts, dict) else {}
    refused, redate = "", False
    if attempt is None:
        paid = any(0 <= now - t < _debounce_s() for t in at)
    else:
        standing, born = _attempt_standing(seat_name, attempt)
        refused = _launch_verdict(attempt, standing, born, now) \
            if launching else ""
        if refused:
            paid = True
        elif standing in ("current", "legacy"):
            receipts = {k: v for k, v in receipts.items() if k == str(attempt)}
            paid = str(attempt) in receipts
            redate = launching and paid
        elif standing == "unknown":
            receipts = {k: v for k, v in receipts.items()
                        if v in at or k == str(attempt)}
            paid, refused = str(attempt) in receipts, standing
        else:
            paid, refused = True, standing
    mine = str(attempt)
    if redate and receipts[mine] in at and receipts[mine] < now:
        at[at.index(receipts[mine])] = now
        receipts[mine] = now
    if count_it and not paid:
        at.append(now)
        if attempt is not None:
            receipts[mine] = now
    entry.pop("attempts", None)
    entry.update({"at": at, "receipts": receipts})
    return refused


def _stamp(key, session, mode, detail, count_it, attempt, seat, blocking,
           launching=False, verdict=None):
    """Write one key's entry under the state lock -> (entry, refusal). Raises;
    each caller decides what a failed write costs.

    `verdict`, a dict when given, receives the refusal under "refused" the
    moment it is DETERMINED, before the write: a refusal is a fact about the
    attempt, and a store that then fails to persist it does not make it
    untrue.

    THE READ UNDER THE LOCK IS STRICT, for the reason `_mutate_entry`'s is.
    This write replaces the WHOLE store, and the store also holds every other
    key's act ledger: obsolete records, act intents, receipts and charges. A
    lenient read answers {} for a store that exists and cannot be read, so
    the write after it would erase that ledger, and the obsolete check would
    then read a clean attempt. A read or parse error, or a store that is not a
    JSON object, raises instead (`_read_store`); `_record`
    and `_record_nb` report it, and `_launch_charge` keeps the refusal it
    determined or its preliminary verdict. The trade: a corrupt store no
    longer heals itself through a bookkeeping write, so the launch decisions
    that read it through `_peek` refuse as unknown until it is repaired, as
    the act doors already did, and `helm doctor` names it. A missing store is
    still empty."""
    from . import pk
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(),
                    fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = _read_store()
        now = time.time()
        entry = st.get(key) or {}
        refused = _charge_entry(entry, now, count_it, attempt, seat, launching)
        if isinstance(verdict, dict):
            verdict["refused"] = refused
        entry.update({"session": session, "mode": mode,
                      "detail": detail, "last_at": now})
        st[key] = entry
        pk.write_json(p, st)
        return entry, refused


def _record(key, session, mode, detail, count_it=False, attempt=None,
            seat=None):
    """Update one key's entry under the state lock -> the written entry.

    `count_it` DEFAULTS TO NOT SPENDING A SLOT, and the default is a cure
    rather than a convenience. With no default, every call that records a
    REFUSAL -- a withdrawal, a pause, a drain -- had to remember an argument
    that only the two ACTUATING call sites care about, and three of them
    forgot: each raised TypeError on a branch no arm reached, so the outcome
    was never recorded at all. Spending a slot is the exceptional act and is
    now the thing that must be said out loud.

    NEVER RAISES. Three call sites run this bookkeeping immediately before
    an _alert, and an unguarded mkdir/open/flock/write here suppressed both
    the room row AND the wake DM (review blocker 1) — a failed count must
    degrade the spiral guard's memory, never cost the wake itself. The
    trade is conscious and bounded: _decide reads this state via _peek, so
    a persistently unwritable store already weakened debounce/cap before
    this guard existed; what changes is that the alert now outlives it."""
    try:
        return _stamp(key, session, mode, detail, count_it, attempt, seat,
                      blocking=True)[0]
    except Exception as e:   # noqa: BLE001 — bookkeeping never costs the wake
        print("helm seat resume-turn: state write failed (%s) — the alert "
              "still fires" % e, file=sys.stderr)
        return None


def _record_nb(key, session, mode, detail, count_it, attempt=None,
               seat=None):
    """The HOOK's only state touch: identical bookkeeping, LOCK_NB.

    The double-fire guarantee ("a second SessionStart this soon is the SAME
    compaction re-firing — silent no-op", the module's own arm) needs the
    spawned stamp visible to a hook firing in the SAME millisecond, which a
    child-side record cannot promise. The reviewer's requirement was that a
    held lock never wedge SessionStart — nonblocking satisfies it exactly:
    uncontended (the overwhelmingly common case) the stamp lands before the
    hook returns; contended, the hook skips the write instead of waiting,
    says so, and the deliverer child's own record covers the window."""
    try:
        return _stamp(key, session, mode, detail, count_it, attempt, seat,
                      blocking=False)[0]
    except Exception as e:   # noqa: BLE001 — contention or breakage: skip, never wait
        print("helm seat resume-turn: nonblocking stamp skipped (%s) — the "
              "deliverer's own record covers it" % e, file=sys.stderr)
        return None


class Unreadable(object):
    """What `_peek` answers for a resume state that exists and cannot be read.

    NOT None AND NOT {}, because both of those already mean something: None is
    "this key was never recorded" and {} is an entry with nothing in it. A
    reader that collapsed an unreadable store into either of them told the
    launch decision that no resume had fired, so a store the bookkeeping
    writer refuses to touch answered "resume" on every compaction with no
    debounce, spiral or cap (task/2530)."""

    def __init__(self, why):
        self.why = why


def _read_store():
    """The whole resume state as a dict. Raises when it cannot be read.

    THE ONE DEFINITION OF A READABLE STORE, for every reader: the launch
    decisions through `_store`, and the bookkeeping stamp, the ledger mutation
    and the obsolete-act check directly. A missing store is empty. A store
    that fails a strict parse, holds duplicate keys, is a dangling symlink, is
    not a regular file, or holds JSON that is not an object raises. Those
    three readers each read `read_json(..., strict=True) or {}`, so a JSON
    null or [] parsed, was falsy, and read as a clean empty store there while
    `_peek` refused it: the act door granted a clean attempt, and the first
    bookkeeping write replaced the file under the refusal (task/2530)."""
    from . import pk
    st = pk.read_json(state_path(), {}, strict=True)
    if not isinstance(st, dict):
        raise ValueError("it holds JSON %s, not an object"
                         % ("null" if st is None else type(st).__name__))
    return st


def _store():
    """-> (the resume state, None) or (None, why it cannot be read), by
    `_read_store`'s definition."""
    try:
        return _read_store(), None
    except Exception as e:              # noqa: BLE001 — every failure is a why
        return None, "%s: %s" % (e.__class__.__name__, e)


def _peek(key):
    """One key's entry, None when it was never recorded, or `Unreadable`.

    THE LAUNCH DECISIONS READ THROUGH HERE, so this read is as strict as the
    charge's. A lenient read here and a strict one in `_stamp` gave the two
    halves of one debounce two different stores."""
    st, why = _store()
    return Unreadable(why) if why else st.get(key)


# ---------------------------------------------------------------------------
# the PreCompact record: which KIND of compaction is this SessionStart ending
# ---------------------------------------------------------------------------

def compaction_key(seat_name, session):
    """The resume-state key ONE session's compaction is accounted under: the
    seat's declared name, or the session prefix for a nameless one.

    ONE DEFINITION FOR TWO HOOKS. PreCompact (`note_precompact`, through
    `helm handoff check --hook-json`) writes under it and SessionStart
    (`hook`) reads under it, in the same seat process environment, so the
    pair meets on the same entry. A second spelling on either side would
    make every compaction read as unrecorded, which is the loud path."""
    return seat_name or ("session:" + str(session or "")[:8])


def _epoch_provenance(src, sid, agent):
    """Why the injector's suppression epoch is about to reset, for the ledger
    row that will show it resetting -> {"source": ..., "vouch": ...}.

    THE LEDGER COULD SAY THAT AN EPOCH RESET AND NEVER WHY, and the cost of
    that is not hypothetical: reading one night's boundaries out of it took
    three cross-referenced files and an instrument that had to be retracted.
    Two facts answer it, and both are on hand HERE and nowhere later — this
    runs ahead of every discriminating arm on purpose, because the drop it
    accompanies does.

    `source` is the SessionStart source verbatim. A payload that carries none
    is recorded as "?" — the module's own spelling for an unknown source, and
    the same one the skip detail and the alert print. IT IS NOT A CLAIM: no
    harness source is spelled that way, so a reader can tell "the payload did
    not say" from every source that did.

    `vouch` is THREE VALUES and the third one is the point:
      vouched   — a PreCompact record for this session and this thread was
                  present and unconsumed when the epoch reset.
      unvouched — the resume state was READ and holds no such record.
      unknown   — the resume state could not be read (`Unreadable`), or the
                  read raised. NOTHING IS KNOWN about the record here.
    Collapsing the last two into one false would rebuild the exact defect
    this field exists to remove, one layer down: "no record was found" and
    "the record could not be read" are different facts, and only the first
    is evidence.

    DELIBERATELY WEAKER THAN `native_autocompaction`, and it must not be read
    as that verdict. It asks whether a record was THERE, not whether the
    record would vouch: the full judgment binds the record to the
    transcript's position, which opens and reads the transcript, and this
    path would then read it twice per SessionStart to answer a diagnostic
    question. A record can be present here and still decline there — an aged
    one, a deliberate /compact's, one the transcript has moved past. So
    `vouched` means "there was something to judge", which is precisely the
    fact missing from the ledger, and the judgment stays where it is made."""
    from . import seats
    from .inject import _ledger
    try:
        prior = _peek(compaction_key(seats.own_name(), sid))
        if isinstance(prior, Unreadable):
            vouch = _ledger.EPOCH_UNKNOWN
        else:
            rec = prior.get("precompact") if isinstance(prior, dict) else None
            vouch = (_ledger.EPOCH_VOUCHED
                     if isinstance(rec, dict) and "consumed" not in rec
                     and rec.get("session") == sid
                     and rec.get("agent", _MISSING) == agent
                     else _ledger.EPOCH_UNVOUCHED)
    except Exception:       # noqa: BLE001 — a diagnosis, never a decision
        vouch = _ledger.EPOCH_UNKNOWN
    return {"source": src or "?", "vouch": vouch}


def precompact_window_s():
    """How long a PreCompact record vouches for the SessionStart that follows
    it. SIZED ON SPIRAL_S, NOT DEBOUNCE_S, and the module's own census says
    why: a compaction takes 100-190s of wall clock between the PreCompact
    hook and the SessionStart that ends it, so DEBOUNCE_S (120s) would let
    the slow half of real autocompactions age out of their own record and
    fall back to the loud path. SPIRAL_S (900s) covers the longest measured
    compaction and sits far below the measured floor between two
    compactions of one session (6-16h), so inside it the record can only
    describe THIS compaction. HELM_RESUME_TURN_SPIRAL_S moves both."""
    return _spiral_s()


def _transcript_position(transcript):
    """The transcript's position for the record (module docstring, clause
    5) -> ({transcript_len, transcript_ino, transcript_dev}, "") or ({},
    why it could not be taken). A position that cannot be taken is written
    as NO position, never a guessed one: the consumer declines a record
    without one, which is the loud path.

    ONLY A REGULAR FILE HAS A POSITION. A FIFO, socket or device at the
    transcript path stats without blocking and reports an st_size, so a
    position recorded over one would send the consumer to open it, and an
    open of a FIFO with no writer never returns. The consumer refuses a
    non-regular file on its own descriptor (`_observe_transcript`); this
    side refuses to record a position for one at all, so a special file
    never becomes a position anybody is asked to read."""
    if not transcript:
        return {}, "the PreCompact payload carries no transcript_path"
    try:
        st = os.stat(transcript)
    except (OSError, ValueError) as e:
        # ValueError is the kernel binding refusing the PATH ITSELF (an
        # embedded NUL), and it is not an OSError: uncaught, it left this
        # function, then `note_precompact`, and aborted the whole PreCompact
        # branch of `handoff check` -- no record, no capture, no nag. It is
        # the same unreadable pole as a failed stat, told the same way.
        return {}, "the transcript cannot be stat'ed (%s: %s)" % (
            e.__class__.__name__, e)
    if not stat_mod.S_ISREG(st.st_mode):
        return {}, ("the transcript at that path is not a regular file (%s)"
                    % stat_mod.filemode(st.st_mode))
    return {"transcript_len": int(st.st_size), "transcript_ino": int(st.st_ino),
            "transcript_dev": int(st.st_dev)}, ""


def note_precompact(session, trigger, agent=None, transcript=None):
    """The PreCompact hook's one state touch: remember which kind of
    compaction is about to run, for WHICH thread, and WHERE the transcript
    stood at that instant -> the record written, or None.

    `transcript` is the payload's transcript_path; its byte length, inode
    and device are recorded (`_transcript_position`) so the SessionStart
    consumer can prove the record describes ITS compaction rather than an
    earlier one (module docstring, clause 5). A position that cannot be
    taken is not recorded, one stderr line says so, and the record then
    never vouches.

    `trigger` is Claude Code's own field and is stored as it arrived --
    "auto" for the native autocompaction, "manual" for a typed or injected
    /compact -- because the reader compares against the producer's spelling,
    never a normalised one. `agent` is the payload's agent_id
    (`actors.sidechain_agent`): None for the seat's main thread, the minted
    id inside a subagent. A subagent shares the seat's name, session and
    environ, so its PreCompact lands under the SAME key; the agent field is
    what keeps a child's autocompaction from vouching for the main thread's
    next SessionStart, whose guard refuses only the CONSUMER of a sidechain
    payload, never a producer that fired earlier.

    THE WRITE WAITS, BOUNDED, AND NEVER RAISES. It takes the state lock the
    way every other writer does (`_mutate_entry`), retrying the nonblocking
    take for up to PRECOMPACT_WAIT_S -- a PreCompact hook that waits past
    its own timeout is a hook that wedges the compaction, and hooks.SPECS
    gives this one 5s with the capture and contract read still to run -- so
    a writer that merely overlaps a deliverer child's stamp lands. The
    record's `at` is taken UNDER the lock, so it orders against the entry's
    `last_at` by the lock's own serialisation (module docstring, clause 4).
    Past the bound nothing is written, and nothing lockless stands in: one
    stderr line says the record was not recorded, and the record standing
    before it stays exactly as it was. What that costs is the module
    docstring's BOUNDARY: the standing record can vouch only while it is
    unconsumed, same session and thread, inside its window and newer than
    the entry's last recorded decision, so the ordinary cost of a lost
    write is one spurious resume alert, never a parked seat."""
    from . import seats
    session = str(session or "")
    if not session:
        return None
    key = compaction_key(seats.own_name(), session)
    position, unbound = _transcript_position(transcript)

    def change(entry):
        rec = {"session": session, "trigger": str(trigger or ""),
               "agent": agent, "at": time.time()}
        rec.update(position)
        entry["precompact"] = rec
        return rec, True

    try:
        rec = _mutate_entry(key, change, nonblocking=True,
                            wait_s=PRECOMPACT_WAIT_S)
    except Exception as e:   # noqa: BLE001 — a missed record means the loud path
        _say("helm handoff check: PreCompact trigger not recorded after "
             "%.1fs (%s: %s) — the record standing before it, if any, is "
             "what the resume leg will read, and it vouches only while "
             "unconsumed, inside its window, newer than the entry's last "
             "resume-turn decision and bound to the transcript's position"
             % (PRECOMPACT_WAIT_S, e.__class__.__name__, e))
        return None
    if unbound:
        _say("helm handoff check: PreCompact transcript position not "
             "recorded (%s) — this record cannot vouch for the SessionStart "
             "that follows, so the resume leg is loud" % unbound)
    return rec


_MISSING = object()   # a field that is absent, told apart from one that is null


def _finite(value):
    """A real, finite number -> float; anything else (bool included) -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _record_position(rec):
    """The (byte length, inode, device) a record was positioned at -> the
    triple, or None when it carries no usable position. A PRESENT-INVALID
    field is no position: null, boolean, non-integer or negative, in any
    of the three."""
    pos = tuple(rec.get(k) for k in ("transcript_len", "transcript_ino",
                                     "transcript_dev"))
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
           for v in pos):
        return None
    return pos


def _observe_transcript(transcript, pos):
    """Read the region a recorded position names -> the observation a
    binding is decided from: {"pos", "region", "why"}, where a non-empty
    `why` is the reason no region could be taken.

    ONE DESCRIPTOR IS MEASURED AND READ. The earlier shape stat'ed the
    PATHNAME and then opened that path a second time, so the file whose
    identity, size and growth bound were proved was not necessarily the
    file the bytes came from: a replacement landing between the two handed
    the reader an empty region out of a prefix-only file at the same
    recorded offset, and an empty region holds no boundary and no turn, so
    a stale auto record vouched over it. Here the path is opened ONCE,
    `os.fstat` of THAT descriptor gives the identity and the size, and the
    read asks for exactly the interval between the recorded position and
    that size.

    A SHORT READ IS A REASON, NOT A CLEAN REGION. A file truncated under
    the descriptor after the fstat returns fewer bytes than the interval,
    and accepting those bytes is the same vacuous acceptance by another
    route, so the length read must equal the length asked for.

    NONBLOCKING AND REGULAR-ONLY, because a hook is on a clock.
    `pk.open_regular` opens with O_NONBLOCK and refuses anything but a
    regular file by `os.fstat` before any read, so a FIFO or device at the
    transcript path declines at once instead of holding the hook until its
    timeout. TRANSCRIPT_GROWTH_CAP bounds the BYTES; the opener prevents
    blocking on a FIFO, but O_NONBLOCK gives regular-file I/O no deadline.

    NO STATE LOCK IS HELD WHILE THIS RUNS -- `_consume_native_autocompaction`
    says why that is structural."""
    from . import pk
    length, ino, dev = pos
    obs = {"pos": pos, "region": None, "why": ""}
    try:
        f = pk.open_regular(transcript, "rb")
    except pk.NotRegularFile as e:
        obs["why"] = ("the transcript at that path is not a regular file "
                      "(%s)" % e)
        return obs
    except OSError as e:
        obs["why"] = "the transcript cannot be read (%s: %s)" % (
            e.__class__.__name__, e)
        return obs
    try:
        st = os.fstat(f.fileno())
        grown = st.st_size - length
        if (st.st_ino, st.st_dev) != (ino, dev):
            obs["why"] = ("the transcript at that path is another file than "
                          "the one PreCompact saw (inode differs)")
        elif grown < 0:
            obs["why"] = ("the transcript shrank below the recorded position "
                          "(%d < %d bytes)" % (st.st_size, length))
        elif grown > TRANSCRIPT_GROWTH_CAP:
            obs["why"] = ("the transcript grew %d bytes past the recorded "
                          "position, over the %d-byte bound"
                          % (grown, TRANSCRIPT_GROWTH_CAP))
        else:
            f.seek(length)
            region = f.read(grown)
            if len(region) != grown:
                obs["why"] = ("the transcript read returned %d of the %d "
                              "bytes between the recorded position and the "
                              "size of the file it opened — it was truncated "
                              "while it was read" % (len(region), grown))
            else:
                obs["region"] = region
    except OSError as e:
        obs["why"] = "the transcript cannot be read (%s: %s)" % (
            e.__class__.__name__, e)
    finally:
        f.close()
    return obs


def _observe_recorded_position(key, transcript):
    """The region THIS key's standing record names, read with NO LOCK HELD
    -> the observation the locked decision consumes.

    The entry is read WITHOUT the lock on purpose, and its position is a
    HINT rather than authority: every write of this store is an atomic
    replace (`pk.write_json`), so an unlocked read sees one whole version
    of it, and the version that decides anything is still the one read
    under the lock. A record that moved in between no longer matches the
    position the locked entry names, and `_transcript_binding` declines
    that mismatch rather than opening the transcript a second time inside
    the transaction -- the conservative side of the race, and the loud
    path.

    THE READ IS UNCONDITIONAL PAST A STANDING POSITION, and that is the
    price of taking it outside the lock: a record the locked decision would
    have refused on a cheaper clause -- a `manual` trigger, an aged-out or
    already-consumed record -- still has its region read here, because the
    only way to skip it would be a second spelling of those clauses on this
    side, and two spellings of one rule is how the two halves of a debounce
    came to disagree. The cost is bounded by the same TRANSCRIPT_GROWTH_CAP
    the binding is: one nonblocking open of a regular file and at most that
    many bytes, off the lock, where nothing else is waiting on it."""
    st, _why = _store()
    entry = (st or {}).get(key)
    rec = entry.get("precompact") if isinstance(entry, dict) else None
    pos = _record_position(rec) if isinstance(rec, dict) else None
    if pos is None or not transcript:
        return {"pos": pos, "region": None,
                "why": "the record carries no transcript position"}
    return _observe_transcript(transcript, pos)


def _transcript_binding(rec, transcript, observed=None):
    """Why the record cannot be bound to THIS compaction's transcript
    position -> the reason, or "" when it is bound (module docstring,
    clause 5, and the measurement under WHY THE POSITION DISCRIMINATES).

    Bound means: the payload named a transcript, the record carries a
    position, the file the reader OPENED is a regular file of the same
    inode and device the producer saw, it has not shrunk, it has grown by
    at most TRANSCRIPT_GROWTH_CAP, the whole interval past the position
    was read, and no record in it is a `compact_boundary` (the record's
    compaction already ended) or a `user` or `assistant` turn (it never
    ended and the session moved on). Every other outcome is a reason, and
    every reason is the loud path.

    `observed` is the region a caller read BEFORE it took the state lock;
    None means no observation was handed over and this call takes its own,
    which is what the dry decision does -- it holds no lock either. A
    caller that read one NEVER re-opens the transcript here."""
    if not transcript:
        return "the SessionStart payload carries no transcript_path"
    pos = _record_position(rec)
    if pos is None:
        return "the record carries no transcript position"
    obs = _observe_transcript(transcript, pos) if observed is None else observed
    if obs.get("pos") != pos:
        return ("the record's transcript position changed while the "
                "transcript was read — the region on hand is not the one "
                "this record names")
    if obs["why"]:
        return obs["why"]
    for line in obs["region"].split(b"\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            d = None
        if not isinstance(d, dict):
            return ("an unparseable record was appended after the recorded "
                    "position")
        if d.get("subtype") == "compact_boundary":
            return ("a compaction boundary was appended after the recorded "
                    "position — the compaction the record describes already "
                    "ended")
        if d.get("type") in ("user", "assistant"):
            return ("a turn record (type %s) was appended after the recorded "
                    "position — the compaction the record describes is not "
                    "this one" % d["type"])
    return ""


def native_autocompaction(entry, session, agent, now, transcript=None,
                          observed=None):
    """Why this SessionStart(compact) is the tail of a NATIVE autocompaction
    the harness continues by itself -> the reason, or "" when it cannot be
    said. `transcript` is the SessionStart payload's transcript_path, the
    other half of the record's position binding (clause 5).

    Every clause falls toward "": no entry (`None`), an `Unreadable` store,
    no record, a record for another session, a record another THREAD wrote
    (the record's agent must equal this payload's `agent`: None for main,
    the minted id inside a subagent -- a child's record never vouches for
    main and main's never for a child), a record a SessionStart already
    consumed, a trigger that is not the producer's exact "auto", a record
    outside `precompact_window_s` (a future stamp included), or a record
    stamped at or before the entry's `last_at` -- the stamp every
    resume-turn decision leaves on this entry, so a record older than the
    last decision describes a compaction whose leg already ran.

    A PRESENT-INVALID FIELD NEVER VOUCHES, and every field is read as
    present-and-typed rather than merely truthy: a record that is not a
    dict (null included), a missing or non-string `session` or `trigger`,
    an `agent` that is absent (told apart from the main thread's null by
    `_MISSING`) or neither null nor a string, an `at` or a `last_at` that is
    missing, boolean, non-numeric or non-finite (NaN and the infinities
    compare their way past a plain `<=`). The producer never writes such a
    record; a store that holds one was edited by something else, and that
    is not evidence of a native compaction.

    ABSENCE OF THE RECORD IS NOT PROOF OF A NATIVE COMPACTION: the
    PreCompact hook may be uninstalled on this home, may have timed out
    before its write, or may have given up on the state lock inside its
    bound, and each of those must land on the loud path, whose cost is a
    spurious row rather than a seat nobody resumes."""
    rec = entry.get("precompact") if isinstance(entry, dict) else None
    if not isinstance(rec, dict) or "consumed" in rec:
        return ""
    if not isinstance(rec.get("session"), str) or rec["session"] != session:
        return ""
    who = rec.get("agent", _MISSING)
    if who is _MISSING or not (who is None or isinstance(who, str)):
        return ""
    if who != agent or rec.get("trigger") != "auto":
        return ""
    at = _finite(rec.get("at"))
    if at is None:
        return ""
    last = entry.get("last_at", _MISSING)
    if last is not _MISSING:
        last = _finite(last)
        if last is None or at <= last:
            return ""
    age = now - at
    if not 0 <= age < precompact_window_s():
        return ""
    unbound = _transcript_binding(rec, transcript, observed)
    if unbound:
        # THE ONE STDERR LINE for a binding that could not be proved: every
        # clause above declines silently because its record is simply not
        # evidence, but this one declines a record that IS an auto record
        # of this session and thread, and the owner reading a spurious
        # resume row deserves to know which proof was missing.
        _say("helm seat resume-turn: the PreCompact record does not vouch — "
             "%s; this compaction is treated as deliberate" % unbound)
        return ""
    return ("PreCompact said trigger=auto %.0fs ago and the transcript stands "
            "where it left it — the harness continues the interrupted turn "
            "itself" % age)


def _consume_native_autocompaction(key, session, agent, now, transcript=None):
    """Decide `native_autocompaction` and CONSUME the record it vouched with,
    in one locked write -> the reason, or "".

    ONE PRECOMPACT VOUCHES FOR EXACTLY ONE SESSIONSTART. The record is judged
    from the entry read under the lock, and the same write marks it consumed
    and stamps the `native-auto` outcome, so no second SessionStart -- a
    deliberate /compact whose own PreCompact write was refused, or was never
    made -- can read the earlier auto evidence and stay silent over a seat
    nobody resumes. Nonblocking, like every hook-side write: a record the
    hook cannot consume is a record it may not act on, so a refused lock is
    "" and the loud path, said on stderr.

    THE TRANSCRIPT IS READ BEFORE THE LOCK IS TAKEN, and that ordering is
    the point rather than a tidiness. An open inside this transaction is an
    open every other writer of the state waits behind, so a FIFO or a slow
    file at the transcript path would hold the store's lock for as long as
    the open took -- and the fallback leg the hook's own timeout exists to
    reach is one of the things waiting. The region is read with no lock
    held (`_observe_recorded_position`) and handed to the locked decision
    as an input; under the lock only the entry's own fields are re-read and
    re-judged, and the transcript is never opened there."""
    from . import seats

    def change(entry):
        why = native_autocompaction(entry, session, agent, now, transcript,
                                    observed)
        if not why:
            return "", False
        entry["precompact"]["consumed"] = now
        _charge_entry(entry, now, False, None, seats.own_name())
        entry.update({"session": session, "mode": "native-auto",
                      "detail": why, "last_at": now})
        return why, True

    try:
        # Off the lock, but inside the fallback boundary: a malformed path
        # or an unexpected read/close failure must not abort ordinary recovery.
        observed = _observe_recorded_position(key, transcript)
        return _mutate_entry(key, change, nonblocking=True)
    except Exception as e:   # noqa: BLE001 — unconsumed is unvouched
        _say("helm seat resume-turn: the PreCompact record could not be "
             "consumed (%s) — this compaction is treated as deliberate" % e)
        return ""


# WHICH THREAD COMPACTED (task/2926). A subagent's autocompaction fires this
# SessionStart(compact) hook in the SUBAGENT, and whatever the hook prints is
# read by the subagent. Measured (task/2926): an Agent-tool lane of a
# project lead's seat compacted, and its own transcript recorded
# `SessionStart:compact hook success: helm seat resume-turn: spawned —
# ... Your own handoff ... (yours by session <the lead's>) says NEXT: ...` —
# the lead's landing queue, which names merges and prod deploys.
#
# THE PAYLOAD CANNOT SAY WHICH THREAD IT IS. Read from the bytes of Claude
# Code 2.1.280: the compaction path calls the SessionStart builder with the
# session, "compact" and the model only, and that builder's base input is
# built without a tool context, so a subagent's payload carries the LEAD's
# session_id, the LEAD's main transcript_path, the main thread's agent_type
# and no agent_id at all. `actors.sidechain_agent` therefore reads None and
# the guard at the top of hook() never fires for this event. The hook's
# process is the same claude pid as well, so a session record kind cannot
# tell them apart either.
#
# WHAT CAN: the transcripts. Every thread of a session writes its own file,
# and a compacting thread writes nothing between its PreCompact and this
# hook. So the MAIN transcript taking a turn after the PreCompact record's
# position proves the main thread was not the one compacting, and no
# subagent transcript written inside the compaction window proves no child
# could be. Anything between is UNKNOWN, and UNKNOWN is told it is not
# proven the lead: a child acting on a lead's merge or deploy NEXT is the
# worse error, and the lead still has `helm handoff check` and the pane.
THREAD_LEAD, THREAD_CHILD, THREAD_UNKNOWN = "lead", "child", "unknown"


def _live_subagents(transcript, session, now):
    """(paths, why) — this session's subagent transcripts written inside the
    compaction window, or why they could not be listed. They live at
    `<dir of the main transcript>/<session>/subagents/agent-*.jsonl`; a
    missing directory is a session that never spawned one."""
    d = os.path.join(os.path.dirname(transcript), session, "subagents")
    try:
        names = os.listdir(d)
    except (FileNotFoundError, NotADirectoryError):
        return [], ""
    except OSError as e:
        return None, "its subagent transcripts cannot be listed (%s)" % e
    floor, live = now - precompact_window_s(), []
    for n in names:
        if not (n.startswith("agent-") and n.endswith(".jsonl")):
            continue
        try:
            if os.stat(os.path.join(d, n)).st_mtime >= floor:
                live.append(n)
        except FileNotFoundError:
            continue
        except OSError as e:
            return None, "subagent transcript %s cannot be read (%s)" % (n, e)
    return sorted(live), ""


def _main_took_a_turn(entry, session, transcript, now):
    """True when the standing PreCompact record for THIS session names a
    position in the main transcript and a user or assistant turn was
    appended past it: the main thread kept working through the compaction,
    so the thread that compacted is not the main one."""
    rec = entry.get("precompact") if isinstance(entry, dict) else None
    if not isinstance(rec, dict) or "consumed" in rec \
            or rec.get("session") != session:
        return False
    at = _finite(rec.get("at"))
    if at is None or not 0 <= now - at < precompact_window_s():
        return False
    last = _finite(entry.get("last_at")) if "last_at" in entry else None
    if last is not None and at <= last:
        return False
    pos = _record_position(rec)
    if pos is None:
        return False
    obs = _observe_transcript(transcript, pos)
    if obs["why"]:
        return False
    for line in obs["region"].split(b"\n"):
        try:
            d = json.loads(line) if line.strip() else None
        except ValueError:
            d = None
        if isinstance(d, dict) and d.get("type") in ("user", "assistant"):
            return True
    return False


def compacting_thread(transcript, session, entry, now):
    """(THREAD_LEAD | THREAD_CHILD | THREAD_UNKNOWN, why) for a
    SessionStart(compact) whose payload carried no agent_id. See the block
    above for why the payload alone cannot answer."""
    if not transcript or not session:
        return THREAD_UNKNOWN, ("the payload names no transcript, so this "
                                "session's subagents cannot be read")
    live, why = _live_subagents(transcript, session, now)
    if live is None:
        return THREAD_UNKNOWN, why
    if not live:
        return THREAD_LEAD, "no subagent of this session is live"
    try:
        took = _main_took_a_turn(entry, session, transcript, now)
    except Exception:                     # noqa: BLE001 — unproven is unknown
        took = False
    if took:
        return THREAD_CHILD, ("the main transcript took a turn during this "
                              "compaction, so a subagent is the thread that "
                              "compacted")
    return THREAD_UNKNOWN, ("%d subagent(s) of this session wrote inside the "
                            "compaction window (%s) and nothing proves the "
                            "main thread is the one that compacted"
                            % (len(live), ", ".join(live[:3])))


def lead_suppressed_text(seat_name, why):
    """What a thread that is not PROVEN the lead hears in place of the lead's
    handoff NEXT: what was suppressed, why, and the route for each reader."""
    seat = _one_line(seat_name)
    whose, child = (("seat %s's" % seat, "a subagent of %s" % seat) if seat
                    else ("this seat's", "a subagent"))
    return ("lead handoff suppressed: this compaction is not proven to be "
            "%s main conversation (%s). If you are %s, "
            "follow your own brief; the seat's handoff NEXT is not yours, so "
            "do not merge, deploy, land or claim anything it names. If you "
            "are the seat's main conversation, `helm handoff check` names "
            "your own NEXT." % (whose, _one_line(why), child))


def _injection_digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _valid_injection(inj, now=None, handle=None, text=None, generation=None):
    """One PROVENANCE predicate shared by every reader and mutator.

    IT ASKS WHOSE TEXT THIS IS, AND NOTHING ELSE. Every clause here is a
    question a clock cannot answer: the text is non-empty, a generation was
    minted, the digest validates against the text, and the handle, text and
    generation are the ones the caller named. None of those change as time
    passes.

    NO FRESHNESS CLAUSE BELONGS HERE, and that is a structural property rather
    than a preference. Six lifecycle owners ask this one question -- observe,
    reset, clear, match, claim, release -- so a clock inside it hands every one
    of them an expiry gate none of them requested, invisibly, because not one
    of those functions mentions time. The horizon therefore lives in the ONE
    reader that answers a freshness question (`recorded_injections`), which
    keeps the lifecycle coherent by construction rather than by six flags that
    must agree.

    A clock here also breaks the lifecycle in three specific ways whenever
    stale recovery is permitted at all: a past-horizon record cannot acquire
    its first observation, a witnessed edit cannot reset it, and -- worst -- a
    claim cannot be RELEASED after a measured non-delivery, so the surviving
    token blocks every later claim on that generation permanently.

    `now` is accepted and ignored so callers that pass a pinned clock keep
    working; removing it from ten call sites would have hidden this change
    inside a rename.
    """
    del now                             # provenance does not consult a clock
    value = (inj or {}).get("text")
    return not (not value or not inj.get("generation")
                or inj.get("digest") != _injection_digest(value)
                or (handle is not None and inj.get("handle") != handle)
                or (text is not None and value != text)
                or (generation is not None
                    and inj.get("generation") != generation))


def _flock(fd, nonblocking, wait_s):
    """Take LOCK_EX on `fd`: blocking; nonblocking (one take, the lock's
    BlockingIOError raised as is); or nonblocking retried every LOCK_RETRY_S
    until `wait_s` has elapsed, then the last refusal raised. One locker
    for every writer of the store: the bound is a parameter of the one lock
    path, never a second path with its own rules."""
    if not nonblocking:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + max(0.0, wait_s or 0.0)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                raise
            time.sleep(min(LOCK_RETRY_S, left))


def _mutate_entry(key, change, nonblocking=False, wait_s=0.0):
    """Apply one locked state-entry mutation -> callback result.

    `nonblocking` takes the lock once and raises the lock's refusal;
    `wait_s` > 0 with it retries that take for up to `wait_s` seconds
    (`_flock`) -- the PreCompact producer's bounded wait -- and then raises
    the same refusal. Every caller already treats a raised mutation as a
    write that did not happen.

    THE READ UNDER THE LOCK IS STRICT. This store holds the act ledger --
    obsolete records, act intents, act receipts and their charges -- and a
    lenient read answers {} for a store that exists and cannot be read, so
    the write after it would rebuild an EMPTY ledger over the one it could
    not see. A read or parse error, or a store that is not a JSON object,
    raises instead (`_read_store`), and every caller already treats a raised
    mutation as a write that did not happen. A missing store is still
    empty."""
    from . import pk
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        _flock(lock.fileno(), nonblocking, wait_s)
        st = _read_store()
        entry = st.get(key) or {}
        result, changed = change(entry)
        if changed:
            st[key] = entry
            pk.write_json(p, st)
        return result


def _record_injection(key, session, handle, text, adapter=None, pids=None,
                      attempt=None, account_key=None, room=None):
    """Persist the exact clean-composer injection before any Enter is spent.

    `attempt` and `account_key` name the attempt that typed it and the resume
    state entry its acts are accounted under, so every recovery door can
    refuse a repeat of an act accounted obsolete-authorization.

    A RECORD TYPED UNDER A REPAIR AUTHORIZATION CARRIES THAT AUTHORIZATION.
    `account_key` marks it, named attempt or not, and `room` is the owed room
    its admission was pinned to, so a recovery whose caller brings no
    admission re-reads the one the text was typed under
    (`_repair_admission`)."""
    now = time.time()
    generation = hashlib.sha256(
        ("%s\0%s\0%s\0%d\0%d" %
         (key, handle, text, os.getpid(), time.time_ns()))
        .encode("utf-8")).hexdigest()[:24]

    def change(entry):
        entry["injection"] = {
            "generation": generation, "handle": handle,
            "session": session, "text": text,
            "digest": _injection_digest(text), "adapter": adapter or "",
            "pids": list(pids or []), "recorded_at": now,
            "expires_at": now + injection_ttl_s(), "held_at": None,
        }
        if account_key:
            entry["injection"].update(account_key=account_key, room=room)
            if attempt is not None:
                entry["injection"]["attempt"] = attempt
        return generation, True

    try:
        return _mutate_entry(key, change, nonblocking=True)
    except Exception as e:              # noqa: BLE001 — never block Enter
        _say("helm seat resume-turn: injection provenance write failed (%s)" % e)
        return False


def _wire_text(key, session, text):
    """Short exact-visible prompt; long directive stays retrievable by digest."""
    visible = " ".join((text or "").split())
    if len(visible) <= WIRE_TEXT_BYTES:
        return visible
    digest = _injection_digest(visible)
    token = hashlib.sha256(
        ("%s\0%s\0%s" % (key, session, visible)).encode("utf-8")
    ).hexdigest()[:16]
    wire = "Run `helm seat resume-turn --show %s`" % token
    if len(wire) > WIRE_TEXT_BYTES:
        _say("helm seat resume-turn: retrieval prompt exceeds exact-read bound")
        return visible

    def change(entry):
        entry["directive"] = {
            "token": token, "digest": digest, "text": visible,
            "session": session, "expires_at": time.time() + injection_ttl_s(),
        }
        return None, True

    try:
        _mutate_entry(key, change)
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: long directive store failed (%s)" % e)
        return visible
    return wire


def show_directive(token, now=None):
    """Unique unexpired long directive by its domain-separated token."""
    from . import pk
    entries = pk.read_json(state_path(), {}) or {}
    # Default admission must use the clock AFTER blocking storage I/O. An
    # explicit now remains a caller-requested historical snapshot, not a lease.
    stamp = time.time() if now is None else now
    found = set()
    for entry in entries.values():
        d = (entry or {}).get("directive") or {}
        text = d.get("text") or ""
        if (d.get("token") == token and stamp <= (d.get("expires_at") or 0)
                and d.get("digest") == _injection_digest(text)):
            found.add(text)
    return next(iter(found)) if len(found) == 1 else None


# The exact shape _wire_text emits for a long directive. Matching it is how a
# caller asks "is this injection an INDIRECTION rather than the instruction
# itself", which is a different question from whether the injection is Helm's.
_WIRE_INDIRECTION = re.compile(
    r"^Run `helm seat resume-turn --show ([0-9a-f]{16})`$")


def indirection_payload_available(text, now=None):
    """(True|False|None, token) — can the prompt's PAYLOAD still be fetched?

    True with a None token means the injection carries its own instruction and
    there is nothing to fetch. False means it names a directive that
    show_directive will refuse, so submitting it delivers a prompt whose
    answer no longer exists.

    THIS IS THE ONE PLACE THE DIRECTIVE'S HORIZON IS STILL AN ACTUATOR GATE,
    and deliberately so. A stale INJECTION is recoverable because its text IS
    the instruction and the pane still holds it. A stale INDIRECTION is not,
    because the text is only a pointer and the thing pointed at is gone --
    pressing Enter would submit a command that answers "no such directive".
    The directive TTL is therefore NOT relaxed to match the injection's; the
    admission is narrowed to match the directive's.
    """
    match = _WIRE_INDIRECTION.match((text or "").strip())
    if not match:
        return True, None
    token = match.group(1)
    return show_directive(token, now=now) is not None, token


def _directive_authority(text):
    """(expires_at or None, the directive record by value) for the directive
    an INDIRECTION prompt points at; (None, None) for a prompt that carries
    its own instruction. The recovery door's final capture takes the horizon
    and the accounting compares the record."""
    from . import pk
    match = _WIRE_INDIRECTION.match((text or "").strip())
    if not match:
        return None, None
    token = match.group(1)
    found = sorted({(d.get("expires_at"), d.get("digest"))
                    for d in ((e or {}).get("directive") or {}
                              for e in (pk.read_json(state_path(), {})
                                        or {}).values())
                    if d.get("token") == token}, key=repr)
    horizon = found[0][0] if len(found) == 1 and isinstance(
        found[0][0], (int, float)) else None
    return horizon, json.dumps([token, found], default=str)


def _matching_injection(key, handle, text, generation, now=None):
    """The unique live record for this exact generation, or None.

    THE BOOKKEEPING BUCKET IS NOT THE PROVENANCE, and requiring them to agree
    blocked a recovery that every provenance question had already passed
    (task/2463 finding 6). Injection records are filed under the key of
    whichever leg wrote them: the ordinary delivery leg files under a bare
    seat/session key, and the DEAF-IN-EFFECT repair runs under its own
    `deaf:<seat>:<session>` episode namespace. So a pane still holding the
    exact text helm put there, on the same handle, at the same generation,
    was refused for recovery by the repair leg because a different leg had
    recorded it -- and the wedged pane had no exit.

    WHAT ACTUALLY PROVES IT IS OURS is `_valid_injection`: the text, its
    digest, the handle and the generation, all four. The reader above already
    drops any handle claimed by two records, so there is at most one candidate
    here and the key can only say which bucket it came from. `key` stays in the
    signature because the caller's episode is what the diagnostic below is
    about, not because it is an authority."""
    inj = recorded_injections(now=now).get(handle)
    if not inj or not _valid_injection(inj, now=now, handle=handle, text=text,
                                       generation=generation):
        return None
    if inj.get("key") != key:
        _say("helm seat resume-turn: recovering an injection recorded by "
             "another leg (%s) for episode %s — same handle, same text, same "
             "generation, so it is provably helm's"
             % (inj.get("key") or "?", key or "?"))
    return inj


def _observe_injection(key, handle, text, generation, now=None):
    """Record the first proven-held observation; never refresh it."""
    stamp = time.time() if now is None else now

    def change(entry):
        inj = entry.get("injection") or {}
        if not _valid_injection(
                inj, now=stamp, handle=handle, text=text,
                generation=generation):
            return None, False
        if inj.get("held_at") is None:
            inj["held_at"] = stamp
            entry["injection"] = inj
            return inj, True
        return inj, False

    try:
        return _mutate_entry(key, change)
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: held observation write failed (%s)" % e)
        return None


def _clear_injection(key, handle, text, generation):
    """Clear exact provenance first; task cleanup can never re-arm Enter."""
    def change(entry):
        inj = entry.get("injection") or {}
        if not _valid_injection(
                inj, handle=handle, text=text, generation=generation):
            return False, False
        entry.pop("injection", None)
        return True, True

    clear_failed = False
    try:
        cleared = _mutate_entry(key, change)
    except Exception as e:              # noqa: BLE001
        cleared, clear_failed = False, True
        _say("helm seat resume-turn: injection provenance clear failed (%s)" % e)
    if not cleared and not clear_failed:
        return False                    # stale/mismatched callback: no authority
    closed, close_err = _close_recovery_task(
        handle, generation, ensure_terminal=clear_failed)
    if not closed:
        _say("helm seat resume-turn: recovery task close failed (%s)" % close_err)
    return bool(cleared or (clear_failed and closed))


def _reset_injection_observation(key, handle, text, generation):
    """A readable clear or mismatch breaks persistence for this generation."""
    def change(entry):
        inj = entry.get("injection") or {}
        if not _valid_injection(
                inj, handle=handle, text=text, generation=generation):
            return False, False
        if inj.get("held_at") is None:
            return True, False
        inj["held_at"] = None
        entry["injection"] = inj
        return True, True

    try:
        return _mutate_entry(key, change)
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: held observation reset failed (%s)" % e)
        return False


def injection_persistent(inj, now=None):
    stamp = time.time() if now is None else now
    held_at = (inj or {}).get("held_at")
    return (held_at is not None
            and stamp - held_at >= recovery_persist_s())


def recorded_injections(now=None, include_expired=False):
    """Injection records keyed by authoritative pane handle.

    Ambiguous handles are omitted: two state entries claiming one handle means
    identity is not unique enough to authorize a keystroke.

    IDENTITY DOES NOT EXPIRE; ONLY CURRENT-INTENT DOES, and this reader is the
    ONE place that asks the second question. `expires_at` is stamped
    ABSOLUTELY at record time, while every clause of `_valid_injection` --
    text, digest, handle, generation -- is a provenance question whose answer
    cannot change with the clock. So the horizon is applied HERE, over the
    records this reader returns, and `include_expired` decides whether a
    past-horizon record is still offered.

    THE HORIZON IS A FRESHNESS ANSWER, NEVER AN ACTUATOR REFUSAL. A record
    past it is still provably Helm's, and `seat composers --submit` may still
    recover it: that door re-reads the live pane at the moment of action and
    proceeds only if the composer still holds this exact text, which is a
    strictly stronger question than the record's age. Dropping the record at
    the horizon threw the provenance away with the currency and left a wedged
    pane with no exit at all. A record a repair typed (`account_key`) also
    meets that repair's act door; for a named attempt the door's horizon is
    shorter than this one, so past this horizon that recovery is refused
    there (`_repair_recovery_verdict`), never here. A manual nudge names no
    attempt and has no horizon at the door.
"""
    from . import pk
    stamp = time.time() if now is None else now
    found, ambiguous = {}, set()
    for key, entry in (pk.read_json(state_path(), {}) or {}).items():
        inj = (entry or {}).get("injection") or {}
        if not _valid_injection(inj, handle=None, text=None, generation=None):
            continue
        # THE ONE FRESHNESS GATE. This reader is where "is this still the
        # current intent" is asked, and it is asked HERE rather than inside
        # the provenance predicate so that the lifecycle owners below cannot
        # inherit it by accident.
        expired = stamp > (inj.get("expires_at") or 0)
        if expired and not include_expired:
            continue
        handle = inj.get("handle")
        if handle in found:
            ambiguous.add(handle)
            continue
        found[handle] = dict(inj, key=key, expired=expired)
    for handle in ambiguous:
        found.pop(handle, None)
    return found


def _claim_injection_recovery(injection):
    """Only a persistent canonical injection can reserve Enter authority."""
    return _reserve_injection(injection, require_persistence=True)


def _claim_injection_observation(injection):
    """Reserve before census reads, even before the first held observation."""
    return _reserve_injection(injection, require_persistence=False)


def _reserve_injection(injection, require_persistence):
    """One exclusive canonical token for observation OR recovery, never a lease."""
    delivered, task_err = _recovery_task_delivered(
        injection.get("handle"), injection.get("generation"))
    if delivered is not False:
        if task_err:
            _say("helm seat resume-turn: recovery claim refused (%s)" % task_err)
        return None
    now = time.time()

    def change(entry):
        inj = entry.get("injection") or {}
        # Both readers and actuators exclude competing owners before looking.
        # Only recovery requires maturity; a census read can witness an edit
        # before a pending timestamp matures, or while another observer would
        # otherwise establish the first timestamp. Identity never expires.
        if (not _valid_injection(
                inj, now=now, handle=injection["handle"],
                text=injection["text"], generation=injection["generation"])
                or (require_persistence and not injection_persistent(inj, now=now))):
            return None, False
        # Both purposes share the same exclusive field, never a renewable
        # lease or permission by itself. Census releases only by atomically
        # finalizing its observation; recovery releases on measured non-delivery
        # or safe zero-attempt refusal. A measured DELIVERED whose cleanup
        # stores both fail must leave this token consumed forever; expiring it
        # after 120s authorized a second Enter when identical text reappeared.
        if inj.get("recovery"):
            return None, False
        token = hashlib.sha256(
            ("%s\0%d\0%d" % (inj["generation"], os.getpid(),
                              time.time_ns())).encode("utf-8"))
        token = token.hexdigest()[:24]
        inj["recovery"] = {"token": token, "at": now}
        entry["injection"] = inj
        return token, True

    try:
        return _mutate_entry(injection["key"], change)
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: recovery claim failed (%s)" % e)
        return None


def _finish_injection_observation(injection, token, exact, observed_at):
    """Return the committed observation snapshot, or None on failure.

    The snapshot and token release belong to ONE transaction. Never classify
    the pre-reservation record or re-read after release: another observer can
    change persistence in either window. Freshness describes observed_at.
    A failed write leaves the PRE-read reservation consumed. Never follow it
    with a generic release: an edit cannot be forgotten because storage is down.
    """
    def change(entry):
        inj = entry.get("injection") or {}
        if (not _valid_injection(
                inj, handle=injection["handle"], text=injection["text"],
                generation=injection["generation"])
                or (inj.get("recovery") or {}).get("token") != token):
            return None, False
        if exact is False:
            inj["held_at"] = None
        elif exact is True and inj.get("held_at") is None:
            inj["held_at"] = observed_at
        # None means unreadable, not clear. Existing exact observations keep
        # their original time; first observations use the actual read time.
        inj.pop("recovery")
        entry["injection"] = inj
        observed = dict(inj, key=injection["key"],
                        expired=observed_at > (inj.get("expires_at") or 0))
        return observed, True

    try:
        # _mutate_entry returns the result only AFTER its write succeeds.
        return _mutate_entry(injection["key"], change)
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: observation finalization failed (%s)" % e)
        return None


def _release_injection_recovery(injection, token):
    def change(entry):
        inj = entry.get("injection") or {}
        if (not _valid_injection(
                inj, handle=injection["handle"], text=injection["text"],
                generation=injection["generation"])
                or (inj.get("recovery") or {}).get("token") != token):
            return False, False
        inj.pop("recovery", None)
        entry["injection"] = inj
        return True, True

    try:
        return _mutate_entry(injection["key"], change)
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: recovery claim release failed (%s)" % e)
        return False


def _repair_admission(injection):
    """(admit, "") -- the repair authorization `injection` was typed under,
    as the `_prepare_due` admission its own child would hand a recovery -- or
    (None, a sentence the owner can act on) when that authorization cannot be
    named.

    The seat is the one the record was filed under, and it is taken only when
    the record's account key IS that seat's repair key for the recorded
    session. A record that disagrees -- a nameless pane's, or one filed by
    another writer -- names no seat whose roster episode, family pause and
    owed rows could be re-read, so nothing is authorized."""
    seat_name, session = injection.get("key"), injection.get("session")
    key = injection.get("account_key")
    if not seat_name or key not in (_repair_key(seat_name, session),
                                    _rearm_key(seat_name, session)):
        return None, (
            "the injection recorded for pane %s was typed under repair "
            "authorization %s, and its record (filed under %s for session "
            "%s) names no seat that authorization belongs to, so the attempt, "
            "the family pause and the owed row it answers to cannot be "
            "re-read and this recovery acts on nothing. Read the pane and "
            "resolve the held text by hand; the repair mints a fresh attempt "
            "on its next pass" % (injection.get("handle"), key,
                                  seat_name or "no seat",
                                  session or "no session"))
    room, attempt = injection.get("room"), injection.get("attempt")
    return _admission(seat_name, session, key, room, attempt), ""


def _repair_recovery_verdict(injection, now=None):
    """(kind, why) -- what the act door a recovery of `injection` meets will
    answer about the ATTEMPT that typed it, read without acting: None for a
    record typed under no repair authorization; "" while that attempt may
    still act; "stale" (with the door's own sentence) once it is retired,
    settled or past its horizon, which it never recovers from; "unknown"
    when the authorization cannot be named or its roster row re-read.

    ONE ROSTER READ, AND THE DOOR'S OWN JUDGES. The authorization is named by
    `_repair_admission` and the attempt judged by `_attempt_refusal`, exactly
    as `_prepare_once` judges it, so a census cannot call a strand eligible
    that the door refuses. The family pause and the owed row are not asked
    here: they are the door's to re-ask at the moment of action."""
    if not (injection or {}).get("account_key"):
        return None, ""
    admit, why = _repair_admission(injection)
    if admit is None:
        return "unknown", why
    seat_name = injection.get("key")
    try:
        _key, row, _rows, err = _roster_row(seat_name)
    except Exception as exc:                 # noqa: BLE001 — unknown, never eligible
        row, err = None, exc.__class__.__name__
    if err or row is None:
        return "unknown", err or "%s is not in the roster" % seat_name
    stale, _horizon = _attempt_refusal(
        injection.get("attempt"), row, time.time() if now is None else now)
    return ("stale", stale) if stale else ("", "")


def _joined(*parts):
    """"a; b" -- and a refusal among `parts` keeps its door and kind through
    the join, so a caller reads "stale" or "paused" as a field and never from
    prose. The first part that carries them names them."""
    from . import harness
    text = "; ".join(str(p) for p in parts)
    refusal = next((p for p in parts if isinstance(p, harness.DoorRefusal)),
                   None)
    return text if refusal is None else harness.DoorRefusal(
        text, refusal.door, refusal.kind)


def recover_injection(injection, adapter=None, on_attempt=None, admit=None):
    """Re-prove pane identity, reserve the generation, and recover it.

    The legacy on_attempt hook prepares a send; final admission can still
    withhold it. Only the actuator's subsequent nonblocking accounting marks
    entry into the Enter path for ambiguity retention.

    `admit(door)` -> harness.Grant is the caller's prepared authorization.
    Given one, every recovery Enter goes through the adapter's act door with
    it, per attempt. A caller that brings none for a record typed under a
    repair authorization gets the admission that authorization is re-read
    through (`_repair_admission`), or a refusal when it cannot be named; only
    a record typed under no authorization is gated by the payload check
    alone.
    """
    from . import autocompact, harness
    # A REPEAT OF AN ACT ACCOUNTED OBSOLETE-AUTHORIZATION IS REFUSED AT EVERY
    # RECOVERY DOOR, whoever calls it: the child's own recovery and `helm seat
    # composers --submit` alike. The injection names the attempt that typed it
    # and the resume-state key its acts were accounted under. Refused before
    # the claim, so nothing is held.
    spent = _obsolete_act(injection.get("account_key"),
                          injection.get("attempt"))
    if spent:
        if spent.get("unreadable"):
            return harness.UNKNOWN, harness.DoorRefusal(
                "the injection recorded for pane %s names attempt %s, and "
                "whether an act for it was accounted OBSOLETE-AUTHORIZATION "
                "could not be read (%s), so this recovery acts on nothing"
                % (injection.get("handle"), injection.get("attempt"),
                   spent.get("what") or "no detail"), "recovery", "unknown")
        return harness.UNKNOWN, harness.DoorRefusal(
            "the injection recorded for pane %s names attempt %s, whose %s "
            "act was accounted OBSOLETE-AUTHORIZATION (%s), so this recovery "
            "does not repeat it" % (injection.get("handle"), injection.get("attempt"),
                           spent.get("door") or "earlier",
                           spent.get("what") or "no detail"),
            "recovery", "obsolete")
    # A RECOVERY DOES NOT SHED THE AUTHORIZATION ITS TEXT WAS TYPED UNDER.
    # The repair child's own recovery hands its admission in; `helm seat
    # composers --submit` has none to hand, and a held text whose attempt
    # retired or passed its horizon, or whose family paused, is still
    # exactly what the pane holds. So the admission is rebuilt from the
    # record, and every Enter meets the same act door. Refused before the
    # claim, so nothing is held.
    if admit is None and injection.get("account_key"):
        admit, why = _repair_admission(injection)
        if admit is None:
            return harness.UNKNOWN, harness.DoorRefusal(why, "recovery",
                                                        "unknown")
    token = _claim_injection_recovery(injection)
    if not token:
        return harness.UNKNOWN, "injection is stale, ineligible, or already recovering"

    attempts, observations = [], []

    def release_unspent():
        # Only a witnessed mismatch breaks persistence. Unreadability spends
        # no Enter and proves no edit. If invalidation fails, the claim that
        # preceded the read remains consumed across later calls.
        if False in observations and not _reset_injection_observation(
                injection["key"], injection["handle"], injection["text"],
                injection["generation"]):
            return "persistence reset failed; recovery authority retained"
        if not _release_injection_recovery(injection, token):
            return "recovery claim release failed; recovery authority retained"
        return ""

    def payload_refusal():
        available, pointer = indirection_payload_available(injection["text"])
        if available is not True:
            return ("pane %s holds a Helm RETRIEVAL PROMPT for directive %s, "
                    "and that directive is no longer available -- submitting "
                    "it would deliver a command whose instruction cannot be "
                    "fetched. Identity is proven; the payload is not"
                    % (injection["handle"], pointer))
        return None

    def admit_attempt():
        # This is inside the common actuator, after each exact composer read,
        # including retries and automatic recovery. Entry-time availability
        # cannot authorize a later Enter after a lock wait or backoff.
        return payload_refusal()

    def door(name):
        # THE CALLER'S AUTHORIZATION AND THE PAYLOAD'S, PREPARED TOGETHER AND
        # VALIDATED TOGETHER. The recovery door takes a fresh exact read
        # between this preparation and its validation, so both halves are
        # re-asked after the proof: the reason to press the key, and whether
        # the directive the prompt points at can still be fetched.
        from . import harness
        grant = admit(name)
        if not grant.ok:
            return grant
        refusal = payload_refusal()
        if refusal:
            # A REFUSAL BEFORE THE KEYSTROKE ABANDONS THE ORIGINAL GRANT. The
            # door is handed a new, refused grant and abandons only that one,
            # so the intent this preparation recorded would otherwise survive
            # a known zero-act refusal and refuse the attempt's next recovery
            # as an act of unknown outcome.
            grant.abandon()
            return harness.Grant(False, refusal, "unknown")

        def still():
            valid, why, facts = grant.capture()
            if valid is not True:
                return valid, why, facts
            facts = dict(facts)
            try:
                refusal = payload_refusal()
                expires, record = _directive_authority(injection["text"])
            except Exception as exc:         # noqa: BLE001 — UNKNOWN, refuses
                return None, ("the directive the prompt points at could not "
                              "be read (%s)" % exc.__class__.__name__), facts
            if refusal:
                return False, refusal, facts
            # THE DIRECTIVE'S HORIZON GATES THE ENTER, so it joins the
            # attempt's in the CPU-only compare before the keystroke; its
            # record joins the versions the accounting compares. The
            # injection's own expires_at does not: it is a freshness answer,
            # never an actuator refusal (`recorded_injections`).
            if expires is not None:
                facts["horizon"] = min(h for h in (facts.get("horizon"),
                                                   expires) if h is not None)
            if record is not None:
                facts["versions"] = dict(facts.get("versions") or {},
                                         directive=record)
            return True, "", facts
        return harness.Grant(True, still=still, account=grant.account,
                             abandon=grant.abandon)

    def prepare_attempt(n):
        from . import pk
        # Audit and the caller's legacy pre-send callback can both block.
        # Finish them before final admission; preparing is NOT an Enter.
        # Keep record age visible without claiming a withheld send occurred.
        recorded_at = injection.get("recorded_at")
        age = "" if not isinstance(recorded_at, (int, float)) else \
            " (record age %ds%s)" % (
                max(0, int(time.time() - recorded_at)),
                ", past its freshness horizon" if injection.get("expired")
                else "")
        pk.event("seat.resume-turn", injection["key"],
                 "preparing bare Enter recovery %d/%d for pane %s%s"
                 % (n, RECOVERY_ATTEMPTS, injection["handle"], age))
        if on_attempt is not None:
            on_attempt(n)

    def act(ad, handle, detail):
        if handle != injection["handle"]:
            return harness.UNKNOWN, ("pane identity changed from %s to %s" %
                                     (injection["handle"], handle))
        if injection.get("adapter") and ad.name != injection["adapter"]:
            return harness.UNKNOWN, ("metaharness changed from %s to %s" %
                                     (injection["adapter"], ad.name))
        state, proof = ad.retry_held_submission(
            handle, injection["text"], attempts=RECOVERY_ATTEMPTS,
            backoff=recovery_backoff_s(), on_attempt=attempts.append,
            on_observation=observations.append,
            prepare_attempt=prepare_attempt,
            **({"admit": door} if admit is not None
               else {"before_attempt": admit_attempt}))
        if not detail:
            return state, proof
        # A REFUSAL KEEPS ITS DOOR AND KIND through the pane proof's prefix.
        return state, _joined(detail, proof)

    try:
        pids = injection.get("pids") or []
        if pids:
            from . import orcaadopt
            ad = adapter if adapter is not None else harness.detect()
            if ad is None:
                result = (harness.UNKNOWN, harness.RECOMMENDATION)
            else:
                identities = [orcaadopt.parse_ident(p) for p in pids]
                if any(p is None for p in identities):
                    result = (harness.UNKNOWN,
                              "recorded process identity is malformed")
                else:
                    handle, why = orcaadopt.authorized_handle(identities, ad)
                    result = ((harness.UNKNOWN, why) if handle is None else
                              act(ad, handle, why))
        else:
            row = {"seat": injection["key"],
                   "registered_session": injection["session"]}
            result, err = autocompact._pane_action(
                row, adapter, act, for_send=True)
            if result is None:
                result = (harness.UNKNOWN, err)
        state, detail = result
        if state == harness.DELIVERED:
            terminal = _clear_injection(
                injection["key"], injection["handle"], injection["text"],
                injection["generation"])
            if not terminal:
                state = harness.UNKNOWN
                detail = _joined(detail, "delivery advanced but no terminal "
                                         "authority persisted")
        # UNKNOWN after ANY attempt retains the claim, even when a later
        # read or payload check refuses. Only a zero-attempt refusal may
        # release it, after invalidating any witnessed edit durably.
        elif state == harness.NOT_DELIVERED:
            if not _release_injection_recovery(injection, token):
                detail = _joined(detail, "recovery claim release failed; "
                                         "recovery authority retained")
        elif not attempts:
            refusal = release_unspent()
            if refusal:
                detail = _joined(detail, refusal)
        return state, detail
    except Exception:
        if not attempts:
            refusal = release_unspent()
            if refusal:
                _say("helm seat resume-turn: " + refusal)
        raise


# ---------------------------------------------------------------------------
# the resume text — the seat's OWN directive, never an invented one
# ---------------------------------------------------------------------------

def _one_line(s):
    """A pane-safe single line. The directive comes out of a journal file the
    agent authored, and it crosses the adapter seam into a live terminal, so it
    is laundered exactly like every other agent-authored string helm prints."""
    from . import seats
    return seats._clip(" ".join(seats._scrub(str(s or "")).split()), TEXT_BYTES)


# THE COMMAND THAT ACTUALLY ANSWERS THE SENTENCE IT SITS IN. All three texts
# below tell a compacted seat what its live obligations are, and all three
# used to name `helm dispatch list --open` — which was
# not caller-scoped at all. Measured from a seat holding zero obligations
# (task/1007): it returned two rows, naming two OTHER seats, and that seat
# briefly adopted one of them as its own. The verb had no filter to offer, so
# the instruction could not be followed literally by the one reader with the
# least context to notice — and it fires on EVERY compaction.
#
# ...AND THE SENTENCE WAS STILL FALSE FOR AN AUTHOR (measured on one seat,
# 2026-08-12T01:54Z). `--mine` is RECIPIENT-scoped, so `--open --mine` printed
# "no matching rows naming @<seat>" while the seat held an OPEN row it had
# SENT, two hours past deadline, that no one else could chase. Three texts told
# it, on every compaction, that those were its ONLY live obligations — "you are
# free" to a seat holding live work, at the moment it has least context to
# doubt it. The issuer's obligation is real: what a sender owes is the DELIVERY
# LEG — that the recipient knows the row exists and what it needs — so a row
# sent but never delivered, or delivered to a seat that went quiet, is the
# sender's to chase.
#
# THE COMMAND NAMES BOTH DIRECTIONS AND IS STILL ONE COMMAND. Naming two would
# rebuild the incident at execution time: this reader runs the first, reads a
# reassuring nothing, and stops. `--mine --issued` is the UNION, both halves
# bound to the SAME resolved identity, so it can widen to no other seat's rows.
#
# ONE CONSTANT, THREE USES, so a later fix cannot land in two texts and leave
# the third teaching the old command. The whole CLAUSE is a constant too, for
# the same reason and by the same argument: the defect was never only the
# command — it was the SENTENCE around it claiming a completeness it did not
# have, and a sentence edited in two places out of three is the identical bug
# one layer up. Concatenated, never %-formatted, because UNCLAIMED carries its
# own `%s` for the attribution clause.
OBLIGATIONS_CMD = "helm dispatch list --open --mine --issued"

# THE SHARED TAIL WAS 645 CHARACTERS, byte-identical in all four variants, and
# every sentence of it restated something the verb's own help says: what --open
# covers, what it is silent about, that a HELD row is in neither half. That is
# the verb's contract, and `helm dispatch list --help` serves it on demand.
#
# WHAT STAYS is the part a reader cannot get from the help: that BOTH halves
# are one question, and that a row you SENT is yours to chase rather than to
# do. Concatenated, never %-formatted, because UNCLAIMED carries its own `%s`.
OBLIGATIONS = ("Obligations: `" + OBLIGATIONS_CMD + "`, then "
               "`helm dispatch list --held --mine --issued` — a HELD row is "
               "in neither half. A row you sent is "
               "yours to chase, not to do; a cancelled or superseded row is "
               "DEAD however open its chat looks, so check its status first. "
               "An unreadable ledger means UNKNOWN, never empty — report it. ")

# The re-ground act, one sentence, shared. A compacted context makes stale
# history read as an invitation, which is why re-grounding comes BEFORE acting
# rather than being offered as an option.
REGROUND = ("Re-ground first: `helm handoff check`, `helm now show`, then "
            "re-read your task. ")

GENERIC = ("Resuming after compaction. No handoff was written for this one. "
           + REGROUND + OBLIGATIONS + "Do not wait for a human.")

UNCLAIMED = ("Resuming after compaction. A handoff was written, but not by "
             "you — this project's journal shelf is shared, and %s. Do not "
             "act on it, and do not release, land or claim anything it "
             "names. " + REGROUND + OBLIGATIONS + "Do not wait for a human.")

UNREADABLE = ("Resuming after compaction. Whether a handoff was written is "
              "unknown: an entry on this project's shelf could not be read at "
              "all, and it may have been yours. Report the unreadable shelf "
              "rather than working around it. " + REGROUND + OBLIGATIONS
              + "Do not wait for a human.")

# THE ONE VARIANT THAT IS DELIBERATELY LONGER, and the report says so. It
# carries two facts no other variant has and neither is reconstructable: that
# `helm handoff check` matches on SESSION ID and so can still find your entry
# without a name, and that a handoff you WRITE from here is stamped with an
# empty seat that no later reader can attribute. The second is a harm this
# turn can still cause, which is the test for what stays in a hook line.
ANONYMOUS = ("Resuming after compaction. This process declares no seat "
             "identity ($HELM_CHAT_NAME is unset), so nothing on the shared "
             "shelf can be proven yours — or anyone else's. Do not read that "
             "as nothing to continue from: `helm handoff check` matches on "
             "session id and will name your own entry if one exists. Export "
             "HELM_CHAT_NAME, because a handoff you write without it carries "
             "an empty seat stamp nobody can attribute later. " + OBLIGATIONS
             + "Do not wait for a human.")


_UNCLAIMED_WHY = {
    "foreign-only": "the fresh entries there name other seats",
    "unattributed": "no fresh entry there can be proven yours",
}


def resume_text(cwd, sid, transcript=None):
    """The line injected into the pane -> (text, source_path_or_None).

    The seat's own freshest handoff wins, because "continue" alone is an
    invitation to invent work: a compacted agent has lost the very context
    that told it what continuing means. `handoff.compaction_floor` is the ONE
    definition of "written for this compaction" — the same floor the PreCompact
    nag enforces, so the two legs can never disagree about which artifact is
    current.

    OWN is a PROVEN word here, not a hopeful one. This text is imperative and
    ends "do not wait for a human", and it arrives at the one reader who has
    just lost the context that would let it notice a mismatch — so the sentence
    NAMES the identity it matched on, and the shared-shelf case gets its own
    text rather than the GENERIC one. "No handoff was written" would be a
    second false claim about the same true reading (see
    `handoff.attribute_entry`, measured 2026-07-31)."""
    from . import handoff
    project = handoff._project(cwd)
    if not project:
        return GENERIC, None
    floor = handoff.compaction_floor(None, transcript)
    path, meta, reason = handoff.attribute_entry(project, sid=sid, floor=floor)
    if not path:
        if reason == handoff.UNREADABLE:
            return UNREADABLE, None
        # A READER WITH NO NAME GETS ITS OWN SENTENCE, never UNCLAIMED's. The
        # UNCLAIMED text asserts "a handoff WAS written for this compaction, but
        # not by you" and "the entry on the shelf is someone else's plan" — two
        # claims an anonymous process cannot support, and both were printed to a
        # seat whose own session id was on the shelf (task/1688).
        if reason == handoff.NO_IDENTITY:
            return ANONYMOUS, None
        why = _UNCLAIMED_WHY.get(reason)
        return (UNCLAIMED % why if why else GENERIC), None
    nxt = _one_line(meta.get("next") or meta.get("remaining"))
    if not nxt:
        return GENERIC, None
    proof = ("session %s" % _one_line(str(meta.get("session_id") or ""))[:8]
             if reason == handoff.BY_SID
             else "seat %s" % _one_line(str(meta.get("seat") or "")))
    return ("Resuming after compaction (helm resume-turn). Your own handoff "
            "%s (yours by %s) says NEXT: %s — continue that now. Re-read the "
            "full entry before acting if you need the DONE/REMAINING context. "
            "Do not wait for a human."
            % (_one_line(os.path.basename(path)), proof, nxt)), path


# ---------------------------------------------------------------------------
# the alert — loud, never a silent no-op
# ---------------------------------------------------------------------------

def _wake_text(seat_name):
    """The measured recovery route after an auto-resume refusal.

    The mandatory Monitor beacon is a REAL self-wake path, and `seats.beacon_procs`
    is already its exact-shape process instrument. Pane input remains a fallback;
    probe trouble is UNKNOWN, never a confident instruction."""
    if not seat_name:
        return ("Inbox beacon state is UNKNOWN because this session declares no "
                "seat name; pane input may be a fallback, not a proven requirement.")
    try:
        from . import seats
        pids, trouble = seats.beacon_procs(seat_name, strict=True)
    except Exception as exc:                 # noqa: BLE001 — an alert never raises
        pids, trouble = [], "%s: %s" % (type(exc).__name__, exc)
    if trouble:
        return ("Inbox beacon state is UNKNOWN (%s); pane input may be a fallback, "
                "not a proven requirement." % _one_line(trouble))
    if pids:
        return ("It has an armed inbox beacon and will wake on its next @mention "
                "or DM; pane input remains a fallback.")
    return ("No live inbox beacon was observed; pane input is the fallback if the "
            "pane still exists.")


def alert_text(seat_name, reason):
    return ("⚠️ RESUME-TURN: seat %s compacted and could NOT be auto-resumed "
            "(%s). %s" % (_one_line(seat_name or "?"), _one_line(reason),
                            _wake_text(seat_name)))


def _display_name(seat_name, session):
    """The label for ALERT text and events ONLY — never an injection target.

    A NAMELESS session still deserves an alert a human can act on, and the
    chat roster usually remembers which seat held the sid. That reverse-lookup
    is COSMETIC and carries no trust: a roster row is other-process-supplied
    data, so it may caption the alert but must never choose the pane (that is
    process evidence, in `_registered`) nor let this process ACT AS the seat
    (seats.own_name()'s strictness, which stays intact)."""
    if seat_name:
        return seat_name
    try:
        from . import seats
        hit = seats.seat_for_session(session)
    except Exception:
        hit = None
    return hit or ("session:" + str(session or "")[:8])


def dm_wake_text(reason):
    return ("⚠️ RESUME-TURN direct wake: %s — the room alert describes you "
            "but cannot wake you; this DM can. Act on it now (your trail: "
            "`helm handoff check`; your debts: `helm chat read`)."
            % _one_line(reason))


def _say(msg):
    """Fail-closed diagnostics: a closed stderr raised BrokenPipe inside a
    failure path and cost the room row (meld finding 7). Narrating a failure
    must never create one."""
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


def _episode(transcript):
    """A STABLE compaction-episode identity (meld corrections 1+5).

    seat+sid cannot key the wake debounce: one session legitimately compacts
    twice inside the window and the second deserves its wake. The transcript
    is rewritten by each compaction, so (mtime_ns, size) repeats for a
    REPLAYED delivery of the same hook event and changes for a genuinely
    later compaction. Unstatable transcript => "" => no debounce at all —
    fail toward waking (a duplicate costs a glance; a suppressed wake costs
    the 65-alert class this module exists to end). Never reason prose, never
    a fresh nonce."""
    try:
        st = os.stat(transcript)
        # dev/ino/ctime_ns join the identity: (mtime, size) alone collides
        # under a restored mtime with an equal-length rewrite (measured by
        # the reviewer), and ctime cannot be set from userspace.
        return "%d:%d:%d:%d:%d" % (st.st_dev, st.st_ino, st.st_ctime_ns,
                                   st.st_mtime_ns, st.st_size)
    except Exception:
        return ""


def _wake_join_s():
    try:
        return float(os.environ.get("HELM_RESUME_TURN_WAKE_JOIN_S") or 60.0)
    except (TypeError, ValueError):
        return 60.0


def _holder_proves(seat_name, holder):
    """KERNEL EVIDENCE for the wake target: the payload's holder token
    (pid:starttime, stamped by the hook from its own PARENT — the seat's
    live process) re-proved at wake time. (pid, starttime) is the
    unforgeable birth identity on Linux; the environ read follows
    orcaadopt's IDENTITY_KEYS discipline (one named key, never a full
    environ dump — an environ can carry credentials). A pipe is transport,
    not authorship (the reviewer's forged-FIFO probe minted a DM), so
    descriptor type grants nothing: this proof or the spawn register are
    the only doors, and both are process evidence a naive or
    prompt-injected caller cannot produce. Same-uid deliberate forgery
    remains the honest ceiling, exactly as the chat store documents."""
    from . import orcaadopt, procid
    ident = orcaadopt.parse_ident(holder or "")
    if ident is None or not getattr(ident, "start", None):
        return False
    pid, start = orcaadopt.ident_key(ident)
    if orcaadopt.proc_start(pid) != start:
        return False
    try:
        with open(os.path.join(procid.proc_root(), str(pid),
                               "environ"), "rb") as f:
            raw = f.read()
    except OSError:
        return False
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep and k == b"HELM_CHAT_NAME":
            return v.decode("utf-8", "replace") == seat_name
    return False


def _dm_target(seat_name, session, holder=None):
    """The ONLY source of a DM-wake target: proved process identity AND a
    currently-routable lane. Two proofs, both independent of caller text:
    the spawn register binding seat<->session, or the holder token's
    kernel evidence (_holder_proves — the 65-alert class fires precisely
    when the register CANNOT bind, and the seat's own live process is the
    evidence that remains). Identity is then not enough: rename_seat
    leaves a live process's env stale while the roster and DM lane move,
    so a positively-ABSENT roster entry refuses routing; UNKNOWN roster
    evidence never manufactures a refusal (the resolver's asymmetry)."""
    if not seat_name:
        return None
    proved = False
    try:
        why, _pids = _registered(seat_name, session)
        proved = not why
    except Exception:
        proved = False
    if not proved and holder:
        proved = _holder_proves(seat_name, holder)
    if not proved:
        return None
    try:
        from . import seats
        cap = seats.recipient_capability(seat_name)
        if (cap.get("membership") == "ABSENT"
                and cap.get("evidence") == "populated"):
            _say("helm seat resume-turn: wake refused — %r is not in the "
                 "current roster (renamed seat? the lane moved with it)"
                 % seat_name)
            return None
    except Exception:
        pass    # UNKNOWN never manufactures a refusal on its own
    return seat_name

def _wake_refusal(target, episode):
    """Why this wake DM is not due, or "" when it is.

    One wake per (target, episode): a replayed delivery of the same
    compaction must not wake twice, while a genuine later compaction (new
    episode) always does. An empty episode is never debounced. A resume state
    that cannot be read cannot say whether this episode already woke the
    target, so the DM is refused and the room row, which no debounce governs,
    still posts."""
    if not episode:
        return ""
    prior = _peek("wake:" + target)
    if isinstance(prior, Unreadable):
        return ("wake DM refused — the resume state could not be read (%s), "
                "so whether episode %s already woke %s is UNKNOWN; the room "
                "row still posts" % (prior.why, episode, target))
    if prior and prior.get("session") == episode \
            and time.time() - prior.get("last_at", 0) < _window_s():
        return "wake debounced — episode %s already woke %s" % (episode,
                                                                 target)
    return ""


def wake_alert(payload):
    """The wake-child's WHOLE job — proof, debounce, routability, delivery,
    honest claim. Runs outside any hook budget, so blocking is legal here;
    what is not legal is COUPLED blocking, so each leg runs in its own
    bounded worker and either completes when the other stalls (meld
    correction 2). The DM's sender is "resume-turn/wake": "/" cannot appear
    in a joinable seat token, so it can never collide with a real seat named
    resume-turn, whose wake would otherwise append and never deliver
    (self-DM suppression). THE CLAIM IS HONEST (meld correction 4): a DM row
    plus a MEASURED armed beacon is "queued to an armed route" — never
    "delivered" — and with no live beacon it is queued bytes, not actuation,
    so the dm leg contributes no success."""
    display = payload.get("display") or "?"
    reason = payload.get("reason") or ""
    seat_name = payload.get("seat") or None
    session = payload.get("session") or None
    episode = payload.get("episode") or ""
    outcome = {"wake": False, "room": False}   # "wake", not "dm": the launder tripwire pins identity-field readers, and this dict is not a chat row
    # Bookkeeping lives HERE, where blocking is legal: the hook carries the
    # record key in the payload instead of taking _record's LOCK_EX itself
    # (a held state lock inside the hook gave wake_armed=True with the hook
    # blocked behind it — the reviewer's probe).
    rec_key = payload.get("record_key") or ""
    if rec_key:
        _record(rec_key, session or "", payload.get("mode") or "alert",
                reason, count_it=False)

    target = _dm_target(seat_name, session,
                        holder=payload.get("holder")) if seat_name else None
    if seat_name and not target:
        _say("helm seat resume-turn: wake DM refused — %r is not a proved, "
             "routable process identity for session %s"
             % (seat_name, session))
    refusal = _wake_refusal(target, episode) if target else ""
    if refusal:
        _say("helm seat resume-turn: " + refusal)
        target = None

    def dm_leg():
        if not target:
            return
        try:
            from . import seats
            _row, err = seats.dm(target, dm_wake_text(reason),
                                 who="resume-turn/wake")
            if err:
                _say("helm seat resume-turn: wake DM refused by the "
                     "resolver (%s): %s" % (target, err))
                return
            _record("wake:" + target, episode, "wake", reason,
                    count_it=False)
            pids, trouble = [], None
            try:
                from . import seats as _s
                pids, trouble = _s.beacon_procs(target, strict=True)
            except Exception as e:              # noqa: BLE001
                trouble = str(e)
            if pids:
                outcome["wake"] = True          # queued to an ARMED route
            else:
                _say("helm seat resume-turn: wake QUEUED for %s but no live "
                     "beacon was measured (%s) — queued bytes are not "
                     "actuation; pane input is the fallback"
                     % (target, trouble or "no beacon procs"))
        except Exception as e:                  # noqa: BLE001
            _say("helm seat resume-turn: wake DM failed (%s): %s"
                 % (target, e))

    def room_leg():
        try:
            from . import chat
            chat.post(alert_text(display, reason), who="resume-turn")
            outcome["room"] = True
        except Exception as e:                  # noqa: BLE001
            _say("helm seat resume-turn: alert post failed (%s): %s"
                 % (display, e))

    # Start the durable room row first. Under a loaded scheduler the first
    # thread can consume the whole tiny test/production join budget before its
    # sibling is ever scheduled; starting the best-effort DM first then returned
    # False while the room thread had not run yet. Both remain concurrent.
    legs = [threading.Thread(target=room_leg, daemon=True),
            threading.Thread(target=dm_leg, daemon=True)]
    for t in legs:
        t.start()
    deadline = time.time() + _wake_join_s()
    for t in legs:
        t.join(max(0.0, deadline - time.time()))
    return outcome["wake"] or outcome["room"]


#: PIPE_BUF on Linux: a write at or under it is ONE atomic, never-blocking
#: write, which is the whole reason the wake payload is bounded at all.
WAKE_PIPE_BOUND = 4096


def _fit_reason(payload, bound):
    """The wake payload serialized within `bound` BYTES, its reason SAYING SO.

    THE CUT MOVES IN FRONT OF THE SERIALIZER. Slicing `json.dumps(...)` to the
    pipe bound ships a payload torn mid-JSON, and the only reader of that pipe
    (`main`'s `--alert` door) answers a torn payload with "wake payload
    malformed" and exits 1: the alert is lost, the seat is never woken, and
    nothing in what was written says the reason was the thing that overflowed.
    So the reason -- the one field a caller can make arbitrarily long -- is
    bounded BEFORE serialization by `pk.cut_marked`, and the payload that
    reaches the pipe is always whole JSON.

    The widest `keep` that fits is SEARCHED rather than computed, because the
    serialized size of a kept prefix is not a function of its length: JSON
    escaping expands a quote to two bytes and a control character to six, and
    the notice carries the two sizes, so its own width depends on the answer.
    Serialized length is non-decreasing in `keep`, which is what makes the
    bisection sound.

    Returns None when the payload does not fit even with the reason removed
    entirely -- the bound cannot be met by cutting the field this function
    owns, so it REFUSES and lets the caller narrate a lost alert, rather than
    writing bytes the reader will reject.
    """
    from . import pk         # local, as everywhere in this module
    whole = json.dumps(payload).encode("utf-8")
    if len(whole) <= bound:
        return whole
    reason = payload["reason"]

    def at(keep):
        payload["reason"] = pk.cut_marked(reason, keep)
        return json.dumps(payload).encode("utf-8")

    if len(at(0)) > bound:
        payload["reason"] = reason
        return None
    lo, hi = 0, len(reason)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(at(mid)) <= bound:
            lo = mid
        else:
            hi = mid - 1
    return at(lo)


def _alert(seat_name, reason, dm_seat=None, dm_session=None, episode="",
           record_key=None, mode="alert", holder_ident=None):
    """ARM the alert; never perform it. The module's founding invariant —
    "the parent does file reads only and returns in milliseconds; a hook
    that blocks is a hook that wedges every compaction" — applies to the
    alert path too. The hook writes the payload into a PIPE and
    detached-spawns the wake-child with the read end as an inherited
    descriptor: no pathname exists, so there is nothing to TOCTOU, no mode
    to get wrong, no stale artifact to GC, and the capability drains once
    by construction (a replayed exec reads EOF and exits quietly). The
    pipe write is bounded far below the kernel buffer, so it cannot
    block. Same-uid remains the trust ceiling — the chat dir's own threat
    model — what this closes is the documented-flag door where legit
    tooling could be steered into minting a wake from caller text. The
    spiral/capped caller passes no dm_seat ON PURPOSE: that guard exists
    to stop poking the seat. `record_key` rides the payload so the CHILD
    takes the state lock, never the hook. If even the spawn fails there
    is NO inline fallback — an unbounded flock inline is exactly the
    wedge — the loss is narrated on guarded stderr and one O_APPEND
    breadcrumb, both bounded."""
    holder = holder_ident or ""
    if dm_seat and not holder:
        # The hook's PARENT is the seat's own live process; its birth stamp
        # is the kernel evidence the child re-proves (_holder_proves). A
        # detached caller whose parent is init simply gets no holder and
        # falls back to the register proof.
        try:
            from . import orcaadopt
            ppid = os.getppid()
            st = orcaadopt.proc_start(ppid)
            if st:
                holder = "%d:%s" % (ppid, st)
        except Exception:
            holder = ""
    payload = {"display": (seat_name or "?")[:200],
               "reason": _one_line(reason),
               "seat": dm_seat or "", "session": dm_session or "",
               "episode": episode, "record_key": record_key or "",
               "mode": mode, "holder": holder}
    body = _fit_reason(payload, WAKE_PIPE_BOUND)
    if body is None:
        _say("helm seat resume-turn: wake payload cannot fit %d bytes with "
             "its reason removed — ALERT LOST: %s"
             % (WAKE_PIPE_BOUND, reason))
        return False
    try:
        r, w = os.pipe()
    except OSError as e:
        _say("helm seat resume-turn: wake pipe unavailable (%s) — ALERT "
             "LOST: %s" % (e, reason))
        return False
    wrote = False
    try:
        os.set_blocking(w, False)
        os.write(w, body)               # <= PIPE_BUF: atomic, cannot block
        os.close(w)
        wrote = True
        from . import hooks
        spawn_child([hooks.helm_bin(), "seat", "resume-turn",
                     "--alert", "--alert-fd", str(r)], pass_fds=(r,))
        return True
    except Exception as e:              # noqa: BLE001
        _say("helm seat resume-turn: wake child could not be spawned (%s) — "
             "ALERT LOST: %s" % (e, reason))
        try:    # one bounded O_APPEND breadcrumb; no lock, no chat, no state
            lf = os.open(os.path.join(tempfile.gettempdir(),
                                      "helm-wake-lost.log"),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(lf, body + b"\n")
            finally:
                os.close(lf)
        except Exception:
            pass
        return False
    finally:
        try:
            os.close(r)                 # the child holds its own copy
        except OSError:
            pass
        if not wrote:
            try:
                os.close(w)
            except OSError:
                pass
def deliver(seat_name, text, session, adapter=None, pids=None,
            on_submit=None, operation=None, admit=None, attempt=None,
            account_key=None, room=None):
    """Inject `text` into the seat's pane -> (mode, detail).

    Runs inside autocompact's pane transaction rather than a second resolver:
    it re-proves the spawn register under the per-seat lifecycle lock (the
    settle wait is a window in which a pane can be replaced), and `for_send`
    accepts an ORPHANED-but-writable pane, which is the majority state in a
    live fleet — refusing those was what killed the /compact rescue before.

    `operation` REPLACES THE ACTUATION AND KEEPS THE AUTHORIZATION. It is
    called `(adapter, handle, on_typed) -> (state, proof)` once the pane has
    been proven, so a caller whose act is not "type this text" — the vendor
    modal, whose safe option must be re-derived from the pane at the keystroke
    — travels the SAME identity, lock and TOCTOU path as an ordinary send
    instead of growing a second one. `text` is then only what the callbacks
    record. Without it this delivers `text` through `submit` exactly as before.

    `admit(door)` -> harness.Grant is a caller's PREPARED authorization for
    the keystrokes, handed to the adapter's act doors on every branch. An
    `operation` carries its own authorization and is not handed one.
    `attempt`, `account_key` and `room` ride the injection record it writes.

    `pids` routes an ORCA-ADOPTED seat down orcaadopt's equivalent transaction:
    there is no spawn register to re-read and no per-seat lifecycle lock to
    take, so the TOCTOU guard is that the same process still holds the seat at
    send time. Same window, same hazard, the proof that is available."""
    from . import autocompact, harness
    key = seat_name or ("session:" + str(session)[:8])
    generation = {"value": None}
    terminal_failure = {"proof": None}
    pid_tokens = []
    if pids:
        from . import orcaadopt
        pid_tokens = [orcaadopt.ident_token(p) for p in pids]

    # WHAT WAS ACTUALLY TYPED, WHICH IS NOT ALWAYS WHAT THE CALLER ASKED FOR.
    # An `operation` derives its own text at the keystroke — the modal choice
    # re-reads the dialog and can legitimately press a different digit than the
    # one assessed earlier. Provenance is recorded against the PLACED text, so
    # clearing it must compare against the same string or a correct delivery
    # fails its own authority check and reports UNKNOWN. Without an operation
    # this is `text`, unchanged.
    placed_text = {"value": text}

    def typed(handle, placed, ad=None):
        placed_text["value"] = placed
        generation["value"] = _record_injection(
            key, session, handle, placed,
            adapter=getattr(ad or adapter, "name", ""), pids=pid_tokens,
            attempt=attempt, account_key=account_key, room=room)

    def submitted(ad, handle, state, proof):
        gen = generation["value"]
        if gen and state == harness.DELIVERED:
            if not _clear_injection(key, handle, placed_text["value"], gen):
                state = harness.UNKNOWN
                proof = "%s; pane advanced but no terminal authority persisted" % proof
                terminal_failure["proof"] = proof
        elif gen and state == harness.NOT_DELIVERED:
            _observe_injection(key, handle, placed_text["value"], gen)
        if on_submit is not None:
            on_submit(ad, handle, state, proof, gen)
        return state, proof

    if pids:
        from . import orcaadopt
        if not seat_name:
            # The NAMELESS leg: the pane was resolved from this session's own
            # sid (argv --resume), so the send re-proves THAT evidence. A seat
            # name never enters the addressing — send_to_pane would resolve by
            # NAME through the roster, which is exactly the trust a nameless
            # delivery must not extend.
            result = orcaadopt.send_to_sid_pane(
                session, text, expect_pids=pids, adapter=adapter,
                on_typed=typed, on_submit=submitted, admit=admit)
        else:
            result = orcaadopt.send_to_pane(
                seat_name, text, expect_pids=pids, adapter=adapter,
                on_typed=typed, on_submit=submitted, operation=operation,
                admit=admit)
        return ((_mode_for(harness.UNKNOWN, terminal_failure["proof"])
                 if terminal_failure["proof"] else result))
    row = {"seat": seat_name, "registered_session": session}

    def action(ad, handle, detail):
        # SUBMIT, not send. `send` succeeded on "the metaharness accepted the
        # bytes", which is precisely what every pane the owner found with its
        # next instruction typed and unsent also reported. The third mode is
        # the point: `unverified` means the pane could not be read, and it must
        # never be spelled "resumed".
        place = lambda h, placed: typed(h, placed, ad)   # noqa: E731
        if operation is not None:
            state, proof = operation(ad, handle, place)
        else:
            # THE AUTHORIZATION GOES TO THE ADAPTER'S ACT DOORS, and nowhere
            # else: `submit` prepares it, proves the composer, validates the
            # preparation without blocking, and only then types -- and again
            # before the Enter. Passed only when given, so an adapter double
            # with the older signature is untouched.
            state, proof = ad.submit(handle, text, on_typed=place,
                                     **({"admit": admit} if admit is not None
                                        else {}))
        state, proof = submitted(ad, handle, state, proof)
        return _mode_for(state, proof, detail=detail)

    result, err = autocompact._pane_action(row, adapter, action, for_send=True)
    return result if result is not None else ("manual", err)


def _mode_for(state, proof, detail=None):
    """Adapter tri-state -> this leg's (mode, detail).

    DELIVERED alone earns "resumed". NOT_DELIVERED is "manual" — a human has to
    look, because the directive is sitting in a composer nobody submitted.
    UNKNOWN gets its OWN mode: every consumer here branches on `== "resumed"`,
    so an unproven delivery falls to the alerting arm by construction instead
    of being laundered into a success nobody measured.
    """
    from . import harness
    mode = {harness.DELIVERED: "resumed",
            harness.NOT_DELIVERED: "manual"}.get(state, "unverified")
    return mode, (_joined(detail, proof) if detail else proof)


def _native_registered(seat_name, session):
    """(reason, pids, recognized) for a seat outside proxy-backed FAMILIES.

    Native Claude seats are recognized from process or chat-roster evidence,
    never by adding them to `seat.FAMILIES` (the router iterates that proxy
    registry). A blind process/roster read is UNKNOWN and suppresses the proxy
    family-list verdict; a proven absence leaves the genuine-typo message intact.
    """
    from . import orcaadopt, seats
    try:
        procs, unreadable = orcaadopt.claude_processes()
    except Exception as exc:                 # noqa: BLE001 — hook never raises
        procs, unreadable = [], ["process scan: %s" % exc]
    pid, why = orcaadopt.turn_restart_identity(
        seat_name, session, procs=procs, unreadable=unreadable)
    if pid is not None:
        return None, [pid], True
    try:
        roster, roster_failed = seats.roster_checked()
    except Exception:                        # noqa: BLE001
        roster, roster_failed = {}, True
    named = any(p.get("seat") == seat_name for p in procs)
    known = named or seat_name in roster
    if known:
        source = "process and roster" if named and seat_name in roster else \
                 "process" if named else "chat roster"
        return ("native Claude seat %s is known from %s evidence, but its pane "
                "could not be auto-resumed: %s"
                % (seat_name, source, why)), None, True
    if unreadable or roster_failed:
        blind = []
        if unreadable:
            blind.append("%d claude process%s unreadable" %
                         (len(unreadable), "" if len(unreadable) == 1 else "es"))
        if roster_failed:
            blind.append("chat roster unreadable")
        return ("seat %s is outside the proxy-backed registry, and native Claude "
                "identity evidence is UNKNOWN (%s); refusing rather than "
                "printing a proxy-family verdict"
                % (seat_name, "; ".join(blind))), None, True
    return why, None, False


def _roster_report(session):
    """What the ONE identity door answers about `session`, as a REPORT.

    Same door every consumer reads (`actors.self_identity`) so this leg
    keeps no second copy of the question, and the answer carries its own
    provenance: a single bound row is named, and zero or many rows come back as
    that function's own refusal, which NAMES THE COUNT. Nothing here is
    adopted — see the caller's comment for why this leg reports and stops."""
    from . import actors
    from .seats_identity import ROSTERED
    try:
        state, name, why = actors.self_identity(session)
    except Exception as exc:                 # noqa: BLE001 — the hook never raises
        return ("the roster could not be read either (%s: %s), so this is "
                "'cannot look', not 'nothing is bound'"
                % (type(exc).__name__, exc))
    if state == actors.ROSTER_UNKNOWN:
        # A DISTINCT STATE, RENDERED AS ONE. Reporting an unparseable roster
        # with the ZERO sentence — "the roster has never seen this session, run
        # `helm chat join`" — is the one piece of repair advice guaranteed to be
        # wrong for it: it tells the owner to WRITE into the file helm has just
        # failed to read. Keyed on the STATE constant, never on message text.
        return ("and the roster cannot be read either, so whether helm knows "
                "this session is UNKNOWN rather than NO: %s" % why)
    if state == ROSTERED:
        return ("the roster DOES bind this session to seat %r — REPORTED, not "
                "adopted: a roster row is an address, not an identity to act "
                "under, so the pane still has to be proven from process "
                "evidence. Give that pane its name on the launch line "
                "(`helm launch --seat %s`) and this resolves without a scan."
                % (name, name))
    return why or ("the roster was not consulted: this process declares a "
                   "name after all")


def _registered(seat_name, session):
    """The cheap parent-side identity check -> (reason, pids).

    `reason` is None when the child is worth forking, else why it is not.
    `pids` is non-empty only for an ORCA-ADOPTED seat, where it carries the
    holder the decision was made against so the child can re-prove it.

    FILE READS ONLY: the hook budget is 5 seconds for the whole SessionStart
    group and this leg must not spend it probing a metaharness.

    THREE RUNGS, NOT ONE. helm-spawned seats are proven from the spawn
    record; the 53-of-59 roster seats that carry no family name are proven from
    /proc (orcaadopt's doctrine: identity from the PROCESS, never from
    presentation); and a session that declares NO name at all is proven from
    its OWN sid matched against argv `--resume <sid>` in the same /proc scan.
    Stopping at `_seat_family`'s error — which this did until 2026-07-29 —
    meant every claude-family seat answered its own compaction with "unknown
    seat" and then sat idle until the owner typed into its pane. Stopping at
    the MISSING NAME — which this ALSO did until 2026-07-29 — told a human
    "helm cannot address its pane" about a pane that was addressable the whole
    time (measured live 2026-07-29, pid 14632: sid in argv, name nowhere). A
    check that cannot see a case returns UNKNOWN, never a verdict.
    """
    from . import seat
    if not seat_name:
        # NAMELESS IS NOT UNADDRESSABLE — and this must never be "fixed" by
        # loosening seats.own_name(). own_name() answers "may I ACT AS seat
        # X", where a name any other process supplied is impersonation; it
        # stays strict, and no name is minted here. The question HERE is
        # different and narrower: "which pane do I type THIS session's own
        # handoff into". The sid is the session's own (SessionStart payload,
        # self-evidence — no other seat's roster row is trusted), and argv
        # `--resume <sid>` is unique to the resumed pane where a name is
        # inherited by every child the seat spawns. A seat NAME re-enters
        # only in `_display_name`, to caption the alert — cosmetic, no trust.
        from . import orcaadopt
        pid, why = orcaadopt.turn_restart_identity(None, session)
        if pid is None:
            # AND WHAT THE ROSTER SAYS IS SAID, THROUGH THE ONE IDENTITY DOOR.
            # The sentence above was true and read as "helm knows nothing about
            # this session", while helm's own SessionStart join hook had
            # already named this pane (`auto_name` = project + family) and
            # written the row. That omission read as absence to the owner.
            #
            # THE EXPLICIT ANSWER TO THIS BRANCH'S CONTRACT, which is a
            # deliberate killed design and not an oversight: "no other seat's
            # roster row is trusted" stands, and the roster answer here is
            # REPORTED, never ADOPTED. It does not become `seat_name`, it does
            # not reach the child argv, and it is not DM authority — the two
            # arms in tests/test_resumeturn.py that pin those (the nameless
            # resume never adopting the roster name, and the caption being
            # cosmetic) stay green, deliberately. Two facts make adoption the
            # wrong cure rather than merely a bold one: `turn_restart_identity`
            # with a NAME matches processes by `p["seat"]`, which is read from
            # the pane's environ — the very thing this pane lacks — so a name
            # buys no pane resolution here at all; and the roster is
            # hook-written, corruptible state, and adopting it for an ACT is
            # the cross-seat contamination class. Promoting ROSTERED to authority
            # on this leg is therefore a contract change that owes a
            # cross-family ruling, not a cure round.
            return ("this session declares no seat name (HELM_CHAT_NAME) and "
                    "its pane could not be resolved from process evidence "
                    "either: %s; %s" % (why, _roster_report(session))), None
        return None, [pid]
    family, err = seat._seat_family(seat_name)
    if err:
        why, pids, recognized = _native_registered(seat_name, session)
        if recognized:
            return why, pids
        return "%s; and it is not an adopted pane either; no native Claude " \
               "seat evidence matched (%s)" % (err, why), None
    rec = seat._spawn_record(seat._instance_dir(family, seat_name))
    if not rec or rec.get("seat") != seat_name:
        return ("no authoritative spawn handle; `helm seat spawn`/`resume` "
                "registers one"), None
    if rec.get("harness") == "headless":
        return "seat is registered headless — no pane input channel exists", None
    if not rec.get("session"):
        return "spawn register has no bound session identity", None
    if session and rec.get("session") != session:
        # THE PREFIX IS WHERE THESE AGREE. Session ids here are UUIDs whose
        # first block repeats, so clipping both sides to 8 renders a real
        # mismatch as two identical strings and the message reads as a
        # contradiction of itself. Clip enough to stay a fragment and enough
        # to DISCRIMINATE, which is the only job this sentence has.
        return ("spawn register names session %s but this compaction is %s"
                % (str(rec.get("session"))[:18], str(session)[:18])), None
    return None, None


# ---------------------------------------------------------------------------
# the hook — SessionStart. The RESUME arms on source == compact only; the
# injector's suppression reset fires on every source that is not known to
# preserve context (see hook()).
# ---------------------------------------------------------------------------

# DELIBERATELY EMPTY. Every SessionStart source drops the injector's
# suppression, because NO source proves the seat still holds what the seen-file
# says it holds.
#
# `resume` was the obvious candidate and it is wrong: it proves a transcript can
# be OPENED, not that the retained context equals the seen-file's claim.
# The live case — the integrator resumed a seat onto a session
# `cv prune` had stripped of 1,260 turns and ~89,797 tokens; that resume
# returned STRICTLY LESS context than the session it resumed, and it escaped a
# mute seat only because prune mints a NEW session id so there was no seen-file
# to inherit. Prune-then-resume is this fleet's DEFAULT recovery path, not an
# edge case.
#
# THE MEMBERSHIP MATTERS LESS THAN THE DEFAULT. Empty makes the predicate "reset
# unless PROVEN preserving", so the next source nobody has enumerated — and
# there will be one — inherits the cheap failure instead of the silent one:
#   wrong to preserve -> a MUTE seat holding no premises, told nothing, unable
#                        to ask for what it does not know is missing.
#   wrong to reset    -> ONE re-delivery at session start, ~1.1kB, visible in
#                        the fire-ledger.
# Adding a source here therefore needs proof of CONTEXT EQUALITY, not proof that
# a transcript exists. Meld e:1785817308, unanimous (convener/reviewer/integrator).
CONTEXT_PRESERVING_SOURCES = ()


def _repair_key(seat_name, session):
    """The resume-state key a DEAF-IN-EFFECT repair of this seat's session is
    accounted under: its debounce, its cap, its act ledger, and the
    `account_key` of every injection it types."""
    return "deaf:%s:%s" % (seat_name or "?", session or "?")


def wake_undelivered(seat_name, session, waited=None, dry=False, att=None):
    """Make a DEAF-IN-EFFECT pane take a turn -> {"action", "detail"}.

    NOT THE COMPACTION LEG, AND THE DISTINCTION IS WHY THIS EXISTS. `hook`
    answers a context LOSS: it gates on the SessionStart source, forgets the
    injector ledger for the session, and composes a post-compaction resume. A
    seat whose addressed rows are simply never consumed has lost no context
    and is not compacting — its wake path is live, a seat is home, and the
    delivery still did not become a turn. What it needs is a turn and one
    sentence saying why. Only the three general pieces are shared: the
    parent-side pane proof, the child argv, and the fork.

    RE-ARMING THE BEACON WOULD FIX NOTHING, which is the whole reason a
    fallback exists at all. The waiter is not what failed, so the repair has
    to reach the PANE.

    ITS OWN KEY NAMESPACE, deliberately. The debounce and the per-window cap
    are general and are reused; the population is not. Sharing a key with the
    compaction leg would let one seat's resumes spend the other's cap, and a
    spiral verdict — which is a statement about compactions — would silence a
    seat that never compacted.
    """
    from . import home, pk
    key = _repair_key(seat_name, session)
    action, detail = _decide(_peek(key), time.time())
    if action != "resume":
        # An "unknown" decision is about a store no stamp can be written to,
        # so none is attempted and none is blamed on a lock.
        if action != "unknown" and not dry and _record_nb(
                key, session or "", action, detail, False) is None:
            detail += ("; the refusal stamp was skipped because the resume "
                       "state was locked or could not be read, so this "
                       "decision is not in the debounce memory")
        return {"action": action, "detail": detail}
    why, pids = _registered(seat_name, session)
    if why:
        return {"action": "alert", "detail": why}
    # THE REASON TO TYPE IS RE-DERIVED HERE, AT THE ACTUATOR, and never taken
    # from the census that decided to call. Between the verdict and this line
    # the seat may have drained everything: pane identity would still be
    # valid, the cooldown would still permit, and the seat would be told to
    # catch up on nothing. A VALID PANE IS NOT A STILL-OWED REASON.
    waited, owed_why = _still_owed(seat_name, session, waited)
    if waited is None:
        return {"action": "drained", "detail": owed_why}
    # AND THE PAUSE THAT GOVERNS EVERY OTHER DELIVERY GOVERNS THIS ONE. A seat
    # behind a verified credential wall, or one whose proxy state is UNKNOWN,
    # is deliberately holding its addressed rows until it is HEALTHY; typing
    # into it through a side door is the same act the pause exists to refuse,
    # minus the authority.
    paused = _delivery_paused(seat_name, session)
    if paused:
        return {"action": "paused", "detail": (
            "delivery to %s is paused (%s), and a pane nudge is a delivery "
            "with the pause left out — the backlog stays owed and the alarm "
            "stands" % (seat_name, paused))}
    waited_s = ("%.0f" % waited) if isinstance(waited, (int, float)) else "?"
    text = ("[helm] rows addressed to you have been waiting %ss unread and no "
            "turn consumed them — your wake path is live, so this is a pane "
            "nudge rather than a beacon repair. Catch up with `helm chat "
            "read`, then answer what is owed." % waited_s)
    if dry:
        return {"action": "would-wake", "detail": text, "seat": seat_name,
                "pids": pids}
    # THE EPISODE IS OPENED HERE, LAST, AND THE LAUNCH IS BOUND TO IT OR
    # THERE IS NO LAUNCH. Every refusal this function can make has been made
    # above, so an installed attempt is one that WILL be run. `att` is the
    # attendance row the alarm was raised on; a caller with none (the manual
    # --nudge on a seat that is not DEAF-IN-EFFECT) launches an unbound child,
    # which is harmless only because `settle_repair` refuses to let a child
    # with no attempt close an episode that has one. A REQUIRED install that
    # fails -- the register moved, the lock was lost, the write failed -- is
    # a refusal, not a downgrade: the earlier shape launched anyway with no
    # attempt, and that child then settled through the legacy branch against
    # whatever episode the NEXT pass installed.
    attempt = None
    if att is not None:
        from . import beacons as _beacons
        attempt = _beacons.install_repair(seat_name, att, time.time())
        if attempt is None:
            return {"action": "alert", "detail": (
                "the repair episode for %s could not be installed (the "
                "register moved under this pass, or its lock or write "
                "failed) -- NOT launching a child that no episode would "
                "own; the next pass re-derives it" % seat_name)}
    tp = os.path.join(home.helm_home(), home.GLOBAL, ".state", "resume",
                      pk.slug(key) + ".txt")
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    pk.atomic_write(tp, text)
    try:
        owed = (att or {}).get("undrained_row") if isinstance(att, dict) else None
        owed_room = owed[0] if isinstance(owed, (list, tuple)) and owed else None
        spawn_child(_child_argv(seat_name, session, tp, settle_s(), pids=pids,
                                record_key=key, attempt=attempt,
                                owed_room=owed_room))
    except Exception as e:                   # noqa: BLE001
        return {"action": "alert", "attempt": attempt,
                "detail": "wake child could not be spawned (%s)" % e}
    # NONBLOCKING, BECAUSE OF WHERE THIS RUNS. `escalate` calls this
    # synchronously while holding the global `.escalate.lock` and BEFORE any
    # room or phone delivery, so a blocking take of the resume-state lock puts
    # a wedged pane repair in front of every other seat's alarm in the batch
    # and every later escalation pass. The compaction hook already chose
    # nonblocking here for the same reason. The outcome is not dropped: the
    # child writes the same key (`--record-key`), and a skip is SAID rather
    # than swallowed so the caller can surface it.
    stamped = _record_nb(key, session or "", "resume", "", True,
                         attempt=attempt, seat=seat_name)
    note = ("" if stamped is not None else
            " (the spawn stamp was skipped because the resume state was "
            "locked or could not be read; the child records the same key)")
    return {"action": "wake", "detail": text + note, "attempt": attempt,
            "stamped": stamped is not None}


def _rearm_key(seat_name, session):
    """The resume-state key a DEAF seat's RE-ARM nudge is accounted under.

    ITS OWN NAMESPACE, for the reason `_repair_key` has one: a re-arm nudge
    and a DEAF-IN-EFFECT turn nudge answer different questions, and the
    child picks its authorization by this prefix. The bounds are the same
    `_decide` bounds (debounce, spiral, per-hour cap), applied per key."""
    return "rearm:%s:%s" % (seat_name or "?", session or "?")


def rearm_text(seat_name, owes=""):
    """The one sentence a DEAF seat's pane is given: what it owes, then
    re-arm the beacon with the exact unfiltered Monitor recipe
    `seats_advice.beacon_monitor` renders everywhere else, so the nudge and
    every other instruction agree."""
    from .seats_advice import beacon_monitor
    return ("[helm] %s, and your inbox beacon is not running, so helm cannot "
            "wake you. Re-arm it now: %s"
            % (owes or "you owe work", beacon_monitor(seat_name)))


def _rearm_owes(seat_name, session, row=None):
    """(True | False | None, what it owes) -- is this DEAF seat SILENT WHILE
    OWING? Canon `ping-silent-while-owing-never-wake-clean-idle`: a seat is
    woken only while it holds an obligation, and a clean idle seat is never
    woken, deaf or not. A typed prompt starts a paid turn.

    Two readers the fleet already has, never a new one: the pending
    ADDRESSED row, through `_still_owed` (the same reader, the same coverage
    rule, the DEAF-IN-EFFECT path uses), and owed dispatch work, through the
    stop guard's `_beacon_obligation` (`dispatches.owed`, both directions).
    TRUE when either reader finds an obligation. FALSE only when both
    measured an empty result. NONE when either could not be read and neither
    found one; NONE types nothing."""
    ev = {}
    waited, why = _still_owed(seat_name, session, _NO_CACHED_WAIT, row=row,
                              evidence=ev)
    if waited is not None and waited != _NO_CACHED_WAIT:
        n = max(1, len(ev.get("seen") or ()))
        return True, ("a row addressed to you has waited %.0fs unread "
                      "(owes at least %d row%s)"
                      % (waited, n, "s"[:n != 1]))
    try:
        from .seats_stop_signals import _beacon_obligation
        ob = _beacon_obligation(seat_name)
    except Exception as exc:                 # noqa: BLE001 — unknown, types nothing
        ob, why = None, "%s: %s" % (exc.__class__.__name__, exc)
    if ob:
        return True, "you owe open dispatch work (`%s`)" % OBLIGATIONS_CMD
    if waited is None and ob is False:
        return False, ("%s owes nothing: no addressed row is pending and no "
                       "dispatch work is owed" % seat_name)
    if ob is None:
        return None, (why or "the dispatch obligation ledger could not be "
                      "read")
    return None, (why or "the addressed-row census could not see the whole "
                  "room estate, so no pending row was found and none was "
                  "ruled out; no dispatch work is owed")


def _still_deaf(seat_name):
    """(True | False | None, why) -- does `seat_name` STILL have no live
    beacon, asked at the actuator and never taken from the census?

    TRUE is the licence to type. FALSE means a waiter is PROVEN live now (the
    seat re-armed between the verdict and this act), so there is nothing to
    say. NONE is a probe that could not tell, and it types nothing."""
    try:
        from . import seats
        pids, trouble = seats.beacon_procs(seat_name, strict=True)
    except Exception as exc:                 # noqa: BLE001 — unknown, types nothing
        return None, "%s: %s" % (exc.__class__.__name__, exc)
    if trouble:
        return None, _one_line(trouble)
    if pids:
        return False, ("%s re-armed its beacon (pid %s) before this nudge, "
                       "so there is nothing to tell it" % (seat_name, pids[0]))
    return True, ""


def _own_project():
    """The project this process runs for: the checkout its cwd is in, by the
    same resolver a seat's project comes from. The beacons timer runs from
    the helm checkout (its unit's WorkingDirectory)."""
    from .seats_identity import _git_project, safe_cwd
    cwd = safe_cwd()
    return _git_project(cwd) if cwd else None


def _seat_project(row):
    """A roster row's project: its recorded checkout, resolved to the
    registered helm project (a lane worktree resolves to its repo). None
    when the row names no checkout or it cannot be resolved."""
    from .seats_identity import _git_project
    cwd = (row or {}).get("cwd")
    return _git_project(cwd) if cwd and os.path.isdir(cwd) else None


def _auto_scope(seat_name, session, row):
    """("", ("", "")) when an AUTOMATIC keystroke may reach this seat,
    else (kind, (subject, verdict)): the report reads "<subject>: <what it
    owes>; <verdict>". The ruling on task/2925:

    PROJECT SCOPE. The timer types only into seats of the project it runs
    for. Another project's seats and runtime belong to that project's lead.
    A seat whose checkout does not resolve to a project is not ours to type
    into; the report then names the roster's own project label.

    FAMILY SCOPE. Only a VERIFIED native claude seat. A proxy or codex
    family is billed per turn, so a wake is a paid turn the owner did not
    order.

    Every failing clause is reported, so a seat that is both foreign and
    paid says both. The kind names the project first."""
    from .seats_runtime import runtime_for_session
    row = row if isinstance(row, dict) else {}
    mine, theirs = _own_project(), _seat_project(row)
    foreign = not mine or theirs != mine
    runtime, verified = runtime_for_session(row, session)
    runtime = runtime if isinstance(runtime, dict) else {}
    paid = not (verified and runtime.get("family") == "claude"
                and runtime.get("backend") == "native")
    if not (foreign or paid):
        return "", ("", "")
    subject, verdict = [], []
    if foreign:
        name = theirs or ("%s (roster label; its checkout does not resolve)"
                          % row["project"] if row.get("project")
                          else "UNKNOWN (no resolvable checkout)")
        subject.append("belongs to project %s, not %s"
                       % (name, mine or "UNKNOWN"))
        verdict.append("another project's seat")
    if paid:
        subject.append("runs %s/%s%s" % (runtime.get("family") or "?",
                                         runtime.get("backend") or "?",
                                         "" if verified else " (unverified)"))
        verdict.append("wake is a paid turn")
    return ("foreign-project" if foreign else "paid-family"), (
        "%s %s" % (seat_name, " and ".join(subject)),
        "%s; not auto-nudged" % "; ".join(verdict))


def rearm_deaf(seat_name, session, dry=False, att=None):
    """Ask a DEAF seat's pane to re-arm its beacon -> {"action", "detail"}.

    THE SAME PATH AS `wake_undelivered`, WITH A DIFFERENT REASON TO TYPE. A
    DEAF seat whose pane DECLARES the seat is alive and has no waiter: its
    waiter died and nothing re-armed it, so helm's only wake path to it is
    gone. The only party that can arm a beacon is the pane itself, so this
    types one sentence asking it to. Everything else is reused: the per-key
    debounce, spiral and per-hour cap (`_decide`), the parent-side pane
    proof (`_registered`), the delivery pause, the episode latch
    (`beacons.install_repair`, keyed by the DEAF spell's `since`), the
    detached child and its act doors.

    The caller decides that the pane is a DECLARED agent (`agent` True on the
    census row); this function re-derives only what can change between the
    census and the act: the pause, and whether the seat is still deaf.

    AND IT TYPES ONLY WHILE THE SEAT OWES (`_rearm_owes`). That is asked
    FIRST, before the rate store is touched, so a clean idle DEAF seat
    spends nothing on any pass: action "idle", which the caller neither
    records nor prints. The census still reports the seat DEAF."""
    from . import home, pk
    owes, owes_why = _rearm_owes(seat_name, session)
    if owes is False:
        return {"action": "idle", "detail": owes_why}
    if owes is None:
        return {"action": "withheld", "detail": (
            "whether %s owes anything could not be read (%s), so the pane "
            "was not typed into" % (seat_name, owes_why))}
    # OWING, AND STILL NOT OURS TO WAKE: reported with what it owes, never
    # typed into, and asked before the rate store so it spends nothing.
    _key0, srow, _rows0, serr = _roster_row(seat_name)
    if serr:
        return {"action": "withheld", "detail": serr}
    kind, (subject, verdict) = _auto_scope(seat_name, session, srow)
    if kind:
        owed = owes_why.split("(", 1)[-1].rstrip(")") \
            if "(owes" in owes_why else owes_why
        return {"action": kind, "detail": "%s: %s; %s" % (
            subject, owed, verdict)}
    key = _rearm_key(seat_name, session)
    action, detail = _decide(_peek(key), time.time())
    if action != "resume":
        if action != "unknown" and not dry and _record_nb(
                key, session or "", action, detail, False) is None:
            detail += ("; the refusal stamp was skipped because the resume "
                       "state was locked or could not be read, so this "
                       "decision is not in the debounce memory")
        return {"action": action, "detail": detail}
    why, pids = _registered(seat_name, session)
    if why:
        return {"action": "alert", "detail": why}
    deaf, deaf_why = _still_deaf(seat_name)
    if deaf is False:
        return {"action": "drained", "detail": deaf_why}
    if deaf is None:
        return {"action": "withheld", "detail": (
            "whether %s still has no beacon could not be read (%s), so the "
            "pane was not typed into" % (seat_name, deaf_why))}
    paused = _delivery_paused(seat_name, session)
    if paused:
        return {"action": "paused", "detail": (
            "delivery to %s is paused (%s), and a pane nudge is a delivery "
            "with the pause left out" % (seat_name, paused))}
    text = rearm_text(seat_name, owes_why)
    if dry:
        return {"action": "would-wake", "detail": text, "seat": seat_name,
                "pids": pids}
    attempt = None
    if att is not None:
        from . import beacons as _beacons
        attempt = _beacons.install_repair(seat_name, att, time.time(),
                                          kind="rearm")
        if attempt is None:
            return {"action": "alert", "detail": (
                "the re-arm episode for %s could not be installed (the "
                "register moved under this pass, or its lock or write "
                "failed) -- NOT launching a child that no episode would "
                "own; the next pass re-derives it" % seat_name)}
    tp = os.path.join(home.helm_home(), home.GLOBAL, ".state", "resume",
                      pk.slug(key) + ".txt")
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    pk.atomic_write(tp, text)
    try:
        spawn_child(_child_argv(seat_name, session, tp, settle_s(), pids=pids,
                                record_key=key, attempt=attempt))
    except Exception as e:                   # noqa: BLE001
        return {"action": "alert", "attempt": attempt,
                "detail": "re-arm child could not be spawned (%s)" % e}
    stamped = _record_nb(key, session or "", "resume", "", True,
                         attempt=attempt, seat=seat_name)
    note = ("" if stamped is not None else
            " (the spawn stamp was skipped because the resume state was "
            "locked or could not be read; the child records the same key)")
    return {"action": "wake", "detail": text + note, "attempt": attempt,
            "stamped": stamped is not None}


def _still_owed(seat_name, session, waited, room=None, row=None, scope=None,
                evidence=None):
    """(the wait to quote, why-not) — IS A ROW STILL OWED, RIGHT NOW?

    Asked through the same reader and the same eligibility as the verdict, so
    a nudge and the alarm that ordered it are answering one question. An
    UNREADABLE census keeps the cached wait rather than typing on a guess or
    refusing a real repair: the instrument failed, which says nothing about
    the backlog, and the alarm that got us here was itself readable.

    AND AN EMPTY SAMPLE IS NOT A DRAINED BACKLOG. Taking the census's
    three-tuple and dropping the EVIDENCE turns a wait of None into "nothing is
    waiting any more" whatever the sample managed to cover — and the census is
    a BOUNDED ROTATING SAMPLE that can simply not reach the seat's rooms. The
    caller records that as DRAINED, which settles the repair against the spell
    that earned it, and the backlog the alarm was raised about never moves:
    alarm, false recovery, alarm, for as long as the rotation keeps missing.
    `drain_proven` refuses this exact inference one module over, so coverage is
    asked here too, and an UNKNOWN keeps the cached wait rather than announcing
    a recovery.

    `row` and `scope` are a caller's STRICT roster read of this seat; given
    them, the census decides eligibility against that read. `evidence`, a
    dict, receives the census evidence INCLUDING the physical occurrence of
    the oldest row, which is what an act on this answer is validated
    against."""
    try:
        from . import beacons, consumption, seats as _seats
        if row is None:
            row = (_seats.roster() or {}).get(seat_name) or {}
        # PINNED TO THE OWED ROOM WHEN THE CALLER NAMES ONE. `undrained` scans
        # OUT FROM the room it is given, so the named room is always in the
        # sample and a rotation elsewhere cannot starve the one row this
        # question is about. Without a name, the seat's home room as before.
        extra = ({"scope": scope, "occurrence": True}
                 if isinstance(evidence, dict) else {})
        fresh, unread, ev = beacons.undrained(
            seat_name, session=session, room=room or row.get("home_room"),
            **extra)
        if isinstance(evidence, dict):
            evidence.update(ev or {})
    except Exception as e:                   # noqa: BLE001
        return waited, ("the backlog could not be re-read (%s)"
                        % e.__class__.__name__)
    if fresh is not None:
        # AN AGE FROM A ROOM THIS PASS COULD NOT READ WHOLE IS NOT POSITIVE
        # EVIDENCE. The wake cursor is what SUPPRESSES rows a wake already
        # crossed, so losing it does not hide rows -- it UN-HIDES consumed
        # ones, and the oldest of those carries an age that reads as an
        # overdue row and types into a pane about an obligation already met.
        # Only the room the age was MEASURED IN matters here: a capped estate
        # elsewhere does not make a row found in a fully-read room unreal.
        where = (ev or {}).get("oldest") or (None, None)
        trouble = ((ev or {}).get("bounded") or {}).get(where[0])
        if trouble:
            return waited, ("the oldest row was found in %s, which this pass "
                            "could not read whole (%s: %s) — an age measured "
                            "there is not proof a row is owed"
                            % (where[0], trouble[0], trouble[1] or "no detail"))
        return fresh, ""
    if unread:
        return waited, ""
    covered = beacons.sample_is_complete(ev)
    if covered.unknown:
        return waited, ""
    # THE DERIVATION IS THE DISCLOSURE. The emptiness is only a drain BECAUSE
    # the sample could see, and the reading carries that grounds forward
    # rather than restating it as a bare sentence a reader has to trust.
    drained = covered.derive(
        "did %s drain between the verdict and this repair" % seat_name,
        True, consumption.PROVEN, covered.evidence,
        despite="the rooms this pass read are what the emptiness is a fact "
                "about, and that is the same evidence this reading already "
                "carries")
    return None, ("nothing addressed to %s is waiting any more, so the pane "
                  "was not typed into — the seat drained between the verdict "
                  "and this repair (%s)" % (seat_name, drained.why()))


#: Handed to `_still_owed` as the "cached wait" by a caller that HAS no cached
#: wait. It must not be None: None is that function's answer for a PROVEN
#: drain, so passing None as the prior would make an UNREADABLE census
#: indistinguishable from a drained one and cancel a real repair on a failed
#: instrument (task/2463 finding 4, the second face).
_NO_CACHED_WAIT = -1.0


def _outcome_of(kind):
    """The recorded outcome for a refused preparation or act door, from its
    KIND: "unknown" and "moved" are recorded as `withheld` -- a deferral, not
    a settlement -- so the spell stays owed and is asked again."""
    kind = getattr(kind, "kind", kind) or "unknown"
    return kind if kind in ("paused", "drained") else "withheld"


#: How many observations one preparation takes before it answers UNKNOWN when
#: each is found incoherent -- a cursor commit or a roster rewrite landing
#: inside the observation it was taken from.
PREPARE_OBSERVATIONS = 3


def _roster_row(seat_name):
    """(key, row, rows, err) from ONE STRICT roster read.

    An unreadable, malformed or ambiguous roster is an error, never an empty
    row: a preparation that authorizes a keystroke, and a charge that decides
    whether an attempt may be accounted, both refuse on it."""
    from .seats_common import canonical_seat
    from .seats_roster import roster_checked
    rows, failed = roster_checked()
    if failed:
        return None, None, None, "the roster could not be read"
    key, err = canonical_seat(seat_name, rows)
    if err:
        return None, None, None, "the roster row for %s is ambiguous" % seat_name
    row = rows.get(key) if key is not None else None
    return key, (row if isinstance(row, dict) else None), rows, ""


def _binding(key, row, session):
    """The PROJECTION of a seat's roster row that a prepared keystroke depends
    on: its canonical key and incarnation, the session binding, the runtime
    this session was verified to run, and the scope fields eligibility reads.

    A projection and not the bytes, because the roster is rewritten by every
    other seat's join and by this repair's own episode writes; none of those
    changes what a keystroke for this session is authorized by."""
    row = row or {}
    sid = str(session or "")
    exact = row.get("runtime_sessions")
    return json.dumps({
        "key": key, "incarnation": row.get("incarnation"),
        "session": row.get("session"), "sessions": row.get("sessions"),
        "runtime_session": (exact.get(sid) if isinstance(exact, dict)
                            else exact),
        "runtime": row.get("runtime"),
        "runtime_verified": row.get("runtime_verified"),
        "home_room": row.get("home_room"), "mute": row.get("mute"),
        "joined": row.get("joined")}, sort_keys=True, default=str)


def _rename_in_flight():
    from .seats_rename import rename_journal_path
    return os.path.exists(rename_journal_path())


def _pause_verdict(seat_name, session, row):
    """(the pause as a sentence or "", why it could not be read or "").

    The SAME authority `deliver` obeys, from the runtime this exact session
    was verified to run, read without the delivery state guard because a
    nudge moves no cursor. An observer failure is the substrate's own UNKNOWN
    pause and holds.

    STRICTER THAN DELIVERY ABOUT ABSENCE, because this authorizes a keystroke
    rather than a row. A seat with no proxy family has no wall and is clear. A
    seat WITH one is clear only on a RECOGNISED record for THAT family: a
    family that could not be resolved, a snapshot with no record for it (the
    watcher has measured some other family, or nothing yet), and a record
    whose state or dark latch is not one the watcher writes are each UNKNOWN,
    and UNKNOWN authorizes nothing."""
    from . import proxywatch, seat as seatmod
    from .seats_runtime import runtime_for_session
    runtime, verified = runtime_for_session(row or {}, session)
    try:
        family, ferr = seatmod.family_for(str(seat_name or ""), runtime,
                                          verified)
    except Exception as exc:                 # noqa: BLE001
        return "", "seat family unreadable: %s" % exc.__class__.__name__
    if ferr or not family:
        return "", ""
    state, err = proxywatch._read_delivery_state()
    pause, why = proxywatch.delivery_pause(
        seat_name, state=None if err else state, runtime=runtime,
        runtime_verified=verified)
    if pause:
        return "state %s" % (pause.get("state") or "UNKNOWN"), ""
    record = ((proxywatch.upstream_records(state)[0] or {}).get(family)
              if isinstance(state, dict) and state else None)
    known = proxywatch._UPSTREAM_DARK | proxywatch._UPSTREAM_AGGREGATE | {
        "HEALTHY", "UNKNOWN", proxywatch._PROXY_COOLDOWN}
    recognised = isinstance(record, dict) and record.get("state") in known \
        and (record.get("dark") is None or type(record.get("dark")) is bool)
    if why or not recognised:
        return "", ("no recognised %s pause record (%s)" % (
            family, why or ("the watcher has recorded no %s verdict yet"
                            % family if not isinstance(record, dict)
                            else "an unrecognised %s record" % family)))
    return "", ""


#: The door names that REPEAT an act already taken for the same attempt: a
#: whole-delivery retry and every recovery Enter. After an act accounted as
#: obsolete-authorization, each of them refuses for that attempt.
REPEAT_DOORS = ("retry", "recovery", "recovery-attempt")

#: The door names that TYPE OR PRESS A KEY, as `harness._through_door` names
#: them. Only these record an act intent; a preparation asked by any other name
#: ("settle", "retry", "recovery") decides whether to enter the pane
#: transaction and acts on nothing itself.
ACT_DOORS = ("placement", "enter", "recovery-attempt")


def _prepare_due(seat_name, session, room=None, door="", attempt=None,
                 key=None, observe=None):
    """harness.Grant -- THE PREPARATION of one DEAF-IN-EFFECT keystroke.

    Blocking, and allowed to be: a strict roster read, the pause verdict, and
    the consumption census pinned to the owed room, under the census's own
    locks. It authorizes only on POSITIVE evidence -- a row observed owed in a
    room read whole -- and it binds, DURING that observation, everything the
    authorization rests on: the seat's roster projection, and the physical
    occurrence of the row with the two cursor values it was judged pending
    under. The grant's `capture()` is the act door's FINAL CAPTURE of exactly
    those, and its `account()` records how an act taken on it came out.

    `attempt` is the attempt the act is FOR, and it is part of the
    authorization: an attempt its episode no longer names, one whose episode
    settled, or one past its horizon is refused here and again at the final
    capture. `key` is the resume-state entry its acts are accounted under; a
    door that repeats an act (REPEAT_DOORS) refuses for an attempt whose act
    was accounted obsolete-authorization, and one whose act has an INTENT on
    record with no receipt -- the act may have run in a process that died
    before accounting it, or may be running now.

    An ACT door (ACT_DOORS) for a named attempt records that intent before it
    returns a grant, outside the act window, and a grant whose intent could
    not be written authorizes nothing: an act that could not be reconciled
    after a crash is not taken. The accounting write replaces the intent with
    the receipt; a door that acts on nothing withdraws it.

    An observation found incoherent -- a cursor commit inside the room read,
    a roster rewrite that moved the projection inside the census -- is
    discarded and taken again, PREPARE_OBSERVATIONS times, then UNKNOWN.

    Refusal kinds: "paused" (the pause holds), "drained" (a census that could
    see found nothing owed), "stale" (the attempt may not act), "obsolete"
    (a repeat of an act accounted obsolete-authorization), and "unknown"
    (anything that could not be observed -- it keeps the obligation and
    authorizes nothing, and it is never spelled as a drain).

    `observe` replaces the ONE observation and keeps everything around it:
    the repeat refusal, the intent, the incoherence retries. The re-arm leg
    passes `_prepare_rearm_once`, whose licence is "still no beacon" rather
    than "a row is still owed"."""
    from . import harness
    observe = observe or _prepare_once
    if key and door in REPEAT_DOORS:
        spent = _obsolete_act(key, attempt)
        if spent and spent.get("unreadable"):
            return harness.Grant(False, (
                "whether an act for attempt %s was accounted "
                "OBSOLETE-AUTHORIZATION could not be read (%s), so this %s is "
                "WITHHELD; the obligation stands"
                % (attempt or "-", spent.get("what") or "no detail", door)),
                "unknown")
        if spent:
            return harness.Grant(False, (
                "the %s act for attempt %s was accounted OBSOLETE-AUTHORIZATION "
                "(%s), so this %s does not repeat it; the obligation stands "
                "and the next pass mints a fresh attempt"
                % (spent.get("door") or "earlier", attempt or "-",
                   spent.get("what") or "no detail", door)), "obsolete")
    nonce = None
    if key and attempt is not None and door in ACT_DOORS:
        nonce = hashlib.sha256(("%s\0%s\0%d\0%d" % (
            key, attempt, os.getpid(), time.time_ns())).encode(
                "utf-8")).hexdigest()[:16]
    for _n in range(PREPARE_OBSERVATIONS):
        grant = observe(seat_name, session, room, attempt, key, nonce)
        if grant is None:
            continue
        if grant.ok and nonce:
            why = _record_intent(key, attempt, door, nonce)
            if why:
                return harness.Grant(False, (
                    "the %s act for attempt %s could not record its intent "
                    "(%s), so an act that a crash would leave unreconcilable "
                    "is not taken; the obligation stands"
                    % (door, attempt, why)), "unknown")
            grant._abandon = (lambda k=key, a=attempt, n=nonce:
                              _withdraw_intent(k, a, n))
        return grant
    return harness.Grant(False, (
        "whether rows are still owed to %s could not be observed coherently "
        "in %d tries (the seat's cursors or roster binding kept moving under "
        "the census), so this keystroke is WITHHELD; the repair is not "
        "cancelled and the next pass asks again"
        % (seat_name, PREPARE_OBSERVATIONS)), "unknown")


def _attempt_refusal(attempt, row, now):
    """(why the attempt may not act now or "", its horizon or None), judged
    from one roster row already read. No attempt named is the manual nudge:
    it has no episode to answer to and no horizon."""
    if attempt is None:
        return "", None
    standing, born = _standing_of(row, attempt)
    why = _launch_verdict(attempt, standing, born, now)
    return why, (born + _debounce_s() if standing == "current" else None)


def _prepare_once(seat_name, session, room, attempt=None, key=None,
                  nonce=None):
    """One observation for `_prepare_due` -> a Grant, or None when the
    observation was incoherent and must be taken again."""
    from . import harness
    from .seats_address import seat_scope

    def unknown(why):
        return harness.Grant(False, (
            "whether rows are still owed to %s could not be re-read (%s), so "
            "this keystroke is WITHHELD; the repair is not cancelled and the "
            "next pass asks again" % (seat_name, why)), "unknown")
    try:
        key0, row, rows, err = _roster_row(seat_name)
        if err or row is None:
            return unknown(err or "%s is not in the roster" % seat_name)
        bound = _binding(key0, row, session)
        stale, _horizon = _attempt_refusal(attempt, row, time.time())
        if stale:
            return harness.Grant(False, stale, "stale")
        if _rename_in_flight():
            return unknown("a seat rename is in flight")
        paused, perr = _pause_verdict(seat_name, session, row)
        if perr:
            return unknown("the pause verdict: %s" % perr)
        if paused:
            return harness.Grant(False, (
                "delivery to %s is paused (%s) — a pane nudge is a delivery "
                "with the pause left out" % (seat_name, paused)), "paused")
        ev = {}
        waited, why = _still_owed(seat_name, session, _NO_CACHED_WAIT,
                                  room=room, row=row,
                                  scope=seat_scope(seat_name, rows),
                                  evidence=ev)
    except Exception as exc:                 # noqa: BLE001 — a preparation
        return unknown(exc.__class__.__name__)  # that failed proves nothing
    if waited is None:
        return harness.Grant(False, (
            "nothing addressed to %s is waiting any more — it drained while "
            "this repair was settling" % seat_name), "drained")
    if waited is _NO_CACHED_WAIT:
        return unknown(why or "the backlog census failed")
    occ = ev.get("occurrence")
    if not isinstance(occ, dict):
        return unknown("the owed row's physical occurrence was not captured, "
                       "so there is nothing to validate the act against")
    if not occ.get("coherent"):
        return None
    if occ.get("replay"):
        return unknown("the owed room was read in replay mode, so its byte "
                       "positions are not ones the cursor vouched for")
    key2, row2, _rows2, err2 = _roster_row(seat_name)
    if err2 or row2 is None:
        return unknown(err2 or "%s left the roster" % seat_name)
    if _binding(key2, row2, session) != bound:
        return None

    def capture():
        return _capture(seat_name, session, bound, occ, attempt)

    def account(receipt):
        return _account_act(key, session, seat_name, attempt, receipt, nonce)
    return harness.Grant(True, still=capture,
                         account=account if key else None)


def _identities(paths):
    """{label: identity} of each dependency FILE -- (inode, size, mtime,
    ctime) in nanoseconds, or None for a file that does not exist. Every
    writer of these files replaces or rewrites them, so two equal identities
    around a read say no writer landed inside it, and an identity that
    differs says one did even when the bytes it left read the same."""
    out = {}
    for label, path in paths:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            out[label] = None
            continue
        out[label] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return out


def _roster_authority(seat_name, session):
    """This seat's roster authority BY VALUE: the binding projection, and the
    repair episode's attempt, birth, spell and outcome. Every other seat's
    join, rename, mute or attendance write rewrites the same file and changes
    none of this. Attempt ids are minted once with their birth, so an episode
    replaced and then restored cannot read the same."""
    key, row, _rows, err = _roster_row(seat_name)
    if err:
        return "unreadable: %s" % err
    att = (row or {}).get("attendance")
    att = att if isinstance(att, dict) else {}
    ep = att.get("repair") if isinstance(att.get("repair"), dict) else {}
    return json.dumps({
        "binding": _binding(key, row, session), "since": att.get("since"),
        "repair": {k: ep.get(k) for k in ("attempt", "born", "since",
                                          "outcome")}},
        sort_keys=True, default=str)


def _pause_authority(seat_name, session):
    """This seat's family pause record BY VALUE: its state, dark latch,
    `since` and TRANSITION IDENTITY. The watcher rewrites the whole file on
    every pass with a new `ts`, whether or not any verdict changed, so the
    file's identity is not the authority; the family record is.

    `since` alone cannot tell a verdict changed and restored inside one
    second from the one it replaced: it is a one-second stamp. The watcher
    mints a new `transition_id` on every change of the state or the dark
    latch and keeps it across passes that change neither (proxywatch
    `_transition_id`), so a restored verdict reads differently from the one
    before the change and an unchanged rewrite reads the same.

    A LEGACY RECORD, written before the watcher minted that identity, falls
    to the conservative direction: the identities of the pause file and its
    backup join its value, so any watcher rewrite reads as a change."""
    from . import proxywatch, seat as seatmod
    from .seats_runtime import runtime_for_session
    _key, row, _rows, err = _roster_row(seat_name)
    if err:
        return "unreadable: %s" % err
    runtime, verified = runtime_for_session(row or {}, session)
    family, ferr = seatmod.family_for(str(seat_name or ""), runtime, verified)
    if ferr or not family:
        return "no family"
    state, serr = proxywatch._read_delivery_state()
    if serr:
        return "unreadable: %s" % serr
    records = (state or {}).get("upstream") if isinstance(state, dict) else None
    record = records.get(family) if isinstance(records, dict) else None
    if not isinstance(record, dict):
        return "no %s record" % family
    value = {"family": family, "state": record.get("state"),
             "dark": record.get("dark"), "since": record.get("since")}
    transition = record.get("transition_id")
    if isinstance(transition, str) and transition:
        value["transition"] = transition
    else:
        value["legacy files"] = _identities((
            ("pause file", proxywatch._state_path()),
            ("pause backup", proxywatch._backup_state_path())))
    return json.dumps(value, sort_keys=True, default=str)


def _versions(seat_name, session, room):
    """What an act's authorization is read as, for the capture's before and
    after and for the accounting after the act: the roster and pause
    authority by value, and the identity of every file that belongs to this
    authorization alone."""
    out = _identities(_dependency_paths(seat_name, session, room))
    out["roster row"] = _roster_authority(seat_name, session)
    out["pause"] = _pause_authority(seat_name, session)
    return out


def _dependency_paths(seat_name, session, room):
    """The dependency FILES compared by identity: the seat's own delivery and
    wake cursors for the owed room, the rename and rotation journals, and --
    for a project-canonical seat whose family its spawn register names -- that
    register. The fleet-global roster and pause files are NOT here: they are
    compared by value (`_versions`), because unrelated writers rewrite them."""
    from .seats_cursor import (beacon_cursor_path, cursor_path,
                               rotation_journal_path)
    from .seats_rename import rename_journal_path
    paths = [("rename journal", rename_journal_path()),
             ("rotation journal", rotation_journal_path(room)),
             ("delivery cursor", cursor_path(room, seat_name, session)),
             ("wake cursor", beacon_cursor_path(room, seat_name, session))]
    register = _spawn_register_path(seat_name)
    if register:
        paths.append(("spawn register", register))
    return tuple(paths)


def _spawn_register_path(seat_name):
    """The spawn register that names a project-canonical seat's family, or
    None for a seat whose family its name answers."""
    from . import seat
    from .seat_lifecycle import _spawn_path
    from .seat_lifecycle_sessions import registered_seat_family
    family, _project = registered_seat_family(str(seat_name or ""))
    return _spawn_path(seat._instance_dir(family, seat_name)) if family else None


def _capture(seat_name, session, bound, occ, attempt):
    """(True | False | None, why, facts) -- THE FINAL CAPTURE of a prepared
    keystroke's dependencies, every one the preparation certified: the roster
    projection, the attempt's standing and birth, no rename in flight, the
    pause verdict and its source, and the owed occurrence under its cursors,
    its room bytes and the rotation journal. No lock is taken and no census
    is run; the act door bounds the whole of it by its deadline.

    What the authorization is read as (`_versions`: the roster and pause
    authority by value, the seat's own dependency files by identity) is taken
    before and after, so a writer that changes it INSIDE the capture makes the
    capture not one observation: that is False, and the door restarts.
    `facts` carries those versions for the accounting after the act, and the
    attempt's horizon for the CPU-only compare before the keystroke."""
    from .seats_address import seat_scope
    from .seats_stop_fp import occurrence_owed
    facts = {}
    try:
        before = _versions(seat_name, session, occ.get("room"))
        key, row, rows, err = _roster_row(seat_name)
        if err:
            return None, err, facts
        if row is None or _binding(key, row, session) != bound:
            return False, ("the roster binding of %s (session, runtime or "
                           "scope) changed" % seat_name), facts
        stale, facts["horizon"] = _attempt_refusal(attempt, row, time.time())
        if stale:
            return False, stale, facts
        if _rename_in_flight():
            return None, "a seat rename is in flight", facts
        paused, perr = _pause_verdict(seat_name, session, row)
        if perr:
            return None, "the pause verdict: %s" % perr, facts
        if paused:
            return False, ("delivery to %s was paused (%s)"
                           % (seat_name, paused)), facts
        valid, why = occurrence_owed(seat_name, session, occ,
                                     seat_scope(seat_name, rows))
        if valid is not True:
            return valid, why, facts
        after = _versions(seat_name, session, occ.get("room"))
        if after != before:
            return False, ("%s was rewritten during the final capture" % (
                ", ".join(k for k in after if after[k] != before.get(k)))), facts
        facts["versions"] = after
        return True, "", facts
    except Exception as exc:                 # noqa: BLE001
        return None, "the final capture could not be taken (%s)" % (
            exc.__class__.__name__), facts


def _prepare_rearm_once(seat_name, session, _room=None, attempt=None,
                        key=None, nonce=None):
    """One observation for a RE-ARM keystroke -> a Grant, or None when the
    roster binding moved inside it.

    `_prepare_once` with the licence swapped: the roster binding, the
    attempt's standing, no rename in flight and the pause are the same
    checks; the reason to type is "the seat still has no live beacon"."""
    from . import harness

    def unknown(why):
        return harness.Grant(False, (
            "whether %s still has no beacon could not be re-read (%s), so "
            "this re-arm keystroke is WITHHELD; the next pass asks again"
            % (seat_name, why)), "unknown")
    try:
        key0, row, _rows, err = _roster_row(seat_name)
        if err or row is None:
            return unknown(err or "%s is not in the roster" % seat_name)
        bound = _binding(key0, row, session)
        stale, _horizon = _attempt_refusal(attempt, row, time.time())
        if stale:
            return harness.Grant(False, stale, "stale")
        if _rename_in_flight():
            return unknown("a seat rename is in flight")
        paused, perr = _pause_verdict(seat_name, session, row)
        if perr:
            return unknown("the pause verdict: %s" % perr)
        if paused:
            return harness.Grant(False, (
                "delivery to %s is paused (%s) — a pane nudge is a delivery "
                "with the pause left out" % (seat_name, paused)), "paused")
        kind, (subject, verdict) = _auto_scope(seat_name, session, row)
        if kind:
            return harness.Grant(False, "%s; %s" % (subject, verdict), kind)
        deaf, why = _still_deaf(seat_name)
        owes, owes_why = (_rearm_owes(seat_name, session, row=row)
                          if deaf else (None, ""))
    except Exception as exc:                 # noqa: BLE001
        return unknown(exc.__class__.__name__)
    if deaf is False:
        return harness.Grant(False, why, "drained")
    if deaf is None:
        return unknown(why)
    # OWING NOTHING IS A DEFERRAL, NOT A SETTLEMENT ("idle" records as
    # withheld): a row that arrives later in the same DEAF spell is owed.
    if owes is False:
        return harness.Grant(False, owes_why, "idle")
    if owes is None:
        return unknown(owes_why)
    key2, row2, _rows2, err2 = _roster_row(seat_name)
    if err2 or row2 is None:
        return unknown(err2 or "%s left the roster" % seat_name)
    if _binding(key2, row2, session) != bound:
        return None

    def capture():
        return _capture_rearm(seat_name, session, bound, attempt)

    def account(receipt):
        return _account_act(key, session, seat_name, attempt, receipt, nonce)
    return harness.Grant(True, still=capture,
                         account=account if key else None)


def _capture_rearm(seat_name, session, bound, attempt):
    """THE FINAL CAPTURE of a prepared re-arm keystroke: `_capture` with the
    owed occurrence replaced by "still no beacon". What the authorization is
    read as is the roster and pause authority by value and the rename
    journal and spawn register by identity; no chat cursor belongs to it."""
    from .seats_rename import rename_journal_path
    facts = {}

    def versions():
        paths = [("rename journal", rename_journal_path())]
        register = _spawn_register_path(seat_name)
        if register:
            paths.append(("spawn register", register))
        out = _identities(tuple(paths))
        out["roster row"] = _roster_authority(seat_name, session)
        out["pause"] = _pause_authority(seat_name, session)
        return out
    try:
        before = versions()
        key, row, _rows, err = _roster_row(seat_name)
        if err:
            return None, err, facts
        if row is None or _binding(key, row, session) != bound:
            return False, ("the roster binding of %s (session, runtime or "
                           "scope) changed" % seat_name), facts
        stale, facts["horizon"] = _attempt_refusal(attempt, row, time.time())
        if stale:
            return False, stale, facts
        if _rename_in_flight():
            return None, "a seat rename is in flight", facts
        paused, perr = _pause_verdict(seat_name, session, row)
        if perr:
            return None, "the pause verdict: %s" % perr, facts
        if paused:
            return False, ("delivery to %s was paused (%s)"
                           % (seat_name, paused)), facts
        deaf, why = _still_deaf(seat_name)
        if deaf is not True:
            return deaf, why, facts
        after = versions()
        if after != before:
            return False, ("%s was rewritten during the final capture" % (
                ", ".join(k for k in after if after[k] != before.get(k)))), facts
        facts["versions"] = after
        return True, "", facts
    except Exception as exc:                 # noqa: BLE001
        return None, "the final capture could not be taken (%s)" % (
            exc.__class__.__name__), facts


def _admission(seat_name, session, key, room, attempt):
    """The act-door authorization a repair child of `key` carries: the
    re-arm licence for a `rearm:` key, the owed-row licence otherwise."""
    if str(key or "").startswith("rearm:"):
        return (lambda door: _prepare_due(seat_name, session, room=room,
                                          door=door, attempt=attempt,
                                          key=key,
                                          observe=_prepare_rearm_once))
    return (lambda door: _prepare_due(seat_name, session, room=room,
                                      door=door, attempt=attempt, key=key))


#: THIS PROCESS'S OWN ACCOUNTING, keyed (resume-state key, attempt id). An
#: act accounted OBSOLETE-AUTHORIZATION is recorded here BEFORE its write, so a
#: write that fails cannot hand the same process a repeat of the act: the
#: refusal was determined, and a store that did not persist it does not make
#: it untrue. Only named attempts are recorded; see `_account_act`.
_OBSOLETE_HERE = {}

#: The act intents THIS process has settled -- accounted or withdrawn -- by
#: nonce, so its own intent left behind by a failed write is not read back as
#: an act of unknown outcome by the process that knows the outcome.
_SETTLED_INTENTS = set()

#: How many act intents and obsolete records one resume-state entry keeps.
ACT_RECORDS_KEPT = 4


def _bindable(attempt):
    """Whether an act's accounting can be bound to `attempt`: only a NAMED
    attempt can. The one place both the record and the refusal ask it."""
    return attempt is not None


def _obsolete_act(key, attempt):
    """Why a repeat of an act for `attempt` is refused, as a record, or None.

    Three sources, in order: this process's own accounting; the store's
    OBSOLETE-AUTHORIZATION record; and an act INTENT in the store with no
    receipt that this process did not settle -- an act that may have run in a
    process that died before its accounting write, or that is running now.
    Either way nothing can show it ran under its authorization, so it is
    obsolete with its outcome unknown. No age makes an intent safe to ignore:
    an age would admit a repeat of an act still in flight elsewhere.

    An UNNAMED attempt has no identity to bind a record to -- 'None' would
    name every later unnamed child of the same key -- so it is never refused
    here; its repeat still runs the whole preparation and final capture.

    AN UNREADABLE STORE IS NOT A CLEAN RECORD. The read is STRICT: the store's
    ordinary reader answers its default for a read or parse error, which is
    exactly the empty entry a clean attempt has, so an obsolete record that
    could not be read would be granted. A strict read that fails, by
    `_read_store`'s definition (so a store holding JSON null or [] fails too),
    answers with a record marked `unreadable`, and every caller refuses it as
    UNKNOWN."""
    if not key or not _bindable(attempt):
        return None
    mine = str(attempt)
    here = _OBSOLETE_HERE.get((key, mine))
    if here:
        return here
    try:
        entry = _read_store().get(key) or {}
    except Exception as exc:                 # noqa: BLE001
        return {"unreadable": True,
                "what": "the resume state could not be read (%s)"
                % exc.__class__.__name__}
    got = (entry.get("obsolete") or {}).get(mine)
    if isinstance(got, dict):
        return got
    intent = (entry.get("intents") or {}).get(mine)
    if isinstance(intent, dict) and intent.get("nonce") not in _SETTLED_INTENTS:
        return {"door": intent.get("door"), "what": (
            "an act intent recorded at %s has no receipt, so the act may have "
            "run and its outcome is unknown" % intent.get("at"))}
    return None


def _record_intent(key, attempt, door, nonce):
    """Record that an act for `attempt` is about to be taken -> "" or why it
    could not be written. Written before the composer proof, outside the act
    window; the accounting write replaces it."""
    def change(entry):
        intents = {k: v for k, v in (entry.get("intents") or {}).items()
                   if isinstance(v, dict) and k != str(attempt)}
        intents[str(attempt)] = {"door": door, "nonce": nonce,
                                 "at": time.time(), "pid": os.getpid()}
        entry["intents"] = dict(list(intents.items())[-ACT_RECORDS_KEPT:])
        return None, True
    try:
        _mutate_entry(key, change)
    except Exception as exc:                 # noqa: BLE001
        return "%s: %s" % (exc.__class__.__name__, exc)
    return ""


def _withdraw_intent(key, attempt, nonce):
    """A door that acted on nothing withdraws its intent. This process knows
    the outcome, so it settles the nonce first; the store write is best
    effort, and an intent it fails to remove refuses a repeat elsewhere,
    which is the safe direction."""
    _SETTLED_INTENTS.add(nonce)

    def change(entry):
        intents = entry.get("intents") or {}
        if (intents.get(str(attempt)) or {}).get("nonce") != nonce:
            return None, False
        entry["intents"] = {k: v for k, v in intents.items()
                            if k != str(attempt)}
        return None, True
    _mutate_entry(key, change)


#: How many act receipts one resume-state entry keeps, newest last.
ACT_RECEIPTS_KEPT = 8


def _account_act(key, session, seat_name, attempt, receipt, nonce=None):
    """THE RECONCILIATION LEG for one act -> "" or why it could not be written.

    One write under the resume state's lock. Every act leaves a receipt with
    its measured window, and the receipt replaces the act's intent. An act
    accounted OBSOLETE-AUTHORIZATION for a NAMED attempt also leaves the record
    that makes every repeat door refuse for this attempt, and it is CHARGED in
    the SAME write: the rate stamp and its receipt land together, so no
    instant exists at which the act is recorded and not charged or the
    reverse. An attempt already charged -- by its launch, or by an earlier
    obsolete act -- is not charged again: charged is not launched, and an act
    is not a second launch.

    THE OUTCOME IS KNOWN BEFORE IT IS WRITTEN. This process records an
    obsolete act and settles the intent in memory first, so a write that
    fails -- a full disk, an unopenable lock -- still refuses this process's
    repeat. A process that dies before the write leaves its INTENT with no
    receipt, and every other door reads that as an act of unknown outcome.

    AN UNNAMED ATTEMPT (a legacy child launched without --attempt) is given a
    receipt and nothing else: no record, no charge. There is no attempt id to
    bind them to, and a record keyed 'None' would refuse and debounce every
    later unnamed child of the key; the launch already charged it."""
    from . import harness
    obsolete = receipt.get("outcome") != harness.ACT_CLEAN
    named = _bindable(attempt)
    mine = str(attempt)
    if nonce:
        _SETTLED_INTENTS.add(nonce)
    if obsolete and named:
        _OBSOLETE_HERE[(key, mine)] = {"door": receipt.get("door"),
                                       "what": receipt.get("what"),
                                       "at": time.time()}

    def change(entry):
        now = time.time()
        entry["acts"] = (list(entry.get("acts") or [])
                         + [dict(receipt, attempt=attempt, at=now)]
                         )[-ACT_RECEIPTS_KEPT:]
        intents = entry.get("intents") or {}
        if nonce and (intents.get(mine) or {}).get("nonce") == nonce:
            entry["intents"] = {k: v for k, v in intents.items() if k != mine}
        if obsolete and named:
            records = {k: v for k, v in (entry.get("obsolete") or {}).items()
                       if isinstance(v, dict)}
            receipts = entry.get("receipts")
            receipts = dict(receipts) if isinstance(receipts, dict) else {}
            if mine not in receipts and mine not in records:
                entry["at"] = [t for t in entry.get("at", [])
                               if isinstance(t, (int, float))
                               and now - t < _window_s()] + [now]
                receipts[mine] = now
                entry["receipts"] = receipts
            records[mine] = {"door": receipt.get("door"),
                             "what": receipt.get("what"), "at": now}
            entry["obsolete"] = dict(list(records.items())[-ACT_RECORDS_KEPT:])
        return None, True

    try:
        _mutate_entry(key, change)
    except Exception as exc:                 # noqa: BLE001 — the act happened
        written = "the act receipt could not be written (%s)" % exc
    else:
        written = ""
    if obsolete:
        _say("helm seat resume-turn: %s act for %s (attempt %s) accounted "
             "OBSOLETE-AUTHORIZATION: %s%s" % (
                 receipt.get("door"), seat_name, mine, receipt.get("what"),
                 "; %s" % written if written else ""))
    return written


def _deaf_attendance(seat_name):
    """The seat's attendance row IF it is under a DEAF-IN-EFFECT alarm, else
    None -- the row a manual nudge must bind its episode to."""
    try:
        from . import beacons as _beacons, seats as _seats
        row = (_seats.roster() or {}).get(seat_name) or {}
        att = row.get("attendance")
        if isinstance(att, dict) and att.get("alarm") \
                and att.get("state") == _beacons.DEAF_IN_EFFECT:
            return att
    except Exception:                        # noqa: BLE001 — an unreadable
        pass                                 # roster binds nothing
    return None


def _delivery_paused(seat_name, session):
    """The pause verdict as a sentence, or "" — the SAME authority `deliver`
    obeys, read without taking the delivery state guard because a nudge moves
    no cursor and must not serialize against a real delivery. A verdict that
    could not be read is a sentence too: UNKNOWN holds, it is not a clear."""
    try:
        from .seats_identity import _delivery_pause
        pause = _delivery_pause(seat_name, session)
    except Exception as e:                   # noqa: BLE001
        return "UNKNOWN (the pause verdict could not be read: %s)" % (
            e.__class__.__name__)
    if not pause:
        return ""
    return "state %s" % (pause.get("state") or "UNKNOWN")


def _seat_session(seat_name):
    """(the seat's current session, refusal) — read from the roster, which is
    where a seat's live session is written, so the operator never has to hold
    a sid to repair a pane."""
    try:
        from . import seats as _seats
        rows = _seats.roster()
    except OSError as e:
        return None, ("the roster could not be read (%s), so %s's session is "
                      "unknown — pass --session" % (e, seat_name))
    want = str(seat_name).casefold()
    row = next((v for k, v in (rows or {}).items()
                if str(k).casefold() == want and isinstance(v, dict)), None)
    if row is None:
        return None, ("%s is not in the roster, so it has no session to "
                      "resume — pass --session if you hold one" % seat_name)
    sid = str(row.get("session") or "")
    if not sid:
        return None, ("%s is enrolled but carries no session, so there is no "
                      "turn loop to restart" % seat_name)
    return sid, ""


def _seat_waited(seat_name, session):
    """How long this seat's oldest addressed row has waited, or None.

    BEST EFFORT AND SAID SO. The directive reads better with a duration, and
    it is the only thing this measurement is for — a repair must never fail
    because the consumption census could not be taken."""
    try:
        from . import beacons, seats as _seats
        row = (_seats.roster() or {}).get(seat_name) or {}
        waited, _unread, _ev = beacons.undrained(
            seat_name, session=session, room=row.get("home_room"))
        return waited
    except Exception:                        # noqa: BLE001 — see docstring
        return None


def spawn_child(argv, pass_fds=()):
    """Fork the detached deliverer. The one seam tests replace. `pass_fds`
    hands the child an inherited descriptor — the wake-alert capability
    rides a pipe, never a caller-nameable path."""
    import subprocess
    with open(os.devnull, "r+b") as null:
        subprocess.Popen(argv, stdin=null, stdout=null, stderr=null,
                         start_new_session=True, close_fds=True,
                         pass_fds=tuple(pass_fds))


def _child_argv(seat_name, session, text_path, delay, pids=None,
                transcript=None, since=None, record_key=None, attempt=None,
                owed_room=None):
    from . import hooks
    argv = [hooks.helm_bin(), "seat", "resume-turn", "--deliver"]
    # THE EPISODE NAMESPACE CROSSES THE FORK OR IT DOES NOT EXIST. The parent
    # keeps deaf nudges under `deaf:<seat>:<session>` precisely so a pane
    # repair cannot spend the COMPACTION leg's debounce, spiral and cap — and
    # the child, which is where the bookkeeping actually lands, was re-deriving
    # the bare seat key and writing into exactly the namespace the parent had
    # separated. A key computed twice is a key that agrees only by luck, so it
    # is computed once and carried.
    if record_key:
        argv += ["--record-key", record_key]
    # THE ATTEMPT IS NOT THE NAMESPACE. `record_key` is stable across every
    # repair of one seat -- it has to be, or each spell would get a fresh
    # debounce window and the cap would mean nothing -- so it cannot say WHICH
    # ATTEMPT this child was launched for. One spell earns several attempts,
    # so even the spell is not enough: a delayed child needs the attempt in
    # order to refuse to close somebody else's, and the charge needs it in
    # order not to treat a retry as already paid for.
    if attempt is not None:
        argv += ["--attempt", str(attempt)]
    # THE ROOM THE ALARM WAS RAISED ON RIDES TOO. The child re-asks "is a row
    # still owed" at every act, and that question read a ROTATING bounded
    # sample of foreign rooms: with more rooms than the budget, consecutive
    # passes alternate slices and the one owed room can sit in the slice the
    # child never draws -- UNKNOWN, refuse, forever, while the row waits.
    # Handing the child the room pins every re-check to the room the verdict
    # measured, which is the only room the question is about.
    if owed_room:
        argv += ["--owed-room", str(owed_room)]
    # A NAMELESS pane rides --session + --pids only: the child must never be
    # handed a seat name the hook did not prove (not even the roster's).
    if seat_name:
        argv += ["--seat", seat_name]
    argv += ["--session", session,
             "--text-file", text_path, "--delay", "%.3f" % delay]
    # THE WITHDRAWAL EVIDENCE CROSSES THE FORK OR THERE IS NO WITHDRAWAL. The
    # child can re-derive neither: the transcript path arrives only in the
    # hook payload, and `since` must be the moment the HOOK fired, not the
    # moment the child got round to looking — a child that timestamped itself
    # would ask "did the seat speak in the last instant" and always answer no.
    if transcript:
        argv += ["--transcript", transcript]
    if since is not None:
        argv += ["--since", "%.3f" % since]
    if pids:
        # THE BIRTH STAMP CROSSES THE FORK OR THE GUARD DOES NOT EXIST. `str(p)`
        # would print the bare pid, and a bare pid is a slot the kernel can
        # hand to another agent's pane during the very settle delay this flag
        # exists to survive. `ident_token` writes `<pid>:<starttime>`, which is
        # what `authorized_handle` re-proves against at send time.
        from . import orcaadopt
        argv += ["--pids", ",".join(orcaadopt.ident_token(p) for p in pids)]
    return argv


def hook(payload, dry=False):
    """One SessionStart payload -> {"action": …, "detail": …}. Never raises,
    never blocks: the caller returns 0 whatever this decides."""
    from . import actors, pk, seats
    # A SUBAGENT'S COMPACTION IS NOT THE SEAT'S (task/2542), and it is decided
    # FIRST because everything below acts on the seat: it hands over the seat's
    # handoff NEXT, forks a child that types into the seat's pane, alerts and
    # DMs on the seat's behalf, and drops the injector's suppression for a
    # session whose main thread lost nothing. A sidechain hears the rule and
    # nothing else. Keyed on the documented agent_id; see
    # actors.SIDECHAIN_RULE for why that is unmeasured on SessionStart.
    agent = actors.sidechain_agent(payload)
    if agent:
        return {"action": "sidechain", "detail": actors.sidechain_notice()}
    src = str(payload.get("source") or "")
    sid = str(payload.get("session_id") or "")
    # THE CONTEXT IS GONE BEFORE ANY OF THE DECISIONS BELOW ARE TAKEN, so the
    # injector's per-session suppression is dropped HERE — ahead of enabled(),
    # ahead of the spiral/cap/alert arms, every one of which returns early.
    # A seat whose resume is disabled or rate-capped still lost its premises;
    # tying their re-delivery to a SUCCESSFUL resume would silence exactly the
    # seats least able to notice. The session id SURVIVES a compaction and the
    # context does not — measured on the fire-ledger, session a single session id carries
    # 306 turns across five days and several compactions under one id — so
    # nothing else in the pipeline can infer this boundary.
    # THE PREDICATE IS "DID THE CONTEXT GO AWAY", NOT "IS THE SOURCE COMPACT",
    # and it is deliberately expressed as a DENY-LIST so an unknown source
    # resets. /clear wipes the context and KEEPS the session id, so the seen
    # file still names every entry the seat was sent while the seat holds none
    # of it — the injector then suppresses precisely the content it just lost.
    # That shipped with the pinned dedup and made inject's own "COMPACTION or
    # /clear -> FIRES" comment false in landed code. The owner /clears seats.
    # (In the CC 2.1.281 binary the conversation reset calls
    # regenerateSessionId and exports the new id, so on that version /clear
    # CHANGES the id, and autocompact's clear rungs depend on it. This reset
    # is right under either behaviour, because it keys on the source.)
    #
    # THE ASYMMETRY DECIDES THE DEFAULT: an unknown source that lost context and
    # stayed suppressed is SILENT, and a silent seat cannot ask for what it does
    # not know is missing; an unknown source that reset needlessly costs ONE
    # re-delivery. So every source resets except those that provably keep the
    # context — AND THAT SET IS EMPTY, so EVERY source resets, `resume` with
    # them. Read CONTEXT_PRESERVING_SOURCES for the whole reason it is empty:
    # `resume` is the obvious member and it is refuted, because it proves a
    # transcript can be OPENED, never that the retained context equals what the
    # seen-file claims, and prune-then-resume is this fleet's default recovery
    # path. Do not read a membership claim into this paragraph without checking
    # the constant: this is the one place a reader comes to decide whether
    # resume resets, so the two must be read together or not at all.
    #
    # It sits ahead of the source gate for the same reason it already sat ahead
    # of enabled(): only `compact` ARMS A RESUME, but every context loss must
    # re-deliver, and tying re-delivery to a successful resume silences exactly
    # the seats least able to notice.
    if sid and not dry and src not in CONTEXT_PRESERVING_SOURCES:
        from .inject import _ledger
        # THE PROVENANCE RIDES WITH THE DROP, because this is the only place
        # that holds both halves of it: the payload's source, and a resume
        # state no later arm has consumed yet. It is diagnostic and cannot
        # change the drop — `forget_session` judges and reports the drop
        # before it considers the marker at all.
        if not _ledger.forget_session(sid, _epoch_provenance(src, sid, agent)):
            # NOTHING DOWNSTREAM CAN INFER THIS. The seat is about to take
            # turns while suppressed against content it no longer holds, and
            # the one property of that failure is that the seat cannot notice
            # it — so the alert is the whole remedy, and it fires BEFORE the
            # source gate returns for a non-compact source (found by a review
            # at 6f7cb572: a readable seen file under an unwritable parent).
            # ROOM-ONLY, deliberately: this path runs INSIDE the seat's own
            # SessionStart hook — the seat is awake by construction, so there
            # is nothing to wake — and it fires BEFORE _decide, so a DM here
            # would feed a compact spiral the loop guard cannot yet veto
            # (blocker: capped action with a DM already sent).
            _alert(_display_name(seats.own_name(), sid),
                   "INJECTION SUPPRESSION SURVIVED A CONTEXT BOUNDARY — could "
                   "neither unlink nor truncate %s (source=%s). This seat is "
                   "suppressed against premises it no longer holds and CANNOT "
                   "detect that itself: fix the permissions on that path, then "
                   "delete the file." % (_ledger._seen_path(sid), src or "?"))
        # THE ACT STEERS FORGET AT THE SAME BOUNDARY: each speaks once per
        # CONTEXT (task/2980 lane 5), and the session id outlives one.
        try:
            from . import chat
            chat.forget_steers(sid)
        except Exception:                      # noqa: BLE001 — fail open
            pass
    if src != "compact":
        return {"action": "skip", "detail": "SessionStart source=%s" % (src or "?")}
    if not enabled():
        return {"action": "off", "detail": "HELM_RESUME_TURN=0"}
    cwd = str(payload.get("cwd") or "") or os.getcwd()
    transcript = str(payload.get("transcript_path") or "")
    seat_name = seats.own_name()
    key = compaction_key(seat_name, sid)
    prior, now = _peek(key), time.time()
    # THE RECORD IS CONSUMED BY THE DECISION THAT ACTS ON IT, in the one
    # locked write that also stamps the outcome, so it vouches for this
    # SessionStart and no later one; a dry run only reads. `agent` is None
    # here by the guard above, and it is passed rather than assumed so the
    # predicate asks the record the same question the guard asked the
    # payload: a record a subagent wrote under this key does not vouch. The
    # transcript path rides along as the other half of the record's
    # position binding (clause 5): the same file, grown only by what the
    # harness writes during a compaction, or the record is not this one's.
    native = (native_autocompaction(prior, sid, agent, now, transcript) if dry
              else _consume_native_autocompaction(key, sid, agent, now,
                                                  transcript))
    if native:
        # THE HARNESS IS ALREADY CONTINUING THE TURN. Injecting would type a
        # second directive into a live turn, and alerting would tell the room
        # a seat is parked that is not — 240 such rows in one room. Nothing
        # goes to the pane, the room or the seat's DM; the decision is
        # auditable in three places (stderr, the state entry, the ledger),
        # and it spends no resume slot: no injection happened, so the spiral
        # and cap guards have nothing to count. Absence of the record does
        # NOT reach here — see native_autocompaction.
        _say("helm seat resume-turn: native-auto — %s; nothing injected, "
             "nothing alerted" % native)
        if not dry:
            pk.event("seat.resume-turn", _display_name(seat_name, sid),
                     "native autocompaction — no resume injected, no alert "
                     "(%s)" % native)
        return {"action": "native-auto", "detail": native}
    # WHOSE COMPACTION THIS IS decides what the hook may PRINT, because the
    # printed line is read by the thread that compacted (task/2926; the
    # measurement is above compacting_thread). A PROVEN child ends here: the
    # seat lost nothing, so nothing is typed into its pane, charged, alerted
    # or DMed, and the child hears what was withheld and why. An UNKNOWN
    # thread still arms the pane leg below, since pane input reaches only
    # the seat's main conversation, but its printed line is the same notice.
    thread, thread_why = compacting_thread(transcript, sid, prior, now)
    if thread == THREAD_CHILD:
        notice = lead_suppressed_text(seat_name, thread_why)
        _say("helm seat resume-turn: subagent compaction — %s; nothing "
             "injected, nothing alerted" % thread_why)
        if not dry:
            pk.event("seat.resume-turn", _display_name(seat_name, sid),
                     "subagent compaction — lead handoff suppressed (%s)"
                     % thread_why)
        return {"action": "subagent", "detail": notice}
    action, detail = _decide(prior, now)
    if action != "resume":
        if action in ("spiral", "capped", "unknown") and not dry:
            # No dm_seat here, deliberately: spiral/capped means the guard
            # decided to STOP poking this seat, and a wake DM would hand the
            # loop a side door around the very cap that just fired. UNKNOWN
            # means that guard cannot be judged, which grants no more. The
            # record key rides the payload — the CHILD takes the state
            # lock; the hook never does (a held lock behind the spawn still
            # blocked the hook, the reviewer measured). An unreadable store
            # has nothing to record into, so its alert carries no key.
            _alert(_display_name(seat_name, sid), detail,
                   record_key=None if action == "unknown" else key,
                   mode=action)
        return {"action": action, "detail": detail}
    why, pids = _registered(seat_name, sid)
    if why:
        if not dry:
            # The wake-child owns the episode debounce AND the state
            # bookkeeping (record_key in the payload) — the hook takes no
            # lock on this path at all.
            _alert(_display_name(seat_name, sid), why,
                   dm_seat=seat_name, dm_session=sid,
                   episode=_episode(transcript), record_key=key)
        return {"action": "alert", "detail": why}
    # ONE BOUNDARY FROM THE DECISION TO RESUME TO THE CHILD THAT CARRIES IT.
    # Everything past the registration check is the ACT, and an act that
    # fails anywhere is the alert -- never the caller's last-resort catch,
    # which prints the error and returns 0 with nothing armed. That catch is
    # exactly the silent parked seat this leg exists to end, and it was
    # reachable: `resume_text` opens the transcript for the compaction floor
    # (`handoff.last_compaction`), and a path the harness handed over with a
    # byte the kernel refuses raises ValueError there, not OSError -- the
    # native-auto boundary above declined it loudly, the register matched,
    # and then this leg died between the decision and the spawn (review
    # CL97: the observation-boundary arm only passed because its register
    # did not match). Composing the text, writing it and forking the child are one
    # act, so they share the one boundary the spawn already had, and the
    # error text reaches stderr as well as the wake payload.
    try:
        text, source = resume_text(cwd, sid, transcript)
        said = (text if thread == THREAD_LEAD
                else lead_suppressed_text(seat_name, thread_why))
        delay = settle_s()
        if dry:
            return {"action": "would-resume", "detail": said, "text": text,
                    "handoff": source, "seat": seat_name, "delay": delay,
                    "pids": pids}
        tp = os.path.join(home.helm_home(), home.GLOBAL, ".state", "resume",
                          pk.slug(key) + ".txt")
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        pk.atomic_write(tp, text)
        spawn_child(_child_argv(seat_name, sid, tp, delay, pids=pids,
                                transcript=transcript, since=time.time()))
    except Exception as e:   # noqa: BLE001 — a resume that was not armed is loud
        why = ("the resume could not be armed (%s: %s)"
               % (e.__class__.__name__, e))
        _say("helm seat resume-turn: %s" % why)
        if not dry:
            _alert(_display_name(seat_name, sid), why,
                   dm_seat=seat_name, dm_session=sid,
                   episode=_episode(transcript), record_key=key)
        return {"action": "alert", "detail": why}
    # Nonblocking stamp: visible to a same-millisecond double-fire (the
    # loop-guard arm), skipped rather than waited-for on contention (the
    # reviewer's wedge). The deliverer child records again, idempotently.
    _record_nb(key, sid, "spawned", text, count_it=True)
    pk.event("seat.resume-turn", _display_name(seat_name, sid),
             "compaction resume armed (+%.1fs)%s"
             % (delay, " from " + os.path.basename(source) if source else ""))
    return {"action": "spawned", "detail": said, "text": text,
            "handoff": source, "seat": seat_name, "delay": delay}


def _recovery_task_identity(handle, generation):
    handle, generation = str(handle or ""), str(generation or "")
    if not handle or not generation:
        return None
    ref = "resume-turn-injection:%s:%s" % (handle, generation)
    token = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:20]
    return {"id": "task/resume-turn-%s" % token,
            "ref": ref,
            "title": "Recover recorded Helm injection in pane %s" %
                     _one_line(handle),
            "source": "resume-turn/recovery"}


def _recovery_owner(seat_name):
    """Best non-owner task assignee plus honest degraded-probe detail."""
    from . import beacons, dispatches, seats, seats_work_offer
    trouble = []
    try:
        exclude = {str(seat_name or "").casefold()}
        exclude.update(str(s).casefold() for s in seats.owner_names())
    except Exception as e:               # noqa: BLE001
        return None, "owner identity unreadable (%s)" % e

    try:
        live = seats_work_offer._live_seats()
    except Exception as e:               # noqa: BLE001
        live = None
        trouble.append("live-seat probe failed (%s)" % e)
    if live is None and not trouble:
        trouble.append("live-seat probe is unreadable")
    try:
        roster, failed = seats.roster_checked()
    except Exception as e:               # noqa: BLE001
        roster, failed = {}, True
        trouble.append("checked roster failed (%s)" % e)
    if failed:
        trouble.append("checked roster is unreadable")
        return None, "; ".join(trouble)
    canonical = {str(name).casefold(): name for name in roster}

    for key in sorted(live or ()):
        folded = str(key).casefold()
        peer = canonical.get(folded)
        if peer is None or folded in exclude:
            continue
        try:
            pids, issue = seats.beacon_procs(peer, strict=True)
        except Exception as e:           # noqa: BLE001
            trouble.append("beacon probe for %s failed (%s)" % (peer, e))
            continue
        if issue:
            trouble.append("beacon probe for %s is unreadable (%s)" %
                           (peer, issue))
            continue
        if pids:
            return peer, "; ".join(trouble) or None

    try:
        lander = dispatches._default_lander()
    except Exception as e:               # noqa: BLE001
        lander = None
        trouble.append("default lander probe failed (%s)" % e)
    key = str(lander or "").casefold()
    if key in canonical and key not in exclude:
        return canonical[key], "; ".join(trouble) or None

    try:
        quiet = beacons.roll()
    except Exception as e:               # noqa: BLE001
        quiet = ()
        trouble.append("quiet-roster probe failed (%s)" % e)
    for peer in quiet:
        key = str(peer).casefold()
        if key in canonical and key not in exclude:
            return canonical[key], "; ".join(trouble) or None
    return None, "; ".join(trouble) or None


#: refusal kind -> (why `helm seat composers --submit` refuses the recovery,
#: for how long). Every other kind leaves the pane to a later census.
_SUBMIT_WILL_REFUSE = {
    "obsolete": ("The act that typed it was accounted OBSOLETE-AUTHORIZATION",
                 ""),
    "stale": ("The repair attempt that typed it may no longer act, and an "
              "attempt its episode retired or its horizon passed never acts "
              "again", ""),
    "paused": ("Delivery to its seat is paused", " while the pause holds"),
    "drained": ("Nothing addressed to its seat is waiting any more",
                " while nothing is owed"),
}


def _route_recovery_inner(seat_name, injection, detail):
    """Persist one idempotent non-owner recovery task; DM is best-effort."""
    from . import seats, tasks
    handle = str((injection or {}).get("handle") or "")
    generation = str((injection or {}).get("generation") or "")
    identity = _recovery_task_identity(handle, generation)
    if not identity:
        return None, "recorded injection identity is incomplete"
    tid, ref = identity["id"], identity["ref"]
    title, source = identity["title"], identity["source"]
    owner, route_detail = _recovery_owner(seat_name)
    wait = max(0.0, recovery_persist_s())
    # A NOTE MUST NOT POINT AT A DOOR THAT WILL REFUSE. `refused` is the kind
    # the act door refused this recovery with, and `helm seat composers
    # --submit` meets the same door: it refuses an obsolete repeat or a stale
    # attempt for good, and a pause or a drain for as long as it holds.
    why = _SUBMIT_WILL_REFUSE.get((injection or {}).get("refused"))
    if why:
        note = ("Recorded Helm injection %s for pane %s remains unresolved "
                "(%s). %s, so `helm seat composers --submit %s` will refuse "
                "it%s. Read the pane and resolve the held text by hand; the "
                "repair mints a fresh attempt on its next pass."
                % (generation, handle, _one_line(detail), why[0], handle,
                   why[1]))
    else:
        note = ("Recorded Helm injection %s for pane %s remains unresolved "
                "(%s). Run `helm seat composers` to establish an exact "
                "observation; wait at least %.1fs, then run the scan again. "
                "Only if that later reading reports helm-stranded run `helm "
                "seat composers --submit %s`. Never submit held, "
                "helm-pending, or cannot-tell text."
                % (generation, handle, _one_line(detail), wait, handle))
    if route_detail:
        note += " Candidate routing degraded: %s." % _one_line(route_detail)
    try:
        # force_new BECAUSE THIS ROW IS KEYED BY ITS OWN ID, NOT BY ITS TITLE.
        # The recovery row is minted at a computed `tid` and the branch below
        # handles "already exists" as the idempotent case — so identity here
        # is the id, and the title is a generated sentence that is MEANT to
        # read like the last generation's. `tasks.add`'s title-similarity
        # refusal would turn every repeat recovery into a refusal with no
        # recovery row at all, on the path whose whole job is to leave one
        # behind. Explicit and named rather than inherited by accident.
        row, err = tasks.add(
            title, owner, note=note, refs=[ref], source=source,
            tid=tid, status="in_progress" if owner else "open", origin="agent",
            project=tasks.current_project(), force_new=True)
    except Exception as e:               # noqa: BLE001 — stay UNKNOWN, never alert
        return None, "recovery task write failed: %s" % e
    created = row is not None
    if err and "already exists" in err:
        try:
            rows, unavailable = tasks.snapshot(strict=True)
        except Exception as e:           # noqa: BLE001
            return None, "recovery task ledger unreadable (%s)" % e
        if unavailable:
            return None, "recovery task ledger unreadable (%s)" % unavailable
        row = rows.get(tid)
        if (not row or row.get("id") != tid or row.get("title") != title
                or row.get("source") != source
                or ref not in (row.get("refs") or [])
                or row.get("status") not in tasks.OPEN_STATUSES):
            return None, "recovery task identity collision or closed task %s" % tid
        err = None
    if err:
        return None, "recovery task write failed: %s" % err
    if owner and created:
        text = ("Recovery task %s: pane %s has an unresolved recorded Helm "
                "injection. Start with `helm seat composers`; wait %.1fs and "
                "scan again before any `--submit`." % (row["id"], handle, wait))
        try:
            seats.dm(owner, text, who="resume-turn/recovery")
        except Exception:
            pass                         # the task, not the nudge, is delivery
    return row, route_detail


def _route_recovery(seat_name, injection, detail):
    """Total routing boundary: no probe or ledger failure escapes the child."""
    try:
        return _route_recovery_inner(seat_name, injection, detail)
    except Exception as e:               # noqa: BLE001
        return None, "recovery routing failed: %s" % e


def _close_recovery_task(handle, generation, ensure_terminal=False):
    """Close, or mint when needed, this exact delivered terminal task."""
    from . import tasks
    identity = _recovery_task_identity(handle, generation)
    if not identity:
        return False, "recorded injection identity is incomplete"
    try:
        rows, unavailable = tasks.snapshot(strict=True)
    except Exception as e:               # noqa: BLE001
        return False, "recovery task ledger unreadable (%s)" % e
    if unavailable:
        return False, "recovery task ledger unreadable (%s)" % unavailable
    row = rows.get(identity["id"])
    reason = "exact recorded injection delivered; composer advanced"
    if row is None:
        if not ensure_terminal:
            return True, None            # initial delivery needed no task
        try:
            row, err = tasks.add(
                identity["title"], None,
                note="Terminal authority after provenance clear failed.",
                refs=[identity["ref"]], source=identity["source"],
                tid=identity["id"], status="closed", origin="agent",
                closed_reason=reason, project=tasks.current_project())
        except Exception as e:           # noqa: BLE001
            return False, "recovery terminal task write failed: %s" % e
        if err:
            return False, "recovery terminal task write failed: %s" % err
        return bool(row), None if row else "recovery terminal task returned no row"
    if (row.get("id") != identity["id"]
            or row.get("title") != identity["title"]
            or row.get("source") != identity["source"]
            or identity["ref"] not in (row.get("refs") or [])):
        return False, "recovery task identity collision %s" % identity["id"]
    if row.get("status") == "closed":
        return ((True, None) if row.get("closed_reason") == reason else
                (False, "recovery task %s closed for another reason" %
                 identity["id"]))
    try:
        closed, err = tasks.update(
            identity["id"], status="closed", closed_reason=reason)
    except Exception as e:               # noqa: BLE001
        return False, "recovery task close failed: %s" % e
    if err:
        return False, "recovery task close failed: %s" % err
    return bool(closed), None if closed else "recovery task close returned no row"


def _recovery_task_delivered(handle, generation):
    """True/False/None: exact task closed delivered, still live/absent, UNKNOWN."""
    from . import tasks
    identity = _recovery_task_identity(handle, generation)
    if not identity:
        return None, "recorded injection identity is incomplete"
    try:
        rows, unavailable = tasks.snapshot(strict=True)
    except Exception as e:               # noqa: BLE001
        return None, "recovery task ledger unreadable (%s)" % e
    if unavailable:
        return None, "recovery task ledger unreadable (%s)" % unavailable
    row = rows.get(identity["id"])
    if row is None:
        return False, None
    if (row.get("id") != identity["id"]
            or row.get("title") != identity["title"]
            or row.get("source") != identity["source"]
            or identity["ref"] not in (row.get("refs") or [])):
        return None, "recovery task identity collision %s" % identity["id"]
    reason = "exact recorded injection delivered; composer advanced"
    if row.get("status") == "closed":
        if row.get("closed_reason") == reason:
            return True, None
        return None, "recovery task %s closed for another reason" % identity["id"]
    return False, None


def _await_or_withdraw(transcript, since, delay, deadline=None):
    """(withdrawn, detail) — wait out the settle, WITHDRAWING if the seat woke.

    THE SECOND DELIVERY IS THE ONE THIS FUNCTION CANCELS. A compaction is
    announced to the seat TWICE and neither leg knows the other exists: the
    SessionStart hook PRINTS the directive, and CC injects hook stdout into
    the resumed session's context; separately this child types it into the
    seat's pane. When the hook path lands, the pane copy is a duplicate —
    and because a seat's pane composer is also where the OWNER types to that
    seat, the duplicate sits in his way rather than merely being redundant.

    PROOF, NOT A SOURCE LIST. The question is not "does this SessionStart
    source usually preserve context" but "did THIS session speak" —
    `handoff.spoke_since` answers it from the transcript and answers UNKNOWN
    when it cannot look. Only True withdraws. None and False both deliver,
    so every way of failing to prove the seat woke ends in the pane getting
    the directive, which is the direction this leg must fail in.

    IT POLLS RATHER THAN SLEEPS BECAUSE THE PROOF ARRIVES LATE. Measured: the
    first `assistant` record lands a median 7s after the boundary and NEVER
    inside SETTLE_S=3.0 (0 of 69). A single check at the settle would be a
    check that can only ever say no. So the settle is served first — the
    composer grace is still owed — and the remaining budget is spent looking.
    """
    from . import handoff
    if delay > 0:
        time.sleep(delay)
    if handoff.spoke_since(transcript, since) is True:
        return True, "seat spoke after the compaction; pane copy withdrawn"
    # NO EVIDENCE SOURCE, NO WAITING. `spoke_since` returns None the instant
    # the transcript is absent or `since` was never supplied, and polling a
    # source that cannot exist spends the WHOLE deadline on answers that are
    # structurally guaranteed. Every caller that predates the withdrawal
    # passes neither, so without this the cure inserted a 30s block into
    # paths that used to return promptly. The poll below exists because proof
    # arrives LATE, never because absence needs waiting out.
    if not transcript or since is None:
        return False, None
    end = time.time() + (_withdraw_deadline_s() if deadline is None else deadline)
    while time.time() < end:
        time.sleep(min(WITHDRAW_POLL_S, max(0.0, end - time.time())))
        if handoff.spoke_since(transcript, since) is True:
            return True, "seat spoke after the compaction; pane copy withdrawn"
    return False, None


def _launch_verdict(attempt, standing, born, now):
    """Why an attempt of this standing and birth may NOT launch now, or "".

    CHARGED IS NOT LAUNCHED, AND A MINT IS NOT A LICENCE HELD OPEN. An attempt
    launches only while its episode still names it and inside the DEBOUNCE
    horizon measured from its OWN birth. The parent may already have charged
    it, and that charge does not stretch the horizon: the debounce is the
    spacing a charge buys, so a launch past it would act outside the spacing
    it was charged against. A stale mint is refused -- nothing launched,
    nothing charged -- and the next pass mints a fresh attempt with a fresh
    birth, which is visible to the rate decision like any other. An attempt
    whose birth is unknown, or whose episode no longer names it or cannot be
    read, is refused the same way: no presented id can manufacture a launch
    its episode does not vouch for."""
    if standing == "current":
        age = now - born
        if age >= _debounce_s():
            return ("attempt %s was minted %.0fs ago, past its %.0fs launch "
                    "horizon -- a stale mint is not launched; the next pass "
                    "mints a fresh attempt" % (attempt, age, _debounce_s()))
        return ""
    if standing == "legacy":
        return ("attempt %s carries no birth, so its launch horizon cannot be "
                "judged -- refused; the next pass mints one that does"
                % attempt)
    return ("attempt %s is %s in its episode, so this launch has no episode "
            "to answer to -- refused without accounting" % (attempt, standing))


def _launch_refusal(seat_name, attempt, now):
    """Why the attempt this child was launched for may NOT launch, or ""."""
    standing, born = _attempt_standing(seat_name, attempt)
    return _launch_verdict(attempt, standing, born, now)


def _launch_charge(key, session, text, attempt, seat_name):
    """The launching writer's charge -> why this launch is refused, or "".

    A NAMED ATTEMPT'S CHARGE IS THE LAST LAUNCH GATE: eligibility and the
    charge come from one roster read under the store's lock, so a standing
    that moved while this writer waited for the lock refuses the launch
    rather than accounting nothing and acting.

    A REFUSAL ONCE DETERMINED IS RETURNED WHETHER OR NOT IT PERSISTS. A store
    that fails BEFORE the charge could judge the launch keeps the preliminary
    verdict the caller already has, because bookkeeping never costs the wake;
    a store that fails AFTER the charge judged it stale, retired, absent or
    unknown still refuses, because a failed write is not new authority. The
    unnamed compaction leg charges as it always has."""
    if attempt is None:
        _record(key, session, "spawned", text, count_it=True)
        return ""
    verdict = {}
    try:
        return _stamp(key, session, "spawned", text, True, attempt, seat_name,
                      blocking=True, launching=True, verdict=verdict)[1]
    except Exception as e:   # noqa: BLE001 — bookkeeping never costs the wake
        print("helm seat resume-turn: state write failed (%s) — the launch "
              "keeps %s" % (e, "the refusal it determined"
                            if verdict.get("refused")
                            else "its preliminary verdict"), file=sys.stderr)
        return verdict.get("refused") or ""


def child(seat_name, session, text, delay, adapter=None, pids=None,
          transcript=None, since=None, record_key=None, attempt=None,
          owed_room=None):
    """The detached deliverer: settle, inject, then recover a proven hold.

    `record_key` is the PARENT'S episode namespace. Absent it this leg keeps
    its historical key, which is the COMPACTION leg's — correct for the
    compaction child and wrong for every other caller."""
    from . import harness, pk
    key = record_key or seat_name or ("session:" + str(session)[:8])
    deaf = bool(record_key) and str(record_key).startswith(("deaf:",
                                                            "rearm:"))

    def settle(outcome, detail):
        # ONLY THE CHILD CAN CLOSE THE EPISODE, on EVERY exit. The parent
        # records `wake` when it has SPAWNED this process, which is an
        # attempt; only the party that saw whether a keystroke landed -- or
        # refused before one -- can say so. A failure to write leaves the
        # episode OPEN, which retries, which is the safe direction.
        if deaf:
            try:
                from . import beacons as _beacons
                _beacons.settle_repair(seat_name, outcome, detail,
                                       attempt=attempt)
            except Exception:                # noqa: BLE001 — open retries
                pass

    def refused(outcome, detail):
        _record(key, session, outcome, detail)
        settle(outcome, detail)
        return outcome, detail

    # THE LAUNCH IS JUDGED BEFORE THE CHARGE, so a charge can never stand in
    # for launch eligibility.
    if attempt is not None:
        stale = _launch_refusal(seat_name, attempt, time.time())
        if stale:
            return refused("stale", stale)
    # ONE ATTEMPT, ONE SLOT — DECIDED BY THE RECEIPT, UNDER THE STORE'S LOCK.
    # The parent charges when it decides to spawn and this child charges when
    # it starts; either can arrive first, and the parent's charge is
    # nonblocking and skipped on contention, so both writers name the attempt
    # and the ledger decides. The charge judges the launch again from its own
    # roster read, and a refusal there is a refusal here.
    stale = _launch_charge(key, session, text, attempt, seat_name)
    if stale:
        return refused("stale", stale)
    delivery_text = _wire_text(key, session, text)
    withdrawn, why = _await_or_withdraw(transcript, since, delay)
    if withdrawn:
        # A withdrawal types nothing, so it spends no slot.
        _record(key, session, "withdrawn", text, count_it=False)
        return "withdrawn", why
    submitted = {}

    def capture(ad, handle, state, proof, generation):
        submitted.update({"adapter": ad, "handle": handle,
                          "state": state, "proof": proof,
                          "generation": generation})

    # THE AUTHORIZATION BELONGS TO THE DEAF LEG ONLY. This function is the ONE
    # deliverer for TWO callers asking different questions: `wake_undelivered`
    # types because ROWS ARE OWED, so "are they still owed" is its licence and
    # is prepared at every act; `arm` types because the seat COMPACTED, which
    # no chat backlog answers, so it carries no authorization at all.
    # `record_key` separates them: only the deaf parent passes one.
    #
    # THE ACT CLOSURE CARRIES THE ATTEMPT, so every door judges the attempt
    # again at its final capture -- a child delayed past its charge acts for
    # an attempt its episode still names, or not at all -- and the resume
    # state KEY, under which each act is accounted and a repeat of an act
    # accounted obsolete-authorization is refused.
    admit = (_admission(seat_name, session, key, owed_room, attempt)
             if record_key else None)

    def pre(door):
        """A preparation with no act behind it: it decides whether to enter
        the pane transaction at all, and records a refusal it finds."""
        if admit is None:
            return None
        grant = admit(door)
        return None if grant.ok else (_outcome_of(grant.kind),
                                      harness.DoorRefusal(grant.why, door,
                                                          grant.kind))

    stop = pre("settle")
    if stop:
        return refused(*stop)

    carried = ({"attempt": attempt, "account_key": key, "room": owed_room}
               if record_key else {})
    mode, detail = deliver(seat_name, delivery_text, session,
                           adapter=adapter, pids=pids, on_submit=capture,
                           admit=admit, **carried)

    def retryable():
        return (submitted.get("state") == harness.UNKNOWN
                and submitted.get("generation") is None
                and bool(getattr(submitted.get("proof"), "retryable", False)))

    # A blind bounded pre-read can still lose an entire repaint window. Retry the
    # WHOLE delivery transaction, not the stale handle: deliver() re-enters
    # _pane_action/authorized_handle and therefore re-proves pane identity. A
    # dirty composer is terminal and never waits/retries; False generation means
    # typing happened but provenance persistence failed, and also never retries.
    refusals = [detail] if retryable() else []
    for n in range(1, max(1, RECOVERY_ATTEMPTS)):
        if not retryable():
            break
        backoff = recovery_backoff_s()
        if backoff > 0:
            time.sleep(backoff * (2 ** (n - 1)))
        stop = pre("retry")
        if stop:
            return refused(*stop)
        submitted.clear()
        # THE RETRY IS A DELIVERY TOO, and it carries the same authorization
        # to the same act doors.
        mode, detail = deliver(
            seat_name, delivery_text, session, adapter=adapter, pids=pids,
            on_submit=capture, admit=admit, **carried)
        refusals.append(detail)
    if len(refusals) > 1 and retryable():
        detail = "whole-delivery pre-read attempts exhausted: %s" % "; ".join(
            "attempt %d: %s" % (n, proof)
            for n, proof in enumerate(refusals, 1))
    # A DOOR THAT REFUSED BEFORE PLACEMENT TYPED NOTHING, and its refusal
    # carries its kind: a drain settles, a pause or an UNKNOWN defers. There is
    # no injection to recover and no obligation to hand over.
    door = submitted.get("proof")
    if isinstance(door, harness.DoorRefusal) and door.door == "placement" \
            and not submitted.get("generation"):
        return refused(_outcome_of(door.kind), detail)
    # Once Helm successfully typed into a composer, every non-delivery is OUR
    # unresolved injection. UNKNOWN still refuses automatic Enter, but it earns
    # a durable non-owner task rather than falling through to owner alert rails.
    # A dirty/unreadable PRE-read has no generation because Helm typed nothing,
    # so it remains the ordinary alert case rather than a false stranded claim.
    recovery_unresolved = bool(
        mode != "resumed" and submitted.get("generation"))
    recovery = None                          # the persistent recovery's answer
    if mode != "resumed" and submitted.get("state") == harness.NOT_DELIVERED:
        inj = _matching_injection(key, submitted["handle"], delivery_text,
                                   submitted.get("generation"))
        # The initial verifier is observation one. Wait before observation two:
        # one HELD frame is an in-flight submit, not proof of strandedness. A
        # typing refusal has no provenance and therefore skips this delay too.
        if inj and recovery_persist_s() > 0:
            time.sleep(recovery_persist_s())
            inj = _matching_injection(key, submitted["handle"], delivery_text,
                                   submitted.get("generation"))
        if injection_persistent(inj):
            # THE RECOVERY PATH TYPES, SO IT CARRIES THE SAME AUTHORIZATION. It
            # runs after a persistence wait and presses Enter with its own
            # retries; each of those Enters goes through the act door.
            stop = pre("recovery")
            if stop:
                # A REFUSAL AFTER PLACEMENT IS NOT A REFUSAL BEFORE IT. The
                # text is already IN the composer and its record persists, so
                # withholding the unauthorized Enter is right and walking away
                # from the obligation is not: the typed directive is handed
                # to a durable owner.
                outcome, not_due = stop
                task, route_err = _route_recovery(
                    seat_name, {"handle": submitted.get("handle"),
                                "generation": submitted.get("generation"),
                                "refused": getattr(not_due, "kind", "")},
                    not_due)
                if task:
                    not_due = "%s; recovery task %s persisted%s" % (
                        not_due, task["id"],
                        " (%s)" % route_err if route_err else "")
                else:
                    not_due = "%s; recovery routing UNKNOWN: %s" % (
                        not_due, route_err)
                    _say("helm seat resume-turn: %s" % not_due)
                return refused(outcome, not_due)
            state, recovery = recover_injection(
                inj, adapter=submitted["adapter"], admit=admit)
            detail = "%s; persistent recovery: %s" % (detail, recovery)
            if state == harness.DELIVERED:
                mode = "resumed"
            else:
                recovery_unresolved = True
                if state == harness.UNKNOWN:
                    mode = "unverified"
    settle("typed" if mode == "resumed" else mode, detail)
    proof = submitted.get("proof")
    safe = getattr(proof, "alert_detail", None)
    if safe and str(proof) in detail:
        detail = detail.replace(str(proof), safe)
    if mode != "resumed":
        if recovery_unresolved:
            # A recovery its act door refused names the kind, so the task
            # does not send the owner to a --submit that meets the same door.
            injection = dict({"handle": submitted.get("handle"),
                              "generation": submitted.get("generation")},
                             **({"refused": recovery.kind} if isinstance(
                                 recovery, harness.DoorRefusal) else {}))
            task, route_err = _route_recovery(seat_name, injection, detail)
            if task:
                detail = "%s; recovery task %s persisted%s" % (
                    detail, task["id"],
                    " (%s)" % route_err if route_err else "")
            else:
                detail = "%s; recovery routing UNKNOWN: %s" % (
                    detail, route_err)
                _say("helm seat resume-turn: %s" % detail)
        else:
            from . import orcaadopt
            _alert(_display_name(seat_name, session), detail,
                   dm_seat=seat_name, dm_session=session,
                   holder_ident=(orcaadopt.ident_token(pids[0])
                                 if pids else None))
    else:
        pk.event("seat.resume-turn", _display_name(seat_name, session), detail)
    # count_it=False: the hook already counted this episode. Counting again
    # here would halve the effective cap and make the spiral guard fire on the
    # NEXT legitimate compaction. Record after routing so task ids and durable
    # write failures stay visible in the state surface.
    _record(key, session, mode, detail, count_it=False)
    return mode, detail


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------

def _unreadable_sentence(why):
    return ("the resume state %s is UNREADABLE (%s): every compaction resume, "
            "deaf-in-effect nudge and episode wake DM on this host refuses "
            "as unknown until it is repaired or moved aside, and the records "
            "inside it are already lost to every reader" % (state_path(), why))


def doctor_rows():
    """[(level, msg)] for `helm doctor`: can the resume state be read?

    FAIL when it cannot, because every launch decision now refuses on it. A
    missing store is the ordinary state of a host that never compacted."""
    st, why = _store()
    if why:
        return [("FAIL", _unreadable_sentence(why))]
    if not os.path.lexists(state_path()):
        return [("OK", "resume state: none recorded yet")]
    return [("OK", "resume state: readable, %d key(s)" % len(st))]


# EVERY NAMESPACE THE RESUME STATE'S KEYS ARE MINTED IN, and where each one
# spells its seat: (prefix, leg, how the key names its seat, what its
# `session` field means). Five minters write this one store —
#
#   <seat>                      `child`/`arm`, the compaction leg
#   deaf:<seat>:<session>       `_repair_key`, one DEAF-IN-EFFECT episode
#   rearm:<seat>:<session>      `_rearm_key`, one DEAF seat's re-arm nudge
#   wake:<seat>                 `wake_dm`, the wake-DM debounce latch
#   session:<sid8>              `child`, a delivery that never learned the seat
#
# — because "the bookkeeping bucket is not the provenance"
# (`_matching_injection`): each leg needs its own act ledger. That is right at
# the STORE and fatal at a SURFACE that prints one line per key, which printed
# a live seat up to three times under three different modes and let a reader
# counting rows count seats that do not exist (task/2881).
#
# A PREFIX THAT IS NOT LISTED HERE IS NOT GUESSED AT. Its records fall into the
# listing's "naming no seat" block: visible, and never folded into a seat or
# counted as one. That is the cheap failure — a new minter shows up unattributed
# rather than silently twinning the seat it belongs to.
_KEY_NAMESPACES = (
    # prefix,     leg,                 seat from key,                  field
    ("deaf:",     "deaf-in-effect",    lambda p: ":".join(p[1:-1]),    "session"),
    ("rearm:",    "deaf re-arm",       lambda p: ":".join(p[1:-1]),    "session"),
    ("wake:",     "wake latch",        lambda p: ":".join(p[1:]),      "episode"),
    ("session:",  "nameless delivery", lambda p: "",                   "session"),
)


def _key_seat(key):
    """-> (the SEAT this resume-state key records for, leg, its field's name).

    A `deaf:` key's seat comes from the KEY, where `_repair_key` spelled it,
    NEVER from its session. Matching sessions would have missed two of the
    three live twins: a `deaf:<seat>:<session>` row sat beside that seat's own
    row under a DIFFERENT session — an OLDER episode of the same seat is still that
    seat. A `session:` key names no seat at all; only its record can answer."""
    for prefix, leg, seat_of, field in _KEY_NAMESPACES:
        if key.startswith(prefix):
            # seat names hold no colon, but joining the middle keeps a key
            # that somehow carries one attributed rather than split in half.
            return (seat_of(key.split(":")) or None), leg, field
    # A COLON IS THE NAMESPACE MARK, so an unlisted prefix names no seat —
    # it is NOT read as a seat called "newleg:codex:<sid>", and it is not
    # read as a COMPACTION record either, so it can never stand in for the
    # leg whose entry the seat's own row prints. The ambiguity is real (seat
    # names are display labels, not a validated slug) and it is resolved
    # toward the failure this row was filed for: OVER-counting. The record
    # still prints in full under "naming no seat", so an unlisted namespace
    # costs a line in the wrong block, never a seat in the count.
    if ":" in key:
        return None, "unlisted namespace", "session"
    return key, "compaction", "session"


def _by_seat(st):
    """-> ({seat: (compaction entry or None, [(leg, field, entry)])},
           [(key, entry)] for the records that name no seat).

    The second list is NOT folded into a seat and NOT counted as one."""
    owners = {}
    for key, e in st.items():
        seat, leg, _f = _key_seat(key)
        sid = (e or {}).get("session")
        if leg == "compaction" and seat and sid:
            owners.setdefault(sid, []).append(seat)
    seats, loose = {}, []
    for key in sorted(st):
        e = st[key] or {}
        seat, leg, field = _key_seat(key)
        if seat is None and leg == "nameless delivery":
            # A NAMELESS KEY STILL BELONGS TO A SEAT WHEN ITS SESSION DOES,
            # and only when exactly one seat claims that session — two
            # claimants is an ambiguity a listing must not invent an answer
            # to. Measured: 3 of the 14 live `session:` keys twinned a named
            # seat this way, the same reader-counting harm one namespace over.
            claim = owners.get(e.get("session")) or []
            seat = claim[0] if len(claim) == 1 else None
        if seat is None:
            loose.append((key, e))
            continue
        row = seats.setdefault(seat, [None, []])
        if leg == "compaction":
            row[0] = e
        else:
            row[1].append((leg, field, e))
    return {k: (v[0], v[1]) for k, v in seats.items()}, loose


def _record_line(label, e, now, indent):
    return ("%s%-14s %-9s %d in %.0fm, last %.0fm ago — %s"
            % (indent, _one_line(label), e.get("mode") or "?",
               len(e.get("at") or []), _window_s() / 60,
               (now - (e.get("last_at") or now)) / 60,
               _one_line(e.get("detail"))[:90]))


def report_lines():
    """Read-only doctor lines: what the resume leg last did, per SEAT.

    ONE ROW PER SEAT AT INDENT 2, every session-scoped leg indented beneath
    the seat it belongs to, and every meta line parenthesised — so the rows a
    reader counts are seats, which is the property task/2881 found broken."""
    st, why = _store()
    lines = ["resume-turn (post-compaction turn restart, settle %.1fs):"
             % settle_s()]
    if not enabled():
        lines.append("  (DISARMED — HELM_RESUME_TURN=0)")
    if why:
        return lines + ["  " + _unreadable_sentence(why)]
    if not st:
        return lines + ["  no compaction resume recorded yet"]
    now = time.time()
    seats, loose = _by_seat(st)
    lines.append("  (%d seat(s) across %d record(s))" % (len(seats), len(st)))
    for seat in sorted(seats):
        compaction, legs = seats[seat]
        if compaction is None:
            lines.append("  %-14s (no compaction record; %d leg(s))"
                         % (_one_line(seat), len(legs)))
        else:
            lines.append(_record_line(seat, compaction, now, "  "))
        for leg, field, e in legs:
            # An empty field is NOT printed as "?": these legs record a real
            # absence (a wake latch with no episode) and a question mark
            # would read as a value the surface failed to look up.
            held = _one_line(e.get("session") or "")
            lines.append(_record_line(
                "%s %s" % (leg, "%s %s" % (field, held) if held
                           else "(no %s)" % field),
                e, now, "      "))
    if loose:
        lines.append("  (%d record(s) naming no seat)" % len(loose))
        for key, e in loose:
            lines.append(_record_line(key, e, now, "      "))
    return lines


def _opt(rest, flag):
    if flag in rest:
        i = rest.index(flag)
        return rest[i + 1] if i + 1 < len(rest) else None


def cmd_resume_turn(args):
    """seat resume-turn --hook-json | --deliver — the post-compaction leg."""
    args = list(args)
    from .cli import guard_tail
    rc = guard_tail("helm seat resume-turn", args,
                    flags=("--hook-json", "--deliver", "--nudge", "--dry-run",
                           "--json", "--status", "--alert"),
                    valued=("--seat", "--session", "--text-file", "--delay",
                            "--pids", "--alert-fd", "--show",
                            "--transcript", "--since", "--record-key",
                            "--attempt", "--owed-room"),
                    usage=_USAGE)
    if rc is not None:
        return rc

    if "--show" in args:
        token = _opt(args, "--show") or ""
        text = show_directive(token)
        if text is None:
            print("helm seat resume-turn: directive %s is absent, ambiguous, or "
                  "expired" % token, file=sys.stderr)
            return 1
        print(text)
        return 0

    if "--status" in args:
        for line in report_lines():
            print(line)
        return 0

    if "--alert" in args:
        # The detached wake-child. The capability is an INHERITED PIPE from
        # the spawning hook, never a caller-nameable path: no pathname means
        # nothing to TOCTOU, no mode to validate wrong, no stale artifact,
        # and one-shot by construction — a pipe drains once, and a replayed
        # exec reads EOF and exits quietly. The fstat gate refuses anything
        # that is not a FIFO, so the documented flag cannot be pointed at a
        # crafted file. Same-uid stays the trust ceiling, as everywhere.
        try:
            fd = int(_opt(args, "--alert-fd") or "")
        except (TypeError, ValueError):
            print(_USAGE, file=sys.stderr)
            return 2
        raw = b""
        try:
            if not stat_mod.S_ISFIFO(os.fstat(fd).st_mode):
                _say("helm seat resume-turn: wake capability refused — "
                     "--alert-fd is not an inherited pipe")
                return 1
            while len(raw) <= 65536:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
        except OSError as e:
            _say("helm seat resume-turn: wake capability unreadable (%s)" % e)
            return 1
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if not raw:
            return 0        # drained: the wake already happened; stay quiet
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:                  # noqa: BLE001
            _say("helm seat resume-turn: wake payload malformed (%s)" % e)
            return 1
        return 0 if wake_alert(payload) else 1

    if "--nudge" in args:
        # THE DOOR THE ALARM NAMES. `--deliver` is the child's own interface
        # and consumes a one-shot text file, so advertising it as the repair
        # handed the operator an argv its own parser refuses. This composes
        # the directive the way the alarm does, which is what makes the
        # printed command and the automatic repair the SAME act.
        seat_name = _opt(args, "--seat")
        if not seat_name:
            print(_USAGE, file=sys.stderr)
            return 2
        session = _opt(args, "--session")
        if not session:
            session, why = _seat_session(seat_name)
            if not session:
                _say("helm seat resume-turn: %s" % why)
                return 1
        # THE MANUAL DOOR BINDS THE SAME WAY THE AUTOMATIC ONE DOES. A seat
        # that is DEAF-IN-EFFECT has a standing episode, and a nudge launched
        # for it must carry that attempt or its child is the unbound one the
        # episode design exists to refuse. A seat with no such alarm has no
        # episode, and the nudge is then the plain, unbound delivery it was.
        got = wake_undelivered(seat_name, session,
                               waited=_seat_waited(seat_name, session),
                               dry="--dry-run" in args,
                               att=_deaf_attendance(seat_name))
        _say("helm seat resume-turn: %s — %s"
             % (got.get("action"), got.get("detail")))
        return 0 if got.get("action") in ("wake", "would-wake",
                                          "withdrawn") else 1

    if "--deliver" in args:
        seat_name, session = _opt(args, "--seat"), _opt(args, "--session")
        tf = _opt(args, "--text-file")
        from . import orcaadopt
        pids = [ident for ident in
                (orcaadopt.parse_ident(x)
                 for x in (_opt(args, "--pids") or "").split(","))
                if ident is not None]
        # NAMELESS form: no --seat, but then --pids is mandatory — without a
        # proven holder the child would have nothing to re-prove the pane by.
        if not (session and tf and (seat_name or pids)):
            print(_USAGE, file=sys.stderr)
            return 2
        try:
            with open(tf, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as e:
            print("helm seat resume-turn: resume text unreadable (%s)" % e,
                  file=sys.stderr)
            _alert(_display_name(seat_name, session),
                   "resume text file unreadable: %s" % e,
                   dm_seat=seat_name, dm_session=session)
            return 1
        try:
            os.unlink(tf)     # one shot; a leftover file must never re-inject
        except OSError:
            pass
        try:
            delay = float(_opt(args, "--delay") or settle_s())
        except (TypeError, ValueError):
            delay = settle_s()
        try:
            since = float(_opt(args, "--since"))
        except (TypeError, ValueError):
            since = None
        mode, detail = child(seat_name, session, text, delay, pids=pids or None,
                             transcript=_opt(args, "--transcript"), since=since,
                             record_key=_opt(args, "--record-key"),
                             attempt=_opt(args, "--attempt"),
                             owed_room=_opt(args, "--owed-room"))
        print("helm seat resume-turn: %s — %s" % (mode, detail))
        # WITHDRAWN IS A SUCCESS. The directive reached the seat by the hook's
        # own channel; declining to type a second copy is this leg working,
        # not failing, and rc 1 would file it beside "the pane was deaf".
        return 0 if mode in ("resumed", "withdrawn") else 1

    if "--hook-json" not in args:
        print(_USAGE, file=sys.stderr)
        return 2

    # HOOK MODE — fail-open TOTAL (handoff.py's law): rc 0 always, silent
    # unless it acted. A SessionStart hook that raises, blocks, or refuses is a
    # hook that wedges every session start on this host.
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        try:
            payload = json.loads(raw or "")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        res = hook(payload, dry="--dry-run" in args)
        # A CHILD HEARS NOTHING. What this hook prints is read by the thread
        # that compacted, and a subagent's compaction owes it no resume: the
        # seat's NEXT is withheld, and the beacon rule is a PreToolUse deny on
        # every arm a child could attempt, so a line re-telling it changes no
        # act. The withheld line goes to stderr and the event ledger.
        if "--json" in args:
            print(json.dumps(res))
        elif res["action"] in ("sidechain", "subagent"):
            _say("helm seat resume-turn: %s — %s" % (res["action"], res["detail"]))
        elif res["action"] in ("spawned", "would-resume", "alert", "spiral",
                               "capped", "unknown"):
            print("helm seat resume-turn: %s — %s"
                  % (res["action"], res["detail"]))
    except Exception as e:
        print("helm seat resume-turn: %s" % e, file=sys.stderr)
    return 0
