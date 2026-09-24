"""helm.cred doctor + CLI cluster (see __init__)."""
import json
import os
import shlex
import sys

from .. import cred as _cred
from .. import homes
from ._common import (ACCOUNT_JSON, AUTH_JSON, RETIRED_GUARD_SPECS,
                      _display_path, backup_root)
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
            "source_home", "name", "aliases", "wants_account_folded",
            "orca_path"}:
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
    for r in rs:
        # AGREE says the identity is right, never that the token is alive: a
        # home copied from Orca weeks ago agrees and cannot log in. FRESHNESS
        # is the second question, asked of Orca's copy of the same account.
        r["orca"] = _orca_column(r)
    if as_json:
        print(json.dumps(_public_paths(rs), indent=2))
        return 0
    if not rs:
        print("helm cred: no claude credential homes found")
        return 0
    print("helm cred — identity read from CONTENT (%s oauthAccount), never from "
          "the dir name; FRESHNESS is the token against Orca's copy of that account:"
          % ACCOUNT_JSON)
    print("  %-30s %-32s %-8s %-14s %-8s %s"
          % ("DIR NAME", "ACTUAL ACCOUNT", "VERDICT", "FRESHNESS", "BACKUPS", "NOTE"))
    for r in sorted(rs, key=lambda r: (r["default"], r["name"])):
        note = []
        orca = r["orca"]
        if (orca["verdict"] in (_cred.STALE, _cred.OWN_CHAIN, _cred.UNPROVEN,
                                _cred.UNKNOWN, _cred.DISAGREE)
                and orca["reason"] and r["verdict"] != "UNKNOWN"):
            note.append(orca["reason"])
        if orca["verdict"] == _cred.STALE and r["verdict"] == "AGREE":
            # the note promises a sync only where the sync would write: its own
            # dry run answers (a DRIFT home keeps the heal note below)
            plan = _cred.sync(r["real"])
            # AND ONLY A FREE HOME IS PROMISED ONE: the dry run stops before the
            # holder check both applying doors make, so a held or unprovable
            # home is told it will stay unsynced, with the pids that hold it.
            held = (_cred._claude_holders(r["real"])
                    if plan["action"] == "would-sync" else [])
            if plan["action"] != "would-sync":
                note.append("NOT syncable: %s" % (plan.get("blocked") or plan["reason"]))
            elif held is None or held:
                note.append("NOT synced while held: %s — `helm cred sync-orca --home "
                            "%s --apply` syncs it once no claude runs on it"
                            % ("a live claude process could not be ruled out"
                               if held is None else "live claude pid%s %s"
                               % ("s"[:len(held) != 1],
                                  ",".join(str(p) for p, _ in held)),
                               _display_path(r["name"])))
            else:
                note.append("`helm cred sync-orca --home %s --apply` or the next "
                            "`helm launch --home` syncs it" % _display_path(r["name"]))
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
        print("  %-30s %-32s %-8s %-14s %-8s %s"
              % (_display_path(r["name"])[:30], (r["account"] or "-")[:32], r["verdict"],
                 orca["verdict"], r["backups"], "; ".join(note)))
    drift = [r for r in rs if r["verdict"] == "DRIFT"]
    stale = [r for r in rs if r["orca"]["verdict"] == _cred.STALE]
    print("helm cred: %d home%s, %d drift%s, %d stale against Orca%s"
          % (len(rs), "s"[:len(rs) != 1], len(drift), "" if len(drift) == 1 else "s",
             len(stale),
             " — `helm cred heal` (dry-run) shows the repair" if drift else ""))
    return 0


def _orca_column(row):
    """The FRESHNESS cell for one estate row. The default home is Orca's own
    to rewrite and is never synced, so it reads N/A rather than a verdict a
    sync would act on. A failed measurement is UNKNOWN with its class, never
    FRESH."""
    if row["default"] or not _cred.is_credhome(row["real"]):
        return {"verdict": "N/A", "reason": None}
    try:
        return _cred.freshness(row["real"])
    except Exception as e:           # class only: text may carry a path/token
        return {"verdict": _cred.UNKNOWN,
                "reason": "freshness probe failed (%s)" % e.__class__.__name__}


def _print_sync_orca(args):
    args = list(args)
    apply = "--apply" in args
    replace = "--replace-own-chain" in args
    args = [a for a in args if a not in ("--apply", "--replace-own-chain")]
    name = _take_flag(args, "--home")
    if args or not name:
        print("helm cred sync-orca: usage: helm cred sync-orca --home H [--apply] "
              "[--replace-own-chain]", file=sys.stderr)
        return 2
    path, err = _resolve_home(name)
    if err:
        print("helm cred: " + err, file=sys.stderr)
        return 1
    if not _cred.is_credhome(path):
        print("helm cred sync-orca: %s is not a named credhome under %s — only "
              "those are synced from Orca" % (_display_path(name),
                                              _display_path(homes.ROOTS["claude"])),
              file=sys.stderr)
        return 1
    res = _cred.sync(path, apply=apply, replace_own_chain=replace)
    shown = _display_path(name)
    print("helm cred sync-orca (%s): home %s holds %s — %s"
          % ("APPLIED" if apply else "dry-run — add --apply", shown,
             res["account"] or "-", res["verdict"]))
    if res["reason"]:
        print("  " + res["reason"])
    if res["action"] == "would-sync":
        print("  WOULD SYNC %s from Orca's copy (pre-image first; Orca untouched)"
              % AUTH_JSON)
    elif res["action"] == "synced":
        print("  synced; pre-image %s" % (_display_path(res["pre_image"])
                                         if res["pre_image"] else "none (no prior file)"))
    elif res["action"] in ("skip", "refused"):
        print("  NOT synced (%s)" % res["action"],
              file=sys.stderr)
    return 1 if res["action"] in ("skip", "refused") else 0


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


def _retired_guard_command(command, spec):
    """Exact, whole-command ownership, never a marker somewhere in shell text.

    Accept the current rendered wrapper at an old helm path and the original
    timeout/true wrapper, including backup's pre-explicit-apply spelling.
    Extra shell work, unknown wrappers and prose remain foreign even when they
    mention (or execute) the retired command.
    """
    from .. import hooks
    executable, _ = hooks._executed(command)
    if (os.path.basename(executable) != "helm"
            or not (executable == "helm" or os.path.isabs(executable))):
        return False
    if (spec["name"] in ("cred-guard", "cred-guard-turn")
            and os.path.isabs(executable)
            and command == "timeout 5 %s cred backup --quiet || true" % shlex.quote(executable)):
        return True
    direct = "%s %s" % (shlex.quote(executable), spec["args"])
    # TODAY'S RENDERING IS NOT THE ONLY ONE ON DISK. `hooks.HISTORICAL_COMMANDS`
    # carries every ladder helm has written into a settings file; without it,
    # ownership of an installed wrapper silently expires the next time the
    # template changes, and retirement then reads helm's own writing as foreign
    # and leaves it in place. Accepting a rendering helm demonstrably produced
    # is exact ownership, not a loosened match.
    known = [direct, "timeout %d %s || true" % (spec["timeout"], direct),
             hooks.spec_command(spec, executable=executable)]
    known += [c for c in (r(spec, executable) for r in hooks.HISTORICAL_COMMANDS)
              if c is not None]
    return command in known


def _retire_guard_settings(settings):
    """Copy-minus-owned leaves; retain mixed groups and every unrelated key.

    Ambiguity is a refusal count, not deletion authority. Even exact commands
    with unknown hook keys are preserved: those keys may belong to a foreign
    integration. Empty groups disappear only when they have no foreign keys.
    """
    out = json.loads(json.dumps(settings))
    events = out.get("hooks", {})
    if not isinstance(events, dict):
        raise ValueError("hooks is not an object; retirement refused")
    removed, refused = 0, 0
    for event in dict.fromkeys(s["event"] for s in RETIRED_GUARD_SPECS):
        if event not in events:
            continue
        groups = events[event]
        if not isinstance(groups, list):
            raise ValueError("hook event is not a list; retirement refused")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                refused += 1
                kept.append(group)
                continue
            leaves = []
            for hook in group["hooks"]:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    refused += 1
                    leaves.append(hook)
                    continue
                owned = (hook.get("type") == "command"
                         and not set(hook) - {"type", "command", "timeout"}
                         and any(_retired_guard_command(command, s)
                                 for s in RETIRED_GUARD_SPECS if s["event"] == event))
                if owned:
                    removed += 1
                    continue
                # Substrings can justify a visible refusal, NEVER a removal.
                if any(m in command for s in RETIRED_GUARD_SPECS for m in s["own"]):
                    refused += 1
                leaves.append(hook)
            if leaves == group["hooks"]:
                kept.append(group)
            elif leaves or set(group) - {"hooks", "matcher"}:
                group["hooks"] = leaves
                kept.append(group)
        events[event] = kept
    return out, {"removed": removed, "refused": refused}


def retire_guard_home(path, dry=True):
    """Retire one inventoried home's entries through the config CAS owner."""
    from .. import configs, hooks

    def verify(candidate, before, _metadata):
        expected, _ = _retire_guard_settings(before)
        return candidate == expected

    res = configs.transform_json_file(os.path.join(path, "settings.json"),
                                     _retire_guard_settings, verify=verify, dry_run=dry)
    if not res.get("ok"):
        # Config errors can contain arbitrary file content. Never echo it.
        return hooks.InstallResult("fail", "settings retirement refused (%s)"
                                   % res.get("code", "config"))
    refused = max((res.get(k) or {}).get("refused", 0)
                  for k in ("initial_metadata", "metadata"))
    action = {"ok": "ok", "dry": "dry-update", "updated": "update"}[res["action"]]
    detail = {"ok": "no owned retired entries changed",
              "dry-update": "owned retired entries would be removed",
              "update": "owned retired entries removed"}[action]
    if refused:
        return hooks.InstallResult("fail", detail + "; refused %d ambiguous hook "
                                   "entries; preserved for manual inspection" % refused)
    return hooks.InstallResult(action, detail)


def retire_guards(dry=True):
    """[(name, action, detail)] over the credential + minted-seat census only.

    No current-session environment is needed (cron has none). Arbitrary
    outside config inventories are not this door's population. An unread
    census refuses the entire pass BEFORE the first settings transform.
    """
    from .. import hooks
    try:
        # claude_homes uses glob/isdir, which hide permission failures. Check
        # its bounded input population explicitly before trusting that census.
        try:
            names = os.listdir(homes.ROOTS["claude"])
        except OSError as e:
            if e.errno not in hooks._CENSUS_ABSENT:
                raise
            names = []
        for path in [os.path.join(homes.ROOTS["claude"], n) for n in names] + [
                homes.DEFAULTS["claude"]]:
            try:
                os.stat(path)
            except OSError as e:
                if e.errno not in hooks._CENSUS_ABSENT:
                    raise
        targets = hooks.claude_homes()
        seats, unread = hooks.seat_homes()
        if unread:
            return [("(census)", "fail", "seat census unread; no settings changed")]
    except Exception as e:
        return [("(census)", "fail", "home census unavailable (%s); no settings changed"
                 % type(e).__name__)]
    results, seen = [], set()
    for name, path in targets + seats:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        action, detail = _cred.retire_guard_home(real, dry=dry)
        results.append((name, action, detail))
    return results


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
        results = _cred.retire_guards(dry=not apply)
        for hname, action, detail in results:
            print("  %-30s %s" % (_display_path(hname), action))
            print("    " + detail, file=sys.stderr if action == "fail" else sys.stdout)
        failed = sum(action == "fail" for _, action, _ in results)
        print("helm cred switch-guard (%s): retired SessionStart + Stop backup/heal "
              "cleanup; %d failed; no per-turn credential hooks installed. "
              "Scope: discovered credential and minted seat homes only. "
              "Credential upkeep: helm doctor --ensure (no schedule installed)."
              % ("APPLIED" if apply else "dry-run — add --apply", failed))
        return 1 if failed else 0
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
    """cred [list|ls [--json]|backup [--all] [--apply]|switch-guard|guard [--install] [--apply]|heal [--apply] [--quiet]|sync-orca --home H [--apply] [--replace-own-chain]]  (ls = list; guard = switch-guard)"""
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
        if verb == "sync-orca":
            return _print_sync_orca(rest)
        print("helm cred: unknown subverb — %s" % cmd_cred.__doc__, file=sys.stderr)
        return 2
    except Exception as e:
        # Last-resort CLI boundary: exception strings can embed paths or parsed
        # input. Class-only reporting guarantees no credential/token traceback.
        print("helm cred: operation failed (%s)" % e.__class__.__name__, file=sys.stderr)
        return 1
