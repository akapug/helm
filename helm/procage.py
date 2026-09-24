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
