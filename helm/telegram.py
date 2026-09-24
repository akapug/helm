#!/usr/bin/env python3
"""helm telegram — the owner's phone as a TWO-WAY surface, off the fleet.

WHY THIS IS NOT JUST A SECOND NOTIFIER. `helm/notify.py` applies the rule that
an alarm about the fleet being unreachable must not depend on a fleet member
being reachable. That supplies the OUTBOUND half: the owner can be told. The
INBOUND half cannot use chat, beacons, panes, or a web console bound to
127.0.0.1 because they rely on fleet-local reachability. On a phone, that
loopback address names the phone, not this box. Orca panes may span daemon
generations, so the shared failure boundary is the fleet-local route rather
than one daemon common to every pane.

This module is the inbound half of that symmetry, and it is the reason to
prefer Telegram over another push topic. Free-text replies matter beyond
structured decision cards, and this surface is DELIBERATELY not
attention-budgeted the way a room is: it is a place the owner chooses to look,
not a place that interrupts him.

CONFIG IS A KEY NAME, NEVER A VALUE, exactly as notify.py holds it: the bot
token is a capability — anyone holding it can speak AS the fleet to the owner —
so nothing here returns it, prints it, or puts it in an error string.
`configured()` answers the only question a caller may ask. UNSET is a
deliberate opt-out that makes NO network call.

FAIL-OPEN, ALWAYS, for the same reason notify.py is: a notifier must never
break the verb it reports on. Every error is one journal line and a False
return, where False means "not delivered" so an outbox caller keeps the edge
armed for its next pass.

THE INBOUND LEG IS PULL, NEVER A WEBHOOK. A webhook needs a public endpoint,
which this box does not have and should not grow; `poll()` is one bounded
getUpdates call a timer drives. That also keeps the inbound path honest about
the founding rule: it reaches the owner's reply without any fleet member being
alive, because Telegram holds the queue, not us.
"""
import os

from . import home

TIMEOUT_S = 6          # per-socket-op; long-poll uses its own bounded wait
API = "https://api.telegram.org/bot%s/%s"
MAX_BODY = 3800        # Telegram hard-caps a message at 4096 UTF-16 units


def _token():
    """The bot token, or None when the owner has opted out.

    PRIVATE BY DESIGN, and more sharply than ntfy's topic: a topic can only
    push, while this token can also READ every message the bot can see and
    SPEAK AS the fleet. It never leaves this module."""
    return (home.env("TELEGRAM_TOKEN") or "").strip() or None


def _chat_id():
    """The owner's DM chat. A push with no destination is not an error, it is
    an opt-out — same law as notify._endpoint returning None."""
    return (home.env("TELEGRAM_CHAT_ID") or "").strip() or None


def configured():
    """Is there a phone at the end of this channel, in BOTH directions?

    Deliberately one predicate rather than two. A half-configured bridge — a
    token with no chat id — can neither deliver nor route a reply, and a
    caller that believed the outbound half alone would raise an alarm nobody
    receives, which is exactly the half-working alarm notify.py exists to
    forbid."""
    return _token() is not None and _chat_id() is not None


def _api(method, payload, timeout=TIMEOUT_S):
    """One bounded API call -> (ok, result). Never raises."""
    tok = _token()
    if tok is None:
        return False, None
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            API % (tok, method),
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        return bool(doc.get("ok")), doc.get("result")
    except Exception:                        # noqa: BLE001 — never blocks a verb
        return False, None


def owner_send(body, title=None, receipt=None, reply_key=None):
    """Send ONE message to the owner -> delivered.

    True  — delivered, or deliberately opted out (nothing to retry).
    False — the call failed; an outbox caller keeps the edge armed.

    `reply_key` is an opaque routing token echoed in the message footer. A
    Telegram REPLY to that message carries the original text, so `poll` can
    read the key back and route the answer to the seat that asked — which is
    what makes free text work without a second addressing scheme. The key is
    a seat name, never a secret: it is rendered into a message on the owner's
    phone and must survive being read by anyone holding it."""
    if not configured():
        return True                          # deliberately opted out
    text = str(body)
    if title:
        text = "*%s*\n%s" % (title, text)
    if reply_key:
        text = "%s\n\n⤷ reply to answer %s" % (text, reply_key)
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + "\n… (truncated)"
    ok, _res = _api("sendMessage", {"chat_id": _chat_id(), "text": text,
                                    "disable_notification": False})
    if not ok and receipt:
        try:
            from . import pk
            pk.event(receipt[0], receipt[1], "owner telegram send failed")
        except Exception:                    # noqa: BLE001 — best-effort receipt
            pass
    return ok


def _offset_path():
    return os.path.join(home.global_dir(), "telegram-offset.json")


def _read_offset():
    from . import pk
    got = pk.read_json(_offset_path(), None)
    return got.get("offset") if isinstance(got, dict) else None


def _write_offset(offset):
    from . import pk
    pk.write_json(_offset_path(), {"offset": int(offset)})


def poll(limit=20, commit=True):
    """One bounded getUpdates pass -> [{text, reply_key, msg_id, update_id}].

    ACKNOWLEDGE AFTER ACTING, NOT AFTER READING. Telegram re-delivers anything
    below the acknowledged offset, which makes the cursor the only thing
    standing between a crash and a lost reply — and a lost reply from the one
    person who cannot see that it failed is the worst edge this bridge has.
    At-least-once is the right side to err on: a duplicated reply is visible
    and annoying, a lost one is invisible.

    So `commit` is a real choice, not a flag. commit=True suits a caller whose
    action IS the read (a status glance). A caller that will DO something with
    these rows passes commit=False and calls `ack` once the doing succeeded;
    then a crash mid-route re-delivers instead of silently swallowing. A dry
    run must also pass commit=False, or it destroys the very rows it was
    supposed to preview."""
    if not configured():
        return []
    payload = {"timeout": 0, "limit": int(limit)}
    off = _read_offset()
    if off is not None:
        payload["offset"] = off
    ok, result = _api("getUpdates", payload)
    if not ok or not isinstance(result, list):
        return []
    out, high = [], None
    mine = str(_chat_id())
    for upd in result:
        high = max(high or 0, int(upd.get("update_id") or 0))
        msg = upd.get("message") or upd.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) != mine:
            continue                         # never accept input from elsewhere
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        parent = (msg.get("reply_to_message") or {}).get("text") or ""
        key = None
        for line in parent.splitlines():
            if line.startswith("⤷ reply to answer "):
                key = line.split("answer ", 1)[1].strip() or None
        out.append({"text": text, "reply_key": key,
                    "msg_id": msg.get("message_id"),
                    "update_id": upd.get("update_id")})
    if commit and high is not None:
        _write_offset(high + 1)
    return out


def ack(rows):
    """Advance the cursor past rows that have been ACTED ON -> acknowledged.

    The second phase of `poll(commit=False)`. Takes the rows the caller
    actually handled, so a partial pass acknowledges only what it finished —
    and note this acknowledges past the HIGHEST update id present, because
    Telegram's cursor is a single offset and cannot express a hole. A caller
    that handled row 5 but not row 4 must not call this with row 5."""
    ids = [r.get("update_id") for r in (rows or [])
           if isinstance(r.get("update_id"), int)]
    if not ids:
        return False
    _write_offset(max(ids) + 1)
    return True


USAGE = ("telegram status|send <text> [--reply-key SEAT]|poll [--apply] "
         "[--room R] — the owner's phone as a two-way surface")


def _route(row, room=None):
    """Turn ONE inbound reply into a chat row -> (kind, target, text).

    A reply carrying a routing key becomes a DM to the seat that asked, so an
    answer lands where the question was raised rather than in a room the asker
    may not be watching. Free text with no key becomes an ordinary post — that
    is the common case, because the owner mostly just types, and a message
    dropped for lacking a key is a message he watched himself send into
    nothing."""
    from . import chat, seats
    text = row["text"]
    key = row.get("reply_key")
    who = seats.owner_name()
    if key:
        chat.post(text, who=who, dm=key, origin="telegram")
        return "dm", key, text
    chat.post(text, room=room, who=who, origin="telegram")
    return "post", room or "main", text


def cmd_telegram(args):
    """telegram status|send|poll — the owner's phone as a two-way surface."""
    import sys

    from .cli import guard_tail
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

    if verb == "status":
        rc = guard_tail("helm telegram status", rest, usage=USAGE)
        if rc is not None:
            return rc
        # NEVER the values: the token can speak AS the fleet to the owner and
        # READ every message the bot sees. Which HALVES are bound is the only
        # question a caller may ask, and it is the one that gets debugged.
        tok = _token() is not None
        chat_id = _chat_id() is not None
        print("token:   %s" % ("bound" if tok else "UNSET"))
        print("chat id: %s" % ("bound" if chat_id else "UNSET"))
        print("bridge:  %s" % ("live" if configured() else
                               "opted out (both halves required)"))
        off = _read_offset()
        print("cursor:  %s" % ("at %d" % off if off is not None
                               else "unset (nothing acknowledged yet)"))
        return 0 if configured() else 1

    if verb == "send":
        # NO guard_tail here, deliberately, and for `coach`'s reason: the tail
        # IS the message. Guarding it would junk any word the owner wrote that
        # happens to start with a dash. The failure mode is bounded and
        # self-announcing — a stray token arrives as text on his phone, where
        # he can see it — unlike a dropped flag on `poll`, which would silently
        # change what the verb did.
        key = None
        if "--reply-key" in rest:
            i = rest.index("--reply-key")
            key = rest[i + 1] if i + 1 < len(rest) else None
            del rest[i:i + 2]
        body = " ".join(rest).strip()
        if not body:
            print(USAGE, file=sys.stderr)
            return 2
        if not configured():
            print("helm telegram: bridge not configured — nothing sent",
                  file=sys.stderr)
            return 1
        if not owner_send(body, reply_key=key):
            print("helm telegram: send FAILED", file=sys.stderr)
            return 1
        print("sent")
        return 0

    if verb == "poll":
        # Junk REFUSES before anything runs. Silently ignoring an unknown token
        # is how a verb answers a different question than the one asked — the
        # `helm chat pending <selector>` incident, where a dropped selector
        # printed the caller's own rows and misled a live investigation.
        rc = guard_tail("helm telegram poll", rest, flags=("--apply",),
                        valued=("--room",), usage=USAGE)
        if rc is not None:
            return rc
        apply = "--apply" in rest
        room = None
        if "--room" in rest:
            i = rest.index("--room")
            room = rest[i + 1] if i + 1 < len(rest) else None
        if not configured():
            print("helm telegram: bridge not configured — nothing to poll",
                  file=sys.stderr)
            return 1
        # commit=False on BOTH paths: a dry run must stay repeatable, and the
        # apply path acknowledges only what it actually routed, so a crash
        # mid-loop re-delivers the remainder instead of swallowing it.
        rows = poll(commit=False)
        if not rows:
            print("no replies pending")
            return 0
        done = []
        for row in rows:
            key = row.get("reply_key")
            where = ("-> dm %s" % key) if key else ("-> %s" % (room or "main"))
            print("%s  %s" % (where, row["text"][:120]))
            if apply:
                _route(row, room=room)
                done.append(row)
        if apply:
            ack(done)
            print("\nrouted %d, acknowledged" % len(done))
        else:
            print("\nDRY RUN: printed, not posted, NOT acknowledged — these "
                  "rows are still queued, so --apply will see them again.")
        return 0

    print(USAGE, file=sys.stderr)
    return 2
