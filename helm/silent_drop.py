"""helm.silent_drop — the empty-completion loud-fail rung.

THE BUG CLASS (owner hypothesis, kimi investigation 2026-07-23): the codex
cc-proxy (router-for-me/CLIProxyAPI) translates an upstream codex completion
into Anthropic SSE for claude-code. On a security/crypto-heavy turn the
generated TEXT is DROPPED at that boundary — the transcript records an
assistant turn with stop_reason=end_turn, NO text, NO tool_use, NO real
thinking, yet usage.output_tokens > 0 (codex DID generate; the answer never
reached the pane). HTTP still logs 200 (a completed request), so status-code
monitors are blind. claude-code's own "no visible output" recovery nudge fired
exactly once in 75 observed cases — the other 74 silent deaths wedged nothing
but said nothing and woke nothing. This rung turns that silent class LOUD.

THE SIGNATURE (drop-after-generate, NOT a refusal — a refusal is ~0 output
tokens):
    assistant turn AND stop_reason == "end_turn"
    AND no non-empty text block AND no tool_use block AND no non-empty thinking
    AND usage.output_tokens > 0

READ-ONLY by design: this rung NEVER injects into a pane (unlike autocompact,
which owns pane actuation). It posts a loud a2a alert naming the seat, the
output_tokens that were lost, and the transcript line — so the seat and the
integrator SEE the drop instead of the pane silently idling. Distinct from
the codex-cc-never-ends-turns quirk (that = turn never terminates; this = turn
terminates EMPTY).

COMPOSED, not net-new (decision-spirit #25 existence-sweep): reuses
autocompact's bounded transcript tail (_tail_lines), newest-transcript walk
(_newest_transcript), proxy-seat discovery (proxy_seats), the fcntl state
latch, and chat.post's alert idiom. It is a SIBLING rung, not an autocompact
extension, because autocompact's check() is a fire-into-pane path keyed on the
gauge pct — the drop detector is a read-only recent-turn scan with its own
per-line dedup latch. One timer cadence can run both.
"""
import fcntl
import json
import os
import sys
import time

from . import autocompact, home

LATCH_TTL_S = 15 * 60     # one alert per seat per drop episode
RECENT_LINES = 400        # bounded scan of the transcript tail
RECENT_ALERT_WINDOW_S = 20 * 60   # only alert on a drop recent enough to ACT on;
                          # a stale drop on a frozen/idle seat must never re-cry —
                          # the newest-drop-in-tail otherwise re-alerts forever
_STATE = "silent_drop.json"

_USAGE = """usage: helm seat silent-drop [--seat S] [--once] [--dry-run] [--quiet] [--json]
  One read-only pass over every proxy seat: scan the recent transcript tail
  for the empty-completion drop (end_turn + no text/tool + output_tokens>0)
  and post a LOUD a2a alert naming the lost tokens. Latched: one alert per
  seat per episode. --dry-run reports without posting; --quiet skips the chat
  post; --once accepted for interface stability.
"""


# ---------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------

def _is_drop(record):
    """True for the drop-after-generate signature: an end_turn assistant turn
    that produced output_tokens yet delivered no visible content."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return False
    msg = record.get("message")
    if not isinstance(msg, dict) or msg.get("stop_reason") != "end_turn":
        return False
    content = msg.get("content") or []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            return False
        if b.get("type") == "text" and (b.get("text") or "").strip():
            return False
        if b.get("type") == "thinking" and (b.get("thinking") or "").strip():
            return False
    usage = msg.get("usage") or {}
    try:
        return int(usage.get("output_tokens") or 0) > 0
    except (TypeError, ValueError):
        return False


def _recent(ts):
    """True if a drop's timestamp is within the alert window (or unparseable —
    fail-OPEN so a clock/format surprise never suppresses a real drop)."""
    if not ts:
        return True
    try:
        import datetime
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - t).total_seconds() <= RECENT_ALERT_WINDOW_S
    except (ValueError, TypeError):
        return True


def scan_seat(seat_name):
    """Read-only scan of one seat's newest transcript tail. Returns a finding
    dict on the most RECENT drop (within the alert window), else None. A stale
    drop on a frozen/idle seat is NOT a finding — it already happened and the
    seat is producing no turns to rescue. Never injects, never writes."""
    from . import seat
    family, err = seat._seat_family(seat_name)
    if err:
        return None
    d = seat._instance_dir(family, seat_name)
    tp = autocompact._newest_transcript(d)
    if not tp:
        return None
    newest = None
    for ln in autocompact._tail_lines(tp)[-RECENT_LINES:]:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if _is_drop(r):
            usage = (r.get("message") or {}).get("usage") or {}
            newest = {
                "seat": seat_name,
                "transcript": tp,
                "ts": r.get("timestamp"),
                "output_tokens": usage.get("output_tokens"),
                "session": os.path.basename(tp)[:-len(".jsonl")],
            }
    if newest and not _recent(newest.get("ts")):
        return None   # stale drop (older than the window) — don't cry wolf
    return newest


def scan(seats=None):
    if seats is None:
        seats = autocompact.proxy_seats()
    return [f for f in (scan_seat(s) for s in seats) if f]


# ---------------------------------------------------------------------------
# the latch (one alert per seat per episode)
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def _alert_text(f):
    n = f.get("suppressed_since_last") or 0
    storm = ((" (+%d more drops on this seat suppressed since the last alert — "
              "a drop-storm; known upstream reasoning-only class, watchdog "
              "caught each, seat self-recovers)" % n) if n else "")
    return ("@%(seat)s @opus-integrator SILENT-DROP detected: codex "
            "produced %(output_tokens)s output_tokens but the completion "
            "arrived EMPTY (proxy drop-after-generate, not a refusal). The "
            "turn ended silently — nothing surfaced. transcript %(session)s "
            "at %(ts)s. If this was security/crypto work, the answer was "
            "generated then lost — consider re-asking. [silent-drop "
            "watchdog]%(storm)s" % {
                "seat": f["seat"],
                "output_tokens": f.get("output_tokens"),
                "session": f.get("session"),
                "ts": f.get("ts"),
                "storm": storm,
            })


def check(seats=None, post=True, quiet=False):
    """One read-only pass: scan -> dedup-latch -> alert. Returns
    {"findings": [...], "alerted": [...]}. The fcntl lock covers the
    scan/latch/write transaction so overlapping passes never double-alert."""
    from . import pk
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        findings = scan(seats)
        st = pk.read_json(p, {}) or {}
        now = time.time()
        alerted = []
        for f in findings:
            entry = st.get(f["seat"])
            # PER-SEAT rate-limit (attention-budget): once a seat has alerted,
            # suppress further drops for LATCH_TTL_S REGARDLESS of the drop ts.
            # A known drop-class (codex reasoning-only completions, ~50% on
            # some seats) is ONE signal per window, not N wakes — the old
            # per-(seat,ts) latch re-fired on every DISTINCT drop and flooded
            # the fleet. A genuinely new seat's FIRST drop still alerts at once
            # (no prior entry); the suppressed COUNT rides the next alert so one
            # message conveys the storm size.
            if entry and now - (entry.get("alerted_at") or 0) < LATCH_TTL_S:
                f["latched"] = True
                entry["suppressed"] = (entry.get("suppressed") or 0) + 1
                st[f["seat"]] = entry
                continue
            f["latched"] = False
            f["suppressed_since_last"] = (entry.get("suppressed") or 0) if entry else 0
            st[f["seat"]] = {"alerted_at": now, "ts": f.get("ts"), "suppressed": 0}
            alerted.append(f)
        pk.write_json(p, st)

    if post and not quiet:
        for f in alerted:
            try:
                from . import chat
                chat.post(_alert_text(f), who="silent-drop")
            except Exception as e:   # a down chat node never blocks detection
                print("helm silent-drop: chat post failed (%s): %s"
                      % (f["seat"], e), file=sys.stderr)
    return {"findings": findings, "alerted": alerted}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_silent_drop(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if "-h" in args or "--help" in args:
        print(_USAGE)
        return 0
    seat_name = None
    if "--seat" in args:
        i = args.index("--seat")
        seat_name = args[i + 1] if i + 1 < len(args) else None
    res = check(seats=[seat_name] if seat_name else None,
                post="--dry-run" not in args,
                quiet="--quiet" in args)
    if "--json" in args:
        print(json.dumps(res))
    else:
        for f in res["findings"]:
            print("%s: drop output_tokens=%s ts=%s%s"
                  % (f["seat"], f.get("output_tokens"), f.get("ts"),
                     " (latched)" if f.get("latched") else " ALERTED"))
        if not res["findings"]:
            print("no drops detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_silent_drop())
