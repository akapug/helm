#!/usr/bin/env python3
"""helm launch — the metaharness seam (meld-launch.sh's capability, ported
onto helm's own machinery). Use it in place of a bare `claude` invocation:

    helm launch [--seat NAME] [--home HOME] [--room R] [--no-install] [--] [claude args…]

It (1) wires the hook estate into the target home (inject + the delivery
lane's deliver/join — hooks.py's installer, idempotent), (2) pre-writes the
seat's roster row so teammates can address it before the first boundary,
(3) exports one aligned chat + dregg signer identity so every surface speaks
and signs as the STABLE seat (meld's agent-join lesson: an addressable identity
must survive sessions), then (4) execs claude with the passed-through args.

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
    """The child's env: one aligned chat/signer identity, optional credential-
    home pin, and an explicit or derived project room. Pure — tested without
    an exec. The signer binary honors an explicitly configured canonical or
    legacy path (including an intentional empty kill switch), else uses helm's
    dregg default. Profiles never inherit: a child speaking as ``seat`` must
    sign as that same seat, not its launcher.

    The child-session stamp is stripped (seat.CHILD_STAMP_VARS): launched
    from inside a Claude session, an inherited CLAUDE_CODE_CHILD_SESSION/
    SID marks the child a subprocess and kills its transcript persistence."""
    from . import seat as _seat
    # THE PROXY TRIPLE NEVER RIDES INTO A NATIVE SEAT. This function is the
    # env for `os.execvpe("claude", ...)` on the path that registers itself
    # as family=claude / backend=native — so if the launching shell belongs to
    # a PROXIED seat (every non-claude family here runs behind CLIProxyAPI but
    # inside Claude Code's harness, deliberately), an inherited
    # ANTHROPIC_BASE_URL aims the new seat at a proxy fronting another vendor.
    # It would look native, be billed native, and route elsewhere, and nothing
    # downstream reports it. Same seam seat.py uses — never a second scrubber.
    env = _seat.scrub_env(base)
    for v in _seat.CHILD_STAMP_VARS:
        env.pop(v, None)
    env["HELM_CHAT_NAME"] = seat
    if "HELM_CELL_BIN" not in env:
        env["HELM_CELL_BIN"] = (env["MELD_CELL_BIN"]
                                if "MELD_CELL_BIN" in env
                                else _seat.DREGG_SIGNER_DEFAULT)
    env["HELM_CELL_PROFILE"] = seat
    env["DREGG_PROFILE"] = seat
    # GIT SPEAKS THE SAME NAME AS DREGG. The line above already gives this
    # child its own signing profile, and this function's own law is that "a
    # child speaking as `seat` must sign as that same seat, not its launcher".
    # Git was the one plane left out of that law, so every seat's commits
    # landed under a single shared identity — which is why `helm work release`
    # prints "git committer, shared across seats … not seat provenance" on
    # every triage line it emits. helm has been reporting this gap all along.
    #
    # NAME ONLY, and setdefault so an operator's explicit identity still wins
    # (the TMPDIR block below takes the same posture). The EMAIL is left alone
    # so GitHub linkage, mailmap and existing history are untouched; the name
    # carries the one fact nothing downstream can recover — WHICH SEAT.
    env.setdefault("GIT_AUTHOR_NAME", seat)
    env.setdefault("GIT_COMMITTER_NAME", seat)
    env["HELM_AGENT_HARNESS"] = "claude"
    env["HELM_MODEL_FAMILY"] = "claude"
    env["HELM_MODEL_BACKEND"] = "native"
    if "TMPDIR" not in env:
        # The mount plane's 'agents never think about it' half (scratch.py):
        # point the child's TMPDIR at a mount helm CHOSE and helm reaps, so
        # every mktemp / tempfile / git-temp inside the seat lands off a
        # capped tmpfs by default. An operator's own TMPDIR always wins;
        # HELM_SCRATCH_TMPDIR=0 opts out. Best-effort — a routing failure
        # leaves the child exactly as it was.
        try:
            from . import scratch
            tmp = scratch.launch_tmpdir()
        except Exception:
            tmp = None
        if tmp:
            env["TMPDIR"] = tmp
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


def home_note(home_path, asked):
    """The anti-drift line: `--home admin` names a DIRECTORY, and a past
    `/login` may have put a different account inside it. Print who the home
    ACTUALLY holds (cred.account_of reads the content) before exec — the
    operator asked for an account, not a path. Never blocks the launch."""
    from . import cred
    verdict, acct = cred.verdict_for(home_path)
    if verdict == "UNKNOWN":
        print("[helm launch] home %s: account unreadable (%s) — launching anyway"
              % (asked, acct["error"]), file=sys.stderr)
        return
    print("[helm launch] home %s HOLDS %s%s"
          % (asked, acct["email"],
             "  ** DRIFT: this dir's name promises another account; that account's "
             "home is %s (`helm cred list`) **" % homes.canonical_name(acct["email"])
             if verdict == "DRIFT" else ""), file=sys.stderr)


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
        home_note(home_path, opts["home"])
    # seats.resolve_homing is THE one precedence (CLI --room > env seam >
    # project derivation) — launch never re-derives its own copy. safe_cwd:
    # a deleted process cwd must not crash the launch seam (eager-getcwd
    # class), it just launches un-homed — hoisted above the seat default
    # because stable_seat() derives from cwd too.
    cwd = seats.safe_cwd()
    # home.chat_name / home.validate_seat_arg are THE two validated seams: a
    # hostile name from EITHER the --seat CLI arg or the HELM_CHAT_NAME env is
    # rejected (home.SeatNameError) here rather than exported to the child as
    # its identity or written as a roster key.
    seat = home.validate_seat_arg(opts["seat"])
    inherited = None if seat else home.chat_name()
    seat = seat or inherited or stable_seat(cwd or "here")
    if inherited:
        # SAY WHAT THE MIDDLE RUNG DID: this names the child after the
        # LAUNCHER's exported HELM_CHAT_NAME — an exported name outlives the
        # pane it named, which is the exact contagion vector of a prior
        # identity-hijack incident. Kept for the deliberate relaunch-myself
        # shape, but never again silently.
        print("[helm launch] seat %r INHERITED from this shell's "
              "HELM_CHAT_NAME — an exported name outlives the pane it named; "
              "pass --seat to name the child deliberately" % seat,
              file=sys.stderr)
    room, source = seats.resolve_homing(opts["room"], cwd)
    room_source = "derived" if source == "derived" else None
    room_explicit = source == "explicit"
    if opts["install"]:
        for name, path in ([(opts["home"], home_path)] if home_path
                           else hooks.claude_homes()):
            action, detail = hooks.install_home(path)
            if action == "fail":
                print("helm launch: hook install failed in %s: %s"
                      % (name, detail), file=sys.stderr)
    seats.join(cwd=cwd, seat=seat, room=room or "main",
               room_explicit=room_explicit, room_source=room_source,
               runtime={"agent_harness": "claude", "family": "claude",
                        "backend": "native"})
    env = build_env(os.environ, seat, home_path, room, room_source)
    print("[helm launch] seat '%s'%s — exec claude" % (
        seat, (" home " + opts["home"]) if opts["home"] else ""), file=sys.stderr)
    try:
        os.execvpe("claude", ["claude"] + claude_args, env)
    except OSError as e:
        print("helm launch: exec claude failed: %s" % e, file=sys.stderr)
        return 1
