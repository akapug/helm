#!/usr/bin/env python3
"""HOW OLD IS THIS PROCESS, IN THE KERNEL'S FRAME RATHER THAN PYTHON'S.

THE PHASE NO PYTHON CODE CAN TIME FROM INSIDE ITSELF IS THE ONE BEFORE IT.
`time.monotonic()` taken at helm's first line measures from that line, so an
interpreter that spent eight seconds starting up and importing reads as zero.
Field 22 of /proc/self/stat is the process start in clock ticks since boot and
/proc/uptime is now in the same frame, so their difference is the real origin.

ONE CONSTRUCTOR, BECAUSE FIVE HAND-ROLLED COPIES OF ONE COMPUTATION IS FIVE
PLACES TO GET THE COMM FIELD WRONG. A process named `(evil name)` breaks every
parse that splits /proc/<pid>/stat on whitespace from the left; the fields
resume after the LAST close paren, and each existing copy re-derives that.
The other readers (rearm's starttime_of, seat_health's _proc_cpu_sample,
roguescan, gatechild) are older than this module and are not converted here --
converting a caller is a change to that caller's lane, not to this one.

IT ANSWERS None RATHER THAN GUESSING. Off Linux, or with /proc unreadable,
there is no age to report and an instrument that invents a number is the thing
the modules calling this exist to replace.
"""
import os


def process_age(pid=None):
    """Seconds since this process (or `pid`) started, or None when unknown."""
    who = "self" if pid is None else str(int(pid))
    try:
        with open("/proc/%s/stat" % who, "rb") as fh:
            raw = fh.read()
        # The comm field can contain spaces AND parentheses, so the fields
        # resume after the last ')' -- never from a left-to-right split.
        rest = raw[raw.rindex(b")") + 2:].split()
        started_ticks = int(rest[19])
        hz = os.sysconf("SC_CLK_TCK") or 100
        with open("/proc/uptime", "rb") as fh:
            up = float(fh.read().split()[0])
        age = up - (started_ticks / float(hz))
    except Exception:                     # noqa: BLE001 — every failure here
        return None                       # is "cannot tell", and a caller
    # A NEGATIVE AGE IS A CLOCK WE DO NOT UNDERSTAND, not a young process:
    # report nothing rather than a number that cannot be true.
    return age if age >= 0 else None


#: The hook wrapper's start, in /proc/uptime's frame (`bin/helm-hook`).
HOOK_T0_ENV = "HELM_HOOK_T0"
#: A start further back than this is not this hook's: a stale value inherited
#: by something that is not a hook child.
HOOK_T0_MAX_S = 3600.0


def hook_elapsed(environ=None):
    """Seconds since the hook wrapper started this hook, or None.

    THE BUDGET IS THE HOOK'S, NOT THE HANDLER'S. The wrapper arms the outer
    `timeout` the moment it starts, and everything before a handler's first
    line — the interpreter, its site hooks, `import helm.cli` — runs against
    that same clock. A handler that starts its own budget from its first line
    was measured spending 2.42s of an owner's stop before its ladder began,
    which is how the outer `timeout 20` killed ladders that believed they had
    time left. `bin/helm-hook` records /proc/uptime in HELM_HOOK_T0 before it
    starts the child, with no fork; this reads the same clock now.

    None when the variable is absent, unparseable, in the future or older
    than HOOK_T0_MAX_S, or when /proc/uptime cannot be read: the caller then
    budgets from its own start, as it always did."""
    env = os.environ if environ is None else environ
    raw = env.get(HOOK_T0_ENV)
    if not raw:
        return None
    try:
        t0 = float(raw)
        with open("/proc/uptime", "rb") as fh:
            now = float(fh.read().split()[0])
    except Exception:                     # noqa: BLE001 — "cannot tell"
        return None
    elapsed = now - t0
    if elapsed < 0 or elapsed > HOOK_T0_MAX_S:
        return None
    return elapsed
