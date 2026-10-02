#!/usr/bin/env python3
"""THE CONTEXT LOOP OF ONE SEAT: how big every request it made was, where
Claude Code compacted it, and what every request re-sends regardless.

THE MODEL KEEPS NOTHING BETWEEN REQUESTS. Every tool call is a fresh request
that re-sends the whole conversation (system prompt, tools, CLAUDE.md,
memory, every message so far), so a session's cost is requests x context
size, and the only reset is a compaction. This module reads that loop off the
seat's own transcript, which is the one place it is measured: each
main-thread assistant record carries the request's `usage`, and each
compaction leaves a `compact_boundary` with its own pre/post sizes.

ONE PASS, STREAMED, NEVER A WHOLE READ. A lead's transcript runs to hundreds
of MB (one lead's was 494 MB with 18.8k requests), so the file is read one
line at a time and folded into a compact series: per request
[t, ctx, cache_read, cache_write, input, output] and per compaction
[t, pre, post, trigger, index of the next request]. A line is parsed only when its bytes can hold what
the fold needs (`usage` with an assistant role, a compact boundary, or an
attachment just after a boundary), which keeps the pass near one second for
that file. The result is CACHED BY (inode, size, mtime) AND RESUMED FROM THE
LAST COMPLETE LINE: a transcript only ever grows, so the next read costs the
bytes appended since, and a file that shrank or was replaced starts over.

DEDUPED BY MESSAGE ID, LAST RECORD WINS, FIRST POSITION KEPT. Claude Code
writes one assistant record per content block, all carrying the same
`message.id` and usage. That rule reproduces the standalone context-loop
page's series (the review artifact the owner already used) row for row;
tests/test_contextloop.py holds the shape.

THE FLOOR IS THE FIRST REQUEST AFTER THE LAST COMPACTION (or the session's
first request when it never compacted): input + cache_read + cache_creation.
What that request re-sent is partly visible in the records Claude Code writes
just before it -- the CLAUDE.md layers and the auto-memory index it loaded
(an `instructions` attachment carries their text), the skills it restored
(`invoked_skills`), the compaction summary. Those are sized here in
characters and converted at CHARS_PER_TOKEN, an ESTIMATE, and said so; the
remainder (system prompt, tool schemas and the kept conversation tail) is the
measured total less the estimated parts. Where an exact count exists for the
same text (a `parts_index.json` from a floor measurement, whose counter is
`claude -p` input-token deltas) it is cited beside the estimate.
"""
import bisect
import glob
import json
import os
import threading
import time

#: Characters per token for the floor ESTIMATE. MEASURED over one lead's
#: floor parts with an exact counter: 2.7 on average (2.35 to
#: 2.83 for English prose; 1.58 for the CJK-dense /dev CLAUDE.md). The common
#: 3.6 undercounts by about a quarter.
CHARS_PER_TOKEN = 2.7

#: Where Claude Code fires its own auto-compaction, as a share of
#: CLAUDE_CODE_AUTO_COMPACT_WINDOW. MEASURED, not settable: 1M fired at 765k
#: (76%), 160k at 122k (76%), 240k at 172k (72%). It keeps the rest free for
#: the summary request and its output.
FIRE_PCT = 74
FIRE_PCT_RANGE = (72, 76)

#: The priced weights the page offers beside raw tokens (Opus list ratios).
PRICE = {"cache_read": 0.1, "cache_write": 1.25, "output": 5}

#: What one compaction costs beyond its summary, MEASURED over 43 compactions
#: and 16,687 tool calls of one lead: about five extra re-grounding reads, and
#: a slip rate elevated for about fifty calls.
REGROUND_READS = 5
SLIP_CALLS = 50

#: The size of a compaction's summary when a seat's own transcript holds none
#: to size. MEASURED by the floor census with an exact counter on one lead's
#: summary: 6,702 tokens. (An earlier 38k was the floor less the fixed part,
#: which also holds the restored files and the kept conversation tail.)
SUMMARY_TOKENS = 6700

#: How many UserPromptSubmit hook records are kept per transcript for the
#: inject pack's "the exact text this turn received" toggle. The pack shows
#: the last dozen turns of one window; this keeps a few windows of slack.
TEXT_KEEP = 64

#: How many transcripts the cache holds at once (one per seat looked at).
CACHE_MAX = 6

#: The per-request row's fields, in order.
FIELDS = ("t", "ctx", "cache_read", "cache_write", "input", "output")

_LOCK = threading.Lock()
_CACHE = {}

_ATTACH_LOOK = 600       # a record's type keys sit in its first bytes
_BOUNDARY = b'"compact_boundary"'
_USAGE = b'"usage"'
_ASSISTANT = b'"role":"assistant"'
_ATTACH = b'"attachment"'
_UPS = b'UserPromptSubmit'
_SUMMARY = b'"isCompactSummary":true'


def _epoch(ts):
    """ISO-8601 (trailing Z) -> float epoch, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    from datetime import datetime, timezone
    try:
        at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _cjk(text):
    return sum(1 for ch in text if ord(ch) > 0x2e80)


def _tilde(path):
    home = os.path.expanduser("~")
    path = str(path or "")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def _new_state(key):
    return {"key": key, "offset": 0, "ids": {}, "reqs": [], "comp": [],
            "after": _new_after(None), "ups": [], "cwd": None,
            "summaries": [], "lines": 0, "parsed": 0}


def _new_after(boundary):
    """The record of one context window's opening: the boundary that began it
    (None for the session start), the parts loaded before its first request,
    and that request once it arrives."""
    return {"boundary": boundary, "first": None, "parts": []}


def _part(state, kind, name, text):
    after = state["after"]
    if after["first"] is not None or not isinstance(text, str):
        return
    for p in after["parts"]:
        if p["kind"] == kind and p["name"] == name:
            p["chars"] += len(text)
            p["cjk"] += _cjk(text)
            return
    after["parts"].append({"kind": kind, "name": name, "chars": len(text),
                           "cjk": _cjk(text)})


def _attachment(state, rec):
    a = rec.get("attachment")
    if not isinstance(a, dict):
        return
    ty = a.get("type")
    if a.get("hookEvent") == "UserPromptSubmit" and ty in (
            "hook_success", "hook_additional_context"):
        content = a.get("content")
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        if not isinstance(content, str) or not content:
            content = a.get("stdout") if isinstance(a.get("stdout"), str) else ""
        if content:
            state["ups"].append({"ts": rec.get("timestamp"),
                                 "at": _epoch(rec.get("timestamp")),
                                 "text": content,
                                 "command": str(a.get("command") or "")})
            del state["ups"][:-TEXT_KEEP]
        return
    if state["after"]["first"] is not None:
        return
    if ty == "instructions":
        for f in a.get("files") or ():
            if isinstance(f, dict):
                path = _tilde(f.get("path"))
                kind = "memory" if path.endswith("/memory/MEMORY.md") \
                    else "claude_md"
                _part(state, kind, path, f.get("content"))
    elif ty == "invoked_skills":
        for s in a.get("skills") or ():
            if isinstance(s, dict):
                _part(state, "skill", str(s.get("name") or "?"), s.get("content"))
    elif ty == "deferred_tools_delta":
        _part(state, "listing", "deferred tool names",
              "\n".join(str(x) for x in a.get("addedLines") or ()))
    elif ty == "agent_listing_delta":
        _part(state, "listing", "agent listing",
              "\n".join(str(x) for x in a.get("addedLines") or ()))
    elif ty == "mcp_instructions_delta":
        _part(state, "listing", "MCP server instructions",
              "\n\n".join(str(x) for x in a.get("addedBlocks") or ()))
    elif ty == "skill_listing":
        content = a.get("content")
        _part(state, "listing", "skill listing",
              content if isinstance(content, str) else json.dumps(a))
    elif ty == "hook_additional_context" and a.get("hookEvent") == "SessionStart":
        content = a.get("content")
        _part(state, "hook", "SessionStart hook text",
              "\n".join(content) if isinstance(content, list) else str(content or ""))
    elif ty in _RESTORED:
        _part(state, "restored", "restored files, tasks and environment",
              json.dumps(a))


#: The attachments a compaction restores beside the summary: open files, the
#: task list, the environment block. Folded into one part (the floor
#: measurement counted them together, as `misc_restored`).
_RESTORED = ("file", "task_status", "environment", "model", "session_context",
             "date", "compact_file_reference", "remote_session_change")


def _consume(state, line):
    """Fold one complete transcript line into `state`."""
    head = line[:_ATTACH_LOOK]
    if _BOUNDARY in head:
        rec = json.loads(line)
        if rec.get("subtype") == "compact_boundary":
            meta = rec.get("compactMetadata") or {}
            at = _epoch(rec.get("timestamp"))
            state["comp"].append([at, _int(meta.get("preTokens")),
                                  _int(meta.get("postTokens")),
                                  str(meta.get("trigger") or "auto"),
                                  len(state["reqs"])])
            state["after"] = _new_after(len(state["comp"]) - 1)
            state["parsed"] += 1
        return
    if _ATTACH in head and (_UPS in head or state["after"]["first"] is None):
        rec = json.loads(line)
        state["parsed"] += 1
        if rec.get("type") == "attachment" and not rec.get("isSidechain"):
            _attachment(state, rec)
        return
    if state["after"]["first"] is None and _SUMMARY in line:
        rec = json.loads(line)
        state["parsed"] += 1
        msg = rec.get("message") or {}
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        _part(state, "summary", "compaction summary", text)
        if state["after"]["boundary"] is not None:
            state["summaries"].append(len(text))   # characters, one per compaction
        return
    if _USAGE not in line or _ASSISTANT not in line:
        return
    rec = json.loads(line)
    state["parsed"] += 1
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return
    msg = rec.get("message") or {}
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return
    if isinstance(rec.get("cwd"), str):
        state["cwd"] = rec["cwd"]
    cr, cw = _int(usage.get("cache_read_input_tokens")), \
        _int(usage.get("cache_creation_input_tokens"))
    inp, out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    row = [_epoch(rec.get("timestamp")), inp + cr + cw, cr, cw, inp, out]
    mid = msg.get("id")
    at = state["ids"].get(mid) if mid else None
    if at is None:
        if mid:
            state["ids"][mid] = len(state["reqs"])
        state["reqs"].append(row)
    else:
        state["reqs"][at] = row
    after = state["after"]
    if after["first"] is None and row[1] > 0:
        after["first"] = {"at": row[0], "ctx": row[1], "cache_read": cr,
                          "cache_write": cw, "input": inp}


def _key(path):
    st = os.stat(path)
    return (st.st_ino, st.st_dev), st.st_size, st.st_mtime


def read(path, limit=None):
    """-> the folded state for `path`, resumed from the cache when the file
    only grew. `limit` stops at that byte offset (tests and the verification
    against a frozen copy use it); a limited read is never cached."""
    ident, size, mtime = _key(path)
    with _LOCK:
        state = _CACHE.get(path) if limit is None else None
        if state is not None and (state["key"][0] != ident
                                  or size < state["offset"]):
            state = None
        if state is not None and state["key"][1:] == (size, mtime):
            return state
        if state is None:
            state = _new_state((ident, size, mtime))
        end = size if limit is None else min(size, limit)
        with open(path, "rb") as f:
            f.seek(state["offset"])
            pos = state["offset"]
            while pos < end:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break           # a record still being written: next time
                pos += len(line)
                state["lines"] += 1
                try:
                    _consume(state, line)
                except ValueError:
                    continue        # a torn or foreign line is not the loop
            state["offset"] = pos
        state["key"] = (ident, size, mtime)
        if limit is None:
            _CACHE[path] = state
            while len(_CACHE) > CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
        return state


def series(state):
    """-> {"t0", "S", "C"} in the standalone page's own shape: seconds since
    the first request, zero-size requests (Claude Code's synthetic error
    records) left out.

    Each compaction in C ends with the index in S of the first request after
    it (len(S) when none has come yet). The times are whole seconds, so a
    compaction in the same second as the request before it ties with it;
    only its place in the transcript says which side of it it is on."""
    keep = [i for i, r in enumerate(state["reqs"])
            if r[1] > 0 and r[0] is not None]
    if not keep:
        return {"t0": None, "S": [], "C": []}
    reqs = [state["reqs"][i] for i in keep]
    t0 = int(reqs[0][0])
    s = [[int(r[0]) - t0] + r[1:] for r in reqs]
    c = [[int(at) - t0, pre, post, trig, bisect.bisect_left(keep, k)]
         for at, pre, post, trig, k in state["comp"] if at is not None]
    return {"t0": t0, "S": s, "C": c}


def _exact_index():
    """-> ({part key: exact tokens}, {part key: chars}, source path) from the
    newest floor measurement on this host, or empty maps. A part is cited
    only when its text is the SAME LENGTH as the one that was counted, so a
    file edited since never borrows a stale exact number."""
    override = os.environ.get("HELM_FLOOR_PARTS")
    from . import home
    cands = [override] if override else glob.glob(os.path.join(
        home.helm_home(), "*", "reviews", "*", "floor", "parts_index.json"))
    best = None
    for p in cands:
        try:
            best = max(best or (0, ""), (os.path.getmtime(p), p))
        except OSError:
            continue
    if not best:
        return {}, {}, None
    try:
        with open(best[1]) as f:
            parts = (json.load(f) or {}).get("parts") or {}
    except (OSError, ValueError):
        return {}, {}, None
    exact = {k: v.get("tokens_exact") for k, v in parts.items()
             if isinstance(v, dict) and isinstance(v.get("tokens_exact"), int)}
    chars = {k: v.get("chars") for k, v in parts.items() if isinstance(v, dict)}
    return exact, chars, best[1]


#: The floor measurement's own key for each part that is not a file or a
#: skill (floor/extract.py names them this way).
_EXACT_KEYS = {"compaction summary": "compact_summary",
               "deferred tool names": "deferred_tool_names",
               "agent listing": "agent_listing",
               "MCP server instructions": "mcp_instructions",
               "SessionStart hook text": "sessionstart_additionalContext",
               "skill listing": "skill_listing(if resent)",
               "restored files, tasks and environment":
                   "misc_restored(file/task/env/model)"}


def _part_key(part):
    if part["kind"] in ("claude_md", "memory"):
        return "claudemd:" + part["name"]
    if part["kind"] == "skill":
        return "skill:" + part["name"]
    return _EXACT_KEYS.get(part["name"])


def floor(state):
    """-> what every request in the current window re-sends.

    `measured` is the first request after the last compaction (input +
    cache_read + cache_creation), MEASURED. Each part is an ESTIMATE at
    CHARS_PER_TOKEN unless an exact count for the same text exists."""
    after = state["after"]
    first = after["first"]
    if first is None:
        return {"state": "none", "parts": [], "measured": None,
                "why": "no request has been made since the last compaction"}
    exact, counted, source = _exact_index()
    parts, est_sum = [], 0
    for p in after["parts"]:
        est = int(round(p["chars"] / CHARS_PER_TOKEN))
        key = _part_key(p)
        ex = exact.get(key) if key and counted.get(key) == p["chars"] else None
        est_sum += est
        parts.append(dict(p, estimate=est, exact=ex))
    boundary = after["boundary"]
    comp = state["comp"][boundary] if boundary is not None else None
    # THE CACHED PREFIX IS MEASURED, NOT ESTIMATED: right after a compaction
    # the only bytes the server still holds are the system prompt and the
    # tool schemas that open every request, so this request's cache_read IS
    # their size. Zero on a session's first request, which writes everything.
    prefix = first["cache_read"]
    return {"state": "observed", "measured": first, "parts": parts,
            "estimated_parts": est_sum, "prefix": prefix,
            "remainder": max(0, first["ctx"] - prefix - est_sum),
            "after": "compaction" if comp else "session start",
            "boundary": {"at": comp[0], "pre": comp[1], "post": comp[2],
                         "trigger": comp[3]} if comp else None,
            "chars_per_token": CHARS_PER_TOKEN,
            "exact_source": source}


def floors(state, cap=None):
    """-> the measured floor after every compaction: the first request's ctx
    after each boundary, found by its place in the transcript (as series()
    places it). Its median is the page's default floor knob."""
    out, ci, reqs, comp = [], 0, state["reqs"], state["comp"]
    for at, _pre, _post, _trig, k in comp:
        if at is None:
            continue
        ci = max(ci, k)
        for r in reqs[ci:]:
            ci += 1
            if r[0] is not None and r[1] > 0:
                out.append(r[1])
                break
    return out[-cap:] if cap else out


def summary_size(state):
    """-> {"tokens", "how", "n", ...}: how big this seat's compaction summary
    is, from the summaries its own transcript holds (the median, so one long
    summary does not set the price of every compaction).

    ESTIMATE at CHARS_PER_TOKEN from the summary's text; when the floor
    census counted one of these same summaries exactly (same length), that
    count is cited beside it. A seat whose transcript holds no summary yet gets the census
    figure, flagged as the fallback it is."""
    chars = state.get("summaries") or []
    if not chars:
        return {"tokens": SUMMARY_TOKENS, "how": "fallback", "n": 0,
                "source": "the floor census's measured summary (no compaction "
                          "summary in this transcript yet)"}
    med = _median(chars)
    out = {"tokens": int(round(med / CHARS_PER_TOKEN)), "how": "estimate",
           "n": len(chars), "chars_median": med,
           "source": "median of this seat's %d compaction summaries at %s "
                     "characters per token" % (len(chars), CHARS_PER_TOKEN)}
    exact, counted, _src = _exact_index()
    key = _EXACT_KEYS["compaction summary"]
    if exact.get(key) and counted.get(key) in chars:
        out["exact_one"] = {"tokens": exact[key], "chars": counted[key]}
    return out


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


# ---------------------------------------------------------------------------
# which transcript is this seat's
# ---------------------------------------------------------------------------

def locate(seat=None, session=None):
    """-> {"seat", "session", "path", "cwd", "project", "why", "unavailable"}.

    THE BINDING IS THE CONFIG VIEW'S (injection_config._roster_row), so the
    panel and the identity above it cannot disagree about which seat this is.
    The transcript is the catalog's row for that session (newest copy when
    `cv port` left two); a proxy seat whose session the catalog has not
    indexed yet falls back to the autocompact gauge's own resolver."""
    out = {"seat": seat, "session": session, "path": None, "cwd": None,
           "project": None, "why": "", "unavailable": []}
    try:
        from . import injection_config
        label, roster, err = injection_config._roster_row(seat, session)
    except Exception:
        label, roster, err = None, None, "roster"
    if err:
        out["unavailable"].append(err)
    roster = roster or {}
    out["seat"] = label or seat
    out["session"] = session = session or roster.get("session")
    out["cwd"] = roster.get("cwd")
    out["project"] = roster.get("project")
    if not session:
        out["why"] = "no session is bound to this seat yet"
        return out
    paths = []
    try:
        from . import transcripts
        for row in transcripts.get_catalog()["rows"]:
            if isinstance(row, dict) and row.get("i") == session and row.get("p"):
                if row.get("h") not in (None, "claude"):
                    out["why"] = ("this seat runs %s, whose transcript does not "
                                  "record per-request context" % row.get("h"))
                    return out
                out["cwd"] = out["cwd"] or row.get("cwd")
                paths.append(os.path.expanduser(row["p"]))
    except Exception:
        out["unavailable"].append("catalog")
    if not paths:
        paths = _seat_transcript(out["seat"], session)
    live = []
    for p in paths:
        try:
            live.append((os.path.getmtime(p), p))
        except OSError:
            continue
    if not live:
        out["why"] = "no transcript found for session %s" % session
        return out
    out["path"] = max(live)[1]
    return out


def _seat_transcript(seat, session):
    """A proxy seat's transcript, by the autocompact gauge's own resolver."""
    if not seat:
        return []
    try:
        from . import seat as seat_mod
        family, err = seat_mod._seat_family(seat)
        if err:
            return []
        d = seat_mod._instance_dir(family, seat)
        return glob.glob(os.path.join(glob.escape(d), "claude", "projects",
                                      "*", session + ".jsonl"))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# window and budgets
# ---------------------------------------------------------------------------

_WINDOW_KEYS = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")


def _ledger_runtime(session):
    """-> (pid, proc_start) of the agent process the newest fire-ledger row
    of `session` bound itself to, or (None, None)."""
    try:
        from . import inject
        rows = inject._ledger_rows(strict=False)
    except Exception:
        return None, None
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("session") == session:
            rt = row.get("runtime") or {}
            if rt.get("pid"):
                return rt.get("pid"), rt.get("proc_start")
    return None, None


def live_window(session, proc_dir=None):
    """-> {"value", "source", "max_context"} — the window the seat's LIVE
    process was launched with, read from its environment.

    ONLY THE TWO WINDOW KEYS ARE READ OUT and nothing else of that environment
    leaves this function. The pid is the one the seat's own injection hook
    recorded, and it is trusted only while /proc still shows the same process
    start, so a recycled pid never answers for a seat."""
    pid, start = _ledger_runtime(session) if session else (None, None)
    if not pid:
        return {"value": None, "source": "no live process recorded"}
    from . import beacons
    now = beacons.proc_starttime(pid, proc_dir)
    if now is None or (start and str(now) != str(start)):
        return {"value": None, "source": "its recorded process has exited"}
    env = beacons.proc_env(pid, proc_dir)
    if env is None:
        return {"value": None, "source": "its process environment is unreadable"}
    win = env.get(_WINDOW_KEYS[0])
    cap = env.get(_WINDOW_KEYS[1])
    num = lambda v: int(v) if v and str(v).isdigit() else None  # noqa: E731
    if num(win):
        return {"value": num(win), "max_context": num(cap),
                "source": "the live process (%s)" % _WINDOW_KEYS[0]}
    return {"value": None, "max_context": num(cap),
            "source": "unset on the live process, so Claude Code's own default "
                      "applies"}


def budgets():
    """-> [{"name", "limit", "lane", "what"}] — the injector's configured
    per-turn limits, read off helm.inject's own constants."""
    from .inject import _common as c
    return [
        {"name": "PINNED_BUDGET", "lane": "pinned", "limit": c.PINNED_BUDGET,
         "what": "always-on rules, per turn"},
        {"name": "JIT_BUDGET", "lane": "jit", "limit": c.JIT_BUDGET,
         "what": "entries matched to the prompt, per turn"},
        {"name": "GATE_BUDGET", "lane": "jit", "limit": c.GATE_BUDGET,
         "what": "a matched rule's preconditions, per turn"},
        {"name": "JIT_LANE_MAX", "lane": "jit", "limit": c.JIT_LANE_MAX,
         "what": "the whole matched lane's ceiling"},
        {"name": "JIT_CAP", "lane": "jit", "limit": c.JIT_CAP, "unit": "entries",
         "what": "matched entries per turn"},
        {"name": "LINE_CAP", "lane": None, "limit": c.LINE_CAP,
         "what": "one entry's line"},
        {"name": "STEER_CAP", "lane": "reflex", "limit": c.STEER_CAP,
         "what": "one reflex steer"},
        {"name": "WHO_CAP", "lane": "pinned", "limit": c.WHO_CAP,
         "what": "the operator digest"},
        {"name": "WHISPER_CAP", "lane": "whisper", "limit": c.WHISPER_CAP,
         "what": "the one-line brief"},
    ]


def window_view(seat, session, gauge_ctx):
    """-> the seat's window, threshold, gauge and launch profile, read-only.

    The window is the LIVE process's when it can be read; a proxy seat also
    gets the catalog reading `autocompact._window` divides by, so a
    disagreement between the two is on screen rather than averaged away."""
    from . import autocompact
    out = {"live": live_window(session), "catalog": None, "role": None,
           "profile": None, "profile_switches": {},
           "threshold_pct": autocompact.threshold_pct(),
           "threshold_default": autocompact.DEFAULT_THRESHOLD,
           "fire_pct": FIRE_PCT, "fire_pct_range": list(FIRE_PCT_RANGE),
           "ctx": gauge_ctx, "lead_window": None,
           "override": {"settable": False, "seam": "task/3593",
                        "why": "no per-seat window override exists yet: a "
                               "seat's window is set when it launches"}}
    try:
        from . import seat as seat_mod, seat_catalog
        family, err = seat_mod._seat_family(seat) if seat else (None, "none")
        if not err and family in seat_mod.FAMILIES:
            win, src = autocompact._window(family)
            out["catalog"] = {"value": win, "source": src, "family": family}
            out["profile"] = (seat_mod.FAMILIES[family] or {}).get("profile")
            out["profile_switches"] = {
                k: list(v) if isinstance(v, tuple) else v
                for k, v in seat_catalog.launch_profile(family).items()}
        if not err:
            rec = seat_mod._spawn_record(
                seat_mod._instance_dir(family, seat)) or {}
            if rec.get("seat") == seat and rec.get("role"):
                out["role"] = str(rec["role"])
        # THE LEAD POSTURE'S NUMBERS ARRIVE WITH task/4049 AND task/4056. Read
        # by name so this view shows them the day they land and needs no
        # change of its own; until then the keys simply are not there.
        out["lead_window"] = getattr(seat_catalog, "LEAD_CONTEXT_WINDOW", None)
        lean = getattr(seat_catalog, "lead_lean_settings", None)
        if out["role"] == "lead" and callable(lean):
            out["profile"] = out["profile"] or "lead-lean"
            out["profile_switches"].update({k: v for k, v in lean()})
    except Exception:
        pass
    win = out["live"].get("value") or (out["catalog"] or {}).get("value")
    out["window"] = win
    out["gauge_pct"] = round(100.0 * gauge_ctx / win, 1) \
        if win and gauge_ctx else None
    return out


# ---------------------------------------------------------------------------
# the model the panel draws
# ---------------------------------------------------------------------------

def view(seat=None, session=None, path=None):
    """-> the Loop editor's model for one seat. Never raises: a missing or
    empty transcript is a `state: "none"` model with its reason, because a
    panel that 500s teaches the owner nothing about his loop."""
    seat = str(seat or "").strip() or None
    session = str(session or "").strip() or None
    where = {"seat": seat, "session": session, "path": path, "cwd": None,
             "project": None, "why": "", "unavailable": []} if path \
        else locate(seat, session)
    out = {"state": "none", "seat": where["seat"], "session": where["session"],
           "transcript": where["path"], "cwd": where["cwd"],
           "project": where["project"], "unavailable": where["unavailable"],
           "why": where["why"], "facts": {
               "fire_pct": FIRE_PCT, "fire_pct_range": list(FIRE_PCT_RANGE),
               "price": PRICE, "summary_tokens": SUMMARY_TOKENS,
               "reground_reads": REGROUND_READS, "slip_calls": SLIP_CALLS,
               "chars_per_token": CHARS_PER_TOKEN}}
    if not where["path"]:
        out["why"] = out["why"] or "no transcript"
        out["budgets"] = _safe(budgets, [])
        return out
    started = time.time()
    try:
        state = read(where["path"])
    except OSError as exc:
        out["why"] = "no transcript: %s" % exc
        return out
    data = series(state)
    out.update(data)
    out["read_s"] = round(time.time() - started, 3)
    out["bytes"] = state["offset"]
    out["cwd"] = out["cwd"] or state["cwd"]
    if not data["S"]:
        out["why"] = "no transcript: this session has made no request yet"
        out["budgets"] = _safe(budgets, [])
        return out
    fl = floors(state)
    summ = summary_size(state)
    out["facts"] = dict(out["facts"], summary_tokens=summ["tokens"],
                        summary=summ)
    out.update({
        "state": "observed",
        "floor": floor(state),
        "floors": fl[-12:],
        "floor_median": _median(fl),
        "window": window_view(out["seat"], out["session"], data["S"][-1][1]),
        "budgets": _safe(budgets, []),
        "why": "",
    })
    return out


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def turn_text(seat=None, session=None, ts=None, path=None):
    """-> {"text", "at", "command"} — the exact UserPromptSubmit hook text
    the turn the fire ledger stamped `ts` received, read from the seat's own
    transcript (the ledger keeps the ids and bytes, never the text).

    The hook record lands within a few seconds AFTER the ledger row (the
    ledger stamps whole seconds when the gather starts; the harness stamps
    the record when the hook returns), so the match is the first record of
    helm's injector in [ts - 1 s, ts + 30 s]."""
    at = _epoch(ts)
    if at is None:
        return {"text": None, "why": "need the turn's ts"}
    if not path:
        where = locate(seat, session)
        path = where["path"]
        if not path:
            return {"text": None, "why": where["why"] or "no transcript"}
    try:
        state = read(path)
    except OSError as exc:
        return {"text": None, "why": "no transcript: %s" % exc}
    best = None
    for rec in state["ups"]:
        if rec["at"] is None or not (at - 1 <= rec["at"] <= at + 30):
            continue
        helm = "inject" in rec["command"]
        score = (not helm, rec["at"] - at)
        if best is None or score < best[0]:
            best = (score, rec)
    if not best:
        return {"text": None,
                "why": "the transcript holds no hook text for this turn (a "
                       "silent turn sends none; only the last %d are kept)"
                       % TEXT_KEEP}
    rec = best[1]
    return {"text": rec["text"], "at": rec["ts"],
            "bytes": len(rec["text"].encode("utf-8")),
            "command": rec["command"].rsplit("/", 1)[-1][:120]}
