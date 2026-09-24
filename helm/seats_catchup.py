#!/usr/bin/env python3
"""helm seats — deliberate, accountable parking of one seat's chat backlog."""

import json
import os
import sys

from . import chat, pk
from .seats_common import (_seat_key, dm_lane, names_match)
from .seats_identity import (_mention_re, deliverable, seat_names, seat_scope)
from .seats_cursor import (_all_cursor_locks, _commit_cursor_updates,
                           _cursor_locks, _cursor_state,
                           _cursor_transaction_epoch, cursor_path,
                           normalize_rotated_cursor, parse_cursor_path,
                           seat_state_lock)
from .seats_delivery import _scan_rooms


def _addressed(m, seat, names=None):
    """Is this row a DIRECT ask of `seat` — dm, @mention, or reply-to-me?
    Every direct ask must also be deliverable. Reaction additions deliberately
    sit outside this catchup/ACTED tier: they wake the target author as an
    attention signal, but never become work the author must acknowledge.
    `names` is seat_names(seat, scope) — the seat and its live rename
    aliases; None resolves it (one roster read) for the single-row callers."""
    names = seat_names(seat) if names is None else names
    if m.get("dm"):
        return (names_match(m["dm"], names)
                or bool(seat) and str(m.get("room") or "") == dm_lane(seat))
    text = str(m.get("text") or "")
    if any(_mention_re(n).search(text) for n in names):
        return True
    return names_match(m.get("rfrom"), names)


def _catchup_cursor_paths(seat):
    """Exact on-disk delivery+wake paths by room from the canonical parser."""
    key, out = _seat_key(seat), {}
    for name in os.listdir(chat.chat_dir()):
        parsed = parse_cursor_path(os.path.join(chat.chat_dir(), name))
        if parsed is None or parsed["seat_key"] != key:
            continue
        out.setdefault(parsed["room"], set()).add(parsed["path"])
    return out


def _catchup_snapshot(room):
    """One room frozen against append/rotation: target state + ordered rows."""
    with chat._room_lock(room):
        try:
            with open(chat.room_path(room), "rb") as f:
                st = os.fstat(f.fileno())
                data = f.read()
        except OSError:
            return (None, None, 0, None), []
    entries, pos, rid = [], 0, None
    for chunk in data.split(b"\n"):
        end = pos + len(chunk) + 1
        if end > len(data):
            break
        row = None
        try:
            v = json.loads(chunk.decode("utf-8", errors="replace"))
            if isinstance(v, dict):
                row = v
                rid = row.get("id") or rid
        except ValueError:
            pass
        if chunk:
            entries.append((row, end))
        pos = end
    return (st.st_dev, st.st_ino, pos, rid), entries


def _catchup_start(cur, target, entries):
    """Effective current-file boundary for one exact cursor record."""
    dev, ino, size, _rid = target
    off = cur.get("off", 0)
    same = (dev, ino) == (cur.get("dev"), cur.get("ino")) \
        and isinstance(off, int) and 0 <= off <= size
    suppress = cur.get("skip") if same else cur.get("rid")
    if not suppress:
        return off if same else 0
    floor = off if same else 0
    for row, end in entries:
        if end > floor and row is not None and row.get("id") == suppress:
            return end
    return 0


def _catchup_cursor(path):
    epoch, pending = _cursor_transaction_epoch(path)
    if pending:
        with _cursor_locks({path}):
            pass
        epoch, pending = _cursor_transaction_epoch(path)
    if pending:
        raise OSError("cursor transaction pending")
    row = pk.read_json(path, None)
    after, pending = _cursor_transaction_epoch(path)
    if pending or after != epoch:
        raise OSError("cursor transaction changed during read")
    return row


def _catchup_pending(room, seat):
    """Complete pending union across every exact cursor, one scan per room."""
    primary = room or "main"
    sc = seat_scope(seat)
    paths = _catchup_cursor_paths(seat)
    ppaths = {p for p in paths.get(pk.slug(primary), set())
              if not parse_cursor_path(p)["beacon"]} | {cursor_path(primary, seat)}
    tracked = sc["tracked"] or any(
        isinstance(_catchup_cursor(p), dict) for p in ppaths)
    out, targets = [], {}
    rooms = [primary] if room else _scan_rooms(
        primary, seat=seat, scope=sc, bounded=False)
    for r in rooms:
        rpaths = {p for p in paths.get(pk.slug(r), set())
                  if not parse_cursor_path(p)["beacon"]} | {cursor_path(r, seat)}
        curs = [c for c in (_catchup_cursor(p) for p in rpaths)
                if isinstance(c, dict) and isinstance(c.get("off"), int)]
        if not curs:
            if not ((tracked and r != primary) or r.startswith(chat.DM_PREFIX)):
                continue
            curs = [{"off": 0}]
        target, entries = _catchup_snapshot(r)
        start = min(_catchup_start(c, target, entries) for c in curs)
        rows = [m for m, end in entries if end > start and m is not None
                and deliverable(m, seat, r, sc)]
        if not rows:
            continue
        targets[r] = target
        out.extend((r, m) for m in rows)
    return out, targets


def catchup(seat, room=None, apply=False, session=None,
            include_addressed=False):
    """Park the seat's OWN pending backlog, deliberately and accountably.

    Mute and catchup are different acts over one decision: MUTE silences the
    ambient wake (mentions pierce, by law) while the backlog stays pending —
    correctly, because a direct ask is an obligation. This verb is the
    missing decision: "seen, park it." Measured 2026-07-28: the integrator
    muted a flooded room and its stop-guard kept re-arming on the churning
    backlog; the mute worked, the guard worked, and the decision being made
    had no verb.

    THREE LAWS, each from a live incident:
      - SELF-ONLY: a seat parks its own backlog, never another seat's —
        parking someone else silently hides THEIR obligations.
      - ADDRESSED ROWS NEED THE FLAG (the integrator's affected-party
        verdict, pinned by the catchup tests): catchup is used reflexively, and
        ambient noise is what "I have seen this pile" means; a direct ask is
        precisely what it does not mean. Default parks ambient only; a room
        whose pending contains addressed rows is REFUSED in default mode —
        cursors are single offsets, so parking "around" an addressed row is
        positionally impossible, and pretending otherwise would park it.
      - PARKING AN ADDRESSED ROW LEAVES A DURABLE TRACE readable by its
        senders: a row posted INTO the parked room naming who parked how
        many asks from whom. Withholding never removes access — nor
        accountability. (Senders are named, not @mentioned: the trace must
        be readable, not a wake storm.)

    Parked rows are never deleted or hidden — they remain readable via
    `helm chat read`; only delivery cursors move. On apply, per room, one
    act: seat-level and every session-scoped cursor of THIS seat to the same
    classified room snapshot, the stop-guard fingerprint latch cleared. Rows
    appended after that snapshot stay pending rather than being swept silently.
    """
    report = {"rooms": {}, "applied": bool(apply), "parked": 0, "held": 0,
              "retry": 0, "unknown": False}
    try:
        pend, targets = _catchup_pending(room, seat)
    except OSError:
        report["unknown"] = True
        return report
    by_room = {}
    for r, m in pend:
        by_room.setdefault(r, []).append(m)
    parking = {}
    names = seat_names(seat)        # once per report, not once per row
    for r in sorted(by_room):
        rows = by_room[r]
        addressed = [m for m in rows if _addressed(m, seat, names)]
        info = {"rows": [{"from": m.get("from"),
                          "text": str(m.get("text") or "")[:80],
                          "addressed": _addressed(m, seat, names)}
                         for m in rows],
                "addressed": len(addressed)}
        report["rooms"][r] = info
        if addressed and not include_addressed:
            info["held"] = len(rows)     # positional truth: all or nothing
            report["held"] += len(rows)
            continue
        info["parking"] = len(rows)
        report["parked"] += len(rows)
        if not apply:
            continue
        if addressed:
            # the durable trace, posted BEFORE the cursors move so a crash
            # between the two leaves over-accounting, never a silent park
            senders = {}
            for m in addressed:
                frm = str(m.get("from") or "?")
                senders[frm] = senders.get(frm, 0) + 1
            named = ", ".join("%d from %s" % (n, chat._dsan(f))
                              for f, n in sorted(senders.items()))
            chat.post("catchup: %s parked %d addressed row%s without "
                      "answering (%s). The rows remain above, readable — "
                      "this trace is the accountability, not the answer."
                      % (chat._dsan(str(seat)), len(addressed),
                         "s"[:len(addressed) != 1], named),
                      room=r, who=seat, ambient=True)
            # THE TRACE IS A WRITE TO THE ROOM WE ARE PARKING, so the snapshot
            # taken above it is now stale — and `_catchup_write_cursor` guards
            # freshness by (dev, ino), which an atomic replace changes on every
            # post. Left un-refreshed this makes catchup DEFEAT ITSELF: measured
            # 2026-08-14, every `--including-mentions --apply` reported "N rows
            # NOT safely parked — retry catchup", six retries in a row, on a
            # room whose inode was otherwise stable for seconds at a time. The
            # prescribed remedy could never converge, because each retry posted
            # the trace again.
            #
            # The post-BEFORE-cursors order above is deliberate and preserved (a
            # crash between the two must over-account, never silently park), so
            # the cure is to re-derive rather than to reorder. Parking past our
            # OWN trace is correct: it is accountability we authored, not an ask
            # we owe an answer to.
            fresh, _entries = _catchup_snapshot(r)
            targets[r] = fresh
        parking[r] = targets[r]
    if not apply or not parking:
        return report

    # One room transaction: base delivery+wake first, then every exact session
    # pair discovered while the base locks freeze initialization. Validate the
    # snapshot once, prepare every update, and commit all-or-rollback.
    applied = {}
    for r, target in parking.items():
        ok = False
        try:
            with _all_cursor_locks(r, seat) as paths:
                try:
                    st = os.stat(chat.room_path(r))
                except OSError:
                    st = None
                if st is not None and (st.st_dev, st.st_ino) == target[:2] \
                        and st.st_size >= target[2]:
                    updates = {}
                    for path in paths:
                        cur = normalize_rotated_cursor(
                            r, pk.read_json(path, None), path=path)
                        cur = cur if isinstance(cur, dict) \
                            and isinstance(cur.get("off"), int) else {}
                        if (cur.get("dev"), cur.get("ino")) == target[:2] \
                                and target[2] <= cur.get("off", -1) <= st.st_size:
                            updates[path] = cur
                            continue
                        updates[path] = _cursor_state(
                            target, active=cur.get("active"), base=target[2])
                    ok = _commit_cursor_updates(updates)
        except OSError:
            report["unknown"] = True
        if ok:
            applied[r] = target
            continue
        info = report["rooms"][r]
        n = info.pop("parking")
        info["retry"] = n
        report["parked"] -= n
        report["retry"] += n
    parking = applied
    with seat_state_lock(seat, session=session) as current:
        if not current:
            report["unknown"] = True
            return report
        try:
            names = os.listdir(chat.chat_dir())
        except OSError:
            report["unknown"] = True
            return report
        latch = ".stopfp.%s" % _seat_key(seat)
        rooms = {pk.slug(r) for r in parking}
        for name in names:
            if latch not in name:
                continue
            r, suffix = name.rsplit(latch, 1)
            if r not in rooms or suffix and not suffix.startswith("."):
                continue
            try:
                os.remove(os.path.join(chat.chat_dir(), name))
            except OSError:
                pass                          # no stale block state survives
    return report


def render_catchup(out, seat):
    """Narrate one catchup() result to an operator. -> exit code.

    THE LEGEND BELONGS BESIDE THE READER THAT PRODUCES IT. `catchup` above
    decides what parked, held, retry and unknown MEAN; this says those words
    out loud. Held a file away in the dispatcher, the two can be changed
    independently and the screen goes on describing the old states with full
    confidence — the failure seats_ack.render_pending names in its own
    docstring after the identical move.

    RETRY AND UNKNOWN EXIT NON-ZERO. Neither means "nothing to do": one says
    the room moved under the apply, the other that the cursor census was
    unreadable, and a caller that cannot tell either from success will believe
    it parked a backlog it never claimed.
    """
    if not out["parked"] and not out["held"] and not out["retry"] \
            and not out["unknown"]:
        print("helm chat catchup: nothing pending for %s — nothing to "
              "park" % seat)
        return 0
    for r, info in sorted(out["rooms"].items()):
        if info.get("retry"):
            state = "RETRY — room changed during catchup; nothing was claimed"
        elif info.get("held"):
            state = ("HELD — %d addressed row(s); answer them or add "
                     "--including-mentions" % info["addressed"])
        else:
            state = "parking %d" % info.get("parking", 0)
        print("  [%s] %s" % (r, state))
        for m in info["rows"]:
            print("    %s%s: %s" % (
                "@" if m["addressed"] else " ",
                chat._dsan(str(m.get("from") or "?")),
                str(m.get("text") or "")[:70]))
    if out["applied"] and out["parked"]:
        print("helm chat catchup: parked %d row%s for %s — cursors at "
              "the classified snapshot, stop-guard latch cleared, addressed "
              "parks traced in-room. The rows remain readable: helm chat read"
              % (out["parked"], "s"[:out["parked"] != 1], seat))
    elif not out["applied"] and out["parked"]:
        print("helm chat catchup: DRY RUN — %d row%s would be parked"
              "%s; add --apply."
              % (out["parked"], "s"[:out["parked"] != 1],
                 (", %d held" % out["held"]) if out["held"] else ""))
    elif out["held"]:
        print("helm chat catchup: nothing parked — every pending room "
              "holds addressed rows. Answer them, or park deliberately "
              "with --including-mentions (leaves an in-room trace).")
    if out["retry"]:
        print("helm chat catchup: %d row%s NOT safely parked during "
              "apply — retry catchup" %
              (out["retry"], "s"[:out["retry"] != 1]), file=sys.stderr)
    if out["unknown"]:
        print("helm chat catchup: UNKNOWN — cursor census unreadable; "
              "nothing unclassified was claimed", file=sys.stderr)
    return 1 if out["retry"] or out["unknown"] else 0
