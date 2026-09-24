"""helm.silent_drop — the empty-completion loud-fail rung.

THE BUG CLASS (owner hypothesis, confirmed by investigation): the codex
cc-proxy (router-for-me/CLIProxyAPI) translates an upstream codex completion
into Anthropic SSE for claude-code. On a security/crypto-heavy turn the
generated TEXT is DROPPED at that boundary — the transcript records an
assistant turn with stop_reason=end_turn, NO text, NO tool_use, NO real
thinking, yet usage.output_tokens > 0 (codex DID generate; the answer never
reached the pane). HTTP still logs 200 (a completed request), so status-code
monitors are blind. This rung turns that silent class LOUD.

CORRECTED 2026-07-29 — this header carried the PRE-REFUTATION reading for five
days and directly contradicted `_followed_by_nudge_before_text` a hundred lines
below. It said CC's "no visible output" nudge fired "exactly once in 75
observed cases — the other 74 silent deaths wedged nothing but said nothing",
which reads as: the detector requires a signal present in 1 of 75 real drops
and therefore catches almost none of them. That inference is wrong because the
premise was refuted the next day. A mislabel fix established
that 73 of those 75 candidates were NOT drops at all — they were normal
reasoning-prefix rows whose text arrived as a continuation — and that exactly 2
were genuine, both nudged. So the nudge is not a rare accident on a real drop;
it is CC noticing the silence, and it is the discriminator that separates 2 real
drops from 73 false ones. A reader who trusted the old sentence would conclude
this watchdog is near-blind and stop believing its clean bills. I made that
inference myself before reading further, which is how a stale sentence costs
more than a missing one.

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
  seat per episode. --dry-run reports without posting AND without touching
  the alert latch (a simulation never spends the alert budget); --quiet skips the chat
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


def _blocks(r):
    return (r.get("message") or {}).get("content") or []


def _has_text(r):
    return any(isinstance(b, dict) and b.get("type") == "text"
               and (b.get("text") or "").strip() for b in _blocks(r))


def _followed_by_nudge_before_text(lines, i, lookahead=8):
    """TRUE only when a user 'no visible output' nudge lands BEFORE the next
    assistant text block. This is THE discriminator between a real drop and a
    normal turn (the mislabel fix): claude-code records the
    reasoning-only `end_turn` row FIRST (empty thinking, ot>0), then the text
    as a continuation — 73 such prefix rows on the founding transcript, all
    FALSE drops. A REAL drop is when the text does NOT come: CC detects the
    silence itself and injects a 'no visible output' nudge (only 2 on the
    same transcript, both genuine). Scanning the row in isolation cannot tell
    them apart — the signature is identical — so the detector must look
    FORWARD: nudge-before-text = drop; text-first = normal continuation."""
    for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
        if "no visible output" in lines[j]:
            return True
        try:
            r2 = json.loads(lines[j])
        except ValueError:
            continue
        if r2.get("type") == "assistant" and _has_text(r2):
            return False        # text arrived first — a normal prefix, not a drop
    return False


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
    lines = autocompact._tail_lines(tp)[-RECENT_LINES:]
    newest = None
    for i, ln in enumerate(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        # the isolation signature (a drop CANDIDATE) AND the forward
        # discriminator (a REAL drop: CC nudged the silence) — a thinking-only
        # prefix row that text follows directly is a normal turn, never a drop.
        if _is_drop(r) and _followed_by_nudge_before_text(lines, i):
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


def unscannable(seats=None):
    """[(seat, why)] for every seat this pass COULD NOT LOOK AT.

    CANNOT-LOOK IS NOT A CLEAN BILL, and this module was violating that law
    while enforcing it elsewhere. `scan_seat` returns None for three unrelated
    reasons — an unknown seat family, NO TRANSCRIPT TO READ, and a genuinely
    quiet seat — and `scan` filtered all three to the same empty list. The
    watchdog then printed "no drops detected".

    A SILENT SEAT IS THE SECOND, SUBTLER CASE and the one live on this host.
    `scan_seat` only reports a drop inside RECENT_ALERT_WINDOW_S, deliberately
    (a stale drop is not actionable — the seat has stopped producing turns to
    rescue). But a seat whose NEWEST transcript predates that window can never
    produce a finding at all, so counting it toward a clean bill is counting a
    seat nobody asked anything. Measured on one codex seat: its newest
    transcript was 141 HOURS old while the watchdog reported it clean every
    ninety seconds, through exactly the week the owner kept saying codex was
    still dropping. "Quiet" and "healthy" render identically and mean opposite
    things.

    HONEST NOTE ON A CLAIM THIS DOCSTRING NEARLY MADE. The first version of
    this said two numbered codex seats had NO transcript at all, because the chat
    ROSTER records no session for them. That was a wrong instrument: the
    watchdog reads a seat's own instance dir, not the roster, and both seats
    scan fine. The roster and the instance dir are different sources of truth
    and only one of them is the one this module uses.
    """
    from . import seat
    if seats is None:
        seats = autocompact.proxy_seats()
    out, now = [], time.time()
    for s in seats:
        family, err = seat._seat_family(s)
        if err:
            out.append((s, "not a known seat family"))
            continue
        tp = autocompact._newest_transcript(seat._instance_dir(family, s))
        if not tp:
            out.append((s, "no transcript to read (no session bound to this "
                           "seat) — a drop here cannot be seen"))
            continue
        try:
            age = now - os.path.getmtime(tp)
        except OSError:
            out.append((s, "transcript vanished mid-pass"))
            continue
        if age > RECENT_ALERT_WINDOW_S:
            out.append((s, "silent for %.1fh — older than the %d-minute alert "
                           "window, so no finding is possible; quiet, not clean"
                        % (age / 3600.0, RECENT_ALERT_WINDOW_S // 60)))
    return out


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
    # the integrator is resolved from the roster, never spelled (see
    # seats_integrator.integrator_addressed)
    from .seats_integrator import integrator_addressed
    return integrator_addressed(
        "@%(seat)s SILENT-DROP detected: codex "
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


def check(seats=None, post=True, quiet=False, dry=False):
    """One pass: scan -> dedup-latch -> alert. Returns
    {"findings": [...], "alerted": [...]}. The fcntl lock covers the
    scan/latch/write transaction so overlapping passes never double-alert.

    dry=True is GENUINELY read-only and wins over post: findings are
    classified against the current latch state (latched / would-alert) but
    nothing is written and nothing is posted. The alert budget — the
    LATCH_TTL_S window a real alert opens — is spent only by a pass that
    can actually deliver; before this contract a --dry-run stamped
    alerted_at and silently suppressed the next REAL drop for 15 minutes
    (found by a QC pass, filed by the integrator). post=False
    without dry still latches: those callers (tests, --quiet machine
    consumers) DO consume the alert through the returned structure."""
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
                if not dry:
                    entry["suppressed"] = (entry.get("suppressed") or 0) + 1
                    st[f["seat"]] = entry
                continue
            f["latched"] = False
            f["suppressed_since_last"] = (entry.get("suppressed") or 0) if entry else 0
            if not dry:
                st[f["seat"]] = {"alerted_at": now, "ts": f.get("ts"), "suppressed": 0}
            alerted.append(f)
        if not dry:
            pk.write_json(p, st)

    if post and not quiet and not dry:
        for f in alerted:
            try:
                from . import chat
                # post to #helm (fleet ops), NOT the default #main: every seat
                # homed to #main was woken by rule-(b) home-room surface on a
                # row addressed to codex/integrator (owner 2026-07-24: silent-
                # drop posts don't belong in main). The @mentions in the alert
                # still wake the addressees from any room (rule a).
                chat.post(_alert_text(f), who="silent-drop", room="helm")
            except Exception as e:   # a down chat node never blocks detection
                print("helm seat silent-drop: chat post failed (%s): %s"
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
    want = [seat_name] if seat_name else None
    dry = "--dry-run" in args
    res = check(seats=want, post=not dry, quiet="--quiet" in args, dry=dry)
    blind = unscannable(want)
    res["blind"] = [{"seat": s, "why": w} for s, w in blind]
    if "--json" in args:
        print(json.dumps(res))
    else:
        for f in res["findings"]:
            print("%s: drop output_tokens=%s ts=%s%s"
                  % (f["seat"], f.get("output_tokens"), f.get("ts"),
                     " (latched)" if f.get("latched")
                     else (" WOULD ALERT" if dry else " ALERTED")))
        if not res["findings"]:
            # NEVER a bare "no drops detected" while a seat went unread. The
            # count is the honest qualifier: a clean bill is only as wide as
            # what the pass could actually see.
            print("no drops detected" if not blind
                  else "no drops detected in %d seat(s) — %d COULD NOT BE "
                       "SCANNED, so this is not a clean bill"
                       % (len(autocompact.proxy_seats() if want is None
                                else want) - len(blind), len(blind)))
        for s, why in blind:
            print("  UNSCANNED %-9s %s" % (s, why))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_silent_drop())
