"""helm inject — the use cluster: `helm inject --use-report`, whether a seat
USED what the fire ledger says reached its turn.

The fire ledger records DELIVERY: which ids reached which session's turn, and
when. It does not record use, and `--lane-report` says so ("fires are not
heeds"). Injected context is paid for on every turn it rides, so the pruning
lever is an entry that fires often and is never used — and "never used" has
to be read from what the seat actually did, not guessed. This analyzer reads
it from the seat's own transcript. It is READ-ONLY: no ledger row, no store
write, no state file, and transcripts are opened for reading only.

THE TURN A FIRE BELONGS TO. A ledger row is written while the UserPromptSubmit
hook runs, and its `ts` is that instant cut to the second. The harness writes
one attachment record per hook when the hook completes (`hookEvent`
UserPromptSubmit), every hook of one prompt event sharing that event's
`toolUseID`. The fire's ANCHOR is the first such record at or after the row's
second, and no later than ANCHOR_S past it. Each row is its own prompt event,
so a row in the same second as the one before it never takes the event that
row took (measured: 92 of 2,492 fired rows share a session and a second with
another row, every one checked a different event). The turn is every
main-thread assistant record after the anchor, up to the next prompt event: a
typed user prompt, a queued message delivered into the running turn, or a
hook record of another event. Before the first assistant record, records of
the anchor's own event (its other hooks, the queued messages it delivered, the
prompt record written within CLUSTER_S of its hook — a task notification's
lands 1-3 ms AFTER the hook record) stay in the event. Stop-hook feedback,
tool results and compaction summaries are not prompts, so they never end a
turn. Sidechain records are not the seat's own turn and are skipped.

USE, AND WHY IT HAS TWO SIGNALS. A fire is used when that turn's assistant
text, thinking or tool input
  * NAMES the id: the id as a whole token (neighbours that could extend a slug
    — letters, digits, `_`, `-` — are not allowed on either side), so the
    `type:id` form and `helm store get <type>:<id>` both count; or
  * carries at least KEYWORD_MIN of the entry's RARE store keywords: its own
    non-generic keywords (the id itself excluded) that at most RARE_DF entries
    of the store carry, matched under the store's own keyword law
    (store._probe_hits: word boundaries, the inflection allowance).
Naming is the high-trust signal. The keyword signal is weaker by construction:
a keyword entry fired BECAUSE the prompt carried its words, so a reply that
echoes the prompt can match with no help from the entry. The two are counted
separately and a reader weighs them separately. Either one is a LOWER bound on
use: a seat can follow a rule without naming it.

UNKNOWN IS NEVER UNUSED (unreadable and empty must never share a value). A
fire is UNKNOWN when its turn could not be read whole and no use was seen in
the part that was read: no session on the row, a harness whose transcripts
this reader does not parse, no transcript or more than one, an open or
unreadable file, no anchor, a turn still open at the end of a live transcript,
a turn the seat never answered (NO-REPLY: the next prompt event, or the end
of the file, came before any assistant words — a burst of prompts the seat
answered together, an interrupt), a turn or record past its cap, or a read
budget spent. An UNKNOWN fire is out of every rate's denominator and never
counts toward a prune floor. A use seen before a cap is still a use.

THE TRANSCRIPT LOCATOR is turnresponse.transcript_path — found by session id
in the FILENAME, never by mtime, and two files for one id are an ambiguity,
not a choice. Its root is the row's own validated config home
(injection_schema.config_home) when the row records one, and a row that names
its home is looked up there only. A row that records none (its runtime context
was unavailable) is looked up in the harness default (HELM_CLAUDE_DIR, else
~/.claude) and in helm's own seat homes (<helm home>/_global/seats/<family>
[/instances/<seat>]/claude, the roots session._persisting_sids walks); one
file (by real path) is the transcript, two are unlocatable.

COST. Transcripts reach hundreds of MB, and this runs on the owner's hub, so
nothing is read whole. Records are read one bounded line at a time; a record
longer than RECORD_CAP is drained, never parsed. Each fire's turn is found by
galloping forward from the previous fire's position and then bisecting on
record time, so a fire costs about log2(distance / PROBE_SPAN) short probes
plus its own turn, and a fire whose prompt event begins where the previous
fire's turn ended costs no probe at all. Every byte handed back is charged
against TURN_CAP per turn, TRANSCRIPT_CAP per transcript, and TOTAL_CAP per
report. Transcripts are read in rising order of their fire count, so the
light ones finish first and a report budget that runs out falls on the
heaviest, whose remaining fires are UNKNOWN (read-cap). An even split made up
front would instead cap a heavy transcript while light ones left their share
unspent. The report states the bytes it read.
"""
import glob
import json
import os
import re
import sys
import time

from .. import home, injection_schema, turnresponse
from ._ledger import _ledger_rows

MIN_FIRES = 20          # readable fires an id needs before it can be pruned
MAX_USE_RATE = 0.05     # prune below this use rate (strict)
RARE_DF = 3             # a keyword at most this many entries carry is rare
KEYWORD_MIN = 2         # rare keywords a turn must carry to count as use
EXAMPLES = 3            # unused turns named per prune candidate
TOP = 20                # rows of each table the text render prints
TURN_CAP = 8 << 20      # bytes read inside one turn
TRANSCRIPT_CAP = 64 << 20  # bytes read from one transcript
TOTAL_CAP = 384 << 20   # bytes read by one report
RECORD_CAP = 2 << 20    # a longer record is drained, never parsed
PROBE_SPAN = 32 << 10   # the bisection stops when its bracket is this narrow:
                        # a probe step costs about two records (most are
                        # under 8 KB) and the scan then reads about half the
                        # bracket before the anchor, so halving pays while a
                        # quarter of the bracket exceeds a step
SLACK_S = 5             # seconds before the fire the search starts
ANCHOR_S = 60           # the fire's hook record lands within this of its row
CLUSTER_S = 3           # an id-less hook this close to the anchor is its event
OPEN_S = 600            # a transcript written this recently may hold an open turn
LANES = ("whisper", "pinned", "jit", "reflex")
_ID_EDGE = "A-Za-z0-9_-"


class _Cap(Exception):
    """A read budget ran out."""


class _Budget(object):
    """Bytes read, charged per transcript and per report."""

    def __init__(self, total):
        self.total_left = total
        self.read = 0
        self.left = 0

    def open(self, cap):
        self.left = min(cap, self.total_left)

    def charge(self, n):
        self.read += n
        self.left -= n
        self.total_left -= n

    def check(self):
        if self.left <= 0:
            raise _Cap()


class _Reader(object):
    """One transcript read forward in bounded records."""

    def __init__(self, fh, size, budget):
        self.fh, self.size, self.budget = fh, size, budget
        self.pos = 0
        self.head = b""

    def seek(self, off):
        self.fh.seek(off)
        self.pos = off

    def _line(self, limit):
        self.budget.check()
        chunk = self.fh.readline(limit)
        self.budget.charge(len(chunk))
        self.pos += len(chunk)
        return chunk

    def _drain(self):
        while True:
            more = self._line(1 << 20)
            if not more or more.endswith(b"\n"):
                return

    def align(self):
        """Skip the rest of the record the position fell inside."""
        self._drain()

    def record(self):
        """-> (start, raw). raw is one whole record line, None for a record
        longer than RECORD_CAP (drained; its head kept in .head), or b"" at
        the end of the file or at a last line still being written."""
        start = self.pos
        chunk = self._line(RECORD_CAP + 1)
        if chunk.endswith(b"\n"):
            return start, chunk
        if len(chunk) <= RECORD_CAP:
            return start, b""
        self.head = chunk[:4096]
        self._drain()
        return start, None


def _load(raw):
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _kind(rec):
    """assistant | prompt | queued | hook, or None for anything that is
    neither a prompt event nor the seat's own output."""
    if rec is None or rec.get("isSidechain"):
        return None
    t = rec.get("type")
    if t == "assistant":
        return "assistant"
    if t == "attachment":
        a = rec.get("attachment")
        if not isinstance(a, dict):
            return None
        if a.get("hookEvent") == "UserPromptSubmit":
            return "hook"
        return "queued" if a.get("type") == "queued_command" else None
    if t != "user" or rec.get("isMeta") or rec.get("isCompactSummary") \
            or "toolUseResult" in rec:
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return "prompt" if content.strip() else None
    blocks = [b for b in content if isinstance(b, dict)] \
        if isinstance(content, list) else []
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    return "prompt" if any(b.get("type") in ("text", "image")
                           for b in blocks) else None


def _clock(rec):
    """A main-thread record's own time, when it follows file order. A queued
    message's stamp is when it was queued, not when it was written."""
    if rec is None or rec.get("isSidechain"):
        return None
    a = rec.get("attachment")
    if isinstance(a, dict) and a.get("type") == "queued_command":
        return None
    return turnresponse._epoch(rec.get("timestamp"))


def _said(rec):
    """The strings an assistant record carries: text, thinking, tool input."""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [content]
    out = []
    for b in content if isinstance(content, list) else ():
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append(str(b.get("text") or ""))
        elif t == "thinking":
            out.append(str(b.get("thinking") or ""))
        elif t == "tool_use":
            out.append(json.dumps(b.get("input"), ensure_ascii=False))
    return out


def _event(rec):
    a = rec.get("attachment") or {}
    return a.get("toolUseID") or None


def _probe(reader, off, hi, aligned=False):
    """(time, start) of the first main-thread user or assistant record that
    starts in [off, hi), or (None, None). `aligned` says `off` is a record
    start; otherwise the record it fell inside is skipped."""
    reader.seek(off)
    if off and not aligned:
        reader.align()
    while reader.pos < hi:
        start, raw = reader.record()
        if raw == b"":
            break
        rec = _load(raw) if raw else None
        if rec and rec.get("type") in ("user", "assistant"):
            ts = _clock(rec)
            if ts is not None:
                return ts, start
    return None, None


def _seek(reader, t, lo):
    """A record start at or before the fire's prompt event: gallop forward
    from `lo`, then bisect, on main-thread record time."""
    target, step, hi = t - SLACK_S, PROBE_SPAN, reader.size
    ts, _start = _probe(reader, lo, reader.size, aligned=True)
    if ts is None or ts >= target:
        return lo
    while lo + step < reader.size:
        ts, start = _probe(reader, lo + step, reader.size)
        if ts is None or ts >= target:
            hi = lo + step
            break
        lo, step = start, step * 2
    while hi - lo > PROBE_SPAN:
        mid = (lo + hi) // 2
        ts, start = _probe(reader, mid, hi)
        if ts is None or ts >= target:
            hi = mid
        else:
            lo = start
    return lo


class _Turn(object):
    """What reading one fire's turn found. status: done | eof | no-reply |
    no-anchor | turn-cap | oversized | read-cap | unreadable; texts: the
    assistant strings read; lo: where the search began; end: where it
    stopped; anchor: the anchor record's (offset, time, event) or None."""
    __slots__ = ("status", "texts", "lo", "end", "anchor")

    def __init__(self, status, texts=(), lo=0, end=0, anchor=None):
        self.status, self.texts = status, list(texts)
        self.lo, self.end, self.anchor = lo, end, anchor


def _turn(reader, t, lo, taken=None):
    """Read the fire's turn forward from `lo` (a record start). `taken` is
    the previous fire's anchor when this fire shares its second: that
    record, and every hook of its event, belong to that fire."""
    reader.seek(lo)
    texts, anchor, event, seen, first = [], None, None, False, 0
    try:
        while True:
            start, raw = reader.record()
            if raw == b"":
                return _Turn("eof" if anchor else "no-anchor", texts, lo,
                             start, anchor)
            if raw is None:
                # an unparsed record inside the turn: a tool result is not
                # the seat's words, anything else may be, or may be a prompt
                if anchor and b'"tool_result"' not in reader.head:
                    return _Turn("oversized", texts, lo, start, anchor)
            else:
                rec = _load(raw)
                kind, ts = _kind(rec), _clock(rec)
                if anchor is None:
                    if ts is not None and ts > t + ANCHOR_S:
                        return _Turn("no-anchor", texts, lo, start)
                    if kind == "hook" and ts is not None and ts >= t \
                            and not _taken(rec, start, taken):
                        event, first = _event(rec), start
                        anchor = (start, ts, event)
                    continue
                if kind == "assistant":
                    seen = True
                    texts.extend(_said(rec))
                elif kind and (seen or _next_event(kind, rec, ts, anchor[1],
                                                   event)):
                    return _Turn("done", texts, lo, start, anchor)
            if anchor and reader.pos - first > TURN_CAP:
                return _Turn("turn-cap", texts, lo, reader.pos, anchor)
    except _Cap:
        return _Turn("read-cap", texts, lo, reader.pos, anchor)


def _taken(rec, start, taken):
    """Is this hook record the previous fire's anchor, or of its event?"""
    if taken is None:
        return False
    return start == taken[0] or bool(taken[2]) and _event(rec) == taken[2]


def _next_event(kind, rec, ts, anchor_ts, event):
    """Before the turn's first assistant record: does this prompt-shaped
    record begin ANOTHER prompt event? The anchor's own event keeps its other
    hooks, the queued messages it delivered, and its own prompt record, which
    the harness writes before the hook record or, for a task notification,
    1-3 ms after it (another event's prompt is followed by its own hook
    record, which ends the turn)."""
    if kind == "queued":
        return False
    if kind == "prompt":
        return ts is None or ts > anchor_ts + CLUSTER_S
    theirs = _event(rec)
    if event and theirs:
        return theirs != event
    return ts is None or ts > anchor_ts + CLUSTER_S


def _session_turns(path, rows, budget, now):
    """-> {row key: _Turn} for one transcript's fired rows, oldest first."""
    try:
        fh = open(path, "rb")
    except OSError:
        return {k: _Turn("unreadable") for k, _t in rows}
    out = {}
    with fh:
        st = os.fstat(fh.fileno())
        reader = _Reader(fh, st.st_size, budget)
        live = now - st.st_mtime < OPEN_S
        prev = None
        for key, t in sorted(rows, key=lambda kt: kt[1]):
            lo, taken = 0, None
            if prev is not None:
                if prev.anchor and t <= prev.anchor[1]:
                    lo, taken = prev.anchor[0], prev.anchor
                elif prev.status in ("no-anchor", "read-cap", "unreadable"):
                    lo = prev.lo
                else:
                    lo = prev.end
            try:
                turn = _turn(reader, t, _seek(reader, t, lo), taken)
            except _Cap:
                turn = _Turn("read-cap", lo=lo, end=lo)
            except OSError:
                turn = _Turn("unreadable", lo=lo, end=lo)
            if turn.status == "eof":
                turn.status = "turn-open" if live else "done"
            if turn.status == "done" and not turn.texts:
                turn.status = "no-reply"
            out[key] = turn
            prev = turn
    return out


def _roots(root):
    """The config homes a row's transcript is looked for under."""
    if root:
        return [root]
    seats = os.path.join(home.global_dir(), "seats")
    return [None] + sorted(
        glob.glob(os.path.join(glob.escape(seats), "*", "claude"))
        + glob.glob(os.path.join(glob.escape(seats), "*", "instances", "*",
                                 "claude")))


def _locate(session, root):
    """-> (path, None) for the one transcript of this session, else (None,
    no-transcript | unlocatable)."""
    hits, err = set(), False
    for r in _roots(root):
        path, why = turnresponse.transcript_path(session, root=r)
        err = err or bool(why)
        if path:
            hits.add(os.path.realpath(path))
    if len(hits) == 1 and not err:
        return hits.pop(), None
    return None, "unlocatable" if hits or err else "no-transcript"


def _vocabulary(projects):
    """(entries by id, rare keywords by id) over every store scope the rows
    were drawn from; (None, None) when the store cannot be read. The store
    is imported here, not at module load: `helm inject` is the per-turn hook,
    and its fast path never loads the store."""
    try:
        from .. import store
        by_id = {}
        for p in sorted(projects, key=lambda p: (p is not None, str(p))):
            # the parse load_entries caches, without the cache: a miss there
            # WRITES the parsed-entry cache, and this report writes nothing
            for e in store.load_all(project=p, scope_fence=True):
                by_id.setdefault(str(e["id"]), e)
        df = store._df_map(list(by_id.values()))
        rare = {i: {p for p, generic in store._own_probes(e)
                    if not generic and p != i.lower()
                    and df.get(p, 0) <= RARE_DF}
                for i, e in by_id.items()}
    except Exception:
        return None, None
    return by_id, rare


def _lane_bytes(row, lane):
    sample = row.get("sample")
    lanes = sample.get("lane_bytes") if isinstance(sample, dict) else None
    if not isinstance(lanes, dict):
        lanes = row.get("bytes") if isinstance(row.get("bytes"), dict) else {}
    value = lanes.get(lane)
    return value if type(value) is int and value >= 0 else 0


def _row_home(row):
    """The row's validated config home, or None for the harness default."""
    return injection_schema.config_home(row) if row.get("v") != 1 else None


def _named(i, text):
    return re.search("(?<![%s])%s(?![%s])" % (_ID_EDGE, re.escape(i), _ID_EDGE),
                     text) is not None


def use_report(hours=24.0, min_fires=MIN_FIRES, now=None):
    """The use report as a dict (the --json shape). READ-ONLY."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    clock = time.monotonic()
    rows = _ledger_rows()
    stamps = [turnresponse._epoch(r.get("ts")) for r in rows]
    oldest = min((t for t in stamps if t is not None), default=None)
    window = [r for r, t in zip(rows, stamps) if t is not None
              and since < t <= now]
    fires, projects = [], set()
    for n, r in enumerate(window):
        fired = r.get("fired")
        if r.get("silent") or not isinstance(fired, dict):
            continue
        projects.add(r.get("project"))
        for lane in LANES:
            ids = [str(i) for i in fired.get(lane) or ()]
            for i in ids:
                fires.append({"row": n, "id": i, "lane": lane,
                              "session": r.get("session"), "ts": r["ts"],
                              "bytes": _lane_bytes(r, lane) / len(ids)})
    by_id, rare = _vocabulary(projects)
    budget = _Budget(TOTAL_CAP)
    turns, why, groups = {}, {}, {}
    for n in sorted({f["row"] for f in fires}):
        r = window[n]
        harness = injection_schema.context_value(r, "harness")
        if not r.get("session"):
            why[n] = "no-session"
        elif harness and harness != "claude":
            why[n] = "harness"
        else:
            groups.setdefault((r["session"], _row_home(r)), []).append(
                (n, turnresponse._epoch(r["ts"])))
    opened = 0
    for (session, root), keyed in sorted(
            groups.items(), key=lambda kv: (len(kv[1]), str(kv[0]))):
        path, err = _locate(session, root)
        if not path:
            for n, _t in keyed:
                why[n] = err
            continue
        opened += 1
        budget.open(TRANSCRIPT_CAP)
        turns.update(_session_turns(path, keyed, budget, now))
    stats, unknown = {}, {}
    texts = {}
    for f in fires:
        i, n = f["id"], f["row"]
        s = stats.setdefault(i, {"id": i, "lanes": set(), "fires": 0,
                                 "sessions": set(), "known": 0, "unknown": 0,
                                 "named": 0, "keyword": 0, "used": 0,
                                 "bytes": 0.0, "unused": []})
        s["fires"] += 1
        s["lanes"].add(f["lane"])
        s["bytes"] += f["bytes"]
        if f["session"]:
            s["sessions"].add(f["session"])
        turn = turns.get(n)
        if n not in texts:
            texts[n] = "\n".join(turn.texts) if turn else ""
        text = texts[n]
        named = bool(text) and _named(i, text)
        keyword = False
        if text and by_id is not None and len(rare.get(i, ())) >= KEYWORD_MIN:
            from .. import store
            hits = set(store._probe_hits(by_id[i], text.lower())[2])
            keyword = len(hits & rare[i]) >= KEYWORD_MIN
        status = turn.status if turn else why.get(n, "unreadable")
        if named or keyword:
            s["known"] += 1
            s["used"] += 1
            s["named"] += named
            s["keyword"] += keyword
        elif status == "done":
            s["known"] += 1
            s["unused"].append({"session": f["session"], "ts": f["ts"]})
        else:
            s["unknown"] += 1
            unknown[status] = unknown.get(status, 0) + 1
    ids = []
    for s in stats.values():
        s["use_rate"] = round(s["used"] / s["known"], 4) if s["known"] else None
        ids.append(s)
    ids.sort(key=lambda s: (-s["fires"], -s["bytes"], s["id"]))
    prune = [{"id": s["id"], "lanes": sorted(s["lanes"]),
              "owner_rule": "pinned" in s["lanes"],
              "fires": s["fires"], "known": s["known"],
              "used": s["used"], "use_rate": s["use_rate"],
              "bytes": int(round(s["bytes"])),
              "examples": s["unused"][:EXAMPLES]}
             for s in sorted(ids, key=lambda s: (-s["bytes"], -s["fires"],
                                                 s["id"]))
             if s["known"] >= min_fires and s["used"] < MAX_USE_RATE * s["known"]]
    known = sum(s["known"] for s in ids)
    return {
        "window_hours": hours,
        "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
        "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "ledger_complete": rows.complete,
        "ledger_since": None if oldest is None else time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(oldest)),
        "rows": len(window),
        "fires": len(fires),
        "known": known,
        "unknown": len(fires) - known,
        "unknown_reasons": unknown,
        "read": {"bytes": budget.read, "transcripts": opened,
                 "caps": {"turn": TURN_CAP, "transcript": TRANSCRIPT_CAP,
                          "total": TOTAL_CAP},
                 "seconds": round(time.monotonic() - clock, 2)},
        "min_fires": min_fires,
        "max_use_rate": MAX_USE_RATE,
        "rule": _rule(by_id is not None),
        "ids": [{"id": s["id"], "lanes": sorted(s["lanes"]),
                 "fires": s["fires"], "sessions": len(s["sessions"]),
                 "known": s["known"], "unknown": s["unknown"],
                 "named": s["named"], "keyword": s["keyword"],
                 "used": s["used"], "use_rate": s["use_rate"],
                 "bytes": int(round(s["bytes"]))} for s in ids],
        "prune": prune,
    }


def _rule(store_read):
    keyword = ("at least %d of the entry's own non-generic store keywords "
               "that at most %d entries carry (the id excluded) match there "
               "under the store's keyword law; lower trust, because a "
               "keyword entry fired on the prompt's own words"
               % (KEYWORD_MIN, RARE_DF))
    return {
        "named": "the id appears as a whole token (its type:id form counts) "
                 "in the assistant text, thinking or tool input of the "
                 "fire's own turn: from the hook record of the fire's prompt "
                 "event to the next prompt event",
        "keyword": keyword if store_read else
                   "unavailable: the store could not be read, so no fire "
                   "counts as a keyword use",
    }


def _size(n):
    if n < 1024:
        return "%d B" % n
    return "%.1f %s" % ((n / 1024.0, "KB") if n < 1 << 20
                        else (n / 1048576.0, "MB"))


def _rate(s):
    return "-" if s["use_rate"] is None else "%.1f%%" % (100 * s["use_rate"])


def _render(r):
    out = []
    if r["ledger_complete"] is not True:
        out.append("ledger census incomplete: the counts below are a lower "
                   "bound over the rows that could be read.")
    first = r["ledger_since"]
    reach = " (the ledger holds no row)" if first is None else \
        "" if first <= r["since"] else \
        " (the ledger reaches back only to %s)" % first
    out.append("use-report, last %gh: %d fires of %d ids from %d ledger "
               "rows%s" % (r["window_hours"], r["fires"], len(r["ids"]),
                           r["rows"], reach))
    reasons = ", ".join("%s %d" % kv for kv in
                        sorted(r["unknown_reasons"].items(),
                               key=lambda kv: (-kv[1], kv[0])))
    out.append("readable %d, UNKNOWN %d%s: a fire whose turn could not be "
               "read is UNKNOWN, never unused, and is out of every rate"
               % (r["known"], r["unknown"],
                  " (%s)" % reasons if reasons else ""))
    rd = r["read"]
    out.append("read %s from %d transcript(s) in %.1fs (caps: turn %s, "
               "transcript %s, total %s)"
               % (_size(rd["bytes"]), rd["transcripts"], rd["seconds"],
                  _size(rd["caps"]["turn"]), _size(rd["caps"]["transcript"]),
                  _size(rd["caps"]["total"])))
    out.append("named: " + r["rule"]["named"] + ".")
    out.append("keyword: " + r["rule"]["keyword"] + ".")
    fmt = "%-48s %6s %6s %6s %6s %7s %8s  %s"
    out.append("")
    out.append("top ids by fires:")
    out.append(fmt % ("id", "fires", "known", "named", "kw", "use%",
                      "bytes", "lanes"))
    for s in r["ids"][:TOP]:
        out.append(fmt % (s["id"], s["fires"], s["known"], s["named"],
                          s["keyword"], _rate(s), s["bytes"],
                          ",".join(s["lanes"])))
    if len(r["ids"]) > TOP:
        out.append("(+%d more ids; --json has all)" % (len(r["ids"]) - TOP))
    out.append("")
    out.append("prune candidates (readable fires >= %d, use rate < %g%%): %d"
               % (r["min_fires"], 100 * r["max_use_rate"], len(r["prune"])))
    for p in r["prune"][:TOP]:
        out.append("  %s [%s]  fires %d, readable %d, used %d (%s), %d bytes%s"
                   % (p["id"], ",".join(p["lanes"]), p["fires"], p["known"],
                      p["used"], _rate(p), p["bytes"],
                      "; pinned: an owner rule, only the owner retires it"
                      if p["owner_rule"] else ""))
        for ex in p["examples"]:
            out.append("      unused: session %s at %s"
                       % (ex["session"], ex["ts"]))
    if len(r["prune"]) > TOP:
        out.append("(+%d more candidates; --json has all)"
                   % (len(r["prune"]) - TOP))
    out.append("")
    out.append("Use is a lower bound: a seat can follow a rule without "
               "naming it. A prune candidate is evidence for a review, "
               "never an automatic retire.")
    return out


def _use_report(args):
    """--use-report [--hours N] [--min-fires N] [--json]: the use report,
    rendered or as JSON. rc 2 on a bad value; the report is read-only."""
    hours, min_fires = 24.0, MIN_FIRES
    try:
        if "--hours" in args:
            hours = float(args[args.index("--hours") + 1])
            if not hours > 0 or hours == float("inf"):
                raise ValueError
            time.gmtime(time.time() - hours * 3600)  # the window's start
    except (IndexError, ValueError, OverflowError, OSError):
        print("helm inject: --hours needs a positive number", file=sys.stderr)
        return 2
    try:
        if "--min-fires" in args:
            min_fires = int(args[args.index("--min-fires") + 1])
            if min_fires < 1:
                raise ValueError
    except (IndexError, ValueError):
        print("helm inject: --min-fires needs a whole number of at least 1",
              file=sys.stderr)
        return 2
    rep = use_report(hours=hours, min_fires=min_fires)
    if "--json" in args:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        for line in _render(rep):
            print(line)
    return 0
