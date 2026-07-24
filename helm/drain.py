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
                "originsessionid": ""}

TYPED_PREFIXES = ("prior-", "lex-", "heuristic-", "ref-", "reflex-")
DRAIN_CONFIDENCE = 0.9  # human feedback is strong evidence, not certainty

# Built-in alias map: bulk global entries can name a project by a short/old
# handle the registry knows under a different canonical name. Per-project
# authored `aliases` (registry AUTHORED_FIELDS) are folded in on top when present
# (registry.py is another lane's — we only READ). No built-in defaults ship.
_BUILTIN_ALIASES = {}


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


def _alias_map(reg):
    """alias(lowercased) -> canonical project name. Built-ins plus every
    project record's authored `aliases` list."""
    m = dict(_BUILTIN_ALIASES)
    for name, rec in (reg.get("projects") or {}).items():
        for a in rec.get("aliases") or []:
            if str(a).strip():
                m[str(a).strip().lower()] = name
    return m


def _route_target(base, blob, project_names, aliases):
    """The project an entry routes to, canonical names first (filename prefix or
    exact, then a specific >=6-char description word), aliases second — a short
    alias matches by filename only (a 2-char word wallpapers)."""
    low = base.lower()
    for pn in project_names:
        if low.startswith(pn.lower() + "-") or low == pn.lower():
            return pn
    for alias, canon in aliases.items():
        if canon in project_names and (low.startswith(alias + "-") or low == alias):
            return canon
    for pn in project_names:
        if len(pn) >= 6 and re.search(
                r"(?<![a-z0-9])" + re.escape(pn.lower()) + r"(?![a-z0-9])", blob):
            return pn
    for alias, canon in aliases.items():
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
    return {"op": "retype", "src": src, "to_type": to_type, "dst": dst,
            "id": slug, "statement": e.get("description") or "",
            "origin": e.get("originsessionid") or ""}


def classify(mem=None):
    """-> list of action dicts. Pure derivation, no writes."""
    mem = mem or _mem_dir()
    names = set(os.listdir(mem)) if os.path.isdir(mem) else set()
    reg = registry.load()
    project_names = sorted(reg["projects"], key=len, reverse=True)
    aliases = _alias_map(reg)
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
                plan.append({"op": "keep", "src": n,
                             "why": "project entry, no registry match"})
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
    st = re.sub(r"\s+", " ", (act["statement"] or act["id"]).replace('"', "'"))[:300]
    to_prior = act["to_type"] == "prior"
    kind = "prior" if to_prior else "reference"
    fm = [
        "---",
        "name: " + act["dst"][:-3],
        'description: "' + kind + ": " + (act["id"] + " - " + st)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: " + kind,
        "  id: " + act["id"],
        "  statement: " + st,
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
    fm += ["  status: live",
           "  keywords: " + _keywords_from(act["id"], act["statement"] or ""),
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
    st = re.sub(r"\s+", " ", (act["statement"] or act["id"]).replace('"', "'"))[:300]
    kw = act.get("keywords") or _keywords_from(act["id"], act["statement"] or "")
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
_PROMOTE_MARKERS = ("from now on", "remember this", "remember that",
                    "make it a rule", "going forward", "the rule is",
                    "always ", "never ")
_PROMOTE_MAXLEN = 600  # a durable rule is a sentence, not an essay/task prompt


def _promote_cache_path():
    return os.path.join(registry.cache_root(), "promote-scan.json")


def _user_texts(path):
    """Yield (line_no, text) for REAL user-typed messages in a harness jsonl —
    content str, or list TEXT blocks (tool_result/other blocks skipped, so tool
    output injected as role:user never counts). Claude (type:user +
    message.role) and the generic role:user shape both parse. Fail-open."""
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
                if role != "user":
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
    for p, mtime, size in _jsonl_files(roots, since_days):
        if cache.get(p) == [mtime, size]:
            skipped_cache += 1
            continue
        scanned += 1
        sid = os.path.basename(p)[:-6]
        file_done = True
        for line_no, text in _user_texts(p):
            if len(text) > _PROMOTE_MAXLEN:
                continue
            low = text.lower()
            marker = next((m for m in _PROMOTE_MARKERS if m in low), None)
            if not marker:
                continue
            slug = _promotion_slug(text)
            if not slug or slug in planned or slug in ex_slugs:
                continue
            if os.path.exists(os.path.join(mem, "feedback-promoted-" + slug + ".md")):
                continue
            ctoks = store._tokens(text)
            if ctoks and any(len(ctoks & et) / len(ctoks | et) >= 0.6 for et in ex_tokens):
                continue
            planned[slug] = {"slug": slug, "statement": text[:300],
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
               "written": 0, "actions": acts}
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
    operator's dry-run review; the body preserves the provenance line."""
    slug = a["slug"]
    st = re.sub(r"\s+", " ", a["statement"].replace('"', "'"))[:300]
    body = [
        "---",
        "name: feedback-promoted-" + slug,
        'description: "' + st[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: feedback",
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


def apply(plan, mem=None, sweep_dups=False, limit=None):
    """Execute a plan. Archive-first with a verified net; returns the receipt."""
    mem = mem or _mem_dir()
    ts = pk.now_ts()
    doable = [a for a in plan if a["op"] in ("retype", "route-project", "upgrade")
              or (sweep_dups and a["op"] == "sweep-dup")]
    if limit:
        doable = doable[:limit]
    if not doable:
        return {"ts": ts, "applied": 0, "note": "nothing to apply"}

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

    renames = {}
    applied = []
    for a in doable:
        src = os.path.join(mem, a["src"])
        if a["op"] == "sweep-dup":
            os.remove(src)
            renames[a["src"]] = a["twin"]
        elif a["op"] == "retype":
            pk.atomic_write(os.path.join(mem, a["dst"]), _retype_text(src, a, ts))
            os.remove(src)
            renames[a["src"]] = a["dst"]
        elif a["op"] == "upgrade":
            pk.atomic_write(os.path.join(mem, a["dst"]), _upgrade_text(src, a, ts))
            if a["dst"] != a["src"]:   # prem- -> prior- rename; in-place keeps src==dst
                os.remove(src)
            renames[a["src"]] = a["dst"]
        elif a["op"] == "route-project":
            os.makedirs(os.path.dirname(a["dst"]), exist_ok=True)
            shutil.copy2(src, a["dst"])
            os.remove(src)
            renames[a["src"]] = a["dst"]
        applied.append(a)
    repointed = _repoint_index(mem, renames)
    receipt = {"ts": ts, "applied": len(applied), "actions": applied,
               "index_lines_repointed": repointed, "net": net}
    pk.write_json(os.path.join(net, "RECEIPT.json"), receipt)
    pk.event("drain.apply", net, "%d action%s routed, %d index lines re-pointed"
             % (len(applied), "s"[:len(applied) != 1], repointed))
    return receipt


def cmd_drain(args):
    """drain [--apply] [--sweep-dups] [--limit N] [--project P] | drain --rekey
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
                    flags=("--apply", "--sweep-dups", "--rekey",
                           "--expire-candidates"),
                    valued=("--limit", "--project", "--days"),
                    usage="drain [--apply] [--sweep-dups] [--limit N] "
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
    plan = classify(mem)
    by_op = {}
    for a in plan:
        by_op.setdefault(a["op"], []).append(a)
    print("helm drain plan (%d raw entries%s):"
          % (len(plan), (" — project " + project + " @ " + mem) if project else ""))
    # per op: (rows shown, "... more" threshold); everything else defaults (3, 3)
    show_limit = {"conflict": (5, 5), "keep": (2, 3)}
    for op in ("retype", "upgrade", "route-project", "sweep-dup", "conflict", "keep"):
        acts = by_op.get(op, [])
        if not acts:
            continue
        shown, more_at = show_limit.get(op, (3, 3))
        print("  %-14s %d" % (op, len(acts)))
        for a in acts[:shown]:
            tgt = a.get("dst") or a.get("twin") or a.get("why", "")
            print("      %s -> %s" % (a["src"], tgt))
        if len(acts) > more_at:
            print("      ... %d more" % (len(acts) - more_at))
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
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    receipt = apply(plan, mem, sweep_dups="--sweep-dups" in args, limit=limit)
    print("helm drain: APPLIED %d action%s; net + receipt at %s; "
          "%d index lines re-pointed"
          % (receipt["applied"], "s"[:receipt["applied"] != 1],
             receipt.get("net", "-"), receipt.get("index_lines_repointed", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(cmd_drain(sys.argv[1:]))
