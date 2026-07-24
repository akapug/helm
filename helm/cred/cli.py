"""helm.cred doctor + CLI cluster (see __init__)."""
import json
import os
import sys

from .. import cred as _cred
from .. import homes
from ._common import (ACCOUNT_JSON, AUTH_JSON, GUARD_SPECS, _display_path,
                      backup_root)
from .heal import heal
from .snapshots import backup_all, snapshots


# ------------------------------------------------------------------ doctor ---
def doctor_rows():
    """[(level, msg)] for helm doctor: the loud drift row + the missing-backup
    row. Read-only; an audit that cannot run WARNs, it never claims health."""
    try:
        rs = _cred.rows()
    except Exception as e:                       # no exception text: it may carry a path/token
        return [("WARN", "cred identity audit unavailable (%s)"
                 % e.__class__.__name__)]
    if not rs:
        return []
    out = []
    for r in rs:
        if r["verdict"] == "DRIFT":
            out.append(("WARN",
                        "credhome %s HOLDS %s (drift — that account's home is %s); "
                        "%s, `helm cred list` shows the whole estate"
                        % (_display_path(r["name"]), r["account"], r["wants_home"],
                           "`helm cred heal` restores %s from its %d snapshot%s"
                           % (_display_path(r["name"]), r["named_backups"],
                              "s"[:r["named_backups"] != 1]) if r["named_backups"]
                           else "and NOTHING was snapshotted for %s — only a fresh "
                                "login brings it back" % _display_path(r["name"]))))
        elif r["verdict"] == "UNKNOWN" and r["authed"]:
            out.append(("WARN", "credhome %s holds credentials but its identity is "
                                "unreadable (%s) — no account claimed"
                        % (_display_path(r["name"]), r["error"])))
    accounts = sorted({r["account"] for r in rs if r["account"]})
    missing = [a for a in accounts if not snapshots(a)]
    for a in missing:
        out.append(("WARN", "no cred backup for %s — `helm cred backup --all` is "
                            "what makes the next /login reversible" % a))
    if not out:
        out.append(("OK", "cred homes: %d claude home%s, every dir name matches the "
                          "account it holds; %d account%s backed up"
                    % (len(rs), "s"[:len(rs) != 1],
                       len(accounts), "s"[:len(accounts) != 1])))
    return out


# --------------------------------------------------------------------- cli ---
def _take_flag(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            return None
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def _public_paths(value, key=None):
    """Redact token-shaped values only in fields whose schema is a path/label."""
    if isinstance(value, dict):
        return {k: _public_paths(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_public_paths(v, key) for v in value]
    if isinstance(value, str) and key in {
            "path", "real", "home", "dest", "restore_from", "pre_image",
            "source_home", "name", "aliases", "wants_account_folded"}:
        return _display_path(value)
    return value


def _resolve_home(name):
    """--home NAME | path -> realpath, via the same resolver every other verb
    uses. No name -> $CLAUDE_CONFIG_DIR -> the default home."""
    if name:
        row, err = homes._resolve(name, "claude")
        if err:
            return None, "home could not be resolved (see `helm cred list`)"
        return os.path.realpath(row["path"]), None
    env = os.environ.get(homes.ENV_VAR["claude"])
    if env:
        return os.path.realpath(os.path.expanduser(env)), None
    return os.path.realpath(homes.DEFAULTS["claude"]), None


def _print_list(args):
    as_json = "--json" in args
    rs = _cred.rows()
    if as_json:
        print(json.dumps(_public_paths(rs), indent=2))
        return 0
    if not rs:
        print("helm cred: no claude credential homes found")
        return 0
    print("helm cred — identity read from CONTENT (%s oauthAccount), never from "
          "the dir name:" % ACCOUNT_JSON)
    print("  %-30s %-32s %-8s %-8s %s"
          % ("DIR NAME", "ACTUAL ACCOUNT", "VERDICT", "BACKUPS", "NOTE"))
    for r in sorted(rs, key=lambda r: (r["default"], r["name"])):
        note = []
        if r["verdict"] == "DRIFT":
            note.append("this account's home is %s" % r["wants_home"])
            note.append("heal can restore %s (%d snapshot%s)"
                        % (_display_path(r["name"]), r["named_backups"],
                           "s"[:r["named_backups"] != 1]) if r["named_backups"]
                        else "NO snapshot of %s — only a fresh login brings it "
                             "back" % _display_path(r["name"]))
        if r["verdict"] == "UNKNOWN":
            note.append(r["error"] or "identity unreadable")
        if r["aliases"]:
            note.append("alias: " + ",".join(_display_path(a) for a in r["aliases"]))
        if r["live_pids"]:
            note.append("live pids " + ",".join(map(str, r["live_pids"])))
        if not r["authed"]:
            note.append("no %s" % AUTH_JSON)
        print("  %-30s %-32s %-8s %-8s %s"
              % (_display_path(r["name"])[:30], (r["account"] or "-")[:32], r["verdict"],
                 r["backups"], "; ".join(note)))
    drift = [r for r in rs if r["verdict"] == "DRIFT"]
    print("helm cred: %d home%s, %d drift%s%s"
          % (len(rs), "s"[:len(rs) != 1], len(drift), "" if len(drift) == 1 else "s",
             " — `helm cred heal` (dry-run) shows the repair" if drift else ""))
    return 0


def _print_backup(args):
    args = list(args)
    quiet = "--quiet" in args
    apply = "--apply" in args
    args = [a for a in args if a not in ("--quiet", "--apply")]
    name = _take_flag(args, "--home")
    all_homes = "--all" in args
    args = [a for a in args if a != "--all"]
    if args or (all_homes and name):
        if not quiet:
            print("helm cred backup: invalid arguments", file=sys.stderr)
        return 2
    if all_homes:
        results = backup_all(apply=apply)
    else:
        path, err = _resolve_home(name)
        if err:
            if not quiet:
                print("helm cred: " + err, file=sys.stderr)
            return 1
        results = [_cred.backup(path, apply=apply)]
    if quiet:
        return 1 if any(not r["ok"] for r in results) else 0
    made = [r for r in results if r["action"] == "backup"]
    planned = [r for r in results if r["action"] == "would-backup"]
    for r in results:
        if r["action"] == "backup":
            print("  backed up %-32s <- %s  (%s)"
                  % (r["account"], _display_path(r["name"]), _display_path(r["dest"])))
        elif r["action"] == "would-backup":
            print("  WOULD BACK UP %-26s <- %s" % (r["account"], _display_path(r["name"])))
        elif r["ok"]:
            print("  %-32s %s (%s)" % (r["account"], r["reason"], _display_path(r["name"])))
        else:
            print("  SKIP %-27s %s" % (_display_path(r["name"]), r["reason"]))
    if apply:
        print("helm cred backup (APPLIED): %d snapshot%s written, %d already current "
              "(root %s, 0700 dirs / 0600 files — token bytes are copied, never printed)"
              % (len(made), "s"[:len(made) != 1], len(results) - len(made),
                 _display_path(backup_root())))
    else:
        print("helm cred backup (dry-run): %d snapshot%s would be written; add --apply "
              "(%d already current, no filesystem changes)"
              % (len(planned), "s"[:len(planned) != 1], len(results) - len(planned)))
    return 1 if any(not r["ok"] for r in results) else 0


def _print_switch_guard(args):
    args = list(args)
    apply = "--apply" in args
    args = [a for a in args if a not in ("--apply", "--dry")]
    install = "--install" in args
    args = [a for a in args if a != "--install"]
    name = _take_flag(args, "--home")
    if args or (install and name):
        print("helm cred switch-guard: invalid arguments", file=sys.stderr)
        return 2
    if install:
        from .. import hooks
        targets = hooks.claude_homes()
        if not targets:
            print("helm cred: no claude homes to guard")
            return 0
        worst = 0
        for hname, path in targets:
            action, detail = hooks.install_home(path, dry=not apply, specs=GUARD_SPECS)
            print("  %-30s %s" % (hname, action))
            if action == "fail":
                print("    hook install failed", file=sys.stderr)
                worst = 1
        print("helm cred switch-guard (%s): SessionStart + Stop backup+heal "
              "guard %s in %d home%s"
              % ("APPLIED" if apply else "dry-run — add --apply",
                 "installed" if apply else "would be installed",
                 len(targets), "s"[:len(targets) != 1]))
        return worst
    path, err = _resolve_home(name)
    if err:
        print("helm cred: " + err, file=sys.stderr)
        return 1
    res = _cred.backup(path, apply=apply)
    if not res["ok"]:
        print("helm cred switch-guard: NOT protected — %s" % res["reason"],
              file=sys.stderr)
        return 1
    if not apply and res["action"] == "would-backup":
        print("helm cred switch-guard (dry-run): %s is NOT protected yet; add --apply"
              % res["account"])
        return 0
    protected = ("snapshot " + _display_path(res["dest"])
                 if res["action"] == "backup" else
                 "already snapshotted: " + _display_path(res["dest"]))
    print("helm cred switch-guard: %s is protected (%s)" % (res["account"], protected))
    shown = _display_path(path)
    if shown != "<redacted-path>":
        print("  now safe to run:  %s" % homes.LOGIN_CMDS["claude"](shown))
    else:
        print("  home path redacted; select it by name before running /login")
    print("  after the login: `helm cred list` shows what this home now holds; "
          "`helm cred heal` puts %s back when no session holds it." % res["account"])
    return 0


def _print_heal(args):
    quiet = "--quiet" in args
    apply = "--apply" in args
    as_json = "--json" in args
    rest = [a for a in args if a not in ("--apply", "--json", "--quiet")]
    if len(rest) > 1 or any(a.startswith("-") for a in rest):
        if not quiet:
            print("helm cred heal: invalid arguments", file=sys.stderr)
        return 2
    res = heal(rest[0] if rest else None, apply=apply, hook=quiet)
    if quiet:
        # hook mode: stdout would land in the session's context — total
        # silence either way; the exit code alone says whether every plan
        # (if any) reached restored.
        return 1 if apply and any(p["status"] != "restored"
                                  for p in res["plans"]) else 0
    if as_json:
        print(json.dumps(_public_paths(res), indent=2))
        return 0
    plans = res["plans"]
    if not plans:
        print("helm cred heal: no drifted home — every dir name matches the "
              "account it holds")
        return 0
    print("helm cred heal (%s):" % ("APPLIED" if apply else "dry-run — add --apply"))
    for p in plans:
        print("  %-30s holds %-32s %s" % (p["name"], p["holds"] or "-", p["status"]))
        print("      %s" % p["reason"])
        if p.get("pre_image"):
            print("      pre-image of the evicted occupant: %s"
                  % _display_path(p["pre_image"]))
    bad = apply and any(p["status"] != "restored" for p in plans)
    return 1 if bad else 0


def cmd_cred(args):
    """cred [list|backup [--all] [--apply]|switch-guard [--install] [--apply]|heal [--apply] [--quiet]]"""
    args = list(args)
    verb = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    try:
        if verb in ("list", "ls"):
            if any(a != "--json" for a in rest):
                print("helm cred list: invalid arguments", file=sys.stderr)
                return 2
            return _print_list(rest)
        if verb == "backup":
            return _print_backup(rest)
        if verb in ("switch-guard", "guard"):
            return _print_switch_guard(rest)
        if verb == "heal":
            return _print_heal(rest)
        print("helm cred: unknown subverb — %s" % cmd_cred.__doc__, file=sys.stderr)
        return 2
    except Exception as e:
        # Last-resort CLI boundary: exception strings can embed paths or parsed
        # input. Class-only reporting guarantees no credential/token traceback.
        print("helm cred: operation failed (%s)" % e.__class__.__name__, file=sys.stderr)
        return 1
