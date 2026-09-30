"""THE LAND ORDER: one lock orders the land vetoes named here against
auto-land's push.

`helm train auto`'s last word (helm/autoland.py `_Tick.last_word`) reads the
plan, each door car's admission and the receipt's land authority one last
time, then pushes, all under this lock: the READINESS LOCK, the dispatch
ledger's own (every verdict, hold, retip and withdrawal writer takes it). A
veto written between that read and the push would be one the push never saw.
So these writers of a land veto or of land authority take this lock before
they write (task/3265 races R2), and each write is either visible to the last
read or ordered after the push:

  * the ejection store (`landwindow._append_ejection`);
  * the gate canary's DISABLE marker and every row of its record
    (`gatecanary.write_marker`, `gatecanary._append_record`), which the gate
    shadow's DISAGREE writes too;
  * the approval-tier policy (`store.write_prior` of a prior that declares
    it, or replaces one that did);
  * the runtime testimony the admission reads to name a holder's family and
    model (`seats.write_roster` with testimony, `seats.bind_lifecycle_runtime`,
    `seats.stamp_proxy_runtime`), and the verbs that delete, move or rebind
    it: `seats.disown_session` (task/3265 r4) and a `seats.write_roster` join
    that rebinds its row's session or evicts the session from another row
    (door read B4), each only when a row it changes carries testimony
    (`seats_runtime._carries_testimony`).

NOT EVERY WRITER THE LAND READS TAKES IT. These write outside the order:
  * proxywatch's watch state (proxywatch.py's state write): a write of it
    alone only makes a proxy holder's admission fail closed (DAMAGED);
  * git config and refs: the destination is read last and pinned
    (autoland `_pinned`), and a trunk moved since the gate is refused by the
    push's lease, but no other git write is ordered;
  * `burnflags.local_families`;
  * gateimport's flip activation;
  * `seats.gc_roster` (task/3265 r6): gc deletes only dead seats' rows,
    which is housekeeping and not a veto, and the land acts on the snapshot
    its last read took.

THE ORDER OF LOCKS: the auto-land store's, then this one, then any writer's
own (the ejection store's, the roster's, the canary record's). A writer that
takes this one takes no auto-land lock after it.

IT IS LET GO BY CLOSING, NEVER BY UNLOCKING. The push's lock holder
(`vcs.GitVcs.run_holding`) inherits this lock's descriptor so that a tick
killed mid-push lets nothing go while its git runs on (task/3265 races F4);
the holder starts git with the descriptor closed, so nothing git leaves
running holds it (door read B3), and ends when git ends. An explicit unlock
would free the lock for every holder at once, the holder among them; a close
frees it only when the last holder closes its copy (`eventledger.locked`
`unlock=False`).

REENTRANT ON ONE THREAD. A thread that holds it and writes a veto (a test's
seam, or a writer called inside the last word) passes straight through,
because the write is inside the ordered section already. Another thread, or
another process, waits.

LIGHT ON PURPOSE: a join at session start takes it when it carries or
changes runtime testimony, so it imports only the event ledger and the home.
The ledger's name is `dispatches.LEDGER`, spelled here so the join does not
import the dispatch module; tests/test_landorder.py holds the two equal.
"""
import contextlib
import os
import threading

from . import eventledger, home

#: `dispatches.LEDGER`: the ledger whose lock this is.
LEDGER = "dispatches.jsonl"

_HELD = threading.local()


def ledger_path(global_dir=None):
    """The dispatch ledger of `global_dir` (the home's by default)."""
    return os.path.join(global_dir or home.global_dir(), LEDGER)


def path(global_dir=None):
    """The file this lock is a flock on: the ledger's `<ledger>.lock`
    sibling, as `eventledger.locked` opens it."""
    return os.path.abspath(ledger_path(global_dir)) + ".lock"


@contextlib.contextmanager
def locked(timeout=None, global_dir=None):
    """Yield whether the land order is held. With no `timeout` it waits for
    it (an ambient projscope deadline still bounds the wait); a thread that
    already holds it yields True at once. False is a lock that could not be
    set up or was not taken in time, and what follows is the caller's: a
    veto writer writes anyway, the ejection store refuses, and the land's
    last word does not push. UNDER AN AMBIENT PROJSCOPE DEADLINE a wait that
    outlasts it raises `projscope.Expired` (`eventledger.locked`) instead of
    yielding: that veto writer does not write anyway, its write is not made
    and its scope fails."""
    key = path(global_dir)
    depth = getattr(_HELD, "depth", None)
    if depth is None:
        depth = _HELD.depth = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield True
        finally:
            depth[key] -= 1
        return
    with eventledger.locked(ledger_path(global_dir), timeout=timeout,
                            unlock=False) as held:
        if not held:
            yield False
            return
        depth[key] = 1
        try:
            yield True
        finally:
            depth[key] = 0
