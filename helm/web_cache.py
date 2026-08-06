"""Shared cache state for :mod:`helm.web`."""
import sys
import threading

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



# ── quota surface: creds / history / burn / allocate / status / homes ──
# ABSORBED from the predecessor cockpit's route handlers (behavior-preserving):
# same JSON shapes, so the ported quota UI works unmodified. providers.py owns
# every fact about accounts/windows/history; these handlers only cache + join.

_qlock = threading.Lock()

_qstate = {}

_qinflight = {}

_PROVIDER = None  # lazy singleton; tests may inject a stub here



def _provider():
    global _PROVIDER
    if _PROVIDER is None:
        from . import providers
        provider = providers.default_provider()
        facade = sys.modules.get(__package__ + ".web")
        if facade is None:
            _PROVIDER = provider
        else:
            setattr(facade, "_PROVIDER", provider)
    return _PROVIDER



def _cached_swr(key, ttl, hard_ttl, fn, cold_body=None, cold_wait=None):
    """Serve-stale-while-revalidating over the same single-flight state.

    THE FLAP THIS CLOSES, measured on the owner's screen: `_cached` BLOCKS the
    first request after every TTL lapse for the whole rebuild — /api/lr's
    projection runs 22-28s against the card's 12s fetch deadline, so every
    30s cycle one poll aborted and the home card alternated healthy/UNKNOWN
    all evening.

    Three regimes, and the boundaries are the design:
    - FRESH (age < ttl): serve the entry, same as `_cached`.
    - STALE (ttl <= age < hard_ttl): kick ONE background rebuild and serve the
      previous body immediately. This is NOT the forbidden mtime short-circuit
      from `_lr_build`'s history: the read is never SKIPPED — a rebuild runs
      right now, and its result (including a fresh `unavailable`) replaces the
      entry within one build. Truth arrives at most one build late, and the
      card's own read-age header counts the staleness in the open.
    - ANCIENT (age >= hard_ttl) or EMPTY: block on a foreground build exactly
      like `_cached`. A rebuild that cannot finish inside hard_ttl has lost
      the right to keep serving its past — the chmod-000 law bounded in time
      instead of re-opened.

    COLD (EMPTY) MAY BOUND ITS BLOCK, opt-in via `cold_body` + `cold_wait`:
    wait up to `cold_wait` for the first build and answer `cold_body` only if
    it has not finished. MEASURED on the owner's console: a cold /api/lr costs
    21.4s (a whole `stalls()` projection) against the card's 12s fetch
    deadline, so the FIRST view after any restart aborted and rendered "⚠
    DISPATCH LEDGER UNREADABLE — pipeline UNKNOWN". The ledger was perfectly
    readable; it had not been COMPUTED, and those are different facts that
    only the server can tell apart.

    THE WAIT IS WHAT MAKES THIS SAFE. Answering the placeholder immediately
    was the first cut and it was wrong: every first caller took a placeholder
    it did not need — including callers with no deadline at all — and a test
    suite went from green to 51 errors because handlers that had always
    returned real data on the first call suddenly did not. With the wait, a
    build that finishes quickly (every test, every warm machine) still answers
    with REAL DATA and this regime stays invisible until it is needed.

    ANCIENT IS UNTOUCHED and still blocks: it HAS a past it has lost the right
    to serve. EMPTY has no past, so nothing is served stale — the only choice
    is between a false claim and an honest one.
    """
    now = time.time()
    cold_waiter = None
    with _qlock:
        ent = _qstate.get(key)
        age = None if ent is None else now - ent[0]
        if age is not None and age < ttl:
            return ent[1]
        if age is not None and age < hard_ttl:
            if key not in _qinflight:            # ONE background rebuild
                ev = threading.Event()
                _qinflight[key] = ev
                try:
                    threading.Thread(
                        target=_swr_rebuild, args=(key, fn, ev),
                        daemon=True).start()
                except BaseException:
                    # Two cross-family review blockers. Round one: a spawn
                    # that fails before any worker exists orphans the event —
                    # every future ANCIENT reader blocks 300s-per-loop on it.
                    # Round two, the subtle half: start() can raise AFTER the
                    # OS worker launched, so this catch retracts event A while
                    # worker A is still alive. The worker therefore carries its
                    # OWN event and its finally pops the mapping only when the
                    # mapping still holds that exact event — a successor's
                    # event B published after this retraction survives worker
                    # A's completion untouched. Both sides identity-check;
                    # neither ever pops the other's.
                    if _qinflight.get(key) is ev:
                        _qinflight.pop(key)
                    ev.set()
                    raise
            return ent[1]
        if ent is None and cold_body is not None and cold_wait is not None \
                and now - _qcold.setdefault(key, now) < _COLD_BODY_MAX_S:
            # BOUNDED, BECAUSE AN UNBOUNDED PLACEHOLDER IS A PERMANENT LIE —
            # and a worse surface than the alarm it replaces, since the alarm
            # at least tells the owner to look. If no build ever stores, the
            # cache stays EMPTY and this branch would reassure forever; after
            # _COLD_BODY_MAX_S of never once succeeding we stop and fall
            # through to the blocking path, which surfaces the real failure.
            # `_qcold`'s stamp is cleared the moment a build stores, so an
            # endpoint that warms normally never approaches the bound.
            if key in _qinflight:
                cold_waiter = _qinflight[key]
            else:
                cold_waiter = threading.Event()
                _qinflight[key] = cold_waiter
                try:
                    threading.Thread(
                        target=_swr_rebuild, args=(key, fn, cold_waiter),
                        daemon=True).start()
                except BaseException:
                    if _qinflight.get(key) is cold_waiter:
                        _qinflight.pop(key)
                    cold_waiter.set()
                    raise
    if cold_waiter is not None:
        finished = cold_waiter.wait(cold_wait)
        with _qlock:
            ent = _qstate.get(key)
        if ent is not None:
            return ent[1]            # it finished in time: REAL data, as before
        if not finished:
            return cold_body         # still building — honestly "not read yet"
        # FINISHED WITHOUT STORING MEANS IT RAISED, and a placeholder must
        # never stand in front of a real failure. `_swr_rebuild` sets its event
        # in a finally, so event-set with no entry is exactly the failed build
        # — the one case where "warming" would be a reassuring lie about a
        # handler that is genuinely broken. Fall through and let the blocking
        # path surface it as `unavailable`, which is the fail-LOUD contract
        # this endpoint has always had.
        _qcold.pop(key, None)
    return _cached(key, ttl, fn)



def _swr_rebuild(key, fn, own_ev):
    """The background arm of `_cached_swr` — same completion contract as the
    foreground path: store under the lock, then release the in-flight event so
    blocked waiters (an ANCIENT-regime reader that arrived meanwhile) wake.

    `own_ev` is THIS worker's event, passed at spawn. The finally pops the
    mapping ONLY if it still holds exactly that event: when start() raised
    after the OS worker launched, the spawner already retracted this event and
    a successor may have published its own — a blind pop(key) here would
    remove and set the successor's event while the successor's worker is still
    running (a review probe measured exactly that). The worker's own
    event is ALWAYS set, so anything that waited on it wakes regardless."""
    try:
        val = fn()
        with _qlock:
            _qstate[key] = (time.time(), val)
            _qcold.pop(key, None)      # a real body exists; the cold clock is moot
    finally:
        with _qlock:
            if _qinflight.get(key) is own_ev:
                _qinflight.pop(key)
        own_ev.set()



def _cached(key, ttl, fn):
    """Single-flight TTL cache (ported from the predecessor): concurrent misses
    on one key share one build instead of racing."""
    while True:
        with _qlock:
            ent = _qstate.get(key)
            if ent and time.time() - ent[0] < ttl:
                return ent[1]
            ev = _qinflight.get(key)
            if ev is None:
                _qinflight[key] = threading.Event()
                break
        ev.wait(timeout=300)
    try:
        val = fn()  # computed outside the lock; other keys stay readable
        with _qlock:
            _qstate[key] = (time.time(), val)
        return val
    finally:
        with _qlock:
            _qinflight.pop(key).set()



# ── LAND PIPELINE (GET /api/lr) ──────────────────────────────────────────
# The owner's first surface for LANES. roster covers seats and the ledger tab
# covers turns; until this endpoint the land pipeline existed ONLY in the CLI
# (`helm lr list` / `helm lr stalls`), so every claim about a lane reached him
# as an agent's prose with nothing on screen able to contradict it.
#
# Two laws shape everything below.
#
# (1) UNAVAILABILITY IS LOUD. An unreadable dispatch ledger answers
#     `unavailable` carrying landreq's own verbatim reason and ZERO rows,
#     which the card renders as a strip. A quiet empty list would read as
#     "nothing in flight" — the exact lie this surface exists to prevent — so
#     there is no code path here that turns a failed read into a happy board.
#     `_api_todos`' `{"unavailable": true}` boolean is deliberately NOT copied:
#     a strip that cannot name its own reason sends the reader back to the CLI
#     with nothing to go on.
#
#     THE SAME LAW APPLIES BELOW THE WHOLE-BOARD LEVEL, which is where a
#     cross-family review found it broken. A read that half-succeeded
#     (`landreq.project` reads the
#     dispatch ledger twice), a closure whose instant the record does not carry,
#     a list the phone cap truncated, a receipt ledger that could not be opened
#     — every one of them used to render as a confident value: a zero age, an
#     empty window, a smaller count, "no receipt". Each now has a term of its
#     own on the wire (`dwell_known`, `closed_ts`, `closed_total` /
#     `closed_unknown_when`, `receipt_state: unreadable`) and a LOUD rendering.
#
# (2) THE RECOMPUTE FLOOR. landreq.project() spawns git per closed row per
#     repo — the thread-pool starvation class that bit /api/chat (see the
#     _rooms_summary_cached single-flight above). So the whole projection sits
#     behind a 30s single-flight floor keyed on a CONSTANT — ledger churn must
#     not be able to force an early recompute — and NOTHING below that floor
#     may skip the walk. See _lr_build for why that second half is a law and
#     not an oversight.
# A key may answer `cold_body` only while its cache has NEVER been filled, and
# only for this long. Past it we stop saying "warming" and let the blocking
# path speak: a placeholder that outlives every build is a lie with no expiry.
_COLD_BODY_MAX_S = 300

_qcold = {}                 # key -> when this key FIRST answered from cold
del _web
