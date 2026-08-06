#!/usr/bin/env python3
"""helm codex — the codexhome roster + proxy cred pooling (stdlib only).

THE MECHANISM (the codex-credhome-proxy-pooling premise, owner-taught
2026-07-21, done by hand that night — codified here): codex accounts live as
credHOMES under ~/.codex-homes/<name>/ (each a CODEX_HOME dir carrying the
CLI-native auth.json; login stays human-only — homes.py's
`CODEX_HOME=<h> codex login --device-auth`). Codex SEATS run `claude` against
the local CLIProxyAPI (seat.py, :8317), which POOLS creds from its auth-dir
(~/.helm/_global/seats/codex/auth/), HOT-RELOADS that dir on file changes,
and FALLS THROUGH to a working cred when one is usage-capped. Pooling =
translate a home's auth.json (nested `tokens`) into the proxy's FLAT record
{type, email, account_id, access_token, id_token, refresh_token, disabled,
expired, last_refresh}, written 0600 as codex-<name>.json. The translation
itself is seat.translate_codex_auth — the proven recipe, consumed not copied.

LAWS:
  - source auth.json files are READ-ONLY, forever (seat.py's law): the codex
    CLI refreshes the home's copy, the proxy refreshes its own pooled copy.
  - the access_token `exp` is NOT a liveness verdict — the codex CLI
    autorefreshes homes, so a past exp means "refresh before pooling" (run
    `CODEX_HOME=<h> codex` once; the proxy's own refresh can 401 on a stale
    copy), never "the account is dead". Pool warns, never refuses, on it.
  - token material NEVER reaches stdout/stderr — email / account_id / plan /
    paths only. Pooled files are 0600 from creation.
  - plan tiers: chatgpt_plan_type "pro" = ultra (handles multiple concurrent
    codexes), "team" = team (one codex each). Read from the access_token's
    https://api.openai.com/auth claim, id_token fallback — decode-only
    identity metadata, tokens discarded.

KNOWN INTERACTION: `helm seat add codex` replaces only the pooled file(s)
carrying the SAME account_id it mints — other pooled accounts survive a seat
re-add (one-cred-per-seat is a default, never an invariant; the pool is the
proxy's usage-cap fall-through and must not collapse).

ORCA ONE-WAY LEG (`helm codex sync-orca [--watch]`): orca — the metaharness
host — manages per-account CODEX_HOMEs natively, and its account SELECTION
drives this pool: accounts.list over harness.OrcaAdapter's daemon socket
(the one orca client — the CLI fallback below is a SUBPROCESS, never a
second socket implementation), degrading to the `orca account list` prose
roster when the RPC fails (email + active marker only — see
_orca_cli_codex_state), then the selected identity is pooled from the LIVE
bytes with a strict SOURCE PRECEDENCE (amendment, measured 2026-08-04):
  1. orca's own managed per-account home — userData/codex-accounts/<id>/home/
     auth.json, ownership-markered (orca codex-accounts/service.ts mints it,
     host-codex-managed-home-ownership.ts guards it). codex ROTATES refresh
     tokens, and orca's copy refreshing first INVALIDATES any duplicate — so
     when orca holds the account, orca's file is the live credential and our
     ~/.codex-homes copy is potentially burned bytes. THAT PREMISE HOLDS
     ONLY WHILE ORCA ACTIVELY USES THE ACCOUNT: an idle managed copy can be
     arbitrarily stale, so overwriting an EXISTING pool file from this
     source passes a FRESHNESS RUNG — a managed auth.json whose mtime is
     strictly older than the pool file's REFUSES (both timestamps printed;
     `--force-managed` / HELM_CODEX_FORCE_MANAGED=1 is the deliberate
     override). Measured 2026-08-04 19:10Z: a July-26 managed copy pooled
     over an 18:17Z login grant took the codex family auth-dark 11 minutes.
  2. the matching ~/.codex-homes copy (account_id-then-email, codex_pool) —
     the FALLBACK when orca has no managed home for the selection (system
     default slot, WSL-managed, marker/identity mismatch, or absent).
Both sources stay READ-ONLY forever; the stale-exp WARN law applies to
whichever source is chosen. No managed source AND zero/many codexhome
matches = loud refusal printing BOTH rosters plus the managed-store probe
verdict, never a guess, never a write; --watch polls and re-pools only on a
selection CHANGE (the proxy's hot-reload watcher is not churned per tick).

Every public function returns a JSON-able dict (or list); errors are
{"error": "..."} — loud, attributed, never an exception across the API edge.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

from . import home, pk, seat as _seat

PLAN_TIER = {"pro": "ultra", "team": "team"}

# `helm codex launch` cred-% gate (runbook 2026-07-21 fix #3): the refuse
# policy lives here, in numbers, so a launch never silently drains a capped
# pool (fall-through masks per-cred burn until everyone 429s at once).
NEAR_PCT = 80.0          # a pooled cred at/above this in ANY live window = near
STALE_S = 3600           # rollout tail older than this = stale = UNKNOWN
SCAN_ROLLOUTS = 3        # newest rollout files to tail per credhome
TAIL_BYTES = 65536       # bytes tailed per rollout (the last rate_limits win)


def tier(plan):
    """chatgpt_plan_type -> the fleet vocabulary (ultra/team); unknowns pass
    through raw so a new plan name is visible, never masked."""
    return PLAN_TIER.get(plan, plan or "?")


# slice 6 — N-codex-per-credhome: tier IS the seat-count policy. An ultra
# credhome (plan pro, 20x) drives N concurrent codex seats against the SAME
# proxy/pool; a team credhome stays 1-each. HELM_CODEX_ULTRA_SEATS moves the
# ultra count without a state file; unknown tier = 1 (safe).
def _ultra_seats():
    try:
        return max(1, int(home.env("CODEX_ULTRA_SEATS") or 3))
    except ValueError:
        return 3


def seat_capacity(t):
    """Fleet seats a tier supports. ultra -> HELM_CODEX_ULTRA_SEATS (dflt 3);
    team/unknown -> 1."""
    return _ultra_seats() if t == "ultra" else 1


def capacity():
    """Fleet seat capacity = what the POOL holds, not what exists under
    ~/.codex-homes (an unpooled ultra contributes 0). {creds: [{email, tier,
    seats}], total} over every parseable, non-disabled pooled codex record.
    Read by `helm codex capacity` and the `helm seat launch -i` guard."""
    creds, total = [], 0
    for r in codex_pooled():
        if r.get("error") or r.get("disabled") or r.get("type") != "codex":
            continue
        t = r.get("tier") or "?"
        n = seat_capacity(t)
        creds.append({"email": r.get("email"), "tier": t, "seats": n})
        total += n
    return {"creds": creds, "total": total}


def homes_root():
    """~/.codex-homes, HELM_CODEX_HOMES_DIR-overridable (tests, odd installs)."""
    return os.path.realpath(os.path.expanduser(
        home.env("CODEX_HOMES_DIR") or os.path.join("~", ".codex-homes")))


def pool_dir():
    """The codex seat proxy's auth-dir — the ONE dir CLIProxyAPI hot-reloads."""
    return os.path.join(_seat.seat_dir("codex"), "auth")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _write_pool_atomic(dest, text):
    """A pool write the proxy's hot-reload watcher can never catch half-made:
    0600 tmp sibling + os.replace (atomic on the same fs) — O_TRUNC-in-place
    (_write_private) lets the watcher read a truncated cred mid-write (slice
    6, risk 2). Mode enforced from creation like _write_private."""
    d = os.path.dirname(dest)
    os.makedirs(d, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pool-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _identity(auth):
    """(email, plan, account_id, exp_epoch) from a codex auth.json dict —
    decode-only claims (access_token first for plan, per the premise; id_token
    fallback). Tokens are decoded and discarded, never returned."""
    t = (auth or {}).get("tokens") or {}
    idc = _seat._jwt_claims(t.get("id_token"))
    acc = _seat._jwt_claims(t.get("access_token"))
    email = idc.get("email") or acc.get("email")
    plan = ((acc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
            or (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type"))
    account_id = (t.get("account_id")
                  or (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
                  or (acc.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id"))
    exp = acc.get("exp")
    return (email if isinstance(email, str) and email else None,
            plan if isinstance(plan, str) and plan else None,
            account_id if isinstance(account_id, str) and account_id else None,
            exp if isinstance(exp, (int, float)) else None)


def _pool_records():
    """[(filename, dict-or-None)] for every *.json in the pool dir — None =
    unparseable (reported, never fatal)."""
    return [(os.path.basename(p), _read_json(p))
            for p in sorted(glob.glob(os.path.join(pool_dir(), "*.json")))]


def _pooled_by_account():
    """account_id -> pooled filename for every parseable codex record."""
    out = {}
    for fname, rec in _pool_records():
        if isinstance(rec, dict) and rec.get("type") == "codex" and rec.get("account_id"):
            out[rec["account_id"]] = fname
    return out


def codex_list():
    """Every codexhome: real dirs claim the row, symlink aliases fold onto
    their target, distinct dirs sharing one account_id collapse to the first
    (dedupe by account — they ARE one account). pooled = the pool filename
    currently carrying this account, else None."""
    root = homes_root()
    pooled = _pooled_by_account()
    links, dirs = [], []
    for p in sorted(glob.glob(os.path.join(root, "*"))):
        if os.path.islink(p) and os.path.isdir(p):
            links.append(p)
        elif os.path.isdir(p):
            dirs.append(p)
    rows, by_real, by_account = [], {}, {}
    for p in dirs + links:  # real dirs first: the canonical name never loses to an alias
        real = os.path.realpath(p)
        name = os.path.basename(p)
        if real in by_real:
            by_real[real]["aliases"].append(name)
            continue
        auth_path = os.path.join(real, "auth.json")
        email, plan, account_id, exp = _identity(_read_json(auth_path))
        if account_id and account_id in by_account:
            by_account[account_id]["aliases"].append(name)  # same account, other dir
            by_real[real] = by_account[account_id]
            continue
        row = {"name": name, "path": real, "aliases": [],
               "authed": os.path.exists(auth_path), "email": email,
               "plan": plan, "tier": tier(plan) if plan else None,
               "account_id": account_id, "access_exp": exp,
               "pooled": pooled.get(account_id)}
        by_real[real] = row
        if account_id:
            by_account[account_id] = row
        rows.append(row)
    return rows


def _resolve_home(name):
    """(canonical_name, real_dir, err) for a home name, alias, or path."""
    name = (name or "").strip().rstrip("/")
    if not name:
        return None, None, {"error": "need a codexhome name (see `helm codex list`)"}
    root = homes_root()
    path = os.path.expanduser(name) if os.path.sep in name else os.path.join(root, name)
    if not os.path.isdir(path):
        return None, None, {"error": "no codexhome %r under %s (see `helm codex list`)"
                                     % (name, root)}
    real = os.path.realpath(path)
    canonical = os.path.basename(real) if os.path.dirname(real) == root \
        else os.path.basename(path.rstrip("/"))
    return canonical, real, None


def _pool_auth(src, canonical, stale_fix, refuse_stale_src=False):
    """The ONE pool write, whatever store holds the source: translate a codex
    auth.json into the proxy flat record and write it 0600 into the pool dir
    as codex-<canonical>.json. Idempotent — re-pooling refreshes the copy
    (that IS the stale-401 cure). src is READ-ONLY forever; no token material
    in the result. stale_fix = the source-specific cure sentence the past-exp
    WARN carries (the warn-never-refuse law applies to EVERY source).
    refuse_stale_src = the FRESHNESS RUNG (managed-source callers): when the
    dest exists with DIFFERENT bytes and src's mtime is strictly older,
    refuse instead of writing — pooling an older file over a fresher cred is
    credential REGRESSION, not a refresh (measured 2026-08-04 19:10Z: a
    July-26 orca-managed copy overwrote an 18:17Z login grant and the codex
    family went auth-dark for 11 minutes). Byte-identical still skips ahead
    of this rung (an identical copy cannot regress anything), and a missing
    dest accepts src freely (first pool)."""
    rec, _fname, terr = _seat.translate_codex_auth(src)
    if terr:
        return {"error": terr}
    rec["disabled"] = False  # the proxy's own kill-switch field, born live
    email, plan, account_id, exp = _identity(_read_json(src))
    if not rec.get("account_id") and account_id:
        # translate + _identity share one resolution today; this guarantees the
        # pooled record ALWAYS carries what identity knows even if they ever
        # diverge — dedup, list linkage, and seat-add preservation key off it.
        rec["account_id"] = account_id
    dupes = [f for a, f in _pooled_by_account().items()
             if a == rec.get("account_id") and f != "codex-%s.json" % canonical]
    dest = os.path.join(pool_dir(), "codex-%s.json" % canonical)
    body = json.dumps(rec, indent=2) + "\n"
    existed = os.path.exists(dest)
    warn = None
    if exp is not None and exp <= time.time():
        warn = "access token exp is past — " + stale_fix
    base = {"ok": True, "pooled": os.path.basename(dest), "path": dest,
            "email": email or rec.get("email"), "account_id": account_id,
            "tier": tier(plan) if plan else "?", "updated": existed,
            "also_pooled_as": dupes or None, "warn": warn,
            "note": "the proxy hot-reloads its auth-dir — no restart needed"}
    if existed and _read_text(dest) == body:
        # bytes-identical: skip the write entirely — an identical rewrite
        # cures nothing (the stale-401 cure IS fresh bytes) and only churns
        # the proxy's hot-reload watcher + the file mtime.
        base.update(updated=False, unchanged=True,
                    note="pool already carries these exact bytes — untouched")
        return base
    if refuse_stale_src and existed:
        try:
            src_m, dest_m = os.stat(src).st_mtime, os.stat(dest).st_mtime
        except OSError:
            src_m = dest_m = None      # dest raced away — nothing to regress
        if src_m is not None and src_m < dest_m:
            return {"error":
                    "refusing to pool orca's managed copy over a FRESHER "
                    "pooled cred: %s (%s) is older than %s (%s) — orca "
                    "refreshes a managed home only while actively using the "
                    "account, so an idle copy can be arbitrarily stale; "
                    "override deliberately with `helm codex sync-orca "
                    "--force-managed` or HELM_CODEX_FORCE_MANAGED=1"
                    % (src, _utc(src_m), dest, _utc(dest_m))}
    _write_pool_atomic(dest, body)
    return base


def codex_pool(name):
    """Translate one codexhome's auth.json into the proxy flat record and
    write it 0600 into the pool dir as codex-<canonical-name>.json.
    The source file is never touched; no token material in the result."""
    canonical, real, err = _resolve_home(name)
    if err:
        return err
    src = os.path.join(real, "auth.json")
    if not os.path.exists(src):
        return {"error": "%s has no auth.json — not logged in; human-only: "
                         "CODEX_HOME=%s codex login --device-auth" % (real, real)}
    return _pool_auth(src, canonical,
                      "the codex CLI autorefreshes, so run `CODEX_HOME=%s "
                      "codex` once and re-pool the FRESH token (the proxy's "
                      "own refresh can 401 on a stale copy)" % real)


def codex_unpool(name):
    """Remove the pooled file for a home (canonical + given-name spellings
    both tried). Fail-open: nothing pooled under that name is ok, not error."""
    given = (name or "").strip().rstrip("/")
    if not given:
        return {"error": "need a codexhome name (see `helm codex pooled`)"}
    cands = [given] if given.endswith(".json") else ["codex-%s.json" % given]
    canonical, _real, err = _resolve_home(given)
    if not err and canonical and "codex-%s.json" % canonical not in cands:
        cands.append("codex-%s.json" % canonical)  # alias given, canonical pooled
    removed = []
    for fname in cands:
        p = os.path.join(pool_dir(), fname)
        if os.path.isfile(p):
            os.remove(p)
            removed.append(fname)
    return {"ok": True, "removed": removed,
            "note": None if removed else
            "nothing pooled under %r (fail-open — already absent)" % given}


def codex_pooled():
    """What the proxy can actually draw on: every *.json in its auth-dir with
    identity metadata only (unparseable files reported, never fatal)."""
    rows = []
    for fname, rec in _pool_records():
        if not isinstance(rec, dict):
            rows.append({"file": fname, "error": "unparseable"})
            continue
        _email, plan, _acct, _exp = _identity({"tokens": rec})
        rows.append({"file": fname, "type": rec.get("type"),
                     "email": rec.get("email"), "account_id": rec.get("account_id"),
                     "plan": plan, "tier": tier(plan) if plan else None,
                     "disabled": bool(rec.get("disabled")),
                     "expired": rec.get("expired")})
    return rows


# ---------------------------------------------------------------- usage gate

def _latest_rate_limits(real_home):
    """Newest rate_limits event in a credhome's own rollout logs — the local
    ground truth for headroom (the codex CLI appends one per turn; the usage
    MCP's codex feed is just these, forwarded). Reads only the newest
    SCAN_ROLLOUTS files' last TAIL_BYTES. Returns the rate_limits dict plus
    {"file": path} for attribution, or None when the home has no recent
    rollout telemetry at all (never-used / pre-rate_limits CLI = UNKNOWN)."""
    root = os.path.join(real_home, "sessions")
    try:
        files = [os.path.join(dp, f) for dp, _dn, fn in os.walk(root)
                 for f in fn if f.startswith("rollout-") and f.endswith(".jsonl")]
    except OSError:
        return None
    files.sort(key=lambda p: os.path.basename(p), reverse=True)  # ts in name
    for path in files[:SCAN_ROLLOUTS]:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > TAIL_BYTES:
                    f.seek(-TAIL_BYTES, os.SEEK_END)
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if "rate_limits" not in line:
                continue
            try:
                ev = json.loads(line)
                rl = ((ev.get("payload") or {}).get("rate_limits")
                      if isinstance(ev, dict) else None)
            except ValueError:
                continue            # mid-line tail cut; older lines are whole
            if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
                rl["file"] = path
                return rl
    return None


def usage_gate():
    """The launch headroom verdict per credhome, from the homes' own rollout
    rate_limits. Statuses: ok (freshest event fresh + every live window under
    NEAR_PCT), near (>= NEAR_PCT or a reached-type recorded), exhausted
    (a live window at 100%), unknown (no rollout telemetry or stale >
    STALE_S — stale/unread = NOT ok, the runbook's core rule)."""
    now = time.time()
    rows = []
    for h in codex_list():
        if not h["authed"]:
            continue
        row = {"name": h["name"], "email": h["email"], "tier": h["tier"],
               "pooled": h["pooled"], "account_id": h["account_id"],
               "status": "unknown"}
        rl = _latest_rate_limits(h["path"])
        if rl:
            age = now - os.path.getmtime(rl.pop("file"))
            row["age_s"] = int(age)
            if age > STALE_S:
                row["status"] = "unknown"
                row["note"] = "rollout tail stale (%dm > %dm)" % (
                    age // 60, STALE_S // 60)
            else:
                worst, live = 0.0, False
                for w in (rl.get("primary"), rl.get("secondary")):
                    if not isinstance(w, dict):
                        continue
                    resets = w.get("resets_at")
                    if isinstance(resets, (int, float)) and resets <= now:
                        continue          # window over — no longer binding
                    live = True
                    pct = w.get("used_percent")
                    if isinstance(pct, (int, float)):
                        worst = max(worst, pct)
                row["pct"] = worst
                if rl.get("rate_limit_reached_type"):
                    row["status"], row["reached"] = "exhausted", \
                        rl["rate_limit_reached_type"]
                elif not live:
                    # a fresh event whose windows have ALL reset binds
                    # nothing — the freshest reading there is = green
                    row["status"] = "ok"
                elif worst >= 100.0:
                    row["status"] = "exhausted"
                elif worst >= NEAR_PCT:
                    row["status"] = "near"
                else:
                    row["status"] = "ok"
        rows.append(row)
    return rows


def _suggest_pool(gate):
    """The concrete fix: best unpooled credhome to `helm codex pool` next —
    prefer an ok-status ultra, then any ok, then the top ultra regardless
    (its reading is the likeliest to improve once the CLI refreshes it)."""
    unpooled = [g for g in gate if not g["pooled"]]
    for pred in (lambda g: g["status"] == "ok" and g["tier"] == "ultra",
                 lambda g: g["status"] == "ok",
                 lambda g: g["tier"] == "ultra"):
        hits = [g for g in unpooled if pred(g)]
        if hits:
            return hits[0]
    return None


def launch_gate(inst=1, force=False, out=None):
    """Refuse-by-default cred-% gate ahead of a codex seat launch (runbook
    fix #3). Pooled rows near/exhausted/unknown are the problem classes; a
    launch is allowed while at least one POOLED cred reads ok, otherwise
    rc 1 with the concrete `helm codex pool <name>` fix on stderr (--force
    overrides: the gate advises, the operator decides — same law as seat
    launch's own warns). Prints the gate table; returns the rc."""
    out = out or sys.stderr
    gate = usage_gate()
    pooled = [g for g in gate if g["pooled"]]
    print("helm codex: launch gate (fresh = rollout tail <%dm, near >= %.0f%%)"
          % (STALE_S // 60, NEAR_PCT), file=out)
    for g in gate:
        mark = "pooled" if g["pooled"] else "-"
        extra = (" %.0f%% used" % g["pct"]) if g.get("pct") is not None else ""
        if g.get("note"):
            extra += " (%s)" % g["note"]
        if g.get("reached"):
            extra += " (reached %s)" % g["reached"]
        print("  %-6s %-28s %-32s %-6s %-6s%s" % (
            g["status"], g["name"], g["email"] or "-", g["tier"] or "?",
            mark, extra), file=out)
    ok_pool = [g for g in pooled if g["status"] == "ok"]
    if ok_pool or force:
        if not ok_pool:
            print("helm codex: WARN — --force over a gate with no ok pooled "
                  "cred; the pool may 429 under load", file=out)
        return 0
    fix = _suggest_pool(gate)
    print("helm codex: REFUSE — no pooled cred reads ok "
          "(near/exhausted/unknown); the fix is pooling, not retrying", file=out)
    if fix:
        print("  fix: helm codex pool %s   # %s, %s%s" % (
            fix["name"], fix["email"] or "-", fix["tier"] or "?",
            " (currently %s)" % fix["status"] if fix["status"] != "ok" else ""),
            file=out)
    print("  then re-run, or override: helm codex launch -i %d --force" % inst,
          file=out)
    return 1


def _run_launch(rest):
    """Delegate to the seat-launch mint (codex family): ONE mint path — the
    gate only guards entry to it; -i/--room/--model pass straight through."""
    return _seat.cmd_seat(["launch", "codex"] + list(rest))


# ------------------------------------------------------------ orca sync leg

# `--watch` cadence: selection flips are human-paced, and the pool write rides
# the CHANGE, never the tick. HELM_CODEX_SYNC_POLL_S moves it (floor 5s).
SYNC_POLL_S = 30
# accounts.list refreshes provider state daemon-side before answering (orca
# accounts.ts: refreshAccountsForMobile), so it can outlive the 5s
# pane-resolution budget — this call carries its own window. Measured live
# 2026-08-04 against the real daemon: 15.5-16.4s per call (the refresh runs
# EVERY call, warm or cold), so the old 15s budget lost the race every time
# and misread a healthy daemon as unreachable. 30 covers the measured band
# with margin; the CLI fallback covers a daemon that blows even that.
ACCOUNTS_TIMEOUT_S = 30
# `orca account list` answered instantly live even while the RPC leg was
# timing out (it reads state without the provider refresh) — 20 is generous.
CLI_TIMEOUT_S = 20


def _sync_poll_s():
    try:
        return max(5, int(home.env("CODEX_SYNC_POLL_S") or SYNC_POLL_S))
    except ValueError:
        return SYNC_POLL_S


def _accounts_timeout_s():
    # HELM_CODEX_ACCOUNTS_TIMEOUT_S: the test seam (a hanging fake daemon
    # must time out in ~1s, not 30) doubling as the operator override.
    try:
        return max(1, int(home.env("CODEX_ACCOUNTS_TIMEOUT_S")
                          or ACCOUNTS_TIMEOUT_S))
    except ValueError:
        return ACCOUNTS_TIMEOUT_S


def _orca_adapter():
    # the ONE orca daemon client (fail-open rpc, HELM_ORCA_RPC kill-switch,
    # auth token never surfaced) — never a second socket implementation here
    from .harness import OrcaAdapter
    return OrcaAdapter()


def _orca_socket_tried():
    """What the daemon client actually aimed at, for the absent-daemon exit:
    the unix endpoint from orca-runtime.json when readable, else the metadata
    path itself (with no metadata, no socket was ever tried)."""
    meta_path = os.path.join(_orca_adapter()._user_data_path(),
                             "orca-runtime.json")
    meta = _read_json(meta_path)
    transports = (meta or {}).get("transports")
    if not isinstance(transports, list):
        transports = [(meta or {}).get("transport")]
    t = next((t for t in transports if isinstance(t, dict)
              and t.get("kind") == "unix" and t.get("endpoint")), None)
    return t["endpoint"] if t else meta_path


def _parse_cli_accounts(text):
    """(codex-state, err) from `orca account list` prose. Scoped STRICTLY to
    the "Managed Codex accounts" section — the Claude section above it carries
    its own `(active)` marker that must never leak into codex selection. Rows
    become {id: email, email} (the email IS the CLI's whole identity; account
    ids are RPC-only) and the `(active)` row becomes activeAccountId. An
    empty codex section is a VALID zero-account roster; a MISSING header is a
    parse error (older/foreign CLI) so the caller can report both dead
    routes instead of a silent empty."""
    accounts, active, saw, in_codex = [], None, False, False
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[:1].isspace():           # a section header, not a row
            in_codex = line.strip().lower().startswith(
                "managed codex accounts")
            saw = saw or in_codex
            continue
        if not in_codex:
            continue
        entry = line.strip()
        is_active = entry.endswith("(active)")
        email = entry[:-len("(active)")].strip() if is_active else entry
        if not email:
            continue
        accounts.append({"id": email, "email": email})
        if is_active:
            active = email
    if not saw:
        return None, ("orca account list output has no 'Managed Codex "
                      "accounts' section — cannot read the roster")
    return {"accounts": accounts, "activeAccountId": active,
            "_helm_source": "cli"}, None


def _orca_cli_codex_state():
    """(codex-state, err) — the DEGRADED subprocess twin of the accounts.list
    RPC (the OrcaAdapter.panes() dual-source pattern: two routes, ONE
    downstream shape). The binary is discovered exactly as the adapter's CLI
    verbs discover it (PATH lookup of its `bin`); HELM_ORCA_CLI pins a path
    or switches the route off — the same live-workspace kill-switch law as
    HELM_ORCA_RPC (harness._runtime_call), because this leg reaches the REAL
    orca account roster. Output is prose, so the snapshot carries email +
    active only, stamped _helm_source=cli; no token material ever appears in
    `orca account list` output or in these errors."""
    pin = (home.env("ORCA_CLI") or "").strip()
    if pin.lower() in ("off", "0", "none", "false"):
        return None, "orca CLI fallback disabled (HELM_ORCA_CLI)"
    try:
        p = subprocess.run([pin or _orca_adapter().path, "account", "list"],
                           capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "orca account list: %s" % e
    if p.returncode != 0:
        return None, "orca account list: rc %d — %s" % (
            p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    return _parse_cli_accounts(p.stdout)


def _orca_codex_state():
    """(codex branch of orca's AccountsSnapshot, err). RPC FIRST — the rich
    source (account ids, activeAccountIdsByRuntime, systemDefault; shape per
    orca src/shared/types.ts CodexRateLimitAccountsState: {accounts: [{id,
    email, providerAccountId, ...}], activeAccountId,
    activeAccountIdsByRuntime?: {host, wsl}, systemDefault?: {hasAuth,
    authKind, email, providerAccountId}}) — then the CLI roster as the
    fallback, so a slow/wedged daemon degrades the sync instead of blinding
    it. err only when BOTH routes fail, and it carries both lines."""
    result, rerr = _orca_adapter().rpc("accounts.list", {},
                                       timeout=_accounts_timeout_s())
    if not rerr:
        codex = (result or {}).get("codex")
        if isinstance(codex, dict):
            return codex, None
        rerr = "accounts.list answered without a codex account state"
    codex, cerr = _orca_cli_codex_state()
    if codex is not None:
        return codex, None
    return None, "rpc: %s; cli fallback: %s" % (rerr, cerr)


def _orca_selected(codex):
    """(identity, err) — the codex account orca selects for the HOST runtime
    (helm shares orca's host; WSL slots select for other roots). identity =
    {id, email, account_id}; id None = orca's system-default slot (the real
    ~/.codex), whose identity rides the snapshot's systemDefault block."""
    by_rt = codex.get("activeAccountIdsByRuntime")
    active = by_rt.get("host") if isinstance(by_rt, dict) and "host" in by_rt \
        else codex.get("activeAccountId")
    if active is None:
        if codex.get("_helm_source") == "cli":
            # the CLI roster showed no `(active)` row; the system-default
            # prose below would mislead — the CLI simply cannot see that far
            return None, ("orca CLI names no (active) codex account — the "
                          "selection detail needs the daemon RPC")
        sd = codex.get("systemDefault")
        sd = sd if isinstance(sd, dict) else {}
        if not sd.get("hasAuth") or sd.get("authKind") != "oauth":
            return None, ("orca selects its system-default slot (~/.codex) "
                          "and resolves it %r — no oauth identity to sync"
                          % (sd.get("authKind") or "unresolved"))
        return {"id": None, "email": sd.get("email"),
                "account_id": sd.get("providerAccountId")}, None
    row = next((a for a in codex.get("accounts") or []
                if isinstance(a, dict) and a.get("id") == active), None)
    if row is None:
        return None, ("orca's active codex account %r is missing from its "
                      "own roster — refusing to guess an identity" % active)
    return {"id": active, "email": row.get("email"),
            "account_id": row.get("providerAccountId"),
            "runtime": row.get("managedHomeRuntime")}, None


def _orca_managed_auth(sel):
    """(auth_path, None) for the LIVE per-account credential orca itself
    maintains, else (None, why-not). Convention read from orca's source:
    codex-accounts/service.ts mints managedHomePath = userData/codex-accounts/
    <accountId>/home and writes the `.orca-managed-home` ownership marker with
    the account id; runtime-home-service.ts keeps that file the canonical
    store (auth read-backs on every rate-limit poll, or codex refreshing it
    in place on the self-contained lane). accounts.list summaries carry the
    id but NOT the path (shared/types.ts CodexManagedAccountSummary), so the
    path is DERIVED and then PROVEN: dir exists, marker is a regular file
    naming this id, auth.json parses, and its identity matches the selection
    (account_id first, email fallback) — orca's own ownership assert,
    mirrored read-only. Any failed proof = fall back to ~/.codex-homes, which
    can at worst pool a stale copy of the RIGHT account, never a wrong one."""
    aid = sel.get("id")
    if not aid:
        return None, "orca selects its system-default slot — no managed home"
    if sel.get("runtime") == "wsl":
        return None, ("orca manages this account inside WSL — no host-side "
                      "managed home")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", aid):
        return None, "orca account id %r is not a safe path segment" % aid
    root = os.path.join(_orca_adapter()._user_data_path(), "codex-accounts")
    home_dir = os.path.join(root, aid, "home")
    if not os.path.isdir(home_dir):
        return None, "no orca-managed home under %s" % os.path.join(root, aid)
    marker = os.path.join(home_dir, ".orca-managed-home")
    if not os.path.isfile(marker) or os.path.islink(marker):
        return None, ("%s lacks the .orca-managed-home ownership marker"
                      % home_dir)
    try:
        with open(marker) as f:
            owner = f.read().strip()
    except OSError as e:
        return None, "ownership marker unreadable: %s" % e
    if owner != aid:
        return None, ("ownership marker in %s names account %r, not %r — "
                      "not orca's dir for this account" % (home_dir, owner, aid))
    src = os.path.join(home_dir, "auth.json")
    auth = _read_json(src)
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
        return None, "%s has no parseable codex auth.json" % home_dir
    email, _plan, account_id, _exp = _identity(auth)
    want_acct, want_email = sel.get("account_id"), (sel.get("email") or "")
    if want_acct and account_id:
        if account_id != want_acct:
            return None, ("%s carries account %s, orca selects %s — identity "
                          "mismatch, not trusting the managed copy"
                          % (src, _short(account_id), _short(want_acct)))
    elif not (want_email and email
              and email.casefold() == want_email.casefold()):
        return None, ("cannot confirm %s carries the selected identity "
                      "(no account id or email agreement)" % src)
    return src, None


def _pooled_for(sel):
    """The pool filename currently carrying orca's selected identity —
    account_id first, else email. The unchanged-tick check for the managed
    source, where no codexhome row exists to carry the pooled linkage."""
    acct = sel.get("account_id")
    if acct:
        return _pooled_by_account().get(acct)
    email = (sel.get("email") or "").casefold()
    if not email:
        return None
    return next((fname for fname, rec in _pool_records()
                 if isinstance(rec, dict) and rec.get("type") == "codex"
                 and (rec.get("email") or "").casefold() == email), None)


def _orca_roster(codex, sel):
    """Print-safe rows of every codex account orca holds (managed + a
    system-default pseudo-row), the selected one flagged — the refusal's
    orca-side column. Emails/account ids only, never token material."""
    rows = [{"email": a.get("email"), "account_id": a.get("providerAccountId"),
             "selected": sel is not None and a.get("id") is not None
             and a.get("id") == sel.get("id")}
            for a in codex.get("accounts") or [] if isinstance(a, dict)]
    sd = codex.get("systemDefault")
    if isinstance(sd, dict) and sd.get("hasAuth"):
        rows.append({"email": sd.get("email"),
                     "account_id": sd.get("providerAccountId"),
                     "selected": sel is not None and sel.get("id") is None,
                     "system_default": True})
    return rows


def _sync_match(sel, rows):
    """The codexhome rows carrying orca's selected identity — account_id
    exact first (the identity law; codex_list folds same-account dirs, so an
    account hit is unique by construction), else case-folded email. Zero or
    many = the caller refuses; this never guesses."""
    acct = sel.get("account_id")
    if acct:
        hits = [r for r in rows if r.get("account_id") == acct]
        if hits:
            return hits
    email = (sel.get("email") or "").casefold()
    return [r for r in rows
            if email and (r.get("email") or "").casefold() == email]


def codex_sync_orca(prev_key=None, force_managed=False):
    """The ONE-WAY leg orca -> pool: read orca's selected codex account and
    pool its LIVE credential through the SAME atomic-0600/hot-reload/
    stale-exp-WARN-never-refuse path. SNAPSHOT ROUTE: accounts.list RPC
    first, the `orca account list` roster on RPC failure
    (_orca_codex_state); rc=2 only when BOTH die, naming both failures and
    the socket tried. SOURCE PRECEDENCE: orca's own managed per-account home
    first (_orca_managed_auth — codex rotates refresh tokens, so orca's copy
    refreshing FIRST burns our ~/.codex-homes copy), the matching codexhome
    as the fallback — BUT that premise only holds while orca is ACTIVELY
    using the account: an idle managed copy can be arbitrarily stale, so
    overwriting an EXISTING pool file from the managed store passes the
    freshness rung (_pool_auth refuse_stale_src — mtime of the managed
    auth.json vs the pool file; strictly-older managed bytes REFUSE, naming
    both timestamps; measured 2026-08-04: a July-26 managed copy overwrote
    an 18:17Z login grant, 11-minute family outage). force_managed=True (or
    HELM_CODEX_FORCE_MANAGED=1) is the deliberate override. A CLI snapshot
    carries no account ids, so its legs narrow honestly: the managed store
    is never consulted, an already-pooled selection answers unchanged (the
    pool copy may BE the managed live bytes — never overwritten with a
    possibly-staler codexhome copy), and only a pool-absent identity pools
    from its codexhome. A refusal never writes and only fires when NO
    reachable source holds the selection (both rosters + the managed-store
    probe attached) or when the freshness rung blocks a regression. rc
    rides the dict: 2 = daemon unreachable (socket named), 1 = refusal,
    else the pool result + {selected, home, key, source, snapshot}.
    prev_key = the last synced key — EMAIL-casefold first (account_id only
    when orca carries no email) so the key survives an RPC->CLI flap mid
    --watch: an unchanged, still-pooled selection answers
    {"unchanged": True} WITHOUT rewriting (the anti-churn seam; refusal
    dicts carry the key too, so a watch tick after a freshness refusal goes
    unchanged-silent instead of re-printing it)."""
    codex, err = _orca_codex_state()
    if err:
        return {"rc": 2, "error": "orca daemon unreachable (%s) — tried %s"
                                  % (err, _orca_socket_tried())}
    snap = "cli" if codex.get("_helm_source") == "cli" else "rpc"
    sel, serr = _orca_selected(codex)
    if serr:
        return {"rc": 1, "error": serr, "orca": _orca_roster(codex, None)}
    rows = codex_list()
    hits = _sync_match(sel, rows)
    key = (sel.get("email") or "").casefold() or sel.get("account_id")
    if snap == "cli":
        pooled = _pooled_for(sel)
        if pooled:
            return {"ok": True, "unchanged": True, "selected": sel,
                    "key": key, "pooled": pooled, "source": "pool",
                    "snapshot": snap,
                    "home": hits[0]["name"] if len(hits) == 1 else None}
        managed, mwhy = None, ("orca CLI snapshot carries no account ids — "
                               "the managed store needs the daemon RPC")
    else:
        managed, mwhy = _orca_managed_auth(sel)
    if managed:
        # orca-managed live bytes win; a codexhome match only lends its NAME
        # so the pooled file keeps its slot (else the homes-prepare slug, so
        # a later `helm homes prepare codex <email>` lands on the same file).
        name = hits[0]["name"] if len(hits) == 1 else \
            pk.slug(sel.get("email") or "orca-" + sel["id"])
        pooled = _pooled_for(sel)
        if prev_key is not None and key == prev_key and pooled:
            return {"ok": True, "unchanged": True, "selected": sel, "key": key,
                    "home": name, "pooled": pooled, "source": "orca-managed",
                    "snapshot": snap}
        force = force_managed or \
            (home.env("CODEX_FORCE_MANAGED") or "").strip().lower() in \
            ("1", "true", "yes", "on")
        res = _pool_auth(managed, name,
                         "orca refreshes its managed copy in use — select/use "
                         "the account in orca once and re-run sync-orca (the "
                         "proxy's own refresh can 401 on a stale copy)",
                         refuse_stale_src=not force)
        if "error" in res:
            return {"rc": 1, "error": res["error"], "selected": sel,
                    "key": key, "snapshot": snap}
        res.update(selected=sel, home=name, key=key, source="orca-managed",
                   snapshot=snap)
        return res
    if len(hits) != 1:
        why = ("no codexhome carries that identity" if not hits else
               "%d codexhomes carry it (%s) — refusing to guess"
               % (len(hits), ", ".join(h["name"] for h in hits)))
        return {"rc": 1, "orca": _orca_roster(codex, sel), "selected": sel,
                "homes": [{"name": r["name"], "email": r["email"],
                           "account_id": r["account_id"]} for r in rows],
                "orca_managed": mwhy, "snapshot": snap,
                "error": "orca selects %s (account %s) but %s"
                         % (sel.get("email") or "?",
                            _short(sel.get("account_id")), why)}
    row = hits[0]
    if prev_key is not None and key == prev_key and row["pooled"]:
        return {"ok": True, "unchanged": True, "selected": sel, "key": key,
                "home": row["name"], "pooled": row["pooled"],
                "source": "codex-homes", "snapshot": snap}
    res = codex_pool(row["name"])
    if "error" in res:
        return {"rc": 1, "error": res["error"], "selected": sel}
    res.update(selected=sel, home=row["name"], key=key, source="codex-homes",
               snapshot=snap)
    return res


def _print_sync(res):
    """One sync verdict -> printed lines + rc. The refusal prints BOTH
    rosters — the operator's next move (mint the missing home, or flip
    orca's selection) needs the two lists side by side."""
    if res.get("rc") == 2:
        print("helm codex: sync-orca — %s" % res["error"], file=sys.stderr)
        return 2
    if "error" in res:
        print("helm codex: sync-orca REFUSE — %s" % res["error"],
              file=sys.stderr)
        for r in res.get("orca") or []:
            print("  orca%s %-8s %-32s %s" % (
                "*" if r.get("selected") else " ",
                "sysdflt" if r.get("system_default") else "managed",
                r.get("email") or "-", _short(r.get("account_id"))),
                file=sys.stderr)
        homes = res.get("homes")
        if homes is not None:
            if not homes:
                print("  codexhomes: NONE under %s" % homes_root(),
                      file=sys.stderr)
            for r in homes:
                print("  home  %-26s %-32s %s" % (
                    r["name"], r["email"] or "-", _short(r["account_id"])),
                    file=sys.stderr)
            if res.get("orca_managed"):
                # the shopping list names BOTH locations: the codexhomes
                # roster above AND why orca's managed store yielded nothing
                print("  orca-managed store: %s" % res["orca_managed"],
                      file=sys.stderr)
            print("  fix: `helm homes prepare codex <email>` + the human-only"
                  " `CODEX_HOME=~/.codex-homes/<name> codex login "
                  "--device-auth`, or select a listed account in orca",
                  file=sys.stderr)
        return 1
    if res.get("unchanged"):
        print("helm codex: sync-orca — selection unchanged (%s), pool "
              "untouched (%s)%s" % (res["selected"].get("email") or "-",
                                    res["pooled"],
                                    " [orca CLI roster — daemon RPC "
                                    "unavailable]"
                                    if res.get("snapshot") == "cli" else ""))
        if res.get("warn"):
            print("  WARN: " + res["warn"])
        return 0
    print("helm codex: sync-orca %s orca-selected %s -> %s (%s, account %s)"
          % ("refreshed" if res["updated"] else "pooled",
             res["email"] or res["home"], res["path"], res["tier"],
             _short(res["account_id"])))
    if res.get("source"):
        print("  source: %s" % (
            "orca-managed home (the live bytes — codex rotates refresh "
            "tokens, orca's copy is canonical)"
            if res["source"] == "orca-managed"
            else "codexhome %s (%s)"
            % (res["home"],
               "orca CLI roster — daemon RPC down, managed store not "
               "consulted" if res.get("snapshot") == "cli"
               else "no orca-managed home for the selection")))
    print("  " + res["note"])
    if res.get("also_pooled_as"):
        print("  note: same account also pooled as %s — `helm codex unpool` "
              "the stale one" % ", ".join(res["also_pooled_as"]))
    if res.get("warn"):
        print("  WARN: " + res["warn"])
    return 0


def _run_sync_orca(watch=False, force_managed=False):
    """One-shot: sync once, exit honest. --watch: poll accounts.list every
    SYNC_POLL_S (accounts.subscribe is a STREAMING method; the shared client
    reads exactly one reply per call, so polling is the deliberate framing).
    A dead daemon at START exits 2 (both snapshot routes down — the error
    names each) — a watcher aimed at nothing says so now; once watching,
    transient errors and refusals are reported and ridden out (a selection
    flip can cure either), and unchanged ticks stay silent. A poll that
    degrades to the CLI roster still answers unchanged for a still-pooled
    selection (email-keyed, source-independent), so a flapping daemon never
    spams the log or churns the pool."""
    res = codex_sync_orca(force_managed=force_managed)
    rc = _print_sync(res)
    if not watch or rc == 2:
        return rc
    key = res.get("key")
    while True:
        time.sleep(_sync_poll_s())
        res = codex_sync_orca(prev_key=key, force_managed=force_managed)
        if not res.get("unchanged"):
            _print_sync(res)
        key = res.get("key", key)


# ---------------------------------------------------------------- CLI leg

def _short(account_id):
    return (account_id[:8] + "…") if account_id and len(account_id) > 9 \
        else (account_id or "-")


def _fail(res):
    print("helm codex: " + res["error"], file=sys.stderr)
    return 1


def _print_list():
    rows = codex_list()
    if not rows:
        print("helm codex: no codexhomes under %s — `helm homes prepare codex "
              "<email>` starts one" % homes_root())
        return 0
    n_pooled = sum(1 for r in rows if r["pooled"])
    print("helm codex: %d codexhome%s under %s (%d pooled -> %s)" % (
        len(rows), "s"[:len(rows) != 1], homes_root(), n_pooled, pool_dir()))
    for r in sorted(rows, key=lambda r: r["name"]):
        flags = []
        if not r["authed"]:
            flags.append("no-auth")
        elif not r["email"]:
            flags.append("identity?")
        flags.append("pooled:" + r["pooled"] if r["pooled"] else "-")
        alias = " [alias: %s]" % ",".join(r["aliases"]) if r["aliases"] else ""
        print("  %-26s %-32s %-6s %-10s %s%s" % (
            r["name"], r["email"] or "-", r["tier"] or "?",
            _short(r["account_id"]), "; ".join(flags), alias))
    return 0


def _print_pooled():
    rows = codex_pooled()
    if not rows:
        print("helm codex: pool empty (%s) — `helm codex pool <name>` feeds "
              "the proxy" % pool_dir())
        return 0
    print("helm codex: %d pooled cred%s in %s (proxy hot-reloads this dir)" % (
        len(rows), "s"[:len(rows) != 1], pool_dir()))
    for r in rows:
        if r.get("error"):
            print("  %-34s %s" % (r["file"], r["error"]))
            continue
        flags = ["disabled"] if r["disabled"] else []
        if r.get("expired"):
            flags.append("exp " + r["expired"])
        print("  %-34s %-32s %-6s %-10s %s" % (
            r["file"], r["email"] or "-", r["tier"] or r.get("type") or "?",
            _short(r["account_id"]), "; ".join(flags) or "-"))
    return 0


def _print_capacity():
    """The one policy readout: per-pooled-cred email/tier/seats + total fleet
    capacity, plus the codex* seats LIVE on the roster (so over/under is
    visible at a glance)."""
    from . import seats as _s
    cap = capacity()
    print("helm codex: fleet seat capacity %d (what the POOL holds, ultra=%d/"
          "cred via HELM_CODEX_ULTRA_SEATS, team=1)" % (cap["total"], _ultra_seats()))
    for c in cap["creds"]:
        print("  %-32s %-6s %d seat%s" % (
            c["email"] or "-", c["tier"], c["seats"], "s"[:c["seats"] != 1]))
    live = sorted(s for s in _s.roster() if s == "codex" or s.startswith("codex-"))
    if live:
        # the seat KEY is the unvalidated HELM_CHAT_NAME join seam — launder
        # the emitted label so a hostile codex-<ESC/bidi> seat cannot reshape
        # this readout's terminal (matching still rode the raw key above).
        print("  live codex seats: %s" % ", ".join(_s._seat_label(s) for s in live))
    return 0


def cmd_codex(args):
    """codex [list] | pool <name> | unpool <name> | pooled | capacity |
    sync-orca [--watch] [--force-managed] | launch [-i N|--instance N]
    [--force] [--room R] [--model M] — codexhome roster (ultra/team) + proxy cred
    pooling (auth.json -> 0600 pool file). launch = the cred-% gate ahead
    of the seat-launch mint; sync-orca = the one-way orca-selection -> pool
    adapter (--force-managed overrides its freshness rung)."""
    args = list(args)
    verb, rest = (args[0], args[1:]) if args else ("list", [])
    from .cli import guard_tail
    if verb in ("list", "pooled", "capacity"):
        rc = guard_tail("helm codex " + verb, rest, usage="codex " + verb)
        if rc is not None:
            return rc
        return {"list": _print_list, "pooled": _print_pooled,
                "capacity": _print_capacity}[verb]()
    if verb == "launch":
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
        # junk refuses BEFORE launch_gate: a failing pool gate (rc 1) used to
        # mask the unknown flag entirely, printing gate diagnostics as if it
        # existed. Mirrors seat's launch tail (the downstream authority);
        # drift refuses loudly here rather than silently passing junk on.
        rc = guard_tail("helm codex launch", rest, flags=("--multi",),
                        valued=("--room", "--model", "-i", "--instance"),
                        usage="codex launch [-i N|--instance N] [--force] "
                              "[--room R] [--model M] [--multi]")
        if rc is not None:
            return rc
        inst = 1
        for flag in ("-i", "--instance"):
            if flag in rest:
                try:
                    inst = int(rest[rest.index(flag) + 1])
                except (ValueError, IndexError):
                    print("helm codex: %s wants an integer" % flag,
                          file=sys.stderr)
                    return 2
        rc = launch_gate(inst=inst, force=force)
        if rc:
            return rc
        return _run_launch(rest)
    if verb == "sync-orca":
        watch = "--watch" in rest
        force_managed = "--force-managed" in rest
        rest = [a for a in rest if a not in ("--watch", "--force-managed")]
        rc = guard_tail("helm codex sync-orca", rest,
                        usage="codex sync-orca [--watch] [--force-managed]")
        if rc is not None:
            return rc
        return _run_sync_orca(watch=watch, force_managed=force_managed)
    if verb == "pool":
        if not rest:
            print("usage: helm codex pool <name>", file=sys.stderr)
            return 2
        rc = guard_tail("helm codex pool", rest[1:], usage="codex pool <name>")
        if rc is not None:
            return rc
        res = codex_pool(rest[0])
        if "error" in res:
            return _fail(res)
        print("helm codex: %s %s -> %s (%s, %s)" % (
            "refreshed" if res["updated"] else "pooled",
            res["email"] or rest[0], res["path"], res["tier"],
            "account " + _short(res["account_id"])))
        print("  " + res["note"])
        if res.get("also_pooled_as"):
            print("  note: same account also pooled as %s — `helm codex unpool` "
                  "the stale one" % ", ".join(res["also_pooled_as"]))
        if res.get("warn"):
            print("  WARN: " + res["warn"])
        return 0
    if verb == "unpool":
        if not rest:
            print("usage: helm codex unpool <name>", file=sys.stderr)
            return 2
        rc = guard_tail("helm codex unpool", rest[1:],
                        usage="codex unpool <name>")
        if rc is not None:
            return rc
        res = codex_unpool(rest[0])
        if "error" in res:
            return _fail(res)
        print("helm codex: " + (("removed " + ", ".join(res["removed"]))
                                if res["removed"] else res["note"]))
        return 0
    print("helm codex: unknown subverb %r (%s)"
          % (verb, cmd_codex.__doc__.strip().split("\n")[0]), file=sys.stderr)
    return 2
