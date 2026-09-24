"""The sentences printed beside a NON-WAKING inbox beacon.

EXTRACTED FROM THE seats_cli FACADE, which the split budget drains rather
than grows. What lives here is a RENDERING with a hard rule: every clause it
prints about another process is RE-READ at render time, never carried over
from the record that occasioned the message.

The record is a PAST observation. Between the pass that wrote it and the
message that explains it, a waiter can dup2 a live pipe over its fd 1 or exit
outright -- and the stop pass already withholds on exactly the first of those.
So a message repeating the old reading is MORE CONFIDENT THAN THE DECISION IT
EXPLAINS, and a seat acting on it retires the one process that is waking it.

Returning the string rather than printing it is what makes the property
testable directly: the sentences are the unit under test, and a test that has
to scrape stderr cannot say which clause it caught.
"""


#: THE ONE ARMING INSTRUCTION. Every place helm tells a seat how to arm its
#: inbox beacon renders THIS call, so the wording has one home and a harness
#: change is one edit. The harness kills every Monitor at its deadline and caps
#: that deadline here; a re-arm mints a new task id. So the instruction carries
#: the deadline, and the sentences beside it say what the expiry IS: the seat's
#: check-in, re-armed in the same turn, and named by role because the id a seat
#: wrote down is dead half an hour later.
BEACON_TIMEOUT_MS = 1800000


def beacon_monitor(seat, replace=False):
    """The exact, copy-pasteable Monitor call that arms `seat`'s beacon.

    `seat` may be a format placeholder: the result carries no other percent
    sign, so a caller's template can embed it and fill the seat later."""
    return ('Monitor(command: "helm chat wait --seat %s --follow%s", '
            'timeout_ms: %d)' % (seat, " --replace" if replace else "",
                                 BEACON_TIMEOUT_MS))


BEACON_EXPIRY = (
    "Your inbox beacon expires every 30 minutes; that is your check-in. "
    "Re-arm it in the same turn, and never write its ID anywhere. It is "
    "'the beacon on `helm chat wait --seat <you> --follow`'.")
#: The same rule where a line has no room for three sentences.
BEACON_EXPIRY_TERSE = ("expires every 30 minutes: re-arm it in the same turn, "
                       "never record its ID")


#: WHAT A FRESH READING CAN SAY about the waiter this message is about. Every
#: sentence in this module is selected by one of these, so a clause cannot be
#: added without deciding what it says in all five worlds -- which is exactly
#: the discipline that was missing when the sink clause was re-read and the
#: liveness clauses beside it went on speaking from the record.
GONE = "gone"                       # proven exited, or the pid is reused
UNREADABLE = "unreadable"           # the process cannot be read at all
REFUTING = "refuting"               # still running, still reaching nobody
#: NOT "it reaches a reader". ADMISSIBLE is the absence of a refutation: the
#: fd is a pipe or a socket, which CAN carry a wake, and a pipe whose read
#: ends are all closed answers ADMISSIBLE while every write to it fails with
#: EPIPE. So the strongest true sentence is that the reason to retire it has
#: expired, never that its output arrives anywhere. The rung refuses only what
#: it can PROVE reaches nobody, and the same discipline binds its prose.
EXPIRED = "expired"                 # running; no longer provably non-waking
SINK_UNREADABLE = "sink-unreadable"  # running, but its sink cannot be read

_SINK_SAYS = {
    GONE: ("the process this pass classified is GONE — it has exited, or "
           "that pid is now worn by a different incarnation"),
    UNREADABLE: ("its stdout was a FILE or /dev/null when this pass looked, "
                 "and the process cannot be read at all now, so whether it "
                 "still wakes nobody is UNKNOWN rather than settled"),
    REFUTING: ("its stdout is a FILE or /dev/null, so it consumes addressed "
               "rows and wakes nobody"),
    EXPIRED: ("its stdout was a FILE or /dev/null WHEN THIS PASS LOOKED and "
              "is NOT any more — re-read just now it is a pipe or socket, "
              "which this pass cannot refute, so the reason this line exists "
              "has EXPIRED (that is not a claim that anything is READING it: "
              "a pipe with no reader left answers the same way)"),
    SINK_UNREADABLE: ("it is still running, but its stdout CANNOT BE RE-READ "
                      "now, so whether it still wakes nobody is UNKNOWN "
                      "rather than settled"),
}

#: WHETHER THIS SEAT IS REACHED BY ANYTHING ELSE. Claiming the seat is
#: reachable "again" asserts that the other waiter is not competing for its
#: rows, and that is only true where the fresh reading says so.
_EXCLUSIVITY = {
    GONE: ("THIS waiter was kept rather than exited, so the seat is "
           "reachable again."),
    UNREADABLE: ("THIS waiter was kept rather than exited, so the seat is "
                 "reachable through it; whether anything else also reaches "
                 "it cannot be read now."),
    REFUTING: ("THIS waiter was kept rather than exited, so the seat is "
               "reachable again."),
    EXPIRED: ("THIS waiter was kept rather than exited, so the seat is "
              "reachable through it — and the other one can no longer be "
              "ruled out as reaching it too."),
    SINK_UNREADABLE: ("THIS waiter was kept rather than exited, so the seat "
                      "is reachable through it; whether anything else also "
                      "reaches it cannot be read now."),
}

_WITHHELD_STATE = {
    GONE: ("It has since exited anyway, so it is no longer sharing this "
           "seat's wakes."),
    UNREADABLE: ("Whether it is still running cannot be read now, so what it "
                 "is doing with this seat's rows is UNKNOWN."),
    REFUTING: ("It is still running, so this seat now has two waiters and "
               "only this one wakes it."),
    EXPIRED: ("It is still running and its stdout can no longer be shown to "
              "reach nobody, so it may be waking this seat too."),
    SINK_UNREADABLE: ("It is still running, but whether its output reaches "
                      "anyone cannot be read now."),
}

#: THE RECIPE IS A MONITOR, NOT A SHELL LINE. A bare `helm chat wait` in a
#: terminal cannot re-invoke an agent's turn loop, so prescribing one retires
#: a working beacon and replaces it with output nobody is reading — the exact
#: failure this message is about. AND IT IS ONLY PRESCRIBED WHERE THERE IS
#: SOMETHING TO RETIRE: against a pid this pass cannot see, re-arming spends a
#: live beacon on nothing.
_RETIRE = ("retire it deliberately by RE-ARMING THE MONITOR with --replace: "
           + beacon_monitor("%(seat)s", replace=True)
           + " from a session exporting HELM_CHAT_NAME=%(seat)s.")

_REPORTED_STATE = {
    GONE: ("Nothing further is owed: this process never had the authority to "
           "stop it, and there is no longer anything there to stop. If this "
           "seat is still missing wakes, the cause is a different waiter."),
    UNREADABLE: ("This process has no authority to stop it, and whether it "
                 "is still there cannot be read now — so nothing is "
                 "prescribed against a pid this pass cannot see."),
    REFUTING: ("This process has no authority to stop it, so it is still "
               "running and still consuming. To fix that, " + _RETIRE),
    EXPIRED: ("This process has no authority to stop it, and the reason to "
              "retire it has EXPIRED: its stdout can no longer be shown to "
              "reach nobody. If you still want a single waiter, " + _RETIRE),
    SINK_UNREADABLE: ("This process has no authority to stop it, and whether "
                      "it still consumes silently cannot be read now. If you "
                      "want a single waiter regardless, " + _RETIRE),
}


def nonwaking_message(armed, claimed):
    """The full stderr line for `armed["nonwaking"]`, re-read at render time.

    `claimed` is the seat name the recipe must name. Every branch below is a
    sentence about a process THIS process does not own, so each one states
    only what a fresh reading supports -- including UNKNOWN, which is an
    answer and not a failure."""
    dead = armed["nonwaking"]
    # THE OUTCOME DIFFERS BY AUTHORITY BRANCH, SO THE SENTENCE MUST TOO.
    # Saying "that one is still running and still consuming" was true on
    # the reporting path and false on the election path, where the stop
    # pass had already retired it — and a diagnostic that is wrong half
    # the time sends a seat chasing a process that no longer exists.
    withheld = dict((pid, why) for pid, why in (armed.get("kept") or ())
                    if pid is not None)
    # ONE FRESH READING GOVERNS THE WHOLE MESSAGE — every clause of it.
    #
    # The record is a PAST observation, and between the pass that wrote it
    # and the message that explains it a waiter can dup2 a live pipe over
    # its fd 1 or exit outright. This message makes THREE claims about that
    # process: what its sink is, whether it is still running and consuming,
    # and whether this seat is now reached by anything else. Curing only
    # the first left the other two speaking from the record, so the message
    # could print "its reason has EXPIRED and it may well be waking the
    # seat" and "only this one wakes it" in the same breath, or "cannot be
    # read at all" beside "it is still running". A message that contradicts
    # itself is worse than either half alone, because a reader cannot tell
    # which clause to believe.
    #
    # So the reading is taken ONCE, reduced to a single state, and every
    # sentence below is selected from that state. Adding a clause here
    # means adding a case to each table, which is the point: there is no
    # way to write a new sentence that quietly keeps speaking from the
    # record.
    #
    # LIVENESS IS SETTLED BEFORE THE SINK, because a bare pid names a SLOT
    # and not a process: between the pass and this line the waiter can exit
    # and the kernel can hand that number to a stranger, and a sink reading
    # taken then describes the STRANGER under the waiter's name.
    try:
        # IMPORTED HERE RATHER THAN RELIED ON FROM ABOVE. The module-level
        # name is bound inside an earlier `try`, so if that one failed a
        # bare reference would raise NameError INSIDE this catch and be
        # reported as an unreadable SINK — turning a programming error
        # into a confident statement about the world, which is the one
        # thing this whole cure is against.
        from . import beacons as _beacons
        alive = _beacons.pid_alive(dead["pid"], dead.get("starttime"))
        if alive is False:
            fresh = GONE
        elif alive is None:
            fresh = UNREADABLE
        else:
            now_state = _beacons.sink_probe(dead["pid"])[0]
            fresh = (REFUTING if now_state == _beacons.SINK_REFUTES else
                     EXPIRED if now_state == _beacons.SINK_ADMISSIBLE else
                     SINK_UNREADABLE)
    except Exception:                    # noqa: BLE001 — unreadable is UNKNOWN
        fresh = UNREADABLE
    if dead["pid"] in (armed.get("stopped") or ()):
        tail = ("It has been RETIRED in this pass, so nothing further is "
                "owed — this line is here so the disappearance is not a "
                "mystery.")
    elif dead["pid"] in withheld:
        tail = ("The stop pass WITHHELD its signal: %s. %s"
                % (withheld[dead["pid"]], _WITHHELD_STATE[fresh]))
    else:
        tail = _REPORTED_STATE[fresh] % {"seat": claimed}
    return ("[helm chat] inbox beacon at pid %s serves this seat+session but "
            "%s. %s %s" % (dead["pid"], _SINK_SAYS[fresh],
                           _EXCLUSIVITY[fresh], tail))
