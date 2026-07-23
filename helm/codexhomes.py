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

Every public function returns a JSON-able dict (or list); errors are
{"error": "..."} — loud, attributed, never an exception across the API edge.
"""
import glob
import json
import os
import sys
import time

from . import home
from .seat import _jwt_claims, _write_private, seat_dir, translate_codex_auth

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
    return os.path.join(seat_dir("codex"), "auth")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
    idc = _jwt_claims(t.get("id_token"))
    acc = _jwt_claims(t.get("access_token"))
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


def codex_pool(name):
    """Translate one codexhome's auth.json into the proxy flat record and
    write it 0600 into the pool dir as codex-<canonical-name>.json.
    Idempotent — re-pooling refreshes the copy (that IS the stale-401 cure).
    The source file is never touched; no token material in the result."""
    canonical, real, err = _resolve_home(name)
    if err:
        return err
    src = os.path.join(real, "auth.json")
    if not os.path.exists(src):
        return {"error": "%s has no auth.json — not logged in; human-only: "
                         "CODEX_HOME=%s codex login --device-auth" % (real, real)}
    rec, _fname, terr = translate_codex_auth(src)
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
    existed = os.path.exists(dest)
    _write_pool_atomic(dest, json.dumps(rec, indent=2) + "\n")
    warn = None
    if exp is not None and exp <= time.time():
        warn = ("access token exp is past — the codex CLI autorefreshes, so run "
                "`CODEX_HOME=%s codex` once and re-pool the FRESH token (the "
                "proxy's own refresh can 401 on a stale copy)" % real)
    return {"ok": True, "pooled": os.path.basename(dest), "path": dest,
            "email": email or rec.get("email"), "account_id": account_id,
            "tier": tier(plan) if plan else "?", "updated": existed,
            "also_pooled_as": dupes or None, "warn": warn,
            "note": "the proxy hot-reloads its auth-dir — no restart needed"}


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
    from . import seat as _seat
    return _seat.cmd_seat(["launch", "codex"] + list(rest))


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
    """codex [list] | pool <name> | unpool <name> | pooled | capacity | launch
    [-i N] [--force] [--room R] [--model M] — codexhome roster (ultra/team) +
    proxy cred pooling (auth.json -> 0600 pool file). launch = the cred-%
    gate (runbook fix #3) ahead of the seat-launch mint."""
    args = list(args)
    verb, rest = (args[0], args[1:]) if args else ("list", [])
    if verb == "list":
        return _print_list()
    if verb == "pooled":
        return _print_pooled()
    if verb == "capacity":
        return _print_capacity()
    if verb == "launch":
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
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
    if verb == "pool":
        if not rest:
            print("usage: helm codex pool <name>", file=sys.stderr)
            return 2
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
        res = codex_unpool(rest[0])
        if "error" in res:
            return _fail(res)
        print("helm codex: " + (("removed " + ", ".join(res["removed"]))
                                if res["removed"] else res["note"]))
        return 0
    print("helm codex: unknown subverb %r (%s)"
          % (verb, cmd_codex.__doc__.strip().split("\n")[0]), file=sys.stderr)
    return 2
