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
import json
import os
import sys
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
DEBOUNCE_S = 120      # re-fire of the SAME compaction (measured: a compaction
                      # itself takes 100–190s; refilling a window inside two
                      # minutes is not physically possible)
SPIRAL_S = 900        # a NEW compaction this soon after a resume = a spiral
                      # (measured floor between real compactions: hours)
MAX_RESUMES = 3       # per rolling WINDOW_S
WINDOW_S = 3600
TEXT_BYTES = 600      # the injected line rides one keystroke burst

_USAGE = """usage: helm seat resume-turn --hook-json [--dry-run] [--json]
       helm seat resume-turn --status
       helm seat resume-turn --deliver --session SID [--seat S]
                             [--delay SEC] [--text-file PATH] [--pids P,P]
  The SessionStart(source=compact) resume leg: restart the turn loop after a
  compaction; if injection cannot be proven, alert with the measured self-wake
  route (armed inbox beacon, pane fallback, or UNKNOWN).
  --status prints the leg's recorded state lines and exits;
  --hook-json is the hook form (fail-open, rc 0 always, silent unless it acts;
  --json prints its structured result);
  --deliver is the DETACHED CHILD it forks — it waits out the composer settle,
  re-proves pane identity, and injects. Never call --deliver by hand unless you
  are reproducing the child.
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
    """(action, detail) from the prior entry — resume|debounce|spiral|capped.

    Reads a ROLLING list of resume stamps rather than a single latch: a latch
    keyed on one event either sticks forever or re-arms on a clock, and this
    has to survive both a hook that double-fires and a session that legitimately
    compacts again tomorrow."""
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


def _record(key, session, mode, detail, count_it):
    """Update one key's entry under the state lock -> the written entry."""
    from . import pk
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = pk.read_json(p, {}) or {}
        now = time.time()
        entry = st.get(key) or {}
        at = [t for t in entry.get("at", [])
              if isinstance(t, (int, float)) and now - t < _window_s()]
        if count_it:
            at.append(now)
        entry.update({"at": at, "session": session, "mode": mode,
                      "detail": detail, "last_at": now})
        st[key] = entry
        pk.write_json(p, st)
        return entry


def _peek(key):
    from . import pk
    return (pk.read_json(state_path(), {}) or {}).get(key)


# ---------------------------------------------------------------------------
# the resume text — the seat's OWN directive, never an invented one
# ---------------------------------------------------------------------------

def _one_line(s):
    """A pane-safe single line. The directive comes out of a journal file the
    agent authored, and it crosses the adapter seam into a live terminal, so it
    is laundered exactly like every other agent-authored string helm prints."""
    from . import seats
    return seats._clip(" ".join(seats._scrub(str(s or "")).split()), TEXT_BYTES)


GENERIC = ("Resuming after compaction (helm resume-turn). No handoff was "
           "written for this compaction, so re-ground BEFORE acting: run "
           "`helm handoff check` and `helm now show`, re-read your task, then "
           "continue the work in progress. Your live obligations are ONLY "
           "the OPEN dispatch rows naming you (`helm dispatch list --open`) "
           "— a compacted context makes stale history read as an invitation, "
           "and cancelled or superseded rows are DEAD however open the chat "
           "about them looks; check the row's status before claiming or "
           "rebuilding anything. If that fold cannot be read, your "
           "obligations are UNKNOWN, never empty — report the unreadable "
           "ledger rather than inferring no-work from a failed read. Do "
           "not wait for a human.")

UNCLAIMED = ("Resuming after compaction (helm resume-turn). A handoff WAS "
             "written for this compaction, but not by you — this project's "
             "journal shelf is shared by every seat on it, and %s. So there is "
             "nothing here to continue FROM, and the entry on the shelf is "
             "someone else's plan: do not act on it, and do not release, land "
             "or claim anything it names. Re-ground BEFORE acting: run `helm "
             "handoff check` and `helm now show`, re-read your task, then "
             "continue the work in progress. Your live obligations are ONLY "
             "the OPEN dispatch rows naming you (`helm dispatch list --open`). "
             "Do not wait for a human.")

UNREADABLE = ("Resuming after compaction (helm resume-turn). Whether a handoff "
              "was written for this compaction is UNKNOWN: an entry on this "
              "project's journal shelf could not be READ AT ALL (permissions, "
              "or a file that vanished mid-read), and it may have been yours. "
              "This is NOT 'no handoff exists' — do not act as though you have "
              "none, and REPORT the unreadable shelf rather than working "
              "around it silently. Re-ground BEFORE acting: run `helm handoff "
              "check` and `helm now show`, re-read your task, then continue "
              "the work in progress. Your live obligations are ONLY the OPEN "
              "dispatch rows naming you (`helm dispatch list --open`). Do not "
              "wait for a human.")

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


def _alert(seat_name, reason):
    try:
        from . import chat
        chat.post(alert_text(seat_name, reason), who="resume-turn")
        return True
    except Exception as e:      # a down chat node never blocks the hook
        print("helm seat resume-turn: alert post failed (%s): %s"
              % (seat_name, e), file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# the fire — inject the directive into the seat's pane
# ---------------------------------------------------------------------------

def deliver(seat_name, text, session, adapter=None, pids=None):
    """Inject `text` into the seat's pane -> (mode, detail).

    Runs inside autocompact's pane transaction rather than a second resolver:
    it re-proves the spawn register under the per-seat lifecycle lock (the
    settle wait is a window in which a pane can be replaced), and `for_send`
    accepts an ORPHANED-but-writable pane, which is the majority state in a
    live fleet — refusing those was what killed the /compact rescue before.

    `pids` routes an ORCA-ADOPTED seat down orcaadopt's equivalent transaction:
    there is no spawn register to re-read and no per-seat lifecycle lock to
    take, so the TOCTOU guard is that the same process still holds the seat at
    send time. Same window, same hazard, the proof that is available."""
    from . import autocompact, harness
    if pids:
        from . import orcaadopt
        if not seat_name:
            # The NAMELESS leg: the pane was resolved from this session's own
            # sid (argv --resume), so the send re-proves THAT evidence. A seat
            # name never enters the addressing — send_to_pane would resolve by
            # NAME through the roster, which is exactly the trust a nameless
            # delivery must not extend.
            return orcaadopt.send_to_sid_pane(session, text, expect_pids=pids,
                                              adapter=adapter)
        return orcaadopt.send_to_pane(seat_name, text, expect_pids=pids,
                                      adapter=adapter)
    row = {"seat": seat_name, "registered_session": session}

    def action(ad, handle, detail):
        # SUBMIT, not send. `send` succeeded on "the metaharness accepted the
        # bytes", which is precisely what every pane the owner found with its
        # next instruction typed and unsent also reported. The third mode is
        # the point: `unverified` means the pane could not be read, and it must
        # never be spelled "resumed".
        return _mode_for(*ad.submit(handle, text), detail=detail)

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
    return mode, ("%s; %s" % (detail, proof) if detail else proof)


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
            return ("this session declares no seat name (HELM_CHAT_NAME) and "
                    "its pane could not be resolved from process evidence "
                    "either: %s" % why), None
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
        return ("spawn register names session %s but this compaction is %s"
                % (str(rec.get("session"))[:8], str(session)[:8])), None
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
# be OPENED, not that the retained context equals the seen-file's claim
# (@codex-3). The live case — @opus-integrator resumed a seat onto a session
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


def spawn_child(argv):
    """Fork the detached deliverer. The one seam tests replace."""
    import subprocess
    with open(os.devnull, "r+b") as null:
        subprocess.Popen(argv, stdin=null, stdout=null, stderr=null,
                         start_new_session=True, close_fds=True)


def _child_argv(seat_name, session, text_path, delay, pids=None):
    from . import hooks
    argv = [hooks.helm_bin(), "seat", "resume-turn", "--deliver"]
    # A NAMELESS pane rides --session + --pids only: the child must never be
    # handed a seat name the hook did not prove (not even the roster's).
    if seat_name:
        argv += ["--seat", seat_name]
    argv += ["--session", session,
             "--text-file", text_path, "--delay", "%.3f" % delay]
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
    from . import pk, seats
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
    #
    # THE ASYMMETRY DECIDES THE DEFAULT: an unknown source that lost context and
    # stayed suppressed is SILENT, and a silent seat cannot ask for what it does
    # not know is missing; an unknown source that reset needlessly costs ONE
    # re-delivery. So every source resets except those that provably keep the
    # context. `resume` reloads the transcript, so it alone is listed — and if
    # that ever stops being true, the failure is one wasted re-delivery rather
    # than a mute seat. (@opus-integrator, board #181; found by @codex and
    # @codex-2 independently, both in review.)
    #
    # It sits ahead of the source gate for the same reason it already sat ahead
    # of enabled(): only `compact` ARMS A RESUME, but every context loss must
    # re-deliver, and tying re-delivery to a successful resume silences exactly
    # the seats least able to notice.
    if sid and not dry and src not in CONTEXT_PRESERVING_SOURCES:
        from .inject import _ledger
        if not _ledger.forget_session(sid):
            # NOTHING DOWNSTREAM CAN INFER THIS. The seat is about to take
            # turns while suppressed against content it no longer holds, and
            # the one property of that failure is that the seat cannot notice
            # it — so the alert is the whole remedy, and it fires BEFORE the
            # source gate returns for a non-compact source (found by @codex-2
            # in review: a readable seen file under an unwritable parent).
            _alert(_display_name(seats.own_name(), sid),
                   "INJECTION SUPPRESSION SURVIVED A CONTEXT BOUNDARY — could "
                   "neither unlink nor truncate %s (source=%s). This seat is "
                   "suppressed against premises it no longer holds and CANNOT "
                   "detect that itself: fix the permissions on that path, then "
                   "delete the file." % (_ledger._seen_path(sid), src or "?"))
    if src != "compact":
        return {"action": "skip", "detail": "SessionStart source=%s" % (src or "?")}
    if not enabled():
        return {"action": "off", "detail": "HELM_RESUME_TURN=0"}
    cwd = str(payload.get("cwd") or "") or os.getcwd()
    transcript = str(payload.get("transcript_path") or "")
    seat_name = seats.own_name()
    key = seat_name or ("session:" + sid[:8])
    action, detail = _decide(_peek(key), time.time())
    if action != "resume":
        if action in ("spiral", "capped") and not dry:
            _record(key, sid, action, detail, count_it=False)
            _alert(_display_name(seat_name, sid), detail)
        return {"action": action, "detail": detail}
    why, pids = _registered(seat_name, sid)
    if why:
        if not dry:
            _record(key, sid, "alert", why, count_it=False)
            _alert(_display_name(seat_name, sid), why)
        return {"action": "alert", "detail": why}
    text, source = resume_text(cwd, sid, transcript)
    delay = settle_s()
    if dry:
        return {"action": "would-resume", "detail": text, "handoff": source,
                "seat": seat_name, "delay": delay, "pids": pids}
    tp = os.path.join(home.helm_home(), home.GLOBAL, ".state", "resume",
                      pk.slug(key) + ".txt")
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    pk.atomic_write(tp, text)
    try:
        spawn_child(_child_argv(seat_name, sid, tp, delay, pids=pids))
    except Exception as e:
        _record(key, sid, "alert", "child spawn failed: %s" % e, count_it=False)
        _alert(_display_name(seat_name, sid),
               "resume child could not be spawned (%s)" % e)
        return {"action": "alert", "detail": str(e)}
    _record(key, sid, "spawned", text, count_it=True)
    pk.event("seat.resume-turn", _display_name(seat_name, sid),
             "compaction resume armed (+%.1fs)%s"
             % (delay, " from " + os.path.basename(source) if source else ""))
    return {"action": "spawned", "detail": text, "handoff": source,
            "seat": seat_name, "delay": delay}


def child(seat_name, session, text, delay, adapter=None, pids=None):
    """The detached deliverer: settle, then inject -> (mode, detail)."""
    from . import pk
    if delay > 0:
        time.sleep(delay)
    mode, detail = deliver(seat_name, text, session, adapter=adapter, pids=pids)
    key = seat_name or ("session:" + str(session)[:8])
    # count_it=False: the hook already counted this episode. Counting again
    # here would halve the effective cap and make the spiral guard fire on the
    # NEXT legitimate compaction.
    _record(key, session, mode, detail, count_it=False)
    if mode != "resumed":
        _alert(_display_name(seat_name, session), detail)
    else:
        pk.event("seat.resume-turn", _display_name(seat_name, session), detail)
    return mode, detail


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------

def report_lines():
    """Read-only doctor lines: what the resume leg last did, per seat."""
    from . import pk
    st = pk.read_json(state_path(), {}) or {}
    lines = ["resume-turn (post-compaction turn restart, settle %.1fs):"
             % settle_s()]
    if not enabled():
        lines.append("  DISARMED (HELM_RESUME_TURN=0)")
    if not st:
        return lines + ["  no compaction resume recorded yet"]
    now = time.time()
    for key in sorted(st):
        e = st[key] or {}
        lines.append("  %-14s %-9s %d in %.0fm, last %.0fm ago — %s"
                     % (_one_line(key), e.get("mode") or "?",
                        len(e.get("at") or []), _window_s() / 60,
                        (now - (e.get("last_at") or now)) / 60,
                        _one_line(e.get("detail"))[:90]))
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
                    flags=("--hook-json", "--deliver", "--dry-run", "--json",
                           "--status"),
                    valued=("--seat", "--session", "--text-file", "--delay",
                            "--pids"),
                    usage=_USAGE)
    if rc is not None:
        return rc

    if "--status" in args:
        for line in report_lines():
            print(line)
        return 0

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
                   "resume text file unreadable: %s" % e)
            return 1
        try:
            os.unlink(tf)     # one shot; a leftover file must never re-inject
        except OSError:
            pass
        try:
            delay = float(_opt(args, "--delay") or settle_s())
        except (TypeError, ValueError):
            delay = settle_s()
        mode, detail = child(seat_name, session, text, delay, pids=pids or None)
        print("helm seat resume-turn: %s — %s" % (mode, detail))
        return 0 if mode == "resumed" else 1

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
        if "--json" in args:
            print(json.dumps(res))
        elif res["action"] in ("spawned", "would-resume", "alert", "spiral",
                               "capped"):
            print("helm seat resume-turn: %s — %s"
                  % (res["action"], res["detail"]))
    except Exception as e:
        print("helm seat resume-turn: %s" % e, file=sys.stderr)
    return 0
