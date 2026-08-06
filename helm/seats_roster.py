#!/usr/bin/env python3
"""helm seats — the roster: RAM presence, and the admin verbs that edit it.

The roster is the fleet's answer to "who is here". A full row is written at
join; the hot path only touches `.seen`, because a presence beat that rewrote
the whole row would make every heartbeat a write amplification of the census.

TWO CONCERNS, ONE FILE, and the seam between them is worth naming: the ROW
(roster_checked, touch_seen, last_seen, write_roster, the runtime-metadata
derivation) and the ADMIN VERBS that mutate identity after the fact
(disown_session, rename_seat, rehome_seat, set_mute). They share `_flocked`
and the key discipline, and splitting them would have put the reader and the
writer of the same file on opposite sides of an import — which is how two
readers of one question start to drift.

`process_sid_scan` is deliberately NOT here even though it was authored here:
it is a generic /proc liveness primitive, and the claims census needs it
independently. It lives in seats_common, and moving it is what brought this
module under the 1000-line budget without a second cut.

The two deferred imports at the bottom of _baseline_rooms and write_roster
close the roster<->delivery cycle at its thinner leg — two names, two call
sites, measured. Same reasoning as seats_identity's three: the cycle is real,
and the smaller side pays.
"""

import json
import os
import re
import time

from . import chat, home, pk
from .seats_common import (_BROADCAST, _clip, _flocked, _scrub, _seat_key,
                           _seat_label, _sessions_with_a_process, own_name,
                           recipient_matches, roster, roster_path)
# seats_identity sits BELOW this module in the layering — it defers its own
# three roster queries to call time, so importing it here at module level is
# a DAG edge, not a cycle.
from .seats_identity import (_warn_foreign, _warn_once, foreign_seat,
                             owner_names)

def seen_path(seat):
    return os.path.join(chat.chat_dir(), ".seen." + _seat_key(seat))
def roster_checked():
    """(roster, failed) for truth consumers that cannot accept fail-open {}.

    Missing is a proven empty roster. Unreadable, malformed, or wrong-shaped
    state is a failed probe: callers render UNKNOWN rather than treating a
    parse failure as evidence that no seat exists.
    """
    try:
        with open(roster_path(), encoding="utf-8") as f:
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

    if not isinstance(value, dict) or not all(
            isinstance(k, str) and valid_row(v) for k, v in value.items()):
        return {}, True
    return value, False
def runtime_for_session(row, session=None):
    """(runtime, verified) for one delivery process, never a seat-level guess.

    Co-named sessions fan out through distinct cursors and may use different
    providers. New joins therefore retain verified launch metadata beside the
    exact session id. The row-level label remains the owner-facing newest-launch
    summary and is a compatibility fallback only for its current session (or for
    a legacy sessionless caller).
    """
    if not isinstance(row, dict):
        return None, False
    if session:
        entry = (row.get("runtime_sessions") or {}).get(str(session))
        if isinstance(entry, dict) and entry.get("verified") is True \
                and isinstance(entry.get("runtime"), dict):
            return entry["runtime"], True
        if row.get("session") != str(session):
            return None, False
    runtime = row.get("runtime")
    verified = row.get("runtime_verified") is True
    return (runtime, verified) if isinstance(runtime, dict) else (None, False)
def touch_seen(seat):
    """The hot-path presence beat: utime a per-seat empty file — no shared
    read-modify-write, no lock, no lost sibling rows. Returns True when the
    beat LANDED.

    SELF-ATTESTED ONLY (owner incident 2026-07-24): a beat is evidence that
    THIS process is alive, so it may only ever land on THIS process's own
    seat. A process that declares a different identity is refused LOUDLY and
    stamps nothing — a foreign heartbeat wearing another seat's name is
    precisely how the roster showed 🟢 fresh, every ~2s, for a seat that was
    answering nobody. Fail-open for a process with no declared name."""
    if foreign_seat(seat):
        _warn_foreign("presence beat", seat)
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

    WHAT IS PROVEN (codex-3, from inside its own turn-loop, 2026-07-24):
      * a seat's OWN session identity EQUALS its env sid — env sid
        32285620…, Claude's sessions/<pid>.json sessionId, and the top-level
        transcript rows all agree. So the no-false-positive half holds: a
        seat's own hook is never mistaken for a child.

    WHAT IS REFUTED, and therefore NOT CAUGHT HERE:
      * an Agent-tool SIDECHAIN carries the PARENT's sessionId. codex-3's
        subagents/agent-<id>.jsonl is isSidechain:true with its own agentId,
        yet every row carries the parent's sessionId, and its Bash tools
        inherit the parent's CLAUDE_CODE_SESSION_ID unchanged. Payload/env
        inequality cannot see that class at all — and if sidechains never fire
        SessionStart, this predicate is simply never reached for them.
      * so: NOT "subagents". Different-sid hook processes, nothing more.

    Why that is still worth having: the corruption actually observed on the
    live estate IS in the caught class. console-design's row had accumulated
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

    Plain FIFO is what let the corruption become unrecoverable: console-design
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
            hit.append(other)
    return hit
_RUNTIME_ENV = {"agent_harness": "HELM_AGENT_HARNESS",
                "family": "HELM_MODEL_FAMILY",
                "backend": "HELM_MODEL_BACKEND"}
_RUNTIME_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
def _runtime_metadata(runtime=None):
    """(metadata, rejected) — THE one validator, and the owner of both facts.

    Agent harness (claude/pi) and model backend (native/proxy) are independent
    axes: pi can deliberately traverse a helm seat's proxy. Values are optional
    and self-declared by the launch seam; an old/unlabelled row stays unlabelled
    rather than being guessed from its display name.

    `rejected` is True when a KNOWN field arrived non-empty and did not survive
    validation. That fact belongs HERE and nowhere else: every path into the
    roster — the env translator, `join(runtime=...)`, a direct launch mirror —
    funnels through this function, so a caller cannot bypass the distinction
    between "no evidence at all" (preserve the standing label) and "explicit
    but unreadable" (clear it). An earlier cut carried the fact on a private
    key from the env translator only, which left the public writer blind to a
    malformed direct call (codex, three rounds of the same validation-then-drop
    class: reject-then-drop makes broken testimony indistinguishable from
    silence, and silence is an upgrade).
    """
    supplied = runtime if isinstance(runtime, dict) else {}
    out, rejected = {}, False
    for field in _RUNTIME_ENV:
        value = str(supplied.get(field) or "").strip().lower()
        if not value:
            continue
        if _RUNTIME_TOKEN.fullmatch(value):
            out[field] = value
        else:
            rejected = True
    return out, rejected
def _runtime_environment(env=None):
    """Translate one joining process's launch environment into runtime facts.

    Two self-detection legs mirror each other: pi stamps PI_CODING_AGENT into
    its own environment, and claude-code stamps CLAUDECODE/CLAUDE_CODE_SESSION_ID
    into its. Both may identify the HARNESS, never a display name. FAMILY is
    stricter: CLAUDECODE is inherited by arbitrary children, so it can testify
    only that the claude-code harness is somewhere above this process. Native
    Claude family requires this process's CLAUDE_CODE_SESSION_ID, a native
    backend, and no ANTHROPIC_BASE_URL override. Helm's proxy seats run the same
    harness pointed at CLIProxyAPI; their family comes from proxywatch's measured
    session-to-route proof, never from inherited environment or a launch label.
    """
    env = os.environ if env is None else env
    # RAW pass-through: the values reach the roster's validator unparsed, so a
    # malformed launch stamp is rejected at the SAME boundary a malformed
    # direct call is, rather than being silently dropped here.
    # STRIPPED-LOWERCASE raw: the validator's contract is case-insensitive
    # (its token class spans both cases), so the derive predicates must read
    # values the same way it will — HELM_AGENT_HARNESS=Claude normalizes to
    # claude and must still permit the family derivation. Lowercasing cannot
    # change any accept/reject decision, so malformed testimony is preserved
    # exactly as before (codex meld e:1785568214: normalization-order
    # regression introduced by the raw pass-through).
    out = {field: str(env.get(name) or "").strip().lower()
           for field, name in _RUNTIME_ENV.items()}
    out = {field: value for field, value in out.items() if value}
    # RAW presence is testimony even when the value is malformed: a rejected
    # explicit stamp must stay UNKNOWN, never be treated as absent and then
    # re-derived into an authorization-bearing label (codex, 2a899c9317:
    # HELM_MODEL_FAMILY="codex/family" fails the token check, vanishes from
    # `out`, and the derive leg would have upgraded the seat to claude).
    raw = {field: field in out for field in _RUNTIME_ENV}
    claude_session = bool(str(env.get("CLAUDE_CODE_SESSION_ID") or "").strip())
    claude_harness = bool(str(env.get("CLAUDECODE") or "").strip()
                          or claude_session)
    if "agent_harness" not in out and not raw["agent_harness"] \
            and str(env.get("PI_CODING_AGENT") or "").lower() == "true":
        out["agent_harness"] = "pi"
    if "agent_harness" not in out and not raw["agent_harness"] and claude_harness:
        out["agent_harness"] = "claude"
    # An EXPLICIT backend is launch testimony about the serving path: proxy
    # (or any non-native token, or a malformed one) contradicts a native
    # claude-family derivation even without ANTHROPIC_BASE_URL set.
    backend_native = (not raw["backend"]) or out.get("backend") == "native"
    # CLAUDECODE alone is inherited harness testimony. Only the session-bearing
    # runtime can claim native Claude family; stamp backend=native beside the
    # derived family so dispatch can distinguish this measured native path from
    # an unverified/legacy family string.
    if "family" not in out and not raw["family"] and claude_session \
            and out.get("agent_harness") == "claude" and backend_native \
            and not str(env.get("ANTHROPIC_BASE_URL") or "").strip():
        out["family"] = "claude"
        if "backend" not in out and not raw["backend"]:
            out["backend"] = "native"
    return out
def write_roster(seat, session=None, cwd=None, home_room=None, identity=True,
                 home_room_source=None, runtime=None, presence_beat=True):
    """The one-time (join) roster write — keyed by seat. `session` is the
    newest writer; every co-named session is ALSO kept in row["sessions"]
    (newest last, capped) so seat_for_session resolves ALL of them and each
    keeps its own delivery cursor (fan-out, never race-consume). home_room_source
    is `explicit` or `derived`: explicit joins may deliberately move a seat;
    derived joins follow a seat across projects only while its prior home was
    also derived; they never undo an explicit/operator home or clear. An
    UNLABELED home_room reads as derived — unknown provenance takes the
    weakest tier, never the strongest (the old back-compat seam stamped it
    explicit and let a spawn mirror downgrade a deliberate home).

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
        r = roster()
        row = r.get(seat) or {}
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
            # Known tier gap (documented; follow-up card): explicit beats
            # explicit regardless of AGE, so an operator rehome holds only
            # until a pane launched with env HELM_CHAT_ROOM (explicit, no
            # derived stamp) restarts — its SessionStart join re-writes the
            # stale env room. The homing law only forbids DERIVED downgrades;
            # ranking 'operator' above a stale explicit env (or re-minting
            # launch.sh on rehome) is the candidate fix, deliberately not
            # smuggled into this lane.
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
            # auto-namer stamped OI's session 56a628d4 into helm-claude-2's row
            # and rows silently ACCUMULATED foreign sids — helm-claude-2 held
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
            # CLASS-INDEPENDENT, and the reason the same-sid gap is survivable:
            # this runs on EVERY join, so even a write we cannot classify can
            # never evict the row's current binding or a session some live
            # process still holds.
            row["sessions"] = _keep_sessions(
                sess, row.get("session") or str(session))
            # Provider family is a PER-SESSION capability. A co-named Codex and
            # Kimi process must never inherit whichever row-level label joined
            # last, or one can consume the other's paused cursor. Only the
            # process's self-written stamp is authority-bearing.
            runtimes = row.get("runtime_sessions")
            runtimes = dict(runtimes) if isinstance(runtimes, dict) else {}
            sid = str(session)
            if metadata:
                runtimes[sid] = {"runtime": metadata,
                                 "verified": not foreign}
            elif rejected:
                runtimes.pop(sid, None)
            kept_ids = set(row.get("sessions") or [])
            if row.get("session"):
                kept_ids.add(row["session"])
            runtimes = {k: v for k, v in runtimes.items() if k in kept_ids}
            if runtimes:
                row["runtime_sessions"] = runtimes
            else:
                row.pop("runtime_sessions", None)
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
        r[seat] = row
        pk.write_json(roster_path(), r)
    if presence_beat:
        touch_seen(seat)
    return row
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
        r = roster()
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
            r[owner] = orow
            handed = " and handed it to %s" % _seat_label(owner)
        pk.write_json(roster_path(), r)
    return True, ("%s disowned session %.12s%s — its presence is attributable "
                  "again (now %s)"
                  % (_seat_label(seat), dead, handed,
                     "session %.12s" % row["session"] if row.get("session")
                     else "no bound session"))
def seats_for_session(session):
    """EVERY seat whose row remembers `session`, CURRENT binding first then
    history, each group name-sorted. More than one is an inconsistency, not a
    tie to break silently."""
    if not session:
        return []
    sid = str(session)
    cur, hist = [], []
    for seat, row in roster().items():
        if not isinstance(row, dict):
            continue
        if row.get("session") == sid:
            cur.append(seat)
        elif sid in (row.get("sessions") or []):
            hist.append(seat)
    return sorted(cur) + sorted(hist)
def seat_for_session(session):
    """The ONE seat a session id belongs to, or None.

    Was: the first row in roster INSERTION ORDER that mentioned the sid — so
    which seat a live session resolved to depended on dict order, and a
    re-join could not heal a mis-binding because the older key still came
    first (fable audit, 2026-07-24: opus-integrator re-joined and recreated
    his row, but console-design's key preceded it, so his live session kept
    resolving to console-design forever). Now: a CURRENT binding outranks a
    historical one, ties break by name, and a multi-row match is announced
    LOUDLY as the repairable inconsistency it is (`helm chat seat disown`).
    Precedence over a process's OWN validated identity is gone entirely —
    acting_seat asks the process first."""
    hits = seats_for_session(session)
    if len(hits) > 1:
        _warn_once("ambiguous-session:%s" % session,
                   "session %.8s is remembered by %d seats (%s) — resolving to "
                   "%r by binding age. Repair it: `helm chat seat disown "
                   "<wrong-seat> %.8s`\n"
                   % (str(session), len(hits),
                      ", ".join(repr(h) for h in hits), hits[0],
                      str(session)))
    return hits[0] if hits else None
def _resolve_seat(r, token):
    """A roster key, else the seat whose session (or 8+-char prefix of one)
    matches — how the owner names a live agent they only know by sid."""
    if token in r:
        return token
    t = str(token or "")
    if len(t) >= 8:
        for seat, row in r.items():
            sess = [row.get("session") or ""] + list(row.get("sessions") or [])
            if any(s == t or s.startswith(t) for s in sess if s):
                return seat
    return None
_STATE_MARKERS = (".cursor.", ".seen.", ".stopfp.", ".scan.")
def _key_bounded(name, key):
    """Does this state filename belong to THIS seat key? Match only at a
    FIELD BOUNDARY: after '<marker><key>' the name must end or continue with
    '.' (the .k<sid8>/.lock suffixes — _seat_key's slug+hash alphabet never
    contains '.'). A bare substring test cross-fired: seat 'foo' (key
    foo-<h1>) prefix-matched every state file of a seat literally NAMED
    'foo-<h1>' (its key foo-<h1>-<h2>), so pruning/renaming 'foo' unlinked or
    moved the LIVE seat's cursors — the same gc state cross-fire class the
    case-variant fix closed, substring flavor (fable adversarial probe B3)."""
    for m in _STATE_MARKERS:
        probe, i = m + key, 0
        while True:
            i = name.find(probe, i)
            if i < 0:
                break
            end = i + len(probe)
            if end == len(name) or name[end] == ".":
                return True
            i += 1
    return False
def _bounded_sub(name, ok, nk):
    """_key_bounded's boundary law applied to the RENAME substitution: swap
    the key only where it fills a whole '.'-field (bare, or dm-prefixed for
    the dm-lane room segment) — keys/rooms never contain '.' (slug + hash
    alphabets), so '.' is a hard field boundary. The raw str.replace it
    replaces rewrote a ROOM slug that merely EMBEDS the key
    ('<key>-updates.cursor.<key>' -> room segment corrupted), silently
    detaching the cursor from its room (fable adversarial probe C10)."""
    dm_ok, dm_nk = chat.DM_PREFIX + ok, chat.DM_PREFIX + nk
    return ".".join(nk if s == ok else (dm_nk if s == dm_ok else s)
                    for s in name.split("."))
def _move_seat_state(old, new):
    """Carry every state file from the old seat key to the new one — cursors
    (+ per-session variants + locks), .seen, stop latches, every room. The
    tracked delivery ground survives a rename; an EOF re-baseline would be
    silent loss. Fail-open per file."""
    ok, nk = _seat_key(old), _seat_key(new)
    d = chat.chat_dir()
    try:
        os.replace(chat.room_path(chat.DM_PREFIX + ok),   # the private DM lane
                   chat.room_path(chat.DM_PREFIX + nk))   # rides the rename too
    except OSError:
        pass
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if _key_bounded(n, ok):
            try:
                # replace EVERY key occurrence: a dm-lane cursor carries the
                # key twice (dm-<key>.cursor.<key>…) and both must move —
                # the lane file kept its inode, so the cursor stays valid.
                # Segment-bounded (_bounded_sub), never raw str.replace: a
                # room slug embedding the key must keep its room segment.
                os.replace(os.path.join(d, n),
                           os.path.join(d, _bounded_sub(n, ok, nk)))
            except OSError:
                pass
def rename_seat(old, new, whole_row=False):
    """(ok, message). G-stable-names: bind a live agent to a memorable @name.
    `old` is a roster seat name or a session id (full, or an 8+-char prefix).
    Rebinds delivery: the roster row moves (so the hook's session_id resolves
    to the new name) and every keyed state file moves with it. The seat's
    HELM_CHAT_NAME env (if it launched with one) still names the OLD seat —
    the message says so; a beacon armed on the old name must be re-armed.

    THE VERB IS ROW-GRANULAR. A SESSION-ID argument LOOKS pane-granular and is
    not, and that gap destroyed an identity on 2026-07-22T18:05:21Z: a resumed
    console-design pane ran `seat rename <its-sid> console-design` believing it
    rebound only its own pane; _resolve_seat found the sid inside the
    opus-integrator ROW, and `r[new] = r.pop(seat)` moved that ENTIRE row —
    every one of opus-integrator's sessions — under the console-design key. Two
    agents then shared one identity, and for two days the roster showed
    console-design 🟢 fresh off opus-integrator's tool boundaries.

    Two guards, and a receipt (why guards and not a pane-granular split:
    splitting one sid out of a row would mint a SECOND row for the same agent,
    with no home room, no mutes and no cursors — swapping a merge bug for a
    fork bug, in a verb whose whole job is to keep one agent's one row. The
    unambiguous form already exists: rename BY SEAT NAME):
      * a caller that declares its own identity may only rename ITS OWN row;
      * a SESSION-ID target that lands in a row remembering OTHER sessions is
        refused unless `whole_row` (the CLI's `--row`) says the caller means
        the whole row — the message names the row, the seat, and the sessions;
      * every success prints WHAT MOVED, so nobody can believe it did
        something narrower than it did.
    """
    new = (new or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", new):
        return False, ("new name %r must be 1-64 chars of [A-Za-z0-9._-] "
                       "(what an @mention can address)" % new)
    if new.lower() in owner_names() or _BROADCAST.search("@" + new):
        return False, "%r is reserved (an owner/broadcast name)" % new
    with _flocked(roster_path() + ".lock"):
        r = roster()
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
        # unlink onto the live seat (kimi cross-family review, live-probed
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
        r[new] = r.pop(seat)
        pk.write_json(roster_path(), r)
        _move_seat_state(seat, new)
    old_lbl = _seat_label(seat)   # raw key drove r[new]=r.pop(seat); echoed
    return True, ("moved the WHOLE row %s -> %s, carrying %d session%s (%s). "
                  % (old_lbl, new, len(sess), "s"[:len(sess) != 1],
                     ", ".join("%.8s" % s for s in sess))
                  + "@%s now delivers to it. If it armed a "            # old
                  "beacon on the old name, re-arm: Monitor(command: \"helm "  # name
                  "chat wait --seat %s --follow\", persistent: true) — if Monitor "
                  "is not in your surface it is DEFERRED: ToolSearch(query: "
                  "\"select:Monitor\") first. A seat "  # laundered (new is validated
                  "launched with HELM_CHAT_NAME=%s re-registers the old name "  # safe)
                  "on its next session — relaunch to make the rename stick "
                  "there." % (new, new, old_lbl))
def set_mute(seat, room, on=True):
    """(ok, message) — the seat's own beacon filter (the beacon-scope
    premise's tuning control). A muted room stops surfacing home-room
    chatter / @all at this seat; a direct @seat mention or a DM
    ALWAYS still surfaces — mute tunes noise, never direct address. Stored
    on the roster row so every lane (boundary, beacon, stop-guard, report)
    reads one truth."""
    room = pk.slug(room)
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat) or {}
        mute = [m for m in row.get("mute") or [] if m != room]
        if on:
            mute.append(room)
        row["mute"] = sorted(mute)
        r[seat] = row
        pk.write_json(roster_path(), r)
    lbl = _seat_label(seat)   # raw key drove the dict write above; the echoed
    if on:                    # label is laundered (a hostile HELM_CHAT_NAME
        return True, ("%s muted for %s — @%s mentions and DMs still surface "
                      "(unmute: helm chat seat unmute %s)"  # must not reshape
                      % (room, lbl, lbl, room))             # the CLI terminal)
    return True, "%s unmuted for %s" % (room, lbl)
def mutes(seat):
    """The seat's muted rooms, sorted — one roster read."""
    return sorted((roster().get(seat) or {}).get("mute") or [])
def _allowed_rooms(home_room):
    """Current live rooms admitted by one home choice (used only to compute a
    rehome delta; the delivery chokepoint remains _scan_rooms)."""
    try:
        rooms = set(chat.list_rooms()) | {"main"}
    except OSError:
        rooms = {"main"}
    return {home_room, "main"} if home_room else rooms
def room_in_scope(room, row):
    """Whether `room` belongs to a roster row's CURRENT delivery scope.
    Un-homed rows retain all-room legacy scope; a home admits only itself + main.
    Cursor activity is historical, so web presence must intersect it with this."""
    home_room = (row or {}).get("home_room")
    return not home_room or pk.slug(room) in {home_room, "main"}
def _rooms_to_baseline(old, new):
    """Rooms whose pre-transition history must be skipped. A destination home
    is always re-baselined, even when the old un-homed scope could theoretically
    see it; clearing to all rooms baselines only newly admitted foreign rooms."""
    if new and new != old:
        # A homed seat already admits #main. Narrowing that scope to explicit
        # main must preserve pending main traffic; every other destination is
        # newly admitted (including legacy un-homed -> homed, which rebases a
        # potentially stale all-room cursor by design).
        return set() if old and new == "main" else {new}
    return _allowed_rooms(new) - _allowed_rooms(old)
def _baseline_rooms(seat, row, rooms):
    """Baseline newly admitted rooms at their current EOF for the seat and all
    remembered sessions. Rehome changes scope, never replays pre-admission
    history or traffic accumulated while the seat was away."""
    # DEFERRED: the roster<->delivery cycle, closed at its thinner leg
    # (two names, two call sites — measured, not assumed).
    from .seats import _baseline_room_cursors
    sessions = [s for s in ([row.get("session")] + list(row.get("sessions") or []))
                if s]
    for room in rooms:
        _baseline_room_cursors(room, seat, sessions)
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
        r = roster()
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
        if clear:
            return True, ("seat %s re-homed %s -> un-homed (all rooms); "
                          "takes effect on its next delivery scan"
                          % (lbl, old_lbl or "un-homed"))
    return True, ("seat %s re-homed %s -> #%s; delivery is now { #%s, #main } "
                  "— takes effect on its next delivery scan (no relaunch)"
                  % (lbl, old_lbl or "un-homed", home_room, home_room))
