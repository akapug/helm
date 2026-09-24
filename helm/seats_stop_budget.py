#!/usr/bin/env python3
"""Measured Stop budget, successor reserves, and incomplete verdicts."""
import time

from . import projscope, seats_stop_timing

# MEASURED, NOT GUESSED (task/2069). The provisional transcript-derived fleet
# has 339 Stop traces across three Claude Code seats: p90 5.102s, p95 5.906s,
# p99 7.968s, max 10.319s. Three exact-hook samples in the earlier timing
# lineage were 8.7/8.1/5.9s. The established 2x observed-sample policy gives
# 17.4s, rounded to 17.5s. This leaves 2.5s beneath the installed, immutable
# `timeout 20` for startup outside this scope, cooperative overrun, fallback
# publication, and process exit. Evidence and its transcript-retention limit
# are preserved in tests/fixtures/stop-guard-ladder-2026-09-10.txt.
BUDGET_S = 17.5

# A fat-tail rung starts only when its fitted cost AND its successors still
# fit. OBSERVED_MAX_S includes the fresh 6.942s seam that falsified an exact
# historical-max bound. COST_MARGIN is an INFERRED engineering choice: the
# measured seam drifted 6.8% above the pinned 6.500s maximum, so 1.25x leaves
# further headroom; ADMISSION_COST_S rounds that product upward to a tenth. Any
# rung exceeding its fitted cost, or outer `timeout 20` firing before fallback,
# falsifies this fit and requires remeasurement. The local deadline remains
# ambient-reserve, so overrun yields UNKNOWN instead of consuming the tail.
#
# THE dispatch-ledger PIN WAS FALSIFIED AND THE READ PATH WAS CURED, NOT THE
# NUMBER (helm task/2394). The falsification is this module's own
# instrument rather than an argument: `~/.helm/helm/pause-ops/stopprobe.log`
# carried 175 records for this rung, p50 4.499s, p90 6.984s, p95 7.091s, max
# 7.395s — 65 of them ABOVE the 5.139s pinned maximum, 31 above the 6.5s
# admission cost, and 22 ladders whose rung ended at or past the 7.5s local
# reserve, which is the cut that publishes COVERAGE UNKNOWN. The population is
# censored at that cut, so the true tail is unknowable from it, and the fleet
# spent those stops telling every seat to confirm its lane by hand.
#
# WHY A REFIT WAS REFUSED. The cost was not a constant to be re-pinned: profiled
# on the live 14,444-event, 8,166,754-byte ledger, a cold `dispatches.snapshot()`
# cost 5.384s of which 2.362s was TWO `carried`-close `carriage_proof`
# re-derivations (18 git subprocesses, one `merge-tree --write-tree` at 1.183s
# and one `git cherry` at 0.619s) and 1.059s was the successor-frontier rescan
# (98 parents x ~2,650 rows = 259,881 liveness predicate calls). One term grows
# with the carried-close count AND with trunk's own length; the other is
# quadratic in the ledger's history. No margin survives either, so the cure is
# in WHAT THE READ DOES, never in how much of the file it reads:
# `dispatches._successor_frontier` now asks its cheap `supersedes` discriminator
# before the liveness predicate, which takes that scan from 259,881 predicate
# calls to a few hundred. THE FOLD STILL READS AND FOLDS THE WHOLE LEDGER —
# `eventledger.checked_events` returns every event and `dispatches._fold` walks
# all of them — and deliberately so: parsing the 14,444 lines measured
# 0.060-0.104s, 2% of the rung, so an offset index over the appended bytes would
# have cured 2% of this.
#
# AND THE CARRIAGE WITNESS IS NOT MEMOISED, DELIBERATELY. A durable memo of the
# ancestry/patch-identity verdict is the obvious cure for the rest of that cost,
# and it cannot be made correct: git answers about an id through a HISTORY VIEW,
# and `refs/replace`, `info/grafts` and a shallow boundary each reinterpret the
# same immutable ids without changing one of them, so a stored answer owes both a
# semantic generation refusing every entry written under a different
# interpretation and a view held immutable from the eligibility probe through the
# witness. So `dispatches.carriage_proof` derives BOTH witness families on every
# read instead, under
# a view it PINS — replacement objects off and `GIT_GRAFT_FILE` at `os.devnull`,
# the overlay `landreq._object_view` already defines — and REFUSES a shallow
# repository outright, because there the parent objects are absent rather than
# hidden.
#
# MEASURED WITH THE WITNESS STORE IN PLACE, same ledger, alternating cold
# processes against trunk: 1.162-1.768s before, 0.397-1.024s after, with the
# expensive git reads gone (18 spawns and 1.079s down to 6 spawns and 0.014s).
# THOSE TWO NUMBERS BELONG TO THE STORE, AND THE STORE IS CUT — they describe an
# arm this module no longer ships.
#
# THE SHIPPED READ'S COST, MEASURED ON THE FINAL TREE. Timed through THIS name —
# `seats_room_advice._ledger_snapshot` -> `dispatches.snapshot()`, which is what
# `seats_stop_guard` hands to `timing.measure("dispatch-ledger")` — on THE TREE
# THIS COMMENT SHIPS IN, identified by the property that decides the cost rather
# than by a sha a fresh clone cannot resolve: no witness store exists anywhere
# under `helm/` and both witness families are re-derived on every read. The exact
# tree of the run is recorded on the lane's review row. Against the live
# 14,567-event, 8,349,380-byte ledger and the real helm home, read-only, five
# cold processes, ONE call each:
#
#   load 29.7-30.7   median 3.351s (52% of the 6.5s below)  min 1.578s  max 4.452s
#
# Two carriage witnesses are derived per read, counted by spying the shipped
# `dispatches.carriage_proof` call. ALL FIVE SAMPLES FIT ADMISSION_COST_S, the
# slowest at 68% of it, on a box carrying a load average near 30. That is the
# cost of the read this module budgets for.
#
# THE THREE ROWS BELOW ARE NOT THAT MEASUREMENT, and they are kept because they
# are why the cut happened. They are the PRE-DELETION comparison — store-DISABLED
# against store-ENABLED arms on the predecessor tree, the remembering write
# replaced by a no-op, author-reported at that time against a 14,540-event,
# 8,301,262-byte ledger, five cold processes per arm interleaved so box load is
# common-mode:
#
#   load 10.4-10.8   store DISABLED 1.532s median (24% of 6.5s)  enabled 1.058s
#   load 9-15        store DISABLED 4.470s median (69%)          enabled 5.425s
#   load 27-51       store DISABLED 8.384s median (129%)         enabled 6.161s (95%)
#
# Witness time alone in those runs was 0.795s median without the store against
# 0.374s with it. So the store bought about 0.4s at LOW load; at load 9-15 the
# arm WITHOUT it was the faster of the two; and at load 27-51 the ENABLED median
# fit ADMISSION_COST_S (6.161s under 6.5s) while the DISABLED median did NOT
# (8.384s), with one enabled run producing a 12.867s TAIL sample rather than a
# failing median. The cut rests on the correctness class the store carried being
# removed, and on those comparative figures — not on a claim that the store
# never mattered, and not on them being measurements of the code that ships.
#
# THAT BOX LOAD, RATHER THAN THE MEMO, SETS THE ABSOLUTE SCALE IS AN INFERENCE
# drawn from interleaved observations, never a controlled isolation of load: on
# the same instrument 7.677s at load 24-60 against 1.099s at load ~10 with the
# memo disabled, plus the three rows above, where the ordering by load survives
# the store being switched either way. Read it as the reason a constant pinned
# from one box is untrustworthy on the next, which is why the pin below waits for
# a production population.
#
# THE THREE CONSTANTS BELOW ARE DELIBERATELY UNCHANGED, and that is a posture,
# not an oversight. Lowering them re-derives ADMISSION_COST_S downward, which
# only ADMITS the rung more often — the direction this defect wants — but the
# honest input for a new pin is a post-cure PRODUCTION population, and the
# instrument that would supply it writes only above its 3.0s slow threshold.
# Re-pin from the stop-ladder rung of `helm doctor` — which is what reads
# `stopprobe.log` (see helm/stopprobe.py `read` and `findings`) — once this cure
# has been running on the fleet, not from this box.
#
# THE `wiring` PIN, AND WHY THE RUNG HAD NONE UNTIL IT CAUSED AN OUTAGE. This
# rung walked the package's import graph under NO fitted cost and NO reserve,
# so nothing on this ladder had an opinion about what it cost, and the only
# figure anyone could have consulted was a sentence in the rung's own
# docstring. That sentence said ~1s, measured against a package a third of the
# present size, and it was twelve times under the truth by the time it
# disabled seven rungs in one stop. A NUMBER IN PROSE CANNOT NOTICE IT HAS
# EXPIRED; a number in this table is read back against the ladder's own log on
# every `helm doctor` run (`stopprobe.overruns`, one-directional: it reports a
# pin the log has FALSIFIED and never certifies one).
#
# WHAT IS PINNED IS THE RUNG'S COST AFTER THE CURE, not the census's. The rung
# asked `wiring.census` for four rungs of answers and read one of them; it now
# asks `wiring.unreachable_modules` for that one. Measured on the real package
# (330 modules), five cold processes per arm, arms INTERLEAVED so box load is
# common-mode, load average 9.6-13.1 throughout:
#
#   census(with_events=False)["unreachable"]   median 30.340s   max 64.630s
#   unreachable_modules()                      median  2.873s   max  4.615s
#
# The discarded 89% is `tested` and `facade_reached`, neither of which can
# change a reachability verdict; the profile is in `wiring.unreachable_modules`.
# The 64.630s sample is a load spike on a shared box and is kept rather than
# dropped — it is what the rung's OLD tail actually looked like next to a 17.5s
# whole-ladder budget.
#
# CORROBORATED AT THE RUNG RATHER THAN THE WALK, because the pin gates
# `wiring.gate_lines` and not the walk inside it. Eight cold processes through
# the whole rung — the two git reads for the added-set, both source-graph
# fingerprints, the walk, the memo write — at load 8.5-8.9: 2.264s to 2.882s,
# median 2.66s. The added-set costs 0.006-0.008s and a fingerprint 0.030-0.051s,
# so the rung IS the walk to within 2% and the two populations measure one
# thing. The pin stays fitted to the 4.615s maximum, which came from the busier
# box.
#
# AND SAMPLES TAKEN WHILE A TEST SUITE RAN ON THE SAME BOX ARE EXCLUDED, which
# is a statement about the instrument and not a convenience. Six rung samples
# read 4.839s-9.087s at load 11.35-16.85, and that load was a suite THIS cure
# was running — a control that perturbs its subject is not a control, and a pin
# fitted to it would describe a box with a build on it rather than a box with
# seats on it. They are named here because they do carry one true finding: this
# rung's cost moves by about 3x with box load, so NO pin fits every hour of a
# shared host's day.
#
# WHICH IS WHY THE PIN IS NO LONGER THE ONLY DEFENCE, and the two mechanisms
# should not be confused. The pin decides whether the rung STARTS. The
# cooperative checkpoint decides that a rung which started cannot run past its
# slice. Before the checkpoint an under-pin was unbounded — the rung ran to
# completion and every rung behind it was lost. With it, an under-pin costs at
# most the difference between the rung's local deadline and the moment the pin
# expected it to finish, and the successors still run. That is the difference
# between a budget that is enforced and one that is merely forecast.
#
# AND THE COST IS NOW BOUNDED RATHER THAN FORECAST, which is the part a pin
# cannot do by itself. `wiring.graph` yields to the ambient deadline once per
# module, so this rung stops at its local reserve instead of running past it —
# the pin decides whether the rung STARTS, the checkpoint decides that a rung
# which started cannot overrun. Every other fat tail here is still admission
# only, and a stale pin on one of those is still able to spend its successors'
# time; that is the next rung to make interruptible, not a property this
# module already has.
MEASURED_RUNS = 339
COST_MARGIN = 1.25
OBSERVED_MAX_S = {"dispatch-ledger": 5.139, "claims": 5.250, "seam": 6.942,
                  "wiring": 4.615}
ADMISSION_COST_S = {"dispatch-ledger": 6.5, "claims": 6.6, "seam": 8.7,
                    "wiring": 5.8}
RESERVE_S = {"dispatch-ledger": 10.0, "claims": 8.0, "seam": 2.0}

# ORDER IS THE CHEAPEST HALF OF THIS BUDGET, AND IT WAS THE HALF NOBODY SET.
#
# THE MEASURED OUTAGE. `wiring` ran FIFTH of thirteen with no reserve of its
# own, so a cold run of it spent the whole ambient budget and the ladder
# published `spiral=UNFINISHED, seam=UNREACHED, ndp=UNREACHED, punt=UNREACHED,
# whisper=UNREACHED, claim-evidence=UNREACHED, mechanical=UNREACHED,
# response=UNREACHED` — eight rungs, seven of which never ran — and PASSED the
# stop on that line. 26.9s end to end, 26.4s of it in `wiring`. Failing open is
# correct for a Stop hook and is exactly what made the outage invisible: the
# seat is told to carry on, so nothing about the turn looks wrong.
#
# THE RUNGS ARE ORDERED BY HOW PERISHABLE THEIR EVIDENCE IS, not by how
# expensive they are, and for these thirteen the two orders nearly coincide.
# Every rung from `spiral` to `response` reads state THIS TURN produced and has
# exactly one moment to fire: a verdict that landed mid-turn, a composition
# merged with no arm run against it, a load held all session with no
# delegation, a declination wearing a reason, a settled-sounding claim the turn
# made no measurement to earn. Miss one and the finding is gone — the next stop
# asks about a different turn.
#
# `wiring` IS THE OPPOSITE OF PERISHABLE, which is why it now runs LAST among
# the rungs that can block. A module nothing can reach is still unreachable at
# the next stop, and the one after; the finding waits, it does not expire. It
# is also the only rung on this ladder whose subject is the SHARED CHECKOUT
# rather than the acting seat's turn — it cannot attribute the debt to the
# session that stopped (see `seats_stop_guard`, the latch that exists because
# of it) — so deferring it costs the fleet one stop's notice of a standing
# condition, while running it early cost the fleet every perishable rung behind
# it. The three rungs still after it are the cheap ones: a WARN about claims
# with no measurement, a silent index/scratch leg, and the response publisher.
RUNGS = ("identity", "inbox", "claims", "beacon", "spiral", "seam",
         "ndp", "punt", "whisper", "wiring", "claim-evidence", "mechanical",
         "response")

# WHAT AN UNPINNED RUNG COSTS, AND WHY THE TABLE MAY NOT STAY AN ALLOWLIST.
#
# `RESERVE_S` named three rungs, and `rung_deadline` answered None for the
# other ten — which does not mean "no reserve", it means NO LOCAL DEADLINE AT
# ALL: an unlisted rung ran against the ambient budget and could therefore
# spend every successor's share of it. The three-entry table was not a
# conservative subset of a general rule, it WAS the rule, and a rung outside it
# had the whole ladder to spend. So the reserve is now total: pinned where a
# fat tail has been fitted, derived from the ladder's own shape everywhere
# else.
#
# 0.3s PER ORDINARY RUNG, and the derivation is thin on purpose. The three
# exact-hook samples that fitted `seam` put every other rung on the same ladder
# at about 2.1s in total across ten rungs — a mean over a sum, not a per-rung
# distribution, so it is a floor to reason from and not a fit. ERRING SMALL IS
# THE SAFE DIRECTION HERE and it is worth saying which way: a default that is
# too small leaves a predecessor reserving too little, which is what the code
# did already with zero; a default that is too LARGE refuses an early rung
# admission and publishes COVERAGE UNKNOWN on `identity` — a new failure, in
# the rung that can least afford one.
DEFAULT_COST_S = 0.3


def cost(stage):
    """Seconds `stage` is fitted to take — pinned, or the ordinary rung."""
    return ADMISSION_COST_S.get(stage, DEFAULT_COST_S)


def reserve(stage):
    """Seconds the rungs AFTER `stage` are owed, pinned or derived.

    THE DERIVED ARM SUMS ORDINARY COSTS AND NOT FITTED ONES, which looks like
    an omission and is the point. Summing every successor's fitted cost totals
    more than `BUDGET_S` before the ladder has run a single rung, so `identity`
    would be refused admission at its own start — a reserve large enough to
    forbid the ladder is not a reserve. A fat tail's worst case is already
    guarded ONCE, by its own admission check, and charging it again to every
    predecessor double-counts a worst case that cannot happen twice. What a
    predecessor owes is the ORDINARY cost of finishing the ladder; a successor
    that then does not fit yields itself, which is the designed degradation.

    Unknown to the ladder -> None -> no local deadline, unchanged: a stage
    nobody put in `RUNGS` has no successors this module can name, and
    inventing a bound for it would be a number about nothing."""
    pinned = RESERVE_S.get(stage)
    if pinned is not None:
        return pinned
    try:
        i = RUNGS.index(stage)
    except ValueError:
        return None
    return round(DEFAULT_COST_S * len(RUNGS[i + 1:]), 6)


# UNKNOWN NEVER REFUSES A STOP, AND THAT IS THE WHOLE OF THIS LINE.
#
# A REFUSAL BUILT ON AN ABSENCE CANNOT BE DISCHARGED BY ANYTHING THE SEAT
# DOES. File a COVERAGE UNKNOWN in `blocks` and the stop exits 2; the next stop
# re-measures the same rung on the same board, overruns again, and refuses
# again, so the guard spends a seat's turns on its own inability to finish.
# Measured, three samples on one box against a fitted 8.7s: 8.309s, 11.4s and
# 12.2s, while every other rung on the same ladder totalled about 2.1s. So the
# cost STRADDLES the admission bound rather than sitting above it — which is
# the distribution that makes this refusal feel intermittent to a reader and
# permanent to a seat on a busy board, and it refused one seat's stops in a
# row with no finding in any of them. Ruling task/2300: "a rung that cannot finish inside
# its budget must yield UNKNOWN (correct) and UNKNOWN must not refuse a stop
# forever (the defect)".
#
# THE DISTINCTION IS BETWEEN A FINDING AND AN ABSENCE OF LOOKING. Every other
# block on this ladder is a MEASURED obligation — a held lease, an unread row,
# an untested composition — and those still refuse, from this same `blocks`
# list, on their own evidence. This line is the opposite: it reports what the
# guard did not manage to examine. "Missing evidence is not evidence against"
# is the rule the rest of helm already follows, and a refusal built on an
# absence is the one kind that cannot be argued with, because there is nothing
# to answer.
#
# IT IS STILL LOUD, ON EVERY STOP THE GUARD DOES NOT OTHERWISE REFUSE. One
# warn line naming the rung and what went unexamined — the lease sermon's
# degradation, which compresses to one line rather than going quiet. On a stop
# that IS refused for a measured reason the warn channel is suppressed by
# design (`publish`), and that is the right trade: the seat is already being
# told to keep working, and a coverage note is not the actionable half. The
# ambient-expiry path prints both, because there nothing else explains why the
# ladder stopped early.
PASSES = ("This Stop PASSES on this line: an unexamined rung is UNKNOWN, "
          "which is not evidence of work — and a rung whose measured cost no "
          "longer fits reports the same UNKNOWN at every stop, so refusing "
          "here refuses forever. Blocks from rungs that DID finish still "
          "refuse on their own evidence.")


def yielded(stage):
    """Render one local yield without claiming the later ladder was skipped."""
    return ("[helm stop-guard] COVERAGE UNKNOWN — measured rung cost plus "
            "successor reserve no longer fit: %s=UNFINISHED, "
            "later-rungs=CONTINUED. %s"
            % (stage, PASSES))


def unknown(stage):
    """Render one honest incomplete-coverage verdict."""
    stage = "inbox" if stage == "continuing-inbox" else stage
    try:
        i = RUNGS.index(stage)
    except ValueError:
        states = ["active-rung=UNKNOWN", "later-rungs=UNKNOWN"]
    else:
        states = [stage + "=UNFINISHED"]
        states.extend(name + "=UNREACHED" for name in RUNGS[i + 1:])
    return ("[helm stop-guard] COVERAGE UNKNOWN — ambient budget expired: "
            "%s. Earlier completed blockers remain valid. %s"
            % (", ".join(states), PASSES))


class _RungScope:
    """Tighten to one successor reserve and suppress only that local expiry."""

    def __init__(self, state, stage):
        self.state = state
        self.stage = stage
        self.complete = True
        self.current = None
        self.ambient = None
        self.local = None
        self.scope = None

    def __enter__(self):
        self.current = projscope.deadline()
        self.ambient = self.state.bind_deadline(self.current)
        self.local = self.state.rung_deadline(self.stage)
        self.scope = projscope.scope(deadline=self.local)
        self.scope.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        self.scope.__exit__(kind, value, traceback)
        if kind is None or not issubclass(kind, projscope.Expired):
            return False
        # The immutable outer budget still has time: this is the local reserve,
        # not whole-ladder expiry. Preserve UNKNOWN and resume at the next rung.
        now = time.monotonic()
        if self.ambient is not None and now < self.ambient \
                and self.local is not None and now >= self.local \
                and (self.current is None or self.local <= self.current):
            self.complete = False
            self.state.yield_rung(self.stage)
            return True
        return False


class State:
    """Mutable partial result retained across local and ambient expiry."""

    def __init__(self):
        self.blocks = []
        self.warns = []
        self.expired = False
        self.yielded = set()
        self.deadline = None
        self.timing = seats_stop_timing.RungTiming()

    def bind_deadline(self, current=None):
        if self.deadline is None:
            self.deadline = projscope.deadline() if current is None else current
        return self.deadline

    def rung_deadline(self, stage):
        ambient = self.bind_deadline()
        owed = reserve(stage)
        return None if ambient is None or owed is None else ambient - owed

    def start_deadline(self, stage):
        deadline = self.rung_deadline(stage)
        return None if deadline is None else deadline - cost(stage)

    def admit(self, stage):
        """Start only while measured rung cost plus successors still fit."""
        deadline = self.start_deadline(stage)
        if deadline is not None and time.monotonic() > deadline:
            self.yield_rung(stage)
            return False
        return True

    def rung(self, stage):
        return _RungScope(self, stage)

    def run(self, stage, fn, fallback=None):
        """Run one whole cooperative rung under its successor reserve."""
        if not self.admit(stage):
            return fallback
        with self.rung(stage) as rung:
            projscope.spend_or_raise("%s admission" % stage)
            answer = fn()
            projscope.spend_or_raise("%s result" % stage)
        return answer if rung.complete else fallback

    def yield_rung(self, stage):
        # THE WARN CHANNEL, UNCONDITIONALLY. This chose between `warns` and
        # `blocks` on `stop_active`, which made the harness's own state decide
        # whether an ABSENCE refused a stop; the absence is the same fact
        # either way. See the note above PASSES for the measurement.
        if stage not in self.yielded:
            self.warns.append(yielded(stage))
            self.yielded.add(stage)

    def expire(self):
        if not self.expired:
            self.warns.append(unknown(self.timing.current))
            self.expired = True
        return self.blocks, self.warns
