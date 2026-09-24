"""Explicit worker/lead launch posture for Helm-managed seats."""
import os
import shlex

SEAT_ROLES = ("worker", "lead")
_LEAD_SETTINGS = '{"ultracode":true}'
_ROLE_ENV = "HELM_SEAT_ROLE"
# The env name a spawn ATTEMPT TOKEN rides under, from the launch line a spawn
# hands its adapter (or the env it hands Popen) into the child and every hook
# the child runs. The token's LIFE — who mints it, what it means at each stage
# — is `seat_lifecycle_runtime._publish_spawn_attempt`'s; this module owns only
# the grammar of the command that carries it, beside the role marker.
SPAWN_ATTEMPT_ENV = "HELM_SPAWN_ATTEMPT"


def _seat_role(value):
    role = "worker" if value is None else str(value)
    return role if role in SEAT_ROLES else None


def _launch_argv(launch_sh, role, tail=()):
    """Structured Claude argv; only an explicit lead gains ultracode."""
    if role not in SEAT_ROLES:
        raise ValueError("unknown seat role %r" % role)
    out = [launch_sh]
    if role == "lead":
        out += ["--settings", _LEAD_SETTINGS]
    return out + list(tail)


def _launch_env(role, env=None):
    """Process env for one launch, stripping an inherited lead from workers."""
    if role not in SEAT_ROLES:
        raise ValueError("unknown seat role %r" % role)
    out = dict(os.environ if env is None else env)
    if role == "lead":
        out[_ROLE_ENV] = "lead"
    else:
        out.pop(_ROLE_ENV, None)
    return out


def _launch_command(launch_sh, role, tail=(), token=None):
    """Shell command for pane adapters; a worker with no attempt token keeps the
    historical bytes (a resume relaunch mints no attempt, so its command is
    exactly what it always was).

    `token` is a spawn ATTEMPT TOKEN and rides as `SPAWN_ATTEMPT_ENV` in the same
    `env` prefix as the lead marker, for the same reason the marker does: a pane
    adapter is handed a shell command and no environment, so the command is the
    only channel through which the child can learn WHICH spawn attempt it is
    the child of. `launch.sh` execs claude through `env`, which preserves it, so
    the child and every hook it runs carry the exact value the spawn published.
    """
    command = " ".join(shlex.quote(a) for a in _launch_argv(
        launch_sh, role, tail=tail))
    assigns = []
    if role == "lead":
        assigns.append("%s=lead" % _ROLE_ENV)
    if token:
        assigns.append("%s=%s" % (SPAWN_ATTEMPT_ENV, shlex.quote(str(token))))
    return "env %s %s" % (" ".join(assigns), command) if assigns else command


def _native_launch_command(argv, role, config_home=None, token=None):
    """Shell command for a NATIVE seat's pane: `helm launch --seat …` carrying
    THE SAME lead posture every other seat gets, from this module's own two
    levers rather than a second opinion about what a lead is.

    `config_home` PINS THE CLAUDE STORAGE INTO THE COMMAND, and omitting it is
    the defect it exists to close. A pane adapter is handed a SHELL COMMAND and
    no environment, so the child's CLAUDE_CONFIG_DIR was whatever the TERMINAL or
    DAEMON running that command happened to export — while the spawn had already
    recorded the home IT selected, and every later exact-session proof read that
    recorded path with full confidence. Two different homes, one of them written
    down as fact: the census listed a directory the seat never wrote into and read
    zero sessions as proof the seat was dead. Computing an env in the parent
    transmits nothing; this assignment does, so the recorded home and the actual
    child home are the same value by construction.

    It rides in the SAME `env` invocation as the role, because that is already
    the one boundary where this module decides what the child's environment is.

    A native seat has no launch.sh to put the settings flag beside, so the flag
    rides after `--`, which is exactly where `helm launch` passes arguments
    through to claude; `HELM_SEAT_ROLE` goes in the env, where `build_env` keeps
    it and `seat boot-brief` reads it.

    OMITTING THE MARKER IS NOT CLEARING IT, and that is the whole of the worker
    branch. A pane adapter is handed a SHELL COMMAND, not an environment, so the
    command runs in a shell that inherits whatever the launcher exported: a
    worker spawned from a lead's pane inherited `HELM_SEAT_ROLE=lead`, and
    `seat boot-brief` then read lead while the seat's own register said worker.
    The structured launcher has always known the asymmetry (`_launch_env` POPS
    the marker for a worker rather than leaving it), so this command is built
    FROM that function's answer — `env -u` for the removal, `env NAME=` for the
    set — instead of from a second opinion about what a lead is. A future change
    to the role policy's env therefore reaches the pane boundary by itself.

    `token` is the spawn ATTEMPT TOKEN, carried exactly as `_launch_command`
    carries it and for the same "a command is the only env channel" reason:
    `helm launch` reads it back through the one reader
    (`spawn_attempt_token`) and pins it into claude's env, so the pane's first
    SessionStart arrives carrying the attempt it is the child of.
    """
    if role not in SEAT_ROLES:
        raise ValueError("unknown seat role %r" % role)
    out = list(argv) + (["--", "--settings", _LEAD_SETTINGS]
                        if role == "lead" else [])
    line = " ".join(shlex.quote(a) for a in out)
    # Asked of `_launch_env` against an environment that HOLDS the inherited
    # value, so the answer distinguishes "set it" from "take it away".
    decided = _launch_env(role, {_ROLE_ENV: "lead"})
    # `env` takes its OPTIONS before its assignments (POSIX), so `-u` cannot be
    # appended after one: `env FOO=1 -u BAR cmd` runs a utility named `-u`.
    opts, assigns = [], []
    if _ROLE_ENV in decided:
        assigns.append("%s=%s" % (_ROLE_ENV, shlex.quote(decided[_ROLE_ENV])))
    else:
        opts += ["-u", _ROLE_ENV]
    if config_home:
        from . import homes
        assigns.append("%s=%s" % (homes.ENV_VAR["claude"],
                                  shlex.quote(config_home)))
    if token:
        assigns.append("%s=%s" % (SPAWN_ATTEMPT_ENV, shlex.quote(str(token))))
    return " ".join(["env"] + opts + assigns + [line])


def _resume_role(rest, prior=None):
    """Explicit override, then recorded role; legacy records are workers."""
    if "--role" in rest:
        i = rest.index("--role")
        if i + 1 >= len(rest) or str(rest[i + 1]).startswith("--"):
            return None, "--role wants a value"
        value = rest[i + 1]
    else:
        value = (prior or {}).get("role")
    role = _seat_role(value)
    return (role, None) if role else (
        None, "--role wants one of: %s" % ", ".join(SEAT_ROLES))
