#!/usr/bin/env python3
"""helm seats — the SessionStart banner a seat reads first (`join_banner`).

Split out of helm/seats_join.py, which imports it back, so the renderer of
the highest-frequency message helm prints sits at its own door while
`join` decides coverage. It reads nothing and writes nothing but words.
"""

from . import seats_advice
from .seats_common import GUIDE_PATH


def join_banner(seat, display_room, scope, covered_pid=None):
    """The SessionStart banner for one seat — the armed form when a live
    waiter's pid is supplied, else the one that asks for the first action.

    A PURE RENDERER AT ITS OWN DOOR, because this is the highest-frequency
    message helm prints and a budget over it has to measure the artifact
    rather than a reconstruction of it. `join` decides coverage; this decides
    nothing and only writes words.

    THE ARMED FORM IS THE SHORTER ONE, which it was not: 771 characters to say
    that nothing is owed, four sentences of them re-arm caveats that matter
    only later. It states the fact and stops; the caveats are in the guide it
    already cites, which is where a seat that needs them is reading anyway.

    THE MONITOR ARGV IS THE PAYLOAD of the other form and stays verbatim, and
    so does the deferred-tool fallback, because a seat that cannot find the
    tool cannot perform the act. What is not here is the rationale — why a
    background shell cannot wake a PTY agent — which the guide carries whole.
    The call grew by the description the Monitor tool requires (task/3435),
    and the prose around it gave that width back: the fallback keeps its act
    and drops its label, and the pointer says what the guide explains in
    fewer words.

    AND THE CITATION IT CARRIED WAS DEAD: it named premise
    `native-wake-only-agent-armed`, the store holds
    `native-wake-only-agent-armed-or-headless`, and `helm store get` on the
    cited spelling returns nothing. A dangling id printed at every session
    start is worse than no id — it teaches readers that helm's citations do
    not resolve. The pointer is the guide now, and it is a path that exists."""
    if covered_pid is not None:
        return ("[helm chat] seat '%s' in room %s. Your beacon is already "
                "armed for this session (waiter pid %s), so nothing is owed; "
                "catch up with `helm chat read`. If it dies, or you are "
                "addressed and do not wake, re-arm — see %s"
                % (seat, display_room, covered_pid, GUIDE_PATH))
    return ("[helm chat] seat '%s' in room %s — mentions, DMs and @all wake "
            "you between tool calls%s. First action: arm your "
            "beacon — %s; %s. No Monitor tool? ToolSearch(query: "
            "\"select:Monitor\"). Why not a background shell: %s"
            % (seat, display_room, scope, seats_advice.beacon_monitor(seat),
               seats_advice.BEACON_EXPIRY_TERSE, GUIDE_PATH))
