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

from . import home, homes, hooks, seats

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def stable_seat(cwd=None):
    """meld-launch's derivation: <host>-<cwd-basename>, sanitized, ≤64."""
    host = socket.gethostname().split(".")[0] or "host"
    base = os.path.basename((cwd or os.getcwd()).rstrip(os.sep)) or "here"
    return _SAFE.sub("-", "%s-%s" % (host, base))[:64].strip("-") or "seat"


def parse_args(args):
    """-> (opts dict, claude_args). Everything after `--` (or the first
    unknown token) passes through verbatim."""
    opts = {"seat": None, "home": None, "room": None, "install": True}
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


def build_env(base, seat, home_path=None, room=None, room_source=None):
    """The child's env: stable chat identity, optional credential-home pin,
    and an explicit or derived project room. Pure — tested without an exec.
    The child-session stamp is stripped (seat.CHILD_STAMP_VARS): launched
    from inside a Claude session, an inherited CLAUDE_CODE_CHILD_SESSION/
    SID marks the child a subprocess and kills its transcript persistence."""
    from . import seat as _seat
    env = dict(base)
    for v in _seat.CHILD_STAMP_VARS:
        env.pop(v, None)
    env["HELM_CHAT_NAME"] = seat
    if home_path:
        env[homes.ENV_VAR["claude"]] = home_path
    for name in ("HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
                 "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM_SOURCE"):
        env.pop(name, None)
    if room:
        env["HELM_CHAT_ROOM"] = room
        if room_source:
            env["HELM_CHAT_ROOM_SOURCE"] = room_source
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
    env_room, env_source = home.env_pair("CHAT_ROOM", "CHAT_ROOM_SOURCE")
    room = opts["room"] or env_room or seats.derive_home_room(os.getcwd())
    room_source = ("derived" if room and opts["room"] is None
                   and (not env_room or env_source == "derived") else None)
    room_explicit = opts["room"] is not None or bool(env_room and not room_source)
    if opts["install"]:
        for name, path in ([(opts["home"], home_path)] if home_path
                           else hooks.claude_homes()):
            action, detail = hooks.install_home(path)
            if action == "fail":
                print("helm launch: hook install failed in %s: %s"
                      % (name, detail), file=sys.stderr)
    seats.join(cwd=os.getcwd(), seat=seat, room=room or "main",
               room_explicit=room_explicit, room_source=room_source)
    env = build_env(os.environ, seat, home_path, room, room_source)
    print("[helm launch] seat '%s'%s — exec claude" % (
        seat, (" home " + opts["home"]) if opts["home"] else ""), file=sys.stderr)
    try:
        os.execvpe("claude", ["claude"] + claude_args, env)
    except OSError as e:
        print("helm launch: exec claude failed: %s" % e, file=sys.stderr)
        return 1
