"""How many subagents a seat is holding RIGHT NOW — the DEMAND term, per seat.

THIS MODULE MEASURES NOTHING ITSELF, AND THAT IS THE DESIGN. The walk that
counts a seat's live subagent transcripts already exists, scarred by two
rounds of review against the live fleet: `proxywatch.fanout_reading` owns the
lstat-before-stat rule, the dangling-symlink refusal, the
a-directory-named-agent-x.jsonl refusal and — the property this module exists
to preserve — ZERO IS ONLY REACHABLE FROM A DIRECTORY WE PROVED WE COULD
READ. A second walk here would be a second census that inherits none of those
scars, which is the class its own docstring indicts.

WHAT THIS ADDS is the one hop the routing verb needs and the watchdog pass
did not: `fanout_reading` takes an INSTANCE DIRECTORY, and a router holds a
SEAT NAME. Resolving a seat name to its instance directory is the whole job,
plus keeping the UNKNOWN that the reading hands back from being flattened
into a number on the way out.

A SEAT WITH NO TRANSCRIPT PATH IS UNMEASURED, NEVER IDLE. Proxy-family seats
(codex, kimi, ds4pro, gemini, grok, openrouter) keep a claude-shaped instance
tree; a family that does not, or a seat that was never launched, has no
directory to count. The honest answer there is `running: None,
measured: False` with the reason attached — and a cap arithmetic that
subtracts an invented zero would report the FULL cap as free headroom for a
seat that is already saturated, which is the direction that costs money.
"""

import os


def _seat_instance_dir(seat_name, family=None, seatmod=None):
    """(instance dir, why-not) for one seat name.

    THE FACADE, NOT THE IMPLEMENTATION MODULE. `seat` re-exports both the
    family resolver and the instance-dir locator that `proxywatch` itself
    calls, so this asks the same door the producer of the reading asks and
    the two cannot disagree about where a seat lives.
    """
    from . import seat as _seat
    seatmod = seatmod or _seat
    name = str(seat_name or "").strip()
    if not name:
        return None, "no seat name was given, so there is no instance to read"
    if family is None:
        family, err = seatmod._seat_family(name)
        if err or not family:
            return None, ("this seat's family is unresolved (%s), so helm "
                          "cannot say which instance tree holds its "
                          "subagents" % (err or "no family"))
    try:
        return seatmod._instance_dir(family, name), None
    except Exception as exc:                            # noqa: BLE001
        return None, ("the instance directory would not resolve (%s: %s)"
                      % (exc.__class__.__name__, exc))


def live(seat_name, family=None, now=None, reading=None, seatmod=None):
    """{seat, family, running, measured, window_s, why} — the seat's live
    fan-out, or an honest UNKNOWN.

    `running` is None whenever anything between the seat name and the mtime
    could not be read, and `measured` is the flag a caller tests instead of
    testing the number: `if not row["measured"]` reads correctly, while
    `if not row["running"]` reads a measured zero and an unreadable tree the
    same way. The cap node consumes `measured`.

    NO SPAWN, NO NETWORK, NO WRITE. One scandir walk under a directory this
    host already owns.
    """
    from . import proxywatch
    fn = reading or proxywatch.fanout_reading
    out = {"seat": str(seat_name or ""), "family": family, "running": None,
           "measured": False, "window_s": proxywatch._FANOUT_WINDOW_S,
           "why": None}
    inst, why = _seat_instance_dir(seat_name, family=family, seatmod=seatmod)
    if why:
        out["why"] = why
        return out
    if out["family"] is None:
        from . import seat as _seat
        fam, _err = (seatmod or _seat)._seat_family(out["seat"])
        out["family"] = fam
    try:
        read = fn(inst, now=now)
    except Exception as exc:                            # noqa: BLE001
        # THE READER FAILING IS NOT THE SEAT BEING IDLE. Same law as the
        # reading it wraps, one layer out: a broken walk must never hand a
        # controller a free slot it did not measure.
        out["why"] = ("the fan-out reading failed (%s: %s)"
                      % (exc.__class__.__name__, exc))
        return out
    active = (read or {}).get("active")
    out["window_s"] = (read or {}).get("window_s", out["window_s"])
    if active is None:
        out["why"] = ("nothing under %s could be read as a subagent "
                      "transcript tree, so this seat's load is UNKNOWN and "
                      "not zero" % os.path.basename(inst.rstrip("/")))
        return out
    out["running"], out["measured"] = int(active), True
    return out
