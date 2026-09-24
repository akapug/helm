#!/usr/bin/env python3
"""helm autocompact — the proxy-seat context watchdog (pre-empt, not post-mortem).

CONFIRMED HANG (owner, live 2x, ~20min lost each): a codex/kimi PROXY seat
silently hangs when its claude-code context reaches 100% — CC's native
autocompaction does not fire for non-claude proxy models, and a /compact queued
once wedged never runs. The owner had to esc + manually compact.

This module reads a proxy seat's context% PROGRAMMATICALLY and injects /compact
into the seat's pane at a threshold (default 80%) — BEFORE the 100% hang, while
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

WINDOW: FAMILIES[family]["max_context"], narrowed by the family's
`context_budget` where one is declared (seat_catalog.taught_window; kimi's 1M
is taught as 380k, task/2944), which the launch line teaches CC
through BOTH of its context knobs — CLAUDE_CODE_MAX_CONTEXT_TOKENS (capacity)
and CLAUDE_CODE_AUTO_COMPACT_WINDOW (the auto-compact window, clamped by that
capacity). They are not synonyms and #182 nearly swapped one for the other; the
seat.py comment at the mint site carries the verified mechanism. When a family
doesn't set one, CC assumes 200k for any non-claude model — we mirror that
assumption (HELM_AUTOCOMPACT_ASSUME_WINDOW, default 200000; 0/off = strict
no-op when unset) so pct still tracks CC's gauge.

TRIGGER (decision-spirited): /compact INJECTION via the metaharness seam
(harness.py send — orca `terminal send`/herdr `pane run`). cv-side transcript
pruning was rejected: it edits the file, not the LIVE process's memory — only
the pane's own /compact changes what CC is holding. A codex-family pane does NOT
consume queued commands while its turn remains open, so threshold alone never
authorizes the send: the authoritative pane must also read IDLE at its prompt.
RUNNING, BLOCKED_ON_HUMAN and UNKNOWN/read-blind panes refuse with a reason and
are re-measured next pass. Pane identity comes from the seat's authoritative
spawn.json handle, verified against the matching adapter's live inventory.
Legacy panes without a register are never guessed from copied launch text or a
mutable terminal title: they fail loudly until `seat spawn`/`resume` registers
an authoritative handle. No metaharness / no identifiable pane -> loud
manual-paste chat alert instead, never silent.

CONTEXT_FULL recovery is deliberately narrower than a HUNG verdict. Only two
line-leading terminal 400 context-overflow errors on the identity-proven pane
may actuate it: cv prune mints a revived bounded COPY, then seat._resume replaces
the authoritative pane with that exact session while preserving its worktree,
room, dispatches and claim leases. A quiet/HUNG-looking seat is NEVER resumed by
this module. /clear remains the loud last resort only when cv cannot mint the
copy; once a copy exists it is preserved even if automatic resume fails.

POLL — LEAN, NO DEMONS (machine law: single-shot verbs, external cadence):
`helm seat autocompact` is one idempotent bounded pass; schedule it with a
systemd --user timer (`--install-timer` prints/writes the units) or any
Monitor/cron loop. A latch (state file) makes overlapping/frequent calls safe:
one fire per seat per episode, keyed by the strongest available session, pane
handle, or seat identity and re-armed only when context drops or that identity
changes. A missing session therefore never disables an identity-proven pane.
Verified injected/submitted compactions never time-rearm; manual alerts may
repeat after LATCH_TTL_S. An attempted send is not success: the composer must
visibly drain or transition before the actuation latches.

CLAUDE SEATS ARE SCANNED TOO (HELM_AUTOCOMPACT_CLAUDE=0 restores the old skip).
This module used to skip any transcript whose model said claude-*, on the
reasoning that "native seats autocompact fine". That reasoning was true and
still incomplete: CC's NATIVE autocompaction continues the turn it interrupted,
so a claude seat never went dark from it — but it fires only at CC's own
threshold, and the owner wants deliberate self-compaction at 80% for EVERY helm
agent. Firing /compact at a claude seat was unsafe for exactly one reason: a
DELIBERATE /compact ends the turn and nothing restarted it (bug class
`compaction-has-no-resume-leg`). resumeturn.py is that missing leg, so the
gate opens behind it — and it is the resume leg, not this flag, that makes it
safe: turning the flag on without that hook installed parks the seat.
"""
import fcntl
import glob
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time

from . import home

# Fire at >= this pct (override: HELM_AUTOCOMPACT_THRESHOLD). 80, not 90, per the
# owner (2026-07-29: "autocompact ACTUALLY fires at 80" for every proxied seat).
# This watchdog is THE enforcer, not a backup: CC's native autocompaction is
# STRUCTURALLY DEAD for a proxied seat. CC computes context% from the usage block
# on message_start, but the cli-proxy translator hardcodes that block to
# {input_tokens:0,output_tokens:0} with NO cache fields (verified in the fork,
# references/CLIProxyAPI/.../codex/claude/codex_claude_response.go:98) and fills
# the real numbers only on the terminal message_delta (ibid. 144-148). A codex
# seat's whole prompt lives in cache_read_input_tokens (measured live: 161,280 of
# 162,131), so CC's live gauge reads ~0% all session and
# CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (any percent) multiplies a numerator pinned near
# zero — it never crosses any threshold. This watchdog instead reads the persisted
# TRANSCRIPT (the merged final usage, real numbers) and injects /compact, so 80%
# here is the one firing point that actually fires. The minted systemd unit runs
# `helm seat autocompact --once` with no --threshold, so this default IS the
# fleet's live trigger. seat.py's AUTOCOMPACT_PCT_OVERRIDE is aligned to the same
# 80 so the two knobs can never imply different firing points.
DEFAULT_THRESHOLD = 80
LATCH_TTL_S = 15 * 60       # manual alerts may repeat while still actionable
FRESH_S = 6 * 3600          # older transcript = not this pane's live context
CC_ASSUMED_WINDOW = 200000  # CC's hardcoded window for non-claude models
TAIL_BYTES = 512 * 1024     # bounded tail reads (transcripts + proxy.log)
DEFAULT_INTERVAL_S = 60     # bounded scan cadence; one large turn can cross 80%
SUBMIT_VERIFY_READS = 5     # ~1s repaint window, never 20 CLI subprocesses
SUBMIT_VERIFY_INTERVAL_S = 0.2
SUBMIT_VERIFY_READ_TIMEOUT_S = 0.5
RECOVERY_SYSTEM_HEADROOM = 30000  # cv: resumed load is window + ~30k system
RECOVERY_MAX_WINDOW = 180000       # transcripts.prune_session's proven ceiling
RECOVERY_MIN_WINDOW = 2000

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


def scan_claude():
    """Whether a claude-model seat is measured and fired like every other one.

    ON by default since the resume leg landed. The kill switch exists because
    the gate is only safe WITH that leg: a fleet whose SessionStart
    resume-turn hook is not installed should set HELM_AUTOCOMPACT_CLAUDE=0
    rather than compact seats nothing will wake."""
    return str(home.env("AUTOCOMPACT_CLAUDE", "1")).lower() not in \
        ("0", "off", "no", "false")


def _window(family):
    """(window_tokens, source) for a family; (None, reason) when unknowable."""
    from . import seat
    from .seat_catalog import taught_window
    fam = seat.FAMILIES.get(family) or {}
    if fam.get("max_context"):
        # THE SAME NUMBER THE LAUNCH LINE TEACHES CC. A budget-narrowed family
        # reads against its budget, and the source names which key decided.
        win = taught_window(fam, fam["max_context"])
        if win != fam["max_context"]:
            return win, "FAMILIES.context_budget"
        return win, "FAMILIES.max_context"
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
        # A ZERO TOTAL IS NOT A MEASUREMENT — keep walking back.
        #
        # The newest assistant record can legitimately carry all-zero usage (an
        # aborted turn, a tool-only turn, the first record after a compact), and
        # this used to return that 0 as the seat's context. Measured 2026-07-26:
        # one seat read 0.0% of 360k with a 33-minute-old transcript of 2363
        # lines and 755 usage-bearing assistant records — thirty-three minutes
        # after it had been rescued from 102%. A second read 0.0% against 19341
        # lines. Meanwhile a third seat whose newest record carried real
        # numbers read 41.9% correctly.
        #
        # So the watchdog went blind on exactly the seats that had just done
        # something unusual, and 0% is the one value that guarantees it never
        # fires. A seat that climbs back to 100% after a compact would be
        # invisible to the surface built to catch it.
        if ctx:
            return ctx, (msg.get("model") or "")
    # Every usage record in the tail was zero: the context is UNKNOWN, not zero.
    # Returning None lets the caller say so; returning 0 would be a confident
    # lie that silences the alarm.
    return None


# ---------------------------------------------------------------------------
# read source 2 — proxy.log usage line (fallback; empty on today's gin format)
# ---------------------------------------------------------------------------

_TOK = {k: re.compile(r'"%s"\s*:\s*(\d+)' % k) for k in
        ("input_tokens", "cache_read_input_tokens",
         "cache_creation_input_tokens")}
_PROXY_SESSION = re.compile(
    r'"(?:session|session_id|sessionId)"\s*:\s*"([^"\\]+)"')
_PROXY_TIME = re.compile(
    r'"(?:timestamp|time|ts)"\s*:\s*"([^"\\]+)"')


def _proxy_line_age(line, path, is_last):
    stamp = _PROXY_TIME.search(line)
    if stamp:
        try:
            from datetime import datetime, timezone
            text = stamp.group(1).replace("Z", "+00:00")
            at = datetime.fromisoformat(text)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return max(0, time.time() - at.timestamp())
        except (ValueError, OverflowError):
            return None
    if not is_last:
        return None                       # file mtime belongs to a later row
    try:
        return max(0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


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
    lines = _tail_lines(path)
    last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i]
        if "input_tokens" not in ln:
            continue
        parts = {k: rx.search(ln) for k, rx in _TOK.items()}
        if not parts["input_tokens"]:
            continue
        sid = _PROXY_SESSION.search(ln)
        return (sum(int(m.group(1)) for m in parts.values() if m),
                sid.group(1) if sid else None,
                _proxy_line_age(ln, path, i == last))
    return None


# ---------------------------------------------------------------------------
# the read — one seat's context row
# ---------------------------------------------------------------------------

def read(seat_name):
    """One seat's context row: seat/family/window/ctx_tokens/pct/source/model/
    session/age_s/status. Fresh context is actionable when its pane identity is
    provable even if the session latch is absent or stale; every other status
    names exactly why the seat is a no-op (doctor prints it)."""
    from . import seat
    family, err = seat._seat_family(seat_name)
    if err:
        return {"seat": seat_name, "status": "unknown-seat"}
    d = seat._instance_dir(family, seat_name)
    rec = seat._spawn_record(d) or {}
    owned = rec if rec.get("seat") == seat_name else {}
    registered = owned.get("session")
    row = {"seat": seat_name, "family": family, "ctx_tokens": None,
           "pct": None, "source": None, "model": None, "session": None,
           "registered_session": registered,
           "pane_handle": owned.get("handle"),
           "pane_harness": owned.get("harness"), "age_s": None}
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
    row["headroom_tokens"] = max(0, win - row["ctx_tokens"])
    if row["source"] == "proxy.log" and not row["session"]:
        row["status"] = "proxy-log-unattributed"
    elif (row["model"] or "").startswith("claude") and not scan_claude():
        row["status"] = "claude-model"       # gate closed by configuration
    elif row["age_s"] is None:
        row["status"] = "context-undated"
    elif row["age_s"] > _int_env(
            "AUTOCOMPACT_FRESH_S", FRESH_S):
        row["status"] = "stale"              # not this pane's live context
    elif not registered:
        # Fresh measurable context plus an identity-proven pane is actionable;
        # only the session-derived latch rung is absent.
        row["status"] = "session-unbound"
    elif row["session"] != registered:
        row["status"] = "session-mismatch"
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


def _latch_identity(row):
    """Best stable episode identity, independent of whether a session is bound.

    A bound registered session is strongest. When the register is unbound, its
    pane handle outranks the transcript's unauthoritative session: replacing the
    pane is a new actuation episode even while the old hot transcript persists.
    The transcript session and exact seat are read-only fallbacks for rows with
    no pane capability.
    """
    return (row.get("registered_session") or row.get("pane_handle") or
            row.get("session") or row.get("seat"))


def _episode_complete(entry, row):
    """Whether the prior high-context episode has observably ended."""
    if not entry:
        return False
    prior = entry.get("identity") or entry.get("session")
    current = _latch_identity(row)
    if prior and current and prior != current:
        return True                         # proven new identity = new episode
    fired_pct = entry.get("pct")
    return fired_pct is not None and row.get("pct") is not None and \
        row["pct"] < fired_pct              # compaction made context shrink


def _latch_blocks(entry, row, now):
    """True while a prior verified fire should suppress another attempt."""
    if not entry or _episode_complete(entry, row):
        return False
    if entry.get("mode") == "pending":
        return False  # pre-#171 state: text was observed, Enter was never proven
    if entry.get("mode") in ("manual", "clear-manual") and \
            now - (entry.get("fired_at") or 0) > \
            _int_env("AUTOCOMPACT_LATCH_TTL", LATCH_TTL_S):
        return False                        # repeat a still-actionable alert
    return True                             # verified actuation waits for pct drop


# ---------------------------------------------------------------------------
# the fire — /compact into the seat's pane
# ---------------------------------------------------------------------------

def resolve_pane(seat_name, adapter=None):
    """Resolve one identity-proven pane without sending input.
    Seat owns pane identity; every actuator consumes that one proof."""
    from . import seat
    return seat._resolve_registered_pane(seat_name, adapter=adapter)


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EMPTY_COMPOSER = re.compile(r"^\s*❯\s*$")
_API_400 = re.compile(r"^\s*(?:API\s+Error:|HTTP(?:\s+Error)?)\s*400\b",
                      re.IGNORECASE)
_CONTEXT_400 = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bprompt is too long\b",
    r"\bmaximum context length\b",
    r"\bcontext (?:window |length )?(?:is )?(?:too long|exceed(?:s|ed)?|overflow(?:ed)?)\b",
    r"\bcontext limit (?:is )?(?:reached|exceed(?:ed|s)?)\b",
    r"\btoken count exceed(?:s|ed)?\b.*\bmaximum\b",
))


def _visible_lines(tail):
    return [line for line in (
        _ANSI.sub("", raw).strip() for raw in (tail or "").splitlines()) if line]


def _exact_command_line(line, command):
    text = (line or "").strip()
    return text.startswith("❯") and text[1:].strip() == "/" + command


def _command_pending(tail, command):
    """Whether Claude's current composer holds only the exact command.

    The `❯` glyph plus current-composer proof are both required; markdown
    blockquotes, earlier rendered conversation text, command arguments, and
    prompts followed by newer semantic content can never authorize Enter.
    """
    from . import seat
    return _exact_command_line(seat._current_prompt_line(tail), command)


def _compact_pending(ad, handle):
    """True only when the visible composer itself holds an unsent /compact.
    Mentions in transcript/history are not pending input."""
    return _command_pending(ad.read(handle, limit=2000), "compact")


def _verify_compact_submission(ad, handle, before_tail):
    """Prove a pane send reached Claude's composer, not only its transport.

    A successful adapter RPC says bytes reached the metaharness. It does not say
    Claude consumed Enter: measured live, two of three panes left their composer
    and cursor unchanged after the RPC returned success. Poll one bounded repaint
    window and require a command drain or a genuine post-send transition. False
    failure remains retryable next cadence; false success would latch the dying
    seat permanently.
    """
    from . import harness, seat
    before_clean = _ANSI.sub("", before_tail or "")
    before_state, _ = seat._classify_pane_tail(before_clean)
    saw_command = _command_pending(before_tail, "compact")
    command_visible = saw_command
    readable = 0
    last = "no readable post-send pane frame"
    for n in range(1, SUBMIT_VERIFY_READS + 1):
        if n > 1 and SUBMIT_VERIFY_INTERVAL_S:
            time.sleep(SUBMIT_VERIFY_INTERVAL_S)
        try:
            if isinstance(ad, harness._CLIAdapter):
                tail = ad.read(handle, limit=2000,
                               timeout=SUBMIT_VERIFY_READ_TIMEOUT_S)
            else:
                tail = ad.read(handle, limit=2000)
        except Exception as e:  # noqa: BLE001 — unreadable is UNKNOWN, not success
            last = "pane re-read failed: %s" % e
            continue
        clean = _ANSI.sub("", tail or "")
        if not clean.strip():
            last = "pane re-read returned no visible tail"
            continue
        readable += 1
        strike = _new_context_400(before_clean, clean)
        if strike:
            strike["detail"] = (
                "pane consumed input but produced a new context-overflow 400; "
                "recording the first causal strike, never success")
            return "context-400", strike
        state, _ = seat._classify_pane_tail(clean)
        current = seat._current_prompt_line(clean)
        command_visible = _exact_command_line(current, "compact")
        # A new live turn is positive consumption evidence even when Claude still
        # renders the submitted message with the same `❯ /compact` glyph.
        if state == "RUNNING" and before_state != "RUNNING":
            return True, "pane entered RUNNING after %d pane read(s)" % n
        if _EMPTY_COMPOSER.match(current or "") and saw_command:
            return True, "current composer drained after %d pane read(s)" % n
        if command_visible:
            saw_command = True
            last = "exact /compact still occupies the current composer"
            continue
        # Tail movement, UNKNOWN classification, and an unrelated semantic line
        # are not command postconditions. Only a command appearance+drain or a
        # new RUNNING state can turn transport acceptance into actuation success.
        last = "no exact-command drain or new RUNNING state was visible"
    if not readable:
        return None, "%s after %d bounded read(s)" % (
            last, SUBMIT_VERIFY_READS)
    if command_visible:
        return False, "%s after %d bounded read(s)" % (
            last, SUBMIT_VERIFY_READS)
    return None, "%s after %d bounded read(s)" % (last, SUBMIT_VERIFY_READS)


def _context_400_line(line):
    return bool(_API_400.match(line) and
                any(rx.search(line) for rx in _CONTEXT_400))


def _context_400_lines(tail):
    return [line for line in _visible_lines(tail) if _context_400_line(line)]


def _context_400_counts(tail):
    counts = {}
    for line in _context_400_lines(tail):
        counts[line] = counts.get(line, 0) + 1
    return counts


def _new_context_400(before, after):
    """The newest added context-400 fingerprint and its prior count, or None."""
    prior = _context_400_counts(before)
    fresh = _context_400_counts(after)
    for line in reversed(_context_400_lines(after)):
        if fresh[line] > prior.get(line, 0):
            return {"line": line, "baseline": prior.get(line, 0)}
    return None


def _overflow_400(tail):
    """True only when the pane ENDS in two exact context-overflow 400s.

    Historical errors followed by output or a live composer are not terminal
    state. Broad word bags are unsafe here: e.g. "prompt caching maximum
    breakpoints" is a 400 but not CONTEXT_FULL. The final two visible lines must
    each carry one of the provider phrasings that specifically names context
    length, a too-long prompt, or token count exceeding its maximum.
    """
    final = _visible_lines(tail)[-2:]
    return len(final) == 2 and all(_context_400_line(line) for line in final)


def _pane_action(row, adapter, action, repair=True, require_context=False,
                 require_session=True, for_send=False, identity_session=None):
    """Run one pane read/write inside the seat's lifecycle transaction.

    `for_send=True` is for actions that only WRITE. An orphaned pane is
    read-blind but send-capable, and resolving through the observation bar made
    every injection refuse once `_pane_live` started answering honestly about
    orphaned panes — which killed the autocompact rescue for the 30-of-34 panes
    that were orphaned. The identity checks above are untouched; only the
    liveness bar moves, and it moves because a send does not need a renderer."""
    from . import seat
    family, err = seat._seat_family(row["seat"])
    if err:
        return None, err
    d = seat._instance_dir(family, row["seat"])
    with seat._seat_lifecycle_lock(d):
        rec = seat._spawn_record(d)
        if not rec:
            return None, "no authoritative spawn handle after context scan"
        if rec.get("seat") != row["seat"]:
            return None, "spawn identity changed after context scan"
        registered = row.get("registered_session")
        if require_session and not registered:
            return None, "spawn register has no bound session identity"
        if rec.get("session") != registered:
            return None, "registered session changed after context scan"
        if not require_session and row.get("pane_handle") and \
                rec.get("handle") != row["pane_handle"]:
            return None, "registered pane changed after context scan"
        if require_context and (not row.get("session") or
                                rec.get("session") != row["session"]):
            return None, "context session changed before pane actuation"
        ad, handle, detail = seat._resolve_registered_pane(
            row["seat"], d=d, adapter=adapter, repair=repair, locked=True,
            for_send=for_send, identity_session=identity_session)
        if ad is None or handle is None:
            return None, detail
        return action(ad, handle, detail), None


def _recovery_budget(row):
    """The real-token tail cv should keep while leaving resume-system headroom."""
    window = int(row.get("window") or CC_ASSUMED_WINDOW)
    return max(RECOVERY_MIN_WINDOW,
               min(RECOVERY_MAX_WINDOW, window - RECOVERY_SYSTEM_HEADROOM))


def _prune_context(row):
    """Mint and verify one revived, bounded copy of the measured seat session.

    Returns (resumable_sid, detail, clear_safe). Helm pins the output UUID with
    --to, so every failure can probe that exact path. /clear is permitted only
    when no copy exists and the old source still does; once target bytes exist,
    any report/validation failure is manual recovery and never destructive.
    """
    import subprocess
    import uuid
    from . import seat
    sid = row.get("session")
    if not sid or sid != row.get("registered_session"):
        return None, "context and registered session no longer agree", False
    family, err = seat._seat_family(row["seat"])
    if err:
        return None, err, False
    d = seat._instance_dir(family, row["seat"])
    source = seat._seat_session_path_by_id(d, sid)
    if not source:
        return None, "source session is not one exact seat transcript", False
    budget = _recovery_budget(row)
    target = str(uuid.uuid4())
    target_glob = os.path.join(d, "claude", "projects", "*", target + ".jsonl")
    if glob.glob(target_glob):
        return None, "pinned prune target already exists", False
    cmd = ["cv", "prune", sid, "--to", target, "--window", str(budget),
           "--thinking", "--json"]
    env = home.cv_env()
    env["CLAUDE_CONFIG_DIR"] = os.path.join(d, "claude")

    def failed(detail):
        copy = seat._seat_session_path_by_id(d, target)
        target_exists = bool(glob.glob(target_glob))
        source_kept = seat._seat_session_path_by_id(d, sid)
        if target_exists:
            state = "exact copy" if copy else "target bytes"
            return None, "%s; pruned %s %s survive for manual recovery" % (
                detail, state, target), False
        if not source_kept:
            return (None, "%s; source transcript disappeared — refusing /clear"
                    % detail, False)
        return None, detail, True

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           env=env)
    except FileNotFoundError:
        return failed("cv prune unavailable: cv is not installed")
    except subprocess.TimeoutExpired:
        return failed("cv prune timed out after 300s")
    if p.returncode != 0:
        text = ((p.stderr or "") + (p.stdout or "")).strip()
        return failed("cv prune failed: " + text[-300:])
    try:
        report = json.loads((p.stdout or "").strip())
    except (TypeError, ValueError):
        return failed("cv prune returned no valid JSON report")
    if not isinstance(report, dict) or report.get("newId") != target:
        return failed("cv prune report did not identify the pinned target")
    if report.get("sourceId") not in (None, sid):
        return failed("cv prune report identified a different source session")
    copy = seat._seat_session_path_by_id(d, target)
    if not copy:
        return failed("cv prune target is not one exact seat transcript")
    if not seat._seat_session_path_by_id(d, sid):
        return failed("cv prune did not preserve the source transcript")
    measured = _transcript_ctx(copy)
    tokens = measured[0] if measured else None
    if tokens is None:
        return failed("cv prune target has no measurable revived usage")
    if tokens > budget:
        return failed("cv prune target records {:,} tokens, above {:,} budget".format(
            tokens, budget))
    return target, ("cv prune --revive minted session %s with %s/%s recorded "
                    "tokens" % (target[:8], format(tokens, ","),
                                 format(budget, ","))), False


def _fire_prune_resume(row, ad, handle, detail, tail):
    """Recover a proven context wall; /clear exists only as prune's fallback."""
    from . import seat
    new_sid, prune_detail, clear_allowed = _prune_context(row)
    if not new_sid:
        if not clear_allowed:
            return "recovery-manual", ("%s; /clear was NOT used — inspect the "
                                       "preserved transcript state" % prune_detail)
        mode, clear_detail = _fire_clear(row["seat"], ad, handle, detail, tail)
        return mode, "%s; LAST RESORT /clear because %s" % (
            clear_detail, prune_detail)
    rc = seat._resume(row["seat"], [], _locked=True, target_sid=new_sid,
                      expected_session=row.get("registered_session"), adapter=ad)
    if rc:
        return ("resume-manual", "%s; automatic exact-session resume failed; "
                "the original and pruned session %s both survive — run `helm "
                "seat resume %s` after inspecting the pane" %
                (prune_detail, new_sid, row["seat"]))
    return ("pruned-resumed", "%s; resumed exact session %s" %
            (prune_detail, new_sid))


def _context_recovery_action(row, adapter, fire, limit, evidence):
    """Read, prove, and recover one context wall under pane identity."""
    from . import harness

    def action(ad, handle, detail):
        try:
            tail = ad.read(handle, limit=limit)
        except harness.HarnessError:
            return False, None, None
        if not evidence(tail):
            return False, None, detail
        if not fire:
            return True, None, detail
        mode, result = _fire_prune_resume(row, ad, handle, detail, tail)
        return True, mode, result

    result, err = _pane_action(
        row, adapter, action, repair=fire, require_context=True)
    return result if result is not None else (False, None, err)


def _overflow_action(row, adapter, fire):
    """Detect and recover one proven repeated context overflow."""
    return _context_recovery_action(
        row, adapter, fire, 12000, _overflow_400)


def _pane_preview_batch(rows, adapter):
    """Inventory each recorded harness once for trustworthy negative evidence.

    Inventory preview can exclude a terminal repeated-400 signature; it can never
    authorize recovery. Missing, empty, ambiguous, mismatched, or non-live rows
    stay absent so their callers fall back to the identity-locked fresh read.
    """
    from . import harness, seat
    claims = {}
    for row in rows:
        key = (row.get("pane_harness"), row.get("pane_handle"))
        if all(isinstance(v, str) and v for v in key):
            claims[key] = claims.get(key, 0) + 1
    by_harness = {}
    for (name, handle), count in claims.items():
        if count == 1:
            by_harness.setdefault(name, set()).add(handle)

    adapters, previews = {}, {}
    detected = harness.detect() if any(
        adapter is None or adapter.name != name for name in by_harness) else None
    for name, handles in by_harness.items():
        ad = adapter if adapter is not None and adapter.name == name else None
        if ad is None and detected is not None and detected.name == name:
            ad = detected
        if ad is None:
            cls = harness.ADAPTERS.get(name)
            path = shutil.which(cls.bin) if cls else None
            ad = cls(path) if path else None
        if ad is None:
            continue
        adapters[name] = ad
        try:
            inventory = ad.list()
        except harness.HarnessError:
            continue
        matches = {}
        for pane in inventory:
            handle = pane.get("handle")
            if handle in handles:
                matches.setdefault(handle, []).append(pane)
        for handle, panes in matches.items():
            if len(panes) != 1 or not seat._pane_live(panes[0]):
                continue
            preview = panes[0].get("preview")
            if isinstance(preview, str) and preview.strip():
                previews[(name, handle)] = preview
    return adapters, previews


def _overflow_preview_excludes(row, previews):
    preview = previews.get((row.get("pane_harness"), row.get("pane_handle")))
    return preview is not None and not _overflow_400(preview)


def _causal_context_400_action(row, adapter, entry, fire):
    """Recover a context 400 caused by our prior verified submission attempt.

    The first post-send 400 is persisted, never latched as success. Seeing that
    same exact context error on the next cadence supplies the second independent
    observation the destructive recovery rung requires, even when Claude redrew
    an empty prompt after the error instead of printing a duplicate line.
    """
    line = entry.get("context_400_line")
    baseline = entry.get("context_400_baseline")

    def evidence(tail):
        counts = _context_400_counts(tail)
        return (isinstance(line, str) and isinstance(baseline, int) and
                counts.get(line, 0) > baseline)

    # Match the exact 2k pre-send observation window that minted the fingerprint;
    # widening here would reveal older scrollback absent from its baseline.
    return _context_recovery_action(
        row, adapter, fire, 2000, evidence)


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
    """Queue /clear once, but only into a positively empty current composer."""
    from . import composers, harness
    if _command_pending(tail, "clear"):
        return "clear-pending", "pane %s already has /clear queued" % handle
    # This door's successful read is itself a positive output observation;
    # classify still keeps an empty/unlocatable tail in CANNOT_TELL.
    pane = {"handle": handle, "last_output_at": True}
    try:
        current = ad.read(handle, limit=2000)
        if _command_pending(current, "clear"):
            return "clear-pending", "pane %s already has /clear queued" % handle
        state, body, why = composers.classify(pane, current)
    except (harness.HarnessError, OSError) as e:
        state, body, why = composers.classify(
            pane, None, read_error=str(e))
    if state != composers.CLEAR or body != "":
        return ("recovery-manual", "pane %s current composer is %s — %s; "
                "/clear was NOT injected" % (handle, state, why))
    try:
        ad.send(handle, "/clear", enter=True)
    except harness.HarnessError as e:
        return "clear-manual", str(e)
    return ("clear-pending", "%s; /clear injected into pane %s; waiting for "
            "SessionStart before onboarding" % (detail, handle))


def _retry_rebrief(row, adapter):
    def action(ad, handle, detail):
        mode, brief_detail = _rebrief_after_clear(row["seat"], ad, handle)
        return mode, "%s; %s" % (detail, brief_detail)

    result, err = _pane_action(row, adapter, action, for_send=True)
    return result if result is not None else ("clear-needs-brief", err)


# ---------------------------------------------------------------------------
# a fresh session between rows (task/2944)
# ---------------------------------------------------------------------------
# A LIMITED FAMILY PAYS FOR ITS CONTEXT ON EVERY REQUEST. Each request re-sends
# the whole context, so a seat that carries one row's history into the next
# row pays for that history again on every request of the new row. MEASURED on
# kimi (task/2944): its typical request carried 329k tokens.
#
# THIS RUNG ADDS NO SECOND /clear MECHANISM. It only decides WHEN. A seat whose
# family declares `fresh_session_floor` gets the same identity-proven
# `_fire_clear` as a context wall, and the same clear-pending -> SessionStart
# -> onboarding rungs at the top of `check()` finish it. The onboarding brief
# re-arms the beacon and reads `helm dispatch list --mine --open`, so the next
# row the seat takes starts in a clean context.
#
# BETWEEN ROWS, NEVER AT A ROW'S ARRIVAL. A dispatch wakes the seat through its
# beacon in seconds, long before a 60 s pass can see the seat idle, so a clear
# keyed on the new row would land in the middle of that row's first turn. The
# clear comes after a row is DONE instead: no owed row names the seat, it holds
# no live claim, and it has been silent for FRESH_ROW_QUIET_S. The next row
# then arrives in a context that holds only the onboarding.
#
# EVERY UNKNOWN REFUSES. An unreadable dispatch or claims ledger is never
# "between rows", and neither is a claim with no readable holder, because that
# claim cannot be ruled out as this seat's.

# How long the seat's transcript must be untouched before its context is
# dropped. A seat that parks between two turns of one piece of work (a gate it
# started, an owner reply it is waiting for) resumes inside this window.
FRESH_ROW_QUIET_S = 5 * 60
# The latch's `why` for a between-rows clear, so the shared clear-pending and
# onboarding rungs can tell it from a context-wall recovery.
FRESH_ROW = "fresh-row"
# Its modes that post nothing: a routine clear, and a completed onboarding. A
# seat cleared but NOT re-briefed is deaf, so that mode is always posted.
_ROUTINE_FRESH = ("clear-pending", "cleared")
# At most one between-rows clear per seat inside this spacing. The floor is
# what keeps a freshly onboarded seat from qualifying again; the spacing bounds
# the cost when the floor is wrong. In a room busy enough that the onboarding
# reads alone cross the floor, the seat would otherwise be cleared and
# re-onboarded every few minutes, paying for a whole onboarding each time.
FRESH_ROW_SPACING_S = 15 * 60
_FRESH_KEY = "_fresh"       # state: {seat: epoch of its last between-rows clear}


def _fresh_floor(family):
    """The family's declared fresh_session_floor in tokens, or None."""
    from . import seat
    return (seat.FAMILIES.get(family) or {}).get("fresh_session_floor")


def _fresh_candidate(row):
    """The cheap half of the rung: facts the scan has already measured.

    An exact registered session (status "ok") is required, as it is for the
    other context-destroying act in this module. A /compact may proceed on a
    weaker identity; a /clear cannot be undone."""
    floor = _fresh_floor(row.get("family"))
    return bool(floor and row.get("status") == "ok"
                and (row.get("ctx_tokens") or 0) >= floor
                and row.get("age_s") is not None
                and row["age_s"] >= FRESH_ROW_QUIET_S)


def _between_rows(seat_name):
    """(True, why) only on a MEASURED absence of work; otherwise (False, why).

    Owed rows are counted under every name the seat answers to (its live
    rename aliases), because a row addressed to an alias is this seat's."""
    from . import dispatches, seats
    names = set()
    for name in seats.seat_names(seat_name):
        key, _err = seats._canonical_recipient(name)
        if key:
            names.add(str(key))
    counts, unavailable = dispatches.open_recipients()
    if counts is None:
        return False, "the dispatch ledger could not be read (%s)" % unavailable
    owed = 0
    for recipient, n in counts.items():
        key, _err = seats._canonical_recipient(recipient)
        if key and str(key) in names:
            owed += n
    if owed:
        return False, "%d owed dispatch row(s) name this seat" % owed
    claims = seats._live_claims()
    if claims is None:
        return False, "the claims ledger could not be read"
    for resource, lease in sorted(claims.items()):
        if resource == "_fence":
            continue
        holder = lease.get("holder") if isinstance(lease, dict) else None
        key, _err = seats._canonical_recipient(holder)
        if not key:
            return False, ("live claim %s has no readable holder, so it cannot "
                           "be ruled out as this seat's" % resource)
        if str(key) in names:
            return False, "the seat holds live claim %s" % resource
    return True, "no owed dispatch row names the seat and it holds no live claim"


def _fresh_spacing_refusal(st, seat_name, now):
    """'' when this seat may take a between-rows clear now; else why not."""
    fresh = st.get(_FRESH_KEY)
    last = fresh.get(seat_name) if isinstance(fresh, dict) else None
    if isinstance(last, (int, float)) and now - last < FRESH_ROW_SPACING_S:
        return ("the last between-rows clear was %s ago, inside the %s "
                "spacing" % (_age(now - last), _age(FRESH_ROW_SPACING_S)))
    return ""


def _fire_fresh(row, adapter):
    """Queue /clear into one idle between-rows seat through `_fire_clear`.

    IDLE is checked here, ahead of `_fire_clear`, because that helper was built
    for a context wall and asks only whether the composer is empty. An open
    turn can show an empty composer too, and a /clear typed into it would wait
    in the queue and then drop the context of the work that turn was doing."""
    from . import harness, seat

    def action(ad, handle, detail):
        try:
            tail = ad.read(handle, limit=2000)
        except (harness.HarnessError, OSError) as e:
            return "refused-unknown", "pane read failed: %s" % e
        state, blocked_on = seat._classify_pane_tail(_ANSI.sub("", tail or ""))
        if state != "IDLE":
            return ("refused-not-idle",
                    "pane %s turn state is %s%s; a fresh session needs an "
                    "IDLE prompt" % (handle, state or "UNKNOWN",
                                     " (%s)" % blocked_on if blocked_on else ""))
        return _fire_clear(row["seat"], ad, handle, detail, tail)

    result, err = _pane_action(row, adapter, action, require_context=True)
    return result if result is not None else ("refused-unknown", err)


def _fresh_action(row, adapter, fire):
    """(mode, detail) for one fresh candidate, or (None, why) when refused.

    The ledger reads sit behind a catch because they are the only reads in
    this pass that this pass did not already depend on. A failure in them is
    an UNKNOWN for this seat, never an exception that ends the whole pass and
    takes every other seat's /compact with it."""
    try:
        between, why = _between_rows(row["seat"])
    except Exception as e:  # noqa: BLE001 — an unread ledger is UNKNOWN
        return None, "the between-rows read failed: %s: %s" % (
            e.__class__.__name__, e)
    if not between:
        return None, why
    row["would_fresh"] = True
    if not fire:
        return None, why
    mode, detail = _fire_fresh(row, adapter)
    if mode != "clear-pending":
        return None, "%s: %s" % (mode, detail)
    return mode, "%s; %s" % (why, detail)


def _actuate_compact(row, ad, handle, detail, tail, payload, success_mode,
                      label):
    from . import harness
    try:
        ad.send(handle, payload, enter=True)
    except harness.HarnessError as e:
        return ("refused-failed-to-submit",
                "pane %s rejected %s: %s" % (handle, label, e))
    accepted, proof = _verify_compact_submission(ad, handle, tail)
    if accepted is True:
        return success_mode, "%s; %s" % (detail, proof)
    if accepted == "context-400":
        row["context_400_strike"] = proof
        return "context-400", "%s; %s" % (detail, proof["detail"])
    if accepted is False:
        return ("refused-failed-to-submit",
                "pane %s did not consume %s — %s" % (handle, label, proof))
    return ("refused-unknown",
            "pane %s %s is unproven — %s" % (handle, label, proof))


def _fire(row, adapter):
    """Actuate /compact in one identity-proven pane and verify consumption.

    A session binding is latch evidence, not actuation authority. The spawn
    register's pane handle authorizes the write, while the proven current
    composer answers the separate safety question. Arbitrary drafts are never
    displaced; an exact existing /compact authorizes Enter only, and neither
    injection nor submission succeeds until a bounded reread proves it drained.
    """
    from . import seat

    def action(ad, handle, detail):
        try:
            tail = ad.read(handle, limit=2000)
        except Exception as e:  # noqa: BLE001 — any unreadable pane is UNKNOWN
            return "refused-unknown", "pane read failed: %s" % e
        clean_tail = _ANSI.sub("", tail or "")
        state, blocked_on = seat._classify_pane_tail(clean_tail)
        composer = seat._current_prompt_line(clean_tail)

        # RUNNING stays first. Claude renders a SUBMITTED message with the same
        # `❯` glyph, so while an open turn is visible the apparent composer may be
        # history rather than editable input. Enter there would queue a duplicate.
        if state == "RUNNING":
            return ("refused-running",
                    "pane %s has an open turn (esc to interrupt); /compact was "
                    "not queued mid-tool-call" % handle)
        if state == "BLOCKED_ON_HUMAN":
            return ("refused-blocked-on-human",
                    "pane %s is BLOCKED_ON_HUMAN%s; /compact would discard the "
                    "pending prompt" %
                    (handle, " on %s" % blocked_on if blocked_on else ""))

        # Outside a live turn, an exact command in the proven CURRENT composer is
        # explicit user intent. Submit Enter only — retyping would turn `/compact`
        # into `/compact/compact` — then prove the TUI, not merely the adapter RPC,
        # consumed it.
        if _command_pending(tail, "compact"):
            return _actuate_compact(
                row, ad, handle, detail, tail, "", "submitted",
                "Enter for exact /compact")

        if state not in ("IDLE", "CONTEXT_FULL"):
            return ("refused-unknown",
                    "pane %s turn state is %s%s; /compact requires an IDLE or "
                    "CONTEXT_FULL prompt" %
                    (handle, state or "UNKNOWN",
                     " (%s)" % blocked_on if blocked_on else ""))
        if not _EMPTY_COMPOSER.match(composer or ""):
            return ("refused-unknown",
                    "pane %s composer contains unsent input; /compact was not "
                    "injected" % handle)
        return _actuate_compact(
            row, ad, handle, detail, tail, "/compact", "injected",
            "/compact + Enter")

    # The pane handle is the actuation capability. Session equality stays on the
    # destructive prune/resume path; ordinary /compact needs a readable current
    # composer and uses the best session/handle/seat identity only for latching.
    result, err = _pane_action(
        row, adapter, action, require_session=False, for_send=False,
        identity_session=(row.get("session")
                          if row.get("status") == "session-mismatch" else None))
    return result if result is not None else ("refused-unknown", err)


def _fire_text(row, mode, detail):
    if row.get("fresh_row"):
        # Only the failure is posted (check() skips _ROUTINE_FRESH); the
        # routine texts must still not read as an overflow wherever shown.
        if mode == "clear-needs-brief":
            return ("⚠️ FRESH SESSION: seat %s was cleared between rows but its "
                    "onboarding brief still needs injection, so its beacon is "
                    "NOT armed — %s" % (row["seat"], detail))
        return ("🧹 FRESH SESSION: seat %s, between rows, %s — %s"
                % (row["seat"], "re-onboarded in a clean context"
                   if mode == "cleared" else "cleared (%s)" % mode, detail))
    if mode == "pruned-resumed":
        return ("🛟 CONTEXT RECOVERY: cv prune --revive + exact resume restored "
                "seat %s without discarding its session — %s" %
                (row["seat"], detail))
    if mode == "resume-manual":
        return ("⚠️ CONTEXT RECOVERY: seat %s was pruned safely but automatic "
                "resume failed; /clear was NOT used — %s" %
                (row["seat"], detail))
    if mode == "recovery-manual":
        return ("⚠️ CONTEXT RECOVERY: seat %s needs manual inspection; recovery "
                "could not safely prove a resumable copy and /clear was NOT used "
                "— %s" % (row["seat"], detail))
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
    elif mode == "submitted":
        head = ("🌀 AUTOCOMPACT: existing /compact submitted in seat %s" %
                row["seat"])
    else:
        head = ("⚠️ AUTOCOMPACT: seat %s needs /compact NOW (injection "
                "unavailable — paste it into the pane)" % row["seat"])
    return ("%s at %.0f%% (%s/%s, %s) — pre-empting the 100%% proxy hang. %s"
            % (head, row["pct"], k(row["ctx_tokens"]), k(row["window"]),
               row["source"], detail))


def _recovery_session_changed(entry, row):
    """Whether SessionStart has bound a different session after /clear.

    A newer transcript is only an artifact: cv, a crash stub, or Claude startup
    can create it before spawn.json changes. Only the authoritative registered
    session proves that /clear completed and permits onboarding injection.
    """
    prior = (entry or {}).get("registered_session") or (entry or {}).get("session")
    current = row.get("registered_session")
    return bool(prior and current and prior != current)


def _resume_recovery_complete(entry, row):
    """Only the authoritative spawn binding completes prune/resume recovery.

    The pruned copy becomes the newest transcript before resume succeeds; using
    row.session would therefore unlatch the old pane and fall through to /clear.
    """
    prior = (entry or {}).get("registered_session") or (entry or {}).get("session")
    current = row.get("registered_session")
    return bool(prior and current and prior != current)


HOT_BUCKET = 5   # re-alert only once the seat has climbed another 5 points

# An unbound or mismatched session is still actionable when its pane resolves;
# session identity is a latch/destructive-recovery rung, not ordinary /compact's
# capability. claude-model remains the configured no-op when native compaction is
# disabled. UNKNOWN pane identity still refuses inside `_pane_action`.
_ACTIONABLE = ("ok", "session-unbound", "session-mismatch")
_BY_DESIGN = _ACTIONABLE + ("claude-model",)


def _live_seat_names():
    """Seats with a live claude process, or None when the census cannot run.

    None is NOT an empty set. Treating "I could not look" as "nothing is alive"
    would silently reclassify every hot seat as dead and mute the alert, which
    is the failure mode this whole rung exists to prevent.

    AND A PARTIAL LOOK IS A LOOK THAT DID NOT HAPPEN. `_live_seats` returns the
    names it read AND whether it could read every claude on the host; a set of
    names taken from an incomplete census is exactly the confident-looking
    value this rung refuses. `blind` is `orcaadopt.cannot_look`'s reason — the
    one implementation of that rule — and it arrives here as None, the value
    this function already meant it by."""
    try:
        from . import proxywatch
        names, blind, per_seat = proxywatch._live_seats()
        # FOLDED THROUGH THE ONE RULE. This rung acts on a single yes/no, so a
        # refusal about ONE seat still costs it the whole pass — deliberately.
        # `fold_blind` is where that rule lives; re-deriving it here is how one
        # surface starts calling a fleet measured while another calls it blind.
        return None if proxywatch.fold_blind(blind, per_seat) else names
    except Exception:                        # noqa: BLE001 — census is advisory
        return None


def _dead_holding_state(row, thr):
    """Over threshold with NO live process behind it.

    A seat nobody launched cannot wedge, so telling anyone to "re-bind it or
    /clear it" is advice about a pane that does not exist. Measured 2026-07-29:
    two codex seats (93.8%, 6.2d; 103.0%, 23.7h) were reported as hot for
    an hour while the process census showed NEITHER had a claude process at
    all. Their percentages came from abandoned transcript files.

    This is the same law proxywatch already states — a seat nobody launched is
    not hung, it is off — and the hot rung shipped without it."""
    pct = row.get("pct")
    return (pct is not None and pct >= thr
            and row.get("status") not in _BY_DESIGN
            and row.get("pane_live") is False)


def _hot_blocked(row, thr):
    """A seat this pass can MEASURE but is not allowed to ACT on.

    THE HOLE THIS CLOSES (measured 2026-07-29, and it cost a seat). The fire
    path ends in `if row["status"] != "ok" or row["pct"] < thr: continue` — so a
    seat at 103% whose session is stale or unbound is scanned, scored, and then
    dropped in silence. Chat posts are emitted for FIRED rows only. The result
    is a watchdog that knows a seat is dying and says nothing, which reads to
    every observer exactly like a watchdog reporting all-clear.

    Refusing to inject into a pane whose identity is unproven is CORRECT — that
    is how a directive reaches the wrong agent — so the refusal stays. What was
    wrong is that the refusal was indistinguishable from health. A guard that
    declines to act must still be loud, or its correctness is the thing that
    hides the failure.

    Live at the moment this landed: one codex seat at 93.8% (session-unbound,
    6.1d) and another at 103.0% (stale, 22.7h), neither fired on, neither
    reported.
    ds4pro reached 1,016,359 tokens across 3,441 turns and five days with ZERO
    compactions before it wedged into an unrecoverable 400 — its live session
    was never the registered one, so every actionable path declined, quietly.
    """
    pct = row.get("pct")
    return (pct is not None and pct >= thr
            and row.get("status") not in _BY_DESIGN
            and row.get("pane_live") is not False)


def _silently_dropped(row, thr):
    """THE BACKSTOP — the owner's AS-PREVENTED for this whole module (2026-07-29:
    "a guard that CATCHES a seat over-threshold that did NOT compact — it must
    not be able to fail silently again").

    Fire, latch, hot, dead are each a SPECIFIC answer to "why didn't this seat
    compact". This is the GENERAL one, and it is deliberately independent of
    them: an over-threshold row carrying none of their flags has fallen through
    every net, and a watchdog that drops a dying seat in silence reads to every
    observer exactly like one reporting all-clear — the original 100%-wedge, one
    layer out. Every specific rung above was itself a hole once (_hot_blocked and
    _dead_holding_state both shipped without covering a case that then cost a
    seat); this invariant cannot be satisfied by an over-threshold seat that no
    rung claimed, so a future edit that reopens a hole lights THIS alarm instead
    of going quiet. It NEVER actuates a pane — it only refuses to let one go
    unspoken. Total predicate (correct standalone or after the hot/dead rungs
    continue out), so it is verifiable in isolation, not only through check().

    claude-model is the single by-design silence: with HELM_AUTOCOMPACT_CLAUDE=0
    a claude seat is deliberately left to CC's own native autocompaction (which
    DOES work for a real claude model), so it is not a dropped seat."""
    pct = row.get("pct")
    if pct is None or pct < thr:
        return False
    if row.get("status") == "claude-model":
        return False
    return not any(row.get(f) for f in (
        "mode", "latched", "would_fire", "would_rebrief", "would_recover",
        "would_clear", "overflow", "actuation_state", "hot_blocked",
        "dead_holding_state"))


def _hot_fingerprint(row):
    """seat + why + a 5-point bucket. Latching on the bucket means a seat that
    sits at 94% forever is announced ONCE, while one climbing 94 -> 99 -> 104
    speaks again at each step. A timer that repeats itself every 60s trains
    the room to filter it, which is the same silence by a different route."""
    pct = row.get("pct") or 0
    state = row.get("actuation_state") or row.get("status")
    return "%s|%s|%d" % (row["seat"], state,
                         int(pct // HOT_BUCKET) * HOT_BUCKET)


# A transcript touched this recently belongs to a seat that is TAKING TURNS.
# Not a guess about health — the file grew, so the loop ran.
WORKING_S = 300


def _hot_text(row, thr):
    """The high-context alarm, with its remedy gated on measured liveness.

    THIS TEXT USED TO ASSERT, UNCONDITIONALLY, that "a non-claude seat that
    reaches 100% wedges into a 400 that /compact itself cannot escape", and to
    prescribe prune+resume and /clear. Both halves were wrong to state flatly.

    The owner watched a codex seat run at 100% context and keep working
    (2026-07-31): "whatever settings we set for codex in cliproxy etc, it seems
    to reliably be able to hit 100% context reported in cc and keep going ...
    they don't like to end turns, they just keep working forever as long as they
    have stuff to do". His observation is the ground truth here and it outranks
    this string, which was a hardcoded universal derived from ONE claude-seat
    incident and then applied to every family.

    The cost of the flat version was not noise, it was DESTRUCTIVE ADVICE: it
    fired every couple of minutes at a seat that was mid-lane and told the
    reader to /clear it. Following it would have thrown away exactly the context
    that was doing the work.

    So the remedy is now evidence-gated on the row's own `age_s`. A seat whose
    transcript grew inside WORKING_S is demonstrably still taking turns; for it,
    a wedge is a RISK AHEAD, not a state to recover from, and no destructive
    step is offered. The recovery text is reserved for a seat that has actually
    gone quiet. Note this alarm still cannot SEE a 400 loop — `_overflow_400`
    measures precisely that from the pane tail and is not on this path; wiring
    it is the honest end state and is filed, not faked here."""
    if row.get("actuation_state"):
        return ("autocompact REFUSED on %s at %.1f%% (>= %d%%): actuation "
                "state=%s — %s. /compact was NOT injected; the next pass will "
                "re-measure the pane rather than latch this refusal as success."
                % (row["seat"], row.get("pct") or 0.0, thr,
                   row["actuation_state"], row.get("actuation_reason") or
                   "no actionable pane evidence"))
    age = row.get("age_s")
    taking_turns = age is not None and age <= WORKING_S
    head = (
        "autocompact CANNOT ACT on %s — %.1f%% of its %s-token window (>= the "
        "%d%% trigger) and status is '%s'. The context is real and measured; "
        "the refusal is about PANE IDENTITY, not about the seat being fine. "
        "Nothing will compact this seat until its session is re-bound" % (
            row["seat"], row.get("pct") or 0.0,
            "{:,}".format(row.get("window") or 0), thr, row.get("status")))
    if taking_turns:
        return head + (
            ", and it is STILL TAKING TURNS — its transcript grew %s ago, so "
            "it is working, not wedged. Do NOT prune, /clear or respawn it on "
            "the strength of this line; that would destroy a live context that "
            "is mid-work. Re-bind it (helm seat rebind / respawn) so a future "
            "compaction can act, or leave it be and let it finish."
            % _age(age))
    return head + (
        ", and this seat has gone quiet — its transcript was last touched %s "
        "ago. A high-context seat that has stopped taking turns may be in a "
        "repeated 400 that /compact itself cannot escape. Re-bind it (helm "
        "seat rebind / respawn); if the pane is genuinely in the 400 loop, "
        "prune+resume it and use /clear only if pruning fails."
        % (_age(age) if age is not None else "an unknown time"))


def _dead_text(row, thr):
    return (
        "%s is at %.1f%% of its %s-token window with NO LIVE PROCESS — its "
        "transcript was last touched %s ago. Nothing will wedge, because "
        "nothing is running; the percentage is an abandoned file being counted "
        "as a seat. Respawn it (helm seat spawn %s) or clean the stale "
        "transcript so it stops reading as a live seat."
        % (row["seat"], row.get("pct") or 0.0,
           "{:,}".format(row.get("window") or 0), _age(row.get("age_s")),
           row["seat"]))


def _silent_text(row, thr):
    return (
        "SILENT NON-FIRING CAUGHT on %s — %.1f%% of its %s-token window (>= the "
        "%d%% trigger), status '%s', yet nothing fired, latched, or flagged it. "
        "This is the backstop: a seat at the wall that every specific rung let "
        "through, which is the exact silence that reads as all-clear. Some check "
        "upstream stopped classifying this seat — read it now (helm seat "
        "autocompact --seat %s --json) and re-bind or /compact it by hand."
        % (row["seat"], row.get("pct") or 0.0,
           "{:,}".format(row.get("window") or 0), thr, row.get("status"),
           row["seat"]))


# ---------------------------------------------------------------------------
# the refusal series — the safety exemption's missing EXIT
# ---------------------------------------------------------------------------

_REFUSAL_KEY = "_refusals"
_REFUSAL_ALARM_KEY = "_refusal_alarms"
_REFUSAL_NOTICE_KEY = "_refusal_notices"
_REFUSAL_NOTICE_LEASE_S = 60.0


def _refusal_delivery_lock(blocking=True):
    """Serialize external refusal appends against recovery transitions."""
    path = _state_path() + ".delivery.lock"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    lock = os.fdopen(fd, "a")
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock.fileno(), flags)
    except BlockingIOError:
        lock.close()
        return None
    except Exception:
        lock.close()
        raise
    return lock


# N CONSECUTIVE non-falling refusals = wedged, not busy.
#
# Chosen at 3 against the measured series: gemini refused 29 times in a row
# while climbing 112% -> 200%, so any N up to 29 catches it and the choice is
# decided entirely by the other side — a healthy seat that happens to hold one
# long turn. At DEFAULT_INTERVAL_S (60s) three passes is ~3 minutes of an
# UNBROKEN open turn on a seat ALREADY past the 80% trigger whose context never
# once fell. A false positive costs one chat line, rate-limited to once per
# HOT_BUCKET of further climb; the false negative cost is measured and it is a
# seat (ds4pro: 1,016,359 tokens over five days, zero compactions, then an
# unrecoverable 400). The costs are asymmetric, so take the low N.
WEDGE_REFUSALS = 3

# ...AND N NON-FALLING REFUSALS IS A WATCH ITEM, NOT AN EMERGENCY.
#
# MEASURED over the whole durable chat journal, seven weeks of it: 73 wedge
# episodes across 8 seats. 66 of them (90.4%) ended in a MEASURED discharge —
# the seat's own percentage observed back below the trigger — median 32.1 min,
# 56 of 66 inside an hour, falling to a median 23.4%. Not one reached a 400.
# So the wedge alarm's claim that the seat "can NEVER become eligible on its
# own" is false of nine seats in ten, and a first alarm at n=3 (two minutes)
# that says so is the thing that trains a reader to filter this sender.
#
# What the population DOES separate is DURATION. p90 of the self-clearing
# episodes is 103 min; 63 of 66 cleared inside four hours. An episode that
# outlives that envelope is a different animal — the three that did were
# one project's codex seat pinned FLAT at 99.5% for 4.3 days, and two
# numbered-seat alarms still pinned in live state 11 and 5.8 days after their seat stopped
# being measurable at all. Four hours is therefore the discriminator: above it
# the population says the seat is not converging, at a measured 3-in-66 cost of
# calling a slow-but-recovering seat stuck.
#
# It also closes a measured HOLE, and it is the opposite of the one the row
# alleged. HOT_BUCKET only re-announces on another 5 points of CLIMB, so a
# wedge pinned FLAT never re-announces: that 4.3-day project-seat episode
# produced exactly ONE chat line in four days (median across all 73 episodes: 4
# lines, max 6). The stuck rung fires on elapsed time, so a flat wedge cannot
# stay silent.
WEDGE_STUCK_S = 4 * 3600.0


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _refusal_episode(row):
    for key, tag in (("session", "session"),
                     ("registered_session", "registered-session"),
                     ("pane_handle", "pane"), ("seat", "seat")):
        value = row.get(key)
        if isinstance(value, str) and value:
            return "%s:%s" % (tag, value)
    return None


def _normalize_refusal_series(raw):
    if not isinstance(raw, dict):
        return None
    n = raw.get("n")
    first, last, since = (raw.get("first_pct"), raw.get("last_pct"),
                          raw.get("since"))
    state, episode = raw.get("state"), raw.get("episode")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1 \
            or not all(_finite_number(v) for v in (first, last, since)) \
            or not isinstance(state, str) or not state \
            or episode is not None and not isinstance(episode, str):
        return None
    alerted = raw.get("alerted_pct")
    if alerted is not None and not _finite_number(alerted):
        alerted = None
    return {"v": 1, "episode": episode, "n": n,
            "first_pct": float(first), "last_pct": float(last),
            "state": state, "since": float(since),
            "alerted_pct": None if alerted is None else float(alerted)}


def _wedge_stuck(evidence, now=None):
    """Has this non-falling series outlived the self-clearing envelope?

    Read off `since`, never off a stored flag, so the answer is the same
    whether a notice is rendered on the pass that queued it or a retry later.
    """
    if not isinstance(evidence, dict):
        return False
    since = evidence.get("since")
    if not _finite_number(since):
        return False
    return (time.time() if now is None else now) - since >= WEDGE_STUCK_S


def _normalize_refusal_alarm(raw, default_threshold):
    if not isinstance(raw, dict):
        return None
    structured = "evidence" in raw
    evidence = _normalize_refusal_series(
        raw.get("evidence") if structured else raw)
    if not evidence or evidence["n"] < WEDGE_REFUSALS:
        return None
    threshold = raw.get("threshold", default_threshold) if structured \
        else default_threshold
    if not _finite_number(threshold):
        return None
    episode = raw.get("episode", evidence.get("episode")) if structured \
        else evidence.get("episode")
    if episode is not None and not isinstance(episode, str):
        return None
    alerted = raw.get("alerted_pct", evidence.get("alerted_pct")) \
        if structured else evidence.get("alerted_pct")
    if alerted is not None and not _finite_number(alerted):
        alerted = None
    # Additive and defaulted: an alarm written before the stuck rung existed
    # reads as not-yet-escalated, which is exactly right for one still running.
    stuck_at = raw.get("stuck_at") if structured else None
    if not _finite_number(stuck_at):
        stuck_at = None
    return {"v": 1, "episode": episode, "threshold": float(threshold),
            "evidence": evidence,
            "alerted_pct": None if alerted is None else float(alerted),
            "stuck_at": None if stuck_at is None else float(stuck_at)}


def _normalize_refusal_notice(raw):
    if not isinstance(raw, dict):
        return None
    kind, notice_id, seat = raw.get("kind"), raw.get("id"), raw.get("seat")
    episode, payload = raw.get("episode"), raw.get("payload")
    if kind not in ("wedge", "discharge") \
            or not all(isinstance(v, str) and v
                       for v in (notice_id, seat, episode)) \
            or not isinstance(payload, dict):
        return None
    evidence = _normalize_refusal_series(payload.get("evidence"))
    threshold = payload.get("threshold")
    if not evidence or not _finite_number(threshold):
        return None
    observed = payload.get("observed_pct") if kind == "discharge" else None
    if kind == "discharge" and not _finite_number(observed):
        return None
    claim = raw.get("claim_token")
    until = raw.get("claim_until", 0.0)
    if claim is not None and not isinstance(claim, str):
        claim = None
    if not _finite_number(until):
        until = 0.0
    clean_payload = {"threshold": float(threshold), "evidence": evidence,
                     "observed_pct": None if observed is None
                     else float(observed),
                     "window": payload.get("window"),
                     "actuation_reason": payload.get("actuation_reason")}
    return {"v": 1, "id": notice_id, "kind": kind, "seat": seat,
            "episode": episode, "payload": clean_payload,
            "claim_token": claim, "claim_until": float(until)}


def _notice_id(kind, seat, episode, payload):
    body = json.dumps([kind, seat, episode, payload], sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _enqueue_refusal_notice(notices, kind, row, alarm):
    episode = alarm.get("episode")
    if not isinstance(episode, str) or not episode:
        return None
    for notice in notices.values():
        if notice["kind"] == kind and notice["seat"] == row["seat"] \
                and notice.get("episode") == alarm.get("episode"):
            return notice["id"]
    evidence = dict(alarm["evidence"])
    payload = {"threshold": alarm["threshold"], "evidence": evidence,
               "observed_pct": row.get("pct") if kind == "discharge"
               else None, "window": row.get("window"),
               "actuation_reason": row.get("actuation_reason")}
    notice_id = _notice_id(kind, row["seat"], alarm.get("episode"), payload)
    if notice_id not in notices:
        notices[notice_id] = {
            "v": 1, "id": notice_id, "kind": kind, "seat": row["seat"],
            "episode": alarm.get("episode"), "payload": payload,
            "claim_token": None, "claim_until": 0.0}
    return notice_id


def _notice_row(notice, state=None):
    payload, evidence = notice["payload"], notice["payload"]["evidence"]
    row = {"seat": notice["seat"], "window": payload.get("window"),
           "actuation_reason": payload.get("actuation_reason")}
    if notice["kind"] == "wedge":
        row["refusal_wedge"] = dict(evidence)
    else:
        row["refusal_alarm"] = {
            "state": state or "discharge-pending",
            "observed_pct": payload["observed_pct"],
            "threshold": payload["threshold"],
            "episode": notice.get("episode"),
            "evidence": dict(evidence), "event_id": notice["id"]}
    return row


def _refusal_notice_text(notice):
    row = _notice_row(notice, state="discharged")
    text = _wedge_text(row, int(notice["payload"]["threshold"])) \
        if notice["kind"] == "wedge" else _wedge_discharge_text(
            row, int(notice["payload"]["threshold"]))
    return "%s [refusal-event:%s]" % (text, notice["id"])


def _project_refusal_state(rows, alarms, notices):
    by_name = {row["seat"]: row for row in rows}
    views = {}
    for name, alarm in alarms.items():
        row = by_name.get(name)
        observed = row.get("pct") if row else None
        episode = _refusal_episode(row) if row else None
        if row is None:
            observation = "missing"
        elif alarm.get("episode") and episode != alarm["episode"]:
            observation = "identity-mismatch"
        elif observed is None:
            observation = "unmeasured"
        else:
            observation = "observed"
        view = row if row is not None else {"seat": name}
        view["refusal_alarm"] = {
            "state": "active", "observation": observation,
            "observed_pct": observed, "threshold": alarm["threshold"],
            "episode": alarm.get("episode"),
            "evidence": dict(alarm["evidence"])}
        views[name] = view
    for notice in notices.values():
        if notice["kind"] != "discharge":
            continue
        view = by_name.get(notice["seat"]) or _notice_row(notice)
        view["refusal_alarm"] = _notice_row(notice)["refusal_alarm"]
        views[notice["seat"]] = view
    return list(views.values())


def _track_refusals(st, rows, now, thr, mutate=True, post=True):
    """Advance and project refusal alarms without performing chat I/O.

    Consecutive series, durable alarms, and delivery notices are distinct owner
    state. A quiet pass may clear only the series. A same-session measured fall
    below the alarm's stored threshold moves its immutable evidence to a
    retryable discharge notice. Chat claims are persisted here and acknowledged
    later, outside this lock.
    """
    raw_series = st.get(_REFUSAL_KEY)
    raw_alarms = st.get(_REFUSAL_ALARM_KEY)
    raw_notices = st.get(_REFUSAL_NOTICE_KEY)
    series = {name: clean for name, raw in (
        raw_series.items() if isinstance(raw_series, dict) else ())
              if (clean := _normalize_refusal_series(raw))}
    alarms, unbound = {}, {}
    for name, raw in (
            raw_alarms.items() if isinstance(raw_alarms, dict) else ()):
        clean = _normalize_refusal_alarm(raw, thr)
        if not clean:
            continue
        if clean.get("episode"):
            alarms[name] = clean
            continue
        prior = series.get(name)
        if prior and prior.get("episode"):
            continue
        unbound[name] = clean
        if not prior or clean["evidence"]["n"] >= prior["n"]:
            series[name] = dict(clean["evidence"])
    notices = {notice_id: clean for notice_id, raw in (
        raw_notices.items() if isinstance(raw_notices, dict) else ())
               if (clean := _normalize_refusal_notice(raw))
               and clean["id"] == notice_id}
    by_name = {row["seat"]: row for row in rows}
    wedged, discharge_candidates = [], {}

    unbound_names = set(unbound) | {
        name for name, entry in series.items()
        if entry["n"] >= WEDGE_REFUSALS and not entry.get("episode")}
    # Upgrade mature parent-format evidence only when a current refusal binds it
    # to one episode. Legacy evidence without identity is not active ownership:
    # putting it in alarms would suppress every lower-severity surface while no
    # future row could discharge or replace it.
    for name, entry in series.items():
        if entry["n"] < WEDGE_REFUSALS or name in alarms:
            continue
        row = by_name.get(name)
        legacy = unbound.get(name)
        threshold = legacy["threshold"] if legacy else float(thr)
        episode = entry.get("episode")
        if not episode and row and row.get("actuation_state") \
                and _finite_number(row.get("pct")) \
                and row["pct"] >= threshold:
            episode = _refusal_episode(row)
            entry["episode"] = episode
        if not episode:
            continue
        alarm = {"v": 1, "episode": episode, "threshold": threshold,
                 "evidence": dict(entry),
                 "alerted_pct": legacy.get("alerted_pct") if legacy
                 else entry.get("alerted_pct"),
                 "stuck_at": legacy.get("stuck_at") if legacy else None}
        alarms[name] = alarm
        if alarm["alerted_pct"] is None and row:
            _enqueue_refusal_notice(notices, "wedge", row, alarm)

    if mutate:
        for name in unbound_names:
            if name not in alarms:
                series.pop(name, None)

        for row in rows:
            name = row["seat"]
            state, pct = row.get("actuation_state"), row.get("pct")
            episode = _refusal_episode(row)
            alarm = alarms.get(name)
            if alarm and alarm.get("episode") and episode == alarm["episode"] \
                    and row.get("status") != "session-mismatch" \
                    and _finite_number(pct) and pct < alarm["threshold"]:
                delivery_lock = _refusal_delivery_lock(blocking=False)
                if delivery_lock is not None:
                    try:
                        for notice_id, notice in list(notices.items()):
                            if notice["kind"] == "wedge" \
                                    and notice["seat"] == name \
                                    and notice.get("episode") == episode:
                                notices.pop(notice_id)
                        notice_id = _enqueue_refusal_notice(
                            notices, "discharge", row, alarm)
                        alarms.pop(name, None)
                        series.pop(name, None)
                        discharge_candidates[notice_id] = row
                        alarm = None
                    finally:
                        delivery_lock.close()

            if not state or not _finite_number(pct):
                series.pop(name, None)
                continue
            prior = series.get(name)
            last = prior.get("last_pct") if prior \
                and prior.get("episode") == episode else None
            if not _finite_number(last) or pct < last:
                entry = {"v": 1, "episode": episode, "n": 1,
                         "first_pct": float(pct), "last_pct": float(pct),
                         "state": state, "since": float(now),
                         "alerted_pct": None}
            else:
                entry = dict(prior)
                entry["n"] += 1
                entry["last_pct"], entry["state"] = float(pct), state
            series[name] = entry
            if entry["n"] < WEDGE_REFUSALS:
                continue
            alarm = alarms.get(name)
            if not alarm:
                alarm = {"v": 1, "episode": episode,
                         "threshold": float(thr), "evidence": dict(entry),
                         "alerted_pct": entry.get("alerted_pct"),
                         "stuck_at": None}
                alarms[name] = alarm
            elif alarm.get("episode") == episode:
                evidence = alarm["evidence"]
                if entry["first_pct"] == evidence["first_pct"]:
                    alarm["evidence"] = dict(entry)
                elif entry["last_pct"] > evidence["last_pct"]:
                    merged = dict(evidence)
                    merged["last_pct"] = entry["last_pct"]
                    merged["state"] = entry["state"]
                    alarm["evidence"] = merged
            if alarm.get("episode") == episode:
                # `since` must mean the start of the CURRENT unbroken
                # non-falling run, which is what both the alarm text ("never
                # once falling, over %s") and the stuck rung below assert. The
                # merge arms above can leave the alarm holding an older run's
                # `since` when a series fell and re-climbed to below its prior
                # peak; the live entry's `since` is reset by exactly that fall.
                alarm["evidence"]["since"] = entry["since"]
            evidence = alarm["evidence"]
            alerted = alarm.get("alerted_pct")
            # The stuck rung is keyed on ELAPSED TIME, not on further climb,
            # because the measured worst case never climbed: a wedge pinned
            # flat above the trigger satisfies neither arm of the HOT_BUCKET
            # gate and so goes permanently quiet. It latches once per episode.
            stuck = alarm.get("episode") == episode \
                and _wedge_stuck(evidence, now) \
                and alarm.get("stuck_at") is None
            if alarm.get("episode") == episode and (
                    stuck or alerted is None or
                    evidence["last_pct"] >= alerted + HOT_BUCKET):
                notice_id = _enqueue_refusal_notice(
                    notices, "wedge", row, alarm)
                if notice_id in notices:
                    if stuck:
                        alarm["stuck_at"] = float(now)
                        row["wedge_stuck"] = True
                    row["refusal_wedge"] = dict(evidence)
                    wedged.append(row)
            elif alarm.get("episode") == episode:
                row["wedge_latched"] = True

    suppressed = {}
    claims = []
    if mutate and not post:
        for notice_id, notice in list(notices.items()):
            if notice["kind"] == "wedge":
                alarm = alarms.get(notice["seat"])
                if alarm and alarm.get("episode") == notice.get("episode"):
                    alarm["alerted_pct"] = \
                        notice["payload"]["evidence"]["last_pct"]
            suppressed[notice_id] = notice
            notices.pop(notice_id, None)
    elif mutate and post:
        token = "%d:%d" % (os.getpid(), int(now * 1_000_000))
        for notice in notices.values():
            if notice.get("claim_token") and notice["claim_until"] > now:
                continue
            notice["claim_token"] = token
            notice["claim_until"] = now + _REFUSAL_NOTICE_LEASE_S
            claims.append(dict(notice))

    st[_REFUSAL_KEY], st[_REFUSAL_ALARM_KEY] = series, alarms
    st[_REFUSAL_NOTICE_KEY] = notices
    active = _project_refusal_state(rows, alarms, notices)
    discharged = []
    for notice_id, row in discharge_candidates.items():
        notice = suppressed.get(notice_id)
        if not notice:
            continue
        row["refusal_alarm"] = _notice_row(
            notice, state="discharged")["refusal_alarm"]
        discharged.append(row)
    return wedged, active, discharged, claims, discharge_candidates


def _ack_refusal_notice(st, notice_id, claim_token, delivered, thr):
    notices = st.get(_REFUSAL_NOTICE_KEY)
    if not isinstance(notices, dict):
        return False
    notice = _normalize_refusal_notice(notices.get(notice_id))
    if not notice or notice.get("claim_token") != claim_token:
        return False
    if not delivered:
        notice["claim_token"], notice["claim_until"] = None, 0.0
        notices[notice_id] = notice
        return True
    notices.pop(notice_id, None)
    if notice["kind"] == "wedge":
        alarms = st.get(_REFUSAL_ALARM_KEY)
        alarm = _normalize_refusal_alarm(
            alarms.get(notice["seat"]), thr) if isinstance(alarms, dict) \
            else None
        if alarm and alarm.get("episode") == notice.get("episode"):
            alarm["alerted_pct"] = \
                notice["payload"]["evidence"]["last_pct"]
            alarms[notice["seat"]] = alarm
    return True


def _wedge_text(row, thr):
    w = row.get("refusal_wedge") or {}
    first = w.get("first_pct") or 0.0
    last = w.get("last_pct") or 0.0
    if w.get("state") == "FAILED_TO_SUBMIT":
        return (
            "⚠️ WATCHDOG: seat %s has FAILED TO SUBMIT /compact for %d consecutive "
            "watchdog passes — context %s from %.1f%% to %.1f%% of its %s-token "
            "window, all above the %d%% trigger. Helm did NOT call or latch any "
            "attempt as success. Latest reason: %s. The next cadence retries once; "
            "inspect the named transport/composer failure or respawn its input path. "
            "[attempted-is-not-acted]"
            % (row["seat"], w.get("n") or 0,
               "CLIMBING" if last > first else "FLAT", first, last,
               "{:,}".format(row.get("window") or 0), thr,
               row.get("actuation_reason") or "submission remained unverified"))
    common = (row["seat"], w.get("n") or 0, w.get("state") or "?",
              "CLIMBING" if last > first else "FLAT", first, last,
              "{:,}".format(row.get("window") or 0),
              _age(max(0.0, time.time() - (w.get("since") or time.time()))),
              thr)
    if _wedge_stuck(w):
        return (
            "🚨 WATCHDOG: seat %s is STUCK BEHIND AUTOCOMPACT'S OWN SAFETY "
            "REFUSAL — %d consecutive refusals (state=%s), context %s from "
            "%.1f%% to %.1f%% of its %s-token window over %s, never once "
            "falling, all above the %d%% trigger. The refusal is CORRECT — "
            "/compact must never be queued into an open turn — but this "
            "episode has now outlived the self-clearing envelope: 66 of the 73 "
            "wedge episodes on record cleared themselves at a median of 32 "
            "minutes, and 63 of those inside four hours. A turn that never "
            "lands never yields an empty composer, so nothing in helm will "
            "make this seat eligible. THIS ONE NEEDS ITS LEAD: esc the pane by "
            "hand and /compact it, or rebind/respawn if the pane is gone. "
            "[watchdogs-correct-composition-holed]" % common)
    return (
        "⚠️ WATCHDOG: seat %s is REFUSING /compact BEHIND ITS OWN SAFETY CHECK "
        "— %d consecutive refusals (state=%s), context %s from %.1f%% to %.1f%% "
        "of its %s-token window over %s, never once falling, all above the %d%% "
        "trigger. The refusal is CORRECT — /compact must never be queued into an "
        "open turn — and a non-falling series is the discriminator between a "
        "busy seat and a wedged one, so this is worth WATCHING. It is not yet "
        "worth interrupting: measured over 73 such episodes, 66 cleared "
        "themselves when the turn landed, median 32 minutes, falling to a "
        "median 23%% of the window. No action is asked for here. If this one "
        "outlives that envelope it escalates on its own at %s. "
        "[watchdogs-correct-composition-holed]"
        % (common + (_age(WEDGE_STUCK_S),)))


def _wedge_discharge_text(row, thr):
    alarm = row.get("refusal_alarm") or {}
    evidence = alarm.get("evidence") or {}
    return (
        "WATCHDOG DISCHARGED on %s — context was OBSERVED at %.1f%%, below "
        "the %d%% trigger, after an active refusal alarm that reached %d "
        "consecutive refusals from %.1f%% to %.1f%%. This is measured recovery, "
        "not silence; the next over-threshold episode starts re-armed."
        % (row["seat"], alarm.get("observed_pct") or 0.0, thr,
           evidence.get("n") or 0, evidence.get("first_pct") or 0.0,
           evidence.get("last_pct") or 0.0))


def _record_recovery(st, row, now, mode, detail, fired):
    row["mode"], row["detail"] = mode, detail
    st[row["seat"]] = {
        "fired_at": now,
        "session": row.get("registered_session") or row.get("session"),
        "registered_session": row.get("registered_session"),
        "pct": row.get("pct"), "mode": mode}
    fired.append(row)


def check(seats=None, thr=None, fire=True, post=True, adapter=None):
    """One bounded pass: scan -> latch -> fire -> latch-update. Returns
    {"rows", "fired", "hot", "dead", "silent", "wedged", "alarms",
    "discharged"} where each fired row carries mode/detail; hot/dead/silent are
    the loud non-fire surfaces, wedged is the fresh refusal-loop announcement,
    alarms is the still-active refusal frontier, and discharged contains only
    alarms whose percentage was observed below the trigger this pass.
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
        pane_adapters, pane_previews = _pane_preview_batch(rows, adapter)
        stored = pk.read_json(p, {}) or {}
        # A dry run may exercise every decision branch, but it owns a detached
        # state image: no latch pop, migration, notice claim, or rewrite reaches
        # the timer's durable owner state merely because an operator looked.
        st = stored if fire else json.loads(json.dumps(stored))
        now = time.time()
        fired = []
        for row in rows:
            pane_adapter = pane_adapters.get(row.get("pane_harness"), adapter)
            entry = st.get(row["seat"])
            recovery = (entry or {}).get("mode")
            if recovery in ("pruned-resumed", "resume-manual", "recovery-manual"):
                if _resume_recovery_complete(entry, row):
                    st.pop(row["seat"], None)
                    entry = None
                else:
                    row["latched"] = True
                    continue
            if recovery == "clear-needs-brief" or (
                    recovery == "clear-pending" and
                    _recovery_session_changed(entry, row)):
                row["would_rebrief"] = True
                # A between-rows clear finishes on these same rungs; the
                # reason rides along so its texts do not read as an overflow.
                why = (entry or {}).get("why")
                if why == FRESH_ROW:
                    row["fresh_row"] = True
                if not fire:
                    continue
                mode, detail = _retry_rebrief(row, pane_adapter)
                row["mode"], row["detail"] = mode, detail
                if mode == "cleared":
                    st.pop(row["seat"], None)  # recovery complete: re-arm
                else:
                    st[row["seat"]] = {
                        "fired_at": now,
                        "session": row.get("registered_session") or
                        row.get("session"),
                        "pct": row.get("pct"), "mode": mode}
                    if why:
                        st[row["seat"]]["why"] = why
                fired.append(row)
                continue
            if recovery == "clear-pending":
                row["latched"] = True
                continue
            if _episode_complete(entry, row):
                st.pop(row["seat"], None)
                entry = None                     # episode over — re-arm

            if (entry or {}).get("mode") == "context-400":
                causal, mode, detail = _causal_context_400_action(
                    row, pane_adapter, entry, fire=fire)
                if causal:
                    row["overflow"], row["would_recover"] = True, True
                    if mode in ("clear-pending", "clear-manual"):
                        row["would_clear"] = True
                    if not fire:
                        continue
                    _record_recovery(st, row, now, mode, detail, fired)
                    continue
                if fire:
                    st.pop(row["seat"], None)
                    entry = None  # strike disappeared; ordinary retry may proceed

            clear_entry = entry if (entry or {}).get("mode") in (
                "cleared", "clear-manual") else None
            clear_blocked = bool(
                clear_entry and _latch_blocks(clear_entry, row, now))
            if _overflow_preview_excludes(row, pane_previews):
                overflow, mode, detail = False, None, None
            else:
                overflow, mode, detail = _overflow_action(
                    row, pane_adapter, fire=fire and not clear_blocked)
            if overflow:
                row["overflow"], row["would_recover"] = True, True
                if mode in ("clear-pending", "clear-manual"):
                    row["would_clear"] = True
                if clear_blocked:
                    row["latched"] = True
                    continue
                if not fire:
                    continue
                _record_recovery(st, row, now, mode, detail, fired)
                continue

            # A FRESH SESSION BETWEEN ROWS (task/2944), ahead of the /compact
            # threshold: for a seat with no work, dropping the old rows beats
            # summarising them. Only with no latch of any kind on the seat.
            if not entry and _fresh_candidate(row):
                spaced = _fresh_spacing_refusal(st, row["seat"], now)
                mode, detail = ((None, spaced) if spaced else
                                _fresh_action(row, pane_adapter, fire))
                if mode:
                    row["fresh_row"] = True
                    _record_recovery(st, row, now, mode, detail, fired)
                    st[row["seat"]]["why"] = FRESH_ROW
                    if not isinstance(st.get(_FRESH_KEY), dict):
                        st[_FRESH_KEY] = {}
                    st[_FRESH_KEY][row["seat"]] = now
                    continue
                if row.get("would_fresh") and not fire:
                    continue                     # dry run: it WOULD clear
                row["fresh_refused"] = detail

            if row.get("status") not in _ACTIONABLE or row["pct"] < thr:
                continue
            row["would_fire"] = True
            if _latch_blocks(entry, row, now):
                row["latched"] = True
                continue
            if not fire:
                continue
            mode, detail = _fire(row, pane_adapter)
            if mode == "context-400":
                row["actuation_state"] = "CONTEXT_400"
                row["actuation_reason"] = detail
                strike = row["context_400_strike"]
                st[row["seat"]] = {
                    "fired_at": now, "identity": _latch_identity(row),
                    "session": row.get("session"),
                    "registered_session": row.get("registered_session"),
                    "pct": row["pct"], "mode": mode,
                    "context_400_line": strike["line"],
                    "context_400_baseline": strike["baseline"]}
                continue
            if mode.startswith("refused-"):
                row["actuation_state"] = mode[len("refused-"):].upper().replace(
                    "-", "_")
                row["actuation_reason"] = detail
                continue
            row["mode"], row["detail"] = mode, detail
            st[row["seat"]] = {"fired_at": now,
                               "identity": _latch_identity(row),
                               "session": row.get("session"),
                               "pct": row["pct"], "mode": mode}
            fired.append(row)

        # The seats this pass could measure but not touch. Marked on every row
        # (so --dry-run/--json shows them) but latched and announced only on a
        # real pass, because a dry run must not consume the change-latch that
        # the live timer depends on.
        live = _live_seat_names()
        for row in rows:
            row["pane_live"] = None if live is None else (row["seat"] in live)
        hot, dead, silent = [], [], []
        wedged, alarms, discharged, notice_claims, discharge_candidates = \
            _track_refusals(st, rows, now, thr, mutate=fire, post=post)
        alarmed = {row["seat"] for row in alarms}
        for row in rows:
            if any(row is f for f in fired) or row["seat"] in alarmed:
                continue
            if row.get("actuation_state"):
                # The threshold crossed and the pane path answered explicitly:
                # RUNNING/BLOCKED are safety refusals; UNKNOWN names a missing
                # pane or unreadable turn state. None may disappear as all-clear.
                row["hot_blocked"] = True
                continue
            if _dead_holding_state(row, thr):
                row["dead_holding_state"] = True
                continue
            if _hot_blocked(row, thr):
                row["hot_blocked"] = True
                continue
            # Every rung above declined. The backstop catches an over-threshold
            # seat that would otherwise vanish in silence (owner AS-PREVENTED) —
            # in normal operation the dead/hot rungs claim every non-ok hot seat,
            # so this stays empty and only lights when a rung above regresses.
            if _silently_dropped(row, thr):
                row["silent_non_fire"] = True
        if fire:
            seen = st.setdefault("_hot", {})
            for name in alarmed:
                seen.pop(name, None)  # the active alarm owns this incident
            for row in rows:
                if not (row.get("hot_blocked") or
                        row.get("dead_holding_state") or
                        row.get("silent_non_fire")):
                    continue
                fp = _hot_fingerprint(row)
                if seen.get(row["seat"]) == fp:
                    row["hot_latched"] = True
                    continue
                seen[row["seat"]] = fp
                if row.get("dead_holding_state"):
                    dead.append(row)
                elif row.get("silent_non_fire"):
                    silent.append(row)
                else:
                    hot.append(row)
            # A seat that recovered stops being hot/dead/silent, and its latch
            # must go with it — otherwise the NEXT episode arrives pre-muted.
            for name in [s for s in seen
                         if not any(r["seat"] == s and
                                    (r.get("hot_blocked") or
                                     r.get("dead_holding_state") or
                                     r.get("silent_non_fire"))
                                    for r in rows)]:
                seen.pop(name, None)
            pk.write_json(p, st)

    if post:
        for row in fired:
            if row.get("fresh_row") and row["mode"] in _ROUTINE_FRESH:
                continue    # routine: the seat's own onboarding post shows it
            try:
                from . import chat
                chat.post(_fire_text(row, row["mode"], row["detail"]),
                          who="autocompact")
            except Exception as e:   # a down chat node never blocks the fire
                print("helm seat autocompact: chat post failed (%s): %s"
                      % (row["seat"], e), file=sys.stderr)
        for row, text in ([(r, _hot_text(r, thr)) for r in hot] +
                          [(r, _dead_text(r, thr)) for r in dead] +
                          [(r, _silent_text(r, thr)) for r in silent]):
            try:
                from . import chat
                chat.post(text, who="autocompact")
            except Exception as e:
                print("helm seat autocompact: hot-alert post failed (%s): %s"
                      % (row["seat"], e), file=sys.stderr)
        delivered = set()
        for notice in notice_claims:
            delivery_lock = _refusal_delivery_lock()
            try:
                with open(_state_lock_path(), "a") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    current = pk.read_json(p, {}) or {}
                    raw_notices = current.get(_REFUSAL_NOTICE_KEY)
                    active = _normalize_refusal_notice(
                        raw_notices.get(notice["id"])) \
                        if isinstance(raw_notices, dict) else None
                    if not active or active.get("claim_token") \
                            != notice["claim_token"]:
                        continue
                ok = False
                try:
                    from . import chat
                    chat.post(_refusal_notice_text(notice), who="autocompact",
                              sign=False, event_id=notice["id"])
                    ok = True
                except Exception as e:
                    print("helm seat autocompact: refusal notice failed (%s): %s"
                          % (notice["seat"], e), file=sys.stderr)
                with open(_state_lock_path(), "a") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    current = pk.read_json(p, {}) or {}
                    if _ack_refusal_notice(
                            current, notice["id"], notice["claim_token"], ok,
                            thr):
                        pk.write_json(p, current)
                        if ok:
                            delivered.add(notice["id"])
            finally:
                delivery_lock.close()
        for notice in notice_claims:
            row = discharge_candidates.get(notice["id"])
            if notice["kind"] != "discharge" or not row \
                    or notice["id"] not in delivered:
                continue
            row["refusal_alarm"] = _notice_row(
                notice, state="discharged")["refusal_alarm"]
            discharged.append(row)
        if delivered:
            alarms = [row for row in alarms
                      if (row.get("refusal_alarm") or {}).get("event_id")
                      not in delivered]
    return {"rows": rows, "fired": fired, "hot": hot, "dead": dead,
            "silent": silent, "wedged": wedged, "alarms": alarms,
            "discharged": discharged}


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
            "claude-model": " — claude gate off (HELM_AUTOCOMPACT_CLAUDE=0)"}.get(
                row["status"], " — " + row["status"])
    flag = ""
    if row.get("mode"):
        flag = " -> FIRED (%s%s)" % (
            row["mode"], ", fresh session between rows"
            if row.get("fresh_row") else "")
    elif row.get("latched"):
        flag = " [latched]"
    elif row.get("would_fresh"):
        flag = " -> would start a fresh session (between rows)"
    elif row.get("would_fire"):
        flag = " -> would fire"
    hr_k = row["headroom_tokens"] // 1000 if row.get("headroom_tokens") is not None else 0
    return ("%-8s %5.1f%% of %dk (%dk via %s, %dk headroom, %s old)%s%s"
            % (row["seat"], row["pct"], row["window"] // 1000,
               row["ctx_tokens"] // 1000, row["source"], hr_k,
               _age(row.get("age_s")), note, flag))


def _refusal_line(row):
    """The refusal the text surface computed and then discarded, as one line.

    _fire's precise (state, reason) tuple reaches every row as
    actuation_state/actuation_reason and --json always carried it, but the text
    path printed only `-> would fire` — the operator watched a threshold cross
    with no line saying why nothing happened (measured 2026-08-04: 89 refusals
    vs THREE actuations across one night, every reason invisible outside
    --json). Rendered as its own indented continuation line so anything parsing
    the stable `_row_line` format keeps parsing untouched."""
    if not row.get("actuation_state") or row.get("mode"):
        return None
    return "  REFUSED [%s] %s" % (
        row["actuation_state"],
        row.get("actuation_reason") or "no actionable pane evidence")


def _refusal_alarm_line(row):
    alarm = row.get("refusal_alarm") or {}
    evidence = alarm.get("evidence") or {}
    if alarm.get("state") == "active":
        observed = alarm.get("observed_pct")
        pct = "UNKNOWN" if observed is None else "%.1f%%" % observed
        observation = alarm.get("observation") or "observed"
        return ("  REFUSAL ALARM [%s] %s %s against %d%% trigger; "
                "wedge evidence %d refusals, %.1f%% -> %.1f%%"
                % ("STUCK" if _wedge_stuck(evidence) else "ACTIVE",
                   observation, pct, alarm.get("threshold") or 0,
                   evidence.get("n") or 0,
                   evidence.get("first_pct") or 0.0,
                   evidence.get("last_pct") or 0.0))
    if alarm.get("state") == "discharge-pending":
        return ("  REFUSAL ALARM [DISCHARGE PENDING] observed %.1f%% below "
                "%d%% trigger; recovery notice %s awaits delivery"
                % (alarm.get("observed_pct") or 0.0,
                   alarm.get("threshold") or 0,
                   alarm.get("event_id") or "?"))
    if alarm.get("state") == "discharged":
        return ("  DISCHARGED refusal alarm: observed %.1f%% below %d%% "
                "trigger (prior wedge %d refusals)"
                % (alarm.get("observed_pct") or 0.0,
                   alarm.get("threshold") or 0, evidence.get("n") or 0))
    return None


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
# see proxywatch: no WorkingDirectory => cwd is $HOME => no project => #main
WorkingDirectory=%(cwd)s
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
    # A persistent unit must never capture a disposable worktree's PATH entry.
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    # WorkingDirectory DERIVED, never a literal (seat.py's rebind-unit law):
    # an operator path baked into a tracked template is a never-track needle
    # in history — this one blocked the G4 lane's commit to proxywatch.py.
    # work.find_root folds a lane worktree back to the SHARED checkout.
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    service = _UNIT_SERVICE % {"helm": helm_bin, "cwd": cwd}
    timer = _UNIT_TIMER % {"interval": interval}
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-autocompact.service"), service,
            os.path.join(udir, "helm-autocompact.timer"), timer)


TIMER_ENV = "HELM_AUTOCOMPACT_TIMER"
TIMER_OFF_VALUES = ("0", "off", "no", "false")


def timer_switched_off():
    """The HELM_AUTOCOMPACT_TIMER value when it turns the timer install off,
    else None.

    Off means ensure_timer writes no unit file and runs no systemctl. The
    switch is for a process that must not change this host's scheduler: a
    test that runs a real `helm seat launch` child, or a host that runs the
    autocompact pass from another scheduler. Only the HELM_ spelling is read;
    the variable has no legacy name."""
    value = os.environ.get(TIMER_ENV)
    if value is not None and value.strip().lower() in TIMER_OFF_VALUES:
        return value
    return None


def ensure_timer(interval=DEFAULT_INTERVAL_S):
    """Install/refresh and enable the external cadence. Returns (ok, detail).
    Seat launch/spawn/resume call this lifecycle seam so a runnable proxy seat
    cannot silently outlive its prevention loop.

    ok has three values. True: the timer is enabled. False: the install
    failed. None: HELM_AUTOCOMPACT_TIMER turned the install off, and nothing
    was written or run. None is falsy, so a caller that asks only "is the
    timer armed?" reads no, which is true. A caller that reports a failure
    tests `ok is None` first, because a switched-off install did not fail."""
    if interval < 1:
        return False, "interval must be at least 1 second"
    off = timer_switched_off()
    if off is not None:
        return None, ("install skipped by %s=%s: no unit file written, no "
                      "systemctl run" % (TIMER_ENV, off))
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
    # A switched-off install (ok is None) takes the not-installed branch:
    # stderr and rc 1. The operator asked for a timer and none was enabled.
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
    rendered_alarms = set()
    for row in res["rows"]:
        print(_row_line(row))
        refusal = _refusal_line(row)
        if refusal:
            print(refusal)
        alarm = _refusal_alarm_line(row)
        if alarm:
            print(alarm)
            rendered_alarms.add(row["seat"])
    for row in res.get("alarms", []):
        if row["seat"] in rendered_alarms:
            continue
        alarm = _refusal_alarm_line(row)
        if alarm:
            print("%-8s durable refusal state" % row["seat"])
            print(alarm)
    for row in res["fired"]:
        print("FIRED %s: %s" % (row["seat"], row["detail"]))
    if not res["rows"] and not res.get("alarms"):
        print("autocompact: no proxy seats minted")
    return 0
