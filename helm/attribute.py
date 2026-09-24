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

PROXY-FAMILY SEATS (codex, kimi, gemini, grok…) attribute through a second
source: the sidecar meter (helm/proxy_usage.py). Every request a seat's own
CLIProxyAPI sidecar forwarded is a ledger row naming the seat, the model and
the pooled account that spent it, with the input and output totals the
proxy counted. Those rows join the same rollup: `--by seat` groups on the
sidecar, `--by cred` on `<family>:<account email>`, and INPUT is reported
beside effort because a retained reader re-sends its whole context on every
call, so input is the meter that moves on a pooled account. A seat whose
sidecar could not be read is never a zero: the row (or the trailer under
the table) says UNREADABLE and why.
"""
import json
import os
import sys
import time

from . import localscan

DIMENSIONS = ("project", "model", "cred", "seat")
SIDECAR_PROJECT = "(sidecar)"


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
                    "seat": None, "effort": effort, "input": 0, "requests": 0})
    return out


def proxy_efforts(floor):
    """The sidecar meter's rows in the window -> ([{project, model, cred,
    seat, effort, input, requests}], {seat: unread reason}). effort is the
    proxy's OUTPUT total (the same measure a codex rollout gives), input its
    INPUT total; cred is `<family>:<account>` off the record's `source`, the
    pooled account that request spent, and None when the producer named
    none. The second value names every seat whose LAST read was UNREADABLE,
    so the rollup can say so instead of showing nothing."""
    from . import proxy_usage
    requests, last_read, unknown = proxy_usage.events(since=floor)
    out = []
    for ev in requests:
        inp, outp = proxy_usage.tokens_of(ev)
        account = ev.get("source") or None
        out.append({"project": SIDECAR_PROJECT,
                    "model": ev.get("alias") or ev.get("model") or "unknown",
                    "cred": "%s:%s" % (ev.get("family"), account) if account else None,
                    "seat": ev.get("seat"), "effort": outp, "input": inp,
                    "requests": 1})
    unread = {seat: ev.get("reason") or "unreadable"
              for seat, ev in last_read.items()
              if ev.get("status") == proxy_usage.UNREADABLE}
    # A SEAT WHOSE LATEST MARKER IS FAILED-PERSIST HAS A PARTIAL TOTAL: the
    # rows that reached the ledger are real and are counted, the ones the
    # ledger refused are gone, so the seat's row must say so and the rollup
    # is incomplete. The LATEST marker decides — a later READ marker means
    # the pass after the loss landed whole, and the loss was already told.
    partial = {seat: (ev.get("records", 0) - (ev.get("persisted") or 0),
                      ev.get("reason") or "records not persisted")
               for seat, ev in last_read.items()
               if ev.get("status") == proxy_usage.FAILED_PERSIST}
    return out, unread, unknown, partial


def rollup(efforts, by, unread=None, partial=None):
    """Group by one dimension and sum, descending by input + effort (key
    breaks ties for stable output). A missing cred or seat renders as
    UNATTRIBUTED — that bucket is always visible. `unread` ({seat: reason})
    adds a ZERO row per unread seat under `--by seat`, carrying the reason as
    its note, so an unreadable sidecar is a row that says why and never an
    absence. `partial` ({seat: (lost, reason)}) marks the seat's row PARTIAL
    under `--by seat` — on the row that carries its counted requests, or on
    a zero row when none reached the ledger — so a partial total never
    reads as a whole one."""
    agg = {}
    for e in efforts:
        key = (e.get(by) or "UNATTRIBUTED") if by in ("cred", "seat") else e[by]
        slot = agg.setdefault(key, [0, 0, 0, 0])
        slot[0] += e["effort"]
        slot[1] += 1 if not e.get("requests") else 0
        slot[2] += e.get("input") or 0
        slot[3] += e.get("requests") or 0
    rows = [{"key": k, "effort_tokens": v[0], "sessions": v[1],
             "input_tokens": v[2], "requests": v[3], "note": None}
            for k, v in agg.items()]
    if by == "seat":
        for seat, reason in sorted((unread or {}).items()):
            if seat not in agg:
                rows.append({"key": seat, "effort_tokens": 0, "sessions": 0,
                             "input_tokens": 0, "requests": 0,
                             "note": "UNREADABLE — %s" % reason})
        by_key = {r["key"]: r for r in rows}
        for seat, (lost, reason) in sorted((partial or {}).items()):
            note = "PARTIAL — %d record(s) lost — %s" % (lost, reason)
            if seat in by_key:
                by_key[seat]["note"] = note
            else:
                rows.append({"key": seat, "effort_tokens": 0, "sessions": 0,
                             "input_tokens": 0, "requests": 0, "note": note})
    rows.sort(key=lambda r: (-(r["effort_tokens"] + r["input_tokens"]), r["key"]))
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


def _proxy_efforts(floor):
    """(rows, unread seats, unknown reason). The third value is the meter's
    UNKNOWN channel: a ledger that could not be read whole makes the sidecar
    totals INCOMPLETE and every surface says so beside the number. A reader
    that raises is the same fact in a different register — the session
    rollup still answers, the meter reads UNREADABLE."""
    try:
        return proxy_efforts(floor)
    except Exception as exc:                # noqa: BLE001 — a rollup never raises
        return [], {}, "ledger unreadable: %s: %s" % (type(exc).__name__, exc), {}


def cmd_attribute(args):
    """attribute [--by project|model|cred|seat] [--since Nd|Nh] [--limit N]
    [--project P] [--json] — token-effort rollup over the session catalog
    and the sidecar meter."""
    args = list(args)
    # flags-only membership reader — guard the tail before the catalog scan:
    # `attribute --bogus` silently printed the rollup and exited 0.
    from .cli import guard_tail
    rc = guard_tail("helm attribute", args, flags=("--json",),
                    valued=("--by", "--since", "--limit", "--project"),
                    usage="attribute [--by project|model|cred|seat] [--since Nd|Nh] "
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
    # THE SIDECAR METER JOINS UNSCOPED BY PROJECT: a proxy record names a seat
    # and an account, never a cwd, so `--project` (a session filter) leaves
    # it out rather than guessing a project onto it.
    proxied, unread, unknown, partial = ([], {}, None, {}) if opt["--project"] \
        else _proxy_efforts(floor)
    table = rollup(efforts + proxied, by, unread, partial)
    # ONE INCOMPLETENESS CHANNEL: a ledger that could not be read whole and a
    # seat whose last pass lost records are the same fact to a reader of the
    # total — it is not the whole spend — so both land in `meter_unknown`.
    # THE TRAILER WORD FOLLOWS THE MARKER STATUS: UNREADABLE only when the
    # ledger itself could not be read whole; PARTIAL when the ledger read
    # fine and a seat's latest marker says records were lost.
    meter_word = "UNREADABLE" if unknown else ("PARTIAL" if partial else None)
    partial_text = "; ".join(
        "%s FAILED-PERSIST — %d record(s) lost — %s" % (seat, lost, reason)
        for seat, (lost, reason) in sorted(partial.items()))
    unknown = "; ".join(s for s in (unknown, partial_text) if s) or None
    if "--json" in args:
        print(json.dumps({"by": by, "since": opt["--since"], "limit": limit,
                          "sessions": len(efforts), "requests": len(proxied),
                          "unread_seats": unread, "meter_unknown": unknown,
                          "incomplete": unknown is not None,
                          "rows": table}, indent=2))
        return 0
    if not table:
        print("helm attribute: no sessions or sidecar requests in the last %s."
              % opt["--since"])
        if unknown:
            print("  sidecar meter: %s — %s — the sidecar total is "
                  "INCOMPLETE" % (meter_word, unknown))
        return 0
    scope = " project %s," % opt["--project"] if opt["--project"] else ""
    print("helm attribute — effort by %s,%s %d sessions + %d sidecar requests "
          "(last %s, limit %d; measure: output + cache-creation tokens per "
          "session, never raw input; output and INPUT per sidecar request)"
          % (by, scope, len(efforts), len(proxied), opt["--since"], limit))
    width = max(len(r["key"]) for r in table)
    for r in table:
        parts = ["%d session%s" % (r["sessions"], "s"[:r["sessions"] != 1])] \
            if r["sessions"] or not r["requests"] else []
        if r["requests"]:
            parts.append("%d request%s" % (r["requests"], "s"[:r["requests"] != 1]))
        if r["note"]:
            parts.append(r["note"])
        print("  %-*s  %8s  in %8s  %s" % (
            width, r["key"], localscan.fmt_tokens(r["effort_tokens"]),
            localscan.fmt_tokens(r["input_tokens"]), ", ".join(parts)))
    if unread and by != "seat":
        for seat, reason in sorted(unread.items()):
            print("  sidecar unread: %s — UNREADABLE — %s" % (seat, reason))
    if unknown:
        print("  sidecar meter: %s — %s — the sidecar totals above are "
              "INCOMPLETE" % (meter_word, unknown))
    return 0
