#!/usr/bin/env python3
"""helm proxywatch — do the cli-proxy fixes still hold, and is the seat alive?

THE OWNER'S QUESTION, 2026-07-29, on lifting codex's routing bench: "since codex
is back in service, please set a timer to check on it appropriately to make sure
that the CLI proxy fixes for it actually work."

WHAT WAS ALREADY WATCHED AND WHAT WAS NOT. helm-silent-drop runs every 90
seconds and catches the DROP class — a completion that generated tokens and
arrived empty. That is one of three ways the fixes can stop working, and it was
the only one anything looked at:

  DROPS      covered by helm-silent-drop, every 90s.
  CONFIG     NOT covered. `nonstream-keepalive-interval: 15` is the fix that
             plausibly stopped the drops — without it a long non-streaming pass
             sits silent, the proxy reaps the idle socket, and Claude Code gets
             an empty HTTP 200. `helm seat doctor` census can see it drift, and
             nothing ran that census on a schedule. ds4pro went a week with the
             key absent precisely because no one re-read the file.
  HANGS      NOT covered, and I said so plainly when lifting the bench: a hang
             that never returns produces no transcript row to detect. The
             watchdog's signature needs a completed turn; a wedged one is
             invisible to it.

So this is the composition, not a fourth detector: it runs the checks helm
already has, adds the liveness rung nothing had, and REPORTS ONLY ON CHANGE.

THE GROK INCIDENT WIDENED THE FLEET AND ADDED THE LOG RUNG. Measured
2026-07-29: Grok's proxy.log held a two-day HTTP 402 wall while this watch
probed every ~15 minutes covering only the codex-family seats. The transport
failure was fully legible on disk; the instrument's seat list was one family
short. Owner intent is separate from that evidence: Grok is deliberately parked
pending CLI proxy cursor support, not waiting on a payment. The lesson is to
surface and classify the refusal without inventing its remedy or deployment
state. Hence two changes, one lesson each:

  EVERY SEAT  the watch walks `_minted_seats()` — the one enumeration doctor
              and the CPU canary already walk — never a hardcoded family list.
  LOG         a sustained STREAK of refusals in the recent tail of a seat's OWN
              proxy.log, classified without guessing: 401/403 auth, 402 billing,
              429 rate-limit, 5xx upstream, mixed UNKNOWN. A short burst is a
              BLIP, not STARVED, and recent completed turns outrank scary prose.
              No other rung sees a refusal that completes no transcript turn;
              the structured fields establish liveness, the sentence reports it.

THE UPSTREAM RUNG CLOSES THE LOCAL-PROBE GAP. An invalid-key 401 proves the
listener and auth path answer, but not that a configured provider can complete.
One deterministic primary request per represented family starts that question;
each request is capped at eight tokens. A dark primary is confirmed once and then
corroborated by every locally healthy sibling, whose completed failures receive
the same one confirmation. Client timeouts never retry and risk duplicating a
completion still in flight.

THE SESSION-BOUND PROOF PATH IS SEPARATE FROM FAMILY AVAILABILITY. For each
healthy proxy seat it binds the exact current roster session, Claude harness,
and live agent pid to the Claude session register plus /proc's model and loopback
base URL, the unique listener and launch-recorded config digest, the configured
alias/provider/upstream route, and its own successful authenticated eight-token
canary. OAuth proofs additionally bind the
canary's X-CPA trace to one exact loaded auth record's secret-free provider/path
index and require the upstream response model to equal the measured live model.
It remeasures after the canary,
persists only the sanitized proof, and grants UNKNOWN on any missing, stale,
ambiguous, changed, or mismatched link.

THE CODEX HANG ADDED THE FUSE — the named verdict no single probe could give.
Measured live TWICE, 2026-07-29: the primary codex seat hung with its pane
LIVE and its turn loop DEAD — transcript frozen, no assistant turn, only this
watch's own 401 probe rows in the proxy log. Four separate probes each
CORRECTLY refused to call it a failure:

  proxywatch   pane=live + transcript-stale — honest, and by its own design
               "never a verdict" (an agent legitimately thinks for a long time)
  drop watchdog STRUCTURALLY BLIND: a turn that never completes writes NO
               transcript row, and its signature needs a completed row to count
  roster       amber — lagging, not lying
  chat         quiet, pending growing — a symptom, not a name

Every probe was right about its own question and the fleet still could not
NAME the state — a human had to hand-count transcript rows. The same shape as
the grok STARVED gap, one layer over: bug class
watchdogs-correct-composition-holed. So the HUNG rung is a COMPOSITION, not a
fifth detector — it fuses readings the watch already takes, and it fires ONLY
when ALL of these compose:

  pane LIVE            the seat exists (a seat nobody launched is off)
  transcript stale >T  no COMPLETED SEMANTIC entry recently (the existing
                       HANG? gate — measured at the last persisted assistant
                       response or tool result, never file mtime)
  ZERO in-flight       nothing is open at the proxy — see the socket census
                       below; a long legitimate generation is THINKING, not hung
  below the compact bar at >= threshold the known-benign class is COMPACT-NEEDED
                       and autocompact owns the fix
  not freshly spawned  a seat that just came up and has not turned yet is
                       starting (a resume relaunches onto an OLD transcript,
                       which reads stale from second one)
  HAS pending work     a stale seat whose every pending census measures EMPTY
                       — turn complete, nothing queued, zero sockets, zero
                       open dispatch rows addressed to it — is IDLE, not hung.
                       Out of work is not stuck; the hang remedy aimed at an
                       idle seat kills a healthy pane.

Any input it cannot read makes the verdict HUNG-UNKNOWN, never HUNG — the
load-bearing law, same as logscan's: a check that cannot see a case returns
UNKNOWN, never a false verdict.

THE 2026-07-30 CODEX FAMILY WALL REPLACED THE MTIME CLOCK WITH TURN REALITY.
For 65 minutes a codex pane, local probe, CPU, socket, and transcript mtime
all looked alive. The last completed semantic entry did not move: upstream
fingerprint-shedding made each request retry for minutes, and every retry
wrote queue-operation bookkeeping that refreshed file mtime. Activity was not
progress. So staleness is measured at the last COMPLETED semantic entry, and
raw mtime survives only for the second question — did retries keep writing
(churn), or did the transcript stop completely (silence)?

HOW "IN-FLIGHT" IS READ, and why not from proxy.log: the gin logger writes ONE
line per request, at COMPLETION, with its duration — an open/streaming request
writes NOTHING until it finishes, so log-silence cannot distinguish a
90-minute generation from a dead turn loop. The kernel's socket table can:
an in-flight request IS an ESTABLISHED client connection to the seat's proxy
port, held for the whole generation. Measured live 2026-07-29 across all five
seats: codex mid-work held 5, kimi and ds4pro 1 each, idle grok and gemini
exactly 0. The keep-alive corner composes itself away: a socket lingering
after a COMPLETED request coexists with a fresh transcript, so the stale-gate
already excludes it — for keep-alive to fake THINKING a client would have to
hold an idle socket past the stale bar, and no HTTP client keeps idle sockets
open for 45 minutes. The family wall added the socket's limit: the victim
held one THROUGHOUT the dark window, so a held socket is pending work, never
proof of progress — beside FRESH nonsemantic writes it is churn and composes
toward HUNG; only a socket with nothing written since the prompt still earns
THINKING.

WHY CHANGE-ONLY IS THE DESIGN, not a nicety. A timer that posts every pass
trains everyone to filter it, and a filtered alarm is an absent one. This
latches on a fingerprint of the health state and speaks when the state MOVES —
so silence is meaningful and a message means something actually changed.

THE OWNER IS GUI-FIRST. He must never have to run this to learn its answer, so
a state change posts to the room he reads and lands on the board he scans. The
timer is the agent's surface; the post is his.
"""
import contextlib
import glob
import hashlib
import json
import math
import os
import re
import stat as stat_module
import subprocess
import sys
import time
import fcntl

from . import home
from . import proxyjournal as _journal
from .yaml_scalar import yaml_scalar, yaml_scalar_typed
from . import pk

INTERVAL_S = 15 * 60      # mirrors the INSTALLED helm-proxywatch.timer
                          # (OnBootSec/OnUnitActiveSec=900s): installed is
                          # truth — the cadence is operational, so the code
                          # updates to match the machine, never the reverse
                          # (ruled 2026-08-04). The change/dark latch keeps
                          # healthy passes quiet at any period.
HANG_S = 45 * 60          # a seat with a live pane and no COMPLETED SEMANTIC
                          # transcript entry for this long is a HANG CANDIDATE,
                          # never a verdict — an agent legitimately thinks for a
                          # long time, and the owner's own report was that codex
                          # "hangs", so the bar is deliberately suggestive not
                          # accusatory. Raw mtime never moves this clock.
_STATE = "proxywatch.json"

_SERVICE = """[Unit]
Description=helm proxywatch (cli-proxy fix invariants + seat liveness, one pass)

[Service]
Type=oneshot
# Run FROM the repo: helm derives a post's room from cwd, and a unit with no
# WorkingDirectory starts in $HOME, derives no project, and falls back to
# #main — so every watchdog alert landed in a room no seat homes in, while the
# agents that could act on it live in #helm. Measured 2026-07-29.
WorkingDirectory=%(cwd)s
ExecStart=%(helm)s proxywatch --post
# Exit 1 means the watch RAN and FOUND FAULTS (they are latched and posted);
# only exit 2 means the watchdog itself failed. Without this line the two are
# one signal: systemctl read `failed (exit-code 1)` off this unit all through
# 2026-08-03 while it was working correctly, so a dead watchdog and a working
# one were indistinguishable.
SuccessExitStatus=1
"""

_TIMER = """[Unit]
Description=helm proxywatch cadence (external, no demons)

[Timer]
OnBootSec=%(interval)ss
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def _backup_state_path():
    return _state_path() + ".last-good"


# THE VENDOR-RESET OWNER CHANNEL (task/45). A dark family with no recorded
# horizon reads as INDEFINITELY broken; the one person who knows the vendor's
# reset window — the owner, holding the provider's own page — had no door to
# say it. Two clocks must never be collapsed into one field (the
# provenance law): the auth-file `next_retry_after` is the MEASURED proxy
# bench horizon, and the owner-entered value is the VENDOR quota reset. Every
# record this channel produces therefore carries its provenance inline —
# reset_kind=vendor, reset_source=owner, recorded_at — so a reader never
# mistakes a typed-in page value for a measured provider header.
_VENDOR_RESETS = "proxywatch-vendor-resets.json"
_VENDOR_RESET_KIND = "vendor"
_VENDOR_RESET_SOURCE = "owner"


def _vendor_resets_path():
    return os.path.join(home.helm_home(), home.GLOBAL, _VENDOR_RESETS)


def read_vendor_resets():
    """({family: record}, error_or_none) — the owner-entered vendor resets.

    Tri-state like every store read here: a MISSING file is ({}, None) — no
    owner input yet is not a fault — and a corrupt or wrong-shaped file is
    ({}, error), never a silent empty that reads as "no horizons recorded".
    Per-family entries that are malformed are DROPPED with the file still
    answering: one bad row must not blank the families the owner did set."""
    path = _vendor_resets_path()
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, "vendor-reset config unreadable — %s" % e
    if not isinstance(raw, dict):
        return {}, "vendor-reset config is not an object"
    out = {}
    now_ms = int(time.time() * 1000)
    for family, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        ms = rec.get("resets_at_ms")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) \
                and math.isfinite(ms):
            # A PAST horizon is not a horizon: the reset has already passed,
            # and rendering "resets in 0s" forever is the indefinitely-broken
            # reading this channel exists to cure (a review's P1 — T-1s was
            # composed and the UI rendered "now" forever).
            if int(ms) <= now_ms:
                continue
            out[str(family)] = {
                "resets_at_ms": int(ms),
                "reset_kind": _VENDOR_RESET_KIND,
                "reset_source": _VENDOR_RESET_SOURCE,
                "recorded_at": rec.get("recorded_at"),
            }
    return out, None


def _vendor_config_lock():
    """An exclusive flock on a sidecar file for the config's read-modify-write
    — two synchronized passes (timer + operator) both succeeding and one
    family lost is a write race, not a validation failure (a review's P1).
    Returns the open fd; caller closes.

    O_CREAT makes the FILE, never the parent DIRECTORY — on a fresh home
    this open raised FileNotFoundError and the owner's FIRST use of the
    verb got a traceback. The directory is made first,
    here and never assumed."""
    lock_path = _vendor_resets_path() + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def write_vendor_reset(family, resets_at_ms):
    """Set (or replace) one family's owner-entered vendor reset. Validates the
    horizon is a finite epoch-ms in the FUTURE — a past horizon is a stale
    page, and persisting it would render "reset 3h ago" as if it were news.
    The read-modify-write runs under an exclusive flock. Returns (record,
    None) or (None, error)."""
    family = str(family or "").strip()
    if not family:
        return None, "a family name is required"
    if not isinstance(resets_at_ms, (int, float)) \
            or isinstance(resets_at_ms, bool) \
            or not math.isfinite(resets_at_ms):
        return None, "resets_at_ms must be a finite epoch-ms number"
    ms = int(resets_at_ms)
    now_ms = int(time.time() * 1000)
    if ms <= now_ms:
        return None, ("the horizon is in the PAST (%s) — a stale page value, "
                      "not a reset; re-read the provider's page"
                      % _iso_from_ms(ms))
    fd = _vendor_config_lock()
    try:
        table, err = read_vendor_resets()
        if err:
            return None, err
        rec = {"resets_at_ms": ms, "reset_kind": _VENDOR_RESET_KIND,
               "reset_source": _VENDOR_RESET_SOURCE,
               "recorded_at": _iso_from_ms(now_ms)}
        table[family] = rec
        from . import pk
        pk.write_json(_vendor_resets_path(), table)
    finally:
        os.close(fd)
    return rec, None


def clear_vendor_reset(family):
    """Remove one family's owner-entered reset -> (removed_bool, error).
    Same flock as the writer — a clear racing a set must not resurrect."""
    fd = _vendor_config_lock()
    try:
        table, err = read_vendor_resets()
        if err:
            return False, err
        family = str(family or "").strip()
        if family not in table:
            return False, None
        del table[family]
        from . import pk
        pk.write_json(_vendor_resets_path(), table)
    finally:
        os.close(fd)
    return True, None


def _iso_from_ms(ms):
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_reset_instant(text):
    """(epoch_ms, error) for an owner-typed reset horizon. Accepts epoch-ms,
    epoch-s (a bare 10-digit number is seconds, a 13-digit one ms), or an
    ISO-8601 instant. Anything else is an error, never a guess — a horizon
    parsed wrong renders a wrong certainty on the owner's own console."""
    import datetime
    s = str(text or "").strip()
    if not s:
        return None, "a reset time is required (ISO-8601 or epoch)"
    if re.fullmatch(r"\d{13}", s):
        return int(s), None
    if re.fullmatch(r"\d{10}", s):
        return int(s) * 1000, None
    try:
        norm = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            return None, ("ISO time %r has no timezone — UTC Z or an offset "
                          "is required, a naive instant is a guess" % s)
        return int(dt.timestamp() * 1000), None
    except ValueError:
        return None, ("could not parse %r — use ISO-8601 (2026-08-07T20:36:00Z)"
                      " or epoch-ms" % s)


#: Yielded to a delivery boundary in place of a pause decision when this lock
#: could not be taken inside the hook's remaining budget. TRUTHY on purpose:
#: every delivery site already reads a truthy pause as "deliver nothing, change
#: nothing, return", so the bounded wait needed no new branch anywhere — only a
#: name, so the reason reaches the record instead of reading as a pause the
#: proxy never declared.
DELIVERY_BUSY = {"state": "BUSY", "stale": True,
                 "observer_error": "delivery-state lock busy"}


def delivery_wait():
    """Seconds a hook boundary may WAIT for this lock, or None for unbounded.

    HALF the alarm left on the calling process, so the delivery that follows
    keeps at least as much time as the wait spent. A FRACTION and never a
    number: `hooks.spec_command` owns the budget, and a number here would have
    to be re-measured every time that budget or this box moved. None — no alarm
    armed, so a CLI run, a timer or a test — is the unbounded behaviour every
    non-hook caller has always had.
    """
    from . import hooklatency
    left = hooklatency.remaining_budget()
    return None if left is None else left / 2.0


@contextlib.contextmanager
def delivery_state_guard(wait=None):
    """Serialize one actuator decision with proxywatch state replacement.

    Yields True while the lock is HELD and False when a caller-supplied `wait`
    ran out first. A caller that passes nothing blocks exactly as it always
    has and can only ever be handed True.

    Atomic rename prevents torn reads but cannot order "checked healthy" against
    "recorded dark" and a later cursor commit. Delivery holds this short lock
    through its cursor mutation; record() holds it only around the atomic write.
    The authenticated canary remains outside, so a chat boundary never waits on
    provider I/O.

    WHY A BOUNDED WAIT EXISTS AT ALL, and why it is OPT-IN rather than the
    default. This lock is fleet-wide and every seat takes it on every tool
    call, so the HOLDS are short and the QUEUE is not: measured in production
    on this host, five consecutive PostToolUse delivery timeouts in one minute,
    every one of them killed at its 2s budget with `blocked_in` naming this
    acquisition, having waited 1.6-1.9s of that budget here — before any room
    was read, so the owner's chat was not delivered and the banner said
    UNCHECKED. Delivery is idempotent and cursor-driven: not delivering on THIS
    tool boundary costs one boundary of latency, while being killed costs the
    whole event and a line the owner cannot act on.

    OPT-IN because the other holders are not on a budget and must not learn to
    skip. `record()` writing proxywatch state and the chat writer that replaces
    it are not retried by a next tool call; a silent skip there would lose the
    write this lock exists to order. They pass no `wait` and are untouched.
    """
    path = os.path.join(os.path.dirname(_state_path()), ".proxywatch-state.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    held = False
    try:
        from . import hooklatency
        deadline = None if wait is None else time.monotonic() + wait
        held = bool(hooklatency.flock(fd, fcntl.LOCK_EX, "lock-proxywatch",
                                      deadline=deadline))
        if not held:
            # THE REASON REACHES THE RECORD, not only the caller. A boundary
            # that delivers nothing looks identical to one with nothing to
            # deliver, and the outcome channel is the only place the two
            # separate. SKIPPED and never UNCHECKED: nothing was examined and
            # nothing failed, and UNCHECKED is what prints a banner.
            try:
                from . import hookoutcome
                hookoutcome.declare(hookoutcome.SKIPPED,
                                    "delivery-state lock busy; the next tool "
                                    "boundary delivers")
            except Exception:                        # noqa: BLE001
                pass
        yield held
    finally:
        try:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

def _read_watch_state():
    """(state_dict, error_or_none) — the last persisted watch record.

    Tri-state, because a corrupt outbox is the precise case at-least-once
    exists for and reading it as {} silently converts the safety mechanism
    into a loss. A probe wrote "{corrupt json[" to the state path,
    _read_watch_state returned {}, pending_chat -> [], and a queue that HAD
    pending alerts silently delivered nothing. A blind read must never
    masquerade as clean.

    Returns (parsed_dict, None) on valid read, ({}, "missing") on a genuinely
    absent file (first run is not an error), and ({}, error_string) on any
    unreadable/corrupt state. Callers must refuse the pass on an error.
    """
    try:
        with pk.open_regular(_state_path(), encoding="utf-8") as f:
            return json.load(f) or {}, None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, "proxywatch state unreadable — %s" % e


# TURN REALITY. The codex family wall: for 65 minutes a codex
# pane, local probe, CPU, socket, and transcript MTIME all looked alive while
# the last COMPLETED semantic entry never moved — upstream fingerprint-shedding
# made each request retry for minutes, and every retry wrote queue-operation
# bookkeeping that refreshed the file. Activity was not progress. So staleness
# is measured at the last completed semantic entry (a persisted main-chain
# assistant response or tool result), and raw mtime survives only to answer
# the SECOND question: did retries keep writing, or did the transcript stop
# completely? User prompts, queue operations, attachments, and CPU ticks never
# advance semantic age.

def _read_delivery_state():
    """Best readable canonical snapshot for the delivery actuator.

    The primary remains proxywatch's transactional outbox authority. Delivery
    additionally keeps one byte-equivalent last-good snapshot so deleting or
    corrupting the primary during a dark episode cannot masquerade as HEALTHY.
    A genuinely first-run absence (neither file exists) stays empty and does not
    pause every proxy before the watcher has ever measured one.
    """
    state, err = _read_watch_state()
    primary_exists = os.path.exists(_state_path())
    shape_err = upstream_records(state)[1] if primary_exists and not err else None
    if primary_exists and not err and not shape_err:
        return state, None
    try:
        with pk.open_regular(_backup_state_path(), encoding="utf-8") as handle:
            backup = json.load(handle)
        if upstream_records(backup)[1] is None:
            return backup, None
    except (OSError, ValueError, TypeError):
        pass
    if not primary_exists and not err:
        return {}, None
    return state, err or shape_err or "proxywatch delivery snapshot unreadable"


def _parse_timestamp(value):
    """Epoch seconds for one transcript timestamp, or None when opaque."""
    try:
        import datetime
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _reverse_records(path, chunk=64 * 1024):
    """Newest-first JSON records without loading a multi-megabyte transcript.

    The caller stops at the first completed semantic record, so a normal pass
    reads one chunk. Retry churn can push that record farther back; walking
    until it is actually found is the contract — a byte cap would turn a long
    outage UNKNOWN at the exact moment this probe is needed most.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos, carry = f.tell(), b""
            while pos:
                n = min(chunk, pos)
                pos -= n
                f.seek(pos)
                parts = (f.read(n) + carry).split(b"\n")
                carry = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        yield row
            if carry.strip():
                try:
                    row = json.loads(carry.decode("utf-8", "replace"))
                except ValueError:
                    row = None
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _semantic_record(row):
    """(kind, turn_complete) for completed main-chain work, else None.

    Queue operations, attachments, hook metadata, and user prompts are writes,
    not progress. A persisted assistant response is semantic progress; a tool
    result is too, because the requested operation completed and entered the
    conversation. ``tool_use`` leaves the enclosing turn open; another
    assistant stop reason leaves it idle until a later prompt arrives.
    """
    if row.get("isSidechain"):
        return None
    msg = row.get("message")
    if row.get("type") == "assistant" and isinstance(msg, dict) \
            and msg.get("stop_reason"):
        return "assistant", msg.get("stop_reason") != "tool_use"
    if row.get("type") != "user" or not isinstance(msg, dict):
        return None
    blocks = msg.get("content") or []
    if any(isinstance(b, dict) and b.get("type") == "tool_result"
           for b in blocks):
        return "tool-result", False
    return None


def _queue_pending(operations):
    """Whether chronological queue operations leave work enqueued."""
    depth = 0
    for operation in operations:
        if operation == "enqueue":
            depth += 1
        elif operation in ("dequeue", "remove"):
            depth = max(0, depth - 1)
        elif operation == "popAll":
            depth = 0
    return depth > 0


_FANOUT_WINDOW_S = 180


def fanout_reading(instance_dir, now=None, window_s=_FANOUT_WINDOW_S):
    """How many SUBAGENTS this seat has written recently — the DEMAND term.

    helm measures SUPPLY precisely — per-family upstream health, per-account
    quota, cred pooling, reset horizons — and DEMAND not at all, so the number
    that actually moves a credential meter has never had a surface. Measured
    2026-08-06: ONE seat in fan-out mode held five concurrent subagents at
    131k/138k/140k/159k/50k tokens, ~618k in flight, while every instrument the
    fleet owns reported only that three seats existed. SEAT COUNT IS ADDITIVE;
    FAN-OUT IS MULTIPLICATIVE, and the integrator rationed on the additive one
    because it was the only one displayed. The instrument selected the
    hypothesis.

    WHAT THIS COUNTS, AND WHAT IT REFUSES TO CLAIM. A subagent transcript
    written inside ``window_s`` is ACTIVE — which is RUNNING *or* FINISHED
    MOMENTS AGO, and this does not pretend to tell those apart. Separating
    them needs a SECOND SAMPLE (size growth across two passes); mtime alone
    cannot, and a count that silently called a just-finished subagent
    "running" would overstate demand at exactly the moment a drain completes
    — the moment a controller is deciding whether it is safe to raise a cap
    again. It would hold the fleet down on a phantom. Live near-miss the day
    this was written: the bare count read 1 for a seat that had just
    truthfully reported DRAIN COMPLETE, and was one sentence from being used
    to contradict it.

    ``active`` is None when the directory cannot be read, NEVER 0 — an UNKNOWN
    is not a measured zero, and a controller must treat them differently.
    """
    # ZERO IS ONLY REACHABLE FROM A DIRECTORY WE PROVED WE COULD READ.
    #
    # Two cuts of this got it wrong in the same direction, and both were found
    # by somebody else. (1) A missing instance dir answered 0 — caught by a
    # must-hit control against the LIVE fleet, where one seat has no dir at
    # all and rendered as a quiet seat. (2) A review's exact-source probes then
    # found three MORE collapses, and the root cause is sharper than "add a
    # check": `glob` SILENTLY RETURNS [] on an unreadable directory. It does
    # not raise. So the `try/except OSError` wrapped around it was DEAD CODE —
    # it could never fire, and every unreadable path became a confident zero.
    #
    # An honest primitive answering a question it cannot tell apart is the
    # whole failure mode of this function's subject matter, committed twice
    # inside the function itself. The cure is OWNING the error rather than
    # borrowing someone else's silence: walk with scandir, and let only a
    # vanished FILE (ENOENT — a real race with a finishing subagent) be
    # skipped. Every other stat or listing failure is UNKNOWN.
    if not instance_dir or not os.path.isdir(instance_dir):
        return {"active": None, "window_s": window_s}
    now = time.time() if now is None else now
    projects = os.path.join(instance_dir, "claude", "projects")
    # The expected root must EXIST and be READABLE before a zero means
    # anything. A `claude` or `projects` component that is a FILE, or a dir we
    # lack +rx on, is a layout we do not understand — not an idle seat.
    try:
        project_dirs = [e.path for e in os.scandir(projects) if e.is_dir()]
    except OSError:
        return {"active": None, "window_s": window_s}
    active = 0
    for proj in project_dirs:
        try:
            sessions = [e.path for e in os.scandir(proj) if e.is_dir()]
        except OSError:
            return {"active": None, "window_s": window_s}
        for sess in sessions:
            sub = os.path.join(sess, "subagents")
            # A REVIEW'S RULE, adopted verbatim because it is generative where
            # mine was only descriptive. AT EVERY EXPECTED TYPED PATH, PRESERVE
            # THREE OUTCOMES: the EXPECTED KIND descends or counts, a
            # SEMANTICALLY LEGITIMATE ENOENT skips, and WRONG TYPE OR ANY OTHER
            # ERROR is UNKNOWN. An ambiguous negative is safe ONLY when every
            # cause of it shares one outcome. My own rule — "anywhere the walk
            # can say no for more than one reason" — named the smell and found
            # nothing; this one generated two boundaries by construction, and
            # a probe MEASURED both rather than arguing them: a dangling
            # `subagents` symlink read 0, and a DIRECTORY named agent-x.jsonl
            # read 1.
            #
            # lstat FIRST: os.stat FOLLOWS SYMLINKS, so a dangling subagents
            # link raises ENOENT and reads as "this session never fanned out".
            # The link EXISTS; it is malformed. That is UNKNOWN, not absence.
            try:
                st = os.lstat(sub)
            except FileNotFoundError:
                continue                    # genuinely no subagents dir
            except OSError:
                return {"active": None, "window_s": window_s}
            if stat_module.S_ISLNK(st.st_mode):
                try:                        # a link is fine if it RESOLVES
                    st = os.stat(sub)
                except OSError:             # dangling or unresolvable
                    return {"active": None, "window_s": window_s}
            if not stat_module.S_ISDIR(st.st_mode):
                return {"active": None, "window_s": window_s}
            try:
                entries = list(os.scandir(sub))
            except OSError:
                return {"active": None, "window_s": window_s}
            for e in entries:
                name = e.name
                if not (name.startswith("agent-")
                        and name.endswith(".jsonl")):
                    continue
                # NAME IS NOT TYPE. The filter above is a naming convention and
                # nothing enforces it: a DIRECTORY called agent-x.jsonl passed
                # it and counted as a live subagent (measured 1). Prove REGULAR
                # FILE before the mtime means anything — and prove it with
                # follow_symlinks=False so the ENOENT skip below cannot absorb
                # a dangling link as a finishing subagent.
                try:
                    est = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue                # a finishing subagent's race
                except OSError:
                    return {"active": None, "window_s": window_s}
                if stat_module.S_ISLNK(est.st_mode):
                    try:
                        est = e.stat()      # resolve it, or it is malformed
                    except OSError:
                        return {"active": None, "window_s": window_s}
                if not stat_module.S_ISREG(est.st_mode):
                    return {"active": None, "window_s": window_s}
                if now - est.st_mtime < window_s:
                    active += 1
    return {"active": active, "window_s": window_s}


def transcript_reality(path, now=None):
    """Age and shape of the last COMPLETED SEMANTIC transcript entry.

    File mtime survives only as raw-write age. It never counts as progress:
    the founding 65-minute victim wrote fresh ``queue-operation`` retry rows
    while no assistant response or tool result completed. Every field keeps
    None as "could not read", never as a measured zero.
    """
    now = time.time() if now is None else now
    try:
        write_age = max(0, now - os.path.getmtime(path))
    except OSError:
        return {"semantic_age_s": None, "semantic_ts": None,
                "write_age_s": None, "semantic_kind": None,
                "turn_complete": None, "pending_after": None,
                "newest_type": None}
    user_pending, queue_operations = False, []
    newest_type = None
    for row in _reverse_records(path):
        newest_type = newest_type or row.get("type")
        semantic = _semantic_record(row)
        if semantic:
            at = _parse_timestamp(row.get("timestamp"))
            return {"semantic_age_s": max(0, now - at) if at is not None else None,
                    "semantic_ts": row.get("timestamp"),
                    "write_age_s": write_age,
                    "semantic_kind": semantic[0],
                    "turn_complete": semantic[1],
                    "pending_after": user_pending or
                    _queue_pending(reversed(queue_operations)),
                    "newest_type": newest_type}
        typ = row.get("type")
        if typ == "queue-operation":
            queue_operations.append(row.get("operation"))
        elif typ == "user":
            msg = row.get("message")
            if isinstance(msg, dict) and msg.get("content") is not None:
                user_pending = True
    return {"semantic_age_s": None, "semantic_ts": None,
            "write_age_s": write_age, "semantic_kind": None,
            "turn_complete": None,
            "pending_after": user_pending or
            _queue_pending(reversed(queue_operations)),
            "newest_type": newest_type}


def health(seats=None, include_upstream=True, prior_state=None,
           include_probe=True):
    """The composite -> a dict. Reads only; never posts, never writes.

    The authenticated family rung is deliberately LAST: the current pane census,
    lazy fuse readers and turn_state ladder derive exactly as before, then the
    completed rows are grouped by family for upstream corroboration. Local-only
    callers pass include_upstream=False and spend no canary tokens.

    include_probe=False drops the PROBE rung, and ONLY that rung — a DISPLAY
    that composes this report (`helm seat list`'s usability join) must not open
    an HTTP request per seat every time an operator scans the roster. Same law
    `upstream_snapshot` already states for the wall: A DISPLAY MUST NOT PROBE.
    The skipped fields stay None, which every reader here already treats as
    "not measured" — `turn_state` never consults `probe`, and `findings()`
    raises no probe finding from a probe-less pass, which is correct: a rung
    that did not run has found nothing.
    """
    from . import seat as seatmod
    from . import silent_drop, seats as seatsmod
    from . import autocompact
    rows, now = [], time.time()
    names = seats or [s for s in _watched_seats()]
    runtime_roster = seatsmod.roster()
    for name in names:
        runtime_row = runtime_roster.get(name) or {}
        runtime = runtime_row.get("runtime")
        runtime_verified = runtime_row.get("runtime_verified") is True
        family, err = seatmod.family_for(name, runtime, runtime_verified)
        verified_family = runtime.get("family") \
            if runtime_verified and isinstance(runtime, dict) \
            and runtime.get("family") == family else None
        row = {"seat": name, "family": family if not err else None,
               "config_ok": None, "drift": [], "alerted_at": None,
               "transcript_age_s": None, "write_age_s": None,
               "semantic_kind": None, "turn_complete": None,
               "pending_after": None, "hang_candidate": False,
               "probe": None, "probe_detail": None, "probe_ms": None,
               "log": None, "log_detail": None, "log_status_401": None,
               "log_empty_turns": None,
               "inflight": None, "ctx_pct": None, "spawn_age_s": None,
               "open_dispatches": None, "fanout": None,
               "turn_state": None, "turn_evidence": None,
               "upstream": None, "upstream_detail": None,
               "upstream_ms": None, "upstream_since": None}
        if err:
            row["error"] = err
            rows.append(row)
            continue
        phome = seatmod._proxy_home(family, name)
        cfg = os.path.join(phome, "config.yaml")
        drift = seatmod.config_drift(cfg) if os.path.exists(cfg) else []
        row["config_ok"] = not drift
        row["drift"] = ["%s=%s want %s" % (k, g, w) for k, w, g, _why in drift]
        log = log_observation(os.path.join(phome, "proxy.log"))
        row["log"], row["log_detail"] = log["state"], log["detail"]
        row["log_status_401"] = log["status_401"]
        row["log_empty_turns"] = log.get("empty_turns")
        try:
            from . import pi as pimod
            port, perr = pimod.seat_port(
                name, verified_family=verified_family)
            if perr:
                row["probe_detail"] = perr
            elif port and include_probe:
                row["probe"], row["probe_detail"], row["probe_ms"] = probe(port)
        except Exception as ex:             # noqa: BLE001 — a watch never raises
            row["probe_detail"] = "proxy endpoint resolution failed: %s" % ex
        # transcript_age_s is SEMANTIC age — the last COMPLETED assistant
        # response or tool result — never file mtime. The founding victim's
        # mtime stayed fresh for 65 minutes of retry bookkeeping.
        inst = seatmod._instance_dir(family, name)
        # The DEMAND term, off the SAME instance dir the semantic age already
        # resolves — one locator, not a second census that inherits none of
        # this one's scars.
        row["fanout"] = fanout_reading(inst, now=now)
        tp = autocompact._newest_transcript(inst)
        reality = transcript_reality(tp, now=now) if tp else {
            "semantic_age_s": None, "semantic_ts": None, "write_age_s": None,
            "semantic_kind": None, "turn_complete": None,
            "pending_after": None, "newest_type": None}
        row["transcript_reality"] = reality
        row["transcript_age_s"] = reality.get("semantic_age_s")
        row["write_age_s"] = reality.get("write_age_s")
        row["semantic_kind"] = reality.get("semantic_kind")
        row["turn_complete"] = reality.get("turn_complete")
        row["pending_after"] = reality.get("pending_after")
        rows.append(row)
    # the silent-drop latch is the DROP rung's memory: an alert timestamp per
    # seat. A NEW one since our last pass is the signal the bench lift said
    # would put the bench back.
    try:
        from . import pk
        latch = pk.read_json(silent_drop._state_path(), {}) or {}
    except Exception:                       # noqa: BLE001 — a watch never raises
        latch = {}
    # KEPT APART, BECAUSE THIS LOOP RENDERS ONE ROW PER SEAT. `fold_blind`'s
    # single answer is right for a caller that acts on one yes/no; here it made
    # one seat's unkeyable process the explanation printed on EVERY other
    # seat's row. `host_blind` still reaches every row, because the pid behind
    # it could be any seat's; a per-seat refusal reaches only its own seat
    # (task/2739 — `_live_seats` carries the split's argument and the
    # measurement behind it).
    live, host_blind, seat_blind = _live_seats()
    open_counts, open_scanned = None, False
    for row in rows:
        entry = latch.get(row["seat"]) or {}
        row["alerted_at"] = entry.get("alerted_at")
        age = row["transcript_age_s"]
        census_blind = host_blind or seat_blind.get(row["seat"])
        # THREE-VALUED, like the census it comes from. None is "helm could not
        # look", and it must not collapse into False — False is the positive
        # claim "helm read every claude on this host and none names this seat",
        # which is what earns the `off` verdict.
        row["pane_live"] = (True if row["seat"] in live
                            else None if census_blind else False)
        row["census_blind"] = census_blind
        row["hang_candidate"] = bool(row["pane_live"] and age is not None
                                     and age > HANG_S)
        if row.get("error"):
            continue                        # an unknown seat gets no verdict
        # The fuse's extra readers run ONLY for the candidate shape (live pane,
        # stale-or-undated transcript): a fleet that turned recently pays
        # nothing new, and the socket census is never read for a seat whose
        # answer cannot change the verdict. The dispatch ledger is folded once
        # per pass, and only when a candidate needs it.
        if row["pane_live"] and (age is None or age > HANG_S):
            row["inflight"] = _inflight_for(row["seat"])
            row["ctx_pct"] = _ctx_pct(row["seat"])
            row["spawn_age_s"] = _spawn_age_s(row["seat"])
            if not open_scanned:
                open_counts, open_scanned = _open_work(), True
            if open_counts is None:
                row["open_dispatches"] = None
            else:
                from . import seats as _seats
                recipient, _err = _seats._canonical_recipient(row["seat"])
                row["open_dispatches"] = open_counts.get(str(recipient or ""), 0)
        # PARSED ONCE, HERE, FOR EVERY ROW — not only for a hang candidate
        # like `spawn_age_s` above. A register nobody can read is a fact about
        # the record and not about staleness, so gating it on the candidate
        # shape would make a corrupt register on a busy seat silent, which is
        # the class this door exists to end. One small read per seat per pass.
        onboarding = _spawn_onboarded(row["seat"], family=row.get("family"))
        # THE KIND IS THE READING AND THE BOOL IS THE DECISION. `findings()`
        # reduces the report, and a reducer that compared the kind string
        # would own a second copy of this vocabulary — the drift the door was
        # built to remove. It reads the decision the door already made.
        row["onboarding_kind"] = onboarding.kind
        row["onboarding_unreadable"] = onboarding.unreadable
        row["onboarding_reason"] = onboarding.reason
        row["onboarding_proof"] = onboarding.proof
        # THE AGE IS DERIVED HERE, NEVER AT THE DOOR. The door returns an
        # ABSOLUTE validated moment; only this layer knows `now`, and only
        # this layer is allowed to turn one into the other.
        row["onboarding_age_s"] = onboarding_age_s(onboarding, now=now)
        row["turn_state"], row["turn_evidence"] = turn_state(
            row["pane_live"], age, row.get("log"), row["inflight"],
            row["ctx_pct"], _compact_threshold(), row["spawn_age_s"],
            pane_blind=census_blind, reality=row.get("transcript_reality"),
            open_dispatches=row["open_dispatches"],
            suspend_gap_s=host_suspend_gap_s(), onboarding=onboarding,
            onboarding_age_s=row["onboarding_age_s"])
        # Sample the pane-tail classifier HERE, at the owner layer, so the
        # renderer stays a pure reduction (r1 HIGH): findings() reading
        # seat_liveness live made the verdict depend on WHEN it was asked —
        # one immutable report returned HUNG on one render and BLOCKED-HUMAN
        # on the next as the pane changed under it. Sampled once and stored,
        # the verdict is a fact of the report; folded into fingerprint(), a
        # real IDLE/UNKNOWN/BLOCKED transition is a state MOVE and speaks.
        # Only the hung candidate pays the pane read — every other verdict is
        # unaffected by it.
        row["liveness"] = None
        if row["turn_state"] == "hung":
            try:
                from . import seat as _seatmod
                row["liveness"] = _seatmod.seat_liveness(row["seat"])
            except Exception:
                row["liveness"] = None
        if row["turn_state"] == "idle":
            # every pending census measured EMPTY — the opposite of a hang
            # shape, so the candidate flag must not survive the verdict
            row["hang_candidate"] = False
    upstream = upstream_health(rows, now=now, prior=prior_state) \
        if include_upstream else {}
    runtime_proofs = proxy_runtime_proofs(
        rows, observed_at=int(now)) if include_upstream else {}
    for row in rows:
        family = upstream.get(row.get("family")) or {}
        measured = upstream_seat_sample(upstream, row.get("family"), row["seat"])
        row["upstream"] = measured.get("state")
        row["upstream_detail"] = measured.get("detail")
        row["upstream_ms"] = measured.get("ms")
        row["upstream_since"] = measured.get("since")
    return {"ts": int(now), "seats": rows, "upstream": upstream,
            "proxy_runtime": runtime_proofs}


# The PROBE rung. Credit where it is due: this idea was not mine — an untracked
# proxywatch.py appeared at the repo root the same minutes this module was
# written, by a seat that never claimed it, and it carried a
# check_proxy_status() that actually TALKED to the proxy. Preserved at tag
# rescue/proxywatch-root. Reading config and transcript age never asks the
# endpoint anything, and a TCP connect only proves a socket is open; a timed
# request proves the thing BEHIND it answers.

PROBE_TIMEOUT_S = 6
_BAD_KEY = "helm-proxywatch-deliberately-invalid"   # never a real credential


def probe(port, timeout=PROBE_TIMEOUT_S):
    """(state, detail, ms) — ask the proxy a question it must refuse.

    NO CREDENTIAL IS SENT, and that is what makes this both safe and sharp: an
    UNAUTHENTICATED request distinguishes every failure mode on its own.

        refused    the proxy is down
        401        UP, and its auth path works — the healthy answer
        200 + tiny THE BUG. A wrong key answering 200-with-empty-body is the
                   known CLIProxyAPI fault that makes an auth failure
                   indistinguishable from a dropped completion, which is the
                   shape the owner reported for a week.
        timeout    a HANG, measured at the endpoint rather than inferred from a
                   quiet transcript

    Measured 2026-07-29 across ports 8317/8360/8390: all three answered 401
    with a 27-byte body, so the empty-200 fault is NOT live on this host today
    — which is itself evidence about the fixes, and the reason this rung exists
    rather than a premise asserting it.
    """
    import json as _json
    import urllib.error
    import urllib.request
    body = _json.dumps({"model": "probe", "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}]}
                       ).encode("utf-8")
    # THE PROBE LABELS ITSELF, exactly as the authenticated canary already does
    # (b503279 added ?beta=true&helm_canary=1 to the /v1/messages probe). The
    # gin logger records the QUERY STRING and no headers, so a query marker is
    # the only form of "identifying header" the log can actually carry — the
    # Authorization bearer below is invisible to every reader of proxy.log.
    #
    # WHY IT MATTERED BEFORE ANYTHING CONSUMES IT: this probe deliberately
    # sends an invalid key to prove the proxy answers 401, and its rows are
    # indistinguishable from real traffic on the same path. Reading those 401s
    # as a fault is not hypothetical — they were once reported to the owner as
    # the root cause of a seat's unreliability, and a starvation scan drafted
    # from the proxy-log 4xx tail would have called EVERY healthy seat starved
    # (codex carries ~344 of these beside ~241 real successes).
    #
    # The marker is consumed twice at the owner layer: refusal-state scanning
    # excludes Helm canaries, while log_observation accounts every 401 as
    # Helm-marked or unmarked traffic within an explicit bounded tail. The public
    # marker is not authenticated origin; endpoint alone never decides provenance.
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions?%s" % (port, _CANARY_QUERY),
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _BAD_KEY})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read(CANARY_MAX_BODY + 1)
            ms = int((time.time() - t0) * 1000)
            if len(payload) > CANARY_MAX_BODY:
                return ("OVERSIZE", "HTTP %d body exceeded canary cap of %d bytes"
                        % (r.status, CANARY_MAX_BODY), ms)
            if r.status == 200 and len(payload) < 64:
                return ("EMPTY200", "answered 200 with a %d-byte body to an "
                        "INVALID key — an auth failure that looks exactly like "
                        "a dropped completion" % len(payload), ms)
            return "ok", "HTTP %d" % r.status, ms
    except urllib.error.HTTPError as e:
        try:
            code = e.code
        finally:
            e.close()
        ms = int((time.time() - t0) * 1000)
        if code in (401, 403):
            return "healthy", "HTTP %d — refused an invalid key correctly" % code, ms
        return "ok", "HTTP %d" % code, ms
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        ms = int((time.time() - t0) * 1000)
        if ms >= timeout * 1000 - 100:
            return "hang", "no response in %ds — measured at the ENDPOINT, not " \
                           "inferred from a quiet transcript" % timeout, ms
        return "down", "%s" % e, ms


# The UPSTREAM rung. The invalid-key probe above proves the LOCAL listener and
# auth path answer; this one spends eight tokens through the configured route and
# asks whether the FAMILY can complete a fresh request now.
UPSTREAM_TOKENS = 8
UPSTREAM_TIMEOUT_S = 30
UPSTREAM_CONFIRM_S = 3
DARK_FALSIFICATION_S = INTERVAL_S
_CANARY_QUERY = "helm_canary=1"
# Bounded body size for canary responses. Carrier signatures can be long, but
# the 8-token canary envelope is tiny; a generous but explicit cap prevents
# unbounded allocation from a malicious or broken upstream while still
# accepting any realistic carrier.
CANARY_MAX_BODY = 256 * 1024  # 256 KiB
# THE ONLY STOP REASONS THAT MEAN "THIS ANSWER IS FINISHED", as an ALLOWLIST.
# It was first written as a deny-list (`!= "tool_use"`), borrowed from
# `_semantic_progress`, and was measured wrong: `pause_turn` and any
# stop reason this file has never heard of both passed it and certified an
# upstream on an UNFINISHED answer. A deny-list on an open vocabulary is a
# promise about strings nobody has written yet — and the neighbouring rule it
# was borrowed from answers a DIFFERENT question (did this session make
# progress), where an unknown assistant stop reason genuinely is progress.
# Here the question is whether ONE canary response completed, so anything not
# known to be terminal must read as not-yet-finished.
_TERMINAL_STOP_REASONS = ("end_turn", "max_tokens", "stop_sequence")
_REFUSAL_ORIGIN_HEADER = "X-CPA-Refusal-Origin"
_REFUSAL_LOCAL = "local"
_REFUSAL_PROVIDER = "provider"
_REFUSAL_UNKNOWN = "unknown"
_CLIENT_TIMEOUT = "CLIENT-TIMEOUT"
_CONTENT_FLAGGED = "CONTENT-FLAGGED/cyber-classifier"
_PROXY_COOLDOWN = "PROXY-COOLDOWN"
# A VENDOR QUOTA WALL IS NOT A BAD KEY. Measured: kimi answered the
# canary HTTP 403 "You've reached your weekly (7-day) usage limit" while its own
# usages endpoint showed the week spent with a resetTime, and the code-only rule
# (401/403 -> AUTH-401) printed a dead key, RED on reach, "unknown when". The
# repair for a quota wall is a WAIT for the vendor's reset, so it is named by
# the body and read on the money axis (burnflags), never on reach.
_QUOTA_WALL = "QUOTA-WALL"
# A 403 OUR PROXY MINTED ITSELF IS OURS, NOT THE VENDOR'S. When the proxy marks
# a refusal local (`X-CPA-Refusal-Origin: local`, no selected trace) no request
# reached the vendor, so quota wording in its body is a belief the proxy holds,
# not a vendor wall: reading it as QUOTA-WALL put the family on the money axis,
# "wait for the vendor reset", for a refusal no vendor ever made. It is not
# AUTH-401 either (untyped, so upstream RED on reach) and not PROXY-COOLDOWN
# (no cooldown was measured). The repair is ours: read the proxy's stated
# reason, fix it, restart and probe.
_PROXY_LOCAL_403 = "PROXY-LOCAL-403"
# The one state each code our proxy marks local reads as, whatever its body
# says. Origin is consulted BEFORE any body wording on both codes.
_LOCAL_REFUSAL = {403: _PROXY_LOCAL_403, 429: _PROXY_COOLDOWN}
_UPSTREAM_QUOTA = frozenset(("QUOTA-402", _QUOTA_WALL))
_UPSTREAM_DARK = frozenset(("UPSTREAM-OVERLOADED", "QUOTA-402", _QUOTA_WALL,
                            "AUTH-401", "AUTH-UNAVAILABLE", "RATE-LIMITED",
                            _PROXY_COOLDOWN, _PROXY_LOCAL_403,
                            "TIMEOUT-500", "UPSTREAM-4XX", "UPSTREAM-5XX",
                            "EMPTY200", "MALFORMED200"))
_UPSTREAM_AGGREGATE = frozenset(("FAMILY-MIXED",))

# WHOSE FAILURE IS IT. A dark state does not say where the failure happened,
# and three of them DO: a PROXY-COOLDOWN and a PROXY-LOCAL-403 are helm's own
# proxy refusing before any request leaves the box, and the 200-shaped states
# are helm's own validation rejecting a reply. Every other dark state has an
# UNTYPED origin, and naming one would be inventing it.
#
# THIS LIVES HERE BECAUSE THIS MODULE OWNS THE STATE NAMES, and it is the
# lower module: seat_usability imports proxywatch, never the other way. It had
# grown its own copy of this classification (two frozensets beside
# `_dark_reason`) and the two surfaces disagreed in the one case that matters —
# the per-seat surface says "HELM'S OWN PROXY is in cooldown; no request reached
# a provider" while the FAMILY-DARK row for the same state said "upstream
# PROXY-COOLDOWN". Both cannot be true, and the second is the sentence a reader
# consults to decide whether a provider is down.
DARK_OURS, DARK_OUR_VALIDATION, DARK_UNTYPED = "ours", "our-validation", "untyped"
_DARK_ORIGIN = {_PROXY_COOLDOWN: DARK_OURS,
                _PROXY_LOCAL_403: DARK_OURS,
                "EMPTY200": DARK_OUR_VALIDATION,
                "MALFORMED200": DARK_OUR_VALIDATION}


def quota_wall(record):
    """The vendor quota state one family record stands on -> the state name,
    or None. A PROXY-COOLDOWN that followed a quota wall carries it as
    ``quota_wall`` (`_compose_upstream_records`): the local cooldown is the
    mirror of the vendor's refusal, and its repair is the same wait."""
    if not isinstance(record, dict):
        return None
    state = record.get("state")
    if state in _UPSTREAM_QUOTA:
        return state
    held = record.get("quota_wall")
    return held if state in (_PROXY_COOLDOWN, "AUTH-UNAVAILABLE") \
        and held in _UPSTREAM_QUOTA else None


def dark_origin(state):
    """Who produced this dark state -> DARK_OURS / DARK_OUR_VALIDATION / DARK_UNTYPED.

    UNTYPED IS AN ANSWER, not a gap: most dark states record WHAT was observed
    and carry nothing about WHERE it originated, so a caller that wants to name
    an origin for them would be inventing it.
    """
    return _DARK_ORIGIN.get(state, DARK_UNTYPED)

_PROXY_RUNTIME_V = 3
_PROXY_RUNTIME_V2 = 2
_PROXY_RUNTIME_V2_FIELDS = {
    "v", "session", "agent_pid", "agent_starttime", "model",
    "local_base_url", "proxy_pid", "proxy_identity", "proxy_config",
    "config_sha256", "route", "observed_at", "canary",
}
_PROXY_RUNTIME_FIELDS = _PROXY_RUNTIME_V2_FIELDS | {"agent_harness"}
_PROXY_KEY_ROUTE_FIELDS = {"alias", "provider", "upstream_model", "base_url"}
_PROXY_AUTH_ROUTE_FIELDS = {"alias", "provider", "upstream_model"}
_PROXY_SAFE_TOPLEVEL = frozenset((
    "host", "port", "auth-dir", "api-keys", "debug",
    "usage-statistics-enabled", "redis-usage-queue-retention-seconds",
    "remote-management",
    "nonstream-keepalive-interval", "transient-error-cooldown-seconds",
    "streaming", "routing", "openai-compatibility",
))
_PROXY_CANARY_FIELDS = {"state", "status"}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PROXY_AUTH_INDEX = re.compile(r"[0-9a-f]{16}\Z")
_PROXY_TRACE_ID = re.compile(r"[0-9]{14}-([0-9a-f]{16})-[0-9a-f]{8}\Z")
# A shared JSON cache is telemetry, never canary provenance: every process must
# authenticate one current shape for itself before it can consume that cache as
# family authority. The memo is process-local and keyed by the complete measured
# shape; every read still remeasures that shape, so a pid/listener/config/route
# change misses immediately rather than surviving the memo window.
_PROXY_AUTH_CANARIES = {}
_PROXY_AUTH_CANARY_FRESH_S = INTERVAL_S


def _proof_text(value, limit=4096):
    return isinstance(value, str) and bool(value) and len(value) <= limit \
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _safe_url(value, local=False):
    """A credential-free HTTP URL, exact or None.

    Userinfo/query/fragment are refused because a persisted proof must never
    become a side channel for API keys. Local runtime URLs are stricter: the
    measured agent must point straight at one loopback listener, not at a path
    or remote host whose ownership this machine cannot prove.
    """
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.scheme not in (("http",) if local else ("http", "https")) \
            or not parsed.hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        return None
    if local and (parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                  or parsed.path not in ("", "/") or port is None):
        return None
    return parsed


def _proxy_route_error(route):
    """Structural route validity, independent of today's family catalogue."""
    if not isinstance(route, dict) or set(route) not in (
            _PROXY_KEY_ROUTE_FIELDS, _PROXY_AUTH_ROUTE_FIELDS):
        return "proxy route is malformed"
    if any(not _proof_text(route.get(key), 512) for key in set(route)):
        return "proxy route carries malformed values"
    if set(route) == _PROXY_KEY_ROUTE_FIELDS \
            and _safe_url(route["base_url"]) is None:
        return "proxy route carries malformed values"
    return None


def _proxy_route_family(route):
    """The one configured family matching a measured route, else UNKNOWN.

    The proof's storage key, roster label, and any recorded family string are
    absent from this comparison. API-key routes bind alias + provider + upstream
    model + endpoint. OAuth routes have no endpoint in the loaded config, so they
    bind the live request model to the homogeneous credential type the exact
    listener loaded. Zero and two configured matches both fail closed.
    """
    why = _proxy_route_error(route)
    if why:
        return None, why
    from . import seat as seatmod
    return seatmod.proxy_route_family(route)


def _proxy_proof_shape(proof):
    """Validated immutable proof fields, without mutable family resolution."""
    if not isinstance(proof, dict) or type(proof.get("v")) is not int:
        return None, "proxy runtime proof has the wrong schema or version"
    version, fields = proof["v"], set(proof)
    legacy = version == _PROXY_RUNTIME_V2 \
        and fields in (_PROXY_RUNTIME_V2_FIELDS, _PROXY_RUNTIME_FIELDS)
    if not legacy and (version != _PROXY_RUNTIME_V
                       or fields != _PROXY_RUNTIME_FIELDS):
        return None, "proxy runtime proof has the wrong schema or version"
    harness = proof.get("agent_harness")
    if "agent_harness" in proof and harness != "claude" \
            or not _proof_text(proof.get("session"), 256) \
            or any(type(proof.get(key)) is not int or proof[key] < 2
                   for key in ("agent_pid", "proxy_pid")) \
            or any(type(proof.get(key)) is not int or proof[key] < 1
                   for key in ("agent_starttime", "observed_at")):
        return None, "proxy runtime proof has malformed process/session identity"
    if not _proof_text(proof.get("model"), 256) \
            or _safe_url(proof.get("local_base_url"), local=True) is None \
            or not _proof_text(proof.get("proxy_identity"), 256) \
            or not _proof_text(proof.get("proxy_config")) \
            or not os.path.isabs(proof["proxy_config"]) \
            or not _HEX64.fullmatch(str(proof.get("config_sha256") or "")):
        return None, "proxy runtime proof has malformed measured runtime fields"
    canary = proof.get("canary")
    # Two attestations and no third: the upstream answered (HEALTHY/200), or
    # helm's own proxy refused for a cooling credential and named the loaded
    # route (PROXY-COOLDOWN/429). Availability differs; identity does not.
    if not isinstance(canary, dict) or set(canary) != _PROXY_CANARY_FIELDS \
            or (canary.get("state"), canary.get("status")) not in \
            (("HEALTHY", 200), (_PROXY_COOLDOWN, 429)):
        return None, "proxy runtime proof has no authenticated canary"
    route = proof.get("route")
    why = _proxy_route_error(route)
    if why:
        return None, why
    if route.get("alias") != proof.get("model"):
        return None, "proxy runtime proof model does not match its measured alias"
    return {"agent_harness": harness, "route": route}, None


def _proxy_proof_runtime(proof):
    """(runtime, error) derived wholly from one immutable proxy proof.

    v2 originally shipped without ``agent_harness``. A later writer added that
    field without bumping the version, so stale v2 readers rejected every new
    proof globally. Accept both historical v2 shapes, mint v3, and never repeat
    an additive exact-schema change under one version number.
    """
    shape, err = _proxy_proof_shape(proof)
    if err:
        return None, err
    family, err = _proxy_route_family(shape["route"])
    if err:
        return None, err
    runtime = {"family": family, "backend": "proxy"}
    # THE MODEL THE ROUTE ALREADY BOUND, carried instead of discarded. Both
    # route shapes carry `upstream_model` -- the docstring above says so for
    # each of them -- and until now this line collapsed the route to a FAMILY
    # and dropped the one fact that says WHICH MODEL ANSWERED. That is the
    # difference between a fact about SUPPLY (which families the proxy could
    # route to) and a fact about THE TURN, and only the second one can tell a
    # cross-family verdict from a seat that substituted while its own
    # credential was cooling.
    #
    # DERIVED FROM THE SAME ROUTE THE FAMILY IS, so the two can never describe
    # different requests. It is a RAW fact and never a tier: a missing model
    # is an ABSENCE OF EVIDENCE that no consumer may read as "same family".
    model = shape["route"].get("upstream_model")
    if isinstance(model, str) and model.strip():
        runtime["model"] = model.strip()
    if shape["agent_harness"] is not None:
        runtime["agent_harness"] = shape["agent_harness"]
    return runtime, None


def _proxy_proof_family(proof):
    """(family, error) derived from one sanitized immutable proxy proof."""
    runtime, err = _proxy_proof_runtime(proof)
    return (runtime.get("family"), None) if runtime else (None, err)


def resolved_from_proof(proof):
    """{model, provider, upstream_model} one proxy proof BINDS, or None.

    Exactly the three fields a verdict already records for its author
    (`dispatches._verdict_author_runtime_evidence` writes `resolved` with
    these names), so a seat surface reading a live proof and a review surface
    reading a stored verdict hand the SAME record to the SAME renderer. A
    proof this module cannot shape answers None: a surface that cannot read
    the route says so rather than falling back to the alias, which is the one
    answer that is always available and never means anything.
    """
    shape, err = _proxy_proof_shape(proof)
    if err:
        return None
    route = shape["route"]
    return {"model": proof.get("model"), "provider": route.get("provider"),
            "upstream_model": route.get("upstream_model")}


def resolved_model_phrase(resolved, family=None):
    """`alias -> provider/upstream (rung)`: WHICH MODEL ANSWERED. Never a guess.

    THE ALIAS IS NOT THE ANSWER. One claude-side alias may be served by
    several providers on several upstream ids -- that is what a pool family
    IS -- so an alias printed alone renders a seat identical on the rung it
    was named for and on any rung it fell back to. The provider and the
    upstream model come from the measured route and nothing else.

    THE RUNG IS RENDERED ONLY WHERE THE CATALOG DECLARES ONE. A family that
    says nothing about a provider's cost gets no cost word here: `free` is a
    claim about money and an undeclared rung is an absence of evidence, not a
    zero. An unreadable route renders UNRESOLVED beside the alias rather than
    silently collapsing to it.
    """
    if not isinstance(resolved, dict):
        return ""
    alias = str(resolved.get("model") or "").strip()
    provider = str(resolved.get("provider") or "").strip()
    upstream = str(resolved.get("upstream_model") or "").strip()
    if not provider or not upstream:
        return "%s -> UNRESOLVED" % (alias or "?")
    phrase = "%s/%s" % (provider, upstream)
    if alias and alias != upstream:
        phrase = "%s -> %s" % (alias, phrase)
    rung = None
    if family:
        # THROUGH THE FACADE, not into the impl module: helm.seat is the door
        # every consumer is meant to use, and reaching past it into
        # seat_catalog is the exact shape tests/test_seat_facade_injection.py
        # refuses. The facade re-exports provider_rung for this caller.
        from . import seat
        rung = seat.provider_rung(family, provider)
    return phrase + (" (%s)" % rung if rung else "")


def _sanitized_proxy_proof(proof):
    """Exact secret-free copy, or None. Extra fields are rejected, not scrubbed."""
    runtime, err = _proxy_proof_runtime(proof)
    if err or not runtime:
        return None
    out = {"v": proof["v"], "session": proof["session"],
           "agent_pid": proof["agent_pid"],
           "agent_starttime": proof["agent_starttime"],
           "model": proof["model"],
           "local_base_url": proof["local_base_url"],
           "proxy_pid": proof["proxy_pid"],
           "proxy_identity": proof["proxy_identity"],
           "proxy_config": proof["proxy_config"],
           "config_sha256": proof["config_sha256"],
           "route": dict(proof["route"]),
           "observed_at": proof["observed_at"],
           "canary": dict(proof["canary"])}
    if "agent_harness" in proof:
        out["agent_harness"] = proof["agent_harness"]
    return out


def _proxy_proof_comparison(proof):
    """Version-neutral value for compatible v2/v3 proof comparison."""
    shape, err = _proxy_proof_shape(proof)
    if err:
        return None
    out = dict(proof)
    out.pop("v", None)
    # The canary is AVAILABILITY, not authority: a proof minted healthy and
    # the same proof re-minted while the credential cools attest the same
    # session, pids, listener, config digest and route. Comparing the canary
    # made every wall read as "runtime changed" and refused the verdict.
    out.pop("canary", None)
    if shape["agent_harness"] is None:
        out["agent_harness"] = "claude"
    return out


def _proxy_proofs_equivalent(left, right, ignore_observed_at=False):
    """Version-neutral proof equality over the AUTHORITY fields.

    ``ignore_observed_at`` exists for exactly ONE caller: the roster-stamp
    comparison inside `proxy_runtime_snapshot`. The watcher writes its two
    copies of one proof at different instants — the state file first
    (atomically), then each seat's roster stamp — and both carry their own
    `observed_at` clock, so demanding byte-equality there manufactured a
    contradiction between two TRUTHFUL records for any reader landing in
    between. Every other caller compares copies minted in one motion and
    keeps the strict default: the canary reproof mints its candidate with
    the cached proof's own `observed_at`, so the clock can only differ there
    if something is actually wrong."""
    left = _proxy_proof_comparison(left)
    right = _proxy_proof_comparison(right)
    if left is None or right is None:
        return False
    if ignore_observed_at:
        left = {key: value for key, value in left.items()
                if key != "observed_at"}
        right = {key: value for key, value in right.items()
                 if key != "observed_at"}
    return left == right


def _proxy_proof_matches_live_base(proof, live_base, auth_routes=None):
    """Exact live base match, resolving only a deliberately deferred route.

    Multi-route aliases cannot know which credential CLIProxyAPI selected until
    a canary returns its auth index. A later authority read must not spend a new
    canary merely to reconstruct that choice: the cached proof already binds the
    successful selected route, while the fresh loaded config supplies every
    currently selectable candidate. Reproof therefore compares every other
    canonical field exactly and accepts the cached full route when at least one
    credential names it and every candidate resolves uniquely to that route's
    family. Multiple credentials may legitimately name the same route; one
    credential naming multiple routes remains a collision. Missing, mutated,
    cross-family, unknown, colliding, or malformed candidates stay UNKNOWN.
    Singular-route comparison is unchanged.
    """
    if not isinstance(live_base, dict) or type(live_base.get("v")) is not int:
        return False
    fields = set(live_base)
    v2 = _PROXY_RUNTIME_V2_FIELDS - {"observed_at", "canary"}
    current = _PROXY_RUNTIME_FIELDS - {"observed_at", "canary"}
    deferred = current - {"route"}
    version = live_base["v"]
    if version == _PROXY_RUNTIME_V2:
        if fields not in (v2, current):
            return False
    elif version != _PROXY_RUNTIME_V or fields not in (current, deferred):
        return False
    harness = live_base.get("agent_harness")
    if "agent_harness" in live_base and harness != "claude":
        return False
    captured = _proxy_proof_comparison(proof)
    if captured is None:
        return False
    measured = dict(live_base)
    measured.pop("v", None)
    measured.setdefault("agent_harness", "claude")
    if fields == deferred:
        route = captured.get("route")
        family, why = _proxy_route_family(route)
        if why or not family or not isinstance(auth_routes, dict) \
                or not auth_routes:
            return False
        found = False
        for index, candidates in auth_routes.items():
            if not isinstance(index, str) or not _PROXY_AUTH_INDEX.fullmatch(index) \
                    or not isinstance(candidates, tuple) \
                    or len(candidates) != 1:
                return False
            candidate = candidates[0]
            candidate_family, why = _proxy_route_family(candidate)
            if why or candidate_family != family:
                return False
            found |= candidate == route
        if not found:
            return False
    return all(captured.get(key) == value for key, value in measured.items())


def _roster_session(seat_name):
    """(session, canonical roster identity, error) for one exact current row."""
    from . import seats
    roster, failed = seats.roster_checked()
    if failed:
        return None, None, "roster is unreadable"
    canonical, err = seats._resolve_against(seat_name, roster)
    if err:
        return None, None, err
    matches = [(name, row) for name, row in roster.items()
               if seats.recipient_matches(name, canonical)]
    if len(matches) != 1 or not isinstance(matches[0][1], dict):
        return None, None, ("roster identity @%s matched %d rows" %
                            (canonical, len(matches)))
    identity, row = matches[0]
    session = row.get("session")
    if not _proof_text(session, 256):
        return _renamed_seat_session(canonical)
    return session, identity, None


def _renamed_seat_session(canonical):
    """(session, CURRENT identity, error) for a seat whose NAME went stale.

    A rename moves the roster row to the new name, and the old name is then
    re-created as a BARE STUB by the next hook join from a process whose
    environ still carries it; the bind-refused guard nulls that stub's session.
    The pass enumerates CATALOG names, so it asks about the stub, gets no
    session, and mints no proof — and the verdict door, reading a roster
    session that IS current, refuses every verdict the seat files. Measured
    live: one seat's stub row was literally {}, while another seat held
    the session AND its runtime stamp; four verdicts could not bind.

    THE NAME WAS ONLY AN INDEX. This repairs the index from helm's OWN durable
    record of what it launched into that instance, and nothing else: the
    session must still resolve to exactly ONE current roster identity, and
    every binding downstream (pid, start time, listener, config digest, route,
    canary, byte-equal stamp, live re-proof) is untouched. A seat with no
    spawn record, or whose record names a different seat, still gets the
    original refusal."""
    from . import seat as seatmod
    try:
        family, err = seatmod._seat_family(canonical)
        if err:
            raise ValueError(err)
        rec = seatmod._spawn_record(
            seatmod._instance_dir(family, canonical)) or {}
    except Exception:                     # noqa: BLE001 — a watch never raises
        rec = {}
    session = rec.get("session") if rec.get("seat") == canonical else None
    if not _proof_text(session, 256):
        return None, None, ("roster row @%s has no exact current session"
                            % canonical)
    identity, err = _roster_identity_for_session(session)
    if err:
        return None, None, err
    return session, identity, None


def _roster_identity_for_session(session):
    """The one current roster identity carrying `session`, else UNKNOWN."""
    from . import seats
    roster, failed = seats.roster_checked()
    if failed:
        return None, "roster is unreadable"
    matches = [name for name, row in roster.items()
               if isinstance(row, dict) and row.get("session") == session]
    if len(matches) != 1:
        return None, ("roster session %s matched %d current identities" %
                      (session, len(matches)))
    return matches[0], None


def _live_session_runtime(session):
    """Exact live pid + selected /proc facts for one roster session.

    Claude's pid-keyed session record supplies session -> process incarnation;
    /proc then supplies only the four values this proof needs. Full environments
    contain credentials and never leave this function.
    """
    from . import beacons, sessions
    try:
        homes = sessions.cred_homes()
    except Exception as ex:                    # noqa: BLE001 — proof stays UNKNOWN
        return None, "credential homes are unreadable: %s" % ex
    found = {}
    for root in homes:
        for path in glob.glob(os.path.join(root, "sessions", "*.json")):
            try:
                with pk.open_regular(path, encoding="utf-8") as f:
                    record = json.load(f)
                if record.get("sessionId") != session:
                    continue
                pid = int(record.get("pid") or 0)
                start = record.get("procStart")
            except (OSError, ValueError, TypeError):
                continue
            if pid < 2 or not start or not sessions._pid_is_claude(pid, start):
                continue
            env = beacons.proc_env(pid)
            if env is None:
                return None, "live session %s environment is unreadable" % session
            claimed_session = env.get("CLAUDE_CODE_SESSION_ID")
            if claimed_session and claimed_session != session:
                return None, ("live pid %d session environment contradicts the "
                              "roster session" % pid)
            actual_start = beacons.proc_starttime(pid)
            if actual_start is None or str(actual_start) != str(start):
                return None, "live session process incarnation changed during proof"
            found[pid] = (actual_start, env)
    if len(found) != 1:
        return None, ("roster session %s has %d exact live pids" %
                      (session, len(found)))
    pid, (start, env) = next(iter(found.items()))
    models = {str(env.get(key) or "").strip()
              for key in ("CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")
              if str(env.get(key) or "").strip()}
    if len(models) != 1:
        return None, "live session has %d distinct model values" % len(models)
    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
    token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if _safe_url(base_url, local=True) is None or not token:
        return None, "live session has no exact local base URL and bearer"
    return {"agent_harness": "claude", "pid": pid, "starttime": start,
            "model": next(iter(models)), "base_url": base_url.rstrip("/"),
            "token": token}, None


def _yaml_scalar(value):
    """The WATCHDOG side of the ONE reader (helm/yaml_scalar.py, task/2126):
    the parsing rules answer (value, err); a watchdog cannot refuse the
    config it watches, so absence reads as UNKNOWN — None — and never
    becomes drift. The fork boolean compares the typed value, never
    str().lower(), so a None or a quote-wrapped value cannot silently read
    as fork=False."""
    scalar, _err = yaml_scalar(value)
    return scalar


def _proxy_auth_provider(auth_dir):
    """One provider + its proxy auth indexes from the loaded auth-dir.

    OAuth records contain credentials, but authority needs only their validated
    ``type`` metadata and CLIProxyAPI's stable, secret-free auth index. The index
    is SHA-256(type + absolute clean path), never token/account material; the
    canary's X-CPA-TRACE-ID must name one of these exact records. Token/account
    values never leave this function. Mixed, aliased, symlinked, malformed,
    unreadable, or empty input stays UNKNOWN.
    """
    if not _proof_text(auth_dir) or not os.path.isabs(auth_dir):
        return None, None, "loaded config names no absolute auth-dir"
    clean = os.path.abspath(os.path.normpath(auth_dir))
    if clean != os.path.realpath(auth_dir):
        return None, None, "loaded auth-dir uses an unresolved symlink"
    paths = sorted(glob.glob(os.path.join(clean, "*.json")))
    if not paths:
        return None, None, "loaded auth-dir has no credential records"
    providers, indexes = [], []
    for path in paths:
        if os.path.realpath(path) != path:
            return None, None, "loaded auth record uses an unresolved symlink"
        try:
            with pk.open_regular(path, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError) as ex:
            return None, None, "loaded auth record is unreadable: %s" % ex
        provider = record.get("type") if isinstance(record, dict) else None
        if not _proof_text(provider, 128):
            return None, None, "loaded auth record has no safe provider type"
        if any(key in record for key in ("model_aliases", "model-aliases")):
            return None, None, "loaded auth record carries a per-auth model alias"
        seed = (provider.lower() + ":" + path).encode("utf-8")
        indexes.append(hashlib.sha256(seed).digest()[:8].hex())
        providers.append(provider)
    distinct = sorted(set(providers))
    if len(distinct) != 1:
        return None, None, "loaded auth-dir carries %d provider types" % len(distinct)
    return distinct[0], tuple(sorted(indexes)), None


def _alias_rows_that_rename_a_route(rows):
    """The oauth-model-alias rows a route proof cannot live beside, as text.

    The block is ACCEPTED ONLY IN THE SHAPE THE GENERATOR EMITS (task/1948):
    every row forks — the family's own id stays routable and the alias is
    ADDED beside it — every alias is a foreign id, one no catalogued family
    serves, so no route this observer could attest is renamed, and a row
    carries NOTHING ELSE. The last clause is what keeps the stamp honest:
    the fork's `force-mapping` rewrites the upstream RESPONSE model back to
    the alias (config.go: "ForceMapping rewrites upstream response model
    fields back to Alias"), and the response model is the evidence this
    observer correlates a stamp against — a config that can rewrite it is a
    config under which the stamp would certify a model the runtime did not
    run. Without it the alias is routing only and every response names the
    real model. A row without fork is a rename (measured: the family's own
    id 502s "unknown provider" behind it); a row whose alias is a catalogued
    model id would make that id's requests serve a different upstream than
    its route proof names. Each is refused with the row spelled out, never
    waved through on the block's presence alone.

    THE NAME MAY BE ANY MODEL THE CHANNEL'S OWN FAMILY CATALOGUES, and that is
    not a loosening — it is the shape a family with a `subagent_tiers` table
    emits (seat_catalog): an astra codex seat's block names gpt-6-astra on the
    opus row and gpt-5.6-sol on the sonnet row, both ids one codex OAuth
    serves on the codex channel, so every row still points at a route this
    observer could attest for THIS family. The `instance_models` table is the
    same fact one level out: a codex instance declared on gpt-5.6-sol emits
    sol on every row of its own block while its sibling emits astra, and both
    blocks are this channel's family's catalogue, so neither is foreign. A name belonging to ANOTHER
    family's catalogue is refused instead, and that is the tightening this
    door was missing: the name is the model the upstream actually serves, so a
    foreign name on this channel would let a response come back stamped with
    another family's model, and family proof reads a served model. MEASURED
    against the minted configs on this host before tightening: every alias
    block names its own family's model, so nothing live is newly refused.
    A name
    catalogued NOWHERE stays accepted, deliberately — an id the owner points
    at before helm catalogues it is UNKNOWN, and reddening it would be a
    watchdog calling its own catalog's staleness a config defect."""
    from . import seat as seatmod
    known = {m for fam in seatmod.FAMILIES.values()
             for m in (fam.get("model"),) + tuple(fam.get("probe_models") or ())
             if m}
    # Per OAuth channel, the models the family serving it catalogues; and the
    # union, which is what makes a foreign name MEASURED rather than merely
    # unrecognised.
    by_channel = {}
    for fam in seatmod.FAMILIES.values():
        channel = fam.get("auth_type")
        if channel:
            by_channel.setdefault(channel, set()).update(
                seatmod.family_catalogued_models(fam))
    catalogued_somewhere = {model for fam in seatmod.FAMILIES.values()
                            for model in seatmod.family_catalogued_models(fam)}

    def foreign_name(row):
        """True only when this channel's family is KNOWN and the row's name is
        a model some OTHER family catalogues."""
        own = by_channel.get(row.get("channel"))
        return bool(own) and row["name"] not in own \
            and row["name"] in catalogued_somewhere

    bad = []
    unknown = []
    for row in rows:
        where = "%s: %s -> %s" % (row.get("channel"), row.get("name"), row.get("alias"))
        if not row.get("name") or not row.get("alias"):
            bad.append("%s (malformed row)" % where)
        elif row.get("fork") is None:
            # UNKNOWN IS NOT NO-FORK: an absent or unreadable fork value is
            # not evidence the rename protection is off, and it is not the
            # rename defect either — it is a row the watchdog cannot read,
            # reported on its own channel below.
            unknown.append("%s (fork is UNKNOWN: the value is absent or "
                           "unreadable)" % where)
        elif not row.get("fork"):
            bad.append("%s (no fork: the original id stops routing)" % where)
        elif row.get("extra"):
            bad.append("%s (carries %s: a row may hold only name, alias and fork, "
                       "once each)" % (where, ", ".join(row["extra"])))
        elif row["alias"] in known:
            bad.append("%s (the alias is a catalogued route id)" % where)
        elif foreign_name(row):
            bad.append("%s (the name is another family's catalogued model; this "
                       "channel serves %s)"
                       % (where, ", ".join(sorted(by_channel[row["channel"]]))))
    return bad, unknown


def _proxy_config_route(path, alias):
    """(singular route, bearer, auth-index routes, error) for loaded config.

    A multi-provider alias has no route until the authenticated canary's trace
    names the credential CLIProxyAPI actually selected. The returned map is
    secret-free: stable credential index -> candidate exact routes.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as ex:
        return None, None, None, "proxy config is unreadable: %s" % ex
    tokens, routes, auth_dirs, top_keys = [], [], [], []
    in_api = in_compat = in_models = in_alias = False
    alias_rows, alias_channel, alias_row = [], None, None
    compatibility_blocks = 0
    provider = base_url = upstream = None
    api_keys = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            top_keys.append(stripped.partition(":")[0])
            if stripped.startswith("auth-dir:"):
                auth_dirs.append(_yaml_scalar(stripped.partition(":")[2]))
            in_api = stripped == "api-keys:"
            in_compat = stripped == "openai-compatibility:"
            in_alias = stripped == "oauth-model-alias:"
            in_models = False
            alias_channel = alias_row = None
            if in_compat:
                compatibility_blocks += 1
            continue
        if in_api and indent == 2 and stripped.startswith("- "):
            token = _yaml_scalar(stripped[2:])
            if token:
                tokens.append(token)
            continue
        if in_alias:
            if indent == 2 and stripped.endswith(":"):
                alias_channel, alias_row = stripped[:-1], None
            elif indent == 4 and stripped.startswith("- name:"):
                alias_row = {"channel": alias_channel,
                             "name": _yaml_scalar(stripped.partition(":")[2]),
                             "alias": None, "fork": False}
                alias_rows.append(alias_row)
            elif alias_row is not None and indent == 6:
                key, _sep, value = stripped.partition(":")
                seen = alias_row.setdefault("seen", [])
                if key in seen:
                    # a repeated key: the proxy honours whichever wins in ITS
                    # parser, this reader must not guess — refuse the row
                    alias_row.setdefault("extra", []).append("%s (repeated)" % key)
                seen.append(key)
                if key == "alias":
                    alias_row["alias"] = _yaml_scalar(value)
                elif key == "fork":
                    # FORK IS A PRODUCER-TYPED BOOL, not a scalar. The row
                    # default (omitted key) and bare null/blank are the
                    # producer's known-false zero value. The valid spellings
                    # are the producer's exact case variants — bare
                    # true/false in YAML's three cases, and the typed-bool
                    # compatibility spellings y/yes/on and n/no/off in
                    # theirs; yaml.v3 accepts those QUOTED too, so a quoted
                    # compatibility string keeps its meaning while a quoted
                    # true/false is a string the producer rejects, as is
                    # garbage or a numeric. Everything unreadable or
                    # unsupported is UNKNOWN, never read as no-fork.
                    fork, ferr, quoted = yaml_scalar_typed(value)
                    # A BLANK value is not a parse failure: `fork:` with
                    # nothing after it is the producer's null, which loads
                    # into a fresh Go bool as false — the same zero value as
                    # omission. The shared reader reports it as an empty
                    # scalar because that answer is right for STRINGS; the
                    # boolean question predates it.
                    if ferr == "empty scalar":
                        ferr, fork, quoted = None, None, False
                    if ferr is not None:
                        alias_row["fork"] = None
                    elif quoted:
                        if fork in ("y", "Y", "yes", "Yes", "YES",
                                    "on", "On", "ON"):
                            alias_row["fork"] = True
                        elif fork in ("n", "N", "no", "No", "NO",
                                      "off", "Off", "OFF"):
                            alias_row["fork"] = False
                        else:
                            alias_row["fork"] = None
                    elif fork is None or fork in ("null", "Null", "NULL",
                                                  "~"):
                        alias_row["fork"] = False
                    elif fork in ("true", "True", "TRUE",
                                  "y", "Y", "yes", "Yes", "YES",
                                  "on", "On", "ON"):
                        alias_row["fork"] = True
                    elif fork in ("false", "False", "FALSE",
                                  "n", "N", "no", "No", "NO",
                                  "off", "Off", "OFF"):
                        alias_row["fork"] = False
                    else:
                        alias_row["fork"] = None
                else:
                    alias_row.setdefault("extra", []).append(key)
            continue
        if not in_compat:
            continue
        if indent == 2 and stripped.startswith("- name:"):
            provider = _yaml_scalar(stripped.partition(":")[2])
            base_url = upstream = None
            api_keys = []
            in_models = False
        elif indent == 4 and stripped.startswith("base-url:"):
            base_url = _yaml_scalar(stripped.partition(":")[2])
        elif not in_models and indent == 6 \
                and stripped.startswith("- api-key:"):
            api_key = _yaml_scalar(stripped.partition(":")[2])
            if api_key:
                api_keys.append(api_key)
        elif indent == 4 and stripped == "models:":
            in_models = True
        elif in_models and indent == 6 and stripped.startswith("- name:"):
            upstream = _yaml_scalar(stripped.partition(":")[2])
        elif in_models and indent == 8 and stripped.startswith("alias:"):
            route_alias = _yaml_scalar(stripped.partition(":")[2])
            if all((provider, base_url, upstream, route_alias)):
                route = {"alias": route_alias, "provider": provider,
                         "upstream_model": upstream,
                         "base_url": base_url.rstrip("/")}
                indexes = tuple(hashlib.sha256(
                    ("openai-compatibility:%s+%s" % (base_url, api_key))
                    .encode("utf-8")).digest()[:8].hex()
                                for api_key in api_keys)
                routes.append((route, indexes))
    unknown = sorted(set(top_keys) - _PROXY_SAFE_TOPLEVEL - {"oauth-model-alias"})
    if unknown:
        return None, None, None, ("loaded config has unsupported top-level routing "
                                  "fields: %s" % ", ".join(unknown))
    renamed, unknown_rows = _alias_rows_that_rename_a_route(alias_rows)
    # BOTH categories keep their evidence in a mixed population: a measured
    # rename and an unreadable sibling are different defects, and returning
    # the first alone would discard the second. The UNKNOWN never travels
    # inside the rename sentence — it gets its own clause.
    parts = []
    if renamed:
        parts.append("loaded config's oauth-model-alias renames a "
                     "catalogued route: %s" % "; ".join(renamed))
    if unknown_rows:
        parts.append("loaded config has an unreadable "
                     "oauth-model-alias row: %s" % "; ".join(unknown_rows))
    if parts:
        return None, None, None, "; and ".join(parts)
    if len(tokens) != 1:
        return None, None, None, \
            "loaded config has %d inbound bearers" % len(tokens)
    auth_dir = auth_dirs[0] if len(auth_dirs) == 1 else None
    matches = [(route, indexes) for route, indexes in routes
               if route.get("alias") == alias]
    auth_routes = {}
    if compatibility_blocks == 1 and not auth_dirs and matches:
        for candidate, indexes in matches:
            for index in indexes:
                auth_routes.setdefault(index, []).append(candidate)
        if not auth_routes:
            return None, None, None, \
                "loaded config routes carry no selectable credential indexes"
        route = matches[0][0] if len(matches) == 1 else None
    elif compatibility_blocks == 0 and auth_dir and not routes:
        provider, auth_indexes, err = _proxy_auth_provider(auth_dir)
        if err:
            return None, None, None, err
        route = {"alias": alias, "provider": provider,
                 "upstream_model": alias}
        auth_routes = {index: [route] for index in auth_indexes}
    else:
        return None, None, None, ("loaded config has %d compatibility blocks, "
                                  "%d auth-dir declarations, and %d routes for "
                                  "alias %s" % (compatibility_blocks,
                                                len(auth_dirs), len(matches), alias))
    if route is not None and _proxy_route_family(route)[1]:
        return None, None, None, "loaded config route is unknown or ambiguous"
    return route, tokens[0], {index: tuple(candidates)
                             for index, candidates in auth_routes.items()}, None


def _loaded_proxy_config(listener):
    """(real config path, loaded digest, error) for the exact listener pid."""
    from . import seat as seatmod
    config = listener.get("config") if isinstance(listener, dict) else None
    if not config or not os.path.isabs(config):
        return None, None, "proxy listener names no absolute config"
    config = os.path.realpath(config)
    try:
        with open(os.path.join(os.path.dirname(config), "proxy.pid"),
                  encoding="utf-8") as f:
            parts = f.read().split()
        pid = int(parts[0])
        identity = parts[1]
        launch = seatmod._decode_launch_inputs(parts[2])
    except (OSError, ValueError, IndexError):
        return None, None, "proxy listener has no readable launch record"
    if pid != listener.get("pid") or not listener.get("identity") \
            or identity != listener.get("identity") or not launch:
        return None, None, "proxy listener does not match its launch record"
    try:
        digest = seatmod._config_digest(config)
    except OSError as ex:
        return None, None, "proxy config digest is unreadable: %s" % ex
    if launch.get("config_sha256") != digest:
        return None, None, "proxy config changed after the listener loaded it"
    return config, digest, None


def _proxy_runtime_shape(seat_name):
    """Measured request inputs + secret-free proof base for one roster session."""
    from . import seat as seatmod
    session, _identity, err = _roster_session(seat_name)
    if err:
        return None, err
    runtime, err = _live_session_runtime(session)
    if err:
        return None, err
    parsed = _safe_url(runtime["base_url"], local=True)
    port = parsed.port
    listeners = seatmod._port_listeners(port, exact=True)
    if len(listeners) != 1:
        return None, "local proxy port %d has %d exact readable listeners" % (
            port, len(listeners))
    listener = listeners[0]
    config, digest, err = _loaded_proxy_config(listener)
    if err:
        return None, err
    values = seatmod._config_values(config)
    if not isinstance(values, dict) or values.get("port") != str(port):
        return None, "loaded proxy config does not declare the live listener port"
    route, token, auth_routes, err = _proxy_config_route(
        config, runtime["model"])
    if err:
        return None, err
    if token != runtime["token"]:
        return None, "live session bearer does not match the loaded proxy config"
    proof = {"v": _PROXY_RUNTIME_V, "session": session,
             "agent_harness": runtime["agent_harness"],
             "agent_pid": runtime["pid"],
             "agent_starttime": runtime["starttime"],
             "model": runtime["model"],
             "local_base_url": runtime["base_url"],
             "proxy_pid": listener["pid"],
             "proxy_identity": listener["identity"],
             "proxy_config": config, "config_sha256": digest}
    if route is not None:
        proof["route"] = route
    return {"url": runtime["base_url"], "token": token,
            "model": runtime["model"], "auth_routes": auth_routes,
            "proof": proof}, None


def _canary_once(base_url, token, model, timeout=UPSTREAM_TIMEOUT_S):
    """One authenticated request -> state/detail/ms/status/model/trace-id."""
    import urllib.error
    import urllib.request
    body = json.dumps({"model": model, "max_tokens": UPSTREAM_TOKENS,
                       "messages": [{"role": "user",
                                     "content": "Reply with exactly OK"}]}) \
        .encode("utf-8")
    req = urllib.request.Request(
        "%s/v1/messages?beta=true&%s" % (base_url.rstrip("/"), _CANARY_QUERY),
        data=body,
        headers={"Content-Type": "application/json",
                 "Anthropic-Version": "2023-06-01",
                 "Authorization": "Bearer " + token})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(CANARY_MAX_BODY + 1)
            trace = getattr(response, "headers", {}).get("X-CPA-TRACE-ID")
            ms = int((time.time() - t0) * 1000)
            if len(payload) > CANARY_MAX_BODY:
                return ("OVERSIZE", "HTTP %d body exceeded canary cap of %d bytes"
                        % (response.status, CANARY_MAX_BODY), ms,
                        response.status, None, trace)
            if not payload.strip():
                return ("EMPTY200", "HTTP %d with an empty body" % response.status,
                        ms, response.status, None, trace)
            success = _valid_canary_payload(payload)
            try:
                response_model = json.loads(payload.decode("utf-8")).get("model")
            except (AttributeError, UnicodeDecodeError, ValueError):
                response_model = None
            if not success:
                return ("MALFORMED200",
                        "HTTP %d whose envelope this canary cannot parse — %s"
                        % (response.status, _body_text(payload)), ms,
                        response.status, response_model, trace)
            return ("HEALTHY", "HTTP %d %s" % (response.status, success), ms,
                    response.status, response_model, trace)
    except urllib.error.HTTPError as ex:
        try:
            payload = ex.read(CANARY_MAX_BODY + 1)
            headers = getattr(ex, "headers", None) or {}
            trace = headers.get("X-CPA-TRACE-ID")
            origin = headers.get(_REFUSAL_ORIGIN_HEADER)
        finally:
            ex.close()
        ms = int((time.time() - t0) * 1000)
        if len(payload) > CANARY_MAX_BODY:
            # Explicit oversize on error body; still surface the code but note cap.
            text = _body_text(payload[:CANARY_MAX_BODY])
            return ("OVERSIZE", "HTTP %d body exceeded canary cap of %d bytes — %s"
                    % (ex.code, CANARY_MAX_BODY, text), ms, ex.code, None, trace)
        text = _body_text(payload)
        selected = bool(_PROXY_TRACE_ID.fullmatch(str(trace or "")))
        if origin == _REFUSAL_LOCAL:
            origin = _REFUSAL_UNKNOWN if selected else _REFUSAL_LOCAL
        else:
            origin = _REFUSAL_PROVIDER if selected else _REFUSAL_UNKNOWN
        # The classifier reads the WHOLE bounded body; only the detail is cut.
        # Cutting first broke the JSON, so a vendor code written after a long
        # message never reached `_vendor_words`. The full text is not kept.
        whole = _body_text(payload, limit=None)
        state = _upstream_state(ex.code, whole, origin=origin)
        detail = "HTTP %d%s" % (ex.code, " — " + text if text else "")
        # The reset is read from that same whole body: a reset written after
        # the cut was lost with it. Only the instant is kept, ahead of any
        # Retry-After, so the body's own reset still wins (`_quota_reset_ms`).
        reset = _quota_reset_ms(whole, t0) \
            if state in _UPSTREAM_QUOTA else None
        if reset and reset != _quota_reset_ms(text, t0):
            detail += "; body reset %s" % _iso(reset / 1000.0)
        retry = str(headers.get("Retry-After") or "").strip()
        if state in _UPSTREAM_QUOTA and retry.isdigit():
            detail += "; Retry-After %ss: resets %s" % (
                retry, _iso(t0 + int(retry)))
        return state, detail, ms, ex.code, None, trace
    except Exception as ex:                  # noqa: BLE001 — a watch never raises
        ms = int((time.time() - t0) * 1000)
        reason = getattr(ex, "reason", None)
        if isinstance(ex, TimeoutError) or isinstance(reason, TimeoutError) \
                or ms >= timeout * 1000 - 100:
            return (_CLIENT_TIMEOUT, "no upstream completion in %ds" % timeout,
                    ms, None, None, None)
        return "UNKNOWN", "%s" % ex, ms, None, None, None


# A WALL IS NOT AN IDENTITY CHANGE. When every credential behind an alias is
# cooling, CLIProxyAPI answers the canary itself, before any request leaves the
# box, with a 429 whose body names the alias and the provider block it could
# not serve ("no available credential for ds4-pro via provider
# openai-compatible-opencode-go: 1 cooling down ..."). That sentence is
# produced by the listener whose pid, identity and loaded config digest the
# shape already binds, and it names a route that config carries: everything a
# family proof attests except the upstream answering is measured. Refusing to
# mint here erased the proof of every walled seat once per pass, and the
# ledger then refused verdicts from seats whose family had not changed at all.
_COOLDOWN_NAMES_ROUTE = re.compile(
    r"no available credential for (\S+) via provider "
    r"(?:openai-compatible-)?([A-Za-z0-9._-]+)")


def _cooldown_names_one_loaded_route(detail, shape):
    """(route, error): the ONE loaded route a proxy cooldown refusal names.

    Fails closed on every ambiguity: no alias/provider in the sentence, an
    alias other than the measured one, a provider matching zero or several
    loaded candidates, or candidates that span more than one family (a
    two-family config is the substitution channel this proof exists to close,
    and a wall must not open it)."""
    match = _COOLDOWN_NAMES_ROUTE.search(str(detail or ""))
    if not match:
        return None, "cooldown refusal names no alias and provider"
    alias, block = match.group(1), match.group(2)
    if alias != shape.get("model"):
        return None, ("cooldown refusal names alias %r, the measured alias is %r"
                      % (alias, shape.get("model")))
    candidates = [route for routes in (shape.get("auth_routes") or {}).values()
                  if isinstance(routes, tuple)
                  for route in routes if isinstance(route, dict)]
    if not candidates:
        return None, "loaded proxy config carries no candidate route for the alias"
    families = set()
    for route in candidates:
        family, why = _proxy_route_family(route)
        if why or not family:
            return None, why or "candidate route family is unknown"
        families.add(family)
    if len(families) != 1:
        return None, "candidate routes span %d families" % len(families)
    named = [route for route in candidates if route.get("provider") == block]
    if len(named) != 1:
        return None, ("cooldown refusal names provider %r, which matches %d "
                      "loaded routes" % (block, len(named)))
    return named[0], None


def proxy_runtime_canary(seat_name, observed_at=None):
    """(sanitized proof, error) after one stable exact route completes."""
    shape, err = _proxy_runtime_shape(seat_name)
    if err:
        return None, err
    state, detail, _ms, status, response_model, trace = _canary_once(
        shape["url"], shape["token"], shape["model"])
    if state == _PROXY_COOLDOWN and status == 429:
        # walled, and attested by the proxy's own refusal: identity holds,
        # availability does not, and the proof says exactly that. A
        # PROXY-LOCAL-403 names no loaded route, so it attests nothing and
        # falls to the refusal below.
        route, err = _cooldown_names_one_loaded_route(detail, shape)
        if err:
            return None, "walled canary cannot attest authority: %s" % err
        confirmed, err = _proxy_runtime_shape(seat_name)
        if err or confirmed != shape:
            return None, ("measured proxy runtime changed across its canary%s" %
                          (" — " + err if err else ""))
        proof = dict(shape["proof"], route=route)
        proof["observed_at"] = int(time.time() if observed_at is None else observed_at)
        proof["canary"] = {"state": _PROXY_COOLDOWN, "status": status}
        sanitized = _sanitized_proxy_proof(proof)
        return (sanitized, None) if sanitized else (
            None, "walled measured route produced a malformed proof")
    if state != "HEALTHY" or status != 200:
        return None, "authenticated measured-route canary %s — %s" % (state, detail)
    match = _PROXY_TRACE_ID.fullmatch(str(trace or ""))
    candidates = shape.get("auth_routes", {}).get(
        match.group(1), ()) if match else ()
    if len(candidates) != 1:
        return None, "canary trace does not name one loaded provider route"
    route = candidates[0]
    family, err = _proxy_route_family(route)
    if err or not family:
        return None, err or "selected route family is unknown"
    from . import seat as seatmod
    expected_model, err = seatmod.proxy_route_response_model(family, route)
    if err:
        return None, err
    if response_model != expected_model:
        return None, ("canary response model does not match the selected route "
                      "projection (got %r, expected %r)" %
                      (response_model, expected_model))
    confirmed, err = _proxy_runtime_shape(seat_name)
    if err or confirmed != shape:
        return None, ("measured proxy runtime changed across its canary%s" %
                      (" — " + err if err else ""))
    proof = dict(shape["proof"], route=route)
    proof["observed_at"] = int(time.time() if observed_at is None else observed_at)
    proof["canary"] = {"state": state, "status": status}
    sanitized = _sanitized_proxy_proof(proof)
    return (sanitized, None) if sanitized else (
        None, "measured route produced a malformed proof")


def proxy_runtime_proofs(rows, observed_at=None):
    """Current successful session-bound proofs, keyed only for storage lookup."""
    out = {}
    for row in rows:
        if row.get("error") or row.get("probe") != "healthy":
            continue
        proof, _err = proxy_runtime_canary(
            row.get("seat"), observed_at=observed_at)
        if proof:
            out[str(row.get("seat"))] = proof
    return out


def _stamp_proxy_runtime_proofs(proofs):
    """Promote only same-pass unique authenticated proofs into the roster."""
    from . import seats
    grouped = {}
    for proof in proofs.values():
        grouped.setdefault(proof.get("session"), []).append(proof)
    errors = []
    for session, candidates in grouped.items():
        if not session or len(candidates) != 1:
            if session:
                errors.append("session %s has %d measured proofs" %
                              (session, len(candidates)))
            continue
        proof = candidates[0]
        runtime, err = _proxy_proof_runtime(proof)
        if err:
            errors.append(err)
            continue
        try:
            _entry, err = seats.stamp_proxy_runtime(session, runtime, proof)
        except Exception as exc:              # noqa: BLE001 — watcher stays alive
            err = "roster runtime stamp failed: %s" % exc
        if err:
            errors.append(err)
    return errors


def _generating_at_cap(body, has_substantive_text, thinking_blocks, typed):
    """The one empty-looking envelope that is a GENERATING model, else None.

    Through an openai-compatible upstream the translator emits no thinking
    block, so a reasoning model that spent the cap thinking arrives as
    content [] (or empty text blocks) + stop_reason max_tokens + a usage
    line whose output_tokens counts the thinking. An exhausted or dead
    credential never produces that pair: it carries no output_tokens and no
    max_tokens stop. The tell is output_tokens plus stop_reason, never the
    presence of content (task/2664; measured on deepseek-v4.1-flash and on
    dots-3-note-preview, which darkened two families at the eight-token cap).
    """
    usage = body.get("usage")
    out_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    if body.get("stop_reason") == "max_tokens" and not has_substantive_text \
            and not thinking_blocks and typed \
            and type(out_tokens) is int and out_tokens > 0:
        return "generating at the eight-token cap (reasoning spent the budget)"
    return None


def _valid_canary_payload(payload):
    """Success detail for a real assistant message envelope, else None."""
    try:
        body = json.loads(payload.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or "error" in body \
            or body.get("type") != "message" \
            or body.get("role") != "assistant":
        return None
    blocks = body.get("content")
    if not isinstance(blocks, list) \
            or any(not isinstance(block, dict) for block in blocks):
        return None
    if not blocks:
        # content [] is the openai-compat translator's rendering of a
        # reasoning model that spent the cap thinking; only that exact pair
        # (max_tokens stop + output_tokens > 0) is admitted, see below.
        return _generating_at_cap(body, has_substantive_text=False,
                                  thinking_blocks=(), typed=True)
    text = [block.get("text") for block in blocks
            if block.get("type") == "text"]
    thinking_blocks = [block for block in blocks
                       if block.get("type") == "thinking"]
    typed = len(text) + len(thinking_blocks) == len(blocks)
    has_substantive_text = any(isinstance(t, str) and t.strip() for t in text)
    # A thinking block is valid if it has non-empty text OR a *non-empty string*
    # carrier signature. Plain truthy non-string values (or empty string) do not
    # count as a carrier; a block with neither substantive text nor a proper
    # signature is still malformed.
    def _thinking_block_valid(b):
        t = b.get("thinking")
        if isinstance(t, str) and t.strip():
            return True
        sig = b.get("signature")
        if isinstance(sig, str) and sig.strip():
            return True
        return False
    valid_thinking = all(_thinking_block_valid(b) for b in thinking_blocks) \
        if thinking_blocks else True
    # "OK" WITH WHITESPACE AROUND IT IS STILL OK. Models answer the probe
    # with " OK" or "OK\n" routinely (deepseek-v4-flash measured " OK"), and
    # an exact list match read every one of those as MALFORMED200, which
    # darkened the family and paused its beacon on a healthy upstream. Empty
    # text blocks are ignored the same way the carrier shape below ignores them.
    spoken = [t.strip() for t in text if isinstance(t, str) and t.strip()]
    # NO stop_reason TERM HERE, ON PURPOSE. A spoken "OK" is itself the
    # completion evidence; the carrier branch below needs `complete` only
    # because a thinking-only envelope carries no other sign the turn ended.
    # The openai-compat translator leaves stop_reason null when the upstream
    # omits finish_reason and can emit tool_use with zero tool blocks
    # (CLIProxyAPI openai_claude_response.go:32, :422, :472); both are routes
    # that just answered OK, and refusing them would darken a family the way
    # the whitespace refusal did. Trunk never gated this branch on stop_reason
    # either (measured across nine values, identical on both sides).
    if spoken == ["OK"] and typed and valid_thinking:
        return "with OK"
    # A REASONING MODEL THAT SPENT THE CAP THINKING IS GENERATING, NOT BROKEN.
    # Through an openai-compatible upstream the translator emits NO thinking
    # block, so the envelope is content [] with stop_reason max_tokens and a
    # usage line whose output_tokens counts the thinking. An exhausted or dead
    # credential never produces that pair: it carries no output_tokens and no
    # max_tokens stop. The tell is output_tokens plus stop_reason, never the
    # presence of content (task/2664; measured on deepseek-v4.1-flash and on
    # dots-3-note-preview, which darkened two families at the eight-token cap).
    generating = _generating_at_cap(body, has_substantive_text,
                                    thinking_blocks, typed)
    if generating:
        return generating
    if body.get("stop_reason") == "max_tokens" and not has_substantive_text \
            and thinking_blocks and typed and valid_thinking:
        return "with valid thinking-only message at the eight-token cap"
    # Exact live carrier completion shape for the canary:
    # - no *substantive* text content (empty-string text blocks are ignored)
    # - at least one thinking block carries a non-empty string "signature"
    # - the block is fully typed and the thinking part is valid per the rule above
    # This accepts the observed gemini (and similar) carrier responses that
    # return an empty thinking text + signature blob (optionally accompanied by
    # an empty text block) as a complete answer. It does NOT accept plain
    # thinking-only envelopes that lack a signature (those must still go through
    # the max_tokens path or be rejected).
    # COMPLETION SEMANTICS, NOT JUST SHAPE (FIX finding 3). Without a
    # stop_reason term this branch called ANY thinking-only envelope HEALTHY —
    # including one with the field missing, and one whose turn is not finished
    # at all. The rule is this file's OWN, derived rather than invented: a
    # stop_reason must be PRESENT and must not be ``tool_use``, exactly as
    # ``_semantic_progress`` decides at line ~513, where tool_use "leaves the
    # enclosing turn open" and any other assistant stop reason does not.
    #
    # MEASURED 2026-09-09 against the LIVE gemini proxy, which is why this
    # tightening is safe: the real carrier answers stop_reason 'end_turn' with
    # content ['thinking', 'text'] where the text block is "OK" — so it is
    # accepted by the FIRST branch above and never reaches this one. This path
    # is defensive coverage for a thinking-only carrier, and narrowing it costs
    # nothing that production has been observed to send.
    stop = body.get("stop_reason")
    complete = stop in _TERMINAL_STOP_REASONS
    if (not has_substantive_text and thinking_blocks and typed
            and valid_thinking and complete
            and any(isinstance(b.get("signature"), str) and b.get("signature").strip()
                    for b in thinking_blocks)):
        return "with valid thinking (carrier or opaque)"
    return None


_BODY_SHOWN = 400


def _body_text(payload, limit=_BODY_SHOWN):
    """A refusal body as one line -> str, cut to `limit` characters for
    display; `limit=None` keeps it whole for the classifier."""
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) \
        else str(payload or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if limit is None else text[:limit]


# Named states are declared beside `_UPSTREAM_DARK`: the cause classifier and
# every downstream reader share one identity rather than duplicating strings.

# Bounded body signatures observed at provider boundaries. They extend the one
# canonical Kimi classifier below; no second status classifier and no response
# body is persisted. The value names only the reset grammar.
REFUSAL_SIGNATURES = {
    "xai": (("usage balance exhausted", "duration"),),
    "opencode-go": (("gousagelimiterror", "duration"),),
    "openrouter": (("free-models-per-day", "daily-rollover"),),
    "deepseek": (("insufficient balance", "none"),),
    "antigravity": (("individual quota reached", "duration"),
                    ("quota_exhausted", "timestamp")),
}
QUOTA_WALL_INHERIT_S = 30 * 60
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.I)
_RESET_TIMESTAMP = re.compile(r"quotaResetTimeStamp[^0-9]*(\d{10,13})", re.I)
# The wording that names a vendor QUOTA, the wording that names a per-minute
# RATE limit, and the wording that names a CREDENTIAL failure. "rate limit"
# keeps its space on purpose: the proxy's own envelope types every 429
# `rate_limit_error`, and matching that would read every quota 429 as a rate.
_QUOTA_WORDS = re.compile(
    r"usage limit|(?<![a-z])quota|insufficient (?:balance|credit)"
    r"|out of credits"
    r"|(?:daily|weekly|monthly|7-day|5-hour) limit|limit (?:reached|exceeded)")
_RATE_WORDS = re.compile(r"rate limit|per minute|too many requests")
# `\binvalid\b` and not bare "invalid": a proxy envelope may type the error
# `invalid_request_error`, which says nothing about a credential.
_AUTH_WORDS = re.compile(r"\binvalid\b|\bexpired\b|unauthori[sz]ed|revoked")
_ISO_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")


# CLIProxyAPI's OWN envelope for a 403 whose upstream text was NOT JSON
# (`BuildErrorResponseBody`, sdk/api/handlers/handlers.go) is exactly
# {"error":{"message":<text>,"type":"permission_error",
# "code":"insufficient_quota"}}: the code is the proxy's per-status stamp,
# whatever the upstream said, so only the wrapped message may speak.
_PROXY_403_STAMP = {"type": "permission_error", "code": "insufficient_quota"}


def _vendor_words(low):
    """The part of a lower-cased refusal body the vendor wrote -> str."""
    try:
        body = json.loads(low)
    except (ValueError, RecursionError):
        return low
    e = body.get("error") if isinstance(body, dict) and len(body) == 1 \
        else None
    if isinstance(e, dict) and set(e) == {"message", "type", "code"} \
            and isinstance(e["message"], str) \
            and all(e[k] == v for k, v in _PROXY_403_STAMP.items()):
        return e["message"]
    return low


def _signature_match(low):
    words = _vendor_words(low)
    for name, signatures in REFUSAL_SIGNATURES.items():
        for needle, mode in signatures:
            if needle in words:
                return name, mode
    return None


def _signature_mode(low):
    match = _signature_match(low)
    return match[1] if match else None


def _refusal_provenance(state, detail):
    """A bounded producer label for persisted quota evidence; never the body."""
    if state == _QUOTA_WALL and str(detail or "").startswith("HTTP 402"):
        return "http-status:402"
    match = _signature_match(str(detail or "").lower())
    return "body-signature:%s" % match[0] if match else "body-signature:quota"


def _vendor_quota(low):
    """Does this lower-cased refusal body name a vendor quota wall -> bool."""
    words = _vendor_words(low)
    # A provider-specific signature is more precise than generic envelope prose:
    # OpenRouter's real daily wall begins "Rate limit exceeded" and then names
    # free-models-per-day. The named window wins; an ordinary rate limit does not.
    if _signature_match(words):
        return not _AUTH_WORDS.search(words)
    return bool(_QUOTA_WORDS.search(words)) and not _RATE_WORDS.search(words) \
        and not _AUTH_WORDS.search(words)


def _quota_reset_ms(detail, now):
    """The first future vendor reset in canonical epoch-ms form, or None."""
    text = str(detail or "")
    for found in _ISO_INSTANT.findall(text):
        at = _parse_timestamp(found)
        if at is not None and at > now:
            return int(at * 1000)
    low = text.lower()
    mode = _signature_mode(low) or (
        "duration" if low.startswith("http 402") else None)
    if mode == "timestamp":
        found = _RESET_TIMESTAMP.search(low)
        if found:
            raw = int(found.group(1))
            at = raw / 1000.0 if raw >= 10 ** 12 else float(raw)
            return int(at * 1000) if at > now else None
    if mode == "daily-rollover":
        import datetime
        stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
        nxt = (stamp + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return int(nxt.timestamp() * 1000)
    if mode == "duration":
        seconds = 0.0
        units = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60,
                 "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
                 "d": 86400, "day": 86400}
        for amount, unit in _DURATION_PART.findall(low.partition("reset")[2]):
            key = unit.lower()
            if key != "s" and key.endswith("s"):
                key = key[:-1]
            seconds += float(amount) * units.get(key, 0)
        if seconds > 0:
            return int((now + seconds) * 1000)
    return None


def _upstream_state(code, text, origin=None):
    low = str(text or "").lower()
    # Origin is a closed producer verdict, and it is read BEFORE every body
    # classifier on each code our proxy marks. A local refusal is marked before
    # the proxy renders it; neither quota nor content wording in a body our
    # proxy wrote itself is evidence of a vendor refusal. Missing, malformed,
    # or unknown provenance is never upgraded by body prose.
    if origin == _REFUSAL_LOCAL and code in _LOCAL_REFUSAL:
        return _LOCAL_REFUSAL[code]
    # For an upstream or untyped refusal, an explicit content-classifier verdict
    # in the body outranks the status code, which varies by gateway. Keeping the
    # named cause lets the reader reroute the content instead of re-diagnosing.
    if ("flagged for possible cybersecurity risk" in low
            or "chatgpt.com/cyber" in low):
        return _CONTENT_FLAGGED
    # HTTP 402 is itself the payment-required contract. A 403 or 429 is a quota
    # wall only when the vendor body says so; a genuine credential failure, and
    # a 403 whose body names nothing, stay AUTH-401.
    if code == 402:
        return _QUOTA_WALL
    if code in (403, 429) and _vendor_quota(low):
        return _QUOTA_WALL
    if code in (401, 403):
        return "AUTH-401"
    if code == 429:
        return "RATE-LIMITED"
    if code >= 500 and any(word in low for word in
                           ("auth_unavailable", "no auth available")):
        return "AUTH-UNAVAILABLE"
    if code >= 500 and any(word in low for word in
                           ("overload", "service_unavailable_error",
                            "servers are currently unavailable")):
        return "UPSTREAM-OVERLOADED"
    if code >= 500 and any(word in low for word in
                           ("timeout", "timed out", "deadline")):
        return "TIMEOUT-500"
    if 400 <= code < 500:
        return "UPSTREAM-4XX"
    if code >= 500:
        return "UPSTREAM-5XX"
    return "UNKNOWN"


def _upstream_once(seat_name, timeout=UPSTREAM_TIMEOUT_S, family=None):
    """One authenticated eight-token request -> (state, detail, elapsed_ms)."""
    import urllib.error
    import urllib.request
    from . import pi as pimod, seat as seatmod

    if family is None:
        family, err = seatmod._seat_family(seat_name)
        if err:
            return "UNKNOWN", err, None
    port, perr = pimod.seat_port(seat_name)
    token = pimod._pi_api_key(seat_name, family)
    model = (seatmod.FAMILIES.get(family) or {}).get("model")
    if perr or not port or not token or not model:
        return ("UNKNOWN", perr or "proxy port, token, or model unavailable",
                None)
    state, detail, ms, _status, _response_model, _trace = _canary_once(
        "http://127.0.0.1:%d" % port, token, model, timeout=timeout)
    return state, detail, ms


# NOT DERIVED FROM INTERVAL_S, AND THAT IS THE WHOLE POINT. The bar tracks
# the INSTALLED unit's period — OnUnitActiveSec=900s, 15 minutes — measured,
# not assumed. INTERVAL_S now agrees with the installed unit (installed is
# truth, ruled 2026-08-04), but when it lagged at 5 minutes, deriving the bar
# from it set the bar three times too tight, and my first version did exactly
# that: 3 * INTERVAL_S = 900s = ONE installed cycle, so any pass overrun would
# have read UNKNOWN in normal operation.
#
# helm/web.py hit this first and paid for it: it had 20 minutes here and EVERY
# badge on the fleet read UNKNOWN, because 15-minute cycles plus any overrun
# cross 20 routinely. Its conclusion is the one worth keeping — "a staleness
# rule that fires in normal operation does not report staleness, it just
# deletes the feature". Two missed cycles plus margin is the honest line: one
# late pass is not news, a watcher that has missed two is.
#
# ONE PREDICATE, TWO CONSUMERS: web.py's _UPSTREAM_STALE_S is this same number
# and should read it from here rather than keep its own copy — a duplicated
# threshold is how two surfaces come to disagree about whether the same record
# is stale.
UPSTREAM_CACHE_FRESH_S = 40 * 60


def proxy_runtime_snapshot(session, now=None, expected_proof=None):
    """(family, proof, error) from one cached proof re-proven live.

    The JSON record is an index and an immutable comparison target, never its
    own authority: ordinary seat processes can write files as the same Unix
    user. Every read therefore remeasures the exact session/pid, listener,
    loaded-config digest, and either singular route or deferred route-candidate
    set. The first read of that complete shape in a process also repeats the
    authenticated canary; only that process-local
    attestation may be reused, and only while the whole measured shape and
    persisted proof remain byte-equivalent. A hand-written cache, replaced pid,
    restarted listener, changed config, or changed route is UNKNOWN.

    Storage keys are deliberately ignored: a proof persisted under `ds4pro`
    derives Codex if and only if its measured route maps uniquely to Codex.
    ``expected_proof`` is the exact-session roster stamp; when supplied, it must
    agree with the cached proof ON EVERY AUTHORITY FIELD — session, pids,
    starttime, config digest, route, canary — before either can authorize a
    family. `observed_at` is EXCLUDED from that comparison, and only that key:
    one pass writes these two copies at different instants (the state file
    first, atomically, then each seat's roster stamp) and both carry their own
    clock, so demanding byte-equality manufactured a contradiction between two
    TRUTHFUL records for any reader landing in between. Everything a forged or
    stale proof would have to get right is still compared, the state copy is
    still bound to its own pass, and the shape is still re-proven live below.
    """
    now = time.time() if now is None else now
    state, err = _read_watch_state()
    if err:
        return None, None, err
    ts = (state or {}).get("ts")
    if type(ts) not in (int, float):
        return None, None, "proxywatch proof state has no readable timestamp"
    age = now - ts
    if age < -60:
        return None, None, "proxywatch proof state timestamp is in the future"
    if age > UPSTREAM_CACHE_FRESH_S:
        return None, None, ("proxywatch proof state is %dm old, bar %dm" %
                            (age // 60, UPSTREAM_CACHE_FRESH_S // 60))
    proofs = (state or {}).get("proxy_runtime")
    if not isinstance(proofs, dict) or not proofs:
        return None, None, "proxywatch has recorded no proxy runtime proof"
    matched = []
    malformed = []
    for key, proof in proofs.items():
        family, why = _proxy_proof_family(proof)
        if why:
            # A MALFORMED PROOF REFUSES ONLY THE SESSION IT CLAIMS. The state
            # holds every proxy seat's proof, and one seat's proof can be
            # malformed for a pass on its own account (a route that maps to
            # no family while its config is being rewritten, a foreign
            # process seeding a stale record). Refusing every OTHER seat's
            # verdict on it, naming that other seat, held two composed
            # approves on a transient the minting seat had no part in
            # (task/2693). Another seat's malformed record is reported on
            # that seat's own surfaces; here it authorizes nothing and
            # forbids nothing.
            if isinstance(proof, dict) and proof.get("session") == session:
                malformed.append("%s (%s)" % (key, why))
            continue
        if proof.get("session") == session:
            matched.append((family, proof))
    if malformed:
        return None, None, ("proxywatch runtime proof is malformed under %s" %
                            ", ".join(sorted(malformed)))
    if len(matched) != 1:
        return None, None, ("proxywatch runtime proof matched roster session %s "
                            "%d times" % (session, len(matched)))
    family, proof = matched[0]
    if proof.get("observed_at") != ts:
        return None, None, "proxywatch proof/session timestamp does not match its pass"
    sanitized = _sanitized_proxy_proof(proof)
    if expected_proof is not None:
        expected = _sanitized_proxy_proof(expected_proof)
        if expected is None:
            return None, None, "roster proxy runtime stamp is malformed"
        # EQUALITY OVER THE AUTHORITY FIELDS, NEVER OVER THE CLOCK. The pass
        # writes these two copies at different instants — the state file
        # first (atomically), then each seat's roster stamp — and BOTH carry
        # `observed_at`, which changes every pass. Byte-equality therefore
        # manufactured a contradiction between two TRUTHFUL records whenever
        # a reader landed between the state swap and the re-stamp: every
        # proxy-backed seat's tier read "unknown" for that window, every
        # 15 minutes, and a projection running through it demoted the seat's
        # gated approves to REVIEWED (task/1067, measured twice 2026-08-11).
        # Everything an attacker would have to forge — session, pids,
        # starttime, config digest, route, canary — is still compared; the
        # state copy is still bound to its own pass (`observed_at == ts`
        # above) and still re-proven live below. A mismatched clock between
        # two honest passes authorizes nothing and refuses nothing.
        if not _proxy_proofs_equivalent(expected, sanitized,
                                        ignore_observed_at=True):
            return None, None, "roster proxy runtime stamp does not match cached proof"
    identity, err = _roster_identity_for_session(session)
    if err:
        return None, None, err
    shape, err = _proxy_runtime_shape(identity)
    if err:
        return None, None, "live proxy runtime cannot re-prove cached evidence: %s" % err
    live_base = shape.get("proof") if isinstance(shape, dict) else None
    if not _proxy_proof_matches_live_base(
            proof, live_base, shape.get("auth_routes")):
        return None, None, "live proxy runtime no longer matches the cached proof"
    raw = json.dumps(sanitized, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":"))
    cache_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    attested_at = _PROXY_AUTH_CANARIES.get(cache_key)
    if type(attested_at) not in (int, float) or now - attested_at < 0 \
            or now - attested_at > _PROXY_AUTH_CANARY_FRESH_S:
        current, err = proxy_runtime_canary(
            identity, observed_at=proof["observed_at"])
        if err:
            return None, None, "live proxy runtime canary cannot attest authority: %s" % err
        if not _proxy_proofs_equivalent(current, sanitized):
            return None, None, "live proxy runtime changed since the cached proof"
        _PROXY_AUTH_CANARIES[cache_key] = now
    return family, sanitized, None


def upstream_snapshot(now=None):
    """({family: {state, since, dark}}, error_or_None) — the CACHED per-family
    upstream verdict, for surfaces that must never make a network call.

    THE GAP THIS CLOSES. proxywatch has classified the upstream for a long time
    — RATE-LIMITED, MALFORMED200, AUTH-UNAVAILABLE — and NOTHING read it.
    `helm seat where codex` printed "LIVE; liveness IDLE" and `helm seat list`
    printed "proxy UP ... valid until" for a seat that had been hard-walled by
    its provider for three hours. Measured 2026-08-04: that false picture cost
    four reviews between 20 and 153 minutes. The knowledge existed; the display
    never asked.

    A DISPLAY MUST NOT PROBE. `upstream_canary` makes a live authenticated HTTP
    request; a `seat where` that did that would be slow and side-effecting. So
    this reads the persisted verdict and nothing else.

    THREE WAYS TO GET NOTHING, AND ALL OF THEM ARE UNKNOWN, NEVER HEALTHY:
    missing (proxywatch has never run), unreadable/corrupt (the case a review
    found, where a blind read once masqueraded as clean), and STALE — a record
    from a watcher that stopped an hour ago describes a world that no longer
    exists. Staleness is the one a cache adds on its own, and reading it as
    current would rebuild the exact false picture this exists to end, only with
    fresher-looking words.

    The bar (UPSTREAM_CACHE_FRESH_S) is two missed 15-minute cycles plus
    margin, tracking the watcher's installed cadence: one missed run is
    jitter, a watcher that has missed two is not running."""
    now = time.time() if now is None else now
    state, err = _read_watch_state()
    if err:
        return None, "proxywatch state unreadable: %s" % err
    up = (state or {}).get("upstream")
    if not isinstance(up, dict) or not up:
        return None, "proxywatch has recorded no upstream verdict yet"
    ts = (state or {}).get("ts")
    if not isinstance(ts, (int, float)):
        return None, "proxywatch state carries no readable timestamp"
    age = now - ts
    if age > UPSTREAM_CACHE_FRESH_S:
        return None, ("proxywatch state is %dm old, bar %dm — the watcher is not "
                      "running" % (age // 60, UPSTREAM_CACHE_FRESH_S // 60))
    return up, None


def upstream_records(state):
    """The persisted family mapping, or a structural error.

    JSON validity is not schema validity. A list-shaped ``upstream`` or a string
    boolean must never steer an actuator by Python truthiness or crash the owner
    roster. Callers classify the target record separately so one corrupt sibling
    cannot hide every otherwise-readable family.
    """
    if not isinstance(state, dict):
        return None, "proxywatch state is not an object"
    records = state.get("upstream")
    if not isinstance(records, dict):
        return None, "proxywatch upstream state is not an object"
    return records, None


def upstream_record(state, family):
    records, err = upstream_records(state)
    if err:
        return None, err
    record = records.get(family)
    if not isinstance(record, dict):
        return None, "proxywatch has no readable %s family record" % family
    verdict = record.get("state")
    if verdict not in _UPSTREAM_DARK | _UPSTREAM_AGGREGATE | \
            {"HEALTHY", "UNKNOWN", _PROXY_COOLDOWN}:
        return None, "proxywatch %s state is invalid" % family
    dark = record.get("dark")
    invalid_dark = dark is not None and type(dark) is not bool
    since = record.get("since")
    if since is not None and not isinstance(since, str):
        return None, "proxywatch %s episode timestamp is invalid" % family
    normalized = dict(record)
    if invalid_dark:
        # Preserve the independently-valid named state. A bad boolean cannot
        # mint recovery: named dark stays paused, UNKNOWN holds, HEALTHY clears.
        normalized["dark"] = verdict != "HEALTHY"
        normalized["dark_invalid"] = True
    else:
        normalized["dark"] = _named_upstream_dark(verdict) \
            if dark is None else dark
    return normalized, None


def upstream_seat_record(state, family, seat_name):
    """The persisted canonical record for one local proxy process."""
    family_record, err = upstream_record(state, family)
    if err:
        return None, err
    seats = family_record.get("seats")
    if not isinstance(seats, dict):
        return None, "proxywatch %s seats state is not an object" % family
    record = seats.get(seat_name)
    if not isinstance(record, dict):
        return None, "proxywatch has no readable %s/%s seat record" % (
            family, seat_name)
    verdict = record.get("state")
    if verdict not in _UPSTREAM_DARK | {"HEALTHY", "UNKNOWN", _PROXY_COOLDOWN}:
        return None, "proxywatch %s/%s state is invalid" % (family, seat_name)
    since = record.get("since")
    if since is not None and not isinstance(since, str):
        return None, "proxywatch %s/%s episode timestamp is invalid" % (
            family, seat_name)
    normalized = dict(record)
    normalized.pop("falsification_bar_s", None)
    bar = family_record.get("falsification_bar_s")
    if type(bar) is int and bar > 0:
        normalized["falsification_bar_s"] = bar
    return normalized, None


def beacon_paused(record):
    """Whether one validated family record holds delivery in CRED-WALL pause.

    The dark latch is the canonical state: proxywatch writes it on a measured
    provider wall and clears it only on a measured HEALTHY result. UNKNOWN while
    a dark episode is in flight therefore HOLDS the pause rather than inventing
    recovery. A contradictory HEALTHY row always clears — the exit arc is the
    measured recovery, not a stale boolean.
    """
    if not isinstance(record, dict):
        return False
    state, dark = record.get("state"), record.get("dark")
    if state not in _UPSTREAM_DARK | _UPSTREAM_AGGREGATE | \
            {"HEALTHY", "UNKNOWN", _PROXY_COOLDOWN} \
            or dark is not None and type(dark) is not bool:
        return False
    if state in _UPSTREAM_AGGREGATE:
        return False
    dark = _named_upstream_dark(state) if dark is None else dark
    return state != "HEALTHY" and (dark or _named_upstream_dark(state))


def _observer_pause(family, error):
    return {"family": family, "state": "UNKNOWN", "since": None,
            "age_s": None, "stale": True, "observer_error": error}


def delivery_pause(seat_name, state=None, now=None, runtime=None,
                   runtime_verified=False):
    """(pause_or_none, error_or_none) for one seat's delivery actuator.

    This intentionally reads the persisted dark LATCH rather than
    ``upstream_snapshot``'s display freshness gate. Once a wall is measured,
    delivery stays paused until proxywatch records HEALTHY; a watcher that goes
    stale is not evidence that credentials recovered. Missing, unreadable, or
    structurally invalid observation state also cannot invent recovery: proxy
    delivery holds at UNKNOWN until the watcher records a valid family verdict.
    Direct-Claude/unknown seats have no proxy family and remain unaffected.

    THE SEAT'S OWN POOL WALL IS READ FIRST (helm/poolwall.py). The family
    latch is one verdict for every seat of a family, and it stays HEALTHY
    while one seat's proxy pool has every credential cooling down and its
    siblings on other accounts take turns. That seat's proxy.log carries the
    refusal with its reset; while the reset is ahead, delivery to that seat
    is paused with the same record shape, ``state`` PROXY-COOLDOWN, ``until``
    the reset instant and ``reason`` the one sentence every held path gives.
    """
    try:
        from . import seat as seatmod
        family, ferr = seatmod.family_for(
            str(seat_name or ""), runtime, runtime_verified)
    except Exception as exc:
        return None, "seat family unreadable: %s" % exc.__class__.__name__
    if ferr or not family:
        return None, None              # direct-Claude/unknown seats have no wall
    now = time.time() if now is None else now
    from . import poolwall
    wall, _why = poolwall.seat_wall(seat_name, family=family, now=now)
    if wall is not None:
        return poolwall.pause(wall, now), None
    if state is None:
        state, err = _read_delivery_state()
        if err:
            return _observer_pause(family, err), err
    if state == {}:
        return None, None                 # first run: no measurement exists yet
    records, shape_err = upstream_records(state)
    if shape_err:
        return _observer_pause(family, shape_err), shape_err
    if family not in records:
        return None, "proxywatch has no %s family record" % family
    record, err = upstream_record(state, family)
    if err:
        return _observer_pause(family, err), err
    warning = ("proxywatch %s dark latch is not boolean" % family
               if record.get("dark_invalid") else None)
    if not beacon_paused(record):
        return None, warning
    stamp = state.get("ts")
    age = max(0, now - stamp) if isinstance(stamp, (int, float)) else None
    return {"family": family, "state": record.get("state") or "UNKNOWN",
            "since": record.get("since"), "age_s": age,
            "stale": age is None or age > UPSTREAM_CACHE_FRESH_S}, warning


def upstream_canary(seat_name, sleep=time.sleep, family=None):
    """Confirmed family state; client timeouts never duplicate token spend."""
    first = _upstream_once(seat_name) if family is None else \
        _upstream_once(seat_name, family=family)
    if first[0] == _CLIENT_TIMEOUT:
        return "TIMEOUT-500", first[1], first[2]
    if first[0] in ("HEALTHY", "UNKNOWN"):
        return first
    sleep(UPSTREAM_CONFIRM_S)
    second = _upstream_once(seat_name) if family is None else \
        _upstream_once(seat_name, family=family)
    elapsed = (first[2] or 0) + (second[2] or 0)
    if second[0] == "HEALTHY":
        return ("HEALTHY", "transient %s cleared on %ds confirmation; %s"
                % (first[0], UPSTREAM_CONFIRM_S, second[1]), elapsed)
    if second[0] in ("UNKNOWN", _CLIENT_TIMEOUT):
        return (first[0], "%s observed; %ds confirmation unreadable — %s"
                % (first[1], UPSTREAM_CONFIRM_S, second[1]), elapsed)
    return (second[0], "%s then %s (%ds apart); latest %s"
            % (first[0], second[0], UPSTREAM_CONFIRM_S, second[1]), elapsed)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _proxy_incarnation(family, seat_name):
    """(verified birth identity, stable evidence state) for one local proxy."""
    if not family or not seat_name:
        return None, "NO-REPRESENTATIVE"
    try:
        from . import seat as seatmod
        record, reason, _raw = seatmod._proxy_pid_verdict(family, seat_name)
    except Exception:                       # noqa: BLE001 — watch stays alive
        return None, "UNREADABLE"
    return (record.get("identity"), "VERIFIED") if record else \
        (None, (reason or "UNREADABLE").upper())


def _seat_canary_observation(seat_name, family):
    """Canary plus a birth bracket; an in-flight restart never earns freshness."""
    birth_before, state_before = _proxy_incarnation(family, seat_name)
    result = upstream_canary(seat_name, family=family)
    birth_after, state_after = _proxy_incarnation(family, seat_name)
    if state_before == state_after == "VERIFIED" and birth_before == birth_after:
        return result, birth_before, "VERIFIED"
    if state_before == state_after == "VERIFIED":
        return result, None, "CHANGED-DURING-CANARY"
    return result, None, state_before if state_before != "VERIFIED" else state_after


def _named_upstream_dark(state):
    return state == _PROXY_COOLDOWN or state in _UPSTREAM_DARK


def _prior_seats(record):
    seats = record.get("seats") if isinstance(record, dict) else None
    return seats if isinstance(seats, dict) else {}


def upstream_seat_sample(upstream, family, seat_name):
    """One report-time seat observation, falling back only for legacy records."""
    family_record = upstream.get(family) if isinstance(upstream, dict) else None
    if not isinstance(family_record, dict):
        return {}
    seats = family_record.get("seats")
    if isinstance(seats, dict) and isinstance(seats.get(seat_name), dict):
        return seats[seat_name]
    return family_record


def _compose_upstream_seat(seat_name, result, before, now, birth=None,
                           identity_state="NO-REPRESENTATIVE"):
    """One canonical local-process verdict with cooldown continuity."""
    state, detail, elapsed = result
    before = before if isinstance(before, dict) else {}
    prior_wall = (before.get("state") if before.get("state") in _UPSTREAM_QUOTA
                  else before.get("quota_wall")
                  if before.get("quota_wall") in _UPSTREAM_QUOTA else None)
    wall_at = _parse_timestamp(before.get("wall_observed_at") or before.get("since"))
    reset = before.get("resets_at_ms")
    before_reset = type(reset) is not int or now * 1000 < reset
    auth_hold = state == "AUTH-UNAVAILABLE" and prior_wall is not None \
        and wall_at is not None and 0 <= now - wall_at <= QUOTA_WALL_INHERIT_S \
        and before_reset
    held_wall = prior_wall if state == _PROXY_COOLDOWN or auth_hold else None
    before_dark = bool(before.get("dark") or
                       _named_upstream_dark(before.get("state")))
    dark = _named_upstream_dark(state) or state == "UNKNOWN" and before_dark
    same_state = before.get("state") == state
    blind_dark_episode = state == "UNKNOWN" and before_dark
    since = before.get("since") if (same_state or blind_dark_episode) \
        and isinstance(before.get("since"), str) else _iso(now)
    current = {"state": state, "detail": detail, "ms": elapsed,
               "since": since, "dark": dark}
    if state in _UPSTREAM_QUOTA:
        current.update({
            "refusal_class": "money",
            "refusal_provenance": _refusal_provenance(state, detail),
            "wall_observed_at": _iso(now),
        })
        reset = _quota_reset_ms(detail, now)
        if type(reset) is int and reset > now * 1000:
            current.update({"resets_at_ms": reset,
                            "reset_kind": "vendor",
                            "reset_source": "canary"})
    elif held_wall:
        # A local refusal is reach evidence. The prior provider wall remains a
        # separate money fact with its original provenance and horizon; it is
        # never relabelled as a refusal the local proxy measured.
        current.update({"quota_wall": held_wall,
                        "wall_observed_at": before.get("wall_observed_at") or
                                            before.get("since")})
        for key in ("refusal_class", "refusal_provenance", "resets_at_ms",
                    "reset_kind", "reset_source"):
            if before.get(key) is not None:
                current[key] = before[key]

    # The falsification clock is a COOLDOWN's: it times a belief the proxy
    # will drop on restart. A PROXY-LOCAL-403 carries no clock and no
    # automatic restart; its repair starts with the proxy's stated reason.
    continuity = state == _PROXY_COOLDOWN or state == "UNKNOWN" and any(
        before.get(key) is not None for key in (
            "falsification_observed_at", "falsification_proxy_identity"))
    if not continuity:
        # THE EPISODE ENDS HERE AND THE EVIDENCE WOULD END WITH IT. `current`
        # is a fresh record holding only state/detail/ms/since/dark, and this
        # store is a snapshot replaced wholesale each pass -- so every
        # falsification field the belief was built on is dropped at exactly the
        # moment somebody would sit down to ask whether the machinery behaved.
        # One appended line before the drop, and NOTHING when there was nothing
        # to lose: a healthy watch writes no records at all.
        _journal.record_episode_end(seat_name, before, state, seat=seat_name)
        return current
    prior_birth = before.get("falsification_proxy_identity")
    if not isinstance(prior_birth, str) or not prior_birth.strip():
        prior_birth = None
    observed = before.get("falsification_observed_at")
    if not isinstance(observed, str):
        observed = None
    if identity_state == "VERIFIED" and birth:
        if birth != prior_birth:
            observed = _iso(now)
        prior_birth = birth
    elif observed is None:
        observed = _iso(now)
    current.update({
        "falsification_observed_at": observed,
        "falsification_proxy_identity": prior_birth,
        "falsification_identity_state": identity_state,
    })
    if state != _PROXY_COOLDOWN:
        return current
    started = _parse_timestamp(observed)
    age = None if started is None or started > now + 60 else \
        max(0, int(now - started))
    due = type(age) is int and age >= DARK_FALSIFICATION_S \
        and isinstance(seat_name, str) and bool(seat_name.strip()) \
        and isinstance(prior_birth, str) and bool(prior_birth.strip())
    current.update({
        "falsification_due": due,
        "falsification_age_s": age,
        "falsification_seat": seat_name,
    })
    return current


def upstream_health(rows, now=None, prior=None):
    """Per-seat upstream truth with a derived family compatibility summary."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = time.time() if now is None else now
    if prior is None:
        prior, err = _read_watch_state()
        if err:
            prior = {}
    prior_upstream = (prior.get("upstream") or {}) if isinstance(prior, dict) else {}
    grouped = {}
    for row in rows:
        if row.get("family") and not row.get("error"):
            grouped.setdefault(row["family"], []).append(row)

    candidates = [(family, row["seat"])
                  for family, family_rows in grouped.items()
                  for row in family_rows if row.get("probe") == "healthy"]
    results = {}
    if candidates:
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            futures = {pool.submit(_seat_canary_observation, name, family):
                       (family, name) for family, name in candidates}
            for future in as_completed(futures):
                family, name = futures[future]
                try:
                    results[(family, name)] = future.result()
                except Exception as ex:      # noqa: BLE001 — no false verdict
                    results[(family, name)] = (("UNKNOWN", "%s" % ex, None),
                                                None, "UNREADABLE")

    out = {}
    families = set(grouped) | set(prior_upstream)
    for family in sorted(families):
        before_family = prior_upstream.get(family) or {}
        before_seats = _prior_seats(before_family)
        seats = {}
        for row in sorted(grouped.get(family, ()), key=lambda item: item["seat"]):
            name = row["seat"]
            if (family, name) in results:
                result, birth, identity_state = results[(family, name)]
            else:
                result = ("UNKNOWN",
                          "no locally healthy proxy to carry the canary", None)
                birth, identity_state = None, "NO-REPRESENTATIVE"
            seats[name] = _compose_upstream_seat(
                name, result, before_seats.get(name), now,
                birth=birth, identity_state=identity_state)
        for name, before in sorted(before_seats.items()):
            if name not in seats and (before.get("dark") or any(
                    before.get(key) is not None for key in (
                        "falsification_observed_at",
                        "falsification_proxy_identity"))):
                seats[name] = _compose_upstream_seat(
                    name, ("UNKNOWN", "seat absent from current census", None),
                    before, now)
        if not seats:
            if before_family.get("dark") is True:
                out[family] = {
                    "state": "UNKNOWN",
                    "detail": "family absent from current seat census",
                    "ms": None, "seat": None, "seats": {}, "members": {},
                    "since": before_family.get("since"), "dark": True,
                }
            continue

        states = {record["state"] for record in seats.values()}
        dark = all(record.get("dark") is True for record in seats.values())
        if "UNKNOWN" in states:
            state = "UNKNOWN"
        elif len(states) == 1:
            state = next(iter(states))
        elif dark:
            state = "FAMILY-MIXED"
        else:
            state = "UNKNOWN"
        selected = min(seats, key=lambda name: (
            seats[name]["state"] == "HEALTHY", name))
        evidence = "; ".join("%s=%s (%s)" %
                             (name, record["state"], record.get("detail") or "")
                             for name, record in sorted(seats.items()))
        timings = [record["ms"] for record in seats.values()
                   if record.get("ms") is not None]
        family_since = min((record["since"] for record in seats.values()
                            if isinstance(record.get("since"), str)),
                           default=_iso(now)) if dark else \
            before_family.get("since") if before_family.get("state") == state \
            and isinstance(before_family.get("since"), str) else _iso(now)
        current = {"state": state, "detail": evidence,
                   "ms": sum(timings) if timings else None,
                   "seat": selected, "seats": seats,
                   "members": {name: record["state"]
                               for name, record in sorted(seats.items())},
                   "since": family_since, "dark": dark}
        wall_rows = [record for _name, record in sorted(seats.items())
                     if quota_wall(record)]
        unknown_reset = next((record for record in wall_rows
                              if type(record.get("resets_at_ms")) is not int),
                             None)
        wall = (unknown_reset or min(
            wall_rows, key=lambda record: record["resets_at_ms"])) \
            if wall_rows else seats[selected]
        held = quota_wall(wall)
        if state in _UPSTREAM_QUOTA or state == "AUTH-UNAVAILABLE" and held:
            current.update({key: wall.get(key) for key in (
                "refusal_class", "refusal_provenance", "wall_observed_at",
                "resets_at_ms", "reset_kind", "reset_source")
                            if wall.get(key) is not None})
            if held and state not in _UPSTREAM_QUOTA:
                current["quota_wall"] = held
        if any(record.get("state") == _PROXY_COOLDOWN
               for record in seats.values()):
            current["falsification_bar_s"] = DARK_FALSIFICATION_S
        out[family] = current
    return out


# The LOG rung. The grok seat's starvation was written, line by line, into the
# one file per seat that records every request's fate — and nothing read it.
# The real 402, quoted from grok's proxy.log (whitespace compressed; the id and
# address are a per-request hash and localhost — nothing sensitive):
#
#   [2026-07-29 11:20:00] [a2f8ba2a] [warn ] [gin_logger.go:95] 402 | 1.255s
#       | 127.0.0.1 | POST "/v1/messages?beta=true"
#
# TWO PATH FIELDS ARE LOAD-BEARING. The endpoint distinguishes agent refusal
# evidence from unrelated proxy traffic; the ``helm_canary=1`` query marker
# distinguishes Helm's own instrument from unmarked traffic on EITHER endpoint.
# This watch's probe writes a 401 to POST /v1/chat/completions on every pass
# (measured: three in one second, 2026-07-29 12:48:21). Counting every request
# would alarm on the instrument's reflection forever, so refusal state still
# considers only unmarked POST /v1/messages rows. But #97 proved exclusion alone
# is not accounting: a raw audit later called 1,952 marked canaries an unknown
# external hammer. ``log_observation`` therefore preserves both answers — the
# refusal state and explicit marked-vs-unmarked 401 provenance — from one read.

_TAIL_BYTES = 64 * 1024   # "recent" = the last 64KB of the file, not a time
                          # window: grok's 402s arrived HOURS apart across two
                          # days, and any freshness cutoff would have re-hidden
                          # the exact incident. History is dismissed only by
                          # RECOVERY — a later success breaks the streak.
STREAK_N = 3              # enough requests to establish a refusal cluster
STREAK_MIN_WINDOW_S = 60  # but not starvation: four 503s in four seconds was a
                          # measured upstream blip beside a healthy completed turn.
_AGENT_PATH = "/v1/messages"
_CAUSE_BY_CODE = {401: "AUTH", 403: "AUTH",
                  402: "BILLING/quota-exhausted", 429: "RATE-LIMITED"}

_REQ = re.compile(
    r'^\[([^\]]+)\] \[[^\]]*\] \[[^\]]*\] \[gin_logger\.go:\d+\] +'
    r'(\d{3}) \|.*\| +[A-Z]+ +"([^"]*)"')
_REFUSAL_ORIGIN = re.compile(
    r' \| refusal_origin_v1=(local|provider|unknown)$')
_RESPONSE_BODY = re.compile(
    r' \| response_body=("(?:\\.|[^"\\])*")(?: \[truncated\])?$')


def _logged_refusal_origin(line):
    """One exact producer verdict outside the quoted response body, else none."""
    body = _RESPONSE_BODY.search(line)
    if body:
        line = line[:body.start()]
    elif " | response_body=" in line:
        return None
    marker = " | refusal_origin_v1="
    if line.count(marker) != 1:
        return None
    found = _REFUSAL_ORIGIN.search(line)
    return found.group(1) if found else None


def _logged_response_body(line):
    """Decoded bounded fork response_body suffix, or None on old/opaque rows.

    Our CLIProxyAPI fork (silent-swallow-fix) appends the quoted error body to
    each refusal line; upstream rows carry none. Both shapes must scan — a
    parser that required the suffix would go blind on every unforked seat.
    """
    found = _RESPONSE_BODY.search(line)
    if not found:
        return None
    try:
        import ast
        body = ast.literal_eval(found.group(1))
        return body if isinstance(body, str) else None
    except (SyntaxError, ValueError):
        return None


# THE DELIVERY SUFFIX. A 200 is not a delivered turn. Our CLIProxyAPI fork
# appends, to every completed agent request, what the stream actually carried:
#
#   | stream_v1=committed terminal=message_stop text_bytes=54 tool_uses=0
#     empty_turn=1 last_input=user_text empty_recovery=exhausted empty_retry_n=2
#
# For months the proxy wrote those tokens and NOTHING in helm read them, so the
# founding complaint — that the watch cannot see an empty turn — stayed true on
# the consumer side and the only measurement of it was hand-rolled log
# archaeology. `_REQ` stops at timestamp/status/path, and every rung above it
# therefore reads an empty generation as a healthy 200.
#
# ORDERING IS UPSTREAM'S CONTRACT, NOT OURS TO MOVE. gin_logger.go renders this
# suffix BEFORE `response_body=` and `refusal_origin_v1=` precisely because
# `_REFUSAL_ORIGIN` and `_RESPONSE_BODY` anchor with `$`. Nothing here anchors,
# nothing here appends, and the two existing anchors keep resolving on a row
# that carries all three.
_STREAM_V1 = re.compile(r' \| stream_v1=(\S+) terminal=(\S+) '
                        r'text_bytes=(\d+) tool_uses=(\d+)')
_EMPTY_TURN = re.compile(r' empty_turn=(\S+)')
_EMPTY_LAST_INPUT = re.compile(r' last_input=(\S+)')
_EMPTY_RECOVERY = re.compile(r' empty_recovery=(\S+)')
_EMPTY_RETRY_N = re.compile(r' empty_retry_n=(\d+)')


def _stream_evidence(line):
    """What one completed request DELIVERED, or None when the row is silent.

    None means the row carries no suffix at all — an unforked proxy, or a
    build older than the instrument. That is UNOBSERVABLE, and every count
    downstream keeps it apart from an observed zero: a seat whose rows are all
    None reports UNINSTRUMENTED, never `0 empty turns`.

    The quoted error body is cut off BEFORE the tokens are searched, the same
    way `_logged_refusal_origin` cuts it, so a body quoting `empty_turn=` can
    never be read as an instrument reading. The cut takes the FIRST
    `response_body=` marker: a later one can only be body text, and a request
    path contrived to contain the marker costs this row its suffix rather than
    letting body bytes in.
    """
    body = line.find(" | response_body=")
    if body != -1:
        line = line[:body]
    found = _STREAM_V1.search(line)
    if not found:
        return None
    tail = line[found.end():]
    recovery = _EMPTY_RECOVERY.search(tail)
    retry = _EMPTY_RETRY_N.search(tail)
    last_input = _EMPTY_LAST_INPUT.search(tail)
    return {"stream": found.group(1), "terminal": found.group(2),
            "text_bytes": int(found.group(3)),
            "tool_uses": int(found.group(4)),
            "empty_turn": _EMPTY_TURN.search(tail) is not None,
            "last_input": last_input.group(1) if last_input else None,
            "recovery": recovery.group(1) if recovery else None,
            "retry_n": int(retry.group(1)) if retry else None}


def _refusal_cause(codes, bodies=None, origins=None):
    """The one cause class status codes + optional error bodies establish, or
    UNKNOWN. A body names the layer more precisely than its code class (a 503
    carrying auth_unavailable is an auth outage, not upstream weather), so
    unanimous named bodies outrank the code fallback; disagreeing ones stay
    UNKNOWN rather than laundering mixed evidence into one diagnosis."""
    codes = list(codes)
    bodies = list(bodies) if bodies is not None else [None] * len(codes)
    origins = list(origins) if origins is not None else [None] * len(codes)
    if len(bodies) != len(codes) or len(origins) != len(codes):
        return "UNKNOWN"
    # A local refusal names its cause with or without a body, on each code
    # our proxy marks (`_LOCAL_REFUSAL`); mixed with anything else it is
    # UNKNOWN rather than one side of a disagreement.
    origin_states = {_upstream_state(code, body, origin=origin)
                     for code, body, origin in zip(codes, bodies, origins)
                     if code in _LOCAL_REFUSAL}
    ours = origin_states & set(_LOCAL_REFUSAL.values())
    if ours:
        return next(iter(ours)) if len(origin_states) == 1 and \
            len(set(codes)) == 1 else "UNKNOWN"
    named = {_upstream_state(code, body, origin=origin)
             for code, body, origin in zip(codes, bodies, origins) if body}
    named.discard("UNKNOWN")
    if len(named) == 1:
        return next(iter(named))
    if len(named) > 1:
        return "UNKNOWN"
    kinds = {_CAUSE_BY_CODE.get(c, "UPSTREAM/transient" if c >= 500 else
                               "UNKNOWN") for c in codes}
    return next(iter(kinds)) if len(kinds) == 1 else "UNKNOWN"


def _window_s(streak):
    """Observed first-to-last refusal span, or None when a timestamp is opaque."""
    try:
        import calendar
        first = calendar.timegm(time.strptime(streak[-1][0],
                                              "%Y-%m-%d %H:%M:%S"))
        last = calendar.timegm(time.strptime(streak[0][0],
                                             "%Y-%m-%d %H:%M:%S"))
        return max(0, last - first)
    except (ValueError, OverflowError):
        return None


def _proxy_log_rows(path, tail_bytes):
    """Parsed request rows plus exact bounded-tail scope, or one read error."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            raw = f.read(tail_bytes)
    except OSError as e:
        return None, "proxy.log unreadable (%s)" % e, None
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]                   # a mid-file seek starts on a torn line
    rows = []
    for ln in lines:
        m = _REQ.match(ln)
        if m:
            rows.append((m.group(1), int(m.group(2)), m.group(3),
                         _logged_response_body(ln),
                         _logged_refusal_origin(ln),
                         _stream_evidence(ln)))
    return rows, None, {"bytes": len(raw), "truncated": size > tail_bytes}


def _is_helm_canary_path(request_path):
    """Whether one logged request carries Helm's exact canary query field."""
    return _CANARY_QUERY in request_path.partition("?")[2].split("&")


def _status_401_provenance(rows, scope):
    """Bounded 401 accounting by the request path's explicit Helm marker.

    ``helm_marked`` is evidence of the producer convention, not authenticated
    origin: another client could copy a public query field. ``unmarked`` is
    deliberately not called external or provider either. These are the two
    exact observations #97's raw status census erased, without overclaiming
    either side.
    """
    helm_marked = sum(code == 401 and _is_helm_canary_path(request_path)
                      for _ts, code, request_path, _body, _origin, _d in rows)
    unmarked = sum(code == 401 and not _is_helm_canary_path(request_path)
                   for _ts, code, request_path, _body, _origin, _d in rows)
    return {"helm_marked": helm_marked, "unmarked": unmarked,
            "total": helm_marked + unmarked,
            "basis": "request query marker %s" % _CANARY_QUERY,
            "scope": scope}


_EMPTY_ZERO_TERMINAL = "message_stop"


def _empty_turn_accounting(rows, scope):
    """Bounded empty-turn accounting from the SAME read as the 401 census.

    Measured over the fleet's logs when this reader was built: 1393 engaged
    turns, 97 recovered (7.0%, stable day to day), 1293 exhausted, every
    exhausted row carrying ``empty_retry_n=2`` — three upstream generations
    that delivered nothing. Codex holds the fleet's smallest window, so this
    is a SPEND reading before it is a health one, which is why the roll-up
    prints beside the codex budget lines rather than as an alarm.

    FOUR COUNTS THAT MUST NEVER SHARE A VALUE:

    ``instrumented`` 0 with ``rows`` above 0 means agent traffic reached this
    proxy and it emitted no delivery suffix — empty turns here are
    UNOBSERVABLE, not absent. ``rows`` 0 is an idle tail, which `_log_state`
    already reports and this census stays silent about.

    ``recovery`` counts by the token's LITERAL value, so a verdict the fork
    adds later is visible the day it ships instead of vanishing into an
    ``else``. Nothing here enumerates a closed set.

    ``unresolved`` is a row flagged ``empty_turn=1`` that carries no
    ``empty_recovery`` at all — the recovery machinery's answer is unknown.
    It is not ``none`` and is excluded from ``wasted_generations``, whose
    basis says so.

    ``zero_delivery_unflagged`` is the scope gap, reported and NOT diagnosed.
    313 such rows sat on non-codex seats when this was built: committed,
    ``message_stop``, zero text, zero tools, and no empty-turn token, because
    the instrument covers the codex path only. A real empty generation and a
    known upstream defect — a wrong api key answered HTTP 200 with an empty
    completion instead of 401 — are INDISTINGUISHABLE from the access row, so
    this count names the observation and refuses the diagnosis.
    """
    # THE DENOMINATOR IS AGENT TRAFFIC, on the same filter `_log_state` uses.
    # A delivery census over every logged row counts helm's own canary probes,
    # which carry no suffix by construction — and an IDLE seat whose tail is
    # all canaries would then be labelled UNINSTRUMENTED, which is a claim
    # about the proxy BUILD, not about what this tail happened to contain.
    # Measured on the live fleet before this filter: 17 of 18 seats read
    # UNINSTRUMENTED while every one of them also read `log=idle`.
    agent = [delivery for _ts, _c, request_path, _b, _o, delivery in rows
             if request_path.startswith(_AGENT_PATH)
             and not _is_helm_canary_path(request_path)]
    seen = [delivery for delivery in agent if delivery is not None]
    recovery, last_input = {}, {}
    engaged = unresolved = wasted = 0
    for d in seen:
        if not d["empty_turn"] and d["recovery"] is None:
            continue
        engaged += 1
        if d["last_input"]:
            last_input[d["last_input"]] = last_input.get(d["last_input"], 0) + 1
        if d["recovery"] is None:
            unresolved += 1
            continue
        recovery[d["recovery"]] = recovery.get(d["recovery"], 0) + 1
        if d["retry_n"] is None:
            unresolved += 1
            continue
        # Every retry was followed by another attempt, so every retry is spent;
        # the final attempt is spent too unless it is the one that delivered.
        wasted += d["retry_n"] + (0 if d["recovery"] == "recovered" else 1)
    zero_unflagged = sum(
        d["terminal"] == _EMPTY_ZERO_TERMINAL and not d["text_bytes"]
        and not d["tool_uses"] and not d["empty_turn"] and d["recovery"] is None
        for d in seen)
    return {"rows": len(agent), "instrumented": len(seen), "engaged": engaged,
            "recovery": recovery, "unresolved": unresolved,
            "wasted_generations": wasted, "last_input": last_input,
            "zero_delivery_unflagged": zero_unflagged,
            "basis": "upstream generations that delivered nothing, over rows "
                     "carrying empty_retry_n; unresolved rows excluded. "
                     "zero_delivery_unflagged is unflagged by the instrument "
                     "and its cause is UNREADABLE from the access row",
            "scope": scope}


def _tail_scope_label(scope):
    """How much of the file this count actually saw, or scope-UNKNOWN.

    Every bounded accounting labels its OWN scope. Sharing one label across
    two counts reads fine while both come from the same pass and silently
    mislabels the moment one of them does not.
    """
    scope = scope or {}
    if not isinstance(scope.get("bytes"), int):
        return "scope-UNKNOWN"
    return "%dB,%s" % (scope["bytes"],
                       "truncated" if scope.get("truncated") else "complete")


def _empty_turn_clause(empty):
    """The per-seat render, or "" when the seat has nothing to report.

    Silence means a measured zero, which is only honest because the two other
    answers print explicitly: a proxy with no suffix says UNINSTRUMENTED, and
    an unreadable log never reaches here at all.
    """
    if not empty:
        return ""
    if not empty["instrumented"]:
        return " empty=UNINSTRUMENTED" if empty["rows"] else ""
    out = ""
    if empty["engaged"]:
        by = ["%s:%d" % (k, empty["recovery"][k])
              for k in sorted(empty["recovery"])]
        if empty["unresolved"]:
            by.append("unresolved:%d" % empty["unresolved"])
        out += " empty[tail=%s]=%s waste=%dgen" % (
            _tail_scope_label(empty.get("scope")), "/".join(by),
            empty["wasted_generations"])
    if empty["zero_delivery_unflagged"]:
        out += " zero-delivery:%d(cause-UNREADABLE)" % \
            empty["zero_delivery_unflagged"]
    return out


def empty_turn_lines(rep):
    """The fleet roll-up, printed beside the codex budget it spends.

    One line, and only when a seat had something to say. A recovery rate over
    a handful of turns is noise, so the percentage prints only once the
    denominator can carry it; below that the raw counts stand alone.
    """
    total = {}
    uninstrumented, gap = [], []
    for row in sorted(rep.get("seats") or [], key=lambda r: r["seat"]):
        empty = row.get("log_empty_turns")
        if not empty:
            continue
        if not empty["instrumented"]:
            if empty["rows"]:
                uninstrumented.append(row["seat"])
            continue
        if empty["zero_delivery_unflagged"]:
            gap.append("%s:%d" % (row["seat"], empty["zero_delivery_unflagged"]))
        for key in ("engaged", "unresolved", "wasted_generations"):
            total[key] = total.get(key, 0) + empty[key]
        for state, n in empty["recovery"].items():
            total[state] = total.get(state, 0) + n
    out = []
    if total.get("engaged"):
        states = "/".join("%s:%d" % (k, total[k]) for k in sorted(total)
                          if k not in ("engaged", "unresolved",
                                       "wasted_generations"))
        if total.get("unresolved"):
            states += "/unresolved:%d" % total["unresolved"]
        rate = ""
        if total["engaged"] >= 20:
            rate = ", recovered %.0f%%" % (
                100.0 * total.get("recovered", 0) / total["engaged"])
        out.append("  empty-turns %d engaged — %s%s; %d upstream generation(s) "
                   "delivered nothing" % (total["engaged"], states, rate,
                                          total["wasted_generations"]))
    if gap:
        out.append("  empty-turns zero-delivery rows OUTSIDE the instrument: "
                   "%s — committed message_stop with no text and no tools, "
                   "cause UNREADABLE from the access row (an empty generation "
                   "and a 200-with-empty-body auth rejection look identical "
                   "here)" % " ".join(gap))
    if uninstrumented:
        out.append("  empty-turns UNINSTRUMENTED on %s — these proxies emit no "
                   "delivery suffix, so an empty turn there is unobservable, "
                   "not absent" % " ".join(uninstrumented))
    return out


def _log_state(rows, tail_bytes, streak_n, min_window_s):
    hits = [(ts, code, body, origin)
            for ts, code, request_path, body, origin, _delivery in rows
            if request_path.startswith(_AGENT_PATH)
            and not _is_helm_canary_path(request_path)]
    if not hits:
        return "idle", "no agent traffic in the last %dKB" % (tail_bytes // 1024)
    streak = []                             # newest first
    for ts, code, body, origin in reversed(hits):
        if code in (401, 402, 403, 429) or code >= 500:
            streak.append((ts, code, body, origin))
        else:
            break                           # RECOVERY: a non-refusal ends it
    if len(streak) < streak_n:
        return "ok", "last agent request HTTP %d at %s" % (hits[-1][1],
                                                           hits[-1][0])
    code = streak[0][1]
    codes = [c for _ts, c, _body, _origin in streak]
    others = sorted(set(codes) - {code})
    cause = _refusal_cause(
        codes, [body for _ts, _c, body, _origin in streak],
        [origin for _ts, _c, _body, origin in streak])
    window = _window_s(streak)
    summary = "cause %s; HTTP %d x%d%s, %s → %s" % (
        cause, code, len(streak),
        " (also %s)" % "/".join(str(c) for c in others) if others else "",
        streak[-1][0], streak[0][0])
    # Quote the refusal body the way FAMILY-DARK rows already quote upstream
    # detail. The cause class compresses; the body is the evidence — it sat
    # in the log all day on 2026-08-08 while every surface printed UNKNOWN,
    # and the owner found it by reading a pane. Newest non-empty body wins
    # (streak is newest-first); bounded so one verbose gateway cannot flood
    # the report; absent on unforked seats whose lines carry no body.
    quoted = next((b for _ts, _c, b, _origin in streak if b), None)
    if quoted:
        summary += "; body %r" % (quoted if len(quoted) <= 160
                                  else quoted[:157] + "...")
    # A CONTENT FLAG IS NOT AN OPINION ABOUT THE TRANSPORT, so it must not be
    # erased by disagreeing with one. `_refusal_cause` collapses to UNKNOWN
    # when bodies name different states, which is right for a QUESTION ONLY
    # ONE ANSWER CAN WIN — was this auth, or quota, or weather. Content
    # flagging is a different question: it says THIS REQUEST'S TEXT was
    # refused, which stays true whether or not a rate-limit shared the window.
    # Measured live 2026-08-09: codex refused 11 times in 243s carrying the
    # classifier body, one 429 joined the streak, and the alert printed cause
    # UNKNOWN — the exact silence the cause-naming cure was landed to end,
    # reappearing one layer along because the mixed case was never armed.
    # Reported as a SEPARATE clause, never by overriding the cause: the cause
    # stays honestly UNKNOWN, and the actionable fact stops being invisible.
    if any(b and _upstream_state(c, b, origin=o) == _CONTENT_FLAGGED
           for _ts, c, b, o in streak) and cause != _CONTENT_FLAGGED:
        summary += ("; AT LEAST ONE REFUSAL IN THIS WINDOW WAS %s — route "
                    "this content to another family" % _CONTENT_FLAGGED)
    if window is None:
        return "blip", "%s; refusal window UNKNOWN, cannot establish %ds " \
                       "starvation threshold" % (summary, min_window_s)
    if window < min_window_s:
        return "blip", "%s; %d refusals across %ds < %ds starvation threshold" % (
            summary, len(streak), window, min_window_s)
    return "streak", "%s; %d refusals across %ds >= %ds starvation threshold" % (
        summary, len(streak), window, min_window_s)


def log_observation(path, tail_bytes=_TAIL_BYTES, streak_n=STREAK_N,
                    min_window_s=STREAK_MIN_WINDOW_S):
    """One proxy-log read -> refusal state plus explicit 401 provenance.

    The 2026-08-03 #97 audit counted 1,967 keyless 401s and called their source
    unknown; 1,952 already carried ``helm_canary=1``. The earlier marker fix made
    Helm traffic identifiable, but no owner-layer accounting surface consumed
    the provenance, so a raw status census could still reverse the verdict.
    """
    rows, err, scope = _proxy_log_rows(path, tail_bytes)
    if err:
        return {"state": "unknown", "detail": err, "status_401": None,
                "empty_turns": None}
    state, detail = _log_state(rows, tail_bytes, streak_n, min_window_s)
    return {"state": state, "detail": detail,
            "status_401": _status_401_provenance(rows, scope),
            "empty_turns": _empty_turn_accounting(rows, scope)}


def logscan(path, tail_bytes=_TAIL_BYTES, streak_n=STREAK_N,
            min_window_s=STREAK_MIN_WINDOW_S):
    """(state, detail) — is the seat's OWN traffic being refused, repeatedly?

        streak   the recent tail ENDS in >= streak_n consecutive refusals of
                 agent traffic spanning >= min_window_s. The code class names
                 the supported cause; liveness decides whether it is STARVED.
        blip     enough consecutive refusals to notice, but their observed
                 window is shorter than min_window_s (or unreadable). Reported
                 as a disagreement/blip, never promoted to STARVED.
        ok       the newest agent request was not a refusal, or the refusals
                 ended before reaching streak_n. A streak that ENDED (recent
                 successes after the errors) is this, not a finding — history
                 is not an alarm.
        idle     no agent traffic in the recent tail. Not healthy, not a
                 finding — a truthful "nothing to observe".
        unknown  the log could not be read. NEVER reported healthy, never a
                 finding: unknown stays unknown.

    ``log_observation`` is the owner surface. This compatibility wrapper keeps
    existing callers on the refusal-state pair without discarding the canonical
    401 accounting from health reports.
    """
    observed = log_observation(path, tail_bytes, streak_n, min_window_s)
    return observed["state"], observed["detail"]


# The HUNG fuse. Each reader below is a thin, guarded wrap of a reading helm
# already takes somewhere — the fuse adds NO new opinion at any single probe,
# it composes theirs. Every reader answers None for "cannot see", and the fuse
# turns any None into HUNG-UNKNOWN rather than a verdict (logscan's law).

SPAWN_FRESH_S = 10 * 60   # a seat whose process came up inside this window is
                          # STARTING, not hung — `helm seat resume` relaunches
                          # onto an OLD transcript, so the stale gate passes
                          # from second one and only the spawn register knows
                          # the seat is new. 10m covers slow MCP/boot; against
                          # the 45m stale bar it hides a hung-from-birth seat
                          # for at most 10 of the 45 minutes it must wait anyway.

_TCP_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")


def inflight(port, tables=_TCP_TABLES):
    """ESTABLISHED client connections to the seat's proxy port, or None.

    The gin logger writes one line per request AT COMPLETION — an open
    streaming request writes nothing until it finishes, so "in-flight" is not
    readable from proxy.log at all. The kernel socket table is the honest
    source: each accepted client connection is one row whose LOCAL port is the
    proxy's listen port in state 01 (ESTABLISHED); the listener itself is 0A
    and never counted. Both v4 and v6 tables are read because a dual-stack
    listener lands 127.0.0.1 clients in tcp6 as v4-mapped rows.

    None means NO table could be read — unknown, never zero: a census that
    reports silence when it is blind is how a false HUNG gets composed. A
    count of 0 is a MEASUREMENT (true silence), which is exactly the reading
    the fuse needs. Measured live 2026-07-29: codex mid-work 5, kimi/ds4pro 1,
    idle grok/gemini 0.
    """
    n, seen = 0, False
    for path in tables:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                rows = f.read().splitlines()[1:]     # drop the header line
        except OSError:
            continue                                 # one table down != blind
        seen = True
        for ln in rows:
            cols = ln.split()
            if len(cols) < 4:
                continue
            try:
                lport = int(cols[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
            if lport == port and cols[3] == "01":
                n += 1
    return n if seen else None


def _inflight_for(name):
    """The socket census for one seat, or None when the port is unknowable."""
    try:
        from . import pi as pimod
        port, _err = pimod.seat_port(name)
        return inflight(port) if port else None
    except Exception:                       # noqa: BLE001 — a watch never raises
        return None


def _ctx_pct(name):
    """The seat's context %, via the reader autocompact already trusts —
    transcript usage first, proxy.log usage fallback. None = unknowable."""
    try:
        from . import autocompact
        return autocompact.read(name).get("pct")
    except Exception:                       # noqa: BLE001 — a watch never raises
        return None


def _compact_threshold():
    """autocompact's own bar — the fuse must not invent a second one."""
    try:
        from . import autocompact
        return autocompact.threshold_pct()
    except Exception:                       # noqa: BLE001
        return 90                           # autocompact's DEFAULT_THRESHOLD


def _open_work():
    """recipient -> open dispatch count, or None when the ledger cannot be
    read. None is 'helm could not read the obligations', never an empty board
    — the same unknown-stays-unknown law as every other fuse reader: a blind
    read collapsing to {} would derive IDLE for every seat at once."""
    try:
        from . import dispatches
        counts, _unavailable = dispatches.open_recipients()
        return counts
    except Exception:                       # noqa: BLE001 — a watch never raises
        return None


def _spawn_age_s(name):
    """Seconds since the seat's last (re)launch, from the spawn register —
    the one place that knows a resume happened, since the resumed pane's
    transcript keeps its OLD mtime. None when the record is absent, is another
    seat's (the same identity guard autocompact.read applies), or is undated."""
    try:
        import calendar
        from . import seat as seatmod
        family, err = seatmod._seat_family(name)
        if err:
            return None
        rec = seatmod._spawn_record(seatmod._instance_dir(family, name)) or {}
        if rec.get("seat") != name or not rec.get("ts"):
            return None
        at = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        return max(0, int(time.time() - at))
    except Exception:                       # noqa: BLE001 — a watch never raises
        return None


def onboarding_age_s(onboarding, now=None):
    """WALL seconds since the door's validated onboarding event, or None.

    A FLOAT, NEVER TRUNCATED, and that is the whole reason this is a named
    function instead of an expression. The value is compared against
    `transcript_age_s`, which is a FRACTIONAL semantic age, and truncating one
    side of a comparison changes its answer inside the same second: a 3600.4s
    semantic age against a 3600.9s onboarding event is correctly BELOW it, but
    against a truncated 3600 it reads as at-or-past and the ladder returns a
    verdict the precise values refuse.

    WALL SECONDS, on the same clock as the raw staleness, because the rung
    that uses it compares the two and a suspend correction applied to one and
    not the other would let a host suspend hide the answer.

    The floor at 0.0 is DISPLAY ONLY and changes no verdict: a stamp inside
    the door's future grace yields a small negative, and `wall_age >= age` is
    true for both the negative and the floored value, since semantic staleness
    is never itself negative. It exists so operator copy never reads a
    negative age.
    """
    if onboarding is None or onboarding.event_at is None:
        return None
    # `float(...)` IS NOT DECORATION. `max(0.0, x)` returns x UNCHANGED when x
    # is the larger operand, so two integer inputs give an integer back and
    # the type of this value would depend on the caller's clock. One type,
    # always, so no consumer has to ask.
    return float(max(0.0, (time.time() if now is None else now)
                     - onboarding.event_at))


def _spawn_onboarded(name, family=None):
    """This seat's onboarding register, parsed by its ONE door.

    THE LOADER, NOT THE SCHEMA. Reading the file and guarding the identity
    belong to a watch that must never raise; deciding what the fields MEAN
    belongs to `seat.parse_onboarding`, and this function makes no such
    decision — not a default, not a fallback to `ts`, not a reading of one
    field without the other.

    IT DOES OWN ONE DISTINCTION, AND IT IS THE ABSENT/INVALID ONE.
    `_spawn_record` answers None for a missing file and for a corrupt one
    alike, so a loader that passed that None straight through would collapse a
    register that IS THERE AND CANNOT BE READ into ABSENT — the door's own
    distinction, undone one layer below it. The existence of the file is
    therefore checked FIRST and it decides which kind a failure becomes:

      the path does not exist            ABSENT — legacy, change nothing
      the path exists and will not read   INVALID — a positive contradiction
      the record is not this seat's       INVALID — it is not a failed look

    `family` IS PASSED IN, NEVER RE-DERIVED. `_seat_family` resolves from the
    DISPLAY NAME, and an alias whose runtime family differs sends this reader
    to a different instance directory, where it finds no register and answers
    ABSENT about a record that exists and may be unreadable. The caller has
    already resolved and verified the family for the row; it hands that over
    rather than making this the second, weaker resolution of the same fact.
    """
    from . import seat as seatmod
    try:
        if family is None:
            family, err = seatmod._seat_family(name)
            if err:
                # A FAILED LOOK, and that is not a claim. An unresolvable name
                # means this reader never reached a register at all.
                return seatmod.ONBOARDING_NONE
        inst = seatmod._instance_dir(family, name)
        path = seatmod._spawn_path(inst)
        exists = os.path.exists(path)
    except Exception:                       # noqa: BLE001 — a watch never raises
        from . import seat as _seatmod
        return _seatmod.ONBOARDING_NONE
    if not exists:
        return seatmod.ONBOARDING_NONE
    # PAST THIS LINE THE REGISTER EXISTS, so every remaining failure is a
    # positive contradiction and none of them may read as legacy.
    try:
        rec = seatmod._spawn_record(inst)
    except Exception as exc:                # noqa: BLE001 — a watch never raises
        return seatmod.onboarding_invalid(
            "the spawn register at %s exists and could not be read (%s)"
            % (path, type(exc).__name__))
    if not isinstance(rec, dict):
        return seatmod.onboarding_invalid(
            "the spawn register at %s exists and does not parse as a JSON "
            "object" % (path,))
    if rec.get("seat") != name:
        return seatmod.onboarding_invalid(
            "the spawn register at this seat's own path records seat %r, not "
            "%r — the register is there and does not describe this seat"
            % (rec.get("seat"), name))
    try:
        return seatmod.parse_onboarding(rec)
    except Exception as exc:                # noqa: BLE001 — a watch never raises
        return seatmod.onboarding_invalid(
            "the onboarding door raised %s on the register at %s"
            % (type(exc).__name__, path))


def host_suspend_gap_s():
    """Seconds this HOST has spent suspended since boot, or None if unreadable.

    CLOCK_BOOTTIME advances while the machine is suspended; CLOCK_MONOTONIC
    does not. Their difference IS the suspended total — one syscall, no
    threshold, no heuristic, and a FACT rather than an inference. That is why
    it is the authority and the lockstep signature is only its corroborator.

    NONE, NEVER ZERO, WHEN IT CANNOT LOOK. A kernel without CLOCK_BOOTTIME, or
    a platform where the two clocks mean something else, has told us nothing —
    and `turn_state` treats None as "cannot tell" precisely so that silence
    cannot become a confident "the box was up the whole time". Reporting 0 here
    on failure is the single change that would turn this guard back into the
    six false HUNGs it exists to prevent.

    CAVEAT WORTH KEEPING: this is suspend-since-BOOT, so it is a monotone
    total, not a per-window figure. Subtracting it whole is correct only while
    the staleness window is younger than the last suspend — which is the case
    that matters (a suspend that predates the transcript entry did not inflate
    its age). A caller wanting per-window precision must difference two
    readings, and proxywatch's own posted rows are already
    that record."""
    try:
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)
        mono = time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return None
    gap = boot - mono
    # THE TWO READS ARE NOT SIMULTANEOUS, so a host that has never suspended
    # yields a delta of a few NANOSECONDS on either side of zero — and the
    # first version of this returned None for it, because -0.0000001 is not
    # >= 0. MEASURED LIVE: this box (38h uptime, never suspended) read None
    # rather than 0, which would have made every stale seat hung-unknown and
    # suppressed real hang detection entirely. A mock with clean integers
    # would never have shown it.
    #
    # So the band around zero is NOISE, not signal: under a second in either
    # direction is "no suspend". Only a substantially NEGATIVE delta means the
    # two clocks lack the semantics this depends on, and that is UNKNOWN.
    if -1 < gap < 1:
        return 0
    return int(gap) if gap > 0 else None


def turn_state(pane_live, age, log_state, inflight_n, ctx_pct, ctx_threshold,
               spawn_age, pane_blind=None, reality=None, open_dispatches=None,
               suspend_gap_s=0, onboarding=None, onboarding_age_s=None):
    """(verdict, evidence) — the fused turn-state ladder, pure on its inputs.

    `pane_live` is THREE-VALUED, exactly as the process census is: True, False,
    or None for "helm could not read every claude on this host". None is the
    one this ladder used not to have — an unreadable census arrived here as
    False and left as `off`, a verdict that says helm looked. `pane_blind`
    carries the reason so the evidence can name the pids.

    `age` is SEMANTIC staleness — the last COMPLETED main-chain entry, never
    mtime. `reality` is transcript_reality's reading (None when a caller has
    only the age); its raw-write age separates retry churn from a silent long
    generation at the in-flight rung, and turn_complete/pending_after join the
    pending census. `open_dispatches` is the count of open dispatch rows
    addressed to this seat (None = ledger unreadable) — the input that
    separates IDLE from HUNG.

    `suspend_gap_s` is THREE-VALUED like `pane_live`, and for the same reason:
    the seconds the HOST was suspended across the staleness window, 0 when it
    provably was not, and None when the caller COULD NOT TELL. None is not
    zero. A reading that could not look has made no claim, and letting it
    collapse into "no suspend" is how a box-level event becomes N false hangs.

    The vocabulary is deliberately distinct and never collapsed:

        onboarding-unreadable
                       the spawn register's onboarding record is PRESENT and
                       does not parse. Decided BEFORE every other rung, and it
                       authorizes nothing — no takeover may be built on a
                       register helm cannot read
        unonboarded    the LAST launch recorded its onboarding brief as NOT
                       PROVEN submitted and no turn has completed since. The
                       seat cannot be reached, and the remedy is a guarded
                       pane read rather than a restart
        off            no live pane — a seat nobody launched is not hung
        ok             a turn completed inside the stale window
        starved        the proxy is refusing the seat's own traffic — the log
                       rung OWNS this class and its finding; the fuse defers
        thinking       an in-flight request is open at the proxy and nothing
                       nonsemantic was written past the wall — a long
                       legitimate generation. The 2026-07-30 victim held a
                       socket while queue-operation retries kept the file
                       fresh: a held socket beside FRESH nonsemantic writes is
                       churn (pending work, not progress) and falls through
                       toward hung. Without a reality reading a socket still
                       reads thinking — the pre-reality contract.
        compact-needed context at/over the compact bar — the known-benign
                       class; autocompact owns the fix
        fresh          the process came up moments ago and has not turned yet
                       — starting, not hung (a resume relaunches onto an old
                       transcript, which reads stale immediately)
        idle           every pending census measured EMPTY — turn complete,
                       nothing queued after it, zero in-flight, zero open
                       dispatches. A stale transcript on a seat that is OUT OF
                       WORK is idleness, not a hang.
        hung-unknown   ANY needed input was unreadable. The load-bearing law:
                       a check that cannot see a case returns UNKNOWN, never a
                       false HUNG verdict
        hung           ALL of: pane live, semantic entry stale, below the
                       compact bar, not freshly spawned, and MEASURED pending
                       work the seat is not completing

    `onboarding` is the CLOSED result of `seat.parse_onboarding` (None from a
    caller that has not read the register), and `onboarding_age_s` is the WALL
    seconds since its validated event, derived by the caller because only the
    caller knows `now`. This ladder never touches the raw fields: it reads the
    result's own predicates and nothing else, which is the whole point of the
    door — a rung that re-derived "was it briefed" from an outcome and a stamp
    would be a second schema standing beside the one the door owns.

    Evidence rides with the verdict — which signals composed it and their
    values — because a verdict the owner cannot audit is a guess with
    confidence.
    """
    # DECIDED ONCE, AND FIRST. An onboarding record that is present and does
    # not parse is a fact about the REGISTER, readable without looking at a
    # pane, a socket or a clock — so no rung below can be trusted to have
    # composed it correctly, and none of them should have to try. Putting it
    # above the blind-input rungs costs nothing in safety, because both are
    # non-authorizing: `hung-unknown` and this verdict are equally unusable by
    # takeover. What it buys is that the answer cannot depend on which OTHER
    # input happened to be unreadable at the same moment.
    #
    # THE FINDING IS NOT KEYED ON THIS VERDICT. `findings()` reports an
    # unreadable register from the parse result directly, so a row can carry
    # both this verdict and its own separate report line, and neither fact can
    # mask the other.
    if onboarding is not None and onboarding.unreadable:
        return "onboarding-unreadable", (
            "the spawn register's LATEST onboarding record is present and does "
            "not parse (%s), so whether that launch's brief reached this seat "
            "is UNKNOWN — not proven, and not known-unsent. This reads the "
            "LATEST record only and makes no claim about earlier launches. "
            "Read the pane "
            "(`helm seat composers`), which classifies the composer without "
            "typing into it" % (onboarding.reason or "reason not recorded"))
    # THE HOST WAS NOT RUNNING, SO NEITHER WAS THE SEAT. Staleness is measured
    # in WALL time, and wall time keeps counting while the box is suspended —
    # so every seat comes back stale by the length of the suspend, all at once.
    # Six seats each answer "I am stale" truthfully and the composition invents
    # six hangs (measured: six lockstep false HUNGs).
    #
    # THIS IS THE "WRONG AUTHORITY" IN ONE LINE: no per-seat question can see a
    # host-wide event, because it is not a property of any seat. The fix is not
    # a new verdict beside `hung` — it is that the AGE was never the seat's
    # elapsed time to begin with. Subtracting the suspend restores the quantity
    # the whole ladder below already reasons about correctly, so every rung
    # inherits the correction instead of each growing a suspend clause.
    #
    # A GENUINELY HUNG SEAT STILL READS HUNG: its excess staleness survives the
    # subtraction. This only ever removes time the host provably did not run.
    suspended = ""
    # BOUND BEFORE ANY CORRECTION: `age` is rewritten in the suspend branch
    # below, and the unonboarded rung needs the WALL stamp so it compares two
    # values measured on the same clock as the onboarding event age.
    wall_age = age
    if suspend_gap_s is None:
        # UNKNOWN NEVER READS AS "NO SUSPEND" (from the meld). If helm
        # cannot tell whether the box stopped running, it cannot tell whether
        # this seat's staleness is its own — and the file's own law is that any
        # unreadable input yields hung-unknown, never a confident verdict. Only
        # a seat that would otherwise be a HANG CANDIDATE is affected: inside
        # the stale window the suspend cannot change the answer.
        if age is not None and age > HANG_S and pane_live:
            return "hung-unknown", (
                "semantic entry stale %dm, but whether the HOST was suspended "
                "across that window is UNREADABLE — a suspend adds the same "
                "delta to every seat and would make this staleness not the "
                "seat's own. UNKNOWN, never a confident HUNG" % (age // 60))
    elif suspend_gap_s and age is not None:
        corrected = max(0, age - suspend_gap_s)
        suspended = ("; %dm of that is host suspend (wall age %dm, agent age "
                     "%dm)" % (suspend_gap_s // 60, age // 60, corrected // 60))
        age = corrected
    if pane_live is None:
        # THE SAME RUNG THE OTHER BLIND INPUTS ALREADY HAD, one input later to
        # get it. `off` is a positive claim about the world; a census that
        # could not look has made no claim at all, and UNKNOWN must never
        # collapse into another state.
        return "hung-unknown", ("cannot see: which seats hold a live pane%s — "
                                "UNKNOWN, never a confident `off`"
                                % ("" if not pane_blind else " (%s)"
                                   % pane_blind))
    if not pane_live:
        return "off", None
    if age is not None and age <= HANG_S:
        return "ok", None
    # From here: live pane, stale (or undated) semantic entry — the candidate
    # shape.
    if log_state == "streak":
        return "starved", "proxy refusing the seat's own traffic — the " \
                          "STARVED class, owned by the log rung"
    write_age = reality.get("write_age_s") if reality is not None else None
    if inflight_n:
        socket = "%d in-flight connection%s at the proxy port" \
            % (inflight_n, "" if inflight_n == 1 else "s")
        if reality is None:
            return "thinking", socket + " — an open request IS the turn " \
                "running; a long generation is not hung"
        if write_age is None:
            return "hung-unknown", socket + "; raw-write age unreadable — " \
                "cannot separate a silent long generation from retry churn"
        if write_age > HANG_S:
            return "thinking", socket + "; nothing written since the prompt " \
                "(%dm) — a long generation is not hung" % (write_age // 60)
        # Fresh nonsemantic writes beside a stale semantic entry: the churn
        # shape. The held socket is pending work, never proof of progress —
        # fall through and let the pending census decide.
    # PRECEDENCE, DECIDED EXPLICITLY: THIS RUNG OUTRANKS compact-needed.
    # A failed resume onto an old 93%-context transcript composes both, and
    # compact-needed says "autocompact owns the fix" — which is FALSE here,
    # because autocompact refuses on unsent input, and unsent input is exactly
    # what this state is. The more specific verdict wins, and it wins BELOW
    # every blind-input rung so UNKNOWN still outranks both.
    #
    # UNONBOARDED — A SEAT WHOSE LAST LAUNCH NEVER GOT ITS BRIEF IN. Without
    # this rung it reads `idle`, with the evidence "out of work, not stuck",
    # and every clause of that sentence is TRUE: the pane is live, the censuses
    # are empty, nothing is queued. It is also the exact reading a seat gives
    # when it never read its brief, so it armed no wake beacon and no
    # @mention, DM or dispatch can reach it — and idle is the one verdict that
    # tells an operator to leave a seat alone.
    #
    # THE PREDICATE IS "NO COMPLETED TURN SINCE THE ONBOARDING EVENT", not "no
    # beacon". A beacon proves a wake PATH and never a LISTENER: an armed
    # `helm chat wait` on a seat with no turn loop leaves it just as
    # unreachable, because there is nothing for the beacon to wake.
    #
    # IT CONSUMES THE DOOR AND DECIDES NOTHING ITSELF. `refused` is true only
    # for a fully VALID record whose outcome is one of the two refusals —
    # never for ABSENT, which nobody wrote, and never for INVALID, which
    # nobody can read and which this ladder already answered further up. A
    # malformed outcome cannot reach this rung, by construction rather than by
    # a check written here.
    if (onboarding is not None and onboarding.refused
            and wall_age is not None and onboarding_age_s is not None
            and wall_age >= onboarding_age_s):
        # THIS VERDICT PRESCRIBES NO KEYSTROKE, and that is the load-bearing
        # half. `submit` refuses for FOUR different reasons and only one of
        # them — a positively foreign composer — means a person's text is
        # sitting there; the other three are an unidentifiable paste chip, an
        # unreadable pane, and a pane that never finished painting Helm's own
        # text. This reader has taken NO fresh look, so it cannot tell which,
        # and "press Enter" would be correct for three and would SUBMIT A
        # HUMAN'S TEXT for the fourth. The recorded refusal said which one it
        # was, so quote it and send the operator to the door that re-reads the
        # pane and refuses anything it cannot identify.
        return "unonboarded", (
            "pane LIVE and REGISTERED, but the LATEST launch recorded its "
            "onboarding brief as NOT PROVEN submitted and no turn has "
            "completed since (last semantic entry %dm, onboarding event %dm) "
            "— so this seat has not armed a wake beacon since that launch and "
            "nothing can reach it. Recorded reason: %s. Read the pane before "
            "anything else: `helm seat composers` classifies it, and "
            "`--submit` is the only safe actuator because it re-reads and "
            "refuses any composer it cannot identify as Helm's own. Do NOT "
            "send a bare Enter on this evidence alone"
            % (wall_age // 60, onboarding_age_s // 60,
               (onboarding.proof or "not recorded")))
    if ctx_pct is not None and ctx_threshold is not None \
            and ctx_pct >= ctx_threshold:
        return "compact-needed", "context %.1f%% >= %d%% — the known-benign " \
            "compact class; autocompact owns the fix" % (ctx_pct, ctx_threshold)
    if spawn_age is not None and spawn_age <= SPAWN_FRESH_S:
        return "fresh", "spawned %dm ago (< %dm) — starting, not hung; a " \
            "resume relaunches onto an old transcript" \
            % (spawn_age // 60, SPAWN_FRESH_S // 60)
    unread = [label for label, v in (("semantic transcript age", age),
                                     ("socket census", inflight_n),
                                     ("context%", ctx_pct),
                                     ("spawn age", spawn_age),
                                     ("open-dispatch census", open_dispatches))
              if v is None]
    if reality is not None:
        unread += [label for label, v in
                   (("turn completion", reality.get("turn_complete")),
                    ("pending-after census", reality.get("pending_after")))
                   if v is None]
    if unread:
        return "hung-unknown", "cannot see: %s — UNKNOWN, never a false " \
                               "HUNG" % ", ".join(unread)
    pending = ["%d open dispatch%s" % (open_dispatches,
                                       "" if open_dispatches == 1 else "es")] \
        if open_dispatches else []
    if reality is not None:
        if reality.get("turn_complete") is False:
            pending.append("an open turn (last stop was tool_use)")
        if reality.get("pending_after"):
            pending.append("queued transcript work after the last completed "
                           "entry")
    if inflight_n:
        pending.append("a held socket beside fresh nonsemantic writes")
    if not pending:
        return "idle", ("pane LIVE; semantic entry stale %dm but every "
                        "pending census measured EMPTY (0 in-flight, 0 open "
                        "dispatches%s) — out of work, not stuck"
                        % (age // 60, "; turn complete, nothing queued"
                           if reality is not None else ""))
    inflight_part = "0 in-flight connections at the proxy port (true " \
        "silence, not mid-stream)" if not inflight_n else \
        "%d in-flight connection%s (held socket — churn, not progress)" \
        % (inflight_n, "" if inflight_n == 1 else "s")
    churn = ""
    if write_age is not None:
        churn = ("; no raw transcript writes either (%dm — total silence)"
                 if write_age > HANG_S else
                 "; raw writes stayed fresh (%dm) — only queue/retry/"
                 "nonsemantic rows advanced") % (write_age // 60)
    return "hung", ("pane LIVE; semantic entry stale %dm > %dm; pending: %s; "
                    "%s; context %.1f%% < %d%% compact bar; spawned %dm ago "
                    "(not fresh)%s"
                    % (age // 60, HANG_S // 60, ", ".join(pending),
                       inflight_part, ctx_pct, ctx_threshold,
                       spawn_age // 60, churn))


def _watched_seats():
    """EVERY minted seat — the widening the grok incident bought.

    This function is where the fleet was one family short: it filtered
    `_minted_seats()` down to codex and its instances, the seats the bench
    lift was scoped to, and grok starved for TWO DAYS in full view of a watch
    that never looked at it. `_minted_seats()` is already the ONE enumeration
    doctor, --ensure, and the CPU canary walk — a seat with a config.yaml is
    minted, and a minted seat is watched. No hardcoded family list can be
    short.

    (`_minted_seats()`, not `registered_seats()` — and the reason CHANGED in
    2026-08-02, so read it fresh rather than by memory. It used to be that the
    register walk was flat and silently omitted the numbered codex instances, which was
    this function's original narrower bug. That walk is fixed now and does see
    the instances, so the old reason is gone. The CURRENT reason is a
    predicate difference that survives the fix: a MINTED seat has a
    config.yaml and is therefore a proxy this watch supervises, while a
    REGISTERED seat merely has a spawn record and may have no proxy at all.
    This watch is about proxies, so it must ask which seats are minted — not
    which have ever been spawned.)
    """
    from . import seat as seatmod
    try:
        return sorted({n for _f, n in seatmod._minted_seats()})
    except Exception:                       # noqa: BLE001 — a watch never raises
        return ["codex"]


def fold_blind(blind, per_seat):
    """The ONE host-wide answer for a caller that can only act on one.

    `_live_seats` reports HOST-WIDE doubt and PER-SEAT doubt separately, and a
    consumer rendering a row per seat must keep them apart. A consumer that
    ACTS on a single yes/no — `autocompact._live_seat_names` mutes its whole
    rung on any blindness, deliberately — needs them folded, and this is the
    only place that rule is written. Two copies of a fold is how one surface
    starts calling a fleet measured while the other calls it blind.
    """
    if blind:
        return blind
    if not per_seat:
        return None
    # A SEAT THE JOIN REFUSED IS A SEAT THE CENSUS COULD NOT KEY, and saying
    # nothing about it is the positive claim `_read_panes` forbids. Ambiguity
    # and a declared name helm cannot resolve are what `_session_joined`
    # refuses, and both mean helm KNOWS a live process may hold this seat and
    # declines to say which — the opposite of `off`.
    return ("helm could not key %d roster seat(s) to a live process: %s"
            % (len(per_seat), "; ".join(sorted(per_seat.values()))[:400]))


def _live_seats():
    """(names, blind, {seat: refusal}) — seat names with a live claude
    process, whether the census could see the WHOLE HOST, and the refusals
    that belong to exactly ONE SEAT EACH.

    A hang needs a LIVE pane, since a seat nobody launched is not hung, it is
    off. But "no readable process names this seat" and "helm could not read
    every claude on this host" are different facts, and only the first of them
    earns `off`. A round-7 finding (dispatch 90845108): this function
    filtered `procs` and dropped `unreadable`, so a seat whose only process
    helm could not identify came back absent — and `turn_state` reads absent as
    `off`, which this module's own vocabulary defines as deliberate.

    THE THIRD ELEMENT IS A BLAST-RADIUS BOUND, AND THE SPLIT IS A FACT ABOUT
    THE TWO BUCKETS RATHER THAN A PREFERENCE (task/2739):

      `blind`    an UNIDENTIFIED pid, a census that RAISED, or a roster that
                 would not read. In every one of those the unaccounted process
                 COULD BE ANY SEAT'S, so the doubt is genuinely host-wide and
                 `orcaadopt.cannot_look` is right to make it so.
      `per_seat` a refusal `_session_keyed_seats` raised while keying ONE
                 roster seat. Its evidence is that seat's own current session
                 and the pids carrying it; it names that seat, and every pid it
                 is about is already accounted for in `names` or in the walk.
                 It says NOTHING about any other seat.

    MEASURED on the live host that filed task/2739, before the split: 986 pids
    enumerated, 24 claude candidates examined, 0 unreadable — and ONE per-seat
    refusal (a seat renamed while live, see `orcaadopt._declared_verdict`) was
    the whole host's `blind`. That flips `pane_live` from a measured False to
    None for 29 of 40 roster seats, and pastes one seat's refusal sentence
    verbatim into nine `helm seat list` rows about OTHER seats, each reading
    `UNKNOWN pane=UNKNOWN` with a stranger's pid as its explanation. One
    unkeyable process must cost exactly one seat.

    ONE PRODUCER, ONE NAME. An earlier cut of this lane kept a two-tuple
    `_live_seats` beside a keyed one so the existing stand-ins would not move.
    That put a SECOND door on the host walk, and `tests/test_landreq`'s
    stand-in guard measured the cost immediately: its module-wide "never walk
    the host" patch named one door, the usability join had quietly moved to the
    other, and the whole module went back to reading the real machine while
    staying green. A consumer that wants the single answer folds it with
    `fold_blind`; nobody gets a second function to patch.

    `blind` is the reason string from `orcaadopt.cannot_look` — the one
    implementation of that rule — or None. The `except` returns it too: a
    census that RAISED did not find an empty fleet, it found nothing at all.
    """
    from . import orcaadopt
    try:
        procs, unreadable = orcaadopt.claude_processes()
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        return set(), ("the process census could not be taken (%s), so helm "
                       "cannot say which seats hold a live pane" % e), {}
    named = [p for p in procs if p.get("seat")]
    seats = {p["seat"] for p in named}
    joined, refusals = _session_keyed_seats(procs, named)
    blind = orcaadopt.cannot_look(unreadable,
                                  "helm cannot say which seats hold a live "
                                  "pane")
    per_seat = {}
    for subject, why in refusals:
        if subject is None:
            # NOT EVERY REFUSAL HAS A SUBJECT, and the one that does not is the
            # roster read itself: with no roster NO seat could be keyed, so the
            # doubt covers the host and belongs beside `cannot_look`'s.
            blind = blind or why
        else:
            per_seat[subject] = why
    return seats | joined, blind, per_seat


def _session_keyed_seats(procs, named):
    """(seats, [(subject, refusal)]) — roster seats a live process holds by
    SESSION, and every refusal WITH THE SEAT IT IS ABOUT.

    EACH REFUSAL NAMES ITS SUBJECT, and that was always a fact about the loop
    below rather than a new measurement: every refusal here is raised while
    keying ONE roster seat, and the seat is the loop variable. Only the roster
    read above the loop has no subject, and it carries `None` because with no
    roster NOTHING could be keyed. A BARE-STRING refusal cannot be filed against
    a seat, so a caller holding one has nowhere to put it but a host-wide
    channel — which is how the `one seat never blinds the rest` comment on the
    per-seat `except` stays true about the LOOP while the ANSWER blinds the
    host (task/2739).

    THE ENV WALK ALONE IS BLIND TO EVERY ADOPTED SEAT, PERMANENTLY. A proc's
    `seat` comes from HELM_CHAT_NAME, which launch.sh exports only for seats
    helm SPAWNS, and `/proc/PID/environ` is frozen at exec — so a seat adopted
    or hand-launched after the fact never acquires one however long it runs.

    MEASURED ON THIS HOST: 44 live claude processes, 10 carrying a
    seat name and 28 carrying none, with `unreadable` EMPTY — so the census
    returned 10 seats and `blind=None`, a confident complete answer that was
    missing five live seats including two claude seats and
    the integrator itself. The three seats that sat parked for seventeen
    hours were among them.

    THE JOIN IS THE ONE ALREADY TRUSTED FOR ADDRESSING, not a second weaker
    one: `_session_joined` is what `orcaadopt.resolve` uses, with its refusals
    intact — current session only, a contradictory declared seat refuses, and
    one session claimed by two live processes refuses rather than picking. A
    census may be more generous than an ADDRESS, but it must not be generous
    in a way that invents a holder, so it reuses the strict rung rather than
    relaxing it.

    COST, measured beside the alternative that was rejected: the roster read
    is 0.001s and the whole sweep over 23 seats is 0.175s, against 1.48s for a
    SINGLE adapter pane read. Keying the census is affordable in a way that
    reading every tail is not.
    """
    from . import orcaadopt, seats_common
    seats, refusals = set(), []
    try:
        roster = seats_common.roster() or {}
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        return seats, [(None, "the roster could not be read (%s)" % e)]
    for name in sorted(roster):
        if any(p.get("seat") == name for p in named):
            continue                        # env already keyed it
        try:
            current_sid, _sids, _failed = orcaadopt.roster_identity(name)
            # THE JOIN'S `named` IS THIS SEAT'S OWN PROCESSES, NOT THE FLEET'S.
            # `resolve` passes `[p for p in procs if p["seat"] == seat]` and
            # `_session_joined` EXCLUDES that set before testing ambiguity and
            # contradiction. Handing it every env-named process in the census
            # excluded other seats' rows from those rungs, so the refusals
            # stopped firing: with two processes on B's current session, one
            # declaring itself A and one nameless, the contradictory A row was
            # excluded as "already named" and the nameless one keyed B alone —
            # this census INVENTED a live B where the canonical owner refuses
            # both. Same helper, same seat, a different population, opposite
            # answer.
            own = [p for p in procs if p.get("seat") == name]
            # THE ROSTER THIS PASS ALREADY READ, handed down rather than
            # re-read. `_session_joined`'s declared-name rung resolves a rename
            # alias against it, and a second read here would let the file move
            # under one sweep — the same one-reading law `roster_current_sids`
            # states for the sids.
            hits, why = orcaadopt._session_joined(name, procs, current_sid,
                                                  own, rows=roster)
        except Exception as e:              # noqa: BLE001 — one seat never blinds the rest
            refusals.append((name, "%s (%s)"
                             % (seats_common._seat_label(name), e)))
            continue
        if hits:
            seats.add(name)
        elif why:
            # LAUNDERED AT THE SINK. Every other roster read in this module is
            # internal-matching-only — a key selected to look something up and
            # never printed — but this refusal becomes operator prose in
            # `_live_seats`'s `blind`, so the roster key it names is emitted
            # and goes through the display launder like any other.
            refusals.append((name, "%s (%s)"
                             % (seats_common._seat_label(name), why)))
    return seats, refusals


def findings(rep):
    """[(level, text)] — what a human should be told, or []."""
    out = []
    for row in rep["seats"]:
        if row.get("error"):
            continue
        if row["config_ok"] is False:
            out.append(("CONFIG", "%s: proxy config drifted — %s. This is the "
                        "class that left ds4pro without the keepalive for a "
                        "week; a long non-streaming pass can return an empty "
                        "HTTP 200." % (row["seat"], "; ".join(row["drift"]))))
        if row.get("probe") == "EMPTY200":
            out.append(("EMPTY200", "%s: %s. This is the fault that makes a bad "
                        "key indistinguishable from a dropped completion — the "
                        "exact shape reported for a week."
                        % (row["seat"], row["probe_detail"])))
        elif row.get("probe") in ("down", "hang"):
            out.append((row["probe"].upper(),
                        "%s: proxy %s — %s" % (row["seat"], row["probe"],
                                               row["probe_detail"])))
        # "unknown" and "idle" are deliberately NOT findings: an unreadable
        # log is not evidence of health OR of failure, and saying either
        # would be the lie. They still show in the report lines.
        log = row.get("log")
        ts = row.get("turn_state")
        taking_turns = row.get("probe") == "healthy" and ts == "ok"
        if log == "blip":
            out.append(("BLIP", "%s: proxy refusal blip (%s) — below the named "
                        "starvation threshold%s. Trust the structured fields; the "
                        "prose never upgrades a transient code cluster into a "
                        "credential or liveness verdict."
                        % (row["seat"], row["log_detail"],
                           "; probe=healthy and turn=ok say the seat is taking turns"
                           if taking_turns else "")))
        elif log == "streak" and ts == "starved":
            out.append(("STARVED", "%s: proxy refusing (%s) — sustained refusal "
                        "plus turn=starved establishes starvation; the status-code "
                        "class above is the cause evidence. Grok's two-day INERT "
                        "402 wall (2026-07-27→29) made this transport class legible "
                        "while the watch's seat list was one family short. It does "
                        "not establish the remedy or deployment intent: Grok is "
                        "deliberately parked pending CLI proxy cursor support, not "
                        "waiting on a payment." % (row["seat"], row["log_detail"])))
        elif log == "streak":
            out.append(("REFUSAL", "%s: sustained proxy refusal (%s), but the "
                        "liveness fields do not establish STARVED%s. Report the "
                        "disagreement; do not replace measured fields with a "
                        "scarier sentence."
                        % (row["seat"], row["log_detail"],
                           "; probe=healthy and turn=ok say the seat is taking turns"
                           if taking_turns else "")))
        # AN UNREADABLE REGISTER IS LOUD, AND IT IS ITS OWN LINE.
        # Reported from the DECISION the door made, independently of the turn
        # verdict, so the two facts cannot mask each other: a blind pane
        # census and a corrupt register can be true at once, and whichever won
        # the ladder, the other still reaches whoever is reading. It
        # authorizes nothing and prescribes nothing but a look — the finding
        # takes no pane read and makes no claim about what the composer holds.
        # UNONBOARDED IS A FINDING, unlike idle and unknown, and the
        # difference is that this one PRESCRIBES. A recorded unproven submit
        # with no turn since IS evidence, it names the remedy, and leaving it
        # as a report line would rebuild the failure the rung exists for — a
        # state visible to whoever ran the launch and reaching nobody who
        # could act.
        if ts == "unonboarded":
            out.append(("UNONBOARDED", "%s: %s" % (
                row["seat"], row.get("turn_evidence") or
                "the latest launch recorded its onboarding brief as NOT "
                "PROVEN submitted")))
        if row.get("onboarding_unreadable"):
            out.append(("ONBOARDING-UNREADABLE",
                        "%s: the spawn register's onboarding record is present "
                        "and this build cannot read it (%s), so whether the "
                        "LATEST launch briefed this seat is UNKNOWN — not "
                        "proven, and not known-unsent. This reads one record "
                        "and makes no claim about earlier launches. Read the "
                        "pane (`helm seat composers`) rather than inferring "
                        "either"
                        % (row["seat"], row.get("onboarding_reason")
                           or "reason not recorded")))
        # The fused turn verdict. HUNG is the composed NAME the fleet lacked
        # both times on 2026-07-29 — every probe answered its own question
        # correctly and a human still had to hand-count transcript rows. The
        # old HANG? question survives ONLY where the fuse could not verdict
        # (no fuse ran, or an input was unreadable): thinking / compact-needed
        # / fresh / starved are ANSWERS to that question, and asking it again
        # over an answer is the false alarm the in-flight rung exists to kill.
        ts = row.get("turn_state")
        if ts == "hung":
            # CONSULT THE PANE-TAIL CLASSIFIER BEFORE PRESCRIBING A RESTART.
            # turn_state=="hung" is the fuse answering "the turn loop is dead"
            # — true, and incomplete: a seat frozen at a plan-approval prompt
            # has a dead turn loop AND is BLOCKED_ON_HUMAN, and the
            # resume/reseed this branch used to prescribe DISCARDS the pending
            # plan (fleet #124, owner ruling: complying destroys work). The
            # classifier (helm/seat.py seat_liveness) already reads the same
            # pane tail and mints BLOCKED_ON_HUMAN with the plan path attached
            # — reuse it, never a second pane-tail reader. seat.py documents
            # BLOCKED_ON_HUMAN and RUNNING as mutually exclusive, so this is a
            # clean lookup, not a heuristic race.
            # PURE REDUCTION: the liveness was sampled once in health() and
            # stored on the row. Calling seat_liveness here made findings()
            # impure — the verdict depended on when it was asked (r1).
            liv = row.get("liveness")
            if liv and liv.get("state") == "WALLED":
                why = liv.get("blocked_on") or \
                    "a measured availability wall is present"
                out.append(("WALLED", "%s: pane LIVE, turn loop quiet, but %s. "
                            "The measured wall explains the refusal; remediation "
                            "cannot be derived from this record. helm proxywatch "
                            "safely remeasures current state."
                            % (row["seat"], why)))
            elif liv and liv.get("state") == "BLOCKED_ON_HUMAN":
                where = liv.get("blocked_on")
                out.append(("BLOCKED-HUMAN", "%s: pane LIVE, turn loop DEAD, "
                            "but the seat is BLOCKED ON A HUMAN, not hung — a "
                            "plan/permission prompt is the only thing between "
                            "it and continuing, and no restart helps. %s "
                            "RESUME/RESEED WOULD DISCARD THE PENDING PLAN — do "
                            "not. ANSWER THE PROMPT: open the pane and approve "
                            "or reject %s." % (
                                row["seat"],
                                ("Plan: %s. " % where) if where else "",
                                ("the plan at %s" % where) if where
                                else "the pending prompt")))
            elif liv and liv.get("state") == "UNKNOWN":
                # an unreadable classifier is not a clean pane: HUNG may
                # stand, but the prescription must NAME the uncertainty rather
                # than assert resume over a state nobody read.
                # MED (r1): an unreadable classifier means nobody
                # cleared this seat of being human-blocked — so the resume
                # command does NOT go out here either. Name the blindness and
                # the read-first gate in WORDS; the executable resume lives
                # only on the cleared control below, where the classifier
                # actually answered.
                out.append(("HUNG", "%s: pane LIVE, turn loop DEAD — %s. The "
                            "pane-tail classifier could not confirm this is "
                            "not a human-blocked prompt (evidence=%s), and a "
                            "restart here may discard pending work — READ THE "
                            "PANE FIRST and judge what you see; no restart is "
                            "prescribed over an unread state."
                            % (row["seat"], row.get("turn_evidence"),
                               liv.get("evidence") or "?")))
            elif liv and liv.get("state") == "LIVE":
                # LIVE is PROCESS evidence only (#141 r2, review blocker): a
                # named process exists and NO pane tail was classified, so
                # nobody has cleared this seat of being human-blocked. Same
                # read-first gate as UNKNOWN, in LIVE's own words — the
                # evidence is strictly weaker than the restart it would
                # otherwise authorize, and a resume over a pending plan
                # discards the plan.
                out.append(("HUNG", "%s: pane LIVE, turn loop DEAD — %s. "
                            "Liveness is PROCESS-ONLY (evidence=%s): a "
                            "process exists but no pane tail was read, so "
                            "this could be a human-blocked prompt — READ THE "
                            "PANE FIRST and judge what you see; no restart "
                            "is prescribed on process evidence alone."
                            % (row["seat"], row.get("turn_evidence"),
                               liv.get("evidence") or "?")))
            else:
                out.append(("HUNG", "%s: pane LIVE, turn loop DEAD — %s. "
                            "Measured twice 2026-07-29 on the primary codex "
                            "seat: four probes each correctly refused to call "
                            "it a failure and nothing could name it. Action: "
                            "`helm seat resume %s`; if two resumes do not "
                            "stick, reseed — that night two resumes did not "
                            "stick and a fresh reseed did."
                            % (row["seat"], row.get("turn_evidence"),
                               row["seat"])))
        elif row["hang_candidate"] and ts in (None, "hung-unknown"):
            out.append(("HANG?", "%s: pane is LIVE but no semantic transcript "
                        "entry completed in %dm. Not a verdict — an agent may "
                        "be thinking — but this is the class the drop watchdog "
                        "cannot see, because a turn that never completes "
                        "writes no row.%s"
                        % (row["seat"], row["transcript_age_s"] // 60,
                           " (%s)" % row["turn_evidence"]
                           if ts == "hung-unknown" else "")))
    for family, upstream in sorted((rep.get("upstream") or {}).items()):
        state = upstream.get("state")
        if not upstream.get("dark") and state not in _UPSTREAM_DARK:
            continue
        # "upstream" IS A CLAIM ABOUT ORIGIN AND IT IS FALSE FOR OUR OWN
        # STATES. A PROXY-COOLDOWN means helm's proxy refused before any
        # request left the box, so calling it an upstream state tells the
        # reader the provider is down when nothing reached a provider. The
        # word is dropped exactly where `dark_origin` says the failure is
        # ours; every other state keeps it.
        #
        # AND THE CORRECTION MUST NOT OVERSHOOT INTO THE OPPOSITE FALSEHOOD.
        # "helm's own proxy is refusing" answers WHERE, and a reader deciding
        # whether a provider is down takes it as an answer to WHETHER — the
        # same error with the sign flipped. A local cooldown is frequently the
        # MIRROR of an upstream wall: the proxy cools a credential precisely
        # BECAUSE the provider refused it, so a long local horizon is evidence
        # of a provider limit rather than of a local one. The sentence
        # therefore names where the refusal happened, says outright that it
        # measures nothing about the provider, and points at the evidence,
        # which is the only field in this row that separates the two.
        #
        # THE `since` CLAUSE BELONGS TO THE STATE. With a full clause in front
        # of it the line reads "no request reached a provider since
        # <timestamp>" — which says the box sent nothing since then, not that
        # the state has held since then. Every branch keeps `%s since %s`
        # adjacent for that reason.
        origin = dark_origin(state)
        latched = (" (latched; current evidence is unreadable)"
                   if state == "UNKNOWN" else "")
        named = state if origin != DARK_UNTYPED else "upstream %s" % state
        head = "%s: %s%s since %s via %s" % (
            family, named, latched, upstream.get("since") or "?",
            upstream.get("seat") or "no representative")
        if state == _PROXY_LOCAL_403:
            # No cooldown was measured, so the cooldown-mirror gloss below
            # would invent one; the repair of a refusal we minted is ours.
            gloss = ("HELM'S OWN PROXY refused this itself before any request "
                     "left the box. That names WHERE this refusal happened "
                     "and measures NOTHING about the provider; the repair is "
                     "ours: read the proxy's stated reason, fix it, restart "
                     "and probe. The proxy's stated reason: ")
        elif origin == DARK_OURS:
            gloss = ("HELM'S OWN PROXY refused before any request left the "
                     "box. That names WHERE this refusal happened and measures "
                     "NOTHING about the provider — a local cooldown is often "
                     "the mirror of an upstream wall. The evidence is what "
                     "separates them: ")
        elif origin == DARK_OUR_VALIDATION:
            gloss = ("HELM'S OWN VALIDATION rejected the reply, so this names "
                     "the measurement rather than an origin: ")
        else:
            gloss = ""
        out.append(("FAMILY-DARK", "%s — %s%s"
                    % (head, gloss, upstream.get("detail") or "")))
    # A STALE COOLDOWN IS A FINDING: the proxy is refusing on a credential
    # that measurably has headroom, which is the fault the owner sees as
    # "6 cooling down" over a pool he just reset.
    for row in rep.get("codex_cooldown") or ():
        out.append((STALE_COOLDOWN, _cooldown_text(row)))
    return out


def fingerprint(rep, include_upstream=True):
    """A digest of the HEALTH STATE, so a pass speaks only when it moves.

    Deliberately excludes transcript AGE and the timestamp: those change every
    pass by construction, and folding them in would make every pass a "change"
    and every message noise. What is latched is the shape — config ok, hang
    candidacy, and the drop-latch instant.
    """
    import hashlib
    parts = []
    for row in sorted(rep["seats"], key=lambda r: r["seat"]):
        # turn_state is latched like every other rung: ok→hung must post once,
        # and hung→ok (the resume worked) once. Ages/counters stay out — the
        # VERDICT is the shape, its inputs move every pass by construction.
        liv = row.get("liveness") or {}
        parts.append("%s|%s|%s|%s|%s|%s|%s|%s" % (row["seat"], row["config_ok"],
                                               row["hang_candidate"],
                                               row["alerted_at"],
                                               row.get("probe"),
                                               row.get("log"),
                                               row.get("turn_state"),
                                               liv.get("state")))
    if include_upstream:
        for family, upstream in sorted((rep.get("upstream") or {}).items()):
            state = upstream.get("state")
            bucket = "DARK" if upstream.get("dark") or \
                _named_upstream_dark(state) else state
            parts.append("upstream|%s|%s" % (family, bucket))
            for seat_name, seat_record in sorted(
                    (upstream.get("seats") or {}).items()):
                seat_state = seat_record.get("state")
                seat_bucket = "DARK-STALE" if \
                    seat_state == _PROXY_COOLDOWN and \
                    seat_record.get("falsification_due") is True else \
                    "DARK" if seat_record.get("dark") or \
                    _named_upstream_dark(seat_state) else seat_state
                parts.append("upstream-seat|%s|%s|%s" %
                             (family, seat_name, seat_bucket))
    return hashlib.blake2b("\n".join(parts).encode("utf-8"),
                           digest_size=8).hexdigest()


def changed(rep):
    """(bool, previous_fingerprint) — has the health state moved since the last
    pass? A first-ever run counts as changed ONLY if it has findings, so
    installing the timer on a healthy fleet does not announce itself."""
    try:
        with pk.open_regular(_state_path(), encoding="utf-8") as f:
            prior = json.load(f) or {}
    except (OSError, ValueError):
        prior = {}
    prev = prior.get("fingerprint")
    if prev is None:
        return bool(findings(rep)), None
    # Pre-UPSTREAM records have no family latch. Compare their old-format digest
    # once, then record() silently upgrades the state to the new fingerprint.
    fp = fingerprint(rep, include_upstream="upstream" in prior)
    return fp != prev, prev


def upstream_transitions(rep, prior=None):
    """Family dark/recovered edges; cause changes inside darkness stay quiet."""
    if prior is None:
        prior, err = _read_watch_state()
        if err:
            prior = {}
    before_all = (prior.get("upstream") or {}) if isinstance(prior, dict) else {}
    out = []
    for family, current in sorted((rep.get("upstream") or {}).items()):
        before = before_all.get(family) or {}
        was_dark = before.get("dark") is True or \
            _named_upstream_dark(before.get("state"))
        is_dark = current.get("dark") is True or \
            _named_upstream_dark(current.get("state"))
        if is_dark and not was_dark:
            out.append({"kind": "family-dark", "family": family,
                        "state": current.get("state"),
                        "since": current.get("since"),
                        "detail": current.get("detail")})
        elif not is_dark and was_dark:
            out.append({"kind": "family-recovered", "family": family,
                        "state": current.get("state"),
                        "since": current.get("since"),
                        "detail": current.get("detail")})
    return out


def _transition_lines(transitions):
    return ["%s %s: %s since %s — %s" %
            ("🚨 FAMILY-DARK" if row["kind"] == "family-dark" else
             "✅ FAMILY-RECOVERED", row["family"], row["state"],
             row.get("since") or "?", row.get("detail") or "")
            for row in transitions]


def _merge_transitions(*groups):
    """Stable union for the durable per-channel transition queues: a stuck
    pending edge and this pass's fresh one must never duplicate, and order of
    first appearance is delivery order."""
    out, seen = [], set()
    for group in groups:
        for transition in group or ():
            key = (transition.get("kind"), transition.get("family"),
                   transition.get("state"), transition.get("since"))
            if key not in seen:
                seen.add(key)
                out.append(transition)
    return out


def _owner_push(transitions):
    """One optional phone push for the whole transition batch -> delivered.

    The owner is GUI-first and off-host: family dark/recovered edges are the
    two he must not learn about by running anything. HELM_NTFY_TOPIC absent is
    a deliberate opt-out and acknowledges the batch; a failed POST returns
    False so the caller's outbox keeps the edges for at-least-once retry.

    THE TRANSPORT IS `notify.owner_push` AND NOTHING HERE, since 2026-08-03:
    this function used to carry its own copy of the endpoint resolution, the
    timeout and the fail-open receipt, and so did the store's graduation push.
    A third caller (beacons' reachability alarm) made the duplication a bug —
    the owner has ONE phone channel and every alarm composes onto it.
    """
    if not transitions:
        return True
    from . import notify
    body = "helm proxywatch: " + "; ".join(
        "%s %s %s" % (row["family"],
                      "dark" if row["kind"] == "family-dark" else "recovered",
                      row["state"]) for row in transitions)
    return notify.owner_push(body, title="helm upstream transition",
                             receipt=("proxywatch.notify_failed", "upstream"))


def _codexbudget():
    """Lazy import. The budget reader pulls in providers and codexhomes, and
    proxywatch is imported by hot read paths (seat_usability's join) that must
    not pay for that."""
    from . import codexbudget
    return codexbudget


def _codex_budget_pass():
    """The pooled codex budget read on THIS pass: the rows, [] for a pool that
    is KNOWN EMPTY, or None when the pool could not be read at all.

    AN EMPTY POOL IS A MEASUREMENT AND IT RETIRES THE OLD REFUSAL (task/2480
    R3). THE FAILURE: `helm codex unpool` of the last pooled account leaves the
    last all-capped snapshot on disk, and a pass that returns before the writer
    keeps that snapshot refusing every codex dispatch until it ages past
    `codexbudget.GATE_MAX_AGE_S` — up to an hour of refusals about accounts
    that are provably gone, ended by the CLOCK rather than by any measurement.
    The pass holds the fact that ends it: it enumerated the pool and found
    nothing. So an empty census PUBLISHES itself, and the next send reads a
    snapshot that says "nothing pooled" and admits.

    NONE IS NOT EMPTY. None means this pass could not read the pool — the
    directory would not enumerate, or the reader broke — and every consumer
    treats that as "say nothing, latch nothing, leave the standing snapshot
    exactly as it is". An account that WAS read and could not be understood is
    neither: it comes back as an `unknown` ROW and is printed as one.

    Never raises: a budget reader that throws must not take the fleet's
    watchdog down with it."""
    try:
        mod = _codexbudget()
        accounts, census = mod.pool_census()
        if census == mod.CENSUS_UNREAD:
            print("helm proxywatch: codex pool census UNREAD — the standing "
                  "budget snapshot is left exactly as it was", file=sys.stderr)
            return None
        if not accounts:
            mod.write_snapshot([], census=census)
            return []
        # THE ROWS THIS PASS ALREADY READ, not a second enumeration (task/2480
        # R5). Asking the directory again between the census and the probe
        # opens a window in which the two readings disagree, and the second
        # one carries no census to say so.
        return mod.pool_budget(accounts=accounts)
    except Exception as e:                  # noqa: BLE001
        print("helm proxywatch: codex pool budget unread (%s)" % e,
              file=sys.stderr)
        return None


def _burnflags():
    """Lazy, for `_codexbudget`'s reason: the fold pulls in the budget reader
    and the seat catalog, and proxywatch is imported by hot read paths that
    must not pay for that."""
    from . import burnflags
    return burnflags


def _moneyread():
    from . import moneyread
    return moneyread


def _money_pass(now):
    """Run the generic reader table: every catalog-declared reader, once per
    credential a minted seat holds (moneyread; the vendor reads are GETs that
    spend no quota)."""
    try:
        return _moneyread().refresh(now=now)
    except Exception as e:                  # noqa: BLE001
        print("helm proxywatch: money readers unread (%s)" % e, file=sys.stderr)
        return None


def _usage_then_money(rep):
    """Persist this pass's proxy events before any ledger reader consumes them."""
    usage = _proxy_usage_pass()
    rep["proxy_usage"] = usage
    rep["money_readers"] = _money_pass(rep["ts"])
    return usage


def _codex_pace_pass(rep):
    """The codex pace and runway on THIS pass -> the fold's runway input, or
    None when the pass did not read the pool (task/2984).

    IT RUNS AFTER THE METER AND BEFORE THE FOLD. It reads the budget rows
    probed on this pass and the ledger rows the meter just made durable, and
    the fold that follows steps the codex money axis on its verdict. It also
    speaks, once, for an account whose week is spent: one room line and one
    phone push per wall, latched in its own ledger.

    Never raises: a reporting rung must not take the watchdog down."""
    if rep.get("codex_budget") is None:
        return None
    try:
        from . import codexpace
        return codexpace.watch_pass(rep["codex_budget"], now=rep["ts"])
    except Exception as e:                  # noqa: BLE001
        print("helm proxywatch: codex pace unread (%s)" % e, file=sys.stderr)
        return None


def _burn_flags_pass(rep, prior=None):
    """Fold THIS PASS'S OWN READINGS into the burn-flag snapshot -> the flags.

    IT RIDES THIS PASS FOR THE BUDGET'S REASON AND SHARES ITS CLOCK. The pooled
    budget, the upstream family states and the native usage log are all read
    here already; folding them anywhere else would mint a second reading of the
    same world at a second instant, and two surfaces disagreeing about one file
    is the failure the imported staleness bound exists to prevent.

    Never raises: the fold is a reporting rung and must not take the fleet's
    watchdog down with it."""
    mod = _burnflags()
    try:
        budget = _codexbudget()
        history = mod.usage_history()
        rows, latest = mod.anthropic_money_rows(history, now=rep["ts"])
        generic = _moneyread().inputs(
            rep.get("money_readers"), now=rep["ts"], max_age_s=mod.max_age_s(),
            error="watch-pass-unreadable" if rep.get("money_readers") is None else None)
        money = dict(generic["money"])
        stamps = dict(generic["money_measured_at"])
        fresh = dict(generic["money_fresh"])
        if rep.get("codex_budget") is not None:
            money["codex"] = rep["codex_budget"]
            stamps["codex"] = rep["ts"]
            fresh["codex"] = True
        if rows:
            money[mod.NATIVE_FAMILY] = rows
            stamps[mod.NATIVE_FAMILY] = latest
            fresh[mod.NATIVE_FAMILY] = bool(
                latest and (rep["ts"] - latest) <= mod.max_age_s())
        inputs = {"ceiling": budget.ceiling_pct(), "money": money,
                  "money_measured_at": stamps, "money_fresh": fresh,
                  "upstream": rep.get("upstream") or {},
                  "anthropic_history": history,
                  # THE RESET RUNG'S OWN ROWS, from this pass and never a
                  # probe: a wall an owner asset can lift in one pass is
                  # dated from the pass, not from the weekly reset.
                  "reset_credits": {"codex": rep.get("codex_resets")},
                  # The fold runs before the later durable record composition,
                  # so it joins the owner horizon by the SAME rule and against
                  # the SAME prior records `record` composes with (task/2935).
                  "vendor_resets": read_vendor_resets()[0],
                  "upstream_before": (prior.get("upstream") or {})
                  if isinstance(prior, dict) else {},
                  # THE CODEX RUNWAY this pass read (task/2984): the fold's
                  # step table is its only effect on a colour.
                  "runway": ({"codex": rep["codex_runway"]}
                             if rep.get("codex_runway") else {}),
                  "declarations": mod.read_declarations()}
        payload = mod.fold(inputs, now=rep["ts"])
        mod.write_snapshot(inputs=inputs, now=rep["ts"])
        return payload
    except Exception as e:                  # noqa: BLE001
        print("helm proxywatch: burn flags unread (%s)" % e, file=sys.stderr)
        return None


def _burn_flags_board(payload, prior_key):
    """Put the ONE LINE on the integration board when the READING changes ->
    the key that reading latches on, or None when there was no reading.

    ONE SCALAR STRING under one key. The board's own writer refuses to replace
    a structured value with a scalar, and a list here would have written once
    and raised on every pass after — a row that freezes at the first reading
    while looking live is worse than no row. `--new` is a statement that the
    console generator has been coordinated with, not a convenience.

    THE LATCH IS ON THE READING, NOT ON THE RENDERED LINE. The line carries a
    countdown, so a latch comparing lines fires on every tick of the clock and
    writes the row every pass — the exact per-pass noise it was added to
    prevent. A pass with NO payload latches NOTHING (None), so the prior key
    is carried forward rather than replaced by the not-measured line."""
    mod = _burnflags()
    if not payload:
        return None
    key = mod.line_key(payload)
    if key == prior_key:
        return key
    try:
        from . import board
        board.set_value("burn_flags", mod.line(payload), allow_new=True,
                        seat="proxywatch")
    except Exception as e:                  # noqa: BLE001
        print("helm proxywatch: burn-flag board row unwritten (%s)" % e,
              file=sys.stderr)
    return key



def _codexresets():
    """Lazy import. The reset-credit rung pulls in the budget reader and the
    pool census, and this module is imported by hot read paths that must not
    pay for either."""
    from . import codexresets
    return codexresets


def _codex_reset_pass(budget, sidecars=None):
    """Spend an earned rate-limit reset credit on any pooled codex account
    whose WEEKLY window this pass measured at zero remaining; [] when nothing
    qualified, None when the rung could not run or the budget was not read.

    THE RUNG RIDES THE READING THE PASS JUST TOOK. `budget` is the rows this
    pass probed a moment ago, so the decision is made on a reading of age zero
    rather than on the snapshot's hour-wide bound — the tightest freshness
    this policy can ever have, taken at the one place it is free.

    A NON-READING IS NOT A ZERO. `None` means the pool was not read, and the
    policy's whole point is that an unread account is never an exhausted one,
    so the rung does not run at all.

    Never raises, never moves `rc`: the watch's exit codes mean "faults found"
    and "watchdog broken", and a credential the vendor would not talk to about
    reset credits is neither."""
    if budget is None:
        return None
    mod = _codexresets()
    try:
        cooling = None if sidecars is None \
            else codex_cooling_by_file(sidecars, pool_sidecar_seats())
        # THE LIVE VENDOR IS NAMED HERE, AND AT THE CLI DOOR, AND NOWHERE
        # ELSE. The module refuses a pass carrying no base url, so a caller
        # that forgets one cannot reach the endpoint by inheriting a default.
        return mod.reset_pass(budget, reading_age_s=0.0, cooling=cooling,
                              url_base=mod.live_base_url())
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        # THROUGH THE REDACTOR. This is the one string in this feature built
        # from an exception nobody inspected, and the module's own law is that
        # every note it mints is scrubbed first; an exception raised inside a
        # vendor client can carry the url, the header or the token itself.
        print("helm proxywatch: the codex reset-credit rung failed (%s) — "
              "proxy health below is unaffected" % mod.safe_error(e),
              file=sys.stderr)
        return None


def record(rep, pending_chat=None, pending_ntfy=None, prior_state=None):
    """Persist watch state + alert outbox atomically -> the persisted state
    (a non-empty dict) if written, else False.

    The outbox is the transactional boundary: persistence happens BEFORE
    external delivery. A failed initial write therefore sends nothing (fail
    closed); a failed acknowledgement write preserves the pending item for
    at-least-once retry rather than losing the edge.

    The persisted state is returned so a later write IN THE SAME PASS can
    name it as its `prior_state`. The family records are composed against the
    prior, and a transition identity is minted whenever the prior's verdict
    differs; an acknowledgement write composed against the pass's ORIGINAL
    prior would mint a second identity for the one change the first write
    already recorded.

    pending_chat is a list of message bodies still waiting for chat.post
    delivery. pending_ntfy is the family-transition batch still waiting for
    the HELM_NTFY_TOPIC owner push. Both are reduced (acknowledged) after
    their respective channel succeeds, independently — a dead phone must not
    re-post to a healthy room, nor the reverse.
    """
    p = _state_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        from . import pk
        if prior_state is None:
            prior_state, prior_err = _read_watch_state()
            if prior_err:
                prior_state = {}
        before_all = (prior_state.get("upstream") or {}) \
            if isinstance(prior_state, dict) else {}
        upstream, vendor_err = _compose_upstream_records(rep, before_all)
        if vendor_err:
            print("helm proxywatch: %s (horizons withheld this pass)"
                  % vendor_err, file=sys.stderr)
        runtime = {}
        for key, proof in (rep.get("proxy_runtime") or {}).items():
            sanitized = _sanitized_proxy_proof(proof)
            if sanitized and sanitized.get("observed_at") == rep.get("ts"):
                runtime[str(key)] = sanitized
        # THE CODEX BUDGET LATCH. Only the DECISION is persisted, because the
        # dedup is on the decision (the numbers move every pass); the rows
        # themselves live in the budget snapshot the dispatch gate reads. A
        # pass that did not read the pool CARRIES THE PRIOR LATCH FORWARD
        # rather than clearing it — dropping it would re-post FAMILY-BUDGET-LOW
        # on the next pass that does read.
        budget_latch = rep.get("codex_budget_decision")
        if budget_latch is None:
            budget_latch = (prior_state.get("codex_budget") or {}).get("decision") \
                if isinstance(prior_state.get("codex_budget"), dict) else None
        # THE BURN-FLAG LATCH, and the same law: only the COLOURS and the one
        # rendered line are persisted, because the dedup is on the colour and
        # the flags themselves live in the snapshot every reader already has.
        # A pass that did not fold CARRIES THE PRIOR LATCH FORWARD.
        prior_flags = prior_state.get("burn_flags") \
            if isinstance(prior_state.get("burn_flags"), dict) else {}
        # AN EMPTY LATCH IS A LATCH, and `or` cannot tell it from an absent
        # one: a fold that read no family answers {} and a first board key can
        # be any string, so the `or` carried a STALE colour map forward over a
        # pass that had measured the world and found nothing in it — and the
        # room then never hears the next crossing. Only None means "this pass
        # did not fold", exactly as the budget and reset latches above.
        flag_colours = rep.get("burn_flag_colours")
        if flag_colours is None:
            flag_colours = prior_flags.get("colours")
        flag_line = rep.get("burn_flag_line")
        if flag_line is None:
            flag_line = prior_flags.get("line")
        flag_latch = {"colours": flag_colours, "line": flag_line}
        # THE RESET RUNG'S BLOCKED LATCH, on the same terms and for the same
        # reason: a pass that did not run the rung (None) keeps the prior,
        # while a pass that ran it and found nothing blocked writes the EMPTY
        # latch, which is how the room hears the wall again if it comes back.
        reset_latch = rep.get("codex_resets_blocked")
        if reset_latch is None:
            reset_latch = (prior_state.get("codex_resets") or {}).get("blocked") \
                if isinstance(prior_state.get("codex_resets"), dict) else None
        payload = {"fingerprint": fingerprint(rep), "ts": rep["ts"],
                   "upstream": upstream, "proxy_runtime": runtime,
                   "codex_budget": {"decision": budget_latch},
                   "burn_flags": flag_latch,
                   "codex_resets": {"blocked": reset_latch},
                   # the sidecar meter's per-seat rows from this pass (None
                   # when the rung did not run), so a FAILED-PERSIST pass is
                   # on disk beside the health it rode with
                   "proxy_usage": rep.get("proxy_usage"),
                   "pending_chat": list(pending_chat or ()),
                   "pending_ntfy": list(pending_ntfy or ())}
        encoded = json.dumps(payload, sort_keys=True)
        with delivery_state_guard():
            # Backup first: once the primary names a new dark episode, damage to
            # that primary must still recover the same latch. Both files carry
            # the identical canonical payload; there is no second transition log.
            pk.atomic_write(_backup_state_path(), encoded)
            pk.atomic_write(p, encoded)
        for error in _stamp_proxy_runtime_proofs(runtime):
            print("helm proxywatch: runtime stamp withheld — %s" % error,
                  file=sys.stderr)
        return payload
    except Exception:                       # noqa: BLE001 — caller fails closed
        return False


def report_lines(rep):
    out, represented = [], set()
    for row in sorted(rep["seats"], key=lambda r: r["seat"]):
        if row.get("family"):
            represented.add(row["family"])
        if row.get("error"):
            out.append("  %-9s %s" % (row["seat"], row["error"]))
            continue
        age = row["transcript_age_s"]
        write_age = row.get("write_age_s")
        upstream = row.get("upstream") or "-"
        if row.get("upstream_since"):
            upstream += "(since %s)" % row["upstream_since"]
        paused = beacon_paused((rep.get("upstream") or {}).get(row.get("family")))
        status_401 = row.get("log_status_401") or {}
        provenance = " 401[tail=%s]=helm-marked:%d/unmarked:%d" % (
            _tail_scope_label(status_401.get("scope")),
            status_401.get("helm_marked", 0),
            status_401.get("unmarked", 0)) if status_401.get("total") else ""
        # DELIVERY, labelling ITS OWN tail rather than borrowing the 401
        # census's: a 200 that carried no text is not a completed turn, and
        # until this clause existed every rung on this row read one as healthy.
        delivery = _empty_turn_clause(row.get("log_empty_turns"))
        # DEMAND, rendered only when it says something. `active` 0 is the
        # expected state on nearly every seat nearly always, and nine lines of
        # `fanout=0` every fifteen minutes is exactly the attention-budget
        # spam that trains a reader to skim the line. Silence therefore MEANS
        # zero — which is only safe because UNKNOWN prints EXPLICITLY: a seat
        # whose instance dir could not be read says so rather than looking
        # like a quiet one.
        fan = row.get("fanout") or {}
        fanout = " fanout=UNKNOWN" if fan.get("active") is None \
            else (" fanout=%d" % fan["active"] if fan["active"] else "")
        out.append("  %-9s probe=%s upstream=%s config=%s log=%s pane=%s "
                   "semantic=%s write=%s turn=%s%s%s%s%s"
                   % (row["seat"], row.get("probe") or "-", upstream,
                      "OK" if row["config_ok"] else "DRIFTED",
                      row.get("log") or "-",
                      "live" if row.get("pane_live") else "-",
                      "%dm" % (age // 60) if age is not None else "none",
                      "%dm" % (write_age // 60)
                      if write_age is not None else "none",
                      row.get("turn_state") or "-", fanout,
                      provenance + delivery,
                      " beacon=PAUSED-CRED-WALL" if paused else "",
                      "  HANG?" if row["hang_candidate"]
                      and row.get("turn_state") in (None, "hung-unknown")
                      else ""))
    for family, upstream in sorted((rep.get("upstream") or {}).items()):
        if family in represented:
            continue
        state = upstream.get("state") or "-"
        since = "(since %s)" % upstream["since"] \
            if upstream.get("since") else ""
        out.append("  family:%-9s upstream=%s%s%s — %s" %
                   (family, state, since,
                    " beacon=PAUSED-CRED-WALL"
                    if beacon_paused(upstream) else "",
                    upstream.get("detail") or ""))
    # PER-ACCOUNT CODEX WINDOWS, when this pass read them. Silence means the
    # pool was not read on this pass, never that it is empty — an account whose
    # token was rejected prints `unknown`, explicitly, and never 0%.
    if rep.get("codex_budget"):
        out.extend(_codexbudget().budget_lines(rep["codex_budget"]))
    # THE RESET-CREDIT RUNG PRINTS ONLY WHEN IT DID SOMETHING, or wanted to
    # and could not. Its ordinary answer is that no weekly window is spent,
    # and a line saying so on every pass would bury the one that matters.
    if rep.get("codex_resets"):
        out.extend(_codexresets().pass_lines(rep["codex_resets"]))
    out.extend(empty_turn_lines(rep))
    for level, text in findings(rep):
        out.append("  %s %s" % (level, text))
    return out


def timer_units(interval=INTERVAL_S):
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    # WorkingDirectory is DERIVED, never a literal — seat.py's rebind-unit
    # law, learned here the same way: an operator path baked into a tracked
    # template is a never-track needle in history and a machine identity this
    # repo cannot carry (the scan refused this very lane's commit on it).
    # work.find_root folds a lane worktree back to the SHARED checkout, so a
    # persistent unit never captures a disposable worktree as its cwd.
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, "helm-proxywatch.service"),
            _SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-proxywatch.timer"),
            _TIMER % {"interval": interval})


def ensure_timer(interval=INTERVAL_S):
    """(ok, detail) — install + enable the cadence."""
    import shutil
    from . import pk
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm proxywatch --post` "
                       "from another scheduler")
    spath, service, tpath, timer = timer_units(interval)
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", "helm-proxywatch.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "timer enabled every %ds (%s)" % (interval, tpath)


_USAGE = """usage: helm proxywatch [--post] [--json] [--force] [--install-timer]

  Do the cli-proxy fixes still hold for EVERY minted seat and represented family,
  and is each pane alive? Six rungs, a fuse, and one actuator:

    CRED-FOLLOW (the one thing this pass WRITES, and it writes only into
            helm's own pool) makes the codex proxy pool carry whichever
            account orca has ACTIVE, read daemon-free from orca's provenance
            file, so a switch the owner makes in orca reaches the proxy within
            ONE pass instead of by hand. It imports an account ONCE and never
            over one already pooled, reports a disabled member instead of
            flipping it, never writes into orca's files, and never moves this
            command's exit code — see `helm seat cred-follow`.

    CONFIG  the invariants `helm seat doctor` censuses — chiefly
            nonstream-keepalive-interval, whose absence lets a long
            non-streaming pass return an EMPTY HTTP 200. Nothing ran that
            census on a schedule; ds4pro went a week without the setting.
    DROPS   already covered by helm-silent-drop every 90s. This reads its
            LATCH, so a NEW alert since the last pass is surfaced.
    HANGS   a LIVE pane whose transcript has not grown. The drop watchdog
            cannot see this — a turn that never completes writes no row.
    PROBE   an invalid-key request proves the local listener and auth path answer
            without reading or sending a real credential.
    LOG     trailing failures in the seat's OWN proxy.log tail, classified as
            401/403 AUTH, 402 BILLING/quota, 429 RATE-LIMITED, 5xx UPSTREAM, or
            mixed UNKNOWN; a 403/429 body naming a usage quota is QUOTA-WALL.
            Every 401 within the displayed bounded proxy.log tail
            is also accounted by provenance: helm_canary=1 is Helm-marked (not
            authenticated origin); absence is unmarked, never guessed external or
            provider. The report names the observed byte scope and whether older
            rows were truncated. Three failures
            establish a cluster; only a cluster spanning >=60s is a STREAK. A
            shorter burst is BLIP, never STARVED. STARVED additionally requires
            turn=starved. Grok's two-day 402 wall remains visible, but does not
            imply payment is the remedy: that seat is deliberately parked pending
            CLI proxy cursor support.
    DELIVERY reads what each completed agent request actually CARRIED, from the
            fork's stream_v1/empty_turn/empty_recovery tokens on the same tail.
            A 200 that delivered no bytes is not a completed turn. `empty[...]`
            counts recovery verdicts and the upstream generations they spent —
            an exhausted turn is three codex generations for nothing, and codex
            holds the fleet's smallest window, so this is a SPEND reading.
            Three answers that never share a value: silence is a measured zero;
            UNINSTRUMENTED means agent traffic reached a proxy that emits no
            such tokens, so an empty turn there is unobservable, not absent;
            and `zero-delivery:N(cause-UNREADABLE)` is a committed message_stop
            with no text and no tools that the instrument never flagged. That
            last one is REPORTED AND NOT DIAGNOSED: a real empty generation and
            a wrong api key answered HTTP 200 with an empty completion are
            indistinguishable from the access row.
    UPSTREAM starts one canary at a deterministic family primary; every request
            is capped at eight tokens. A dark primary is confirmed, then every
            healthy sibling corroborates it and may also confirm a completed
            failure once after 3s. A client timeout is never retried.
    TURN    the fused verdict over a stale live pane. Staleness is the age of
            the last COMPLETED SEMANTIC transcript entry — never mtime, which
            retry bookkeeping kept fresh through the 65-minute 2026-07-30
            wall. HUNG only when ALL of: context below the compact bar (at it
            = COMPACT-NEEDED, autocompact's class), not freshly spawned
            (starting is not hung), and MEASURED pending work — an open turn,
            queued transcript work, a churning socket, or open dispatch rows.
            A held socket with nothing written since the prompt is THINKING (a
            long generation is not hung); beside fresh nonsemantic writes it
            is churn. Every pending census measuring EMPTY = IDLE — out of
            work, not stuck. Any unreadable input = HUNG-UNKNOWN, never HUNG.
            The codex seat hung TWICE on 2026-07-29 with every probe correct
            and no name for the state; this is the name.

  The default cadence is fifteen minutes (the installed timer's 900s). --post
  reports only on change, through a
  durable per-channel outbox: one fleet post, plus one optional HELM_NTFY_TOPIC
  phone push for family dark/recovered edges. Acknowledgement is per channel
  and at-least-once — a failed delivery is retried next pass rather than lost.
  --force posts a report without inventing an edge. --install-timer wires the
  cadence (neither of those two runs the cred-follow rung: they configure, they
  do not supervise).
"""


def _transition_id(before, state, dark):
    """The family record's TRANSITION IDENTITY: the prior record's when its
    state and dark latch are both unchanged, and a freshly minted one on
    every change of either -- or when the prior record carries none.

    `since` cannot be that identity. It is a one-second stamp, so a verdict
    changed and restored inside one second reads identical to the one it
    replaced, and an act that ran under the change in between would be
    accounted clean. A minted identity is never reused, so the restored
    verdict reads differently from the one before the change, while a pass
    that rewrites an unchanged verdict keeps it (resumeturn
    `_pause_authority`).

    The identity is only as stable as the prior each write names. A posting
    pass writes twice; its acknowledgement write names the pass's first write
    as its prior (`cmd_proxywatch`), so one change mints one identity."""
    prior = before.get("transition_id") if isinstance(before, dict) else None
    if isinstance(prior, str) and prior and before.get("state") == state \
            and (before.get("dark") is True) == bool(dark):
        return prior
    return os.urandom(8).hex()


#: The fields one family's quota wall and its reset horizon ride in. The join
#: REPLACES all of them together, so a horizon a later pass no longer
#: supports cannot survive beside the wall it described.
WALL_RESET_KEYS = ("quota_wall", "resets_at_ms", "reset_kind", "reset_source",
                   "reset_recorded_at")


def _future_ms(value, now_ms):
    return type(value) is int and value > now_ms


def _family_quota_wall(row, before, now_ms):
    """The quota wall this family record stands on this pass, or None.

    THE LOCAL COOLDOWN THAT FOLLOWS A QUOTA WALL IS ITS MIRROR. The proxy
    cools a credential the vendor refused (measured: kimi's 403 was followed
    by "1 cooling down (reset in 29m32s)"), so the passes between two
    upstream refusals read PROXY-COOLDOWN. The quota verdict is held across
    those passes and only those: any other state ends it, PROXY-LOCAL-403
    included, because nothing measured ties a 403 our proxy minted to the
    vendor's refusal. An AUTH-UNAVAILABLE hold whose own reset has passed
    holds nothing."""
    state = row.get("state")
    current = quota_wall(row)
    if state == "AUTH-UNAVAILABLE" and type(row.get("resets_at_ms")) is int \
            and row["resets_at_ms"] <= now_ms:
        current = None
    legacy = quota_wall(dict(before, state=state)) \
        if state == _PROXY_COOLDOWN and before else None
    if type(before.get("resets_at_ms")) is int \
            and before["resets_at_ms"] <= now_ms:
        legacy = None
    return state if state in _UPSTREAM_QUOTA else current or legacy


def _wall_members(row, before, now_ms):
    """The walled members whose resets make up the family's: every seat
    that carries the wall, or else the family row itself (falling back to
    the prior record's own measured canary reset, which the local cooldown
    that mirrors the wall does not re-measure)."""
    seats = [seat for _name, seat in sorted((row.get("seats") or {}).items())
             if isinstance(seat, dict) and quota_wall(seat)]
    if seats:
        return seats
    if not _future_ms(row.get("resets_at_ms"), now_ms) \
            and before.get("reset_source") == "canary" \
            and _future_ms(before.get("resets_at_ms"), now_ms):
        return [before]
    return [row]


def owner_reset_fields(row, owner, now, before=None):
    """THE ONE RULE for a family's quota wall and vendor reset -> the
    `WALL_RESET_KEYS` fields to write; an absent reset is UNKNOWN.

    Used by `_compose_upstream_records` (the durable record) and by the
    posting pass's burn fold (`burnflags.fold`, through `join_owner_reset`),
    so the two can never disagree (task/2935, the integrator's ruling):

      - a PAST owner horizon is dropped, at this pass's own instant;
      - a measured member reset beats the owner entry only when it was
        observed AFTER the owner recorded it — both are readings of the
        vendor clock and the newer one wins, so an owner entry replaces a
        stale measured one;
      - a FUTURE owner horizon fills a member whose reset is unknown;
      - the family reset is the EARLIEST member reset, and is UNKNOWN only
        when some member's reset is unknown and no future owner horizon
        covers it.

    A family with no quota wall carries a future owner horizon as it is."""
    before = before if isinstance(before, dict) else {}
    now_ms = now * 1000
    horizon = owner if isinstance(owner, dict) \
        and _future_ms(owner.get("resets_at_ms"), now_ms) else None
    owned = {} if horizon is None else {
        "resets_at_ms": horizon["resets_at_ms"],
        "reset_kind": horizon.get("reset_kind") or _VENDOR_RESET_KIND,
        "reset_source": horizon.get("reset_source") or _VENDOR_RESET_SOURCE,
        "reset_recorded_at": horizon.get("recorded_at")}
    wall = _family_quota_wall(row, before, now_ms)
    if not wall:
        return owned
    recorded = _parse_timestamp(horizon.get("recorded_at")) if horizon else None
    opens = []
    for member in _wall_members(row, before, now_ms):
        ms = member.get("resets_at_ms")
        seen = _parse_timestamp(member.get("wall_observed_at")
                                or member.get("since"))
        if _future_ms(ms, now_ms) and (horizon is None or seen is not None and (
                recorded is None or seen > recorded)):
            opens.append((ms, {"resets_at_ms": ms,
                               "reset_kind": _VENDOR_RESET_KIND,
                               "reset_source": member.get("reset_source")
                               or "canary"}))
        elif horizon is not None:
            opens.append((horizon["resets_at_ms"], owned))
        else:
            return {"quota_wall": wall}
    return dict(min(opens, key=lambda item: item[0])[1], quota_wall=wall)


def join_owner_reset(record, owner, now, before=None):
    """A copy of one family record whose wall and reset fields are the ones
    `owner_reset_fields` decides — the fold's door to the same rule."""
    out = {key: value for key, value in record.items()
           if key not in WALL_RESET_KEYS}
    out.update(owner_reset_fields(record, owner, now, before=before))
    return out


def _compose_upstream_records(rep, before_all):
    """The per-pass family-record composition, extracted so the owner-horizon
    join is testable without driving a full posting pass.

    THE OWNER HORIZON JOINS HERE, AT THE ONE WRITE. The pass rewrites each
    family record from measured state only, so an owner-entered vendor reset
    that lived anywhere in the record would be dropped on the next pass —
    the producer gap task/45 names. The join reads the durable config (never
    the prior record, which would let a cleared horizon haunt the next
    pass), and only a FINITE ms rides. The provenance fields travel with it
    so the render never mistakes the owner's page value for a measured
    provider header. The rule that joins it is `owner_reset_fields`, the one
    the posting pass's fold also asks. A config that cannot be read leaves
    the record UNCHANGED on this axis — UNKNOWN, never an invented absence.

    Returns (records, vendor_err): the error is RETURNED, not swallowed —
    a corrupt config silently reading as "no horizons recorded" is the
    false-clean shape (a review's P1); the caller decides whether the
    diagnostic surfaces (the timer pass prints it) without ever writing it
    into a record a render would read as fact."""
    vendor_resets, vendor_err = read_vendor_resets()
    upstream = {}
    for family, row in (rep.get("upstream") or {}).items():
        state = row.get("state")
        dark = row.get("dark") is True or _named_upstream_dark(state)
        before = before_all.get(family) or {}
        last_dark = state if state in _UPSTREAM_DARK else \
            before.get("last_dark_state")
        if not last_dark and before.get("state") in _UPSTREAM_DARK:
            last_dark = before["state"]
        upstream[family] = {"state": state, "since": row.get("since"),
                            "dark": dark,
                            "transition_id": _transition_id(before, state,
                                                            dark)}
        seats = row.get("seats")
        if isinstance(seats, dict):
            upstream[family]["seats"] = {
                name: {key: value for key, value in record.items()
                       if key not in ("detail", "ms")}
                for name, record in sorted(seats.items())
                if isinstance(name, str) and isinstance(record, dict)
            }
        if type(row.get("falsification_bar_s")) is int \
                and row["falsification_bar_s"] > 0:
            upstream[family]["falsification_bar_s"] = \
                row["falsification_bar_s"]
        if last_dark:
            upstream[family]["last_dark_state"] = last_dark
        # The quota wall and its reset horizon: ONE helper, shared with the
        # posting pass's fold, so the durable record and the flag `can_spend`
        # reads can never name two different resets (task/2935).
        stamp = rep.get("ts")
        now = stamp if isinstance(stamp, (int, float)) else \
            _parse_timestamp(stamp) or time.time()
        upstream[family].update(owner_reset_fields(
            row, vendor_resets.get(family), now, before=before))
    return upstream, vendor_err


def _codexhomes():
    """The module that owns pool-side codex auth reads and the ONE pool
    write. Imported lazily — this watch runs on hosts with no codex pool."""
    from . import codexhomes
    return codexhomes


def _cred_follow_pass():
    """Make the codex pool follow orca's ACTIVE account for this pass, or None
    when the rung could not run. A watch NEVER raises (this module's standing
    law): a credential rung that broke must not cost the fleet its liveness
    report."""
    try:
        return _codexhomes().cred_follow(apply=True)
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        print("helm proxywatch: the orca cred-follow rung failed (%s: %s) — "
              "proxy health below is unaffected"
              % (e.__class__.__name__, e), file=sys.stderr)
        return None


def _proxy_usage_pass():
    """Pop every sidecar's usage queue into the proxy-usage ledger on THIS
    pass (task/2522), or None when the reader could not run. The queue is
    in-memory and pruned by age, so the fifteen-minute timer is what keeps
    the meter continuous; the rows ride the report and never move `rc`. A
    watch never raises."""
    try:
        from . import proxy_usage
        return proxy_usage.snapshot()
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        print("helm proxywatch: the proxy-usage rung failed (%s: %s) — "
              "proxy health below is unaffected"
              % (e.__class__.__name__, e), file=sys.stderr)
        return None


def _proxy_usage_mod():
    """Lazy import: the meter pulls in the seat facade, and this module is
    imported by hot read paths that must not pay for it."""
    from . import proxy_usage
    return proxy_usage


# THE STALE-COOLDOWN ROW. A sidecar learns a codex 429 from the
# vendor's own `resets_in_seconds` and then TRUSTS it for the whole window —
# seven days on a weekly wall — while the owner resets the account by hand
# upstream. Measured: nine un-bounced sidecars held two Team logins
# "cooling down (reset in 4h41m)" for hours after helm's own usage probe read
# both at 4% used, allowed=true. The sidecar's belief lives in its memory
# (no cooldown is persisted, nothing is re-probed at start), so the one
# instrument that can see the contradiction is this pass, which reads BOTH
# sides: the proxy's roster with its `next_retry_after` and the vendor's
# usage window per credential. The row names the credential, the proxy's
# belief and the measured headroom — a row, NEVER a bounce: which sidecar to
# restart, or which file to park-and-unpark, is the integrator's act.
AUTH_FILES_PATH = "/v0/management/auth-files"
STALE_COOLDOWN = "STALE-COOLDOWN"


def _go_time_epoch(stamp):
    """Epoch seconds off a Go RFC 3339 stamp (`next_retry_after`), or None.
    Go prints nanoseconds and a bare `Z` for UTC; Python reads at most
    microseconds, so the tail is cut and `Z` is spelt out."""
    from datetime import datetime
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", stamp.strip())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _auth_files(port, secret, timeout=2.0):
    """(entries, err) — one sidecar's in-memory credential roster, the
    management `auth-files` list. `secret` goes into the header and nowhere
    else; an error names the HTTP class, never the header."""
    import urllib.error
    import urllib.request
    url = "http://127.0.0.1:%d%s" % (port, AUTH_FILES_PATH)
    req = urllib.request.Request(url, headers={"X-Management-Key": secret})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return None, "HTTP %d" % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "management call failed: %s" % (
            getattr(exc, "reason", None) or exc.__class__.__name__)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "management response is not JSON"
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return None, "management response carries no files list"
    return [e for e in files if isinstance(e, dict)], None


def codex_cooldown_rows(sidecars, budget, now=None):
    """The contradictions, as rows: every codex credential a sidecar holds in
    cooldown (`next_retry_after` in the future) while THIS pass measured
    headroom on it (budget state `ok` or `near`: allowed, no window at 100).

    `sidecars` is [(seat, port, entries)] — the auth-files roster per
    sidecar; `budget` the `codexbudget.pool_budget` rows of the same pass,
    joined by pool FILE NAME (the roster's `name` is the pool file), email
    as the fallback. A credential the budget could not read (`unknown`) is
    never a contradiction: an unread window contradicts nothing. Pure —
    every arm on it runs without a socket."""
    now = time.time() if now is None else now
    by_file, by_email = {}, {}
    for r in budget or ():
        for f in r.get("files") or ():
            by_file[f] = r
        if r.get("email"):
            by_email.setdefault(r["email"], r)
    out = []
    for seat, port, entries in sidecars or ():
        for e in entries or ():
            if (e.get("type") or e.get("provider")) != "codex":
                continue
            reset = _go_time_epoch(e.get("next_retry_after"))
            if reset is None or reset <= now:
                continue
            name = e.get("name") or ""
            measured = by_file.get(name) or by_email.get(e.get("email"))
            if measured is None or measured.get("state") not in ("ok", "near"):
                continue
            binding = measured.get("binding") or {}
            out.append({"seat": seat, "port": port, "file": name,
                        "email": e.get("email") or measured.get("email"),
                        "proxy_reset_at": reset,
                        "proxy_reset_in_s": int(reset - now),
                        "proxy_status": e.get("status"),
                        "measured_state": measured["state"],
                        "measured_pct": binding.get("used_percent"),
                        "measured_window": binding.get("label"),
                        "measured_reset_in_s": binding.get("reset_after_seconds")})
    return out


def sidecar_rosters():
    """[(seat, port, entries)] — every reachable proxy sidecar's in-memory
    credential roster.

    ONE ROSTER READ PER PASS, SHARED. Two rungs now ask the same question of
    the same sidecars (the stale-cooldown row, and the reset-credit policy's
    "was this credential actually refused?"), and two reads a few milliseconds
    apart are two different beliefs about one fleet: a rung opening its own
    sockets could see a credential cooling that the rung beside it saw
    healthy, and the pass would report both. So the pass reads once and hands
    the same rows to both.

    The management secret is read per sidecar, used for the one call, and
    dropped; it reaches no caller."""
    from . import seat as seatmod
    from .seat_paths import MGMT_SECRET_FILE, _mgmt_secret_present
    pu = _proxy_usage_mod()
    sidecars = []
    for _family, seat, proxy_home in pu.instances():
        port, err = pu._port_of(os.path.join(proxy_home, "config.yaml"))
        if err or not _mgmt_secret_present(proxy_home) \
                or not seatmod._port_open(port):
            continue
        with open(os.path.join(proxy_home, MGMT_SECRET_FILE),
                  encoding="utf-8") as f:
            secret = f.read().strip()
        entries, err = _auth_files(port, secret)
        del secret
        if err:
            continue
        sidecars.append((seat, port, entries))
    return sidecars


def pool_sidecar_seats():
    """The seats whose sidecar LOADS THE CODEX POOL — the set of names in
    `sidecar_rosters()` whose rosters may speak for a pooled credential.

    WHICH SIDECAR SERVES THE POOL IS A QUESTION THE CONFIG ANSWERS, and the
    tree already answers it this way: `codexhomes.pool_dir()` is the ONE
    auth-dir CLIProxyAPI hot-reloads for the pool, and `helm seat` refuses to
    mint a codex family whose declared auth-dir is not that directory
    (`seat_provision`). So a sidecar serves the pool exactly when its own
    config.yaml declares that dir — read with the same `auth-dir` scalar the
    sidecar meter reads (`proxy_usage.pool_accounts`), never inferred from a
    seat NAME.

    TOTAL, and empty is the safe answer: an unreadable config or an
    unenumerable seat list yields a smaller scope, which can only drop 429
    evidence and make the reset policy refuse more. Measured on this fleet:
    eighteen sidecars, twelve of them loading the pool, and one codex sidecar
    with an auth dir of its own whose cooling credential is claimed by no
    pooled account."""
    try:
        # INSIDE THE GUARD, because the import itself is one of the ways this
        # can fail: `codexhomes` pulls in the seat facade at module top, and
        # a function documented TOTAL cannot have a line above its try.
        from . import codexhomes
        want = os.path.abspath(codexhomes.pool_dir())
        pu = _proxy_usage_mod()
        out = set()
        for _family, seat, proxy_home in pu.instances():
            declared, err = pu._top_scalar(
                os.path.join(proxy_home, "config.yaml"), "auth-dir")
            if err or not isinstance(declared, str) or not declared:
                continue
            if os.path.abspath(declared) == want:
                out.add(seat)
        return out
    except Exception:                       # noqa: BLE001 — never a failure
        return set()


def codex_cooling_by_file(sidecars, pool_seats, now=None):
    """{pool file name: the LATEST instant a sidecar will retry it} for every
    codex credential a POOL-SERVING sidecar holds in cooldown at `now`.

    `pool_seats` is `pool_sidecar_seats()` — the seats whose sidecar loads the
    pool — and it is REQUIRED, with None meaning "the scope is unknown" and
    yielding no evidence at all. A roster entry names its credential by the
    pool FILE and by nothing else, so an entry from a proxy holding its own
    auth dir can carry a file name identical to a pooled credential's while
    being a different credential: unscoped, that entry supplies a 429 for an
    account it has no relation to. Measured on this fleet: of six cooling
    codex files, five belong to pooled credentials and one is held by the
    single codex sidecar with an auth dir of its own and is claimed by no
    pooled account.

    WHAT THIS IS EVIDENCE OF, EXACTLY. A CLIProxyAPI sidecar puts a credential
    in cooldown when the vendor answers it 429, and it sets `next_retry_after`
    from the vendor's own `resets_in_seconds`. So an entry here is helm's own
    record that THIS credential — named by the pool file, which is the
    credential's own name — was refused for rate limiting, and the instant it
    names is the instant the vendor said the offending window reopens.

    WHAT IT IS NOT. It is a BELIEF, held in one process's memory, never
    re-probed, and trusted for the whole window — which is the defect the
    STALE-COOLDOWN row exists to report. It carries NO timestamp for when the
    429 arrived, so nothing here can say how old the belief is. A consumer
    acting on it therefore owes its own corroboration; the reset-credit
    policy's is that the instant this names must be the instant the vendor's
    usage endpoint independently gave for the WEEKLY window of that account,
    and for no other window of it.

    Pure: every arm on it runs without a socket."""
    now = time.time() if now is None else now
    scope = set(pool_seats or ())
    out = {}
    for _seat, _port, entries in sidecars or ():
        if _seat not in scope:
            continue
        for e in entries or ():
            if (e.get("type") or e.get("provider")) != "codex":
                continue
            reset = _go_time_epoch(e.get("next_retry_after"))
            name = e.get("name") or ""
            if reset is None or reset <= now or not name:
                continue
            out[name] = max(reset, out.get(name, 0.0))
    return out


def _sidecar_rosters_pass():
    """`sidecar_rosters()` for the timed pass — [] when the read broke, so a
    management endpoint that will not answer costs the pass its cooldown rows
    and its 429 evidence, never the pass itself."""
    try:
        return sidecar_rosters()
    except Exception as e:                  # noqa: BLE001 — never a failure
        print("helm proxywatch: the sidecar credential roster is unread (%s: "
              "%s)" % (e.__class__.__name__, e), file=sys.stderr)
        return []


def _codex_cooldown_pass(budget, sidecars=None):
    """The stale-cooldown rows for THIS pass, reading every proxy sidecar's
    roster beside the budget rows the pass already probed; [] when nothing
    contradicts, None when the rung could not act or the budget was not
    read. A watch never raises."""
    if budget is None:
        return None
    try:
        return codex_cooldown_rows(
            sidecar_rosters() if sidecars is None else sidecars, budget)
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        print("helm proxywatch: the stale-cooldown rung failed (%s: %s) — "
              "proxy health below is unaffected"
              % (e.__class__.__name__, e), file=sys.stderr)
        return None


def _cooldown_text(row):
    """One row's sentence: the credential, the proxy's belief, the measured
    headroom WITH ITS POLARITY (`used N%`, never a bare percentage)."""
    pct = row.get("measured_pct")
    measured = ("used %.0f%% of %s" % (pct, row.get("measured_window") or "?")
                if isinstance(pct, (int, float)) else "state %s" % row["measured_state"])
    secs = row.get("measured_reset_in_s")
    if isinstance(secs, (int, float)):
        measured += " (resets in %dh%02dm)" % (secs // 3600, (secs % 3600) // 60)
    belief = row["proxy_reset_in_s"]
    return ("%s holds %s (%s) cooling for another %dh%02dm, until %s — "
            "measured %s: the proxy's belief is stale, the credential has "
            "headroom. A sidecar restart or a disabled=true/false round trip "
            "of the pool file clears it; this pass only reports."
            % (row["seat"], row["file"] or "?", row.get("email") or "?",
               belief // 3600, (belief % 3600) // 60,
               time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(row["proxy_reset_at"])), measured))


def codex_cooldown_lines(rows):
    """The stale-cooldown rows as report lines; nothing for [] or None."""
    return ["  %s %s" % (STALE_COOLDOWN, _cooldown_text(r)) for r in (rows or ())]


def proxy_usage_lines(rows):
    """The meter's NON-READ rows as their own report lines: a sidecar that
    could not be read, and — its own state, never folded into READ — a pass
    whose append the ledger refused (FAILED-PERSIST, with the lost count,
    which is 0 when only the read marker was refused). None (the rung did
    not run) and an all-READ pass print nothing."""
    from . import proxy_usage
    return ["helm proxywatch: proxy-usage %s %s"
            % (r["seat"], proxy_usage.row_note(r))
            for r in (rows or ()) if r.get("status") != proxy_usage.READ]


def cmd_proxywatch(args):
    """proxywatch — cli-proxy fix invariants + seat liveness, change-latched."""
    import sys
    args = list(args or [])
    from .cli import guard_tail
    # THE OWNER'S VENDOR-RESET DOOR (task/45), dispatched BEFORE guard_tail:
    # its tail is positional (set <family> <when>), and guard_tail's whole
    # contract is that remaining tokens after a matched subverb are FLAGS —
    # so routing the door after it makes set/show/clear unreachable rc=2
    # (a review's P1: `vendor-reset show` refused as junk before the branch
    # ever ran).
    if args and args[0] == "vendor-reset":
        sub = args[1] if len(args) > 1 else ""
        if sub == "set" and len(args) == 4:
            family, when = args[2], args[3]
            ms, perr = _parse_reset_instant(when)
            if perr:
                print("helm proxywatch vendor-reset: %s" % perr,
                      file=sys.stderr)
                return 2
            # VALIDATE AGAINST THE CONFIGURED FAMILIES, never a live health()
            # pass: the live pass rejects a configured family whose seat is
            # temporarily absent (a reproduction showed the kimi family
            # refused while the seat was down) and spends authenticated
            # canaries just to answer "does this family exist". The
            # registry plus the config's own history is the closed set.
            from . import seat as _seatmod
            known, _kerr = read_vendor_resets()
            families = set(_seatmod.FAMILIES) | set(known)
            if family not in families:
                print("helm proxywatch vendor-reset: %r is not a family this "
                      "watcher has ever seen (%s); refusing to record a "
                      "horizon no card will render"
                      % (family, ", ".join(sorted(families)) or "none"),
                      file=sys.stderr)
                return 2
            rec, werr = write_vendor_reset(family, ms)
            if werr:
                print("helm proxywatch vendor-reset: %s" % werr,
                      file=sys.stderr)
                return 2
            # SCOPE IS FAMILY, NAMED PLAINLY AT WRITE: the record joins by
            # family, so one value shows on EVERY seat of the family —
            # correct when they share one provider account (tonight's
            # directive: one codex seat), misleading if two seats of one
            # family ever hold DIFFERENT vendor accounts with different
            # windows (a review's P2). The message says what was recorded so
            # the owner cannot mistake it for a per-seat fact.
            print("helm proxywatch: %s FAMILY vendor reset recorded at %s "
                  "(owner-entered, %s) — it will show on every %s seat; "
                  "if they ever hold separate vendor accounts, set the "
                  "window of the SHARED one or say so per family"
                  % (family, _iso_from_ms(ms), rec["recorded_at"], family))
            return 0
        if sub == "clear" and len(args) == 3:
            removed, cerr = clear_vendor_reset(args[2])
            if cerr:
                print("helm proxywatch vendor-reset: %s" % cerr,
                      file=sys.stderr)
                return 2
            print("helm proxywatch: %s vendor reset %s"
                  % (args[2], "cleared" if removed else "was not set"))
            return 0
        if sub == "show" and len(args) == 2:
            table, serr = read_vendor_resets()
            if serr:
                print("helm proxywatch vendor-reset: %s" % serr,
                      file=sys.stderr)
                return 2
            if not table:
                print("helm proxywatch: no owner-entered vendor resets")
                return 0
            for fam, rec in sorted(table.items()):
                print("%s resets at %s (owner-entered %s, recorded %s)"
                      % (fam, _iso_from_ms(rec["resets_at_ms"]),
                         rec["reset_kind"], rec["recorded_at"]))
            return 0
        print("usage: helm proxywatch vendor-reset set <family> "
              "<ISO-8601|epoch-ms> | show | clear <family>", file=sys.stderr)
        return 2
    rc = guard_tail("helm proxywatch", args,
                    flags=("--post", "--json", "--force", "--install-timer"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    # DISTINCT EXIT CODES, the same law the unit template states: 0 clean,
    # 1 = the watch RAN and FOUND FAULTS, 2 = the watchdog ITSELF failed
    # (guard_tail's bad-invocation 2 is the same species). Both watch units
    # declare SuccessExitStatus=1, so systemctl shows red only for a genuinely
    # broken watchdog instead of for every fault it correctly found.
    if "--install-timer" in args:
        ok, detail = ensure_timer()
        print("helm proxywatch: %s%s" % ("" if ok else "timer NOT installed — ",
                                         detail),
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2

    # THE ORCA CRED-FOLLOW RUNG, ahead of the health read (task/2478). The
    # owner switches the codex account in orca and the proxy keeps serving the
    # account he switched away from — a fault every liveness rung below calls
    # healthy, because the proxy IS healthy; it is holding the wrong
    # credential. This pass already runs on a timer, so putting the rung here
    # is what makes the pool learn a switch within ONE pass. It rides the
    # output and never touches `rc`: the watch's exit codes mean "faults
    # found" and "watchdog broken", and a host with no orca is neither.
    follow = _cred_follow_pass()
    usage = None

    def render(rep, hits):
        if "--json" in args:
            print(json.dumps({"report": rep, "findings": hits,
                              "cred_follow": follow, "proxy_usage": usage},
                             indent=2, sort_keys=True))
        else:
            if follow is not None \
                    and _codexhomes().cred_follow_noteworthy(follow):
                for line in _codexhomes().cred_follow_lines(follow):
                    print(line)
            for line in proxy_usage_lines(usage):
                print(line)
            for line in report_lines(rep):
                print(line)

    posting = "--post" in args or "--force" in args
    if not posting:
        # THE BARE VERB STAYS A STATUS READ. It spends no vendor round-trip and
        # writes no projection, which is why the projection manifest names
        # `helm proxywatch --post` (NOT this branch) as the rebuild recipe for
        # codex-pool-budget — see the projection's own `rebuild` field and the
        # side effect it discloses (task/2480 R7).
        rep = health()
        hits = findings(rep)
        render(rep, hits)
        return 1 if hits else 0

    # The existing outbox lock is also the authenticated-canary single-flight.
    # Health must run INSIDE it: otherwise two timer passes can both spend family
    # tokens before either reaches the protected state decision.
    state_dir = os.path.dirname(_state_path())
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    lock_path = os.path.join(state_dir, ".proxywatch.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError:
        os.close(lock_fd)
        print("helm proxywatch: could not acquire outbox lock", file=sys.stderr)
        return 2
    try:
        prior, read_err = _read_watch_state()
        if read_err:
            # Refuse BEFORE health: a corrupt outbox must not be treated as an
            # empty latch, and no authenticated canary is spent on a pass that
            # cannot persist its transition.
            print("helm proxywatch: %s (%s)" % (read_err, _state_path()),
                  file=sys.stderr)
            return 2
        rep = health(prior_state=prior)
        # THE CODEX POOL BUDGET RIDES THIS PASS (task/2480). It is read HERE
        # rather than inside `health` because it costs one vendor round-trip
        # per pooled account, and `health` is called by readers that must stay
        # free (seat_usability's join runs it with include_probe=False on every
        # dispatch). The snapshot this writes is what the dispatch budget gate
        # reads, so this timed pass is what keeps routing honest.
        rep["codex_budget"] = _codex_budget_pass()
        # THE STALE-COOLDOWN ROW RIDES THE BUDGET IT JUST PROBED: the same
        # per-credential window, put beside each sidecar's own belief.
        # ONE ROSTER READ, TWO RUNGS: the stale-cooldown row and the reset
        # rung both ask the sidecars what they believe about a credential, so
        # the pass reads that once and both reason over the same answer.
        rosters = _sidecar_rosters_pass()
        rep["codex_cooldown"] = _codex_cooldown_pass(rep["codex_budget"],
                                                     sidecars=rosters)
        # THE RESET-CREDIT RUNG RIDES THE SAME READING (task/2734, owner: a
        # pooled cred that hits zero on its weekly window should spend an
        # available reset credit rather than wait for a human to notice). It
        # is the only rung here that can spend an owner asset, so it acts on
        # the freshest reading that exists — the rows probed two lines above —
        # and on nothing else.
        rep["codex_resets"] = _codex_reset_pass(rep["codex_budget"],
                                                sidecars=rosters)
        # THE SIDECAR METER RIDES THE SAME PASS (task/2522): a pop is the one
        # read that empties the queue, so it belongs to the timed writer, not
        # to the free status read above. Generic readers run AFTER that pop is
        # durable, or their ledger is one queue behind this pass.
        usage = _usage_then_money(rep)
        rep["codex_runway"] = _codex_pace_pass(rep)
        hits = findings(rep)
        render(rep, hits)
        moved, _prev = changed(rep)
        transitions = upstream_transitions(rep, prior=prior)
        pending_chat = list(prior.get("pending_chat") or ())
        # A FAILED-PERSIST ROW REACHES THE OWNER CHANNEL THROUGH THE SAME
        # OUTBOX AS EVERY OTHER ALERT (task/2522 round 3): the very lines the
        # text render printed go into `pending_chat`, which `record` writes
        # before delivery (so the outbox carries them if the post fails) and
        # `chat.post` delivers. Every such pass posts, because every such
        # pass left the ledger behind the sidecar — records lost, or only
        # the read marker refused (lost 0), which the header says as such —
        # there is no latch to wait out. UNREADABLE rows stay on stdout: a
        # sidecar awaiting its respawn is a state, not a loss.
        persist_failed = [r for r in (usage or ())
                          if r.get("status") == _proxy_usage_mod().FAILED_PERSIST]
        if persist_failed:
            # THE HEADER SAYS WHAT THE ROWS SAY: records are LOST only when a
            # row's `lost` is above zero; a refused read marker over fully
            # persisted records is named as exactly that.
            lost = sum(r.get("lost") or 0 for r in persist_failed)
            pending_chat.append("proxywatch: sidecar meter FAILED-PERSIST — %s\n%s" % (
                "popped records the ledger refused are LOST" if lost
                else "the read marker was not persisted, 0 record(s) lost",
                "\n".join(proxy_usage_lines(persist_failed))))
        # The phone channel queues EDGES, deduplicated against any stuck batch
        # from a failed prior push — at-least-once, same as chat.
        pending_ntfy = _merge_transitions(prior.get("pending_ntfy"),
                                          transitions)
        if moved or "--force" in args:
            edges = _transition_lines(transitions)
            body = ("proxywatch: proxy health CHANGED\n" +
                    ("\n".join(edges) + "\n" if edges else "") +
                    "\n".join(report_lines(rep)) +
                    ("\n(no findings — no alerting state)" if not hits else ""))
            pending_chat.append(body)
        # FAMILY-BUDGET-LOW IS LATCHED ON THE COLOUR, NOT ON THE NUMBERS.
        # Percentages move every pass by construction; what the room needs to
        # hear once is that a FAMILY CROSSED. The announcer is the burn-flag
        # fold rather than the codex budget's own, because two announcers on
        # one tag is two chances to tell the room a different thing.
        # THE POOL'S OWN DECISION STAYS PERSISTED AND STAYS TRUE. Only the
        # ANNOUNCER moved; the latch field is read by surfaces outside this
        # repository, so it keeps its meaning and is computed from the same
        # verdict as before rather than left to carry a stale value forward.
        if rep.get("codex_budget") is not None:
            rep["codex_budget_decision"] = _codexbudget().verdict(
                rep["codex_budget"])["decision"]
        rep["burn_flags"] = _burn_flags_pass(rep, prior=prior)
        prior_flags = (prior.get("burn_flags")
                       if isinstance(prior.get("burn_flags"), dict) else {})
        # A PASS THAT DID NOT FOLD LATCHES NOTHING. `watch_notice` answers an
        # EMPTY colour map for an absent fold, which is a real latch value
        # meaning "folded, no family" — writing it here would clear the latch
        # on a pass that never read, and re-post every crossing next pass.
        if rep["burn_flags"] is None:
            flag_body, rep["burn_flag_colours"] = None, None
        else:
            flag_body, rep["burn_flag_colours"] = _burnflags().watch_notice(
                rep["burn_flags"].get("families"), prior_flags.get("colours"))
        if flag_body:
            pending_chat.append(flag_body)
        rep["burn_flag_line"] = _burn_flags_board(
            rep["burn_flags"], prior_flags.get("line"))
        # ONE ROOM LINE FOR THE RESET RUNG, AND ONLY WHEN IT ACTED OR WANTED
        # TO. Spending an owner asset is never silent, and neither is wanting
        # to and being unable; an ordinary pass where no weekly window is
        # spent says nothing at all. THE STATE HALF IS LATCHED, on which
        # accounts are in which state and why: a wall a reset cannot lift
        # stands for days, this pass runs every fifteen minutes, and an
        # unlatched line would say the same thing a hundred times a day. An
        # attempt that spent or may have spent a credit is an EVENT and is
        # never latched — the policy's cool-down bounds how often one can
        # happen. An attempt the vendor REFUSED is a state, not an event: the
        # cool-down exempts it, so nothing else bounds its rate.
        # The import is reached only when the rung produced rows, so a host
        # whose reset module could not be imported at all does not meet it a
        # second time here, where there is no rung guard around it.
        if rep.get("codex_resets"):
            reset_body, reset_blocked = _codexresets().watch_notice(
                rep["codex_resets"],
                ((prior.get("codex_resets") or {}).get("blocked")
                 if isinstance(prior.get("codex_resets"), dict) else None))
            if reset_body:
                pending_chat.append(reset_body)
            rep["codex_resets_blocked"] = reset_blocked
        # ONE ROOM LINE PER POOL WALL PER SEAT (helm/poolwall.py). The pass
        # is the observer; the claim is taken here, once, and the line rides
        # the same durable outbox as every other alert, so a failed post is
        # retried next cadence and never re-claimed.
        from . import poolwall
        pending_chat.extend(body for _seat, body in poolwall.announcements(
            sorted({row["seat"] for row in rep["seats"]
                    if row.get("family") and not row.get("error")})))
        # Persist both channel outboxes and the family latch before delivery.
        written = record(rep, pending_chat=pending_chat,
                         pending_ntfy=pending_ntfy, prior_state=prior)
        if not written:
            print("helm proxywatch: state write failed; no alerts delivered",
                  file=sys.stderr)
            return 2
        remaining_chat = list(pending_chat)
        try:
            from . import chat
            for body in pending_chat:
                chat.post(body, who="proxywatch", room="helm")
                remaining_chat.pop(0)
        except Exception as ex:          # noqa: BLE001 — retry next cadence
            print("helm proxywatch: chat post failed (%s)" % ex,
                  file=sys.stderr)
        if _owner_push(pending_ntfy):
            pending_ntfy = []
        # THE ACKNOWLEDGEMENT REPLACES THE FIRST WRITE, so the first write is
        # its prior. Composed against the pass's original prior, a pass that
        # changed a family's verdict would mint that change's transition
        # identity twice, and an act that captured the first write while the
        # new verdict was already in force would read a change it never ran
        # under.
        if not record(rep, pending_chat=remaining_chat,
                      pending_ntfy=pending_ntfy, prior_state=written):
            print("helm proxywatch: delivery acknowledgement write failed; "
                  "at-least-once retry may duplicate an alert", file=sys.stderr)
            return 2
        if remaining_chat:
            # The queue is durable (retried next cadence), but a watch that
            # cannot reach its own alert surface is a broken watchdog, not a
            # finding — exit 2 keeps the unit red until delivery works.
            return 2
        # A LOST RECORD IS A FINDING: the pass exits 1 on it exactly as it
        # does on a health fault, so the timer unit reads it the same way.
        return 1 if hits or persist_failed else 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
