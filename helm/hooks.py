#!/usr/bin/env python3
"""helm hooks — the self-closing installer for the crown-jewel wiring: every
claude credential home gets the UserPromptSubmit hook that pipes each turn's
FULL hook JSON to `helm inject --hook-json` (which derives the project scope
from the turn's cwd). Hand-wiring one home at a time was the adoption gap;
`install` closes it, `status` + doctor keep it closed.

Laws:
  * MERGE-preserving: existing settings keys and foreign hook entries are
    NEVER clobbered; re-install is idempotent (an up-to-date entry reports ok);
    a stale helm entry (old path/old style) is updated in place, not doubled.
  * Safety rails are configs.py's: backup -> validate -> atomic write; the file
    is re-parsed AFTER the write and the backup restored on any failure. A bad
    install must never brick a home's launch.
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

from . import configs, homes

HOOK_EVENT = "UserPromptSubmit"
TIMEOUT_S = 10  # inject is ~ms; 10s is the never-hold-a-turn ceiling


def helm_bin():
    """This checkout's bin/helm, absolute — the generated hook must resolve
    without assuming the hook-time PATH."""
    return os.path.realpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "helm"))


def hook_command():
    """The exact generated hook text. Fail-open by construction: timeout so a
    wedged helm can never hold a turn, `|| true` so a missing/failing helm
    injects nothing instead of blocking (docs/HOOKS.md law)."""
    return "timeout %d %s inject --hook-json || true" % (TIMEOUT_S, shlex.quote(helm_bin()))


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


def _hook_cmds(settings):
    """Every UserPromptSubmit command string in a settings dict (shape-tolerant)."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(HOOK_EVENT) if isinstance(hooks, dict) else None
    out = []
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict):
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append(str(h["command"]))
    return out


def _merge_hook(settings, cmd):
    """-> (merged_copy, action ok|add|update). MERGE-preserving: only OUR entry
    is ever written; foreign hooks and every other settings key survive
    byte-identical. Raises ValueError on a shape we must not touch."""
    out = json.loads(json.dumps(settings))  # deep copy — never mutate the input
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing 'hooks' key is not an object — fix it by hand")
    groups = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise ValueError("existing hooks.%s is not a list — fix it by hand" % HOOK_EVENT)
    for g in groups:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and _ours(str(h.get("command") or "")):
                if h.get("command") == cmd:
                    return out, "ok"
                h["command"] = cmd
                h["type"] = "command"
                return out, "update"
    groups.append({"hooks": [{"type": "command", "command": cmd}]})
    return out, "add"


def install_home(path, dry=False):
    """Install/refresh the inject hook in <path>/settings.json.
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
    cmd = hook_command()
    try:
        merged, action = _merge_hook(cur, cmd)
    except ValueError as e:
        return "fail", str(e)
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
            ok = cmd in _hook_cmds(json.load(f))
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


def status_rows():
    """Per-claude-home coverage: hook present? helm resolvable? fail-open
    contract present? Read-only."""
    rows = []
    for name, path in claude_homes():
        cmd = None
        try:
            with open(os.path.join(path, "settings.json"), encoding="utf-8") as f:
                cmds = _hook_cmds(json.load(f))
            cmd = next((c for c in cmds if _ours(c)), None)
        except (OSError, ValueError):
            pass
        rows.append({"home": name, "path": path, "hook": bool(cmd), "command": cmd,
                     "resolvable": bool(cmd) and _resolvable(cmd),
                     "fail_open": bool(cmd) and _fail_open(cmd)})
    return rows


def coverage():
    """(covered, total) claude homes — covered = hook present, helm resolvable,
    fail-open contract intact."""
    rows = status_rows()
    return (sum(1 for r in rows
                if r["hook"] and r["resolvable"] and r["fail_open"]), len(rows))


_CODEX_PENDING = ("codex: recipe pending — docs/HOOKS.md carries no mechanical "
                  "notify-hook shape yet; wire it by hand per that doc's codex section")

_USAGE = """usage: helm hooks install [--harness claude|codex] [--home NAME] [--dry]
       helm hooks status"""


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
    — self-wire the per-turn inject hook into every claude home."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

    if verb == "status":
        rows = status_rows()
        if not rows:
            print("helm hooks: no claude homes found")
            return 0
        print("helm hooks status (claude):")
        print("  %-28s %-5s %-5s %s" % ("home", "hook", "helm", "fail-open"))
        def mark(r, k):
            if not r["hook"]:
                return "-"
            return "ok" if r[k] else "NO"
        for r in rows:
            print("  %-28s %-5s %-5s %s" % (
                r["home"], "yes" if r["hook"] else "-",
                mark(r, "resolvable"), mark(r, "fail_open")))
        n, m = coverage()
        line = "inject coverage: %d of %d claude homes" % (n, m)
        print(line if n == m else line + " — `helm hooks install` closes the gap")
        print(_CODEX_PENDING)
        return 0

    if verb == "install":
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
        print("helm hooks: command: " + hook_command())
        failed = 0
        for name, path in targets:
            action, detail = install_home(path, dry=dry)
            failed += action == "fail"
            if action.startswith("dry-"):
                print("  %-28s %s (dry — nothing written)" % (name, action[4:]))
                if detail:
                    print("    " + detail.replace("\n", "\n    "))
            else:
                print("  %-28s %-6s %s" % (name, action, detail))
        if not dry:
            n, m = coverage()
            print("helm hooks: %d of %d claude homes covered" % (n, m))
        return 1 if failed else 0

    print("helm hooks: unknown subverb %r" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
