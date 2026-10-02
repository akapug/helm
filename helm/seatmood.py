#!/usr/bin/env python3
"""helm seat mood — each seat's mood, measured and self-reported (task/3899).

WHY IT EXISTS. The owner asked that each agent at its cubicle show "some
indicator of whether it was frustrated or happy with its current progress or
its otherwise current mood". This is the first slice of the office floor; the
cubicle tabs, the voice with a budget and the office weather all read it.

ONE READING PER SEAT, from the trace helm already keeps
(`seatmood_signals` names each source and what it cannot see):

    state      flowing | grinding | stuck | blocked-on-owner | walled | idle
    score      0-100, how much of helm's own friction the seat is meeting
    reason     the top cause, one line ("3 refusals by author-gate in 20 min")
    cause      that cause's stable key: `loop:refusal`, `loop:rerun`,
               `loop:failing`, `refusals`, `quiet`, `stalled`, `context`
               (FRICTION_CAUSES), or `wall`, `card`, `rest`, `idle`,
               `unmeasured`, `progress`, `none`
    self       the seat's own word, from `helm seat mood set`, and self_at
    checkin    the rest of that check-in: a 1-5 rating, a why, a blocker and
               a win (`seatmood_pulse`), or None
    divergent  the seat says it is going well (a rating of 4 or 5, else a
               word such as flowing or fine) while the measure says stuck or
               grinding: the honesty signal
    signals    every measured input, so a reader can check the call

HOW THE STATE IS CALLED, in this order:
  walled            its family is RED on money or reach, its own last turn
                    ended on a typed wall, or its proxy pool refuses. A wall
                    is not frustration, so the score reads 0.
  blocked-on-owner  it filed a decision card the owner has not answered.
  idle              its turn ended (or the owner rests it) and nothing it met
                    reached GRIND_AT; or helm cannot date its turns at all
                    and it met no friction and made no progress for a day.
  stuck             a loop (LOOP_AT re-runs or failing calls in a row, or
                    FRICTION_LOOP_AT identical refusals without intervening
                    progress or a two-minute gap), a score of STUCK_AT, or
                    busy QUIET_STUCK_S with no progress.
  grinding          a score of GRIND_AT, busy QUIET_GRIND_S with no progress,
                    or busy over an hour since its last progress.
  flowing           anything else with recent progress or a fresh turn.

THE SCORE. Refusals inside the last hour: REFUSAL_PTS each, and every repeat
of the SAME guard REPEAT_PTS more, because one guard refusing a seat again and
again is a wall it keeps meeting, while three guards once each is ordinary
friction. A proven loop adds LOOP_PTS per repeat. Busy minutes with no progress add
one point per three minutes past QUIET_FROM_S; the clock starts at the later
of the last progress event and the start of the seat's current turn, so a
seat is not blamed for time it was not working. If ANY progress source is
unreadable, the clock cannot prove quiet and contributes no points or state.
Stalled turns and a full
context add a little. Each part is capped and the sum is capped at 100.

THE SELF-REPORT is keyed by the calling seat's ROSTER identity, through the
identity law (`actors.resolve_actor`): a process no roster seat names, or one
whose identity is disputed, records nothing. The word lives in one small
file per seat (the latest) and one append-only ledger (the history). It is a
CHECK-IN: beside the word it may carry a 1-5 rating, a blocker and a win; a
blocker is posted once to #seats for the seat's steward (`seatmood_pulse`).

EVERY MEASURE IS REMEMBERED per seat (`seatmood_surface.remember`), so the
roster listing (`helm chat seats`) shows each seat's latest reading and its
age without measuring anything itself.

THE ASK. A Claude Code Stop hook reaches the model only by blocking the stop,
and a stop the mood blocks is a turn the mood costs, so the ask rides the
reflex lane at the next turn's start instead (`helm inject`). It is asked at
most once an hour per seat, and not at all within the hour after the seat
answered. It is words only: nothing is refused for ignoring it.

THE RANKING (`rank`) is helm's own friction across every seat: refusals per
guard from the friction ledger over N days, merged with the causes in the
burn-down notes file (`notes_path()`). The ranking of the burn-down
friction notes plugs in there: a JSON object
{"source": "...", "causes": [{"cause", "count", "example"}]}, where a cause
spelled `guard:<name>` adds to that guard's ledger count.
"""
import json
import math
import os
import re
import sys
import time
import unicodedata

from . import home, pk

STATES = ("flowing", "grinding", "stuck", "blocked-on-owner", "walled",
          "idle")
#: The floor's order: what needs the owner first.
ATTENTION = ("blocked-on-owner", "stuck", "walled", "grinding", "flowing",
             "idle")
GLYPH = {"flowing": "\U0001f7e2", "grinding": "\U0001f7e1",
         "stuck": "\U0001f534", "blocked-on-owner": "✋",
         "walled": "\U0001f9f1", "idle": "⚪"}

REFUSAL_PTS, REPEAT_PTS, REFUSAL_CAP = 6, 10, 50
LOOP_AT, FRICTION_LOOP_AT = 3, 4
#: A repeated refusal is a loop only while calls are consecutive in time.
FRICTION_RUN_GAP_S = 2 * 60
LOOP_PTS, LOOP_CAP = 10, 30
QUIET_FROM_S, QUIET_GRIND_S, QUIET_STUCK_S = 30 * 60, 45 * 60, 120 * 60
QUIET_CAP = 30
STALL_FROM, STALL_PTS, STALL_CAP = 2, 3, 15
CONTEXT_FROM, CONTEXT_CAP = 70.0, 10
GRIND_AT, STUCK_AT = 25, 60

#: A self-report older than this is shown and never called divergent.
SELF_FRESH_S = 2 * 3600
#: The ask's cadence per seat.
ASK_EVERY_S = 3600
ASK_ID = "steer:seat-mood"
ASK_LINE = ("MOOD: helm asks each seat hourly how its work is going, in one "
            "word (flowing, fine, grinding, stuck, blocked...): `helm seat "
            "mood set <word> [--why \"...\"]`. Optional; `helm seat mood` "
            "shows what helm measures.")
#: The causes a measured grinding or stuck state can carry: each one has a
#: cheapest next move in `seatmood_steer.MOVES`.
FRICTION_CAUSES = ("loop:refusal", "loop:rerun", "loop:failing", "refusals",
                   "quiet", "stalled", "context")
#: The words that claim things are going well.
POSITIVE = frozenset(("flowing", "fine", "good", "great", "happy", "ok",
                      "okay", "smooth"))
WORD_MAX, WHY_MAX = 24, 200
_WORD = re.compile(r"\A[a-z][a-z-]{0,%d}\Z" % (WORD_MAX - 1))
_SEAT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_TS = "%Y-%m-%dT%H:%M:%SZ"
#: The history ledger rolls to `<ledger>.1` past this.
MAX_BYTES = 256 * 1024
LOCK_WAIT_S = 1.0


# ------------------------------------------------------------------ paths

def _state_dir():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", "seat-mood")


def ledger_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "seat-mood.jsonl")


def notes_path():
    """Where the burn-down notes' ranking plugs into `rank`."""
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "friction-notes.json")


def _file(seat, suffix):
    k = str(seat or "").casefold()
    if not _SEAT.match(k):
        return None
    return os.path.join(_state_dir(), k + suffix)


# ------------------------------------------------------------------ helpers

def _age(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _one_line(text):
    return not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
                   for c in text)


# ------------------------------------------------------------------ the judge

def _refusal_part(refusals, now):
    """(points, phrase, {guard: n})."""
    by = {}
    for r in refusals:
        by.setdefault(str(r.get("guard")), []).append(r["at"])
    if not by:
        return 0, None, {}
    pts = sum(REFUSAL_PTS + REPEAT_PTS * (len(v) - 1) for v in by.values())
    guard, ats = min(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    total = len(refusals)

    def mins(at):
        return max(1, int(math.ceil((now - at) / 60.0)))
    if len(ats) == total:
        why = "%d refusal%s by %s in %d min" % (total, "s"[:total != 1], guard,
                                                mins(min(ats)))
    else:
        why = "%d refusals in %d min, %d by %s" % (
            total, mins(min(r["at"] for r in refusals)), len(ats), guard)
    return min(REFUSAL_CAP, pts), why, {g: len(v) for g, v in by.items()}


def _run(refusals, progress_at=None):
    """The CURRENT continuous guard+reason run, since the last progress.

    An older loop that landed is not a present loop; neither are isolated
    refusals minutes apart, even when their guard and token match.
    """
    cur, prev, at, guard = 0, None, None, None
    for r in refusals:
        if progress_at is not None and r["at"] <= progress_at:
            cur, prev, at, guard = 0, None, None, None
            continue
        k = (r.get("guard"), r.get("reason"))
        cur = cur + 1 if k == prev and at is not None \
            and 0 <= r["at"] - at < FRICTION_RUN_GAP_S else 1
        prev, at, guard = k, r["at"], r.get("guard")
    return cur, guard


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value > 0 else 0


def judge(raw, now):
    """One seat's measured inputs (`seatmood_signals.inputs`) -> its mood."""
    from .seatmood_signals import text
    seat = text(raw.get("seat"), 64)
    refusals = sorted(raw.get("refusals") or (), key=lambda r: r["at"])
    counters = raw.get("counters") or {}
    idle = raw.get("idle") or {}
    progress = raw.get("progress")
    progress_unread = list(raw.get("progress_unread") or ())
    unread = list(raw.get("unread") or ())
    wall, card = raw.get("wall"), raw.get("card")
    istate, busy = idle.get("state"), idle.get("state") == "BUSY"

    ref_pts, ref_why, by_guard = _refusal_part(refusals, now)
    progress_at = _num(progress[0]) if progress else None
    forward = raw.get("forward_progress")
    run_at = _num(forward[0]) if forward else None
    # The refusals are present evidence and the two-minute gap bounds the
    # run, so an unreadable progress source does not erase a tight loop: it
    # could only hide a forward op inside it, and the floor names it unread.
    run, run_guard = _run(refusals, run_at)
    reruns = _int(counters.get("loop-streak"))
    failing = _int(counters.get("stuck-streak"))
    stalled = _int(counters.get("stalled-turns"))
    loops = ((run >= FRICTION_LOOP_AT, run - 1,
              "the same refusal by %s %d times in a row" % (run_guard, run),
              "loop:refusal"),
             (reruns >= LOOP_AT, reruns,
              "the same command re-run %d times" % reruns, "loop:rerun"),
             (failing >= LOOP_AT, failing - 1,
              "%d failing calls in a row" % failing, "loop:failing"))
    loop, excess, loop_why, loop_cause = max(loops,
                                             key=lambda t: (t[0], t[1]))
    loop_pts = min(LOOP_CAP, LOOP_PTS * max(0, excess)) if loop else 0

    progress_gap_s = max(0, now - progress_at) if progress_at is not None \
        else None
    started = max([t for t in (progress_at, _num(idle.get("turn_opened")))
                   if t is not None], default=None)
    quiet_s = max(0, now - started) if started is not None else None
    quiet_pts = min(QUIET_CAP, int((quiet_s - QUIET_FROM_S) // 180)) \
        if busy and not progress_unread and quiet_s is not None \
        and quiet_s > QUIET_FROM_S else 0
    if progress:
        last = "last: %s" % progress[1]
        quiet_why = "no progress for %s while busy (%s)" % (
            _age(progress_gap_s if progress_gap_s is not None
                 and progress_gap_s >= 3600 else quiet_s or 0), last)
    else:
        quiet_why = "no progress event for %s while busy" % _age(quiet_s or 0)
    stall_pts = min(STALL_CAP, STALL_PTS * max(0, stalled - STALL_FROM))
    ctx = _num(raw.get("context_pct"))
    ctx_pts = min(CONTEXT_CAP, int((ctx - CONTEXT_FROM) / 3)) \
        if ctx is not None and ctx > CONTEXT_FROM else 0

    points = {"refusals": ref_pts, "loop": loop_pts, "quiet": quiet_pts,
              "stalled": stall_pts, "context": ctx_pts}
    score = min(100, sum(points.values()))
    stuck_quiet = busy and not progress_unread and quiet_s is not None \
        and quiet_s >= QUIET_STUCK_S
    grind_quiet = busy and not progress_unread and quiet_s is not None \
        and quiet_s >= QUIET_GRIND_S
    stale_progress = progress_gap_s is not None and progress_gap_s > 3600 \
        and not progress_unread
    from_progress = False
    if wall:
        state, score, cause = "walled", 0, "wall"
        reason = _wall_why(wall)
    elif card:
        state, cause = "blocked-on-owner", "card"
        score = min(100, score - quiet_pts)
        since = _num(card.get("since"))
        reason = 'waiting on the owner: decide card %s "%s"%s' % (
            card.get("id"), str(card.get("title") or "")[:60],
            ", open %s" % _age(now - since) if since is not None else "")
    elif istate in ("IDLE", "RESTING") and not loop and score < GRIND_AT:
        state, cause = "idle", "rest" if istate == "RESTING" else "idle"
        reason = "resting: %s" % (idle.get("why") or "the owner paused it") \
            if istate == "RESTING" else \
            "idle %s: its turn ended and nothing has run since" % _age(
                idle.get("idle_s") or 0)
    elif istate not in ("BUSY", "IDLE", "RESTING") and score == 0 \
            and not progress:
        state, from_progress, cause = "idle", True, "unmeasured"
        reason = "nothing measured: %s" % (idle.get("why") or
                                           "its turns cannot be dated")
    else:
        if loop or score >= STUCK_AT or stuck_quiet:
            state = "stuck"
        elif score >= GRIND_AT or grind_quiet or (stale_progress and busy):
            state = "grinding"
        elif stale_progress:
            state = "idle"
        else:
            state = "flowing"
        parts = (("refusals", ref_why), ("loop", loop_why),
                 ("quiet", quiet_why), ("stalled",
                                        "%d turns in a row with no forward op"
                                        % stalled),
                 ("context", "context %.0f%% full" % (ctx or 0)))
        name, why = max(parts, key=lambda p: points[p[0]])
        if loop:
            reason, cause = loop_why, loop_cause
        elif stuck_quiet or grind_quiet or stale_progress \
                or (points[name] and name == "quiet"):
            reason, from_progress, cause = quiet_why, True, "quiet"
        elif points[name]:
            reason, cause = why, name
        elif progress:
            reason, from_progress, cause = "last progress %s ago: %s" % (
                _age(now - progress[0]), progress[1]), True, "progress"
        else:
            reason, from_progress, cause = ("no friction in the last hour",
                                            True, "none")
    if from_progress and unread:
        reason += " (unread: %s)" % "; ".join(unread)
    reason = text(reason, 400)

    from .seatmood_pulse import checkin, diverges
    said = raw.get("self") if isinstance(raw.get("self"), dict) else None
    said_at = _num((said or {}).get("at"))
    word = (said or {}).get("word")
    divergent = diverges(state, said, now)
    signals = {"refusals": len(refusals), "by_guard": by_guard,
               "repeat": run, "loop_streak": reruns, "stuck_streak": failing,
               "stalled_turns": stalled, "idle": istate,
               "idle_s": idle.get("idle_s"),
               "turn_opened": idle.get("turn_opened"),
               "last_call": idle.get("last_call"),
               "progress_at": progress_at,
               "progress": progress[1] if progress else None,
               "quiet_s": quiet_s, "context_pct": ctx, "wall": wall,
               "owner_card": card, "points": points, "unread": unread,
               "progress_unread": progress_unread}
    return {"seat": seat, "state": state, "score": int(score),
            "reason": reason, "cause": cause, "self": word,
            "self_at": pk.epoch_ts(said_at) if said_at is not None else None,
            "checkin": checkin(said), "divergent": divergent,
            "signals": signals}


def _wall_why(wall):
    src = wall.get("source")
    if src == "family":
        return "family %s walled on %s: %s" % (wall.get("family"),
                                               wall.get("axis"),
                                               wall.get("why"))
    if src == "turn":
        return "its last turn ended on a %s wall (%s)" % (
            str(wall.get("axis") or "?").upper(), wall.get("why"))
    return "its %s proxy pool has %s" % (wall.get("family"), wall.get("why"))


# ------------------------------------------------------------------ readings

def _blank(seat, unread=()):
    return {"seat": seat, "refusals": [], "counters": {}, "idle": {},
            "progress": None, "progress_unread": [], "wall": None, "card": None,
            "context_pct": None, "self": self_report(seat),
            "unread": list(unread)}


def measure(seat, now=None):
    """One seat's mood (see the module docstring for the keys)."""
    from . import seatmood_signals as sig
    now = time.time() if now is None else now
    from .seatmood_surface import remember
    raws, unread = sig.inputs([seat], now)
    m = judge(raws.get(sig.key(seat)) or _blank(str(seat), unread), now)
    remember(m, now)
    return m


def floor(now=None):
    """Every live roster seat's mood, attention first. Raises OSError when
    the roster cannot be read: that is never an empty floor."""
    from . import seatmood_signals as sig
    now = time.time() if now is None else now
    names, rows, why = sig.live_seats()
    if why:
        raise OSError(why)
    from .seatmood_surface import remember
    raws, unread = sig.inputs(names, now, rows=rows)
    out = [judge(raws.get(sig.key(n)) or _blank(n, unread), now)
           for n in names]
    for m in out:
        remember(m, now)
    return sorted(out, key=lambda m: (ATTENTION.index(m["state"]),
                                      -m["score"], m["seat"].casefold()))


# ------------------------------------------------------------------ self

def self_report(seat):
    """{word, why, at, rating, blocker, win} the seat last said, or None.
    A word that is not one word, or a field of the wrong type, reads as
    absent rather than reaching a terminal."""
    from .seatmood_pulse import RATINGS
    path = _file(seat, ".json")
    got = pk.read_json(path, None) if path else None
    if not isinstance(got, dict) or not isinstance(got.get("word"), str) \
            or not _WORD.match(got["word"]) or _num(got.get("at")) is None:
        return None

    def line(k):
        v = got.get(k)
        return v if isinstance(v, str) and v and _one_line(v) else None
    rating = got.get("rating")
    return {"word": got["word"], "why": line("why"), "at": got["at"],
            "rating": rating if rating in RATINGS
            and not isinstance(rating, bool) else None,
            "blocker": line("blocker"), "win": line("win")}


def asked_at(seat):
    path = _file(seat, ".asked")
    got = pk.read_json(path, None) if path else None
    return _num(got.get("at")) if isinstance(got, dict) else None


def _roster_key(name):
    """(the roster's own spelling of `name`, None) or (None, why). A key that
    is not a seat token is refused, never printed (`seatmood_signals.read_roster`
    says why), and so is a name two case-variant rows claim."""
    from . import seatmood_signals as sig
    from .seats_common import canonical_seat
    name = sig.text(name, 64)
    rows, why = sig.read_roster()
    if why:
        return None, why
    k, err = canonical_seat(name, rows)
    if err:
        return None, ("the roster holds more than one row for %s; `helm chat "
                      "seat rename` repairs it" % name)
    if k is None:
        return None, "%s is not a seat on the roster" % name
    if not sig.is_seat(k):
        return None, "the roster's row for %s is not a seat name" % name
    return k, None


def check_word(word, why=None):
    """(word, why, refusal) — the word lowercased, the why stripped."""
    w = str(word or "").strip().lower()
    if not _WORD.match(w):
        return None, None, ("the mood is ONE word of %d letters or fewer "
                            "(a-z and hyphens), e.g. flowing, fine, grinding, "
                            "stuck, blocked" % WORD_MAX)
    if why is None:
        return w, None, None
    y = " ".join(str(why).split())
    if not y or len(y) > WHY_MAX or not _one_line(str(why)):
        return None, None, ("--why is one printable line of %d characters "
                            "or fewer" % WHY_MAX)
    return w, y, None


def set_mood(word, why=None, now=None, rating=None, blocker=None, win=None):
    """Record the calling seat's check-in -> (row, refusal). `rating`,
    `blocker` and `win` are `seatmood_pulse.check`'s; posting a blocker is
    the caller's (`seatmood_pulse.post_blocker`)."""
    from . import actors, eventledger
    from .seatmood_pulse import check
    from .seats_identity import safe_cwd
    now = time.time() if now is None else now
    w, y, refusal = check_word(word, why)
    if refusal:
        return None, refusal
    pulse, refusal = check(rating, blocker, win)
    if refusal:
        return None, refusal
    actor, err = actors.resolve_actor(home.session_id(), safe_cwd(),
                                      act="record a seat's mood")
    if err:
        return None, err
    seat, err = _roster_key(actor.canonical_name)
    if err:
        return None, err
    row = dict({"seat": seat, "word": w, "why": y, "at": now}, **pulse)
    path = _file(seat, ".json")
    if not path:
        return None, "%r cannot name a mood file" % seat
    os.makedirs(_state_dir(), exist_ok=True)
    pk.write_json(path, row)
    dest = ledger_path()
    with eventledger.locked(dest, timeout=LOCK_WAIT_S) as held:
        if held:
            try:
                if os.path.getsize(dest) > MAX_BYTES:
                    os.replace(dest, dest + ".1")
            except OSError:
                pass
            eventledger.append_unlocked(dest, dict({
                "id": os.urandom(8).hex(), "ts": pk.epoch_ts(now),
                "seat": seat, "word": w, "why": y}, **pulse))
    return row, None


def history():
    """(every self-report row, oldest first, unreadable reason)."""
    from . import eventledger
    out = []
    for p in (ledger_path() + ".1", ledger_path()):
        rows, why = eventledger.checked_events(p)
        if why:
            return [], why
        out.extend(rows)
    return out, None


# ------------------------------------------------------------------ the ask

def _due(seat, now):
    last = max([t for t in (asked_at(seat),
                            _num((self_report(seat) or {}).get("at")))
                if t is not None], default=None)
    return last is None or now - last >= ASK_EVERY_S


def ask(session=None, now=None):
    """(the ask line, the roster seat) when this process's seat is due an
    ask, else None. THE HOT PATH: one small file read for a seat asked within
    the hour; the roster is read only for a seat that is due. Never raises
    for a caller that wraps it, and asks nobody it cannot name."""
    from . import friction
    now = time.time() if now is None else now
    name = friction._seat(session)
    if not name or not _due(name, now):
        return None
    seat, err = _roster_key(name)
    if err or not seat or not _due(seat, now):
        return None
    return ASK_LINE, seat


def mark_asked(seat, now=None):
    """Record that `seat` was asked. Fail-open: a lost mark asks again."""
    path = _file(seat, ".asked")
    if not path:
        return False
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        pk.write_json(path, {"seat": seat,
                             "at": time.time() if now is None else now})
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ rank

def _notes():
    """([(cause, count, example)], source, why) from the notes file."""
    path = notes_path()
    try:
        got = pk.read_json(path, None, strict=True)
    except Exception as exc:                    # noqa: BLE001 — named
        return [], None, "notes file (%s)" % type(exc).__name__
    if got is None:
        return [], None, None
    causes = got.get("causes") if isinstance(got, dict) else None
    if not isinstance(causes, list):
        return [], None, "notes file (no `causes` list)"
    out = []
    for c in causes:
        cause = c.get("cause") if isinstance(c, dict) else None
        count = c.get("count") if isinstance(c, dict) else None
        if isinstance(cause, str) and cause.strip() and _int(count):
            out.append((" ".join(cause.split())[:200], count,
                        str(c.get("example") or "")[:200]))
    return out, str(got.get("source") or path), None


def ranking(days=7, now=None):
    """{ranked, notes, unread}: `rank`, plus where the notes came from and
    a notes file that could not be used. Raises OSError for an unreadable
    friction ledger: that is never zero friction."""
    from . import friction
    now = time.time() if now is None else now
    rows, unreadable = friction.read()
    if unreadable:
        raise OSError("the friction ledger is UNREADABLE (%s): this is not "
                      "zero friction" % unreadable)
    since, causes = now - days * 86400, {}
    for r in rows:
        guard, at = r.get("guard"), pk.parse_ts_epoch(r.get("ts"))
        if not guard or at is None or not since <= at <= now:
            continue
        c = causes.setdefault("guard:%s" % guard, [0, "", -1.0])
        c[0] += 1
        if at >= c[2]:
            c[1] = "%s refused%s at %s" % (
                r.get("seat") or "(no seat)",
                " (%s)" % r["reason"] if r.get("reason") else "",
                pk.epoch_ts(at))
            c[2] = at
    notes, source, why = _notes()
    for cause, count, example in notes:
        c = causes.setdefault(cause, [0, "", -1.0])
        c[0] += count
        c[1] = c[1] or example
    ranked = sorted(((cause, c[0], c[1]) for cause, c in causes.items()),
                    key=lambda t: (-t[1], t[0]))
    return {"ranked": ranked, "notes": source, "unread": why}


def rank(days=7, now=None):
    """[(cause, count, example)] of helm's own friction across all seats,
    most first."""
    return ranking(days, now)["ranked"]


# ------------------------------------------------------------------ CLI

USAGE = """usage: helm seat mood [SEAT] [--json]
       helm seat mood set <word> [--why "..."] [--rating 1-5]
                          [--blocker "..."] [--win "..."]
       helm seat mood rank [--days N] [--json]
  Each seat's mood: flowing, grinding, stuck, blocked-on-owner, walled or
  idle, a 0-100 frustration score and the top reason, measured from guard
  refusals, loops, minutes busy without progress, its family's credential
  wall and open decision cards. Bare, it shows every live seat, attention
  first; with SEAT, that seat in full. `set` records the calling seat's
  check-in: its own word (one word, a-z and hyphens), --why, a --rating of
  how its work is going (1 worst, 5 best), a --blocker and a --win (one line
  each). A blocker is posted once to #seats with the seat's steward
  mentioned (its project's lead, else the build-lanes steward); a win is
  recorded only. The measure shows the check-in beside its own reading and
  flags a seat that says it is going well while it measures stuck or
  grinding. `rank` ranks helm's own friction across every seat over N days
  (default 7), with the burn-down notes file merged in."""


def _err(text, rc):
    print("helm seat mood: %s" % text, file=sys.stderr)
    return rc


def _flags(argv, allowed, valued=()):
    """(positionals, {flag: value}, refusal)."""
    pos, flags = [], {}
    while argv:
        head = argv.pop(0)
        if head in valued:
            if not argv or head in flags:
                return None, None, "%s needs one value" % head
            flags[head] = argv.pop(0)
        elif head in allowed and head not in flags:
            flags[head] = True
        elif head.startswith("-") and head:
            return None, None, "unknown argument %r" % head[:64]
        else:
            pos.append(head)
    return pos, flags, None


def _clock(epoch):
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def _self_cell(m):
    if not m["self"]:
        return "-"
    at = pk.parse_ts_epoch(m["self_at"])
    rating = (m.get("checkin") or {}).get("rating")
    cell = "%s%s (%s)" % (m["self"], " %d/5" % rating if rating else "",
                          _clock(at) if at else "?")
    return cell + (" DIVERGES" if m["divergent"] else "")


def _print_floor(moods, now):
    print("helm seat mood — %d live seat%s at %s, attention first"
          % (len(moods), "s"[:len(moods) != 1], _clock(now)))
    for m in moods:
        print("  %s %-16s %3d  %-20s self: %-22s %s" % (
            GLYPH[m["state"]], m["state"], m["score"], m["seat"][:20],
            _self_cell(m), m["reason"]))


def _print_one(m):
    s = m["signals"]
    print("helm seat mood — %s: %s %s (score %d)" % (
        m["seat"], GLYPH[m["state"]], m["state"], m["score"]))
    print("  why       %s" % m["reason"])
    print("  self      %s%s" % (_self_cell(m), "" if not m["divergent"] else
                                "  (it says this; helm measures %s)"
                                % m["state"]))
    for key in ("blocker", "win"):
        if (m.get("checkin") or {}).get(key):
            print("  %-9s %s" % (key, m["checkin"][key]))
    print("  refusals  %d in the last hour%s" % (
        s["refusals"], " (%s)" % ", ".join("%s %d" % kv for kv in sorted(
            s["by_guard"].items(), key=lambda kv: (-kv[1], kv[0])))
        if s["by_guard"] else ""))
    print("  loops     same refusal x%d, re-runs %d, failing calls %d, "
          "stalled turns %d" % (s["repeat"], s["loop_streak"],
                                s["stuck_streak"], s["stalled_turns"]))
    print("  idle      %s" % (s["idle"] or "UNKNOWN"))
    print("  progress  %s" % ("%s at %s" % (s["progress"],
                                            _clock(s["progress_at"]))
                              if s["progress"] else "none in 24h"))
    if s["context_pct"] is not None:
        print("  context   %.0f%%" % s["context_pct"])
    if s["unread"]:
        print("  unread    %s" % "; ".join(s["unread"]))


def _cmd_show(argv):
    pos, flags, refusal = _flags(argv, ("--json",))
    if refusal or len(pos) > 1:
        return _err((refusal or "at most one SEAT") + "\n" + USAGE, 2)
    now = time.time()
    if pos:
        try:
            name = home.validate_seat_arg(pos[0])
        except home.SeatNameError as exc:
            return _err(str(exc), 2)
        seat, err = _roster_key(name or "")
        if err:
            return _err(err, 1)
        m = measure(seat, now=now)
        if "--json" in flags:
            print(json.dumps(m, sort_keys=True))
        else:
            _print_one(m)
        return 0
    try:
        moods = floor(now=now)
    except OSError as exc:
        return _err("%s — this is not an empty floor" % exc, 1)
    if "--json" in flags:
        print(json.dumps(moods, sort_keys=True))
    else:
        _print_floor(moods, now)
    return 0


def _cmd_set(argv):
    from . import seatmood_pulse
    pos, flags, refusal = _flags(argv, (), valued=(
        "--why", "--rating", "--blocker", "--win"))
    if refusal or len(pos) != 1:
        return _err((refusal or "set takes one word") + "\n" + USAGE, 2)
    word, why, refusal = check_word(pos[0], flags.get("--why"))
    pulse, refusal2 = seatmood_pulse.check(flags.get("--rating"),
                                           flags.get("--blocker"),
                                           flags.get("--win"))
    if refusal or refusal2:
        return _err(refusal or refusal2, 2)
    row, err = set_mood(word, why, **pulse)
    if err:
        return _err("not recorded: %s" % err, 1)
    print("helm seat mood — recorded: %s says %s%s%s at %s. `helm seat mood "
          "%s` shows it beside what helm measures."
          % (row["seat"], row["word"], " %d/5" % row["rating"]
             if row["rating"] else "", ' ("%s")' % row["why"]
             if row["why"] else "", _clock(row["at"]), row["seat"]))
    if row["blocker"]:
        print("  " + seatmood_pulse.post_line(*seatmood_pulse.post_blocker(
            row)))
    if row["win"]:
        print("  win recorded: %s" % row["win"])
    return 0


def _cmd_rank(argv):
    pos, flags, refusal = _flags(argv, ("--json",), valued=("--days",))
    if refusal or pos:
        return _err((refusal or "rank takes no SEAT") + "\n" + USAGE, 2)
    raw = flags.get("--days", "7")
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= 365:
        return _err("--days is a whole number from 1 to 365", 2)
    days = int(raw)
    try:
        got = ranking(days)
    except OSError as exc:
        return _err("rank: %s" % exc, 1)
    if "--json" in flags:
        print(json.dumps(dict(got, days=days), sort_keys=True))
        return 0
    print("helm seat mood rank — helm's own friction, last %d day%s, most "
          "first" % (days, "s"[:days != 1]))
    for cause, count, example in got["ranked"]:
        print("  %5d  %-36s e.g. %s" % (count, cause[:36], example))
    print("  notes: %s" % (got["notes"] or "none (%s)" % notes_path()))
    if got["unread"]:
        print("  unread: %s" % got["unread"])
    return 0


def cmd(args):
    """helm seat mood [SEAT] [--json] | set <word> [--why W] | rank."""
    argv = list(args)
    if argv[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if argv[:1] == ["set"]:
        return _cmd_set(argv[1:])
    if argv[:1] == ["rank"]:
        return _cmd_rank(argv[1:])
    return _cmd_show(argv)
