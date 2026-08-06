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
healthy proxy seat it binds the exact current roster session and live agent pid
to /proc's model and loopback base URL, the unique listener and launch-recorded
config digest, the configured alias/provider/upstream route, and its own
successful authenticated eight-token canary. OAuth proofs additionally bind the
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
For 65 minutes codex-2's pane, local probe, CPU, socket, and transcript mtime
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
import time
import fcntl

from . import home

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
# say it. Two clocks must never be collapsed into one field (codex-2's
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
        with open(path, encoding="utf-8") as f:
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
            # reading this channel exists to cure (codex-2's P1 — T-1s was
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
    family lost is a write race, not a validation failure (codex-2's P1).
    Returns the open fd; caller closes.

    O_CREAT makes the FILE, never the parent DIRECTORY — on a fresh home
    this open raised FileNotFoundError and the owner's FIRST use of the
    verb got a traceback (helm-claude-2, r2). The directory is made first,
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


@contextlib.contextmanager
def delivery_state_guard():
    """Serialize one actuator decision with proxywatch state replacement.

    Atomic rename prevents torn reads but cannot order "checked healthy" against
    "recorded dark" and a later cursor commit. Delivery holds this short lock
    through its cursor mutation; record() holds it only around the atomic write.
    The authenticated canary remains outside, so a chat boundary never waits on
    provider I/O.
    """
    path = os.path.join(os.path.dirname(_state_path()), ".proxywatch-state.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

def _read_watch_state():
    """(state_dict, error_or_none) — the last persisted watch record.

    Tri-state, because a corrupt outbox is the precise case at-least-once
    exists for and reading it as {} silently converts the safety mechanism
    into a loss. Found by @kimi: wrote "{corrupt json[" to the state path,
    _read_watch_state returned {}, pending_chat -> [], and a queue that HAD
    pending alerts silently delivered nothing. A blind read must never
    masquerade as clean.

    Returns (parsed_dict, None) on valid read, ({}, "missing") on a genuinely
    absent file (first run is not an error), and ({}, error_string) on any
    unreadable/corrupt state. Callers must refuse the pass on an error.
    """
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f) or {}, None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, "proxywatch state unreadable — %s" % e


# TURN REALITY. The 2026-07-30 codex family wall: for 65 minutes codex-2's
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
        with open(_backup_state_path(), encoding="utf-8") as handle:
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
    # must-hit control against the LIVE fleet, where seat `cj` has no dir at
    # all and rendered as a quiet seat. (2) @codex-2's exact-source probes then
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
            # @codex-2's RULE, adopted verbatim because it is generative where
            # mine was only descriptive. AT EVERY EXPECTED TYPED PATH, PRESERVE
            # THREE OUTCOMES: the EXPECTED KIND descends or counts, a
            # SEMANTICALLY LEGITIMATE ENOENT skips, and WRONG TYPE OR ANY OTHER
            # ERROR is UNKNOWN. An ambiguous negative is safe ONLY when every
            # cause of it shares one outcome. My own rule — "anywhere the walk
            # can say no for more than one reason" — named the smell and found
            # nothing; this one generated two boundaries by construction, and
            # codex MEASURED both rather than arguing them: a dangling
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


def health(seats=None, include_upstream=True, prior_state=None):
    """The composite -> a dict. Reads only; never posts, never writes.

    The authenticated family rung is deliberately LAST: the current pane census,
    lazy fuse readers and turn_state ladder derive exactly as before, then the
    completed rows are grouped by family for upstream corroboration. Local-only
    callers pass include_upstream=False and spend no canary tokens.
    """
    from . import seat as seatmod, silent_drop, seats as seatsmod
    from . import autocompact
    rows, now = [], time.time()
    names = seats or [s for s in _watched_seats()]
    runtime_roster = seatsmod.roster()
    for name in names:
        runtime_row = runtime_roster.get(name) or {}
        family, err = seatmod.family_for(
            name, runtime_row.get("runtime"),
            runtime_row.get("runtime_verified") is True)
        row = {"seat": name, "family": family if not err else None,
               "config_ok": None, "drift": [], "alerted_at": None,
               "transcript_age_s": None, "write_age_s": None,
               "semantic_kind": None, "turn_complete": None,
               "pending_after": None, "hang_candidate": False,
               "probe": None, "probe_detail": None, "probe_ms": None,
               "log": None, "log_detail": None, "log_status_401": None,
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
        try:
            from . import pi as pimod
            port, perr = pimod.seat_port(name)
            if port:
                row["probe"], row["probe_detail"], row["probe_ms"] = probe(port)
        except Exception:                   # noqa: BLE001 — a watch never raises
            pass
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
    live, census_blind = _live_seats()
    open_counts, open_scanned = None, False
    for row in rows:
        entry = latch.get(row["seat"]) or {}
        row["alerted_at"] = entry.get("alerted_at")
        age = row["transcript_age_s"]
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
        row["turn_state"], row["turn_evidence"] = turn_state(
            row["pane_live"], age, row.get("log"), row["inflight"],
            row["ctx_pct"], _compact_threshold(), row["spawn_age_s"],
            pane_blind=census_blind, reality=row.get("transcript_reality"),
            open_dispatches=row["open_dispatches"],
            suspend_gap_s=host_suspend_gap_s())
        # Sample the pane-tail classifier HERE, at the owner layer, so the
        # renderer stays a pure reduction (codex r1 HIGH): findings() reading
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
        row["upstream"] = family.get("state")
        row["upstream_detail"] = family.get("detail")
        row["upstream_ms"] = family.get("ms")
        row["upstream_since"] = family.get("since")
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
    # (an earlier fix added ?beta=true&helm_canary=1 to the /v1/messages probe). The
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
            payload = r.read(4096)
            ms = int((time.time() - t0) * 1000)
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
_CANARY_QUERY = "helm_canary=1"
_CLIENT_TIMEOUT = "CLIENT-TIMEOUT"
_UPSTREAM_DARK = frozenset(("UPSTREAM-OVERLOADED", "QUOTA-402", "AUTH-401",
                            "AUTH-UNAVAILABLE", "RATE-LIMITED", "TIMEOUT-500",
                            "UPSTREAM-4XX", "UPSTREAM-5XX", "EMPTY200",
                            "MALFORMED200", "FAMILY-MIXED"))
_PROXY_RUNTIME_V = 1
_PROXY_RUNTIME_FIELDS = {
    "v", "session", "agent_pid", "agent_starttime", "model",
    "local_base_url", "proxy_pid", "proxy_identity", "proxy_config",
    "config_sha256", "route", "observed_at", "canary",
}
_PROXY_KEY_ROUTE_FIELDS = {"alias", "provider", "upstream_model", "base_url"}
_PROXY_AUTH_ROUTE_FIELDS = {"alias", "provider", "upstream_model"}
_PROXY_SAFE_TOPLEVEL = frozenset((
    "host", "port", "auth-dir", "api-keys", "debug",
    "usage-statistics-enabled", "remote-management",
    "nonstream-keepalive-interval", "transient-error-cooldown-seconds",
    "streaming", "routing", "openai-compatibility",
))
_PROXY_CANARY_FIELDS = {"state", "status"}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
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


def _proxy_route_family(route):
    """The one configured family matching a measured route, else UNKNOWN.

    The proof's storage key, roster label, and any recorded family string are
    absent from this comparison. API-key routes bind alias + provider + upstream
    model + endpoint. OAuth routes have no endpoint in the loaded config, so they
    bind the live request model to the homogeneous credential type the exact
    listener loaded. Zero and two configured matches both fail closed.
    """
    if not isinstance(route, dict) or set(route) not in (
            _PROXY_KEY_ROUTE_FIELDS, _PROXY_AUTH_ROUTE_FIELDS):
        return None, "proxy route is malformed"
    if any(not _proof_text(route.get(key), 512) for key in set(route)):
        return None, "proxy route carries malformed values"
    keyed = set(route) == _PROXY_KEY_ROUTE_FIELDS
    if keyed and _safe_url(route["base_url"]) is None:
        return None, "proxy route carries malformed values"
    from . import seat as seatmod
    wanted = (route["alias"], route["provider"], route["upstream_model"])
    if keyed:
        wanted += (route["base_url"].rstrip("/"),)
    matches = []
    for family, configured in seatmod.FAMILIES.items():
        if keyed:
            if configured.get("mode") != "proxy-key":
                continue
            providers = configured.get("pool_providers")
            if providers:
                candidates = ((name, row.get("upstream_model"), row.get("base_url"))
                              for name, row in providers.items())
            else:
                urls = [configured.get("base_url")]
                urls.extend(url for _prefix, url in
                            configured.get("key_base_urls", ()))
                candidates = ((configured.get("provider"),
                               configured.get("upstream_model")
                               or configured.get("model"), url) for url in urls)
            for provider, upstream, base_url in candidates:
                candidate = (configured.get("model"), provider, upstream,
                             str(base_url or "").rstrip("/"))
                if candidate == wanted:
                    matches.append(family)
            continue
        if configured.get("mode") not in ("proxy", "proxy-oauth"):
            continue
        candidate = (configured.get("model"), configured.get("auth_type"),
                     configured.get("model"))
        if candidate == wanted:
            matches.append(family)
    matches = sorted(set(matches))
    if len(matches) != 1:
        return None, ("proxy route maps to %d configured families%s" %
                      (len(matches), ": " + ", ".join(matches) if matches else ""))
    return matches[0], None


def _proxy_proof_family(proof):
    """(family, error) for one sanitized immutable proxy-runtime proof."""
    if not isinstance(proof, dict) or set(proof) != _PROXY_RUNTIME_FIELDS \
            or type(proof.get("v")) is not int \
            or proof.get("v") != _PROXY_RUNTIME_V:
        return None, "proxy runtime proof has the wrong schema or version"
    if not _proof_text(proof.get("session"), 256) \
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
    if not isinstance(canary, dict) or set(canary) != _PROXY_CANARY_FIELDS \
            or canary.get("state") != "HEALTHY" \
            or type(canary.get("status")) is not int \
            or canary.get("status") != 200:
        return None, "proxy runtime proof has no successful authenticated canary"
    route = proof.get("route")
    if isinstance(route, dict) and route.get("alias") != proof.get("model"):
        return None, "proxy runtime proof model does not match its measured alias"
    return _proxy_route_family(route)


def _sanitized_proxy_proof(proof):
    """Exact secret-free copy, or None. Extra fields are rejected, not scrubbed."""
    family, err = _proxy_proof_family(proof)
    if err or not family:
        return None
    return {"v": proof["v"], "session": proof["session"],
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
        return None, None, "roster row @%s has no exact current session" % canonical
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
                with open(path, encoding="utf-8") as f:
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
    return {"pid": pid, "starttime": start, "model": next(iter(models)),
            "base_url": base_url.rstrip("/"), "token": token}, None


def _yaml_scalar(value):
    """One generated YAML scalar; enough to read, never to rewrite, the config."""
    value = value.strip()
    if not value:
        return None
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, str) else None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip() or None


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
            with open(path, encoding="utf-8") as f:
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


def _proxy_config_route(path, alias):
    """(route, inbound bearer, OAuth auth indexes, error) for loaded config."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as ex:
        return None, None, None, "proxy config is unreadable: %s" % ex
    tokens, routes, auth_dirs, top_keys = [], [], [], []
    in_api = in_compat = in_models = False
    compatibility_blocks = 0
    provider = base_url = upstream = None
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
            in_models = False
            if in_compat:
                compatibility_blocks += 1
            continue
        if in_api and indent == 2 and stripped.startswith("- "):
            token = _yaml_scalar(stripped[2:])
            if token:
                tokens.append(token)
            continue
        if not in_compat:
            continue
        if indent == 2 and stripped.startswith("- name:"):
            provider = _yaml_scalar(stripped.partition(":")[2])
            base_url = upstream = None
            in_models = False
        elif indent == 4 and stripped.startswith("base-url:"):
            base_url = _yaml_scalar(stripped.partition(":")[2])
        elif indent == 4 and stripped == "models:":
            in_models = True
        elif in_models and indent == 6 and stripped.startswith("- name:"):
            upstream = _yaml_scalar(stripped.partition(":")[2])
        elif in_models and indent == 8 and stripped.startswith("alias:"):
            route_alias = _yaml_scalar(stripped.partition(":")[2])
            if all((provider, base_url, upstream, route_alias)):
                routes.append({"alias": route_alias, "provider": provider,
                               "upstream_model": upstream,
                               "base_url": base_url.rstrip("/")})
    unknown = sorted(set(top_keys) - _PROXY_SAFE_TOPLEVEL)
    if unknown:
        return None, None, None, ("loaded config has unsupported top-level routing "
                                  "fields: %s" % ", ".join(unknown))
    if len(tokens) != 1:
        return None, None, None, \
            "loaded config has %d inbound bearers" % len(tokens)
    auth_dir = auth_dirs[0] if len(auth_dirs) == 1 else None
    matches = [route for route in routes if route.get("alias") == alias]
    auth_indexes = None
    if compatibility_blocks == 1 and not auth_dirs and len(matches) == 1:
        route = matches[0]
    elif compatibility_blocks == 0 and auth_dir and not routes:
        provider, auth_indexes, err = _proxy_auth_provider(auth_dir)
        if err:
            return None, None, None, err
        route = {"alias": alias, "provider": provider,
                 "upstream_model": alias}
    else:
        return None, None, None, ("loaded config has %d compatibility blocks, "
                                  "%d auth-dir declarations, and %d routes for "
                                  "alias %s" % (compatibility_blocks,
                                                len(auth_dirs), len(matches), alias))
    if _proxy_route_family(route)[1]:
        return None, None, None, "loaded config route is unknown or ambiguous"
    return route, tokens[0], auth_indexes, None


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
    route, token, auth_indexes, err = _proxy_config_route(
        config, runtime["model"])
    if err:
        return None, err
    if token != runtime["token"]:
        return None, "live session bearer does not match the loaded proxy config"
    family, err = _proxy_route_family(route)
    if err or not family:
        return None, err or "measured route family is unknown"
    proof = {"v": _PROXY_RUNTIME_V, "session": session,
             "agent_pid": runtime["pid"],
             "agent_starttime": runtime["starttime"],
             "model": runtime["model"],
             "local_base_url": runtime["base_url"],
             "proxy_pid": listener["pid"],
             "proxy_identity": listener["identity"],
             "proxy_config": config, "config_sha256": digest,
             "route": route}
    return {"url": runtime["base_url"], "token": token,
            "model": runtime["model"], "auth_indexes": auth_indexes,
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
            payload = response.read(4096)
            trace = getattr(response, "headers", {}).get("X-CPA-TRACE-ID")
            ms = int((time.time() - t0) * 1000)
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
                        "HTTP %d without a valid canary envelope — %s"
                        % (response.status, _body_text(payload)), ms,
                        response.status, response_model, trace)
            return ("HEALTHY", "HTTP %d %s" % (response.status, success), ms,
                    response.status, response_model, trace)
    except urllib.error.HTTPError as ex:
        try:
            payload = ex.read(4096)
        finally:
            ex.close()
        ms = int((time.time() - t0) * 1000)
        text = _body_text(payload)
        state = _upstream_state(ex.code, text)
        return (state, "HTTP %d%s" %
                (ex.code, " — " + text if text else ""), ms, ex.code, None, None)
    except Exception as ex:                  # noqa: BLE001 — a watch never raises
        ms = int((time.time() - t0) * 1000)
        reason = getattr(ex, "reason", None)
        if isinstance(ex, TimeoutError) or isinstance(reason, TimeoutError) \
                or ms >= timeout * 1000 - 100:
            return (_CLIENT_TIMEOUT, "no upstream completion in %ds" % timeout,
                    ms, None, None, None)
        return "UNKNOWN", "%s" % ex, ms, None, None, None


def proxy_runtime_canary(seat_name, observed_at=None):
    """(sanitized proof, error) after one stable exact route completes."""
    shape, err = _proxy_runtime_shape(seat_name)
    if err:
        return None, err
    state, detail, _ms, status, response_model, trace = _canary_once(
        shape["url"], shape["token"], shape["model"])
    if state != "HEALTHY" or status != 200:
        return None, "authenticated measured-route canary %s — %s" % (state, detail)
    auth_indexes = shape.get("auth_indexes")
    if auth_indexes is not None:
        match = _PROXY_TRACE_ID.fullmatch(str(trace or ""))
        if response_model != shape["model"]:
            return None, "OAuth canary response model does not match the live request"
        if not match or match.group(1) not in auth_indexes:
            return None, "OAuth canary trace does not name a loaded provider credential"
    confirmed, err = _proxy_runtime_shape(seat_name)
    if err or confirmed != shape:
        return None, ("measured proxy runtime changed across its canary%s" %
                      (" — " + err if err else ""))
    proof = dict(shape["proof"])
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
    if not isinstance(blocks, list) or not blocks \
            or any(not isinstance(block, dict) for block in blocks):
        return None
    text = [block.get("text") for block in blocks
            if block.get("type") == "text"]
    thinking = [block.get("thinking") for block in blocks
                if block.get("type") == "thinking"]
    typed = len(text) + len(thinking) == len(blocks)
    valid_thinking = all(isinstance(value, str) and value.strip()
                         for value in thinking)
    if text == ["OK"] and typed and valid_thinking:
        return "with OK"
    if body.get("stop_reason") == "max_tokens" and not text and thinking \
            and typed and valid_thinking:
        return "with valid thinking-only message at the eight-token cap"
    return None


def _body_text(payload):
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) \
        else str(payload or "")
    return re.sub(r"\s+", " ", text).strip()[:400]


def _upstream_state(code, text):
    low = text.lower()
    if code in (401, 403):
        return "AUTH-401"
    if code == 402:
        return "QUOTA-402"
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


def proxy_runtime_snapshot(session, now=None):
    """(family, proof, error) from one cached proof re-proven live.

    The JSON record is an index and an immutable comparison target, never its
    own authority: ordinary seat processes can write files as the same Unix
    user. Every read therefore remeasures the exact session/pid, listener,
    loaded-config digest, and route. The first read of that complete shape in a
    process also repeats the authenticated canary; only that process-local
    attestation may be reused, and only while the whole measured shape and
    persisted proof remain byte-equivalent. A hand-written cache, replaced pid,
    restarted listener, changed config, or changed route is UNKNOWN.

    Storage keys are deliberately ignored: a proof persisted under `ds4pro`
    derives Codex if and only if its measured route maps uniquely to Codex.
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
            malformed.append(str(key))
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
    identity, err = _roster_identity_for_session(session)
    if err:
        return None, None, err
    shape, err = _proxy_runtime_shape(identity)
    if err:
        return None, None, "live proxy runtime cannot re-prove cached evidence: %s" % err
    live_base = shape.get("proof") if isinstance(shape, dict) else None
    if not isinstance(live_base, dict) \
            or any(proof.get(key) != value for key, value in live_base.items()):
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
        if current != sanitized:
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
    missing (proxywatch has never run), unreadable/corrupt (the case @kimi
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
    if verdict not in _UPSTREAM_DARK | {"HEALTHY", "UNKNOWN"}:
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
        normalized["dark"] = verdict in _UPSTREAM_DARK if dark is None else dark
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
    if state not in _UPSTREAM_DARK | {"HEALTHY", "UNKNOWN"} \
            or dark is not None and type(dark) is not bool:
        return False
    dark = state in _UPSTREAM_DARK if dark is None else dark
    return state != "HEALTHY" and (dark or state in _UPSTREAM_DARK)


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
    """
    try:
        from . import seat as seatmod
        family, ferr = seatmod.family_for(
            str(seat_name or ""), runtime, runtime_verified)
    except Exception as exc:
        return None, "seat family unreadable: %s" % exc.__class__.__name__
    if ferr or not family:
        return None, None              # direct-Claude/unknown seats have no wall
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
    now = time.time() if now is None else now
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


def upstream_health(rows, now=None, prior=None):
    """Family availability with deterministic corroboration and episode memory."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = time.time() if now is None else now
    if prior is None:
        prior, err = _read_watch_state()
        if err:
            prior = {}
    prior_upstream = (prior.get("upstream") or {}) if isinstance(prior, dict) else {}
    grouped, out = {}, {}
    for row in rows:
        if row.get("family") and not row.get("error"):
            grouped.setdefault(row["family"], []).append(row)
    viable, primary = {}, {}
    for family, candidates in grouped.items():
        viable[family] = sorted(r["seat"] for r in candidates
                                if r.get("probe") == "healthy")
        if not viable[family]:
            out[family] = {"state": "UNKNOWN",
                           "detail": "no locally healthy proxy to carry the canary",
                           "ms": None, "seat": None, "members": {}}
            continue
        primary[family] = min(viable[family],
                              key=lambda name: (name != family, name))

    results = {}
    if primary:
        with ThreadPoolExecutor(max_workers=min(8, len(primary))) as pool:
            futures = {pool.submit(upstream_canary, name, family=family): (family, name)
                       for family, name in primary.items()}
            for future in as_completed(futures):
                family, name = futures[future]
                try:
                    results[(family, name)] = future.result()
                except Exception as ex:      # noqa: BLE001 — no false verdict
                    results[(family, name)] = "UNKNOWN", "%s" % ex, None

    corroborate = [(family, name) for family, first in primary.items()
                   if results[(family, first)][0] in _UPSTREAM_DARK
                   for name in viable[family] if name != first]
    if corroborate:
        with ThreadPoolExecutor(max_workers=min(8, len(corroborate))) as pool:
            futures = {pool.submit(upstream_canary, name, family=family): (family, name)
                       for family, name in corroborate}
            for future in as_completed(futures):
                family, name = futures[future]
                try:
                    results[(family, name)] = future.result()
                except Exception as ex:      # noqa: BLE001 — no family guess
                    results[(family, name)] = "UNKNOWN", "%s" % ex, None

    for family, first in primary.items():
        members = {name: results[(family, name)] for name in viable[family]
                   if (family, name) in results}
        states = {result[0] for result in members.values()}
        healthy = next((name for name, result in sorted(members.items())
                        if result[0] == "HEALTHY"), None)
        unknown = any(result[0] == "UNKNOWN" for result in members.values())
        if unknown:
            state, selected = "UNKNOWN", first
        elif healthy:
            state, selected = "HEALTHY", healthy
        elif len(states) == 1:
            state, selected = next(iter(states)), first
        else:
            state, selected = "FAMILY-MIXED", first
        evidence = "; ".join("%s=%s (%s)" % (name, result[0], result[1])
                             for name, result in sorted(members.items()))
        timings = [result[2] for result in members.values()
                   if result[2] is not None]
        out[family] = {"state": state, "detail": evidence,
                       "ms": sum(timings) if timings else None,
                       "seat": selected,
                       "members": {name: result[0]
                                   for name, result in sorted(members.items())}}

    for family, before in prior_upstream.items():
        if family not in out and before.get("dark"):
            out[family] = {"state": "UNKNOWN",
                           "detail": "family absent from current seat census",
                           "ms": None, "seat": None, "members": {},
                           "since": before.get("since"), "dark": True}
    for family, current in out.items():
        before = prior_upstream.get(family) or {}
        before_dark = bool(before.get("dark") or
                           before.get("state") in _UPSTREAM_DARK)
        state = current["state"]
        dark = state in _UPSTREAM_DARK or (state == "UNKNOWN" and before_dark)
        same_state = before.get("state") == state
        blind_dark_episode = before_dark and state == "UNKNOWN"
        current["since"] = current.get("since") or (
            before.get("since") if (same_state or blind_dark_episode)
            and before.get("since") else _iso(now))
        current["dark"] = dark
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
_RESPONSE_BODY = re.compile(
    r' \| response_body=("(?:\\.|[^"\\])*")(?: \[truncated\])?$')


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


def _refusal_cause(codes, bodies=None):
    """The one cause class status codes + optional error bodies establish, or
    UNKNOWN. A body names the layer more precisely than its code class (a 503
    carrying auth_unavailable is an auth outage, not upstream weather), so
    unanimous named bodies outrank the code fallback; disagreeing ones stay
    UNKNOWN rather than laundering mixed evidence into one diagnosis."""
    named = {_upstream_state(code, body)
             for code, body in zip(codes, list(bodies or ())) if body}
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
                         _logged_response_body(ln)))
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
                      for _ts, code, request_path, _body in rows)
    unmarked = sum(code == 401 and not _is_helm_canary_path(request_path)
                   for _ts, code, request_path, _body in rows)
    return {"helm_marked": helm_marked, "unmarked": unmarked,
            "total": helm_marked + unmarked,
            "basis": "request query marker %s" % _CANARY_QUERY,
            "scope": scope}


def _log_state(rows, tail_bytes, streak_n, min_window_s):
    hits = [(ts, code, body) for ts, code, request_path, body in rows
            if request_path.startswith(_AGENT_PATH)
            and not _is_helm_canary_path(request_path)]
    if not hits:
        return "idle", "no agent traffic in the last %dKB" % (tail_bytes // 1024)
    streak = []                             # newest first
    for ts, code, body in reversed(hits):
        if code in (401, 402, 403, 429) or code >= 500:
            streak.append((ts, code, body))
        else:
            break                           # RECOVERY: a non-refusal ends it
    if len(streak) < streak_n:
        return "ok", "last agent request HTTP %d at %s" % (hits[-1][1],
                                                           hits[-1][0])
    code = streak[0][1]
    codes = [c for _ts, c, _body in streak]
    others = sorted(set(codes) - {code})
    cause = _refusal_cause(codes, [body for _ts, _c, body in streak])
    window = _window_s(streak)
    summary = "cause %s; HTTP %d x%d%s, %s → %s" % (
        cause, code, len(streak),
        " (also %s)" % "/".join(str(c) for c in others) if others else "",
        streak[-1][0], streak[0][0])
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
        return {"state": "unknown", "detail": err, "status_401": None}
    state, detail = _log_state(rows, tail_bytes, streak_n, min_window_s)
    return {"state": state, "detail": detail,
            "status_401": _status_401_provenance(rows, scope)}


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
    readings; see @kimi's note that proxywatch's own posted rows are already
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
               suspend_gap_s=0):
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

    Evidence rides with the verdict — which signals composed it and their
    values — because a verdict the owner cannot audit is a guess with
    confidence.
    """
    # THE HOST WAS NOT RUNNING, SO NEITHER WAS THE SEAT. Staleness is measured
    # in WALL time, and wall time keeps counting while the box is suspended —
    # so every seat comes back stale by the length of the suspend, all at once.
    # Six seats each answer "I am stale" truthfully and the composition invents
    # six hangs (kimi, measured: six lockstep false HUNGs).
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
    if suspend_gap_s is None:
        # UNKNOWN NEVER READS AS "NO SUSPEND" (kimi, on the meld). If helm
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
    register walk was flat and silently omitted codex-2/codex-3, which was
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


def _live_seats():
    """(names, blind) — seat names with a live claude process, AND whether the
    census could see the whole host.

    A hang needs a LIVE pane, since a seat nobody launched is not hung, it is
    off. But "no readable process names this seat" and "helm could not read
    every claude on this host" are different facts, and only the first of them
    earns `off`. @codex's round-7 finding (dispatch 90845108): this function
    filtered `procs` and dropped `unreadable`, so a seat whose only process
    helm could not identify came back absent — and `turn_state` reads absent as
    `off`, which this module's own vocabulary defines as deliberate.

    `blind` is the reason string from `orcaadopt.cannot_look` — the one
    implementation of that rule — or None. The `except` returns it too: a
    census that RAISED did not find an empty fleet, it found nothing at all.
    """
    from . import orcaadopt
    try:
        procs, unreadable = orcaadopt.claude_processes()
    except Exception as e:                  # noqa: BLE001 — a watch never raises
        return set(), ("the process census could not be taken (%s), so helm "
                       "cannot say which seats hold a live pane" % e)
    return ({p.get("seat") for p in procs if p.get("seat")},
            orcaadopt.cannot_look(unreadable,
                                  "helm cannot say which seats hold a live "
                                  "pane"))


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
            # impure — the verdict depended on when it was asked (codex r1).
            liv = row.get("liveness")
            if liv and liv.get("state") == "WALLED":
                why = liv.get("blocked_on") or "its cached upstream is dark"
                out.append(("WALLED", "%s: pane LIVE, turn loop quiet, but %s. "
                            "The provider wall explains the missing turn; a "
                            "restart does not repair upstream availability, so "
                            "no resume/reseed is prescribed."
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
                # MED (codex r1): an unreadable classifier means nobody
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
                # LIVE is PROCESS evidence only (#141 r2, codex-3 blocker): a
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
        out.append(("FAMILY-DARK", "%s: upstream %s%s since %s via %s — %s"
                    % (family, state,
                       " (latched; current evidence is unreadable)"
                       if state == "UNKNOWN" else "",
                       upstream.get("since") or "?",
                       upstream.get("seat") or "no representative",
                       upstream.get("detail") or "")))
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
            bucket = "DARK" if upstream.get("dark") or state in _UPSTREAM_DARK \
                else state
            parts.append("upstream|%s|%s" % (family, bucket))
    return hashlib.blake2b("\n".join(parts).encode("utf-8"),
                           digest_size=8).hexdigest()


def changed(rep):
    """(bool, previous_fingerprint) — has the health state moved since the last
    pass? A first-ever run counts as changed ONLY if it has findings, so
    installing the timer on a healthy fleet does not announce itself."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
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
        was_dark = bool(before.get("dark") or
                        before.get("state") in _UPSTREAM_DARK)
        is_dark = bool(current.get("dark") or
                       current.get("state") in _UPSTREAM_DARK)
        if current.get("state") in _UPSTREAM_DARK and is_dark and not was_dark:
            out.append({"kind": "family-dark", "family": family,
                        "state": current.get("state"),
                        "since": current.get("since"),
                        "detail": current.get("detail")})
        elif current.get("state") == "HEALTHY" and was_dark:
            out.append({"kind": "family-recovered", "family": family,
                        "state": "HEALTHY", "since": current.get("since"),
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


def record(rep, pending_chat=None, pending_ntfy=None, prior_state=None):
    """Persist watch state + alert outbox atomically -> True if written.

    The outbox is the transactional boundary: persistence happens BEFORE
    external delivery. A failed initial write therefore sends nothing (fail
    closed); a failed acknowledgement write preserves the pending item for
    at-least-once retry rather than losing the edge.

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
        payload = {"fingerprint": fingerprint(rep), "ts": rep["ts"],
                   "upstream": upstream, "proxy_runtime": runtime,
                   "pending_chat": list(pending_chat or ()),
                   "pending_ntfy": list(pending_ntfy or ())}
        encoded = json.dumps(payload, sort_keys=True)
        with delivery_state_guard():
            # Backup first: once the primary names a new dark episode, damage to
            # that primary must still recover the same latch. Both files carry
            # the identical canonical payload; there is no second transition log.
            pk.atomic_write(_backup_state_path(), encoded)
            pk.atomic_write(p, encoded)
        return True
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
        scope = status_401.get("scope") or {}
        scope_label = "%dB,%s" % (scope["bytes"],
                                  "truncated" if scope.get("truncated")
                                  else "complete") \
            if isinstance(scope.get("bytes"), int) else "scope-UNKNOWN"
        provenance = " 401[tail=%s]=helm-marked:%d/unmarked:%d" % (
            scope_label, status_401.get("helm_marked", 0),
            status_401.get("unmarked", 0)) if status_401.get("total") else ""
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
                      row.get("turn_state") or "-", fanout, provenance,
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
  and is each pane alive? Six rungs and a fuse:

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
            mixed UNKNOWN. Every 401 within the displayed bounded proxy.log tail
            is also accounted by provenance: helm_canary=1 is Helm-marked (not
            authenticated origin); absence is unmarked, never guessed external or
            provider. The report names the observed byte scope and whether older
            rows were truncated. Three failures
            establish a cluster; only a cluster spanning >=60s is a STREAK. A
            shorter burst is BLIP, never STARVED. STARVED additionally requires
            turn=starved. Grok's two-day 402 wall remains visible, but does not
            imply payment is the remedy: that seat is deliberately parked pending
            CLI proxy cursor support.
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
  cadence.
"""


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
    provider header. A config that cannot be read leaves the record
    UNCHANGED on this axis — UNKNOWN, never an invented absence.

    Returns (records, vendor_err): the error is RETURNED, not swallowed —
    a corrupt config silently reading as "no horizons recorded" is the
    false-clean shape (codex-2's P1); the caller decides whether the
    diagnostic surfaces (the timer pass prints it) without ever writing it
    into a record a render would read as fact."""
    vendor_resets, vendor_err = read_vendor_resets()
    upstream = {}
    for family, row in (rep.get("upstream") or {}).items():
        state = row.get("state")
        dark = row.get("dark") is True or state in _UPSTREAM_DARK
        before = before_all.get(family) or {}
        last_dark = state if state in _UPSTREAM_DARK else \
            before.get("last_dark_state")
        if not last_dark and before.get("state") in _UPSTREAM_DARK:
            last_dark = before["state"]
        upstream[family] = {"state": state, "since": row.get("since"),
                            "dark": dark}
        if last_dark:
            upstream[family]["last_dark_state"] = last_dark
        vrec = vendor_resets.get(family)
        if vrec:
            upstream[family]["resets_at_ms"] = vrec["resets_at_ms"]
            upstream[family]["reset_kind"] = vrec["reset_kind"]
            upstream[family]["reset_source"] = vrec["reset_source"]
            upstream[family]["reset_recorded_at"] = vrec["recorded_at"]
    return upstream, vendor_err


def cmd_proxywatch(args):
    """proxywatch — cli-proxy fix invariants + seat liveness, change-latched."""
    import sys
    args = list(args or [])
    from .cli import guard_tail
    # THE OWNER'S VENDOR-RESET DOOR (task/45), dispatched BEFORE guard_tail:
    # its tail is positional (set <family> <when>), and guard_tail's whole
    # contract is that remaining tokens after a matched subverb are FLAGS —
    # so routing the door after it makes set/show/clear unreachable rc=2
    # (codex-2's P1: `vendor-reset show` refused as junk before the branch
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
            # temporarily absent (codex-2 reproduced: kimi's own family
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
            # windows (codex-2's P2). The message says what was recorded so
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

    def render(rep, hits):
        if "--json" in args:
            print(json.dumps({"report": rep, "findings": hits}, indent=2,
                             sort_keys=True))
        else:
            for line in report_lines(rep):
                print(line)

    posting = "--post" in args or "--force" in args
    if not posting:
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
        hits = findings(rep)
        render(rep, hits)
        moved, _prev = changed(rep)
        transitions = upstream_transitions(rep, prior=prior)
        pending_chat = list(prior.get("pending_chat") or ())
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
        # Persist both channel outboxes and the family latch before delivery.
        if not record(rep, pending_chat=pending_chat,
                      pending_ntfy=pending_ntfy, prior_state=prior):
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
        if not record(rep, pending_chat=remaining_chat,
                      pending_ntfy=pending_ntfy, prior_state=prior):
            print("helm proxywatch: delivery acknowledgement write failed; "
                  "at-least-once retry may duplicate an alert", file=sys.stderr)
            return 2
        if remaining_chat:
            # The queue is durable (retried next cadence), but a watch that
            # cannot reach its own alert surface is a broken watchdog, not a
            # finding — exit 2 keeps the unit red until delivery works.
            return 2
        return 1 if hits else 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
