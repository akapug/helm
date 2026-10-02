"""Explicit worker/lead launch posture for Helm-managed seats."""
import os
import shlex

SEAT_ROLES = ("worker", "lead")
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
    """Structured Claude argv. The role rides the env (`_launch_env`), never
    a settings flag: every agent starts at its home's effort (high), and
    ultracode is not special to a lead (owner ruling)."""
    if role not in SEAT_ROLES:
        raise ValueError("unknown seat role %r" % role)
    return [launch_sh] + list(tail)


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

    The role is the env alone: `HELM_SEAT_ROLE` goes in the env, where
    `build_env` keeps it and `seat boot-brief` reads it. No role adds a claude
    flag after `--` (every agent starts at its home's high effort; ultracode
    is not special to a lead).

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
    line = " ".join(shlex.quote(a) for a in argv)
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


#: A NATIVE seat's role declaration: the file beside where its spawn register
#: would sit, written by `helm launch --seat S --role R` and by `helm seat
#: resume S --role R` on an orca-adopted seat. It is read ONLY when no spawn
#: register exists for the seat, so a seat helm spawned keeps the register as
#: its one authority.
DECLARATION = "role.json"


def _declaration_dir(seat_name):
    from . import seat
    return seat._instance_dir(seat.NATIVE_FAMILY, seat_name)


def _unresolved_register(seat_name):
    """The spawn.json a project seat tree holds for `seat_name`, or None.
    Asked only once no register RESOLVED the name, so any file found here is
    a broken register, never an absent one."""
    from . import seat
    for family in seat.project_families():
        path = seat._spawn_path(seat._instance_dir(family, seat_name))
        if os.path.lexists(path):
            return path
    return None


def declare_role(seat_name, role):
    """None once `role` stands for native seat `seat_name`, else why not.

    A SPAWNED SEAT IS NOT REDECLARED. Its spawn register already records a
    role, and that register is the authority: a declaration that agrees with
    it is a no-op, and one that disagrees is refused naming the spawn door
    that changes it. Only a seat no register resolves gets a declaration.

    WHY NOT A SPAWN REGISTER. A spawn.json in the native seat tree makes the
    name resolve as a REGISTERED native seat, which moves an orca-adopted
    lead off the adoption path in resume, compaction recovery and the reboot
    sweep, onto a register that holds no pane handle and no session. The
    declaration is a separate file that only `recorded_role` reads, so it
    changes the role and nothing else."""
    if not seat_name or role not in SEAT_ROLES:
        return "--role wants one of: %s" % ", ".join(SEAT_ROLES)
    from . import pk, seat
    family, err = seat._seat_family(seat_name)
    if not err:
        recorded = recorded_role(seat_name)
        if recorded == role:
            return None
        return ("%s is a %s seat whose spawn register is the authority for "
                "its role (it reads %s); `helm seat spawn %s --replace --role "
                "%s` changes it" % (seat_name, family, recorded, seat_name, role))
    if seat_name == seat.NATIVE_FAMILY:
        return ("native claude has no bare seat — name the seat (%s)"
                % seat_name)
    broken = _unresolved_register(seat_name)
    if broken:
        return ("%s has a spawn register that does not resolve (%s) — repair "
                "the register; a declaration does not stand in for it"
                % (seat_name, broken))
    linked = seat._seat_surface_error(seat.NATIVE_FAMILY, seat_name)
    if linked:
        return linked
    d = _declaration_dir(seat_name)
    with seat._seat_lifecycle_lock(d):
        pk.write_json(os.path.join(d, DECLARATION),
                      {"v": 1, "seat": seat_name, "role": role,
                       "ts": pk.now_ts()})
    return None


def _declared_role(seat_name):
    """The role a native seat's DECLARATION names, for a seat no spawn
    register resolves. A spawn.json that exists and did not resolve is a
    broken register, never "no register", so it reads worker; so do an
    absent, unreadable or mismatched declaration."""
    from . import pk, seat
    if not seat_name or seat_name == seat.NATIVE_FAMILY:
        return "worker"
    if _unresolved_register(seat_name):
        return "worker"
    rec = pk.read_json(os.path.join(_declaration_dir(seat_name), DECLARATION),
                       None)
    if not isinstance(rec, dict) or rec.get("seat") != seat_name:
        return "worker"
    return _seat_role(rec.get("role")) or "worker"


def recorded_role(seat_name):
    """'lead' or 'worker' for a SPAWNED seat, off the register the spawn
    produced; for a native seat helm never spawned, off its declaration.

    THE REGISTER IS THE AUTHORITY, NEVER THE NAME. Helm has no roster of which
    seats are leads, and a name-shaped guess would be wrong the first time a
    seat was renamed. The spawn writes `role` into its record (seat.py) and
    the worker branch deliberately DROPS an inherited marker, so a seat with
    an exported HELM_SEAT_ROLE from some other pane is not a lead by
    contagion. An unreadable register and a seat whose record predates the
    field are ordinary workers.

    A NATIVE SEAT HELM NEVER SPAWNED (a lead started by hand or adopted from
    an orca pane) has no register, so its role is its DECLARATION
    (`declare_role`), and a seat with neither is a worker.

    Read-only and cheap: one file, and only a launch or a gauge reading pays
    it."""
    try:
        from . import seat
        family, err = seat._seat_family(seat_name)
        if err:
            return _declared_role(seat_name)
        rec = seat._spawn_record(seat._instance_dir(family, seat_name)) or {}
        if rec.get("seat") != seat_name:
            return "worker"
        return _seat_role(rec.get("role")) or "worker"
    except Exception:                          # noqa: BLE001 — fail open
        return "worker"
