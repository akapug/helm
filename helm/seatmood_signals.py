#!/usr/bin/env python3
"""helm seat mood — the measured inputs (task/3899, office floor slice 1).

EVERY INPUT IS READ FROM THE READER THAT ALREADY OWNS IT. Nothing here keeps a
new record of what a seat did; it reads the trace helm already writes:

  refusals    the friction ledger (`friction.read`): one row per guard
              refusal, carrying the guard and a reason TOKEN (event:tool),
              never the refused command. So "the same refused command in a
              row" is read as the same guard with the same token in a row.
  loops       the seat's own hook record (`record.counters`): `loop-streak`
              (a command re-run while still in the recent-hash window),
              `stuck-streak` (failing action calls in a row) and
              `stalled-turns` (turns with no forward op). Counters older than
              the window are not now's, and read as zero.
  idle        `seat_idle.reading`: BUSY, IDLE, RESTING or UNKNOWN, with the
              time the current turn opened.
  progress    four sources, each read once for every seat asked about:
                tasks       a comment the seat wrote, a task it owned closed
                dispatches  a verdict or hold on a row sent to the seat, a
                            row the seat sent, and a LAND: a row the seat
                            sent closed landed or source-clean landed
                lanes       a commit off trunk on a lane the seat holds the
                            lease on (`worktree:<project>:<lane>` claims)
                lands       a trunk merge "merge lane X (... author SEAT ...)"
                            in helm's own repository: the subject auto-land
                            wrote before task/4033; a subject names no seat
                            now, so the ledger's landed close credits a land
  wall        the family's burn flag RED on MONEY or REACH (`burnflags`), the
              seat's own last turn ending on a typed wall (`turnwall`), or its
              proxy pool refusing (`poolwall`) — darkmove's judgement, read
              per seat.
  owner       an OPEN decision card the seat filed (`ownerasks`): it waits on
              the owner.
  context     a proxy seat's context fill (`autocompact.read`), the number
              that wedges it at 100%.

A SOURCE THAT CANNOT BE READ IS NAMED, NEVER READ AS SILENCE. Each reader
returns its reason, the reason goes into `unread`, and the judge says so
beside any reading it would have changed. An unreadable progress source is not
"no progress".

WHAT IT CANNOT SEE, named. A merge subject is read from helm's own repository
only; a land in any project is credited by its row's landed close. A
lease that expired and was swept no longer names its holder. A native Claude
seat has no context gauge here: Claude Code compacts it itself.
"""
import os
import re
import time

from . import home, pk

#: Refusals, loops and counters count over this window.
WINDOW_S = 60 * 60
#: How far back a progress event is looked for.
PROGRESS_LOOKBACK_S = 24 * 3600
#: The git reads' bound, in seconds.
GIT_TIMEOUT_S = 10

_SEAT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_FAMILY = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,31}\Z")
_AUTHOR = re.compile(r"\bauthor ([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
_MERGE = re.compile(r"\bmerge lane (\S+)")
#: The closes that say a row's work landed (`dispatches.CLOSE_REASONS`).
LANDED = ("landed", "source-clean-landed")
_NATIVE = ("claude", "anthropic", "opus", "fable", "sonnet", "haiku")
COUNTERS = ("loop-streak", "stuck-streak", "stalled-turns")


def key(seat):
    """The one comparison spelling of a seat name: casefold."""
    return str(seat or "").casefold()


def _num(value):
    """A finite epoch from a number field, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _credit(out, who, at, what):
    """Keep the LATEST progress event per seat."""
    if who and at is not None and (who not in out or at > out[who][0]):
        out[who] = (at, what)


def _why(exc):
    return "%s: %s" % (type(exc).__name__, text(exc, 120))


def text(value, cap=200):
    """Free text from a ledger, inert on a terminal: controls, format and
    separator characters dropped, then clipped."""
    return pk.launder(str(value if value is not None else ""), keep="")[:cap]


# ------------------------------------------------------------------ roster

def read_roster():
    """(rows, why): THE ONE ROSTER READ of this module and `seatmood`.

    A ROSTER KEY LEAVES ONLY AS A SEAT TOKEN. The roster is written
    unvalidated, and every name the mood prints comes from here or from a
    ledger, so `live_seats` drops a key that is not a seat token and
    `seatmood._roster_key` refuses one: a key carrying a control or bidi
    character never reaches a terminal or a JSON reader."""
    from .seats_roster import roster_checked
    try:
        rows, failed = roster_checked()
    except Exception as exc:                    # noqa: BLE001 — named
        return {}, "the roster could not be read (%s)" % type(exc).__name__
    return ({}, "the roster could not be read") if failed else (rows, None)


def is_seat(name):
    return isinstance(name, str) and bool(_SEAT.match(name))


def live_seats():
    """(names, roster rows, why). A seat is live while its presence is not
    absent (fresh, quiet or unverified). `why` set means the roster could not
    be read: never an empty floor."""
    from .seats_report import presence_with_identity, unverified_seats
    from .seats_roster import last_seen
    rows, why = read_roster()
    if why:
        return None, None, why
    unverified = unverified_seats(rows)
    names = [s for s, row in rows.items() if is_seat(s)
             and isinstance(row, dict)
             and presence_with_identity(last_seen(s, row),
                                        unverified.get(s)) != "absent"]
    return sorted(names, key=key), rows, None


# ------------------------------------------------------------------ refusals

def _refusals(keys, now):
    """({seat: [{at, guard, reason}] oldest first}, why) inside WINDOW_S."""
    from . import friction
    rows, unreadable = friction.read()
    if unreadable:
        return {}, "friction ledger (%s)" % unreadable
    out = {}
    for r in rows:
        who = key(r.get("seat"))
        at = pk.parse_ts_epoch(r.get("ts"))
        if who in keys and at is not None and now - WINDOW_S <= at <= now:
            out.setdefault(who, []).append(
                {"at": float(at), "guard": str(r.get("guard") or "?"),
                 "reason": r.get("reason")})
    for rows_ in out.values():
        rows_.sort(key=lambda r: r["at"])
    return out, None


# ------------------------------------------------------------------ counters

def _counters(session, now):
    """The seat's loop counters, zero when older than the window."""
    zero = {k: 0 for k in COUNTERS}
    if not session:
        return zero
    from . import record
    c = record.counters(session)
    at = pk.parse_ts_epoch(c.get("ts")) if isinstance(c, dict) else None
    if at is None or now - at > WINDOW_S:
        return zero
    out = {}
    for k in COUNTERS:
        v = c.get(k)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) \
            and v > 0 else 0
    return out


# ------------------------------------------------------------------ progress

def _task_progress(keys, since, forward=None):
    """A comment the seat wrote, or a task it owned closed, since `since`."""
    from . import tasks
    rows, why = tasks.snapshot()
    if why:
        return {}, "tasks ledger (%s)" % why
    out = {}
    for tid, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        for c in row.get("comments") or ():
            if isinstance(c, dict):
                at, who = _num(c.get("ts")), key(c.get("by"))
                if who in keys and at is not None and at >= since:
                    _credit(out, who, at, "a comment on %s" % text(tid, 40))
        if row.get("status") == "closed":
            at, who = _num(row.get("last_updated")), key(row.get("owner"))
            if who in keys and at is not None and at >= since:
                label = "closed %s" % text(tid, 40)
                _credit(out, who, at, label)
                if forward is not None:
                    _credit(forward, who, at, label)
    return out, None


def _dispatch_progress(keys, since, forward=None):
    """A verdict or hold on a row sent to the seat, a row the seat sent, or
    a row the seat sent closed as landed.

    A LAND IS CREDITED HERE, from the ledger (task/4033): a merge subject
    names no author any more, so the land request's author, the seat that
    sent the row, is credited when the row closes landed or source-clean
    landed, as forward progress.

    The genesis is the ledger's own (`dispatches._new_state`, the reader
    `repo_ids` uses): a first event it declines opens nothing."""
    from . import dispatches, eventledger
    events, why = eventledger.checked_events(dispatches.ledger_path())
    if why:
        return {}, "dispatch ledger (%s)" % why
    to, by, out = {}, {}, {}
    for ev in events:
        rid = str(ev.get("id") or "")
        if rid not in to:
            try:
                state = dispatches._new_state(ev)
            except Exception:                   # noqa: BLE001 — opens nothing
                state = None
            if not state:
                continue
            to[rid] = key(state.get("recipient"))
            at = pk.parse_ts_epoch(ev.get("ts"))
            sender = key(ev.get("sender"))
            by[rid] = (sender, text(ev.get("lane") or rid[:8], 80))
            if sender in keys and at is not None and at >= since:
                _credit(out, sender, float(at), "a dispatch on %s"
                        % by[rid][1])
            continue
        kind = ev.get("event")
        if kind == "close" and ev.get("close_reason") in LANDED:
            at = pk.parse_ts_epoch(ev.get("ts"))
            who, lane = by[rid]
            if who in keys and at is not None and at >= since:
                label = "a land of lane %s" % lane
                _credit(out, who, float(at), label)
                if forward is not None:
                    _credit(forward, who, float(at), label)
            continue
        if kind not in ("verdict", "hold"):
            continue
        at = pk.parse_ts_epoch(ev.get("ts"))
        who = key(ev.get("hold_actor")) if kind == "hold" else ""
        who = who or to.get(rid)
        if who in keys and at is not None and at >= since:
            label = "a %s on %s" % (kind, text(rid[:8]))
            _credit(out, who, float(at), label)
            if forward is not None and kind == "verdict":
                _credit(forward, who, float(at), label)
    return out, None


def _project_roots():
    """{project token: repository root}: every registered project, and helm's
    own checkout. The token is the root's basename, as a lane claim names it
    (`work._lanes.project_token`)."""
    from . import registry
    out = {}
    projects = (registry.load() or {}).get("projects") or {}
    for rec in projects.values():
        path = rec.get("path") if isinstance(rec, dict) else None
        if isinstance(path, str) and path:
            out.setdefault(os.path.basename(path.rstrip(os.sep)), path)
    own = _helm_repo()
    out.setdefault(os.path.basename(own.rstrip(os.sep)), own)
    return out


def _helm_repo():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(repo, *args):
    from . import vcs
    return vcs.backend(repo).text(repo, *args, timeout=GIT_TIMEOUT_S)


def _lane_progress(keys, since, forward=None):
    """A commit off trunk on a lane the seat holds the lease on."""
    from .seats_common import claims_path
    try:
        claims = pk.read_json(claims_path(), {}, strict=True) or {}
    except Exception as exc:                    # noqa: BLE001 — named below
        return {}, "lane leases (%s)" % _why(exc)
    held = {}
    for resource, row in claims.items():
        parts = str(resource).split(":", 2)
        if len(parts) != 3 or parts[0] != "worktree" \
                or not isinstance(row, dict):
            continue
        who = key(row.get("holder"))
        if who in keys:
            held.setdefault(parts[1], {})[parts[2]] = who
    if not held:
        return {}, None
    roots, out, failed = _project_roots(), {}, []
    from . import vcs
    for token, lanes in sorted(held.items()):
        root = roots.get(token)
        if not root or not os.path.isdir(root):
            continue                    # a project this host does not carry
        try:
            trunk = vcs.backend(root).trunk_ref(root)
            rc, listing, _err = _git(root, "for-each-ref",
                                     "--no-merged=" + trunk,
                                  "--format=%(refname)%09%(committerdate:unix)",
                                  "refs/heads/lane/")
        except Exception as exc:                # noqa: BLE001 — named below
            failed.append("%s (%s)" % (token, _why(exc)))
            continue
        if rc != 0:
            failed.append("%s (git rc %s)" % (token, rc))
            continue
        for line in (listing or "").splitlines():
            ref, _tab, when = line.partition("\t")
            lane = ref[len("refs/heads/lane/"):]
            who = lanes.get(lane)
            if who and when.strip().isdigit() and int(when) >= since:
                label = "a commit on lane %s" % text(lane, 80)
                _credit(out, who, float(int(when)), label)
                if forward is not None:
                    _credit(forward, who, float(int(when)), label)
    return out, ("lane commits in %s" % ", ".join(failed) if failed else None)


def _land_progress(keys, since, repo=None, trunk=None, forward=None):
    """A trunk merge of a lane whose subject names the seat as its author:
    the subjects written before task/4033. A subject names no seat now, so
    `_dispatch_progress` credits a land from the row's landed close."""
    repo = repo or _helm_repo()
    try:
        if trunk is None:
            from . import vcs
            trunk = vcs.backend(repo).trunk_ref(repo)
        rc, merges, _err = _git(repo, "log", "--first-parent", trunk,
                              "--since=@%d" % int(since),
                              "--format=%ct%x09%s")
    except Exception as exc:                    # noqa: BLE001 — named below
        return {}, "lands (%s)" % _why(exc)
    if rc != 0:
        return {}, "lands (git log rc %s)" % rc
    out = {}
    for line in (merges or "").splitlines():
        when, _tab, subject = line.partition("\t")
        lane, author = _MERGE.search(subject), _AUTHOR.search(subject)
        who = key(author.group(1)) if author else ""
        if lane and who in keys and when.isdigit() and int(when) >= since:
            label = "a land of lane %s" % text(lane.group(1).rstrip(":,;"), 80)
            _credit(out, who, float(int(when)), label)
            if forward is not None:
                _credit(forward, who, float(int(when)), label)
    return out, None


#: (name, reader(keys, since) -> ({seat: (at, what)}, why)).
SOURCES = (
    ("tasks ledger", _task_progress),
    ("dispatch ledger", _dispatch_progress),
    ("lane commits", _lane_progress),
    ("lands", _land_progress),
)


def progress(keys, now, forward=None):
    """Latest activity and unread sources; optionally accumulate the latest
    qualifying forward operation separately so later comments/holds cannot
    erase an intervening land, commit, close or verdict."""
    since = now - PROGRESS_LOOKBACK_S
    out, unread = {}, []
    for name, reader in SOURCES:
        try:
            got, why = reader(keys, since, forward=forward)
        except Exception as exc:                # noqa: BLE001 — named
            got, why = {}, "%s (%s)" % (name, _why(exc))
        if why:
            unread.append(why)
        for who, (at, what) in (got or {}).items():
            _credit(out, who, at, what)
    return out, unread


# ------------------------------------------------------------------ walls

def family(seat, row=None):
    """The seat's provider family in the burn flags' spelling, or None."""
    from . import burnflags
    from . import seat as seatmod
    row = row if isinstance(row, dict) else {}
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) \
        else None
    try:
        fam, err = seatmod.family_for(str(seat), runtime,
                                      row.get("runtime_verified") is True)
    except Exception:                           # noqa: BLE001 — no family
        fam, err = None, "unresolved"
    if err or not fam:
        fam = (runtime or {}).get("family")
    fam = str(fam or "").strip().lower()
    if not _FAMILY.match(fam):
        return None             # a roster label that is not a family name
    return burnflags.NATIVE_FAMILY if fam in _NATIVE else fam


def _hm(epoch):
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def wall(seat, fam, flags, now):
    """{source, family, axis, why} for a seat that cannot take a turn, else
    None. The family flag first, then the seat's own last turn, then its
    proxy pool. A reader that raises reads as no wall (each one says the
    same about itself)."""
    from . import burnflags
    flag = (flags or {}).get(fam) if fam else None
    axes = (flag or {}).get("axes") or {}
    for axis in ("money", "reach"):
        if axes.get(axis) == burnflags.RED:
            return {"source": "family", "family": fam, "axis": axis,
                    "why": text(flag.get("cause") or "no cause recorded")}
    if not fam or fam == burnflags.NATIVE_FAMILY:
        return None
    try:
        from . import turnwall
        turn = turnwall.seat_wall(seat, now=now)
    except Exception:                           # noqa: BLE001
        turn = None
    if turn:
        return {"source": "turn", "family": fam,
                "axis": text(turn.get("axis") or "?", 16),
                "why": text(turn.get("label") or turn.get("code") or "?")}
    try:
        from . import poolwall
        pool, _why_ = poolwall.seat_wall(seat, fam, now=now)
    except Exception:                           # noqa: BLE001
        pool = None
    if pool and _num(pool.get("expires_at")):
        return {"source": "pool", "family": fam, "axis": "pool",
                "why": "no credential until %s" % _hm(pool["expires_at"])}
    return None


def _flags():
    from . import burnflags
    try:
        flags, _age = burnflags.cached_flags()
    except Exception:                           # noqa: BLE001 — no snapshot
        return {}
    return flags or {}


# ------------------------------------------------------------------ owner

def _cards(keys):
    """({seat: oldest OPEN decision card it filed}, why)."""
    from . import ownerasks
    cards, why = ownerasks.decisions_snapshot()
    if why:
        return {}, "decision cards (%s)" % why
    out = {}
    for cid, card in (cards or {}).items():
        if not isinstance(card, dict) or card.get("status") != "open":
            continue
        who = key(card.get("asker"))
        since = pk.parse_ts_epoch(card.get("ts"))
        if who in keys and (who not in out or (since or 0) < (
                out[who]["since"] or 0)):
            out[who] = {"id": text(cid, 16),
                        "title": text(card.get("title") or "", 80),
                        "since": since}
    return out, None


# ------------------------------------------------------------------ context

def context_pct(seat, fam):
    """A proxy seat's live context fill in percent, else None."""
    from . import burnflags
    if not fam or fam == burnflags.NATIVE_FAMILY:
        return None
    try:
        from . import autocompact
        row = autocompact.read(seat)
    except Exception:                           # noqa: BLE001 — no gauge
        return None
    pct = row.get("pct") if isinstance(row, dict) else None
    if row.get("status") in ("stale", "unknown-seat") \
            or isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return float(pct)


# ------------------------------------------------------------------ the join

def _idle(seat, rows, roster_why, now):
    from . import seat_idle
    try:
        r = seat_idle.reading(seat, now=now, rows=rows, roster_why=roster_why)
    except Exception as exc:                    # noqa: BLE001 — UNKNOWN
        r = {"state": "UNKNOWN", "why": _why(exc)}
    return {"state": r.get("state"), "idle_s": r.get("idle_s"),
            "turn_opened": r.get("turn_opened"), "last_call": r.get("last_call"),
            "why": r.get("why"), "session": r.get("session")}


def inputs(seats, now, rows=None):
    """({seat key: raw inputs}, unread) for every seat in `seats`, one read
    per source. `rows` is a roster read the caller already holds; None reads
    it here (an unreadable roster leaves each idle reading UNKNOWN)."""
    from . import seatmood
    names = [str(s) for s in seats if _SEAT.match(str(s or ""))]
    keys = {key(s) for s in names}
    roster_why = None
    if rows is None:
        rows, roster_why = read_roster()
    unread = [text(roster_why)] if roster_why else []
    refusals, why = _refusals(keys, now)
    if why:
        unread.append(why)
    forward = {}
    prog, progress_unread = progress(keys, now, forward=forward)
    unread.extend(progress_unread)
    cards, why = _cards(keys)
    if why:
        unread.append(why)
    flags, out = _flags(), {}
    for name in names:
        k = key(name)
        row = next((v for s, v in (rows or {}).items() if key(s) == k), None)
        idle = _idle(name, rows, roster_why, now)
        fam = family(name, row)
        out[k] = {"seat": name, "refusals": refusals.get(k, []),
                  "counters": _counters(idle.get("session"), now),
                  "idle": idle, "progress": prog.get(k),
                  "forward_progress": forward.get(k),
                  "progress_unread": [text(u) for u in progress_unread],
                  "wall": wall(name, fam, flags, now), "card": cards.get(k),
                  "context_pct": context_pct(name, fam),
                  "self": seatmood.self_report(name),
                  "unread": [text(u) for u in unread]}
    return out, [text(u) for u in unread]
