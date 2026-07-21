#!/usr/bin/env python3
"""helm mentor — observe / teach / review / log: the inception actuator.

RSH extended SELF -> PAIR: a recurring failure-mode becomes a REFLEX the
junior carries instead of a per-dispatch correction re-paid forever. helm has
no panes — the junior is a PROJECT (v1 same-home; a seat is a project's
sessions), and a taught reflex in <project>/reflexes/ reaches every future
session of every harness through the inject hooks already installed: delivery
is free, teaching is the only new verb.

  observe <project> [--since 7d]   the ranked critique brief, READ-ONLY.
      Joins the window's sessions (the catalog lens, all harnesses) with the
      recorder's per-session counters (stuck / loop-thrash / passive / dirty
      streaks), scans their transcript tails for lexicon-seeded BUG-CLASS
      terms (kind: bug-class — LITERAL patterns, never open inference: the
      card's plausible-noise risk), and folds in what the estate already
      observes (drift findings snapshot-FREE + evolve's fire/reflex ledger
      observers). Deterministic signals supply the eyes and hands; the senior
      MODEL does the judging — every miss-pattern mints a paste-ready teach
      command, nothing is ever auto-taught.
  teach <project> "<id> | <steer>"  the incept — the ONE writing subverb.
      Writes one reflex into <project>/reflexes/ via reflex.write (the
      byte-shape owner), then annotates teacher/target provenance in place
      (the premise annotate pattern) + one events-journal receipt. A same-id
      file in the project is a hard refuse (supersede-not-duplicate), never a
      silent overwrite. --attest chains the incept onto the native
      attest-chain: ONE record carrying ment:b2b:<blake2b-256> over the
      canonical incept text — teaching provenance PROVABLE (offline, no
      signature). Substrate down =
      the reflex still lands, said loudly; `teach <project> <id> --attest`
      (no steer) backfills later. Mentor rows never ride premise's retry
      queue: its replay annotates store priors by id and would keep an alien
      row forever.
  review <project>   observe re-scoped per taught reflex, READ-ONLY: fires
      since stated_ts off the inject fire-ledger (did it even FIRE?) and the
      id-term's transcript recurrence before -> after (session-granular
      mentions, the teaching session included). The ledger logs fires, not
      heeds; mentions are not outcomes — the report says so.
  log [--project P]   who taught what, when, fired-since. READ-ONLY.

WRITER GATE (loud by law): teach writes ONE file in THIS helm home —
<project>/reflexes/reflex-<id>.md — and nothing else, ever. observe/review/
log write NOTHING: no drift snapshot consumed, no registry sync, no ledger
row, no cache. v1 is same-home: the junior is a project/seat of THIS
operator; cross-operator teach is out of scope until packs exist.
Fail-open everywhere: a missing catalog, torn counters, an absent ledger, or
a raising observer degrades to an empty section, never a crash.
"""
import os
import re
import sys
import time

from . import home, pk, reflex

SINCE_DEFAULT = "7d"
SCAN_TAIL = 2 * 1024 * 1024  # transcript tail bytes read per session
SCAN_SESSIONS = 40           # newest sessions scanned per verb
STUCK_MIN = 3                # counter floors before a session is flagged
LOOP_MIN = 3
PASSIVE_MIN = 15
DIRTY_MIN = 10
FLAG_CAP = 8                 # flagged sessions per brief
MISS_CAP = 8                 # miss-pattern terms per brief
BUG_CLASS = "bug-class"      # the lexicon kind that seeds miss-patterns
DIGEST_TAG = "ment:b2b:"     # blake2b-256, the premise digest family

_FLAG_TELLS = (("stuck-streak", STUCK_MIN, "stuck"),
               ("loop-streak", LOOP_MIN, "loop-thrash"),
               ("passive-streak", PASSIVE_MIN, "passive"),
               ("dirty-streak", DIRTY_MIN, "uncommitted"))

_USAGE = """usage: helm mentor observe <project> [--since 7d]
       helm mentor teach <project> "<id> | <steer>" [--signal S] [--pattern RE]
                        [--marker PATH] [--teacher NAME] [--attest]
       helm mentor teach <project> <id> --attest      (backfill-attest a taught reflex)
       helm mentor review <project>
       helm mentor log [--project P]

WRITER GATE: teach writes ONE reflex file — <helm home>/<project>/reflexes/ —
in THIS home, nothing else; observe/review/log write nothing. v1 same-home:
the junior is a project/seat of this operator (cross-operator waits for packs)."""


def _since_secs(spec):
    m = re.fullmatch(r"(\d+)([dhm])", str(spec or "").strip())
    return int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)] if m else None


def _epoch(ts):
    """pk.now_ts ISO-Z -> unix seconds; 0 when unparseable."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def _rows(project, cutoff=0):
    """-> (the project's catalog rows newest-first with mt >= cutoff, capped
    at SCAN_SESSIONS; catalog-reachable flag). Fail-open: ([], False)."""
    from . import sessions
    try:
        rows = sessions.rows_for(project=project)
    except Exception:
        return [], False
    return [r for r in rows if (r.get("mt") or 0) >= cutoff][:SCAN_SESSIONS], True


def _flags(rows):
    """Recorder counters joined per session -> flagged sessions ranked by
    evidence strength (sum of floor-crossing ratios). Torn counters read {}."""
    from . import record
    out = []
    for r in rows:
        try:
            c = record.counters(str(r.get("i") or ""))
        except Exception:
            c = {}
        tells = [(name, int(c.get(k) or 0), int(c.get(k) or 0) / floor)
                 for k, floor, name in _FLAG_TELLS if int(c.get(k) or 0) >= floor]
        if tells:
            out.append({"sid": str(r.get("i") or ""), "harness": str(r.get("h") or "?"),
                        "title": str(r.get("t") or ""),
                        "tells": [(n, v) for n, v, _s in tells],
                        "strength": sum(s for _n, _v, s in tells)})
    out.sort(key=lambda f: (-f["strength"], f["sid"]))
    return out


def seeds(project=None):
    """The miss-pattern seed set: lexicon terms of kind bug-class, lowercased
    — literal needles only (the card's precision law). Capture lane:
    helm store add lexicon "<term> | <definition> | bug-class"."""
    from . import store
    try:
        es = store.load_all(project=project, types=("lexicon",))
    except Exception:
        return []
    return sorted({str(e.get("term") or e["id"]).strip().lower()
                   for e in es if pk.slug(str(e.get("kind") or "")) == BUG_CLASS} - {""})


def _tail(path):
    """The transcript's last SCAN_TAIL bytes, lowercased; '' on any trouble."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - SCAN_TAIL))
            return f.read().decode("utf-8", "replace").lower()
    except OSError:
        return ""


def _scan(rows, needles):
    """{needle: {"sessions": n, "hits": m}} — literal lowercase counts over
    each row's transcript tail."""
    out = {}
    for r in rows:
        text = _tail(str(r.get("p") or ""))
        if not text:
            continue
        for t in needles:
            k = text.count(t)
            if k:
                d = out.setdefault(t, {"sessions": 0, "hits": 0})
                d["sessions"] += 1
                d["hits"] += k
    return out


def _estate(project):
    """What the estate already observes: drift findings (snapshot=False — a
    mentor look must never eat the operator's pending report) + evolve's
    fire/reflex ledger observers. -> [(area, what, verb)] ranked; each leg
    fail-open."""
    from . import drift, evolve
    props = []
    try:
        found, _n = drift.findings(project=project, snapshot=False)
        props += evolve._drift_props(found, project)
    except Exception:
        pass
    try:
        rows = evolve._ledger_rows()
        props += evolve._fire_props(rows, project) + evolve._reflex_props(rows, project)
    except Exception:
        pass
    props.sort(key=lambda p: (-p[0], p[2]))
    return [p[1:] for p in props[:evolve.BEHAVIOR_CAP]]


def observe(project, since=SINCE_DEFAULT):
    """The critique brief, READ-ONLY -> {project, since, sessions, catalog,
    seeds, flags, misses, estate}. Deterministic eyes; no judging here."""
    secs = _since_secs(since)
    rows, ok = _rows(project, time.time() - secs if secs else 0)
    terms = seeds(project)
    misses = [dict(d, term=t,
                   teach='helm mentor teach %s "%s | <steer>" --pattern "%s"'
                         % (project, t, re.escape(t)))
              for t, d in _scan(rows, terms).items()] if terms else []
    misses.sort(key=lambda m: (-m["sessions"], -m["hits"], m["term"]))
    return {"project": project, "since": since, "sessions": len(rows),
            "catalog": ok, "seeds": len(terms), "flags": _flags(rows)[:FLAG_CAP],
            "misses": misses[:MISS_CAP], "estate": _estate(project)}


def _cmd_observe(args):
    if not args or args[0].startswith("--"):
        print(_USAGE, file=sys.stderr)
        return 2
    project = args[0]
    since = args[args.index("--since") + 1] \
        if "--since" in args and args.index("--since") + 1 < len(args) else SINCE_DEFAULT
    if _since_secs(since) is None:
        print("helm mentor: bad --since '%s' (want Nd/Nh/Nm)" % since, file=sys.stderr)
        return 2
    b = observe(project, since)
    head = "helm mentor observe %s — last %s: %d session%s scanned" % (
        project, since, b["sessions"], "s"[:b["sessions"] != 1])
    if not b["catalog"]:
        head += " (session catalog unavailable — recorder/transcript legs dark)"
    if not (b["flags"] or b["misses"] or b["estate"]):
        print(head + ": quiet — no recorder tells, no bug-class hits, estate steady.")
        if not b["seeds"]:
            print('  (miss-pattern leg dark: no bug-class seeds — helm store add '
                  'lexicon "<term> | <definition> | bug-class")')
        return 0
    print(head + ":")
    if b["flags"]:
        print("  RECORDER — sessions whose counters cross a tell floor:")
        for f in b["flags"]:
            print("    %-10s %-7s %s  %s" % (f["sid"][:10], f["harness"],
                  ", ".join("%s x%d" % t for t in f["tells"]), f["title"][:48]))
    if b["misses"]:
        print("  MISS-PATTERNS — bug-class terms in the window's transcripts:")
        for m in b["misses"]:
            print("    %-36s %d session%s, %d mention%s" % (
                m["term"], m["sessions"], "s"[:m["sessions"] != 1],
                m["hits"], "s"[:m["hits"] != 1]))
            print("      teach: " + m["teach"])
    elif not b["seeds"]:
        print('  (miss-pattern leg dark: no bug-class seeds — helm store add '
              'lexicon "<term> | <definition> | bug-class")')
    if b["estate"]:
        print("  ESTATE — what drift + the fire ledger already observe:")
        for area, what, verb in b["estate"]:
            print("    [%s] %s%s" % (area, what, ("  ->  " + verb) if verb else ""))
    print("eyes only, nothing written — the judging is yours: pick what recurs, "
          "write the steer, teach it.")
    return 0


# ---------------------------------------------------------------------------
# teach — the incept, the ONE writing subverb
# ---------------------------------------------------------------------------

_TAUGHT_DEFAULTS = dict(reflex._DEFAULTS, teacher="", target="",
                        attest_payload="", attest_ts="", attest_by="",
                        attest_record="", attest_chain_index="",
                        attest_anchor="", attest_anchor_turn="")


def teacher_name():
    """HELM_ACTOR else the hook session else the login user — the events
    journal's actor resolution, so receipts and provenance agree."""
    import getpass
    return os.environ.get("HELM_ACTOR") or home.session_id() \
        or getpass.getuser()


def taught(project):
    """The project's TAUGHT reflexes (teacher-provenanced), retired included —
    review/log read the record, not just the live set."""
    d = os.path.join(home.project_dir(project), "reflexes")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        if not (n.startswith("reflex-") and n.endswith(".md")):
            continue
        e = pk.parse_simple_frontmatter(os.path.join(d, n), _TAUGHT_DEFAULTS)
        if e and e["id"] and e["teacher"]:
            e["path"] = os.path.join(d, n)
            out.append(e)
    return out


def incept_payload(target, rid, steer, teacher):
    """ment:b2b:<blake2b-256 hex> over the canonical incept text
    '<target>/<rid> | <steer> | teacher: <teacher>' (premise.canonicalize
    rules) — recomputable from the taught file alone."""
    import hashlib
    from . import premise
    text = "%s/%s | %s | teacher: %s" % (target, rid, steer, teacher)
    return DIGEST_TAG + hashlib.blake2b(
        premise.canonicalize(text).encode("utf-8"), digest_size=32).hexdigest()


def _attest(e):
    """ONE native attestation record for a taught reflex (+ optional dregg
    anchor) + in-place attest_* annotation. -> (ok, line). Never raises; the
    native record always lands, so the incept stays independently checkable."""
    from . import premise
    payload = incept_payload(e["target"], e["id"], e["steer"], e["teacher"])
    profile = premise.attest_profile()
    try:
        info = premise.record_attestation(
            "create", "%s/%s" % (e["target"], e["id"]), payload,
            root="reflex", attest_by=profile)
    except Exception as ex:
        return False, "attestation NOT recorded (%s: %s) — reflex is live; " \
            "backfill: helm mentor teach %s %s --attest  [payload %s]" \
            % (ex.__class__.__name__, ex, e["target"], e["id"], payload)
    fields = [("attest_payload", payload), ("attest_ts", info["ts"]),
              ("attest_by", profile), ("attest_record", info["rec_hash"]),
              ("attest_chain_index", info["chain_index"])]
    if info.get("anchor_turn"):
        fields += [("attest_anchor", info["anchor_label"]),
                   ("attest_anchor_turn", info["anchor_turn"])]
    premise._annotate(e["path"], fields)
    anchored = (" — %s" % info["anchor_label"]) if info.get("anchor_turn") else ""
    return True, "attested (native): record %s (chain_index %s) recorded by " \
        "'%s'%s" % (info["rec_hash"][:16], info["chain_index"], profile, anchored)


def _cmd_teach(args):
    from . import premise, registry
    flags, kept = {}, []
    i = 0
    while i < len(args):
        if args[i] == "--attest":
            flags["attest"] = True
        elif args[i] in ("--signal", "--pattern", "--marker", "--teacher") \
                and i + 1 < len(args):
            flags[args[i][2:]] = args[i + 1]
            i += 1
        else:
            kept.append(args[i])
        i += 1
    if not kept:
        print(_USAGE, file=sys.stderr)
        return 2
    project = kept[0]
    if registry.get(project) is None:
        print("helm mentor: unknown project '%s' — a taught reflex must have a "
              "reachable junior (helm projects; helm sync discovers)" % project,
              file=sys.stderr)
        return 1
    parts = [p.strip() for p in " ".join(kept[1:]).split("|")]
    if not parts[0]:
        print(_USAGE, file=sys.stderr)
        return 2
    rid = parts[0]
    if len(parts) < 2 or not parts[1]:
        if not flags.get("attest"):
            print(_USAGE, file=sys.stderr)
            return 2
        es = {pk.slug(e["id"]): e for e in taught(project)}  # backfill-attest
        e = es.get(pk.slug(rid))
        if not e:
            print("helm mentor: nothing taught as '%s' in %s" % (rid, project),
                  file=sys.stderr)
            return 1
        if e["attest_payload"]:
            print("helm mentor: '%s' already attested (%s)" % (rid, e["attest_payload"]))
            return 0
        ok, line = _attest(e)
        print("helm mentor: " + line)
        return 0 if ok else 1
    steer = parts[1]
    d = os.path.join(home.project_dir(project), "reflexes")
    path = os.path.join(d, "reflex-" + pk.slug(rid) + ".md")
    if os.path.exists(path):
        print("helm mentor: '%s' already exists in %s — supersede-not-duplicate: "
              "pick a new id, or retire the old one first (flip `status: live` -> "
              "`status: retired` in %s; the file stays as the record)"
              % (rid, project, path), file=sys.stderr)
        return 1
    teacher = flags.get("teacher") or teacher_name()
    e = {"id": rid, "steer": steer, "signal": flags.get("signal", "prompt"),
         "pattern": flags.get("pattern", ""), "marker": flags.get("marker", ""),
         "stated_ts": pk.now_ts(), "source": "mentor-teach"}
    if e["signal"] == "prompt" and not e["pattern"]:
        e["pattern"] = r"\b" + re.escape(rid) + r"\b"
    os.makedirs(d, exist_ok=True)
    wrote = reflex.write(e, project)
    if not premise._annotate(wrote, [("teacher", teacher), ("target", project)]):
        print("helm mentor: provenance annotation FAILED — %s is live but "
              "unprovenanced (log/review will not see it)" % wrote, file=sys.stderr)
        return 1
    pk.event("mentor.teach", rid, "taught -> %s by %s" % (project, teacher),
             actor=teacher)
    print("helm mentor: TAUGHT '%s' -> %s (%s) by %s" % (rid, project, e["signal"], teacher))
    print("  " + wrote)
    if os.path.isfile(reflex.reflex_path(rid)):
        print("  (shadows the global reflex '%s' inside %s)" % (rid, project))
    print("  delivery is free: the project's inject hook serves it on the next "
          "matching turn")
    if flags.get("attest"):
        _ok, line = _attest(dict(e, teacher=teacher, target=project, path=wrote))
        print("  " + line)
    return 0


# ---------------------------------------------------------------------------
# review / log — did it fire, did the miss recur
# ---------------------------------------------------------------------------

def fires_since(rid, ts):
    """Inject fire-ledger reflex fires for rid at/after ts. ISO-Z strings from
    one clock (pk.now_ts) on both sides — lexicographic compare is exact."""
    from . import evolve
    s = pk.slug(str(rid))
    return sum(1 for r in evolve._ledger_rows()
               if str(r.get("ts") or "") >= str(ts) and s in evolve._fired(r, "reflex"))


def review(project):
    """-> (per-taught rows, sessions scanned). Each row: the taught entry +
    fires (ledger, since stated_ts) + before/after transcript mentions of the
    id term around stated_ts (session-granular)."""
    rows, _ok = _rows(project)
    tails = [(r, _tail(str(r.get("p") or ""))) for r in rows]
    out = []
    for e in taught(project):
        t0 = _epoch(e["stated_ts"])
        needle = str(e["id"]).strip().lower()
        hit = [(r.get("mt") or 0) >= t0 for r, text in tails if needle in text]
        out.append(dict(e, fires=fires_since(e["id"], e["stated_ts"]),
                        before=hit.count(False), after=hit.count(True)))
    return out, len(rows)


def _mark(e):
    return ("" if e["status"] == "live" else " [%s]" % e["status"]) \
        + (" [attested]" if e["attest_payload"] else "")


def _cmd_review(args):
    if not args or args[0].startswith("--"):
        print(_USAGE, file=sys.stderr)
        return 2
    project = args[0]
    rows, scanned = review(project)
    if not rows:
        print("helm mentor review %s: nothing taught yet — `helm mentor observe %s` "
              "finds the candidates." % (project, project))
        return 0
    print("helm mentor review %s — %d taught:" % (project, len(rows)))
    for e in rows:
        print("  %s  taught %s by %s%s" % (e["id"], e["stated_ts"], e["teacher"], _mark(e)))
        print("    fired since taught: %d turn%s (inject ledger)"
              % (e["fires"], "s"[:e["fires"] != 1]))
        print("    '%s' mentions: %d session%s before -> %d after (of %d scanned; "
              "the teaching session counts)" % (e["id"], e["before"],
              "s"[:e["before"] != 1], e["after"], scanned))
    print("the ledger logs fires, not heeds; mentions are not outcomes — the "
          "judging is yours.")
    return 0


def _projects():
    """Every home dir that carries a reflexes/ chain (symlinked adoptions
    honored), _global excluded — the log's sweep set."""
    root = home.helm_home()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if n != home.GLOBAL and not n.startswith(".")
            and os.path.isdir(os.path.join(root, n, "reflexes"))]


def _cmd_log(args):
    project = args[args.index("--project") + 1] \
        if "--project" in args and args.index("--project") + 1 < len(args) else None
    rows = []
    for p in [project] if project else _projects():
        rows += [dict(e, target=e["target"] or p) for e in taught(p)]
    if not rows:
        print("helm mentor log: nothing taught yet%s."
              % (" for '%s'" % project if project else ""))
        return 0
    rows.sort(key=lambda e: (str(e["stated_ts"]), e["id"]), reverse=True)
    print("helm mentor log — %d taught:" % len(rows))
    for e in rows:
        print("  %s  %-16s %-28s by %s  fires-since: %d%s" % (
            e["stated_ts"], e["target"][:16], e["id"][:28], e["teacher"],
            fires_since(e["id"], e["stated_ts"]), _mark(e)))
    return 0


def cmd_mentor(args):
    """mentor observe|teach|review|log — the inception actuator: ranked
    critique brief, taught project reflexes with teacher/target provenance
    (optionally attested), before/after review. teach is the ONE write."""
    if not args:
        print(_USAGE)
        return 0
    sub, rest = args[0], list(args[1:])
    if sub == "observe":
        return _cmd_observe(rest)
    if sub == "teach":
        return _cmd_teach(rest)
    if sub == "review":
        return _cmd_review(rest)
    if sub == "log":
        return _cmd_log(rest)
    print("helm mentor: unknown subcommand '%s'\n%s" % (sub, _USAGE), file=sys.stderr)
    return 2
