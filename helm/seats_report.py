#!/usr/bin/env python3
"""helm seats — the roster report: what an operator SEES.

THIS IS THE ONE SEATS SURFACE A HUMAN READS DIRECTLY — the CLI table, the
/api/chat/roster payload, and the seats panel all render from here. That is
why every seat name leaving this module goes through _seat_label rather than
raw: this is the last place a roster key can turn into display text, and the
launder tripwire exists because it did.

PRESENCE IS A JUDGEMENT WITH THREE ANSWERS, not a boolean. Fresh, quiet, and
unverified are different claims about different evidence, and collapsing
unverified into absent is what makes an operator relaunch a seat that was
merely unproven. The dots are a glance; the row behind them has to survive
being wrong.

THE ROSTER GC IS helm/seats_gc.py, NOT THIS FILE, and the two share no
module-level name in either direction — a claim about the parse tree, and
the arm that keeps it true is ReportGcSeamTest. The reason to keep them
apart is that they are opposite kinds of thing. A REPORT IS A READ:
everything below deletes nothing and fails open per row, because it has to
be right on a 2-second poll. The GC takes the roster lock and re-probes
under it before it unlinks anything. An auto-reap riding roster_report is
cleanup smuggled into a read, and the file boundary is what makes that
awkward to write rather than natural.
"""

import json
import os
import re
import sys
import time
from collections import namedtuple

from . import chat, home, pk, projscope, record, vcs
from .seats_common import (FRESH_S, MAX_BYTES, PREVIEW_CHARS, QUIET_S,
                           SEAT_BYTES, STATUS_BYTES,
                           UNVERIFIED, _clip, _flocked, _scrub,
                           _seat_label, canonical_seat, recipient_matches,
                           roster, roster_for_write, roster_path, row_aliases)
from .seats_roster import last_seen, roster_acquired, touch_seen
from .seats_stop_signals import _pending_all
from .seats_claims import (claim_holder_listedness, claim_marks,
                           claims_list)

def presence_of(ls):
    if not ls:
        return "absent"
    age = time.time() - ls
    return "fresh" if age < FRESH_S else "quiet" if age < QUIET_S else "absent"
def session_owners(r=None):
    """sid -> sorted seats that remember it. The roster's identity index; a
    healthy roster maps every sid to exactly ONE seat (write_roster enforces
    it going forward — _evict_session)."""
    owners = {}
    for seat, row in (roster() if r is None else r).items():
        if not isinstance(row, dict):
            continue
        for sid in [row.get("session")] + list(row.get("sessions") or []):
            if isinstance(sid, str) and sid:
                owners.setdefault(sid, set()).add(seat)
    return {k: sorted(v) for k, v in owners.items()}
def unverified_seats(r=None):
    """{seat: (sid, [the other claimants])} for every row whose presence is
    NOT ATTRIBUTABLE — a session id it remembers is ALSO remembered by another
    row, so ANY beat on either row could be either process. This is the
    owner-visible half of the 2026-07-24 contamination fix: while two rows
    share an id, every 🟢 painted off `.seen` is a GUESS, and helm must say
    'unverified' rather than guess (the roster showed one seat fresh
    every 2s while another process wore its name). Pure roster arithmetic — no
    I/O past the one roster read the callers already did, so it rides the ~2s
    presence poll. Fail-open: a junk roster reads {}."""
    try:
        r = roster() if r is None else r
        out = {}
        for sid, owners in session_owners(r).items():
            if len(owners) < 2:
                continue
            for s in owners:
                out.setdefault(s, (sid, [o for o in owners if o != s]))
        return out
    except Exception:
        return {}
def presence_with_identity(ls, conflict=None):
    """presence_of, then the HONEST override: a row whose beat cannot be
    attributed to its own process never reads fresh/quiet. An ABSENT row stays
    absent — no beat at all is not an identity question, and unhiding hundreds
    of dead rows would bury the one that matters."""
    p = presence_of(ls)
    return UNVERIFIED if conflict and p != "absent" else p
def identity_warning(conflict):
    """The one owner-legible sentence for an unverified row. `conflict` is
    unverified_seats()'s (sid, others) pair."""
    sid, others = conflict
    return ("presence UNVERIFIED — session %.8s is also on %s, so this row's "
            "beat may be another process's" % (sid, ", ".join(others)))
# the ICQ-style glance: one dot + one line per seat, on every surface (the
# web presence bar, `helm chat seats`, the roster payload) — same truth
PRESENCE_DOTS = {"fresh": "\U0001f7e2",    # 🟢 active at a tool boundary
                 "quiet": "\U0001f7e1",    # 🟡 seated, idle a while
                 UNVERIFIED: "\U0001f7e0",  # 🟠 a beat we cannot attribute
                 "absent": "⚫"}       # ⚫ gone (no recent beat)
def presence_dot(p):
    return PRESENCE_DOTS.get(p, PRESENCE_DOTS["absent"])
def set_status(seat, text, by=None):
    """(ok, message). The seat's explicit one-line status ('what am I on') —
    `helm chat status <line>` / `--clear`. Rides THE roster writer's flock
    (a sibling of write_roster, mutating only the status fields — never a
    second writer path; the homing lane unified writers for a reason).
    Scrubbed + byte-clipped like every roster-borne label. Cross-seat writes
    stay allowed (a coordinator annotating a wedged seat is the point), but
    a writer that isn't the target is RECORDED as status_by — the same
    attribution parity posts have; a self-set carries no by field. The
    presence beat lands on the WRITER (the seat evidently alive is the one
    announcing, not a wedged target being annotated)."""
    if not seat:
        return False, "no seat to set a status on (join first, or --seat S)"
    line = _clip(_scrub(str(text or "")).strip(), STATUS_BYTES) or None
    # `by` is RECORDED, so it is durable ATTRIBUTION and not a label — a
    # derived name here credits a stranger with the write. It arrives as an
    # AdmittedActor from the CLI door; actors.name_of takes the label out of
    # the capability rather than letting `str()` do it, because a capability
    # deliberately stringifies to `<AdmittedActor …>` and not to a seat name.
    from . import actors
    by = _clip(_scrub(str(actors.name_of(by) or "")).strip(), 40) or None
    # The roster status line is the OTHER free-text body under `helm chat` —
    # 160 bytes is ample room for a 40-hex tip, it renders on every roster and
    # console surface, and it does NOT pass through chat.post, so the funnel
    # guard cannot see it. Scanned AFTER scrub+clip, i.e. the exact string that
    # will be stored and read: warning about a token the clip is about to cut
    # off would be a warning about text nobody will ever see. Warn only, before
    # the flock — a guard has no business holding the roster's lock.
    if line:
        from . import shaguard
        shaguard.warn(line)
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        from .seats_roster import roster_checked
        observed, failed = roster_checked()
        if failed:
            return False, ("roster is unreadable — status for %r was not written "
                           "and the unreadable state was left untouched" % seat)
        # THE ROSTER'S OWN SPELLING FOR THIS IDENTITY, never the caller's.
        # Seat identity is casefold-exact — `write_roster` keeps one canonical
        # key per equivalent name and refuses a roster holding two — so an
        # exact-key lookup here made a LIVE seat unaddressable by a spelling
        # every other layer treats as the same seat, and the miss rendered as
        # "no roster row", which is what a seat that never joined looks like.
        target = seat                        # the caller's spelling, for the
        seat, ambiguous = (canonical_seat(seat, observed) if seat
                           else (seat, None))          # message they typed
        if ambiguous:
            return False, ambiguous
        if seat is None or observed.get(seat) is None:
            return False, ("no roster row for %r — sessions join on start "
                           "(helm hooks install wires it); `helm chat status "
                           "--seat <live-seat>` targets an existing one"
                           % target)
        r = roster_for_write()
        row = r.get(seat)
        if row is None:
            return False, "roster changed before status write for %r" % seat
        if line:
            row["status"] = line
            row["status_ts"] = time.time()
            # SAME RELATION FOR THE WRITER. `by != seat` raw would record a
            # seat as the ANNOTATOR OF ITS OWN ROW whenever the two spellings
            # differ, inventing a cross-seat annotation out of capitalisation.
            if by and not recipient_matches(by, seat):
                row["status_by"] = by
            else:
                row.pop("status_by", None)
        else:
            row.pop("status", None)
            row.pop("status_ts", None)
            row.pop("status_by", None)
        r[seat] = row
        pk.write_json(roster_path(), r)
    touch_seen(by or seat)
    lbl = _seat_label(seat)   # raw key drove the write; echoed label laundered
    return True, ("%s ▸ %s" % (lbl, line) if line
                  else "%s status cleared" % lbl)
def _fmt_left(sec):
    sec = max(0, int(sec or 0))
    if sec >= 3600:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dm" % (sec // 60) if sec >= 60 else "<1m"
_WORKTREE_RES = re.compile(r"^worktree:([^:]+):(.+)$")
STATUS_FRESH_S = 4 * 3600   # how long an explicit status outranks LIVE truth:
STATUS_SKEW_S = 300   # clock-skew allowance on status_ts: a ts slightly in
def _status_age(row):
    """Seconds since the explicit status was set, or None (no status, or a
    planted row without a sane status_ts — missing, non-numeric, or dated
    beyond STATUS_SKEW_S into the future)."""
    ts = row.get("status_ts")
    if row.get("status") and isinstance(ts, (int, float)):
        d = time.time() - ts
        if d >= -STATUS_SKEW_S:
            return max(0, int(d))
    return None
def _status_by(row):
    """The recorded cross-seat writer for '(by X)', scrubbed reader-side."""
    b = row.get("status_by")
    return (_clip(_scrub(str(b)).strip(), 40) or None) if b else None
def _fmt_age(sec):
    sec = max(0, int(sec or 0))
    if sec >= 86400:
        return "%dd" % (sec // 86400)
    if sec >= 3600:
        return "%dh" % (sec // 3600)
    return "%dm" % (sec // 60) if sec >= 60 else "<1m"
def status_row_for(who):
    """(row, refusal) for the seat a reader NAMED — the SHOW leg's resolution.

    Beside `set_status` and `status_line` because it answers their question:
    which stored row IS the identity this caller spelled. Keeping it here is
    what stops one verb refusing a spelling its own other leg accepts, and it
    surfaces the ambiguity refusal rather than collapsing it into an absence.
    """
    from .seats_common import seat_row
    return seat_row(who)
def status_line(row, claim=None):
    """(line, source) — the ONE status line every surface shows, composed
    from what already exists. Precedence: a FRESH explicit status (the seat
    said so, within STATUS_FRESH_S) > a live claim (the lease says what it
    holds) > a stale explicit status > the home room (where it lives).
    source ∈ status|claim|home names the winning tier. Reader-side law:
    WHICHEVER tier wins, the line leaves here scrubbed (Cc/Cf incl. bidi,
    Zl/Zp) + clipped — a planted claim resource or roster field must not
    reshape a terminal or reorder the seats table. Never raises: a junk row
    reads '?' (one corrupt row must not blank the whole fleet bar)."""
    try:
        s = str(row.get("status") or "").strip()
        age = _status_age(row)
        fresh = age is not None and age <= STATUS_FRESH_S
        if s and (fresh or not claim):
            line, source = s, "status"
        elif claim:
            left = _fmt_left(claim.get("remaining"))
            m = _WORKTREE_RES.match(str(claim.get("resource") or ""))
            # A DEAD HOLDER IS NOT "WORKING". This is the line that reaches the
            # widest audience — bare `helm chat status`, the roster/presence
            # JSON, the web doing-cells and dashboard tooltips — and it was
            # reporting a dead seat's lock as work in progress, which is the
            # exact condition this lane exists to end. A cross-family read
            # found it after I fixed the two NARROWER surfaces and declared the
            # class closed; the verb `stale` is on the claim, so every renderer
            # that reads a claim has to read it.
            state = claim.get("liveness")
            verb = ("STALE hold on" if state == "stale"
                    else "UNVERIFIED hold on" if state == "unknown"
                    else "working")
            line, source = (("%s lane/%s (%s), %s left"
                             % (verb, m.group(2), m.group(1), left)) if m else
                            "%s %s, %s left"
                            % ("STALE hold on" if state == "stale"
                               else "UNVERIFIED hold on" if state == "unknown"
                               else "holds", claim.get("resource"), left)), "claim"
        elif row.get("home_room"):
            line, source = "in #%s" % row["home_room"], "home"
        else:
            line, source = ("in %s" % row["project"]
                            if row.get("project") else ""), "home"
        return _clip(_scrub(str(line)).strip(), STATUS_BYTES), source
    except Exception:
        return "?", "home"
def _claims_by_holder(cl=None):
    """holder -> its longest-lived live claim (the most work-shaped one)."""
    by = {}
    for c in (claims_list() if cl is None else cl):
        h = c.get("holder")
        if h and (h not in by
                  or (c.get("remaining") or 0) > (by[h].get("remaining") or 0)):
            by[h] = c
    return by
def _is_ephemeral_sa(name, home_room, cwd):
    """An EPHEMERAL review-subagent: auto-named agent-<hex>, no home room, a
    /tmp cwd — a transient fan-out SA (a review/scan SA), not a conversational
    seat. The ONE criterion every surface that hides them shares (the web
    'message a seat' picker AND the fleet-presence 'online' list). Fail-safe:
    any surprise reads False, so a real seat is never mis-hidden."""
    try:
        n = str(name or "")
        rest = n[6:] if n.startswith("agent-") else ""
        auto = bool(rest) and all(c in "0123456789abcdef" for c in rest)
        return auto and not home_room and "/tmp" in str(cwd or "")
    except Exception:
        return False
# THE VENDOR AVAILABILITY CELL. A walled seat printed `absent` and HID by
# default while `helm seat list` called it `pane=live UNUSABLE upstream=<cause>`
# in the same minute. Every RULE about the fact lives in seat_usability, which
# owns it; this module holds the CALL — rules here would fill the split budget.
# A READ THAT DID NOT ANSWER IS UNKNOWN, NEVER THE EMPTY CELL: {} is what a seat
# with no vendor publishes. These cells are built here, because seat_usability
# may be the module that would not import; they equal its own UNKNOWN cells.
#: The whole-roster vendor read that raised, and why. Never a dict, so no seat
#: can be looked up in it and come back with no vendor.
AvailUnread = namedtuple("AvailUnread", "why")


def _avail(roster_rows, snapshot=None):
    """{seat: record} for a roster read, or an AvailUnread when it raised —
    never dies for a cell.
    `snapshot` is the CALLER'S acquisition when this report is half a render."""
    try:
        from . import seat_usability
        return seat_usability.availability_for_roster(roster_rows, snapshot)
    except Exception as exc:                # noqa: BLE001
        from . import record
        record.swallow("seats_report._avail", exc)
        return AvailUnread("the vendor availability read raised %s"
                           % exc.__class__.__name__)


def _avail_unknown_cells(why):
    text = "UNKNOWN ? — %s" % why
    return {"availability": "UNKNOWN", "availability_text": text,
            "availability_family": None, "availability_walled": False,
            "availability_mark": "? " + text}


def _avail_cells(avail, seat):
    """The published keys for one row: {} when no vendor answers for it, the
    UNKNOWN cells when the read did not answer for it."""
    if isinstance(avail, AvailUnread):
        return _avail_unknown_cells(avail.why)
    try:
        from . import seat_usability
        return seat_usability.availability_cells((avail or {}).get(seat))
    except Exception as exc:                # noqa: BLE001
        from . import record
        record.swallow("seats_report._avail_cells", exc)
        return _avail_unknown_cells("the vendor availability cell raised %s"
                                    % exc.__class__.__name__)


# THE MEMORY CELL. A seat throttled by its own memory cgroup keeps its process,
# pane, beacon and presence beat, so this row read it `fresh` while it could not
# take a keystroke. The present-tense reading is seatceiling's; this module
# holds the call and the join by slice name, as it does for the vendor cell.
# A READ THAT DID NOT ANSWER IS UNKNOWN, NEVER THE EMPTY CELL. {} already means
# "read, and nothing to say", so a raise or an incomplete walk folded into it
# would call every seat calm. The failure is this module's fact, not seatceiling's
# (which may be the thing that would not import), so its cells are built here.
#: The memory word for a seat the read could not answer for. seatceiling's
#: UNKNOWN is the same word, spelled there for one slice's unreadable files.
MEM_UNKNOWN = "UNKNOWN"
#: The whole-fleet read that failed, and why. Never a dict, so no seat can be
#: looked up in it and come back calm.
MemUnread = namedtuple("MemUnread", "why")


def _mem_readings():
    """{slice basename: Pressure} for every seat slice on this box, or a
    MemUnread when the read raised or its walk did not complete. Never dies
    for a cell. One acquisition per report."""
    try:
        from . import seatceiling
        got, trouble = seatceiling.fleet_pressure()
        if trouble:
            return MemUnread(trouble)
        return {os.path.basename(path): r for path, r in got.items()}
    except Exception as exc:                # noqa: BLE001
        record.swallow("seats_report._mem_readings", exc)
        return MemUnread("the seat-memory read raised %s"
                         % exc.__class__.__name__)


def _mem_unknown_cells(why):
    return {"mem_pressure": MEM_UNKNOWN,
            "mem_pressure_text": "UNKNOWN now: %s" % why,
            "mem_pressure_mark": "? memory UNKNOWN (%s)" % why,
            "mem_shmem": None}


def _mem_cells(readings, seat):
    """The published memory keys for one row: {} when its slice is calm or it
    has none, the UNKNOWN cells when the read did not answer for it."""
    if isinstance(readings, MemUnread):
        return _mem_unknown_cells(readings.why)
    try:
        from . import seatceiling
        return seatceiling.pressure_cells(
            (readings or {}).get(seatceiling.seat_slice_name(seat)))
    except Exception as exc:                # noqa: BLE001
        record.swallow("seats_report._mem_cells", exc)
        return _mem_unknown_cells("the seat-memory cell raised %s"
                                  % exc.__class__.__name__)


def presence_report():
    """The fleet-wide glance bar: one LIGHT row per roster seat — presence
    dot + the one status line — with zero cursor scans (roster_report walks
    pending; this must stay cheap enough to ride every ~2s web poll). Each row
    carries `ephemeral` so the 'online' list can drop done review-SAs.
    [{seat, presence, dot, last_seen, status, line, source, ephemeral}],
    fresh first."""
    try:
        by = _claims_by_holder()
    except Exception:
        by = {}
    rank = {"fresh": 0, UNVERIFIED: 1, "quiet": 2, "absent": 3}
    out = []
    r = roster()
    unver = unverified_seats(r)
    # THE OWNER TOTALS STRIP READS THIS PAYLOAD and carried no vendor key, so a
    # walled seat fell into its "gone" bucket while the sibling roster endpoint
    # held the fact. One file read per CALL, not per row, buys it the word.
    avail = _avail(r)
    for seat, row in sorted(r.items()):
        try:
            ls = last_seen(seat, row)
            conflict = unver.get(seat)
            p = presence_with_identity(ls, conflict)
            line, source = status_line(row, by.get(seat))
            measured = {"resolved": resolved_route(row),
                        "runtime": row.get("runtime")}
            # the fleet bar is a roster-consuming SURFACE too: every string it
            # ships (seat KEY, status, status_by, line, source) rides the SAME
            # publish boundary as roster_report — presence_report escaping this
            # choke point is exactly how the 5th ESC surface was born (r4). One
            # owner, not a scrub scattered per surface.
            out.append(_pub_row({
                        "seat": seat, "runtime": row.get("runtime"),
                        # WHICH MODEL ANSWERED, on the light bar too: the
                        # fleet bar is where a route change hides if only the
                        # full listing carries it.
                        "resolved": measured["resolved"],
                        "resolved_text": resolved_text(measured),
                        "presence": p, "dot": presence_dot(p),
                        "last_seen": ls, "status": row.get("status"),
                        "status_age": _status_age(row),
                        "status_by": _status_by(row),
                        "line": line, "source": source,
                        "unverified": (conflict[1] if conflict else None),
                        "unverified_session": (conflict[0] if conflict
                                               else None),
                        "warn": (identity_warning(conflict)
                                 if conflict else None),
                        "ephemeral": _is_ephemeral_sa(
                            seat, row.get("home_room"), row.get("cwd")),
                        **_avail_cells(avail, seat)}))
        except Exception:   # per-row fail-open: one junk roster row renders
            out.append(_pub_row({    # '?', it never blanks the whole fleet bar
                "seat": seat, "runtime": None, "resolved": None,
                "resolved_text": "", "presence": "absent",
                "dot": presence_dot("absent"), "last_seen": None,
                "status": None, "status_age": None, "status_by": None,
                "line": "?", "source": "home",
                "unverified": None, "unverified_session": None,
                "warn": None}))
    out.sort(key=lambda s: (rank.get(s["presence"], 3), s["seat"]))
    return out
# per-field byte caps for a roster row's DISPLAY strings; unlisted string
# fields ride the default. seat + session are here too so NO roster-borne
# string — the seat KEY included — reaches an operator terminal unlaundered.
_ROW_CAPS = {"seat": SEAT_BYTES, "session": MAX_BYTES, "project": 80,
             "old": SEAT_BYTES, "until": 40,
             "cwd": 160, "home_room": 40, "home_room_source": 40,
             "status": STATUS_BYTES, "status_by": 40, "line": STATUS_BYTES,
             "source": 40, "preview": PREVIEW_CHARS, "active": STATUS_BYTES,
             "agent_harness": 64, "family": 64, "backend": 64,
             # honest-presence fields: `unverified` is a LIST of seat KEYS (so
             # it takes the seat cap, per element) and `warn` interpolates one
             # of those keys into a sentence — both must clear the same launder
             # bar as every other roster-borne string
             "unverified": SEAT_BYTES, "unverified_session": MAX_BYTES,
             "warn": STATUS_BYTES,
             # the vendor cell: its text and mark are SENTENCES (the UNKNOWN arm
             # quotes proxywatch's own reason), so they take the status bar
             # rather than the 80-byte default, which cuts a reason mid-word
             "availability": 40, "availability_family": 64,
             "availability_text": STATUS_BYTES,
             "availability_mark": STATUS_BYTES,
             # the memory cell: the seat's own slice, read NOW (seatceiling)
             "mem_pressure": 40, "mem_pressure_text": STATUS_BYTES,
             "mem_pressure_mark": STATUS_BYTES}
def _pub_row(d):
    """The ONE publish boundary for a roster row: scrub+clip EVERY string
    field (Cc/Cf incl. bidi, Zl/Zp) at its per-field cap, so no roster-borne
    string — seat KEY, project, cwd, status, status_by, line, source,
    preview, and any FUTURE string field — can reshape an operator terminal
    or reorder the fleet table. RECURSES into nested dicts and lists so a
    nested cell (the todo mirror's `active` text, or any future nested
    surface) is laundered by the same enumeration — not a hand-maintained
    special case that the next nested field would silently escape. Non-string
    values (last_seen, pending, dot, status_age) and falsy strings pass
    through. The stored roster keeps its raw keys (rename/claim match the dict
    itself); only this report copy is laundered — one owner, not a scrub
    scattered across every print site."""
    for k, v in list(d.items()):
        if isinstance(v, str) and v:
            d[k] = _clip(_scrub(v).strip(), _ROW_CAPS.get(k, 80))
        elif isinstance(v, dict):
            _pub_row(v)
        elif isinstance(v, list):
            d[k] = [_pub_row(x) if isinstance(x, dict)
                    else _clip(_scrub(x).strip(), _ROW_CAPS.get(k, 80))
                    if isinstance(x, str) and x else x
                    for x in v]
    return d
def resolved_route(row, session=None):
    """{model, provider, upstream_model} the row's EXACT session proof binds.

    THE ROW-LEVEL `runtime` CANNOT ANSWER THIS. It is the newest-launch
    summary and carries a family, not a route; the provider and the upstream
    id live in the measured proof stamped against one session id. A row with
    no proof answers None, and every surface then prints what it has always
    printed rather than an invented route.
    """
    if not isinstance(row, dict):
        return None
    from .seats_runtime import runtime_entry_for_session
    # A CALLER THAT HOLDS A SESSION ID ASKS ABOUT THAT PROCESS. The row's own
    # `session` is the newest join, and a census walking live processes is
    # asking about a pid it already resolved a session for -- answering that
    # caller from the newest join would label one process with another's
    # route. Default only where the caller has nothing more exact.
    entry = runtime_entry_for_session(row, session or row.get("session"))
    proof = entry.get("proxy_proof") if isinstance(entry, dict) else None
    if not isinstance(proof, dict):
        return None
    from . import proxywatch
    return proxywatch.resolved_from_proof(proof)


def resolved_text(row):
    """The rendered `alias -> provider/upstream (rung)` for one report row.

    RENDERED ONCE, SERVER-SIDE, so the console and the CLI cannot word one
    seat two ways -- the console already carried its own copy of the runtime
    label, and the rung needs the catalog, which no browser can read.
    """
    resolved = row.get("resolved") if isinstance(row, dict) else None
    if not isinstance(resolved, dict) or not resolved:
        return ""
    runtime = row.get("runtime")
    family = (runtime or {}).get("family") if isinstance(runtime, dict) else None
    from . import proxywatch
    return proxywatch.resolved_model_phrase(resolved, family or None)


def runtime_label(row):
    """Compact family + harness/backend + WHICH MODEL ANSWERED; empty legacy.

    The model cell is the point of the label, not a decoration on it: a seat
    named for one model and answering on another rendered identically here
    until the route joined the line. It reads from the measured route when the
    row carries one (`resolved`, projected beside `runtime`), and from the
    runtime's own recorded upstream id when it does not — never from the seat
    name, and never from the claude-side alias alone.
    """
    runtime = row.get("runtime") if isinstance(row, dict) else None
    if not isinstance(runtime, dict):
        return ""
    family = str(runtime.get("family") or "")
    harness = str(runtime.get("agent_harness") or "")
    backend = str(runtime.get("backend") or "")
    route = "/".join(x for x in (harness, backend) if x)
    model = resolved_text(row) or str(runtime.get("model") or "")
    return " · ".join(x for x in (family, route, model) if x)
def roster_report(room="main", availability=None, pressure=None):
    """{"seats": [...], "claims": [...]} — fail-open by caller. Pending is
    computed from each seat's cursor WITHOUT moving it. A report is a READ:
    it deletes NOTHING. (The legacy auto-reap that rode this verb was a
    second cleanup owner, dropping stale rows on presence alone — a
    persisted seat vanished on a poll while gc's evidence probe would have
    kept it. Cleanup has ONE owner now: gc_roster, a verb someone runs.
    Absent rows merely hide behind --all in the surfaces.)

    THE PASS IS OPENED HERE, NOT LEFT TO THE CALLER. Pending is the
    multi-room truth, so the per-row read walks every room the seat could be
    addressed in and asks `chat.list_rooms` ONCE PER ROW — one distinct
    question, N times, and each answer costs a full directory read of a flat
    chat directory whose lock files outnumber its rooms by two orders of
    magnitude. `chat.list_rooms` memoises through `projscope.memo`, but that
    memo is INERT outside a scope: it is a no-op for any caller that has not
    declared a pass. The CLI render declared one and the web poll path did
    not, so the same report cost several times more through the surface the
    owner actually watches — the memo that exists to prevent exactly this
    never engaged on the route that needed it.

    THE BOUNDARY IS THIS FUNCTION BECAUSE THE REPEATS ARE, and because this
    body already promises to be ONE INSTANT: "one acquisition feeds the whole
    report" is the law every other read here is written to, and the derived
    reads underneath it were the one part still asking per row. Wrapping a
    CALLER instead would be wrong in both directions — too narrow, because the
    next caller forgets and silently pays the fan-out again; and too wide,
    because a request handler's scope holds the memo across the vendor
    snapshot, the upstream join and any per-row git that route then does, so a
    room born during that fan-out stays invisible to work that has nothing to
    do with this report. A stale answer is worse than a slow one, so the scope
    ends where the projection does. Entering starts an empty cache and LEAVING
    DROPS IT; a caller that already holds a scope keeps its own, because
    nesting only tightens and only the outermost clears.

    `pressure` is the caller's seat-memory reading ({slice basename:
    Pressure}, or a MemUnread for a read that failed) when it already holds
    one; otherwise the report takes ONE.
    """
    with projscope.scope():
        return _roster_rows(room, availability, pressure)


def _roster_rows(room, availability, pressure=None):
    """`roster_report`'s body. Runs inside that function's memo scope and is
    never called from anywhere else."""
    seats = []
    try:
        cl = claims_list()
    except Exception:
        cl = []
    by_holder = _claims_by_holder(cl)
    # ONE ACQUISITION FEEDS THE WHOLE REPORT — the seat list AND the verdict
    # every consumer needs about it. This used to be roster() here and a
    # separate roster_checked() in each caller, so the two halves of one screen
    # were two reads with a window between them: a roster changing inside that
    # window let a single render list a seat above and call the same holder
    # unlisted below. `r` is byte-identical to what roster() answered — the
    # fail-open rows — so no seat disappears on a malformed row; what is new is
    # that `roster_failed` travels WITH them.
    r, roster_failed = roster_acquired()
    unver = unverified_seats(r)
    # ONE read for the vendor cell, and the CALLER'S when it has one: two
    # acquisitions in one render put two instants on one row.
    avail = _avail(r, snapshot=availability)
    mem = _mem_readings() if pressure is None else pressure
    for seat, row in sorted(r.items()):
        try:
            # pending is the MULTI-ROOM truth (the owner's panel must show a
            # helm-dogfood mention, not just main), read off the row's newest
            # session cursor (hook joins are session-keyed) with the
            # seat-level fallback — cursors never move here.
            hits = _pending_all(
                room, seat, session=row.get("session"), scan_lane="report")
            pending, preview = len(hits), None
            if hits:
                preview = _scrub(hits[-1][1].get("text") or "")[:PREVIEW_CHARS]
            ls = last_seen(seat, row)
            # the seat's CURRENT task, pulled (never pushed) off the todo
            # mirror — what turns "who is here" into "who is working on what".
            try:
                from . import todos as _todos
                todo = _todos.seat_digest(row)
            except Exception:
                todo = None              # fail-open: a roster read never 500s
            line, source = status_line(row, by_holder.get(seat))
            conflict = unver.get(seat)
            p = presence_with_identity(ls, conflict)
            measured = {"resolved": resolved_route(row),
                        "runtime": row.get("runtime")}
            seats.append(_pub_row({
                          "seat": seat, "session": row.get("session"),
                          # every LIVE rename alias with its expiry — the
                          # roster listing is where an operator learns an
                          # old name still answers, and until when
                          "aliases": [{"old": old, "until": pk.epoch_ts(when)}
                                      for old, when in row_aliases(seat, r)],
                          "runtime": row.get("runtime"),
                          "resolved": measured["resolved"],
                          "resolved_text": resolved_text(measured),
                          "runtime_verified": row.get("runtime_verified") is True,
                          "project": row.get("project"),
                          "cwd": row.get("cwd"),
                          "home_room": row.get("home_room"),
                          "home_room_source": row.get("home_room_source"),
                          "last_seen": ls, "presence": p,
                          "dot": presence_dot(p), "status": row.get("status"),
                          "status_age": _status_age(row),
                          "status_by": _status_by(row),
                          "line": line, "source": source,
                          "unverified": (conflict[1] if conflict else None),
                          "unverified_session": (conflict[0] if conflict
                                                 else None),
                          "warn": (identity_warning(conflict)
                                   if conflict else None),
                          "pending": pending, "preview": preview,
                          "todo": todo,
                          **_avail_cells(avail, seat),
                          **_mem_cells(mem, seat)}))
        except Exception:   # per-row fail-open (the same law as the bar): a
            seats.append(_pub_row({  # junk row reads '?', never kills the report
                "seat": seat, "session": None, "runtime": None,
                "resolved": None, "resolved_text": "",
                "runtime_verified": False,
                "project": None, "cwd": None,
                "home_room": None, "home_room_source": None,
                "last_seen": None, "presence": "absent",
                "dot": presence_dot("absent"), "status": None,
                "status_age": None, "status_by": None,
                "line": "?", "source": "home",
                "unverified": None, "unverified_session": None, "warn": None,
                "pending": 0, "preview": None, "todo": None}))
    # LISTEDNESS IS STAMPED HERE, WHERE THE ONE READ LIVES, so all three
    # surfaces answer from the same bytes: the CLI claims verb, the CLI roster
    # verb, and the /api/chat/roster payload the owner's web console draws. Two
    # of them were cured and the third served claims with no verdict at all,
    # which kept the contradiction alive on the surface the owner actually
    # looks at. Any consumer computing this for itself would be a second read
    # by another name.
    #
    # THE SNAPSHOT HANDED DOWN IS THE **CHECKED** ONE — ({} when the read
    # failed) — because that is the contract claim_holder_listedness was
    # written against: it must not find a holder among rows a failed read
    # cannot vouch for. The fail-open rows stay upstairs for the seat list,
    # which is exactly the split this one acquisition exists to serve.
    snap = ({}, True) if roster_failed else (r, False)
    for c in (cl or []):
        try:
            c["listedness"] = claim_holder_listedness(c, snap)
        except Exception:     # noqa: BLE001 — a marker never takes the report
            c["listedness"] = "unmeasurable"      # (or the owner's page) down
    return {"room": room, "seats": seats, "claims": cl,
            "roster_failed": bool(roster_failed)}
def empty_roster_ending(failed, has_claims):
    """An empty seat list has THREE endings; return (message, is_terminal).

    "EMPTY" IS A CLAIM, AND A READ THAT FAILED CANNOT MAKE IT. The render
    already prints THE ROSTER DID NOT VALIDATE above this line, and then
    printed "no seats yet" underneath it — a confident empty directly beneath
    the sentence saying nothing could be read (task/914 item 3, reproduced
    with a top-level `[]` and with `0`). The coherence fix I wrote for this
    screen left standing the one branch it existed to correct.

    ONLY THE VALIDATED, CLAIMLESS ENDING IS TERMINAL. The other two fall
    through to the claims section on purpose: an empty roster must never HIDE
    a live claim, and an unreadable one is exactly when every holder reads
    unlisted and the contradiction is at its widest.

    A pure decision so the three-way rule can be tested without a terminal:
    the branch that was wrong here was wrong in the RULE, not in the printing.
    """
    if failed:
        return ("THE SEAT LIST IS UNKNOWN, not empty — the roster did not "
                "validate, so nothing on this screen can call a seat absent",
                False)
    if has_claims:
        return ("no seats on the roster — the claims below are held by "
                "nobody it lists", False)
    return ("no seats yet — sessions join on their next start "
            "(helm hooks install wires it)", True)


def render_roster(room, show_all):
    """The `helm chat seats` screen — the CLI table this module's own docstring
    already claims it renders. -> exit code.

    IT LIVES HERE BECAUSE ITS EVIDENCE DOES. Every helper this render reaches
    for — roster_report, empty_roster_ending, runtime_label, presence_dot,
    _fmt_age, _seat_label — is defined in this file, and the contradiction the
    screen exists to prevent (a seat listed above a claim reading ROSTER
    UNREADABLE) is a disagreement between two halves of ONE acquisition. A
    renderer a module away from the reader that produces its keys is how a
    legend and its data drift apart; seats_ack.render_pending left the same
    dispatcher for the same reason.

    `show_all` rather than `args`: the caller owns CLI grammar, this owns the
    screen. Nothing here can misparse a flag because nothing here sees one.
    """
    # ONE PASS FOR THE WHOLE SCREEN, extending the one-acquisition rule
    # below the roster: the per-row helpers still asked per ROW.
    with projscope.scope():
        # ONE ACQUISITION FEEDS BOTH HALVES OF THIS SCREEN. roster_report
        # needs the RAW rows (malformed ones are its subject) and the marks
        # need the VALIDATED view plus the failed bit; taking those from two
        # READS is what let a seat render listed above and reported absent —
        # or ROSTER UNREADABLE — on the line below. Four rounds of this lane
        # were half-fixes that each left one of the two readers behind.
        # ONE ACQUISITION FOR THE WHOLE RENDER, AND IT IS THE REPORT'S. A
        # SECOND roster_checked() sat here for the marks, so the seat list and
        # the marks read the roster at two different instants — this lane's own
        # contradiction, inside its cure. roster_report reads once and stamps
        # each claim's listedness from that read; the marks take it off the row.
        rep = roster_report(room)
        # A FAILED PROBE MUST NARRATE THE WHOLE SCREEN. Otherwise the list
        # names a seat while every claim beside it reads ROSTER UNREADABLE —
        # the same contradiction, one read narrated two ways.
        if rep.get("roster_failed"):        # the read FAILED
            print("  ⚠ THE ROSTER DID NOT VALIDATE — the rows below are RAW "
                  "and unverified, and no claim beside them can be judged "
                  "listed or unlisted. Fix the roster before trusting either "
                  "half of this screen.")
        rows = rep["seats"]
        hidden = 0
        if not show_all:             # absent rows hide by default (junk rows
            # A WALLED SEAT IS NEVER HIDDEN — the owner's ruling. `absent` is a
            # PRESENCE beat verdict; a measurably UNAVAILABLE seat is the row he
            # asked to see, and it sat behind "N absent seats hidden" holding dozens
            # of addressed rows. Hiding that while the wall holds the mail is the
            # defect, not the presence verdict.
            shown = [s for s in rows
                     if s["presence"] != "absent"
                     or s.get("availability_walled")]
            hidden = len(rows) - len(shown)          # leave via seat gc only)
            rows = shown
        if not rows and not hidden:
            # THE THREE ENDINGS LIVE IN empty_roster_ending, WHERE THEY CAN BE
            # TESTED WITHOUT A TERMINAL: the branch that contradicted the
            # banner directly above was wrong in the RULE, not in the printing
            # (task/914 item 3). Only the validated-and-claimless one returns.
            msg, terminal = empty_roster_ending(bool(rep.get("roster_failed")),
                                                bool(rep["claims"]))
            print("helm chat: " + msg)
            if terminal:
                return 0
        display_rows = []
        for s in rows:
            runtime = runtime_label(s)
            label = "%s [%s]" % (s["seat"], runtime) if runtime else s["seat"]
            display_rows.append((s, label))
        w = max([len(label) for _s, label in display_rows] or [0])
        for s, label in display_rows:
            scope = "#" + s["home_room"] if s.get("home_room") else "all"
            source = " (%s)" % s["home_room_source"] \
                if s.get("home_room_source") else ""
            # the todo cell rides the existing row (who is working on what) —
            # `helm todos --all` is the full pull surface
            t = s.get("todo") or {}
            task = (" · %s (%d/%d)" % (t["active"][:44], t["done"], t["total"])
                    if t.get("active") else
                    " · %d/%d done" % (t["done"], t["total"]) if t else "")
            # the same one-line status the web presence bar shows (fresh
            # explicit status > live claim > stale status > home) — home-tier
            # is already the row. An explicit line carries its age (a 2-day-
            # old away message must READ as 2 days old) + the cross-seat
            # writer where one was recorded.
            extra = ""
            if s.get("source") == "status":
                if s.get("status_age") is not None:
                    extra = " (%s)" % _fmt_age(s["status_age"])
                if s.get("status_by"):
                    extra += " (by %s)" % s["status_by"]
            line = (" ▸ %s%s" % (s["line"], extra)
                    if s.get("line") and s.get("source") != "home" else "")
            # the honest-presence surface: an UNVERIFIED row SAYS so, names the
            # other claimant, and names the one verb that clears it — the owner
            # is GUI/CLI-first, so a state only agents can compute is not done
            warn = (" ⚠ %s — `helm chat seat disown %s %s`"
                    % (s["warn"], s["seat"],
                       (s.get("unverified_session") or "")[:8])
                    if s.get("warn") else "")
            alias = "".join(" · was @%s until %s" % (a["old"], a["until"])
                            for a in s.get("aliases") or ())
            # ON THE ROW, not a footer: matching rows to a footer by hand is how
            # the FAMILY-DARK lines stayed unactionable beside this table. Rendered
            # by the predicate's module, so no two surfaces word one seat apart.
            mark = s.get("availability_mark") or ""
            vendor = ("  " + mark) if mark else ""
            # THE THROTTLE ON THE ROW, beside the vendor word: a seat frozen by
            # its own memory cgroup reads `fresh` on every other column.
            if s.get("mem_pressure_mark"):
                vendor += "  " + s["mem_pressure_mark"]
            print("  %s %-*s  %-10s  pending %-3d %s · home %s%s%s%s%s%s%s" % (
                s.get("dot") or presence_dot(s["presence"]),
                w, label, s["presence"], s["pending"],
                (s.get("project") or ""), scope, source, alias, line, task, warn,
                vendor))
        if hidden:
            print("  (%d absent seat%s hidden — --all shows them; `helm chat "
                  "seat gc` prunes evidence-free rows)"
                  % (hidden, "s"[:hidden != 1]))
        for c in rep["claims"]:
            # The roster is the surface the fleet actually reads, so the stale
            # verdict has to be HERE and not only in the claims data. A dead
            # holder's lock is indistinguishable from a working seat's until
            # something says so out loud.
            print("  claim: %s -> %s (%ds left, fence %s)%s" % (
                c["resource"], c["holder"], c["remaining"], c["fence"],
                claim_marks(c)))
        # THE SELF-CONTRADICTION IS NAMED, not left for the reader to notice.
        # This render prints a seat list and then prints claims, and those two
        # halves read different state: measured — it listed five
        # seats and then named two OTHER seats holding leases — two working
        # holders absent from the list directly above them. Both
        # halves were honest; the screen was not. Marking each line is what a
        # reader scanning claims sees, and this count is what a reader scanning
        # the SEAT LIST sees, which is the half that looked complete.
        # COUNT WHAT IT DISPLAYS: this counted CLAIMS and printed deduped
        # holders, so one seat with two leases read "2 holders ... (one name)".
        # NO SNAPSHOT ARGUMENT: the verdict is stamped ON the row by
        # roster_report, from the same acquisition that built the seat list —
        # so this COUNT and the per-line marks cannot disagree on one screen.
        gaps = sorted({_seat_label(c["holder"]) for c in rep["claims"]
                       if claim_holder_listedness(c) == "unlisted"})
        if gaps:
            print("  ⚠ %d claim holder%s above %s NOT in the seat list "
                  "(%s) — the list is incomplete, not the claims; nothing can "
                  "dispatch to them until they join"
                  % (len(gaps), "s"[:len(gaps) != 1],
                     "is" if len(gaps) == 1 else "are", ", ".join(gaps)))
        return 0


def render_status(who):
    """The bare `helm chat status` read — one seat's current line. -> exit code.

    ONLY THE READ. The WRITE half stays with the dispatcher because it is an
    authority decision (resolve_actor_reason, status_write_refused), not a
    rendering; splitting on that line keeps this function free of any judgement
    about who may write, which is the half that must never be duplicated.

    `who` arrives resolved. Taking a session here would import `_env_session`
    from the dispatcher and close a cycle for one lookup the caller already
    holds — the reason seats_ack.render_pending takes `session` too.
    """
    row, ambiguous = status_row_for(who)
    if ambiguous:        # rc 2: a repair state is not an absent row
        print("helm chat: " + ambiguous, file=sys.stderr)
        return 2
    if row is None:
        print("helm chat: no roster row for %r yet" % who,
              file=sys.stderr)
        return 1
    line, source = status_line(row, _claims_by_holder().get(who))
    extra = ""
    if source == "status":
        age, sb = _status_age(row), _status_by(row)
        if age is not None:
            extra = " (%s)" % _fmt_age(age)
        if sb:
            extra += " (by %s)" % sb
    print("helm chat: %s %s ▸ %s (%s)%s" % (
        presence_dot(presence_of(last_seen(who, row))),
        _seat_label(who), line or "—", source, extra))
    return 0
