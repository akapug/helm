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
runs at turn end). Pane found by title == seat name (helm-spawned panes,
`helm seat resume`), falling back to the pane whose visible tail still shows
the launch line's HELM_CHAT_NAME=<seat> (freshly hand-launched panes; on a
long-lived pane it has scrolled away). No metaharness / no identifiable pane
-> loud manual-paste chat alert instead, never silent.

Watchdog vs autocompact: watchdog.py detects the already-wedged seat (rescue =
/clear, destructive, human-gated by canon). Autocompact PRE-EMPTS with the
non-destructive /compact, so forging it from outside is safe by construction.

POLL — LEAN, NO DEMONS (machine law: single-shot verbs, external cadence):
`helm seat autocompact` is one idempotent bounded pass; schedule it with a
systemd --user timer (`--install-timer` prints/writes the units) or any
Monitor/cron loop. A latch (state file) makes overlapping/frequent calls safe:
one fire per seat per episode, re-armed when the context actually drops
(compaction landed) or the session changes; a fire that never landed retries
after LATCH_TTL_S.

PROXY-ONLY GATE: only FAMILIES seats (all proxy-backed by construction) are
scanned, and a transcript whose model says claude-* is skipped — native claude
seats autocompact fine on their own.
"""
import glob
import json
import os
import re
import sys
import time

from . import home

DEFAULT_THRESHOLD = 90      # fire at >= this pct (HELM_AUTOCOMPACT_THRESHOLD)
REARM_MARGIN = 15           # latch re-arms once pct < threshold - margin
LATCH_TTL_S = 15 * 60       # a fire that never shrank the context retries here
FRESH_S = 6 * 3600          # older transcript = not this pane's live context
CC_ASSUMED_WINDOW = 200000  # CC's hardcoded window for non-claude models
TAIL_BYTES = 512 * 1024     # bounded tail reads (transcripts + proxy.log)

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


def _proxy_log_ctx(family):
    from . import seat
    path = os.path.join(seat.seat_dir(family), "proxy.log")
    for ln in reversed(_tail_lines(path)):
        if "input_tokens" not in ln:
            continue
        parts = {k: rx.search(ln) for k, rx in _TOK.items()}
        if not parts["input_tokens"]:
            continue
        return sum(int(m.group(1)) for m in parts.values() if m)
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
    row = {"seat": seat_name, "family": family, "ctx_tokens": None,
           "pct": None, "source": None, "model": None, "session": None,
           "age_s": None}
    win, win_src = _window(family)
    row["window"], row["window_src"] = win, win_src
    if win is None:
        row["status"] = "window-unset"
        return row
    tp = _newest_transcript(seat._instance_dir(family, seat_name))
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
        ctx = _proxy_log_ctx(family)
        if ctx is not None:
            row["ctx_tokens"], row["source"] = ctx, "proxy.log"
            row["age_s"] = 0
    if row["ctx_tokens"] is None:
        row["status"] = "no-context-data"
        return row
    row["pct"] = round(100.0 * row["ctx_tokens"] / win, 1)
    if (row["model"] or "").startswith("claude"):
        row["status"] = "claude-model"       # native seats autocompact fine
    elif row["age_s"] is not None and row["age_s"] > _int_env(
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


def _latch_blocks(entry, row, thr, now):
    """True while a prior fire for this same episode should suppress another."""
    if not entry:
        return False
    if entry.get("session") != row.get("session"):
        return False                        # new session = new episode
    if row["pct"] < thr - REARM_MARGIN:
        return False                        # compaction landed (caller clears)
    if now - (entry.get("fired_at") or 0) > _int_env(
            "AUTOCOMPACT_LATCH_TTL", LATCH_TTL_S):
        return False                        # the fire never landed — retry
    return True


# ---------------------------------------------------------------------------
# the fire — /compact into the seat's pane
# ---------------------------------------------------------------------------

def _fire(seat_name, adapter):
    """Inject '/compact' + Enter into the pane titled <seat_name>.
    Returns (mode, detail): 'injected' on success, 'manual' when there is no
    metaharness/pane to drive (the caller alerts loudly instead)."""
    from . import harness
    ad = adapter if adapter is not None else harness.detect()
    if ad is None:
        return "manual", harness.RECOMMENDATION
    try:
        panes = ad.list()
        # exact title first (helm-spawned panes are titled the seat name),
        # then the pane whose visible tail carries the seat's launch-line
        # identity (manually-launched panes get auto-summary titles; the
        # trailing space keeps 'codex' from matching 'codex-2').
        hit = next((p for p in panes
                    if p.get("title") == seat_name and p.get("handle")), None)
        if hit is None:
            mark = "HELM_CHAT_NAME=%s " % seat_name
            hits = [p for p in panes
                    if mark in (p.get("preview") or "") and p.get("handle")]
            hit = hits[0] if len(hits) == 1 else None
        if hit:
            ad.send(hit["handle"], "/compact", enter=True)
            return "injected", "pane %s via %s" % (hit["handle"], ad.name)
    except harness.HarnessError as e:
        return "manual", str(e)
    return "manual", "no pane titled %r on %s" % (seat_name, ad.name)


def _fire_text(row, mode, detail):
    k = lambda n: "%.0fk" % (n / 1000.0)
    head = ("🌀 AUTOCOMPACT: /compact injected into seat %s" if mode == "injected"
            else "⚠️ AUTOCOMPACT: seat %s needs /compact NOW (injection "
            "unavailable — paste it into the pane)") % row["seat"]
    return ("%s at %.0f%% (%s/%s, %s) — pre-empting the 100%% proxy hang. %s"
            % (head, row["pct"], k(row["ctx_tokens"]), k(row["window"]),
               row["source"], detail))


def check(seats=None, thr=None, fire=True, post=True, adapter=None):
    """One bounded pass: scan -> latch -> fire -> latch-update. Returns
    {"rows": [...], "fired": [...]} where each fired row carries mode/detail.
    fire=False = dry-run (rows still show would_fire). Idempotent: the latch
    means calling this every N seconds never double-fires an episode."""
    from . import pk
    thr = thr if thr is not None else threshold_pct()
    rows = scan(seats)
    st = pk.read_json(_state_path(), {}) or {}
    now = time.time()
    fired = []
    for row in rows:
        if row.get("status") != "ok" or row["pct"] < thr:
            if (row.get("pct") is not None and row.get("seat") in st
                    and row["pct"] < thr - REARM_MARGIN):
                st.pop(row["seat"], None)    # episode over — re-arm
            continue
        row["would_fire"] = True
        if _latch_blocks(st.get(row["seat"]), row, thr, now):
            row["latched"] = True
            continue
        if not fire:
            continue
        mode, detail = _fire(row["seat"], adapter)
        row["mode"], row["detail"] = mode, detail
        st[row["seat"]] = {"fired_at": now, "session": row.get("session"),
                           "pct": row["pct"], "mode": mode}
        fired.append(row)
        if post:
            try:
                from . import chat
                chat.post(_fire_text(row, mode, detail), who="autocompact")
            except Exception as e:   # a down chat node never blocks the fire
                print("helm autocompact: chat post failed (%s): %s"
                      % (row["seat"], e), file=sys.stderr)
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    pk.write_json(p, st)
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
Environment=PYTHONPATH=%(repo)s
ExecStart=%(python)s -m helm seat autocompact --once
"""

_UNIT_TIMER = """[Unit]
Description=helm autocompact cadence (external, no demons)

[Timer]
OnBootSec=%(interval)ss
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _install_timer(args):
    interval = 120
    if "--interval" in args:
        try:
            interval = int(args[args.index("--interval") + 1])
        except (ValueError, IndexError):
            print("helm seat autocompact: --interval wants seconds",
                  file=sys.stderr)
            return 2
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    service = _UNIT_SERVICE % {"repo": repo, "python": sys.executable}
    timer = _UNIT_TIMER % {"interval": interval}
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    spath = os.path.join(udir, "helm-autocompact.service")
    tpath = os.path.join(udir, "helm-autocompact.timer")
    if "--apply" not in args:
        print("# %s\n%s\n# %s\n%s" % (spath, service, tpath, timer))
        print("# install:\n#   helm seat autocompact --install-timer --apply\n"
              "# or write the two files above, then:\n"
              "#   systemctl --user daemon-reload && "
              "systemctl --user enable --now helm-autocompact.timer")
        return 0
    from . import pk
    os.makedirs(udir, exist_ok=True)
    pk.atomic_write(spath, service)
    pk.atomic_write(tpath, timer)
    import subprocess
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now",
                 "helm-autocompact.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("helm seat autocompact: %s failed: %s"
                  % (" ".join(cmd), (r.stderr or "").strip()[:200]),
                  file=sys.stderr)
            return 1
    print("helm seat autocompact: timer enabled (every %ds) — %s" %
          (interval, tpath))
    return 0


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
