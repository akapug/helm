#!/usr/bin/env python3
"""What a hook handler's rc 0 actually MEANT, carried from where it is known.

THE PROBLEM THIS EXISTS FOR. A Stop handler returns 0 for three different
reasons and the exit code cannot tell them apart:

  ANSWERED   the guard ran, looked, and found nothing to block on.
  UNCHECKED  the guard was asked and did not answer — it crashed, or its
             coverage budget expired mid-ladder. It ALLOWS, loudly, because a
             guard that can wedge the fleet is worse than one that misses a
             row; but nothing was examined.
  SKIPPED    the handler never ran at all — a fleet-scoped hook exited at the
             project-scope door before reaching the verb.

ALL THREE RETURN 0, and the harness must keep seeing 0: fail-open is the law
and nothing here converts an advisory failure into a veto. What changes is that
the DISTINCTION survives to the aggregate, so a corroborating record is not
written for a dispatch in which nothing was actually checked.

WHY A CHANNEL RATHER THAN A RETURN VALUE. The places that KNOW are deep inside
the handler — `seats_stop_response.publish_failure` knows it is publishing a
crash, `cli.main` knows it is skipping a foreign-project hook — and every one
of them returns an int through call sites that have no idea. Threading a second
value out would mean changing every intermediate signature, and each one is a
chance to drop it silently. The handler runs IN-PROCESS under
`hookrun.run_one`, so a process-local slot reaches the one reader that needs
it.

THE SLOT IS STRICTEST-WINS AND SINGLE-USE. Two declarations in one dispatch
keep the worse one, because a handler that skipped part of its work and
crashed in another part is UNCHECKED and not SKIPPED. `taken()` clears, so a
stale declaration can never be read as the next handler's outcome — a leak
there would be the same failure-that-looks-like-success this module prevents.
"""

ANSWERED = "answered"
UNCHECKED = "unchecked"
SKIPPED = "skipped"

#: Worse-first. `taken()` returns the worst declaration made in one dispatch.
_RANK = {ANSWERED: 0, SKIPPED: 1, UNCHECKED: 2}

_declared = []


def begin():
    """Clear the slot before a handler runs. Idempotent, never raises."""
    del _declared[:]


def declare(status, why=None):
    """Record what this handler's rc actually means. NEVER raises.

    Callers are guards on a fail-open path: a bookkeeping write here must not
    turn an allowed stop into a crashed hook.
    """
    try:
        if status not in _RANK:
            return
        _declared.append((status, str(why or "")))
    except Exception:                             # noqa: BLE001
        return


def taken():
    """-> (status, why) or (None, None), and CLEARS the slot.

    None means no handler declared anything, which is the ordinary case: a
    verb that returns normally has answered.
    """
    try:
        if not _declared:
            return None, None
        worst = max(_declared, key=lambda d: _RANK.get(d[0], 0))
        del _declared[:]
        return worst
    except Exception:                             # noqa: BLE001
        del _declared[:]
        return None, None
