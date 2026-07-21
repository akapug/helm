#!/usr/bin/env python3
"""helm launch — the metaharness seam (meld-launch.sh's capability, ported
onto helm's own machinery). Use it in place of a bare `claude` invocation:

    helm launch [--seat NAME] [--home HOME] [--room R] [--no-install] [--] [claude args…]

It (1) wires the hook estate into the target home (inject + the delivery
lane's deliver/join — hooks.py's installer, idempotent), (2) pre-writes the
seat's roster row so teammates can address it before the first boundary,
(3) exports HELM_CHAT_NAME so every chat surface in the child speaks as the
STABLE seat (meld's agent-join lesson: an addressable identity must survive
sessions), then (4) execs claude with the passed-through args.

The fleet default needs no wrapper — `helm hooks install` covers every home
and the SessionStart hook joins each session under a derived name. launch
adds the stable NAME and the per-home pin (CLAUDE_CONFIG_DIR)."""
import os
import re
import socket
import sys

from . import homes, hooks, seats

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def stable_seat(cwd=None):
    """meld-launch's derivation: <host>-<cwd-basename>, sanitized, ≤64."""
    host = socket.gethostname().split(".")[0] or "host"
    base = os.path.basename((cwd or os.getcwd()).rstrip(os.sep)) or "here"
    return _SAFE.sub("-", "%s-%s" % (host, base))[:64].strip("-") or "seat"


def parse_args(args):
    """-> (opts dict, claude_args). Everything after `--` (or the first
    unknown token) passes through verbatim."""
    opts = {"seat": None, "home": None, "room": "main", "install": True}
    rest, i = [], 0
    args = list(args or [])
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i + 1:])
            break
        if a in ("--seat", "--home", "--room") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            i += 2
            continue
        if a == "--no-install":
            opts["install"] = False
            i += 1
            continue
        rest.extend(args[i:])
        break
    return opts, rest


def build_env(base, seat, home_path=None, room=None):
    """The child's env: stable chat identity, optional credential-home pin,
    and an explicit non-main team room. Pure — tested without an exec."""
    env = dict(base)
    env["HELM_CHAT_NAME"] = seat
    if home_path:
        env[homes.ENV_VAR["claude"]] = home_path
    if room and room != "main":
        env["HELM_CHAT_ROOM"] = room
    return env


def cmd_launch(args):
    """launch [--seat S] [--home H] [--room R] [--no-install] [--] [args…]"""
    opts, claude_args = parse_args(args)
    home_path = None
    if opts["home"]:
        targets, err = hooks._select_homes(opts["home"])
        if err:
            print("helm launch: " + err, file=sys.stderr)
            return 1
        home_path = targets[0][1]
    seat = opts["seat"] or os.environ.get("HELM_CHAT_NAME") or stable_seat()
    if opts["install"]:
        for name, path in ([(opts["home"], home_path)] if home_path
                           else hooks.claude_homes()):
            action, detail = hooks.install_home(path)
            if action == "fail":
                print("helm launch: hook install failed in %s: %s"
                      % (name, detail), file=sys.stderr)
    seats.join(cwd=os.getcwd(), seat=seat, room=opts["room"])
    env = build_env(os.environ, seat, home_path, opts["room"])
    print("[helm launch] seat '%s'%s — exec claude" % (
        seat, (" home " + opts["home"]) if opts["home"] else ""), file=sys.stderr)
    try:
        os.execvpe("claude", ["claude"] + claude_args, env)
    except OSError as e:
        print("helm launch: exec claude failed: %s" % e, file=sys.stderr)
        return 1
