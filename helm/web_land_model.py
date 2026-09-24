"""Land-pipeline source model for :mod:`helm.web`."""
import contextlib
import errno
import json
# EXPLICITLY, THOUGH THE FACADE SPLAT BELOW ALSO SUPPLIES IT. Every `os.` in
# this module — `_read_stat`, `_open_once`, `_read_repo` — was resolving through
# `vars(helm.web)`, so importing `helm.web_land_model` without `helm.web` having
# been imported first left `os` undefined and every read answered NameError.
# Measured that way while driving the two sealed doors from a bare script; the
# suite never saw it because it imports the facade first.
import os
import re
import sys
import threading

from . import openflags
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import calendar
import hashlib
import time

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})

# THE MEASURED COST OF ONE `_lr_build`, and the number every constant below is
# derived from. Four unprofiled runs of `_lr_build` under `_lr_snapshot`,
# against the live 4622-row dispatch ledger on the owner's box at load ~30:
# 27.3s, 29.5s, 33.5s, 38.7s. Call the fill 30s.
#
# WHERE IT GOES, because a constant argued against an unlocated cost is argued
# against a guess. `filed_split` is 17.2s of a 29.5s fill and `project_raw` is
# the other 11.9s; everything else in the body — the loop rows, the stalls, the
# lands card, 1388 `card()` calls — sums to under 0.6s. Inside `filed_split`
# the whole bill is `off_frontier_closable`, the close ladder asked in DRY RUN
# of every off-frontier row so the strip can print how many the verb would
# take: 227 rows, 876 git spawns, of which 131 are `merge-tree --write-tree` at
# about 97ms each = 12.7s. That census answered `closable: 1`,
# `not_closable: 226`.
#
# AND THE CONSTANTS BELOW ARE THE DOWNSTREAM HALF, NOT THE CURE. They bound how
# OFTEN that census runs; they do not make it cheaper. The cure
# `off_frontier_closable`'s own docstring prescribes is to "remove the QUESTION
# for rows that cannot be closed anyway", and the refusals were censused for
# it: 111 of the 226 spend the whole merge-tree on the SAME sentence ("no
# content witness could see base..tip") while the other 115 refuse in under a
# second on rungs that never reach a replay. No cheap discriminator separates
# the 111 — a replay can affirm where the postimage witness cannot see, which
# is why `rowworld._carriage` asks both — and a memo of the answer is refused
# on its own measurement (see the note above `_carriage_trunk_sha`). What is
# left is not asking the door from this surface at all, which removes 17.2s of
# a 29.5s fill.
#
# AND THE OBVIOUS SPELLING OF THAT IS A TRAP, WHICH IS WHY IT IS WRITTEN DOWN
# RATHER THAN LEFT AS "just drop `world=`". Dropping it from `filed_split`'s
# `frontier_verdicts` call does stop the census — measured, zero calls to
# `off_frontier_closable` — but `filed_split` still WRITES both keys, and with
# no verdict to read they come out `closable: 0, not_closable: 227`. That is a
# confident zero over a population nobody measured, and it is the precise claim
# the renderer's own comment forbids inventing ("a zero invented here would say
# 'nothing can be cleared' about a board nobody measured"). The card would read
# "227 placed but NOT closable yet" over a door nobody asked. Its UNKNOWN
# branch is reached through `fnum`, which answers null for anything that is not
# a number — so the pair must be ABSENT or None, and zero will not do it. The
# cure is therefore two edits, not one: skip the door AND drop the pair, the
# same law `filed` and `withheld` already follow one field over — None on a
# body that did not read the population, never a zero.
#
# IT CHANGES A LINE THE OWNER READS — the strip would say "helm lr retire
# --off-frontier censuses them" instead of naming a number — so it is his call
# and not this file's, and it is recorded here rather than done.
#
# AND THE COST IS PERMANENT AND GROWS, which is what makes it worth his
# attention rather than a footnote. The 226 rows the ladder refuses are refused
# BECAUSE no witness can affirm them, so no verb clears them and they sit in
# `off_frontier` indefinitely — every rebuild re-derives the same refusal for
# the same rows, and the population only grows as more lanes are reaped. The
# census is not amortised by anything.
_LR_MEASURED_FILL_S = 30


# How long /api/lr blocks a COLD reader before answering "warming".
#
# 3, NOT 8, AND THE MEASUREMENT SAYS THE WAIT CANNOT DO ITS JOB HERE. The wait
# exists so a build that finishes quickly still answers with REAL DATA — see
# `_cached_swr`, where answering the placeholder immediately took a suite from
# green to 51 errors. That property is about builds measured in milliseconds
# (every fixture ledger in the suite) and it survives at 3 with room to spare.
# What it can never cover is THIS build: the fill is `_LR_MEASURED_FILL_S`, so
# no value a phone can wait behind — the card aborts its own fetch at 12s —
# will ever catch it. The first view after any restart is therefore GUARANTEED
# to answer `{"warming": true}`, and the only question left is how long the
# owner stares at nothing before being told so. It was 8 seconds. It is 3.
#
# RAISING IT ABOVE THE FILL IS THE OTHER DIRECTION AND IT IS WORSE: the card
# would abort at 12s and render "DISPATCH LEDGER UNREADABLE" over a ledger that
# is merely uncomputed, which is the exact sentence `cold_body` exists to
# prevent.
_LR_COLD_WAIT_S = 3


# THE FLOOR, AND IT MAY NOT SIT BELOW THE FILL. A body is stamped when its
# build COMPLETES (`_swr_rebuild`), so over a ledger that keeps moving the
# rebuilder's duty cycle is fill / (floor + fill). At 30 that was 30/60 — half
# a core held forever, on a box every seat shares, whether or not anyone has
# the card open. A floor UNDER the fill is the degenerate case of the same
# arithmetic: the body is due again before its successor can exist, so the
# floor bounds nothing and the only thing still spacing the rebuilds is how
# long they take.
#
# 90 IS THREE TIMES THE FILL: duty 30/120 = 25%.
#
# AND THE CEILING IS THE RENDERER'S, CHECKED AGAINST THE RENDERER RATHER THAN
# AGAINST A COMMENT ABOUT IT. The pipeline band calls `cardStale(read_age_s,
# 45)` and `cardBoundS` is `cadence * 4`, so the card REFUSES TO PRINT ITS
# TOTALS past a 180s reading — "STALE — not rendered", by the owner's own
# ruling, because a headline he pastes may not outlive its read. The worst age
# this floor can produce is floor + fill: the body is served until it reaches
# the floor, the rebuild is kicked there, and the previous body stands for one
# more fill while it runs. 90 + 30 = 120s, and 90 + the slowest fill measured
# on a loaded box (38.7s) is 129s — both inside 180 with room. The floor may
# not be raised past 140 without moving that bound too, and a fill that grows
# past 90s takes the headroom with it: this constant and the card's cadence are
# one budget, not two.
#
# So the freshness bought back by the three quarters of a core is age the
# header already discloses, never a band going UNKNOWN.
#
# THE FINGERPRINT IS NOT A SUBSTITUTE FOR THIS; THE TWO BOUND DIFFERENT THINGS,
# AND WHICH ONE IS BINDING DEPENDS ON THE FLEET. `_cached_swr`'s fingerprint
# declines the rebuild when no input MOVED, and then `_LR_UNCHANGED_MAX_S` sets
# the rate; the floor is what spaces the rebuilds when inputs DO move.
#
# BOTH REGIMES WERE MEASURED, AND THE QUIET ONE IS THE ONE THAT SURPRISED ME.
# Sampling `_lr_fingerprint()` twice a second for 120s over the five ledgers it
# covers: 240 samples, ZERO changes. On that fleet the gate is closed, the cap
# is what fires, and this constant changes nothing at all — the rate is
# fill / `_LR_UNCHANGED_MAX_S` either way. The floor binds only when the
# ledgers move at least once per floor, and that is exactly the busy fleet the
# duty cycle above is about. So this raise is free in the regime that was
# measured and halves the cost in the regime that was not — but the 25% is a
# WORST CASE, not an observed one, and nobody should quote it as a reading.
_LR_FLOOR_S = 90

# The truth cap for serve-stale-while-revalidating: a body may be served
# STALE only while younger than this and only with a rebuild in flight.
#
# 600, NOT the old 120, because the old cap sat BELOW the rebuild it waits
# on: MEASURED (sequential runs on the same box, interleaved
# samples pending) the projection behind `helm lr list --all` ran 249.20s on
# trunk / 267.75s on the lane at 4.5% CPU — spawn-latency class, not compute
# — against a 120s cap. A hard cap below the build time guarantees a
# blocking window EVERY cycle, so the owner's card read "pipeline UNKNOWN"
# on a healthy system, by construction. 600 is 2.4x the measured ~250s
# rebuild: the block phase is extinct while the rebuild path stays live —
# STALE still kicks one background build per hit and truth still arrives at
# most one build late.
#
# The honesty cost is already paid: read_age_s is stamped on every body at
# response time and the card renders the age, so the owner sees a true
# "Nm old" instead of a false UNKNOWN. What this does NOT cure, correctly:
# a cold start still waits (cold_wait) and an unreadable ledger still reads
# UNKNOWN — the cap moves only the boundary where a KNOWN past may stand in
# for a rebuild in flight.
#
# This is the STOPGAP half of the outcome-A ruling; the structural half is the
# monotonic ancestry cache (council-sanctioned).
#
# THE CONDITION THAT NOTE SET HAS SINCE BEEN MET AND THE ANSWER IS STILL 600,
# which is worth saying because the note read as an instruction to lower it.
# It said "when the projection drops under ~60s, revisit this constant DOWN",
# and it has: `_LR_MEASURED_FILL_S` is 30. But this cap is no longer sized
# against the rebuild at all — `_LR_UNCHANGED_MAX_S` is what sets its floor.
# A fingerprint match may hold a body only while the entry is younger than that
# cap, and the cap must stay BELOW this one or a match could carry an entry
# into the ANCIENT regime, where the next poll blocks on the whole projection
# (see `_cached_swr`, and the ordering test that pins it). So the pair moves
# together or not at all, and nothing here argues for moving the pair.
_LR_HARD_TTL_S = 600

_LR_CLOSED_WINDOW_S = 86400  # "closed in the last 24h"

_LR_CLOSED_CAP = 12          # the phone shows a footer, not a history

_LR_LANDS_CAP = 6            # dashboard rows: above the fold on a phone, not a history

# THE TASK NUMBER THE INTEGRATOR WRITES INTO A LAND'S CLOSE EVIDENCE, the one
# identity on a landed row the owner recognises by sight. Three spellings are
# live in this ledger (`task/2362`, `task 2362`, `task2362`) and nothing else
# is accepted; three to five digits, because a bare year or a sha fragment is
# not a task number. Measured over the landed closes on this estate: it names
# a task on the rows whose closer wrote one and on no others, which is why the
# absence renders as NO TASK RECORDED rather than as a blank.
#
# BOUNDED AT BOTH ENDS. A width bound with no trailing boundary is not a width
# bound at all: `task/123456` matched the first five digits and rendered
# `task/12345`, which is a DIFFERENT task that exists, so an identifier outside
# the supported width became a valid-looking one rather than an absence. The
# trailing guard makes an unsupported width answer None, which the card already
# renders as NO TASK RECORDED — an honest absence beats a confident prefix.
_LR_LAND_TASK = re.compile(r"task[\s/#]*([0-9]{3,5})(?![0-9])", re.I)


# ── ONE READ-SET, TWO PRODUCTS ───────────────────────────────────────────
# THE GENERATING MECHANISM, named after three cures each closed one door and a
# fourth appeared (task/757):
#
#   1. SUCCESS  — the witness pinned trunk and the body re-resolved the moving
#                 ref. Cured by handing the body the witness's pin.
#   2. FAILURE  — `_resolved_ref(...) or trunk` fell back, so a FAILED pin let
#                 the body take the moving name while the witness said
#                 UNRESOLVABLE. Two equal UNRESOLVABLE witnesses compared FRESH.
#   3. UNHASHABLE — `projscope` deliberately skips memoisation for an unhashable
#                 key, so with `repo_id: []` the witness's tier read answered
#                 UNKNOWN while the body's re-read answered OK inside the SAME
#                 pin. Two uncached calls, an equal UNKNOWN witness, and a READY
#                 body restored FRESH.
#
# Every one of those is the SAME shape: the witness was COMPUTED ALONGSIDE the
# body rather than DERIVED FROM it. Two readings compared for equality can never
# be made total — they can only be patched per path, and each patch narrows the
# gap without closing it. Success fallback, failed peel, unhashable memo key and
# scope boundaries are four ways for two readings to differ, and the list has no
# end because it is a property of there BEING two readings.
#
# SO THERE IS ONE READING. The projection consumes every moving input through
# ONE read-set that records each actual operand, result and error it served. The
# BODY is built from that object. The WITNESS is a PURE DETERMINISTIC PROJECTION
# of the SAME record, taken AFTER the build. A success fallback, a failed peel,
# an unhashable key and a scope boundary cannot make the two disagree, because
# there is no second build-time read for them to disagree about.
#
# WHAT THIS DOES NOT COVER, stated because a refactor that quietly widens its
# own claim is the next defect: collecting many inputs is NOT an atomic
# repository transaction. Read 1 and read 40 happen at different instants, so a
# world that changes between them leaves a record no single instant produced.
# That is a DIFFERENT class from the same-input double-read A-B-A this closes,
# and nothing here should be read as closing it — see `recheck`, which compares
# a whole record and therefore reports such a world as CHANGED (the safe
# direction) rather than reconciling it.
#
# PER-THREAD because `_swr_rebuild` runs the rebuild on a background worker and
# helm web is a ThreadingHTTPServer: a module-level read-set would let one
# build's record answer another build's reads.
_LR_READS = threading.local()


class _Blind:
    """A read that HAPPENED and IDENTIFIES NOTHING.

    The value is still SERVED — the surface must render, and a body assembled
    from a failed instrument is still the honest answer to this request. What it
    may never do is enter the witness, because an instrument failure says
    nothing about whether the input moved, and two of them comparing equal is
    exactly the A-B-A of rounds two and three wearing the name of a safe
    default.

    ABSENT IS NOT BLIND, DELIBERATELY. "this ledger does not exist" and "this
    repository has no upstream trunk" are MEASURED facts about the world that a
    later reading can contradict. UNREADABLE, unresolvable and an enumeration
    that raised are failures of the INSTRUMENT."""

    __slots__ = ("value", "why")

    def __init__(self, value, why):
        self.value, self.why = value, why


class _Sealed:
    """A read IDENTIFIED BY THE ONE OPEN THAT SERVED IT, not by its value.

    THE DOORS THEMSELVES HAD THE TOCTOU THEY WERE BUILT TO CLOSE. `premise_chain`
    recorded the operand `_lr_input_path("premise")` resolved and then called
    `premise.chain_records()`, which resolves the chain path AGAIN and opens
    whatever it names at THAT instant; `receipt_rows` recorded
    `landreq.receipts_path()` and then called `landreq._receipt_rows()`, which
    calls `receipts_path()` again. A resolver that moves between the record and
    the reopen — a `HELM_HOME` rebind, a home swap, a project re-resolution —
    leaves the record naming file A while the body consumed file B. That is the
    ORIGINAL same-input divergence, alive INSIDE the door built against it
    (exact-tip round five).

    SO THE IDENTITY COMES FROM THE HANDLE. `_LrReadSet._open_once` opens the
    path ONCE, `fstat`s THAT FILE DESCRIPTION and reads THOSE BYTES; the stamp
    it returns is [path, dev, ino, blake2b(content)] — which name, which file
    that name reached at the moment of the open, and what that open contained,
    all from one descriptor with no reopen able to intervene. The parse is a
    pure function of the bytes (`eventledger.checked_rows`,
    `premise._chain.records_from` — not re-exported from the package, because a
    parse over bytes is not a reader anyone outside a door should reach for),
    which is why the split exists at all.

    A SEAL NEVER RESCUES A FAILED READ. `_Blind` still answers the unreadable
    and the corrupt: a stamp identifies the FILE, and a file helm could not
    parse is not a world this snapshot may certify. The two wrappers are
    disjoint on purpose — one says "identify this by what I opened", the other
    says "identify this not at all"."""

    __slots__ = ("value", "stamp")

    def __init__(self, value, stamp):
        self.value, self.stamp = value, stamp


def _lr_unwrap(value):
    """(value, why, stamp) for the three shapes a reader may answer with.

    ONE PLACE, because `_serve` and `_observe` both unwrap and a caller that has
    to remember which recorder handles which wrapper is a caller that will
    eventually record a wrapper OBJECT as an identity. That already happened
    once with `_Blind`."""
    if isinstance(value, _Blind):
        return value.value, value.why, None
    if isinstance(value, _Sealed):
        return value.value, None, value.stamp
    return value, None, None


def _lr_operands(operands):
    """The operand tuple as a STABLE SERIALIZABLE IDENTITY, or None.

    A repository binding, a ref name, a ledger path, a reviewer: every operand
    this read-set serves NAMES something, and a name is a string (or the
    absence of one). `repo_id` is copied verbatim out of the ledger row and
    never re-validated, so `["not", "a", "path"]` arrives here as itself — and
    it is not the name of a repository today, tomorrow or in the saved record
    of a previous server life. It has NO identity, so the read that consumed it
    cannot be identified, so the snapshot that served it cannot be persisted.

    THAT IS THE THIRD ROUND'S DOOR, CLOSED AT THE OPERAND RATHER THAN AT THE
    MEMO. `projscope` degrades an unhashable key to computing — correctly, since
    caching under a colliding key is worse — and the previous cure inherited
    that as "the witness and the body each compute, and their answers may
    differ". Here the answer is not compared at all: a read whose operand has no
    identity makes the whole record NON-PERSISTABLE, which is the one outcome
    that cannot be wrong in either direction."""
    for value in operands:
        if not (value is None or isinstance(value, str)):
            return None
    return json.dumps(list(operands))


# HOW LONG AN IDENTITY MAY BE BEFORE IT COLLAPSES TO A DIGEST. Every term's
# identity is stored INLINE in the witness and the witness is persisted and
# string-compared, so a read whose VALUE is a whole file — the premise chain,
# the receipt index — would put that file into the fingerprint, twice over
# (saved and current) on every restore. Above this bound the identity becomes a
# digest OF THE SAME CANONICAL BYTES, which identifies exactly what the
# serialization identified. Below it the reading stays legible, which is worth
# keeping: `refsha` naming its commit and `tier` naming its verdict are read
# directly out of a witness during diagnosis. Every non-content reader in this
# class is far under it (measured: the largest is `ledgers` at ~120 chars).
_LR_IDENT_INLINE = 512


def _lr_identity(value):
    """`value` as a canonical serialization, or None when it has none.

    A LONG ONE COLLAPSES TO A DIGEST OF ITSELF. See `_LR_IDENT_INLINE`: this is
    a fingerprint, so bounding its length costs nothing it was providing —
    blake2b over the canonical bytes changes when they change, which is the
    whole contract. The prefix keeps the two forms from ever being confused for
    one another."""
    import hashlib
    try:
        canon = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(canon) <= _LR_IDENT_INLINE:
        return canon
    return "blake2b:" + hashlib.blake2b(canon.encode("utf-8"),
                                        digest_size=16).hexdigest()


# PROJECTION-CACHE KEYS THAT ARE DERIVED, NOT INPUTS. `landreq` memoises three
# things into the dict a caller hands `project_raw`, and only two of them name a
# MUTABLE binding. `base_behind` is keyed on (gitdir, tip, RESOLVED SHA) — two
# immutable object names and a commit this record already carries under
# `refsha` — so it cannot move without one of its own operands moving first, and
# fingerprinting it would only restate a term already present.
#
# THE DEFAULT IS THE OTHER WAY, and that is the point: a cache key this read-set
# does NOT recognise makes the record blind rather than being silently skipped.
# A future memo added to `landreq` is a new read, and a new read is either an
# input identity or a derivation of one; until someone says which, the honest
# answer is that this snapshot cannot describe itself.
_LR_DERIVED_CACHE_KEYS = frozenset(("base_behind", "objexist-pending",
                                    "patchid-pending"))
# `patchid-pending` EARNS THE EXEMPTION ON THE SAME GROUND AS ITS SIBLING AND
# ONE MORE. It is built from ROW DICTS — the tips of rows this projection will
# actually ask about — narrowed by the durable landing-proof ledger, which the
# body already reads per row through `_landing_proof` and which is therefore no
# new class of input. And unlike `objexist-pending` it has no live twin here at
# all: the batch it arms is bought into `projscope` and never written back into
# this cache, so there is no git result under any key of this record to
# classify. A tip left out of the batch falls through to its own per-row spawn
# and answers identically, so the narrowing cannot move a value in the body.
# `objexist-pending` EARNS THE EXEMPTION THAT `objexist` DOES NOT, and the two
# are split here rather than treated as one family. `_prefetch_object_existence`
# builds the pending list from ROW DICTS ALONE — the tips of rows this
# projection will actually ask about — and reads nothing outside the record. Its
# operands are therefore derivations of the dispatch ledger, whose `stat` is
# already a term. The LIVE key beside it (`("objexist", gitdir)`) is a git read
# of a MOVING repository and is classified as a term in `__setitem__`; see the
# argument there. CURING ONLY THIS HALF IS THE TRAP — it removes a blind REASON
# while leaving the real one, so the record reads fixed and is not.


class _LrReadSet(dict):
    """THE ONE DOOR between the land projection and every input that MOVES.

    IT IS A `dict` BECAUSE IT IS ALREADY THE PROJECTION CACHE. `landreq` threads
    a per-projection dict through `project_raw`, `_trunk_refs`, `_resolved_ref`
    and `_landing_proofs`, memoising `(gitdir) -> (local ref, upstream ref)` and
    `("refsha", gitdir, ref) -> commit` into it. Subclassing means the body's
    OWN reads land in the record as it makes them, with no parallel enumeration
    that could describe a different set — the witness observes the build rather
    than re-performing it.

    EVERY IDENTIFIED READ IS SERVED ONCE. `_serve` memoises on (op, operand
    identity), so whoever asks first performs the read and every later asker —
    the body, an annotator, the projection of the witness — is answered from the
    record.

    AN UNIDENTIFIED OPERAND IS THE EXCEPTION AND IT IS STATED HERE RATHER THAN
    GLOSSED, because a docstring that claims more than the code does is how the
    next reader inherits a guarantee nobody implemented. An operand with no
    identity cannot BE a memo key, so it is computed each time — `projscope`'s
    own law, kept deliberately, since answering two questions from one colliding
    slot is worse than answering one twice.

    WHAT MAKES THAT SAFE IS NOT THE COUNT. Divergence is unrepresentable because
    there is no SECOND PRODUCT computed alongside the first: the witness is a
    projection of this record, so it cannot name a reading the body did not
    receive. And a record that served an unidentified operand is BLIND, so
    whatever those repeated computes return can never be certified. The double
    read is tolerated; it is never trusted.

    THE WITNESS IS A PROJECTION, NOT A READER. `witness()` performs no I/O; it
    sorts the record and joins it. Called before anything was read it answers
    None, because a record of nothing identifies nothing — never an empty string
    two builds could match on.

    OUTSIDE A SNAPSHOT NOTHING ACCUMULATES. `_lr_reads` hands a THROWAWAY
    read-set to any caller with no snapshot open — `helm lr`, a direct call to
    one leg, every non-web reader — so behaviour there is exactly what it was
    before this existed: each call reads for itself and remembers nothing. Same
    law `projscope` and `store.load.read_scope` already state, and the reason
    the write paths cannot be answered from a snapshot they did not open."""

    def __init__(self):
        dict.__init__(self)
        # (op, operand identity) -> (served value, result identity)
        self._reads = {}
        # (op, why) for every read that identified nothing. ONE of these makes
        # the whole record non-persistable; they are kept as a list rather than
        # a flag so a diagnosis can name which read went blind.
        self._blind = []
        # `_objexist_map` POPS the derived pending tips before writing its live
        # result. Keep the operands beside the dict so the result term can name
        # the exact batch it read and replay it after a restart; the dict value
        # alone may collapse to a digest and cannot recover its keys.
        self._objexist_pending = {}

    # ── the record ───────────────────────────────────────────────────────

    def _serve(self, op, operands, read):
        """Serve ONE read, at most ONCE, recording its operand and its outcome.

        `read` may return a `_Blind` to mean "this value is real, serve it, and
        it identifies nothing" — an unreadable ledger, a ref that will not peel.
        A RAISE is the same answer with no value: the instrument failed, so the
        record is blind and the caller is handed None.

        IT MAY ALSO RETURN A `_Sealed`, which is the door's own TOCTOU cure: the
        identity is then THE ONE OPEN that produced the value — path, inode and
        content digest from a single file description — rather than a
        fingerprint of the parsed value taken beside a `stat` of the path."""
        okey = _lr_operands(operands)
        slot = None if okey is None else (op, okey)
        if slot is not None and slot in self._reads:
            return self._reads[slot][0]
        try:
            value, why, stamp = _lr_unwrap(read())
        except Exception as exc:
            value, why, stamp = None, type(exc).__name__, None
        ident = None if why is not None \
            else _lr_identity(value if stamp is None else stamp)
        if slot is None:
            # NOT MEMOISED, for `projscope`'s reason: there is no key to
            # memoise under, and computing twice is better than answering two
            # questions from one slot. The record is already blind, so the
            # persistence hazard the double read used to open is closed above
            # this line rather than below it.
            self._blind.append((op, "operand names nothing: %r" % (operands,)))
            return value
        self._reads[slot] = (value, ident)
        if ident is None:
            self._blind.append((op, why or "result has no stable identity"))
        return value

    def _observe(self, op, operands, value):
        """Record a read `landreq` performed THROUGH this dict.

        Unwraps through `_lr_unwrap` on the SAME contract as `_serve`, because a
        caller that has to remember which recorder unwraps is a caller that will
        eventually record a wrapper object as an identity.

        A SECOND READING OF ONE SLOT IS COMPARED, NEVER DISCARDED, and that is
        this recorder's difference from `_serve`. `_serve` MEMOISES, so a second
        asker is handed the first answer and the body cannot hold two. Nothing
        memoises here: `landreq` writes into the dict, and `_landing_proofs._pin`
        POPS the slot on a failed probe precisely so the next carrier row reads
        it again. Dropping the later value on the floor is what made the body
        take the retry's answer while the record kept the failure's — measured
        as a body carrying ("refs/heads/main", None) under a witness still
        saying (null, null), which is the same-input divergence this class
        exists to make unrepresentable.

        SO A DISAGREEING RE-READ MAKES THE RECORD BLIND, rather than either
        value winning. Both readings were CONSUMED — the rows before the pop
        were judged on the first and the rows after it on the second — so there
        is no single world this snapshot describes, and the one answer that
        cannot be wrong in either direction is to refuse to certify it. An
        AGREEING re-read is the memo working and says nothing new; a slot
        already blind stays blind and needs no second reason."""
        value, why, stamp = _lr_unwrap(value)
        okey = _lr_operands(operands)
        if okey is None:
            self._blind.append((op, "operand names nothing: %r" % (operands,)))
            return
        slot = (op, okey)
        ident = None if why is not None \
            else _lr_identity(value if stamp is None else stamp)
        prior = self._reads.get(slot)
        if prior is not None:
            if prior[1] is not None and prior[1] != ident:
                self._blind.append(
                    (op, "re-read of %s disagrees with the record: %s -> %s"
                     % (okey, prior[1], ident)))
            return
        self._reads[slot] = (value, ident)
        if ident is None:
            self._blind.append((op, why or "result has no stable identity"))

    def __setitem__(self, key, value):
        """`landreq` memoising a read into the projection cache IS the read.

        Classified rather than copied: `_trunk_refs` writes under a bare gitdir
        string and `_resolved_ref` under ("refsha", gitdir, ref), and those two
        name refs a fetch can move. Anything else is either a derivation of a
        pin this record already holds (`_LR_DERIVED_CACHE_KEYS`) or a read
        nobody has classified, and the second makes the record blind."""
        dict.__setitem__(self, key, value)
        if isinstance(key, str):
            self._observe("trunkrefs", (key,), value)
            return
        if isinstance(key, tuple) and key and key[0] == "refsha":
            # "" IS NOT AN IDENTITY, IT IS A FAILED PEEL — round two's whole
            # finding. Two refs that will not resolve are not the same ref.
            self._observe("refsha", tuple(key[1:]), value if value else
                          _Blind("", "unresolvable: %r" % (key[1:],)))
            return
        if isinstance(key, tuple) and key and key[0] == "objexist-pending":
            # Preserve the DERIVED operands beside the dict before
            # `_objexist_map` pops this key. They are not a read and therefore
            # are not a term, but the live `objexist` result below cannot be
            # replayed without them.
            if len(key) != 2 or not isinstance(value, (list, tuple)) \
                    or not all(isinstance(tip, str) for tip in value):
                self._blind.append(
                    ("objexist", "pending batch has no replayable operands: %r"
                     % ((key, value),)))
                return
            self._objexist_pending[key[1]] = tuple(value)
            return
        if isinstance(key, tuple) and key and key[0] == "objexist":
            # A LIVE GIT READ OF A MOVING REPOSITORY, so it is a TERM and never
            # an exemption. An object can APPEAR by fetch or VANISH by gc with
            # no ledger write, no trunk move and no marker change — so a board
            # judged on "this tip exists" restores as FRESH over a repository
            # that no longer says so.
            #
            # THE BAR IS `base_behind`'s OWN, and this key fails it: that
            # exemption is stated to hold only because its operands are two
            # immutable object names plus a commit already carried under
            # `refsha`. A batch keyed on a gitdir, whose value is what git said
            # a moment ago, satisfies neither half.
            #
            # `_observe`, NOT `_serve`, because `landreq` PERFORMS this read on
            # its own first-need path and merely writes the result here. That is
            # the whole reason this cure is a classifier change and touches
            # `landreq` nowhere: trunk's own law is row-observation-lazy, never
            # repository-lazy — "that actually needs an answer buys the batch
            # through `_objexist_map`" — and a term that RELOCATED the read into
            # the prefetch would trade the lazy property for the served one,
            # which the ruling on task/1137 makes binding. Nothing here can
            # relocate it; the read happens exactly where it always did.
            #
            # FAILURE IS NOT EMPTY. Both shapes behave as empty mappings so the
            # body falls through to exact per-SHA probes, but the failure marker
            # makes this record BLIND: those fallback probes are outside this
            # read-set, so certifying their world from `{}` would be false-current.
            repo = key[1] if len(key) == 2 else None
            tips = self._objexist_pending.get(repo)
            if tips is None:
                self._blind.append(
                    ("objexist", "live batch has no preserved pending tips: %r"
                     % (key,)))
                return
            from . import landreq
            observed = (_Blind(value, "object-existence batch unreadable")
                        if isinstance(value, landreq._ObjectExistenceBatchFailure)
                        else value)
            self._observe("objexist", (repo,) + tips, observed)
            return
        if isinstance(key, tuple) and key and key[0] in _LR_DERIVED_CACHE_KEYS:
            return
        self._blind.append(("cache", "unclassified projection-cache key %r"
                            % (key,)))

    # ── THE OTHER WRITE DOORS, because `dict` DOES NOT ROUTE THEM ────────
    #
    # `dict.update`, `dict.setdefault` and `dict.__ior__` are implemented in C
    # against the concrete storage and DO NOT call an overridden
    # `__setitem__`. So each one puts a value into the projection cache that
    # `_observe` never sees: a read the body then consumes and the witness
    # cannot name — the single hazard this class exists to make impossible,
    # left open because the door classified only the writes spelled `d[k] = v`.
    # Routing them through `__setitem__` is what makes "every write is
    # classified" a property of the type rather than of the callers' spelling.
    #
    # THE EVICTORS ARE DELIBERATELY NOT OVERRIDDEN, and this is a measurement
    # rather than an omission: `pop`, `__delitem__`, `popitem` and `clear` can
    # only REMOVE a memo, and removing one cannot introduce a reading the record
    # has not seen — it can only cause the slot to be read AGAIN, which arrives
    # back through `__setitem__` and is compared there. `_landing_proofs._pin`
    # is the live caller of exactly that pattern, and it is `_observe`'s compare
    # that answers it, not a ban here. Forbidding the pop would break the
    # weather-retry the annotator needs; leaving it unrecorded is what broke the
    # witness. The enumerating arm in tests/test_web_lr.py holds this split.

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def __ior__(self, other):
        self.update(other)
        return self

    # ── the canonical readers: the ONLY door to a moving input ───────────

    def ledger_paths(self):
        """Every LEDGER the projection reads, in one enumeration.

        The enumeration itself is a read: a config change that points the
        projection at a different ledger set changes what the body consumed
        without changing any path already in the record. Recording it is what
        lets `recheck` replay a bounded operand list and still be total."""
        return self._serve("ledgers", (), _lr_ledger_paths)

    def input_path(self, label):
        """Where ONE non-ledger input file lives — the premise chain, the
        attest sidecar. Same argument as `ledger_paths`: the path is derived
        from home configuration, so it can move without the file moving."""
        return self._serve("path", (label,), lambda: _lr_input_path(label))

    def stat(self, label, path):
        """One file's exact identity: device, inode, size, mtime_ns, ctime_ns.

        AN MTIME IS NOT AN IDENTITY. It is coarse, settable from userspace,
        repeatable across a same-second rewrite, and completely unmoved by a
        chmod that makes a ledger unreadable — each of which admits a body whose
        input has demonstrably changed. `ctime_ns` cannot be set from userspace,
        and dev/ino say it is the same file rather than a same-named successor.

        ABSENT AND UNREADABLE ARE DIFFERENT FACTS. A path that does not exist is
        a measurement; a path that cannot be read is the instrument failing, and
        `_Blind` keeps the second out of the witness. Measured identical before
        this distinction existed, so a chmod that hid a ledger left the
        fingerprint byte-identical and restored the old body as current."""
        return self._serve("stat", (label, path),
                           lambda: self._read_stat(path))

    def identity(self, label):
        """Resolve and stat ONE input file the body consumes without stat-ing.

        `project_raw` reads the attest sidecar per row and needs no inode to
        compute its answer, yet every badge is an answer ABOUT that file's
        contents. So the read-set records the identity of the exact file it
        served — the body's own input, recorded once, rather than a second
        enumeration taken beside it.

        THIS IS THE WEAKER FORM AND IT IS NAMED AS SUCH. A stat is a SECOND
        resolution of the name: it can reach a file other than the one whose
        bytes the body consumed, and it says nothing about what those bytes
        were. Where the door owns the content — `premise_chain`, `receipt_rows`
        — it opens once and seals path, inode and content together, and does not
        call this. The attest sidecar is still stat-only, which is a DECLARED
        LIMIT of this record and not a property it provides."""
        path = self.input_path(label)
        return None if not isinstance(path, str) else self.stat(label, path)

    def premise_chain(self):
        """THE PREMISE CHAIN'S RECORDS — the CONTENT, read once, for everyone.

        A STAT IS NOT THIS INPUT. A stat records WHICH file and which inode a
        summary is about; it does not record what the file
        SAID, and every field of that summary is a function of the content. The
        two readings are separated by every instruction between them, so an
        append landing in the gap gives a body built on N+1 records under a
        witness naming the file at N — the same-input divergence this class
        exists to make unrepresentable, surviving through a seam the record did
        not own.

        AND THE BODY READ IT TWICE. `_lr_native_chain` called `chain_records()`
        for the count and the head index, then `verify_chain()` — which calls
        `chain_records()` AGAIN — for the verdict and its "(N records)" wording.
        One card therefore carried a count from one reading of an append-only
        file and a verification from another, with no term in the witness able
        to name either. Serving the records HERE makes those one reading by
        construction, and puts the CONTENT's identity into the witness instead
        of a proxy for it: a chain that grew is a term that changed, whether or
        not the stat that describes the file was taken before or after.

        AN UNRESOLVABLE PATH ANSWERS None. `input_path` already recorded that
        blind, so the record cannot be certified — and None rather than [] is
        what keeps "helm could not look" from rendering as "the chain is empty"
        on the card. [] is the MEASURED answer for a chain file that is not
        there; None is every failure of the instrument.

        THE PATH IS THE OPERAND AND THE OPEN IS THE IDENTITY. `input_path`
        resolved this string and this reader opens THAT STRING — it does not
        call `chain_records`, which would resolve the chain path a second time
        and open whatever it named at that instant. See `_Sealed`: the recorded
        identity is the path, the inode and the content digest of ONE file
        description, so no reopen can slide between the record and the body."""
        path = self.input_path("premise")
        if not isinstance(path, str):
            return None
        return self._serve("premisechain", (path,),
                           lambda: self._read_chain(path))

    def receipt_rows(self):
        """The durable receipt index's ROWS — the LEDGER'S CONTENT, read once.

        THE SAME DEFECT AS THE PREMISE CHAIN, IN THE OTHER FILE THE RECORD ONLY
        STATTED. `ledger_paths` puts this ledger's PATH in the record and `stat`
        puts its inode there; neither says what it CONTAINED, and the badge
        column on every recent-lands row is a function of the contents. So the
        rows were read straight out of `landreq` beside the record, which is a
        body term the witness cannot name — the one thing this class exists to
        make impossible, surviving in the seam nobody had enumerated.

        A FAILED READ IDENTIFIES NOTHING, AND IT IS NOW SAID RATHER THAN
        ARRANGED. The previous cure leaned on `_receipt_rows`' failure marker
        having module-private OBJECT keys, so `json.dumps` failed, so the record
        went blind — the right outcome resting on a serialization accident that
        any future marker respelling would silently remove. Unreadable and
        corrupt now answer `_Blind` with a named reason. An ABSENT ledger is not
        that case and must not be confused with it: a missing file answers
        ([], None), a MEASURED "no receipts are recorded", sealed as ABSENT.

        THE PATH IS RESOLVED ONCE, HERE, AND OPENED HERE. `landreq._receipt_rows`
        calls `receipts_path()` itself, so recording the path and then calling it
        was two resolutions with the body bound to the second and the record to
        the first. This resolves once into `path`, records THAT, opens THAT, and
        parses the bytes that open returned through `eventledger.checked_rows` —
        the same parser `checked_events` uses, so a complete row means here
        exactly what it means everywhere else."""
        from . import landreq
        path = landreq.receipts_path()
        return self._serve("receiptrows", (path,),
                           lambda: self._read_receipts(path))

    def policy_history(self):
        """One indexed retained-policy read, sealed into the freshness witness."""
        from .store import policy_history
        path = policy_history.path()

        def read():
            sealed = self._read_receipts(path)
            value = policy_history.index(*sealed.value)
            return _Blind(value, sealed.why) if isinstance(sealed, _Blind) else _Sealed(value, sealed.stamp)

        return self._serve("policyhistory", (path,), read)

    def repo(self):
        """(gitdir, trunk ref) for the repository this dashboard reports on.

        THE ANSWER IS THE TERM, NOT THE FILES BEHIND IT. This resolves through
        `dispatches.home_repo_id` and `landreq._origin_configured` — movable
        inputs whose only effect on the body is WHICH repository and WHICH ref
        answer for trunk. Recording the answer covers them all: a change that
        moves neither is not a change this body can see, and one that moves
        either is.

        THE LIST USED TO NAME THE REGISTRY AND INJECT'S PROJECT MAPPING, and it
        no longer does because the resolution no longer reads them — see
        `_read_repo`, which now delegates identity to the write door's own
        answer rather than to a rebuildable projection."""
        return self._serve("repo", (), self._read_repo)

    def trunkrefs(self, repo):
        """(local trunk ref, upstream trunk ref) for one repository.

        The ref NAME is part of the identity, not just the sha it resolves to: a
        repository that stops having an upstream trunk has changed what the
        projection reads even when every sha it still resolves is unchanged."""
        from . import landreq
        return list(landreq._trunk_refs(repo, self))

    def refsha(self, repo, ref):
        """The COMMIT `ref` names — resolved once per (repo, ref), for everyone.

        `refs/remotes/origin/main` is a local snapshot a background fetch MOVES
        and can REWIND. Every question asked with the NAME in its argv is
        answered about whatever it pointed at at that instant, so the whole
        build binds to the commit this read resolved."""
        from . import landreq
        return landreq._resolved_ref(repo, ref, self)

    # ── reads about IMMUTABLE objects, which are DERIVATIONS not inputs ──

    def _pinned(self, operands):
        """Are these all IMMUTABLE OBJECT NAMES? -> True, or record and refuse.

        A commit id names one object that can never become another, so two
        reads about it seconds apart cannot be answered about two worlds and
        its answer is a DERIVATION of a pin this record already holds — not an
        input identity, and not something to fingerprint.

        THE CHECK IS WHAT MAKES THAT SAFE, AND IT IS ROUND ONE'S DEFECT STATED
        MECHANICALLY. `git log refs/remotes/origin/main` is a moving binding
        wearing an immutable read's clothes: it looks like a derivation, it is
        answered about whatever the name pointed at as it ran, and that is
        exactly how the lands leg came to describe a trunk the witness never
        named. So a non-hex operand does not pass quietly — it makes the record
        blind, because the read happened and nothing can say what it read.

        AN ABBREVIATION IS NOT AN OBJECT NAME EITHER, AND IT FAILS THE OTHER
        WAY. A seven-character prefix is a PREFIX SEARCH, not a name: it
        resolves to whatever single object starts with it, and the moment a
        second object
        in this repository shares that prefix — one fetch, one fold, one
        imported bundle — `rev-parse` refuses it as ambiguous and the identical
        question answers UNKNOWN where it answered ANCESTOR. Measured exactly
        so: one witness, two proofs. Git itself treats abbreviation length as a
        function of the object COUNT (`core.abbrev` grows as the repository
        does), which is the same statement — a prefix's meaning is a property
        of the world, and a property of the world is precisely what an
        immutable-object derivation may not depend on.

        SO IT IS SERVED AND NEVER CERTIFIED, which is `_Blind`'s split rather
        than the refusal above. A ref name must not be READ here at all: git
        would answer it just as happily, about whatever it points at, and that
        answer must never reach the body. An abbreviation names a real object
        NOW, so the surface may render its answer — what it may never do is
        enter the witness, because no later reading can be sure it re-found the
        same object. 40 hex is sha1, 64 is sha256; every other length is a
        prefix of one of them."""
        for value in operands:
            if not (isinstance(value, str)
                    and re.fullmatch(r"[0-9a-f]{7,64}", value)):
                self._blind.append(
                    ("object", "not an immutable object name: %r" % (value,)))
                return False
            if len(value) not in (40, 64):
                self._blind.append(
                    ("object", "abbreviated object name is a prefix search, "
                               "not a stable identity: %r" % (value,)))
        return True

    def object_read(self, repo, subcommand, *argv):
        """One git read whose non-flag operands are all object names.

        Serves and records NOTHING beyond the refusal above: the answer is a
        function of the pinned commit, which the record already identifies.

        EVERY OPTION MUST CARRY ITS OWN VALUE (`--max-count=6`, never `-n 6`),
        because a value sitting in its own argv slot is indistinguishable here
        from a revision — and this door's whole job is to refuse a revision that
        is not an object name. A separated value therefore reads as a moving
        binding and makes the record blind, which is LOUD: the leg answers
        UNKNOWN and says so on the card. That is the correct direction and it is
        how this constraint was measured rather than assumed."""
        from . import landreq
        if not self._pinned([a for a in argv if not str(a).startswith("-")]):
            return None
        return landreq._git(repo, subcommand, *argv)

    def object_existence(self, repo, tip, *more_tips):
        """One lazy non-empty object-existence batch, with complete operands.

        `landreq._objexist_map` consumes a derived pending-tip list, POPS it, and
        writes only the resulting map. The map may exceed the inline identity
        limit and collapse to a digest, so a persisted witness cannot recover
        the tips from its result. They belong in the operand tuple: repository
        plus every exact tip the batch asked about. Recheck can then ask the same
        moving repository question rather than refusing an otherwise-valid saved
        body because `objexist` had no replay reader."""
        from . import landreq
        tips = (tip,) + tuple(more_tips)

        def read():
            got = landreq._object_exists_batch(repo, list(tips))
            return (_Blind({}, "object-existence batch unreadable")
                    if got is None else got)

        return self._serve("objexist", (repo,) + tips, read)

    def object_id(self, repo, name):
        """The FULL object id `name` denotes, or "" — SERVED ONCE AND RECORDED.

        THE FOLD GRAMMAR CARRIES AN ABBREVIATION. Every recent-lands row takes
        its tip out of a fold subject, and that tip is 12 hex — so every landing
        question this card asks was asked about a PREFIX SEARCH. Measured on the
        fab: the only abbreviated operand reaching the immutable-object door in
        a real build is this one, twice per row.

        A PREFIX IS NOT AN IDENTITY, WHICH IS WHY THE ANSWER IS RECORDED RATHER
        THAN THE PREFIX REFUSED. It denotes whatever single object currently
        starts with it; the day a second object does, git refuses it as
        ambiguous and the identical row's proof flips ANCESTOR to UNKNOWN with
        nothing in the witness having moved. Resolving here puts exactly that in
        the record: the prefix is the OPERAND and the full id is the ANSWER, so
        a prefix that stops denoting this object is a term that CHANGED and the
        body ages out — which is the property blocker (3) asked for, reached by
        requiring full identity rather than by refusing to serve.

        AN UNRESOLVABLE PREFIX IS MEASURED, NOT BLIND, on `_Blind`'s own split:
        "no object here starts with this" and "this prefix is ambiguous" are
        facts about the world that a later reading can contradict, not a failed
        instrument. They are safe to record as "" because the OPERAND carries
        the name — two unresolvable prefixes occupy different slots and can
        never compare equal, which is the trap `refsha` had to use `_Blind`
        for."""
        from . import landreq

        def read():
            p = landreq._git(repo, "rev-parse", "--verify", "--quiet",
                             "--end-of-options", str(name) + "^{commit}")
            out = (p.stdout or "").strip() \
                if p is not None and p.returncode == 0 else ""
            return out if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", out) else ""

        return self._serve("objectid", (repo, name), read)

    def landing_proof(self, repo, tip, pinned):
        """Is the change in `tip` on `pinned`? Both operands are objects."""
        from . import landreq
        if not self._pinned((tip, pinned)):
            return "unknown"
        return landreq._landing_proof(repo, tip, pinned)

    # ── the readers' own implementations, INSIDE the door ────────────────

    @staticmethod
    def _read_stat(path):
        """One path's stat fields, or the honest failure. See `stat`."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return ["ABSENT"]
        except OSError as exc:
            return _Blind(["UNREADABLE", exc.errno], "unreadable")
        return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                st.st_ctime_ns]

    @staticmethod
    def _open_once(path, nofollow=True):
        """ONE open of `path` -> (stamp, bytes, fstat of THAT descriptor).

        THE WHOLE POINT IS THAT THERE IS NO SECOND SYSCALL BY NAME. `os.stat`
        resolves the name again, and a reader handed a PATH resolves it again;
        either can reach a different file than the one whose bytes were served,
        and then the record and the body describe two worlds. Here the `fstat`
        and the `read` are both against the descriptor `os.open` returned, so
        WHICH FILE and WHAT IT SAID cannot disagree — a rename, a swap or a
        home rebind after the open leaves this reading intact and correct.

        THE STAMP IS [path, dev, ino, content digest] AND NOT MORE. The name we
        opened, the file that name reached at the instant of the open, and what
        that descriptor contained. Size and the timestamps are deliberately
        absent: the digest already says the content changed, and mtime would
        additionally age the record out for a rewrite that changed nothing —
        false-stale is safe but it is not free, and this term is compared on
        every restore.

        THE STAMP SAYS WHAT WAS OPENED, NOT WHAT THE NAME POINTS AT, AND THAT
        IS WHY `nofollow` DEFAULTS ON. One open closes the gap between the read
        and its own record; it does NOT close the gap between a caller that
        VALIDATED the name and this open, which resolves that name a second
        time. `_read_receipts` was the live case: `eventledger._prepare` lstat`s
        the ledger and refuses a symlink, then hands on the PATH STRING, and a
        link appearing in that window was followed here — MEASURED, the decoy's
        bytes came back under a stamp naming the ledger and carrying the DECOY's
        inode and digest, so the seal certified a file the validation had never
        seen (exact-tip round six). O_NOFOLLOW makes the refusal part
        of the open itself, so there is no window to stand in: the check and the
        act are one syscall. It refuses nothing `_prepare` accepts — `_prepare`
        already rejects a symlinked leaf — so this closes a race without
        narrowing the set of ledgers helm will read.

        THE THREAT MODEL IS ACCIDENT. Atomic replace via rename, log rotation,
        a deploy swapping a link, an editor writing through a temp file: all of
        them retarget the FINAL component, which is exactly what O_NOFOLLOW
        refuses. A DECLARED LIMIT, because it is the honest edge of this guard:
        an ancestor directory swapped between `_prepare`'s realpath and this
        open is still followed. Anchoring each component to a held descriptor is
        the cure for that, it is `todos`' discipline, and it is not built here —
        no ordinary accident retargets a parent directory mid-read.

        `nofollow=False` IS FOR A READER WHOSE WRITER FOLLOWS LINKS. Refusing
        where the writer permits does not make that install safer, it makes the
        input permanently unreadable — see `_read_chain`.

        A MISSING O_NOFOLLOW REFUSES; IT DOES NOT SILENTLY DEGRADE. The house
        spelling for an optional open flag here is `getattr(os, "X", 0)`, and
        this door was first written that way by reflex — copied from
        `eventledger._flags`, where the flag is defence-in-depth BESIDE
        `_prepare`'s lstat. At THIS site it is not defence-in-depth, it is the
        whole cure for the race, so the same spelling carries weight it never
        carried at its origin: `|= 0` on a platform without the constant is a
        guard that vanishes, an open that succeeds, a symlink that is followed
        and a seal that certifies the target — with every arm still green,
        because they run where the constant exists. That is this lane's own
        defect shape (a check consulting something narrower than the property
        it claims, degrading in the reassuring direction), so it is refused
        instead: asking for a protection this platform cannot provide raises,
        and the caller turns that into `_Blind` — UNKNOWN, which is what canon
        requires of any bound that skips its work. O_CLOEXEC keeps the tolerant
        spelling on purpose; it is hygiene, and its absence falsifies no claim
        this stamp makes.

        Raises OSError, which every caller turns into the right kind of answer:
        FileNotFoundError is a MEASUREMENT, anything else is the instrument.
        ELOOP arrives as the second kind, which is correct: a link where a
        regular file was expected is a read that identifies nothing."""
        import hashlib
        flags = openflags.flags(os.O_RDONLY, cloexec=True)
        if nofollow:
            guard = getattr(os, "O_NOFOLLOW", None)
            if guard is None:
                raise OSError(errno.ENOSYS,
                              "O_NOFOLLOW is unavailable, so this read cannot "
                              "refuse a symlink at the open and will not "
                              "pretend it did")
            flags |= guard
        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)
            chunks = []
            while True:
                block = os.read(fd, 1 << 20)
                if not block:
                    break
                chunks.append(block)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        return ([path, st.st_dev, st.st_ino,
                 "blake2b:" + hashlib.blake2b(data, digest_size=16).hexdigest()],
                data, st)

    @staticmethod
    def _read_chain(path):
        """The premise chain's records from ONE open of `path`. See `_Sealed`.

        THIS DOOR FOLLOWS A LINK AND THE ASYMMETRY WITH `_read_receipts` IS
        DELIBERATE. Nothing validated this name, so there is no window between a
        check and this open and no anti-symlink claim for the seal to inherit —
        the two conditions that make O_NOFOLLOW the cure over in the ledger door
        are both absent here. What IS here is a writer that follows links:
        `premise._chain._append_record` appends through `open(path, "a+")`, so a
        symlinked chain path is a configuration helm currently WRITES. A reader
        stricter than its own writer does not make that install safer, it makes
        the chain permanently `_Blind` — and a blind read nulls the witness,
        which stops the projection persisting at all. That is the cold-cache
        symptom this lane exists to remove, so refusing here would trade a
        hazard nobody has for the exact defect we are curing.
        THE SWAP IS STILL CAUGHT, just later and by a different term: the stamp
        carries dev/ino, so a repointed link reads as a changed identity at
        `recheck` and the projection is correctly invalidated rather than
        silently served."""
        from .premise import _chain
        try:
            stamp, data, _st = _LrReadSet._open_once(path, nofollow=False)
        except FileNotFoundError:
            # MEASURED: there is no chain here. `_read_stat` calls the same
            # thing ABSENT and for the same reason — a later reading can
            # contradict it, which is what makes it safe to certify.
            return _Sealed([], [path, "ABSENT"])
        except OSError as exc:
            return _Blind(None, "unreadable: %s" % type(exc).__name__)
        return _Sealed(_chain.records_from(data), stamp)

    @staticmethod
    def _read_receipts(path):
        """`landreq._receipt_rows`' answer, from ONE open of `path`.

        THE LEDGER'S OWN SAFETY CHECKS COME WITH IT, from `eventledger` rather
        than restated here: `_prepare` refuses a symlinked path or a symlinked
        parent, and the descriptor must be a regular file with exactly one link.
        A reader that skipped them would be a second, laxer door onto the same
        ledger, which is how a two-door input becomes a two-answer input.

        AND THE OPEN FLAGS ARE ONE OF THOSE CHECKS — that is what this door was
        missing. `eventledger`'s own reader opens through `_flags()`, which adds
        O_NOFOLLOW; this door reproduced `_prepare` and the fstat but opened
        WITHOUT it, so it was a second, laxer door in the one respect it claimed
        to have copied. `_prepare` validates the NAME and then returns a STRING,
        and reopening that string is a second resolution: MEASURED, a link
        swapped into that window was followed and the decoy's bytes came back
        sealed under the ledger's name. `_open_once` now refuses a symlinked
        leaf in the open itself, which is what `_prepare` already promised about
        this path — so no ledger helm would previously read becomes unreadable,
        and the two syscalls can no longer disagree about which file they mean.
        """
        import stat as statmod
        from . import eventledger, landreq
        try:
            stamp, data, st = _LrReadSet._open_once(eventledger._prepare(path),
                                                    nofollow=True)
            if not statmod.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise OSError("ledger is not a private regular file")
        except FileNotFoundError:
            return _Sealed(([], None), [path, "ABSENT"])
        except OSError as exc:
            return _Blind(([], {landreq._RECEIPT_LEDGER_UNREADABLE:
                                "%s: %s" % (type(exc).__name__, exc)}),
                          "unreadable: %s" % type(exc).__name__)
        rows, corrupt = eventledger.checked_rows(data, strict=True)
        if corrupt:
            return _Blind(([], {landreq._RECEIPT_LEDGER_ERROR: corrupt}),
                          "corrupt")
        return _Sealed((rows, None), stamp)

    def _read_repo(self):
        """(gitdir, trunk_ref) for the repository this dashboard reports on, or
        (None, None) when there is no repository to report on.

        THE SERVER CWD IS NOT PROJECT IDENTITY. The systemd web unit starts
        outside any checkout, so deriving from ``os.getcwd()`` blanked the
        owner's LANDED row while folds continued reaching trunk. That rule now
        lives in ``dispatches.home_repo_id``, which this delegates to, because
        the WRITE door refuses foreign rows against the same answer this board
        reports on: two copies of the resolution could disagree, and then a row
        would be rejected as foreign by one and counted as local by the other.

        THE REGISTRY ROUTE THIS USED TO TAKE WAS A MEASURED SECURITY DEFECT,
        and it is gone rather than merely restyled. This read-set wrapped the
        implementation as it stood when the lane opened: package path ->
        `inject.project_for_cwd` -> registry record -> `_repo_info`. Trunk has
        since cured that, and `home_repo_id`'s own docstring records why in the
        strongest terms — the registry is "pure PROJECTION, rebuildable from a
        re-scan", so an absent, corrupt or unreadable cache made the write
        door's authority UNKNOWN, and a door that refuses only on a KNOWN
        mismatch refuses NOTHING while its authority is unknown. That was an
        unlabelled escape reachable by DELETING a file helm regenerates on
        demand, and the sixteenth foreign row was possible precisely while the
        projection was unreadable. Identity is therefore derived from the
        code's own location and never from a rebuildable cache. Carrying the
        old route forward inside a read-set would have re-opened a hole
        cross-family review had already closed, which is the one thing a
        rebase may never do quietly.

        WHAT THE READ-SET STILL OWNS IS UNCHANGED: the ANSWER is served and
        RECORDED through `_serve`, so a registry edit or a repository binding
        that changes which repo or which ref answers for trunk still ages this
        body out. Recording an answer and CHOOSING it are different jobs —
        dispatches chooses, this records.

        Which trunk ANSWERS follows landreq's own precedence unchanged: the
        upstream ref when an origin is configured, the local one otherwise. The
        ref pair comes through this read-set, so the trunk this names and the
        trunk the rows are proved against are ONE reading."""
        from . import dispatches, landreq
        gitdir, _why = dispatches.home_repo_id()
        if not gitdir:
            return [None, None]
        local, upstream = self.trunkrefs(gitdir)
        return [gitdir,
                upstream if landreq._origin_configured(gitdir) else local]

    def tier(self, recipient, repo=None):
        """The APPROVAL TIER for one (reviewer, repository).

        THE POLICY IS AN INPUT AND IT MOVES WITH NO FILE ON THIS BOX CHANGING.
        `landreq._lr` asks this for every row whose verdict would otherwise be
        READY, and a reviewer the tier does not admit demotes that row to
        REVIEWED. It resolves from an owner-revisable prior in the typed store
        PLUS the reviewer's MEASURED live runtime family, so it can move while
        the ledgers, trunk, the premise store and the attest sidecar all sit
        byte-identical. The dangerous direction is not a row that quietly gains
        a badge; it is an OLD APPROVED body surviving the moment its approval
        stopped counting. The tier was revised 2026-08-11, so this is live.

        THE BODY REACHES THIS THROUGH `dispatches.tier_lens`, not by being asked
        to remember to call it. The lens is installed for the duration of the
        snapshot on this thread, so `_approval_refusal` deep inside `project_raw`
        is answered from this record — one resolution, consumed by the body and
        projected into the witness, with no second call able to disagree.

        "unknown" IS A MEASURED ANSWER and is recorded with its reason: a policy
        exists and could not be evaluated is a fact about the world that a later
        reading can contradict, and the reason is the wording the body embeds. A
        RAISE is not — `_serve` records that blind."""
        from . import dispatches

        def read():
            return list(dispatches._approval_tier_uncached(recipient,
                                                           repo=repo))

        got = self._serve("tier", (recipient, repo), read)
        # THE CALLER'S CONTRACT IS `approval_tier`'s, UNCHANGED. The record
        # holds a list because that is what serialises canonically; every
        # consumer of this seam has always been handed a 2-tuple, and a lens
        # that quietly changed the type would be a second behaviour difference
        # riding a correctness fix. A raise answered None above, and
        # `_approval_refusal` fails closed on that exactly as it always has.
        return ("unknown", "the approval tier could not be resolved") \
            if got is None else tuple(got)

    def gate_epoch(self):
        """THE FROZEN CUTOVER that decides whether an unstamped approve counts.

        IT MOVES WITH NO OTHER TERM IN THIS RECORD CHANGING, which is the whole
        reason it needs one. `dispatches.gate_epoch` answers from a MARKER FILE
        (`_read_epoch` -> `_marker_file`) that no ledger stat describes, so
        writing, corrupting or deleting that marker alone flips the answer
        between an index, EPOCH_LOST and None while every recorded ledger term
        stays byte-identical. Measured by a probe at CL96: a marker-only
        mutation restored a stale READY/REVIEWED body as FRESH, because the
        witness could not name the input that had changed underneath it.

        THE BODY CONSUMES IT DEEP, at `landreq.project_raw`, to DEMOTE a row
        whose approval predates the freeze. So the dangerous direction is the
        same one `tier` names one method above: not a row that quietly gains a
        badge, but an old approved body surviving the moment its authority
        stopped counting.

        THE OPERAND SET IS EMPTY ON PURPOSE, and that is the replay table's
        totality argument rather than a shortcut. `gate_epoch`'s other two
        inputs are the ledger snapshot and its verdict index — both DERIVED
        from reads this record already holds, so a world that would make this
        read a different marker must first change a term the replay compares.
        `_lr_operands(())` is `"[]"`, a real key: this read is memoised like
        every other and does NOT take the unidentified-operand path that would
        make the whole record non-persistable.

        ALL THREE ANSWERS CARRY AN IDENTITY, so a normal reading never goes
        blind: an int, the string EPOCH_LOST, and None all serialize under
        `_lr_identity`. EPOCH_LOST especially is a MEASURED answer — a marker
        exists and cannot be trusted — and recording it as a value rather than
        as a blindness is what lets a later reading contradict it. The function
        never raises by contract; if it ever does, `_serve` records that blind
        and the snapshot correctly refuses to persist.

        THE CALLER'S CONTRACT IS `dispatches.gate_epoch`'s, UNCHANGED."""
        from . import dispatches

        def read():
            # THE BODY, NEVER THE DOOR. `dispatches.gate_epoch` consults the
            # lens this reader is installed as, so calling it here would route
            # straight back into this method — the same reason `tier` calls
            # `_approval_tier_uncached`.
            #
            # SUSPEND THE LENS WHILE THE BODY DERIVES ITS LEDGER SNAPSHOT. The
            # body calls `_snapshot` when no pair was supplied, and replay of a
            # build-landed event asks `gate_epoch(current, verdicts)` with that
            # explicit pair. Leaving this lens installed routes the nested call
            # back here, where the same empty-operand slot is not memoised until
            # `read` returns: recursion until `_serve` catches RecursionError and
            # marks the entire otherwise-healthy projection blind. The board
            # renders, but persistence writes no file and every restart is cold.
            #
            # `None` restores the ordinary body for that nested call. It is not
            # a second reading: the explicit pair is the snapshot this outer
            # body is deriving, and the ONE marker answer still returns through
            # this `_serve` call and becomes the witness term.
            with dispatches.epoch_lens(None):
                return dispatches._gate_epoch_uncached()

        return self._serve("gateepoch", (), read)

    def board_scope(self, repo_id=None):
        """THE SCOPE THIS BOARD PROJECTS FOR — {repo_id, project, why}.

        ITS PROJECT LAYER MOVES WITH NO FILE THIS RECORD OTHERWISE NAMES.
        `landreq._project_of` resolves through the REGISTRY, so a registry-only
        remap changes which rows read local, foreign and unresolved — and with
        them the withheld counts and the observation authority the card states
        — while the ledgers, trunk, the premise chain and the attest sidecar
        all sit byte-identical. Measured by a probe at CL96.

        A FORBID-LIST ENTRY WOULD DETECT THIS AND NOT CURE IT. Adding
        `("landreq", "board_scope")` to the seam scan reddens on the call, which
        is a true report that the read happens outside the door; it leaves the
        read outside. Serving it here is the cure the scan was asking for.

        THE REPO LAYER WAS ALREADY COVERED and stays that way: `home_repo_id`
        is served through `_read_repo`, so this term adds the PROJECT
        resolution rather than duplicating the identity underneath it.

        `repo_id` IS A REAL OPERAND — a path string or None, which is exactly
        what `_lr_operands` admits — so the two call sites (the build's bare
        call and `project_raw`'s explicit one) memoise separately and correctly
        rather than answering each other from one slot."""
        from . import landreq

        def read():
            # THE BODY, NEVER THE DOOR — `landreq.board_scope` consults the
            # lens this reader is installed as. Same law as `gate_epoch` and
            # `tier` above.
            return landreq._board_scope_uncached(repo_id)

        return self._serve("boardscope", (repo_id,), read)

    def seat_lineage(self, seat):
        """The holder answer the card consumed, replayed on roster-only changes."""
        from . import seats_lineage

        def read():
            answer = seats_lineage._seat_lineage_uncached(seat)
            if answer[1] == seats_lineage.SEAT_LINEAGE_UNKNOWN:
                return _Blind(answer, answer[2])
            return answer

        return self._serve("seatlineage", (seat,), read)

    # ── the two products ─────────────────────────────────────────────────

    def witness(self):
        """A PURE DETERMINISTIC PROJECTION of this record. Performs no reads.

        None when ANY read went blind, and None when nothing was read at all.
        Both are the same sentence: this snapshot cannot identify what it
        served, so `_persist_store` writes no file and `persist_load` answers
        UNKNOWN. The cache stays EMPTY and behaves exactly as it did before
        persistence existed, which is the honest fallback this lane's design
        note already named. Refusing to serve is acceptable; serving a body
        assembled from an input nobody can name is not."""
        if self._blind or not self._reads:
            return None
        return "\x1e".join(
            "\x1f".join((op, okey, ident))
            for (op, okey), (_value, ident) in sorted(self._reads.items()))

    def recheck(self, saved):
        """Is `saved` still true of the world? -> the CURRENT projection, or None.

        ONE NEW READ-SET, COLLECTED THROUGH THE SAME CANONICAL READERS. The
        saved witness records which operands the saved body's build actually
        consumed, so replaying exactly those operands re-collects exactly that
        input — no second enumeration that could describe a different set, and
        no rebuild of the body to find out.

        THE OPERAND SET DOES NOT NEED ITS OWN COVERAGE, and this is the property
        that makes a bounded replay total: every operand here is either fixed or
        DERIVED from another recorded read. Which ledgers exist comes from
        `ledgers`; which repository answers comes from `repo`; which repositories
        the rows name comes from the dispatch ledger, whose `stat` is in the
        record. So a world that would make the build read a DIFFERENT operand
        must first change a result this replay compares.

        A term this reader cannot replay refuses outright. A saved record naming
        an operation this code no longer has is a record whose meaning changed
        under it, and comparing it would be guessing."""
        if not isinstance(saved, str) or not saved:
            return None
        for term in saved.split("\x1e"):
            fields = term.split("\x1f")
            if len(fields) != 3:
                return None
            reader = _LR_REPLAY.get(fields[0])
            if reader is None:
                return None
            try:
                operands = json.loads(fields[1])
            except ValueError:
                return None
            if not isinstance(operands, list):
                return None
            try:
                reader(self, *operands)
            except TypeError:
                return None              # the saved arity is not this reader's
        return self.witness()


# THE REPLAY TABLE IS THE READER TABLE. Built from the bound methods rather than
# spelled a second time, so an op that can be RECORDED but not REPLAYED cannot
# exist — that drift would make every restore refuse, silently, and look exactly
# like a cache that simply never warms.
_LR_REPLAY = {
    "ledgers": _LrReadSet.ledger_paths,
    "path": _LrReadSet.input_path,
    "stat": _LrReadSet.stat,
    "repo": _LrReadSet.repo,
    "trunkrefs": _LrReadSet.trunkrefs,
    "refsha": _LrReadSet.refsha,
    "tier": _LrReadSet.tier,
    # THE BOUND METHOD DIRECTLY, no lambda, because its saved operand list is
    # EMPTY: `reader(self, *operands)` is `gate_epoch(self)`, which is exactly
    # this signature. `premisechain` and `receiptrows` below need a lambda only
    # because they were RECORDED with a path operand they must now ignore and
    # re-resolve; this term never had one to discard.
    "gateepoch": _LrReadSet.gate_epoch,
    # THE BOUND METHOD, and its saved operand list carries the ONE operand it
    # takes — `[repo_id]` — so `reader(self, *operands)` is
    # `board_scope(self, repo_id)`. Unlike `premisechain` the saved operand is
    # re-USED rather than discarded, because it names WHICH scope was asked
    # for rather than where a file lived.
    "boardscope": _LrReadSet.board_scope,
    "seatlineage": _LrReadSet.seat_lineage,
    # REPLAYED WITH NO ARGUMENT, because its one operand is DERIVED: the saved
    # term carries the path, and the path comes back through `input_path`,
    # whose own term is in the same record. `recheck`'s totality argument
    # exactly — an operand is either fixed or derived from another recorded
    # read — so the saved path is compared as `path`, and this re-reads
    # whatever `input_path` resolves NOW rather than trusting the saved string.
    "premisechain": lambda reads, _path=None: reads.premise_chain(),
    "receiptrows": lambda reads, _path=None: reads.receipt_rows(),
    "policyhistory": lambda reads, _path=None: reads.policy_history(),
    "objexist": _LrReadSet.object_existence,
    "objectid": _LrReadSet.object_id,
}


@contextlib.contextmanager
def _lr_snapshot():
    """THE BUILD'S ONE READ-SET, held open across the body AND its witness.

    Entered by `web_cache` around `fn()` and around the restore's `recheck`, so
    the object that served every read is the object the witness is projected
    from. That is the whole cure: the witness does not merely PRECEDE or FOLLOW
    the build, it is a function of what the build was served.

    IT ALSO OPENS `store.load.read_scope` AND `projscope`, one layer over. The
    approval tier reads an owner-revisable prior, and `landreq._git` memoises
    per argv — without one scope spanning the cycle those are second readings of
    moving state taken later. `project_raw` opens both of its own; both are
    re-entrant and only the outermost clears, so nesting is the composition they
    document.

    AND IT INSTALLS THE TIER LENS, which is how the body reaches this record
    without being asked to remember to. `dispatches.approval_tier` is called from
    inside `project_raw`, four frames below anything this module wrote; the lens
    routes it here for the duration of the snapshot and restores the previous
    resolution on exit, so every write path — the land door, the close ladders,
    the send advisory — still resolves the tier live exactly as before.

    RE-ENTRANT ON THE RECORD: an inner `with` yields the OUTER read-set and never
    starts a second one, so a caller composing two of these cannot silently begin
    a new snapshot halfway through the first."""
    from . import dispatches, projscope, seats_lineage
    from .store import load as store_load, policy_history
    outer = getattr(_LR_READS, "reads", None)
    if outer is not None:
        # THE INNER SCOPE DECLARES NOTHING AND INHERITS THE OUTER BUDGET. A
        # deadline armed here would only ever TIGHTEN the one the outermost
        # reader chose, which is the composition `derive_budget` exists to
        # keep out of nested calls.
        with store_load.read_scope(), projscope.scope():
            yield outer
        return
    reads = _LrReadSet()
    _LR_READS.reads = reads
    try:
        from . import landreq as _landreq
        with store_load.read_scope(), projscope.scope(), \
                dispatches.tier_lens(reads.tier), \
                dispatches.epoch_lens(reads.gate_epoch), \
                policy_history.read_lens(reads.policy_history), \
                _landreq.scope_lens(reads.board_scope), \
                seats_lineage.lineage_lens(reads.seat_lineage):
            # THE BOARD READER SEEDS A BUDGET IT CAN ACTUALLY FINISH IN,
            # INSIDE the scope and before any projection work, because the
            # seed is memoised per scope. This build is the revalidate half of
            # `_cached_swr` — it serves the previous body while it runs and a
            # cold caller is answered by `cold_body` — so the reader that most
            # needed to FINISH was the one paying the inject hook's 4 seconds.
            # It is the SOFT budget on purpose: a projscope deadline here
            # would make a spent bound RAISE out of the projection instead of
            # rendering the rows it could not derive. See
            # `landreq.arm_derive_budget`.
            _landreq.arm_derive_budget(_landreq.BOARD_DERIVE_BUDGET_S)
            yield reads
    finally:
        _LR_READS.reads = None


def _lr_reads():
    """The open snapshot's read-set, or a THROWAWAY one when none is open.

    Never None, and never a bare dict: every consumer in this module reads
    THROUGH this object, so handing back something without the readers would be
    the bypass the whole design forbids. A throwaway records into itself and is
    discarded, which is precisely "pin nothing" — the behaviour outside a
    snapshot, and the behaviour before any of this existed."""
    reads = getattr(_LR_READS, "reads", None)
    return _LrReadSet() if reads is None else reads


def _lr_ledger_paths():
    """Every ledger the land projection reads. The gate receipts ledger is the
    VERIFICATION half and does not exist until that lane lands, so it is probed
    tolerantly — the fingerprint picks it up the day it appears, and until then
    its absence is simply one more term."""
    from . import dispatches, landreq
    paths = [dispatches.ledger_path(), landreq.receipts_path()]
    try:
        from . import gate
        receipts_path = getattr(gate, "receipts_path", None)
        if callable(receipts_path):
            paths.append(receipts_path())
    except Exception:
        pass
    return paths


def _lr_input_path(label):
    """Where one labelled non-ledger input lives. Raises when it cannot be
    resolved, which `_serve` records as blind — an input whose location is
    unknown is not an input whose location is unchanged."""
    if label == "premise":
        from .premise import _chain
        return _chain._chain_path()
    if label == "attest":
        from . import dispatches
        return dispatches.attest_path()
    raise KeyError(label)


# HOW LONG AN UNMOVED FINGERPRINT MAY HOLD THE BOARD. Deliberately below
# `_LR_HARD_TTL_S`: a match that could hold until ANCIENT would put the next
# rebuild on the BLOCKING path, where the owner's poll waits the whole
# projection out. Under this cap the entry never leaves the STALE regime, so
# every rebuild stays in the background — and a change the fingerprint cannot
# see (a trunk that moved with no ledger write is the one to know about) is
# picked up within this window rather than never.
_LR_UNCHANGED_MAX_S = 300

# The labelled non-ledger inputs `_lr_input_path` can resolve. Named here so
# the fingerprint below covers the same file set the read-set records rather
# than a second, shorter list somebody has to remember to extend.
_LR_INPUT_LABELS = ("premise", "attest")


def _lr_fingerprint_paths():
    """Every FILE the land projection is computed from, as paths — no stat.

    THE IDENTITY IS TAKEN ONE LAYER OUT, in `web_cache`, and that split is the
    point rather than tidiness. This module is scanned for reads of a moving
    input taken AROUND the read-set, because every input the BODY consumes must
    enter the witness through `_LrReadSet`. The freshness question is not one
    of those: it is asked BEFORE any build, to decide whether to build at all,
    and a reading the body never made must never enter the body's witness. So
    the enumeration lives here, where the paths already do, and the stat lives
    with the cache that acts on it.

    Raising is the honest answer for an input whose LOCATION cannot be
    resolved: the caller answers "no identity" and rebuilds, which is the
    direction that costs work rather than the one that pins a board."""
    paths = list(_lr_ledger_paths())
    for label in _LR_INPUT_LABELS:
        paths.append(_lr_input_path(label))
    return paths


def _lr_newest_mtime():
    """Newest mtime across the ledgers, or None when none of them exist — the
    card's "newest ledger write Ns ago" term. This is a DISPLAY fact, and
    deliberately not a cache key; see _lr_build.

    THROUGH THE READ-SET, which is why the stats are in the witness at all: the
    body embeds this number, so the files behind it are inputs, and recording
    them here is the body's own reading rather than a second pass beside it."""
    reads = _lr_reads()
    newest = None
    for path in (reads.ledger_paths() or []):
        st = reads.stat("ledger", path)
        if not (isinstance(st, list) and len(st) == 5):
            continue
        seconds = st[3] / 1e9
        newest = seconds if newest is None else max(newest, seconds)
    return newest


def _lr_repo():
    """(gitdir, trunk_ref) through the open snapshot's read-set."""
    got = _lr_reads().repo()
    return (None, None) if not isinstance(got, list) else tuple(got)


def _lr_epoch(ts):
    """Epoch for a ledger stamp, or None. dispatches._age_s answers 0 for an
    unparseable stamp — i.e. "just now" — so a row whose stamp cannot be read
    would otherwise be the freshest thing in the closed-today list."""
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None



def _lr_window(ts, now):
    """"in" | "out" | "unusable" for one closure instant against the 24h window.

    THE WINDOW HAS TWO SIDES AND THE TEST ONLY HAD ONE. `now - ts <= 86400` is
    satisfied by every instant in the FUTURE as well — the subtraction goes
    negative and sails under the ceiling — so a row whose record says it closed
    in 2099 was counted, confidently, as "closed in the last 24h" and printed
    among the newest. A closure that has not happened yet is not recent; it is
    a broken clock or a broken record, and either way the instant is UNUSABLE.
    Unusable is answered as its own state so the caller COUNTS the row as
    closed-at-an-unknown-time rather than dropping it, which is the other half
    of this footer's history.

    ONE owner for both readings the caller has (the closure stamp and the
    entered_ts lower bound), because a guard applied to one of two identical
    comparisons is a guard the next reader walks around."""
    if ts is None:
        return "unusable"
    from . import landreq
    age = now - ts
    if age < -landreq.FUTURE_SKEW_S:
        return "unusable"
    return "in" if age <= _LR_CLOSED_WINDOW_S else "out"



def _lr_land_task(evidence):
    """The task number the CLOSE EVENT names, or None.

    THE NUMBER IS READ, NEVER JOINED. `helm task` keeps its own ledger and a
    join against it would be the second source this card was rebuilt to
    remove: the owner's rule is that a card renders from one read, so the task
    number has to arrive inside the row the read already produced. It does —
    the integrator writes it into the close evidence ("LAND 59 task/2362:
    composed on train 180 ...") — so this is an extraction from a field the
    projection already carries.

    ANCHORED AND BOUNDED. The pattern takes `task/2362`, `task 2362` and
    `task2362` and nothing else; three to five digits, because a land is
    numbered in the thousands and a bare year or a sha fragment is not a task.
    A close whose evidence names none answers None, which the card renders as
    NO TASK RECORDED rather than as a blank that reads like a missing render.
    """
    if not isinstance(evidence, str) or not evidence:
        return None
    m = _LR_LAND_TASK.search(evidence)
    return m.group(1) if m else None


def _lr_land_proof(lr):
    """(reached, how) for one landed close — READ OFF THE RECORD, not re-derived.

    THE CLOSE LADDER ALREADY PROVED THIS, at close time, against the trunk it
    names in the same event (`closing_trunk_ref` / `closing_trunk_sha`). That
    proof is the reason the row is closed at all, so re-asking git here would
    buy no new fact and would reintroduce exactly the second reading this card
    was rebuilt to delete — a live derivation beside a recorded one, free to
    disagree with it.

    `close_proof_mode` is the recorded mode. A row that carries one has a
    positive proof; a row that carries none answers None, which the card says
    out loud. There is deliberately no False: a close reason of `landed` IS the
    ledger asserting the land, and a card may not contradict the record it is
    rendering.
    """
    how = lr.get("close_proof_mode")
    if isinstance(how, str) and how:
        return True, how
    return None, None


def _lr_land_order(epoch, close_seq, position):
    """The sort key for "which land is newest", and there is exactly ONE.

    Both readers of that question use this: the card's rows, and any reader
    picking the newest land out of `helm lr list`'s rows (that listing groups by
    STAGE and dwell for display, which answers a different question — two
    comparators for one fact is how two surfaces come to disagree about one
    record).

    THREE TERMS, EACH FOR A DISTINCT FAILURE OF THE ONE ABOVE IT. The closure
    instant is the measurement; `close_seq` is the ledger's append index for the
    close event, which orders two closes the whole-second stamp cannot separate;
    and the row's own position in the projection is the last, stable resort so the
    list never reorders between two reads of one ledger. A row whose close
    position the record does not carry sorts BELOW every row that shares its
    second and does have one — an unknown position may not claim the newest
    place — and an unreadable stamp (epoch 0.0) sorts last of all.
    """
    seq = None if isinstance(close_seq, bool) \
        or not isinstance(close_seq, int) else close_seq
    return (-(epoch or 0.0), 1 if seq is None else -seq, -position)


def _lr_recent_lands(lrs=None, all_projects=False, unavailable=None):
    """RECENT LANDS — READ OFF THE SAME LEDGER `helm lr list` READS.

    THE INVERSION THIS REPLACES. A trunk walk for commits whose subject begins
    `fold:` matches every land only while a fold mints its own commit. Under the
    ff-only landing discipline trunk advances by fast-forward and mints no fold
    commit, so that grammar cannot see a real land at all — it reports weeks-old
    rows on a day full of lands, and the honest version of it can only add a
    disclaimer saying lands are missing from the list. A card that explains why
    it cannot answer is still not answering.

    THE OWNER'S RULE, VERBATIM: if it is there it does not go stale, and
    anything on the homepage should be actually useful, otherwise why pipe it
    there.

    SO THE ROWS COME FROM THE LEDGER'S OWN LANDED CLOSES. A land is recorded
    as `close --reason landed` on the land-request row, which is the event the
    integrator writes as the land happens, and it is what `helm lr list`
    projects. Reading it here means the card and the command line cannot
    disagree about one record, which is the owner's rule stated as a
    mechanism rather than as a habit.

    ONE SOURCE, AND THAT IS THE WHOLE POINT. `lrs` is the mapping
    `landreq.project_raw` already built for this body, handed in rather than
    re-walked: this leg adds no read, no second clock and no second scope
    resolution. It also means the leg is no longer independent of the
    projection — when the dispatch ledger is UNKNOWN these rows are UNKNOWN
    too, and `unavailable` carries the projection's own reason. That is a
    deliberate trade the owner's rule forces: a card sourced elsewhere so it
    could survive the ledger being unreadable is a card that answers a
    different question from the command line, which is the defect.

    NO WINDOW. The old envelope carried a 24h window it could not honestly
    count under ff-only. The card shows the newest lands whenever they
    happened and every row carries its own age, so "the newest land was three
    days ago" is a TRUE answer the owner can act on rather than an empty list
    he has to interpret.

    Answers {"rows", "total", "rows_truncated", "source", "unavailable"}.
    `total` is a real count now — the ledger holds every landed close, so the
    cap above it is a display budget over a MEASURED population rather than a
    lower bound wearing a total.
    """
    from . import landreq
    out = {"rows": [], "total": None, "rows_truncated": False,
           "source": "helm lr list", "unavailable": None}
    if unavailable:
        # THE PROJECTION'S OWN REASON, RELAYED VERBATIM. Writing a sentence of
        # our own here would let the card name a cause the record never gave.
        return dict(out, unavailable=str(unavailable))
    if not isinstance(lrs, dict):
        return dict(out, unavailable="the land-request projection was not "
                    "supplied to the lands reading, so what has landed is "
                    "UNKNOWN — not zero lands")
    landed = []
    # THE CLOSE ORDER COMES OFF THE LEDGER NOW, and that is the whole of the
    # tie-break. Two lands closed inside the same second carry the SAME
    # whole-second stamp (`_lr_epoch` parses seconds), so the stamps cannot order
    # them; the projection's own iteration order is the order rows were OPENED,
    # which is a different question — open A then B, close B then A, and a reader
    # breaking the tie on row order puts the OLDER close on top under a heading
    # that says newest first. `close_seq` is the ledger's append index for the
    # close event itself (`dispatches._close_position`), relayed by the canonical
    # row, so "which of these two closed last" is answered by the record.
    for position, lr in enumerate(lrs.values()):
        if not isinstance(lr, dict):
            continue
        if lr.get("close_reason") != "landed":
            continue
        # SAME SCOPE AS EVERY OTHER LIST IN THIS BODY (task/974): a foreign
        # project's land is not this board's land to announce.
        if not (all_projects or landreq._this_boards_row(lr)):
            continue
        # THE CLOSURE INSTANT, AND AN UNREADABLE ONE STAYS UNREADABLE. The
        # projection reports two ways a closure stamp fails: `closed_ts` None
        # with `closed_ts_unreadable` (the ledger holds something that is not a
        # timestamp) and `closed_ts_impossible` (it holds a well-formed instant
        # that cannot be a closure). `entered_ts` is a DIFFERENT event — the
        # stage the row entered — so substituting it there answered a question
        # nobody measured: the card printed a confident date and an age computed
        # from it, over a closure the record cannot date. The substitution is
        # legitimate only while the stamp is merely ABSENT, which for a LANDED
        # row is the ordinary case (`entered_ts` IS the close stamp there).
        unreadable = bool(lr.get("closed_ts_unreadable")
                          or lr.get("closed_ts_impossible"))
        ts = None if unreadable else (lr.get("closed_ts") or lr.get("entered_ts"))
        reached, how = _lr_land_proof(lr)
        landed.append((_lr_epoch(ts) or 0.0, lr.get("close_seq"), position, {
            "lane": lr.get("lane"),
            "reviewed_tip": lr.get("review_sha") or lr.get("reviewed_tip") or "",
            # THE TASK THE LAND SERVED, read out of the close evidence this
            # same event carries — the number the owner actually recognises.
            "task": _lr_land_task(lr.get("close_evidence")),
            # THE VERIFICATION TOKEN, copied from the row. A land with no gate
            # token renders UNVERIFIED; an absent field never defaults to
            # verified (the card law `card()` already states for this field).
            #
            # THE LANDING REVIEW'S RECEIPT IS THE ONE THIS ROW WAS VERIFIED BY,
            # and it is a different field from `gate`. A build row's `gate` is
            # the receipt its own APPROVE bound, which a build-landed row does
            # not carry; the receipt the close ladder recorded and the command
            # line names is `landing_review_gate`. Reading only `gate` therefore
            # dropped a receipt the ledger HELD and said "no token recorded"
            # over a verified land — the one sentence this field exists to keep
            # from being wrong. `gate` stays as the fallback because a row
            # closed as landed outside the landing-review ladder carries it.
            "gate": lr.get("landing_review_gate") or lr.get("gate") or "",
            "ts": ts,
            # WHY THE INSTANT IS MISSING, when it is missing for a reason. An
            # absent stamp and a corrupt one are different facts with different
            # repairs, and the card says two different sentences for them.
            "ts_unreadable": unreadable,
            "trunk_ref": lr.get("closing_trunk_ref"),
            "trunk_sha": lr.get("closing_trunk_sha")
            or lr.get("landing_trunk_sha"),
            # `on_trunk` KEEPS ITS NAME AND ITS HISTORICAL SEMANTICS — the
            # field has references across this tree and the question it
            # answers has not changed: did the reviewed content reach trunk.
            # What changed is WHERE the answer comes from (the recorded close
            # proof, not a live re-derivation beside it).
            "on_trunk": reached, "how": how,
            "chain_root": lr.get("chain_root"),
            # WHOSE LAND THIS IS, copied from the row the same way the in-flight
            # card carries it: None for the board's own project, the owning
            # project for a foreign row (only an all-projects body has those),
            # and the unresolved mark for a repository no project claims.
            "foreign_project": lr.get("foreign_project"),
            "project_unresolved": bool(lr.get("project_unresolved")),
        }))
    # NEWEST FIRST, and rows whose closure instant is unreadable sort last
    # rather than being dropped: the row is real, only its stamp is not, and
    # the card prints "age unknown" for it.
    landed.sort(key=lambda row: _lr_land_order(row[0], row[1], row[2]))
    out["total"] = len(landed)
    out["rows"] = [row for _ts, _seq, _position, row in landed[:_LR_LANDS_CAP]]
    # A DISPLAY BUDGET THAT DISCLOSES ITSELF, over a population it MEASURED.
    out["rows_truncated"] = len(landed) > _LR_LANDS_CAP
    return out



def _lr_native_chain():
    """The local attest-chain summary for the dashboard's RECORD row: how many
    records, whether the whole chain VERIFIES, and the head index. A summary of
    /api/ledger/native's chain leg rather than a second poll of it — the
    dashboard reads /api/lr anyway, and verify_chain() over ~100 records is
    hashing, not git. Failure is a NAMED unavailable, never {count: 0}: an
    unreadable chain and an empty one are different facts.

    THE CHAIN FILE'S CONTENT IS TAKEN THROUGH THE READ-SET, not just its inode,
    because this summary is an answer ABOUT that file and the file is an input
    the ledgers cannot see: a new premise record changes what this row says with
    no ledger write anywhere. A body embedding an input the witness ignores is a
    silent staleness channel, and independence from the ledgers is exactly what
    makes this one the input to miss.

    ONE READING SERVES ALL THREE FIELDS. The count, the head index and the
    verification used to come from TWO separate reads of an append-only file —
    `chain_records()` here and `chain_records()` again inside `verify_chain()` —
    so a record appended between them left this card reporting N records and
    "chain verified (N+1 records)". `premise_chain()` serves the list once and
    `verify_chain` is handed it, which makes the three fields one measurement by
    construction rather than by how fast the two calls happen to run.

    ABSENT AND UNREADABLE STAY DIFFERENT FACTS, AND THE ONE READ NOW CARRIES
    BOTH. `premise.chain_records` swallows OSError and answers [] — the right
    call for a torn append, the wrong one to report, because it makes a chmod'd
    chain render as an empty one. This used to be recovered from a SEPARATE
    `identity("premise")` stat, which is a second syscall by name and therefore
    a second chance to reach a different file than the one whose bytes were
    read. `premise_chain()` answers None for every failure of the instrument and
    [] only for a chain file that is measurably not there, so the distinction
    comes out of the reading itself rather than from a stat taken beside it."""
    from . import premise
    reads = _lr_reads()
    recs = reads.premise_chain()
    if recs is None:
        return {"count": None, "verified": None, "detail": "",
                "head_index": None,
                "unavailable": "the local chain file could not be read — "
                               "chain UNKNOWN"}
    try:
        verified, detail = premise.verify_chain(recs) if recs else (True, "")
        head = recs[-1] if recs else {}
        return {"count": len(recs), "verified": bool(verified),
                "detail": "" if verified else str(detail),
                "head_index": head.get("chain_index"), "unavailable": None}
    except Exception as e:
        return {"count": None, "verified": None, "detail": "",
                "head_index": None,
                "unavailable": "the local chain read failed (%s) — chain "
                               "UNKNOWN" % type(e).__name__}
del _web
