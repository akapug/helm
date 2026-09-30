"""helm classify — the thin, optional client for the local classifier stream.

ONE VERB, AND IT IS OPTIONAL. `classify(text, labels, source)` asks the local
classify endpoint which of 2 to 16 labels fits one text. The answer is a typed
`Result` (the label, its score, every label's probability, the backend and the
time it took) or a typed `FailOpen` naming why there is no answer: OUTCOMES
below. A consumer that gets a FailOpen does exactly what it did before the
stream existed, so a host without the stream loses nothing.

THE BACKEND IS A HOST SETTING, AND A FRESH INSTALL'S IS `none`. One of
BACKENDS, named by HELM_CLASSIFY_BACKEND, else the `backend` key of
`<helm home>/_global/classify.json` (CONFIG, read on every call). `none` makes
no call and opens no socket: every answer is `unconfigured`. With no backend
named, the older discovery still answers for a host that set it up before the
setting existed: a URL (below) means `openai-compatible`, and no URL means
`none`. A settings file that does not read, or names no kind this module
knows, is `none` and says why: a broken file never turns a backend on.

  openai-compatible  any server that speaks the classify contract at a base
                     URL: HELM_CLASSIFY_URL, else the setting's `url`, else
                     the `classify_url` key of the authored registry's host
                     block (`registry.authored_host`). The client appends
                     `/v1/classify`.
  jev                TypeSafe AI's Jev evaluator through the operator's own AI
                     gateway (`_jev_body` says what is assumed of its wire).

HELM_CLASSIFY_URL=off pins the client off whatever else says. A key is never
written in a file: the setting's `key_env` (or HELM_CLASSIFY_KEY_ENV) NAMES the
environment variable that holds it, and a settings file that carries a key
itself configures nothing.

ONLY HELM'S OWN TEXT LEAVES THIS BOX. The caller names what the text is from a
closed set, SOURCES: helm's chat, the ledger, a lane diff, a helm-lane
transcript. Anything else is refused before discovery and before a byte is
sent, and the refusal is itself an answer (`refused-source`). Client data
(client exports, member data, anything a project marks client-owned) has no
source kind here, so no caller can name it.

A CLASSIFIER FLAGS; A DETERMINISTIC RULE DECIDES. The stream never decides a
land, a review or a guard: a consumer uses a score to flag or to order
candidates for a rule that decides. A ranking may reorder a list and never
hide an item of it. Below THRESHOLD a consumer defers to its rule, and the
rule wins above it too, because a confidently wrong answer exists.

THE TEXT IS NEVER CUT. A text over the stream's limit comes back as
`too-long` (HTTP 413); trimming is the caller's decision to make and to say.

A TIME BUDGET PER CALL. `timeout_ms` (HELM_CLASSIFY_TIMEOUT_MS, else
TIMEOUT_MS) is ONE wall-clock limit on the whole call (name lookup,
connect, every read), so a hook that asks never waits past it, however a
backend drips its reply; a budget of zero answers `timeout` without sending.

THE INJECTION TRIM'S SHADOW (`inject_shadow`) is the first consumer that
runs on every turn. It asks only on a host that SET a backend, only for a
turn in a lane room (a helm-lane transcript), inside its own short budget,
and what it learns is logged on the turn's fire-ledger row; the lines the
agent receives are never changed by it.

EVERY CALL IS COUNTED AND NO TEXT IS KEPT. Each call appends one row to a
small journal under helm's cache (`journal_path`): the consumer, the source
kind, the label, the score, the time and the outcome. `helm classify metrics`
reads it back per consumer, so each consumer's metric can be measured before
and after a change. `helm classify label` adds a labelled example: a hash of
a text a consumer already judged and a number, never the text.

Contract, allowlist, bars and each consumer's metric: docs/CLASSIFY.md.
"""
import collections
import json
import os
import re
import socket
import sys
import threading
import time

#: The source kinds that may be sent to the stream. Closed: a caller naming
#: anything else is refused before any byte leaves this box.
SOURCES = ("helm-chat", "ledger", "lane-diff", "lane-transcript")
#: Why a call has no answer. Every one is fail-open for its consumer.
OUTCOMES = ("unconfigured", "unreachable", "timeout", "no-backend",
            "too-long", "refused-source", "rejected", "malformed", "scrubbed")
#: The backend kinds a host may set; `none` is a fresh install's.
BACKENDS = ("none", "openai-compatible", "jev")
#: The stream's advised floor: below it a consumer defers to its rule.
THRESHOLD = 0.75
TIMEOUT_MS = 2000
URL_ENV = "HELM_CLASSIFY_URL"
TIMEOUT_ENV = "HELM_CLASSIFY_TIMEOUT_MS"
HOST_KEY = "classify_url"
BACKEND_ENV = "HELM_CLASSIFY_BACKEND"
KEY_ENV_ENV = "HELM_CLASSIFY_KEY_ENV"
INJECT_ENV = "HELM_CLASSIFY_INJECT_MS"
#: The host's settings, under the helm home's global dir (home.global_json).
CONFIG = "classify.json"
#: The contract's text limit. The classify stream enforces it itself (413);
#: the Jev door checks it here, before a byte leaves.
TEXT_MAX = 60000
#: An answer longer than this is not read on: it reads `malformed`.
ANSWER_MAX = 1 << 20
JEV_KEY_ENV = "AI_GATEWAY_API_KEY"
#: The injection shadow's budget: short by default, never past the maximum,
#: and zero turns the shadow off.
INJECT_MS, INJECT_MAX_MS = 300, 1000
INJECT_CONSUMER = "inject-shadow"
INJECT_NOTE_CAP = 400
INJECT_TASK = ("An AI coding agent is handling this message. Which of the "
               "notes its knowledge store injected, if any, would plausibly "
               "change what the agent does in response to it?")
MIN_LABELS, MAX_LABELS = 2, 16
#: The journal rotates one generation past this size.
JOURNAL_MAX = 256 * 1024
LOCK_WAIT_S = 0.2

_LABEL_ID = re.compile(r"\A[A-Za-z0-9_.-]{1,40}\Z")
_TOKEN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_HASH = re.compile(r"\A[0-9a-f]{8,64}\Z")
_OFF = ("off", "0", "none", "no")
_ENV_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
#: Keys that would put a secret in the settings file itself.
_KEY_FIELDS = ("key", "api_key", "token", "secret")
_TS = "%Y-%m-%dT%H:%M:%SZ"


class Result(collections.namedtuple(
        "Result", "label score backend ms scores forced", defaults=(None,))):
    """An answer: `label` is the stream's own label or "unsure", `score` is
    that label's probability (the forced label's when the stream was
    unsure), `scores` every label's probability."""
    __slots__ = ()
    ok = True
    outcome = "ok"
    detail = None

    def row(self):
        return {"outcome": "ok", "label": self.label, "score": self.score,
                "scores": self.scores, "forced": self.forced,
                "backend": self.backend, "ms": self.ms}


class FailOpen(collections.namedtuple("FailOpen", "outcome detail ms")):
    """No answer, and why (one of OUTCOMES). The consumer keeps its rule."""
    __slots__ = ()
    ok = False
    label = score = backend = scores = forced = None

    def __new__(cls, outcome, detail, ms=0):
        if outcome not in OUTCOMES:
            raise ValueError("not a classify outcome: %r" % (outcome,))
        return super().__new__(cls, outcome, detail, ms)

    def row(self):
        return {"outcome": self.outcome, "detail": self.detail, "ms": self.ms}


# ------------------------------------------------------------ discovery

def settings():
    """(object, why) from CONFIG: absent is ({}, None), and a file that does
    not read is ({}, why), which the caller must not read as absent."""
    from . import home
    _path, raw, why = home.global_json(CONFIG)
    return raw or {}, why


def _url(raw, where):
    if not isinstance(raw, str) or not re.match(r"\Ahttps?://[^\s/]+", raw):
        return None, "the %s value is not an http(s) URL" % where
    return raw.strip().rstrip("/"), where


def endpoint(cfg=None):
    """(base url, where) — where is "env", "settings" or "registry" — or
    (None, why): the classify contract's base, for `openai-compatible`."""
    raw = (os.environ.get(URL_ENV) or "").strip()
    if raw.lower() in _OFF:
        return None, "%s=%s pins the classify client off" % (URL_ENV, raw)
    where = "env"
    if not raw:
        cfg = settings()[0] if cfg is None else cfg
        raw, where = cfg.get("url"), "settings"
    if not raw:
        where = "registry"
        try:
            from . import registry
            raw = registry.authored_host().get(HOST_KEY)
        except Exception as exc:                               # noqa: BLE001
            return None, "the authored registry did not read (%s)" % (
                type(exc).__name__)
        if not raw:
            return None, ("no %s and no %s in the authored registry's host "
                          "block" % (URL_ENV, HOST_KEY))
    base, where = _url(raw, where)
    if base and base.endswith("/v1/classify"):
        base = base[:-len("/v1/classify")]
    return base, where


def backend_setting(legacy=True):
    """The backend this host set: {"kind", "set_by", "url", "origin",
    "key_env", "why"}. `url` None is no call: `why` says why. `legacy` False
    answers `none` unless a backend was NAMED (env or setting), which is how
    a per-turn consumer makes its every call a choice the host wrote down."""
    def out(kind, why=None, set_by=None, url=None, where=None, key_env=None):
        return {"kind": kind, "set_by": set_by, "url": url, "origin": where,
                "key_env": key_env, "why": why}
    raw = (os.environ.get(URL_ENV) or "").strip()
    if raw.lower() in _OFF:
        return out("none", "%s=%s pins the classify client off" % (URL_ENV,
                                                                  raw), "env")
    cfg, why = settings()
    if why:
        return out("none", why, "settings")
    if any(k in cfg for k in _KEY_FIELDS):
        return out("none", "%s holds a key; name the environment variable "
                   "that holds it in key_env instead" % CONFIG, "settings")
    kind, set_by = (os.environ.get(BACKEND_ENV) or "").strip(), "env"
    if not kind and cfg.get("backend") is not None:
        kind, set_by = cfg.get("backend"), "settings"
    if not kind:
        if not legacy:
            return out("none", "no backend is set (%s, or `backend` in %s)"
                       % (BACKEND_ENV, CONFIG))
        base, where = endpoint(cfg)
        if base is None:
            return out("none", where)
        return out("openai-compatible", None, None, base, where)
    if kind not in BACKENDS:
        return out("none", "backend %r is not one of %s" % (
            kind, ", ".join(BACKENDS)), set_by)
    if kind == "none":
        return out("none", "the backend is set to none", set_by)
    key_env = ((os.environ.get(KEY_ENV_ENV) or "").strip()
               or cfg.get("key_env")
               or (JEV_KEY_ENV if kind == "jev" else None))
    if key_env is not None and not (isinstance(key_env, str)
                                    and _ENV_NAME.match(key_env)):
        return out(kind, "key_env must name an environment variable", set_by)
    if kind == "jev":
        from . import relevance
        where = ("env" if os.environ.get(URL_ENV)
                 else "settings" if raw or cfg.get("url") else "default")
        base, where = _url(raw or cfg.get("url") or relevance.JEV_BASE, where)
    else:
        base, where = endpoint(cfg)
    if base is None:
        return out(kind, where, set_by, key_env=key_env)
    return out(kind, None, set_by, base, where, key_env)


def timeout_ms():
    """The per-call budget from HELM_CLASSIFY_TIMEOUT_MS, else the setting's
    `timeout_ms`, else TIMEOUT_MS."""
    try:
        value = int(os.environ.get(TIMEOUT_ENV, ""))
    except ValueError:
        value = settings()[0].get("timeout_ms")
        if not isinstance(value, int) or isinstance(value, bool):
            return TIMEOUT_MS
    return max(0, value)


def inject_ms():
    """The injection shadow's budget: HELM_CLASSIFY_INJECT_MS, else the
    setting's `inject_ms`, else INJECT_MS; at most INJECT_MAX_MS, 0 is off."""
    try:
        value = int(os.environ.get(INJECT_ENV, ""))
    except ValueError:
        value = settings()[0].get("inject_ms")
        if not isinstance(value, int) or isinstance(value, bool):
            return INJECT_MS
    return max(0, min(value, INJECT_MAX_MS))


def check_labels(labels):
    """The labels as the stream takes them, or ValueError: 2-16 ids of
    [A-Za-z0-9_.-] up to 40 characters, each mapped to a definition or
    given as a plain list."""
    if isinstance(labels, dict):
        ids = list(labels)
        if not all(isinstance(v, str) and v.strip() for v in labels.values()):
            raise ValueError("every label needs a definition")
    elif isinstance(labels, (list, tuple)):
        ids = list(labels)
        labels = ids
    else:
        raise ValueError("labels must be an object or a list of ids")
    if not MIN_LABELS <= len(ids) <= MAX_LABELS:
        raise ValueError("%d labels; the stream takes %d to %d"
                         % (len(ids), MIN_LABELS, MAX_LABELS))
    bad = [i for i in ids if not isinstance(i, str) or not _LABEL_ID.match(i)]
    if bad or len(set(ids)) != len(ids):
        raise ValueError("label ids must be distinct [A-Za-z0-9_.-]{1,40}: %r"
                         % (bad or ids))
    return labels


# ------------------------------------------------------------ the call

def _post(url, body, timeout_s, headers=None):
    """(status, parsed body or None). Transport errors raise. `headers` is
    where a key goes, and the only place."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=dict({"Content-Type": "application/json",
                      "User-Agent": "helm-classify/1"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            status, raw = r.status, r.read(ANSWER_MAX + 1)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(ANSWER_MAX + 1)
    if len(raw) > ANSWER_MAX:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return status, None


def _within(budget_s, send, *args):
    """send(*args) under ONE wall-clock limit. urllib's timeout bounds each
    socket wait (the name lookup, the connect, every read), not the call, so
    a reply that drips a byte at a time runs on; the call runs in a daemon
    thread and is abandoned at the budget, answering socket.timeout."""
    box = {}

    def run():
        try:
            box["got"] = send(*args)
        except BaseException as exc:                         # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=run, name="helm-classify", daemon=True)
    t.start()
    t.join(budget_s)
    if t.is_alive():
        raise socket.timeout("no answer inside the budget")
    if "exc" in box:
        raise box["exc"]
    return box["got"]


def _is_timeout(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (socket.timeout, TimeoutError)) or (
        "timed out" in str(reason))


def _answer(status, body, ms):
    """Result or FailOpen from one HTTP answer."""
    if status == 413:
        return FailOpen("too-long", "the text is over the stream's limit "
                        "(HTTP 413); it was not cut", ms)
    if status == 503:
        tried = body.get("tried") if isinstance(body, dict) else None
        tried = tried if isinstance(tried, (list, tuple)) else None
        return FailOpen("no-backend", "no backend answered (HTTP 503%s)" % (
            ", tried %s" % ", ".join(map(str, tried)) if tried else ""), ms)
    if status == 400:
        why = body.get("error") if isinstance(body, dict) else None
        return FailOpen("rejected", "HTTP 400%s" % (": %s" % why if why else ""),
                        ms)
    if status != 200:
        return FailOpen("unreachable", "HTTP %d" % status, ms)
    if not isinstance(body, dict):
        return FailOpen("malformed", "the answer is not a JSON object", ms)
    label, scores = body.get("label"), body.get("scores")
    if not isinstance(label, str) or not isinstance(scores, dict) or not all(
            isinstance(v, (int, float)) for v in scores.values()):
        return FailOpen("malformed", "the answer carries no label and scores",
                        ms)
    forced = body.get("forced") if isinstance(body.get("forced"), str) else None
    score = scores.get(label, scores.get(forced))
    backend = body.get("backend") if isinstance(body.get("backend"), str) \
        else None
    return Result(label, float(score) if score is not None else None,
                  backend, ms, {k: float(v) for k, v in scores.items()},
                  forced)


def classify(text, labels, source, consumer="cli", task=None, prefix=None,
             budget_ms=None, post=None):
    """Result or FailOpen for one text. Never raises for anything the stream
    or the network does; raises ValueError only for labels no stream takes.
    `post` is the transport seam (tests)."""
    labels = check_labels(labels)
    started = time.monotonic()
    if source not in SOURCES:
        got = FailOpen("refused-source", "%r is not a source kind helm may "
                       "send (%s)" % (source, ", ".join(SOURCES)))
    else:
        got = _ask(text, labels, task, prefix,
                   timeout_ms() if budget_ms is None else budget_ms,
                   post or _post, started)
    record(consumer, source, got)
    return got


def _ask(text, labels, task, prefix, budget_ms, send, started):
    b = backend_setting()
    if b["url"] is None:
        return FailOpen("unconfigured", b["why"])
    if budget_ms <= 0:
        return FailOpen("timeout", "no time budget left; nothing was sent")
    headers = None
    if b["key_env"]:
        key = os.environ.get(b["key_env"])
        if key:
            headers = {"Authorization": "Bearer " + key}
        elif b["kind"] == "jev":
            return FailOpen("unconfigured", "the jev backend's key variable "
                            "%s is unset; nothing was sent" % b["key_env"])
    if b["kind"] == "jev":
        refused = _jev_refusal(text, labels)
        if refused:
            return refused
        url, body = b["url"] + "/v1/evaluate", _jev_body(text, labels, task)
    else:
        url, body = b["url"] + "/v1/classify", {"text": text, "labels": labels}
        if task:
            body["task"] = task
        if prefix:
            body["prefix"] = prefix
    try:
        # The key rides a header, so the test seam's three-argument
        # transport is only ever handed one when there is a key.
        budget_s = budget_ms / 1000.0
        status, parsed = (
            _within(budget_s, send, url, body, budget_s) if not headers
            else _within(budget_s, send, url, body, budget_s, headers))
    except Exception as exc:                                   # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        if _is_timeout(exc):
            return FailOpen("timeout", "no answer inside %d ms" % budget_ms, ms)
        return FailOpen("unreachable", "%s: %s" % (type(exc).__name__,
                                                   getattr(exc, "reason", exc)),
                        ms)
    ms = int((time.monotonic() - started) * 1000)
    if b["kind"] == "jev":
        return _jev_answer(status, parsed, labels, ms)
    return _answer(status, parsed, ms)


# ------------------------------------------------------------ the jev door

def _jev_refusal(text, labels):
    """FailOpen before the Jev door, or None. The gateway is outside the
    LAN: a text over TEXT_MAX is not sent (never cut), and a text or label
    that matches the relevance lane's secret patterns (`relevance.SCRUB`)
    is not sent at all."""
    if len(text or "") > TEXT_MAX:
        return FailOpen("too-long", "the text is %d characters, over %d; it "
                        "was not sent and not cut" % (len(text), TEXT_MAX))
    from . import relevance
    defs = labels.values() if isinstance(labels, dict) else labels
    hits = relevance.scrub_hits(text, *defs)
    if hits:
        return FailOpen("scrubbed", "the text matched %d secret pattern(s); "
                        "nothing was sent" % len(hits))
    return None


def _jev_body(text, labels, task):
    """The evaluate call, ONE request per text.

    ASSUMED WIRE. Jev is reached as helm/relevance.py already reaches it:
    POST <base>/v1/evaluate with {"model": relevance.JEV_MODEL, "state":
    {"message": text}, "questions": {"q<i>": {"type": "boolean",
    "instructions", "criteria": {"true", "false"}}}}, answered
    {"answers": {"q<i>": {"probability": p}}}. That call is the one this
    repo has spoken to the gateway; a multi-label question is not, so each
    label is asked as its own yes/no question and its probability is its
    score. The scores are therefore independent, not a distribution over
    the labels, and a consumer ranks by them rather than summing them."""
    from . import relevance
    ids = list(labels)
    defs = labels if isinstance(labels, dict) else {i: i for i in ids}
    lead = (task.strip() + " ") if task else ""
    qs = {"q%d" % n: {
        "type": "boolean",
        "instructions": "%sDoes the label %s fit the message? %s: %s" % (
            lead, i, i, defs[i]),
        "criteria": {"true": "Yes: the label %s fits the message." % i,
                     "false": "No: the label %s does not fit it." % i}}
        for n, i in enumerate(ids)}
    return {"model": relevance.JEV_MODEL, "state": {"message": text},
            "questions": qs}


def _jev_answer(status, body, labels, ms):
    """Result or FailOpen from the evaluate call's answer."""
    if status == 413:
        return FailOpen("too-long", "the gateway refused the text's size "
                        "(HTTP 413); it was not cut", ms)
    if status == 503:
        return FailOpen("no-backend", "the gateway answered HTTP 503", ms)
    if status == 400:
        return FailOpen("rejected", "HTTP 400", ms)
    if status != 200:
        return FailOpen("unreachable", "HTTP %d" % status, ms)
    answers = body.get("answers") if isinstance(body, dict) else None
    answers = answers if isinstance(answers, dict) else {}
    scores = {}
    for n, i in enumerate(labels):
        p = answers.get("q%d" % n)
        p = p.get("probability") if isinstance(p, dict) else None
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            scores[i] = float(p)
    if not scores:
        return FailOpen("malformed", "the gateway answered no question", ms)
    label = max(scores, key=lambda k: scores[k])
    return Result(label, scores[label], "jev", ms, scores)


def scores(text, labels, source, consumer="cli", budget_ms=None, task=None,
           post=None):
    """{label: score}, or None for NO OPINION: no backend, a refused source,
    a backend that is down, slow or refuses the size, or an answer without
    scores. A classifier flags and never decides, so a caller that gets None
    does exactly what it did before it asked. The call is journalled like
    any other; `classify` gives the same call's reason."""
    got = classify(text, labels, source, consumer=consumer, task=task,
                   budget_ms=budget_ms, post=post)
    return dict(got.scores) if got.ok and got.scores else None


# ------------------------------------------------------------ consumers

def _lane_room(cwd):
    """Is `cwd` a lane or seat room? The fold `hooks._in_lane_room` and
    `automap.find_root` apply, so the three can never disagree."""
    if not cwd:
        return False
    from . import automap
    p = os.path.normpath(cwd)
    return automap._strip_worktree(p) != p


def inject_shadow(prompt, lines, cwd=None, post=None):
    """THE INJECTION TRIM, IN SHADOW. -> None when nothing was asked, else
    {"outcome", "ms", "n"[, "scores", "none", "relevant"]}: the per-turn
    hook logs it on the turn's fire-ledger row, beside the lines it
    delivered, and changes none of them.

    One call per turn: `prompt` is the text, and each delivered line (at
    most MAX_LABELS - 1) is a label whose definition is the line, beside a
    `none` label. `scores` is aligned with `lines` (None past the cap),
    `relevant` the indexes at or above THRESHOLD.

    It asks only on a host that NAMED a backend
    (`backend_setting(legacy=False)`), so a `none` host pays one small
    settings read, and only for a turn in a lane room, whose prompt and
    injected lines are a helm-lane transcript.
    The budget is `inject_ms()`."""
    if backend_setting(legacy=False)["url"] is None:
        return None
    budget = inject_ms()
    rows = [(n, l) for n, l in enumerate(lines or ())
            if isinstance(l, str) and l.strip()][:MAX_LABELS - 1]
    if not budget or not rows or not (prompt or "").strip() \
            or not _lane_room(cwd):
        return None
    labels = {"n%d" % n: l.strip()[:INJECT_NOTE_CAP] for n, l in rows}
    labels["none"] = "none of these notes applies to this message"
    got = classify(prompt, labels, "lane-transcript", consumer=INJECT_CONSUMER,
                   task=INJECT_TASK, budget_ms=budget, post=post)
    out = {"outcome": got.outcome, "ms": got.ms, "n": len(rows)}
    if got.ok:
        got_scores = got.scores or {}
        out["scores"] = [
            round(got_scores["n%d" % n], 4) if isinstance(
                got_scores.get("n%d" % n), float) else None
            for n in range(len(lines))]
        none = got_scores.get("none")
        out["none"] = round(none, 4) if isinstance(none, float) else None
        out["relevant"] = [n for n, p in enumerate(out["scores"])
                           if p is not None and p >= THRESHOLD]
    return out


# ------------------------------------------------------------ the journal

def journal_path():
    """The metric journal, under helm's cache (HELM_CACHE_DIR)."""
    from . import registry
    return os.path.join(registry.cache_root(), "classify", "metrics.jsonl")


def _token(value):
    return value if isinstance(value, str) and _TOKEN.match(value) else None


def _append(row):
    """One row, locked and bounded; False on any failure. Never raises."""
    try:
        from . import eventledger
        dest = journal_path()
        row = dict(row, id=os.urandom(8).hex(),
                   ts=time.strftime(_TS, time.gmtime()))
        with eventledger.locked(dest, timeout=LOCK_WAIT_S) as held:
            if not held:
                return False
            try:
                if os.path.getsize(dest) > JOURNAL_MAX:
                    os.replace(dest, dest + ".1")
            except OSError:
                pass
            return eventledger.append_unlocked(dest, row)
    except Exception:                                          # noqa: BLE001
        return False


def record(consumer, source, got):
    """The one metric row for one call: no text, ever."""
    score = got.score if got.ok else None
    return _append({"kind": "call", "consumer": _token(consumer) or "cli",
                    "source": source if source in SOURCES else "refused",
                    "label": _token(got.label) if got.ok else None,
                    "score": round(score, 4) if score is not None else None,
                    "ms": got.ms, "outcome": got.outcome,
                    "backend": _token(got.backend) if got.ok else None})


def record_labelled(consumer, label, digest, number=None):
    """One labelled example: a consumer's hash of a text it judged, its
    label and an optional number (a list entry). Never the text."""
    if not (_token(consumer) and _LABEL_ID.match(label or "")
            and _HASH.match(digest or "")):
        return False
    return _append({"kind": "labelled", "consumer": consumer, "label": label,
                    "hash": digest,
                    "index": number if isinstance(number, int) else None})


def read_journal():
    """(rows, unreadable), both generations, oldest first."""
    from . import eventledger
    out = []
    for p in (journal_path() + ".1", journal_path()):
        rows, unavailable = eventledger.checked_events(p)
        if unavailable:
            return [], unavailable
        out.extend(rows)
    return out, None


def metrics(consumer=None, rows=None):
    """{consumer: counts} from the journal: calls per outcome, labels,
    the mean time of answered calls, answers at or above THRESHOLD, and
    labelled examples per label. `unreadable` names a journal that did not
    read; the counts are then None."""
    unreadable = None
    if rows is None:
        rows, unreadable = read_journal()
    if unreadable:
        return {"journal": journal_path(), "unreadable": unreadable,
                "consumers": None}
    out = {}
    for r in rows:
        name = r.get("consumer")
        if not isinstance(name, str) or (consumer and name != consumer):
            continue
        c = out.setdefault(name, {"calls": 0, "outcomes": {}, "labels": {},
                                  "confident": 0, "ms_total": 0,
                                  "answered": 0, "labelled": {}})
        if r.get("kind") == "labelled":
            key = str(r.get("label"))
            c["labelled"][key] = c["labelled"].get(key, 0) + 1
            continue
        c["calls"] += 1
        outcome = str(r.get("outcome"))
        c["outcomes"][outcome] = c["outcomes"].get(outcome, 0) + 1
        if outcome == "ok":
            c["answered"] += 1
            c["ms_total"] += r.get("ms") if isinstance(r.get("ms"), int) else 0
            key = str(r.get("label"))
            c["labels"][key] = c["labels"].get(key, 0) + 1
            if isinstance(r.get("score"), (int, float)) \
                    and r["score"] >= THRESHOLD:
                c["confident"] += 1
    for c in out.values():
        c["ms_mean"] = (c.pop("ms_total") // c["answered"]
                        if c["answered"] else None)
    return {"journal": journal_path(), "unreadable": None, "consumers": out}


# ------------------------------------------------------------ label sets

def label_set(name):
    """(labels, task) for a named label set, or KeyError."""
    if name == "private-name":
        from . import localnames, private_names
        public = private_names.PUBLIC_NAMES + tuple(
            n for n in localnames.words("public-names")
            if n not in private_names.PUBLIC_NAMES)
        return private_names.advisory_labels(public)
    raise KeyError(name)


LABEL_SETS = ("private-name",)


def evaluate(name, post=None, budget_ms=None):
    """The measuring pass for one consumer's label set: every row of its
    labelled set asked once, ranked by the positive label's probability.
    {"rows": [(expected, score or None, outcome)], "tp", "fn", "fp", "tn",
    "unanswered"} at THRESHOLD."""
    if name != "private-name":
        raise KeyError(name)
    from . import private_names
    labels, task = label_set(name)
    rows, counts = [], {"tp": 0, "fn": 0, "fp": 0, "tn": 0, "unanswered": 0}
    for text, positive in private_names.labelled_set():
        got = classify(text, labels, "lane-diff", consumer="eval-" + name,
                       task=task, budget_ms=budget_ms, post=post)
        score = got.scores.get("private") if got.ok else None
        rows.append((positive, score, got.outcome))
        if score is None:
            counts["unanswered"] += 1
            continue
        flagged = score >= THRESHOLD
        counts[("tp" if flagged else "fn") if positive
               else ("fp" if flagged else "tn")] += 1
    return dict(counts, rows=rows)


# ------------------------------------------------------------ the verb

USAGE = """usage: helm classify --labels L --source KIND [--consumer NAME] [--task TEXT] [--each-line] [--budget-ms N] [--json]   (text on stdin)
       helm classify where [--json]
       helm classify metrics [--consumer NAME] [--json]
       helm classify label --consumer NAME --label ID   (stdin: HASH [NUMBER] per line)
       helm classify eval private-name [--json]

  --labels   JSON object {id: definition}, JSON list, comma-separated ids, or @SET (@private-name)
  --source   one of: %s — anything else is refused and nothing is sent
  exit 0 answered, 1 fail-open (the answer says why), 2 usage""" % ", ".join(SOURCES)


def _parse_labels(raw):
    raw = (raw or "").strip()
    if raw.startswith("@"):
        return label_set(raw[1:])
    if raw.startswith(("{", "[")):
        return json.loads(raw), None
    return [p.strip() for p in raw.split(",") if p.strip()], None


def _take(argv, flags, valued):
    """({flag: value or True}, positional) or ValueError on a bad tail."""
    got, pos = {}, []
    while argv:
        head = argv.pop(0)
        if head in valued:
            if not argv:
                raise ValueError("%s wants a value" % head)
            got[head] = argv.pop(0)
        elif head in flags:
            got[head] = True
        elif head.startswith("-"):
            raise ValueError("unknown argument '%s'" % head)
        else:
            pos.append(head)
    return got, pos


def _say(row, as_json, line=None):
    if line is not None:
        row = dict(row, line=line)
    if as_json:
        print(json.dumps(row, sort_keys=True))
    elif row["outcome"] == "ok":
        print("%s%s  %s  %s  %sms" % (
            "%d: " % line if line is not None else "", row["label"],
            "%.3f" % row["score"] if row["score"] is not None else "-",
            row.get("backend") or "-", row.get("ms")))
    else:
        print("%sFAIL-OPEN %s: %s" % ("%d: " % line if line is not None
                                      else "", row["outcome"], row["detail"]))


def _cmd_where(argv):
    got, pos = _take(argv, ("--json",), ())
    if pos:
        raise ValueError("where takes no argument")
    b = backend_setting()
    base = b["url"]
    if got.get("--json"):
        print(json.dumps({"backend": b["kind"], "set_by": b["set_by"],
                          "url": base, "from": b["origin"] if base else None,
                          "key_env": b["key_env"],
                          "why": None if base else b["why"]}, sort_keys=True))
    elif base:
        print("helm classify: %s %s (from %s)%s" % (
            b["kind"], base, b["origin"],
            ", key in $%s" % b["key_env"] if b["key_env"] else ""))
    else:
        print("helm classify: %s, unconfigured — %s" % (b["kind"], b["why"]))
    return 0 if base else 1


def _cmd_metrics(argv):
    got, pos = _take(argv, ("--json",), ("--consumer",))
    if pos:
        raise ValueError("metrics takes no argument")
    m = metrics(got.get("--consumer"))
    if got.get("--json"):
        print(json.dumps(m, sort_keys=True))
        return 1 if m["unreadable"] else 0
    if m["unreadable"]:
        print("helm classify: journal UNREADABLE — %s (%s). This is NOT zero "
              "calls." % (m["unreadable"], m["journal"]), file=sys.stderr)
        return 1
    if not m["consumers"]:
        print("helm classify: no calls recorded (%s)" % m["journal"])
        return 0
    for name, c in sorted(m["consumers"].items()):
        print("%s: %d call(s), %d answered (%d at >= %.2f), mean %s ms; "
              "outcomes %s; labels %s; labelled %s" % (
                  name, c["calls"], c["answered"], c["confident"], THRESHOLD,
                  c["ms_mean"] if c["ms_mean"] is not None else "-",
                  json.dumps(c["outcomes"], sort_keys=True),
                  json.dumps(c["labels"], sort_keys=True),
                  json.dumps(c["labelled"], sort_keys=True)))
    return 0


def _cmd_label(argv):
    got, pos = _take(argv, (), ("--consumer", "--label"))
    if pos or "--consumer" not in got or "--label" not in got:
        raise ValueError("label wants --consumer NAME --label ID")
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if parts:
            number = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() \
                else None
            record_labelled(got["--consumer"], got["--label"], parts[0], number)
    return 0


def _cmd_eval(argv):
    got, pos = _take(argv, ("--json",), ())
    if pos != ["private-name"]:
        raise ValueError("eval takes one label set: private-name")
    res = evaluate(pos[0])
    if got.get("--json"):
        print(json.dumps(res, sort_keys=True))
    else:
        print("helm classify eval %s: at >= %.2f  tp %d  fn %d  fp %d  tn %d  "
              "unanswered %d" % (pos[0], THRESHOLD, res["tp"], res["fn"],
                                 res["fp"], res["tn"], res["unanswered"]))
    return 1 if res["unanswered"] else 0


def _cmd_ask(argv):
    got, pos = _take(argv, ("--json", "--each-line"),
                     ("--labels", "--source", "--consumer", "--task",
                      "--budget-ms"))
    if pos:
        raise ValueError("unknown argument '%s'" % pos[0])
    if "--labels" not in got:
        raise ValueError("--labels is required")
    try:
        labels, task = _parse_labels(got["--labels"])
        check_labels(labels)
    except KeyError as exc:
        raise ValueError("no label set named %s (%s)" % (
            exc, ", ".join("@" + s for s in LABEL_SETS)))
    task = got.get("--task") or task
    budget = got.get("--budget-ms")
    if budget is not None:
        if not budget.isdigit():
            raise ValueError("--budget-ms wants a whole number")
        budget = int(budget)
    source = got.get("--source")
    consumer = got.get("--consumer", "cli")
    as_json = bool(got.get("--json"))
    text = sys.stdin.read()
    texts = ([(n, t) for n, t in enumerate(text.splitlines(), 1) if t.strip()]
             if got.get("--each-line") else [(None, text)])
    if not any(t.strip() for _n, t in texts):
        raise ValueError("no text on stdin")
    deadline = None if budget is None else time.monotonic() + budget / 1000.0
    rc, stop = 0, None
    for n, t in texts:
        if stop is not None:
            _say(FailOpen(stop.outcome, "not sent: this run already answered "
                          "%s" % stop.outcome).row(), as_json, n)
            rc = 1
            continue
        left = None if deadline is None else max(
            0, int((deadline - time.monotonic()) * 1000))
        call_budget = timeout_ms() if left is None else min(left, timeout_ms())
        res = classify(t, labels, source, consumer=consumer, task=task,
                       budget_ms=call_budget)
        _say(res.row(), as_json, n)
        if not res.ok:
            rc = 1
            if res.outcome in ("unconfigured", "unreachable", "timeout",
                               "no-backend", "refused-source"):
                stop = res
    return rc


def cmd(args):
    argv = list(args)
    head = args[0] if args else ""
    try:
        if head in ("-h", "--help"):
            print(USAGE)
            return 0
        if head == "where":
            return _cmd_where(argv[1:])
        if head == "metrics":
            return _cmd_metrics(argv[1:])
        if head == "label":
            return _cmd_label(argv[1:])
        if head == "eval":
            return _cmd_eval(argv[1:])
        return _cmd_ask(argv)
    except ValueError as exc:
        print("helm classify: %s\n%s" % (exc, USAGE), file=sys.stderr)
        return 2
