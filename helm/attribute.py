#!/usr/bin/env python3
"""helm attribute — historical token-effort attribution.

The owner's HR-capacity frame: how much effort (in tokens) has a project /
model / cred consumed, so cred homing can follow need. The session catalog
carries no token field and tokens live PER-MESSAGE, so this is a BOUNDED
on-demand rollup: it takes a scoped session set from the catalog helm already
maintains (--since / --limit / --project), sums the effort measure per
transcript, and groups by one dimension. The bound is always printed — a
scoped result never masquerades as a whole-corpus total.

EFFORT MEASURE = output_tokens + cache_creation_input_tokens for claude (the
billable-work proxy: generated tokens + freshly-cached context; localscan.py
owns the parse) and last_token_usage.output_tokens summed per token_count
event for codex (rollouts report no cache-creation figure). It deliberately
EXCLUDES raw input_tokens — per turn that is the full CUMULATIVE context
re-sent, so summing it across a session double-counts massively.

CRED attribution is a PATH-BOUNDARY ancestor match against the accounts'
"-homes/" dirs only. helm claude homes all symlink onto ONE shared session
store (~/.claude/projects — the homes.py canon), which is not cred-specific,
so claude sessions stay UNATTRIBUTED rather than being guessed onto whichever
account is active now; codex rollouts under ~/.codex-homes/<name>/ attribute
cleanly. The UNATTRIBUTED bucket is always visible — a coverage gap never
hides inside an attributed total.
"""
import json
import os
import sys
import time

from . import localscan

DIMENSIONS = ("project", "model", "cred")


def _scan_claude(path, seen):
    """(effort, dominant model) over one claude transcript. `seen` is the
    CROSS-FILE uuid set: a pruned resume copy shares its original's record
    uuids, so its lines count once, never twice."""
    effort, models = 0, {}
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                if '"assistant"' not in line:  # cheap pre-filter, parse less
                    continue
                r = localscan.parse_record(line)
                if r is None or (r["uuid"] and r["uuid"] in seen):
                    continue
                if r["uuid"]:
                    seen.add(r["uuid"])
                effort += r["effort"]
                if r["model"]:
                    models[r["model"]] = models.get(r["model"], 0) + 1
    except OSError:
        pass
    return effort, (max(models, key=models.get) if models else "unknown")


def _scan_codex(path):
    """(effort, dominant model) over one codex rollout: sum each token_count
    event's last_token_usage.output_tokens (the per-request figure — summing
    the cumulative total_token_usage would double-count); model from the
    turn_context lines."""
    effort, models = 0, {}
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                if '"token_count"' in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    info = (r.get("payload") or {}).get("info") or {}
                    v = (info.get("last_token_usage") or {}).get("output_tokens")
                    effort += int(v) if isinstance(v, (int, float)) else 0
                elif '"turn_context"' in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    m = (r.get("payload") or {}).get("model")
                    if isinstance(m, str) and m:
                        models[m] = models.get(m, 0) + 1
    except OSError:
        pass
    return effort, (max(models, key=models.get) if models else "unknown")


def cred_for(path, accounts):
    """`provider:account` for the "-homes/" cred home the transcript lives
    under — a PATH-BOUNDARY (component-wise) ancestor match, never a
    substring, so home ".../a" can never capture a session under ".../abc";
    the longest matching home (most specific ancestor) wins. The shared and
    default stores are not cred-specific and are never guessed -> None."""
    p = os.path.realpath(path)
    best = None
    for a in accounts:
        home, name = a.get("home"), a.get("name") or a.get("account")
        if not home or not name or "-homes/" not in home:
            continue
        h = os.path.realpath(home)
        if (p == h or p.startswith(h + os.sep)) and (best is None or len(h) > best[0]):
            best = (len(h), "%s:%s" % (a.get("provider") or "?", name))
    return best[1] if best else None


def gather(rows, accounts, lens):
    """Catalog rows -> [{project, model, cred, effort}] — one transcript scan
    each. Pure over its inputs (rows carry the paths)."""
    from .sessions import _project_for
    seen = set()
    out = []
    for r in rows:
        path, harness = r.get("p"), r.get("h")
        if not path or harness not in ("claude", "codex") or not os.path.exists(path):
            continue
        effort, model = (_scan_claude(path, seen) if harness == "claude"
                         else _scan_codex(path))
        cwd = r.get("cwd") or r.get("c") or ""
        out.append({"project": _project_for(cwd, lens) or cwd or "(no cwd)",
                    "model": model, "cred": cred_for(path, accounts),
                    "effort": effort})
    return out


def rollup(efforts, by):
    """Group by one dimension and sum, descending by effort (key breaks ties
    for stable output). A missing cred renders as UNATTRIBUTED — that bucket
    is always visible."""
    agg = {}
    for e in efforts:
        key = (e["cred"] or "UNATTRIBUTED") if by == "cred" else e[by]
        slot = agg.setdefault(key, [0, 0])
        slot[0] += e["effort"]
        slot[1] += 1
    rows = [{"key": k, "effort_tokens": v[0], "sessions": v[1]}
            for k, v in agg.items()]
    rows.sort(key=lambda r: (-r["effort_tokens"], r["key"]))
    return rows


def _catalog_rows():
    from . import transcripts
    return transcripts.get_catalog()["rows"]


def _lens():
    from . import sessions
    return sessions._project_lens()


def _accounts():
    """Provider accounts for cred mapping; no provider -> [] (everything
    lands UNATTRIBUTED, the rollup still answers)."""
    from .providers import default_provider
    try:
        return default_provider().accounts()
    except Exception:
        return []


def _parse_since(s):
    s = (s or "").strip().lower()
    try:
        if s.endswith("d"):
            return float(s[:-1]) * 86400
        if s.endswith("h"):
            return float(s[:-1]) * 3600
        return float(s) * 86400
    except ValueError:
        return None


def cmd_attribute(args):
    """attribute [--by project|model|cred] [--since Nd|Nh] [--limit N]
    [--project P] [--json] — token-effort rollup over the session catalog."""
    args = list(args)
    # flags-only membership reader — guard the tail before the catalog scan:
    # `attribute --bogus` silently printed the rollup and exited 0.
    from .cli import guard_tail
    rc = guard_tail("helm attribute", args, flags=("--json",),
                    valued=("--by", "--since", "--limit", "--project"),
                    usage="attribute [--by project|model|cred] [--since Nd|Nh] "
                          "[--limit N] [--project P] [--json]")
    if rc is not None:
        return rc
    opt = {"--by": "project", "--since": "7d", "--limit": "200", "--project": None}
    for name in list(opt):
        if name in args:
            i = args.index(name)
            if i + 1 >= len(args):
                print("helm attribute: %s needs a value" % name, file=sys.stderr)
                return 2
            opt[name] = args[i + 1]
            del args[i:i + 2]
    by = opt["--by"]
    if by not in DIMENSIONS:
        print("helm attribute: --by must be one of %s" % "|".join(DIMENSIONS),
              file=sys.stderr)
        return 2
    since_s = _parse_since(opt["--since"])
    if since_s is None:
        print("helm attribute: bad --since %r (want e.g. 7d, 24h)" % opt["--since"],
              file=sys.stderr)
        return 2
    limit = int(opt["--limit"])
    lens = _lens()
    floor = time.time() - since_s
    rows = [r for r in _catalog_rows() if (r.get("mt") or 0) >= floor]
    if opt["--project"]:
        from .sessions import _project_for
        rows = [r for r in rows
                if _project_for(r.get("cwd") or r.get("c") or "", lens) == opt["--project"]]
    rows = rows[:limit]  # catalog rows arrive newest-first
    efforts = gather(rows, _accounts(), lens)
    table = rollup(efforts, by)
    if "--json" in args:
        print(json.dumps({"by": by, "since": opt["--since"], "limit": limit,
                          "sessions": len(efforts), "rows": table}, indent=2))
        return 0
    if not table:
        print("helm attribute: no sessions in the last %s." % opt["--since"])
        return 0
    scope = " project %s," % opt["--project"] if opt["--project"] else ""
    print("helm attribute — effort by %s,%s %d sessions (last %s, limit %d; "
          "measure: output + cache-creation tokens, never raw input)"
          % (by, scope, len(efforts), opt["--since"], limit))
    width = max(len(r["key"]) for r in table)
    for r in table:
        print("  %-*s  %8s  %d session%s" % (
            width, r["key"], localscan.fmt_tokens(r["effort_tokens"]),
            r["sessions"], "s"[:r["sessions"] != 1]))
    return 0
