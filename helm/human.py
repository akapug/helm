#!/usr/bin/env python3
"""helm --human — the operator's TUI (stdlib curses only). v1 scope per the
PRD: the chat room (scrollback + live follow + an input line posting as the
operator) + a one-line status strip (transport signed/unsigned + chain head,
quota headline from CACHED creds only — never a probe). Runs beautifully as
an orca pane; exits clean on q (empty input) or Ctrl-C.

Pane-extensible by construction: the model is plain data and every pane is a
pure render function (model, width) -> lines; the curses shell only stacks
pane output and feeds keys. Tests drive the model/render functions headless
(the screen-scrape harness pattern); the shell gets an import+layout smoke.

The TUI is the OWNER's surface: posts drop the owner-unread marker (the
shipped reflex nudges every agent next turn) and reads do NOT consume it —
the marker belongs to the agents' read loop, not the owner's own eyes.

Exit runs the log-after leg (chat.log_flush) unless HELM_CHAT_LOG=0 — the
canon's exit-time flush, out-of-band by construction.

Input extras: Enter posts; :shortcodes: expand; `/react <n> <emoji>` reacts
(n counts messages, -1 = latest); q on an EMPTY line quits.
"""
import sys
import time

from . import chat, emoji, home

POLL_S = 2.0          # room poll cadence (matches chat.POLL_S / the web panel)
STATUS_EVERY = 10     # transport re-probe, in poll ticks
_LOOPS = None         # test seam: cap the shell loop


def operator_name():
    """The human at the helm: HELM_CHAT_NAME else the derived owner handle
    (seats.owner_name — git user.name else the login; no name ships in code)."""
    from . import seats
    return home.chat_name() or seats.owner_name()  # THE validated seam (home.chat_name)


def model_new(room="main"):
    return {"room": room, "rows": [], "total": 0, "input": "",
            "status": {"mode": "unsigned", "url": None, "head": None},
            "quota": "", "notice": ""}


def model_feed(m, rows, total):
    """Absorb one read() delta; a shrunken total (rotation) resets the row set."""
    if total < m["total"]:
        m["rows"], m["total"] = list(rows), total
    else:
        m["rows"].extend(rows)
        m["total"] = total
    return m


def quota_headline():
    """Freshest CACHED observation, one line — never probes (brief._seats law:
    no cache -> say so)."""
    try:
        from . import brief
        seats = brief._seats()
    except Exception:
        seats = []
    if not seats:
        return "quota: run `helm creds`"
    s = max(seats, key=lambda r: r.get("headroom_pct") or 0)
    pct = s.get("headroom_pct")
    return "quota %s %s %s" % (s.get("provider"), (s.get("account") or "?")[:20],
                               "%d%% left" % pct if pct is not None else "?")


def chat_pane(m, width, height):
    """The scrollback pane: wrapped message lines + inline reaction
    aggregates, bottom-anchored to `height` rows."""
    msgs, reacts = chat.thread(m["rows"])
    lines = []
    for row in msgs:
        lines.extend(emoji.wrap(chat._fmt(row), width))
        counts = reacts.get(chat.rkey(row))
        if counts:
            lines.append(emoji.clip("   " + chat.react_line(counts), width))
    if not lines:
        lines = ["(no messages yet — type below, Enter posts as %s)"
                 % operator_name()]
    return lines[-height:]


def status_line(m, width):
    st = chat._public_transport(m["status"])
    head = ("#%s" % st["head"]) if st.get("head") is not None else "-"
    transport = "%s %s" % (chat.transport_label(st).upper(), head)
    if st.get("mode") == "degraded":
        transport += " · %s: %s · last %ss ago" % (
            st.get("profile") or "?", st.get("reason") or "unknown",
            st.get("last_age_s") or 0)
    parts = ["helm", m["room"], transport, m["quota"],
             m["notice"] or "q quits"]
    return emoji.clip(" · ".join(p for p in parts if p), width)


def input_line(m, width):
    """The input row, right-anchored so the cursor end stays visible."""
    s = "> " + m["input"]
    while emoji.width(s) > width - 1:
        s = s[1:]
    return s


def render(m, height, width):
    """The full frame: chat pane + status strip + input line, as strings —
    exactly what the shell paints and exactly what the tests scrape."""
    body = chat_pane(m, width, max(1, height - 2))
    return {"chat": body, "status": status_line(m, width),
            "input": input_line(m, width)}


def _submit(m):
    """Enter: /react passthrough or a post AS THE OPERATOR (+ the reflex
    marker). Never raises — the room absorbs it or the notice line says why."""
    text = m["input"].strip()
    m["input"] = ""
    if not text:
        return
    if text.startswith("/react"):
        parts = text.split()
        try:
            n = int(parts[1])
        except (IndexError, ValueError):
            m["notice"] = "usage: /react <n> <emoji>"
            return
        row, err = chat.react(n, parts[2] if len(parts) > 2 else "",
                              m["room"], who=operator_name())
        m["notice"] = err or ""
        return
    # A REFUSED POST IS A NOTICE, NEVER AN EXIT. chat.post now raises on a
    # padded short sha (shaguard), and _submit is called BARE from the key
    # loop — so an unguarded raise would drop the owner out of curses mid-key
    # and leave the terminal to whatever the exception path restored. The
    # guard exists to stop a wrong sha reaching a reader; crashing the one
    # surface the owner types into would be a worse trade than the bug.
    try:
        chat.post(text, m["room"], who=operator_name(), origin="tui")
    except ValueError as exc:
        # AND THE TEXT COMES BACK. m["input"] was cleared at the top of this
        # function, so returning without restoring it would eat what the owner
        # typed — refusing a message and destroying it are different acts, and
        # only the first one is the guard's business.
        m["input"], m["notice"] = text, str(exc)
        return
    chat.mark_owner_unread(m["room"])
    m["notice"] = ""


def _paint(scr, m, curses):
    h, w = scr.getmaxyx()
    frame = render(m, h, w)
    scr.erase()
    rows = [(i, ln) for i, ln in enumerate(frame["chat"])]
    rows.append((h - 2, frame["status"]))
    rows.append((h - 1, frame["input"]))
    for y, ln in rows:
        if 0 <= y < h:
            attr = curses.A_REVERSE if y == h - 2 else curses.A_NORMAL
            try:
                scr.addnstr(y, 0, ln, w - 1, attr)
            except (curses.error, UnicodeEncodeError):
                try:  # degrade law: shortcode text, never a crash on an emoji
                    scr.addnstr(y, 0, emoji.demojize(ln), w - 1, attr)
                except (curses.error, UnicodeEncodeError):
                    pass  # an unmapped glyph on a legacy locale: skip the line
    scr.refresh()


def _shell(scr):
    import curses
    scr.timeout(int(POLL_S * 1000 / 8))
    m = model_new()
    m["quota"] = quota_headline()
    m["status"] = chat.transport_status()
    last_poll = 0.0
    ticks = 0
    loops = 0
    while _LOOPS is None or loops < _LOOPS:
        loops += 1
        now = time.time()
        if now - last_poll >= POLL_S or not m["total"]:
            rows, total = chat.read(m["room"], m["total"])
            model_feed(m, rows, total)
            last_poll = now
            ticks += 1
            if ticks % STATUS_EVERY == 1:
                m["status"] = chat.transport_status()
        _paint(scr, m, curses)
        try:
            ch = scr.get_wch()
        except curses.error:    # timeout — just repoll/repaint
            continue
        if ch == curses.KEY_RESIZE:
            continue
        if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
            _submit(m)
        elif ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
            m["input"] = m["input"][:-1]
        elif ch == "q" and not m["input"]:
            return 0
        elif isinstance(ch, str) and ch.isprintable():
            m["input"] += ch
    return 0


def cmd_human(args):
    """--human / human — the operator interface. Clean exit on q/Ctrl-C;
    exit-time log-flush unless disabled."""
    if not sys.stdout.isatty():
        print("helm --human wants a terminal (it is a curses app) — in a "
              "pipeline use `helm chat read --follow`", file=sys.stderr)
        return 1
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")
    try:  # the exit-time flush runs on EVERY exit path, a crashed shell too
        try:
            rc = curses.wrapper(_shell)
        except KeyboardInterrupt:
            rc = 0
    finally:
        if not chat.log_disabled():
            # THE REPORT IS THE ONLY THING THAT CAN TELL "nothing new" FROM
            # "every room refused". After per-room isolation this flush no
            # longer RAISES on the outage that owns this file's row — it
            # returns an int and continues — so the old `except`/`if n > 0`
            # pair rendered a total failure as SILENCE and a partial one as
            # plain success. The operator closing the shell is the last human
            # who will look at this process; he is told what actually reached
            # disk.
            report = {}
            try:
                n = chat.log_flush(report=report)
            except Exception as exc:
                # Cleanup is warning-only: a journal failure must not replace
                # the shell action's original exit status.
                print("helm chat: WARNING: exit-time log-flush failed: %s" % exc,
                      file=sys.stderr)
            else:
                if n > 0:
                    print("helm chat: log-flush appended %d row%s (the log-after leg)"
                          % (n, "s"[:n != 1]))
                state, reason = chat.flush_outcome(n, report)
                if state not in ("ok", "off"):
                    # DEGRADED IS NOT SUCCESS. It says which rooms were refused
                    # and how many rows stayed in RAM only — the sentence whose
                    # absence cost eight hours.
                    print("helm chat: log-flush %s — %s"
                          % (state.upper(), reason), file=sys.stderr)
    # The health RECORD stays the timer's: this exit-time run is the operator's,
    # not the fleet's scheduled durability run, and letting it reset the streak
    # would be the scoped-repair hazard (see cmd_chat's log-flush branch) with a
    # human's shell exit as the concurrent writer. It REPORTS, it does not wake.
    return rc
