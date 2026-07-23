#!/usr/bin/env python3
"""helm autocompact — the proxy-seat context watchdog (pre-empt, not post-mortem).

CONFIRMED HANG (owner, live 2x, ~20min lost each): a codex/kimi PROXY seat
silently hangs when its claude-code context reaches 100% — CC's native
autocompaction does not fire for non-claude proxy models, and a /compact queued
once wedged never runs. The owner had to esc + manually compact.

This module reads a proxy seat's context% PROGRAMMATICALLY and injects /compact
into the seat's pane at a threshold (default 90%) — BEFORE the 100% hang, while
the composer still works. Non-destructive: /compact is CC's own summarize-and-
continue, the session survives.

READ (ground-truthed 2026-07-21, decision-spirited):
  1. transcript — the seat's own CLAUDE_CONFIG_DIR transcript
     (<instance>/claude/projects/<slug>/<uuid>.jsonl, persisted since the
     child-stamp fix): the newest main-chain assistant record's
     usage.input_tokens + cache_read + cache_creation = the context CC itself
     is holding. This is CC's OWN gauge (statusline-equivalent), so pct tracks
     exactly the number that wedges. PRIMARY.
  2. proxy.log — a usage-bearing JSON line in the seat's proxy log tail.
     CLIProxyAPI's default gin access log carries NO token fields (verified
     live against both seats), so today this yields nothing — kept as the
     zero-cost fallback that lights up if request/usage logging is ever
     enabled (config `request-log: true`) or the proxy version changes.

WINDOW: FAMILIES[family]["max_context"] (the CLAUDE_CODE_MAX_CONTEXT_TOKENS the
launch line teaches CC). When a family doesn't set one, CC assumes 200k for any
non-claude model — we mirror that assumption (HELM_AUTOCOMPACT_ASSUME_WINDOW,
default 200000; 0/off = strict no-op when unset) so pct still tracks CC's gauge.

TRIGGER (decision-spirited): /compact INJECTION via the metaharness seam
(harness.py send — orca `terminal send`/herdr `pane run`). cv-side transcript
pruning was rejected: it edits the file, not the LIVE process's memory — only
the pane's own /compact changes what CC is holding. At 90% the composer is not
wedged, so the injected command actually runs (typed mid-turn it queues and
runs at turn end). Pane identity comes from the seat's authoritative
spawn.json handle, verified against the matching adapter's live inventory.
Legacy panes without a register are never guessed from copied launch text or a
mutable terminal title: they fail loudly until `seat spawn`/`resume` registers
an authoritative handle. No metaharness / no identifiable pane -> loud
manual-paste chat alert instead, never silent.

Watchdog vs autocompact: watchdog.py detects the already-wedged seat (rescue =
/clear, destructive, human-gated by canon). Autocompact PRE-EMPTS with the
non-destructive /compact, so forging it from outside is safe by construction.

POLL — LEAN, NO DEMONS (machine law: single-shot verbs, external cadence):
`helm seat autocompact` is one idempotent bounded pass; schedule it with a
systemd --user timer (`--install-timer` prints/writes the units) or any
Monitor/cron loop. A latch (state file) makes overlapping/frequent calls safe:
one fire per seat per episode, re-armed only when the context actually drops
(compaction landed) or the session changes. Injected/already-pending compactions
never time-rearm; manual alerts may repeat after LATCH_TTL_S.

PROXY-ONLY GATE: only FAMILIES seats (all proxy-backed by construction) are
scanned, and a transcript whose model says claude-* is skipped — native claude
seats autocompact fine on their own.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import sys
import time

from . import home

DEFAULT_THRESHOLD = 90      # fire at >= this pct (HELM_AUTOCOMPACT_THRESHOLD)
LATCH_TTL_S = 15 * 60       # manual alerts may repeat while still actionable
FRESH_S = 6 * 3600          # older transcript = not this pane's live context
CC_ASSUMED_WINDOW = 200000  # CC's hardcoded window for non-claude models
TAIL_BYTES = 512 * 1024     # bounded tail reads (transcripts + proxy.log)
DEFAULT_INTERVAL_S = 60     # bounded scan cadence; one large turn can cross 90%

_USAGE = """usage: helm seat autocompact [--seat S] [--threshold N] [--once]
                             [--dry-run] [--quiet] [--json]
       helm seat autocompact --install-timer [--interval SEC] [--apply]
  One idempotent pass over every proxy seat: read context%% (seat transcript,
  proxy.log fallback), inject /compact into the pane at >= threshold (default
  %d%%, HELM_AUTOCOMPACT_THRESHOLD). Latched: one fire per episode. --dry-run
  reads + decides but never injects; --quiet skips the chat post; --once is
  the (only) mode, accepted for interface stability. --install-timer prints
  the systemd --user units for the external cadence (--apply writes+enables).
""" % DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------

def _int_env(name, default):
    try:
        return int(home.env(name, default))
    except (TypeError, ValueError):
        return default


def threshold_pct():
    return _int_env("AUTOCOMPACT_THRESHOLD", DEFAULT_THRESHOLD)


def _assume_window():
    """The window mirrored from CC's own non-claude default when FAMILIES
    doesn't pin one. 0/off = strict: no window -> no-op for that seat."""
    v = str(home.env("AUTOCOMPACT_ASSUME_WINDOW", CC_ASSUMED_WINDOW)).lower()
    if v in ("0", "off", "none", ""):
        return None
    try:
        return int(v)
    except ValueError:
        return CC_ASSUMED_WINDOW


def _window(family):
    """(window_tokens, source) for a family; (None, reason) when unknowable."""
    from . import seat
    fam = seat.FAMILIES.get(family) or {}
    if fam.get("max_context"):
        return fam["max_context"], "FAMILIES.max_context"
    aw = _assume_window()
    if aw:
        return aw, "cc-assumed-default"
    return None, "window unset (no FAMILIES max_context, assume-window off)"


# ---------------------------------------------------------------------------
# seat discovery (families + slice-6 instances actually on disk)
# ---------------------------------------------------------------------------

def proxy_seats():
    """Every proxy seat with a minted dir: family seats + instances/<seat>."""
    from . import seat
    out = []
    for family in sorted(seat.FAMILIES):
        d = seat.seat_dir(family)
        if not os.path.isdir(d):
            continue
        out.append(family)
        inst_root = os.path.join(d, "instances")
        if os.path.isdir(inst_root):
            out.extend(sorted(s for s in os.listdir(inst_root)
                              if os.path.isdir(os.path.join(inst_root, s))))
    return out


def _tail_lines(path, tail_bytes=TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


# ---------------------------------------------------------------------------
# read source 1 — the seat's own transcript (CC's gauge)
# ---------------------------------------------------------------------------

def _newest_transcript(instance_dir):
    from . import seat
    cands = []
    for p in glob.glob(os.path.join(instance_dir, "claude", "projects",
                                    "*", "*.jsonl")):
        if not seat._SESSION_JSONL.match(os.path.basename(p)):
            continue
        try:
            cands.append((os.path.getmtime(p), p))
        except OSError:
            pass
    return max(cands)[1] if cands else None


def _transcript_ctx(path):
    """(ctx_tokens, model) from the newest main-chain assistant usage record —
    input + cache_read + cache_creation = what CC is holding right now.
    Sidechain (subagent) records never count. None when no usage in the tail."""
    for ln in reversed(_tail_lines(path)):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain"):
            continue
        msg = d.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict) or "input_tokens" not in u:
            continue
        ctx = sum(int(u.get(k) or 0) for k in
                  ("input_tokens", "cache_read_input_tokens",
                   "cache_creation_input_tokens"))
        return ctx, (msg.get("model") or "")
    return None


# ---------------------------------------------------------------------------
# read source 2 — proxy.log usage line (fallback; empty on today's gin format)
# ---------------------------------------------------------------------------

_TOK = {k: re.compile(r'"%s"\s*:\s*(\d+)' % k) for k in
        ("input_tokens", "cache_read_input_tokens",
         "cache_creation_input_tokens")}
_PROXY_SESSION = re.compile(
    r'"(?:session|session_id|sessionId)"\s*:\s*"([^"\\]+)"')


def _proxy_log_ctx(family, seat_name):
    """(tokens, session, age_s) from one usage-bearing proxy row.

    A per-seat log is not a per-session log: never attach today's spawn session
    to an older usage row. The row itself must carry session identity or the
    caller reports proxy-log-unattributed and refuses to fire.
    """
    from . import seat
    proxy_home = getattr(seat, "_proxy_home", seat._instance_dir)(
        family, seat_name)
    path = os.path.join(proxy_home, "proxy.log")
    try:
        age_s = max(0, time.time() - os.path.getmtime(path))
    except OSError:
        age_s = None
    for ln in reversed(_tail_lines(path)):
        if "input_tokens" not in ln:
            continue
        parts = {k: rx.search(ln) for k, rx in _TOK.items()}
        if not parts["input_tokens"]:
            continue
        sid = _PROXY_SESSION.search(ln)
        return (sum(int(m.group(1)) for m in parts.values() if m),
                sid.group(1) if sid else None, age_s)
    return None


# ---------------------------------------------------------------------------
# the read — one seat's context row
# ---------------------------------------------------------------------------

def read(seat_name):
    """One seat's context row: seat/family/window/ctx_tokens/pct/source/model/
    session/age_s/status. status == "ok" is the only fireable state; every
    other status names exactly why the seat is a no-op (doctor prints it)."""
    from . import seat
    family, err = seat._seat_family(seat_name)
    if err:
        return {"seat": seat_name, "status": "unknown-seat"}
    d = seat._instance_dir(family, seat_name)
    rec = seat._spawn_record(d) or {}
    registered = rec.get("session") if rec.get("seat") == seat_name else None
    row = {"seat": seat_name, "family": family, "ctx_tokens": None,
           "pct": None, "source": None, "model": None, "session": None,
           "registered_session": registered, "age_s": None}
    win, win_src = _window(family)
    row["window"], row["window_src"] = win, win_src
    if win is None:
        row["status"] = "window-unset"
        return row
    tp = _newest_transcript(d)
    got = _transcript_ctx(tp) if tp else None
    if got:
        row["ctx_tokens"], row["model"] = got
        row["source"] = "transcript"
        row["session"] = os.path.basename(tp)[:-len(".jsonl")]
        try:
            row["age_s"] = max(0, time.time() - os.path.getmtime(tp))
        except OSError:
            pass
    else:
        proxy = _proxy_log_ctx(family, seat_name)
        if proxy is not None:
            row["ctx_tokens"], row["session"], row["age_s"] = proxy
            row["source"] = "proxy.log"
    if row["ctx_tokens"] is None:
        row["status"] = "no-context-data"
        return row
    row["pct"] = round(100.0 * row["ctx_tokens"] / win, 1)
    if row["source"] == "proxy.log" and not row["session"]:
        row["status"] = "proxy-log-unattributed"
    elif (row["model"] or "").startswith("claude"):
        row["status"] = "claude-model"       # native seats autocompact fine
    elif not registered:
        row["status"] = "session-unbound"
    elif row["session"] != registered:
        row["status"] = "session-mismatch"
    elif row["age_s"] is None:
        row["status"] = "context-undated"
    elif row["age_s"] > _int_env(
            "AUTOCOMPACT_FRESH_S", FRESH_S):
        row["status"] = "stale"              # not this pane's live context
    else:
        row["status"] = "ok"
    return row


def scan(seats=None):
    return [read(s) for s in (seats if seats is not None else proxy_seats())]


# ---------------------------------------------------------------------------
# the latch (one fire per episode; overlapping polls stay safe)
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "autocompact.json")


def _state_lock_path():
    return _state_path() + ".lock"


def _episode_complete(entry, row):
    """Whether the prior high-context episode has observably ended."""
    if not entry:
        return False
    prior, current = entry.get("session"), row.get("session")
    if prior and current and prior != current:
        return True                         # proven new session = new episode
    fired_pct = entry.get("pct")
    return fired_pct is not None and row.get("pct") is not None and \
        row["pct"] < fired_pct              # compaction made context shrink


def _latch_blocks(entry, row, now):
    """True while a prior fire for this same episode should suppress another."""
    if not entry or _episode_complete(entry, row):
        return False
    if entry.get("mode") in ("manual", "clear-manual") and \
            now - (entry.get("fired_at") or 0) > \
            _int_env("AUTOCOMPACT_LATCH_TTL", LATCH_TTL_S):
        return False                        # repeat a still-actionable alert
    return True                             # injected/pending waits for pct drop


# ---------------------------------------------------------------------------
# the fire — /compact into the seat's pane
# ---------------------------------------------------------------------------

def resolve_pane(seat_name, adapter=None):
    """Resolve one identity-proven pane without sending input.
    Seat owns pane identity; every actuator consumes that one proof."""
    from . import seat
    return seat._resolve_registered_pane(seat_name, adapter=adapter)


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_API_400 = re.compile(r"^\s*(?:API\s+Error:|HTTP(?:\s+Error)?)\s*400\b",
                      re.IGNORECASE)


def _command_pending(tail, command):
    """Whether the final visible line is Claude's exact composer command.

    The `❯` glyph and last-nonempty-line position are both required; markdown
    blockquotes (`> /compact`) and earlier rendered conversation text are not
    composer identity and can never create a permanent pending latch.
    """
    rx = re.compile(r"^\s*❯\s*/%s(?:\s|$)" % re.escape(command))
    lines = [_ANSI.sub("", line) for line in tail.splitlines()]
    last = next((line for line in reversed(lines) if line.strip()), "")
    return bool(rx.match(last))


def _compact_pending(ad, handle):
    """True only when the visible composer itself holds an unsent /compact.
    Mentions in transcript/history are not pending input."""
    return _command_pending(ad.read(handle, limit=2000), "compact")


def _overflow_400(tail):
    """True only for a repeated terminal error loop, never prose mentioning 400.

    Claude Code prints failed requests as line-leading `API Error: 400 ...`.
    Requiring two such lines plus context/token/prompt overflow vocabulary keeps
    copied docs, chat discussion, and one transient bad request non-destructive.
    """
    hits = 0
    for line in tail.splitlines()[-80:]:
        clean = _ANSI.sub("", line)
        low = clean.lower()
        if not _API_400.match(clean):
            continue
        if not any(word in low for word in ("context", "token", "prompt")):
            continue
        if not any(word in low for word in
                   ("exceed", "overflow", "too long", "maximum")):
            continue
        hits += 1
    return hits >= 2


def _overflow_probe(seat_name, adapter):
    """The identity-proven pane + tail only when it is in a 400 overflow loop."""
    from . import harness
    ad, handle, detail = resolve_pane(seat_name, adapter)
    if ad is None or handle is None:
        return None
    try:
        tail = ad.read(handle, limit=12000)
    except harness.HarnessError:
        return None
    return (ad, handle, detail, tail) if _overflow_400(tail) else None


def _onboarding(seat_name):
    from . import seat
    family, err = seat._seat_family(seat_name)
    if err:
        return None, err
    rec = seat._spawn_record(seat._instance_dir(family, seat_name)) or {}
    return seat.onboarding_prompt(seat_name, rec.get("room") or "main"), None


def _rebrief_after_clear(seat_name, ad, handle):
    """Re-seed a session whose /clear succeeded but whose brief did not."""
    from . import harness
    brief, err = _onboarding(seat_name)
    if err:
        return "clear-needs-brief", err
    try:
        ad.send(handle, brief, enter=True)
    except harness.HarnessError as e:
        return "clear-needs-brief", str(e)
    return "cleared", "session cleared; onboarding brief re-injected into pane %s" % handle


def _fire_clear(seat_name, ad, handle, detail, tail):
    """Clear one proven overflow loop, then restore the seat's operating brief."""
    from . import harness
    if _command_pending(tail, "clear"):
        return "clear-pending", "pane %s already has /clear queued" % handle
    try:
        ad.send(handle, "/clear", enter=True)
    except harness.HarnessError as e:
        return "clear-manual", str(e)
    mode, brief_detail = _rebrief_after_clear(seat_name, ad, handle)
    return mode, "%s; %s" % (detail, brief_detail)


def _retry_rebrief(seat_name, adapter):
    ad, handle, detail = resolve_pane(seat_name, adapter)
    if ad is None or handle is None:
        return "clear-needs-brief", detail
    mode, brief_detail = _rebrief_after_clear(seat_name, ad, handle)
    return mode, "%s; %s" % (detail, brief_detail)


def _fire(seat_name, adapter):
    """Inject '/compact' + Enter into one identity-proven seat pane."""
    from . import harness
    ad, handle, detail = resolve_pane(seat_name, adapter)
    if ad is None or handle is None:
        return "manual", detail
    try:
        if _compact_pending(ad, handle):
            return "pending", "pane %s already has /compact queued" % handle
        ad.send(handle, "/compact", enter=True)
    except harness.HarnessError as e:
        return "manual", str(e)
    return "injected", detail


def _fire_text(row, mode, detail):
    if mode == "cleared":
        return ("🧹 AUTOCOMPACT RECOVERY: /clear + onboarding injected into "
                "overflowed seat %s — %s" % (row["seat"], detail))
    if mode == "clear-pending":
        return ("🧹 AUTOCOMPACT RECOVERY: seat %s already has /clear pending; "
                "onboarding follows after the session resets — %s"
                % (row["seat"], detail))
    if mode == "clear-needs-brief":
        return ("⚠️ AUTOCOMPACT RECOVERY: seat %s cleared but its onboarding "
                "brief still needs injection — %s" % (row["seat"], detail))
    if mode == "clear-manual":
        return ("⚠️ AUTOCOMPACT RECOVERY: seat %s is in a repeated 400 context "
                "overflow loop; paste /clear, then its onboarding brief — %s"
                % (row["seat"], detail))
    k = lambda n: "%.0fk" % (n / 1000.0)
    if mode == "injected":
        head = "🌀 AUTOCOMPACT: /compact injected into seat %s" % row["seat"]
    elif mode == "pending":
        head = "🌀 AUTOCOMPACT: seat %s already has /compact pending" % row["seat"]
    else:
        head = ("⚠️ AUTOCOMPACT: seat %s needs /compact NOW (injection "
                "unavailable — paste it into the pane)" % row["seat"])
    return ("%s at %.0f%% (%s/%s, %s) — pre-empting the 100%% proxy hang. %s"
            % (head, row["pct"], k(row["ctx_tokens"]), k(row["window"]),
               row["source"], detail))


def check(seats=None, thr=None, fire=True, post=True, adapter=None):
    """One bounded pass: scan -> latch -> fire -> latch-update. Returns
    {"rows": [...], "fired": [...]} where each fired row carries mode/detail.
    fire=False = dry-run (rows still show would_fire). The state lock covers the
    complete read/fire/write transaction, so overlapping timer/manual passes
    cannot inject twice from the same empty latch."""
    from . import pk
    thr = thr if thr is not None else threshold_pct()
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(_state_lock_path(), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        rows = scan(seats)
        st = pk.read_json(p, {}) or {}
        now = time.time()
        fired = []
        for row in rows:
            entry = st.get(row["seat"])
            recovery = (entry or {}).get("mode")
            if recovery == "clear-needs-brief" or (
                    recovery == "clear-pending" and
                    entry.get("session") != row.get("session")):
                row["would_rebrief"] = True
                if not fire:
                    continue
                mode, detail = _retry_rebrief(row["seat"], adapter)
                row["mode"], row["detail"] = mode, detail
                st[row["seat"]] = {
                    "fired_at": now, "session": row.get("session"),
                    "pct": row.get("pct"), "mode": mode}
                fired.append(row)
                continue
            if recovery == "clear-pending":
                row["latched"] = True
                continue
            if _episode_complete(entry, row):
                st.pop(row["seat"], None)
                entry = None                     # episode over — re-arm

            overflow = _overflow_probe(row["seat"], adapter)
            if overflow:
                row["overflow"], row["would_clear"] = True, True
                clear_entry = entry if (entry or {}).get("mode") in (
                    "cleared", "clear-manual") else None
                if clear_entry and _latch_blocks(clear_entry, row, now):
                    row["latched"] = True
                    continue
                if not fire:
                    continue
                mode, detail = _fire_clear(row["seat"], *overflow)
                row["mode"], row["detail"] = mode, detail
                st[row["seat"]] = {
                    "fired_at": now, "session": row.get("session"),
                    "pct": row.get("pct"), "mode": mode}
                fired.append(row)
                continue

            if row.get("status") != "ok" or row["pct"] < thr:
                continue
            row["would_fire"] = True
            if _latch_blocks(entry, row, now):
                row["latched"] = True
                continue
            if not fire:
                continue
            mode, detail = _fire(row["seat"], adapter)
            row["mode"], row["detail"] = mode, detail
            st[row["seat"]] = {"fired_at": now, "session": row.get("session"),
                               "pct": row["pct"], "mode": mode}
            fired.append(row)
        pk.write_json(p, st)

    if post:
        for row in fired:
            try:
                from . import chat
                chat.post(_fire_text(row, row["mode"], row["detail"]),
                          who="autocompact")
            except Exception as e:   # a down chat node never blocks the fire
                print("helm autocompact: chat post failed (%s): %s"
                      % (row["seat"], e), file=sys.stderr)
    return {"rows": rows, "fired": fired}


# ---------------------------------------------------------------------------
# surfaces — doctor lines, CLI, timer units
# ---------------------------------------------------------------------------

def _age(s):
    if s is None:
        return "?"
    if s < 90:
        return "%.0fs" % s
    if s < 5400:
        return "%.0fm" % (s / 60)
    if s < 172800:
        return "%.1fh" % (s / 3600)
    return "%.0fd" % (s / 86400)


def _row_line(row):
    if row.get("status") == "window-unset":
        return "%-8s %s" % (row["seat"], row["window_src"])
    if row.get("ctx_tokens") is None:
        return ("%-8s no context data yet (transcript appears after the seat's "
                "first persisted turn)" % row["seat"])
    note = {"ok": "", "stale": " — STALE, no fire",
            "claude-model": " — claude model, native autocompact"}.get(
                row["status"], " — " + row["status"])
    flag = ""
    if row.get("mode"):
        flag = " -> FIRED (%s)" % row["mode"]
    elif row.get("latched"):
        flag = " [latched]"
    elif row.get("would_fire"):
        flag = " -> would fire"
    return ("%-8s %5.1f%% of %dk (%dk via %s, %s old)%s%s"
            % (row["seat"], row["pct"], row["window"] // 1000,
               row["ctx_tokens"] // 1000, row["source"],
               _age(row.get("age_s")), note, flag))


def report_lines(thr=None):
    """Read-only doctor lines: every proxy seat's context% + latch state."""
    from . import pk
    thr = thr if thr is not None else threshold_pct()
    st = pk.read_json(_state_path(), {}) or {}
    lines = ["autocompact (proxy-seat /compact watchdog, threshold %d%%):" % thr]
    rows = scan()
    if not rows:
        return lines + ["  no proxy seats minted"]
    for row in rows:
        extra = ""
        e = st.get(row.get("seat"))
        if e:
            extra = "  [latched %s ago, %s]" % (_age(time.time() -
                                                     (e.get("fired_at") or 0)),
                                                e.get("mode") or "?")
        lines.append("  " + _row_line(row) + extra)
    return lines


_UNIT_SERVICE = """[Unit]
Description=helm proxy-seat autocompact watchdog (one idempotent pass)

[Service]
Type=oneshot
ExecStart=%(helm)s seat autocompact --once
"""

_UNIT_TIMER = """[Unit]
Description=helm autocompact cadence (external, no demons)

[Timer]
OnBootSec=%(interval)ss
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _timer_units(interval=DEFAULT_INTERVAL_S):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    service = _UNIT_SERVICE % {"repo": repo, "python": sys.executable}
    timer = _UNIT_TIMER % {"interval": interval}
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-autocompact.service"), service,
            os.path.join(udir, "helm-autocompact.timer"), timer)


def ensure_timer(interval=DEFAULT_INTERVAL_S):
    """Install/refresh and enable the external cadence. Returns (ok, detail).
    Seat launch/spawn/resume call this lifecycle seam so a runnable proxy seat
    cannot silently outlive its prevention loop."""
    if interval < 1:
        return False, "interval must be at least 1 second"
    from . import pk
    import subprocess
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl unavailable; run autocompact from another scheduler"
    spath, service, tpath, timer = _timer_units(interval)
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now",
                 "helm-autocompact.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "timer enabled every %ds (%s)" % (interval, tpath)


def _install_timer(args):
    interval = DEFAULT_INTERVAL_S
    if "--interval" in args:
        try:
            interval = int(args[args.index("--interval") + 1])
        except (ValueError, IndexError):
            print("helm seat autocompact: --interval wants seconds",
                  file=sys.stderr)
            return 2
    if interval < 1:
        print("helm seat autocompact: --interval must be at least 1 second",
              file=sys.stderr)
        return 2
    spath, service, tpath, timer = _timer_units(interval)
    if "--apply" not in args:
        print("# %s\n%s\n# %s\n%s" % (spath, service, tpath, timer))
        print("# install:\n#   helm seat autocompact --install-timer --apply\n"
              "# or write the two files above, then:\n"
              "#   systemctl --user daemon-reload && "
              "systemctl --user enable --now helm-autocompact.timer")
        return 0
    ok, detail = ensure_timer(interval)
    stream = sys.stdout if ok else sys.stderr
    print("helm seat autocompact: " + detail, file=stream)
    return 0 if ok else 1


def cmd_autocompact(args):
    args = list(args)
    # junk refuses BEFORE help and BEFORE the check() sweep — `seat
    # autocompact frobnicate --help` is an existence probe, and a typo'd arg
    # must not fire the /compact injector as if the arg existed.
    from .cli import guard_tail
    # --once is load-bearing compatibility: the minted systemd unit
    # (_UNIT_SERVICE) runs `helm seat autocompact --once` every interval —
    # a guard that refuses it kills the fleet's installed watchdog timers.
    rc = guard_tail("helm seat autocompact", args,
                    flags=("--once", "--install-timer", "--apply",
                           "--dry-run", "--quiet", "--json"),
                    valued=("--threshold", "--seat", "--interval"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    if "--install-timer" in args:
        return _install_timer(args)
    thr = None
    if "--threshold" in args:
        try:
            thr = int(args[args.index("--threshold") + 1])
        except (ValueError, IndexError):
            print("helm seat autocompact: --threshold wants a percent",
                  file=sys.stderr)
            return 2
    seats = None
    if "--seat" in args:
        try:
            seats = [args[args.index("--seat") + 1]]
        except IndexError:
            print("helm seat autocompact: --seat wants a name", file=sys.stderr)
            return 2
    res = check(seats=seats, thr=thr, fire="--dry-run" not in args,
                post="--quiet" not in args and "--dry-run" not in args)
    if "--json" in args:
        print(json.dumps(res))
        return 0
    for row in res["rows"]:
        print(_row_line(row))
    for row in res["fired"]:
        print("FIRED %s: %s" % (row["seat"], row["detail"]))
    if not res["rows"]:
        print("autocompact: no proxy seats minted")
    return 0
