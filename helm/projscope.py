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

_TLS = threading.local()
_MISS = object()        # a real None result is CACHEABLE; `.get() is not None`
                        # would silently re-ask for every honest "unknown"


def _state():
    """This thread's scope depth and cache."""
    tls = _TLS
    if not hasattr(tls, "depth"):
        tls.depth = 0
        tls.cache = {}
    return tls


class scope:
    """Memoize derived reads for the duration of ONE projection."""

    def __enter__(self):
        _state().depth += 1
        return self

    def __exit__(self, *exc):
        t = _state()
        t.depth -= 1
        if t.depth <= 0:
            t.depth = 0
            t.cache.clear()
        return False


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
