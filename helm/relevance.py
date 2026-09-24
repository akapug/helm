"""helm relevance — the long-tail re-rank: one probability per (turn, line).

The keyword lane finds candidate store lines by word. A classifier then asks,
for each of the top candidates, whether the line would plausibly change what
the agent does with THIS turn. This module is that classifier's client side:
the hook's half, the worker that scores a turn, the per-turn score cache, the
shadow ledger and the fallback counters.

THE SHAPE, ONE RULE PER PARAGRAPH.

ONE SCORER PER TURN. At prompt submit the hook renders the candidates exactly
as the lane would inject them, starts ONE detached worker for the turn, and
returns. The worker embeds or evaluates the turn once and scores every
candidate, and the score lands in a per-turn cache. A later read that turn
takes it from the cache and calls no model (`turn_scores`; today that read is
`helm relevance show`, and the per-tool-call surfaces that will make it are
not built yet).

THE TURN IS READ IN THE FORM ITS LABELS HAD (`label_form`). The evaluator's
frozen threshold and the local head were both fitted on prompts stripped of
their machine envelopes and capped by one rule; a turn read any other way is
scored off the distribution the numbers came from.

TWO SCORERS, CHOSEN BY RESIDENCY, NEVER BY NAME. An outside evaluator (Jev,
through the AI gateway) scores turns whose project's AUTHORED registry
residency is `may-leave-lan`; the local head (an embedding head behind the
scorer service, `helm.relevanced`) scores everything else and is the outside
scorer's fallback. The residency is read in the worker, immediately before
the only outside call, and it FAILS CLOSED: no field, an unknown project or an
unreadable registry is `lan-only` (`registry.residency`). The code names no
project. The outside scorer stays the teacher: when it scores a turn, the
local head scores the same turn beside it and both land in the ledger.

THE WAIT IS BOUNDED, THE SCORE IS NOT. In live mode the hook waits at most
`wait_s` (1 s) for the worker's score. Past that the turn's first injection
falls back to keyword order and says so with one word (MARK), and the worker
still finishes: it writes the per-turn cache, so every later read that
turn gets the real score. In shadow mode the hook does not wait at
all; the worker records whether its score WOULD have arrived inside the bound,
which is the fallback rate live mode would see.

SHADOW BEFORE LIVE. Shadow logs the keep/drop decision the classifier would
make next to the lines the keyword lane actually delivered, and changes no
delivered byte. Live mode is built and armed, and it is off until a host's
`relevance.json` says `live`.

LOCAL BY CHOICE IS GATED ON THE CLAIM. A project that may leave the LAN runs
local-only by choice (`local_by_choice`) only while a remeasure receipt
(`helm relevance remeasure`) has PASSED for the head the service is serving.
Residency-forced local scoring is never gated: it is the only lawful scorer
there is.

FAIL OPEN, LOUDLY. Nothing here may block or fail a turn. Every trouble is a
fallback that is counted per tier (`relevance-counters.json`), written to the
ledger and readable at `helm relevance report`.
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from . import home, pk

#: The host's settings, under the helm home's global dir (home.global_json).
CONFIG = "relevance.json"
#: The scorer service's URL is a key of the operator's endpoints file, so the
#: tree names no host on the operator's LAN.
ENDPOINTS = "endpoints.json"
ENDPOINT_KEY = "relevance"
MODES = ("off", "shadow", "live")
WAIT_S = 1.0            # the bound on the hook's wait, never on the score
WAIT_MAX_S = 3.0        # the most a host file may set: the hook's whole
                        # budget is 10 s and gather alone costs about 0.5 s,
                        # so a longer wait would put the hook's alarm inside it
CACHE_TTL = 7 * 86400   # a session's score cache untouched this long is swept
CANDIDATES = 8          # the top candidates after the keyword lane's hold
KEEP_TURNS = 6          # turns kept per session in the score cache
POLL_S = 0.02
JEV_MODEL = "typesafe-ai/jev"
JEV_BASE = "https://ai-gateway.vercel.sh"
JEV_THRESHOLD = 0.32    # the evaluator's dev-frozen threshold
JEV_TIMEOUT_S = 8.0
LOCAL_TIMEOUT_S = 10.0
LABEL_CAP = 3000        # characters of turn text the labels were read at
MARK = "[keywords]"     # the one word a fallen-back live injection carries
LEDGER_MAX = 5 * 1024 * 1024
JOB_VERSION = 1

DEFAULTS = {
    "mode": "off", "wait_s": WAIT_S, "candidates": CANDIDATES, "teach": True,
    "local_by_choice": [],
    "jev": {"cred_file": "", "base": JEV_BASE, "threshold": JEV_THRESHOLD,
            "timeout_s": JEV_TIMEOUT_S},
    "local": {"timeout_s": LOCAL_TIMEOUT_S},
}

#: The outside scorer's question, worded exactly as the evaluation that froze
#: JEV_THRESHOLD worded it. Changing a word invalidates the threshold.
QUESTION = ("An AI coding agent is handling the message in the state. Its "
            "knowledge store offers it this note: «%s». Would reading this "
            "note plausibly change what the agent does in response to this "
            "message?")
YES = "Yes: the note applies to a decision or action this message calls for."
NO = ("No: the note is unrelated to what the message calls for, or only "
      "shares a word with it.")

#: A hit on any of these keeps a turn from leaving the LAN at all, whatever
#: its residency: secrets and addresses are never sent out. The turn is then
#: scored locally, and the ledger says why.
SCRUB = tuple(re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{8,}", r"\bvck_[A-Za-z0-9]", r"\bgh[opsu]_[A-Za-z0-9]{8,}",
    r"\bxox[abprs]-", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
    r"\b[0-9a-fA-F]{32,}\b",
    r"(?=[A-Za-z0-9+/]{40,})(?=[^\s]*[0-9])(?=[^\s]*[A-Z])(?=[^\s]*[a-z])"
    r"[A-Za-z0-9+/]{40,}={0,2}",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}",
    r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[=:]\s*\S{8,}",
))

_PTR = re.compile(r"…?\s*\(helm store get [^)]*\)\s*$")
_KIND = re.compile(r"^(?:PREMISE|PRIOR [0-9.]+|MOVE|TERM|REF|CAP|GATE of \S+:)\s+")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


# ------------------------------------------------------------------ settings

def settings():
    """The host's settings, every value checked, defaults filled in. An
    absent file is mode `off`; an unreadable or ill-shaped one is also `off`
    and `why` says what was wrong, so a broken file never turns scoring on."""
    out = json.loads(json.dumps(DEFAULTS))
    out["why"] = None
    path, raw, why = home.global_json(CONFIG)
    out["path"] = path
    if why:
        out["why"] = why
        return out
    if raw is None:
        return out
    mode = raw.get("mode", "off")
    if mode not in MODES:
        out["why"] = "%s: mode must be one of %s" % (path, ", ".join(MODES))
        return out
    out["mode"] = mode
    num = lambda v, lo, hi, d: float(v) if isinstance(v, (int, float)) \
        and not isinstance(v, bool) and lo < v <= hi else d
    out["wait_s"] = num(raw.get("wait_s"), 0, WAIT_MAX_S, WAIT_S)
    c = raw.get("candidates")
    out["candidates"] = c if isinstance(c, int) and not isinstance(c, bool) \
        and 1 <= c <= 32 else CANDIDATES
    out["teach"] = raw.get("teach", True) is not False
    lbc = raw.get("local_by_choice")
    out["local_by_choice"] = [x for x in lbc if isinstance(x, str) and x] \
        if isinstance(lbc, list) else []
    jev = raw.get("jev") if isinstance(raw.get("jev"), dict) else {}
    out["jev"].update({
        "cred_file": jev["cred_file"] if isinstance(jev.get("cred_file"), str) else "",
        "base": jev["base"].rstrip("/") if isinstance(jev.get("base"), str)
        and jev["base"].startswith("https://") else JEV_BASE,
        "threshold": num(jev.get("threshold"), 0, 1, JEV_THRESHOLD),
        "timeout_s": num(jev.get("timeout_s"), 0, 60, JEV_TIMEOUT_S)})
    loc = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    out["local"]["timeout_s"] = num(loc.get("timeout_s"), 0, 120, LOCAL_TIMEOUT_S)
    return out


def endpoint():
    """(url, None) or ("", why): the scorer service's base URL, read from the
    endpoints file on every call. Unconfigured answers "" and never a host."""
    path, table, why = home.global_json(ENDPOINTS)
    value = (table or {}).get(ENDPOINT_KEY)
    if why:
        return "", why
    if value is None:
        return "", ("the relevance scorer endpoint is not configured: add "
                    "\"%s\": \"http://<host>:<port>\" to %s" % (ENDPOINT_KEY, path))
    if not (isinstance(value, str) and value.strip().startswith(("http://", "https://"))):
        return "", "the %s endpoint in %s is not an http(s) URL" % (ENDPOINT_KEY, path)
    return value.strip().rstrip("/"), None


# ---------------------------------------------------------------- candidates

def note(line):
    """The injected line as the classifier reads it: the kind tag and any
    store pointer removed, the id kept. The head was fitted on exactly this
    form, byte for byte, so it is not tidied further (a `[provisional]`
    prefix stays, as it did in the labels)."""
    s = _PTR.sub("…", (line or "").strip())
    if s.startswith("REFLEX: "):
        return "reflex: " + s[8:]
    return _KIND.sub("", s)


def candidate_rows(entries):
    """[{id, note}] for store entries, each rendered as the keyword lane
    renders it (first sentence, the lane's line cap) and then read as a note."""
    from .inject import _common, _entries
    rows = []
    for e in entries:
        try:
            line = _entries._entry_line(e, cap=_common.JIT_LINE_CAP, short=True)
        except Exception:                   # noqa: BLE001 — an unrenderable entry is not a candidate
            continue
        rows.append({"id": _entries.store_typed_id(e), "note": note(line)})
    return rows


def row_hash(text):
    """The content address of a row's text: its embedding is computed once
    per hash and reused until the text changes."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def turn_id(session, text, t0):
    return hashlib.sha256(("%s\0%.6f\0%s" % (session or "-", t0, text or ""))
                          .encode("utf-8")).hexdigest()[:20]


def scrub_hits(*texts):
    """The SCRUB patterns any of `texts` matches (pattern heads, never the
    matched text)."""
    return sorted({p.pattern[:24] for t in texts for p in SCRUB if p.search(t or "")})


def cut(text, cap, head, tail):
    """`text` when it fits `cap` characters, else its first `head` and last
    `tail` characters with a marker between."""
    text = text or ""
    return text if len(text) <= cap else text[:head] + "\n[...]\n" + text[-tail:]


_TAG = {t: re.compile(r"<%s>(.*?)</%s>" % (t, t), re.S) for t in ("summary", "event", "result")}
_HDR = re.compile(r"\[helm chat [^\]\n]*\]\s*")
_FRAME = re.compile(r"\[Subagent hand-back\].*?The report follows:\s*", re.S)
_WAIT = re.compile(r"\s*\(\+\d+ waiting — helm chat read[^)]*\)")
_MORE = re.compile(r"^\[helm chat\] more pending — .*$", re.M)
_HARN = re.compile(r"^\[harness: [^\n]*\]\s*$", re.M)
_AGENT = re.compile(r"^<agent-message[^>]*>\s*|\s*</agent-message>\s*$")


def label_form(prompt):
    """The turn as its LABELS read it, byte for byte: the evaluation that froze
    the evaluator's threshold and fitted the local head read every prompt this
    way, so a scored turn must be read this way too, and changing a rule here
    invalidates both numbers.

    A task notice keeps its summary (unless it is the fixed "Monitor event:"
    line), its event and its result, one per line. An agent message loses its
    tag and a subagent hand-back frame. Harness banners, helm chat frame
    headers, the "waiting" marker and "more pending" lines go. What is left is
    stripped and, past LABEL_CAP characters, cut to its first 2,000 and last
    900 with a marker between. The local head then cuts that to its own
    `trunc`, which gives the same bytes as cutting the uncapped turn as long
    as the head keeps no more than the first 2,000 and last 900 characters:
    a `trunc` of at most 2,857, which the service refuses to load past
    (relevanced.MAX_HEAD_TRUNC)."""
    s = (prompt or "").strip()
    if s.startswith("<task-notification>"):
        got = {t: (r.search(s) or [None, None])[1] for t, r in _TAG.items()}
        parts = []
        if got["summary"] and not got["summary"].startswith("Monitor event:"):
            parts.append(got["summary"].strip())
        parts += [got[t].strip() for t in ("event", "result") if got[t]]
        s = "\n".join(parts)
    elif s.startswith("<agent-message"):
        s = _FRAME.sub("", _AGENT.sub("", s))
    s = _HARN.sub("", s)
    s = _MORE.sub("", _HDR.sub("", _WAIT.sub("", s))).strip()
    return cut(s, LABEL_CAP, 2000, 900)


# ----------------------------------------------------------- cache + ledger

def _state_dir():
    return os.path.join(home.global_dir(), ".state", "relevance")


def cache_path(session):
    return os.path.join(_state_dir(), _UNSAFE.sub("_", str(session))[:120] + ".json")


def ledger_path():
    return os.path.join(home.global_dir(), ".state", "relevance-ledger.jsonl")


def counters_path():
    return os.path.join(home.global_dir(), ".state", "relevance-counters.json")


@contextlib.contextmanager
def _locked(path):
    import fcntl
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _update(path, fn):
    """Read-modify-write one small JSON object under its sibling lock."""
    with _locked(path):
        obj = pk.read_json(path, None)
        obj = fn(obj if isinstance(obj, dict) else {})
        pk.atomic_write(path, json.dumps(obj, ensure_ascii=False) + "\n", mode=0o600)
    return obj


def _cache_put(session, turn, entry, current=False):
    def put(obj):
        turns = obj.get("turns") if isinstance(obj.get("turns"), dict) else {}
        turns[turn] = dict(turns.get(turn) or {}, **entry)
        keep = sorted(turns, key=lambda k: turns[k].get("t0") or 0)[-KEEP_TURNS:]
        obj["turns"] = {k: turns[k] for k in keep}
        if current:
            obj["current"] = turn
        return obj
    return _update(cache_path(session), put)


def sweep(now=None):
    """Remove every session's score cache (and its lock) untouched for
    CACHE_TTL -> how many caches went. Run by the worker, off the hook's
    path. A cache whose lock another process holds is skipped, and the age is
    read again under the lock, so a session that wakes mid-sweep keeps its
    file. Entirely fail-open."""
    import fcntl
    now = time.time() if now is None else now
    d, gone = _state_dir(), 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for n in names:
        p = os.path.join(d, n)
        if n.endswith(".json.lock") and n[:-5] not in names:
            try:                            # a lock whose cache is already gone
                if now - os.path.getmtime(p) > CACHE_TTL:
                    os.remove(p)
            except OSError:
                pass
            continue
        if not n.endswith(".json"):
            continue
        try:
            if now - os.path.getmtime(p) <= CACHE_TTL:
                continue
            fd = os.open(p + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if now - os.path.getmtime(p) > CACHE_TTL:
                os.remove(p)
                os.remove(p + ".lock")
                gone += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return gone


def _cache_entry(session, turn=None):
    """(turn, entry) from the session's score cache; entry is None if absent."""
    obj = pk.read_json(cache_path(session), None) if session else None
    if not isinstance(obj, dict):
        return turn, None
    turn = turn or obj.get("current")
    entry = (obj.get("turns") or {}).get(turn) if turn else None
    return turn, entry if isinstance(entry, dict) else None


def turn_scores(session, turn=None):
    """THE LATER READ: the scored entry for `turn` (default: the session's
    current turn), or None while it is pending, failed or unknown. It reads
    one small file and calls no model, no service and no network. Its caller
    today is `helm relevance show`; the per-tool-call surfaces that will read
    it are not built yet."""
    turn, entry = _cache_entry(session, turn)
    if entry is None or entry.get("state") != "scored":
        return None
    return dict(entry, turn=turn)


def _ledger(row):
    """Append one row, rotating at LEDGER_MAX (one .1 generation). Fail-open."""
    path = ledger_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LEDGER_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:                       # noqa: BLE001 — telemetry never fails a turn
        pass


OUTCOMES = ("in_time", "late", "failed")


def count(tier, outcome):
    """One outcome for one tier in the counters file: `in_time` scored inside
    the wait bound, `late` scored after it, `failed` never scored. A fallback
    is `late` or `failed`."""
    def bump(obj):
        tiers = obj.setdefault("tiers", {})
        row = tiers.setdefault(tier, {k: 0 for k in OUTCOMES})
        row[outcome] = int(row.get(outcome) or 0) + 1
        obj.setdefault("since", pk.now_ts())
        return obj
    try:
        _update(counters_path(), bump)
    except Exception:                       # noqa: BLE001 — see the module law
        pass


def counters():
    obj = pk.read_json(counters_path(), None)
    return obj if isinstance(obj, dict) else {"tiers": {}}


# ------------------------------------------------------------ the hook half

def at_prompt(prompt, entries, project=None, session=None, keyword_ids=(),
              cfg=None, spawn=None, clock=time.time, sleep=time.sleep):
    """THE HOOK'S HALF. `prompt` is the raw prompt; the worker scores its
    `label_form`. -> None when nothing is scored (mode off, no candidates,
    an empty turn), else {turn, mode, state, n[, scores, threshold, source]}
    where state is `pending` (shadow: nobody waits), `scored` (live: the
    score arrived inside the bound), `fallback` (live: it did not) or
    `unspawned` (the worker could not start).

    The hook reads no registry and calls no model: residency is decided in
    the worker, beside the only outside call."""
    cfg = settings() if cfg is None else cfg
    if cfg["mode"] == "off" or not entries:
        return None
    text = label_form(prompt)
    if not text:
        return None
    t0 = clock()
    rows = candidate_rows(list(entries)[:cfg["candidates"]])
    if not rows:
        return None
    turn = turn_id(session, text, t0)
    job = {"v": JOB_VERSION, "turn": turn, "session": session, "project": project,
           "t0": t0, "mode": cfg["mode"], "wait_s": cfg["wait_s"], "text": text,
           "candidates": rows, "keyword": [str(i) for i in keyword_ids]}
    if session:
        try:
            _cache_put(session, turn, {"t0": t0, "state": "pending"}, current=True)
        except Exception:                   # noqa: BLE001 — see the module law
            pass
    try:
        ok, why = (spawn or _spawn)(job)
    except Exception as exc:                # noqa: BLE001 — see the module law
        ok, why = False, "%s: %s" % (exc.__class__.__name__, exc)
    via, why = (why, None) if ok else (None, why)
    out = {"turn": turn, "mode": cfg["mode"], "n": len(rows),
           "state": "pending" if ok else "unspawned"}
    if cfg["mode"] == "live":
        out["state"] = "fallback"
        deadline = t0 + cfg["wait_s"]
        while ok and session:
            _turn, got = _cache_entry(session, turn)
            if got and got.get("state") == "scored":
                out.update(state="scored", scores=got.get("scores") or {},
                           threshold=got.get("threshold"), source=got.get("source"))
                break
            if (got and got.get("state") == "failed") or clock() >= deadline:
                break
            sleep(POLL_S)
        out["waited_ms"] = round((clock() - t0) * 1000, 1)
    # WHAT THIS TURN COST THE HOOK, from the first candidate rendered to the
    # return: the re-rank's whole added latency at prompt submit, per turn.
    _ledger({"ev": "submit", "ts": pk.now_ts(), "turn": turn, "session": session,
             "project": project, "mode": cfg["mode"], "n": len(rows),
             "spawned": ok, "state": out["state"],
             "hook_ms": round((clock() - t0) * 1000, 2),
             **({"via": via} if via else {}), **({"why": why} if why else {})})
    return out


def rerank(entries, scores, threshold, cap):
    """Live mode's order: the entries whose score clears `threshold`, highest
    first, at most `cap`. An entry with no score is not kept."""
    from .inject._entries import store_typed_id
    scored = [(scores.get(store_typed_id(e)), i, e) for i, e in enumerate(entries)]
    kept = [(p, i, e) for p, i, e in scored
            if isinstance(p, (int, float)) and threshold is not None and p >= threshold]
    kept.sort(key=lambda x: (-x[0], x[1]))
    return [e for _p, _i, e in kept[:cap]]


def _package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _worker_log():
    d = _state_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    log = os.path.join(d, "worker.log")
    try:
        if os.path.getsize(log) > 1024 * 1024:
            os.replace(log, log + ".1")
    except OSError:
        pass
    return log


def _spawn(job):
    """(True, "fork" | "exec") once the turn's worker is running, DETACHED in
    its own session: forked from this process when it is single-threaded, else a
    fresh interpreter.

    THE FORK IS THE FAST PATH, AND THE REASON IS MEASURED. A fresh interpreter
    importing helm costs about a second before it can score on a loaded agent
    host (the median worker start was 938 ms at load average 25), which is the
    whole wait bound spent before the scorer is even asked. A fork already has
    helm imported and starts in milliseconds. It is taken only while this
    process has one thread, because a fork copies only the calling thread and
    any lock another thread held would stay held in the child forever."""
    import threading
    if hasattr(os, "fork") and threading.active_count() == 1:
        return _fork(job)
    return _exec(job)


def _fork(job):
    """Double fork: the worker is nobody's child, so no caller reaps it and
    no caller's process group or signal reaches it. The worker keeps none of
    this process's descriptors (its stdout is the harness's pipe, and a lock
    held here would otherwise outlive the hook) and leaves through os._exit,
    so no buffer, atexit handler or finaliser of the hook runs twice."""
    log = _worker_log()
    pid = os.fork()
    if pid:
        # THE INTERMEDIATE CHILD'S EXIT SAYS WHETHER A WORKER EXISTS. It
        # exits 0 only after the second fork succeeded; a failed second fork
        # (EAGAIN on a loaded host) is a worker that never existed, and a
        # live hook must not wait its whole bound for one.
        _pid, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        if code:
            return False, "fork: the intermediate child exited %d (see %s)" % (code, log)
        return True, "fork"
    try:
        # THE HOOK'S DESCRIPTORS GO FIRST, before anything that can fail: fd 2
        # is the harness's pipe, so a traceback written before this point
        # would land in the seat's context.
        null = os.open(os.devnull, os.O_RDONLY)
        sink = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.dup2(null, 0)
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        os.closerange(3, 65536)
    except BaseException:                   # noqa: BLE001 — silent: fd 2 is still the hook's
        os._exit(1)
    code = 1
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        code = 0
        score_turn(job)
    except BaseException:                   # noqa: BLE001 — logged to worker.log, never the hook's pipe
        code = 1
        try:
            import traceback
            os.write(2, traceback.format_exc().encode("utf-8", "replace"))
        except BaseException:               # noqa: BLE001
            pass
    finally:
        os._exit(code)


def _exec(job):
    """The fresh-interpreter path. The job, which carries the turn's text,
    reaches the worker as its stdin from a private file unlinked before this
    returns, so no copy of the text outlives the worker's read."""
    root = _package_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    log = _worker_log()
    fd, tmp = tempfile.mkstemp(prefix=".job-", dir=_state_dir())
    try:
        os.write(fd, json.dumps(job, ensure_ascii=False).encode("utf-8"))
        os.lseek(fd, 0, os.SEEK_SET)
        with open(log, "ab") as sink:
            subprocess.Popen([sys.executable, "-m", "helm", "relevance", "score-turn"],
                             stdin=fd, stdout=subprocess.DEVNULL, stderr=sink,
                             start_new_session=True, close_fds=True, cwd=root, env=env)
    finally:
        os.unlink(tmp)
        os.close(fd)
    return True, "exec"


# -------------------------------------------------------------- the scorers

def jev_key(cfg):
    """The gateway key: AI_GATEWAY_API_KEY, else the credential file named by
    HELM_JEV_CRED_FILE or the settings' `jev.cred_file` (one KEY=value line;
    markdown escapes stripped). It is only ever put in a request header."""
    v = os.environ.get("AI_GATEWAY_API_KEY")
    if v:
        return v
    path = home.env("JEV_CRED_FILE") or cfg["jev"].get("cred_file")
    if not path:
        raise RuntimeError("no outside-scorer credential is configured")
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        line = next((ln for ln in f.read().splitlines() if "=" in ln), None)
    if line is None:
        raise RuntimeError("the credential file holds no KEY=value line")
    return line.split("=", 1)[1].strip().strip("\"'`").replace("\\", "")


def _post(url, body, timeout, headers=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=dict({"Content-Type": "application/json",
                      "User-Agent": "helm-relevance/1"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %d from %s" % (e.code, url.split("?")[0])) from None


def jev_scores(text, cands, cfg):
    """({id: p}, model) from the outside evaluator: ONE request for the turn,
    one yes/no question per candidate.

    `text` IS SENT AS GIVEN. It is the job's text, which `at_prompt` already
    read in its label form, and `score_turn` scrubbed exactly these bytes
    before choosing this door. Reading the form a second time here is not a
    no-op: `label_form` strips, and a strip can join two fragments into a
    token the scrub never saw (measured: two 16-hex runs around a
    "(+N waiting — …)" marker that only the second pass removes became one
    32-hex run, a scrub pattern, in the payload), and it reads a hand-back
    whose body is a task notice as the notice, so the evaluator and the local
    head were scoring different turns. The bytes the scrub read are the bytes
    that leave."""
    qs = {"q%d" % i: {"type": "boolean", "instructions": QUESTION % c["note"],
                      "criteria": {"true": YES, "false": NO}}
          for i, c in enumerate(cands)}
    resp = _post(cfg["jev"]["base"] + "/v1/evaluate",
                 {"model": JEV_MODEL, "state": {"message": text},
                  "questions": qs},
                 cfg["jev"]["timeout_s"],
                 {"Authorization": "Bearer " + jev_key(cfg)})
    answers = resp.get("answers") or {}
    scores = {}
    for i, c in enumerate(cands):
        p = (answers.get("q%d" % i) or {}).get("probability")
        if isinstance(p, (int, float)):
            scores[c["id"]] = float(p)
    if not scores:
        raise RuntimeError("the evaluator answered no question")
    return scores, JEV_MODEL


def local_scores(text, cands, cfg):
    """({id: p}, model, threshold) from the scorer service: ONE request for
    the turn; the service embeds the turn once and each row once per hash."""
    url, why = endpoint()
    if not url:
        raise RuntimeError(why)
    resp = _post(url + "/score",
                 {"turn": text, "rows": [{"id": c["id"], "text": c["note"]}
                                         for c in cands]},
                 cfg["local"]["timeout_s"])
    p = resp.get("p") or {}
    scores = {c["id"]: float(p[c["id"]]) for c in cands
              if isinstance(p.get(c["id"]), (int, float))}
    if not scores:
        raise RuntimeError("the scorer service answered no row")
    return scores, str(resp.get("model") or "?"), resp.get("threshold")


def local_health(cfg, timeout=2.0):
    """The scorer service's /health body, or raises."""
    import urllib.request
    url, why = endpoint()
    if not url:
        raise RuntimeError(why)
    with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------- local-by-choice gate

def receipt_path():
    return os.path.join(home.global_dir(), ".state", "relevance-remeasure.json")


def choice_gate(head_version):
    """(ok, why): may a project that is ALLOWED to leave the LAN run local-only
    by choice? Only while a remeasure receipt has PASSED for this exact head."""
    rec = pk.read_json(receipt_path(), None)
    if not isinstance(rec, dict):
        return False, "no remeasure receipt (helm relevance remeasure --apply)"
    if rec.get("head") != head_version:
        return False, "the remeasure receipt is for head %s, the service serves %s" % (
            rec.get("head"), head_version)
    if rec.get("pass") is not True:
        return False, "the remeasure for head %s did not pass" % head_version
    return True, "remeasure passed for head %s" % head_version


# ------------------------------------------------------------ the worker

def score_turn(job, cfg=None, jev=None, local=None, residency=None,
               health=None, clock=time.time):
    """THE WORKER: score one turn, fill the cache, write the ledger row and
    count the outcome. Returns the ledger row. Never raises past its caller's
    reach for anything but a malformed job.

    The residency is resolved HERE, and the outside scorer is called only
    when it answered `may-leave-lan` and the text passed SCRUB. The scorer
    callables and the residency reader are parameters, so tests drive this
    exact function with doubles instead of patching it."""
    from . import registry
    cfg = settings() if cfg is None else cfg
    jev = jev_scores if jev is None else jev
    local = local_scores if local is None else local
    residency = registry.residency if residency is None else residency
    health = (lambda: local_health(cfg)) if health is None else health
    text, cands = job["text"], job["candidates"]
    project, session, turn = job.get("project"), job.get("session"), job["turn"]
    started = clock()
    try:
        res = residency(project)
    except Exception as exc:                # noqa: BLE001 — fail closed, named
        res = {"value": "lan-only", "why": "residency read raised %s" % exc}
    tier = "jev" if res.get("value") == "may-leave-lan" else "local"
    why = [res.get("why") or ""]
    errors = {}
    if tier == "jev" and project in cfg["local_by_choice"]:
        try:
            version = (health() or {}).get("model")
            ok, gate = choice_gate(version)
        except Exception as exc:            # noqa: BLE001 — an unread gate is shut
            ok, gate = False, "the service health did not read: %s" % exc
        if ok:
            tier = "local"
        why.append(("local by choice: " if ok else "local by choice refused: ") + gate)
    scores = model = source = threshold = None
    t_score = clock()
    if tier == "jev":
        hits = scrub_hits(text, *(c["note"] for c in cands))
        if hits:
            errors["jev"] = "not sent: the turn matched %d scrub pattern(s)" % len(hits)
        else:
            try:
                scores, model = jev(text, cands, cfg)
                source, threshold = "jev", cfg["jev"]["threshold"]
            except Exception as exc:        # noqa: BLE001 — the local head is the fallback
                errors["jev"] = "%s: %s" % (exc.__class__.__name__, str(exc)[:200])
    if scores is None:
        try:
            scores, model, threshold = local(text, cands, cfg)
            source = "local"
        except Exception as exc:            # noqa: BLE001 — counted as failed below
            errors["local"] = "%s: %s" % (exc.__class__.__name__, str(exc)[:200])
    # THE SCORE LANDS BEFORE ANYTHING ELSE RUNS: the cache is what a waiting
    # hook and every later read that turn are polling for, so the
    # teaching call below must never stand between a score and its readers.
    done = clock()
    elapsed = done - float(job.get("t0") or done)
    wait = float(job.get("wait_s") or WAIT_S)
    outcome = "failed" if scores is None else ("in_time" if elapsed <= wait else "late")
    rounded = {k: round(v, 4) for k, v in (scores or {}).items()}
    if session:
        try:
            _cache_put(session, turn, {
                "state": "scored" if scores is not None else "failed",
                "tier": tier, "source": source, "model": model,
                "threshold": threshold, "done": done, "t0": job.get("t0"),
                "elapsed_ms": round(elapsed * 1000, 1), "scores": rounded})
        except Exception:                   # noqa: BLE001 — see the module law
            pass
    teach = None
    if source == "jev" and cfg["teach"]:
        try:
            lp, lmodel, lthr = local(text, cands, cfg)
            teach = {"model": lmodel, "threshold": lthr,
                     "scores": {k: round(v, 4) for k, v in lp.items()}}
        except Exception as exc:            # noqa: BLE001 — a lost lesson, never a lost score
            errors["teach"] = "%s: %s" % (exc.__class__.__name__, str(exc)[:200])
    keep = sorted(k for k, p in (scores or {}).items()
                  if threshold is not None and p >= threshold)
    drop = sorted(set(c["id"] for c in cands) - set(keep)) if scores is not None else []
    row = {"ev": "score", "ts": pk.now_ts(), "turn": turn, "session": session,
           "project": project, "mode": job.get("mode"), "tier": tier,
           "why": "; ".join(w for w in why if w), "source": source,
           "model": model, "threshold": threshold, "outcome": outcome,
           "elapsed_ms": round(elapsed * 1000, 1), "scores": rounded,
           # WHERE THE TIME WENT: from the hook's t0 to this worker running,
           # and the scoring calls themselves (a fallback's included).
           "start_ms": round((started - float(job.get("t0") or started)) * 1000, 1),
           "score_ms": round((done - t_score) * 1000, 1),
           "keep": keep, "drop": drop, "keyword": list(job.get("keyword") or ())}
    if teach:
        row["teach"] = teach
    if errors:
        row["errors"] = errors
    _ledger(row)
    count(tier, outcome)
    sweep()
    return row


def run_worker(stream=None):
    """`helm relevance score-turn`: read one job from stdin and score it."""
    job = json.loads((stream or sys.stdin).read())
    if not isinstance(job, dict) or job.get("v") != JOB_VERSION:
        print("helm relevance score-turn: not a version-%d job" % JOB_VERSION,
              file=sys.stderr)
        return 2
    score_turn(job)
    return 0


# ------------------------------------------------------------------ report

def _read_ledger():
    rows = []
    for path in (ledger_path() + ".1", ledger_path()):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        rows.append(r)
        except OSError:
            continue
    return rows


def _q(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def auc(pos, neg):
    """Mann-Whitney AUC of two score lists (ties count half); None if empty."""
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank, i, rsum = 0.0, 0, 0.0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        avg = (i + 1 + j) / 2.0
        rsum += avg * sum(1 for k in range(i, j) if allv[k][1])
        i = j
    n1, n0 = len(pos), len(neg)
    return (rsum - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def report(since_epoch=None, rows=None, now=None):
    """The shadow week's read: per tier, how often the score arrived inside
    the bound, how often the turn fell back, how long scoring took, what the
    classifier would keep beside what the keyword lane delivered, and, where
    the evaluator taught, how well the local head agreed with it."""
    rows = _read_ledger() if rows is None else rows
    now = time.time() if now is None else now
    since = lambda r: since_epoch is None or \
        (pk.parse_ts_epoch(r.get("ts")) or 0) >= since_epoch
    subs = {r["turn"]: r for r in rows if r.get("ev") == "submit" and since(r)}
    scored = {r["turn"]: r for r in rows if r.get("ev") == "score" and since(r)}
    tiers = {}
    for r in scored.values():
        t = tiers.setdefault(r.get("tier") or "?", {
            "turns": 0, "in_time": 0, "late": 0, "failed": 0, "ms": [],
            "sources": {}, "kept": 0, "cands": 0, "kw": 0, "kw_kept": 0})
        t["turns"] += 1
        t[r.get("outcome") if r.get("outcome") in OUTCOMES else "failed"] += 1
        t["ms"].append(r.get("elapsed_ms") or 0)
        t["sources"][r.get("source") or "none"] = t["sources"].get(r.get("source") or "none", 0) + 1
        t["kept"] += len(r.get("keep") or ())
        t["cands"] += len(r.get("scores") or {})
        kw = set(r.get("keyword") or ())
        t["kw"] += len(kw)
        t["kw_kept"] += len(kw & set(r.get("keep") or ()))
    hook = [r["hook_ms"] for r in subs.values() if isinstance(r.get("hook_ms"), (int, float))]
    out = {"tiers": {}, "submitted": len(subs), "scored": len(scored),
           "hook_ms": {"p50": _q(hook, .5), "p95": _q(hook, .95), "n": len(hook)},
           "live_fallback": sum(1 for r in subs.values() if r.get("state") in ("fallback", "unspawned")
                                and r.get("mode") == "live"),
           "unspawned": sum(1 for r in subs.values() if not r.get("spawned")),
           "lost": sum(1 for k, r in subs.items() if k not in scored and r.get("spawned")
                       and now - (pk.parse_ts_epoch(r.get("ts")) or now) > 60)}
    for name, t in sorted(tiers.items()):
        n = t["turns"]
        out["tiers"][name] = {
            "turns": n, "in_time": t["in_time"], "late": t["late"],
            "failed": t["failed"],
            "fallback_rate": round((t["late"] + t["failed"]) / n, 4) if n else None,
            "p50_ms": _q(t["ms"], .5), "p95_ms": _q(t["ms"], .95),
            "sources": t["sources"],
            "keep_share": round(t["kept"] / t["cands"], 4) if t["cands"] else None,
            "keyword_lines": t["kw"],
            "keyword_lines_kept": t["kw_kept"]}
    pos, neg, agree, both = [], [], 0, 0
    for r in scored.values():
        tch = r.get("teach")
        if r.get("source") != "jev" or not isinstance(tch, dict):
            continue
        jthr, lthr = r.get("threshold"), tch.get("threshold")
        for k, pj in (r.get("scores") or {}).items():
            pl = (tch.get("scores") or {}).get(k)
            if pl is None or jthr is None:
                continue
            (pos if pj >= jthr else neg).append(pl)
            if lthr is not None:
                both += 1
                agree += (pj >= jthr) == (pl >= lthr)
    out["teach"] = {"pairs": len(pos) + len(neg), "evaluator_keeps": len(pos),
                    "auc_local_vs_evaluator": None if auc(pos, neg) is None
                    else round(auc(pos, neg), 4),
                    "decision_agreement": round(agree / both, 4) if both else None}
    return out
