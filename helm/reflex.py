#!/usr/bin/env python3
"""Reflexes — (signal) -> (steer), delivered by inject-capable harness hooks.

A rule is what you believe; a reflex is what FIRES. Rules decay as context
scrolls; a reflex is re-delivered fresh on its signal, unconditionally — only
the heeding is probabilistic. helm re-inherits the reflex layer harness-broad:
the store lives here, delivery is one `helm inject` call any harness hook can
make (no per-harness lane gating — reflexes fire wherever the operator works).

Signal kinds (cheap, observable — a false-firing reflex is theater):
  every-turn    constant steer, budget-capped (keep the set TINY)
  prompt        regex against the current prompt text
  marker-file   fires while a filesystem path exists (AFK flags, mode markers)
  counter       recorder-fed: a record.py counter crossing a threshold — the
  stalled       named aliases below map to a counter + a field-tested default:
  thrash          stalled=passive-streak (6)  thrash=loop-streak (3)
  drift           drift=dirty-streak (8)      stuck=stuck-streak (3, escalating)
  stuck         generic `counter` reads any counter via the `counter:` field.

Counter/latch v2 (the record.py counters upgrade steering static->situational):
  * threshold   fire when counters[name] >= threshold (specificity law: a
                counter reflex with no counter or threshold<=0 is skipped, not
                wallpaper).
  * latch       fire ONCE per episode: the latch persists in the SESSION state
                dir and clears when the counter falls back under threshold (the
                inverse event — a forward op, a commit, a recovery). Without
                latch a counter is level-triggered (fires every turn over the
                line) — the seeded pack latches, per the salience law.
  * escalate:N  a latched counter RE-fires as the streak worsens (val crosses
                each further N) — stuck escalates, the rest fire once.
  * pattern     optional extra gate, matched against the PROMPT TEXT only
                (fixed-text law — a counter never reads tool/user output).
  * window:S    optional freshness gate on the counter's own ts (seconds).

Fail-open: no session and no planted counters -> every counter signal is ABSENT
(harnesses that don't thread a session_id degrade to v1 stateless), never a
raise. Latch state is ONE pruned-on-read JSON per session under the recorder's
session dir (reflex-state/<sid>/latch.json) — NOT the ancestor's per-term file
spray; gc TTL-sweeps that dir, so it never ships cross-machine.

Laws: default-silent; fire only on a live signal; steers are terse FACTS, never
commands; fixed text only (no interpolation of tool/user output).

Store: reflex-<slug>.md in _global/reflexes/ (or a project's premises sibling
dir reflexes/), frontmatter per pk.parse_simple_frontmatter. Every steer,
threshold, and cooldown is an editable, retirable store entry — the authoring
seam is data, not hardcoded Python.
"""
import os
import re

from . import home, pk

_DEFAULTS = {
    "id": "", "steer": "", "signal": "prompt", "pattern": "", "marker": "",
    "counter": "", "threshold": "", "latch": "", "escalate": "", "window": "",
    "status": "live", "stated_ts": "", "notes": "", "source": "",
}

EVERY_TURN_BUDGET = 3  # max constant steers per injection — habituation guard

# counter-family signal aliases -> (counter name, default threshold, default
# escalate step). `counter` is the generic form: the name comes from the entry's
# `counter:` field. The counters themselves are record.py's session state.
COUNTER_SIGNALS = {
    "counter": (None, 0, 0),
    "stalled": ("passive-streak", 6, 0),
    "thrash":  ("loop-streak", 3, 0),
    "drift":   ("dirty-streak", 8, 0),
    "stuck":   ("stuck-streak", 3, 3),
}

# The shipped starter pack — installed by seed_defaults() at scaffold time so
# the lane is live out of the box. Each lands as a normal store entry marked
# source: helm-default (shipped vs authored stays legible); operators edit or
# retire them like any authored reflex and seeding never touches an existing id
# again. Nothing here fires on a generic turn (specificity law): prompt signals
# demand their phrase, the marker demands the owner's chat post, and the counter
# reflexes demand a record.py streak past its field-tested line. The
# comms-fabric-entangled ancestors (meld-salience, bb-first) are dropped.
DEFAULT_PACK = (
    {"id": "correction-language",
     "pattern": r"\b(no,? actually|that'?s (wrong|not what)|i (said|told you)|stop doing)\b",
     "steer": "Correction detected: capture it durably (/premise or helm store add) — "
              "a correction only complied-with is lost by the next compaction."},
    {"id": "punt-tell",
     # punt-SHAPED phrasing only — bare "later"/"todo" ride ordinary narration
     # ("3 days later...", "the todo list") and would fire wallpaper
     "pattern": r"\b(?:(?:wire|do|fix|handle|finish|add|ship|test|clean|deal)\b"
                r"[^.!?\n]{0,24}\blater\b|maybe later\b|for now\b|next session\b|"
                r"punt(?:s|ed|ing)?\b|park (?:it|this|that)\b|"
                r"(?:leave|add) (?:a |the )?todo\b|todo:)",
     "steer": "Deferral language detected: identified fixes land in-pass; anything "
              "truly deferred is loudly flagged to the owner, never silently parked."},
    {"id": "compaction-continuity",
     "pattern": r"\b(summarized|compacted|continu(ed|ing) from)\b",
     "steer": "Compaction marker detected: re-read .remember/now and the live task "
              "state before acting — settled decisions are not re-derived."},
    {"id": "owner-chat-unread",
     # the chat notify loop: the owner's web post drops the marker; any
     # `helm chat read` that consumes past it clears it (chat.consume)
     "signal": "marker-file",
     "steer": "Owner posted in helm chat — read it (`helm chat read`) and reply "
              "(`helm chat post ...`) before continuing."},
    # ── counter/latch pack: field-tested 6/8/3 defaults, latched (edge-fire) ──
    {"id": "stalled-driver",
     "signal": "stalled", "threshold": "6", "latch": "true",
     "steer": "6 turns with no forward op (edit/commit/spawn) — you may be circling; "
              "make a move or name what is blocking."},
    {"id": "loop-thrash",
     "signal": "thrash", "threshold": "3", "latch": "true",
     "steer": "Same command re-run with no change — the loop will not break itself; "
              "read the actual error or change approach, do not re-run."},
    {"id": "uncommitted-drift",
     "signal": "drift", "threshold": "8", "latch": "true",
     "steer": "Many dirtying ops with no commit — checkpoint the green slice; "
              "uncommitted work is work you can lose."},
    {"id": "stuck-commonsense",
     "signal": "stuck", "threshold": "3", "escalate": "3", "latch": "true",
     "steer": "Repeated infra/auth failures — stop retrying the same call; check "
              "creds/network/quota (helm creds) or surface the blocker."},
)


def seed_defaults():
    """Install DEFAULT_PACK into _global/reflexes, marked source: helm-default.
    Idempotent by id: an existing file — operator-edited, re-worded, or retired
    — is never overwritten; a re-seed of a present id is a no-op. Returns the
    paths actually written (empty on re-seed). The chat marker path resolves at
    SEED time (env-respecting — HELM_CHAT_DIR + HELM_CHAT_ROOM, so a homed
    seat's reflex tracks its own team room), never at import time."""
    out = []
    for d in DEFAULT_PACK:
        if os.path.exists(reflex_path(d["id"])):
            continue
        e = dict(d, source="helm-default")
        e.setdefault("signal", "prompt")
        if e["signal"] == "marker-file" and not e.get("marker"):
            from . import chat
            e["marker"] = chat.marker_path(home.env("CHAT_ROOM") or "main")
        out.append(write(e))
    return out


def _dirs(project=None):
    out = []
    if project:
        d = os.path.join(home.project_dir(project), "reflexes")
        if os.path.isdir(d):
            out.append(d)
    out.append(os.path.join(home.global_dir(), "reflexes"))
    return out


def reflex_path(rid, project=None):
    base = _dirs(project)[0] if project else os.path.join(home.global_dir(), "reflexes")
    return os.path.join(base, "reflex-" + pk.slug(rid) + ".md")


def load_all(project=None, include_retired=False):
    out = {}
    for d in reversed(_dirs(project)):  # global first; project shadows by id
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if not (n.startswith("reflex-") and n.endswith(".md")):
                continue
            e = pk.parse_simple_frontmatter(os.path.join(d, n), _DEFAULTS)
            if not (e and e["id"] and e["steer"]):
                continue
            if not include_retired and e["status"] != "live":
                continue
            e["path"] = os.path.join(d, n)
            out[pk.slug(e["id"])] = e
    return list(out.values())


def write(e, project=None):
    path = reflex_path(e["id"], project)
    body = [
        "---",
        "name: reflex-" + pk.slug(e["id"]),
        'description: "reflex: ' + (e["id"] + " - " + e["steer"])[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: reflex",
        "  id: " + e["id"],
        "  steer: " + re.sub(r"\s+", " ", e["steer"]),
        "  signal: " + (e.get("signal") or "prompt"),
    ]
    for opt in ("pattern", "marker", "counter", "threshold", "latch",
                "escalate", "window", "notes", "source"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    body += ["  status: " + (e.get("status") or "live"),
             "  stated_ts: " + str(e.get("stated_ts") or pk.now_ts()),
             "---", "",
             "REFLEX: " + e["steer"], ""]
    pk.atomic_write(path, "\n".join(body))
    return path


# ── counter/latch mechanics ────────────────────────────────────────────────

def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def counter_spec(e):
    """(counter_name, threshold, escalate) for a counter-family signal, or None
    when the entry is not a counter signal OR is under-specified — no counter
    name, or threshold<=0 (specificity law: an unarmed counter reflex is skipped
    silently, never fires as wallpaper)."""
    sig = e.get("signal") or "prompt"
    if sig not in COUNTER_SIGNALS:
        return None
    cn, dth, desc = COUNTER_SIGNALS[sig]
    if sig == "counter":
        cn = (e.get("counter") or "").strip()
    th = _int(e.get("threshold"), dth)
    esc = _int(e.get("escalate"), desc)
    if not cn or th <= 0:
        return None
    return cn, th, esc


def _fresh(ts, window_s):
    """The counter ts is within `window_s` seconds. Fail-open: an unparseable or
    absent ts cannot be judged stale, so it reads fresh."""
    if not ts:
        return True
    import calendar
    import time
    try:
        t = calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return True
    return (time.time() - t) <= window_s


def _latch_path(session):
    from . import record
    return os.path.join(record.session_dir(session), "latch.json")


def _load_latch(session):
    if not session:
        return {}
    d = pk.read_json(_latch_path(session), {})
    return d if isinstance(d, dict) else {}


def _save_latch(session, d):
    pk.write_json(_latch_path(session), d)


def _read_counters(session, counters):
    """Planted counters win (hermetic tests); else the session's live record.py
    counters. Fail-open: any trouble -> None (every counter signal absent)."""
    if counters is not None:
        return counters if isinstance(counters, dict) else None
    if not session:
        return None
    try:
        from . import record
        c = record.counters(session)
        return c if isinstance(c, dict) else None
    except Exception:
        return None


def _counter_fires(e, spec, counters, low, latch, new_latch):
    """Decide one counter reflex against the counters, updating new_latch for
    the pruned-on-read persist. -> True to fire. Under threshold / stale-window
    omits the id from new_latch (the inverse event clears the latch)."""
    cn, th, esc = spec
    pat = e.get("pattern")
    if pat:  # optional prompt-text gate (fixed-text law: prompt only)
        try:
            if not re.search(pat, low, re.IGNORECASE):
                return False
        except re.error:
            return False
    win = _int(e.get("window"), 0)
    if win and not _fresh(counters.get("ts"), win):
        return False  # stale counter: inverse event — drop the latch, silent
    val = _int(counters.get(cn), 0)
    if val < th:
        return False  # under threshold: inverse event — drop the latch, silent
    rid = pk.slug(e["id"])
    if not _truthy(e.get("latch")):
        return True  # level-triggered: fires every turn over the line
    last = _int(latch.get(rid), -1)
    if last < 0 or (esc and val >= last + esc):
        new_latch[rid] = val
        return True
    new_latch[rid] = last  # already latched at this level — stay quiet, stay set
    return False


def fire(text, project=None, session=None, counters=None, persist=True):
    """The reflexes whose signal is LIVE for this turn. Salience law: [].

    Counter-family signals read the session's record.py counters (or planted
    `counters` for hermetic tests) and fire on a threshold crossing; `latch`
    entries fire once per episode via the session's latch.json and re-arm when
    the counter clears. Fail-open: no session and no planted counters -> every
    counter signal absent (v1 degrade), never a raise. persist=False reads the
    latch read-only (smoke/explain must never count as a turn)."""
    out = []
    constant = 0
    low = text or ""
    counters = _read_counters(session, counters)
    latch = _load_latch(session) if counters is not None else {}
    new_latch = {}
    for e in load_all(project):
        sig = e.get("signal") or "prompt"
        if sig == "every-turn":
            if constant < EVERY_TURN_BUDGET:
                out.append(e)
                constant += 1
        elif sig == "prompt" and e.get("pattern"):
            try:
                if re.search(e["pattern"], low, re.IGNORECASE):
                    out.append(e)
            except re.error:
                continue
        elif sig == "marker-file" and e.get("marker"):
            if os.path.exists(os.path.expanduser(e["marker"])):
                out.append(e)
        elif counters is not None:
            spec = counter_spec(e)
            if spec and _counter_fires(e, spec, counters, low, latch, new_latch):
                out.append(e)
    # pruned-on-read: only ids still latched survive; cleared/retired drop out
    if persist and session and counters is not None and new_latch != latch:
        try:
            _save_latch(session, new_latch)
        except Exception:
            pass
    return out


def _sig_desc(e):
    """The signal column for `reflex list` — kind + its live parameters."""
    sig = e.get("signal") or "prompt"
    if counter_spec(e):
        cn, th, esc = counter_spec(e)
        s = "%s>=%d" % (cn, th)
        if esc:
            s += " esc%d" % esc
        if _truthy(e.get("latch")):
            s += " latch"
        return sig + ":" + s
    tail = (":" + e["pattern"]) if e.get("pattern") else \
           (":" + e["marker"]) if e.get("marker") else ""
    return sig + tail


def cmd_reflex(args):
    """reflex list|add|retire|smoke — manage the (signal -> steer) set."""
    if not args or args[0] == "list":
        es = load_all(include_retired="--all" in args)
        if not es:
            print("helm reflexes: none. Add one: helm reflex add <id> | <steer> "
                  "[--signal prompt --pattern <re> | --signal every-turn | "
                  "--signal marker-file --marker <path> | "
                  "--signal stuck --threshold 3 --latch]")
            return 0
        print("helm reflexes (%d):" % len(es))
        for e in es:
            mark = "" if e["status"] == "live" else " [" + e["status"] + "]"
            if e.get("source") == "helm-default":
                mark += " [default]"
            print("  - %s%s (%s): %s" % (e["id"], mark, _sig_desc(e), e["steer"][:90]))
        return 0
    if args[0] == "add":
        return _cmd_add(args[1:])
    if args[0] == "retire":
        return _cmd_retire(args[1:])
    if args[0] == "smoke":
        return _cmd_smoke(args[1:])
    import sys
    print("helm reflex: unknown subcommand '%s'" % args[0], file=sys.stderr)
    return 2


_ADD_FLAGS = ("--signal", "--pattern", "--marker", "--counter", "--threshold",
              "--escalate", "--window", "--project")


def _cmd_add(args):
    import sys
    flags = {}
    kept = []
    i = 0
    while i < len(args):
        if args[i] == "--latch":
            flags["latch"] = "true"
            i += 1
            continue
        if args[i] in _ADD_FLAGS and i + 1 < len(args):
            flags[args[i][2:]] = args[i + 1]
            i += 2
            continue
        kept.append(args[i])
        i += 1
    parts = [p.strip() for p in " ".join(kept).split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print("usage: helm reflex add <id> | <steer> [--signal S] [--pattern RE] "
              "[--marker PATH] [--counter NAME --threshold N [--latch] "
              "[--escalate N] [--window S]] [--project P]", file=sys.stderr)
        return 2
    e = {"id": parts[0], "steer": parts[1], "signal": flags.get("signal", "prompt"),
         "pattern": flags.get("pattern", ""), "marker": flags.get("marker", ""),
         "counter": flags.get("counter", ""), "threshold": flags.get("threshold", ""),
         "escalate": flags.get("escalate", ""), "window": flags.get("window", ""),
         "latch": flags.get("latch", ""), "stated_ts": pk.now_ts()}
    if e["signal"] == "prompt" and not e["pattern"]:
        e["pattern"] = r"\b" + re.escape(parts[0]) + r"\b"
    if e["signal"] in COUNTER_SIGNALS and not counter_spec(e):
        print("helm reflex: '%s' signal needs a counter+threshold (specificity "
              "law) — add --threshold N (and --counter NAME for generic counter)"
              % e["signal"], file=sys.stderr)
        return 2
    path = write(e, flags.get("project"))
    print("helm reflex: LIVE '%s' (%s) -> %s" % (parts[0], _sig_desc(e), path))
    return 0


def _cmd_retire(args):
    import sys
    if not args:
        print("usage: helm reflex retire <id>", file=sys.stderr)
        return 2
    es = {pk.slug(e["id"]): e for e in load_all(include_retired=True)}
    e = es.get(pk.slug(args[0]))
    if not e:
        print("helm reflex: '%s' not found" % args[0], file=sys.stderr)
        return 1
    with open(e["path"]) as fh:
        raw = fh.read()
    pk.atomic_write(e["path"], raw.replace("  status: live", "  status: retired"))
    print("helm reflex: RETIRED '%s' (file kept as the record)" % args[0])
    return 0


def _cmd_smoke(args):
    """smoke [--session S] [--project P] — LIVE read-only counter check: the
    real record.py counters, which counter reflexes fire against them, and each
    one's armed/latched state. Mutates nothing (persist=False)."""
    from . import record
    project = args[args.index("--project") + 1] if "--project" in args \
        and args.index("--project") + 1 < len(args) else None
    session = args[args.index("--session") + 1] if "--session" in args \
        and args.index("--session") + 1 < len(args) else None
    reflexes = [e for e in load_all(project) if counter_spec(e)]
    if not reflexes:
        print("helm reflex smoke: no counter reflexes live — `helm reflex list`")
        return 0
    sessions = [(session, record.counters(session))] if session else \
        [(k, pk.read_json(p, {}) or {}) for k, p, _ in record._sessions()[:5]]
    if not sessions:
        print("helm reflex smoke: no recorded sessions yet (`helm record status`)")
        return 0
    for sid, counters in sessions:
        counters = counters if isinstance(counters, dict) else {}
        fired = {pk.slug(e["id"]) for e in fire("", project=project, session=sid,
                                                counters=counters, persist=False)}
        print("session %s:" % (sid or "(none)"))
        for e in reflexes:
            cn, th, esc = counter_spec(e)
            val = _int(counters.get(cn), 0)
            state = "FIRE" if pk.slug(e["id"]) in fired else \
                    "armed" if val >= th else "idle"
            print("  %-5s %-20s %s=%d/%d : %s" % (
                state, e["id"], cn, val, th, e["steer"][:60]))
    return 0
