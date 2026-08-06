"""helm inject — the whisper cluster: JIT cooldown scoring, the coinage
3-strikes recorder, the first-turn brief whisper, the council-reach rung,
and the engine (gather/render/--explain).

Moved verbatim from the pre-split helm/inject.py. The names the test
contract monkeypatches on the PACKAGE (helm.inject PINNED_BUDGET /
_greeted_today / _today / _seen_load / _coinage) are read through the
package namespace (_inject.X) at call time — a from-import copy would
strand those patches at the package boundary.
"""
import hashlib
import os
import re
import time

from .. import home, pk, reflex
from .. import inject as _inject
from ._common import (
    COINAGE_CAP, COINAGE_STRIKES, COOLDOWN_ESCAPE,
    COUNCIL_OFFER_CAP, COUNCIL_ROUNDS, COUNCIL_TAIL, COUNCIL_WHISPER_ID,
    JIT_CAP, WHISPER_CAP, WHISPER_ID, WHO_ID,
)
from ._entries import (
    _entry_line, _lane_entries, _lanes, _sa_whisper, _who_lines,
)
from ._ledger import _ledger_append, _seen_save
from ._compare import _active_compare, _compare_run


def _jit_score(e, low, df):
    """resolve_prompt's exact arithmetic for ONE entry (matched probes' 1/df
    summed, confidence-weighted) — the cooldown's record + escape currency."""
    from .. import store
    matched = store._probe_hits(e, low)[2]
    return e["confidence"] * sum(1.0 / df[p] for p in matched)


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


def _cooldown(jit_all, seen, turn, low, df):
    """Split ranked JIT hits into (kept, cooled) for this turn. Pre-cap, so
    freed slots reach lower-ranked candidates. cooled = [(entry, n_turns_ago)]."""
    kept, cooled = [], []
    for e in jit_all:
        rec = seen["fired"].get(str(e["id"]))
        if rec and _cooled(rec, turn, _jit_score(e, low, df)):
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


def _coinage(text, entries):
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
        pk.write_json(_coinage_path(), {"v": 1, "ts": now, "terms": terms,
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
    w = b.get("waiting") or ()
    if w:  # verb agrees with the count: "1 needs you" / "2 need you"
        parts.append("%d need%s you" % (len(w), "s"[:len(w) == 1]))
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


def _whisper(text, session):
    """The first-turn whisper: on the day's FIRST session-bearing, non-empty
    turn, lead with the one-line brief digest. Latched once per day — the
    O(1) _greeted_today read means only the first turn pays a brief compose.
    The latch is stamped on the FIRST attempt BEFORE composing, so a slow or
    failed brief costs the day's greeting, never a per-turn read. Plain stdin
    (no session) never whispers, mirroring the cooldown gate. Fully fail-open:
    any brief/state trouble -> [] (no whisper, never a blocked turn)."""
    if not (session and (text or "").strip()):
        return []
    try:
        if _inject._greeted_today():
            return []
        _mark_greeted()          # latch the attempt: at most one brief read/day
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


def _council_reach(session, cwd):
    """The COUNCIL REACH rung — (line, ledger-id) or None. Premise
    council-is-the-number-one-feature + feature-and-rsh-must-both-be-wired:
    the recorded prior-harness failure was SALIENCE — the meld verb existed and
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


def gather(text, project=None, session=None, compare=None, cwd=None):
    """-> dict {whisper: [line], pinned: [line], jit: [line], reflex: [line]}
    (each may be empty).
    The pinned lane leads with the WHO digest (_who_lines) as its FIRST entry
    — atomic (fires whole or not at all against PINNED_BUDGET), ledgered as
    who:operator with its bytes in the pinned lane, cooldown-exempt — and
    closes with the codex-only whispers (_sa_whisper, SA_LINES order) LAST
    when the budget still holds them — each line drops alone under pressure
    (whisper:codex-sa, then whisper:codex-claim-start), never a premise.
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
    try:
        pinned_entries, jit_all, entries = _lanes(text, project=project)
    except Exception:
        pinned_entries, jit_all, entries = [], [], []
    local_jit_ids = [str(e["id"]) for e in jit_all]  # the resolver's full pre-cap opinion — the comparison baseline
    # THE SEEN-STATE IS LOADED BEFORE THE PINNED LANE IS BUILT, because the
    # pinned lane is what it now governs. Everything below is unchanged; only
    # the read moved earlier.
    seen = None
    turn = 0
    if session:
        try:
            seen = _inject._seen_load(session)
            turn = seen["turn"] + 1
        except Exception:
            seen, turn = None, 0        # state trouble = no suppression, ever
    who = _who_lines()
    pinned_lines, pinned_ids = [], []
    used = sum(len(l) for l in who)
    if who and used <= _inject.PINNED_BUDGET:  # the digest fires whole (FIRST entry) or not at all
        pinned_lines += who
        pinned_ids.append(WHO_ID)
    else:
        used = 0
    for e in pinned_entries:
        line = _entry_line(e)
        if used + len(line) > _inject.PINNED_BUDGET:
            break
        pinned_lines.append(line)
        pinned_ids.append(str(e["id"]))
        used += len(line)
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
    pinned_dropped, pinned_dropped_bytes = [], 0
    if seen is not None and pinned_fingerprint \
            and seen.get("pinned") == pinned_fingerprint:
        pinned_dropped = list(pinned_ids)
        pinned_dropped_bytes = sum(len(line) for line in pinned_lines)
        pinned_lines, pinned_ids = [], []
        used = 0
    sa = _sa_whisper()  # codex-only nudges — LAST in the budget; pressure
    for line, wid in sa:  # drops a nudge, never evicts a premise
        if used + len(line) > _inject.PINNED_BUDGET:
            continue
        pinned_lines.append(line)
        pinned_ids.append(wid)
        used += len(line)
    n_jit = len(jit_all)      # pre-cap, pre-suppression candidate count
    df = None
    suppressed = []
    low = (text or "").lower()
    if session and seen is not None:
        try:
            from .. import store
            if jit_all:
                df = store._df_map(store._jit_candidates(entries))
                jit_all, cooled = _cooldown(jit_all, seen, turn, low, df)
                suppressed = [str(e["id"]) for e, _ago in cooled]
        except Exception:
            seen, suppressed = None, []  # no cooldown, never a blocked turn
    jit_entries = jit_all[:JIT_CAP]
    jit = [_entry_line(e) for e in jit_entries]
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
        prior = sum(1 for e in jit_entries if str(e["id"]) in seen["fired"])
        jit_why = {"escape": prior, "new": len(jit_entries) - prior}
    if seen is not None:
        try:
            for e in jit_entries:
                seen["fired"][str(e["id"])] = [turn, round(_jit_score(e, low, df), 4)]
            # A top-level content marker is not a cooldown record: cooldown
            # pruning must never make pinned guidance re-fire every N turns.
            # Changed rendered content replaces it after firing once; an
            # unchanged fingerprint survives until forget_session.
            if pinned_fingerprint:
                seen["pinned"] = pinned_fingerprint
            seen["turn"] = turn
            _seen_save(session, seen)
        except Exception:
            pass
    try:
        # session threads the recorder counters in so counter/latch reflexes
        # fire live (and latch per-session); no session -> v1 degrade, never raise
        fired_reflex = reflex.fire(text, project=project, session=session)
    except Exception:
        fired_reflex = []
    steers = [_cap_steer("REFLEX: " + e["steer"]) for e in fired_reflex]
    reflex_ids = [str(e["id"]) for e in fired_reflex]
    try:
        nudge = _inject._coinage(text, entries)
    except Exception:
        nudge = None
    if nudge:
        steers.append(nudge[0])
        reflex_ids.append(nudge[1])
    try:  # the council reach rung — cwd rides in from the hook (home room)
        reach = _council_reach(session, cwd)
    except Exception:
        reach = None
    if reach:
        steers.append(reach[0])
        reflex_ids.append(reach[1])
    whisper = _whisper(text, session)  # the day's first turn leads with the brief
    sections = {"whisper": whisper, "pinned": pinned_lines, "jit": jit, "reflex": steers}
    row = {"v": 1, "ts": pk.now_ts(), "project": project,
           "elapsed_ms": round((time.time() - t0) * 1000, 1)}
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
    if suppressed:
        row["suppressed"] = suppressed  # the JIT cooldown's measurability
    if jit_why:
        row["jit_why"] = jit_why  # escape vs first-delivery, the dedup's proof
    if pinned_dropped:
        # The feature's own proof: absence and suppression are distinct, and
        # the saved bytes can be summed without re-running historical prompts.
        row["suppressed_pinned"] = pinned_dropped
        row["suppressed_bytes"] = {"pinned": pinned_dropped_bytes}
    fired = {"pinned": pinned_ids, "jit": [str(e["id"]) for e in jit_entries],
             "reflex": reflex_ids}
    if whisper:
        fired["whisper"] = [WHISPER_ID]  # the once-a-day greeting, self-measurable
    if any(fired.values()):
        row.update({"fired": fired,
                    "bytes": {k: sum(len(l) for l in sections[k]) for k in sections},
                    "candidates": bool(who) + len(sa) + len(pinned_entries)
                    + n_jit + len(steers)})
    else:
        row["silent"] = True
    _ledger_append(row)
    # the comparison leg — LAST, sections already assembled and NEVER touched
    # below; off (None) => one env read, zero cost. Fully guarded: fail-open.
    backend = compare if compare is not None else _active_compare()
    if backend is not None and (text or "").strip():
        try:
            _compare_run(backend, text, local_jit_ids, project=project, session=session)
        except Exception:
            pass
    return sections


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
    entries = _lane_entries(project)
    pinned_entries = store.pinned(project=project, entries=entries)
    jit_all = store.resolve_prompt(text, project=project, cap=len(entries),
                                   entries=entries)
    df = store._df_map(store._jit_candidates(entries))
    low = (text or "").lower()
    seen = _inject._seen_load(session) if session else None
    turn = seen["turn"] + 1 if seen else 0  # the would-be turn
    wd = None  # first-turn whisper: what WOULD lead, read-only (stamps no latch)
    if session and (text or "").strip() and not _inject._greeted_today():
        try:
            wd = _brief_digest()
        except Exception:
            wd = None
        if wd:
            print("whisper (first turn today):")
            print("  + " + wd)
    used = 0
    cut = False
    who = _who_lines()
    sa = _sa_whisper()
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
    if who:  # gather's exact atomic walk: the digest leads or drops whole
        w = sum(len(l) for l in who)
        if w > _inject.PINNED_BUDGET:
            walk.append((False, "%s (over budget)" % WHO_ID, "know-your-user"))
        else:
            used = w
            for i, l in enumerate(who):
                delivered.append(l)
                walk.append((True, l, "know-your-user" if i == 0 else None))
    for e in pinned_entries:
        line = _entry_line(e)
        cut = cut or used + len(line) > _inject.PINNED_BUDGET  # greedy walk: first overflow ends the lane
        # discovery attribution: every explain line names the ROOT it came
        # from (adopted / helm-global / adopted-project / project) — the tag
        # is explain-only decoration, never part of the budget arithmetic
        if cut:
            walk.append((False, "%s (over budget)" % e["id"],
                         e.get("root") or "?"))
        else:
            used += len(line)
            delivered.append(line)
            walk.append((True, line, e.get("root") or "?"))
    # THE LANE'S SUPPRESSION VERDICT — read off the SAME fingerprint gather
    # decides with, never a second copy of the arithmetic. Without this the
    # explain printed "+" for every pinned line on every turn while gather
    # was sending none of them: the one surface whose entire job is to say
    # what reaches the seat was the surface that could not see the dedup.
    # Strictly read-only, like the cooldown rung below it — an explain must
    # never stamp the marker it reports on.
    fp = _pinned_fingerprint(delivered)
    lane_suppressed = bool(seen and fp and seen.get("pinned") == fp)
    if lane_suppressed:
        # AND THE BUDGET GOES BACK, exactly as gather's suppression arm does
        # (it empties pinned_lines and resets used=0 BEFORE the nudge walk).
        # Reporting the lane suppressed while still charging its bytes against
        # the tail made explain call the codex nudges over-budget on the very
        # turns gather was sending them — a HALF-modelled suppression, which
        # is its own kind of lie. Found by @codex in review, on the
        # exact arm I asked it to attack.
        used = 0
    for fits, text, tag in walk:
        mark = "-" if lane_suppressed or not fits else "+"
        why = " (already delivered this session)" if fits and lane_suppressed else ""
        print("  %s %s%s%s" % (mark, text, why, " ← " + tag if tag else ""))
    for line, wid in sa:  # gather's exact tail walk: each nudge fits or drops
        if used + len(line) <= _inject.PINNED_BUDGET:
            used += len(line)
            print("  + %s ← %s" % (line, wid))
        else:
            print("  - %s (over budget) ← codex-only nudge" % wid)
    if jit_all:
        print("jit (%d hit%s, cap %d):" % (len(jit_all), "s"[:len(jit_all) != 1], JIT_CAP))
    rank = 0
    for e in jit_all:
        matched = store._probe_hits(e, low)[2]
        why = " ".join("%s=%.3f" % (p, 1.0 / df[p]) for p in matched)
        score = e["confidence"] * sum(1.0 / df[p] for p in matched)
        rec = seen["fired"].get(str(e["id"])) if seen else None
        root = e.get("root") or "?"
        if _cooled(rec, turn, score):
            print("  - %s (cooldown, fired %dt ago) ← %s"
                  % (e["id"], turn - rec[0], root))
            continue
        mark, over = ("  + ", "") if rank < JIT_CAP else ("  - ", " (over cap)")
        rank += 1
        print(mark + str(e["id"]) + " [matched: " + why + "] score %.3f" % score
              + over + " ← " + root)
    fired_reflex = reflex.fire(text, project=project, session=session, persist=False)
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
