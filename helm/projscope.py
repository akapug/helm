"""ONE-INSTANT MEMO for a single land-loop projection.

WHAT THIS IS FOR. `landreq.project_raw` states — in its own docstring and in
four separate comments — that it is ONE READ AT ONE INSTANT: the state fold,
the per-row events, the events the fold took and the verdict positions are
"four views of one instant", and `cmd_lr` binds a single `read_now` so the
stamp a reader sees is the instant that classified the rows. Every derivation
downstream of that read is supposed to describe the same instant.

IT DID NOT. Two DERIVED reads inside that pass were re-asked per row, live,
against state that can move underneath the projection:

  * `dispatches.approval_tier(recipient, repo)` — resolves the reviewer's
    model family by measuring the LIVE proxy runtime: a full walk of /proc's
    fd tables (`seat_ports._socket_holders`), plus a network canary. Measured
    2026-08-06 on the live ledger: 627 calls over SIXTEEN distinct
    (recipient, repo) keys.
  * `landreq._git(...)` — the same argv re-run against the same repository.
    Measured the same pass: 4140 spawns, 650 of them byte-identical repeats,
    the worst single argv asked 48 times.

So the cost was real and so was the incoherence: two identical `merge-base
--is-ancestor` questions asked 40 seconds apart during ONE projection can get
DIFFERENT answers if trunk moves between them, and the board would then carry
two instants under one stamp — the exact defect `project_raw`'s docstring
exists to warn about, one layer down. Memoising for the duration of the pass
is therefore a CORRECTNESS fix that happens to be much faster, not a speed
trick that costs correctness.

THE SCOPE IS AN OPERATION, NEVER A CLOCK. Same law, same reasoning and
deliberately the same shape as `helm.store.load.read_scope` (see its docstring
for the full argument): entering starts an empty cache and LEAVING DROPS IT,
so no answer can outlive the projection that asked for it. There is no ttl, no
mtime key and no process-lifetime memo here, because helm's own history is
full of long-lived things serving state past its truth — helm web served
40h-old code; the owner console served an 8-day-old render. A cache that can
pin a stale board over a fresh one defeats the surface it is speeding up.

OUTSIDE A SCOPE NOTHING IS CACHED and behaviour is byte-identical to no memo
at all. That is what keeps the write paths safe: the land door, the close
ladders and the send advisory all call the same functions and none of them
enters a scope, so none of them can be answered from an older question.

PER-THREAD, NOT PER-PROCESS, and re-entrant — both for the reasons store's
scope records: helm web is a ThreadingHTTPServer, so a module-level dict lets
one request answer another's read; and a nested scope must share the outermost
cache and only the outermost may clear, or the memo evaporates under
composition.

A RAISE IS NEVER MEMOISED. `memo` stores only a value that was returned, so a
transient failure is re-asked rather than frozen into the pass.
"""
import threading
import time

_TLS = threading.local()
_MISS = object()        # a real None result is CACHEABLE; `.get() is not None`
                        # would silently re-ask for every honest "unknown"


def _state():
    """This thread's scope depth, cache and budget stack."""
    tls = _TLS
    if not hasattr(tls, "depth"):
        tls.depth = 0
        tls.cache = {}
        tls.deadlines = []
    if not hasattr(tls, "deadlines"):        # a thread that predates budgets
        tls.deadlines = []
    return tls


class Expired(Exception):
    """The ambient budget was already spent when work was about to START.

    A DISTINCT SIGNAL, NOT AN UNCOLLAPSIBLE ONE. A RETURNED value for "no
    time left" — None, False, an empty result — shares a channel with the
    ordinary answers, so it can be consumed by a caller that never considered
    it: `p is not None and p.returncode == 0` reads an out-of-time spawn as a
    failed one, and a failed one as "no", and "no" is a verdict. Raising puts
    the answer on a SEPARATE channel, so a caller that has not thought about
    the case gets a loud stack rather than a quiet wrong answer.

    IT IS STILL AN ORDINARY EXCEPTION AND A CALLER CAN SWALLOW IT. `except
    Exception: return False` collapses this exactly as it collapses anything
    else. What this type buys is that the collapse must be WRITTEN, not
    inherited from a shape; preserving UNKNOWN across it remains the caller's
    obligation and no exception type can discharge it.

    WHAT THIS BUDGET IS, EXACTLY, so nothing here is read as more: a set of
    COOPERATIVE POINT-IN-TIME CHECKS at explicit boundaries. Publishing the
    deadline ambiently removes the need to plumb it through every signature;
    it does NOT force work through `spend_or_raise`. There is no automatic
    refusal from the memo (a cached answer is still served inside an expired
    scope), no cancellation or preemption of work already running, no cap on
    a subprocess, no guarantee about time spent AFTER a check passes, and no
    bound on total runtime. A caller that spends without asking is
    unbudgeted, and this module cannot tell.
    """


class scope:
    """Memoize derived reads for the duration of ONE projection.

    `deadline` is an OPTIONAL `time.monotonic()` instant. When set, it is
    ambient for everything inside the scope: the spending door reads it
    rather than being handed it, so a helper need not accept a parameter to
    be inside the budget.

    A SCOPE OPENED WITHOUT A DEADLINE IS UNLIMITED ONLY WHEN NO ENCLOSING
    SCOPE CARRIES ONE. Nesting alone changes nothing — an unbudgeted scope
    inside another unbudgeted scope is still unlimited — but inside a scope
    that DOES carry a deadline it inherits that deadline and its spends can
    expire, which is the point of inheritance and the opposite of what "no
    deadline means no limit" would suggest. Whenever no budget exists above
    it, `remaining()` answers None and no spend is ever refused.

    A NESTED SCOPE MAY ONLY TIGHTEN. An inner budget wins when it is sooner
    and is IGNORED when it is later: an inner scope must never be able to buy
    time its caller did not have.
    """

    def __init__(self, deadline=None):
        self._deadline = deadline

    def __enter__(self):
        t = _state()
        t.depth += 1
        outer = t.deadlines[-1] if t.deadlines else None
        mine = self._deadline
        if outer is None:
            eff = mine
        elif mine is None:
            eff = outer
        else:
            eff = min(mine, outer)
        t.deadlines.append(eff)
        return self

    def __exit__(self, *exc):
        t = _state()
        t.depth -= 1
        if t.deadlines:
            t.deadlines.pop()
        if t.depth <= 0:
            t.depth = 0
            t.cache.clear()
            t.deadlines = []
        return False


def deadline():
    """The innermost ambient deadline, or None when no budget is set."""
    t = _state()
    return t.deadlines[-1] if t.deadlines else None


def remaining():
    """Seconds left on the ambient budget; None when there is no budget.

    None and 0.0 are DIFFERENT ANSWERS and callers must not treat them alike:
    None is "spend freely", 0.0 or less is "spend nothing". Returning None
    for both is how an expired budget becomes an unlimited one.
    """
    d = deadline()
    return None if d is None else d - time.monotonic()


def spend_or_raise(what):
    """Call at a spending boundary, BEFORE the work. -> remaining seconds
    (None when unbudgeted); raises `Expired` when the budget is gone.

    `what` names the work in the exception so a caught Expired can say which
    stage did not run, rather than only that something did not.
    """
    left = remaining()
    if left is not None and left <= 0:
        raise Expired("budget spent before %s" % what)
    return left


def active():
    """Is a scope open on THIS thread?"""
    return _state().depth > 0


def memo(key, compute):
    """`compute()` once per `key` per scope; outside a scope, always compute.

    `key` must be hashable and must name every input the answer depends on —
    an under-specified key is how a memo starts answering a question it was
    not asked. An unhashable key is not an error: it degrades to computing,
    because a projection that cannot cache is slow and a projection that
    caches under a colliding key is wrong.
    """
    t = _state()
    if t.depth <= 0:
        return compute()
    try:
        hit = t.cache.get(key, _MISS)
    except TypeError:                   # unhashable key: no memo, no collision
        return compute()
    if hit is _MISS:
        hit = compute()
        t.cache[key] = hit
    return hit


def forget(key):
    """Evict one key. For the caller that computed an UNKNOWN and must not let
    it stand in for a measured answer.

    A memo's contract is "the same question has the same answer for this pass".
    A spawn failure, a timeout, an unreadable file breaks that: it is a fact
    about the MOMENT, not about the subject, and the next caller asking the
    same question may well get a real answer. Caching it lets ONE transient
    blip answer for the whole projection, and — worse — a cached UNKNOWN is
    indistinguishable from a measured one at every read site downstream.
    So the caller that can RECOGNISE its own UNKNOWN evicts it. memo() cannot
    do this itself: it does not know what its callers consider an answer.
    Silent on a missing key and outside a scope: forgetting what was never
    remembered is the desired end state, not an error.
    """
    t = _state()
    if t.depth <= 0:
        return
    try:
        t.cache.pop(key, None)
    except TypeError:                   # unhashable: it was never cached
        pass
