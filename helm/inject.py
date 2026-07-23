#!/usr/bin/env python3
"""helm inject — the ONE active-fire surface. A harness hook calls this once
per turn with the prompt text; helm returns the context worth injecting:

  1. the pinned lane   (load_class=always priors, byte-budget-capped) — led by
     the WHO leg: the operator digest (whoami.load_profile(): technical level
     + top guidance, <=2 terse lines jointly capped at WHO_CAP) rides the
     pinned budget as the lane's FIRST entry (id who:operator) whenever a
     profile exists, so every agent warms from the profile on every turn.
     Cooldown-exempt (pinned-class); fail-open (no profile/garbled = absent).
  2. the JIT lane      (typed-store entries whose specific keywords match)
  3. reflex steers     (signals live this turn)

One resolver, every harness — claude/codex/opencode/hermes hooks all call the
same verb, which is what makes helm's knowledge fire wherever the operator
works. Salience law: no match -> EMPTY output (a silent turn costs nothing).

Wiring (`helm hooks install` writes this; docs/HOOKS.md has the manual recipe):
  claude   UserPromptSubmit hook: helm inject --hook-json  <- the FULL hook
           JSON on stdin (prompt/cwd/session_id, unknown keys tolerated);
           stdout becomes additionalContext. The project scope is DERIVED from
           the hook's cwd via the registry (longest-prefix over project paths,
           drain's longest-first law) — global-only when no project claims it.
  codex    notify/turn hook: same verb, plain prompt text on stdin

PERF (the per-prompt hot path): the store is parsed ONCE per call, not once per
lane — both lanes are fed from a persistent parsed-entry cache keyed by the
stat signature of every store file (the catalog-cache.json pattern), so the
steady state is ~N stat() calls + one JSON read instead of two full
frontmatter parses of the ~880-file adopted store. Cache lives under
$HELM_CACHE_DIR (default ~/.cache/helm); any cache trouble falls back to a
direct parse. The cached list feeds both lanes EXPLICITLY through
store.pinned/resolve_prompt's entries= parameter (the durable seam — no
monkeypatch, so a dropped cache regresses loudly in tests, not silently here).

FIRE-LEDGER (the measurement spine): every gather() appends ONE JSON line to
<helm home>/_global/.state/inject-ledger.jsonl — v:1, ts, project, session
(when the hook supplied one), fired entry IDS per lane (never prompt text),
bytes per lane, pre-cap candidate count, elapsed_ms; a no-fire turn logs
{"silent": true} instead of per-entry fields.
O(1) append, 5MB one-generation rotation (-> .1), and fail-open: ledger
trouble never blocks or slows the hook. `helm inject --explain` is the read
side — what WOULD fire for stdin text and why (the pinned budget walk, each
JIT hit's per-probe DF score contributions) — and writes NO ledger row.

SESSION COOLDOWN (the habituation guard, extended to the JIT lane): with a
--hook-json session, a JIT entry that fired is suppressed for COOLDOWN_TURNS
turns of THAT session unless its score now clears COOLDOWN_ESCAPE x its score
at last fire (the load-bearing-prior escape). State: one tiny JSON per session
at _global/.state/inject-seen/<session>.json — a turn counter + per-id
[turn, score] — stale session files pruned opportunistically on write. Pinned
and reflex lanes are EXEMPT; suppression happens pre-cap, so freed cap-4 slots
reach the candidates previously crowded out; suppressed ids ride the ledger
row ("suppressed") so the guard's effect is itself measurable. Plain stdin
(no session) = no cooldown, unchanged. --explain renders a cooled entry as
"- id (cooldown, fired Nt ago)" and mutates NO state.

COINAGE 3-STRIKES (the capture leg): every prompt is scanned by the NARROWEST
coinage detector (quoted 1-3 word phrases + lowercase hyphenated neologisms;
digits/underscores/dots/slashes/mixed-case — code identifiers and file paths —
disqualify structurally, as does harness machinery: a term seen as an
angle-bracket tag is skipped and a system-notification-shaped prompt is not
counted at all). Distinct turns per term are counted in
_global/.state/coinages.json; at COINAGE_STRIKES, a term missing the store
gets ONE reflex-lane define nudge (offer-if-present, write-a-candidate-if-away
— the AFK guard lives in the nudge text) and latches into the offered-set
FOREVER: one line per term, ever, max one nudge per turn. O(1) small-JSON
write, skipped entirely on candidate-free prompts. Both features fully
fail-open: any state trouble means no cooldown / no nudge, never a crash.

FIRST-TURN WHISPER (the warmth leg): on the FIRST inject turn of a calendar day
that carries a session and a non-empty prompt, the output LEADS with a one-line
brief digest (brief.compose in its tightest form, distinct BRIEF: prefix) so the
day's first agent turn surfaces "since you left" and relays it warmly in its own
voice; every later turn that day is silent. Latched once per day in
_global/.state/greeted.json ({day}, the drift-snapshot single-file pattern): the
HOT-PATH check is one small-JSON day-compare — O(1), and only the first turn pays
a brief compose, never every turn. The latch is stamped on the first attempt
BEFORE composing, so a slow/failed brief costs the day's greeting, not a per-turn
read. Budget-capped (WHISPER_CAP) and fully FAIL-OPEN: brief unavailable -> no
whisper, never a blocked hook. `--explain` renders it read-only (stamps nothing);
a quiet window (no sessions/knowledge/gates) whispers nothing but still latches.

LANE-REPORT (the lane-split eval's instrument): `helm inject --lane-report` is
a READ-ONLY analyzer over the whole fire-ledger — every fired id classified
against the current store into the facts cohort (lexicon / certain
decisions-of-record / references / the operator profile) vs the judgment
cohort (heuristic moves / sub-certain belief priors), with per-cohort fires,
byte estimate, session spread, cooldown suppression, and the silent-rate
trend. Delivery only: fires are not heeds — the outcome-marker protocol lives
in evals/2026-07-19-lane-split-eval.md. No ledger row, no state mutation.

COMPARISON BACKEND (the pluggability seam): the local keyword JIT resolver is
the AUTHORITY; a registered COMPARISON backend (_COMPARE_BACKENDS) runs in
PARALLEL and its ranked ids are LOGGED/COMPARED to the local lane, NEVER
trusted as truth (ARCHITECTURE.md Pluggability + the overlay-not-store law;
every backend declares its `source`). OFF by default: no HELM_CF_ENDPOINT ->
_active_compare() is None and the turn pays only one env read (zero cost — the
fleet-live default). Configured -> one divergence row per turn on
_global/.state/compare-ledger.jsonl (local-only vs compare-only vs agreed, ids
never prompt text). HARD: fail-open (a raising/slow/failing comparison backend
logs an error row and returns — never the authoritative local lane, never a
blocked turn) and the local lane is byte-identical whether the comparison is
on or off (the comparison step runs AFTER the sections are assembled and only
READS the computed local ids). `helm inject --compare-report` is the owner's
read side — the accumulated local-vs-comparison verdict, or "comparison
backend off (set HELM_CF_ENDPOINT)" when unconfigured. The Cloudflare
agentic-memory connector (CFCompareBackend) is a thin stdlib-urllib STUB: the
documented wire shape + the exact env to set; no real endpoint is ever called
in tests. A short-lived hook process can host no thread that outlives it, so
the comparison query is synchronous + hard-timeboxed (CF_TIMEOUT), never a
background job that dies with the process before it logs.

FAIL OPEN (docs/HOOKS.md law): a hook that cannot run helm must inject nothing,
never block — a store or reflex failure yields an empty lane and rc 0.
"""
import hashlib
import json
import os
import re
import sys
import time

from . import home, pk, reflex

PINNED_BUDGET = 1200  # bytes for the always lane — keep the constant tax tiny
JIT_CAP = 4
LINE_CAP = 400        # per-entry cap — the gloss fires, the full entry stays on disk
WHO_CAP = 350         # WHO digest's joint byte cap inside PINNED_BUDGET — the digest stays terse
WHO_ID = "who:operator"  # the digest's ledger id (the profile cohort in --lane-report)
LEDGER_MAX = 5 * 1024 * 1024  # ledger rotates here (one .1 generation)
CF_TIMEOUT = 1.5      # the CF comparison query's hard timebox (s) — a bounded turn, never a hung one

COOLDOWN_TURNS = 15   # a fired JIT entry cools for this many turns per session
COOLDOWN_ESCAPE = 2.0  # a ~2x score jump at fire-time re-fires through the window
SEEN_TTL = 7 * 86400  # inject-seen session files older than this pruned on write

COINAGE_STRIKES = 3   # distinct turns before the one-shot define nudge
COINAGE_CAP = 400     # tracked unoffered terms — oldest evicted past this

WHISPER_ID = "whisper:brief"  # the first-turn digest's ledger id
WHISPER_CAP = 240     # the one-line brief digest's byte cap (attention budget)

_CACHE_VERSION = 1    # bump when store parsing/derivation changes entry shape


def _entry_line(e):
    line = _entry_line_full(e)
    return line if len(line) <= LINE_CAP else line[:LINE_CAP - 1] + "…"


def _who_lines():
    """The WHO leg (know-your-user): operator digest off whoami.load_profile()
    — technical level + top guidance rendered as <=2 terse lines, jointly
    capped at WHO_CAP so the WHO digest never crowds the safety premises out of the
    pinned budget. guidance joins "; "-terse, so truncation keeps the TOP
    items (the list is owner-ordered). Fail-open: no profile / garbled /
    raising whoami -> [] (absent, never a blocked turn)."""
    try:
        from . import whoami
        p = whoami.load_profile()
        raw = []
        if p["technical_level"]:
            raw.append("WHO operator: " + p["technical_level"])
        if p["guidance"]:
            raw.append("WHO guidance: " + "; ".join(p["guidance"]))
    except Exception:
        return []
    lines, left = [], WHO_CAP
    for line in raw:
        if left < 40:  # no room left for a meaningful line
            break
        if len(line) > left:
            line = line[:left - 1] + "…"
        lines.append(line)
        left -= len(line)
    return lines


def _entry_line_full(e):
    # a provisional (xrev-cleared) entry FIRES like live but carries a visible
    # [provisional] PREFIX so the agent can weight it as not-yet-owner-ratified.
    # The prefix (not a suffix) survives LINE_CAP truncation — the tag can't be
    # the part that gets cut. candidates never reach here (load_all excludes them).
    pv = "[provisional] " if e.get("status") == "provisional" else ""
    t = e.get("type")
    if t == "prior":
        tag = "PREMISE" if e.get("class") == "certain" else "PRIOR %.2f" % e["confidence"]
        return "%s%s %s: %s" % (pv, tag, e["id"], e.get("statement") or "")
    if t == "lexicon":
        return "%sTERM %s: %s" % (pv, e.get("term") or e["id"], e.get("definition") or e.get("statement") or "")
    if t == "heuristic":
        return "%sMOVE %s: %s" % (pv, e["id"], e.get("statement") or "")
    if t == "reference":
        return "%sREF %s: %s" % (pv, e["id"], e.get("statement") or "")
    return "%s%s: %s" % (pv, e["id"], e.get("statement") or "")


def _cache_file(project=None):
    """One cache file per physical-root set (the dirs are in the key), so a
    tmp-store test or a --project call never collides with the live store's."""
    from . import store
    base = home.env("CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "helm")
    key = hashlib.sha1(("%s|%r" % (project or "", store.roots(project))).encode()).hexdigest()[:12]
    return os.path.join(base, "store-cache-%s.json" % key)


def _store_sig(project=None):
    """Every store file's [path, mtime_ns, size] — the cache validity key.
    ~N stat() calls (~3-5ms on the live 880-file store) vs ~100ms of parse.
    Lists, not tuples, so equality survives the JSON round-trip."""
    from . import store
    sig = []
    for root, _scope, d in store.roots(project):
        for _name, path in store._entry_files(root, d):
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig.append([path, st.st_mtime_ns, st.st_size])
    sig.sort()
    return sig


def load_entries(project=None):
    """store.load_all(), ONCE, through the persistent parsed-entry cache.
    Any cache trouble (missing, stale, torn, unwritable) degrades to a direct
    parse — a cache is never worth failing a turn over."""
    from . import store
    sig = _store_sig(project)
    path = _cache_file(project)
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("v") == _CACHE_VERSION and c.get("sig") == sig:
            return c["entries"]
    except Exception:
        pass
    entries = store.load_all(project=project)
    try:
        pk.atomic_write(path, json.dumps(
            {"v": _CACHE_VERSION, "sig": sig, "entries": entries}, ensure_ascii=False))
    except Exception:
        pass
    return entries


def _lanes(text, project=None):
    """(pinned_entries, ALL ranked jit matches, the cached entry list) off ONE
    store parse — the cached list feeds both lanes explicitly through the
    entries= seam. JIT comes back UNCAPPED so gather can both cap the lane and
    ledger the pre-cap candidate count; the raw list feeds the cooldown scorer
    and the coinage known-terms check without another parse."""
    from . import store
    entries = load_entries(project)
    return (store.pinned(project=project, entries=entries),
            store.resolve_prompt(text, project=project, cap=len(entries),
                                 entries=entries),
            entries)


def parse_hook_json(raw):
    """Claude Code UserPromptSubmit hook JSON -> (prompt, cwd, session). Unknown
    keys are tolerated (the hook payload grows); missing keys read as empty.
    Malformed/non-object input -> (None, None, None): the caller must FAIL OPEN
    (inject nothing, rc 0) — a garbled payload never blocks a turn."""
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, None
    if not isinstance(d, dict):
        return None, None, None
    return (str(d.get("prompt") or ""),
            str(d.get("cwd") or "") or None,
            str(d.get("session_id") or "") or None)


def project_for_cwd(cwd):
    """cwd -> registry project name by LONGEST-prefix match over project paths
    (drain's longest-first law: the deepest registered path that contains cwd
    wins). None = no project claims it — the caller stays global-only.
    Read-only registry access; fail-open (any trouble -> None)."""
    if not cwd:
        return None
    from . import registry
    try:
        want = os.path.abspath(os.path.expanduser(str(cwd)))
        best = None
        for key, rec in (registry.load().get("projects") or {}).items():
            # the FULL cwd lens, not just the canonical path — the registry
            # records sibling-worktree and alternate cwds in cv_scope
            # (codex-seat review: a hook fired in a worktree fell back to
            # global-only, silently dropping project premises + reflexes)
            prefixes = (rec.get("cv_scope") or {}).get("cwd_prefixes") \
                or [rec.get("path")]
            for pre in prefixes:
                pre = str(pre or "").rstrip("/")
                if pre and (want == pre or want.startswith(pre + "/")) \
                        and len(pre) > len(best[0] if best else ""):
                    best = (pre, str(rec.get("name") or key))
        return best[1] if best else None
    except Exception:
        return None


def _ledger_path():
    return os.path.join(home.global_dir(), ".state", "inject-ledger.jsonl")


def _append_jsonl(path, row, max_bytes):
    """ONE appended JSON line — the measurement-spine primitive (fire-ledger +
    compare-ledger). HARD LAWS: O(1) (one stat + one append, never a read), IDS
    never prompt text, one-generation rotation at max_bytes (-> .1), and
    FAIL-OPEN — a ledger that cannot be written must never block or slow the hook."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
        except OSError:
            pass  # no ledger yet
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _ledger_append(row):
    """The per-inject fire-ledger row — one line to inject-ledger.jsonl."""
    _append_jsonl(_ledger_path(), row, LEDGER_MAX)


# ---------------------------------------------------------------------------
# session cooldown — the habituation guard extended to the JIT lane
# ---------------------------------------------------------------------------

def _seen_dir():
    return os.path.join(home.global_dir(), ".state", "inject-seen")


def _seen_path(session):
    return os.path.join(_seen_dir(), pk.slug(str(session)) + ".json")


def _seen_load(session):
    """Per-session suppression state {turn: N, fired: {id: [turn, score]}}.
    Fail-open: an absent/torn/alien-shaped file reads as a FRESH session (no
    cooldown) — seen-state trouble must never block or crash the hook."""
    d = pk.read_json(_seen_path(session))
    fresh = {"turn": 0, "fired": {}}
    if not isinstance(d, dict) or not isinstance(d.get("turn"), int) \
            or d["turn"] < 0 or not isinstance(d.get("fired"), dict):
        return fresh
    fired = {str(i): r for i, r in d["fired"].items()
             if isinstance(r, list) and len(r) == 2
             and isinstance(r[0], int) and isinstance(r[1], (int, float))}
    return {"turn": d["turn"], "fired": fired}


def _seen_save(session, state):
    """Atomic write of one session's seen-state; records past the cooldown
    window are dropped (the file stays tiny) and stale SIBLING session files
    (mtime beyond SEEN_TTL) are pruned opportunistically. Entirely fail-open."""
    try:
        turn = state["turn"]
        state = {"v": 1, "ts": pk.now_ts(), "turn": turn,
                 "fired": {i: r for i, r in state["fired"].items()
                           if turn - r[0] <= COOLDOWN_TURNS}}
        pk.write_json(_seen_path(session), state)
        now = time.time()
        d = _seen_dir()
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if n.endswith(".json") and now - os.path.getmtime(p) > SEEN_TTL:
                    os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


def _jit_score(e, low, df):
    """resolve_prompt's exact arithmetic for ONE entry (matched probes' 1/df
    summed, confidence-weighted) — the cooldown's record + escape currency."""
    from . import store
    matched = store._probe_hits(e, low)[2]
    return e["confidence"] * sum(1.0 / df[p] for p in matched)


def _cooled(rec, turn, score):
    """The ONE cooled predicate (gather + --explain): a [turn, score] fire
    record within COOLDOWN_TURNS suppresses unless the new score clears
    COOLDOWN_ESCAPE x the score at last fire."""
    return bool(rec) and turn - rec[0] <= COOLDOWN_TURNS \
        and score < COOLDOWN_ESCAPE * rec[1]


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
    return isinstance(d, dict) and d.get("day") == _today()


def _mark_greeted():
    """Stamp today's latch (single-file overwrite, the drift-snapshot shape)."""
    pk.write_json(_greeted_path(), {"v": 1, "day": _today(), "ts": pk.now_ts()})


def _brief_digest():
    """The morning brief in its TIGHTEST form: ONE line off brief.compose()'s
    read-only dict — sessions since you left, the knowledge delta, the count of
    owner gates — under a distinct BRIEF: prefix so the receiving agent relays
    it warmly in its own voice. Seats are omitted (standing state, not news);
    the line points at `helm brief` for the full detail on demand (attention
    budget: relevance + availability). -> the WHISPER_CAP-capped line, or None
    when the window holds nothing worth a whisper (a quiet day)."""
    from . import brief
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
        if _greeted_today():
            return []
        _mark_greeted()          # latch the attempt: at most one brief read/day
        d = _brief_digest()
        return [d] if d else []
    except Exception:
        return []


def gather(text, project=None, session=None, compare=None):
    """-> dict {whisper: [line], pinned: [line], jit: [line], reflex: [line]}
    (each may be empty).
    The pinned lane leads with the WHO digest (_who_lines) as its FIRST entry
    — atomic (fires whole or not at all against PINNED_BUDGET), ledgered as
    who:operator with its bytes in the pinned lane, cooldown-exempt.
    Fail-open per lane: a raising store/reflex yields that lane empty. Every
    call appends one fire-ledger row (silent turns log {"silent": true});
    session (the hook's session_id) rides the row when supplied and switches
    on the JIT cooldown (pinned/reflex exempt; no session = no cooldown).
    compare: the comparison backend to compare the local JIT lane against —
    the honest test-injection seam (no monkeypatch); None consults the registry
    (_active_compare), which is None unless HELM_CF_ENDPOINT is set. The
    comparison step runs LAST and only READS the computed local ids, so the
    returned sections are byte-identical whether the comparison is on or off."""
    t0 = time.time()
    try:
        pinned_entries, jit_all, entries = _lanes(text, project=project)
    except Exception:
        pinned_entries, jit_all, entries = [], [], []
    local_jit_ids = [str(e["id"]) for e in jit_all]  # the resolver's full pre-cap opinion — the comparison baseline
    who = _who_lines()
    pinned_lines, pinned_ids = [], []
    used = sum(len(l) for l in who)
    if who and used <= PINNED_BUDGET:  # the digest fires whole (FIRST entry) or not at all
        pinned_lines += who
        pinned_ids.append(WHO_ID)
    else:
        used = 0
    for e in pinned_entries:
        line = _entry_line(e)
        if used + len(line) > PINNED_BUDGET:
            break
        pinned_lines.append(line)
        pinned_ids.append(str(e["id"]))
        used += len(line)
    n_jit = len(jit_all)      # pre-cap, pre-suppression candidate count
    seen = df = None
    suppressed = []
    turn = 0
    low = (text or "").lower()
    if session:
        try:
            from . import store
            seen = _seen_load(session)
            turn = seen["turn"] + 1
            if jit_all:
                df = store._df_map(store._jit_candidates(entries))
                jit_all, cooled = _cooldown(jit_all, seen, turn, low, df)
                suppressed = [str(e["id"]) for e, _ago in cooled]
        except Exception:
            seen, suppressed = None, []  # no cooldown, never a blocked turn
    jit_entries = jit_all[:JIT_CAP]
    jit = [_entry_line(e) for e in jit_entries]
    if seen is not None:
        try:
            for e in jit_entries:
                seen["fired"][str(e["id"])] = [turn, round(_jit_score(e, low, df), 4)]
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
    steers = ["REFLEX: " + e["steer"] for e in fired_reflex]
    reflex_ids = [str(e["id"]) for e in fired_reflex]
    try:
        nudge = _coinage(text, entries)
    except Exception:
        nudge = None
    if nudge:
        steers.append(nudge[0])
        reflex_ids.append(nudge[1])
    whisper = _whisper(text, session)  # the day's first turn leads with the brief
    sections = {"whisper": whisper, "pinned": pinned_lines, "jit": jit, "reflex": steers}
    row = {"v": 1, "ts": pk.now_ts(), "project": project,
           "elapsed_ms": round((time.time() - t0) * 1000, 1)}
    if session:
        row["session"] = session
    if suppressed:
        row["suppressed"] = suppressed  # the cooldown's own measurability
    fired = {"pinned": pinned_ids, "jit": [str(e["id"]) for e in jit_entries],
             "reflex": reflex_ids}
    if whisper:
        fired["whisper"] = [WHISPER_ID]  # the once-a-day greeting, self-measurable
    if any(fired.values()):
        row.update({"fired": fired,
                    "bytes": {k: sum(len(l) for l in sections[k]) for k in sections},
                    "candidates": bool(who) + len(pinned_entries) + n_jit + len(steers)})
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
    from . import store
    entries = load_entries(project)
    pinned_entries = store.pinned(project=project, entries=entries)
    jit_all = store.resolve_prompt(text, project=project, cap=len(entries),
                                   entries=entries)
    df = store._df_map(store._jit_candidates(entries))
    low = (text or "").lower()
    seen = _seen_load(session) if session else None
    turn = seen["turn"] + 1 if seen else 0  # the would-be turn
    wd = None  # first-turn whisper: what WOULD lead, read-only (stamps no latch)
    if session and (text or "").strip() and not _greeted_today():
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
    n_pin = bool(who) + len(pinned_entries)
    if n_pin:
        print("pinned (%d candidate%s, budget %dB):" % (
            n_pin, "s"[:n_pin != 1], PINNED_BUDGET))
    if who:  # gather's exact atomic walk: the digest leads or drops whole
        w = sum(len(l) for l in who)
        if w > PINNED_BUDGET:
            print("  - %s (over budget) ← know-your-user" % WHO_ID)
        else:
            used = w
            for i, l in enumerate(who):
                print("  + " + l + (" ← know-your-user" if i == 0 else ""))
    for e in pinned_entries:
        line = _entry_line(e)
        cut = cut or used + len(line) > PINNED_BUDGET  # greedy walk: first overflow ends the lane
        # discovery attribution: every explain line names the ROOT it came
        # from (adopted / helm-global / adopted-project / project) — the tag
        # is explain-only decoration, never part of the budget arithmetic
        if cut:
            print("  - %s (over budget) ← %s" % (e["id"], e.get("root") or "?"))
        else:
            used += len(line)
            print("  + %s ← %s" % (line, e.get("root") or "?"))
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
    if not (wd or who or pinned_entries or jit_all or fired_reflex):
        print("silent turn — nothing fires (salience law)")
    cb = _active_compare()  # read-only status; nothing is queried in a dry look
    if cb is not None:
        print("comparison: backend %s active — `helm inject --compare-report` for the "
              "local-vs-comparison divergence" % cb.name)
    return 0


# ---------------------------------------------------------------------------
# lane-split eval — the read-only cohort analyzer (--lane-report)
# ---------------------------------------------------------------------------

def _cohort(e):
    """The lane-split-eval razor (ember's-claude vision review, 2026-07-19):
    'facts' = knowledge that makes a capable model fluent — lexicon terms,
    certain priors (premises / decisions-of-record), references; 'judgment' =
    steering that could anchor it — heuristic moves, sub-certain belief
    priors. None = not cohortable (reflex machinery, unknown ids)."""
    t = e.get("type")
    if t in ("lexicon", "reference"):
        return "facts"
    if t == "prior":
        return "facts" if e.get("class") == "certain" else "judgment"
    if t == "heuristic":
        return "judgment"
    return None


def _read_jsonl(path):
    """Every row of a rotated jsonl (path + its .1 generation) oldest-first.
    Read-only; torn lines skipped; any file trouble reads as fewer rows."""
    rows = []
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        rows.append(r)
        except OSError:
            continue
    return rows


def _ledger_rows():
    """Every fire-ledger row oldest-first, rotated generation (.1) included."""
    return _read_jsonl(_ledger_path())


def lane_report(project=None):
    """The lane-split eval's accumulating instrument, READ-ONLY: every fired
    id in the ledger classified via _cohort against the CURRENT store (the
    operator digest who:operator = facts/profile), with per-cohort fires,
    distinct ids, byte estimate (today's rendering x fires — per-entry bytes
    are not ledgered), session spread, cooldown suppression, and the
    silent-rate first-half vs second-half trend. DELIVERY ONLY: the ledger
    logs fires, not heeds — anchoring is NOT measurable here; the
    outcome-marker protocol lives in evals/2026-07-19-lane-split-eval.md."""
    rows = _ledger_rows()
    try:
        by_id = {str(e["id"]): e for e in load_entries(project)}
    except Exception:
        by_id = {}
    who_bytes = sum(len(l) for l in _who_lines())

    def cohort(i):
        if i == WHO_ID:
            return "facts"  # the operator profile
        e = by_id.get(i)
        return (e and _cohort(e)) or "other"

    c = {k: {"fires": 0, "ids": set(), "bytes": 0, "sessions": set(),
             "suppressed": 0} for k in ("facts", "judgment", "other")}
    fired_rows = silent = 0
    sessions = set()
    halves = [[0, 0], [0, 0]]  # [silent, rows] per ledger half
    for n, r in enumerate(rows):
        half = halves[n * 2 // len(rows)]
        half[1] += 1
        s = r.get("session")
        if s:
            sessions.add(s)
        if r.get("silent"):
            silent += 1
            half[0] += 1
        else:
            fired_rows += 1
        for ids in (r.get("fired") or {}).values():
            for i in map(str, ids):
                d = c[cohort(i)]
                d["fires"] += 1
                d["ids"].add(i)
                e = by_id.get(i)
                d["bytes"] += len(_entry_line(e)) if e else \
                    (who_bytes if i == WHO_ID else 0)
                if s:
                    d["sessions"].add(s)
        for i in map(str, r.get("suppressed") or ()):
            c[cohort(i)]["suppressed"] += 1
    return {"rows": len(rows), "fired_rows": fired_rows, "silent": silent,
            "sessions": len(sessions), "halves": halves,
            "cohorts": {k: {"fires": d["fires"], "ids": len(d["ids"]),
                            "bytes": d["bytes"],
                            "sessions": len(d["sessions"]),
                            "suppressed": d["suppressed"]}
                        for k, d in c.items()}}


def _pct(num, den):
    return 100.0 * num / den if den else 0.0


def _lane_report(project=None):
    """--lane-report: lane_report() rendered as the cohort table. No ledger
    row, no state mutation — an analyzer must never count as a turn."""
    r = lane_report(project)
    if not r["rows"]:
        print("lane-report: no ledger rows yet — the instrument is unfired.")
        return 0
    print("lane-split cohorts — %d rows (%d fired, %d silent), %d sessions" % (
        r["rows"], r["fired_rows"], r["silent"], r["sessions"]))
    total_f = sum(d["fires"] for d in r["cohorts"].values())
    total_b = sum(d["bytes"] for d in r["cohorts"].values())
    fmt = "%-9s %6s %6s %5s %8s %6s %5s %5s %6s"
    print(fmt % ("cohort", "fires", "share", "ids", "bytes~", "share",
                 "sess", "supp", "supp%"))
    for k in ("facts", "judgment", "other"):
        d = r["cohorts"][k]
        print("%-9s %6d %5.1f%% %5d %8d %5.1f%% %5d %5d %5.1f%%" % (
            k, d["fires"], _pct(d["fires"], total_f), d["ids"], d["bytes"],
            _pct(d["bytes"], total_b), d["sessions"], d["suppressed"],
            _pct(d["suppressed"], d["fires"] + d["suppressed"])))
    h1, h2 = r["halves"]
    print("silent rate: %.1f%% overall | first half %.1f%% -> second half %.1f%%" % (
        _pct(r["silent"], r["rows"]), _pct(h1[0], h1[1]), _pct(h2[0], h2[1])))
    print("bytes~ = today's rendering x fires (per-entry bytes are not ledgered).")
    print("DELIVERY ONLY: fires are not heeds — the anchoring verdict needs the")
    print("outcome markers in evals/2026-07-19-lane-split-eval.md.")
    return 0


# ---------------------------------------------------------------------------
# pluggable comparison-resolver seam — the local JIT
# resolver is the AUTHORITY; a comparison backend runs in PARALLEL and its
# results are LOGGED/COMPARED, NEVER trusted as truth. A backend is anything with
# `name`, a mandatory `source` declaration (law 3: name your source), a
# configured() gate, and resolve(text, project) -> ranked entry-id list.
# ---------------------------------------------------------------------------

class LocalBackend:
    """The AUTHORITY: today's keyword JIT behind the one interface. resolve
    returns store.resolve_prompt's UNCAPPED ranked ids — the resolver's full
    opinion, which is what a comparison backend is measured against (the cap-4 is an
    injection-budget policy, not a finding). gather does NOT call this on the
    hot path (it reuses the ids it already computed, keeping the local lane
    byte-identical); it exists so the interface is real and the report can name
    the authority."""
    name = "local"
    source = "the typed store's keyword JIT resolver — authoritative; truth lives in files"

    def configured(self):
        return True

    def resolve(self, text, project=None, entries=None):
        from . import store
        if entries is None:
            entries = load_entries(project)
        return [str(e["id"]) for e in store.resolve_prompt(
            text, project=project, cap=len(entries), entries=entries)]


class CFCompareBackend:
    """A THIN, documented STUB adapter for Cloudflare's agentic-memory (owner
    decision #21: 'just got accepted to cloudflare's agentic memory private
    beta'). Zero-dep: stdlib urllib only. A COMPARISON BACKEND — a projection
    of the typed store queried in parallel and COMPARED, NEVER canonical (the
    `helm backend push` replica-write leg is the OTHER lane's, not this one's).

    OFF unless HELM_CF_ENDPOINT is set (=> the default, zero cost — one env
    read). The wire shape (documented; a real endpoint is NEVER called in tests):
        POST  <HELM_CF_ENDPOINT>              # the agentic-memory query URL
        Authorization: Bearer <HELM_CF_TOKEN> # when HELM_CF_TOKEN is set
        Content-Type: application/json
        {"query": <prompt>, "project": <scope or null>, "top_k": <cap>}
      -> {"results": [{"id": "..."}, ...]}    # ranked; helm reads the ids only
    The call is synchronous + hard-timeboxed (CF_TIMEOUT) because a short-lived
    hook process cannot host a thread that outlives it. resolve() raises on any
    trouble by design; _compare_run centralises the fail-open (one attribution
    point) so beta churn degrades to local silently."""
    name = "cf"
    source = "projection of the typed store; truth lives in files (Cloudflare agentic-memory replica, NEVER canonical)"

    def configured(self):
        return bool(home.env("CF_ENDPOINT"))

    def resolve(self, text, project=None, entries=None, top_k=JIT_CAP):
        endpoint = home.env("CF_ENDPOINT")
        if not endpoint or not (text or "").strip():
            return []
        import urllib.request
        body = json.dumps({"query": text, "project": project,
                           "top_k": top_k}).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        token = home.env("CF_TOKEN")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=CF_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(r["id"]) for r in (data.get("results") or ())
                if isinstance(r, dict) and r.get("id") is not None]


LOCAL_BACKEND = LocalBackend()        # the authority, behind the interface
_COMPARE_BACKENDS = (CFCompareBackend(),)  # registered comparison backends, order = priority


def _active_compare():
    """The first CONFIGURED comparison backend, or None (=> comparison OFF).
    THE hot-path gate: an unconfigured fleet pays only configured()'s env
    read(s) — no import, no object build, no I/O, no store parse. Fail-open: a
    backend whose configured() raises is skipped, never a blocked turn."""
    for b in _COMPARE_BACKENDS:
        try:
            if b.configured():
                return b
        except Exception:
            continue
    return None


def _compare_ledger_path():
    return os.path.join(home.global_dir(), ".state", "compare-ledger.jsonl")


def _compare_diverge(local_ids, compare_ids):
    """(local_only, compare_only, agreed) sorted id lists — one turn's
    divergence. local_only = keyword JIT found, comparison backend missed;
    compare_only = comparison backend found, keyword JIT missed (the beta's
    candidate signal — owner judges signal vs noise); agreed = both."""
    ls, cs = set(local_ids), set(compare_ids)
    return sorted(ls - cs), sorted(cs - ls), sorted(ls & cs)


def _compare_run(backend, text, local_ids, project=None, session=None):
    """Run the comparison backend in PARALLEL to the AUTHORITATIVE local
    resolve, COMPARE, and append ONE divergence row to the compare-ledger.
    HARD: local_ids is already computed and is NOT touched here; a
    raising/slow/failing backend logs an {error: true} row and returns — never
    the local lane, never a blocked turn. Ids only, never prompt text (the
    ledger law)."""
    t0 = time.time()
    row = {"v": 1, "ts": pk.now_ts(), "project": project,
           "backend": getattr(backend, "name", "?")}
    if session:
        row["session"] = session
    try:
        compare_ids = [str(i) for i in backend.resolve(text, project=project)]
    except Exception:
        row["error"] = True
        row["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        _append_jsonl(_compare_ledger_path(), row, LEDGER_MAX)
        return
    local_only, compare_only, agreed = _compare_diverge(local_ids, compare_ids)
    row.update({"local_n": len(local_ids), "compare_n": len(compare_ids),
                "agreed": agreed, "local_only": local_only,
                "compare_only": compare_only,
                "elapsed_ms": round((time.time() - t0) * 1000, 1)})
    _append_jsonl(_compare_ledger_path(), row, LEDGER_MAX)


def _compare_rows():
    return _read_jsonl(_compare_ledger_path())


def compare_report(project=None):
    """Aggregate the compare-ledger into the owner's local-vs-comparison
    verdict, READ-ONLY. `off` is True when NO comparison backend is configured
    AND no rows exist yet (the honest zero-state). Divergence ids ride a count
    map so the verdict is concrete ('CF found N entries keywords missed')
    rather than a vibe."""
    rows = _compare_rows()
    active = _active_compare()
    agg = {"turns": 0, "errors": 0, "agreed": 0,
           "local_only": 0, "compare_only": 0}
    lo_ids, co_ids = {}, {}
    for r in rows:
        if r.get("error"):
            agg["errors"] += 1
            continue
        agg["turns"] += 1
        agg["agreed"] += len(r.get("agreed") or ())
        for i in (r.get("local_only") or ()):
            agg["local_only"] += 1
            lo_ids[i] = lo_ids.get(i, 0) + 1
        for i in (r.get("compare_only") or ()):
            agg["compare_only"] += 1
            co_ids[i] = co_ids.get(i, 0) + 1
    return {"configured": active is not None,
            "backend": getattr(active, "name", None),
            "rows": len(rows), "local_only_ids": lo_ids,
            "compare_only_ids": co_ids, **agg}


def _hot_ids(counts, n=5):
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _compare_report(project=None):
    """--compare-report: compare_report() rendered for the owner. No ledger
    row, no state mutation, no live query — pure read over the accumulated
    ledger."""
    r = compare_report(project)
    if not r["configured"] and r["rows"] == 0:
        print("comparison backend off (set HELM_CF_ENDPOINT). No comparison "
              "backend is configured and the compare-ledger is empty —")
        print("the local keyword JIT resolver is the sole authority.")
        print("activate: export HELM_CF_ENDPOINT=<cloudflare agentic-memory query URL>"
              " [HELM_CF_TOKEN=<bearer>]")
        print("(the CF endpoint is the owner one-step; wire shape + env in "
              "docs/ENVIRONMENT.md).")
        return 0
    state = ("backend %s active" % r["backend"]) if r["configured"] \
        else "comparison backend currently OFF (past rows shown)"
    print("compare-report — %s, %d comparison turn%s (%d error%s degraded to local)" % (
        state, r["turns"], "s"[:r["turns"] != 1],
        r["errors"], "s"[:r["errors"] != 1]))
    print("  agreed       %6d  (both surfaced)" % r["agreed"])
    print("  local-only   %6d  (keyword JIT found, comparison backend missed)" % r["local_only"])
    print("  compare-only %6d  (comparison backend found, keyword JIT missed — "
          "owner judges signal vs noise)" % r["compare_only"])
    hot = _hot_ids(r["compare_only_ids"])
    if hot:
        print("  compare-only hot ids: " + ", ".join("%s x%d" % (i, n) for i, n in hot))
    if not r["configured"]:
        print("set HELM_CF_ENDPOINT to resume the comparison (docs/ENVIRONMENT.md).")
    return 0


def cmd_inject(args):
    """inject [--project P] [--json] [--explain] [--hook-json] [--lane-report]
    [--compare-report] — prompt text on stdin -> context lines. --hook-json reads
    the harness hook's FULL JSON on stdin instead (prompt/cwd/session_id) and
    derives --project from the cwd via the registry; malformed hook JSON injects
    nothing, rc 0 (fail-open). --explain prints what WOULD fire and why, sans
    ledger row. --lane-report renders the lane-split cohort table (read-only).
    --compare-report renders the local-vs-comparison divergence verdict (read-only)."""
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    if "--lane-report" in args:
        return _lane_report(project=project)
    if "--compare-report" in args:
        return _compare_report(project=project)
    session = scope_via = None
    if "--hook-json" in args:
        text, cwd, session = parse_hook_json(
            "" if sys.stdin.isatty() else sys.stdin.read())
        if text is None:
            return 0  # garbled hook payload: inject nothing, never block
        if project is None:
            project = project_for_cwd(cwd)
            scope_via = cwd if project else None
    else:
        text = "" if sys.stdin.isatty() else sys.stdin.read()
        for a in args:
            if not a.startswith("--") and a != project:
                text = a  # allow inline text for quick tests
    if "--explain" in args:
        if scope_via:
            print("[scope: %s via %s]" % (project, scope_via))
        return _explain(text, project=project, session=session)
    sections = gather(text, project=project, session=session)
    if "--json" in args:
        print(json.dumps(sections, ensure_ascii=False))
        return 0
    out = render(sections)
    if out:
        print(out)
    return 0
