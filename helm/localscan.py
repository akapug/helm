#!/usr/bin/env python3
"""helm creds crosscheck — the LOCAL-SESSION-SCAN second source.

The AUTHORITATIVE usage read is the vendor's own usage endpoint (the header
probe in providers.py — server truth, live even on 429s). This module adds a
PASSIVE, zero-cost SECOND source that mirrors what claude's /usage panel
computes ("Approximate, based on local sessions on this machine"): it scans a
session store's JSONL and sums per-record work into the SAME rolling windows
the headers report — the 5h session window, the 7d weekly window, and the 7d
Fable-model sub-window.

It does NOT replace the header probe. Its purpose is CROSS-CHECK: when the
scan-derived usage and the header-reported utilization diverge, that drift is
a HEALTH SIGNAL (an incomplete local store, usage from another machine, a
provider accounting change) — reported, never fatal. The header stays the
decision-point read; the scan is observational.

MEASURE = output_tokens + cache_creation_input_tokens per assistant record —
the billable-work proxy: raw input_tokens is the full CUMULATIVE context
re-sent each turn, so summing it across a session double-counts massively.

STORE, not account: helm claude homes symlink projects/ to ONE shared store
(~/.claude/projects — the homes.py canon) and session records carry no account
field, so a shared store's sums are COMMINGLED across every account that ran
there and are NOT per-account attributable. Accounts group by the CANONICAL
projects path; a shared store yields one commingled row (which never
masquerades as an individual cross-check), a truly isolated store DOES
cross-check its one account's header. Token SUMS, not percentages: turning a
sum into a percentage needs the plan limit, which only the header knows —
so the drift verdict compares ACTIVITY direction, never invents a limit.
"""
import glob
import json
import os
import time

from .providers import _epoch

SESSION_SECS = 5 * 3600
WEEKLY_SECS = 7 * 86400


def parse_record(line):
    """One claude-store JSONL line -> {ts, model, fable, effort, uuid} for a
    usage-bearing assistant record, else None. Pure over the line text."""
    try:
        r = json.loads(line)
    except ValueError:
        return None
    if not isinstance(r, dict) or r.get("type") != "assistant":
        return None
    ts = _epoch(r.get("timestamp") or "")
    if ts is None:
        return None
    msg = r.get("message") if isinstance(r.get("message"), dict) else None
    if msg is None:
        return None
    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    num = lambda k: usage.get(k) if isinstance(usage.get(k), (int, float)) else 0
    model = msg.get("model") if isinstance(msg.get("model"), str) else ""
    uuid = r.get("uuid") if isinstance(r.get("uuid"), str) else None
    return {"ts": ts, "model": model, "fable": "fable" in model.lower(),
            "effort": int(num("output_tokens") + num("cache_creation_input_tokens")),
            "uuid": uuid}


def sum_windows(records, now):
    """Bucket records into the rolling windows relative to `now`. PURE — no
    clock, no filesystem — so window boundaries are unit-tested with fixed
    inputs. Future-timestamped records (clock skew) are skipped rather than
    counted into every window."""
    w = {"session_5h": 0, "weekly_7d": 0, "fable_7d": 0, "records": 0}
    for r in records:
        if r["ts"] > now or r["ts"] < now - WEEKLY_SECS:
            continue
        w["weekly_7d"] += r["effort"]
        w["records"] += 1
        if r["fable"]:
            w["fable_7d"] += r["effort"]
        if r["ts"] >= now - SESSION_SECS:
            w["session_5h"] += r["effort"]
    return w


def scan_projects(projects, now):
    """(window sums, newest record ts | None) over every recent session JSONL
    under a `projects/*/` store. mtime-bounded: a file untouched within the
    weekly window cannot hold an in-window record, so it is skipped entirely
    (a heavy store never forces a whole-corpus walk). uuid-deduped so a
    doubly-read line — a pruned resume copy sharing its original's uuids —
    is never counted twice."""
    cut = now - WEEKLY_SECS
    recs, seen, newest = [], set(), None
    for f in glob.glob(os.path.join(projects, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < cut:
                continue
            with open(f, errors="ignore") as fh:
                for line in fh:
                    r = parse_record(line)
                    if r is None or (r["uuid"] and r["uuid"] in seen):
                        continue
                    if r["uuid"]:
                        seen.add(r["uuid"])
                    newest = r["ts"] if newest is None else max(newest, r["ts"])
                    recs.append(r)
        except OSError:
            continue
    return sum_windows(recs, now), newest


def scan_stores(accounts, now):
    """Group ANTHROPIC accounts by the canonical projects path behind their
    homes and scan each DISTINCT store exactly once. shared (>1 account) ⇒
    the sums are a commingled aggregate, not any one account's usage.
    First-appearance order, so output is deterministic."""
    groups = []
    for a in accounts:
        name = a.get("name") or a.get("account")
        if a.get("provider") != "anthropic" or not a.get("home") or not name:
            continue
        key = os.path.realpath(os.path.join(a["home"], "projects"))
        for k, names in groups:
            if k == key:
                names.append(name)
                break
        else:
            groups.append((key, [name]))
    out = []
    for store, names in groups:
        sums, newest = scan_projects(store, now)
        out.append(dict(sums, store=store, accounts=names,
                        shared=len(names) > 1, newest=newest))
    return out


def crosscheck(prov=None, now=None):
    """The join: scan rows + the freshest header observation per account + a
    drift SIGNAL per isolated store. Probes once (cred_state) so the header
    side is live; the signal compares activity direction only (a 5h header
    window is anchored at first-message, the scan window trails — magnitudes
    legitimately differ, direction should not)."""
    from .providers import default_provider
    prov = prov or default_provider()
    now = time.time() if now is None else now
    prov.cred_state()  # freshen the header truth (appends to burn history)
    latest = {}
    for r in prov.history(1):
        a = r.get("account")
        if a and (a not in latest
                  or r.get("probed_at", "") > latest[a].get("probed_at", "")):
            latest[a] = r
    rows = scan_stores(prov.accounts(), now)
    for row in rows:
        if row["shared"]:
            row["signal"] = ("commingled — %d accounts share this store; not a "
                             "per-account cross-check" % len(row["accounts"]))
            continue
        gauges = (latest.get(row["accounts"][0]) or {}).get("gauges") or []
        util = lambda pred: next((g.get("utilization") for g in gauges if pred(g)), None)
        h5 = util(lambda g: g.get("label") == "5h")
        row["header"] = {
            "session_5h": h5,
            "weekly_7d": util(lambda g: g.get("label") == "7d"),
            "fable_7d": util(lambda g: "fable" in str(g.get("label", "")))}
        if h5 is None:
            row["signal"] = "no header observation — nothing to cross-check"
        elif h5 >= 0.10 and row["session_5h"] == 0:
            row["signal"] = ("DRIFT header-active/scan-empty: header 5h at %d%% but 0 "
                             "local tokens — usage from another machine, or this "
                             "store is incomplete" % round(h5 * 100))
        elif h5 == 0 and row["session_5h"] > 0:
            row["signal"] = ("DRIFT scan-active/header-idle: %s local 5h tokens but "
                             "header 0%% — session window rolled, or accounting lag"
                             % fmt_tokens(row["session_5h"]))
        elif h5 and row["session_5h"] == 0:  # header positive but under the bar
            row["signal"] = ("plausible — header 5h at %d%% with 0 local tokens: "
                             "no strong directional drift at the configured "
                             "threshold (10%%)" % round(h5 * 100))
        else:
            row["signal"] = "plausible — both sources agree on activity"
    return rows


def fmt_tokens(n):
    if n >= 10_000_000:
        return "%.0fM" % (n / 1e6)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 10_000:
        return "%.0fk" % (n / 1e3)
    if n >= 1_000:
        return "%.1fk" % (n / 1e3)
    return str(int(n))


def cmd_crosscheck(args):
    """creds crosscheck [--json] — local-scan second source vs header truth."""
    from .providers import ProviderError
    try:
        rows = crosscheck()
    except ProviderError as e:
        print("helm creds: no quota provider on this machine (%s) — sessions/resume "
              "still work" % e)
        return 1
    if "--json" in args:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("helm creds crosscheck: no anthropic session stores found.")
        return 0
    print("helm creds crosscheck — local-session scan vs header truth "
          "(drift = a health signal, reported never fatal)")
    home = os.path.expanduser("~")
    for r in rows:
        who = ("SHARED by %d: %s" % (len(r["accounts"]), ", ".join(r["accounts"]))
               if r["shared"] else r["accounts"][0])
        print("  %s  (%s)" % (r["store"].replace(home, "~", 1), who))
        newest = (time.strftime(" (newest %Y-%m-%dT%H:%MZ)", time.gmtime(r["newest"]))
                  if r.get("newest") else "")
        print("    scan:   5h %s | 7d %s | fable-7d %s tokens over %d records%s" % (
            fmt_tokens(r["session_5h"]), fmt_tokens(r["weekly_7d"]),
            fmt_tokens(r["fable_7d"]), r["records"], newest))
        h = r.get("header")
        if h and h.get("session_5h") is not None:
            print("    header: 5h %s | 7d %s | fable-7d %s" % tuple(
                "-" if v is None else "%d%%" % round(v * 100)
                for v in (h["session_5h"], h["weekly_7d"], h["fable_7d"])))
        print("    " + r["signal"])
    return 0
