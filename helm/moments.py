"""THE MOMENT SPINE: which moment a hook is standing in, what reaches the seat
there, and an instrument that can say when a moment passed without it.

The trigger design, lanes 1 and 2, task/2980 items 2 and 3. A line is right
when the agent is about to decide something the line governs, the line is not
already in context, and the line is whole. The moment is an act or an event, never a topic: keyword
rank carried no relevance on the E2 gold (13/16/13/11% at ranks 0-3), and all
three lines that changed an agent's behaviour in the consumer panel rode a
fixed event surface.

THREE THINGS LIVE HERE AND NOWHERE ELSE.

  * ROUTES, the one table of moments: route id -> hook event, detector, form,
    status. A store entry declares its moment with a `route:<id>` keyword cell
    (store.resolve.routes); the code owns the detectors, the store owns the
    words. Rows whose detector is not built yet stay in the table as
    `planned` with the lane that owes them, so the report can say UNBUILT
    instead of silently omitting a moment nobody watches.

  * The ARRIVAL at UserPromptSubmit: who began this turn and whether it says
    anything (classify). The hook cannot see the turn's phase, because the
    phase comes from tool calls the turn has not made yet, so the per-turn
    budget keys on the arrival kind (POLICY) and the phase stays a
    measurement axis.

  * The MISSED-MOMENT instrument (report). CRITIC BLOCK 1: a rate computed as
    detected-but-not-delivered over detected reads 0/0 and passes for a
    detector that never fires or a hook that timed out. So each route is
    reported as EXPECTED (its must-hit arms replayed through the live detector
    now, plus the turns an independent raw-text SIGNATURE says carried the
    moment), DETECTED and DELIVERED, and it is RED when detected < expected or
    when a window that carried its moment detected none. Every inject ledger
    row carries timed_out and fast_path, and the report prints p95 hook wall
    time and the typed-turn timeout rate, because the hook's own "[helm
    inject] TIMED OUT at 10s" was counted by no instrument at all.

Pure stdlib, fail-open on the hot path: classify never raises, and the ledger
append swallows its own trouble. Ids and kinds only are recorded, never prompt
text, like the inject ledger.
"""
import calendar
import collections
import hashlib
import json
import os
import re
import signal
import time

from . import home, machine_senders, pk, promptshape

# ---------------------------------------------------------------------------
# arrival kinds
# ---------------------------------------------------------------------------

TYPED = "typed"                    # a person (or a pane injection) typed it
PEER = "peer-wake"                 # a chat wake with a row from a seat
AGENT_MSG = "agent-message"        # a leading <agent-message> with no frame
HANDBACK = "handback"              # a subagent's final report
BG_RESULT = "bg-result"            # the seat's own background work reported
MACHINE = "machine-broadcast"      # a chat wake whose rows are all watchers
EXPIRED = "monitor-expired"        # the beacon Monitor's own expiry line
EMPTY = "empty"                    # a notice with nothing but fixed text
DUPLICATE = "duplicate"            # the same substance again in this context
REPLAY = "replay"                  # a catch-up of rows over an hour old

ARRIVALS = (TYPED, PEER, AGENT_MSG, HANDBACK, BG_RESULT, MACHINE, EXPIRED,
            EMPTY, DUPLICATE, REPLAY)

#: THE ZERO-BYTE ARRIVALS. A turn with nothing to say injects nothing and
#: skips the store load; it still writes a 0-byte ledger row (CRITIC NIT 14:
#: a fast path that skipped the ledger would move every B/turn bar without
#: moving a byte).
FAST_PATH = frozenset((EMPTY, DUPLICATE, REPLAY))

#: WHO SPOKE WHEN NO SEAT DID: every `who=` label a helm subsystem posts
#: under. A wake whose rows are ALL from these is a machine broadcast. This is
#: the ONE registry the tool-boundary delivery reads too
#: (helm/machine_senders.py), so the two cannot disagree about who a machine
#: is. Its walker arm (tests/test_delivery_truth.py) reads every post and dm
#: call site in helm/ and fails on a new literal label missing from it.
#: `agent` is NOT a machine: it is the name any poster with neither a seat
#: name nor a session gets, a person at a bare shell included, and an
#: unknown author is never assumed to be a machine.
#: VALIDATED ON THE E2 GOLD (design 5.1): 1 labelled pair fell on a
#: machine-only wake, strict 0, Fable-lenient 1 and contested; 0 consensus
#: positives. Seat-authored LAND broadcasts are NOT machine: a seat wrote them.
MACHINE_AUTHORS = machine_senders.SUBSYSTEMS

#: A catch-up is stale by AGE: a seat dark for a day replayed three stale
#: broadcasts in eight seconds and got three full blocks (consumer #375, #378,
#: #395). A row older than this when it arrives is a replay. INFERRED: an
#: hour is longer than any ordinary turn the seat could have been busy in.
#: NOT BY ORDER. "Older than the seat's last turn" reads a row queued behind
#: a long turn as a replay: rows posted during turn T1 are delivered one per
#: wake after it, so the second is stamped before T2 began and is still
#: news. MEASURED on the E2 replay: that rule silenced 185 more turns, two of
#: them carrying consensus positives. A re-delivered row is a DUPLICATE.
REPLAY_AGE_S = 3600

#: How many notice substances a context remembers for the duplicate check.
SUBS_KEEP = 16


# ---------------------------------------------------------------------------
# the per-arrival budget (design 5.1, stage 1)
# ---------------------------------------------------------------------------

Policy = collections.namedtuple("Policy", (
    "contract",       # the once-per-context pinned contract may ride
    "who",            # the operator digest may ride (typed only)
    "jit",            # long-tail slots; 0 = routes only (NEVER a rank cut)
    "prompt_reflex",  # prompt-regex reflexes may read this turn's text
    "state_reflex",   # counter / marker reflexes may ride
    "extras",         # coinage count, brief whisper, council reach
    "cap",            # byte ceiling of everything but the contract
))

#: THE CAP IS NEVER SPENT BY KEYWORD ORDER. Slots are 4 wherever the long tail
#: rides at all and 0 where the E2 gold says it carried nothing (task notices
#: 0 of 55, machine broadcasts 0 consensus, Monitor expiry only its routed
#: re-arm line). Cutting slots by rank would keep 6/12/16 of 20 positives at
#: 1/2/3 slots (MEASURED, design 6.2), so a turn over its cap SHORTENS every
#: long-tail line equally (inject._fit_jit) and keeps them all.
#: The contract rides only where a reader acts: a machine broadcast or a
#: Monitor expiry defers it to the context's next working turn (the 12-line
#: pinned block on a machine wake, seen live on the owner's chief seat).
POLICY = {
    TYPED: Policy(True, True, 4, True, True, True, 900),
    PEER: Policy(True, False, 4, True, True, False, 500),
    AGENT_MSG: Policy(True, False, 4, True, True, False, 500),
    HANDBACK: Policy(True, False, 4, True, True, False, 450),
    BG_RESULT: Policy(True, False, 0, False, True, False, 200),
    MACHINE: Policy(False, False, 0, False, False, False, 150),
    # 200, not the design's 150: the re-arm line the expiry routes to is 123 B
    # glossed and 168 B with the lane footer (MEASURED on the live store).
    EXPIRED: Policy(False, False, 0, False, False, False, 200),
    EMPTY: Policy(False, False, 0, False, False, False, 0),
    DUPLICATE: Policy(False, False, 0, False, False, False, 0),
    REPLAY: Policy(False, False, 0, False, False, False, 0),
}

#: The shortest a long-tail line is squeezed to before the turn is left over
#: its cap (and says so, `over_cap`). Below it a line is an id with no rule.
LINE_FLOOR = 110


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

Arrival = collections.namedtuple("Arrival", (
    "kind", "authors", "row_ts", "waiting", "kinds", "substance",
    "fingerprint", "first_context"))

_EVENT = re.compile(r"<event>(.*?)(?:</event>|\Z)", re.S)
_RESULT = re.compile(r"<result>(.*?)(?:</result>|\Z)", re.S)
_STATUS = re.compile(r"<status>\s*([a-z_-]+)\s*</status>")
_EXIT = re.compile(r"\(exit code (\d+)\)")
# helm's wake head, seats_delivery.deliver_any:
#   [helm chat #room → seat @ts] author: text (+N waiting — helm chat read ...)
_WAKE = re.compile(r"^\[helm chat(?: reaction)?(?: dm| #\S+)? → [^\]@]*?"
                   r"(?: @(\S+?))?\]\s*([^:\s]{1,80}):")
_WAITING = re.compile(r"\(\+\d+ waiting — helm chat read")
_FIXED = re.compile(r"^(?:\[helm chat\] |\[Monitor expired after "
                    r"|\[\d+ events? suppressed — )")
_FRAME = "[Subagent hand-back]"


def _iso_ts(value):
    """An ISO-8601 UTC stamp (YYYY-MM-DDTHH:MM:SSZ, fractions allowed) ->
    epoch seconds, or None."""
    try:
        s = str(value).rstrip("Z").split(".")[0]
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def fingerprint(text):
    """The content identity of a substance (sha256, 16 hex), or None."""
    text = (text or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else None


def _wake_rows(prompt):
    """(authors, row stamps, other lines, waiting) of a notice's event bodies:
    each helm chat wake line's author and @ts, and the count of event lines
    that are neither a wake nor harness-fixed text (the seat's own Monitor
    output: a gate result, a watched log line)."""
    authors, stamps, other = [], [], 0
    for body in _EVENT.findall(prompt):
        for line in body.splitlines():
            line = line.strip()
            if not line or _FIXED.match(line):
                continue
            m = _WAKE.match(line)
            if m:
                authors.append(m.group(2))
                stamps.append(m.group(1))
            else:
                other += 1
    return authors, stamps, other, bool(_WAITING.search(prompt))


def _bg_failed(prompt):
    """A background task that finished non-zero, failed or was killed."""
    st = _STATUS.search(prompt)
    if st and st.group(1) not in ("completed", "running"):
        return True
    ex = _EXIT.search(prompt)
    return bool(ex and ex.group(1) != "0")


def classify(prompt, seen=None, now=None):
    """-> Arrival. The kind of turn this prompt began, off its first bytes and
    the seat's seen-state (duplicate and replay need memory). Never raises: a
    parse failure reads as TYPED, the kind that withholds nothing.

    Precedence: the envelope decides the base kind (hand-back or agent
    message; a notice by its Monitor expiry, its wake rows, its result;
    otherwise typed), then the three zero-byte overrides apply to machine-begun
    turns only. A person typing the same words twice is not a duplicate."""
    try:
        return _classify(prompt or "", seen, time.time() if now is None else now)
    except Exception:                          # noqa: BLE001 — fail open
        return Arrival(TYPED, (), (), False, frozenset(), prompt or "", None,
                       _first_context(seen))


def _first_context(seen):
    return seen is None or not seen.get("pinned")


def _classify(prompt, seen, now):
    sub = promptshape.substance(prompt)
    kinds = promptshape.notice_kinds(prompt)
    authors, stamps, waiting = (), (), False
    if promptshape._is_handback(prompt):
        kind = HANDBACK if _FRAME in prompt[:600] else AGENT_MSG
    elif promptshape.is_notice(prompt):
        authors, stamps, other, waiting = _wake_rows(prompt)
        if promptshape.MONITOR_EXPIRED in kinds:
            kind = EXPIRED
        elif authors:
            machine = all(a in MACHINE_AUTHORS for a in authors)
            if not machine:
                kind = PEER
            elif waiting or other:
                # CRITIC NIT 9: the drain coalesces a burst into one wake
                # line, so "+N waiting" rows are authors nobody can see here.
                # Unknown authors are never assumed to be machines.
                kind = PEER if waiting else BG_RESULT
            else:
                kind = MACHINE
        elif sub.strip() or _bg_failed(prompt):
            kind = BG_RESULT
        else:
            kind = EMPTY
    else:
        kind = TYPED
    fp = fingerprint(sub) if kind != TYPED else None
    if kind in (PEER, AGENT_MSG, HANDBACK, BG_RESULT, MACHINE) and fp and seen \
            and fp in (seen.get("subs") or ()):
        kind = DUPLICATE
    elif kind in (PEER, MACHINE) and stamps and _stale(stamps, now):
        kind = REPLAY
    return Arrival(kind, tuple(authors), tuple(stamps), waiting, kinds, sub, fp,
                   _first_context(seen))


def _stale(stamps, now):
    """Every row in the wake is older than REPLAY_AGE_S: nothing in it is
    news to the seat now. One unparseable stamp is not stale (never silence
    what cannot be read)."""
    for s in stamps:
        t = _iso_ts(s)
        if t is None or now - t <= REPLAY_AGE_S:
            return False
    return True


# ---------------------------------------------------------------------------
# the ONE ROUTES table
# ---------------------------------------------------------------------------

#: `needs`: the route's form is CONTENT (the contract, a re-arm line), so a
#: detection with nothing to deliver is NO-CONTENT, a missed moment. A route
#: whose form is a budget or silence is `applied` when detected.
Route = collections.namedtuple("Route", "id family event detector form status "
                               "needs", defaults=(False,))

LIVE = "live"


def _kind_is(kind):
    return lambda a: a.kind == kind


# THE TABLE. `detector` reads an Arrival for the UserPromptSubmit rows; the
# other families have no detector yet and name the lane that owes one. A row
# is `live` only when its detector runs in the hook today. CRITIC BLOCK 4 is
# folded into the result rows: a non-zero Bash fires PostToolUseFailure (the
# conflict paths ARE in its `error`, MEASURED on Claude Code 2.1.281), while
# rg/grep with no match is a SUCCESS: PostToolUse with
# tool_response.returnCodeInterpretation "No matches found" (MEASURED).
ROUTES = (
    Route("arrival.first-context", "arrival", "UserPromptSubmit",
          lambda a: a.first_context and POLICY[a.kind].contract,
          "the pinned contract, once per context", LIVE, True),
    Route("arrival.typed", "arrival", "UserPromptSubmit", _kind_is(TYPED),
          "WHO once per context; correction, owner-feedback, coinage", LIVE),
    Route("arrival.peer-wake", "arrival", "UserPromptSubmit", _kind_is(PEER),
          "routes + long tail, 500 B", LIVE),
    Route("arrival.agent-message", "arrival", "UserPromptSubmit",
          _kind_is(AGENT_MSG), "routes + long tail, 500 B", LIVE),
    Route("arrival.handback", "arrival", "UserPromptSubmit",
          _kind_is(HANDBACK), "routed hand-back lines + long tail, 450 B", LIVE),
    Route("arrival.bg-result", "arrival", "UserPromptSubmit",
          _kind_is(BG_RESULT), "routed lines only, 200 B", LIVE),
    Route("arrival.machine-broadcast", "arrival", "UserPromptSubmit",
          _kind_is(MACHINE), "routed lines only, 150 B", LIVE),
    Route("arrival.monitor-expired", "arrival", "UserPromptSubmit",
          _kind_is(EXPIRED), "the re-arm line, once per context, 200 B", LIVE,
          True),
    Route("arrival.empty", "arrival", "UserPromptSubmit", _kind_is(EMPTY),
          "nothing (fast path)", LIVE),
    Route("arrival.duplicate", "arrival", "UserPromptSubmit",
          _kind_is(DUPLICATE), "nothing (fast path)", LIVE),
    Route("arrival.replay", "arrival", "UserPromptSubmit", _kind_is(REPLAY),
          "nothing (fast path)", LIVE),
    Route("arrival.canon-changed", "arrival", "UserPromptSubmit", None,
          "the new ruling's gloss, first", "planned: lane 4"),
    Route("act.review.dispatch", "act", "PreToolUse", None,
          "ROUTING, from the one routing premise", "planned: lane 4"),
    Route("act.review.enter", "act", "PreToolUse", None,
          "gate tree equals commit tree", "planned: lane 4"),
    Route("act.verdict", "act", "PreToolUse", None,
          "--cl asked by the verb", "planned: lane 5"),
    Route("act.land.compose", "act", "PreToolUse", None,
          "merge-base age, installed build, one whole gate", "planned: lane 4"),
    Route("act.spawn", "act", "PreToolUse", None,
          "brief in the row; model ruling", "planned: lane 4"),
    Route("act.spawn.nested", "act", "PreToolUse", None, "deny",
          "planned: task/1775"),
    Route("act.commit.body", "act", "PreToolUse", None, "deny",
          "planned: lane 5"),
    Route("act.pkill-f", "act", "PreToolUse", None,
          "deny with the bracketed rewrite", "planned: lane 5"),
    Route("act.transcript-read", "act", "PreToolUse", None, "cv first",
          "planned: lane 5"),
    Route("act.liveness-probe", "act", "PreToolUse", None,
          "seat liveness is environ", "planned: lane 4"),
    Route("act.store-write", "act", "PreToolUse", None,
          "few, short, rare keys", "planned: lane 4"),
    Route("act.owner-message", "act", "PreToolUse", None,
          "a channel he uses; inline summary first", "planned: lane 4"),
    Route("act.push.public", "act", "PreToolUse", None,
          "publication boundary", "planned: lane 5"),
    Route("result.merge-conflict", "result", "PostToolUseFailure", None,
          "stale-base-lane-reverts-shared-files, with the paths",
          "planned: lane 4"),
    Route("result.post-land-fail", "result", "PostToolUseFailure", None,
          "the known cause", "planned: lane 4"),
    Route("result.empty-scan", "result", "PostToolUse", None,
          "null-scan-is-not-absence before a destructive act",
          "planned: lane 4"),
    Route("result.cred-wall", "result", "PostToolUseFailure", None,
          "pacing posture", "planned: lane 4"),
    Route("diff.version", "diff", "PostToolUse", None, "read side first",
          "planned: lane 5"),
    Route("stop.permission-ask", "stop", "Stop", None,
          "one continuation per context", "planned: lane 6"),
    Route("stop.deferral", "stop", "Stop", None,
          "one continuation per context", "planned: lane 6"),
    Route("stop.wrapup-open", "stop", "Stop", None,
          "one continuation per context", "planned: lane 6"),
    Route("stop.beacon-missing", "stop", "Stop", None,
          "re-arm, then retire arrival.monitor-expired", "planned: lane 6"),
    Route("jit", "long-tail", "UserPromptSubmit", None,
          "keyword candidates by arrival slots", LIVE),
)

BY_ID = {r.id: r for r in ROUTES}


def detect(arrival):
    """The live arrival routes this Arrival stands in, in table order, then
    `arrival.<kind>` for every fixed notice kind promptshape.notice_kinds
    reports (task/2978's `notice:<kind>` channel: a fixed kind IS a moment,
    and a new kind needs a promptshape detector and a store cell, never a
    new branch here)."""
    out = []
    for r in ROUTES:
        if r.status == LIVE and r.detector is not None:
            try:
                if r.detector(arrival):
                    out.append(r.id)
            except Exception:                  # noqa: BLE001 — fail open
                continue
    for k in sorted(arrival.kinds or ()):
        rid = "arrival." + str(k)
        if rid not in out:
            out.append(rid)
    return out


# ---------------------------------------------------------------------------
# independent signatures and must-hit arms (the report's EXPECTED column)
# ---------------------------------------------------------------------------

_SIG_AUTHOR = re.compile(r"\[helm chat[^\]]*\]\s*([^:\s]+):")


def _sig_machine(p):
    names = _SIG_AUTHOR.findall(p)
    return bool(names) and "waiting —" not in p \
        and all(n in MACHINE_AUTHORS for n in names) \
        and "[Monitor expired after " not in p


#: DELIBERATELY DUMB. A signature is a raw substring test that says the moment
#: happened, written independently of classify so a regression in the
#: envelope parser shows as expected > detected instead of both going quiet
#: together. A signature firing where the detector did not is either a detector
#: gap or a signature false positive; RED makes a person look, which is the
#: point. State-dependent routes (first-context, duplicate, replay) have no
#: signature and are held to their arms.
SIGNATURES = {
    "arrival.monitor-expired": lambda p: "[Monitor expired after " in p,
    "arrival.handback": lambda p: _FRAME in p[:600]
    and p.lstrip().startswith("<agent-message"),
    "arrival.machine-broadcast": _sig_machine,
    "arrival.typed": lambda p: not p.lstrip().startswith("<"),
}


def signatures(prompt):
    """The route ids whose signature this raw prompt carries."""
    p = prompt or ""
    out = []
    for rid, sig in SIGNATURES.items():
        try:
            if sig(p):
                out.append(rid)
        except Exception:                      # noqa: BLE001
            continue
    return out


def _wake(author, text, room="#helm", ts="2026-09-24T01:00:00Z", tail=""):
    return ("<task-notification>\n<task-id>b0</task-id>\n<summary>Monitor "
            "event: \"seat inbox beacon\"</summary>\n<event>[helm chat %s → "
            "seat @%s] %s: %s%s</event> If this event is something the user "
            "would act on, notify them.</task-notification>"
            % (room, ts, author, text, tail))


_EXPIRY = ("<task-notification>\n<task-id>b0</task-id>\n<summary>Monitor "
           "event: \"seat inbox beacon\"</summary>\n<event>[Monitor expired "
           "after 30m with 3 events delivered. Re-arm it if you still need "
           "the watch.]</event>\n</task-notification>")
_HANDBACK_ARM = ("<agent-message from=\"a1\">\n  [Subagent hand-back] The text "
                 "below is the final report of a subagent you launched. The "
                 "report follows:\n  Built the lane; focused run green at tip "
                 "abc123.\n</agent-message>")
_BG_FAIL = ("<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>t1"
            "</tool-use-id>\n<output-file>/tmp/x.output</output-file>\n<status>"
            "completed</status>\n<summary>Background command \"run the gate\" "
            "completed (exit code 1)</summary>\n</task-notification>")
_BG_OK = ("<task-notification>\n<task-id>b2</task-id>\n<tool-use-id>t2"
          "</tool-use-id>\n<output-file>/tmp/y.output</output-file>\n<status>"
          "completed</status>\n<summary>Background command \"wait for it\" "
          "completed (exit code 0)</summary>\n</task-notification>")
_DELIVERED = ("<task-notification>\n<task-id>a2</task-id>\n<status>completed"
              "</status>\n<summary>Agent \"x\" finished</summary>\n<result>This "
              "agent's report was delivered to you as a message from a2."
              "</result>\n</task-notification>")

#: MUST-HIT ARMS, one or more per live arrival route: prompts shaped on the
#: real envelopes (E2 turns, structure only, no fleet text) that the route's
#: detector must fire on. `seen` supplies the memory a state route needs.
#: The report replays these through the LIVE detector every time it runs, so a
#: detector that stops firing reads RED the same day.
ARMS = {
    "arrival.first-context": [(_wake("peer-seat", "review ready?"), None)],
    "arrival.typed": [("you were at 88% four hours ago, remember", None),
                      ("please rescue him", {"pinned": "x"})],
    "arrival.peer-wake": [(_wake("peer-seat", "ready for your verdict"),
                           {"pinned": "x"}),
                          (_wake("proxywatch", "proxy health CHANGED",
                                 tail=" (+3 waiting — helm chat read)"),
                           {"pinned": "x"})],
    "arrival.agent-message": [("<agent-message from=\"a2\">LAND 247 at HEAD "
                               "abc is not clean.</agent-message>",
                               {"pinned": "x"})],
    "arrival.handback": [(_HANDBACK_ARM, {"pinned": "x"})],
    "arrival.bg-result": [(_BG_FAIL, {"pinned": "x"})],
    "arrival.machine-broadcast": [
        (_wake("proxywatch", "proxy health CHANGED codex probe=healthy"),
         {"pinned": "x"}),
        (_wake("beacons", "seat reachability CHANGED"), {"pinned": "x"})],
    "arrival.monitor-expired": [(_EXPIRY, {"pinned": "x"})],
    "arrival.empty": [(_DELIVERED, {"pinned": "x"}), (_BG_OK, {"pinned": "x"})],
    "arrival.duplicate": [(_HANDBACK_ARM, {
        "pinned": "x",
        "subs": [fingerprint(promptshape.substance(_HANDBACK_ARM))]})],
    "arrival.replay": [(_wake("peer-seat", "old news",
                              ts="2026-01-01T00:00:00Z"), {"pinned": "x"})],
}


#: The clock the arms are replayed at: their stamps are fixed, so their age is.
ARMS_NOW = "2026-09-24T01:05:00Z"


def replay_arms(detectors=None, now=None):
    """{route: (expected, detected, control_fires)} for every route with arms,
    through the LIVE detectors (or `detectors`, {route: fn}, for a test that
    plants a broken one). A control fire is this route's detector firing on
    ANOTHER route's arm: the must-miss half."""
    detectors = detectors or {}
    now = _iso_ts(ARMS_NOW) if now is None else now
    arrivals = {rid: [classify(p, seen, now) for p, seen in arms]
                for rid, arms in ARMS.items()}
    out = {}
    for rid in ARMS:
        r = BY_ID[rid]
        fn = detectors.get(rid, r.detector)
        hits = sum(1 for a in arrivals[rid] if _safe(fn, a))
        controls = sum(1 for other, arr in arrivals.items() if other != rid
                       for a in arr if _safe(fn, a) and _exclusive(rid, other))
        out[rid] = (len(arrivals[rid]), hits, controls)
    return out


def _exclusive(rid, other):
    """Two arrival KIND routes exclude each other; first-context overlaps
    every working kind by design, so it has no must-miss arms."""
    return rid != "arrival.first-context" and other != "arrival.first-context"


def _safe(fn, a):
    try:
        return bool(fn(a))
    except Exception:                          # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# the moment ledger
# ---------------------------------------------------------------------------

LEDGER_MAX = 5 * 1024 * 1024
DELIVERED = "delivered"      # the route's content reached the seat
IN_CONTEXT = "in-context"    # already delivered this context (cooled)
APPLIED = "applied"          # a budget-only route: its policy governed
DEFERRED = "deferred"        # the contract waited for a reader
SILENT = "silent"            # a zero-byte route: nothing is the right answer
OVER_CAP = "over-cap"        # applied, but the turn ran past its cap
NO_CONTENT = "no-content"    # MISSED: a content route found nothing to send
TIMED_OUT = "timed-out"      # MISSED: the hook's deadline took the turn
OUTCOMES = (DELIVERED, IN_CONTEXT, APPLIED, DEFERRED, SILENT, OVER_CAP,
            NO_CONTENT, TIMED_OUT)
MISSED = frozenset((NO_CONTENT, TIMED_OUT))


def ledger_path():
    return os.path.join(home.global_dir(), ".state", "moment-ledger.jsonl")


def record(rows):
    """Append moment rows (one per detected route). Fail-open, never raises;
    rotates at LEDGER_MAX with one .1 generation, the inject ledger's shape."""
    if not rows:
        return
    path = ledger_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LEDGER_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
    except Exception:                          # noqa: BLE001
        pass


def moment_rows(row, detected, outcomes, ids):
    """The ledger rows for one inject turn: its ids and kinds, never text."""
    base = {"v": 1, "ts": row.get("ts"), "hook": "UserPromptSubmit",
            "arrival": row.get("arrival"),
            "timed_out": bool(row.get("timed_out")),
            "fast_path": bool(row.get("fast_path"))}
    for k in ("session", "turn", "project"):
        if row.get(k) is not None:
            base[k] = row[k]
    return [dict(base, route=rid, outcome=outcomes.get(rid, SILENT),
                 ids=list(ids.get(rid, ()))) for rid in detected]


# ---------------------------------------------------------------------------
# the hook deadline and wall clock
# ---------------------------------------------------------------------------

#: The inject hook's own deadline, under the wrapper's `timeout` budget
#: (hooks.TIMEOUT_S = 10). At 8 s the turn gives up its context, writes a
#: `timed_out` row, and exits 0 before the wrapper's TERM, so the loss is
#: COUNTED instead of printed once and forgotten.
DEADLINE_S = 8.0


class Deadline(BaseException):
    """The soft deadline. A BaseException on purpose: gather's lanes are
    fail-open with `except Exception`, and a deadline one of them swallowed
    would let the turn run on into the wrapper's kill."""


def _on_alarm(_signum, _frame):
    raise Deadline()


def arm_deadline(seconds=None):
    """Arm the soft deadline measured from PROCESS START; True when armed.

    A hook process is born for its turn, so its age IS the turn's wall time
    under the wrapper's `timeout`. A process already older than the deadline
    is not a hook (a test runner, an embedding) and is left alone, as is one
    whose age cannot be read, and a caller that already holds an interval
    timer keeps it (never stolen)."""
    budget = DEADLINE_S if seconds is None else seconds
    try:
        wall = process_wall_ms()
        if wall is None or wall / 1000.0 >= budget:
            return False
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            return False
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, budget - wall / 1000.0)
        return True
    except (ValueError, OSError, AttributeError):
        return False


def disarm_deadline():
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (ValueError, OSError, AttributeError):
        pass


def process_wall_ms():
    """Milliseconds since this process was exec'd (Linux /proc), or None:
    the hook's real wall time, where gather's elapsed_ms counts only its own
    construction leg."""
    try:
        with open("/proc/self/stat", "rb") as f:
            fields = f.read().rsplit(b")", 1)[1].split()
        start = int(fields[19]) / float(os.sysconf("SC_CLK_TCK"))
        up = time.clock_gettime(time.CLOCK_BOOTTIME)
        return round(max(up - start, 0.0) * 1000.0, 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# the payload probe (design 7.1)
# ---------------------------------------------------------------------------

def payload_shape(payload):
    """The SHAPE of one hook payload: its sorted keys and the fields the
    trigger design has to prove live, as kinds, lengths and counts. Never
    content text. MEASURED on 2.1.281 (scripts/probe-hook-payloads.sh):
    UserPromptSubmit carries prompt_id and no `source` in print mode;
    PostToolBatch carries tool_calls with a string tool_response;
    PostToolUseFailure's `error` holds the command output after "Exit code N"
    (git's CONFLICT lines and paths included); a no-match rg is a PostToolUse
    success with tool_response.returnCodeInterpretation."""
    if not isinstance(payload, dict):
        return {}
    out = {"event": str(payload.get("hook_event_name") or ""),
           "keys": sorted(str(k) for k in payload)}
    if "source" in payload:
        out["source"] = str(payload.get("source"))[:32]
    if "agent_id" in payload:
        out["agent"] = True
    if "agent_type" in payload:
        out["agent_type"] = str(payload.get("agent_type"))[:64]
    if "last_assistant_message" in payload:
        out["last_len"] = len(str(payload.get("last_assistant_message") or ""))
    bt = payload.get("background_tasks")
    if isinstance(bt, list):
        out["background"] = sorted({str((t or {}).get("type"))
                                    for t in bt if isinstance(t, dict)})
        out["background_n"] = len(bt)
    calls = payload.get("tool_calls")
    if isinstance(calls, list):
        out["calls"] = [str((c or {}).get("tool_name"))
                        for c in calls if isinstance(c, dict)][:16]
    err = payload.get("error")
    if isinstance(err, str):
        out["error_len"] = len(err)
        out["conflict_paths"] = len(re.findall(
            r"CONFLICT \([^)]*\): Merge conflict in \S+", err))
    tr = payload.get("tool_response")
    if isinstance(tr, dict) and tr.get("returnCodeInterpretation"):
        out["rc_meaning"] = str(tr["returnCodeInterpretation"])[:40]
    return out


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

#: THE LATENCY BARS (CRITIC BLOCK 1). INFERRED, and set so today's numbers can
#: fail them: the owner waits on a typed turn, so its p95 wall must stay well
#: inside the wrapper's 10 s budget, and a typed turn that lost its context to
#: the deadline is the loss the wrapper's "TIMED OUT at 10s" line announces.
P95_WALL_BAR_MS = 3000
TYPED_TIMEOUT_BAR = 0.01
#: detected-but-not-delivered, for advice routes (design 7.2).
MISSED_BAR = 0.05
#: A window this many rows deep (written by this lane, so carrying the new
#: fields) that detected a live route with arms ZERO times is RED: every
#: arrival kind occurs in a week of fleet turns, so silence is a dead
#: detector or a hook that never ran, not a quiet week. INFERRED.
MIN_MEASURED = 200


def _read(path):
    rows = []
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        rows.append(d)
        except OSError:
            continue
    return rows


def _pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


def report(days=7, now=None, inject_rows=None, moment_rows_=None,
           detectors=None):
    """The per-route verdicts and the hook's latency/timeout bars over the
    last `days` -> dict. Rows may be handed in (tests); otherwise the inject
    ledger and the moment ledger are read."""
    now = time.time() if now is None else now
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days * 86400))
    if inject_rows is None:
        from .inject import _ledger
        inject_rows = _read(_ledger._ledger_path())
    if moment_rows_ is None:
        moment_rows_ = _read(ledger_path())
    inj = [r for r in inject_rows if str(r.get("ts") or "") >= since]
    mom = [r for r in moment_rows_ if str(r.get("ts") or "") >= since]
    arms = replay_arms(detectors)
    live = collections.Counter(r.get("route") for r in mom)
    delivered = collections.Counter(r.get("route") for r in mom
                                    if r.get("outcome") in (
                                        DELIVERED, IN_CONTEXT, APPLIED,
                                        DEFERRED, SILENT, OVER_CAP))
    missed = collections.defaultdict(collections.Counter)
    for r in mom:
        if r.get("outcome") in MISSED:
            missed[r.get("route")][r.get("outcome")] += 1
    sig_rows = [r for r in inj if isinstance(r.get("sig"), list)]
    measured = sum(1 for r in inj if "timed_out" in r)
    routes = []
    red = 0
    for r in ROUTES:
        a_exp, a_hit, a_ctl = arms.get(r.id, (0, 0, 0))
        s_exp = sum(1 for x in sig_rows if r.id in x["sig"])
        s_hit = sum(1 for x in sig_rows if r.id in x["sig"]
                    and r.id in (x.get("moments") or ()))
        expected, detected = a_exp + s_exp, a_hit + s_hit
        why = []
        if r.status != LIVE:
            verdict = "UNBUILT"
        else:
            if detected < expected:
                why.append("detected %d < expected %d" % (detected, expected))
            if a_ctl:
                why.append("%d must-miss control fire(s)" % a_ctl)
            if s_exp and not live[r.id]:
                why.append("0 live detections in a window with %d moment(s)"
                           % s_exp)
            elif a_exp and not live[r.id] and measured >= MIN_MEASURED:
                why.append("0 live detections in %d measured turns" % measured)
            n_missed = sum(missed[r.id].values())
            if live[r.id] and n_missed > MISSED_BAR * live[r.id]:
                why.append("%d of %d detected moments not delivered"
                           % (n_missed, live[r.id]))
            verdict = "RED" if why else ("GREEN" if expected else "NO-ARMS")
        red += verdict == "RED"
        routes.append({"id": r.id, "event": r.event, "status": r.status,
                       "form": r.form, "arms": a_exp, "arms_hit": a_hit,
                       "controls": a_ctl, "sig": s_exp, "sig_hit": s_hit,
                       "expected": expected, "detected": detected,
                       "live": live[r.id], "delivered": delivered[r.id],
                       "missed": dict(missed[r.id]), "verdict": verdict,
                       "why": why})
    hook = hook_health(inj)
    return {"days": days, "since": since, "routes": routes,
            "red": red + len(hook["bars"]), "hook": hook}


def hook_health(rows):
    """Latency, timeouts and bytes over inject ledger rows (the 7.2 bars plus
    CRITIC BLOCK 1's latency and typed-timeout bars). A row without the new
    fields (written before this lane) is counted as UNKNOWN, never as a
    clean one."""
    walls = [r["wall_ms"] for r in rows if isinstance(r.get("wall_ms"), (int, float))]
    typed = [r for r in rows if r.get("arrival") == TYPED]
    typed_walls = [r["wall_ms"] for r in typed
                   if isinstance(r.get("wall_ms"), (int, float))]
    known = [r for r in rows if "timed_out" in r]
    t_out = [r for r in known if r.get("timed_out")]
    typed_out = [r for r in typed if r.get("timed_out")]
    size = [int(((r.get("sample") or {}).get("rendered_bytes")) or 0) for r in rows]
    jit = [int((((r.get("sample") or {}).get("lane_bytes")) or {}).get("jit") or 0)
           for r in rows]
    jit_lines = [len(((r.get("fired") or {}).get("jit")) or ()) for r in rows]
    by_arrival = collections.Counter(r.get("arrival") or "unknown" for r in rows)
    bytes_by = collections.defaultdict(list)
    for r, b in zip(rows, size):
        bytes_by[r.get("arrival") or "unknown"].append(b)
    p95 = _pct(walls, .95)
    typed_p95 = _pct(typed_walls, .95)
    rate = len(typed_out) / float(len(typed)) if typed else None
    bars = []
    if p95 is not None and p95 > P95_WALL_BAR_MS:
        bars.append("p95 wall %.0f ms > %d" % (p95, P95_WALL_BAR_MS))
    if rate is not None and rate > TYPED_TIMEOUT_BAR:
        bars.append("typed-turn timeout rate %.1f%% > %.0f%%"
                    % (100 * rate, 100 * TYPED_TIMEOUT_BAR))
    return {
        "turns": len(rows), "measured": len(known),
        "unknown": len(rows) - len(known),
        "timed_out": len(t_out), "typed_turns": len(typed),
        "typed_timed_out": len(typed_out), "typed_timeout_rate": rate,
        "fast_path": sum(1 for r in rows if r.get("fast_path")),
        "wall_p50_ms": _pct(walls, .5), "wall_p95_ms": p95,
        "typed_wall_p95_ms": typed_p95,
        "mean_bytes": round(sum(size) / float(len(size)), 1) if size else None,
        "median_bytes": _pct(size, .5),
        "mean_jit_bytes": round(sum(jit) / float(len(jit)), 1) if jit else None,
        "jit_lines_per_turn": round(sum(jit_lines) / float(len(jit_lines)), 2)
        if jit_lines else None,
        "silent_share": round(sum(1 for b in size if b == 0) / float(len(size)), 3)
        if size else None,
        "arrivals": dict(by_arrival),
        "mean_bytes_by_arrival": {k: round(sum(v) / float(len(v)), 1)
                                  for k, v in bytes_by.items()},
        "bars": bars,
    }


def render_report(rep):
    """The report as text lines (the CLI's rendering)."""
    out = ["moments over %dd (since %s): %d RED" % (rep["days"], rep["since"],
                                                    rep["red"])]
    for r in rep["routes"]:
        if r["status"] != LIVE and not r["expected"]:
            continue
        out.append("  %-7s %-26s arms %d/%d  sig %d/%d  live %d  delivered %d%s"
                   % (r["verdict"], r["id"], r["arms_hit"], r["arms"],
                      r["sig_hit"], r["sig"], r["live"], r["delivered"],
                      ("  — " + "; ".join(r["why"])) if r["why"] else ""))
    unbuilt = [r["id"] for r in rep["routes"] if r["verdict"] == "UNBUILT"]
    if unbuilt:
        out.append("  UNBUILT (%d, detectors owed by later lanes): %s"
                   % (len(unbuilt), ", ".join(unbuilt)))
    h = rep["hook"]
    out.append("hook: %d turns (%d without the new fields), %d fast-path, "
               "%d timed out; typed %d, typed timed out %d%s"
               % (h["turns"], h["unknown"], h["fast_path"], h["timed_out"],
                  h["typed_turns"], h["typed_timed_out"],
                  "" if h["typed_timeout_rate"] is None
                  else " (%.1f%%)" % (100 * h["typed_timeout_rate"])))
    out.append("wall ms: p50 %s p95 %s typed p95 %s; bytes/turn mean %s median "
               "%s; JIT mean %s, %s lines/turn; silent %s"
               % (h["wall_p50_ms"], h["wall_p95_ms"], h["typed_wall_p95_ms"],
                  h["mean_bytes"], h["median_bytes"], h["mean_jit_bytes"],
                  h["jit_lines_per_turn"], h["silent_share"]))
    for b in h["bars"]:
        out.append("  RED %s" % b)
    return out
