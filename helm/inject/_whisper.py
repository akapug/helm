"""helm inject — the whisper cluster: JIT cooldown scoring, the coinage
3-strikes recorder, the first-turn brief whisper, the council-reach rung,
and the engine (gather/render/--explain).

Moved verbatim from the pre-split helm/inject.py. The names the test
contract monkeypatches on the PACKAGE (helm.inject PINNED_BUDGET /
_greeted_today / _today / _seen_load / _coinage) are read through the
package namespace (_inject.X) at call time — a from-import copy would
strand those patches at the package boundary.
"""
import collections
import hashlib
import os
import re
import time

from .. import (home, injection_schema, moments, pk, promptcensus, promptshape,
               reflex, runtime_config)
from .. import inject as _inject
from ._common import (
    COINAGE_CAP, COINAGE_STRIKES, COOLDOWN_ESCAPE,
    COUNCIL_OFFER_CAP, COUNCIL_ROUNDS, COUNCIL_TAIL, COUNCIL_WHISPER_ID,
    JIT_CAP, REPEAT_ALLOWANCE, REPEAT_WINDOW_TURNS, WHISPER_CAP, WHISPER_ID,
    WHO_ID,
)
from ._entries import (
    _entry_line, _gate_plan, _lane_entries, _lanes, lane_df, pinned_admission,
    pinned_lane,
    pinned_alarm, jit_alarm, lane_footer,
    store_typed_id, _sa_whisper, _who_lines,
)
from ._ledger import _ledger_abort, _ledger_begin, _ledger_finish, _seen_save
from ._compare import _active_compare, _compare_run


def _jit_score(e, low, df):
    """resolve_prompt's exact arithmetic for ONE entry (matched probes' 1/df
    summed, confidence-weighted) — the cooldown's record + escape currency."""
    from .. import store
    matched = store._probe_hits(e, low)[2]
    return e["confidence"] * sum(1.0 / df[p] for p in matched)


# A ROUTED delivery's cooldown score (task/2978). A notice kind routes an
# entry on a turn whose substance can be empty, so its keyword score is 0, and
# a record of 0 could never cool (0 < 2 x 0 is false): the expiry rule would
# re-fire on every 30-minute tick. The route counts as one full hit instead,
# so the second expiry in a context is cooled like any repeat, and a later
# turn that matches the entry's own words twice as strongly still escapes.
ROUTE_SCORE = 1.0


def _turn_score(e, low, df, routed=()):
    """The cooldown currency for this turn: the keyword score, floored at
    ROUTE_SCORE for an entry a notice kind routed."""
    score = _jit_score(e, low, df)
    return max(score, ROUTE_SCORE) if str(e["id"]) in routed else score


def _cooled(rec, turn, score):
    """The ONE cooled predicate (gather + --explain): a [turn, score] fire
    record suppresses FOR THE LIFE OF THE SESSION unless the new score clears
    COOLDOWN_ESCAPE x the score at last fire.

    There is deliberately no turn window. The question a cooldown answers is
    "does the seat still have this?", and the answer changes at COMPACTION,
    not on a timer — _ledger.forget_session drops this whole file at that
    boundary (compaction AND /clear), so everything re-fires to a seat that actually lost it. A
    15-turn window instead re-sent every entry about every 16th turn forever:
    6,949 of 7,375 measured re-deliveries were the window expiring, against
    426 real escapes. The escape is what preserves the signal — a genuine
    relevance spike is new information even when the bytes are not."""
    return bool(rec) and score < COOLDOWN_ESCAPE * rec[1]


def _cool_key(e):
    """A RIDER's cooldown key — the typed id (type:slug), the spelling its
    ledger id and the DROPPED marker already use. A bare key collapsed a
    lexicon and a reference sharing a slug onto one record, so delivering
    one as a rider cooled the other (dispatch 0fb12823851d). A
    SELECTED entry keeps the bare key it always had (the session-file
    contract test_inject pins); _cool_rec reads both."""
    from .. import store
    return store.typed_id(e)


def _cool_rec(seen, e):
    """THE ONE COOLDOWN READ: the entry's typed record (a rider delivery) and
    its bare one (a selected delivery), and the NEWER of the two — by the
    recorded turn — is authoritative. Never an order preference: typed-first
    left a rider's stale record at score S outranking a later selected
    escape at 2S, so every following turn at 2S cleared the escape bar
    against S and re-fired forever, and --explain aged the fire from the
    wrong turn (dispatch c7897ed62f4a). Every reader of
    seen["fired"] — the cooldown split, the escape-vs-new count, --explain —
    goes through this, so however the entry last fired decides."""
    fired = seen["fired"]
    recs = [r for r in (fired.get(_cool_key(e)), fired.get(str(e["id"]))) if r]
    return max(recs, key=lambda r: r[0]) if recs else None


def _cooldown(jit_all, seen, turn, low, df, routed=()):
    """Split ranked JIT hits into (kept, cooled) for this turn. Pre-cap, so
    freed slots reach lower-ranked candidates. cooled = [(entry, n_turns_ago)].
    `routed` names the ids a notice kind routed (their score is floored)."""
    kept, cooled = [], []
    for e in jit_all:
        rec = _cool_rec(seen, e)
        if rec and _cooled(rec, turn, _turn_score(e, low, df, routed)):
            cooled.append((e, turn - rec[0]))
        else:
            kept.append(e)
    return kept, cooled


# ---------------------------------------------------------------------------
# coinage 3-strikes — offer once, latch forever
# ---------------------------------------------------------------------------

# common English hyphenations — never coinages
COINAGE_STOP = frozenset((
    "so-called", "well-known", "long-term", "short-term", "high-level",
    "low-level", "real-time", "built-in", "full-time", "part-time",
    "one-off", "two-way", "re-run", "re-use", "up-to-date", "day-to-day",
    "end-to-end", "state-of-the-art", "follow-up", "sign-off", "trade-off",
    "check-in", "opt-in", "opt-out", "self-serve", "read-only", "front-end",
    "back-end", "open-source", "co-founder", "non-trivial",
))

_QUOTED_RE = re.compile(r'["“]([^"“”\n]{2,40})["”]')
_HYPHEN_RE = re.compile(r"(?<![\w/.'-])([a-z]{2,}(?:-[a-z]{2,}){1,3})(?![\w/.-])")
_WORD_RE = re.compile(r"^[A-Za-z]+(?:[-'][A-Za-z]+)*$")
_TAG_RE = re.compile(r"</?([A-Za-z][\w.-]*)[^<>]*>")  # <term>/</term> tag names
_MACHINE_RE = re.compile(r"\[SYSTEM NOTIFICATION|<task-notification>", re.IGNORECASE)


def _coinage_path():
    return os.path.join(home.global_dir(), ".state", "coinages.json")


def _coinage_candidates(text):
    """The NARROWEST coinage detector (the card's precision law): quoted 1-3
    word phrases + lowercase hyphenated neologisms only. The code-identifier /
    file-path stoplist is structural — any digit, underscore, dot, slash, flag
    dash or mixed case disqualifies — plus COINAGE_STOP for common English
    hyphenations. Machine text is not coining: a system-notification-shaped
    prompt ([SYSTEM NOTIFICATION / <task-notification>) yields NOTHING, and a
    term appearing ANYWHERE in the prompt as an angle-bracket tag (<term> or
    </term> — hook/harness machinery) is disqualified even where it also rides
    prose. -> normalized keys (pk.slug, spaces folded to hyphens) so
    'fire ledger' and fire-ledger count as one term."""
    text = text or ""
    if _MACHINE_RE.search(text):
        return set()
    tagged = {pk.slug(m.group(1)) for m in _TAG_RE.finditer(text)}
    out = set()
    for m in _QUOTED_RE.finditer(text):
        words = m.group(1).split()
        if 1 <= len(words) <= 3 and all(_WORD_RE.match(w) for w in words):
            out.add(pk.slug("-".join(words)))
    for m in _HYPHEN_RE.finditer(text):
        out.add(m.group(1))
    return {t for t in out if t not in COINAGE_STOP and t not in tagged}


def _coinage(text, entries, persist=True):
    """The 3-strikes recorder: count DISTINCT turns per candidate term in
    _global/.state/coinages.json; at COINAGE_STRIKES a term missing the store
    gets ONE reflex-lane define nudge and latches into `offered` FOREVER (one
    line per term, ever; max one nudge per turn). Terms only, never prompt
    text. O(1) small-JSON write, skipped when the prompt has no candidates.
    Fully fail-open -> None."""
    try:
        cands = _coinage_candidates(text)
        if not cands:
            return None
        d = pk.read_json(_coinage_path())
        if not isinstance(d, dict):
            d = {}
        terms = d.get("terms") if isinstance(d.get("terms"), dict) else {}
        terms = {str(k): r for k, r in terms.items()  # shed alien-shaped rows
                 if isinstance(r, list) and len(r) == 2 and isinstance(r[0], int)}
        offered = {str(t) for t in (d.get("offered") or ())}
        known = set()
        for e in entries or ():
            known.add(pk.slug(str(e.get("id") or "")))
            if e.get("term"):
                known.add(pk.slug(str(e["term"])))
        nudge = None
        now = pk.now_ts()
        for key in sorted(cands):
            if key in offered or key in known:
                continue
            n = terms[key][0] + 1 if key in terms else 1
            if n >= COINAGE_STRIKES and nudge is None:
                nudge = key           # offered ONCE...
                offered.add(key)      # ...then latched forever
                terms.pop(key, None)
            else:
                terms[key] = [n, now]
        while len(terms) > COINAGE_CAP:
            terms.pop(min(terms, key=lambda k: str(terms[k][1])))
        if persist:
            pk.write_json(_coinage_path(), {
                "v": 1, "ts": now, "terms": terms,
                "offered": sorted(offered)})
        if nudge:
            return ("REFLEX: coinage '%s' has now been used in %d distinct turns "
                    "and is missing the lexicon — owner present: offer to define "
                    "it now (helm coach); owner away: write a lexicon candidate, "
                    "don't ask. Offer-once: this nudge is latched and never "
                    "repeats for this term." % (nudge, COINAGE_STRIKES),
                    "coinage:" + nudge)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# first-turn whisper — the brief's TL;DR greets the day's first session
# ---------------------------------------------------------------------------

def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _greeted_path():
    return os.path.join(home.global_dir(), ".state", "greeted.json")


def _greeted_today():
    """The O(1) hot-path latch READ: True if the first-turn whisper already
    fired today. One small-JSON day-compare, no scan. Fail-open: an absent /
    torn / alien-shaped file reads as NOT greeted (the whisper may fire) — the
    fire re-stamps the latch, so the brief compose stays once-a-day."""
    d = pk.read_json(_greeted_path())
    return isinstance(d, dict) and d.get("day") == _inject._today()


def _mark_greeted():
    """Stamp today's latch (single-file overwrite, the drift-snapshot shape)."""
    pk.write_json(_greeted_path(), {"v": 1, "day": _inject._today(), "ts": pk.now_ts()})


def _brief_digest():
    """The morning brief in its TIGHTEST form: ONE line off brief.compose()'s
    read-only dict — sessions since you left, the knowledge delta, the count of
    owner gates — under a distinct BRIEF: prefix so the receiving agent relays
    it warmly in its own voice. Seats are omitted (standing state, not news);
    the line points at `helm brief` for the full detail on demand (attention
    budget: relevance + availability). -> the WHISPER_CAP-capped line, or None
    when the window holds nothing worth a whisper (a quiet day)."""
    from .. import brief
    b = brief.compose()
    parts = []
    n = (b.get("sessions") or {}).get("total") or 0
    if n:
        parts.append("%d session%s since you left" % (n, "s"[:n != 1]))
    k = b.get("knowledge") or {}
    kd = [fmt % len(k.get(key) or ()) for fmt, key in
          (("+%d", "added"), ("~%d", "updated"), ("-%d", "retired")) if k.get(key)]
    if k.get("drained"):
        kd.append("%d drained" % k["drained"])
    if kd:
        parts.append(" ".join(kd) + " knowledge")
    w = len(b.get("waiting") or ()) + len(b.get("owner_asks") or ())
    if w:  # verb agrees with the count: "1 needs you" / "2 need you"
        parts.append("%d need%s you" % (w, "s"[:w == 1]))
    if b.get("owner_asks_unavailable"):
        parts.append("owner asks UNKNOWN")
    if not parts:
        return None
    line = "BRIEF: helm morning — " + " · ".join(parts) + " (helm brief)"
    return line if len(line) <= WHISPER_CAP else line[:WHISPER_CAP - 1] + "…"


def _cap_steer(line):
    """One steer line, capped at STEER_CAP with an ellipsis.

    The renderer-side rail of the uncapped-steer class: a hand-authored or
    legacy steer can still carry a wall (the gloss test's rail 2), and the
    budget is the budget. Cut at a word boundary so the cap never severs a
    clause mid-word; under the cap the line passes through untouched."""
    from ._common import STEER_CAP
    if len(line) <= STEER_CAP:
        return line
    cut = line[:STEER_CAP - 1]
    # never sever mid-word: back off to the last space inside the cap
    sp = cut.rfind(" ")
    if sp > STEER_CAP // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _whisper(text, session, persist=True):
    """The first-turn whisper: on the day's FIRST session-bearing, non-empty
    turn, lead with the one-line brief digest. Latched once per day — the
    O(1) _greeted_today read means only the first turn pays a brief compose.
    Direct callers persist by default; gather stages with persist=False and
    stamps only after its READY envelope is durable, so refused delivery cannot
    consume the day's greeting. Plain stdin
    (no session) never whispers, mirroring the cooldown gate. Fully fail-open:
    any brief/state trouble -> [] (no whisper, never a blocked turn)."""
    if not (session and (text or "").strip()):
        return []
    try:
        if _inject._greeted_today():
            return []
        if persist:
            _mark_greeted()
        d = _brief_digest()
        return [d] if d else []
    except Exception:
        return []


_REVIEW_WORDS = ("fix", "clear", "gate", "xrev", "verdict", "blocker",
                 "refute", "approve", "re-gate", "regate")


def _is_review_streak(rows):
    """True when this streak is a REVIEW rather than ordinary back-and-forth.

    NOT a search: the streak itself is already detected deterministically by
    the caller. This only refines WHAT that firing says, over the streak's own
    rows, so a wrong answer costs one extra sentence on a whisper that was
    already firing — never a missed lesson, which is what made the same
    vocabulary the wrong tool for FINDING the premise.

    Both sides must use the vocabulary: one person saying "fix" in a design
    chat is not a review, and a cure line about per-case handling on ordinary
    conversation is wallpaper — which is exactly how a guard stops being read
    (the tree-warning noise lesson, same day)."""
    speakers = {}
    for m in rows:
        text = (m.get("text") or "").lower()
        if any(w in text for w in _REVIEW_WORDS):
            speakers[str(m.get("from"))] = True
    return len(speakers) >= 2


def _council_path():
    return os.path.join(home.global_dir(), ".state", "council-reach.json")


def _council_reach(session, cwd, persist=True):
    """The COUNCIL REACH rung — (line, ledger-id) or None. Premise
    council-is-the-number-one-feature + feature-and-rsh-must-both-be-wired:
    the recorded predecessor failure was SALIENCE — the meld verb existed and
    agents never reached for it, because no per-turn surface advertised it.
    Signal: this seat has ping-ponged >= COUNCIL_ROUNDS rounds with exactly
    ONE other seat in its home room — async back-and-forth that a bounded
    synchronous meld collapses. One bounded room-tail read (chat rooms are
    tmpfs; the delivery lanes already pay this every boundary). Fires ONCE
    per streak: fingerprint = room|peer|streak-start (the start OFFSET in
    attributed rows — reactions/ambient rows/tail-cap growth never drift it;
    a suffix saturating COUNCIL_TAIL hides the start and latches the pair
    itself), latched in _global/.state/council-reach.json (a NEW streak
    re-arms; more rounds of the SAME streak stay silent — no wallpaper).
    Skips meld/dm rooms, owner
    back-and-forth (a human conversation is not a meld candidate), and
    pairs already mid-meld (the verb was reached; the rung's job is done).
    Fail-open to None everywhere (reflex law: a whisper that can crash or
    slow the hook is worse than no whisper)."""
    from .. import chat, seats
    seat = home.chat_name() or seats.seat_for_session(session)
    if not seat:
        return None
    room, _src = seats.resolve_homing(None, cwd)
    room = room or "main"
    if room.startswith("meld-") or room.startswith(chat.DM_PREFIX):
        return None
    rows, _total = chat.read(room)
    kept = [m for m in rows
            if m.get("from") and (m.get("text") or "").strip()
            and not m.get("react") and not m.get("ambient")]
    tail = kept[-COUNCIL_TAIL:]
    owners = seats.owner_names()
    peer = None
    suffix = []                     # newest-first two-party run
    for m in reversed(tail):
        frm = str(m["from"])
        if frm != seat:
            if frm.lower() in owners:
                break               # talking WITH the owner — a conversation
            if peer is None:
                peer = frm
            elif frm != peer:
                break               # a third voice ends the pair run
        suffix.append(m)
    if not peer or len(suffix) < 2 * COUNCIL_ROUNDS:
        return None
    names = [str(m["from"]) for m in suffix]
    mine = names.count(seat)
    changes = sum(1 for a, b in zip(names, names[1:]) if a != b)
    if min(mine, len(names) - mine) < COUNCIL_ROUNDS \
            or changes < 2 * COUNCIL_ROUNDS - 1:
        return None                 # a monologue + one reply is no ping-pong
    try:                            # already mid-meld with this peer: silent
        key = ".meld.%s.json" % seats._seat_key(seat)
        for n in os.listdir(chat.chat_dir()):
            if not n.endswith(key):
                continue
            st = pk.read_json(os.path.join(chat.chat_dir(), n), None) or {}
            if st.get("peer") == peer \
                    and st.get("status") in ("invited", "active"):
                return None
    except OSError:
        pass
    # fingerprint = the streak's START offset in KEPT (attributed) coordinates
    # — react/ambient rows and the COUNCIL_TAIL cap must never drift it (the
    # shipped total-based offset re-fired every turn once a streak outgrew the
    # cap and on any reaction row: wallpaper on exactly the agents deepest in
    # ping-pong; live-probed 2026-07-23). A saturated suffix (len ==
    # COUNCIL_TAIL) hides the true start — "deep" latches the PAIR: silent
    # while any latched fp names it, one fire when none does; a NEW streak
    # re-arms through its exact fp the turn it shows short of the cap (the
    # break row shifts every later offset). Offset, not first-row ts: the
    # second-resolution ts collides across two streaks inside one second.
    pair = "%s|%s" % (room, peer)
    deep = len(suffix) >= COUNCIL_TAIL
    fp = pair + "|deep" if deep else "%s|%d" % (pair, len(kept) - len(suffix))
    d = pk.read_json(_council_path())
    offered = [str(x) for x in (d.get("offered") or ())] \
        if isinstance(d, dict) else []
    if fp in offered \
            or (deep and any(x.startswith(pair + "|") for x in offered)):
        return None
    if persist:
        pk.write_json(_council_path(), {
            "v": 1, "ts": pk.now_ts(),
            "offered": (offered + [fp])[-COUNCIL_OFFER_CAP:]})
    d_peer = chat._dsan(peer)
    line = ("REFLEX: %d async rounds with %s in #%s — this is a council: "
            "converge live instead (helm chat council invite %s <topic> "
            "--wait; bounded blocking beats ping-pong). Fires once per "
            "streak." % (min(mine, len(names) - mine), d_peer, room, d_peer))
    if _is_review_streak(suffix):
        # THE CURE RIDES THE DETECTOR (integrator ruling 2026-07-24). The
        # premise that names this cure was keyed on its own CONCLUSION —
        # "per-case", "whole-object", "enumeration" — the words you have AFTER
        # you understand the spiral. Nobody in round four types those; they
        # type "they found another one". Three rounds of widening its keywords
        # was itself a per-case handler, so we stopped: the triggering
        # condition is a MEASURABLE EVENT this function already detects, and a
        # lesson with a deterministic trigger must ride the trigger rather than
        # wait to be searched for. Keywords stay right for the long tail; they
        # are the wrong tool for a condition we can simply observe.
        line += (" And 3+ rounds on ONE artifact usually means PER-CASE "
                 "handling — the next case is always outside the set you "
                 "enumerated, so consider whole-object validation plus an "
                 "honest refusal instead of a fifth patch.")
    return line, COUNCIL_WHISPER_ID


def _unrepeated(lines, seen, turn):
    """(lines to deliver, fingerprints delivered) — the same LINE, twice in a
    window, is not sent twice.

    THE REFLEX LANE HAD NO CONTENT IDENTITY. Its latches key on a reflex ID and
    its bucket, so one reflex whose steer text is unchanged could re-render the
    identical sentence on consecutive turns whenever its bucket moved — and the
    reader cannot tell a re-fire from a new fact when the words are the same.
    Fingerprinting the RENDERED line is what makes "changed content" fire and
    "same content" stay quiet, and it is the same identity the pinned lane has
    used all along (_pinned_fingerprint over the bytes the seat received), not
    a second mechanism.

    THE ALLOWANCE IS NOT ONE, and that is where an owner-asked contract and an
    owner complaint meet. The reflex lane is EXEMPT from the pinned lane's
    once-per-session suppression because a reflex fires on a signal that is
    live THIS turn, and silencing it at the moment it applies is the failure
    that exemption exists to prevent. Saying a live steer twice honours it.
    Saying it turn after turn is the wallpaper the complaint named. So an
    identical line is delivered REPEAT_ALLOWANCE times inside the window and
    withheld after, and the count resets once the window passes: a fact worth
    saying twenty turns apart is worth saying again.

    Suppression state rides in the session's existing seen-file. There is no
    second store.

    Fail-open: no session state -> everything delivers."""
    if seen is None:
        return list(lines), {}
    prior = seen.get("lines") or {}
    out, fps = [], {}
    for line in lines:
        fp = _pinned_fingerprint([line])
        if fp is None:
            out.append(line)
            continue
        # The copy being built is consulted first: three identical lines in
        # ONE turn are three sayings, and a filter that reads only the prior
        # turn's map counts them as one and delivers all three.
        rec = fps.get(fp) or prior.get(fp)
        shown, last = rec if isinstance(rec, list) and len(rec) == 2 else (0, 0)
        if not isinstance(shown, int) or not isinstance(last, int):
            shown, last = 0, 0
        if turn - last >= REPEAT_WINDOW_TURNS:
            shown = 0                      # out of the window: a fresh saying
        if shown >= REPEAT_ALLOWANCE:
            continue
        out.append(line)
        fps[fp] = [shown + 1, turn]
    return out, fps


def _pinned_fingerprint(lines):
    """The pinned lane's content identity — ONE definition, deliberately.

    gather DECIDES suppression with this hash and --explain REPORTS that
    decision; a second copy would drift silently and send the diagnostic
    straight back to lying about the very lane it exists to explain. Hash the
    rendered, budget-capped lines: exactly the bytes the seat received, not
    store entries the cap may have dropped. Empty lane -> None (there is no
    identity for content that was never rendered)."""
    return hashlib.sha256(
        "\n".join(lines).encode("utf-8")).hexdigest() if lines else None


def _sample_context(session, cwd):
    """(version, context, sources, runtime, unavailable) at sample time.

    Claude v3 reads ONLY the actual agent-process census selected by this hook's
    CLAUDE_PID and session. The hook subprocess's HOME/config env, catalog and
    transcript paths are categorically ineligible. Non-Claude harnesses retain
    frozen v2 until their process owner exposes an equivalent admitted-runtime
    resolver.
    """
    explicit_raw = str(os.environ.get("HELM_AGENT_HARNESS") or "").strip()
    explicit = explicit_raw.lower()
    if explicit_raw:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", explicit):
            harness, source = explicit, injection_schema.HARNESS_ENV_SOURCE
        else:
            harness = source = None
    elif str(os.environ.get("PI_CODING_AGENT") or "").lower() == "true":
        harness, source = "pi", injection_schema.PI_HARNESS_SOURCE
    elif os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE"):
        harness, source = "claude", injection_schema.CLAUDE_HARNESS_SOURCE
    else:
        harness = source = None
    if harness == "claude":
        try:
            pid = int(str(os.environ.get("CLAUDE_PID") or "").strip())
        except (TypeError, ValueError):
            pid = None
        if not pid or pid < 0:
            return (injection_schema.V3, {}, {}, None,
                    "agent-pid-unavailable")
        try:
            from .. import session as session_owner
            runtime, unavailable = session_owner.runtime_config_for_session(
                str(session or ""), pid, str(cwd or ""))
        except TimeoutError:
            raise
        except Exception:
            runtime, unavailable = None, "agent-runtime-unavailable"
        if not runtime:
            return injection_schema.V3, {}, {}, None, unavailable
        context = {name: runtime[name] for name in
                   ("session", "cwd", "harness", "config_home")}
        sources = {
            "session": runtime_config.SESSION_SOURCE,
            "cwd": runtime_config.CWD_SOURCE,
            "harness": runtime_config.HARNESS_SOURCE,
            "config_home": runtime["config_home_source"],
        }
        proof = {"pid": runtime["pid"], "proc_start": runtime["start"]}
        return injection_schema.V3, context, sources, proof, None

    context, sources = {}, {}
    if session:
        context["session"] = str(session)
        sources["session"] = injection_schema.SESSION_SOURCE
    if cwd and os.path.isabs(str(cwd)):
        context["cwd"] = os.path.realpath(os.path.expanduser(str(cwd)))
        sources["cwd"] = injection_schema.CWD_SOURCE
    if harness:
        context["harness"] = harness
        sources["harness"] = source
    env_key = injection_schema.CONFIG_HOME_ENV.get(harness)
    raw_home = str(os.environ.get(env_key) or "").strip() if env_key else ""
    if raw_home:
        context["config_home"] = os.path.realpath(os.path.expanduser(raw_home))
        sources["config_home"] = env_key
    return injection_schema.V2, context, sources, None, None


def _utf8_sample(sections):
    """Exact v2 text-mode cmd_inject stdout bytes plus each lane payload."""
    lanes = {name: len("\n".join(sections[name]).encode("utf-8"))
             for name in injection_schema.V2_LANES}
    rendered = "\n".join(line for name in injection_schema.V2_LANES
                           for line in sections[name])
    stdout = rendered + ("\n" if rendered else "")
    return {"encoding": "utf-8", "rendered_bytes": len(stdout.encode("utf-8")),
            "lane_bytes": lanes}


def _jit_plan(jit_entries, entries, routed=frozenset(), squeeze=None):
    """THE JIT LANE'S PLAN ARGUMENTS IN ONE PLACE. `--explain` and gather both
    read it, so the surface whose whole job is to say what reaches the seat
    cannot render the lane under a different cap than the seat receives — the
    drift _gate_plan's own docstring records, which it had here: `--explain`
    called _gate_plan with only the rider allowance and saw uncut, unbudgeted
    lines while gather cut and budgeted them. `routed` names the ids a notice
    kind routed this turn: the lane's only deterministic lines, so the only
    ones a rider rides on (task/2980)."""
    # `drop_why` is NOT overridden: it labels a RIDER the GATE allowance could
    # not carry, and that is still the gate budget here. Naming the lane
    # instead sent a reader to the wrong constant — the base drop has its own
    # sentence (jit_alarm) and its own number.
    return _gate_plan(jit_entries, entries, base_budget=_inject.JIT_BUDGET,
                      rider_budget=_inject.GATE_BUDGET,
                      line_cap=_inject.JIT_LINE_CAP, short=True,
                      riders_for=frozenset(routed), squeeze=squeeze)


def _jit_lane(jit_entries, entries, routed=frozenset(), squeeze=None):
    """The JIT lane's lines off the one lane model -> (lines, ledger ids,
    riders). Bases spend JIT_BUDGET and render at JIT_LINE_CAP off the
    statement's FIRST SENTENCE; riders spend GATE_BUDGET at the same cap and
    ride only on the `routed` rules, a keyword rule's gates going to the
    lane's one footer (lane_footer) by name.
    riders = [(gate_entry, rule_entry)] that rode as RIDERS, for the cooldown
    record (an earned gate records itself as any selected entry does; a
    MENTIONED gate was not delivered and records nothing).

    THE LANE HAD NO BUDGET. `JIT_CAP` bounded the COUNT of entries and nothing
    bounded their BYTES, so the lane's size was whatever four entries happened
    to be — and the entries it fires are the long ungloss'd ones, because
    length is not what selection weighs. A base the budget cannot carry is
    NAMED by jit_alarm rather than dropped in silence, the pinned lane's law
    applied to the lane that is 95% of the bytes."""
    rules = {str(e["id"]): e for e in jit_entries}
    lines, ids, riders, dropped = [], [], [], []
    plan = _jit_plan(jit_entries, entries, routed, squeeze)
    for kind, e, rule_id, _gate_id, line, lid, ok in plan:
        if not ok:
            if kind == "entry":
                dropped.append(store_typed_id(e))
            continue
        lines.append(line)
        ids.append(lid)
        if kind == "gate":
            riders.append((e, rules[rule_id]))
    # THE FOOTER and the alarm are both UNCHARGED and neither has a ledger id:
    # each is ABOUT the lane's lines, and an alarm about the budget that a
    # full lane could silence is the fault it exists to report.
    footer = lane_footer(plan)
    if footer:
        lines.append(footer)
    alarm = jit_alarm(dropped, _inject.JIT_BUDGET)
    if alarm:
        lines.append(alarm)
    return lines, ids, riders


def _rerank(prompt, jit_all, route_ids, project, session):
    """(jit_all, rel): the long-tail re-rank's turn (helm.relevance). It is
    handed the RAW prompt, because the scorer reads the turn in the form its
    labels had (`relevance.label_form`), which is not the keyword lane's
    substance.

    OFF costs one small settings read and changes nothing. SHADOW starts the
    turn's scorer over the candidates the keyword lane holds and changes
    nothing delivered. LIVE waits at most the bound: a score that arrived
    reorders the candidates by it (a routed line is deterministic, keeps its
    place and is never scored); one that did not leaves keyword order, and
    the caller marks the lane. Any trouble is `rel` None: keyword order."""
    try:
        from .. import relevance
        cfg = relevance.settings()
        if cfg["mode"] == "off":
            return jit_all, None
        routed = [e for e in jit_all if str(e["id"]) in route_ids]
        pool = [e for e in jit_all if str(e["id"]) not in route_ids]
        rel = relevance.at_prompt(
            prompt, pool, project=project, session=session, cfg=cfg,
            keyword_ids=[store_typed_id(e) for e in jit_all[:JIT_CAP]
                         if str(e["id"]) not in route_ids])
        if rel and rel["mode"] == "live" and rel["state"] == "scored":
            return routed + relevance.rerank(
                pool, rel["scores"], rel["threshold"], JIT_CAP), rel
        return jit_all, rel
    except Exception:                       # noqa: BLE001 — never a blocked turn
        return jit_all, None


#: Where the turn under construction is, for the soft deadline's row: the
#: arrival kind once classified and the last stage entered. Per process; the
#: hook is one turn per process.
CURRENT = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "CURRENT": "the turn under construction, cleared when every turn opens",
}


def _stage(name, **kw):
    CURRENT["stage"] = name
    CURRENT.update(kw)


def _lane_bytes(lines):
    return len("\n".join(lines).encode("utf-8")) if lines else 0


def _fit_jit(jit_entries, entries, routed, lane, other_bytes, cap):
    """THE ARRIVAL CAP, spent without a rank cut -> (lines, ids, riders,
    over_cap). `lane` is _jit_lane's first rendering; when the turn's
    non-contract bytes (other_bytes + the lane) exceed `cap`, every KEYWORD
    line is re-rendered at one shared, shorter cap, glossed or not, and none
    is dropped: keyword rank carries no relevance (13/16/13/11% at ranks 0-3,
    E2), so the fourth line is as likely to be the one that matters as the
    first. Routed lines are deterministic and never squeezed. The largest
    shared cap that fits is found by bisection between moments.LINE_FLOOR and
    the lane's own cap; a turn that cannot fit even at the floor keeps every
    line at the floor and reports over_cap (the moment ledger names it)."""
    lines = lane[0]
    if other_bytes + _lane_bytes(lines) <= cap:
        return lane + (False,)
    keyword = frozenset(str(e["id"]) for e in jit_entries
                        if str(e["id"]) not in routed)
    if not keyword:
        return lane + (True,)
    lo, hi = moments.LINE_FLOOR, max(moments.LINE_FLOOR, _inject.JIT_LINE_CAP)
    best = _jit_lane(jit_entries, entries, routed, squeeze=(lo, keyword))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        trial = _jit_lane(jit_entries, entries, routed, squeeze=(mid, keyword))
        if other_bytes + _lane_bytes(trial[0]) <= cap:
            best, lo = trial, mid
        else:
            hi = mid - 1
    return best + (other_bytes + _lane_bytes(best[0]) > cap,)


def _posture_id():
    from .. import ownernotice
    return ownernotice.POSTURE_ID


def _posture(session, seen, policy):
    """(lines, state) — the owner's posture for this turn (ownernotice).

    `state` is the moment spine's outcome word, or None when there is nothing
    to account for:
      delivered   lines rendered, and this context's memo now holds them
      in-context  a standing posture this context already has; zero bytes
      deferred    a change exists, but this arrival has no reader (a machine
                  broadcast, a Monitor expiry): the memo is untouched, so the
                  context's next working turn carries it
      unseen      the seen-memory could not be read at all, so the lines
                  render with nothing to remember them by (the inject's
                  fail-open law, which the pinned contract follows too)

    No session means no context to remember for, and nothing renders: plain
    stdin never whispers. A torn or unreadable seen FILE is not this case:
    _seen_load reads it as a fresh context, the lines render once, and the
    save rewrites the file, so the next turn is quiet. Fail-open: any trouble
    reading the posture renders nothing and never costs the turn."""
    if not session:
        return [], None
    try:
        from .. import ownernotice
        snap = ownernotice.snapshot()
        prior = seen.get("posture") if seen is not None else None
        lines, memo = ownernotice.turn_lines(snap, prior)
    except Exception:
        return [], None
    standing = (snap["away"].get("state") != "present"
                or snap["notice"].get("state") in (ownernotice.SET,
                                                   ownernotice.UNKNOWN))
    if not policy.contract:
        return [], (moments.DEFERRED if lines else None)
    if seen is None:
        return lines, ("unseen" if lines else None)
    seen["posture"] = memo
    if lines:
        return lines, moments.DELIVERED
    return [], (moments.IN_CONTEXT if standing else None)


def _gather_admitted(text, project=None, session=None, compare=None, cwd=None,
                     hook=None):
    """Construct one admitted turn while the caller holds the ledger's SH lock.

    -> dict {whisper: [line], pinned: [line], jit: [line], reflex: [line]}
    (each may be empty).
    The pinned lane leads with the RULES and closes with the WHO digest
    (_who_lines) — atomic (fires whole or not at all, against WHO_CAP alone,
    never PINNED_BUDGET), ledgered as who:operator — and then with the
    codex-only whispers (_sa_whisper, SA_LINES order) LAST when budget holds
    them. Each line has an independent content marker: pressure may delay its
    first delivery, then that unchanged line stays silent and returns budget to
    an unseen sibling for this context; each drops alone, never a premise.
    Fail-open per lane: a raising store/reflex yields that lane empty. Every
    call appends one fire-ledger row (silent turns log {"silent": true});
    session (the hook's session_id) rides the row when supplied and switches
    on the JIT cooldown (pinned/reflex exempt; no session = no cooldown).
    compare: the comparison backend to compare the local JIT lane against —
    the honest test-injection seam (no monkeypatch); None consults the registry
    (_active_compare), which is None unless HELM_CF_ENDPOINT is set. The
    comparison step runs LAST and only READS the computed local ids, so the
    returned sections are byte-identical whether the comparison is on or off.
    cwd: the hook's cwd — resolves the seat's home room for the council
    reach rung (_council_reach); None degrades that rung to the env room."""
    t0 = time.time()
    mutations = {"seen": None, "reflex": None, "coinage": None,
                 "council": None, "greet": False, "census": None,
                 "moments": None}
    _stage("context")
    try:
        version, context, context_sources, runtime, context_unavailable = \
            _sample_context(session, cwd)
    except TimeoutError:
        # Provenance runs before every seen/reflex/latch mutation. The admitted
        # intent remains as the durable witness; the wrapper suppresses output.
        raise
    # THE SEEN-STATE IS LOADED FIRST, because the ARRIVAL reads it (a
    # duplicate, a replay and the context's first turn all need memory) and
    # the pinned lane is what it governs.
    seen = None
    turn = 0
    if session:
        try:
            seen = _inject._seen_load(session)
            turn = seen["turn"] + 1
        except Exception:
            seen, turn = None, 0        # state trouble = no suppression, ever
    # WHO BEGAN THIS TURN (trigger design lane 2): the arrival kind decides
    # the budget, because this hook cannot see the phase (the phase is the
    # turn's own tool calls, which have not happened yet). classify never
    # raises; a parse failure reads as typed, the kind that withholds nothing.
    # WHAT THE KEYWORD MATCH AND THE PROMPT REFLEXES READ (task/2972) is the
    # arrival's substance: a notice's result body and event text, never its
    # fixed envelope. A typed prompt is its own substance, byte for byte.
    _stage("arrival")
    now = time.time()
    notice = promptshape.is_notice(text)
    arrival = moments.classify(text, seen, now)
    policy = moments.POLICY[arrival.kind]
    detected = moments.detect(arrival)
    CURRENT["arrival"] = arrival.kind
    match = arrival.substance
    # THE PROMPT CENSUS COUNTS WHAT THE KEYWORD LANE READS (task/2978): the
    # substance, recorded once the turn's ledger row has landed, so a word the
    # harness writes into every envelope is never "common" for being there.
    if (match or "").strip():
        mutations["census"] = match
    if seen is not None:
        if arrival.fingerprint and arrival.kind not in (
                moments.TYPED, moments.DUPLICATE):
            subs = [f for f in (seen.get("subs") or ()) if f != arrival.fingerprint]
            seen["subs"] = (subs + [arrival.fingerprint])[-moments.SUBS_KEEP:]
    if arrival.kind in moments.FAST_PATH:
        # THE ZERO-LINE FAST PATH, before the store loads: an empty,
        # duplicate or replayed notice says nothing the seat can act on. It
        # still writes its 0-byte row (CRITIC NIT 14) and its moments.
        return _fast_path(text, project, session, seen, turn, t0, version,
                          context, context_sources, runtime,
                          context_unavailable, notice, arrival, detected,
                          mutations, hook)
    _stage("store")
    try:
        pinned_entries, jit_all, entries, turn_df = _lanes(
            match if policy.jit else "", project=project)
    except Exception:
        pinned_entries, jit_all, entries, turn_df = [], [], [], None
    local_jit_ids = [str(e["id"]) for e in jit_all]  # the resolver's full pre-cap opinion — the comparison baseline
    # THE ROUTE (task/2978, generalized in lane 1): each moment this arrival
    # stands in delivers the entries that DECLARE it (`route:<id>`), ahead of
    # any keyword hit, off the same scope-fenced entry list. Deterministic by
    # construction: a detector, a declaration, no word that could match
    # anything else.
    _stage("lanes")
    route_ids = frozenset()
    rt = []
    if detected and entries:
        try:
            from .. import store
            rt = store.routed(entries, detected)
        except Exception:
            rt = []
        if rt:
            route_ids = frozenset(str(e["id"]) for e in rt)
            own = {(e["type"], str(e["id"])) for e in rt}
            jit_all = rt + [e for e in jit_all
                            if (e["type"], str(e["id"])) not in own]
    # WHO LEAVES THE CONTRACT (design section 3): the operator digest has a
    # reader only on a typed turn, so it rides the first typed turn of a
    # context under its own content identity, not every context's first turn.
    who = _who_lines() if policy.who else []
    pinned_lines, pinned_ids = [], []
    # WHO NO LONGER CHARGES PINNED_BUDGET, AND NO LONGER WALKS FIRST.
    #
    # It used to take its bytes off the top of the rules' budget and be
    # appended before them, so a 350-byte digest ABOUT the operator
    # structurally outranked every rule FROM him — and on 2026-08-28 that
    # accounting silently dropped four owner premises while the arithmetic
    # said they fit. That ordering was an accident of shared accounting, not
    # a decision (integrator ruling 2026-08-28): a digest is not a rule.
    #
    # Rules now walk FIRST against the whole PINNED_BUDGET; WHO follows and is
    # charged only against its own WHO_CAP. So if anything is ever squeezed
    # out, it is the digest and never an owner rule — the inversion of what
    # happened tonight.
    used = 0
    # A RULE ARRIVES WITH ITS GATE (task/1346): the walk is over the gate
    # PLAN — each pinned entry, then its gates as riders. An entry past the
    # budget ends the lane (the greedy law, unchanged); a RIDER past the
    # budget is replaced by a one-line marker naming it, so the seat sees a
    # rule that arrived without its gate rather than a rule that looks whole.
    # THE ONE LANE MODEL (_gate_plan): base lines first under the greedy
    # budget, riders only for rendered rules, markers typed. --explain reads
    # the same items; nothing here is re-derived from text.
    # A DROPPED PINNED RULE IS ANNOUNCED. This read `if not it[6]: continue`
    # — a plain skip, no marker, no counter, nothing. So a rule the budget
    # could not carry and a rule that does not exist produced the SAME
    # observable at every reader: absence. Measured 2026-08-28: three pinned
    # owner premises carried no gloss, truncated at LINE_CAP, and consumed
    # exactly the 1200-byte budget between them; the greedy walk then dropped
    # the remaining FIVE in silence, every turn, on every seat. Nobody could
    # have known without hand-summing eight glosses against a constant.
    #
    # The marker is deliberately NOT charged to `used`. It is a diagnostic
    # ABOUT the budget, not a rule competing for it, and charging it would let
    # a full lane silence its own alarm — the failure this exists to end. It
    # is ONE line however many rules were dropped, because an alarm that scales
    # with the fault is how a reader learns to skip it, and the COUNT is exact
    # even when the id list is capped (stalebot's law: the remainder is
    # COUNTED, never dropped).
    admitted = pinned_admission(pinned_entries, entries, who=who)
    dropped = admitted["dropped"]
    pinned_lines.extend(admitted["lines"])
    pinned_ids.extend(admitted["ids"])
    if admitted["footer"]:
        pinned_lines.append(admitted["footer"])
    used = admitted["used"]
    alarm = pinned_alarm(admitted)
    if alarm:
        pinned_lines.append(alarm)
    # WHO IS NOT PART OF THE CONTRACT ANY MORE (trigger design lane 2): it
    # walks after the rules as before, under WHO_CAP alone, but carries its
    # own content identity and rides only a typed turn, so the contract's
    # fingerprint below covers the rules alone.
    who_lines = list(admitted["who"])
    # ── the pinned dedup ────────────────────────────────────────────────
    # THE PINNED LANE IS THE SAME BYTES EVERY TURN AND THE SEAT ALREADY HAS
    # THEM. Measured on the fire-ledger 2026-08-04 across 3,331 turns: pinned
    # is 41.0% of helm's injection at 1,147 B/turn, and 1,157 of those bytes
    # were byte-identical on every prompt sampled. Re-delivering them 444
    # times in one session spent ~128k tokens restating what the seat read on
    # turn 1.
    #
    # SUPPRESSED ONLY WHERE THE SEAT PROVABLY HAS THESE EXACT RENDERED LINES.
    # Content identity covers lifecycle and mutation without a per-case list:
    # new/changed guidance has a new fingerprint and fires; fresh, sessionless,
    # unreadable, and compacted states have no matching marker and fire. The
    # compaction leg still calls _ledger.forget_session because the session id
    # survives while the context does not.
    # CONTENT IDENTITY, not a warmed boolean. Pinned guidance and the WHO
    # profile can change while the session survives; suppressing solely on
    # WHO_ID left a seat on stale guidance until its next compaction.
    # _pinned_fingerprint is shared with --explain ON PURPOSE: this line
    # DECIDES, that one REPORTS, and a second copy of the hash would drift.
    pinned_fingerprint = _pinned_fingerprint(pinned_lines)
    pinned_dropped, pinned_dropped_utf8 = [], 0
    pinned_deferred = []
    if pinned_lines and not policy.contract:
        # THE CONTRACT WAITS FOR A READER (trigger design lane 2): a machine
        # broadcast or a Monitor expiry has none, so the rules are neither
        # sent nor marked sent, and the context's next working turn carries
        # them. Seen live: a 12-line pinned block on a machine wake.
        pinned_deferred = list(pinned_ids)
        pinned_lines, pinned_ids = [], []
        pinned_fingerprint = None
        used = 0
    elif seen is not None and pinned_fingerprint \
            and seen.get("pinned") == pinned_fingerprint:
        pinned_dropped = list(pinned_ids)
        pinned_dropped_utf8 = len("\n".join(pinned_lines).encode("utf-8"))
        pinned_lines, pinned_ids = [], []
        used = 0
    # WHO ONCE PER CONTEXT, typed turns only (policy.who built it at all).
    who_fingerprint = _pinned_fingerprint(who_lines)
    who_state = None
    if who_lines:
        if seen is not None and seen.get("who") == who_fingerprint:
            who_state = moments.IN_CONTEXT
            who_fingerprint = None
        else:
            who_state = moments.DELIVERED
    # CODEX NUDGES HAVE SEPARATE PER-LINE CONTENT IDENTITIES. They walk after
    # the base because pressure may delay a FIRST delivery; a delivered line is
    # skipped before the next budget walk, so it cannot crowd out its unseen
    # sibling. Folding them into `pinned` would repay both on premise mutation;
    # one aggregate marker would repay an already-delivered partial subset.
    sa = _sa_whisper() if policy.contract else ()
    nudge_lines, nudge_ids, nudge_fingerprints = [], [], {}
    nudges_dropped, dropped_lines = [], []
    nudge_seen = seen.get("nudges", {}) if seen is not None else {}
    for line, wid in sa:
        fp = _pinned_fingerprint([line])
        if fp and nudge_seen.get(wid) == fp:
            nudges_dropped.append(wid)
            dropped_lines.append(line)
            continue
        if used + len(line) > _inject.PINNED_BUDGET:
            continue
        nudge_lines.append(line)
        nudge_ids.append(wid)
        nudge_fingerprints[wid] = fp
        used += len(line)
    nudges_dropped_utf8 = len("\n".join(dropped_lines).encode("utf-8"))
    pinned_lines += nudge_lines
    pinned_ids += nudge_ids
    contract_bytes = len("\n".join(pinned_lines).encode("utf-8"))
    if who_state == moments.DELIVERED:
        pinned_lines += who_lines
        pinned_ids.append(WHO_ID)
    n_jit = len(jit_all)      # pre-cap, pre-suppression candidate count
    df = None
    suppressed = []
    low = (match or "").lower()
    if session and seen is not None:
        try:
            from .. import store
            if jit_all:
                # THE SAME MAP THE LANE WAS RANKED WITH, not a second identical
                # pass. _lanes threads it out for exactly this consumer; the
                # fallback is here because a raising _lanes leaves it None and
                # the cooldown must still score rather than skip.
                df = turn_df if turn_df is not None else \
                    store._df_map(store._jit_candidates(entries))
                jit_all, cooled = _cooldown(jit_all, seen, turn, low, df,
                                            route_ids)
                suppressed = [str(e["id"]) for e, _ago in cooled]
        except Exception:
            seen, suppressed = None, []  # no cooldown, never a blocked turn
    jit_all, rel = _rerank(text, jit_all, route_ids, project, session)
    jit_entries = jit_all[:JIT_CAP]
    jit, jit_ids, riders = _jit_lane(jit_entries, entries, route_ids)
    # WHY EACH ENTRY FIRED, counted BEFORE the loop below overwrites the record
    # it is derived from. A fired id that ALREADY had a record cleared the 2x
    # escape; one that did not is a first delivery into this context.
    #
    # Without this the ledger cannot tell a working dedup from a leaking one.
    # Measured 2026-08-04 on the landed JIT change: post-land repeat share read
    # 28.5% against an age-matched pre-land 12.5%, and the excess was equally
    # consistent with (a) legitimate escapes, (b) correct post-boundary
    # re-fires, and (c) suppression leaking — three different verdicts on one
    # number, unresolvable because the row recorded WHICH ids fired and never
    # WHY. A feature justified by a measurement shipped without the instrument
    # to measure it; the pinned lane got suppressed_pinned for exactly this
    # reason and this lane did not.
    jit_why = None
    if seen is not None and jit_entries:
        prior = sum(1 for e in jit_entries if _cool_rec(seen, e))
        jit_why = {"escape": prior, "new": len(jit_entries) - prior}
    if seen is not None:
        try:
            for e in jit_entries:
                seen["fired"][str(e["id"])] = \
                    [turn, round(_turn_score(e, low, df, route_ids), 4)]
            # A RIDER IS A DELIVERY. It is recorded at its RULE's score, so a
            # later turn matching the gate on its own words is cooled like any
            # delivered entry — while a rule that re-fires (escape or a new
            # context) brings the gate again: the pairing, not the gate's own
            # history, decides.
            for g, rule in riders:
                seen["fired"][_cool_key(g)] = \
                    [turn, round(_turn_score(rule, low, df, route_ids), 4)]
            # A top-level content marker is not a cooldown record: cooldown
            # pruning must never make pinned guidance re-fire every N turns.
            # Changed rendered content replaces it after firing once; an
            # unchanged fingerprint survives until forget_session.
            if pinned_fingerprint:
                seen["pinned"] = pinned_fingerprint
            if who_fingerprint and who_state == moments.DELIVERED:
                seen["who"] = who_fingerprint
            if nudge_fingerprints:
                seen["nudges"].update(nudge_fingerprints)
            seen["turn"] = turn
            mutations["seen"] = (session, seen)
        except Exception:
            pass
    _stage("reflex")
    # THE REFLEX LANE BY ARRIVAL (trigger design lane 2): a prompt regex reads
    # this turn's text only where a seat or person wrote it, a counter or a
    # marker rides wherever the seat is working, and a reflex that names its
    # arrivals (`arrival: typed` — correction-language, owner feedback) fires
    # on those alone. The gate is inside reflex.fire, so a withheld reflex
    # keeps its latch.
    allow = frozenset(c for c, ok in (("prompt", policy.prompt_reflex),
                                      ("state", policy.state_reflex)) if ok)
    fired_reflex = []
    if allow:
        try:
            # session threads the recorder counters in so counter/latch
            # reflexes fire live (and latch per-session); no session -> v1
            # degrade, never raise
            fired_reflex = reflex.fire(match, project=project, session=session,
                                       persist=False, arrival=arrival.kind,
                                       allow=allow)
            mutations["reflex"] = (match, project, session, arrival.kind, allow)
        except Exception:
            fired_reflex = []
    steers = [_cap_steer("REFLEX: " + e["steer"]) for e in fired_reflex]
    reflex_ids = [str(e["id"]) for e in fired_reflex]
    nudge = None
    if policy.extras:
        # COINAGE COUNTS WHAT A PERSON COINS: typed turns only (task/2954's
        # typed gate). A hand-back's hyphenated words are a subagent's.
        try:
            nudge = _inject._coinage(text, entries, persist=False)
            if _coinage_candidates(text):
                mutations["coinage"] = (text, entries)
        except Exception:
            nudge = None
    if nudge:
        steers.append(nudge[0])
        reflex_ids.append(nudge[1])
    reach = None
    if policy.state_reflex:
        try:  # the council reach rung — cwd rides in from the hook (home room)
            reach = _council_reach(session, cwd, persist=False)
        except Exception:
            reach = None
    if reach:
        steers.append(reach[0])
        reflex_ids.append(reach[1])
        mutations["council"] = (session, cwd)
    # THE REFLEX LANE'S REPEAT FILTER, over the WHOLE lane and not per source:
    # a coinage nudge and a reflex steer are the same sentence to a reader, and
    # a filter applied per source cannot see that. It runs LAST because it
    # fingerprints the RENDERED line — the bytes the seat would receive, after
    # _cap_steer, which is the only identity that answers "did I already say
    # exactly this".
    kept, line_fps = _unrepeated(steers, seen, turn)
    repeated = [rid for line, rid in zip(steers, reflex_ids) if line not in kept]
    if repeated:
        steers = kept
        reflex_ids = [rid for rid in reflex_ids if rid not in repeated]
    if seen is not None and line_fps:
        try:
            seen.setdefault("lines", {}).update(line_fps)
            seen["turn"] = turn
            mutations["seen"] = (session, seen)
        except Exception:
            pass
    # THE BRIEF GREETS A PERSON: the day's first TYPED turn (the operator
    # digest's rule — on a machine wake it has no reader).
    whisper = _whisper(text, session, persist=False) if policy.who else []
    try:
        # The latch records the first eligible ATTEMPT, not whether that attempt
        # found anything to render. A quiet day (or failed brief read) must not
        # pay the compose cost again on every later turn; READY still decides
        # whether this staged mutation may commit.
        mutations["greet"] = bool(policy.who and session
                                  and (text or "").strip()) \
            and not _inject._greeted_today()
    except Exception:
        pass
    # THE OWNER'S POSTURE (task/3018): his away flag and his fleet notice,
    # rendered once per change in this context, at this working turn. It is
    # read here, where the seat already checks in, so nothing is woken for
    # it. It leads the output and sits OUTSIDE the arrival cap, like the
    # contract: it is his word, once per change, bounded by its own cap.
    _stage("posture")
    posture, posture_state = _posture(session, seen, policy)
    if posture_state and seen is not None:
        mutations["seen"] = (session, seen)
    # THE ARRIVAL CAP covers everything but the contract: the brief, WHO, the
    # JIT lane (routed and long-tail lines) and the reflex lane. Only the
    # long-tail lines give way, all of them equally (_fit_jit).
    _stage("assemble")
    other = _lane_bytes(whisper) + (len("\n".join(pinned_lines).encode("utf-8"))
                                    - contract_bytes) + _lane_bytes(steers)
    jit, jit_ids, riders, over_cap = _fit_jit(
        jit_entries, entries, route_ids, (jit, jit_ids, riders), other,
        policy.cap)
    # A LIVE TURN THAT FELL BACK SAYS SO, AFTER THE CAP: _fit_jit re-renders
    # the lane from its entries when it squeezes, so a marker put in earlier
    # would be dropped exactly on the turns the cap binds. One uncharged word.
    if rel and rel["state"] != "scored" and rel["mode"] == "live" and jit:
        from .. import relevance
        jit.insert(0, relevance.MARK)
    # THE POSTURE LEADS, AND NEITHER THE RE-RANK NOR THE CAP CAN REACH IT. It
    # joins `whisper` only here, after _rerank (which orders jit entries) and
    # _fit_jit (which squeezes the jit lane against `other`, computed above
    # without it), so no step that reorders or cuts can drop or move it. The
    # rendered order is fixed: posture lines, the day's greeting, the pinned
    # contract, then the jit lane led by the [keywords] marker when the live
    # re-rank fell back, then the reflex lane.
    whisper = posture + whisper
    sections = {"whisper": whisper, "pinned": pinned_lines, "jit": jit, "reflex": steers}
    row = _base_row(version, project, t0, sections, context, context_sources,
                    runtime, context_unavailable, arrival, detected, text,
                    hook, fast_path=False)
    row["cap"] = policy.cap
    if over_cap:
        row["over_cap"] = True
    if pinned_deferred:
        row["deferred_pinned"] = pinned_deferred
    if notice:
        # A NOTICE TURN SAYS SO. Absent on a typed turn, so every older row
        # reads as "cannot say"; with it, the share of fires on machine text
        # is one ledger read instead of a transcript join.
        row["notice"] = True
    if posture_state:
        # delivered / in-context / deferred / unseen: the posture's own proof,
        # in the moment spine's outcome words, so "the seat was told" and
        # "the seat already had it" are one ledger read apart.
        row["posture"] = posture_state
    if session:
        row["session"] = session
        # THE SESSION TURN, which is what separates a post-BOUNDARY re-fire
        # from a genuine first delivery: forget_session drops the whole file,
        # so the counter restarts at 1. A "new" fire at turn 1 of a session
        # that has been running for hours is the compaction/clear leg working;
        # the same fire at turn 40 is an entry the seat had never matched.
        # One int, and it is the difference between "the dedup reset" and
        # "the dedup leaked" — indistinguishable in every row written before.
        row["turn"] = turn
        # WHY THE EPOCH RESET, on the row that shows it resetting. The int
        # above says a boundary happened; this says which SessionStart said
        # so and whether a PreCompact record vouched for that. Without it,
        # answering "was this boundary real?" means cross-referencing the
        # fire ledger against the resume state and the hook's own stderr.
        # Present only while the marker is (it dies at the first seen-save),
        # so it lands on the reset row and not on the epoch behind it, and
        # ABSENT ON EVERY OLDER ROW — absence is "this row cannot say",
        # never "nothing vouched".
        epoch = seen.get("epoch") if seen is not None else None
        if epoch:
            row["epoch"] = epoch
    if suppressed:
        row["suppressed"] = suppressed  # the JIT cooldown's measurability
    if repeated:
        # The repeat filter's own proof: a lane that simply got quieter and a
        # lane whose lines were WITHHELD look identical without this row.
        row["suppressed_reflex"] = repeated
    if jit_why:
        row["jit_why"] = jit_why  # escape vs first-delivery, the dedup's proof
    if rel:
        # THE RE-RANK'S TURN KEY: the per-turn score cache and the relevance
        # ledger are keyed by it, so a late score joins this row afterwards.
        row["relevance"] = {k: rel[k] for k in ("turn", "mode", "state", "n",
                                                "waited_ms") if k in rel}
    suppressed_utf8 = {}
    if pinned_dropped:
        # The feature's own proof: absence and suppression are distinct, and
        # the saved bytes can be summed without re-running historical prompts.
        row["suppressed_pinned"] = pinned_dropped
        suppressed_utf8["pinned"] = pinned_dropped_utf8
    if nudges_dropped:
        # Separate from absent/non-codex/over-budget: these exact lines WERE
        # delivered in this context and their bytes are intentionally withheld.
        row["suppressed_nudges"] = nudges_dropped
        suppressed_utf8["nudges"] = nudges_dropped_utf8
    if suppressed_utf8:
        row["suppressed_utf8_bytes"] = suppressed_utf8
    fired = {"pinned": pinned_ids, "jit": jit_ids, "reflex": reflex_ids}
    greeting = whisper[len(posture):]
    if posture or greeting:
        # the posture line and the once-a-day greeting, each self-measurable
        fired["whisper"] = ([_posture_id()] if posture else []) \
            + ([WHISPER_ID] if greeting else [])
    if any(fired.values()):
        row.update({"fired": fired,
                    "candidates": bool(who) + len(sa) + len(pinned_entries)
                    + n_jit + len(steers)})
    else:
        row["silent"] = True
    # THE MOMENT LEDGER: one row per route this arrival stood in, with what
    # reached the seat there (written after READY, like every mutation).
    mutations["moments"] = moments.moment_rows(
        row, detected, *_outcomes(detected, arrival, rt, jit_ids, suppressed,
                                  pinned_ids, pinned_dropped, pinned_deferred,
                                  who_state, over_cap))
    # Comparison remains after READY: it is telemetry, never a prerequisite for
    # the durable evidence that authorizes returning these sections.
    backend = compare if compare is not None else _active_compare()
    return (sections, row, mutations,
            (backend, match, local_jit_ids, project, session))


def _outcomes(detected, arrival, routed_entries, jit_ids, cooled, pinned_ids,
              pinned_dropped, pinned_deferred, who_state, over_cap):
    """({route: outcome}, {route: ids}) for one assembled turn.

    A route whose FORM is content (the contract, a routed store line, WHO)
    is delivered, already in context, deferred, or NO-CONTENT when nothing
    exists to deliver: the last is the missed-moment report's numerator.
    An arrival kind that is only a budget is `applied`."""
    out, ids = {}, {}
    routed_by = collections.defaultdict(list)
    for e in routed_entries or ():
        from .. import store
        for r in store.routes(e):
            routed_by[r].append(str(e["id"]))
    rules = [i for i in pinned_ids if i != WHO_ID and not i.startswith("whisper:")]
    for rid in detected:
        mine = routed_by.get(rid, [])
        if rid == "arrival.first-context":
            ids[rid] = rules or list(pinned_dropped) or list(pinned_deferred)
            out[rid] = (moments.DELIVERED if rules else
                        moments.IN_CONTEXT if pinned_dropped else
                        moments.DEFERRED if pinned_deferred else
                        moments.NO_CONTENT)
        elif mine:
            ids[rid] = mine
            out[rid] = (moments.DELIVERED if any(i in jit_ids for i in mine)
                        else moments.IN_CONTEXT if any(i in cooled for i in mine)
                        else moments.NO_CONTENT)
        elif rid == "arrival.typed" and who_state:
            ids[rid] = [WHO_ID]
            out[rid] = who_state
        elif getattr(moments.BY_ID.get(rid), "needs", False):
            # a notice kind outside the table (a promptshape kind with no row
            # yet) is a budget-only moment, never a raise that empties a turn
            out[rid] = moments.NO_CONTENT
        else:
            out[rid] = moments.OVER_CAP if over_cap else moments.APPLIED
    return out, ids


def _base_row(version, project, t0, sections, context, context_sources,
              runtime, context_unavailable, arrival, detected, text, hook,
              fast_path):
    """The fields every inject ledger row carries (CRITIC BLOCK 1): the
    arrival, the routes it stood in, their raw-text signatures, the hook's
    real wall time, and `timed_out` / `fast_path`, false unless they happened.
    `hook` is the payload probe (moments.payload_shape): the keys beyond the
    known set and `source` when the harness sends one."""
    row = {"v": version, "ts": pk.now_ts(), "project": project,
           "elapsed_ms": round((time.time() - t0) * 1000, 1),
           "sample": _utf8_sample(sections), "context": context,
           "context_sources": context_sources,
           "arrival": arrival.kind, "moments": list(detected),
           "sig": moments.signatures(text), "timed_out": False,
           "fast_path": bool(fast_path), "wall_ms": moments.process_wall_ms()}
    if runtime:
        row["runtime"] = runtime
    if context_unavailable:
        row["context_unavailable"] = context_unavailable
    probe = _hook_probe(hook)
    if probe:
        row["hook"] = probe
    return row


#: The UserPromptSubmit keys MEASURED on 2.1.281; the probe records only a
#: key outside this set, and `source`, which the design needs proved live.
_HOOK_KEYS = frozenset(("session_id", "transcript_path", "cwd", "prompt_id",
                        "permission_mode", "hook_event_name", "prompt",
                        "effort"))


def _hook_probe(hook):
    """{source?, extra?} off the raw hook payload, or {} — never text."""
    if not isinstance(hook, dict):
        return {}
    out = {}
    if "source" in hook:
        out["source"] = str(hook.get("source"))[:32]
    extra = sorted(str(k) for k in hook if k not in _HOOK_KEYS
                   and k != "source")
    if extra:
        out["extra"] = extra[:12]
    return out


def _fast_path(text, project, session, seen, turn, t0, version, context,
               context_sources, runtime, context_unavailable, notice, arrival,
               detected, mutations, hook):
    """The zero-byte turn: no store load, no lanes, one 0-byte ledger row
    with fast_path set, the seen-state's turn and memory advanced, and the
    arrival's moments ledgered as `silent` (their form is nothing)."""
    sections = {lane: [] for lane in injection_schema.V2_LANES}
    row = _base_row(version, project, t0, sections, context, context_sources,
                    runtime, context_unavailable, arrival, detected, text,
                    hook, fast_path=True)
    row["silent"] = True
    row["cap"] = 0
    if notice:
        row["notice"] = True
    if session:
        row["session"] = session
        row["turn"] = turn
        epoch = seen.get("epoch") if seen is not None else None
        if epoch:
            row["epoch"] = epoch
    if seen is not None:
        seen["turn"] = turn
        mutations["seen"] = (session, seen)
    mutations["moments"] = moments.moment_rows(
        row, detected, {r: moments.SILENT for r in detected}, {})
    return sections, row, mutations, (None, arrival.substance, [], project,
                                      session)


def _apply_mutations(mutations):
    seen = mutations.get("seen")
    if seen:
        try:
            _seen_save(*seen)
        except Exception:
            pass
    reflex_plan = mutations.get("reflex")
    if reflex_plan:
        try:
            text, project, session, arrival, allow = reflex_plan
            reflex.fire(text, project=project, session=session, persist=True,
                        arrival=arrival, allow=allow)
        except Exception:
            pass
    coinage = mutations.get("coinage")
    if coinage:
        try:
            _inject._coinage(*coinage, persist=True)
        except Exception:
            pass
    council = mutations.get("council")
    if council:
        try:
            _council_reach(*council, persist=True)
        except Exception:
            pass
    if mutations.get("greet"):
        try:
            _mark_greeted()
        except Exception:
            pass
    if mutations.get("census"):
        promptcensus.record(mutations["census"])   # fail-open by contract
    if mutations.get("moments"):
        moments.record(mutations["moments"])       # fail-open by contract


def gather(text, project=None, session=None, compare=None, cwd=None,
           hook=None):
    """Admit, construct, durably evidence, then expose one injection turn.

    Admission failure happens before seen-state/reflex mutation. Construction or
    READY failure releases SH, retains whatever evidence exists, and returns no
    Helm injection. Once READY and its directory are durable, commit failure is
    recoverable cross-process and therefore does not suppress delivery.

    THE SOFT DEADLINE (moments.DEADLINE_S, armed by the CLI for the hook) is
    caught HERE when it lands in construction: the held attempt finishes with
    a `timed_out` row instead of the turn dying silently at the wrapper's
    TERM. It is disarmed before READY, so a durable row is never interrupted
    mid-write; one that lands before admission is the CLI's to ledger.
    """
    empty = {lane: [] for lane in injection_schema.V2_LANES}
    CURRENT.clear()
    _stage("turn-open")
    if session:
        # THE TURN BOUNDARY, before admission and before any counter is read:
        # this hook is the one place a turn begins, and the turn happened
        # whether or not this injection is admitted (record.turn_open).
        from .. import record
        record.turn_open(session, text)
    _stage("admit")
    attempt = _ledger_begin()
    if attempt is None:
        return empty
    try:
        sections, row, mutations, comparison = _gather_admitted(
            text, project=project, session=session, compare=compare, cwd=cwd,
            hook=hook)
    except moments.Deadline:
        moments.disarm_deadline()
        try:
            if not _ledger_finish(attempt, timed_out_row(
                    text, project, session, cwd, hook)):
                _ledger_abort(attempt)
        except Exception:
            _ledger_abort(attempt)
        CURRENT["ledgered"] = True
        return empty
    except Exception:
        _ledger_abort(attempt)
        return empty
    moments.disarm_deadline()
    CURRENT["ledgered"] = True
    try:
        ready = _ledger_finish(attempt, row)
    except Exception:
        _ledger_abort(attempt)
        return empty
    if not ready:
        return empty
    _apply_mutations(mutations)
    backend, prompt, local_jit_ids, project, session = comparison
    if backend is not None and (prompt or "").strip():
        try:
            _compare_run(backend, prompt, local_jit_ids,
                         project=project, session=session)
        except Exception:
            pass
    return sections


def timed_out_row(text, project, session, cwd, hook=None):
    """The row a turn the soft deadline took leaves behind: silent, 0 bytes,
    `timed_out`, the stage it died in and the arrival if it was classified,
    so the typed-turn timeout rate is one ledger read. Frozen v2 shape with
    the hook's own session/cwd only: the runtime census may be the very
    thing that ran out the clock, so it is not asked again."""
    context, sources = {}, {}
    if session:
        context["session"] = str(session)
        sources["session"] = injection_schema.SESSION_SOURCE
    if cwd and os.path.isabs(str(cwd)):
        context["cwd"] = os.path.realpath(os.path.expanduser(str(cwd)))
        sources["cwd"] = injection_schema.CWD_SOURCE
    kind = CURRENT.get("arrival")
    if kind is None:
        try:
            kind = moments.classify(text, None).kind
        except Exception:
            kind = None
    sections = {lane: [] for lane in injection_schema.V2_LANES}
    row = {"v": injection_schema.V2, "ts": pk.now_ts(), "project": project,
           "sample": _utf8_sample(sections), "context": context,
           "context_sources": sources, "silent": True, "timed_out": True,
           "fast_path": False, "arrival": kind,
           "stage": CURRENT.get("stage") or "unknown",
           "sig": moments.signatures(text),
           "wall_ms": moments.process_wall_ms()}
    if session:
        row["session"] = str(session)
    probe = _hook_probe(hook)
    if probe:
        row["hook"] = probe
    try:
        a = moments.classify(text, None)
        row["moments"] = moments.detect(a)
        moments.record(moments.moment_rows(
            row, row["moments"], {r: moments.TIMED_OUT for r in row["moments"]},
            {}))
    except Exception:
        pass
    return row


def render(sections):
    lines = (sections.get("whisper") or []) + sections["pinned"] \
        + sections["jit"] + sections["reflex"]
    return "\n".join(lines)


def _explain(text, project=None, session=None):
    """--explain: what WOULD fire for this text and WHY — the pinned budget
    walk, each JIT hit's per-probe DF score contributions (probe=1/df, summed
    then confidence-weighted — resolve_prompt's exact arithmetic), live reflex
    signals. With a session (--hook-json), cooled JIT entries render as
    "- id (cooldown, fired Nt ago)" off the seen-state READ-ONLY. A dry look:
    NO ledger row, NO state mutation (an explain must never count as a turn)."""
    from .. import store
    # THE ONE composition, same as gather and pinned_stats. This re-paired
    # _lane_entries with store.pinned itself, which is the second composition
    # pinned_lane() exists to abolish — behaviourally identical today, and the
    # exact line that would NOT follow the next change to the pairing. The
    # claim "the pairing lives in exactly one place" went in while this surface
    # still had its own; a cross-family read caught it false of its own commit.
    seen = _inject._seen_load(session) if session else None
    turn = seen["turn"] + 1 if seen else 0  # the would-be turn
    # gather's arrival, off the same seen-state, read-only (trigger lane 2)
    arrival = moments.classify(text, seen)
    policy = moments.POLICY[arrival.kind]
    detected = moments.detect(arrival)
    print("arrival: %s (contract %s, WHO %s, long-tail slots %d, cap %dB)%s"
          % (arrival.kind, "yes" if policy.contract else "deferred",
             "yes" if policy.who else "no", policy.jit, policy.cap,
             "; routes: " + ", ".join(detected) if detected else ""))
    if arrival.kind in moments.FAST_PATH:
        print("fast path: nothing is injected and the store is not loaded "
              "(a 0-byte ledger row is written)")
        return 0
    pinned_entries, entries = pinned_lane(project)
    df = lane_df(entries, project)
    # gather's substance, not a second reading of the prompt (task/2972)
    match = arrival.substance
    if promptshape.is_notice(text):
        print("notice turn: matching %d of %d chars (the envelope is set aside)"
              % (len(match or ""), len(text or "")))
    held = []
    jit_all = store.resolve_prompt(match if policy.jit else "",
                                   project=project, cap=len(entries),
                                   entries=entries, df=df, held=held)
    # gather's route, prepended the same way (task/2978, lane 1)
    rt = store.routed(entries, detected) if detected else []
    route_ids = frozenset(str(e["id"]) for e in rt)
    if rt:
        own = {(e["type"], str(e["id"])) for e in rt}
        jit_all = rt + [e for e in jit_all
                        if (e["type"], str(e["id"])) not in own]
    low = (match or "").lower()
    wd = None  # first-turn whisper: what WOULD lead, read-only (stamps no latch)
    if policy.who and session and (text or "").strip() \
            and not _inject._greeted_today():
        try:
            wd = _brief_digest()
        except Exception:
            wd = None
        if wd:
            print("whisper (first turn today):")
            print("  + " + wd)
    # the owner's posture, off the same seen-state, read-only: the memo the
    # real turn would record is computed and thrown away
    lines, state = _posture(session, dict(seen) if seen is not None else None,
                            policy)
    if lines:
        print("posture (%s, once per change in this context):" % state)
        for line in lines:
            print("  + " + line)
    elif state:
        print("posture: %s (nothing to say this turn)" % state)
    used = 0
    cut = False
    who = _who_lines() if policy.who else []
    sa = _sa_whisper() if policy.contract else ()
    n_pin = bool(who) + len(pinned_entries) + len(sa)
    if n_pin:
        print("pinned (%d candidate%s, budget %dB):" % (
            n_pin, "s"[:n_pin != 1], _inject.PINNED_BUDGET))
    # THE PINNED WALK IS COLLECTED BEFORE IT IS PRINTED, because whether the
    # lane fires is a property of the WHOLE rendered content and every line's
    # marker depends on that one verdict. The budget arithmetic below is
    # gather's, unchanged; only the printing moved after it.
    walk = []       # (fits, text, tag) in print order
    delivered = []  # gather's pinned_lines exactly — what the fingerprint covers
    # THE SAME ADMISSION RECORD GATHER RENDERS. This block used to rebuild the
    # lane itself — WHO first, charged against PINNED_BUDGET — so `--explain`
    # described a lane no seat receives. The one surface whose entire job is to
    # say what reaches the seat was the one disagreeing with what reaches it.
    # Rules walk first below; WHO is appended after them from the same record.
    admitted = pinned_admission(pinned_entries, entries, who=who)
    for kind, e, rule_id, gate_id, line, lid, ok in admitted["plan"]:
        # discovery attribution: every explain line names the ROOT it came
        # from (adopted / helm-global / adopted-project / project) — the tag
        # is explain-only decoration, never part of the budget arithmetic
        tag = (e.get("root") or "?") if e is not None else "?"
        if kind != "entry":
            tag = "gate of %s ← %s" % (rule_id, tag)
        if not ok:
            walk.append((False, "%s (over budget)" % store_typed_id(e), tag))
            continue
        used += len(line)
        delivered.append(line)
        walk.append((True, line, tag))
    if admitted["footer"]:
        delivered.append(admitted["footer"])
        walk.append((True, admitted["footer"], "footer"))
    # THE ALARM, then WHO — gather's order, from gather's record. explain used
    # to omit the alarm entirely, so a seat reading it could not learn that a
    # rule had been dropped at all.
    if admitted["dropped"]:
        alarm = pinned_alarm(admitted)
        delivered.append(alarm)
        walk.append((True, alarm, "budget alarm"))
    # THE LANE'S SUPPRESSION VERDICT — read off the SAME fingerprint gather
    # decides with, never a second copy of the arithmetic. Without this the
    # explain printed "+" for every pinned line on every turn while gather
    # was sending none of them: the one surface whose entire job is to say
    # what reaches the seat was the surface that could not see the dedup.
    # Strictly read-only, like the cooldown rung below it — an explain must
    # never stamp the marker it reports on.
    fp = _pinned_fingerprint(delivered)
    lane_suppressed = bool(seen and fp and seen.get("pinned") == fp)
    if delivered and not policy.contract:
        print("  (the contract waits: no reader on a %s turn; the context's "
              "next working turn carries it)" % arrival.kind)
        lane_suppressed = True
    if lane_suppressed:
        # AND THE BUDGET GOES BACK, exactly as gather's suppression arm does
        # (it empties pinned_lines and resets used=0 BEFORE the nudge walk).
        # Reporting the lane suppressed while still charging its bytes against
        # the tail made explain call the codex nudges over-budget on the very
        # turns gather was sending them — a HALF-modelled suppression, which
        # is its own kind of lie. Found by a review of a826ce87, on the
        # exact arm that review was asked to attack.
        used = 0
    for fits, text, tag in walk:
        mark = "-" if lane_suppressed or not fits else "+"
        why = (" (deferred)" if not policy.contract else
               " (already delivered this session)") \
            if fits and lane_suppressed else ""
        print("  %s %s%s%s" % (mark, text, why, " ← " + tag if tag else ""))
    # WHO, its own identity, typed turns only (trigger design lane 2)
    who_in = bool(seen and admitted["who"]
                  and seen.get("who") == _pinned_fingerprint(admitted["who"]))
    for i, l in enumerate(admitted["who"]):
        print("  %s %s%s%s" % ("-" if who_in else "+", l,
                               " (already delivered this context)" if who_in
                               else "", " ← know-your-user" if i == 0 else ""))
    if who and not admitted["who"]:
        print("  - %s (over WHO_CAP) ← know-your-user" % WHO_ID)
    # gather's exact nudge walk and its own content-identity verdict. Keeping
    # this separate from the base lane is what lets changed premises re-fire
    # without making unchanged codex guidance look new again.
    nudge_walk = []
    nudge_seen = seen.get("nudges", {}) if seen else {}
    for line, wid in sa:
        fp = _pinned_fingerprint([line])
        suppressed = bool(fp and nudge_seen.get(wid) == fp)
        fits = suppressed or used + len(line) <= _inject.PINNED_BUDGET
        if fits and not suppressed:
            used += len(line)
        nudge_walk.append((fits, suppressed, line, wid))
    for fits, suppressed, line, wid in nudge_walk:
        if suppressed:
            print("  - %s (already delivered this context) ← %s" % (line, wid))
        elif not fits:
            print("  - %s (over budget) ← codex-only nudge" % wid)
        else:
            print("  + %s ← %s" % (line, wid))
    if jit_all:
        print("jit (%d hit%s, cap %d):" % (len(jit_all), "s"[:len(jit_all) != 1], JIT_CAP))
    rank = 0
    top = []  # gather's jit_entries: the post-cooldown cap-4
    for e in jit_all:
        matched = store._probe_hits(e, low)[2]
        why = " ".join("%s=%.3f" % (p, 1.0 / df[p]) for p in matched)
        score = e["confidence"] * sum(1.0 / df[p] for p in matched)
        if str(e["id"]) in route_ids:
            why = "route: %s" % ",".join(
                sorted(store.routes(e) & set(detected)))
            score = _turn_score(e, low, df, route_ids)
        rec = _cool_rec(seen, e) if seen else None
        root = e.get("root") or "?"
        if _cooled(rec, turn, score):
            print("  - %s (cooldown, fired %dt ago) ← %s"
                  % (e["id"], turn - rec[0], root))
            continue
        mark, over = ("  + ", "") if rank < JIT_CAP else ("  - ", " (over cap)")
        if rank < JIT_CAP:
            top.append(e)
        rank += 1
        print(mark + str(e["id"]) + " [matched: " + why + "] score %.3f" % score
              + over + " ← " + root)
    # THE LONE-WORD LAW'S WITHHOLDINGS (task/2978), named rather than silent:
    # an entry held for a common lone word and an entry that never matched
    # are different answers to "why didn't X fire?"
    if held:
        from ..store.resolve import _LONE_COMMON
        print("held (%d, a lone word in more than %d%% of recent turns):"
              % (len(held), round(100 * _LONE_COMMON)))
    for e, probe, frac in held:
        print("  - %s [lone: %s in %.1f%% of turns]" % (e["id"], probe,
                                                         100 * frac))
    # THE GATE RIDERS, off gather's exact lane walk (_jit_lane), so what this
    # reports is what the seat receives: a gate that rides, one the budget
    # drops (loudly, with its marker), one the store no longer holds.
    if top:
        gated = [it for it in _jit_plan(top, entries, route_ids)
                 if it[0] != "entry"]
        if gated:
            print("gates (%d, rider allowance %dB):" % (len(gated), _inject.GATE_BUDGET))
        for kind, _e, rule_id, _g, line, _lid, _ok in gated:
            if kind == "gate-mention":
                # a keyword rule's gate: named in the footer, never a rider
                print("  ~ %s (gate of %s, named in the footer)"
                      % (line, rule_id))
                continue
            print("  %s %s" % ("+" if kind in ("gate", "gate-earned") else "-", line))
    allow = frozenset(c for c, ok in (("prompt", policy.prompt_reflex),
                                      ("state", policy.state_reflex)) if ok)
    fired_reflex = reflex.fire(match, project=project, session=session,
                               persist=False, arrival=arrival.kind,
                               allow=allow) if allow else []
    if fired_reflex:
        print("reflex:")
    for e in fired_reflex:
        p = e.get("path") or ""  # _global/reflexes vs a project's sibling dir
        origin = "project" if p and not p.startswith(home.global_dir() + os.sep) \
            else "helm-global"
        print("  + %s [%s] ← %s: %s" % (e["id"], e.get("signal") or "prompt",
                                        origin, e["steer"]))
    if not (wd or who or sa or pinned_entries or jit_all or fired_reflex):
        print("silent turn — nothing fires (salience law)")
    cb = _active_compare()  # read-only status; nothing is queried in a dry look
    if cb is not None:
        print("comparison: backend %s active — `helm inject --compare-report` for the "
              "local-vs-comparison divergence" % cb.name)
    return 0
