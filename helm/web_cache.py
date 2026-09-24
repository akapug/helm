"""Shared cache state for :mod:`helm.web`."""
import contextlib
import errno
# `json` AND `time` ARE IMPORTED HERE RATHER THAN INHERITED. Every name this
# module uses and does not bind arrives through the facade's fanout (see
# `helm.web._seed_impl_modules`), which works only once `helm.web` itself has
# been imported — true on every route, and NOT true of a caller that reaches an
# endpoint's module directly. `web_owed` is such a caller: it imports this
# module by name to serve the burn-down through `_cached_swr`, and without
# these two lines the first cached read raised `NameError: name 'time' is not
# defined` and the endpoint reported itself unavailable. The fanout still
# rebinds both to the facade's objects, which are these same two modules.
import json
import os
import sys
import threading
import time
from . import pk

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



# ── quota surface: creds / history / burn / allocate / status / homes ──
# ABSORBED from the predecessor (its server route handlers, behavior-preserving):
# same JSON shapes, so the ported quota UI works unmodified. providers.py owns
# every fact about accounts/windows/history; these handlers only cache + join.

_qlock = threading.Lock()
# Tolerance for a persisted record whose stored_ts is ahead of this clock.
# Ordinary jitter between a write and a later read is seconds; anything beyond
# this is a record that cannot be trusted about its own age, and a body with a
# negative age can never leave the FRESH regime on its own.
_FUTURE_TS_GRACE_S = 60.0
# THE SAVED BODY'S SHAPE IS AN INPUT TOO. The witness proves the DATA a body
# was computed from is unchanged; it says nothing about the CODE that shaped
# it. Across a deploy that adds, renames or re-nests a field, the ledgers can
# be byte-identical while the body is the wrong shape — and it would restore
# FRESH on a matching witness, so the console would render a stale schema as
# current with no rebuild to correct it. Bump this whenever the persisted body
# changes shape; an unrecognised value is refused, never migrated in place.
_PERSIST_SCHEMA = 3

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



def _build_scope(snapshot):
    """The caller's READ-SET, held open, or a no-op yielding None.

    A CACHE CANNOT KNOW WHAT A KEY IS COMPUTED FROM. That used to be why the
    input identity was a `witness` callable the caller supplied; it is now why
    the caller supplies the whole SNAPSHOT — one object that serves and RECORDS
    every read the build makes, and from which the identity is PROJECTED.

    ONE ARGUMENT, NOT TWO, AND THAT IS THE FIX RATHER THAN A TIDY-UP. The
    previous shape took a `witness` function and, separately, a `scope` to run
    it in — so the identity could be sampled beside the build instead of derived
    from it, and a caller that wired one and forgot the other still read as
    bound. There is nothing here to half-supply: the object that answered the
    build's reads is the object asked for the witness.

    `None` means the caller has no identifiable input, which is every caller of
    this shared cache except the land projection. Those persist nothing, restore
    nothing, and behave byte-identically to before."""
    return contextlib.nullcontext(None) if snapshot is None else snapshot()


class _ConstSnapshot:
    """The DEGENERATE snapshot: an input whose identity is a LITERAL.

    A caller whose input cannot move — or a suite pinning one axis of this
    module without dragging a whole land projection in — identifies it by
    naming it. It is a real snapshot rather than a bypass: it answers `witness`
    and `recheck` on the same contract, and `recheck` re-states the identity
    exactly as the land read-set re-collects one. What no caller gets from it is
    a way to persist a body under an identity nothing produced.

    IT IS ITS OWN FACTORY. `snapshot=` takes something callable that opens a
    read-set for one build, and `persist_load` takes the OPENED object; a
    literal identity needs no opening, so calling it yields itself. One object
    serves both positions, which is the same "no half-wiring" argument the land
    read-set makes with one argument instead of two."""

    __slots__ = ("token",)

    def __init__(self, token):
        self.token = token

    def __call__(self):
        return contextlib.nullcontext(self)

    def witness(self):
        return self.token

    def recheck(self, _saved):
        return self.token


def _cached_swr(key, ttl, hard_ttl, fn, cold_body=None, cold_wait=None,
                snapshot=None, fingerprint=None, unchanged_max=None):
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
      instead of re-opened. A body RESTORED from the previous server life is
      the one entry that may be older than hard_ttl and still be served: it is
      DATED rather than ancient, because no rebuild of this life has failed to
      replace it yet. See the grant taken at the restore below, which is bound
      to that entry and expires hard_ttl after it.

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
    is between a false claim and an honest one. A RESTORE SITS BETWEEN THE TWO
    and was routed to the wrong one: the cache reads as non-EMPTY the moment a
    saved body lands, so this bounded answer is skipped, while the body's real
    age puts it in the regime with no bound. Neither of those is the state it
    is in — this life has lost nothing, it has not yet built anything.

    STALE MAY DECLINE TO REBUILD WHEN ITS INPUTS HAVE NOT MOVED, opt-in via
    `fingerprint` + `unchanged_max`, and this is the one regime the option
    touches. `fingerprint()` is a caller-supplied identity of everything the
    body is computed FROM; when it matches the identity recorded at the last
    rebuild decision, and the entry is younger than `unchanged_max`, the
    previous body is served and no rebuild is kicked.

    THIS IS NOT THE MTIME SHORT-CIRCUIT `_lr_build` KILLED, and the two
    differences are the whole argument. That one was UNBOUNDED — a `chmod 000`
    moves neither mtime nor size, so its memo served the last healthy board
    forever over a ledger nobody could read. This one is bounded twice: the
    caller's `fingerprint` is responsible for naming permission and identity
    as well as content (see `_input_fingerprint`, which carries mode, inode
    and ctime for exactly that reason), and
    `unchanged_max` caps how long any match may hold — past it the rebuild
    runs whatever the fingerprint says, so the board cannot be pinned by an
    input this cache failed to think of. A caller that supplies neither gets
    the behaviour this function always had.

    THE CAP IS THE CALLER'S AND IT BELONGS BELOW `hard_ttl`. Letting a match
    hold until ANCIENT would put the next rebuild on the BLOCKING path, where
    the owner's poll waits the whole projection out — the flap this function
    exists to close. Under a cap the entry can never leave the STALE regime,
    so every rebuild it ever performs stays in the background.
    """
    now = time.time()
    cold_waiter = None
    # OUTSIDE `_qlock`, BECAUSE IT IS I/O. Same law as the restore below: the
    # cache lock is for the dict. A fingerprint that cannot be taken answers
    # None and the rebuild proceeds, which is the direction that can only cost
    # work and never pin a body.
    mark = None
    if fingerprint is not None and unchanged_max is not None:
        try:
            mark = fingerprint()
        except Exception:                  # noqa: BLE001 — a failed identity
            mark = None                    # is NOT a matching identity
    # THE RESTORE I/O DOES NOT HOLD THE GLOBAL CACHE LOCK. `persist_load` reads
    # the saved envelope AND stats the ledgers, the trunk and the premise store
    # to rebuild a witness — real I/O — and doing that inside `_qlock` blocked
    # unrelated keys that were already FRESH and needed nothing: measured, one
    # 0.350s restore blocked an unrelated already-fresh read for the same
    # 0.350s. A cache lock is for the dict, not for the disk.
    #
    # THE ATTEMPT IS STILL CLAIMED UNDER THE LOCK, so exactly one caller pays
    # for the read and the rest fall straight through — only the I/O moved out.
    # The install then RE-CHECKS under the lock, because a rebuild may have
    # published a real body while we were reading a saved one, and a body from
    # disk must never displace a body computed since.
    restored, claimed = None, False
    with _qlock:
        if _qstate.get(key) is None and key not in _qrestored:
            # ADOPT THE PREVIOUS SERVER LIFE'S BODY, ONCE PER KEY. Done here
            # rather than in a startup hook so it holds for every entry point
            # — the server, a test, the CLI's warm read — instead of only the
            # one path someone remembered to wire.
            #
            # `_qrestored` marks the ATTEMPT, not the success: a key with no
            # saved body must not re-read the disk on every EMPTY poll, and a
            # key whose body was refused for staleness must not be re-offered
            # the same refused body a second later.
            _qrestored.add(key)
            claimed = True
    if claimed:
        # A FRESH READ-SET, COLLECTED ONCE, COMPARED TO THE SAVED PROJECTION.
        # The cache cannot know what a key is computed FROM, so a caller that
        # supplies no snapshot gets no restoration at all — the safe direction,
        # since no past beats an unidentifiable one.
        #
        # THE BODY IS NOT REBUILT TO ANSWER THIS. `recheck` replays the operands
        # the saved build actually consumed through the same canonical readers,
        # which is a stat of the ledgers, one trunk resolution per repository the
        # rows name, and one tier per reviewer — the same work the previous
        # witness function did, minus the second enumeration it needed to find
        # out what to read.
        with _build_scope(snapshot) as snap:
            restored = persist_load(key, ttl, snap)
        # RE-SAMPLE THE CLOCK, BECAUSE THE RESTORE DID REAL I/O. `now` was
        # taken before this block; persist_load then STATS the ledgers, the
        # trunk and the premise store to build its witness, and ages a
        # mismatched body against the clock as it stands AFTERWARDS. Using the
        # earlier `now` to classify that body subtracts a later timestamp from
        # an earlier instant and lands short of ttl: measured with a 2s witness
        # delay, a body persist_load had aged to 31s was classified at ~29s and
        # served FRESH with zero rebuilds. The witness said stale and the
        # arithmetic un-said it.
        now = time.time()
    with _qlock:
        if restored is not None and _qstate.get(key) is None:
            _qstate[key] = restored
            if now - restored[0] >= hard_ttl:
                # A RESTORED BODY IS ADMITTED DATED, NEVER ANCIENT.
                # `persist_load` deliberately ages a body whose input moved
                # while this server was down past `ttl`, so the STALE regime
                # serves it while a rebuild runs and the card states its age in
                # the open. But the age it carries is its REAL age, so a server
                # that was down longer than `hard_ttl` restores an entry that is
                # already ANCIENT — the one regime with no ceiling, which blocks
                # its first caller on the whole foreground build. MEASURED on
                # the owner's console: the first /api/lr of a server life took
                # 56.07s and the three calls after it 0.22-0.66s, which is why
                # the fault read as intermittent and why a reload appeared to
                # cure it. The bounded cold-start answer cannot cover it either:
                # restoring a body makes `ent` non-None, so the EMPTY branch
                # that waits `cold_wait` and answers "warming" is structurally
                # unreachable exactly when the symptom occurs.
                #
                # THE GRANT MOVES THE REGIME, NOT THE CLOCK. Clamping the
                # admitted timestamp up under `hard_ttl` lands the same body in
                # STALE, but it also makes the surface report a three-day-old
                # reading as nine minutes old — and the safety argument this
                # whole path rests on is that A CONFIDENT STALE BOARD IS WORSE
                # THAN THE TIMEOUT IT REPLACES. The timestamp therefore stays
                # true and `read_age_s` keeps disclosing the real age; only the
                # routing changes.
                #
                # BOUNDED TWICE, because a dated body that may be served
                # forever is the chmod-000 law re-opened. It is bound to THIS
                # entry's timestamp, so the first real body ends the grant with
                # nobody clearing it; and it expires `hard_ttl` after the
                # restore, so a rebuild that never once succeeds returns to the
                # blocking path that surfaces the real failure.
                _qdated[key] = (restored[0], now)
        ent = _qstate.get(key)
        age = None if ent is None else now - ent[0]
        if age is not None and age < ttl:
            return ent[1]
        stamp = _qdated.get(key)
        dated = ent is not None and stamp is not None and stamp[0] == ent[0]
        if age is not None and (age < hard_ttl
                                or (dated and now - stamp[1] < hard_ttl)):
            # A MATCHING FINGERPRINT IS A READ OF THE INPUTS, AND IT IS STAMPED
            # AS ONE. The body's own timestamp says when it was PROJECTED; this
            # stamp says when its inputs were last confirmed unmoved, which is
            # the age a freshness bound should judge. Measured on the owner's
            # console: the cap below let a body stand 300s plus a 70s rebuild,
            # the cards refuse anything over 180s, and every band except the
            # two that read elsewhere rendered STALE for most of each cycle —
            # over a ledger nobody had written to. The stamp is taken only
            # when the served body was built UNDER this fingerprint: a rebuild
            # in flight means the entry predates the mark recorded at its
            # decision, and a body that missed a write is not confirmed by the
            # write's own fingerprint.
            #
            # A CAP-KICKED REBUILD IS THE EXCEPTION, AND IT IS WHY THE KICK'S
            # REASON IS RECORDED. When the decision below found the mark
            # UNCHANGED and rebuilt only because the entry reached
            # `unchanged_max`, the served body WAS built under this same
            # fingerprint: nothing it could have missed has been written, so a
            # matching poll is a reading of every input exactly as it is with
            # no rebuild running. Refusing it here is what leaves the cards
            # reading STALE for the whole length of every cap-kicked rebuild
            # over a ledger nobody wrote to. A MOVED mark kicks a rebuild
            # whose body the entry genuinely predates, and that one still
            # confirms nothing.
            #
            # A DATED RESTORE IS NEVER CONFIRMED, WHATEVER THE MARK SAYS. The
            # stamp means "this body was built under the identity a poll is
            # reading now", and a restored body's witness MISMATCHED — that
            # mismatch is the only reason it is here. The mark this key records
            # was first taken on THIS server life, seconds ago, by the poll that
            # adopted the body; letting a later poll match it would stamp a
            # three-day-old board as a reading taken just now, which is the
            # confident stale board the grant above exists to avoid.
            confirmed = (mark is not None and _qfresh.get(key) == mark
                         and not dated
                         and (key not in _qinflight
                              or _qcapkick.get(key) is True))
            if confirmed:
                _qverified[key] = (ent[0], now)   # bound to THIS entry
            if confirmed and age < unchanged_max:
                # NOTHING THIS BODY IS COMPUTED FROM HAS MOVED since the last
                # rebuild decision, and the entry is still inside the cap. The
                # previous body is not stale in any sense a rebuild could fix.
                return ent[1]
            if mark is not None and age < unchanged_max \
                    and _qfresh.get(key) == mark:
                return ent[1]        # a rebuild is in flight; one is enough
            # RECORDED AT THE DECISION, NOT AT THE COMPLETION. A write that
            # lands WHILE the rebuild runs moves the fingerprint away from the
            # one recorded here, so the very next poll rebuilds again rather
            # than holding a body that missed it.
            #
            # AND WHY IT WAS DECIDED, beside the mark it was decided on. Only
            # two roads reach this line: the mark MOVED, or it matched and the
            # entry aged past `unchanged_max`. The second leaves the served
            # body built under the very fingerprint a poll is comparing, which
            # is what lets the confirmation above keep standing while the
            # rebuild runs. Recorded here rather than inferred later, because
            # the mark is overwritten on the next line and the reason is not
            # recoverable from it afterwards.
            _qcapkick[key] = mark is not None and _qfresh.get(key) == mark
            _qfresh[key] = mark
            if key not in _qinflight:            # ONE background rebuild
                ev = threading.Event()
                _qinflight[key] = ev
                try:
                    threading.Thread(
                        target=_swr_rebuild, args=(key, fn, ev, snapshot),
                        daemon=True).start()
                except BaseException:
                    # The review blockers, both rounds. Round one: a spawn
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
                        target=_swr_rebuild,
                        args=(key, fn, cold_waiter, snapshot),
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
    return _cached(key, ttl, fn, snapshot)



# ONE BACKGROUND READ PER KEY, JOINED UNDER EACH CALLER'S OWN BUDGET. A body
# composed of several reads must answer inside a bound even when one of them
# is cold (`web_board._board_build`, whose first read after a restart took 70s
# against the page's 45s). Each read runs on its own daemon thread; a caller
# that runs out of budget abandons that thread and never cancels it, and the
# next caller joins the SAME thread rather than starting a second read of the
# same thing. A reading younger than `ttl` is served as it is; one younger
# than `hard_ttl` is served while a read runs behind it; an older one is
# never served.
#
# ONLY A READING IS KEPT. `refuse(answer)` names why an answer is not one — a
# source that could not be read, or one still warming — and that answer goes
# to the caller that waited for it and is never stored, so the next caller
# asks again instead of being served it for a window. A read that raised or
# was refused is recorded in `_qfailed`, and a caller served an OLDER reading
# is told, so a source that has stopped answering never goes on showing its
# last numbers as if they were current. `_cached_swr` could do neither: it
# stores whatever its build returns, and a rebuild that raises behind a served
# body leaves that body standing until `hard_ttl`, silently.
_qreads = {}                 # key -> (thread, box) of the read running or last run
_qreads_lock = threading.Lock()
_qfailed = {}                # key -> (when, "raised" | "refused", why) of its last failed read


def _read_behind(key, ttl, hard_ttl, fn, refuse=None):
    """(thread, box) for `key`. A reading that may be served comes back at
    once, with no thread and its box filled; otherwise the thread is the read
    already running or a new one, for the caller to join under its own
    budget. A filled box holds `got` and `at` (when it was read); `failed` is
    (when, "raised" or "refused", the class name or the refusal) for a read
    that raised, and rides beside a served reading when a read after it
    failed."""
    now = time.time()
    with _qreads_lock:
        held = _qreads.get(key)
        running = held is not None and held[0].is_alive()
        with _qlock:
            ent, fail = _qstate.get(key), _qfailed.get(key)
        if ent is None or now - ent[0] >= hard_ttl:
            return held if running else _read_start(key, fn, refuse)
        if now - ent[0] >= ttl and not running:
            _read_start(key, fn, refuse)
        box = {"got": ent[1], "at": ent[0]}
        if fail is not None and fail[0] > ent[0]:
            box["failed"] = fail
        return None, box


def _read_start(key, fn, refuse):
    """Start one read of `key`, under `_qreads_lock` -> (thread, box)."""
    box = {}

    def read():
        try:
            got = fn()
            why = refuse(got) if refuse is not None else None
        except Exception as exc:            # noqa: BLE001 — named to the caller
            with _qlock:
                box["failed"] = _qfailed[key] = (time.time(), "raised",
                                                 type(exc).__name__)
            return
        at = time.time()
        with _qlock:
            if why:
                _qfailed[key] = (at, "refused", why)
            else:
                _qstate[key] = (at, got)
        box.update(got=got, at=at)
    held = (threading.Thread(target=read, name="read-" + key, daemon=True),
            box)
    held[0].start()
    _qreads[key] = held
    return held


def _drop(key, timeout=10):
    """Forget `key` whole: wait out a read of it still running, then drop its
    reading and its failure. For a caller (a test) that changes the world
    under a kept reading."""
    with _qreads_lock:
        held = _qreads.pop(key, None)
    if held is not None:
        held[0].join(timeout)
    with _qlock:
        _qstate.pop(key, None)
        _qfailed.pop(key, None)


def _input_fingerprint(paths):
    """An identity for a set of FILES — the term `_cached_swr` compares.

    IT IS NOT THE MTIME MEMO THE LAND CARD KILLED, and the difference is in
    what a term is made of. That one compared mtime and size, which a
    `chmod 000` moves NEITHER — so it served the last healthy board forever
    over a ledger nobody could read. Every term here carries MODE, INODE and
    CTIME as well: a chmod moves mode and ctime, a replace-by-rename moves the
    inode, a truncate-and-rewrite moves size and mtime.

    A MISSING FILE IS ITS OWN TERM NAMING THE ERRNO, so a ledger that VANISHES
    is a CHANGED identity rather than an absent one — the direction that
    rebuilds. An empty path list answers None for the same reason: a caller
    that named nothing has said nothing about what it read.

    THE STAT IS NOT A READ OF THE BODY. This runs before any build, to decide
    whether to build, so it deliberately does NOT go through the land
    projection's read-set — a reading the body never made must not enter the
    body's witness. That is also why the paths are handed in rather than
    resolved here."""
    marks = []
    for path in paths or ():
        try:
            st = os.stat(path)
        except OSError as exc:
            marks.append("%s\x1fmissing\x1f%d" % (path, exc.errno or errno.ENOENT))
            continue
        marks.append("%s\x1f%d\x1f%d\x1f%d\x1f%d\x1f%d"
                     % (path, st.st_mtime_ns, st.st_ctime_ns, st.st_size,
                        st.st_mode, st.st_ino))
    return "\x1e".join(marks) if marks else None


def _witness_value(snap):
    """The snapshot's PROJECTED input identity as a plain string, or None.

    A CALLABLE IS NOT A WITNESS, and passing one where a value belongs fails
    in the worst available way: the object is truthy, so every guard admits
    it, and it then dies inside a best-effort `except` that exists to keep a
    save failure from breaking a good projection. The result is a feature that
    reports success and writes nothing. Resolving the value here, once, keeps
    that shape impossible at the call sites.

    A SNAPSHOT THAT PROJECTS NOTHING IS NOT AN ERROR. It is the read-set saying
    it could not identify what it served — a blind read, or no read at all —
    and the answer to that is no witness, so no file, so no restore."""
    if snap is None:
        return None
    try:
        value = snap.witness()
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _persist_path(key):
    """Where one cache key's last body sleeps between server lives."""
    from . import home
    return os.path.join(home.global_dir(), "web-cache", "%s.json" % key)


def _carries_unavailable(body):
    """Does this body report a failure ANYWHERE, not just at the top?

    The land projection carries independent legs — `recent_lands` reads trunk,
    `native_chain` reads the premise store — and each names its OWN
    `unavailable` when it fails, deliberately, so one leg going dark does not
    blank the board. A check that looked only at the top level therefore saved
    a body whose lands or chain had failed transiently, and restored it as
    current after that leg recovered: a stale failure surfacing on a healthy
    system, which is exactly what refusing unavailable bodies is meant to
    prevent. One level of nesting is enough because that is the shape the
    projection builds; anything deeper is not a leg."""
    if not isinstance(body, dict):
        return False
    if body.get("unavailable"):
        return True
    return any(isinstance(v, dict) and v.get("unavailable")
               for v in body.values())


def _persist_store(key, ts, body, witness):
    """Write-behind one entry. BEST-EFFORT AND SILENT BY CONSTRUCTION.

    This runs on the store path of a cache every surface reads, so it may
    never raise: a projection that computed fine and then failed to be SAVED
    is still a perfectly good projection, and turning that into an error
    trades a real answer for a bookkeeping complaint.

    A WITNESS IS REQUIRED, AND THAT IS WHAT SCOPES PERSISTENCE. No witness, no
    file — so a caller that cannot identify its own input never lands on disk
    at all. `_cached` passes none and therefore persists nothing, which is the
    fix for a real exposure: wiring write-behind into the SHARED cache
    serialised every key it holds, including bodies carrying credential
    identity and home paths, out to a group-readable file. The narrower rule
    also happens to be the honest one, because a body that cannot be dated
    could never be restored anyway.

    AN UNAVAILABLE BODY IS NEVER SAVED. It is a real answer for the request
    that produced it and a lie about the world afterwards: restoring one puts
    a failure on the surface that outlived its cause, and the recovery that
    already happened cannot dislodge it while its input sits unchanged.

    Mode 0600 and an atomic replace. Private because this is fleet state on a
    shared box; atomic because a torn body read at next boot is worse than no
    body — the loader would have to distinguish truncated-JSON from
    absent-file, and the contract is that EMPTY has no past rather than a
    corrupt one."""
    if not witness:
        return
    if not isinstance(body, dict) or _carries_unavailable(body):
        return
    try:
        path = _persist_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema": _PERSIST_SCHEMA, "stored_ts": ts,
                       "body": body, "input_witness": witness}, fh)
        os.replace(tmp, path)
    except Exception:
        return


def persist_load(key, ttl, snap=None):
    """Give the EMPTY regime a past -> (ts, body), or None.

    THE HOLE THIS FILLS is the one `_cached_swr`'s own docstring names: FRESH,
    STALE and ANCIENT all have a previous body to reason about, and EMPTY does
    not. So every server restart re-opens the defect the owner keeps hitting —
    a cold /api/lr costs 22-28s against a 12s fetch deadline, and the card
    renders DISPATCH LEDGER UNREADABLE about a ledger that is perfectly
    readable and merely uncomputed.

    THE ADMITTED TIMESTAMP IS THE WHOLE SAFETY ARGUMENT, and it rests on the
    INPUT rather than on a clock: a body is current only while the input it
    was computed from is unchanged. When that input moved while this server
    was down, the body never saw the change, so it is admitted AGED PAST
    `ttl` — which lands it in the STALE regime that already exists, already
    serves a previous body while rebuilding, and already states the age in the
    open. It is never admitted FRESH on the strength of being young, because
    "young" is a fact about the clock and the question is about the INPUT.

    A CONFIDENT STALE BOARD IS WORSE THAN THE TIMEOUT IT REPLACES: the owner
    can tell a timeout from a working board, and cannot tell a frozen board
    from a live one. So every path that cannot establish freshness returns
    None and leaves the cache EMPTY, which behaves exactly as today."""
    try:
        with pk.open_regular(_persist_path(key), encoding="utf-8") as fh:
            saved = json.load(fh)
    except Exception:
        return None
    if not isinstance(saved, dict):
        return None
    # AN UNRECOGNISED SCHEMA IS REFUSED, NOT MIGRATED. Records written before
    # this field existed carry no schema and are refused for the same reason:
    # nothing proves their shape matches what this code now reads. The cost of
    # refusing is one rebuild; the cost of admitting is a wrong board served
    # confidently.
    if saved.get("schema") != _PERSIST_SCHEMA:
        return None
    body = saved.get("body")
    ts = saved.get("stored_ts")
    if not isinstance(body, dict) or not isinstance(ts, (int, float)):
        return None
    # A FUTURE TIMESTAMP PINS A BODY FRESH FOREVER, so it is refused rather
    # than clamped. Age is `now - ts`; one clock skew, one restored backup or
    # one hand-edited file makes that negative, and negative age is younger
    # than every ttl there will ever be — the entry can then never leave the
    # FRESH regime and never triggers the rebuild that would replace it. A
    # small tolerance absorbs ordinary clock jitter; beyond it the record is
    # not trustworthy about its own age and is dropped.
    if ts > time.time() + _FUTURE_TS_GRACE_S:
        return None
    # AN UNAVAILABLE BODY IS A LIE ABOUT THE PRESENT, however well dated. It
    # is refused on read as well as on write, because a file written before
    # that rule existed is still on disk and would otherwise put a failure on
    # the surface that outlived its cause.
    if _carries_unavailable(body):
        return None
    # THE INPUT IS IDENTIFIED, NOT DATED. A timestamp comparison answers "is
    # the input newer than the body", which mtime cannot honestly support: it
    # is settable, coarse enough to repeat across a same-second rewrite, and
    # completely unmoved by a chmod that makes a ledger unreadable. Each of
    # those admits a body whose input has actually changed. An exact witness
    # answers the stricter and simpler question — is this the SAME input —
    # and anything that is not a byte-for-byte match reads as a change.
    #
    # THE SAVED WITNESS IS READ FIRST BECAUSE IT SAYS WHAT TO RE-READ. `recheck`
    # collects a NEW read-set through the same canonical readers, replaying the
    # operands the saved body's build consumed, and projects it — so the two
    # sides of this comparison are the same function of the same reader set at
    # two instants, rather than two hand-written enumerations that agree today.
    saved_witness = saved.get("input_witness")
    if not isinstance(saved_witness, str) or not saved_witness:
        return None                      # pre-witness record: UNKNOWN, not old
    if snap is None:
        return None                      # cannot identify the input: UNKNOWN
    try:
        current = snap.recheck(saved_witness)
    except Exception:
        return None                      # cannot read the input at all: UNKNOWN
    if not isinstance(current, str) or not current:
        return None
    if current == saved_witness:
        return (ts, body)                # same input: the body is current
    # The input moved while this server was down, so the body never saw it.
    # Admit it AGED PAST ttl: the STALE regime already serves a previous body
    # while rebuilding and already states the age in the open, which is the
    # honest surface for "this is the last thing we knew".
    #
    # THE AGE IS NOT CLAMPED DOWN TO REACH THAT REGIME, and the routing is not
    # this function's to decide. A downtime longer than the caller's hard ttl
    # makes this body older than every ceiling here, and shrinking the reported
    # age to fit under one would buy the regime by lying about how dated the
    # reading is. The timestamp stays true; `_cached_swr` knows its own hard ttl
    # and grants such a body the STALE regime explicitly, bounded, at the
    # instant it installs it.
    return (min(ts, time.time() - ttl - 1), body)


def _swr_rebuild(key, fn, own_ev, snapshot=None):
    """The background arm of `_cached_swr` — same completion contract as the
    foreground path: store under the lock, then release the in-flight event so
    blocked waiters (an ANCIENT-regime reader that arrived meanwhile) wake.

    `own_ev` is THIS worker's event, passed at spawn. The finally pops the
    mapping ONLY if it still holds exactly that event: when start() raised
    after the OS worker launched, the spawner already retracted this event and
    a successor may have published its own — a blind pop(key) here would
    remove and set the successor's event while the successor's worker is still
    running (a round-two probe measured exactly that). The worker's own
    event is ALWAYS set, so anything that waited on it wakes regardless."""
    # THE WITNESS IS PROJECTED FROM THE BUILD'S OWN READ-SET, AFTER IT.
    #
    # ORDERING WAS THE OLD ANSWER AND IT WAS NEVER TOTAL. Sampling a separate
    # witness BEFORE `fn()` orders two readings of an input that moves in one
    # direction, and an input that moves and moves BACK — a force-fetch that
    # advances trunk and rewinds it — leaves the before-sample equal to the
    # after-sample while the body in between saw the middle. Holding both in one
    # scope narrowed that to whatever the scope failed to pin, and three rounds
    # found three such gaps: a success fallback, a failed peel, an unhashable
    # memo key.
    #
    # SO THERE IS NO SECOND READING TO ORDER. The snapshot served every read the
    # body made and recorded each operand, result and error; `_witness_value`
    # asks it to project that record. AFTER is not a relaxation of the old law —
    # it is what "derived from the body" means, because before the build there is
    # nothing to derive from. A write that lands mid-build is simply not in the
    # record unless the body read it, and if the body read it the witness names
    # what the body was served.
    stored = None
    try:
        with _build_scope(snapshot) as snap:
            val = fn()
            wit = _witness_value(snap)
        with _qlock:
            stored_at = time.time()
            _qstate[key] = (stored_at, val)
            _qverified.pop(key, None)  # a new body is its own confirmation
            _qcold.pop(key, None)      # a real body exists; the cold clock is moot
        stored = (stored_at, val)
    finally:
        with _qlock:
            if _qinflight.get(key) is own_ev:
                _qinflight.pop(key)
                # The reason outlives nothing: it describes the rebuild that
                # was in flight, and there is no longer one.
                _qcapkick.pop(key, None)
        own_ev.set()
    # WRITE-BEHIND MEANS *BEHIND*, INCLUDING BEHIND THE WAITERS. Taking the
    # save off the global lock was not enough: it still sat on the COMPLETION
    # path, so a blocked ANCIENT reader waited for the disk even though its
    # answer already existed in `_qstate` — measured as a 0.455s wait behind an
    # injected 0.4s save. The event is set above; nothing in this server life
    # needs the file, so it is written after every waiter has been released.
    if stored is not None:
        _persist_store(key, stored[0], stored[1], wit)



def _cached(key, ttl, fn, snapshot=None):
    """Single-flight TTL cache (ported from the predecessor): concurrent misses on one key
    share one build instead of racing."""
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
    # THE SAME ONE READ-SET AS `_swr_rebuild`, for the reason that path's own
    # comment records at length: this is the ANCIENT foreground fallthrough,
    # where a rebuild that overran its hard ttl lands, and a binding that held
    # only on the background route would leave the hazard open on whichever
    # route a slow build happens to take. Fixing one path and not the other is a
    # shape this file has already been burned by once.
    try:
        with _build_scope(snapshot) as snap:
            val = fn()  # computed outside the lock; other keys stay readable
            wit = _witness_value(snap)
        with _qlock:
            stored_at = time.time()
            _qstate[key] = (stored_at, val)
        # PERSISTENCE FOLLOWS THE WITNESS, NOT THE CODE PATH. Callers without
        # one — which is every caller of this shared cache except the land
        # projection — write nothing, and that is the point: its bodies
        # include ones carrying credential identity and home paths, and
        # persisting them all put that on disk for a feature only one endpoint
        # can use. But a WITNESSED key must not depend on which internal route
        # happened to build it: `_cached_swr` falls through to here whenever a
        # caller supplies no cold body, and a key that persisted on one path
        # and not the other would save or skip according to an argument that
        # has nothing to do with persistence.
        _persist_store(key, stored_at, val, wit)
        return val
    finally:
        with _qlock:
            _qcapkick.pop(key, None)
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
#     THE SAME LAW APPLIES BELOW THE WHOLE-BOARD LEVEL, which is where a review
#     found it broken. A read that half-succeeded (`landreq.project` reads the
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

# key -> the input identity recorded when this key last DECIDED to rebuild.
# Read only by `_cached_swr`'s STALE regime and only for a key whose caller
# supplied a `fingerprint`; a key with no entry here has never declined a
# rebuild, which is the state every key starts and every cleared cache returns
# to.
_qfresh = {}

# key -> whether the rebuild currently in flight was kicked by the CAP alone,
# with the input identity UNCHANGED. Written at the rebuild decision, dropped
# when that rebuild leaves flight, and read only by `_cached_swr`'s STALE
# regime: a cap-kicked rebuild does not make the body it is replacing stale,
# so polls taken while it runs still confirm. A key absent here has no rebuild
# in flight, or one whose inputs had moved when it was kicked.
_qcapkick = {}

# When each key's inputs were last CONFIRMED UNMOVED by a matching fingerprint,
# for a body that was built under that fingerprint. Written only by
# `_cached_swr`'s STALE regime, cleared whenever a new body is stored, read
# through `verified_at`. A key absent here has never been confirmed since its
# body was built, so its body's own timestamp is its only reading.
_qverified = {}

# key -> (that entry's timestamp, when the grant was taken), for a body
# RESTORED from a previous server life that was already older than the caller's
# `hard_ttl` when it landed. Read only by `_cached_swr`, which serves such an
# entry as STALE — dated, rebuilding behind it, real age disclosed — instead of
# routing it to the unbounded blocking build it would otherwise fall through
# to. Bound to the entry's own timestamp so the first body this life builds
# ends the grant without anyone clearing it, and read against a `hard_ttl`
# window so a rebuild that never succeeds returns to the blocking path.
_qdated = {}


def verified_at(key):
    """The instant `key`'s inputs were last confirmed unmoved for the body it
    serves now, or None when nothing has confirmed the current body since it
    was built. An endpoint that stamps `read_age_s` from this rather than from
    the body's build clock tells the owner how old the READING is — which is
    the question a freshness bound asks — while the build clock stays beside
    it as the projection's own age."""
    with _qlock:
        ent, stamp = _qstate.get(key), _qverified.get(key)
        # BOUND TO THE ENTRY IT CONFIRMED: a stamp taken over a body that has
        # since been replaced or restored says nothing about the body served
        # now, whatever its clock reads.
        if ent is None or stamp is None or stamp[0] != ent[0]:
            return None
        return stamp[1]

# Keys whose persisted body has been OFFERED to this process. Marks the
# ATTEMPT, not the adoption: a key with nothing saved must not re-read the
# disk on every EMPTY poll, and a body refused as stale must not be re-offered
# on the next one. Restoration is a once-per-life event by construction.
_qrestored = set()
del _web
