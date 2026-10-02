#!/usr/bin/env python3
"""Knowledge at the DOOR: the rule about an act is said at the act (task/1135).

THE MISS, MEASURED. The store was read only on the text that STARTS a
turn (inject, coach, the MCP server and the `store resolve` CLI). No hook
read it for the agent's own act, so a rule written for exactly that act sat
in the store while the act ran: a reviewer launched with `--home` onto a Max
account (the Max-account reserve rule), `helm task add --owner-asked --priority P1`
typed by habit (friction-tax), "helm's copy of its token is stale" posted
after reading `helm creds` (claude-cred-1yr-auth). The trigger design had
named the fix (its lane 4, the act routes), twelve of its thirteen act
routes were still `planned`, and each attack on "knowledge at the decision"
had built one hand-written rung instead of the general door.

THE DOOR IS DATA. Nothing in this module names a rule. `detect` turns a
helm command line into ROUTE IDS read off its argv, the way the CLI
dispatcher reads it: `act.helm.<verb>`, `act.helm.<verb>.<sub>` for its
subcommand, `act.helm.<verb>.<flag>` for each long flag, and, for a flag
whose VALUE names the moment (`--kind review`), `act.helm.<verb>.<value>`.
An Agent call is `act.spawn`. A store entry declares the moment it answers
with a `route:<id>` keyword cell (store.resolve.routes), and a reflex with
`signal: act` and a `route:` field (reflex.act_steers); argv-guard says each
declaring rule once per context, at most LINE_CAP bytes. A rule nobody
bound is silence, so the door costs nothing where it has nothing to say.

ADVICE, NEVER A REFUSAL (the owner's ruling: "a scalpel where a hammer is
called for"). A PreToolUse line reaches the model WITH the call's
result (f897d7b595), so it reads "you just ran X" and the call proceeds.

THE AGENT'S OWN WORDS CAN BE READ IN SHADOW, and only when
HELM_DOOR_SHADOW=1: no per-post cost ships on by default. Switched on, a
`helm chat post|reply|dm` body is matched against the store's PHRASE probes
only (never a lone word: E2 measured lone single-word matches at 61% of
injections and 8% relevant), one rule, once per context, and the would-fire
is LOGGED to the moment ledger (route act.chat.post, outcome `shadow`) and
never printed, until the report's would-fire rate says the match earns its
bytes.

THE COMMON PATH ADDS NOTHING. Hook processes are the fleet's CPU budget
(each argv-guard python costs 0.13-0.19 s of CPU at ~15 births a second), so
a command that never names helm does not import this module (a substring
test in chat._door_wanting), a helm verb nobody bound stops at one small
file, the DOOR TABLE (`<global>/.state/door-table.json`: the route ids any
rule binds, the store directories each index was built from, and each cwd's
project with the registry mtime it was answered under; one stat of the
registry drops the cwd answers when it moves). A bound act reads one more
file, the index its project's table record names (`door-index-<key>.json`:
each route's store rules and act reflexes), while the record is younger
than INDEX_TTL_S and every directory it lists (the store's and the act
reflexes') keeps its mtime: a stat each, no store import. The store is
imported only to REBUILD an index, off inject's parsed-entry cache (a
direct parse measured 952 ms on the loaded box, the cache 72 ms), and a
rebuild refreshes the table for every seat; the shadow reads one marshal
file of the phrase probes packed by first word.

Fail-open everywhere: a door that throws wedges nothing and says nothing.
"""
import hashlib
import json
import os
import re
import time

from . import home, pk


class _Rx(object):
    """A regex compiled on its first use: argv-guard imports this module on
    every helm call and every Agent call, and most never reach a pattern."""

    __slots__ = ("_args", "_rx")

    def __init__(self, *args):
        self._args, self._rx = args, None

    def __getattr__(self, name):
        if self._rx is None:
            self._rx = re.compile(*self._args)
        return getattr(self._rx, name)

PREFIX = "[helm door] "
# The whole rendered line, prefix included, in UTF-8 BYTES.
LINE_CAP = 250
# Rules one call may carry (the envelope, chat.ENVELOPE_BUDGET, is 900).
MAX_LINES = 2
INDEX_TTL_S = 300
INDEX_V = 2
# An index row's rule text is cut here: the line cut is LINE_CAP anyway.
_ROW_KEEP = 400

SPAWN = "act.spawn"
SHADOW_ROUTE = "act.chat.post"

# A flag whose VALUE names the moment, not the flag: `dispatch send --kind
# review` is the review dispatch (act.helm.dispatch.review).
VALUE_LEAVES = ("--kind",)

_WORD = _Rx(r"^[a-z][a-z0-9-]*$")
_FLAG = _Rx(r"^--([a-z][a-z0-9-]*)(?:=(.*))?$", re.S)
_ASSIGN = _Rx(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A cheap gate before any parse: the word `helm` followed by a blank.
_HELM_AT = _Rx(r"(?:^|[\s;&|(`/])helm\s")
_LAUNCHERS = frozenset(("sudo", "env", "nice", "ionice", "nohup", "command",
                        "exec", "time", "timeout", "stdbuf", "chrt",
                        "setsid"))
# launcher options whose value is the next word (`nice -n 19`, `ionice -c 3`)
_LAUNCH_VALUED = frozenset(("-n", "-c", "-s", "-k", "-u", "-o", "-e", "-i",
                            "-p", "--signal", "--kill-after"))
_OPS = ";&|\n()`"
# A post the agent may be making: the shadow reads nothing else.
_POST_AT = _Rx(r"helm\s+chat\s+(?:post|reply|dm)\b")


# ---------------------------------------------------------------------------
# detect: argv -> route ids
# ---------------------------------------------------------------------------

def _pieces(command):
    """The command-position pieces of a shell command: split at every
    unquoted operator, heredoc bodies blanked (actsteer._code) and quoted
    text masked (chat._mask_quoted), so a verb named inside a quoted body or
    a heredoc is data and never a piece of its own. A command inside ANY
    quoted script string (`bash -lc '...'`) is data here too: the accepted
    hole actsteer documents."""
    from . import actsteer, chat
    code = actsteer._code(command.replace("\\\n", ""))
    masked = chat._mask_quoted(code)[0]
    start = 0
    for i, ch in enumerate(masked):
        if ch in _OPS:
            yield code[start:i]
            start = i + 1
    yield code[start:]


def _helm_argv(words):
    """The argv after a `helm` command word, or None when the piece runs
    something else. Leading assignments and launchers (with their options)
    are skipped, so `HELM_CHAT_NAME=x nice -n 19 ./bin/helm task ...` is a
    helm call."""
    i = 0
    while i < len(words):
        w = words[i]
        if _ASSIGN.match(w):
            i += 1
            continue
        if os.path.basename(w) in _LAUNCHERS:
            i += 1
            while i < len(words) and (words[i].startswith("-")
                                      or re.match(r"^\d+[smhd]?$", words[i])
                                      or _ASSIGN.match(words[i])):
                i += 2 if words[i] in _LAUNCH_VALUED else 1
            continue
        break
    if i < len(words) and os.path.basename(words[i]) == "helm":
        return words[i + 1:]
    return None


def _routes_of(argv):
    """[(route id, label)] for one helm argv (the words after `helm`)."""
    out = []
    while argv and argv[0].startswith("-") and argv[0] != "--":
        argv = argv[1:]                     # a global flag before the verb
    if not argv or not _WORD.match(argv[0]):
        return out
    verb = argv[0]
    out.append(("act.helm." + verb, "helm " + verb))
    rest = argv[1:]
    if rest and _WORD.match(rest[0]):
        out.append(("act.helm.%s.%s" % (verb, rest[0]),
                    "helm %s %s" % (verb, rest[0])))
    for i, w in enumerate(rest):
        if w == "--":
            break                           # past it: a title, or the harness's
        m = _FLAG.match(w)
        if not m:
            continue
        flag = m.group(1)
        out.append(("act.helm.%s.%s" % (verb, flag),
                    "helm %s --%s" % (verb, flag)))
        if "--" + flag in VALUE_LEAVES:
            value = m.group(2) if m.group(2) is not None else (
                rest[i + 1] if i + 1 < len(rest) else "")
            if _WORD.match(value or ""):
                out.append(("act.helm.%s.%s" % (verb, value),
                            "helm %s --%s %s" % (verb, flag, value)))
    return out


def detect(command):
    """[(route id, label)] for every helm call a Bash command RUNS, in the
    order they appear, each id once. Never raises."""
    try:
        text = str(command or "")
        if "helm" not in text or not _HELM_AT.search(text):
            return []
        out, seen = [], set()
        for piece in _pieces(text):
            if "helm" not in piece:
                continue
            import shlex
            try:
                words = shlex.split(piece, comments=True)
            except ValueError:
                continue
            argv = _helm_argv(words)
            if argv is None:
                continue
            for rid, label in _routes_of(argv):
                if rid not in seen:
                    seen.add(rid)
                    out.append((rid, label))
        return out
    except Exception:                          # noqa: BLE001 — fail open
        return []


def detect_payload(tool, tool_input):
    """[(route id, label)] for one PreToolUse payload's tool and input."""
    if tool == "Agent":
        return [(SPAWN, "an Agent spawn")]
    if tool == "Bash":
        return detect((tool_input or {}).get("command") or "")
    return []


def verb_route(verb):
    """The ONE route id a `reflex add --verb "<helm verb> [--flag]"` names:
    the most specific id the detector reads off `helm <verb>` (its flag's,
    else its subcommand's, else the verb's), or None."""
    got = detect("helm " + str(verb or "").strip())
    return got[-1][0] if got else None


# ---------------------------------------------------------------------------
# the door table: which route ids any rule binds, in one small file
# ---------------------------------------------------------------------------

#: The NAMED act routes argv-guard detects (the moments.ROUTES rows of family
#: act this module feeds; tests hold the two equal). A detection of one writes
#: a moment row even when no rule is bound to it; any other id is looked up
#: only when some rule binds it.
NAMED = frozenset(("act.helm.launch.home", "act.helm.seat.rehome",
                   "act.helm.seat.spawn", "act.helm.task.priority",
                   "act.helm.dispatch.review", "act.helm.creds", SPAWN))

#: A project's bound set is dropped from the table when no call has rebuilt
#: its index for this long (a project nobody works in binds nothing).
KEY_TTL_S = 86400
_CWD_KEEP = 256


def _table_path():
    return os.path.join(home.global_dir(), ".state", "door-table.json")


def _read_table():
    try:
        with open(_table_path(), encoding="utf-8") as f:
            t = json.load(f)
        return t if isinstance(t, dict) else {}
    except (OSError, ValueError):
        return {}


def _reg_stamp():
    """The registry file's mtime (ns), or None when there is none: the key
    the table's cwd answers are valid under. One stat, no registry import."""
    try:
        return os.stat(home.registry_path()).st_mtime_ns
    except OSError:
        return None


def _cwds(t, reg):
    """The table's {cwd: project} when it was answered under registry stamp
    `reg`, else {}: a registry that moved may claim a cwd it did not."""
    cwds = t.get("cwd")
    return cwds if isinstance(cwds, dict) and t.get("reg") == reg else {}


def table(now=None):
    """(fresh, bound ids, {cwd: project}, registry stamp) — THE COMMON
    PATH'S ONE READ. `bound ids` is every route id some rule binds in any
    project whose index was rebuilt within KEY_TTL_S; `fresh` is whether any
    index was rebuilt within INDEX_TTL_S. The cwd map is empty when the
    registry's mtime is not the one it was answered under, so a worktree
    seen before the registry knew it is resolved again. One small JSON file
    and one stat, no store or registry import: a tool call whose act nobody
    bound pays this and nothing more."""
    now = time.time() if now is None else now
    t = _read_table()
    ids = set()
    for v in (t.get("keys") or {}).values():
        if isinstance(v, dict) and now - float(v.get("built") or 0) \
                < KEY_TTL_S:
            ids.update(v.get("ids") or ())
    fresh = now - float(t.get("built") or 0) < INDEX_TTL_S
    reg = _reg_stamp()
    return fresh, ids, _cwds(t, reg), reg


def _record(t, project):
    """(key, record) of `project`'s index in table `t` (the newest, should a
    race have left two), or (None, None)."""
    key, rec = None, None
    keys = t.get("keys") if isinstance(t.get("keys"), dict) else {}
    for k, v in keys.items():
        if isinstance(v, dict) and v.get("project") == project and (
                rec is None or float(v.get("built") or 0)
                >= float(rec.get("built") or 0)):
            key, rec = k, v
    return key, rec


def _current(rec, now):
    """Is this index record younger than INDEX_TTL_S, with every directory
    it was built from (the store's and the act reflexes') still at its
    recorded mtime? A stat each, no import. Malformed dirs (wrong type or
    bad pairs) is treated as not-current (rebuild)."""
    if not rec or now - float(rec.get("built") or 0) >= INDEX_TTL_S:
        return False
    dirs = rec.get("dirs") or ()
    if not isinstance(dirs, (list, tuple)):
        return False
    try:
        for d, m in dirs:
            try:
                if os.stat(d).st_mtime_ns != m:
                    return False
            except OSError:
                if m is not None:
                    return False
    except (TypeError, ValueError):
        # bad pair shape, e.g. ("d",) or non-iterable item
        return False
    return True


def _dirty(project, now=None):
    """Has this project's store or its act reflexes changed since its index
    was built, or is the index older than INDEX_TTL_S? Read off the table's
    own record of the index's directories (a stat each, no store import); a
    project with no record is dirty."""
    now = time.time() if now is None else now
    return not _current(_record(_read_table(), project)[1], now)


def invalidate():
    """Mark every index stale, so the next helm verb in each project
    rebuilds it (`helm reflex add --signal act`: a belt beside the reflex
    directories' mtimes each record already lists)."""
    t = _read_table()
    for v in (t.get("keys") or {}).values():
        if isinstance(v, dict):
            v["built"] = 0
    t["built"] = 0
    try:
        os.makedirs(os.path.dirname(_table_path()), exist_ok=True)
        pk.atomic_write(_table_path(), json.dumps(t))
    except (OSError, TypeError, ValueError):
        pass


#: `_write_table`'s default: read the registry stamp at the write.
_READ = object()


def _write_table(key=None, ids=None, project=None, cwd=None, dirs=None,
                 now=None, reg=_READ):
    """Record one index's bound ids (and the directories it was built from)
    and/or one cwd's project. Read, modify, replace: two rebuilds racing can
    drop one key's update, which the next rebuild restores (the table is a
    cache of the index, never the record).

    ONE KEY PER PROJECT. A key is the hash of the project's directory list,
    so a list that changes (a registered cwd gains a memory dir) mints a new
    key. The old one stayed for KEY_TTL_S, `_dirty` read it stale, and every
    helm verb in the project's cwds rebuilt through the store for a day
    (MEASURED: 8-15 ms a call against 0.78). A build drops
    the project's other keys, and a key that leaves the table, this way or
    by age, takes its index file with it.

    A cwd's answer is recorded under `reg`, the registry stamp the caller
    read BEFORE it resolved the cwd (a registry written in between then
    reads as moved, and the next call resolves again); a map answered under
    another stamp is dropped first."""
    now = time.time() if now is None else now
    t = _read_table()
    was = t.get("keys") if isinstance(t.get("keys"), dict) else {}
    keys = dict(was)
    if key is not None:
        keys = {k: v for k, v in keys.items()
                if not (isinstance(v, dict) and v.get("project") == project)}
        keys[key] = {"built": now, "ids": sorted(ids or ()),
                     "project": project, "dirs": dirs or []}
        t["built"] = now
    kept = {k: v for k, v in keys.items() if isinstance(v, dict)
            and now - float(v.get("built") or 0) < KEY_TTL_S}
    for k in set(was) - set(kept):
        if _KEY_SHAPE.match(str(k)):        # a name this module minted
            try:
                os.remove(_index_file(k))
            except OSError:
                pass
    t["keys"] = kept
    if cwd:
        reg = _reg_stamp() if reg is _READ else reg
        cwds = _cwds(t, reg)
        cwds[cwd] = project or ""
        t["cwd"] = dict(list(cwds.items())[-_CWD_KEEP:])
        t["reg"] = reg
    try:
        os.makedirs(os.path.dirname(_table_path()), exist_ok=True)
        pk.atomic_write(_table_path(), json.dumps(t))
    except (OSError, TypeError, ValueError):
        pass


def spawn_bound():
    """Does any project bind a rule to act.spawn? The table only: the Agent
    rung pays for this hook on every delegation and stays a key lookup
    (tests/test_chat_argv_guard.py pins that no store, inject or reflex
    module loads on it). A binding is seen once a helm call has rebuilt its
    project's index (a store write, or INDEX_TTL_S). Unreadable -> False."""
    try:
        return SPAWN in table()[1]
    except Exception:                          # noqa: BLE001 — fail open
        return False


def _project(cwd, cwds=None):
    """The registry project of `cwd`: the table's remembered answer when it
    has one (no registry read), else inject's own resolver."""
    if cwds is not None and cwd in cwds:
        return cwds[cwd] or None
    try:
        from .inject._ledger import project_for_cwd
        return project_for_cwd(cwd)
    except Exception:                          # noqa: BLE001 — fail open
        return None


# ---------------------------------------------------------------------------
# the index: route id -> the rules that declare it (the rare, hot path)
# ---------------------------------------------------------------------------

def _source_dirs(project):
    """[[dir, mtime_ns]] for every directory an index of `project` is read
    from: the store's, and the act reflexes' (the project's own listed even
    before it exists, so its creation reads as a change). Only a rebuild
    asks, since listing the store's directories imports the store."""
    from . import reflex, store
    dirs = []
    for root, _scope, d in store.roots(project):
        dirs += [d] if root in ("adopted", "adopted-project") else \
            [os.path.join(d, s) for s in store._SCAN_SUBDIRS]
    out = []
    for d in dirs + reflex._dirs(project, every=True):
        try:
            out.append([d, os.stat(d).st_mtime_ns])
        except OSError:
            out.append([d, None])
    return out


_KEY_SHAPE = _Rx(r"^[0-9a-f]{12}$")


def _key(project, dirs=None):
    tail = [d for d, _m in dirs] if dirs is not None else []
    return hashlib.sha1(("%s|%r" % (project or "", tail)).encode("utf-8")) \
        .hexdigest()[:12]


def _index_file(key):
    return os.path.join(home.global_dir(), ".state",
                        "door-index-%s.json" % key)


def _index_path(project, dirs):
    return _index_file(_key(project, dirs))


def _phrases_path(project):
    return os.path.join(home.global_dir(), ".state",
                        "door-phrases-%s.marshal" % _key(project))


def _first(word):
    """A phrase's bucket: its first word, trimmed of the punctuation a post
    wraps a word in."""
    return word.strip("\"'`()[]{}<>.,;:!?*_")


def _build(project, *, with_phrases=True):
    """(routes, phrases) off the parsed-entry cache, the seat's fenced list.
    routes = {route id: [row]}, each list in store.routed's own order (the
    ONE ranking of a route's rules). phrases = {first word: packed bucket},
    each bucket one string, `phrase US id:conf:recency,... RS phrase ...`:
    a post is checked against the phrases its own words can start, and a
    map of strings loads in a tenth of the time of nested lists (0.7 ms
    against 7 ms for the live store's 15,000 phrase probes, MEASURED).
    with_phrases=False (shadow off) skips the phrase half entirely."""
    from . import store
    from .inject import _entries
    entries = _entries.load_entries(project)
    cand = store._jit_candidates(entries)
    ids = sorted({r for e in cand for r in store.routes(e)
                  if r.startswith("act.")})
    routes = {rid: [{"id": str(e.get("id") or ""),
                     "line": _entries._entry_line_full(e)[:_ROW_KEEP]}
                    for e in store.routed(entries, [rid])]
              for rid in ids}
    if not with_phrases:
        return routes, {}
    probes, weight = {}, {}
    for e in cand:
        eid = str(e.get("id") or "")
        for p, _generic in store._own_probes(e):
            if eid and " " in p and "\x1e" not in p and "\x1f" not in p:
                probes.setdefault(p, []).append(eid)
                weight[eid] = "%s:%g:%g" % (
                    eid, float(e.get("confidence") or 0),
                    float(store._recency(e) or 0))
    buckets = {}
    for p, pids in probes.items():
        buckets.setdefault(_first(p.split(" ", 1)[0]), []).append(
            p + "\x1f" + ",".join(weight[i] for i in pids))
    return routes, {k: "\x1e".join(v) for k, v in buckets.items()}


# The phrase file's build stamp, under a key no first word can be.
_BUILT = "\x00built"


def _acts(project):
    """[{id, steer, routes}] for every routed act reflex this project's
    seats receive, in reflex.load_all's order: the index's reflex half."""
    from . import reflex
    return [{"id": e["id"], "steer": e["steer"],
             "routes": sorted(reflex.act_routes(e))}
            for e in reflex.act_steers(None, project)]


def _rebuild(project, now):
    """(index, phrase map) for `project`, read fresh from the store and the
    act reflexes and written to the index file, the phrase file and the
    table's record. The one door path that imports the store. The
    directories are stat'ed BEFORE the read, so a write racing the build
    leaves a record that already reads stale.
    Phrase half (and its write) only when HELM_DOOR_SHADOW=1; with it off,
    a rebuild writes only the routes index (no per-verb phrase cost)."""
    import marshal
    import os
    dirs = _source_dirs(project)
    key = _key(project, dirs)
    shadow_on = os.environ.get("HELM_DOOR_SHADOW") == "1"
    routes, phr = _build(project, with_phrases=shadow_on)
    index = {"v": INDEX_V, "dirs": dirs, "built": now, "routes": routes,
             "acts": _acts(project)}
    phr = dict(phr, **{_BUILT: now})
    if shadow_on:
        # only shadow path pays the phrase marshal write
        try:
            pk.atomic_write(_phrases_path(project), marshal.dumps(phr))
        except (OSError, TypeError, ValueError):
            pass
    try:
        pk.atomic_write(_index_file(key), json.dumps(index,
                                                     ensure_ascii=False))
    except (OSError, TypeError, ValueError):
        pass
    try:
        ids = set(routes)
        for e in index["acts"]:
            ids.update(e["routes"])
        _write_table(key=key, ids=ids, project=project, dirs=dirs, now=now)
    except Exception:                          # noqa: BLE001 — fail open
        pass
    return index, phr


def _index(project, now=None):
    """{"routes": {route id: [row]}, "acts": [act reflex]} for `project`.
    FRESH OFF THE TABLE, NO STORE IMPORT: the table's record for the project
    names its index file, which is read while the record is younger than
    INDEX_TTL_S and every directory it lists keeps its mtime (a stat each).
    Anything else rebuilds. Cost is live-sized: with HELM_DOOR_SHADOW off
    (default) a rebuild/index path writes only the routes index; the phrase
    marshal is gated to the shadow path only."""
    now = time.time() if now is None else now
    key, rec = _record(_read_table(), project)
    if _current(rec, now):
        try:
            with open(_index_file(key), encoding="utf-8") as f:
                head = json.load(f)
            if head.get("v") == INDEX_V \
                    and head.get("dirs") == rec.get("dirs") \
                    and now - float(head.get("built") or 0) < INDEX_TTL_S:
                return head
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return _rebuild(project, now)[0]


def _phrases(project, now=None):
    """The phrase map for the shadow, off its own file while it is younger
    than INDEX_TTL_S (a shadow a few minutes behind the store logs the same
    would-fire), else through a rebuild. No store import on the fresh path."""
    import marshal
    now = time.time() if now is None else now
    try:
        with open(_phrases_path(project), "rb") as f:
            body = marshal.load(f)
        if now - float(body.get(_BUILT) or 0) < INDEX_TTL_S:
            return body
    except (OSError, ValueError, TypeError, AttributeError, EOFError):
        pass
    return _rebuild(project, now)[1]


def bound(detected, project=None, index=None):
    """[(route id, label, kind, id, text)] for every rule that declares one
    of the `detected` [(route id, label)] pairs: store entries (kind
    "store", in store.routed's order), then act reflexes (kind "reflex").
    Each rule once, under the first route that reached it. `index` is
    `_index`'s shape; the default is the project's own."""
    out, seen = [], set()
    if not detected:
        return out
    index = _index(project) if index is None else index
    routes = index.get("routes") or {}
    for rid, label in detected:
        for row in routes.get(rid) or ():
            if ("store", row["id"]) not in seen:
                seen.add(("store", row["id"]))
                out.append((rid, label, "store", row["id"], row["line"]))
    for rid, label in detected:
        for e in index.get("acts") or ():
            if rid in (e.get("routes") or ()) \
                    and ("reflex", e["id"]) not in seen:
                seen.add(("reflex", e["id"]))
                out.append((rid, label, "reflex", e["id"], e["steer"]))
    return out


# ---------------------------------------------------------------------------
# D: the agent's own post, matched in SHADOW (only when HELM_DOOR_SHADOW=1)
# ---------------------------------------------------------------------------

def _words(low):
    """The bucket keys a lowercased post can start a phrase with."""
    out = set()
    for w in low.split():
        out.add(w)
        t = _first(w)
        out.add(t)
        out.update(x for x in t.split("/") if x)
    return out


def phrase_re(p):
    """The store's keyword-match law (store.resolve._probe_re) for a PHRASE
    probe: word-boundary on both edges, exact (a probe holding a space is
    never alphabetic, so it takes no inflection). Spelled here so a post's
    match loads no store module; tests hold the two patterns equal."""
    return r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])"


def match_post(body, phrases):
    """(entry id, [phrase probes]) — the ONE rule a post body's PHRASES
    reach, scored as the JIT lane scores (confidence x sum of 1/df over the
    matched phrase probes, df = how many entries carry the phrase), or
    (None, []). `phrases` is _build's packed bucket map."""
    low = str(body or "").lower()
    if not low.strip():
        return None, []
    hits, weight, seen = {}, {}, set()
    for w in _words(low):
        packed = phrases.get(w)
        if not packed or not isinstance(packed, str):
            continue
        for item in packed.split("\x1e"):
            p, _sep, owners = item.partition("\x1f")
            if p in seen or p not in low or not re.search(phrase_re(p), low):
                continue
            seen.add(p)
            owners = owners.split(",")
            for owner in owners:
                eid, conf, rec = (owner.split(":") + ["0", "0"])[:3]
                weight[eid] = (float(conf or 0), float(rec or 0))
                hits.setdefault(eid, []).append((p, len(owners)))
    if not hits:
        return None, []

    def score(eid):
        conf, rec = weight.get(eid) or (0.0, 0.0)
        return (conf * sum(1.0 / max(df, 1) for _p, df in hits[eid]), rec)
    best = max(sorted(hits), key=score)
    return best, sorted(p for p, _df in hits[best])


def _shadow(payload, cmd, project):
    """Log, never print, the one rule the agent's own post would reach."""
    from . import actsteer, chat, momentledger as ml
    code = actsteer._code(cmd.replace("\\\n", ""))
    bodies = actsteer._chat_bodies(cmd, code)
    if not bodies:
        return
    phrases = _phrases(project)
    session, agent = payload.get("session_id"), payload.get("agent_id")
    rows = []
    for body in bodies:
        eid, probes = match_post(body, phrases)
        row = {"v": 1, "ts": pk.now_ts(), "hook": "PreToolUse",
               "route": SHADOW_ROUTE, "ids": [eid] if eid else [],
               "probes": probes, "bytes": 0}
        if eid is None:
            row["outcome"] = ml.SILENT
        elif chat.steer_unfired(session, "door.shadow." + pk.slug(eid), agent):
            row["outcome"] = ml.SHADOW
        else:
            row["outcome"] = ml.IN_CONTEXT
        if session:
            row["session"] = session
        if project:
            row["project"] = project
        rows.append(row)
    ml.record(rows)


# ---------------------------------------------------------------------------
# C: the priority door, printed by `helm task add|update` itself
# ---------------------------------------------------------------------------

#: Two owner-asked titles sharing this share of their words are one ask made
#: again. INFERRED: looser than tasks.DUP_OVERLAP (0.8, a duplicate that
#: refuses) because a repeat ask is restated, not retyped.
REPEAT_OVERLAP = 0.5

PRIORITY_RULE = ("friction-tax: an owner ask is ranked by its tax and "
                 "payback, never by habit. TAX = steps x times a day; "
                 "PAYBACK DAYS = build cost / tax; a cut that pays back "
                 "within %s days goes ahead of features.")


def owner_repeats(row, rows):
    """The ids of OTHER owner-asked rows (any status, same project or none)
    whose titles restate this one's, sorted."""
    from . import tasks
    want = tasks.title_tokens(row.get("title"))
    if not want:
        return []
    proj = tasks.project_of_row(row)
    out = []
    for r in (rows.values() if hasattr(rows, "values") else rows):
        if not isinstance(r, dict) or r.get("id") == row.get("id") \
                or r.get("origin") != "owner":
            continue
        if proj and tasks.project_of_row(r) not in (proj, None):
            continue
        other = tasks.title_tokens(r.get("title"))
        if other and len(want & other) / float(len(want | other)) \
                >= REPEAT_OVERLAP:
            out.append(str(r.get("id")))
    return sorted(out, key=lambda t: (len(t), t))


def priority_note(row, rows=None):
    """The lines `helm task add|update --priority` prints on an owner-asked
    row: the rule, the owner's repeat count with the row's tax and payback,
    and the payback question. It changes nothing and refuses nothing."""
    from . import tasks
    if rows is None:
        rows = tasks.rows()
    again = owner_repeats(row, rows)
    n = 1 + len(again)
    tax, cost, payback = tasks.tax_of(row)
    if tax is None:
        money = "tax UNKNOWN, payback UNKNOWN"
    elif payback is None:
        money = "tax %s steps/day, payback UNKNOWN" % tasks._steps(tax)
    else:
        money = "tax %s steps/day, payback %s" % (tasks._steps(tax),
                                                   tasks._days(payback))
    return [
        PRIORITY_RULE % ("%g" % tasks.PAYBACK_AHEAD_DAYS),
        "%s: the owner has asked %d time%s%s; %s; ranked %s." % (
            row.get("id"), n, "" if n == 1 else "s",
            (" (also %s)" % ", ".join(again[:3])) if again else "", money,
            row.get("priority") or "UNRANKED"),
        "Payback question: what does this cut save a day, and what does it "
        "cost to build? A repeat ask with no lane reads P0. Record it: "
        "`helm task update %s --tax N --tax-cost N`." % row.get("id"),
    ]


# ---------------------------------------------------------------------------
# E: the data move, applied by `helm sync` (cli.cmd_sync), a WRITE: the verb
#    takes no arguments and has no dry run
# ---------------------------------------------------------------------------

#: THE STORE HALF OF THE DOOR. Each row gives an entry the route cells of the
#: moment it answers (`add`) and, where it had them, the common cells those
#: routes replace (`drop`). The ADD half re-asserts: a route cell is the
#: code's promise that this act reaches this rule, and retiring it means
#: deleting the row here. The DROP half is actsteer.MOVED's law: it applies
#: only while the entry still carries EVERY drop cell, so an operator's own
#: edit is never overwritten. Census fractions MEASURED on the live prompt
#: census the day this landed (share of recent turns): function 1.9%, manual
#: 1.1%, rehome 0.8%, feature 0.6%, repeated 0.5%, improvement 0.2%; the
#: dropped phrases are stems generated off longer phrases the entry keeps,
#: plus the two generic ones (every time, every land). claude-cred-1yr-auth
#: carries no common cell: its phrases stay, and the agent's-post shadow
#: reads it through "helm's copy". The Max-account reserve rule (the owner's
#: account-order prior, launch.home and seat.rehome) is bound by a live store
#: write instead of a row here: its id is a private name, and no private
#: name enters the tree.
ROUTED = (
    {"id": "fleet-autoswitch-at-the-wall",
     "add": ("route:act.helm.seat.rehome",),
     "drop": ("rehome", "seat rehome")},
    {"id": "friction-tax",
     "add": ("route:act.helm.task.priority",),
     "drop": ("function", "manual", "feature", "repeated", "improvement",
              "every time", "every land", "agent function",
              "function improvement", "investment feature", "prioritize ax")},
    {"id": "claude-cred-1yr-auth",
     "add": ("route:act.helm.creds",)},
)


def apply_routes(entries=None, apply=None):
    """Apply ROUTED to the live store -> [(id, state)], state one of
    'applied', 'done', 'held' (the write was refused), 'partial' (routes
    added; the drops held because the entry was edited since) and 'absent'.
    `entries` and `apply(id, add, drop)` are seams for tests; the defaults
    are the store's own load and retag. A no-op `apply` is a test seam, not
    a dry run: `helm sync` passes neither, so each non-`done` row it reports
    is a `store.retag` of the live entry (the entry file rewritten, one
    `store.retag` event). Never raises."""
    out = []
    try:
        if entries is None or apply is None:
            from . import store
            entries = store.load_all() if entries is None else entries
            apply = apply or (lambda eid, add, drop: store.retag(
                eid, pk.now_ts(), add=",".join(add) or None,
                remove=",".join(drop) or None))
        by_id = {pk.slug(str(e.get("id") or "")): e for e in entries}
        for row in ROUTED:
            e = by_id.get(pk.slug(row["id"]))
            if e is None:
                out.append((row["id"], "absent"))
                continue
            have = {c.strip().lower()
                    for c in str(e.get("keywords") or "").split(",")}
            add = [c for c in row.get("add") or () if c.lower() not in have]
            drop = {c.lower() for c in row.get("drop") or ()}
            live = drop & have
            cut = sorted(drop) if drop and drop <= have else []
            if not add and not cut:
                out.append((row["id"], "held" if live else "done"))
                continue
            _e, err = apply(row["id"], add, cut)
            if err:
                out.append((row["id"], "held"))
            else:
                out.append((row["id"], "partial" if live and not cut
                            else "applied"))
    except Exception:                          # noqa: BLE001 — fail open
        return out
    return out


def bound_lookup(project=None):
    """route id -> [rule ids] over a FRESH read of the store and the act
    reflexes: the moment report's `bound` column, what each door would say
    today."""
    from . import reflex
    routes, _phr = _build(project)
    acts = reflex.act_steers(None, project)

    def look(rid):
        return [row["id"] for row in routes.get(rid) or ()] + [
            "reflex:" + e["id"] for e in acts if rid in reflex.act_routes(e)]
    return look


# ---------------------------------------------------------------------------
# the resolve-test: `helm store resolve --act "<command>"`
# ---------------------------------------------------------------------------

def explain(command, project=None):
    """Lines for `helm store resolve --act`: the route ids a command stands
    in and the rule each would say, read FRESH from the store (the index is
    bypassed, so a cell just written is seen)."""
    detected = detect(command)
    if not detected:
        return ["helm store resolve --act: no helm verb runs in that command "
                "(a verb inside quotes or a heredoc is data)"]
    routes, _phr = _build(project)
    rules = bound(detected, project,
                  index={"routes": routes, "acts": _acts(project)})
    out = ["routes: " + ", ".join(rid for rid, _l in detected)]
    for rid, label, kind, eid, text in rules:
        out.append("  %s -> %s %s" % (rid, kind, eid))
        out.append("    " + line_for(label, kind, text))
    if not rules:
        out.append("  no rule declares these routes: add one with `helm store "
                   "keywords <id> --add route:<route>` or `helm reflex add "
                   "<id> | <steer> --signal act --verb \"<verb> [--flag]\"`")
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def fit(text, cap=LINE_CAP):
    """`text` on one line under `cap` UTF-8 bytes, cut at a word boundary
    and marked with an ellipsis when cut."""
    text = " ".join(str(text or "").split())
    if len(text.encode("utf-8")) <= cap:
        return text
    room = cap - len("…".encode("utf-8"))
    cut = text.encode("utf-8")[:room].decode("utf-8", "ignore")
    sp = cut.rfind(" ")
    if sp > len(cut) // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:") + "…"


def line_for(label, kind, text):
    head = PREFIX + "you just ran %s. " % label
    return fit(head + (("REFLEX: " + text) if kind == "reflex" else text))


def steer_id(kind, eid):
    return "door.%s.%s" % (kind, pk.slug(eid))


# ---------------------------------------------------------------------------
# argv-guard's two calls: what one payload wants said, then what was said
# ---------------------------------------------------------------------------

def wanting(payload):
    """([(steer-id, line)], pending) for one PreToolUse payload. `pending`
    is what `settle` needs to record the moment once the envelope is
    admitted. With HELM_DOOR_SHADOW=1, a chat post's body is matched in
    shadow here and logged at once; it adds nothing to the lines. Never
    raises.

    THE COMMON PATH COSTS A REGEX. A Bash command with no helm verb at a
    command position, and no `helm chat post|reply|dm`, returns at the
    precompiled gate. A helm verb reads the door table (one small file and
    one stat) and returns when no rule binds any route it stands in and it
    is no named act. A bound rule reads its project's index file, with no
    store import while the index is fresh; the store is read only to
    rebuild a stale index (one rebuild refreshes the table for every seat);
    the shadow reads its phrase file only on a post, and only when it is
    switched on."""
    try:
        tool = payload.get("tool_name")
        tin = payload.get("tool_input") or {}
        cmd, post = "", False
        if tool == "Agent":
            if not spawn_bound():
                return [], None             # nothing bound: a key lookup
            detected = detect_payload(tool, tin)
        elif tool == "Bash":
            cmd = tin.get("command") or ""
            # THE SHADOW IS OFF unless HELM_DOOR_SHADOW=1. Switched on, each
            # post pays a phrase file read (6-11 ms), a ledger row and a
            # latch, and a full rebuild when the phrase file is stale; the
            # report's would-fire rate is what that buys.
            post = os.environ.get("HELM_DOOR_SHADOW") == "1" \
                and "chat" in cmd and bool(_POST_AT.search(cmd))
            detected = detect(cmd)
        else:
            return [], None
        if not detected and not post:
            return [], None
        fresh, bound_ids, cwds, reg = table()
        ids = {rid for rid, _l in detected}
        named = [(rid, label) for rid, label in detected if rid in NAMED]
        cwd = str(payload.get("cwd") or "")
        known = cwd in cwds
        # HOT: a rule binds a route this call stands in; or this project's
        # store changed since its index was built (a helm verb in a known
        # cwd rebuilds it, so a route cell written a minute ago is read);
        # or a named act, or a post, meets a stale or missing table. A helm
        # verb in a cwd the table does not know stays cheap until one of
        # those rebuilds records it.
        hot = bool(ids & bound_ids)
        if not hot and detected and (known or named):
            hot = _dirty(cwds.get(cwd) or None) if known else not fresh
        quiet = ({"detected": named, "rules": [], "lines": [],
                  "project": None} if named else None)
        if not hot and not post:
            return [], quiet
        project = _project(cwd, cwds)
        if cwd and not known:
            _write_table(cwd=cwd, project=project, reg=reg)
        if post:
            _shadow(payload, cmd, project)
        if not hot:
            return [], quiet
        # THE CAP DEFERS, IT NEVER DROPS: a rule whose latch this context
        # already spent is passed over BEFORE the cut, so a third rule bound
        # to one verb is said on its next run instead of never.
        from . import chat
        session, agent = payload.get("session_id"), payload.get("agent_id")
        every = bound(detected, project)
        rules = [r for r in every
                 if not os.path.exists(chat._steer_latch(
                     session, steer_id(r[2], r[3]), agent) or "")][:MAX_LINES]
        lines = [(steer_id(kind, eid), line_for(label, kind, text))
                 for _rid, label, kind, eid, text in rules]
        return lines, {"detected": detected, "rules": rules, "lines": lines,
                       "project": project,
                       "all": [(r[0], steer_id(r[2], r[3]), r[3])
                               for r in every]}
    except Exception:                          # noqa: BLE001 — fail open
        return [], None


def settle(payload, pending, said):
    """Record one moment row per NAMED act route the payload stood in, with
    what reached the seat: `delivered` (a bound rule's line rode this
    envelope), `in-context` (its latch was already spent), `deferred` (the
    envelope was full) or `silent` (nothing is bound to it). Ids, kinds and
    byte counts only. Never raises."""
    try:
        if not pending:
            return
        from . import chat, momentledger as ml
        said = set(said or ())
        session, agent = payload.get("session_id"), payload.get("agent_id")
        rows = []
        for rid, _label in pending["detected"]:
            if rid not in NAMED:
                continue
            here = [(sid, eid) for r, sid, eid in pending.get("all") or ()
                    if r == rid]
            spoke = [line for (_sid, line), rule
                     in zip(pending["lines"], pending["rules"])
                     if rule[0] == rid and line in said]
            if not here:
                outcome = ml.SILENT
            elif spoke:
                outcome = ml.DELIVERED
            elif any(os.path.exists(chat._steer_latch(session, sid, agent)
                                    or "") for sid, _e in here):
                outcome = ml.IN_CONTEXT
            else:
                outcome = ml.DEFERRED
            row = {"v": 1, "ts": pk.now_ts(), "hook": "PreToolUse",
                   "route": rid, "outcome": outcome,
                   "ids": [eid for _sid, eid in here],
                   "bytes": sum(len(line.encode("utf-8")) for line in spoke)}
            if session:
                row["session"] = session
            if pending.get("project"):
                row["project"] = pending["project"]
            if agent:
                row["agent"] = True
            rows.append(row)
        ml.record(rows)
    except Exception:                          # noqa: BLE001 — fail open
        return
