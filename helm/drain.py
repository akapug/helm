#!/usr/bin/env python3
"""The DRAIN actuator — the intake dir is transient, never a destination.

Raw memory entries accumulate in the adopted store faster than anyone re-reads
them; undrained, they are lost AND useless. Drain reads each raw entry,
classifies it, routes it into its typed home, and archives the source with a
receipt. Nothing is ever deleted: every touched file is copied into a dated
archive dir FIRST, and the net is verified non-empty before any mutation (a
rollback net that captured nothing is a failed drain, loudly).

Classification (frontmatter `type:` first, filename prefix second):
  feedback entries    -> typed PRIOR (confidence 0.9, source human-feedback,
                         load_class jit — the JIT lane's specificity guard is
                         what keeps 169 new priors from wallpapering turns)
  reference entries   -> typed REFERENCE (ref-*.md)
  project entries     -> the project's journal in ~/.helm/<project>/journal/
                         (target resolved against the registry; unresolved
                         entries STAY and are reported)
  prem-*/prior-* twins-> the prem duplicate archives (--sweep-dups only;
                         operator-gated: it removes canon-store files, safe
                         per read-time dedup but surfaced first by design)
  already-typed files -> skipped (governed by the store)
  everything else     -> kept in place as episodic (not everything must move)

Dry-run by default; --apply executes; --limit N drains incrementally.
The index file (MEMORY.md) is a projection: lines referencing moved files are
re-pointed, never invented.
"""
import json
import os
import re
import shutil
import sys
import time

from . import home, pk, registry

_FM_DEFAULTS = {"name": "", "description": "", "type": "", "load_class": "",
                "originsessionid": "", "statement": ""}

TYPED_PREFIXES = ("prior-", "lex-", "heuristic-", "ref-", "reflex-")
DRAIN_CONFIDENCE = 0.9  # human feedback is strong evidence, not certainty

# The cap on a drained STATEMENT — deliberately equal to inject's LINE_CAP, the
# per-entry byte budget an entry gets when it FIRES. It used to be 300 here and
# 170 on the `description:` line the statement was actually read from, so an
# entry was cut to 170 at mint while the lane it fires into would have carried
# 400: pure loss, and it landed on the sharpest entries. One MEASURED example:
# the owner's "[CORRECTION ...] never clear your context mid-work, it destroys
# your working memory" was stored as "...let alone anything more [CORREC" — the
# entry could only ever inject his disappointment, never the directive.
# tests/test_drain.py asserts this stays == inject._common.LINE_CAP.
_STATEMENT_CAP = 400

# Built-in alias map: some global entries name a project by a short or old
# handle the registry knows under a different canonical name. The map is
# config-driven and EMPTY by default — the shipped tree carries no site-specific
# names; a deployment that has such legacy entries sets
# HELM_PROJECT_ALIASES="handle=canonical,handle=canonical". Per-project authored
# `aliases` (registry AUTHORED_FIELDS) fold in on top when present (registry.py
# is another lane's — we only READ). This is OPERATOR-AUTHORED ROUTING CONFIG
# feeding a copy-then-DELETE (--apply), so it validates LOUDLY (stderr, never a
# crash) and matches LONGEST-HANDLE-FIRST on the MERGED map — see _alias_map /
# _route_target.
#
# The DETERMINISM model (CD/codex-3/OI meld) — the map is SOURCE-TRACKED per
# handle, and every collision has ONE documented resolution:
#   1. canonical registry names always beat aliases (_route_target order);
#   2. env vs authored, same handle: AUTHORED wins (the project record is the
#      closer authority) — warned, naming ENV as the overridden side;
#   3. authored vs authored, same handle from two DIFFERENT projects:
#      AMBIGUOUS — the handle is REFUSED entirely (routes nothing), both
#      projects named. Never registry-iteration order: --apply copies then
#      DELETES intake, so a wrong winner LOSES the file, while a refused
#      handle only leaves it in intake (recoverable);
#   4. env vs env, same handle: first definition wins, rest rejected
#      (_builtin_aliases);
#   5. an UNREADABLE authored source (exists but cannot be read/parsed)
#      REFUSES alias routing for the whole drain — a partial env-only map
#      would route with HALF the authority while every surface reports
#      success. Absent file = legitimately empty layer, fine.
_ALIAS_ENV = "HELM_PROJECT_ALIASES"


def _alias_err(msg):
    # alias-config problems surface per parse, never raise: a typo'd entry
    # must not take the whole drain down, but silence would mis-route a
    # copy-then-delete (codex-3 review, finding #2)
    print("helm drain: " + msg, file=sys.stderr)


def _builtin_aliases():
    """handle(lowercased) -> canonical, parsed from HELM_PROJECT_ALIASES
    (``handle=canonical,handle=canonical``; whitespace stripped). Malformed
    entries — no ``=``, an empty half, or a second ``=`` (no project name
    contains one) — are REJECTED loudly; a duplicate handle keeps its FIRST
    definition and rejects the rest. Default empty: the public tree ships no
    site-specific aliases. Targets are checked against the registry in
    _alias_map (the registry lives there, not here)."""
    out = {}
    for pair in os.environ.get(_ALIAS_ENV, "").split(","):
        if not pair.strip():
            continue  # unset env / a trailing comma — absence, not malformation
        handle, eq, canonical = pair.partition("=")
        handle, canonical = handle.strip().lower(), canonical.strip()
        if not eq or not handle or not canonical or "=" in canonical:
            _alias_err("%s: malformed entry %r — want handle=canonical"
                       % (_ALIAS_ENV, pair.strip()))
        elif handle in out:
            _alias_err("%s: duplicate handle %r — first definition (%s) kept"
                       % (_ALIAS_ENV, handle, out[handle]))
        else:
            out[handle] = canonical
    return out


def _mem_dir():
    # store.adopted_dir() honors HELM_ADOPTED_DIR (the test-isolation override);
    # home.adopted_memory_dir() does not — routing through store keeps the drain
    # intake path hermetic and consistent with what the resolver reads.
    from . import store
    return store.adopted_dir()


def _project_mem_dir(project):
    """The claude memory dir to drain for `--project P`: the project's canonical
    cwd, else the first observed worktree cwd with a memory dir (collapsed to
    the one project, like sessions). None -> unknown/unmapped project."""
    rec = registry.get(project)
    if not rec:
        return None
    for cwd in [rec.get("path")] + list(rec.get("cwds") or []):
        if not cwd:
            continue
        d = home.claude_memory_dir_for(cwd)
        if os.path.isdir(d):
            return d
    return None


def _authored_source_error():
    """None when the authored registry layer is ABSENT (no file — an empty
    layer, legitimate) or reads as a JSON object; else a short reason string
    (unreadable / unparseable / wrong shape). The distinction is load-bearing
    for alias routing: registry._authored_load() nets a corrupt file and
    returns an EMPTY layer so the rest of helm keeps working, which is exactly
    the degrade drain must NOT ride — a half-written or permission-broken
    authored file would leave an env-only map routing a copy-then-DELETE with
    half the authority (and no surface would say so). So drain probes the file
    ITSELF (home.authored_path() — the one place the authored layer lives,
    read via registry.load), after registry.load() ran: load() never rewrites
    the file outside the one-time mixed-era migration, so the broken bytes are
    still there to see."""
    path = home.authored_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None            # genuinely absent — an empty authored layer
    except OSError as e:       # permission / I-O: exists but cannot be READ
        return "unreadable (%s)" % (e.strerror or e)
    try:
        val = json.loads(raw)
    except ValueError as e:    # bad JSON / a half-written concurrent write
        return "unparseable JSON (%s)" % e
    if not isinstance(val, dict) or not isinstance(val.get("projects", {}), dict):
        return "wrong shape (top level or `projects` is not an object)"
    return None


def _alias_map(reg):
    """-> (aliases, refused): alias(lowercased) -> canonical project name as
    ONE MERGED source-tracked map (env built-ins + every record's authored
    `aliases` — merged HERE so _route_target's longest-handle-first ranking
    spans sources), plus `refused` (None normally; a short reason when alias
    routing is OFF for this drain and the map is empty).

    An env target must name an existing registry project EXACTLY — a case
    mismatch or unknown name is dropped loudly, never guessed (authored
    targets are the record's own key, valid by construction). An authored
    field that is a bare string becomes ONE alias — it must never iterate
    char-by-char. Collisions per the determinism model above:
    same-handle env-vs-authored -> authored wins, warned naming ENV;
    same-handle authored-vs-authored (different projects) -> the handle is
    REFUSED outright, both projects named, any env definition suppressed with
    it (when the two closer authorities disagree, authority is not
    determinable — never pick by iteration order). Same-target duplicates
    (env and a record agreeing, or one record repeating itself) are harmless
    and silent. Projects iterate SORTED so no message or outcome ever depends
    on registry file order.

    An unreadable authored source refuses the WHOLE alias layer: announced on
    stderr with the path and why; classify keeps alias-routable entries in
    intake (reported) instead of falling back to env-only."""
    err = _authored_source_error()
    if err is not None:
        _alias_err("ALIAS ROUTING REFUSED for this drain — authored source %s "
                   "is %s. Alias-routable entries stay in intake; NOT falling "
                   "back to the env-only map (--apply deletes on route, and "
                   "the authored layer may refuse what env alone would route)."
                   % (home.authored_path(), err))
        return {}, "authored alias source " + err.split(" (")[0]
    projects = reg.get("projects") or {}
    by_fold = {n.lower(): n for n in projects}
    m = {}
    env_handles = set()   # env-sourced handles (for honest conflict receipts)
    authored_by = {}      # handle -> the project that authored it
    overridden = {}       # handle -> the env target an authored alias beat
    ambiguous = {}        # handle -> [projects] that all authored it (>= 2)
    for handle, canonical in _builtin_aliases().items():
        if canonical in projects:
            m[handle] = canonical
            env_handles.add(handle)
        elif canonical.lower() in by_fold:
            _alias_err("%s: %s=%s — case mismatch (registry has %r); dropped"
                       % (_ALIAS_ENV, handle, canonical, by_fold[canonical.lower()]))
        else:
            _alias_err("%s: %s=%s — target names no registry project; dropped"
                       % (_ALIAS_ENV, handle, canonical))
    for name in sorted(projects):
        authored = projects[name].get("aliases")
        if isinstance(authored, str):
            _alias_err("project %s: authored `aliases` is a string, not a list "
                       "— treated as the single alias %r" % (name, authored))
            authored = [authored]
        if not isinstance(authored, (list, tuple)):
            if authored is not None:
                _alias_err("project %s: authored `aliases` is %s, not a list — "
                           "ignored" % (name, type(authored).__name__))
            authored = []
        for a in authored:
            a = str(a).strip().lower()
            if not a:
                continue
            if a in ambiguous:                 # a third+ claimant piles on
                if name not in ambiguous[a]:
                    ambiguous[a].append(name)
                continue
            prior = authored_by.get(a)
            if prior is None:
                if a in env_handles and m[a] != name:
                    overridden[a] = m[a]       # warned below, unless refused
                m[a] = name
                authored_by[a] = name
            elif prior != name:                # two RECORDS claim one handle
                ambiguous[a] = [prior, name]
    # receipts last, one line per handle, sorted: an env-override warning must
    # not fire for a handle that then turns out ambiguous (the refusal is the
    # only truth about it), and the ACTUAL source of each side is named —
    # env-vs-authored says ENV, authored-vs-authored names BOTH projects.
    for a in sorted(overridden):
        if a not in ambiguous:
            _alias_err("alias %r: authored (%s) overrides %s (%s)"
                       % (a, authored_by[a], _ALIAS_ENV, overridden[a]))
    for a in sorted(ambiguous):
        _alias_err("alias %r: AMBIGUOUS — authored by BOTH %s; handle REFUSED "
                   "(routes nothing, matching entries stay in intake)%s"
                   % (a, " AND ".join(ambiguous[a]),
                      " — its %s definition is suppressed with it" % _ALIAS_ENV
                      if a in env_handles else ""))
        del m[a]
    return m, None


def _route_target(base, blob, project_names, aliases):
    """The project an entry routes to, canonical names first (filename prefix or
    exact, then a specific >=6-char description word), aliases second — a short
    2-char alias matches by filename only (a 2-char word wallpapers). Aliases
    rank LONGEST HANDLE FIRST (the law project_names already follows in
    classify), alphabetical on equal length — never dict insertion order:
    --apply copies-then-DELETES on a match, so `old-api-roadmap.md` must reach
    `old-api` whichever side of `old` the operator listed it (codex-3, #1).
    The ranking runs on the ONE MERGED map _alias_map returns — a prefix pair
    can straddle sources (`old` from env, `old-api` authored), so any
    per-source sort-before-merge would silently rank them apart (meld)."""
    ranked = sorted(aliases.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    low = base.lower()
    for pn in project_names:
        if low.startswith(pn.lower() + "-") or low == pn.lower():
            return pn
    for alias, canon in ranked:
        if canon in project_names and (low.startswith(alias + "-") or low == alias):
            return canon
    for pn in project_names:
        if len(pn) >= 6 and re.search(
                r"(?<![a-z0-9])" + re.escape(pn.lower()) + r"(?![a-z0-9])", blob):
            return pn
    for alias, canon in ranked:
        if canon in project_names and len(alias) >= 6 and re.search(
                r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", blob):
            return canon
    return None


def _entries(mem):
    for n in sorted(os.listdir(mem)):
        p = os.path.join(mem, n)
        if not (n.endswith(".md") and os.path.isfile(p)) or n == "MEMORY.md":
            continue
        yield n, p


def _retype_or_conflict(src, dst, slug, to_type, e, names, planned_dsts):
    """A retype must NEVER clobber an existing typed entry (it may hold months
    of curated evidence) or a sibling drain action's destination. Either
    collision downgrades to a surfaced 'conflict' the operator resolves, never
    a silent overwrite."""
    if dst in names:
        return {"op": "conflict", "src": src, "dst": dst,
                "why": "target %s already exists in the store — would overwrite "
                       "a curated entry; resolve by hand (supersede/merge)" % dst}
    if dst in planned_dsts:
        return {"op": "conflict", "src": src, "dst": dst,
                "why": "two intake files map to %s (also %s) — slug collision"
                       % (dst, planned_dsts[dst])}
    planned_dsts[dst] = src
    # an explicit `statement:` is AUTHORITATIVE; `description:` is the fallback.
    # description is a SUMMARY field (writers cap it at 170 with an "id - " tag
    # prefix), so reading the statement out of it silently truncated every
    # drained entry to a 170-char prefix of itself. Intake files that carry no
    # statement still fall back, so nothing pre-existing stops draining.
    return {"op": "retype", "src": src, "to_type": to_type, "dst": dst,
            "id": slug, "statement": e.get("statement") or e.get("description") or "",
            "origin": e.get("originsessionid") or ""}


def classify(mem=None):
    """-> list of action dicts. Pure derivation, no writes."""
    mem = mem or _mem_dir()
    names = set(os.listdir(mem)) if os.path.isdir(mem) else set()
    reg = registry.load()
    project_names = sorted(reg["projects"], key=len, reverse=True)
    aliases, alias_refused = _alias_map(reg)
    plan = []
    planned_dsts = {}   # dst filename -> first src that claimed it (collision guard)
    for n, p in _entries(mem):
        if n.startswith("prem-"):
            twin = "prior-" + n[len("prem-"):]
            if twin in names:
                plan.append({"op": "sweep-dup", "src": n, "twin": twin})
                continue
            act = _upgrade_action(n, p, names, planned_dsts)  # un-twinned prem -> upgrade
            if act:
                plan.append(act)
            continue
        if n.startswith(TYPED_PREFIXES):
            # prior-/lex- that FELL BACK to episodic (typed prefix, no typed
            # fields) become upgrade candidates; real typed entries + ref-/reflex-
            # return None and stay governed by the store.
            if n.startswith(("prior-", "lex-")):
                act = _upgrade_action(n, p, names, planned_dsts)
                if act:
                    plan.append(act)
            continue
        e = pk.parse_simple_frontmatter(p, _FM_DEFAULTS) or {}
        etype = (e.get("type") or "").lower()
        base = n[:-3]
        if etype == "feedback" or n.startswith(("feedback-", "feedback_")):
            slug = pk.slug(re.sub(r"^feedback[-_]", "", base))
            dst = "prior-" + slug + ".md"
            plan.append(_retype_or_conflict(n, dst, slug, "prior", e, names, planned_dsts))
            continue
        if etype == "reference" or n.startswith(("reference-", "reference_")):
            slug = pk.slug(re.sub(r"^reference[-_]", "", base))
            dst = "ref-" + slug + ".md"
            plan.append(_retype_or_conflict(n, dst, slug, "reference", e, names, planned_dsts))
            continue
        if etype == "project" or n.startswith(("proj-", "proj_", "project_")):
            # filename-prefix / exact match always counts; a description match
            # needs a name specific enough not to wallpaper (short names like
            # "dev" appear as ordinary words in half the corpus). The alias map
            # routes entries that name a project by a short/old handle.
            blob = (e.get("description") or "").lower()
            target = _route_target(base, blob, project_names, aliases)
            if target:
                plan.append({"op": "route-project", "src": n, "project": target,
                             "dst": os.path.join(home.project_dir(target), "journal", n)})
            else:
                # under an alias refusal every unrouted project entry is
                # REPORTED as held by it — with the authored layer dark we
                # cannot know which of them an authored alias would claim
                why = "project entry, no registry match"
                if alias_refused:
                    why += " (alias routing refused: %s)" % alias_refused
                plan.append({"op": "keep", "src": n, "why": why})
            continue
        plan.append({"op": "keep", "src": n, "why": "episodic"})
    return plan


def _keywords_from(slug, statement):
    """Keywords for a drained entry: the DISTINCTIVE words of slug + the FULL
    statement — length >= 5, non-generic (store.GENERIC_KEYWORDS), ranked by
    in-statement frequency then length (a repeated long word IS the topic),
    first-seen breaking ties; cap 8. The old derivation (first 80 chars,
    len > 3, first-seen only) minted weak generic keywords for the drained
    cohort — --rekey below migrates those in place."""
    from .store import GENERIC_KEYWORDS
    rank = {}
    for w in re.split(r"[^a-z0-9]+", (slug + " " + statement).lower()):
        if len(w) >= 5 and w not in GENERIC_KEYWORDS:
            rank.setdefault(w, [0, len(w), -len(rank)])[0] += 1
    return ",".join(sorted(rank, key=lambda w: rank[w], reverse=True)[:8])


def _retype_text(src_path, act, ts):
    """The upgraded entry: typed frontmatter + the ORIGINAL body preserved
    verbatim (a drain that loses the why-and-how of an entry drained nothing)."""
    with open(src_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    body = raw.split("---", 2)[2].lstrip("\n") if raw.count("---") >= 2 else raw
    st = re.sub(r"\s+", " ", (act["statement"] or act["id"]).replace('"', "'"))[:_STATEMENT_CAP]
    kind = act["to_type"]
    to_prior = kind == "prior"
    value_key = "move" if kind == "heuristic" else "statement"
    fm = [
        "---",
        "name: " + act["dst"][:-3],
        'description: "' + kind + ": " + (act["id"] + " - " + st)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: " + kind,
        "  id: " + act["id"],
        "  " + value_key + ": " + st,
    ]
    if to_prior:
        fm += ["  confidence: %.2f" % DRAIN_CONFIDENCE,
               "  class: prior",
               "  load_class: jit",
               "  evidence_log: " + '[{"ts":"%s","type":"stated","delta":%.2f,'
               '"reason":"drained from feedback memory","by":"human"}]'
               % (ts, DRAIN_CONFIDENCE),
               "  confidence_history: " + '[{"ts":"%s","value":%.2f,'
               '"reason":"drained from feedback memory"}]' % (ts, DRAIN_CONFIDENCE)]
    else:
        fm += ["  load_class: jit"]
    kw = act["trigger"] if act["to_type"] == "heuristic" else \
        act.get("keywords", _keywords_from(act["id"], act["statement"] or ""))
    fm += ["  status: live",
           ("  trigger: " if act["to_type"] == "heuristic" else "  keywords: ") + kw,
           "  source: drain",
           "  stated_ts: " + ts,
           "  last_updated: " + ts,
           "  drained_from: " + act["src"]]
    if act.get("origin"):
        fm.append("  origin_session: " + act["origin"])
    fm += ["---", ""]
    return "\n".join(fm) + body


DRAINED_MARK = "drained from feedback memory"  # the evidence reason drain seeds
UPGRADE_MARK = "upgraded from typed-prefix bulk memory"  # drain-v2 evidence reason


def _upgrade_text(src_path, act, ts):
    """The drain-v2 upgrade: synthesize real typed frontmatter for a
    typed-PREFIX file that never carried typed fields (episodic fallback),
    body preserved verbatim. prem-/prior- -> a PRIOR at 0.9 jit — NOT 1.0: a
    prem- prefix on bulk memory is a naming accident, not an attestation (the
    premise tier requires the ledger path). lex- -> a real LEXICON entry."""
    with open(src_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    body = raw.split("---", 2)[2].lstrip("\n") if raw.count("---") >= 2 else raw
    st = re.sub(r"\s+", " ", (act["statement"] or act["id"]).replace('"', "'"))[:_STATEMENT_CAP]
    kw = act.get("trigger") if act["to_type"] == "heuristic" else \
        act.get("keywords", _keywords_from(act["id"], act["statement"] or ""))
    if act["to_type"] == "heuristic":
        fm = [
            "---",
            "name: " + act["dst"][:-3],
            'description: "heuristic: ' + (act["id"] + " - " + st)[:170] + '"',
            "metadata:",
            "  node_type: memory",
            "  type: heuristic",
            "  id: " + act["id"],
            "  move: " + st,
            "  trigger: " + kw,
            "  load_class: jit",
            "  status: live",
            "  source: drain-upgrade",
            "  stated_ts: " + ts,
            "  last_updated: " + ts,
            "  upgraded_from: " + act["src"],
            "---", "",
        ]
        return "\n".join(fm) + body
    if act["to_type"] == "lexicon":
        fm = [
            "---",
            "name: " + act["dst"][:-3],
            'description: "' + ("lexicon: " + act["id"] + " = " + st)[:200] + '"',
            "metadata:",
            "  node_type: memory",
            "  type: lexicon",
            "  term: " + act["id"],
            "  scope: global",
            "  kind: phrase",
            "  source: drain-upgrade",
            "  updated_ts: " + ts,
            "  hits: 0",
            "  definition: " + st,
        ]
        if kw:
            fm.append("  keywords: " + kw)
        fm += [
            "  upgraded_from: " + act["src"],
            "---", "",
        ]
        return "\n".join(fm) + body
    fm = [
        "---",
        "name: " + act["dst"][:-3],
        'description: "prior: ' + (act["id"] + " - " + st)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: prior",
        "  id: " + act["id"],
        "  statement: " + st,
        "  confidence: %.2f" % DRAIN_CONFIDENCE,
        "  class: prior",
        "  load_class: jit",
        "  evidence_log: " + '[{"ts":"%s","type":"stated","delta":%.2f,"reason":"%s","by":"drain"}]'
        % (ts, DRAIN_CONFIDENCE, UPGRADE_MARK),
        "  confidence_history: " + '[{"ts":"%s","value":%.2f,"reason":"%s"}]'
        % (ts, DRAIN_CONFIDENCE, UPGRADE_MARK),
        "  status: live",
        "  keywords: " + kw,
        "  source: drain-upgrade",
        "  stated_ts: " + ts,
        "  last_updated: " + ts,
        "  upgraded_from: " + act["src"],
        "---", "",
    ]
    return "\n".join(fm) + body


def _upgrade_action(n, p, names, planned_dsts):
    """A typed-PREFIX file (prem-/prior-/lex-) that FAILS typed parse (episodic
    fallback — the 194 dark files) -> an 'upgrade' op. Real typed entries are
    already governed (None). The >=2-specific-keyword gate is load-bearing: a
    terse description that yields <2 distinctive keywords stays episodic and is
    reported, never force-upgraded into a generic wallpaper prior."""
    from . import store
    e = store._parse_entry(p, n)
    if not e or e.get("type") != "episodic":
        return None  # parses as a real typed entry already -> skip (governed)
    statement = (e.get("statement") or "").strip()
    if not statement:
        return {"op": "keep", "src": n, "why": "typed-prefix, no description to upgrade"}
    if n.startswith("lex-"):
        to_type = "lexicon"
        slug = pk.slug(n[len("lex-"):-3])
        dst = "lex-" + slug + ".md"
    else:  # prem- or prior-
        to_type = "prior"
        slug = pk.slug(re.sub(r"^pr(?:em|ior)-", "", n[:-3]))
        dst = "prior-" + slug + ".md"
    kw = _keywords_from(slug, statement)
    specific = [k for k in kw.split(",") if k]
    if len(specific) < 2:
        return {"op": "keep", "src": n,
                "why": "typed-prefix bulk, %d specific keyword(s) (<2) — kept episodic"
                       % len(specific)}
    if dst != n:  # prem- -> prior- rename must not clobber a curated entry
        if dst in names:
            return {"op": "conflict", "src": n, "dst": dst,
                    "why": "upgrade target %s already exists — resolve by hand" % dst}
        if dst in planned_dsts:
            return {"op": "conflict", "src": n, "dst": dst,
                    "why": "two intake files map to %s (also %s)" % (dst, planned_dsts[dst])}
        planned_dsts[dst] = n
    return {"op": "upgrade", "src": n, "dst": dst, "to_type": to_type,
            "id": slug, "statement": statement, "keywords": kw,
            "origin": e.get("originsessionid") or ""}


def _rekey_edit(raw, new_kw, ts):
    """The in-place two-line edit: the frontmatter `keywords:` line replaced
    and a rekeyed receipt appended to its `evidence_log:` line. Every other
    byte survives identical (never a full rewrite); None when either line is
    missing/garbled — a malformed entry is skipped, never guessed at."""
    end = raw.find("\n---", 1)
    if end < 0:
        return None
    fm, rest = raw[:end], raw[end:]
    ev = re.search(r"^(\s*evidence_log:\s*)(\[.*\])\s*$", fm, re.M)
    if not (ev and re.search(r"^\s*keywords:", fm, re.M)):
        return None
    try:
        log = json.loads(ev.group(2))
    except ValueError:
        return None
    kw = re.search(r"^\s*keywords:\s*([^\n]*)$", fm, re.M)
    old = (kw.group(1).strip() if kw else "")[:160]
    log.append({"ts": ts, "type": "rekeyed", "delta": 0,
                "reason": "rekeyed: distinctive keywords from full statement"
                          + (" (was: %s)" % old if old else ""),
                "by": "drain"})
    fm = re.sub(r"^(\s*keywords:)[^\n]*$",
                lambda m: m.group(1) + " " + new_kw, fm, count=1, flags=re.M)
    fm = re.sub(r"^(\s*evidence_log:\s*)\[.*\]\s*$",
                lambda m: m.group(1) + json.dumps(log, separators=(",", ":"),
                                                  ensure_ascii=False),
                fm, count=1, flags=re.M)
    return fm + rest


def rekey(apply=False):
    """The ONE-TIME drained-cohort migration: every store prior whose
    evidence_log carries DRAINED_MARK gets its keywords recomputed with the
    fixed derivation, edited IN PLACE (atomic; untouched bytes preserved) plus
    a rekeyed evidence receipt — which is also the idempotence marker (already-
    rekeyed entries skip). Dry-run unless apply. -> receipt dict."""
    from . import store
    ts = pk.now_ts()
    drained = already = malformed = 0
    changes = []
    # PHYSICAL roots, not the merged view: a drained adopted prior shadowed by
    # a same-slug global winner would never surface through load_all and its
    # file would stay un-rekeyed forever (codex-seat review)
    entries, seen = [], set()
    for root, scope, d in store.roots(None):
        for e in store._load_root(root, scope, d).values():
            if e["path"] not in seen:
                seen.add(e["path"])
                entries.append(e)
    for e in entries:
        log = [i for i in (e.get("evidence_log") or []) if isinstance(i, dict)]
        if e["type"] != "prior" or \
                not any(DRAINED_MARK in str(i.get("reason") or "") for i in log):
            continue
        drained += 1
        if any(i.get("type") == "rekeyed" for i in log):
            already += 1
            continue
        with open(e["path"], encoding="utf-8", errors="replace") as f:
            raw = f.read()
        new_kw = _keywords_from(pk.slug(str(e["id"])), e.get("statement") or "")
        edited = _rekey_edit(raw, new_kw, ts)
        if edited is None:
            malformed += 1
            continue
        changes.append((str(e["id"]), e.get("keywords") or "", new_kw))
        if apply:
            pk.atomic_write(e["path"], edited)
    if apply and changes:
        pk.event("drain.rekey", "drained-cohort",
                 "%d prior%s rekeyed in place" % (len(changes), "s"[:len(changes) != 1]))
    return {"ts": ts, "drained": drained, "already": already,
            "malformed": malformed, "changes": changes,
            "rekeyed": len(changes) if apply else 0}


def _cmd_rekey(args):
    r = rekey(apply="--apply" in args)
    print("helm drain --rekey: %d drained prior%s — %d to rekey, %d already rekeyed%s"
          % (r["drained"], "s"[:r["drained"] != 1], len(r["changes"]), r["already"],
             (", %d malformed skipped" % r["malformed"]) if r["malformed"] else ""))
    for pid, old, new in r["changes"][:5]:
        print("  %s: %s -> %s" % (pid, old or "-", new or "-"))
    if len(r["changes"]) > 5:
        print("  ... %d more" % (len(r["changes"]) - 5))
    if "--apply" not in args:
        print("helm drain --rekey: DRY-RUN (nothing written). Re-run with --apply.")
    elif r["rekeyed"]:
        print("helm drain --rekey: REKEYED %d entr%s in place (receipt appended to "
              "each evidence_log)" % (r["rekeyed"], "y" if r["rekeyed"] == 1 else "ies"))
    return 0


def expire_candidates(days=14, apply=False, project=None):
    """The candidate DECAY leg — operator-visible hygiene, never a silent
    background job. An unconfirmed candidate older than `days` (by its own
    timestamp; a candidate with NO timestamp is never expired) is archived to a
    drain-style net and removed on --apply. Dry-run returns the plan untouched.
    Telemetry-unseen is subsumed by age here: candidates never inject, so 'stale
    and unconfirmed' IS the decay signal. -> receipt dict."""
    from . import store
    ts = pk.now_ts()
    cutoff = time.time() - max(days, 0) * 86400
    stale = [e for e in store.candidates(project=project)
             if store._recency(e) and store._recency(e) < cutoff]
    receipt = {"ts": ts, "days": days, "found": len(stale), "expired": 0,
               "ids": [str(e["id"]) for e in stale]}
    if not apply or not stale:
        return receipt
    net = os.path.join(home.global_dir(), "archive",
                       "candidate-expiry-" + ts.replace(":", "").replace("-", "")[:13])
    os.makedirs(net, exist_ok=True)
    for e in stale:
        shutil.copy2(e["path"], os.path.join(net, os.path.basename(e["path"])))
    for e in stale:  # the rollback net must be verified before ANY delete
        cop = os.path.join(net, os.path.basename(e["path"]))
        if not os.path.isfile(cop) or os.path.getsize(cop) != os.path.getsize(e["path"]):
            raise RuntimeError("drain: candidate net incomplete for %s — ABORTING, "
                               "nothing removed" % os.path.basename(e["path"]))
    for e in stale:
        os.remove(e["path"])
    receipt.update({"expired": len(stale), "net": net})
    pk.write_json(os.path.join(net, "RECEIPT.json"), receipt)
    pk.event("drain.expire-candidates", net,
             "%d unconfirmed candidate%s pruned (>%dd, net kept)"
             % (len(stale), "s"[:len(stale) != 1], days))
    return receipt


def _cmd_expire_candidates(args, project=None):
    days = 14
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except (ValueError, IndexError):
            print("helm drain --expire-candidates: --days needs an integer", file=sys.stderr)
            return 2
    r = expire_candidates(days=days, apply="--apply" in args, project=project)
    print("helm drain --expire-candidates: %d unconfirmed candidate%s older than %dd"
          % (r["found"], "s"[:r["found"] != 1], r["days"]))
    for cid in r["ids"][:8]:
        print("  - " + cid)
    if len(r["ids"]) > 8:
        print("  ... %d more" % (len(r["ids"]) - 8))
    if "--apply" not in args:
        print("helm drain: DRY-RUN (nothing pruned). Re-run with --apply.")
    elif r["expired"]:
        print("helm drain: PRUNED %d candidate%s; net + receipt at %s"
              % (r["expired"], "s"[:r["expired"] != 1], r.get("net", "-")))
    return 0


# ---------------------------------------------------------------------------
# promote — the episodic->durable funnel: recent USER messages -> drain intake
# ---------------------------------------------------------------------------

# Explicit durable-knowledge markers in USER-typed text. Precision is
# load-bearing (the card's own RISKS): the USER-role restriction, the length
# guard, the cap, and the dedupe keep agent-authored text and task prompts out.
#
# THEY DID NOT. MEASURED over the cohort this gauntlet minted (43 entries, all
# retired 2026-08-03): the marker was a bare substring test, so ANY message
# carrying one of these words ANYWHERE was minted as durable canon — 43 junk
# priors, 903 JIT injection fires, ~198KB injected in 4 days, and because the
# JIT lane caps at 4 entries per prompt, every junk hit EVICTED a real one.
# Minted examples: "spark is idle again as always bc it's so fast haha",
# "i never saw the verification", and five verbatim <task-notification> XML
# blobs. Three of the 43 were genuine. The three failures, in order of damage:
#
#   1. SUBSTRING, NOT WORD.  "always "/"never " matched inside other words:
#      3 of the 43 were minted purely because "whenever" contains "never ".
#   2. CONTAINS-A-MARKER != IS-A-DIRECTIVE.  "i never saw the verification"
#      reports the past; "we should always have the latest" states a rule.
#      Same word, opposite speech act.
#   3. NO STRUCTURAL FLOOR.  XML notification payloads, pasted chat logs,
#      [Image #4] attachment stubs, and fleet seat-address wake prompts are
#      machine traffic that happens to arrive on the role:user channel.
#
# So the gate below is three layers, cheapest first: PROVENANCE (is this even
# owner-typed?), STRUCTURE (does it look like prose at all?), then SPEECH ACT
# (is the marker doing directive work?). All stdlib, all deterministic; the
# labelled 43-message corpus in tests/fixtures/promote-corpus.json is the
# regression test, and its 3 genuine entries are the positive controls that
# stop this from degenerating into "refuse everything".
_PROMOTE_MARKERS = ("from now on", "remember this", "remember that",
                    "make it a rule", "going forward", "the rule is",
                    "always", "never")
_PROMOTE_MAXLEN = 600  # a durable rule is a sentence, not an essay/task prompt
_PROMOTE_MINLEN = 30   # ...and not a bare exclamation. Deliberately LOW: on the
                       # 43-message corpus the floor buys nothing the speech-act
                       # check below does not already catch (measured identical
                       # at 0, 30 and 60), and a real rule can be terse — "never
                       # touch the gamma3 store directly" is 37 chars. It exists
                       # only so "always!" cannot reach the analyzer at all.

# The two markers that are ordinary English words carry no directive force by
# themselves; the rest are explicit "capture this" phrasings that do.
_WEAK_MARKERS = ("always", "never")

# Layer 2 — STRUCTURE. Any hit refuses outright, before speech-act analysis.
_PROMOTE_REFUSALS = (
    # literal markup: XML/HTML notification payloads (<task-notification>,
    # </summary>, &lt;repo&gt;). A closing tag or an escaped entity in owner
    # prose is vanishingly rare; in machine payloads it is universal.
    ("markup", re.compile(r"</[A-Za-z][\w.-]*>|&(?:lt|gt|amp|quot|#\d+);")),
    # attachment stubs — the referent is a picture nobody can re-read later
    ("attachment", re.compile(r"\[(?:Image|Screenshot)\b[^\]]*\]", re.I)),
    # a pasted chat log is a transcript OF a conversation, not a directive in one
    ("chat-log", re.compile(r"\[\d{1,2}:\d{2}\s*[AP]M\]|\bcmr://")),
    # laughter/emoticons mark banter. "spark is idle again as always ... haha"
    ("banter", re.compile(r"\b(?:ha(?:ha)+|hehe+|lol|lmao|rofl)\b|:-?[)D]|!!!", re.I)),
    # fleet seat addresses (w5:p1), handoff tags and commit reports are
    # agent-to-agent traffic — real on the role:user channel, never owner canon
    ("fleet-traffic", re.compile(
        r"\bw\d+:p\w+|\bthis pane\b|\[[\w-]+-DONE\]|\bCODE_READY\b"
        r"|\bcommit [0-9a-f]{7,40}\b", re.I)),
    # a leading ellipsis means the subject is off-screen in an earlier message
    ("fragment", re.compile(r"^\s*(?:\.\.\.|…)")),
)

# Layer 3 — SPEECH ACT, evaluated per clause. Clauses split on sentence AND
# comma/dash boundaries: "yes, always best of both, agents def need to be able
# to know their own auth" put a bare "always" and a distant "need to" in one
# sentence, and only clause-level scope keeps them apart.
#
# Two boundary bugs this spelling exists to avoid, both MEASURED on the corpus,
# both of which severed a subordinator from its clause and promoted a purpose
# clause ("...updated so state is always clear") into a bare normative copula:
#   - sentence punctuation only counts at a word boundary, or "planning.linear"
#     splits mid-token;
#   - a newline is WHITESPACE, not a boundary (the text wrapped between "so"
#     and "state"), so the caller collapses runs of whitespace first.
_CLAUSE_SPLIT = re.compile(r"[.?!;:](?=\s|$)|,| -- | — ")

# The marker is DESCRIBING, not directing. Checked first — a disqualified
# clause cannot be rescued by an obligation word elsewhere in it.
_NOT_DIRECTIVE = (
    re.compile(r"\bas always\b"),                       # idiom, pure filler
    # the speaker reporting on HIMSELF ("i never saw...", "im just always
    # concerned"). Only copulas and adverbs may sit between the pronoun and the
    # marker: "i WANT YOU TO always sign before ship" is a directive addressed
    # OUTWARD and must survive, and a {0,3} any-word gap swallowed it.
    re.compile(r"\b(?:i|im|i'm)\b"
               r"(?:\s+(?:am|was|were|really|just|honestly|also|still|only))*"
               r"\s+(?:always|never)\b"),
    # past-tense narration ("that was never ported", "the card was never
    # moved"). have/has/had + "to" is EXCLUDED: it is an obligation modal, and
    # counting it as past tense refused "we have to always sign before ship".
    re.compile(r"(?:\b(?:was|were|been|'ve|'d)\b|\b(?:had|has|have)\b(?!\s+to\b))"
               r"(?:\s+\w+){0,3}?\s+(?:always|never)\b"),
    re.compile(r"\b(?:why|how|whether|what|so that|so|because|bc|since|"
               r"unless|though|although)\b.*?\b(?:always|never)\b"),  # subordinate
)

# The marker IS directing: an obligation modal in the clause, a normative
# copula bound to the marker, or the marker fronting an imperative verb.
_IS_DIRECTIVE = (
    re.compile(r"\b(?:should|must|shall|ought to|need to|needs to|"
               r"have to|has to)\b"),
    re.compile(r"\b(?:is|are|'s|'re)\s+(?:always|never)\b"),
    re.compile(r"\b(?:always|never)\s+(?:be|do|use|make|keep|push|pull|run|"
               r"write|read|send|report|check|verify|trust|clear|touch|leave|"
               r"rely|end|start|stop|integrate|assume|delete|commit|land|ask|"
               r"tell|treat|prefer|ship|squelch|sign|merge|branch)\b"),
)


def _promote_marker(low):
    """The durable-knowledge marker in `low` (already lowercased), or None.
    Word-boundary matched: "whenever" no longer reads as "never"."""
    for m in _PROMOTE_MARKERS:
        if re.search(r"\b" + re.escape(m) + r"\b", low):
            return m
    return None


def _marker_is_directive(low, marker):
    """True when SOME clause uses `marker` to direct rather than to describe.
    Explicit capture phrasings ("from now on", "remember this") are directive
    by construction; the two bare English words have to earn it."""
    if marker not in _WEAK_MARKERS:
        return True
    for clause in _CLAUSE_SPLIT.split(low):
        if not re.search(r"\b" + marker + r"\b", clause):
            continue
        if any(p.search(clause) for p in _NOT_DIRECTIVE):
            continue
        if any(p.search(clause) for p in _IS_DIRECTIVE):
            return True
    return False


def promote_verdict(text):
    """-> (marker, None) when `text` may be promoted, else (None, reason).

    The reason string names the LAYER that refused, so the receipt's `refused`
    histogram (and the line cmd_promote prints from it) can tell an operator
    why a message was dropped instead of dropping it silently."""
    n = len(text)
    if n > _PROMOTE_MAXLEN:
        return None, "too-long"
    if n < _PROMOTE_MINLEN:
        return None, "too-short"
    for name, rx in _PROMOTE_REFUSALS:
        if rx.search(text):
            return None, name
    low = re.sub(r"\s+", " ", text.lower())   # newlines are whitespace, not
    marker = _promote_marker(low)             # clause boundaries — see _CLAUSE_SPLIT
    if not marker:
        return None, "no-marker"
    if not _marker_is_directive(low, marker):
        return None, "marker-not-directive"
    return marker, None


def _promote_cache_path():
    return os.path.join(registry.cache_root(), "promote-scan.json")


def _injected_prompt(d):
    """True when a role:user record is MACHINE-ORIGINATED — a task notification,
    a scheduled wake, any system-authored prompt. The harness records this:
    `isMeta: true`, `promptSource: "system"`, or `origin.kind` naming a
    non-human source; owner keystrokes carry promptSource typed/queued and
    origin.kind "human".

    MEASURED on the 43-entry junk cohort: 5 of them were machine traffic this
    predicate identifies exactly — 4 <task-notification> payloads and one
    autonomous fallback wake. FAIL-OPEN by design: older transcripts carry none
    of these fields, and treating absence as "injected" would silently stop
    promoting from every pre-2.1.176 session."""
    if d.get("isMeta") is True or d.get("promptSource") == "system":
        return True
    origin = d.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return bool(kind) and kind != "human"


def _user_texts(path):
    """Yield (line_no, text) for REAL user-typed messages in a harness jsonl —
    content str, or list TEXT blocks (tool_result/other blocks skipped, so tool
    output injected as role:user never counts), and machine-originated prompts
    dropped by _injected_prompt. Claude (type:user + message.role) and the
    generic role:user shape both parse. Fail-open."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                msg, role = d, d.get("role")
                if d.get("type") == "user" and isinstance(d.get("message"), dict):
                    msg = d["message"]
                    role = msg.get("role") or "user"
                if role != "user" or _injected_prompt(d):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                        and isinstance(b.get("text"), str))
                else:
                    text = ""
                text = text.strip()
                if text:
                    yield i, text
    except OSError:
        return  # unreadable transcript -> no user texts (fail-open)


def _jsonl_files(roots, since_days):
    cutoff = time.time() - max(since_days, 0) * 86400
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, _dn, fn in os.walk(root):
            for n in fn:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(dp, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    out.append((p, st.st_mtime, st.st_size))
    return sorted(out)


def _promotion_slug(text):
    kw = _keywords_from("", text)
    slug = "-".join([k for k in kw.split(",") if k][:4]) or text[:40]
    return pk.slug(slug)


def promote(since_days=7, cap=20, apply=False, roots=None, mem=None):
    """Scan recent USER messages for durable-knowledge markers and write each
    hit as a CANDIDATE file in the drain intake dir (never directly into the
    typed store — the existing classify->dry-run->apply gauntlet gates it).
    Deduped by keyword/token overlap against existing priors and already-written
    intake candidates; capped per run; incremental via a (mtime,size) scan cache
    (catalog-cache's pattern). Dry-run returns the plan untouched. -> receipt."""
    from . import store
    ts = pk.now_ts()
    mem = mem or _mem_dir()
    if roots is None:
        roots = [os.path.join(os.path.expanduser("~"), ".claude", "projects")]
    cache = pk.read_json(_promote_cache_path(), {}) or {}
    new_cache = dict(cache)
    existing = store.load_all(include_retired=True, types=("prior",))
    ex_slugs = {store._slug(str(e["id"])) for e in existing}
    ex_tokens = [t for t in (store._tokens(e.get("statement")) for e in existing) if t]
    planned, scanned, skipped_cache, hit_cap = {}, 0, 0, False
    refusals = {}   # gauntlet layer -> count, surfaced on the receipt
    for p, mtime, size in _jsonl_files(roots, since_days):
        if cache.get(p) == [mtime, size]:
            skipped_cache += 1
            continue
        scanned += 1
        sid = os.path.basename(p)[:-6]
        file_done = True
        for line_no, text in _user_texts(p):
            marker, refused = promote_verdict(text)
            if refused:
                refusals[refused] = refusals.get(refused, 0) + 1
                continue
            slug = _promotion_slug(text)
            if not slug or slug in planned or slug in ex_slugs:
                continue
            if os.path.exists(os.path.join(mem, "feedback-promoted-" + slug + ".md")):
                continue
            ctoks = store._tokens(text)
            if ctoks and any(len(ctoks & et) / len(ctoks | et) >= 0.6 for et in ex_tokens):
                continue
            planned[slug] = {"slug": slug, "statement": text[:_STATEMENT_CAP],
                             "marker": marker.strip(), "origin_session": sid,
                             "origin_line": line_no, "proposed_type": "prior",
                             "capture_confidence": 0.5}
            if len(planned) >= cap:
                hit_cap = file_done = False
                break
        if file_done:
            new_cache[p] = [mtime, size]
        if hit_cap:
            break
    acts = list(planned.values())
    receipt = {"ts": ts, "since_days": since_days, "scanned": scanned,
               "skipped_cache": skipped_cache, "found": len(acts),
               "written": 0, "actions": acts, "refused": refusals}
    if not apply:
        return receipt
    os.makedirs(mem, exist_ok=True)
    for a in acts:
        _write_promotion_candidate(mem, a, ts)
    pk.write_json(_promote_cache_path(), new_cache)
    receipt["written"] = len(acts)
    if acts:
        pk.event("drain.promote", "intake",
                 "%d promotion candidate%s -> intake (awaiting drain)"
                 % (len(acts), "s"[:len(acts) != 1]))
    return receipt


def _write_promotion_candidate(mem, a, ts):
    """One intake candidate file drain routes as feedback->prior. proposed_type,
    capture_confidence, origin_line + session ride the frontmatter for the
    operator's dry-run review; the body preserves the provenance line.

    `statement:` is written EXPLICITLY and is what classify() drains. The
    170-char `description:` beside it stays a human-readable summary for the
    operator's dry-run listing — it is no longer the field the durable
    statement is read out of."""
    slug = a["slug"]
    st = re.sub(r"\s+", " ", a["statement"].replace('"', "'"))[:_STATEMENT_CAP]
    body = [
        "---",
        "name: feedback-promoted-" + slug,
        'description: "' + st[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: feedback",
        "  statement: " + st,
        "  originSessionId: " + a["origin_session"],
        "  origin_line: " + str(a["origin_line"]),
        "  proposed_type: " + a["proposed_type"],
        "  capture_confidence: %.2f" % a["capture_confidence"],
        "  promoted_marker: " + a["marker"],
        "  promoted_ts: " + ts,
        "---",
        "",
        "Promoted from transcript %s line %s (USER message, marker: '%s')."
        % (a["origin_session"], a["origin_line"], a["marker"]),
        "",
        st,
        "",
    ]
    pk.atomic_write(os.path.join(mem, "feedback-promoted-" + slug + ".md"),
                    "\n".join(body))


def pending_promotions(mem=None):
    """Count of un-drained promotion candidates in the intake dir — the seam
    evolve reads for 'N promotion candidates awaiting drain'."""
    mem = mem or _mem_dir()
    try:
        return sum(1 for n in os.listdir(mem)
                   if n.startswith("feedback-promoted-") and n.endswith(".md"))
    except OSError:
        return 0


def cmd_promote(args):
    """promote [--since Nd] [--cap N] [--apply] — the episodic->durable funnel:
    scan recent USER messages for durable-knowledge markers, write each as a
    drain-intake candidate (dry-run default; drain then gates them)."""
    # membership reads ('--apply' in args) see no junk — the tail is guarded
    # BEFORE the scan/write: `promote --bogus --apply` used to WRITE intake
    # files and exit 0 with --help pretending the flag existed.
    from .cli import guard_tail
    rc = guard_tail("helm promote", args, flags=("--apply",),
                    valued=("--since", "--cap"),
                    usage="promote [--since Nd] [--cap N] [--apply]")
    if rc is not None:
        return rc
    since = 7
    if "--since" in args:
        try:
            since = int(str(args[args.index("--since") + 1]).rstrip("dD"))
        except (ValueError, IndexError):
            print("helm promote: --since needs Nd (e.g. --since 14d)", file=sys.stderr)
            return 2
    cap = 20
    if "--cap" in args:
        try:
            cap = int(args[args.index("--cap") + 1])
        except (ValueError, IndexError):
            print("helm promote: --cap needs an integer", file=sys.stderr)
            return 2
    apply = "--apply" in args
    r = promote(since_days=since, cap=cap, apply=apply)
    print("helm promote: scanned %d file%s (%d cached-skip), %d promotion candidate%s %s"
          % (r["scanned"], "s"[:r["scanned"] != 1], r["skipped_cache"],
             r["found"], "s"[:r["found"] != 1], "written" if apply else "found"))
    for a in r["actions"][:8]:
        print("  + %s [%s c=%.2f] (%s L%s): %s"
              % (a["slug"], a["proposed_type"], a["capture_confidence"],
                 a["origin_session"][:8], a["origin_line"], a["statement"][:70]))
    if len(r["actions"]) > 8:
        print("  ... %d more" % (len(r["actions"]) - 8))
    # WHY the rest were dropped. A gauntlet that refuses silently is how the
    # old one ran for weeks: `found 0` and `found 43` looked equally healthy.
    refused = r.get("refused") or {}
    if refused:
        print("  refused: %s" % ", ".join(
            "%s=%d" % kv for kv in sorted(refused.items(), key=lambda kv: -kv[1])))
    if not apply:
        print("helm promote: DRY-RUN (no intake files written). Re-run with "
              "--apply, then `helm drain` to route them.")
    elif r["written"]:
        print("helm promote: WROTE %d candidate%s to the drain intake — review "
              "with `helm drain`, land with `helm drain --apply`."
              % (r["written"], "s"[:r["written"] != 1]))
    return 0


def _repoint_index(mem, renames):
    """MEMORY.md is a projection — re-point lines whose link target moved."""
    idx = os.path.join(mem, "MEMORY.md")
    try:
        with open(idx, encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return 0
    hits = 0
    for old, new in renames.items():
        if "(" + old + ")" in raw:
            raw = raw.replace("(" + old + ")", "(" + new + ")")
            hits += 1
    if hits:
        pk.atomic_write(idx, raw)
    return hits


def _doable_actions(plan, sweep_dups=False, limit=None):
    doable = [a for a in plan if a["op"] in ("retype", "route-project", "upgrade")
              or (sweep_dups and a["op"] == "sweep-dup")]
    return doable[:limit] if limit else doable


def _semantic_keywords(a):
    """The probes the raw drain serializer would mint for one semantic action.

    An explicit action field wins even when empty: forwarded/hand-written plans
    are linted as written, not silently repaired with a derived fallback.
    Classify-created retypes carry no field and use drain's derivation."""
    if a.get("to_type") == "heuristic" and "trigger" in a:
        return a["trigger"]
    if "keywords" in a:
        return a["keywords"]
    return _keywords_from(a["id"], a.get("statement") or "")


def _staged_corpus_entry(a, mem, ts):
    """A guarded mint in the resolver shape needed by the next batch guard."""
    etype = a["to_type"]
    kw = a.get("trigger") if etype == "heuristic" else a.get("keywords")
    return {"type": etype, "id": a["id"], "path": os.path.join(mem, a["dst"]),
            "statement": a.get("statement") or "", "keywords": kw or "",
            "trigger": kw or "", "load_class": "jit", "status": "live",
            "confidence": DRAIN_CONFIDENCE if etype == "prior" else 1.0,
            "last_updated": ts}


def _prepare_actions(plan, mem, ts, sweep_dups=False, limit=None,
                     project=None, force_new=False):
    """Pure preflight: guard + serialize every mint before physical mutation.

    The caller owns one existing corpus and extends it with earlier staged mints,
    so same-batch duplicates refuse. Each action excludes only its exact source
    mapping: self cannot inflate DF/collide, while every other sibling remains.
    """
    from . import store
    doable = _doable_actions(plan, sweep_dups=sweep_dups, limit=limit)
    semantic = [a for a in doable if a["op"] in ("retype", "upgrade")]
    corpus = store.load_all(project=project, include_dormant=False,
                            types=store._JIT_TYPES) if semantic else []
    prepared, refused = [], []
    for a in doable:
        if a["op"] not in ("retype", "upgrade"):
            prepared.append(dict(a))
            continue
        b = dict(a)
        etype = b["to_type"]
        predecessor_paths = {os.path.join(mem, b["src"]),
                             os.path.join(mem, b["dst"])}
        exclusions = tuple({"type": etype, "id": b["id"], "path": p}
                           for p in predecessor_paths)
        kw, bad, notes, events = store.guard_entry_keywords(
            etype, b["id"], _semantic_keywords(b), project=project,
            force=force_new, corpus=corpus, exclusions=exclusions)
        if bad:
            r = dict(a)
            r.update({"op": "refuse", "guarded_op": a["op"], "why": bad})
            refused.append(r)
            continue
        b["keywords"] = kw
        if etype == "heuristic":
            b["trigger"] = kw
        src = os.path.join(mem, b["src"])
        serializer = _retype_text if b["op"] == "retype" else _upgrade_text
        b["_staged_text"] = serializer(src, b, ts)
        b["_mint_notes"] = notes
        b["_mint_events"] = events
        prepared.append(b)
        # The staged corpus represents POST-action state. Remove this accepted
        # action's exact predecessor before adding its replacement; otherwise a
        # later guard counts old+self as two documents and inflates DF. Match all
        # three identity axes so same-id/type siblings at other paths remain.
        predecessor_ids = {(etype, store._slug(str(b["id"])), p)
                           for p in predecessor_paths}
        corpus[:] = [e for e in corpus if (
            str(e.get("type") or ""), store._slug(str(e.get("id") or "")),
            str(e.get("path") or "")) not in predecessor_ids]
        corpus.append(_staged_corpus_entry(b, mem, ts))
    return prepared, refused


def _public_action(a):
    return {k: v for k, v in a.items() if not k.startswith("_")}


def apply(plan, mem=None, sweep_dups=False, limit=None, project=None,
          force_new=False):
    """Guard/serialize first, then archive + write; returns a truthful receipt."""
    mem = mem or _mem_dir()
    ts = pk.now_ts()
    doable, refused = _prepare_actions(
        plan, mem, ts, sweep_dups=sweep_dups, limit=limit,
        project=project, force_new=force_new)
    if not doable:
        return {"ts": ts, "applied": 0, "actions": [], "refused": refused,
                "note": "nothing to apply"}

    net = os.path.join(mem, "archive", "drain-" + ts.replace(":", "").replace("-", "")[:13])
    os.makedirs(net, exist_ok=True)
    # net every SOURCE and any pre-existing DESTINATION (a retype overwriting an
    # existing typed file must leave that file recoverable — classify() blocks
    # the common case, but a race or a hand-crafted plan must still be safe).
    to_net = {}
    for a in doable:
        to_net[a["src"]] = os.path.join(mem, a["src"])
        dst = a.get("dst")
        if a["op"] in ("retype", "upgrade") and dst and dst != a["src"] \
                and os.path.isfile(os.path.join(mem, dst)):
            # a subdir, not a name prefix: a src literally named
            # overwritten-<dst> must never collide with the netted dst
            os.makedirs(os.path.join(net, "overwritten"), exist_ok=True)
            to_net[os.path.join("overwritten", dst)] = os.path.join(mem, dst)
    for label, srcpath in to_net.items():
        shutil.copy2(srcpath, os.path.join(net, label))
    # the rollback net must hold every file, byte-identical, before ANY mutation
    for label, srcpath in to_net.items():
        cop = os.path.join(net, label)
        if not os.path.isfile(cop) or os.path.getsize(cop) != os.path.getsize(srcpath):
            raise RuntimeError("drain: rollback net incomplete for %s — ABORTING, "
                              "nothing mutated" % label)

    from . import store
    renames, applied, mint_notes = {}, [], []
    for a in doable:
        src = os.path.join(mem, a["src"])
        if a["op"] == "sweep-dup":
            os.remove(src)
            renames[a["src"]] = a["twin"]
        elif a["op"] == "retype":
            pk.atomic_write(os.path.join(mem, a["dst"]), a["_staged_text"])
            os.remove(src)
            renames[a["src"]] = a["dst"]
        elif a["op"] == "upgrade":
            pk.atomic_write(os.path.join(mem, a["dst"]), a["_staged_text"])
            if a["dst"] != a["src"]:   # prem- -> prior- rename; in-place keeps src==dst
                os.remove(src)
            renames[a["src"]] = a["dst"]
        elif a["op"] == "route-project":
            os.makedirs(os.path.dirname(a["dst"]), exist_ok=True)
            shutil.copy2(src, a["dst"])
            os.remove(src)
            renames[a["src"]] = a["dst"]
        # Duplicate-override receipts are transaction data: journal only after
        # this action's physical write/copy/delete completed successfully.
        store.record_mint_events(a.get("_mint_events") or [])
        if a.get("_mint_notes"):
            mint_notes.append({"src": a["src"], "notes": a["_mint_notes"]})
        applied.append(_public_action(a))
    repointed = _repoint_index(mem, renames)
    receipt = {"ts": ts, "applied": len(applied), "actions": applied,
               "refused": refused, "mint_notes": mint_notes,
               "index_lines_repointed": repointed, "net": net}
    pk.write_json(os.path.join(net, "RECEIPT.json"), receipt)
    pk.event("drain.apply", net,
             "%d action%s routed, %d refused, %d index lines re-pointed"
             % (len(applied), "s"[:len(applied) != 1], len(refused), repointed))
    return receipt


def cmd_drain(args):
    """drain [--apply] [--sweep-dups] [--force-new] [--limit N] [--project P]
    | drain --rekey
    [--apply] | drain --expire-candidates [--days N] [--apply] — classify raw
    memory entries and route them to typed homes; --project P drains that
    project's OWN claude memory dir (the adopted per-project pile) with the
    identical gauntlet; --rekey is the one-time drained-cohort keyword
    migration; --expire-candidates prunes unconfirmed candidates older than N
    days (14 default). Dry-run by default."""
    # tail guard before ANY routing work — cmd_drain reads flags only via
    # membership, so junk used to run the (mutating on --apply) router as if
    # the typo'd flag existed.
    from .cli import guard_tail
    rc = guard_tail("helm drain", args,
                    flags=("--apply", "--sweep-dups", "--force-new", "--rekey",
                           "--expire-candidates"),
                    valued=("--limit", "--project", "--days"),
                    usage="drain [--apply] [--sweep-dups] [--force-new] [--limit N] "
                          "[--project P] | drain --rekey [--apply] | drain "
                          "--expire-candidates [--days N] [--apply]")
    if rc is not None:
        return rc
    project = None
    if "--project" in args:
        i = args.index("--project")
        project = args[i + 1] if i + 1 < len(args) else None
        if not project:
            print("helm drain: --project needs a name", file=sys.stderr)
            return 2
    if "--rekey" in args:
        return _cmd_rekey(args)
    if "--expire-candidates" in args:
        return _cmd_expire_candidates(args, project=project)
    if project:
        mem = _project_mem_dir(project)
        if not mem:
            print("helm drain: no claude memory dir for project '%s' "
                  "(helm sync, or check `helm show %s`)" % (project, project))
            return 1
    else:
        mem = _mem_dir()
    if not os.path.isdir(mem):
        print("helm drain: no adopted memory dir at " + mem)
        return 1
    limit = None
    if "--limit" in args:
        try:
            limit = int(args[args.index("--limit") + 1])
        except (ValueError, IndexError):
            print("helm drain: --limit needs an integer", file=sys.stderr)
            return 2
    plan = classify(mem)
    if "--apply" not in args:
        _prepared, refused = _prepare_actions(
            plan, mem, pk.now_ts(), sweep_dups="--sweep-dups" in args,
            limit=limit, project=project, force_new="--force-new" in args)
        by_src = {a["src"]: a for a in refused}
        plan = [by_src.get(a["src"], a) for a in plan]
    by_op = {}
    for a in plan:
        by_op.setdefault(a["op"], []).append(a)
    print("helm drain: plan (%d raw entries%s):"
          % (len(plan), (" — project " + project + " @ " + mem) if project else ""))
    # per op: (rows shown, "... more" threshold); everything else defaults (3, 3)
    show_limit = {"refuse": (5, 5), "conflict": (5, 5), "keep": (2, 3)}
    for op in ("retype", "upgrade", "route-project", "sweep-dup", "refuse",
               "conflict", "keep"):
        acts = by_op.get(op, [])
        if not acts:
            continue
        shown, more_at = show_limit.get(op, (3, 3))
        print("  %-14s %d" % (op, len(acts)))
        for a in acts[:shown]:
            tgt = a.get("why", "") if op == "refuse" else \
                (a.get("dst") or a.get("twin") or a.get("why", ""))
            print("      %s -> %s" % (a["src"], tgt))
        if len(acts) > more_at:
            print("      ... %d more" % (len(acts) - more_at))
    if by_op.get("refuse"):
        print("  note: %d semantic mint%s REFUSED by the store findability guard; "
              "source%s stay in intake" % (
                  len(by_op["refuse"]), "s"[:len(by_op["refuse"]) != 1],
                  "s"[:len(by_op["refuse"]) != 1]))
    if by_op.get("conflict"):
        print("  note: %d conflict%s NOT auto-drained (would overwrite a curated "
              "entry or collide) — resolve by hand" % (
                  len(by_op["conflict"]), "s"[:len(by_op["conflict"]) != 1]))
    if by_op.get("sweep-dup") and "--sweep-dups" not in args:
        print("  note: %d prem/prior twin duplicates need --sweep-dups "
              "(removes canon-store files; safe per read-time dedup — "
              "operator-gated by design)" % len(by_op["sweep-dup"]))
    if "--apply" not in args:
        print("helm drain: DRY-RUN (nothing moved). Re-run with --apply%s." %
              (" [--sweep-dups]" if by_op.get("sweep-dup") else ""))
        return 0
    receipt = apply(plan, mem, sweep_dups="--sweep-dups" in args, limit=limit,
                    project=project, force_new="--force-new" in args)
    if receipt.get("net"):
        print("helm drain: APPLIED %d action%s; REFUSED %d; net + receipt at %s; "
              "%d index lines re-pointed"
              % (receipt["applied"], "s"[:receipt["applied"] != 1],
                 len(receipt.get("refused") or []), receipt["net"],
                 receipt.get("index_lines_repointed", 0)))
    else:
        print("helm drain: APPLIED 0 actions; REFUSED %d; no files written"
              % len(receipt.get("refused") or []))
    for group in receipt.get("mint_notes") or []:
        for note in group["notes"]:
            print(note)
    return 0


if __name__ == "__main__":
    sys.exit(cmd_drain(sys.argv[1:]))
