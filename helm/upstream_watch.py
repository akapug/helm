#!/usr/bin/env python3
"""helm upstream-watch — read what each new Claude Code release changed, and file
the helm tweaks that survive an adversarial refute.

THE GAP. The fleet learned each of these by accident, a day or more late: a new
model that needs a minimum Claude Code version (older builds answer 400); an
effort setting that must sit under `modelSettings` to reach that model; a
renamed settings key; new usage-reset behaviour on two vendors; a tool flag that
stopped being honoured. Claude Code ships almost daily, and every release
carries its own changelog. The owner asked for a watcher that reads each new
release and tweaks helm proactively, with helm updates going through the
integrator.

THE LOOP, one pass per day:

  1 DETECT    a release under the versions dir newer than the last one seen
  2 EVIDENCE  the schema-aware diff of the two programs (helm.upstream_surface),
              the CHANGELOG entries since the last seen release, the bundled-
              skill diff, and any tier-2 vendor page whose text changed — one
              bounded evidence bundle
  3 PROPOSE   one headless model run reads the bundle and proposes helm
              tweaks, each citing evidence copied verbatim; a citation the
              bundle does not contain is dropped before anything else sees it
  4 REFUTE    one run per proposal tries to kill it; only survivors proceed
  5 FILE      each survivor becomes a task owned by the integrator seat
              (resolved from the roster, never spelled) with its evidence and
              a proposed diff; one digest goes to #helm, and the digest is
              SILENT when nothing survived

WHY THE REFUTE STAGE IS NOT OPTIONAL. One measured pair (2.1.280 -> 2.1.281)
gave 43 upstream changes, 4 proposed tweaks and 1 survivor. The three that died
followed real changes badly: new strings from a different error class than the
one the tweak cited; a model-version gate on a code path no seat takes; a timer
that a keystroke into the pane clears. A loop that turned changes straight into
tasks would have filed all four.

THE STATE ADVANCES ONLY AFTER A PASS SUCCEEDS. `last_seen` and the vendor-page
hashes move when every stage finished: evidence gathered, every proposal
refuted or kept, every survivor filed, and the digest posted. Anything else
leaves them where they were, so the next pass reads the same release again. A
REFUSAL IS NOT A FAILURE: a survivor that duplicates an open task, or one the
posture guard refuses, is a decision the ledger made, and the digest reports it.
A ledger that cannot be written, a model run that returned nothing usable, or a
digest that did not post fails the pass.

MODEL RUNS GO THROUGH THE `claude` CLI ONLY, under the login of the Claude Code
home it runs on (Max OAuth). The child environment carries no ANTHROPIC_*
variable at all — no API key, no auth token, no base URL — and no CLAUDE*
session stamp; `--bare` is never passed, because bare mode authenticates with
ANTHROPIC_API_KEY and nothing else. The runs get three tools, Read, Grep and
Glob, in the helm checkout, and load no user settings, hooks or MCP servers.
The watcher PROPOSES; it never writes a setting, a file or a seat.

SCHEDULED by a daily systemd user timer (`helm upstream-watch --install-timer`),
never from a seat's session.
"""
import difflib
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from . import home, pk, upstream_surface

BOT = "upstream-watch"
ROOM = "helm"
PROJECT = "helm"
_STATE = "upstream-watch.json"
_DOCS = "upstream-watch"
_VERSIONS = os.path.join("~", ".local", "share", "claude", "versions")
CHANGELOG_URL = ("https://raw.githubusercontent.com/anthropics/claude-code/"
                 "main/CHANGELOG.md")
FETCH_MAX = 8 << 20             # bytes read from any one URL
FETCH_TIMEOUT_S = 30
CHANGELOG_KEEP = 60000          # chars of CHANGELOG text in one bundle
DOC_KEEP = 256 << 10            # chars of one vendor page's text snapshot
DOC_DIFF_LINES = 200            # diff lines per changed vendor page
BUNDLE_MAX = 200000             # serialized bytes of one evidence bundle
CONTEXT_MAX = 60000             # serialized bytes of one refute's context
MAX_TWEAKS = 6                  # proposals refuted per pass
OPEN_TASKS_SHOWN = 25           # open helm tasks one refute run is shown
CHANGELOG_GRACE_S = 2 * 86400   # how long a release waits for its CHANGELOG entry
MODEL_TIMEOUT_S = 1500          # one headless run
TOOLS = "Read,Grep,Glob"
# Every variable whose prefix is here is dropped from a model run's environment.
# ANTHROPIC covers the API key, the auth token and the base URL; CLAUDE covers
# every session stamp and CLAUDECODE, the nested-session marker.
_STRIPPED = ("ANTHROPIC", "CLAUDE", "HELM_CHAT", "CODEX_SESSION")
_SEMVER = re.compile(r"^\d+(?:\.\d+){1,3}$")
_CL_HEAD = re.compile(r"^##\s+\[?v?(\d+(?:\.\d+){1,3})\]?\s*$", re.M)

# THE TIER-2 SEAM: vendor pages that change more slowly than Claude Code and
# carry facts helm depends on. Each entry is fetched as text, hashed, and
# diffed against its last snapshot; a changed page joins the evidence bundle.
# Only enabled sources are fetched.
SOURCES = (
    {"name": "anthropic-models", "enabled": True,
     "url": "https://platform.claude.com/docs/en/models/overview",
     "why": "model ids, aliases, context windows and deprecations"},
    {"name": "anthropic-pricing", "enabled": False,
     "url": "https://platform.claude.com/docs/en/about-claude/pricing",
     "why": "per-model prices"},
    {"name": "anthropic-rate-limits", "enabled": False,
     "url": "https://platform.claude.com/docs/en/api/rate-limits",
     "why": "rate limits and usage tiers"},
    {"name": "codex-changelog", "enabled": False,
     "url": "https://learn.chatgpt.com/docs/changelog",
     "why": "codex CLI releases, flags and reset grants"},
    {"name": "codex-pricing", "enabled": False,
     "url": "https://learn.chatgpt.com/docs/pricing",
     "why": "the codex rate card and plan limits"},
)

_USAGE = """usage: helm upstream-watch [--dry-run] [--bundle] [--json] [--install-timer]

  One pass: find a Claude Code release newer than the last one read, build
  the evidence bundle (schema-aware program diff, CHANGELOG entries, bundled
  skills, changed vendor pages), have one headless `claude -p` run propose
  helm tweaks and one more run per proposal try to refute it, then file each
  survivor as a task owned by the integrator and post one digest to #helm
  (silent when nothing survived). The state advances only after a pass
  succeeds.

  --dry-run        run every stage, print the digest and the tasks it would
                   file; file nothing, post nothing, write no state
  --bundle         print the evidence bundle only; no model run, no state
  --json           machine-readable result
  --install-timer  install and enable the daily systemd user timer

  HELM_UPSTREAM_WATCH=0 turns the watcher off; HELM_UPSTREAM_WATCH_DRY_RUN=1
  makes every pass a dry run (docs/ENVIRONMENT.md lists every knob).
"""


# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------

def _knob(suffix, default=None, env=None):
    value = (os.environ if env is None else env).get("HELM_UPSTREAM_WATCH" + suffix)
    return default if value is None or not value.strip() else value.strip()


def enabled():
    return (_knob("", "1") or "").lower() not in ("0", "off", "no", "false")


def dry_run_forced():
    return (_knob("_DRY_RUN", "") or "").lower() in ("1", "on", "yes", "true")


def _timeout():
    try:
        return max(1, int(_knob("_TIMEOUT_S", str(MODEL_TIMEOUT_S))))
    except ValueError:
        return MODEL_TIMEOUT_S


def versions_dir():
    return os.path.expanduser(_knob("_VERSIONS_DIR", _VERSIONS))


def state_path():
    return os.path.join(home.global_dir(), ".state", _STATE)


def docs_dir():
    return os.path.join(home.global_dir(), ".state", _DOCS)


def _vkey(version):
    return tuple(int(x) for x in version.split("."))


def _checkout():
    """The helm checkout the model runs read: the stable shared checkout, never
    a disposable lane worktree."""
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return work.find_root(here) or here


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def read_state():
    """-> (state, None) or (None, why). A missing file is a first run; a file
    that exists and cannot be parsed is UNKNOWN, because reading it as a first
    run would re-read releases already filed."""
    try:
        st = pk.read_json(state_path(), None, strict=True)
    except (OSError, ValueError) as e:
        return None, "state %s is unreadable (%s)" % (state_path(), e)
    if st is None:
        return {}, None
    if not isinstance(st, dict):
        return None, "state %s is not a JSON object" % state_path()
    return st, None


# ---------------------------------------------------------------------------
# 1 DETECT
# ---------------------------------------------------------------------------

def installed(vdir=None):
    """-> ([release], None) oldest first, or (None, why). A missing versions
    dir is no install (an empty list); an unreadable one is UNKNOWN."""
    vdir = vdir or versions_dir()
    try:
        names = os.listdir(vdir)
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return None, "the versions dir %s cannot be listed (%s)" % (vdir, e.strerror or e)
    return sorted((n for n in names if _SEMVER.match(n)
                   and os.path.isfile(os.path.join(vdir, n))), key=_vkey), None


def plan(state, versions):
    """-> (plan, None) when a release needs reading, else (None, why-idle).

    With no state yet, the release before the newest counts as seen, so the
    first pass reads the newest pair. When the last seen release has been
    removed from disk, the program diff runs from the newest older release
    still installed, and the CHANGELOG still covers everything since."""
    if not versions:
        return None, "no Claude Code release is installed under %s" % versions_dir()
    new = versions[-1]
    since = state.get("last_seen")
    if since is None:
        if len(versions) < 2:
            return {"old": None, "new": new, "since": None, "baseline": True,
                    "notes": []}, None
        since = versions[-2]
    if not isinstance(since, str) or not _SEMVER.match(since):
        return None, "the recorded last_seen %r is not a release" % (since,)
    if since == new:
        return None, "Claude Code %s was already read" % new
    if _vkey(new) < _vkey(since):
        return None, ("the newest installed release %s is older than the last "
                      "one read (%s): a rollback, nothing to read" % (new, since))
    older = [v for v in versions if _vkey(v) < _vkey(new)]
    old = since if since in versions else (older[-1] if older else None)
    notes = []
    if old != since:
        notes.append("the last release read (%s) is no longer installed; the "
                     "program diff is %s -> %s and the CHANGELOG covers every "
                     "release since %s" % (since, old or "(none)", new, since))
    return {"old": old, "new": new, "since": since, "baseline": False,
            "notes": notes}, None


# ---------------------------------------------------------------------------
# 2 EVIDENCE
# ---------------------------------------------------------------------------

def fetch(url, limit=FETCH_MAX):
    """-> (text, None) or (None, why). http(s) and file URLs."""
    req = urllib.request.Request(url, headers={"User-Agent": "helm-upstream-watch"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            body = r.read(limit + 1)
    except (OSError, ValueError) as e:
        return None, "%s could not be fetched (%s)" % (url, e)
    if len(body) > limit:
        return None, "%s is larger than the %d-byte fetch bound" % (url, limit)
    return body.decode("utf-8", "replace"), None


def changelog_entries(text, since, new):
    """[(release, entry text)] for every heading in (since, new], newest first
    as the CHANGELOG orders them."""
    heads = list(_CL_HEAD.finditer(text))
    out = []
    for i, m in enumerate(heads):
        v = m.group(1)
        if (since is None or _vkey(v) > _vkey(since)) and _vkey(v) <= _vkey(new):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            out.append((v, text[m.start():end].strip()))
    return out


_DROP = re.compile(r"(?is)<(script|style|noscript|svg|template)\b[^>]*>.*?</\1\s*>")
_TAG = re.compile(r"<[^>]*>")


def page_text(raw):
    """A page's visible text, one line per text run, whitespace collapsed."""
    body = _TAG.sub("\n", _DROP.sub(" ", raw))
    lines = (" ".join(html.unescape(line).split()) for line in body.split("\n"))
    return "\n".join(line for line in lines if line)


def check_docs(state, sources=None):
    """-> (changes, commits, unavailable). `changes` join the bundle; `commits`
    are the hashes and snapshots a successful pass records. A page seen for
    the first time is a baseline: recorded, never reported as a change."""
    changes, commits, unavailable = [], {}, []
    known = state.get("docs") if isinstance(state.get("docs"), dict) else {}
    for src in SOURCES if sources is None else sources:
        if not src.get("enabled"):
            continue
        raw, why = fetch(src["url"])
        if raw is None:
            unavailable.append("%s: %s" % (src["name"], why))
            continue
        text = pk.cut_marked(page_text(raw), DOC_KEEP)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        prev = known.get(src["name"]) if isinstance(known.get(src["name"]), dict) else {}
        if prev.get("sha") == sha:
            continue
        commits[src["name"]] = {"sha": sha, "text": text}
        old = _read_snapshot(src["name"]) if prev else None
        if old is None:
            continue
        lines = list(difflib.unified_diff(old.splitlines(), text.splitlines(),
                                          lineterm="", n=1))[2:]
        kept = lines[:DOC_DIFF_LINES]
        if len(lines) > len(kept):
            kept.append("… [cut: %d of %d diff lines]" % (len(kept), len(lines)))
        changes.append({"name": src["name"], "url": src["url"], "why": src["why"],
                        "diff": "\n".join(kept)})
    return changes, commits, unavailable


def _read_snapshot(name):
    try:
        with pk.open_regular(os.path.join(docs_dir(), name + ".txt"), encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def gather(state, now=None):
    """-> {"outcome", ...}. outcome: "idle" (nothing to read), "deferred" (a
    release waits for its CHANGELOG entry), "failed" (evidence could not be
    gathered), or "ready" with `bundle`, `plan` and `commits`."""
    now = time.time() if now is None else now
    versions, why = installed()
    if versions is None:
        return {"outcome": "failed", "why": why}
    pl, idle = plan(state, versions)
    docs, doc_commits, doc_unavailable = check_docs(state)
    base = {"plan": pl, "commits": {"docs": doc_commits}, "idle": idle,
            "doc_unavailable": doc_unavailable}
    if pl is None or pl["baseline"]:
        if not docs:
            return dict(base, outcome="idle" if pl is None else "baseline",
                        bundle=None)
    cc = None
    if pl is not None and not pl["baseline"]:
        text, why = fetch(_knob("_CHANGELOG_URL", CHANGELOG_URL))
        if text is None:
            return {"outcome": "failed", "why": "CHANGELOG: " + why}
        entries = changelog_entries(text, pl["since"], pl["new"])
        notes = list(pl["notes"])
        if pl["new"] not in [v for v, _t in entries]:
            waiting = state.get("awaiting_changelog")
            since = (waiting.get("since") if isinstance(waiting, dict)
                     and waiting.get("version") == pl["new"] else None)
            since = since if isinstance(since, (int, float)) else now
            if now - since < CHANGELOG_GRACE_S:
                return dict(base, outcome="deferred",
                            why="the CHANGELOG has no entry for %s yet" % pl["new"],
                            awaiting={"version": pl["new"], "since": since})
            notes.append("the CHANGELOG still has no entry for %s after %dh; "
                         "read without it" % (pl["new"], CHANGELOG_GRACE_S // 3600))
        vdir = versions_dir()
        diff, diff_why = (upstream_surface.diff_versions(
            os.path.join(vdir, pl["old"]), os.path.join(vdir, pl["new"]))
            if pl["old"] else (None, "no older release is installed to diff against"))
        joined = "\n\n".join(t for _v, t in entries)
        cc = {"base": pl["old"], "release": pl["new"], "since": pl["since"],
              "notes": notes,
              "changelog": {"releases": [v for v, _t in entries],
                            "text": pk.cut_marked(joined, CHANGELOG_KEEP)},
              "program": diff if diff else {"unavailable": diff_why}}
    bundle = {"schema": 1, "claude_code": cc, "vendor_pages": docs,
              "vendor_pages_unavailable": doc_unavailable}
    return dict(base, outcome="ready", bundle=_fit(bundle))


def _fit(bundle):
    """Cut the largest list in the bundle in half until the bundle fits
    BUNDLE_MAX, recording each cut in `bundle["cuts"]`."""
    cuts = []
    while len(_dumps(bundle)) > BUNDLE_MAX:
        lists = []
        _lists(bundle, (), lists)
        if not lists:
            break
        path, items = max(lists, key=lambda pi: len(_dumps(pi[1])))
        if len(items) < 2:
            break
        keep = len(items) // 2
        cuts.append("%s: kept %d of %d records" % (".".join(path), keep, len(items)))
        del items[keep:]
    if cuts:
        bundle["cuts"] = cuts
    return bundle


def _lists(value, path, out):
    if isinstance(value, dict):
        for k, v in value.items():
            _lists(v, path + (str(k),), out)
    elif isinstance(value, list):
        if value and all(isinstance(x, dict) for x in value):
            out.append((path, value))
        for i, v in enumerate(value):
            _lists(v, path + (str(i),), out)


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _shown(value):
    """A value exactly as a prompt shows it."""
    return json.dumps(value, ensure_ascii=False, indent=1)


def relevant(bundle):
    """Does the bundle carry anything a model should read?"""
    if not bundle:
        return False
    cc = bundle.get("claude_code") or {}
    program = cc.get("program") or {}
    counts = program.get("counts") or {}
    return bool(bundle.get("vendor_pages") or cc.get("changelog", {}).get("releases")
                or any(counts.values()))


# ---------------------------------------------------------------------------
# 3 PROPOSE and 4 REFUTE — headless claude runs
# ---------------------------------------------------------------------------

def claude_program():
    return _knob("_CLAUDE") or shutil.which("claude")


def claude_argv(program):
    """The one argv every model run uses. The prompt rides stdin."""
    argv = [program, "-p", "--model", _knob("_MODEL", "opus"),
            "--output-format", "json", "--no-session-persistence",
            "--tools", TOOLS, "--permission-prompts", "none",
            "--strict-mcp-config", "--setting-sources", "project"]
    effort = _knob("_EFFORT", "high")
    if effort.lower() not in ("none", "off", "default"):
        argv += ["--effort", effort]
    return argv


def claude_env(base=None):
    """The model run's environment: the caller's, minus every ANTHROPIC_*,
    CLAUDE*, HELM_CHAT* and CODEX_SESSION* variable, plus the Claude Code home
    to run on — HELM_UPSTREAM_WATCH_CLAUDE_HOME, else the caller's own
    CLAUDE_CONFIG_DIR, else Claude Code's default."""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if not k.startswith(_STRIPPED)}
    chosen = _knob("_CLAUDE_HOME", env=base) or base.get("CLAUDE_CONFIG_DIR")
    if chosen:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(chosen)
    return env


def _json_object(text):
    """The JSON object a reply carries: the whole reply, a fenced block, or
    the span from its first `{` to its last `}`."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    tries = [t]
    fence = re.search(r"```(?:json)?[ \t]*\n(.*?)\n```", t, re.S)
    if fence:
        tries.insert(0, fence.group(1))
    if "{" in t:
        tries.append(t[t.find("{"):t.rfind("}") + 1])
    for cand in tries:
        try:
            value = json.loads(cand)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def run_claude(prompt, meter, run=None):
    """One headless run -> (object, None) or (None, why). `meter` accumulates
    runs, seconds and the reported cost."""
    run = run or subprocess.run
    program = claude_program()
    if not program:
        return None, "`claude` is not on PATH (set HELM_UPSTREAM_WATCH_CLAUDE)"
    started = time.monotonic()
    try:
        p = run(claude_argv(program), input=prompt, capture_output=True, text=True,
                cwd=_checkout(), env=claude_env(), timeout=_timeout())
    except subprocess.TimeoutExpired:
        return None, "claude -p did not finish within %ds" % _timeout()
    except OSError as e:
        return None, "claude could not be started (%s)" % (e.strerror or e)
    finally:
        meter["runs"] = meter.get("runs", 0) + 1
        meter["seconds"] = round(meter.get("seconds", 0) + time.monotonic() - started, 1)
    envelope = _json_object(p.stdout)
    tail = ((p.stderr or "").strip().splitlines() or [""])[-1]
    if envelope is None:
        return None, "claude -p exited %s with no JSON result (%s)" % (
            p.returncode, pk.cut_marked(tail, 200) or "no stderr")
    cost = envelope.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        meter["cost_usd"] = round(meter.get("cost_usd", 0) + cost, 4)
    if p.returncode != 0 or envelope.get("is_error"):
        return None, "claude -p exited %s: %s" % (
            p.returncode, pk.cut_marked(str(envelope.get("result") or tail), 200))
    obj = _json_object(envelope.get("result"))
    if obj is None:
        return None, "the reply carried no JSON object"
    return obj, None


_PROPOSE = """ROLE: PROPOSE
You are helm's upstream-change watcher. helm is the checkout in your working
directory: a Python, standard-library-only substrate that runs a fleet of
coding-agent seats, mostly Claude Code plus other model families through a
local proxy. helm launches Claude Code with its own settings, flags,
environment variables, hooks and model choices, and parses what Claude Code
prints and writes.

Below is a bounded EVIDENCE BUNDLE of what changed upstream. Propose the helm
changes this evidence makes necessary, or clearly worth doing now. The fleet
has learned such facts a day late before, for example: a new model needing a
minimum Claude Code version; a setting that only reaches a model when placed
under modelSettings; a renamed settings key; new usage-reset behaviour; a tool
parameter that stopped being honoured.

Rules:
- Ground every proposal in helm's code. Use Read, Grep and Glob in this
  checkout to find where helm depends on the changed behaviour. No dependency
  in helm's code means no proposal.
- Every proposal cites evidence strings copied VERBATIM from the bundle. A
  citation the bundle does not contain is discarded before anyone reads it.
- Propose helm code, doc or test changes. Never propose editing a live
  settings file, a credential home or a running seat.
- Set needs_owner to true when the change would alter fleet policy, spending,
  model choice, authorship, or anything the owner has ruled on, and put the
  question in owner_question.
- When a proposal changes how helm depends on an external tool (pinning,
  patching, forking, vendoring, wrapping or shimming Claude Code, codex, orca
  or another dependency), end its why with a clause that starts "posture:"
  and answers three questions by name: horizon (what the next release does to
  this), ownership (who keeps it aligned, how often) and who-cares (whether
  the other party wants it).
- At most %(max)d proposals, strongest first. An empty list is the correct
  answer when nothing here matters to helm.

Reply with ONLY one JSON object and no prose around it:
{"tweaks": [{"title": "<imperative, under 100 characters>",
  "why": "<the upstream change, the helm dependency as file:line, and what goes wrong if helm does nothing>",
  "evidence": ["<verbatim string from the bundle>"],
  "files": ["helm/<path>"],
  "proposed_diff": "<a unified diff, or exact edit instructions>",
  "needs_owner": false, "owner_question": ""}]}

EVIDENCE BUNDLE (JSON):
%(bundle)s
"""

_REFUTE = """ROLE: REFUTE
You are the adversarial reviewer of one proposed helm change that an
automated watcher derived from an upstream release. Your job is to KILL it if
it deserves to die. helm is the checkout in your working directory; read its
code with Read, Grep and Glob.

Try each of these and report what you checked:
1. Does the cited evidence say what the proposal claims? The error class, the
   flag, the key and the code path must be the SAME one, not a neighbour.
2. Does helm depend on the changed behaviour, on a path the fleet really
   takes? Find it in the code. A dependency you cannot find is a kill.
3. Is it already handled in helm, or already covered by one of the OPEN HELM
   TASKS listed below? When an open task covers it, refute and name the task.
4. Is the consequence real and material, not cosmetic, and not cleared by
   something else?
5. Would the change break something, or contradict an owner ruling?
When in doubt, refute: a wrong task costs more than a missed one, because the
next release gives the loop another chance.

Reply with ONLY one JSON object and no prose around it:
{"verdict": "survives" or "refuted", "reason": "<one or two sentences>",
 "checked": ["<what you verified>"]}

PROPOSAL (JSON):
%(tweak)s

EVIDENCE CONTEXT (the bundle records the proposal cites, JSON):
%(context)s

OPEN HELM TASKS most like this proposal (id: title):
%(open)s
"""


def _norm(text):
    return " ".join(str(text).split())


def _haystack(value, out):
    if isinstance(value, str):
        out.append(_norm(value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(_norm(k))
            _haystack(v, out)
    elif isinstance(value, list):
        for v in value:
            _haystack(v, out)
    return out


def tweaks_from(reply, bundle):
    """-> ([tweak], dropped, None) or (None, 0, why). Each kept tweak carries
    only the evidence strings the bundle really contains; a tweak left with no
    verified evidence is dropped here, before any refute run."""
    items = reply.get("tweaks") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        return None, 0, "the proposal reply has no `tweaks` list"
    # The decoded strings, and the JSON text exactly as the prompt showed it:
    # a citation copied with its escaped quotes is still verbatim.
    hay = "\n".join(_haystack(bundle, []) + [_norm(_shown(bundle))])
    kept, dropped = [], 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        title = _norm(item.get("title") or "")
        evidence = [e for e in item.get("evidence") or []
                    if isinstance(e, str) and len(_norm(e)) >= 8 and _norm(e) in hay]
        if not title or not evidence:
            dropped += 1
            continue
        kept.append({
            "title": pk.cut_marked(title, 120),
            "why": pk.cut_marked(str(item.get("why") or ""), 2000),
            "evidence": [pk.cut_marked(e, 500) for e in evidence],
            "files": [str(f) for f in item.get("files") or [] if isinstance(f, str)],
            "proposed_diff": pk.cut_marked(str(item.get("proposed_diff") or ""), 8000),
            "needs_owner": item.get("needs_owner") is True,
            "owner_question": pk.cut_marked(str(item.get("owner_question") or ""), 500),
        })
    if len(kept) > MAX_TWEAKS:
        dropped += len(kept) - MAX_TWEAKS
        del kept[MAX_TWEAKS:]
    return kept, dropped, None


def _records(value, path, out):
    if isinstance(value, dict):
        for k, v in value.items():
            _records(v, path + (str(k),), out)
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                out.append((".".join(path), v))
            else:
                _records(v, path, out)
    return out


def context_for(tweak, bundle):
    """The bundle records that contain the tweak's evidence, plus the
    CHANGELOG entry when it does — what a refuter needs, and no more."""
    wanted = [_norm(e) for e in tweak["evidence"]]
    picked, size = [], 0
    for where, rec in _records(bundle, (), []):
        text = "\n".join(_haystack(rec, []))
        if not any(w in text for w in wanted):
            continue
        piece = {"where": where, "record": rec}
        size += len(_dumps(piece))
        if size > CONTEXT_MAX:
            picked.append({"cut": "further matching records omitted at %d bytes" % CONTEXT_MAX})
            break
        picked.append(piece)
    cl = ((bundle.get("claude_code") or {}).get("changelog") or {}).get("text") or ""
    if any(w in _norm(cl) for w in wanted):
        picked.append({"where": "claude_code.changelog", "text": cl})
    return picked


def open_tasks_for(tweak):
    """-> (["id: title"], None) or (None, why): the open helm tasks most like
    this proposal, ranked by the ledger's own word overlap on its title and
    evidence. The filing door's duplicate guard compares TITLES and refuses
    only near-identical ones, and a release re-finds known work in new words:
    the attribution survivor of 2.1.281 overlapped the open task that already
    held it by 0.17 against a 0.8 bar. The refuter reads these and judges."""
    from . import tasks
    rows, unavailable = tasks.snapshot()
    if unavailable:
        return None, "task ledger unreadable (%s)" % unavailable
    pool = [r for r in rows.values() if tasks.project_of_row(r) in (PROJECT, None)]
    near = tasks.near_duplicates(" ".join([tweak["title"]] + tweak["evidence"]), pool,
                                 limit=OPEN_TASKS_SHOWN)
    return ["%s: %s" % (r.get("id"), pk.cut_marked(r.get("title") or "", 300))
            for r, _overlap in near], None


def refute(tweak, bundle, meter, run=None):
    """-> ({"verdict", "reason", "checked"}, None) or (None, why)."""
    known, why = open_tasks_for(tweak)
    if known is None:
        return None, why
    prompt = _REFUTE % {"tweak": _shown(tweak), "context": _shown(context_for(tweak, bundle)),
                        "open": "\n".join(known) or "(none)"}
    obj, why = run_claude(prompt, meter, run=run)
    if obj is None:
        return None, why
    verdict = obj.get("verdict")
    if verdict not in ("survives", "refuted"):
        return None, "the refute reply's verdict is %r, not survives|refuted" % (verdict,)
    return {"verdict": verdict,
            "reason": pk.cut_marked(str(obj.get("reason") or ""), 2000),
            "checked": [pk.cut_marked(str(c), 200) for c in obj.get("checked") or []
                        if isinstance(c, str)]}, None


# ---------------------------------------------------------------------------
# 5 FILE and the digest
# ---------------------------------------------------------------------------

def _owner():
    """The integrator seat -> (seat, None) or (None, why): resolved from the
    roster, never spelled here."""
    from . import seats_integrator
    return seats_integrator.integrator_seat()


def _label(bundle):
    """What a pass read, for the digest and the task title. The words are
    chosen for the posture guard: it pairs "upstream" with a seam verb such
    as "vendor", so no label may put the two side by side."""
    cc = bundle.get("claude_code") or {}
    if cc:
        return "Claude Code %s -> %s" % (cc.get("since") or cc.get("base"), cc.get("release"))
    return "tier-2 pages"


def task_text(tweak, label):
    """-> (title, note) exactly as filed."""
    title = pk.cut_marked("upstream-watch, %s: %s" % (label.replace("Claude Code ", "CC "),
                                                     tweak["title"]), 160)
    lines = ["Filed by helm upstream-watch from %s, after an adversarial refute." % label, "",
             "WHY: " + tweak["why"], "", "EVIDENCE (verbatim from the upstream evidence bundle):"]
    lines += ["  - %s" % e for e in tweak["evidence"]]
    if tweak["files"]:
        lines += ["", "FILES: " + ", ".join(tweak["files"])]
    lines += ["", "PROPOSED CHANGE:", tweak["proposed_diff"] or "(none given)", "",
              "REFUTE: survived — " + (tweak.get("refute") or {}).get("reason", "")]
    if tweak["needs_owner"]:
        lines += ["", "NEEDS OWNER DECISION: " + (tweak["owner_question"] or "see WHY")]
    return title, "\n".join(lines)


def file_tweak(tweak, owner, label):
    """-> {"state": filed|duplicate|refused|failed, ...}. A duplicate or a
    posture refusal is the ledger's decision; only an unreadable or
    unwritable ledger is a failure."""
    from . import posture, tasks
    title, note = task_text(tweak, label)
    existing, unavailable = tasks.snapshot(strict=True)
    if unavailable:
        return {"state": "failed", "why": "task ledger unreadable (%s)" % unavailable}
    verdict, near = tasks.duplicate_verdict(title, existing, project=PROJECT)
    if verdict == "duplicate":
        return {"state": "duplicate", "task": near[0][0].get("id")}
    refused = posture.check("helm task add", title + "\n" + note)
    if refused:
        return {"state": "refused", "why": pk.cut_marked(refused.splitlines()[0], 300)}
    row, err = tasks.add(title, owner, note=note, source=BOT, origin="agent",
                         project=PROJECT)
    if err:
        return {"state": "failed", "why": pk.cut_marked(err, 300)}
    return {"state": "filed", "task": row["id"]}


def digest(result):
    """The #helm digest, or None — the silent case — when nothing survived or
    every survivor is already an open task, and no task an earlier pass filed
    is still unannounced."""
    kept = [t for t in result.get("tweaks", []) if t.get("refute", {}).get("verdict") == "survives"]
    earlier = result.get("earlier") or []
    if all((t.get("filing") or {}).get("state") == "duplicate" for t in kept) and not earlier:
        return None
    owner = result.get("owner") or (earlier[0].get("owner") if earlier else None)
    dry = result.get("dry_run")
    head = ("upstream-watch: %s — %d proposed, %d survived the refute%s. @%s"
            % (result.get("label", "no new evidence"), result["proposed"], len(kept),
               " (DRY RUN: nothing filed)" if dry else "", owner or "the integrator"))
    lines = [head]
    for e in earlier:
        lines.append("  FILED %s in an earlier pass whose digest did not post: %s"
                     % (e.get("task"), e.get("title")))
    for t in kept:
        filing = t.get("filing") or {}
        state = filing.get("state", "would-file" if dry else "unfiled")
        tag = {"filed": "FILED %s" % filing.get("task"),
               "duplicate": "ALREADY OPEN %s" % filing.get("task"),
               "refused": "NOT FILED (%s)" % filing.get("why"),
               "would-file": "WOULD FILE"}.get(state, state.upper())
        lines.append("  %s%s: %s" % (tag, " — NEEDS OWNER DECISION" if t["needs_owner"] else "",
                                     t["title"]))
        if t["needs_owner"] and t["owner_question"]:
            lines.append("      owner question: " + t["owner_question"])
    return "\n".join(lines)


def _post(text):
    from . import chat
    key = hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()
    row = chat.post(text, room=ROOM, who=BOT, event_id="upstream-watch:" + key)
    return isinstance(row, dict) and bool(row.get("id"))


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------

def run_pass(dry_run=False, run=None, now=None):
    """-> result dict with `outcome`: disabled, busy, idle, baseline,
    deferred, done, or failed. Nothing is written when `dry_run`."""
    if not enabled():
        return {"outcome": "disabled", "why": "HELM_UPSTREAM_WATCH is off"}
    if dry_run:
        return _pass(dry_run=True, run=run, now=now)
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"outcome": "busy", "why": "another upstream-watch pass holds %s.lock" % path}
        return _pass(dry_run=False, run=run, now=now)


def _pass(dry_run, run, now):
    state, why = read_state()
    if state is None:
        return {"outcome": "failed", "why": why}
    got = gather(state, now=now)
    result = {"outcome": got["outcome"], "dry_run": dry_run, "why": got.get("why"),
              "idle": got.get("idle"), "tweaks": [], "proposed": 0, "dropped": 0,
              "meter": {}, "vendor_pages_unavailable": got.get("doc_unavailable", []),
              "earlier": [e for e in state.get("unannounced") or [] if isinstance(e, dict)]}
    if got["outcome"] == "failed":
        return _finish(state, result, None, dry_run)
    if got["outcome"] == "deferred":
        state = dict(state, awaiting_changelog=got["awaiting"])
        return _finish(state, result, None, dry_run, write=True)
    if got["outcome"] in ("idle", "baseline") and not result["earlier"]:
        return _finish(state, result, got, dry_run, advance=True)
    bundle = got["bundle"]
    if bundle is None or not relevant(bundle):
        return _announce(state, result, got, dry_run)
    result["label"] = _label(bundle)
    result["bundle_bytes"] = len(_dumps(bundle))
    reply, why = run_claude(_PROPOSE % {"max": MAX_TWEAKS, "bundle": _shown(bundle)},
                            result["meter"], run=run)
    if reply is None:
        return _failed(state, result, "propose: " + why, dry_run)
    tweaks, dropped, why = tweaks_from(reply, bundle)
    if tweaks is None:
        return _failed(state, result, "propose: " + why, dry_run)
    result["proposed"], result["dropped"] = len(tweaks), dropped
    for tweak in tweaks:
        verdict, why = refute(tweak, bundle, result["meter"], run=run)
        if verdict is None:
            return _failed(state, result, "refute %r: %s" % (tweak["title"], why), dry_run)
        tweak["refute"] = verdict
    result["tweaks"] = tweaks
    survivors = [t for t in tweaks if t["refute"]["verdict"] == "survives"]
    if survivors:
        owner, why = _owner()
        if owner is None:
            return _failed(state, result, "no integrator to own the tasks: " + why, dry_run)
        result["owner"] = owner
    for tweak in survivors:
        if dry_run:
            title, note = task_text(tweak, result["label"])
            tweak["would_file"] = {"title": title, "owner": result["owner"], "note": note}
            continue
        tweak["filing"] = file_tweak(tweak, result["owner"], result["label"])
        if tweak["filing"]["state"] == "failed":
            return _failed(state, result, "filing: " + tweak["filing"]["why"], dry_run)
    return _announce(state, result, got, dry_run)


def _announce(state, result, got, dry_run):
    """Post the digest (when there is one) and finish the pass."""
    result["digest"] = digest(result)
    if result["digest"] and not dry_run:
        try:
            posted = _post(result["digest"])
        except Exception as e:  # noqa: BLE001 — an unposted digest fails the pass
            posted, result["post_error"] = False, "%s: %s" % (type(e).__name__, e)
        if not posted:
            return _failed(state, result, "the digest did not post to #%s" % ROOM, dry_run)
    if result["outcome"] not in ("idle", "baseline"):
        result["outcome"] = "done"
    return _finish(state, result, got, dry_run, advance=True)


def _failed(state, result, why, dry_run):
    """A failed pass keeps every task it DID file in `unannounced`, so the next
    digest names them: on the retry they read as duplicates, and without this
    the one post that announces them would never be made."""
    result["outcome"], result["why"] = "failed", why
    filed = [{"task": t["filing"]["task"], "title": t["title"], "owner": result.get("owner")}
             for t in result["tweaks"] if (t.get("filing") or {}).get("state") == "filed"]
    state = dict(state, unannounced=result["earlier"] + filed)
    if not state["unannounced"]:
        state.pop("unannounced")
    return _finish(state, result, None, dry_run)


def _finish(state, result, got, dry_run, advance=False, write=False):
    """Record the attempt. `advance` moves last_seen and the vendor-page
    hashes; a failed pass records only `last_run`, so the same evidence is
    read again next time."""
    if dry_run:
        return result
    state = dict(state)
    state["schema"] = 1
    state["last_run"] = {"at": pk.now_ts(), "outcome": result["outcome"],
                         "why": result.get("why"), "proposed": result["proposed"],
                         "survived": sum(1 for t in result["tweaks"]
                                         if t.get("refute", {}).get("verdict") == "survives"),
                         "meter": result["meter"]}
    snapshots = {}
    if advance and got:
        pl = got.get("plan")
        if pl:
            state["last_seen"] = pl["new"]
        state.pop("awaiting_changelog", None)
        state.pop("unannounced", None)
        docs = dict(state.get("docs") or {})
        for name, commit in (got.get("commits") or {}).get("docs", {}).items():
            docs[name] = {"sha": commit["sha"], "at": pk.now_ts()}
            snapshots[name] = commit["text"]
        state["docs"] = docs
    if advance or write or result["outcome"] == "failed":
        pk.write_json(state_path(), state)
        for name, text in snapshots.items():
            pk.atomic_write(os.path.join(docs_dir(), name + ".txt"), text)
    return result


def bundle_only():
    """-> (bundle or None, why): what the next pass would read. Read-only."""
    state, why = read_state()
    if state is None:
        return None, why
    got = gather(state)
    if got["outcome"] != "ready":
        return None, got.get("why") or got.get("idle") or got["outcome"]
    return got["bundle"], None


# ---------------------------------------------------------------------------
# cadence — the daily systemd user timer
# ---------------------------------------------------------------------------

_SERVICE_NAME = "helm-upstream-watch.service"
_TIMER_NAME = "helm-upstream-watch.timer"
SYSTEMCTL_TIMEOUT_S = 30

_SERVICE = """[Unit]
Description=helm upstream-change watcher (one pass: detect, diff, propose, refute, file)

[Service]
Type=oneshot
ExecStart=%%h/.local/bin/helm upstream-watch
UnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID CODEX_SESSION_ID
%(env)sTimeoutStartSec=4h
Nice=10
IOSchedulingClass=idle
"""

_TIMER = """[Unit]
Description=helm upstream-change watcher daily cadence

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
"""


def timer_units(env=None):
    """-> (service path, service text, timer path, timer text). The service
    carries the claude program and the Claude Code home as absolute paths when
    the installer names them, because a user unit's PATH is not a shell's."""
    env = os.environ if env is None else env
    lines = []
    program = _knob("_CLAUDE", env=env) or shutil.which("claude")
    if program:
        lines.append("Environment=HELM_UPSTREAM_WATCH_CLAUDE=%s" % os.path.abspath(program))
    chosen = _knob("_CLAUDE_HOME", env=env)
    if chosen:
        lines.append("Environment=HELM_UPSTREAM_WATCH_CLAUDE_HOME=%s"
                     % os.path.abspath(os.path.expanduser(chosen)))
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, _SERVICE_NAME),
            _SERVICE % {"env": "".join(line + "\n" for line in lines)},
            os.path.join(udir, _TIMER_NAME), _TIMER)


def install_timer():
    """Write both units, daemon-reload, enable --now -> (ok, detail)."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl unavailable; run `helm upstream-watch` from another scheduler"
    spath, service, tpath, timer = timer_units()
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", _TIMER_NAME]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SYSTEMCTL_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, "%s failed: %s" % (" ".join(cmd), e)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), pk.cut_marked((r.stderr or r.stdout or "").strip(), 200))
    return True, "enabled the daily timer %s" % tpath


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _render(result):
    out = []
    outcome = result["outcome"]
    if outcome in ("idle", "baseline", "disabled", "busy", "deferred"):
        out.append("helm upstream-watch: %s — %s" % (
            outcome, result.get("why") or result.get("idle") or "first release recorded"))
    elif outcome == "failed":
        out.append("helm upstream-watch: FAILED — %s (state not advanced; the next "
                   "pass reads the same release)" % result.get("why"))
    else:
        m = result["meter"]
        out.append("helm upstream-watch: %s — bundle %s bytes, %d proposed (%d dropped "
                   "for unverified evidence), %d survived the refute; %d model run(s), "
                   "%ss%s" % (result.get("label", "?"), result.get("bundle_bytes", 0),
                              result["proposed"], result["dropped"],
                              sum(1 for t in result["tweaks"]
                                  if t["refute"]["verdict"] == "survives"),
                              m.get("runs", 0), m.get("seconds", 0),
                              ", $%.2f reported" % m["cost_usd"] if "cost_usd" in m else ""))
        for t in result["tweaks"]:
            out.append("  %-8s %s — %s" % (t["refute"]["verdict"].upper(), t["title"],
                                          t["refute"]["reason"]))
        if result.get("dry_run"):
            out.append("\n--- would post to #%s ---\n%s" % (
                ROOM, result.get("digest") or "(nothing: the digest is silent)"))
            for t in result["tweaks"]:
                wf = t.get("would_file")
                if wf:
                    out.append("\n--- would file (owner @%s) ---\n%s\n\n%s" % (
                        wf["owner"], wf["title"], wf["note"]))
    for why in result.get("vendor_pages_unavailable") or ():
        out.append("helm upstream-watch: vendor page UNAVAILABLE — %s" % why)
    return "\n".join(out)


def cmd_upstream_watch(args):
    args = list(args or [])
    from .cli import guard_tail
    rc = guard_tail("helm upstream-watch", args,
                    flags=("--dry-run", "--bundle", "--json", "--install-timer"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    if "--install-timer" in args:
        ok, detail = install_timer()
        print("helm upstream-watch: %s" % detail, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if "--bundle" in args:
        if not enabled():
            print("helm upstream-watch: disabled — HELM_UPSTREAM_WATCH is off")
            return 0
        bundle, why = bundle_only()
        if bundle is None:
            print("helm upstream-watch: no bundle — %s" % why)
            return 0
        print(json.dumps(bundle, ensure_ascii=False, indent=1))
        return 0
    result = run_pass(dry_run="--dry-run" in args or dry_run_forced())
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(_render(result))
    return 1 if result["outcome"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(cmd_upstream_watch(sys.argv[1:]))
