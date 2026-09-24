#!/usr/bin/env python3
"""Roster presence and the admin verbs that mutate roster identity.

Full rows change under one roster lock; hot presence only touches `.seen`.
Readers and writers stay together so the census has one owner. The two deferred
delivery imports close the roster/delivery cycle at its measured thinner leg.
"""
import contextlib
import json
import os
import re
import time
import uuid

from . import chat, home, pk
from .seats_common import (_BROADCAST, RENAME_ALIAS_FIELD, RENAME_ALIAS_HOURS,
                           _clip, _flocked, _scrub, _seat_key, retire_alias_claims,
                           _seat_label, _sessions_with_a_process, own_name,
                           recipient_matches, roster, roster_for_write,
                           roster_path)
from .seats_incarnation import (  # the identity-generation owner
    ABSENT, INCARNATION_MIGRATION, UNCHECKED, _lifecycle_contested,
    migrate_incarnations, preview_incarnations)
# seats_identity sits BELOW this module in the layering — it defers its own
# three roster queries to call time, so importing it here at module level is
# a DAG edge, not a cycle.
from .seats_identity import (ROOM_CLEARED, _warn_foreign, _warn_once,
                             foreign_seat, owner_names)
from .seats_runtime import (_prune_runtime_sessions, _roster_proxywatch_entry,
                            _runtime_metadata, runtime_for_session)

# THE WRITE STAYS INSIDE EACH WRITER. Hoisting all five through one helper
# made the roster-write guard's arm BLIND — it only inspects functions that
# CONTAIN a write_json call — so the guard held while its detector went dark.
# A projection of mine may not cost another lane its instrument. Estate
# resolution therefore moved to seats_estate but is RE-EXPORTED here: the
# writers call the projector beside their write, and callers hold this name.
from .seats_mute import _baseline_rooms, _rooms_to_baseline
from .seats_estate import (Estate, HOST, ISOLATED, TARGETED, UNKNOWN, estate,
                           _owns_the_global_authority,
                           _project_seat_authority)          # noqa: F401


def seen_path(seat):
    return os.path.join(chat.chat_dir(), ".seen." + _seat_key(seat))
def roster_checked():
    """(roster, failed) for truth consumers that cannot accept fail-open {}.

    Missing is a proven empty roster. Unreadable, malformed, or wrong-shaped
    state is a failed probe: callers render UNKNOWN rather than treating a
    parse failure as evidence that no seat exists.

    AND NAMING THE FILE IS PART OF READING IT. roster_path() resolves the chat
    dir, and under a relative HELM_HOME with the process cwd removed
    os.path.abspath raises FileNotFoundError — which, resolved INSIDE the try
    below, was caught by the missing-file branch and returned as PROVEN EMPTY.
    A path we cannot resolve is the one thing this function exists to
    distinguish from an empty roster, and it was reporting the opposite with
    failed=False. Measured 2026-08-10, and it silently defeated two separate
    cures written to depend on this tri-state: a fail-closed work offer went
    on offering, and a create-only mint would have minted into a roster it
    could not see. The resolution is therefore its own step, and it fails
    CLOSED — cannot-name is never proven-absent.
    """
    rows, failed = roster_acquired()
    return ({}, True) if failed else (rows, False)


def roster_acquired():
    """(rows, failed) from ONE read — rows FAIL-OPEN, `failed` judges THEM.

    TWO READS OF ONE FACT WAS THE DEFECT. A render needs the rows to list seats
    AND the verdict to call a claim's holder unlisted; taking those from
    roster() and roster_checked() separately put a window between them, and a
    roster changing inside it let one screen contradict itself — this lane's
    namesake bug.

    THE OBVIOUS FIX IS THE TRAP: pointing the renderer at roster_checked would
    BLANK THE OWNER'S ENTIRE ROSTER on one type-invalid row, since it is
    all-or-nothing by design and roster() does no row validation by design.
    Both are right for their callers. So this returns the fail-open rows —
    byte-for-byte what roster() answered — beside the verdict on those bytes.

    roster_checked KEEPS ITS CONTRACT EXACTLY; it narrows this instead of
    reading again, so the two can never disagree about one file. The mint and
    the write guard refuse on that tri-state and narrowing it here would
    silently widen what they accept. A MISSING file stays proven-empty
    (failed False); an unresolvable PATH does not — see the note above.
    """
    try:
        path = roster_path()
    except Exception:                       # noqa: BLE001 — see above: this
        return {}, True                     # is UNKNOWN, never proven empty
    try:
        # NOT A REGULAR FILE IS NOT A ROSTER. A blocking open() of a FIFO
        # waits for a writer, so one where the roster belongs hung every
        # caller (a codex seat on task/2523: `helm seat composers`). pk's door
        # opens without blocking and raises NotRegularFile, an OSError: failed.
        with pk.open_regular(path, encoding="utf-8") as f:
            value = json.load(f)
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError, TypeError):
        return {}, True
    def valid_row(row):
        if not isinstance(row, dict):
            return False
        primary, history = row.get("session"), row.get("sessions")
        return ((primary is None or isinstance(primary, str))
                and (history is None or
                     (isinstance(history, list)
                      and all(isinstance(s, str) for s in history))))

    if not isinstance(value, dict):
        return {}, True     # no rows to hand back; roster() answers {} here too
    if not all(isinstance(k, str) and valid_row(v) for k, v in value.items()):
        # THE ROWS GO BACK ANYWAY — the whole difference between the two doors.
        # Withholding them here would turn the seat list dark on one bad row,
        # the outage this function exists to avoid; the verdict rides alongside
        # so the listedness marker can answer UNKNOWN while the list renders.
        return value, True
    return value, False
def touch_seen(seat, session=None, incarnation=None):
    """Presence beat fenced to the caller's seat generation."""
    if foreign_seat(seat):
        _warn_foreign("presence beat", seat)
        return False
    from .seats_cursor import seat_state_lock
    with seat_state_lock(seat, session=session,
                         incarnation=incarnation) as current:
        if not current:
            return False
        p = seen_path(seat)
        try:
            os.utime(p)
        except OSError:
            try:
                with open(p, "w"):
                    pass
            except OSError:
                pass
    return True
def last_seen(seat, row=None):
    try:
        return os.stat(seen_path(seat)).st_mtime
    except OSError:
        return (row or {}).get("last_seen")
SESSIONS_KEPT = 8   # co-named sessions remembered per roster row (addressing)
TEMP_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")   # throwaway cwds: a real seat
def _is_temp_cwd(cwd):
    p = os.path.realpath(str(cwd or "")).rstrip(os.sep) + os.sep
    return any(p.startswith(t.rstrip(os.sep) + os.sep) for t in TEMP_ROOTS)
def nonpane_session(session):
    """True for a DIFFERENT-SID HOOK PROCESS: a session id was supplied (a hook
    payload), the pane declares its own id in the environment, and the two
    DIFFER. That is the whole of what this predicate detects. Read the scope
    below before extending anything on top of it.

    WHAT IS PROVEN (measured from inside a seat's own turn-loop):
      * a seat's OWN session identity EQUALS its env sid — env sid
        32285620…, Claude's sessions/<pid>.json sessionId, and the top-level
        transcript rows all agree. So the no-false-positive half holds: a
        seat's own hook is never mistaken for a child.

    WHAT IS REFUTED, and therefore NOT CAUGHT HERE:
      * an Agent-tool SIDECHAIN carries the PARENT's sessionId. One seat's
        subagents/agent-<id>.jsonl is isSidechain:true with its own agentId,
        yet every row carries the parent's sessionId, and its Bash tools
        inherit the parent's CLAUDE_CODE_SESSION_ID unchanged. Payload/env
        inequality cannot see that class at all — and if sidechains never fire
        SessionStart, this predicate is simply never reached for them.
      * so: NOT "subagents". Different-sid hook processes, nothing more.

    Why that is still worth having: the corruption actually observed on the
    live estate IS in the caught class. One seat's row had accumulated
    eight session ids, every one of them DIFFERENT from its pane's env sid
    (f0ad7476) — so the ids that overwrote its session/cwd/project were
    different-sid hook processes, exactly what this refuses.

    STILL OPEN, named not papered over: same-sid sidechain writes are
    indistinguishable from the seat's own (they are mitigated only by the
    class-INDEPENDENT guards — _keep_sessions and the temp-cwd refusal — which
    apply to every join regardless of this verdict), and a lingering same-name
    child keeping a dead parent's presence fresh remains open. The only real
    discriminator for that class is isSidechain/agentId in the TRANSCRIPT;
    unless a hook payload starts carrying equivalent agent provenance it is
    UNOBSERVABLE to helm — a harness-layer gap, not a helm bug. Do not chase it
    with another env heuristic: two have failed, one self-locked the entire
    fleet (the CLAUDE_CODE_CHILD_SESSION attempt: reverted the same day, and
    later rebases erased both it and its revert from main), and
    tests/test_nonpane_session.py carries a source tripwire forbidding a
    third.

    FAIL-OPEN, ALWAYS: no payload, no env id, or equal ids ⇒ False ⇒ the pane's
    own seat, unchanged. And the answer NEVER selects a seat NAME — it gates
    ONE row field (write_roster's `identity`, the current `session` binding).
    That containment is the whole lesson of the reverted commit: a wrong verdict
    there re-keyed every seat's identity and broke authorship fleet-wide; a
    wrong verdict here can at worst leave one row's `session` un-refreshed by
    one join."""
    payload = str(session or "")
    env = str(home.session_id() or "")
    return bool(payload) and bool(env) and payload != env
def _keep_sessions(sess, current, cap=SESSIONS_KEPT):
    """Trim the remembered-session history to `cap`, evicting the ids LEAST
    likely to be a live pane first — never the row's CURRENT binding, and never
    an id some live process still references.

    Plain FIFO is what let the corruption become unrecoverable: one seat
    took eight transient joins and its REAL session id (f0ad7476, still running
    as pid 2789564) was pushed out of its own row, so `seat_for_session` could
    no longer find the live pane at all. Fail-open: if the liveness probe is
    unusable we keep today's FIFO tail rather than guess."""
    if len(sess) <= cap:
        return sess
    try:
        live = _sessions_with_a_process(sess)
    except Exception:
        live = set()      # probe unusable: fall back to FIFO ORDER only — the
                          # current binding is protected either way, below
    ranked = sorted(
        range(len(sess)),
        key=lambda i: (sess[i] == current, sess[i] in live, i))
    drop = set(ranked[:len(sess) - cap])
    return [s for i, s in enumerate(sess) if i not in drop]
def _evict_session(r, sid, owner):
    """ONE SEAT PER SESSION ID. Drop `sid` from every OTHER row in the
    already-locked roster dict, so seat_for_session can never answer two
    seats — the ambiguity that let a foreign row hijack a live process's
    identity (2026-07-24). Returns the seats it cleaned, for a loud line.

    A row that LOSES its current `session` falls back to the newest session it
    still remembers rather than being blanked: the history is the addressing
    ground, and dropping it would lose the row's own delivery cursors."""
    hit = []
    for other, row in r.items():
        if other == owner or not isinstance(row, dict):
            continue
        had = list(row.get("sessions") or [])
        kept = [s for s in had if s != sid]
        changed = len(kept) != len(had)
        if row.get("session") == sid:
            if kept:
                row["session"] = kept[-1]
            else:
                row.pop("session", None)
            changed = True
        if changed:
            if kept:
                row["sessions"] = kept
            else:
                row.pop("sessions", None)
            _prune_runtime_sessions(row, sid)
            hit.append(other)
    return hit
# THE THREE STATES A CALLER CAN EXPECT OF A ROW, and they are three because
# two cannot tell the interesting cases apart — and they live in
# `seats_incarnation`, which owns the generation marker they are about.


def write_roster(seat, session=None, cwd=None, home_room=None, identity=True,
                 home_room_source=None, runtime=None, presence_beat=True,
                 keyed=False, admission=False, expect=UNCHECKED,
                 lifecycle_sid=None, pane_key=None):
    """The one-time (join) roster write — keyed by seat. Returns the row, or
    (KEY, row) when keyed=True; admission=True adds whether this write admitted
    a casefold-new key. The key is the one THIS locked write chose, never one
    re-derived from a second unlocked snapshot. `session` is the newest writer; every co-named session is ALSO kept in row["sessions"]
    (newest last, capped) so seat_for_session resolves ALL of them and each
    keeps its own delivery cursor (fan-out, never race-consume).
    home_room_source is `explicit`, `derived` or `cleared`: explicit wins;
    derived follows cwd only over derived/unlabelled state; `cleared` is a
    measured empty derivation that removes a stale derived-tier home. An
    unlabelled home takes the weakest (derived) tier, never explicit authority.

    A SESSION ID IS A PER-PROCESS FACT: a process may bind one only into its
    OWN row. A caller that declares a different identity gets the session
    binding REFUSED loudly (the rest of the mirror — cwd, home room — still
    applies, which is all the spawn register ever wrote). A sid that is
    another row's CURRENT session is REFUSED too (2026-08-02: the silent
    steal was itself the corruption when the incoming name was wrong — the
    explicit escape is `helm chat seat disown <owner> <sid8> --to <seat>`),
    while a HISTORY-only mention is still EVICTED from every other row
    (_evict_session), so `seat_for_session` stays single-valued. These close
    the 2026-07-24 contamination: seat A's probe wrote A's live session id as
    seat B's current session, after which A's every tool boundary stamped B's
    presence and drained B's inbox.

    `identity=False` (a NON-PANE session — see nonpane_session) makes the write
    ADDRESSING-ONLY: the id joins `sessions[]` so its rows deliver, but it never
    becomes the row's CURRENT `session` and never re-homes `cwd`/`project`. The
    seat NAME is untouched by this flag — resolution is always the pane's own
    seat. That containment is deliberate: the reverted attempt switched the
    NAME, and one wrong verdict then re-keyed every seat on the fleet.

    `runtime` is optional launch-owned metadata, not addressing identity. It
    keeps model family, agent harness, and backend as separate axes. One
    declared launch replaces the whole label (unknown never inherits stale
    certainty); writes with no runtime evidence preserve it, and legacy rows
    remain unlabelled. `presence_beat=False` is for a pre-exec launch mirror:
    the row records that one launch observation but cannot stamp another seat's
    self-attested seen sidecar. A foreign mirror's runtime stays unverified until
    the launched seat self-writes it; without later beats it ages normally.

    `pane_key` is the ORCA_PANE_KEY of the pane this session runs in. It is
    recorded only beside a CURRENT-session stamp, so an addressing-only or
    foreign write never moves it, and one pane key names one row: the write
    that records it clears it from every other row, because the pane now
    holds this session.
    """
    # DEFERRED: the roster<->delivery cycle, closed at its thinner leg
    # (two names, two call sites — measured, not assumed).
    from .seats import _backfill_missing_room_cursors
    foreign = foreign_seat(seat)
    if session and foreign:
        _warn_foreign("session binding", seat)
        session = None
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        r = roster_for_write()
        # ONE CANONICAL KEY PER IDENTITY, RESOLVED BEFORE THE ROW IS LOADED.
        # The roster keys casefold — a case-variant name is the SAME address
        # and the SAME keyed state downstream — so the row must be found by
        # canonical match, never by exact spelling. Deciding EXISTENCE
        # case-insensitively while LOADING by exact spelling wrote a SECOND
        # row for one identity: session history, home, runtime and presence
        # split in half, while recipient matching and the seat-name authority
        # both still saw a single seat. rename_seat has enforced this law
        # since a cross-family review; write_roster must uphold it too.
        canon = [k for k in r if str(k).casefold() == str(seat).casefold()]
        if len(canon) > 1:
            raise OSError(
                "the roster holds %d case-variant rows for %s (%s) — refusing "
                "to guess which one is the identity. Repair with `helm chat "
                "seat rename` before writing."
                % (len(canon), _seat_label(seat), ", ".join(sorted(canon))))
        # ORDINARY NAME ADMISSION, AND NOTHING CLEVERER: a missing session
        # here means the rebind sweep OR a real `chat join --seat OLD`, so
        # resolving a name would make absence of evidence into ownership at
        # the door that MINTS. Proof lives at the lifecycle boundary (2444).
        admits = not canon
        # THE FENCE IS CONSUMED HERE, AT THE FIRST MUTATION, because a
        # refusal that arrives after the write is not a refusal. It reads one
        # fact — what this key is RIGHT NOW — and compares it to what the
        # caller proved. It resolves no name, reaches no alias and consults no
        # membership list; the cleverness that does not belong at the door
        # that MINTS is still not here.
        if expect is not UNCHECKED:
            found = r.get(canon[0]) if canon else None
            if expect is ABSENT:
                # ABSENCE HAS NO IDENTITY, so the key check alone is not the
                # proof: a key that went empty -> admitted -> renamed away is
                # empty AGAIN, and "still no key here" is true of that second
                # emptiness exactly as it was of the first.
                stale = bool(canon)
            else:
                stale = (not canon
                         or (found or {}).get("incarnation") != expect)
            # AND THE LIFECYCLE PREDICATES GUARD BOTH BRANCHES, BEFORE ANY
            # MUTATION. A generation match proves WHICH ROW and not that the
            # row is still this session's: a disown hands the session to
            # another row and leaves this one's generation intact, so the
            # marked branch matched, refreshed, projected and published, and
            # the binder refused afterwards. `lifecycle_sid` is the
            # host-proven session and is NEVER written — it rides its own
            # parameter so this fence can ask about ownership without the
            # ordinary self-write path acquiring binder authority.
            if not stale:
                stale = bool(_lifecycle_contested(r, seat, lifecycle_sid))
            if stale:
                return (seat, None, False) if (keyed and admission) else \
                       ((seat, None) if keyed else None)
        if canon:
            seat = canon[0]          # keep the EXISTING spelling as the key
        row = r.get(seat) or {}
        if admits or not isinstance(row.get("incarnation"), str):
            # Rename carries this marker with the row; lawful key reuse mints a
            # new one so deferred writers cannot cross identity generations.
            row["incarnation"] = uuid.uuid4().hex
        metadata, rejected = _runtime_metadata(runtime)
        if not metadata and rejected:
            # THE JOIN CONTRADICTED THE STANDING LABEL. An explicit stamp was
            # present and unreadable, so the seat's launch testimony is now
            # UNKNOWN — stale verified authority must not outlive it. Clearing
            # only ever REMOVES authority (approval reads absence as
            # no-explicit-evidence and refuses), so a foreign mirror can use
            # this path to weaken a label and never to forge one.
            row.pop("runtime", None)
            row.pop("runtime_verified", None)
        elif metadata:
            # One launch observation replaces the whole label: retaining an old
            # family/backend when the new process cannot prove them would turn
            # "unknown" into stale certainty. A write with NO runtime evidence
            # leaves the existing label alone for ordinary presence beats.
            row["runtime"] = metadata
            # Runtime identity is self-attested only when this process owns the
            # row. A pre-exec/operator mirror may seed useful display metadata,
            # but a foreign seat cannot make that metadata authority-bearing.
            row["runtime_verified"] = not foreign
        home_room = pk.slug(home_room) if home_room else None
        if home_room_source == "explicit":
            # Known tier gap: a stale explicit env can overwrite operator
            # rehome; ranking operator higher belongs to its follow-up.
            old = row.get("home_room")
            if home_room != old:
                newly_admitted = _rooms_to_baseline(old, home_room)
                _baseline_rooms(seat, row, newly_admitted)
                if old and home_room == "main" and not newly_admitted:
                    _backfill_missing_room_cursors(
                        "main", seat, row.get("sessions") or [])
            if home_room:
                row["home_room"] = home_room
            else:
                row.pop("home_room", None)
            row["home_room_source"] = "explicit"
        elif home_room_source == ROOM_CLEARED:
            # Measured no-project clears only derived/unlabelled state; explicit
            # homes hold. The launch script preserves the durable cleared stamp.
            old, source = row.get("home_room"), row.get("home_room_source")
            if old and source in (None, "derived"):
                _baseline_rooms(seat, row, _rooms_to_baseline(old, None))
                row.pop("home_room", None)
                row.pop("home_room_source", None)
        elif home_room:
            # derived — or UNLABELED (provenance unknown reads as derived, the
            # weakest tier): fills a never-homed row or follows a derived-tier
            # one — and an EXISTING home with no source IS derived-tier, so it
            # follows too (a pre-upgrade row {home: main, source: None} must
            # not freeze its stale scattered value against every later derived
            # join). It can never overwrite an explicit/operator home or
            # clear, so a re-join/resume/mirror never downgrades a deliberate
            # home.
            old, source = row.get("home_room"), row.get("home_room_source")
            if source in (None, "derived") and home_room != old:
                _baseline_rooms(
                    seat, row, _rooms_to_baseline(old, home_room))
                row["home_room"] = home_room
            if row.get("home_room") == home_room \
                    and source in (None, "derived"):
                row["home_room_source"] = "derived"
        if session:
            # A REGISTRATION MAY NEVER SILENTLY STEAL A CURRENT BINDING
            # (incident 2, owner P0 2026-08-02: after a reboot the resume-order
            # auto-namer stamped the integrator's session 56a628d4 into a
            # peer's row; rows silently ACCUMULATED foreign sids — one held
            # two seats' live sessions at once). The old law let the new bind
            # win and _evict_session "heal" the loser; that steal IS the
            # corruption when the incoming name is the wrong one. A sid that is
            # some OTHER row's CURRENT session now refuses the whole bind —
            # identity stamp AND the sessions[] addressing append (a foreign
            # sid accumulating as "addressing" is the same accumulation) —
            # loudly, naming both seats and the explicit repair verb. History-
            # only mentions (sid in sessions[] but not current) still evict
            # below: that is cleanup of a stale claim, not a steal.
            owner = next(
                (k for k, v in r.items() if k != seat and isinstance(v, dict)
                 and v.get("session") == str(session)), None)
            if owner is not None:
                _warn_once(
                    "bind-refused:%s:%s" % (session, seat),
                    "REFUSED to bind session %.12s into %r: it is the CURRENT "
                    "session of %r. One sid, one seat — if the binding is "
                    "stale, repair it explicitly: `helm chat seat disown %s "
                    "%.8s --to %s`\n"
                    % (str(session), seat, owner, owner, str(session), seat))
                session = None
        if session:
            sess = [s for s in row.get("sessions") or [] if s != str(session)]
            sess.append(str(session))
            # A DIFFERENT-SID hook process is addressable — it needs a slot so
            # its own rows deliver — but it is NOT the row's identity: it must
            # not become the CURRENT binding, which is what made a live seat
            # read session=<dead child>. SCOPE: only that class (see
            # nonpane_session); a same-sid Agent-tool sidechain is
            # indistinguishable from the seat's own hook and DOES pass here.
            if identity:
                row["session"] = str(session)
                if pane_key:
                    row["pane_key"] = str(pane_key)
                    for other, v in r.items():
                        if other != seat and isinstance(v, dict) \
                                and v.get("pane_key") == str(pane_key):
                            v.pop("pane_key", None)
            # CLASS-INDEPENDENT, and the reason the same-sid gap is survivable:
            # this runs on EVERY join, so even a write we cannot classify can
            # never evict the row's current binding or a session some live
            # process still holds.
            row["sessions"] = _keep_sessions(
                sess, row.get("session") or str(session))
            # Provider family is a PER-SESSION capability. A co-named Codex and
            # Kimi process must never inherit whichever row-level label joined
            # last, or one can consume the other's paused cursor. Exact process
            # testimony and host-proven lifecycle stamps can steer delivery;
            # approval separately requires proxywatch measurement provenance.
            runtimes = row.get("runtime_sessions")
            runtimes = dict(runtimes) if isinstance(runtimes, dict) else {}
            sid = str(session)
            measured, invalid_measured = _roster_proxywatch_entry(runtimes, sid)
            if measured and metadata and metadata.get("backend") != "proxy":
                # Native and measured-proxy testimony for one exact session is a
                # real disagreement. Preserve neither as authority: the explicit
                # exact conflict blocks runtime_for_session's row-level fallback.
                runtimes[sid] = {"runtime": metadata, "verified": False,
                                 "source": "runtime-conflict"}
            elif measured:
                # A later SessionStart/compaction hook can replay launch labels,
                # including a costume family. Measurement outranks those labels
                # and a metadata-free presence beat must not erase it.
                pass
            elif metadata:
                runtimes[sid] = {"runtime": metadata,
                                 "verified": not foreign}
            elif invalid_measured:
                # Keep a negative exact record so the row summary cannot become a
                # fallback upgrade after a forged/stale measured entry is rejected.
                pass
            elif rejected:
                runtimes.pop(sid, None)
            elif identity and row.get("runtime_verified") is True \
                    and sid not in runtimes:
                # A non-launch lifecycle writer may know the CURRENT session
                # without carrying launch env again (resume/rebind/adopt). Bind
                # the row's standing verified runtime to that exact sid rather
                # than requiring the agent to re-establish it. Addressing-only
                # non-pane sessions never inherit this authority.
                standing, standing_rejected = _runtime_metadata(
                    row.get("runtime"))
                if standing and not standing_rejected:
                    runtimes[sid] = {"runtime": standing, "verified": True}
            row["runtime_sessions"] = runtimes
            _prune_runtime_sessions(row)
            stolen = _evict_session(r, str(session), seat)
            if stolen:
                try:
                    os.write(2, (
                        "[helm chat] session %.8s now belongs to %r ONLY — "
                        "cleared it from %s (one seat per session id; a shared "
                        "id makes every presence dot a guess)\n"
                        % (str(session), seat, ", ".join(repr(s)
                                                        for s in stolen))
                    ).encode("utf-8", "replace"))
                except OSError:
                    pass
        if cwd and (identity or not row.get("cwd")):
            # The inner rule is CLASS-INDEPENDENT — it applies even to an
            # identity-bearing join, so it also covers the same-sid sidechain
            # class the predicate above cannot see: a THROWAWAY cwd never
            # overwrites a real one. That is the exact observed corruption — a
            # seat working in the helm checkout read cwd=/tmp, project=tmp on
            # every owner surface because a fan-out subagent born in /tmp joined
            # under its name. A first-ever join still records whatever it has
            # (never lock a new seat out of its own row), and any join may
            # re-home between two REAL directories freely.
            if not (_is_temp_cwd(cwd) and row.get("cwd")
                    and not _is_temp_cwd(row["cwd"])):
                row["cwd"] = cwd
                row["project"] = os.path.basename(cwd.rstrip(os.sep)) or cwd
        if not row.get("joined"):
            row["joined"] = pk.now_ts()
        row["last_seen"] = time.time()
        # `admits` was decided at the CANONICAL RESOLUTION above, before the
        # row was loaded — not here. Computing it at this point would be
        # correct and useless: by now `seat` has already been rewritten to the
        # existing key, so every write would look like an update.
        r[seat] = row
        # AUTHORITY FIRST, ROSTER SECOND, and the asymmetry is the whole
        # argument: a crash between them leaves an EXTRA CONSERVATIVE
        # ARM — a name armed for a seat the roster does not list, which
        # refuses a little too much and is repaired by the next write.
        # The old order left the opposite: a live seat in the roster
        # that the authority never learned, so the rung passed commits
        # carrying that identity and nothing ever noticed. For a guard
        # those two failures are not symmetric, so the order is not a
        # preference.
        # THE REFUSAL BINDS THE ADMISSION, NOT THE FUNCTION. An earlier cut
        # raised whenever projection reported UNGUARDED — and write_roster is
        # the SessionStart join, the delivery registration, the spawn mirror
        # and every presence refresh, so an unwritable authority directory
        # broke EXISTING seats' presence and delivery even though their
        # identity was already armed and their metadata introduces no
        # unguarded name. That is an outage bought for nothing.
        #
        # Only an edit that ADMITS a casefold-new identity can publish
        # something the rung would never refuse, so only that edit declines.
        # An already-armed seat keeps updating; the authority fault stays
        # loud on its own through the installer notes and the rung.
        # PROJECTION RUNS ON EVERY WRITE, and the refusal is what narrows.
        # Written as `admits and not _project_...` it would SHORT-CIRCUIT:
        # a non-admitting write would skip projection entirely and the
        # authority would stop converging on names added by other paths.
        safe = _project_seat_authority(r)
        if admits and not safe:
            raise OSError(
                "refusing to publish NEW seat %r: its identity could not be "
                "armed in the seat-name authority and the failure could not "
                "be recorded, so nothing downstream would refuse it. Repair "
                "the authority directory (it is most likely unwritable) and "
                "retry. Existing seats are unaffected." % _seat_label(seat))
        if admits:
            from .seats_rename import reclaim_seat_key
            if not reclaim_seat_key(seat):
                raise OSError("retired seat state could not be reclaimed")
            retire_alias_claims(r, seat)   # an admission retires older claims
        pk.write_json(roster_path(), r)
    if presence_beat:
        touch_seen(seat, session=session, incarnation=row["incarnation"])
    return ((seat, row, admits) if admission else (seat, row)) if keyed else row
def disown_session(seat, sid, to=None):
    """(ok, message) — drop ONE remembered session id from ONE seat's row: the
    operator heal for an unverified row (`helm chat seat disown <seat> <sid>`).
    `to` (the CLI's `--to <seat>`) hands the id to its rightful owner in the
    same locked pass — the whole repair for a mis-owned session, without
    waiting for that seat's next SessionStart to reclaim it.

    A row's `sessions` list is append-only ADDRESSING history, and a dead
    seat's row keeps claiming ids that later belong to live work — that is the
    stale half of the 2026-07-24 contamination (the fresh half, a live process
    binding its id into someone else's row, is refused at the write now). The
    id may be given as an 8+-char prefix, which is what every surface prints.
    Never deletes the row and never touches another seat's row."""
    sid = str(sid or "").strip()
    if len(sid) < 8:
        return False, ("give at least 8 characters of the session id "
                       "(got %r) — a short prefix could disown the wrong one"
                       % sid)
    with _flocked(roster_path() + ".lock"):
        r = roster_for_write()
        row = r.get(seat)
        if row is None:
            hits = [k for k in r if str(k).casefold() == str(seat).casefold()]
            if len(hits) != 1:
                return False, "no roster row for %r" % seat
            seat, row = hits[0], r[hits[0]]
        had = [row.get("session")] + list(row.get("sessions") or [])
        gone = sorted({s for s in had if isinstance(s, str)
                       and s.startswith(sid)})
        if not gone:
            return False, ("%s remembers no session starting %s"
                           % (_seat_label(seat), sid))
        if len(gone) > 1:
            return False, ("%r is ambiguous for %s (%s) — give more of it"
                           % (sid, _seat_label(seat),
                              ", ".join("%.12s" % g for g in gone)))
        dead = gone[0]
        runtime_entry = (row.get("runtime_sessions") or {}).get(dead)
        runtimes = dict(row.get("runtime_sessions") or {})
        runtimes.pop(dead, None)
        if runtimes:
            row["runtime_sessions"] = runtimes
        else:
            row.pop("runtime_sessions", None)
        kept = [s for s in (row.get("sessions") or []) if s != dead]
        if kept:
            row["sessions"] = kept
        else:
            row.pop("sessions", None)
        if row.get("session") == dead:
            if kept:
                row["session"] = kept[-1]
            else:
                row.pop("session", None)
        r[seat] = row
        handed = ""
        if to:
            hits = [k for k in r if str(k).casefold() == str(to).casefold()]
            if not hits:
                return False, ("no roster row for --to %r (nothing was "
                               "changed)" % to)
            owner = hits[0]
            orow = r[owner]
            keep = [s for s in (orow.get("sessions") or []) if s != dead]
            keep.append(dead)
            orow["sessions"] = keep[-SESSIONS_KEPT:]
            # HISTORY, not a takeover: never clobber the recipient's CURRENT
            # session — a repair must not re-point a live pane's row at an
            # older id. Only a row with no current binding adopts it as one.
            if not orow.get("session"):
                orow["session"] = dead
            if isinstance(runtime_entry, dict):
                rt = dict(orow.get("runtime_sessions") or {})
                rt[dead] = runtime_entry
                orow["runtime_sessions"] = rt
            _prune_runtime_sessions(orow)
            r[owner] = orow
            handed = " and handed it to %s" % _seat_label(owner)
        pk.write_json(roster_path(), r)
        # NO PROJECTION HERE. This verb cannot admit an identity — it
        # resolves an existing canonical row or fails — so there is
        # nothing new for the authority to learn, and taking the
        # machine-global flock to discover that convoyed every roster
        # writer behind a metadata-only write. Projection belongs to
        # the doors that actually admit: the join and the rename.
    return True, ("%s disowned session %.12s%s — its presence is attributable "
                  "again (now %s)"
                  % (_seat_label(seat), dead, handed,
                     "session %.12s" % row["session"] if row.get("session")
                     else "no bound session"))
def roster_indexes(rows):
    """Project one roster snapshot into ordered session and holder indexes."""
    current, history, holders = {}, {}, {}
    for seat, row in rows.items():
        if not isinstance(row, dict):
            continue
        seat = str(seat)
        primary = row.get("session")
        if isinstance(primary, str) and primary:
            current.setdefault(primary, set()).add(seat)
            holders[seat] = primary
        remembered = row.get("sessions") or []
        if isinstance(remembered, list):
            for sid in remembered:
                if isinstance(sid, str) and sid:
                    history.setdefault(sid, set()).add(seat)
    sessions = {}
    for sid in set(current) | set(history):
        live = sorted(current.get(sid, set()))
        old = sorted(history.get(sid, set()) - set(live))
        sessions[sid] = tuple(live + old)
    return sessions, holders


def seats_for_session_in(index, session):
    """Every indexed seat for `session`, current first then remembered."""
    return list(index.get(str(session), ())) if session else []


def _seat_for_session_hits(hits, session):
    if len(hits) > 1:
        _warn_once("ambiguous-session:%s" % session,
                   "session %.8s is remembered by %d seats (%s) — resolving to "
                   "%r by binding age. Repair it: `helm chat seat disown "
                   "<wrong-seat> %.8s`\n"
                   % (str(session), len(hits),
                      ", ".join(repr(_seat_label(h)) for h in hits),
                      _seat_label(hits[0]), str(session)))
    return hits[0] if hits else None


def seat_for_session_in(index, session):
    """Resolve one indexed session with the canonical ambiguity warning."""
    return _seat_for_session_hits(seats_for_session_in(index, session), session)


def seats_for_session(session):
    """EVERY seat whose row remembers `session`, CURRENT binding first then
    history, each group name-sorted. More than one is an inconsistency, not a
    tie to break silently."""
    if not session:
        return []
    index, _holders = roster_indexes(roster())
    return seats_for_session_in(index, session)


def seat_for_session(session):
    """The ONE seat a session id belongs to, or None.

    Was: the first row in roster INSERTION ORDER that mentioned the sid — so
    which seat a live session resolved to depended on dict order, and a
    re-join could not heal a mis-binding because the older key still came
    first (an audit found it: the integrator re-joined and recreated
    its row, but another seat's key preceded it, so its live session kept
    resolving to the other seat forever). Now: a CURRENT binding outranks a
    historical one, ties break by name, and a multi-row match is announced
    LOUDLY as the repairable inconsistency it is (`helm chat seat disown`).
    Precedence over a process's OWN validated identity is gone entirely —
    acting_seat asks the process first."""
    return _seat_for_session_hits(seats_for_session(session), session)
def seats_for_session_prefix(r, token):
    """EVERY seat whose session (or 8+-char prefix of one) matches, in roster
    order — the match SET behind `_resolve_seat`.

    Extracted so the predicate has one definition and two readers. A caller
    that merely NAMES an agent can take the first hit; a caller that MOVES
    CUSTODY cannot, because a prefix matching two seats would hand one seat's
    holdings to the wrong successor. Sharing the resolver while disagreeing
    about ambiguity is only safe if the ambiguity is VISIBLE, so it is
    returned rather than resolved away.
    """
    t = str(token or "")
    if len(t) < 8:
        return []
    out = []
    for seat, row in (r or {}).items():
        row = row or {}
        sess = [row.get("session") or ""] + list(row.get("sessions") or [])
        if any(s == t or s.startswith(t) for s in sess if s):
            out.append(seat)
    return out


def _resolve_seat(r, token):
    """A roster key, else the seat whose session (or 8+-char prefix of one)
    matches — how the owner names a live agent they only know by sid."""
    if token in r:
        return token
    hits = seats_for_session_prefix(r, token)
    return hits[0] if hits else None
from .seats_rename import (_STATE_MARKERS, _bounded_sub, _key_bounded,
                           _move_seat_state, alias_window_note, rename_plan,
                           rename_record, rename_report, rename_window,
                           rename_register_apply)
def rename_seat(old, new, whole_row=False, dry_run=False,
                alias_hours=RENAME_ALIAS_HOURS):
    """Rename one whole roster row and every keyed state file.

    ``old`` may be a seat or session prefix, but the operation is never
    pane-granular. A declared actor may rename only its own row; a session target
    on a multi-session row requires ``whole_row``. Success states exactly what
    moved and reminds an environment-bound seat to relaunch/re-arm.

    THE OLD NAME STAYS REACHABLE FOR A WINDOW: the row records the rename
    event (`renamed`: old/at/until, default 24h; ``alias_hours=0`` records
    none) and every name-resolving door reads it through
    seats_common.live_alias — see seats_rename.rename_plan for the six.
    ``dry_run`` runs every guard, prints that plan and writes nothing.
    """
    new = (new or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", new):
        return False, ("new name %r must be 1-64 chars of [A-Za-z0-9._-] "
                       "(what an @mention can address)" % new)
    if new.lower() in owner_names() or _BROADCAST.search("@" + new):
        return False, "%r is reserved (an owner/broadcast name)" % new
    from . import actors
    # Hold one actor epoch across preflight, roster publication and relabel.
    # A new-name bind between them would otherwise mint a second actor.
    with _flocked(roster_path() + ".lock"), \
            (contextlib.nullcontext() if dry_run else actors._store_lock()):
        from .seats_rename import recover_seat_rename
        if not recover_seat_rename(
                roster_locked=True, actor_locked=not dry_run):
            return False, "an interrupted seat rename could not be recovered"
        r = roster_for_write()
        seat = _resolve_seat(r, old)
        if seat is None:
            return False, ("no roster row matches %r (a seat name or an "
                           "8+-char session prefix — helm chat seats --all)" % old)
        if seat == new:
            return True, "seat is already named %s" % new
        # case-INSENSITIVE taken-check: _seat_key casefolds, the reserved check
        # lowers, and _mention_re is re.I — a case-variant name (KIMI vs kimi)
        # is the SAME address + the SAME keyed state downstream, so two such
        # rows alias mentions, share presence, and cross-fire gc's state
        # unlink onto the live seat (a cross-family review, live-probed
        # 2026-07-21).
        # Exclude `seat` itself so a pure self-case-change isn't falsely blocked.
        if any(k != seat and k.casefold() == new.casefold() for k in r):
            return False, ("seat name %r is taken (case-insensitive — the "
                           "roster keys casefold; helm chat seats --all)" % new)
        # GUARD 1 — a declared identity may only rename ITS OWN row.
        if foreign_seat(seat):
            return False, (
                "refusing to rename %r: this process is %r, and `seat rename` "
                "moves the WHOLE row (every session, cursor and home) — a seat "
                "may not re-key another seat's identity. Rename your own row, "
                "or have the owner run it."
                % (_seat_label(seat), own_name()))
        # GUARD 2 — a SESSION-ID target that lands in a row holding OTHER
        # sessions is the pane-granular illusion. Refuse, and SHOW the row.
        sess = [s for s in [r[seat].get("session")]
                + list(r[seat].get("sessions") or []) if s]
        sess = sorted(set(sess))
        if seat != old and not whole_row and len(sess) > 1:
            return False, (
                "refusing: %r is ONE session of seat %s, which remembers %d "
                "(%s) — `seat rename` is ROW-granular, so this would move that "
                "whole seat's identity under %r (the 2026-07-22 identity "
                "merge). If you meant the row, say it: `helm chat seat rename "
                "%s %s --row`. If you only meant to detach that session, "
                "`helm chat seat disown %s %.8s`."
                % (old, _seat_label(seat), len(sess),
                   ", ".join("%.8s" % s for s in sess), new,
                   _seat_label(seat), new, _seat_label(seat), sess[0]))
        until, actor_note, actor_rename, err = rename_window(  # BEFORE write
            seat, new, r, alias_hours, actor_locked=not dry_run)
        if err:
            return False, err
        if dry_run:
            return True, rename_plan(seat, new, sess, until, actor_note)
        before = json.loads(json.dumps(r))
        row = r.pop(seat)
        aliases = [str(value) for value in row.get("seat_keys") or ()]
        row["seat_keys"] = list(dict.fromkeys(
            aliases + [_seat_key(seat), _seat_key(new)]))
        record = rename_record(row, seat, until)
        row.pop(RENAME_ALIAS_FIELD, None)
        if record:
            row[RENAME_ALIAS_FIELD] = record
        r[new] = row
        retire_alias_claims(r, new)
        # Publish the seat-name authority before the roster. A crash
        # between them leaves an extra conservative arm, not a seat the
        # authority never learned and would falsely admit.
        if not _project_seat_authority(r):
            return False, (
                "refusing to rename to %s: the new identity could not be armed "
                "in the seat-name authority and the failure could not be "
                "recorded, so nothing would refuse it in a public-bound "
                "commit. The authority directory is most likely unwritable."
                % _seat_label(new))
        if not _move_seat_state(
                seat, new, publish=lambda: pk.write_json(roster_path(), r),
                roster_before=before, roster_after=r,
                actor_rename=actor_rename, _locks_held=True):
            from .seats_rename import rename_journal_path
            if os.path.exists(rename_journal_path()):
                return False, (
                    "seat rename incomplete: the roster may have advanced from "
                    "%s to %s; the durable recovery journal was retained for "
                    "exact replay" % (_seat_label(seat), _seat_label(new)))
            return False, (
                "refusing to rename %s: the state transaction rolled back "
                "before roster/actor commit" % _seat_label(seat))
        if actor_rename is not None:
            ok, actor_note = actors.replay_rename(
                actor_rename, apply=False, _locked=True)
            if not ok:
                return False, ("seat renamed, but actor lineage could not be "
                               "verified: %s" % actor_note)
    old_lbl = _seat_label(seat)   # raw key drove r[new]=r.pop(seat); echoed
    window = alias_window_note(old_lbl, new, until)
    from .seats_lineage import carry_holdings
    carried = carry_holdings(seat, new)   # the bare NAME, never the key:
    register = rename_register_apply(seat, new)
    # THE TAB IS A CHECKSUM AND A RENAME BREAKS IT SILENTLY — read
    # `orcatitle.after_rename`, which owns the why and the fail-open contract.
    from . import orcatitle
    return True, rename_report(           # see carry_holdings' docstring
        old_lbl, new, sess, window, actor_note,
        carried) + " " + register + orcatitle.after_rename(new)


def rehome_seat(token, room):
    """(ok, message). The DELIBERATE home-room move (multi-project isolation):
    set a seat's roster home_room — the operator's explicit re-home (the only
    path that changes an EXISTING seat's home unless a later join carries its
    own explicit room). `room` is slugged; 'main'/'none'/'-' clears the home
    (back to all-rooms un-homed). Newly admitted rooms baseline at current EOF,
    so pre-rehome history never wakes, delivers, or stop-gates the seat."""
    room = (room or "").strip().lower()
    clear = room in ("", "main", "none", "-", "all")
    home_room = None if clear else pk.slug(room)
    with _flocked(roster_path() + ".lock"):
        r = roster_for_write()
        seat = _resolve_seat(r, token)
        if seat is None:
            return False, ("no roster row matches %r (a seat name or an "
                           "8+-char session prefix — helm chat seats --all)"
                           % token)
        row = r.get(seat) or {}
        old = row.get("home_room")
        # raw seat key drove _resolve_seat + the dict write; the echoed seat
        # label AND the roster-borne old home_room are laundered so neither a
        # hostile HELM_CHAT_NAME nor a planted home_room reshapes the terminal.
        lbl, old_lbl = _seat_label(seat), _seat_label(old) if old else old
        if clear and not old and row.get("home_room_source") == "operator":
            return True, "seat %s is already un-homed (all rooms)" % lbl
        if not clear and home_room == old \
                and row.get("home_room_source") == "operator":
            return True, "seat %s is already homed to #%s" % (lbl, home_room)
        new_home = None if clear else home_room
        newly_admitted = _rooms_to_baseline(old, new_home)
        _baseline_rooms(seat, row, newly_admitted)
        if clear:
            row.pop("home_room", None)
        else:
            row["home_room"] = home_room
        row["home_room_source"] = "operator"
        r[seat] = row
        pk.write_json(roster_path(), r)
        # NO PROJECTION HERE. This verb cannot admit an identity — it
        # resolves an existing canonical row or fails — so there is
        # nothing new for the authority to learn, and taking the
        # machine-global flock to discover that convoyed every roster
        # writer behind a metadata-only write. Projection belongs to
        # the doors that actually admit: the join and the rename.
        if clear:
            return True, ("seat %s re-homed %s -> un-homed (all rooms); "
                          "takes effect on its next delivery scan"
                          % (lbl, old_lbl or "un-homed"))
    return True, ("seat %s re-homed %s -> #%s; delivery is now { #%s, #main } "
                  "— takes effect on its next delivery scan (no relaunch)"
                  % (lbl, old_lbl or "un-homed", home_room, home_room))
