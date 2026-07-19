#!/usr/bin/env python3
"""helm chat — the human-included groupchat: one shared conv log + notify +
read/write loop, owner in the room.

Rooms live in RAM (tmpfs): /dev/shm/helm-chat/<room>.jsonl — append-only, one
JSON object per line {"ts","from","text"}, dir 0700, default room "main".
HELM_CHAT_DIR overrides (tests point it at a tmp dir). This is ephemeral
presence-chat, NOT the durable record — /premise anything that must outlive
the room; past SIZE_CAP the oldest half rotates out (RAM etiquette).

The notify loop: the owner's web post drops <room>.owner-unread (the message
count at post time); the shipped owner-chat-unread reflex fires on that marker
every turn until a `helm chat read` consumes past it — so every local agent
SEES the human within one turn, through the already-installed inject hooks.

The owner's orca pane sidecar is exactly: helm chat read --follow
"""
import json
import os
import sys
import time

from . import home, pk

DEFAULT_DIR = "/dev/shm/helm-chat"
SIZE_CAP = 2 * 1024 * 1024  # per-room rotation threshold — RAM etiquette
POLL_S = 2.0                # --follow poll cadence (the web panel matches)


def chat_dir():
    """HELM_CHAT_DIR else the RAM room dir — env read through home.env."""
    return home.env("CHAT_DIR") or DEFAULT_DIR


def room_path(room="main"):
    return os.path.join(chat_dir(), pk.slug(room) + ".jsonl")


def marker_path(room="main"):
    return os.path.join(chat_dir(), pk.slug(room) + ".owner-unread")


def _ensure_dir():
    d = chat_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)  # presence-chat is the operator's — never group-readable
    return d


def whoname():
    """$HELM_CHAT_NAME, else the best local identity guess: session, then user."""
    name = home.env("CHAT_NAME")
    if name:
        return name
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID")
    if sid:
        return "agent-" + sid[:8]
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "anon"


def post(text, room="main", who=None):
    """Append one message; returns it. O(1) append; rotation only past the cap."""
    _ensure_dir()
    path = room_path(room)
    msg = {"ts": pk.now_ts(), "from": who or whoname(), "text": text}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    _rotate(path)
    return msg


def _rotate(path, cap=None):
    """Past the cap, keep the NEWEST half (atomic). The dropped half was
    presence-chat, not a record; pollers' since counters self-heal (read()
    resets a past-the-end since)."""
    cap = SIZE_CAP if cap is None else cap
    try:
        if os.path.getsize(path) <= cap:
            return False
    except OSError:
        return False
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    pk.atomic_write(path, "".join(x + "\n" for x in lines[len(lines) // 2:]))
    return True


def _msg(raw):
    try:
        m = json.loads(raw)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def read(room="main", since=0):
    """(messages[since:], total) — the ONE poll primitive: the CLI read,
    --follow and the web GET all sit on this. A since past the end (the room
    rotated) resets to 0 so a poller re-syncs instead of starving; unparseable
    lines are skipped, never fatal."""
    try:
        with open(room_path(room), encoding="utf-8", errors="replace") as f:
            raw = f.read().splitlines()
    except OSError:
        return [], 0
    msgs = [m for m in map(_msg, raw) if m]
    total = len(msgs)
    return msgs[since if 0 <= since <= total else 0:], total


def mark_owner_unread(room="main"):
    """The owner posted (the web surface calls this): drop the marker carrying
    the message count at post time — the shipped reflex fires on its existence."""
    _ensure_dir()
    pk.atomic_write(marker_path(room), str(read(room)[1]))


def consume(room="main", total=None):
    """A read reached `total` messages — clear the owner-unread marker once the
    reader has seen past the owner's post. True iff cleared."""
    mp = marker_path(room)
    try:
        with open(mp) as f:
            mark = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if total is not None and total < mark:
        return False
    try:
        os.remove(mp)
    except OSError:
        return False
    return True


def _fmt(m):
    ts = str(m.get("ts") or "")
    return "%s %s: %s" % (ts[11:16] or "--:--", m.get("from") or "?",
                          m.get("text") or "")


def _follow(room, since=0):
    """Poll-print loop — the orca pane sidecar. Ctrl-C exits clean. The read
    primitive it loops on is read() (unit-tested); the loop itself is not."""
    try:
        while True:
            msgs, total = read(room, since)
            for m in msgs:
                print(_fmt(m), flush=True)
            consume(room, total)
            since = total
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_chat(args):
    """chat post <text...> | read [--since N] [--follow] | rooms  [--room R]"""
    args = list(args or [])
    room = "main"
    if "--room" in args:
        i = args.index("--room")
        if i + 1 >= len(args):
            print("helm chat: --room wants a name", file=sys.stderr)
            return 2
        room = args[i + 1]
        del args[i:i + 2]
    verb = args[0] if args else "read"
    if verb == "post":
        text = " ".join(args[1:]).strip()
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not text:
            print("usage: helm chat post <text...> [--room R]", file=sys.stderr)
            return 2
        print("helm chat [%s] %s" % (room, _fmt(post(text, room))))
        return 0
    if verb == "read":
        since = 0
        if "--since" in args:
            try:
                since = int(args[args.index("--since") + 1])
            except (IndexError, ValueError):
                print("helm chat: --since wants an integer", file=sys.stderr)
                return 2
        if "--follow" in args:
            return _follow(room, since)
        msgs, total = read(room, since)
        for m in msgs:
            print(_fmt(m))
        if not msgs:
            print("helm chat [%s]: no messages — post one: helm chat post <text>" % room)
        consume(room, total)
        return 0
    if verb == "rooms":
        d = chat_dir()
        names = sorted(n[:-6] for n in os.listdir(d)
                       if n.endswith(".jsonl")) if os.path.isdir(d) else []
        if not names:
            print("helm chat: no rooms yet — helm chat post <text> starts main")
            return 0
        for n in names:
            msgs, total = read(n)
            unread = " [owner-unread]" if os.path.exists(marker_path(n)) else ""
            last = ("  last: " + _fmt(msgs[-1])) if msgs else ""
            print("  %s  %d msg%s%s%s" % (n, total, "s"[:total != 1], unread, last))
        return 0
    print("helm chat: unknown subcommand '%s' (post|read|rooms)" % verb,
          file=sys.stderr)
    return 2
