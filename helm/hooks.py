#!/usr/bin/env python3
"""helm hooks — the self-closing installer for the crown-jewel wiring: every
claude credential home gets the UserPromptSubmit hook that pipes each turn's
FULL hook JSON to `helm inject --hook-json` (which derives the project scope
from the turn's cwd). Hand-wiring one home at a time was the adoption gap;
`install` closes it, `status` + doctor keep it closed.

The estate is TWO surfaces: the claude credential homes (~/.claude-homes/*,
~/.claude) get the full spec set (inject + the delivery lane); the multimodel
SEAT config dirs (<helm_home>/_global/seats/<family>/claude — seat.py's
isolated CLAUDE_CONFIG_DIRs) get the DELIVERY LANE (deliver + join +
stop-guard) so a launched codex/kimi/… seat receives fleet chat under its
family name (seat.py exports HELM_CHAT_NAME=<family> on launch; seats.py
derive_seat keys the roster on it) and cannot idle past it. The full loop:
inject (turn start) + deliver (tool boundary) + join (session start) +
stop-guard (idle gate). `install` covers both surfaces; `status` reports
coverage for both.

Laws:
  * MERGE-preserving: existing settings keys and foreign hook entries are
    NEVER clobbered; re-install is idempotent (an up-to-date entry reports ok);
    a stale helm entry (old path/old style) is updated in place, not doubled.
    A seat carries its own settings (model, permissions); the install only ever
    adds/updates helm's own delivery entries, never touches the rest.
  * Safety rails are configs.py's: backup -> validate -> atomic write; the file
    is re-parsed AFTER the write and the backup restored on any failure. A bad
    install must never brick a home's (or a seat's) launch. (configs.HOME_ROOTS
    recognizes the seat claude dirs so the same gated write path accepts them.)
  * FAIL-OPEN generated text (docs/HOOKS.md law): `timeout` + `|| true` — a
    missing or wedged helm injects nothing, never blocks a turn.
  * codex: docs/HOOKS.md carries no mechanical notify-hook recipe yet —
    reported honestly as pending, never guessed at.
"""
import difflib
import glob
import json
import os
import shlex
import shutil
import sys
import time

from . import configs, home, homes

HOOK_EVENT = "UserPromptSubmit"
TIMEOUT_S = 10  # inject is ~ms; 10s is the never-hold-a-turn ceiling

# The hook estate — one spec per event helm wires. inject is the crown jewel
# (per-turn context); deliver + join are the meld-half's delivery lane
# (tool-boundary chat nudge + session autojoin — seats.py); handoff-precompact
# + handoff-sessionend are the compaction-continuity contract (handoff.py's
# `check --hook-json` rides both triggers — it captures the now-snapshot AND
# nags when no handoff artifact exists, so the next window never starts blind).
# Same laws for every spec: merge-preserving, fail-open text, idempotent. `own`
# markers identify OUR entry in a settings file (so record.py's PostToolUse hook
# and any foreign entry are never touched); matcher rides events that take one —
# the continuity specs OMIT it (matcher=None) so they fire on EVERY compaction
# and EVERY session end, never trigger-gated (the safety-net's whole point).
SPECS = (
    {"name": "inject", "event": HOOK_EVENT, "args": "inject --hook-json",
     "timeout": TIMEOUT_S, "own": ("inject --hook-json", "helm inject"),
     "matcher": None},
    {"name": "deliver", "event": "PostToolUse", "args": "chat deliver --hook-json",
     "timeout": 2, "own": ("chat deliver --hook-json",), "matcher": "*"},
    {"name": "join", "event": "SessionStart", "args": "chat join --hook-json",
     "timeout": 5, "own": ("chat join --hook-json",), "matcher": "*"},
    # stop-guard: the IDLE GATE (the work-arbiter capability, helm-native). Blocks a stop
    # on undelivered mentions/held leases (once per pending-fingerprint),
    # warns to arm the beacon on a clean stop, silently runs the index cap.
    # Stop takes no matcher (like UserPromptSubmit).
    {"name": "stop-guard", "event": "Stop", "args": "chat stop-guard --hook-json",
     "timeout": 5, "own": ("chat stop-guard --hook-json",), "matcher": None},
    # continuity: the compaction/session-end handoff contract (sessions lane).
    {"name": "handoff-precompact", "event": "PreCompact",
     "args": "handoff check --hook-json", "timeout": 5,
     "own": ("handoff check --hook-json",), "matcher": None},
    {"name": "handoff-sessionend", "event": "SessionEnd",
     "args": "handoff check --hook-json", "timeout": 5,
     "own": ("handoff check --hook-json",), "matcher": None},
)

# The delivery lane alone (deliver + join + stop-guard, no inject) — what a
# SEAT's isolated CLAUDE_CONFIG_DIR receives so @<family> and owner posts
# reach it AND it cannot idle past them. inject (the per-turn context brief)
# stays a home concern; a seat joins the roster under its family name via
# HELM_CHAT_NAME (seat.py launch_line).
DELIVERY_SPECS = tuple(s for s in SPECS
                       if s["name"] in ("deliver", "join", "stop-guard"))

# The beacon's permission grease (owner hit it live): the SessionStart join
# line DIRECTS `Monitor(command: "helm chat wait … --follow")` as the
# mandatory first action, but a fresh home/seat has no allow rule for that
# command — so the very first act of every new session HANGS on a human
# permission prompt, and an unattended pane never arms its only wake path.
# install merges these into permissions.allow on every home AND every seat,
# through the same gated backup→validate→atomic write as the hooks block:
# additive + idempotent, existing allow entries are NEVER dropped. Both the
# Bash and Monitor rule forms ride together — the beacon command is the same
# either way the harness runs it.
PERMIT_RULES = ("Bash(helm chat wait:*)", "Monitor(helm chat wait:*)")


def helm_bin():
    """This checkout's bin/helm, absolute — the generated hook must resolve
    without assuming the hook-time PATH."""
    return os.path.realpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "helm"))


def spec_command(spec):
    """The generated hook text for one spec. Fail-open by construction:
    timeout so a wedged helm can never hold a turn, `|| true` so a
    missing/failing helm injects nothing instead of blocking (docs/HOOKS.md
    law)."""
    return "timeout %d %s %s || true" % (
        spec["timeout"], shlex.quote(helm_bin()), spec["args"])


def hook_command():
    """The inject spec's command — the name every older caller knows."""
    return spec_command(SPECS[0])


def _ours(cmd):
    """A hook command helm owns (installer-written or hand-wired helm inject)."""
    return "inject --hook-json" in cmd or "helm inject" in cmd


def _helm_of(cmd):
    """The helm executable token a hook command calls (the word before
    `inject`), None when unparseable."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    for i, t in enumerate(toks):
        if t == "inject" and i:
            return toks[i - 1]
    return None


def _resolvable(cmd):
    h = _helm_of(cmd)
    if not h:
        return False
    if os.path.sep in h:
        return os.path.isfile(h) and os.access(h, os.X_OK)
    return bool(shutil.which(h))


def _fail_open(cmd):
    """The never-block contract in the command text (docs/HOOKS.md law)."""
    return "|| true" in cmd


def claude_homes():
    """[(name, realpath)] per claude home: every real dir under the claude
    homes root plus the default ~/.claude — homes.py's ROOTS/DEFAULTS
    discovery; alias symlinks fold onto their target, archives never appear."""
    out, seen = [], set()
    for p in sorted(glob.glob(os.path.join(homes.ROOTS["claude"], "*"))):
        real = os.path.realpath(p)
        if os.path.islink(p) or not os.path.isdir(real) or real in seen:
            continue
        seen.add(real)
        out.append((os.path.basename(p), real))
    d = os.path.realpath(homes.DEFAULTS["claude"])
    if os.path.isdir(d) and d not in seen:
        out.append(("(default-claude)", d))
    return out


def seat_homes():
    """[(family, realpath)] per seat config dir — <helm_home>/_global/seats/
    <family>/claude, the seat's isolated CLAUDE_CONFIG_DIR (seat.py). These get
    the delivery lane so a launched seat receives fleet chat under its family
    name. Discovered by glob; configs.HOME_ROOTS recognizes the same dirs, so
    the merge-preserving gated write accepts them."""
    root = os.path.join(home.global_dir(), "seats")
    out = []
    for cdir in sorted(glob.glob(os.path.join(root, "*", "claude"))):
        real = os.path.realpath(cdir)
        if os.path.isdir(real):
            out.append((os.path.basename(os.path.dirname(cdir)), real))
    return out


def _hook_cmds(settings, event=HOOK_EVENT):
    """Every <event> command string in a settings dict (shape-tolerant)."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    out = []
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict):
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append(str(h["command"]))
    return out


def _matcher_ok(group, spec):
    """A spec with a matcher demands EXACTLY that matcher on its group — a
    stale `PostToolUse` group pinned to `Bash` silently misses most tool
    boundaries (codex B3). Specs without one (UserPromptSubmit) don't care."""
    return spec["matcher"] is None or group.get("matcher") == spec["matcher"]


def _canonical_entry(spec):
    entry = {"hooks": [{"type": "command", "command": spec_command(spec)}]}
    if spec["matcher"]:
        entry["matcher"] = spec["matcher"]
    return entry


def _merge_event(out, spec):
    """Merge ONE spec's entry into `out` IN PLACE -> action ok|add|update.
    An owned entry is CURRENT only when command, type AND the containing
    group's matcher all match (codex B3). A wrong matcher is repaired in
    place when the group is exclusively ours; with foreign co-tenants our
    hook relocates to a canonical group and the foreigners keep their group
    byte-identical. MERGE-preserving throughout: only the entry carrying
    this spec's own-marker is ever written. Raises ValueError on a shape we
    must not touch."""
    cmd = spec_command(spec)
    own = spec["own"]
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing 'hooks' key is not an object — fix it by hand")
    groups = hooks.setdefault(spec["event"], [])
    if not isinstance(groups, list):
        raise ValueError("existing hooks.%s is not a list — fix it by hand"
                         % spec["event"])
    for g in groups:
        if not isinstance(g, dict):
            continue
        hlist = g.get("hooks") or []
        for h in hlist:
            if not (isinstance(h, dict) and any(m in str(h.get("command") or "")
                                                for m in own)):
                continue
            if h.get("command") == cmd and h.get("type") == "command" \
                    and _matcher_ok(g, spec):
                return "ok"
            h["command"] = cmd
            h["type"] = "command"
            if _matcher_ok(g, spec):
                return "update"          # command/type were the stale part
            if len(hlist) == 1:          # the group is ours alone — repair it
                if spec["matcher"] is None:
                    g.pop("matcher", None)
                else:
                    g["matcher"] = spec["matcher"]
                return "update"
            hlist.remove(h)              # foreign co-tenants stay untouched
            groups.append(_canonical_entry(spec))
            return "update"
    groups.append(_canonical_entry(spec))
    return "add"


def _merge_permits(out):
    """Merge PERMIT_RULES into permissions.allow IN PLACE -> ok|add. Additive
    only: existing entries (and every sibling permissions key — deny, ask,
    defaultMode …) survive byte-identical; a home with no permissions key
    gains one. Raises ValueError on a shape we must not touch."""
    perms = out.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError("existing 'permissions' key is not an object — fix it by hand")
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        raise ValueError("existing permissions.allow is not a list — fix it by hand")
    missing = [r for r in PERMIT_RULES if r not in allow]
    allow.extend(missing)
    return "add" if missing else "ok"


def _permits_live(settings):
    """Every PERMIT_RULE present in permissions.allow (the post-write check)."""
    perms = settings.get("permissions") if isinstance(settings, dict) else None
    allow = perms.get("allow") if isinstance(perms, dict) else None
    return isinstance(allow, list) and all(r in allow for r in PERMIT_RULES)


def _merge_all(settings, specs=SPECS):
    """-> (merged_copy, {spec_name: action}) across `specs` — the whole estate
    for a home (SPECS), the delivery lane for a seat (DELIVERY_SPECS) — plus
    the beacon permit rules (every surface that gets the delivery lane must
    also be ABLE to arm the beacon without a human prompt)."""
    out = json.loads(json.dumps(settings))  # deep copy — never mutate the input
    actions = {s["name"]: _merge_event(out, s) for s in specs}
    actions["permits"] = _merge_permits(out)
    return out, actions


def _agg(actions):
    """One home's aggregate action, worst-first (fail > update > add > ok)."""
    for a in ("fail", "update", "add"):
        if a in actions.values():
            return a
    return "ok"


def install_home(path, dry=False, specs=SPECS):
    """Install/refresh `specs` in <path>/settings.json — the full estate for a
    home (SPECS), the delivery lane for a seat (DELIVERY_SPECS).
    -> (action, detail): ok|add|update|dry-add|dry-update|fail."""
    sp = os.path.join(path, "settings.json")
    raw, cur = "", {}
    if os.path.isfile(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                raw = f.read()
            cur = json.loads(raw)
        except (OSError, ValueError) as e:
            return "fail", "settings.json unreadable (%s) — refusing to touch it" % e
        if not isinstance(cur, dict):
            return "fail", "settings.json root is not an object — refusing to touch it"
    try:
        merged, actions = _merge_all(cur, specs)
    except ValueError as e:
        return "fail", str(e)
    action = _agg(actions)
    if action == "ok":
        return "ok", "hook up to date"
    new_raw = json.dumps(merged, indent=2) + "\n"
    if dry:
        diff = difflib.unified_diff(
            raw.splitlines(), new_raw.splitlines(),
            sp, sp + " (after install)", lineterm="")
        return "dry-" + action, "\n".join(diff)
    res = configs.write_file(sp, new_raw)  # backup -> validate -> atomic
    if res.get("error"):
        return "fail", res["error"]
    try:  # JSON-validate AFTER the write; anything torn restores the backup
        with open(sp, encoding="utf-8") as f:
            got = json.load(f)
        ok = all(spec_command(s) in _hook_cmds(got, s["event"]) for s in specs) \
            and _permits_live(got)
    except (OSError, ValueError):
        ok = False
    if not ok:
        note = "no pre-write backup existed"
        if res.get("backup"):
            r = configs.restore(res["backup"])
            note = "backup restored" if r.get("ok") else \
                "restore ALSO failed: %s" % r.get("error")
        return "fail", "post-write validation failed — " + note
    return action, "backup: %s" % (res.get("backup") or "none — new file")


def _lane_live(settings, spec):
    """A delivery lane counts as live ONLY on the exact spec command, with
    type "command", inside a group whose matcher matches the spec (codex
    B3 + final delta): a marker substring under a `Bash`-pinned group — or
    the right command string under a foreign type — is a stale install the
    harness won't run as we expect, not coverage."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(spec["event"]) if isinstance(hooks, dict) else None
    cmd = spec_command(spec)
    for g in groups if isinstance(groups, list) else []:
        if not (isinstance(g, dict) and _matcher_ok(g, spec)):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == cmd \
                    and h.get("type") == "command":
                return True
    return False


def status_rows():
    """Per-claude-home coverage: inject hook present? helm resolvable?
    fail-open contract present? Plus the delivery lane's two booleans —
    exact command + matcher validated, never marker presence. Read-only."""
    rows = []
    for name, path in claude_homes():
        cmd, settings = None, {}
        try:
            with open(os.path.join(path, "settings.json"), encoding="utf-8") as f:
                settings = json.load(f)
            cmd = next((c for c in _hook_cmds(settings) if _ours(c)), None)
        except (OSError, ValueError):
            pass
        lanes = {s["name"]: _lane_live(settings, s) for s in SPECS[1:]}
        rows.append({"home": name, "path": path, "hook": bool(cmd), "command": cmd,
                     "resolvable": bool(cmd) and _resolvable(cmd),
                     "fail_open": bool(cmd) and _fail_open(cmd),
                     "permits": _permits_live(settings), **lanes})
    return rows


def coverage():
    """(covered, total) claude homes — covered = hook present, helm resolvable,
    fail-open contract intact."""
    rows = status_rows()
    return (sum(1 for r in rows
                if r["hook"] and r["resolvable"] and r["fail_open"]), len(rows))


def seat_status_rows():
    """Per-seat delivery-lane coverage: does the seat's claude/settings.json
    carry the deliver + join + stop-guard hooks (exact command + matcher + type
    validated, never marker presence — same _lane_live law as homes)? Read-only.
    inject is not a seat concern, so it is not reported here."""
    rows = []
    for name, path in seat_homes():
        settings = {}
        try:
            with open(os.path.join(path, "settings.json"), encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            pass
        lanes = {s["name"]: _lane_live(settings, s) for s in DELIVERY_SPECS}
        rows.append({"seat": name, "path": path,
                     "permits": _permits_live(settings), **lanes})
    return rows


def seat_coverage():
    """(covered, total) seats — covered = the whole delivery lane (deliver +
    join + stop-guard) live in the seat's claude config dir."""
    rows = seat_status_rows()
    names = [s["name"] for s in DELIVERY_SPECS]
    return (sum(1 for r in rows if all(r[n] for n in names)), len(rows))


# ── the retrofit surface (G-seatlaunch-installs): a pane that launched
# BEFORE its identity/hooks existed sits idle forever — no hook ever fires
# in an idle PTY, so it can never self-heal into delivery (a live seat:
# running, absent from the roster, its @-mention routing nowhere). The only
# fix is a relaunch, so install/launch SURFACE the uncovered running panes.

def running_panes(proc=None):
    """[{pid, seat, config_dir, family}] for every live claude-harness PTY of
    this uid: /proc scan — cmdline argv0/argv1 basename 'claude', environ
    read for HELM_CHAT_NAME / CLAUDE_CONFIG_DIR (a seat config dir names its
    family). Other-uid entries are unreadable and skipped; HELM_PROC
    overrides the proc root (tests); fail-open []."""
    proc = proc or home.env("PROC") or "/proc"
    out = []
    try:
        pids = sorted(n for n in os.listdir(proc) if n.isdigit())
    except OSError:
        return out
    sroot = os.path.realpath(os.path.join(home.global_dir(), "seats"))
    for pid in pids:
        base = os.path.join(proc, pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = [a for a in f.read(65536).split(b"\0") if a]
            words = [a.decode("utf-8", "replace") for a in argv[:2]]
            if not any(os.path.basename(w) == "claude" for w in words):
                continue
            with open(os.path.join(base, "environ"), "rb") as f:
                env = dict(kv.split(b"=", 1)
                           for kv in f.read(1 << 20).split(b"\0") if b"=" in kv)
        except OSError:
            continue

        def val(k):
            v = env.get(k.encode())
            return v.decode("utf-8", "replace") if v else None

        cdir, family = val("CLAUDE_CONFIG_DIR"), None
        if cdir:
            real = os.path.realpath(cdir)
            if os.path.dirname(os.path.dirname(real)) == sroot:
                family = os.path.basename(os.path.dirname(real))
        out.append({"pid": int(pid), "seat": val("HELM_CHAT_NAME") or
                    val("MELD_CHAT_NAME"), "config_dir": cdir, "family": family,
                    # signing readiness rides the scan (the environ is already
                    # in hand): a pane launched before launch_line baked the
                    # trio posts [unsigned] by configuration and cannot
                    # self-heal — surface it, never let it break silently.
                    "signer_bin": val("HELM_CELL_BIN") or val("MELD_CELL_BIN"),
                    "signer_profile": val("DREGG_PROFILE") or
                    val("HELM_CELL_PROFILE")})
    return out


def uncovered_panes(proc=None, quiet_s=900):
    """Running claude PTYs whose NAMED chat identity is not live on the
    roster (+reason) — covered = the pane's seat name (HELM_CHAT_NAME, else
    its seat family) has a roster row seen within quiet_s. Un-named home
    panes are NOT judged here: their auto-name binds via session id, which
    a /proc scan cannot see — their coverage check is `helm hooks status`
    (the hooks ARE the join path). Read-only; fail-open []."""
    try:
        from . import seats
        panes = [p for p in running_panes(proc) if p["seat"] or p["family"]]
        if not panes:
            return []
        now, r, out = time.time(), seats.roster(), []
        for p in panes:
            name = p["seat"] or p["family"]
            row = r.get(name)
            ls = seats.last_seen(name, row) if row else None
            if row and ls and now - ls < quiet_s:
                continue
            # the seat name is a raw HELM_CHAT_NAME (from /proc environ, the
            # unvalidated join seam) — launder it INTO the reason so the
            # printed line cannot reshape the operator's terminal.
            lbl = seats._seat_label(name)
            p["reason"] = ("no roster row for '%s' — never joined" % lbl
                           if not row else "roster row for '%s' is stale "
                           "(%.0fm quiet)" % (lbl, (now - (ls or 0)) / 60))
            out.append(p)
        return out
    except Exception:
        return []


def unsigned_panes(proc=None):
    """Running NAMED panes that cannot sign their posts: HELM_CELL_BIN unset
    or pointing at a non-executable (cell.bin_ready's law), or the profile
    pair missing (DREGG_PROFILE/HELM_CELL_PROFILE) — launch_line has carried
    the trio since seat-signing landed, so such a pane predates it and posts
    [unsigned] by configuration. Read-only; fail-open []."""
    try:
        from . import cell
        out = []
        for p in running_panes(proc):
            if not (p["seat"] or p["family"]):
                continue
            b = p.get("signer_bin")
            if not cell._usable(b):
                p["sign_reason"] = "no HELM_CELL_BIN (pre-signing launch)"
            elif not p.get("signer_profile"):
                p["sign_reason"] = "no DREGG_PROFILE/HELM_CELL_PROFILE"
            else:
                continue
            out.append(p)
        return out
    except Exception:
        return []


def surface_uncovered(out=None):
    """Print the uncovered running panes — called by `helm hooks install`
    and `helm seat launch`: nothing external can wake an idle PTY agent, so
    a relaunch (human/driver) is the only repair and SURFACING is the lever.
    Same law for signing: a pane launched before the signing trio landed in
    launch_line posts [unsigned] forever (a process cannot retrofit its own
    environment) — name those too (no-silent-break)."""
    from . import seats
    out = out or sys.stdout
    rows = uncovered_panes()
    if rows:
        print("helm hooks: %d running pane(s) NOT receiving fleet chat — relaunch "
              "them (an idle pane cannot self-heal into delivery):" % len(rows),
              file=out)
        for p in rows:
            # launder the printed name column — a hostile HELM_CHAT_NAME rides
            # p["seat"]/p["family"] raw from the /proc scan.
            print("  pid %-7d %-14s %s" % (
                p["pid"], seats._seat_label(p["seat"] or p["family"] or "?"),
                p["reason"]), file=out)
    sign = unsigned_panes()
    if sign:
        print("helm hooks: %d running pane(s) posting UNSIGNED (no signing env) — "
              "relaunch from their minted launch.sh to sign:" % len(sign), file=out)
        for p in sign:
            print("  pid %-7d %-14s %s" % (
                p["pid"], seats._seat_label(p["seat"] or p["family"] or "?"),
                p["sign_reason"]), file=out)


_CODEX_PENDING = ("codex: recipe pending — docs/HOOKS.md carries no mechanical "
                  "notify-hook shape yet; wire it by hand per that doc's codex section")

_USAGE = """usage: helm hooks install [--harness claude|codex] [--home NAME] [--dry]
       helm hooks status
       helm hooks sync [--apply]   (reconcile every home to the canonical set)"""


def _select_homes(name):
    all_homes = claude_homes()
    if not name:
        return all_homes, None
    want = os.path.realpath(os.path.expanduser(name)) if os.path.sep in name else None
    hits = [(n, p) for n, p in all_homes if n == name or p == want]
    if hits:
        return hits, None
    return [], "unknown claude home %r (have: %s)" % (
        name, ", ".join(n for n, _ in all_homes) or "none")


def cmd_hooks(args):
    """hooks [install [--harness claude|codex] [--home NAME] [--dry] | status]
    — self-wire the per-turn inject hook into every claude home, and the
    fleet-delivery lane (deliver + join) into every seat config dir."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

    if verb == "sync":   # reconcile every home to the canonical set (envtidy)
        from . import envtidy
        return envtidy.cmd_hooks_sync(rest)

    if verb == "status":
        from .cli import guard_tail
        rc = guard_tail("helm hooks status", rest, usage="hooks status")
        if rc is not None:
            return rc
        rows = status_rows()
        if not rows:
            print("helm hooks: no claude homes found")
            return 0
        print("helm hooks status (claude):")
        print("  %-28s %-5s %-5s %-9s %-7s %-5s %-5s %s" % (
            "home", "hook", "helm", "fail-open", "deliver", "join", "stop",
            "handoff"))
        def hoff(r):  # the continuity lane is live only when BOTH triggers are
            return r.get("handoff-precompact") and r.get("handoff-sessionend")
        for r in rows:
            print("  %-28s %-5s %-5s %-9s %-7s %-5s %-5s %s" % (
                r["home"], "yes" if r["hook"] else "-",
                "ok" if r["hook"] and r["resolvable"] else ("NO" if r["hook"] else "-"),
                "ok" if r["hook"] and r["fail_open"] else ("NO" if r["hook"] else "-"),
                "yes" if r.get("deliver") else "-",
                "yes" if r.get("join") else "-",
                "yes" if r.get("stop-guard") else "-",
                "yes" if hoff(r) else "-"))
        n, m = coverage()
        line = "inject coverage: %d of %d claude homes" % (n, m)
        print(line if n == m else line + " — `helm hooks install` closes the gap")
        lanes = [s["name"] for s in DELIVERY_SPECS]
        d = sum(1 for r in rows if all(r.get(k) for k in lanes))
        if d < m:
            print("delivery lane (chat deliver/join/stop-guard): %d of %d homes"
                  " — `helm hooks install` wires it" % (d, m))
        c = sum(1 for r in rows if hoff(r))
        if c < m:
            print("continuity lane (handoff PreCompact/SessionEnd): %d of %d homes — "
                  "`helm hooks install` wires it" % (c, m))
        p = sum(1 for r in rows if r.get("permits"))
        if p < m:
            print("beacon permit (permissions.allow %s): %d of %d homes — "
                  "`helm hooks install` grants it (without it every fresh "
                  "session hangs on a human prompt arming its wake beacon)"
                  % (PERMIT_RULES[0], p, m))
        srows = seat_status_rows()
        if srows:
            print("seats (fleet delivery — chat deliver/join/stop-guard):")
            print("  %-28s %-8s %-5s %s" % ("seat", "deliver", "join", "stop"))
            for r in srows:
                print("  %-28s %-8s %-5s %s" % (
                    r["seat"], "yes" if r["deliver"] else "NO",
                    "yes" if r["join"] else "NO",
                    "yes" if r["stop-guard"] else "NO"))
            sc, st = seat_coverage()
            sline = "seat delivery: %d of %d seats" % (sc, st)
            print(sline if sc == st else sline + " — `helm hooks install` wires it")
            sp = sum(1 for r in srows if r.get("permits"))
            if sp < st:
                print("seat beacon permit: %d of %d seats — `helm hooks "
                      "install` grants it" % (sp, st))
        print(_CODEX_PENDING)
        return 0

    if verb == "install":
        # refuse junk BEFORE any home is touched — `hooks install --bogus`
        # used to run the full install and exit 0 as if --bogus existed.
        from .cli import guard_tail
        rc = guard_tail("helm hooks install", rest, flags=("--dry",),
                        valued=("--harness", "--home"),
                        usage="hooks install [--harness claude|codex] "
                              "[--home NAME] [--dry]")
        if rc is not None:
            return rc
        harness = "claude"
        home_name = None
        dry = "--dry" in rest
        if "--harness" in rest:
            i = rest.index("--harness")
            harness = rest[i + 1] if i + 1 < len(rest) else ""
        if "--home" in rest:
            i = rest.index("--home")
            home_name = rest[i + 1] if i + 1 < len(rest) else None
        if harness not in ("claude", "codex"):
            print("helm hooks: unknown harness %r (claude | codex)" % harness,
                  file=sys.stderr)
            return 2
        if harness == "codex":
            print("helm hooks: " + _CODEX_PENDING)
            return 0
        targets, err = _select_homes(home_name)
        if err:
            print("helm hooks: " + err, file=sys.stderr)
            return 1
        if not targets:
            print("helm hooks: no claude homes found — `helm homes prepare` starts one")
            return 0
        for s in SPECS:
            print("helm hooks: %s (%s): %s" % (s["name"], s["event"], spec_command(s)))
        failed = 0
        def _apply(name, path, specs):
            nonlocal failed
            action, detail = install_home(path, dry=dry, specs=specs)
            failed += action == "fail"
            if action.startswith("dry-"):
                print("  %-28s %s (dry — nothing written)" % (name, action[4:]))
                if detail:
                    print("    " + detail.replace("\n", "\n    "))
            else:
                print("  %-28s %-6s %s" % (name, action, detail))
        for name, path in targets:
            _apply(name, path, SPECS)
        # seats get the delivery lane (deliver + join + stop-guard) — only on
        # a full install; a --home-narrowed run stays scoped to that one home.
        seats = seat_homes() if home_name is None else []
        if seats:
            print("helm hooks: seats (fleet delivery — deliver + join + stop-guard):")
            for name, path in seats:
                _apply(name, path, DELIVERY_SPECS)
        if not dry:
            n, m = coverage()
            print("helm hooks: %d of %d claude homes covered" % (n, m))
            sc, st = seat_coverage()
            if st:
                print("helm hooks: %d of %d seats covered (fleet delivery)"
                      % (sc, st))
            surface_uncovered()   # settings fixed ≠ live panes fixed — a pane
        return 1 if failed else 0  # launched pre-install still needs a relaunch

    print("helm hooks: unknown subverb %r" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
