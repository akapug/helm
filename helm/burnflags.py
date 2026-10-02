#!/usr/bin/env python3
"""helm burn flags — ONE fire-danger reading per model family, in one snapshot.

THE FAILURE THIS MODULE PREVENTS. Every agent in the fleet does the capacity
arithmetic itself, from whatever it can reach, and gets a different answer:
one seat holds another project off a family by hand, a second writes a family
off on a dark reading it never re-measures, and a third fans out into a wall.
There is no single signal that says how much may run right now, so the answer
is re-derived per seat, per turn, wrong.

THIS IS `codexbudget` WIDENED, NOT A FIFTH SIGNAL. `codexbudget.verdict` was
already a burn flag for one family on one axis: four decisions, a tunable
ceiling, per-window reset instants, a redacted snapshot with a stated freshness
bound, two consumers that refuse or warn on it, and a change-latched room
notice. What this module adds is N families, four axes, and a read verb; what
it DELETES is the second announcer (`codexbudget.watch_notice`) and the second
staleness constant. It inherits the codex CEILING and nothing else:
`codexbudget.verdict` is NOT called here. Its reading of `longest_pct` per row
cannot see a short window at its wall under a green long one, so the money
axis is this module's own closed table — every window of an account folds to
one account state, and the set of account states folds to one family verdict
(`_ACCOUNT_STATE_TABLE`, `_FAMILY_STATE_TABLE`). `verdict` survives only as
the codex pool's persisted room latch, which proxywatch computes beside this
fold.

THE FOUR AXES, because the four repairs differ: MONEY (wait for a reset),
REACH (bounce a proxy), POLICY (change the model mix), DECLARED (read what the
owner said). A single folded colour that hid WHICH question it answered would
be the same defect two colour scales in this tree already have.

LAWS, each one a measured failure somewhere:

  AN UNREAD ACCOUNT NEVER CREATES PRESSURE OR ERASES PRESSURE ALREADY
  MEASURED. It caps permission: GREEN and a spendability YES become
  unreachable and the repair is named, while a direct measured wall remains
  binding on that same account. Half of one family's accounts read
  `reauth-needed` because HELM'S OWN
  token copy expired, not because the vendor said no; folding that absence as
  new pressure rations the fleet on our broken reader. Folding it as relief
  routes work into a wall. Absence is not a measured contradiction either way.

  GREY IS NOT GREEN AND NOT A REFUSAL. A family nothing measures is not
  walled, it is unmeasured: it renders GREY, behaves as YELLOW for capacity,
  never refuses, and never outranks a measured equal-or-better family.

  NO IDENTITY REACHES THE SNAPSHOT. `_money_rows` projects every input row to
  (state, longest_pct, windows) BEFORE anything reads it, so an email cannot
  reach the file, the board row, the rendered cause or the JSON — the codex
  budget's own renderers name accounts by email and this module must not
  inherit that. Identity stays in `helm creds`.

  A DECLARED COLOUR IS WORSE-ONLY AND NEVER RENDERS AS MEASURED. An owner
  declaration may restrict; it may not make a measured wall look passable.
  On a family nothing measures the declaration IS the answer, because GREY is
  off the ordering, and it carries `provenance: owner-declared` wherever it
  is rendered.

  A LOCAL FAMILY READS GREEN WHILE THE SEAT THAT RUNS ITS HARDWARE CERTIFIES
  IT. The owner's ruling, the certifier's name given by its role:
  "obvs local models should always be green burnflag as long as [the Claude
  seat on the GPU host] certifies them as running on local hardware
  optimally". A local family is served from the operator's own GPU: every
  pool row it lists names its endpoint by `base_url_from` in the catalog
  (`local_families`), so it spends no vendor money, and a GREY for its
  missing money reader rationed it as though it did. `helm burn
  certify-local` records the certification for at most a day, and ONLY ITS
  OPERATOR MAKES ONE: the family's declared operator seat, acting from its
  own roster-bound session. A declared name alone is a value any process can
  export, so it certifies nothing. Each record carries the key of the session
  that made it, and the fold honours a record only while that key still
  binds to the operator on the roster. A record it cannot tie to the
  operator (a bare `by`, a row typed with no key the roster binds to the
  operator, a key rebound elsewhere, a certifier that is not the operator)
  reads GREY `money:certification-unverifiable`, never GREEN. THE KEY IS
  NOT A SECRET, so this check is exactly as strong as the roster: the sid8
  sits in this very file (a revoked row keeps it) and on the verb's own
  output, so a process that can write the file can replay the operator's
  last record with a later `until` and read GREEN. That process can equally
  rebind a session on the roster or write the snapshot every consumer reads,
  so the replay is the same-user trust boundary of helm's own state, stated
  here and not closed by the fold.
  The fold reads a verified, fresh record as the money axis of a local family
  that has no reader: GREEN until the certification's own `until`, stamped
  `provenance: certified-local`, and GREY again, exactly as with no
  certification, the moment it lapses or is revoked. THIS IS NOT A DECLARED
  COLOUR, so the worse-only law above does not bind it: that law is about an
  owner declaration, which is intent with no measurement behind it. A
  certification is an attestation by the seat that runs the hardware, made
  only after its own measured checks of the server pass. It is still NOT A
  MEASUREMENT: the reviewer budget conjunct reads it UNKNOWN, and a LIVE
  READING THAT CONTRADICTS IT WINS, so while the family's own upstream
  record reads anything but HEALTHY the certification is withdrawn. It
  answers only where nothing else measures money, a non-local family never
  reads one whatever the file says, an unreadable file reads GREY naming the
  file, and REACH IS UNTOUCHED: an outage on the local server is still RED on
  reach and RED overall.

  `expires_at` IS ANCHORED TO THE PRODUCER'S OWN INSTANT. The clock at read
  time never enters it, so folding one reading at two different `now` values
  produces byte-identical expiry fields.

FAMILY VOCABULARY, one derivation and not a fourth list: the seat catalog's
families plus `anthropic`, which has no proxy seat because it is the native
credential. FABLE, OPUS, SONNET AND HAIKU ARE NOT FAMILIES — they are models
inside `anthropic` sharing one credential pool, and the scoped weekly window
rides the same account row as the account-wide one.
"""
import json
import math
import os
import time

from . import home
from . import pk

# THE ORDERING. GREY is deliberately absent: it is off the scale (see the law
# above), so `_RANK` cannot be asked about it and a caller that tries gets a
# KeyError rather than a silent seventh position.
RED, ORANGE, YELLOW, GREEN, GREY = "RED", "ORANGE", "YELLOW", "GREEN", "GREY"
_RANK = {GREEN: 0, YELLOW: 1, ORANGE: 2, RED: 3}
COLOURS = (GREEN, YELLOW, ORANGE, RED, GREY)

# THE OWNER'S OWN SENTENCES. Consumers read `behaviour` off the flag and never
# re-derive it, because a second copy of these words is a second policy.
BEHAVIOUR = {
    # GREEN IS NEVER LICENCE TO SPECULATE. It means more lanes may open for
    # work that is already built up and ready, across projects that are in go
    # mode. Tokens spent on a guess are waste at every colour.
    GREEN: {"say": "open more lanes for work that is already built up; never speculative",
            "capacity": 4, "delegate_factor": 1.0},
    YELLOW: {"say": "normal work, no extra lanes",
             "capacity": 2, "delegate_factor": 0.5},
    ORANGE: {"say": "critical path only, one delegate at a time, "
                    "prefer another family",
             "capacity": 1, "delegate_factor": 0.25},
    RED: {"say": "start nothing new on this family; finish or park what is "
                 "running", "capacity": 0, "delegate_factor": 0.0},
    GREY: {"say": "not measured; treated as yellow and said out loud",
           "capacity": 2, "delegate_factor": 0.5},
}
_SPEND_DOWN_SAY = ("keep working at a steady pace until the vendor refuses; "
                   "do not route critical-path work here")

SNAPSHOT_NAME = "burn-flags.json"
DECLARATIONS_NAME = "burn-declarations.json"
LOCAL_CERTIFICATIONS_NAME = "burn-local-certifications.json"
# The provenance a certified local family's money axis carries, so no surface
# renders it as a vendor measurement.
CERTIFIED_LOCAL = "certified-local"
# THE LONGEST CERTIFICATION, from `certified_at` to `until`. A certification
# is an attestation about a server that can degrade at any moment, so it is
# renewed, never held: the operator seat renews it from its own session two
# hours at a time, and a day is the most any one attestation may claim.
CERTIFICATION_CEILING_S = 86400
# ONE RENEWAL PERIOD. A certification lapsed longer than this and never
# revoked means the renewals stopped, which doctor says out loud.
CERTIFICATION_LAPSE_WARN_S = 7200
# THE CAUSE OF A RECORD THE FOLD CANNOT TIE TO THE FAMILY'S OPERATOR SEAT: no
# operator declared, a certifier that is not the operator, no session key, or
# a key the roster no longer binds to the operator. GREY, never GREEN, and
# never the no-reader words, because this GREY has a repair (the operator
# certifies again from its own session) and a forgery looks exactly like it.
CERTIFICATION_UNVERIFIABLE = "money:certification-unverifiable"
# THE CATALOG KEY that names, per local family, the local-names key holding
# the seat that runs its hardware (`local_operator`).
OPERATOR_SEAT_FROM = "operator_seat_from"
# A fold this reader does not know reads as ABSENT, never as a row with
# missing fields: a consumer that pattern-matched a future shape would answer
# from fields that mean something else.
FOLD_VERSION = 1

# The room tag survives the announcer swap. One tag, one channel, whatever
# family crossed.
NOTICE_TAG = "FAMILY-BUDGET-LOW"

# THE POLICY AXIS, and both numbers come from the owner's own overrun episode
# measured on the same instrument in the same units: the lowest scoped/account
# ratio of the four accounts that were over, and a margin under the lowest
# absolute weekly burn any of them carried.
#
# THE MINIMUM-BURN GATE IS LOAD-BEARING. A ratio alone fires on the first day
# of every week: two percent scoped against one percent account-wide is a
# ratio of 2.0 with essentially no usage behind it.
#
# HONEST LIMIT, stated at the constant: the two windows have different
# denominators, so this is a RELATIVE-PRESSURE PROXY for the owner's rule
# about which model burns the credential, not the rule itself. It is stamped
# `derived`, never `measured`, and it needs recalibration whenever the scoped
# cap moves. `helm attribute` is the instrument that should eventually own it.
POLICY_RATIO = 1.19
POLICY_MIN_WEEKLY = 0.30
POLICY_SCOPED_LABEL = "7d-fable"
POLICY_ACCOUNT_LABEL = "7d"

# The one family with no proxy seat: it is the native credential rather than a
# sidecar, so it is named here instead of being read from the seat catalog.
NATIVE_FAMILY = "anthropic"

# Families with a money reader TODAY. The two inherited readers are native;
# every generic reader is derived from the catalog's direct or per-pool
# declaration. A fourth hand-kept family list would drift on the next reader.
def money_readers(table=None):
    from . import moneyread
    return tuple(sorted({NATIVE_FAMILY, "codex"} |
                        set(moneyread.catalog_families(table))))


MONEY_READERS = money_readers()

# THE POOL READING, and the two numbers it needs.
#
# WHY IT IS NOT THE BEST ACCOUNT. Where ONE DOOR ROTATES onto whichever
# account still has headroom — every family with a proxy seat — the loosest
# account IS the account the work lands on, so the best headroom is the whole
# answer. The native credential has NO PROXY SEAT (the law at the top of this
# module): a seat spends the credential its own home holds, so four accounts
# near the wall and one loose one is four seats that cannot work, and reading
# the loose one as the family's colour says "fan out" to every one of them.
#
# CARRY_PCT IS THE HALF-WINDOW MARK, not a tuned knob and not the warning
# band: an account that has spent more than half its worst budget window has
# less left than it has already burned, so it cannot be the one that carries base
# load while the others finish. The owner's own reading is the calibration —
# accounts in the sixties and seventies "need optimized management", an
# account at a quarter of its week does not.
CARRY_PCT = 50.0


def carry_soon_s():
    """How soon a reset must land to carry base load by itself — ONE VENDOR
    SESSION WINDOW, IMPORTED rather than chosen.

    The owner's rule is "no resets coming up immediately that could carry
    baseload while others are finished", and the span that makes a reset
    immediate is the one the vendor meters work in: a wall that lifts inside
    the session window the fleet is already working in is a wait, not a wall.
    Minting a number here would be a second opinion about the vendor's own
    window length, which `providers` already holds."""
    from . import providers
    return int(providers.NativeQuotaProvider.SESSION_WINDOW_H * 3600)


def families():
    """Every family a flag is minted for — the seat facade's, plus the native
    credential. ONE derivation: a hand-written list here would be a fourth
    spelling of a set three other modules already agree on, and the facade is
    the door the tree requires for that set rather than the implementation
    module behind it."""
    from . import seat
    return tuple(sorted(set(seat.FAMILIES) | {NATIVE_FAMILY}))


def ceiling_pct():
    """The soft ceiling on the longest window — the codex knob, unchanged and
    not copied."""
    from . import codexbudget
    return codexbudget.ceiling_pct()


def orange_pct(ceiling=None):
    """The warning band, DERIVED FROM THE CEILING rather than chosen: twice
    the distance from full that the wall sits at. One knob moves the wall and
    the warning together, which is the whole reason this is a function."""
    ceiling = ceiling_pct() if ceiling is None else ceiling
    return 100.0 - 2.0 * (100.0 - ceiling)


def max_age_s():
    """The staleness bound, IMPORTED. proxywatch states the law for its own
    pair of consumers — one predicate, two consumers — and minting a third
    staleness number inside the subsystem whose second one this lane deletes
    would refute the lane."""
    from . import proxywatch
    return proxywatch.UPSTREAM_CACHE_FRESH_S


def snapshot_path():
    """Beside the codex pool snapshot, and for its stated reason: anchored on
    the helm home rather than the cache root, so one env var isolates it and
    no test can be made to read this machine's live fleet."""
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", SNAPSHOT_NAME)


def declarations_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        DECLARATIONS_NAME)


def local_certifications_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        LOCAL_CERTIFICATIONS_NAME)


def local_families(table=None):
    """Every family served from the operator's own GPU -> a sorted tuple.

    THE CATALOG'S OWN SPELLING: a pool row on the operator's box names its
    endpoint by `base_url_from`, a key in the operator's endpoints file, and
    never by a host (`seat_catalog.pool_base_url`). A family is local when its
    DEFAULT row exists and EVERY row it lists is such a row: one paid
    alternate beside a local default is a door to vendor money, so that
    family is not free. The declaration is read and the endpoint is never
    resolved, so the fold stays file-free."""
    from . import seat
    table = seat.FAMILIES if table is None else table
    out = []
    for family, fam in table.items():
        rows = fam.get("pool_providers") if isinstance(fam, dict) else None
        if isinstance(rows, dict) and rows.get(fam.get("pool_default")) \
                and all(isinstance(row, dict) and row.get("base_url_from")
                        for row in rows.values()):
            out.append(family)
    return tuple(sorted(out))


def local_operator(family, table=None):
    """The seat that runs LOCAL `family`'s hardware -> its name, or None.

    DATA, SPELLED THE WAY THE FAMILY'S ENDPOINT IS: the catalog entry names a
    local-names key by `operator_seat_from`, and this host's local-names file
    (`localnames`) holds the seat under that key. A seat name is this host's
    own, so the source carries none, exactly as it carries no host for
    `base_url_from`. Absent at either end is None, and None is NO OPERATOR:
    nobody can certify that family."""
    from . import localnames, seat
    fam = (seat.FAMILIES if table is None else table).get(family)
    key = fam.get(OPERATOR_SEAT_FROM) if isinstance(fam, dict) else None
    return localnames.value(key) if key in localnames.KEYS else None


def _session_key(session):
    """The key a certification stamps for the session that made it: the sid8
    cursor key (`seats_cursor._sid8`), never the id itself, because a whole
    session id is a bearer value (`tasks.public_row`)."""
    from .seats_cursor import _sid8
    return _sid8(session) if isinstance(session, str) and session else None


def _session_bindings(keys):
    """({key: the seat the roster binds that session key to now, or None},
    None), or ({}, why) when the roster cannot be read. Never raises.

    THE ROSTER'S OWN RESOLUTION, applied to every roster session whose sid8
    is the key: a current binding first, then a remembered one
    (`seats_roster.seat_for_session_in`). A key therefore binds to a seat
    exactly when a process presenting that session now would be admitted as
    that seat, and it stays bound while the seat moves on to a new session of
    its own. Two seats behind one key is no binding."""
    keys = {k for k in keys if isinstance(k, str) and k}
    if not keys:
        return {}, None
    try:
        from . import seats_roster
        from .seats_cursor import _sid8
        rows, failed = seats_roster.roster_checked()
        if failed:
            return {}, "the roster could not be read"
        index, _holders = seats_roster.roster_indexes(rows)
        found = {}
        for sid in index:
            key = _sid8(sid)
            if key in keys:
                seat = seats_roster.seat_for_session_in(index, sid)
                if seat:
                    found.setdefault(key, set()).add(str(seat))
    except Exception as exc:                # noqa: BLE001 — never raises
        return {}, "the roster could not be read (%s)" % type(exc).__name__
    return {k: (next(iter(found[k])) if len(found.get(k, ())) == 1 else None)
            for k in keys}, None


# ------------------------------------------------------------------- the axes

def _axis(name, colour, cause, cause_id, expires_at=None, expires_kind=None,
          expires_source=None, measured_at=None, **extra):
    rec = {"axis": name, "colour": colour, "cause": cause,
           "cause_id": cause_id, "expires_at": expires_at,
           "expires_kind": expires_kind, "expires_source": expires_source,
           "measured_at": measured_at}
    rec.update(extra)
    return rec


def _money_rows(rows):
    """Input rows -> the ONLY shape the rest of this module ever sees.

    THE REDACTION IS STRUCTURAL, NOT A RENDERING RULE. The pooled budget rows
    carry email, account id and filename; a cause built from them puts an
    identity in the board, the snapshot and every rendered line. Projecting
    here means no later renderer CAN leak one, however it is written."""
    out = []
    for row in rows or ():
        pct = row.get("longest_pct")
        raw_source = row.get("source")
        row_source = (raw_source if raw_source in ("measured", "derived")
                      else "measured" if raw_source is None else "unknown")
        windows = [{"label": w.get("label"),
                    "used_percent": w.get("used_percent"),
                    "reset_at": w.get("reset_at"),
                    "seconds": w.get("seconds"),
                    "unit": w.get("unit"),
                    "kind": w.get("kind"),
                    "source": (w.get("source") if w.get("source") in
                               ("measured", "derived") else row_source
                               if w.get("source") is None else "unknown")}
                   for w in (row.get("windows") or ())]
        source = ("unknown" if row_source == "unknown"
                  or any(w["source"] == "unknown" for w in windows) else
                  "derived" if row_source == "derived"
                  or any(w["source"] == "derived" for w in windows)
                  else "measured")
        measured = [w for w in windows
                    if isinstance(w.get("used_percent"), (int, float))]
        # A stale/unread row retains its windows for diagnosis, but its explicit
        # missing percentage is the authority boundary. Reconstructing a number
        # from those stale windows would turn the unread account back into a
        # current reading. Readable producers carry a percentage and only those
        # rows are widened from longest-window to worst-window pressure.
        if pct is not None and measured:
            pct = max(w["used_percent"] for w in measured)
        state = "ok" if row.get("state") == "near" else row.get("state")
        out.append({"state": state, "longest_pct": pct,
                    "source": source, "windows": windows})
    return out


def _binding_window(row):
    """The worst measured window that carries this row's budget verdict.

    S1 folds every window, not only the longest. The instant that can change a
    warning/wall is therefore the reset of the highest-utilization window; a
    tie takes the longer window so an equally bad short reset cannot promise
    recovery while the longer wall remains."""
    windows = [w for w in (row.get("windows") or ())
               if isinstance(w.get("used_percent"), (int, float))]
    if not windows:
        return None
    return max(windows, key=lambda w: (w["used_percent"], w.get("seconds") or 0))


def _wall_reset_fields(wall, credits=None, measured_at=None, derived=False):
    """Expiry for an any-window wall, preserving reset-credit precedence."""
    if credit_count(credits) and measured_at:
        return _reset_fields((), credits=credits, measured_at=measured_at)
    if not wall:
        return {"expires_at": None, "expires_kind": "none",
                "expires_source": ("derived wall reset unknown" if derived else
                                   "money rows: wall reset unknown")}
    label = wall.get("label")
    kind = ("weekly-reset" if (wall.get("seconds") or 0) >= 6 * 86400
            else "session-reset")
    return {"expires_at": wall.get("reset_at"),
            "expires_kind": kind,
            "expires_source": "%s: %s reset" % (
                "derived money rows" if derived else "budget rows",
                label or "binding window")}


def _band_reset(rows):
    """(instant, kind, label) for the soonest BINDING reset among these rows."""
    best = None
    for row in rows:
        window = _binding_window(row)
        if window and window.get("reset_at") \
                and (best is None or window["reset_at"] < best["reset_at"]):
            best = window
    if not best:
        return None, "none", None
    kind = ("weekly-reset" if (best.get("seconds") or 0) >= 6 * 86400
            else "session-reset")
    return best["reset_at"], kind, best.get("label")


ACCOUNT_WALLED = "WALLED"
ACCOUNT_CAPPED = "CAPPED"
ACCOUNT_UNREAD = "UNREAD"
ACCOUNT_OPEN = "OPEN"

# (measured authority, completeness) -> the one closed account state. Invalid
# shapes are deliberately absent: callers receive UNREAD with an explicit
# outside-table reason rather than having malformed evidence guessed into a
# state. A measured wall/cap has both complete and incomplete rows because that
# direct evidence dominates unread or derived sibling windows on the same
# account; incompleteness can never establish pressure by itself.
_ACCOUNT_STATE_TABLE = {
    ("wall", "complete"): ACCOUNT_WALLED,
    ("wall", "incomplete"): ACCOUNT_WALLED,
    ("cap", "complete"): ACCOUNT_CAPPED,
    ("cap", "incomplete"): ACCOUNT_CAPPED,
    ("none", "incomplete"): ACCOUNT_UNREAD,
    ("none", "complete"): ACCOUNT_OPEN,
}

# (any WALLED/CAPPED account, the unread class, any OPEN account) -> the
# family-level branch. The unread class is "none" with no UNREAD account,
# "estimated" when some UNREAD account's own incomplete or derived numbers
# reach the warning band, and "unread" otherwise. Every closed-table
# combination is named; anything else is UNKNOWN.
#
# ESTIMATED IS A ROW, NOT A SPECIAL CASE (task/2935, the integrator's ruling).
# With nothing measured walled, capped or open, an estimate in the warning
# band is ORANGE for the reason "estimated": spending stays UNKNOWN, dispatch
# admits, and the estimated account is never counted as headroom. Beside a
# measured wall it is one more unread account (partial); beside an open
# account the open accounts answer.
_FAMILY_STATE_TABLE = {
    (False, "none", False): "unknown",
    (False, "none", True): "open",
    (False, "unread", False): "unknown",
    (False, "unread", True): "open",
    (False, "estimated", False): "estimated",
    (False, "estimated", True): "open",
    (True, "none", False): "blocked",
    (True, "none", True): "open",
    (True, "unread", False): "partial",
    (True, "unread", True): "open",
    (True, "estimated", False): "partial",
    (True, "estimated", True): "open",
}


# THE RUNWAY STEP (task/2984): (the runway verdict, the money axis colour) ->
# the colour after the step. `codexpace` reads the RATE a level cannot see —
# the fleet's hours of supply against the next Pro reset — and hands the fold
# a verdict; this table is the only place that verdict moves a colour. Every
# verdict and every colour is named, so no combination is decided by a
# fall-through.
#
#   short    runway under the horizon: UP one colour.
#   use-it   runway well above the horizon while an account will strand part
#            of its week: DOWN one colour ("use it").
#   even / unknown: no step.
#
# A PROJECTION NEVER REACHES RED, and never leaves it. RED refuses the
# family's work; the fold admits RED only on a measured wall or cap, and a
# straight-line projection is neither. So a short runway holds ORANGE at
# ORANGE, and a measured RED is not stepped down by a runway either.
RUNWAY_SHORT, RUNWAY_USE_IT = "short", "use-it"
RUNWAY_EVEN, RUNWAY_UNKNOWN = "even", "unknown"
_RUNWAY_STEP_TABLE = {
    (RUNWAY_SHORT, GREEN): YELLOW,
    (RUNWAY_SHORT, YELLOW): ORANGE,
    (RUNWAY_SHORT, ORANGE): ORANGE,
    (RUNWAY_SHORT, RED): RED,
    (RUNWAY_SHORT, GREY): GREY,
    (RUNWAY_USE_IT, GREEN): GREEN,
    (RUNWAY_USE_IT, YELLOW): GREEN,
    (RUNWAY_USE_IT, ORANGE): YELLOW,
    (RUNWAY_USE_IT, RED): RED,
    (RUNWAY_USE_IT, GREY): GREY,
}
_RUNWAY_STEP_TABLE.update({(verdict, colour): colour
                           for verdict in (RUNWAY_EVEN, RUNWAY_UNKNOWN)
                           for colour in COLOURS})


def _runway_step(axis, runway):
    """The money axis after the runway step -> an axis record.

    A missing runway, or a verdict outside the table, is no step. GREEN stays
    out of reach while any account is unread: the module's first law holds
    for a step exactly as it holds for a reading. The stepped axis names the
    runway first and keeps the reading it stepped from."""
    if not isinstance(axis, dict) or not isinstance(runway, dict):
        return axis
    verdict = runway.get("verdict")
    colour = axis.get("colour")
    to = _RUNWAY_STEP_TABLE.get((verdict, colour))
    if to is None or to == colour or (to == GREEN
                                      and axis.get("capped_by_coverage")):
        return axis
    stepped = dict(axis, colour=to, cause_id="money:runway-%s" % verdict,
                   stepped_from=colour,
                   cause="%s — the money axis steps %s to %s; the reading it "
                         "steps from: %s" % (runway.get("cause"), colour, to,
                                             axis.get("cause")))
    if verdict == RUNWAY_SHORT and runway.get("horizon_at"):
        stepped.update(expires_at=runway["horizon_at"],
                       expires_kind="weekly-reset",
                       expires_source="codex runway: the horizon reset")
    return stepped


def _pool_pace_step(axis, pool):
    """The money axis after the pool's weekly pace joins it -> an axis record
    (task/3880).

    The pool is a WEEKLY reading, so it folds into the family colour here,
    the ONE place every consumer of the colour reads it: the axis becomes the
    WORSE of the 5h reading and the pool's pace. An EASE (ORANGE) pool holds a
    GREEN 5h at ORANGE; the pace NEVER lifts a reading, so a FASTER (GREEN)
    pool leaves a measured wall alone. An UNREADABLE (GREY) pace touches
    nothing: GREY is off the ordering, so the module's first law holds for a
    pace exactly as it holds for a reading. The stepped axis names the pool
    pace first and keeps the reading it stepped from."""
    if not isinstance(axis, dict) or not isinstance(pool, dict):
        return axis
    colour = axis.get("colour")
    pace = pool.get("colour")
    if colour not in _RANK or pace not in _RANK:
        return axis
    to = _worse(colour, pace)
    if to == colour:
        return axis
    stepped = dict(axis, colour=to, cause_id="money:pool-pace-%s" % to,
                   stepped_from=colour,
                   cause="%s — the money axis steps %s to %s; the reading it "
                         "steps from: %s"
                         % (pool.get("advice") or "the pool's weekly pace",
                            colour, to, axis.get("cause")))
    if to == pace and pool.get("runout_at"):
        stepped.update(expires_at=pool["runout_at"],
                       expires_kind="weekly-reset",
                       expires_source="the pool's weekly pace: the run-out")
    return stepped


def _account_binding(windows, threshold):
    """Latest reset needed for one account to fall below ``threshold``."""
    blockers = [window for window in windows
                if isinstance(window.get("used_percent"), (int, float))
                and window["used_percent"] >= threshold]
    if not blockers or any(
            not isinstance(window.get("reset_at"), (int, float))
            or isinstance(window.get("reset_at"), bool)
            or not math.isfinite(window["reset_at"])
            for window in blockers):
        return None
    return max(blockers, key=lambda window: window["reset_at"])


def _account_money(row, ceiling):
    """One normalized row -> one closed account-state table record.

    WINDOWS FIRST (task/2935). A measured window at or past the cap or the
    ceiling is direct evidence about this account whatever the row's status
    word says; the word decides only where no measured wall or cap exists.
    The native `no-window` row (its 7d unread) with a measured 5h at 100% is
    therefore WALLED, not unread. A status other than `ok` still makes the
    account INCOMPLETE, and producers hand over no current number for a row
    they could not read (a stale or unread row's windows carry None).

    AN ACCOUNT OPENS WHEN ITS LAST OVER-CEILING WINDOW RESETS (task/2935, the
    integrator's correction of its own table): the binding of a WALLED or
    CAPPED account is the LATEST reset among every window at or over the
    ceiling, walled or capped, so a 5h at 100% beside a 7d at 95% opens at
    the 7d reset, not the 5h one."""
    windows = row.get("windows") or []
    word = row.get("state")
    shape = []
    if not windows:
        shape.append("empty window set")
    if row.get("source") not in ("measured", "derived"):
        shape.append("row source outside measured|derived")
    for window in windows:
        if window.get("source") not in ("measured", "derived"):
            shape.append("window source outside measured|derived")
        value = window.get("used_percent")
        if value is not None and (not isinstance(value, (int, float))
                                  or isinstance(value, bool)
                                  or not math.isfinite(value)):
            shape.append("window utilization outside number|null")
    numeric = [window for window in windows
               if isinstance(window.get("used_percent"), (int, float))
               and not isinstance(window.get("used_percent"), bool)
               and math.isfinite(window["used_percent"])]
    measured = [window for window in numeric
                if window.get("source") == "measured"]
    walls = [window for window in measured if window["used_percent"] >= 100]
    caps = [window for window in measured
            if window["used_percent"] >= ceiling]
    unread = [str(window.get("label") or "unnamed") for window in windows
              if window.get("used_percent") is None]
    derived = [str(window.get("label") or "unnamed") for window in windows
               if window.get("source") == "derived"]
    pressure = max((window["used_percent"] for window in numeric), default=None)
    # The status word, read only for what the windows leave open: `ok` says
    # nothing more, `exhausted` agrees with a measured wall or cap and is an
    # unread claim without one, and every other word is a missing reading.
    status = (None if word == "ok" or word == "exhausted" and (walls or caps)
              else "exhausted without a measured cap" if word == "exhausted"
              else str(word or "unread"))
    incomplete = bool(unread or derived or row.get("source") == "derived"
                      or status)
    reasons = (([status] if status else []) +
               (["unread window(s): %s" % ", ".join(unread)] if unread else []) +
               (["derived window(s): %s" % ", ".join(derived)] if derived else []))
    common = {"row": row, "pressure": pressure,
              "incomplete": bool(shape) or incomplete,
              "why": ", ".join(dict.fromkeys(shape + reasons))}
    authority = "wall" if walls else "cap" if caps else "none"
    completeness = "incomplete" if incomplete else "complete"
    state = None if shape else _ACCOUNT_STATE_TABLE.get(
        (authority, completeness))
    if state is None:
        return dict(common, state=ACCOUNT_UNREAD, binding=None,
                    estimated=False,
                    why=common["why"] or
                    "shape outside the account-state table")
    binding = (_account_binding(numeric, min(ceiling, 100))
               if state in (ACCOUNT_WALLED, ACCOUNT_CAPPED) else None)
    return dict(common, state=state, binding=binding,
                estimated=state == ACCOUNT_UNREAD and pressure is not None)


def _family_binding(accounts):
    bindings = [account.get("binding") for account in accounts]
    if not bindings or any(binding is None for binding in bindings):
        return None
    return min(bindings, key=lambda window: window["reset_at"])


def _on_demand_off_family(family):
    from . import moneyread, seat
    return family in moneyread.catalog_families() and \
        moneyread._on_demand_is_off(seat.FAMILIES.get(family))


def derive_money(family, rows, ceiling=None, abundance=None, measured_at=None,
                 fresh=True, rotated=True, reset_credits=None):
    """The MONEY axis for one family -> an axis record.

    `rows` is None for a family with NO READER — which is a permanent GREY and
    says so — and `[]` for a pool that was READ and found empty, which is a
    measurement about accounts that are provably gone rather than an absence.

    One closed account-state table owns the decision. A measured wall or cap
    dominates uncertainty on that same account, but RED requires every account
    to be measured walled/capped. Unread sibling accounts therefore cap
    spendability at UNKNOWN without becoming either headroom or a refusal.

    `rotated` says ONE DOOR PICKS THE ACCOUNT for this family, which is true
    of every family behind a proxy seat and false of the native credential.
    An unrotated family is read as a POOL: how many accounts can still carry
    base load, and whether a reset lands soon enough to carry it for them.

    `reset_credits` are the watchdog pass's own reset-credit rows, never a
    probe from this path. A wall an owner asset can lift in one pass is not a
    wall that stands for days, and the expiry says so."""
    ceiling = ceiling_pct() if ceiling is None else ceiling
    if rows is None:
        return _axis("money", GREY,
                     "no money reader exists for this family yet, so nothing "
                     "is known about what it has left",
                     "money:no-reader", measured_at=measured_at)
    rows = _money_rows(rows)
    if not rows:
        return _axis("money", GREY,
                     "the pool was read and holds no account, so there is no "
                     "budget to be low", "money:known-empty",
                     measured_at=measured_at)
    accounts = [_account_money(row, ceiling) for row in rows]
    pressure = [account for account in accounts if account["state"] in
                (ACCOUNT_WALLED, ACCOUNT_CAPPED)]
    unread = [account for account in accounts
              if account["state"] == ACCOUNT_UNREAD]
    opened = [account for account in accounts if account["state"] == ACCOUNT_OPEN]
    measured = pressure + opened
    source = ("derived" if any(
        account["row"].get("source") == "derived" or account["incomplete"]
        and any(window.get("source") == "derived"
                for window in account["row"].get("windows") or ())
        for account in accounts) else "measured")
    reasons = [account["why"] for account in accounts if account.get("why")]
    coverage_why = "; ".join(dict.fromkeys(reasons)) or None
    coverage = {"measured": len(measured), "unread": len(unread),
                "total": len(accounts),
                "unread_why": _unread_why([account["row"]
                                            for account in unread])}
    incomplete_accounts = [account for account in accounts
                           if account["incomplete"]]
    if incomplete_accounts and not unread:
        coverage["unread_why"] = "%d incomplete" % len(incomplete_accounts)
    coverage_gap = bool(unread) or any(account["incomplete"]
                                       for account in accounts)
    estimates = [account["pressure"] for account in unread
                 if account.get("estimated")]
    estimate = max(estimates) if estimates else None
    unread_class = ("none" if not unread else "estimated"
                    if estimate is not None and estimate >= orange_pct(ceiling)
                    else "unread")
    family_state = _FAMILY_STATE_TABLE.get(
        (bool(pressure), unread_class, bool(opened)))
    if not fresh:
        return _axis("money", GREY,
                     "the budget reading is older than the freshness bound, "
                     "so it is not believed", "money:stale",
                     measured_at=measured_at, coverage=coverage,
                     capped_by_coverage=True, provenance="unmeasured")
    if family_state == "estimated":
        why = coverage_why or coverage["unread_why"]
        return _axis(
            "money", ORANGE,
            "estimated: incomplete or derived account evidence reaches "
            "%.0f%%, but cannot establish a measured cap or wall (%s)" %
            (estimate, why),
            "money:estimated", measured_at=measured_at,
            coverage=coverage, capped_by_coverage=True,
            coverage_why=why, provenance="derived",
            expires_at=None, expires_kind="none",
            expires_source="unread account pressure horizon unknown")
    if family_state in (None, "unknown"):
        why = coverage_why or coverage["unread_why"] or \
            "shape outside the account-state table"
        provenance = "derived" if estimates else "unmeasured"
        return _axis("money", GREY,
                     "%d of %d accounts are UNKNOWN (%s)"
                     % (len(unread), len(accounts), why),
                     "money:unreadable", measured_at=measured_at,
                     coverage=coverage, capped_by_coverage=True,
                     coverage_why=why, provenance=provenance)
    if family_state == "blocked":
        # A family whose catalog claims on-demand off WITH a basis cannot be
        # billed past the included dollars: a high pool reading is not a money
        # wall at the dispatch door, but a down-spend family the vendor itself
        # will still refuse — so the money axis says ORANGE, not RED, and the
        # family stays dispatchable until the vendor refuses on the reach axis.
        if _on_demand_off_family(family):
            return _axis("money", ORANGE,
                         "%s is at or past the %.0f%% ceiling on its measured "
                         "budget, but its catalog claims on-demand off with a "
                         "basis, so this is spending down, not a wall — keep a "
                         "steady pace until the vendor refuses" % (
                             family, ceiling),
                         "money:ondemand-off-spend-down", measured_at=measured_at,
                         coverage=coverage, capped_by_coverage=coverage_gap,
                         coverage_why=coverage_why, provenance="measured")
        binding = _family_binding(pressure)
        reset = _wall_reset_fields(
            binding, credits=reset_credits, measured_at=measured_at)
        all_walled = all(account["state"] == ACCOUNT_WALLED
                         for account in pressure)
        cause = ("every account has a measured window at 100% or more" if
                 all_walled else
                 "every account is at or past the %.0f%% ceiling on a measured "
                 "budget window" % ceiling)
        return _axis("money", RED, cause,
                     "money:window-wall" if all_walled else "money:capped",
                     measured_at=measured_at, coverage=coverage,
                     capped_by_coverage=coverage_gap,
                     coverage_why=coverage_why, provenance="measured", **reset)
    if family_state == "partial":
        return _axis(
            "money", ORANGE,
            "capped where measured; %d unread account%s"
            % (len(unread), "" if len(unread) == 1 else "s"),
            "money:measured-capped-unread", measured_at=measured_at,
            coverage=coverage, capped_by_coverage=True,
            coverage_why=coverage_why, provenance=source,
            expires_at=None, expires_kind="none",
            expires_source="unread account opening is unknown")
    usable = [dict(account["row"], longest_pct=account["pressure"])
              for account in measured]
    open_rows = [dict(account["row"], longest_pct=account["pressure"])
                 for account in opened]
    best = min(row["longest_pct"] for row in open_rows)
    band = orange_pct(ceiling)
    if best >= band:
        return _axis("money", ORANGE,
                     "the account with the most headroom is already at "
                     "%.0f%%, past the %.0f%% warning band the %.0f%% ceiling "
                     "sets" % (best, band, ceiling), "money:warning-band",
                     measured_at=measured_at, coverage=coverage,
                     capped_by_coverage=coverage_gap,
                     coverage_why=coverage_why,
                     provenance=source, **_reset_fields(usable))
    if not rotated:
        pool = _pool_axis(usable, coverage,
                          [account["row"] for account in unread], measured_at)
        if pool is not None:
            pool["provenance"] = source
            pool["coverage_why"] = coverage_why
            pool["capped_by_coverage"] = coverage_gap
            return pool
    waste = [a for a in (abundance or ())
             if a.get("verdict") == "waste-danger"]
    if source == "measured" and not coverage_gap and waste:
        return _axis("money", GREEN,
                     "every account read, the best at %.0f%%, and at least "
                     "one carries more window budget than can physically be "
                     "spent before it resets" % best, "money:abundant",
                     measured_at=measured_at, coverage=coverage,
                     capped_by_coverage=False, abundance_read=True,
                     provenance=source)
    why = ("money coverage is incomplete" if coverage_gap else
           "no abundance reading, so GREEN is not claimed")
    return _axis("money", YELLOW,
                 "the account with the most headroom is at %.0f%%, under the "
                 "%.0f%% warning band; %s" % (best, band, why),
                 "money:has-headroom", measured_at=measured_at,
                 coverage=coverage, capped_by_coverage=coverage_gap,
                 coverage_why=coverage_why,
                 abundance_read=bool(abundance), provenance=source,
                 **_reset_fields(usable))


def _account_reset_below(row, mark):
    """Window whose reset first proves this account below ``mark``."""
    windows = row.get("windows") or ()
    if not windows or any(
            window.get("source") != "measured"
            or not isinstance(window.get("used_percent"), (int, float))
            or isinstance(window.get("used_percent"), bool)
            or not math.isfinite(window["used_percent"])
            for window in windows):
        return None
    return _account_binding(windows, mark)


def _carries_soon(rows, measured_at):
    """Earliest complete account opening inside one vendor session, or None."""
    if not measured_at:
        return None
    openings = [_account_reset_below(row, CARRY_PCT) for row in rows
                if row["longest_pct"] >= CARRY_PCT]
    openings = [window for window in openings if window is not None
                and 0 < window["reset_at"] - measured_at <= carry_soon_s()]
    return min(openings, key=lambda window: window["reset_at"]) \
        if openings else None


def _pool_axis(known, coverage, unknown, measured_at):
    """The POOL reading for an unrotated family -> an axis record, or None.

    None means the pool says nothing the ordinary bands do not already say,
    and the caller falls through to them. The two answers this owns are the
    owner's own: MOST of the accounts spent and no reset coming is a pool that
    needs active management (ORANGE), and the same pool with a reset landing
    inside the carry window is a wait (YELLOW). A MAJORITY still carrying is
    the pool-level abundance reading the native family has no other reader
    for — and it is still refused while any account is unread, because an
    unread account can never reach GREEN."""
    carriers = [r for r in known if r["longest_pct"] < CARRY_PCT]
    soon = _carries_soon(known, measured_at)
    if len(carriers) * 2 < len(known):
        spent = len(known) - len(carriers)
        if soon:
            return _axis("money", YELLOW,
                         "%d of %d accounts have spent more than half their "
                         "worst budget window, and a reset lands within %dh to "
                         "carry the base load"
                         % (spent, len(known), carry_soon_s() // 3600),
                         "money:pool-reset-carries", measured_at=measured_at,
                         coverage=coverage, capped_by_coverage=bool(unknown),
                         **_wall_reset_fields(soon))
        return _axis("money", ORANGE,
                     "%d of %d accounts have spent more than half their "
                     "worst budget window and no reset lands within %dh to carry "
                     "the base load; the repair is active management, not "
                     "fan-out" % (spent, len(known), carry_soon_s() // 3600),
                     "money:pool-thin", measured_at=measured_at,
                     coverage=coverage, capped_by_coverage=bool(unknown),
                     **_reset_fields(known))
    # A STRICT MAJORITY. An even split is not a majority: at exactly half the
    # minority test above is false and the pool must say NOTHING, so the
    # ordinary bands answer (YELLOW), never the owner's most permissive colour.
    if not unknown and len(carriers) * 2 > len(known) and len(carriers) > 1:
        return _axis("money", GREEN,
                     "%d of %d accounts are still under half their longest "
                     "window, so the pool carries base load without being "
                     "managed" % (len(carriers), len(known)),
                     "money:pool-carries", measured_at=measured_at,
                     coverage=coverage, capped_by_coverage=False,
                     abundance_read=True)
    return None


def _reset_fields(rows, credits=None, measured_at=None):
    """The expiry keyword arguments for a money axis built from these rows.

    RUNG ONE IS THE RESET CREDIT. A family holding a spendable rate-limit
    reset credit changes in MINUTES — the next watchdog pass may spend one —
    and dating that wall from the natural weekly reset is the largest expiry
    error available to this module."""
    spendable = credit_count(credits)
    if spendable and measured_at:
        from . import proxywatch
        return {"expires_at": measured_at + proxywatch.INTERVAL_S,
                "expires_kind": "reset-credit",
                "expires_source": "reset-credit rows: %d spendable; the next "
                                  "watchdog pass may spend one" % spendable}
    at, kind, label = _band_reset(rows)
    return {"expires_at": at, "expires_kind": kind,
            "expires_source": (None if not label else
                               "budget rows: the %s window's reset" % label)}


def credit_count(rows):
    """How many reset credits this family can SPEND right now, from the
    watchdog pass's own rows -> int.

    `spendable` is the count the reset policy would actually honour (the
    vendor's balance narrowed by what this plan supports); `available` is the
    raw balance and is read only where the narrower number was not recorded.
    A row that states neither is not a credit."""
    total = 0
    for row in rows or ():
        n = row.get("spendable")
        if n is None:
            n = row.get("available")
        if isinstance(n, int) and n > 0:
            total += n
    return total


def credit_summary(rows):
    """{usable, unread, why} from the reset rung's own rows -> the flag's
    credits field, or None when the rung did not run for this family.

    COUNTS ONLY. A reset row carries the account it is about, and nothing of it
    but the numbers crosses into the snapshot: the same structural redaction
    `_money_rows` keeps.

    EVERY CREDIT READ IS IN HAND. A reset credit lifts a rate-limit wall and a
    workspace credits-depleted wall alike (measured on trunk,
    `codexresets.CREDITS_DEPLETED_TYPES`), so no wall makes a counted credit
    worthless, and a Pro credit the spend order holds back is still in hand.
    An account the rung did not ask for a balance is `unread`, never 0."""
    if rows is None:
        return None
    usable, unread = 0, 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        n = row.get("spendable")
        if n is None:
            n = row.get("available")
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            usable += n
        else:
            unread += 1
    return {"usable": usable, "unread": unread,
            "why": ("%d account%s not asked for a balance this pass (no "
                    "spendable wall)" % (unread, "" if unread == 1 else "s"))
            if unread else None}


def _unread_why(unknown):
    """Why the unread rows are unread, by STATE and count — never by name.

    A count alone tells an operator a census is incomplete and not what to go
    and repair, and the repair is the difference between waiting for a vendor
    and refreshing our own token copy."""
    if not unknown:
        return ""
    seen = {}
    for row in unknown:
        state = row.get("state") or "unknown"
        seen[state] = seen.get(state, 0) + 1
    return ", ".join("%d %s" % (n, s) for s, n in sorted(seen.items()))


def derive_reach(family, record, now=None):
    """The REACH axis -> an axis record, or None when reach says nothing.

    WHO PRODUCED THE DARK STATE DECIDES THE COLOUR, not the state's name. A
    cooldown we imposed on ourselves is a restart away; an untyped upstream
    refusal is a wall; an empty or malformed 200 is OUR VALIDATION failing and
    is not a budget fact at all.

    A dark state is not believed before the record's own falsification bar: a
    seat that went dark a minute ago has not yet been contradicted by anything
    and a flag minted on it would ration the fleet on one bad response."""
    if not isinstance(record, dict):
        return None
    now = time.time() if now is None else now
    state = record.get("state")
    if not record.get("dark"):
        # UNKNOWN means the seats of one family DISAGREE. That contributes
        # nothing — it is neither a wall nor a clearance — and the caller is
        # told which reading is in dispute rather than given a colour.
        return None
    from . import proxywatch
    if proxywatch.quota_wall(record) and state != "AUTH-UNAVAILABLE":
        # With on-demand spend disabled, a vendor refusal is a REACH wall,
        # not an assertion that spending past the included dollars is possible.
        # Other families keep the quota refusal on MONEY. The proxy's cooldown
        # inherits the vendor refusal rather than becoming an OURS repair.
        return (derive_quota_wall(family, record)
                if _on_demand_off_family(family) else None)
    since = pk.parse_ts_epoch(record.get("since"))
    bar = record.get("falsification_bar_s") or 0
    if since and (now - since) < bar:
        return None
    origin = proxywatch.dark_origin(state)
    if state == proxywatch._PROXY_LOCAL_403:
        # A 403 OUR proxy minted is ours, and it is no cooldown and no vendor
        # wall: nothing reached the vendor, so there is no reset to wait for.
        return _axis("reach", ORANGE,
                     "our proxy refused this itself (%s); no request reached "
                     "the vendor; the repair is ours: read the proxy's stated "
                     "reason, fix it, restart and probe" % state,
                     "reach:our-refusal", measured_at=since,
                     expires_kind="none",
                     expires_source="proxywatch upstream record: a refusal "
                                    "our proxy minted has no reset")
    if origin == proxywatch.DARK_OURS:
        return _axis("reach", ORANGE,
                     "the family is dark in a cooldown WE imposed (%s); the "
                     "repair is a restart and then a probe, not a wait"
                     % state, "reach:our-cooldown", measured_at=since,
                     expires_kind="proxy-cooldown",
                     expires_source="proxywatch upstream record")
    if origin == proxywatch.DARK_OUR_VALIDATION:
        return _axis("reach", GREY,
                     "the family answered and OUR OWN validation rejected the "
                     "answer (%s); that says nothing about the vendor's "
                     "budget" % state, "reach:our-validation",
                     measured_at=since, owner_ask=True)
    return _axis("reach", RED,
                 "the family is dark on an upstream state nothing here types "
                 "(%s), so nothing sent to it arrives" % state,
                 "reach:upstream-dark", measured_at=since,
                 expires_kind="none",
                 expires_source="proxywatch upstream record: no reset known")


def derive_quota_wall(family, record):
    """A vendor quota refusal -> MONEY, or REACH for a based on-demand-off
    family, or None when the upstream record has no quota wall.

    The instant is the record's vendor reset: the one the refusal carried, or
    else the owner-entered page value (`helm proxywatch vendor-reset`). With
    neither, the wall stands with its expiry UNKNOWN and says so."""
    from . import proxywatch
    wall = proxywatch.quota_wall(record)
    if not wall or not record.get("dark"):
        return None
    ms = record.get("resets_at_ms")
    at = ms / 1000.0 if isinstance(ms, (int, float)) \
        and not isinstance(ms, bool) else None
    mirror = "" if wall == record.get("state") else (
        ", held across the proxy's %s that mirrors it" % record["state"])
    axis = "reach" if _on_demand_off_family(family) else "money"
    # THE REPAIR FOLLOWS THE REFUSAL'S KIND: an empty balance has no reset to
    # wait for. A window, or a kind nobody recorded, keeps the reset wording.
    if proxywatch.quota_wall_kind(record) == "balance":
        text = ("the vendor refused on an empty balance (%s%s); the repair "
                "is a top-up or a plan change by the account owner, with no "
                "reset to wait for; not a proxy bounce or a new key")
    else:
        text = ("the vendor refused on its usage quota (%s%s); the repair is "
                "a wait for its reset, not a proxy bounce or a new key")
    return _axis(axis, RED, text % (wall, mirror),
                 "%s:vendor-quota-wall" % axis,
                 measured_at=pk.parse_ts_epoch(record.get("since")),
                 expires_at=at,
                 expires_kind="vendor-reset" if at else "none",
                 expires_source=(
                     "proxywatch upstream record: %s vendor reset"
                     % (record.get("reset_source") or "unnamed") if at else
                     "proxywatch upstream record: no vendor reset known"))


def derive_turn_wall(family, walls):
    """(money axis, reach axis) a seat's OWN last turn sets, each None when
    no standing wall of that kind is read (task/3587).

    `walls` is `turnwall.family_walls()`'s entry for this family: a
    provider's billing or credential refusal on a seat's own turn that no
    sibling has answered past. The upstream record can read HEALTHY over it,
    because the proxy answered and the vendor said no, so this is its own
    reading rather than a correction of that one. Neither carries a reset:
    the wall clears when a seat of the family completes a turn."""
    out = []
    for axis, cause_id, repair in (
            ("money", "money:turn-wall", "a top-up, a plan change or the "
                                         "vendor's reset"),
            ("reach", "reach:turn-wall", "a person clearing the account "
                                         "check")):
        w = (walls or {}).get(axis) if isinstance(walls, dict) else None
        based_off = _on_demand_off_family(family)
        if axis == "money" and based_off:
            out.append(None)
            continue
        if axis == "reach" and based_off and not isinstance(w, dict):
            w = (walls or {}).get("money") if isinstance(walls, dict) else None
            if isinstance(w, dict):
                cause_id = "reach:turn-quota-wall"
                repair = "the vendor serving the family again"
        if not isinstance(w, dict):
            out.append(None)
            continue
        out.append(_axis(
            axis, RED,
            "seat %s's own last turn ended on a provider refusal (%s) and no "
            "seat of the family has completed a turn since; the repair is %s"
            % (w.get("seat") or "?", w.get("label") or "?", repair),
            cause_id, measured_at=w.get("at"), expires_kind="none",
            expires_source="the seat's own transcript: the wall clears on "
                           "the family's next completed turn"))
    return out[0], out[1]


def _gauge(row, label):
    for g in row.get("gauges") or ():
        if g.get("label") == label:
            return g
    return None


def derive_policy(rows, now=None, fresh_s=None):
    """The POLICY axis for the native family -> an axis record, or None.

    ONE RULE TODAY, the owner's own named example: when the scoped model
    window is burning the shared credential far faster than the account as a
    whole, the repair is not to wait, it is to move the work onto another
    model. Silent below the minimum burn, because a ratio over nothing is
    noise, and silent on readings older than the freshness bound.

    AN UNREAD ACCOUNT NEVER FIRES THIS AXIS — the module's own first law, which
    lived only in `derive_money` until an unread account rationed the fleet
    through this one. The median is a POSITION IN A CENSUS, so dropping the
    rows nothing could read does not merely shrink the sample, it MOVES the
    position: four readable rows out of six put the median at the third
    smallest ratio where the family's own median is the fourth. Measured on a
    real host, the four readable ratios were 0.99 / 1.15 / 1.25 / 1.65 against
    a 1.19 threshold — the axis fired ORANGE on 1.25, and the family's median
    with the two unread accounts anywhere below 1.15 is 1.15, which does not
    fire at all. The colour, the capacity and `route`'s N1 drop of the native
    family all turned on two numbers helm could not read.

    SO THE UNREAD ROWS SIT WHERE THEY CANNOT FIRE IT. They are counted into the
    census at the FAVOURABLE end, and the axis fires only where the median
    still crosses whatever they would have said. That is `absence is not a
    measured contradiction` in the one arithmetic that can smuggle an absence
    in as pressure. Where the readable subset WOULD have fired and the census
    does not, the axis is not silent: it renders GREY, which this module's
    ordering puts off the scale, and it names the repair — because "we cannot
    tell" and "there is no pressure" are not the same finding.

    A STALE ROW IS AN UNREAD ROW HERE, exactly as it already is for the money
    axis: a reading past the bound is not evidence about now, and a row that
    cannot contribute a ratio cannot be allowed to contribute a position.

    THE UNREAD TEST IS THE RATIO'S OWN INPUTS, not the status vocabulary. Every
    spelling `providers` writes without a reading (`reauth-needed`,
    `no-credentials`, `http_*`, `network-error`) arrives with NO gauges, so a
    row that cannot yield both sides of the ratio is exactly the set the status
    test would name — and it also catches the case the status test would miss,
    a row that answered with gauges the vendor left unfilled. One test, and it
    reads the same fields the ratio does rather than a second vocabulary that
    can drift from the producer's."""
    now = time.time() if now is None else now
    fresh_s = max_age_s() if fresh_s is None else fresh_s
    qualifying, thin, unread = [], 0, 0
    for row in rows or ():
        probed = pk.parse_ts_epoch(row.get("probed_at"))
        if probed and (now - probed) > fresh_s:
            unread += 1
            continue
        account = _gauge(row, POLICY_ACCOUNT_LABEL)
        scoped = _gauge(row, POLICY_SCOPED_LABEL)
        if not account or not scoped:
            unread += 1
            continue
        if (account.get("utilization") is None
                or scoped.get("utilization") is None):
            # BOTH SIDES OF THE RATIO ARE CHECKED. A gauge the vendor sent
            # with no utilization is a gauge with no reading, and dividing by
            # or into it raises inside the fold — the one exception this axis
            # is not allowed to throw, since `read_inputs` swallows it and the
            # whole native policy reading disappears with no line anywhere.
            unread += 1
            continue
        if account["utilization"] < POLICY_MIN_WEEKLY:
            thin += 1
            continue
        qualifying.append((scoped["utilization"] / account["utilization"],
                           account, probed))
    if not qualifying:
        return None
    qualifying.sort(key=lambda q: q[0])
    total = len(qualifying) + thin + unread
    coverage = {"measured": len(qualifying) + thin, "unread": unread,
                "total": total}
    # THE CENSUS, with the unread rows at the favourable end. `None` is the
    # sentinel for "a position no reading fills": it sorts nowhere and it is
    # never compared, it is only landed on — and landing on one IS the answer
    # that the family's median is not knowable.
    census = [None] * unread + [q[0] for q in qualifying]
    ratio = census[len(census) // 2]
    if ratio is None or ratio < POLICY_RATIO:
        readable = qualifying[len(qualifying) // 2][0]
        if unread and readable >= POLICY_RATIO:
            return _axis("policy", GREY,
                         "the %d readable account(s) would put one model's "
                         "scoped burn at %.2f times the account-wide rate, "
                         "over the %.2f threshold — but %d of %d accounts "
                         "carry no reading, so the family's own median is not "
                         "knowable and an absence may not be read as pressure; "
                         "the repair is the credential reading named on those "
                         "accounts, not a wait and not a different model"
                         % (len(qualifying), readable, POLICY_RATIO, unread,
                            total),
                         "policy:unread-census",
                         measured_at=max(q[2] or 0 for q in qualifying) or None,
                         coverage=coverage, capped_by_coverage=True,
                         ratio=round(readable, 2))
        return None
    resets = [q[1].get("reset") for q in qualifying if q[1].get("reset")]
    return _axis("policy", ORANGE,
                 "one model's scoped window is burning the shared credential "
                 "%.2f times its account-wide rate on %d of %d accounts over "
                 "the %.2f minimum burn; the repair is a different model, not "
                 "a wait" % (ratio, len(qualifying), total, POLICY_MIN_WEEKLY),
                 "policy:scoped-share", expires_at=min(resets) if resets else None,
                 expires_kind="weekly-reset",
                 expires_source="usage history: account-wide weekly reset",
                 measured_at=max(q[2] or 0 for q in qualifying) or None,
                 coverage=coverage, capped_by_coverage=bool(unread),
                 ratio=round(ratio, 2))


ADDRESS_TOKEN = "[an account]"


def redact_why(text):
    """The owner's own reason, with every identity and credential taken out.

    THE DECLARATION IS THE ONE INPUT `_money_rows` DOES NOT PROJECT. Every
    measured cause is built from a projected row, so no identity CAN reach the
    snapshot down that path; the declaration is free text the owner typed, it
    becomes the flag's `cause`, and the cause is rendered on the board, the
    one line, the JSON and `helm burn why`. `accounts` owns the grammar of an
    address and of key-shaped material for the whole tree, so the redaction is
    ITS function rather than a second opinion about what an email looks
    like."""
    from . import accounts
    # THE WHOLE ADDRESS GOES, NOT ITS FIRST LETTER AND DOMAIN. `accounts`
    # masks to tell two accounts apart on the declared card; this snapshot's
    # law is that NO identity reaches it, and a domain names the tenant. The
    # grammar of an address stays `accounts`' own; only the replacement is
    # this module's.
    out = accounts._EMAIL_RE.sub(ADDRESS_TOKEN, text) if isinstance(text, str) else text
    return accounts.redact_identities(out) or "the owner declared it"


def derive_declared(family, declarations, now=None):
    """The DECLARED axis -> an axis record, or None.

    An owner declaration is INTENT, so it carries no measurement and it
    expires: a colour somebody typed is true until the moment they said, and
    a declaration with no expiry would outlive the world it described."""
    now = time.time() if now is None else now
    rec = ((declarations or {}).get("families") or {}).get(family)
    if not isinstance(rec, dict):
        return None
    colour = str(rec.get("colour") or "").upper()
    if colour not in COLOURS:
        return None
    until = rec.get("until")
    until = pk.parse_ts_epoch(until) if isinstance(until, str) else until
    if until and now >= until:
        return None
    return _axis("declared", colour, redact_why(rec.get("why")),
                 "declared:owner", expires_at=until,
                 expires_kind="owner-declared",
                 expires_source="helm burn declare",
                 measured_at=rec.get("declared_at"))


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def derive_certified_local(family, certifications, now=None, local=None,
                           record=None):
    """The MONEY axis a certification sets on a LOCAL family -> an axis
    record, or None.

    None, which leaves the ordinary no-reader GREY, for a family that is not
    local whatever the file says, and for a record that is absent, revoked,
    ends at or before the fold's own instant, or claims more than
    `CERTIFICATION_CEILING_S` from its own `certified_at`. A lapse is
    therefore not an error: it reads exactly as no certification does.

    A FRESH RECORD IS HONOURED ONLY WHILE IT IS TIED TO THE OPERATOR SEAT.
    Its `by` must be the family's declared operator (`local_operator`, read
    by `read_local_certifications` into `operators`), and its `session_key`
    must still bind to that operator on the roster (`bound`, from the same
    reader). Anything else, a bare `by`, a row typed with no key the roster
    binds to the operator, a key rebound elsewhere or a roster that cannot be
    read, reads GREY `CERTIFICATION_UNVERIFIABLE`, never GREEN, with a cause
    naming the operator and why. A REPLAY is not caught here: the operator's
    own key copied onto a later `until` binds as the original did, because
    the key is not a secret (the module's LAWS state that boundary).

    A LIVE READING THAT CONTRADICTS "RUNNING OPTIMALLY" WINS. While the
    family's own upstream `record` reads anything but HEALTHY (a dark state,
    an answer our validation rejected, or its seats disagreeing), the
    certification is withdrawn and the family reads as it would uncertified.
    No record at all contradicts nothing.

    AN UNREADABLE FILE IS NOT AN ABSENT ONE: a local family then reads GREY
    with a cause that names the file, never the no-reader words.

    `expires_at` IS the certification's `until` and `measured_at` its
    `certified_at`, both the producer's own instants."""
    now = time.time() if now is None else now
    if family not in (local_families() if local is None else local):
        return None
    certifications = certifications or {}
    unreadable = certifications.get("unreadable")
    if unreadable:
        return _axis("money", GREY,
                     "no money reader exists for this family, and its local "
                     "certification file %s could not be read, so whether "
                     "it is certified is unknown" % unreadable,
                     "money:certification-unreadable", measured_at=None,
                     provenance="unmeasured")
    if isinstance(record, dict) and (record.get("dark")
                                     or record.get("state") != "HEALTHY"):
        return None
    rec = (certifications.get("families") or {}).get(family)
    if not isinstance(rec, dict) or rec.get("revoked_at") is not None:
        return None
    until, at, by = rec.get("until"), rec.get("certified_at"), rec.get("by")
    if not _finite(until) or not _finite(at) or until <= now \
            or not 0 < until - at <= CERTIFICATION_CEILING_S:
        return None
    operator = (certifications.get("operators") or {}).get(family)
    why = _unbound(family, operator, by, rec.get("session_key"),
                   certifications.get("bound") or {},
                   certifications.get("roster_unreadable"))
    if why:
        return _axis("money", GREY,
                     redact_why("no money reader exists for this family, and "
                                "its local certification cannot be verified: "
                                "%s, so it reads GREY until %s certifies "
                                "again from its own session"
                                % (why, operator or "a declared operator "
                                   "seat")),
                     CERTIFICATION_UNVERIFIABLE, measured_at=None,
                     provenance="unmeasured")
    return _axis("money", GREEN,
                 redact_why("served on the operator's own GPU and certified "
                            "running optimally by %s until %s"
                            % (by, _iso(until))),
                 "money:certified-local", expires_at=until,
                 expires_kind="local-certification",
                 expires_source="helm burn certify-local", measured_at=at,
                 capped_by_coverage=False, provenance=CERTIFIED_LOCAL)


def _unbound(family, operator, by, key, bound, roster_unreadable=None):
    """Why a certification of `family` by seat `by`, from the session whose
    key is `key`, is NOT tied to the family's operator seat -> a sentence, or
    None when it is.

    ONE PREDICATE FOR THE PRODUCER AND THE FOLD, so the verb refuses exactly
    the record the fold would read GREY. `operator` is `local_operator`'s
    answer and `bound` is `_session_bindings`'s map, both read by the caller,
    so this reads no file."""
    if not operator:
        return ("%s declares no operator seat, so nobody can certify it (its "
                "catalog entry names the seat by the local-names key %r, and "
                "this host's local names do not set it)"
                % (family, _operator_key(family)))
    # THE FILE IS NOT TRUSTED and its `by` reaches the cause every surface
    # renders, so only a well-formed seat name is repeated
    named = by if isinstance(by, str) \
        and home._SEAT_NAME_RE.fullmatch(by) else None
    if named is None or named.casefold() != operator.casefold():
        return ("the certifier is %s, and %s's operator seat is %s"
                % (named or "no well-formed seat name", family, operator))
    if not isinstance(key, str) or not key:
        return ("no session key is recorded, so no session of the operator "
                "seat %s stands behind it" % operator)
    if roster_unreadable:
        return ("%s, so the session behind it cannot be checked against the "
                "operator seat %s" % (roster_unreadable, operator))
    # A MATCH, NEVER A NAME: the roster's answer is compared and not printed,
    # so no roster key reaches the cause (the display-launder tripwire's
    # INTERNAL-MATCHING-ONLY entry for this module)
    seat = bound.get(key)
    if not isinstance(seat, str) or seat.casefold() != operator.casefold():
        return ("the session behind it is not bound to the operator seat %s "
                "on the roster (rebound to another seat, or to none)"
                % operator)
    return None


def _operator_key(family):
    from . import seat
    fam = seat.FAMILIES.get(family)
    return (fam.get(OPERATOR_SEAT_FROM) if isinstance(fam, dict) else None) \
        or OPERATOR_SEAT_FROM


# --------------------------------------------------------------- composition

def _worse(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if _RANK[a] >= _RANK[b] else b


_EXPIRY_ORDER = ("reset-credit", "weekly-reset", "window-reset", "vendor-reset",
                 "proxy-cooldown", "owner-declared", "local-certification",
                 "daily-rollover", "none")


def expiry(axes, headline=None):
    """(expires_at, kind, source) for a composed flag.

    PRECEDENCE WITHIN THE HEADLINE'S OWN COLOUR, NEVER A BORROWED INSTANT.
    What the reader needs is when THIS colour changes, so the axis that set
    the colour owns the countdown. An axis at some OTHER colour resets without
    moving the family's band at all, and filling a missing expiry from one
    dates the clearing of a wall from a reading that has nothing to do with
    it: an unknown instant stays UNKNOWN and says so. Where two axes carry the
    SAME worst colour the precedence decides WHICH REPAIR is named — a reset
    credit is spent in one pass, a weekly reset is a wait of days."""
    if not headline:
        return None, "none", None
    if headline.get("expires_at"):
        return (headline["expires_at"], headline.get("expires_kind"),
                headline.get("expires_source"))
    candidates = [a for a in axes if a and a.get("expires_at")
                  and a.get("colour") == headline.get("colour")]
    if not candidates:
        return None, headline.get("expires_kind") or "unknown", None
    candidates.sort(key=lambda a: (
        _EXPIRY_ORDER.index(a.get("expires_kind"))
        if a.get("expires_kind") in _EXPIRY_ORDER else len(_EXPIRY_ORDER),
        a["expires_at"]))
    best = candidates[0]
    return best["expires_at"], best.get("expires_kind"), best.get("expires_source")


def compose(family, axes, now=None, credits=None):
    """The four axes -> ONE flag record for one family.

    The headline colour is the WORST measured axis; a declaration may worsen
    it and may never improve it. On a family nothing measured, the declaration
    IS the answer, because GREY is off the ordering rather than at the good
    end of it — but it is stamped owner-declared wherever it renders.

    `credits` is `credit_summary` of the reset rung's rows, carried as the
    flag's `credits` field (None where the rung did not run): it changes no
    colour and says what reset credits are in hand and how many accounts
    were not asked."""
    now = time.time() if now is None else now
    axes = dict(axes or {})
    for name in ("money", "reach", "policy", "declared"):
        axes.setdefault(name, None)
    measured = [axes[n] for n in ("money", "reach", "policy")
                if axes[n] and axes[n]["colour"] != GREY]
    declared = axes["declared"]
    headline, colour = None, None
    for axis in measured:
        if colour is None or _RANK[axis["colour"]] > _RANK[colour]:
            headline, colour = axis, axis["colour"]
    declared_ignored, declared_unmeasured = False, None
    if declared and declared["colour"] == GREY:
        # A DECLARED GREY IS A COVERAGE STATEMENT, NOT A RANK. GREY is off the
        # ordering by this module's own law, so `_RANK` cannot be asked about
        # it — it raised KeyError here and took the whole fold down for any
        # family the owner had declared unmeasured while something measured
        # it. The owner saying "nothing measures this" cannot outrank, improve
        # or worsen a reading that exists; it is CARRIED and SAID instead.
        declared_unmeasured = declared["cause"]
        if colour is None:
            headline, colour = declared, GREY
    elif declared:
        if colour is None:
            headline, colour = declared, declared["colour"]
        elif _RANK[declared["colour"]] > _RANK[colour]:
            headline, colour = declared, declared["colour"]
        elif _RANK[declared["colour"]] < _RANK[colour]:
            # WORSE-ONLY. A declaration that would improve a measured colour is
            # RECORDED and REFUSED, never dropped in silence: the owner is owed
            # the fact that his declaration is no longer the binding reading.
            declared_ignored = True
    money = axes["money"]
    coverage = (money or {}).get("coverage")
    if colour is None:
        colour = GREY
        cause = "; ".join(a["cause"] for a in
                          (axes["money"], axes["reach"]) if a) or \
            "nothing measures this family"
        cause_id = (money or {}).get("cause_id") or "grey:unmeasured"
        provenance, axis_name, measured_at = "unmeasured", None, None
    else:
        cause, cause_id = headline["cause"], headline["cause_id"]
        axis_name = headline["axis"]
        provenance = ("owner-declared" if axis_name == "declared"
                      else "derived" if axis_name == "policy"
                      else headline.get("provenance") or "measured")
        measured_at = headline.get("measured_at")
    at, kind, source = expiry([axes[n] for n in
                               ("money", "reach", "policy", "declared")],
                              headline=headline)
    behaviour = dict(BEHAVIOUR[colour])
    if cause_id == "money:ondemand-off-spend-down" and colour == ORANGE:
        behaviour["say"] = _SPEND_DOWN_SAY
    return {"family": family, "colour": colour, "axis": axis_name,
            "declared_unmeasured": declared_unmeasured,
            "axes": {n: (axes[n]["colour"] if axes[n] else None)
                     for n in ("money", "reach", "policy", "declared")},
            "cause": cause, "cause_id": cause_id,
            "measured_at": measured_at,
            "reading_age_s": (round(now - measured_at)
                              if measured_at else None),
            "coverage": coverage,
            "money_provenance": ((money or {}).get("provenance") or
                                 ("measured" if money and money.get("colour") != GREY
                                  else "unmeasured")),
            "capped_by_coverage": bool((money or {}).get("capped_by_coverage")),
            "coverage_why": (money or {}).get("coverage_why"),
            "abundance_read": (money or {}).get("abundance_read"),
            "reach_cause": (axes["reach"] or {}).get("cause"),
            "reach_expires_at": (axes["reach"] or {}).get("expires_at"),
            "expires_at": at, "expires_kind": kind, "expires_source": source,
            "provenance": provenance,
            "declared_ignored": declared_ignored,
            # WHICH DECLARATION THIS FOLD READ, by its own instant: the key
            # `pending_declarations` matches a saved record against (task/4027).
            "declared_at": (declared or {}).get("measured_at"),
            "credits": credits,
            "behaviour": behaviour}


def overall(flags, critical=None):
    """The fleet headline -> {colour, family, until, scope}.

    THE WORST FAMILY THE CRITICAL PATH NEEDS, never an average and never a
    global maximum: a family nobody's work touches cannot stop anybody. With
    no critical set supplied the scope is every family, and the record SAYS
    that rather than letting a caller read a narrow answer off a wide one."""
    names = [f for f in (critical or sorted(flags))
             if f in flags]
    scope = "critical-path" if critical else "all-families"
    ranked = [flags[f] for f in names if flags[f]["colour"] != GREY]
    if not ranked:
        return {"colour": GREY, "family": None, "until": None, "scope": scope,
                "capacity": BEHAVIOUR[GREY]["capacity"]}
    worst = max(ranked, key=lambda fl: (_RANK[fl["colour"]],
                                        fl["expires_at"] is None))
    return {"colour": worst["colour"], "family": worst["family"],
            "until": worst["expires_at"], "scope": scope,
            "capacity": BEHAVIOUR[worst["colour"]]["capacity"]}


def fold(inputs, now=None):
    """Every reading -> the snapshot payload. PURE: arguments in, dict out,
    no file and no network on this path, so one world folds identically in a
    test and on the fleet."""
    now = time.time() if now is None else now
    inputs = dict(inputs or {})
    ceiling = inputs.get("ceiling")
    money_rows = inputs.get("money") or {}
    money_ts = inputs.get("money_measured_at") or {}
    money_fresh = inputs.get("money_fresh") or {}
    upstream = inputs.get("upstream") or {}
    history = inputs.get("anthropic_history")
    declarations = inputs.get("declarations")
    abundance = inputs.get("abundance") or {}
    credits = inputs.get("reset_credits") or {}
    # THE OWNER HORIZON JOINS BY THE RECORD'S OWN RULE (task/2935). The
    # posting pass folds its raw family rows before `record` composes the
    # durable ones, so it hands over the owner table (and the prior records
    # `record` composes against) and the fold asks the SAME helper the durable
    # composition asks. Two rules here made the flag `can_spend` reads and the
    # record every other surface reads name different resets. Without the
    # table the records are already composed ones and are read as written.
    vendor_resets = inputs.get("vendor_resets")
    before_all = inputs.get("upstream_before") or {}
    # THE RUNWAY IS AN INPUT, and `_RUNWAY_STEP_TABLE` is its only effect.
    runway = inputs.get("runway") or {}
    # THE POOL'S WEEKLY PACE IS AN INPUT TOO (task/3880), and
    # `_pool_pace_step` is its only effect: the native family reads its pool
    # as a weekly burn against a horizon, not a 5h headroom, so the pace folds
    # into the family colour here and never again.
    pool_pace = inputs.get("pool_pace") or {}
    turn_walls = inputs.get("turn_walls") or {}
    certifications = inputs.get("local_certifications")
    local = local_families()
    flags = {}
    for family in families():
        rows = money_rows.get(family, None)
        record = upstream.get(family)
        if isinstance(record, dict) and isinstance(vendor_resets, dict):
            from . import proxywatch
            record = proxywatch.join_owner_reset(
                record, vendor_resets.get(family), now,
                before=before_all.get(family))
        # A LOCAL FAMILY WITH NO MONEY READER READS ITS CERTIFICATION, and
        # only then: a reader, where one exists, is a measurement and answers.
        money = (derive_certified_local(family, certifications, now=now,
                                        local=local, record=record)
                 if family not in money_rows else None) or derive_money(
            family, rows if family in money_rows else None,
            ceiling=ceiling, abundance=abundance.get(family),
            measured_at=money_ts.get(family),
            fresh=money_fresh.get(family, True),
            # ONE DOOR PICKS THE ACCOUNT for every family with a proxy
            # seat; the native credential has none, so it is read as a
            # pool of accounts that each carry a seat of their own.
            rotated=(family != NATIVE_FAMILY),
            reset_credits=credits.get(family))
        # A vendor refusal outranks older pool headroom. With on-demand off,
        # the vendor's actual refusal is REACH; other families retain MONEY.
        wall = derive_quota_wall(family, record)
        base = (wall if wall and wall["axis"] == "money"
                and money["colour"] != RED else money)
        # A REFUSAL ON A SEAT'S OWN TURN IS A MEASURED WALL TOO (task/3587),
        # read where the proxy's record cannot see it: the proxy answered,
        # the vendor said no.
        turn_money, turn_reach = derive_turn_wall(family,
                                                  turn_walls.get(family))
        if turn_money and base["colour"] != RED:
            base = turn_money
        reach = derive_reach(family, record, now=now)
        if wall and wall["axis"] == "reach" and \
                (reach is None or reach["colour"] != RED):
            reach = wall
        if turn_reach and (reach is None or reach["colour"] != RED):
            reach = turn_reach
        axes = {
            "money": _pool_pace_step(_runway_step(base, runway.get(family)),
                                     pool_pace.get(family)),
            "reach": reach,
            "policy": (derive_policy(history, now=now)
                       if family == NATIVE_FAMILY else None),
            "declared": derive_declared(family, declarations, now=now),
        }
        flags[family] = compose(
            family, axes, now=now,
            credits=credit_summary(credits.get(family))
            if family in credits else None)
    return {"v": 1, "fold_version": FOLD_VERSION, "ts": now,
            "families": flags,
            "overall": overall(flags, inputs.get("critical")),
            "readers": {"money": sorted(money_rows),
                        "policy": [NATIVE_FAMILY] if history else [],
                        "reach": sorted(upstream),
                        "turn_walls": sorted(turn_walls),
                        "runway": sorted(f for f, r in runway.items()
                                         if isinstance(r, dict)),
                        "pool_pace": sorted(f for f, p in pool_pace.items()
                                            if isinstance(p, dict)),
                        "declared": sorted(
                            ((declarations or {}).get("families") or {})),
                        "certified_local": sorted(
                            f for f, fl in flags.items()
                            if fl.get("money_provenance") == CERTIFIED_LOCAL)}}


# ------------------------------------------------------------------ the store

def read_declarations(path=None):
    """The owner's declarations, or an empty record. Never raises: a typo in a
    hand-edited file must not be able to take the fold down."""
    try:
        with pk.open_regular(path or declarations_path(), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {"families": {}}
    if not isinstance(payload, dict) or not isinstance(
            payload.get("families"), dict):
        return {"families": {}}
    return payload


def declare(family, colour, until, why=None, now=None, path=None):
    """Record an owner declaration -> (ok, err). Last-writer-wins per family."""
    now = time.time() if now is None else now
    if family not in families():
        return False, "unknown family %r" % family
    colour = str(colour).upper()
    if colour not in COLOURS:
        return False, "unknown colour %r" % colour
    stamp = pk.parse_ts_epoch(until) if isinstance(until, str) else until
    if not stamp:
        return False, "a declaration needs an expiry instant (--until)"
    payload = read_declarations(path)
    payload.setdefault("families", {})[family] = {
        "colour": colour, "until": stamp, "declared_at": now,
        "why": why or "declared by the owner"}
    try:
        target = path or declarations_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        pk.atomic_write(target, json.dumps(payload, sort_keys=True))
    except OSError as exc:
        return False, str(exc)
    return True, None


def pending_declarations(declarations, flags=None, ts=None, now=None):
    """The owner's live declarations that the snapshot has NOT folded yet ->
    {family: {colour, until, declared_at, why, folds_by}} (task/4027).

    `flags` and `ts` are the snapshot's families and its instant, or None
    when no fresh snapshot exists. `folds_by` is the watchdog's next pass
    after `ts`, or None when that is not known.

    THE SNAPSHOT SAYS WHICH RECORD IT FOLDED. The watchdog stamps `ts` at the
    START of its pass and reads the declarations near the end, so a record
    younger than `ts` can already be folded, and the instant cannot decide.
    A record is folded when the flag's `declared_at` and declared colour are
    the record's own. A snapshot written before the flag carried
    `declared_at` reads PENDING for one pass at most, never FOLDED on a
    guess.

    PENDING IS NOT A COLOUR. Nothing here changes a flag: the reading stays
    the fold's, and worse-only and `declared_ignored` stay the fold's rules.
    A reader shows the record BESIDE the reading, as declared and pending.
    The families and the expiry are the fold's own (`derive_declared`), so a
    record the fold would never read is never pending."""
    now = time.time() if now is None else now
    from . import proxywatch
    said = (declarations or {}).get("families") or {}
    folds_by = ts + proxywatch.INTERVAL_S if _finite(ts) else None
    out = {}
    for family in families():
        axis = derive_declared(family, declarations, now=now) \
            if family in said else None
        if axis is None:
            continue
        flag = (flags or {}).get(family)
        flag = flag if isinstance(flag, dict) else {}
        if (flag.get("axes") or {}).get("declared") == axis["colour"] \
                and flag.get("declared_at") == axis["measured_at"]:
            continue
        out[family] = {"colour": axis["colour"], "until": axis["expires_at"],
                       "declared_at": axis["measured_at"],
                       "why": axis["cause"], "folds_by": folds_by}
    return out


def improving_declaration(colour, flag):
    """The refusal sentence when a colour would lighten a measured axis, else None.

    Equal is admitted: the fold keeps a declaration that matches the measured
    colour and ignores only a strictly better one. A family with no measured
    colour is admitted. The sentence names the worst measured axis, not the
    headline. A colour off the rank (GREY) is not a lightening and is not indexed.
    """
    colour = str(colour).upper()
    if colour not in _RANK:
        return None
    measured = None
    axes = (flag or {}).get("axes") or {}
    for axis in ("money", "reach", "policy"):
        got = axes.get(axis)
        if got in _RANK and (measured is None or _RANK[got] > _RANK[measured]):
            measured = got
    if measured is not None and _RANK[colour] < _RANK[measured]:
        return ("Refused: %s is not worse than the measured %s. A "
                "declaration may only make a colour worse. To let "
                "one project run lighter, raise its share."
                % (colour, measured))
    return None


def read_local_certifications(path=None):
    """The local-family certifications and what ties each to its operator ->
    {"families": {...}, "operators": {family: seat or None}, "bound": {key:
    seat or None}}, plus `roster_unreadable` when the roster could not be
    read, or {"families": {}, "unreadable": why} for a file that exists and
    cannot be read. Never raises, so a bad file can never take the fold down.

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS. No file is no certification,
    and every local family reads GREY for its missing reader; a file that
    will not open, parse or hold the right shape reads GREY with a cause that
    names it, because that GREY has a repair and the other has none.

    THE OPERATOR AND THE BINDING ARE READ HERE, NEVER FROM THE FILE: each
    local family's declared operator seat (`local_operator`), and, for every
    session key a record carries, the seat the roster binds it to now
    (`_session_bindings`). This is the only reader both production folds
    take (`read_inputs` and the posting pass), so the fold decides from
    these and reads no file itself."""
    got = _read_certification_file(path)
    if got.get("unreadable"):
        return got
    rows = [rec for rec in got["families"].values() if isinstance(rec, dict)]
    bound, why = _session_bindings(rec.get("session_key") for rec in rows)
    got.update(operators={f: local_operator(f) for f in local_families()},
               bound=bound)
    if why:
        got["roster_unreadable"] = why
    return got


def _read_certification_file(path=None):
    """The certification file alone -> {"families": {...}}, or with an
    `unreadable` entry naming the file and why. Never raises."""
    target = path or local_certifications_path()
    try:
        with pk.open_regular(target, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return {"families": {}}
    except (OSError, ValueError) as exc:
        return {"families": {}, "unreadable": "%s (%s)" % (
            target, exc.strerror if isinstance(exc, OSError) and exc.strerror
            else type(exc).__name__)}
    if not isinstance(payload, dict) or not isinstance(
            payload.get("families"), dict):
        return {"families": {}, "unreadable": "%s (not a table of "
                "families)" % target}
    return {"families": payload["families"]}


def _not_local(family):
    """Why `family` cannot be certified, or None when it is local."""
    local = local_families()
    if family in local:
        return None
    return ("%r is not a local family: only a family whose every pool row, "
            "its default included, names its endpoint by base_url_from, "
            "served from the operator's own GPU, can be certified (local "
            "families: %s)"
            % (family, ", ".join(local) or "none"))


def _until_instant(until, now):
    """(epoch, None) or (None, why) for a certification's `--until`.

    A DURATION in helm's one TTL grammar (`seats_common.ttl_flag`: 2h, 90m,
    1d, 3600), resolved against the producer's own instant, or an INSTANT: an
    epoch number from a library caller, or ISO-8601 UTC read as `helm burn
    declare` reads it. The resolved instant must be after `now` and at most
    `CERTIFICATION_CEILING_S` past it.

    A BARE NUMBER IS A DURATION IN SECONDS, as for every `--ttl` in helm, so
    an epoch typed as an instant resolves decades out; the ceiling refuses
    it, and the refusal says how an instant is written."""
    bare = False
    if _finite(until):
        stamp = float(until)
    else:
        from .seats_common import TTL_FORMS, ttl_flag
        secs = ttl_flag(["--ttl", until], None)[0] \
            if isinstance(until, str) else None
        bare = bool(secs) and until.strip().isdigit()
        stamp = now + secs if secs else pk.parse_ts_epoch(until)
        if stamp is None:
            return None, ("--until %r is neither a duration (%s) nor an "
                          "ISO-8601 UTC instant (YYYY-MM-DDTHH:MM:SSZ)"
                          % (until, TTL_FORMS))
    if stamp <= now:
        return None, ("--until %s is not in the future: a certification that "
                      "ends by now certifies nothing; --revoke ends one"
                      % _iso(stamp))
    if stamp - now > CERTIFICATION_CEILING_S:
        return None, ("--until %r ends %.1fh from now, past the %dh ceiling "
                      "on one certification: renew it instead of holding it "
                      "long%s" % (until, (stamp - now) / 3600.0,
                                  CERTIFICATION_CEILING_S // 3600,
                                  "; a bare number is a duration in seconds, "
                                  "so an instant is written as ISO-8601 UTC "
                                  "(YYYY-MM-DDTHH:MM:SSZ)" if bare else ""))
    return float(stamp), None


def _write_certification(family, build, path=None):
    """Replace `family`'s record with `build(prior record)` under the file's
    lock -> (ok, err). Last-writer-wins per family, as `declare` is; the lock
    keeps two families certified at once from dropping each other's row."""
    import fcntl
    target = path or local_certifications_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                # AN UNREADABLE FILE IS REPLACED, never merged into: its
                # rows cannot be read, and the write is what repairs it
                families = dict(_read_certification_file(target)["families"])
                prior = families.get(family)
                families[family] = build(
                    prior if isinstance(prior, dict) else {})
                pk.atomic_write(target, json.dumps({"families": families},
                                                   sort_keys=True))
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        return False, str(exc)
    return True, None


def _not_the_operator(family, by, session):
    """Why seat `by`, acting from harness session `session`, may not certify
    or revoke `family` -> a sentence, or None when it is the family's
    operator seat and the roster binds that session's key to it now: the
    same predicate the fold applies to the record this would write."""
    key = _session_key(session)
    bound, why = _session_bindings([key])
    return _unbound(family, local_operator(family), by, key, bound, why)


def certify_local(family, until, by, session, note=None, now=None,
                  path=None):
    """Record that the OPERATOR SEAT `by`, acting from its own roster-bound
    harness `session`, certifies LOCAL `family` as running on the operator's
    own hardware, optimally, until `until` -> (ok, err).

    The record stamps the session's key (`_session_key`), never the id.
    Refused for a family that is not local, a seat that is not the family's
    declared operator, a session the roster does not bind to it, and an
    `until` that is unreadable or not in the future. The verb is the door
    that PROVES `session` is the caller's own (`actors.resolve_actor_reason`);
    this checks the binding the fold will check."""
    now = time.time() if now is None else now
    why = _not_local(family) or _not_the_operator(family, by, session)
    if why:
        return False, why
    stamp, why = _until_instant(until, now)
    if why:
        return False, why
    return _write_certification(family, lambda _prior: {
        "family": family, "certified_at": now, "until": stamp, "by": by,
        "session_key": _session_key(session), "note": note or None}, path)


def revoke_local(family, by, session, note=None, now=None, path=None):
    """End LOCAL `family`'s certification now -> (ok, err), from the same
    door `certify_local` takes: the operator seat, in its own roster-bound
    `session`.

    The revocation is recorded with who, from which session key, and when,
    and `until` becomes that instant, so every later fold reads the family
    GREY. The certified fields stay on the row for the record; a fresh
    `certify_local` replaces it."""
    now = time.time() if now is None else now
    why = _not_local(family) or _not_the_operator(family, by, session)
    if why:
        return False, why
    return _write_certification(family, lambda prior: dict(
        prior, family=family, until=now, revoked_at=now, revoked_by=by,
        revoked_session_key=_session_key(session),
        revoke_note=note or None), path)


def write_snapshot(inputs=None, now=None, path=None):
    """Fold and persist -> True/False. BEST EFFORT AND NEVER RAISES: a budget
    reader that throws must not take the fleet's watchdog down with it."""
    try:
        payload = fold(read_inputs(now=now) if inputs is None else inputs,
                       now=now)
        target = path or snapshot_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        pk.atomic_write(target, json.dumps(payload, sort_keys=True))
        return True
    except Exception:                       # noqa: BLE001
        return False


def cached_flags(max_age=None, now=None, path=None):
    """(flags, age_s) from the snapshot, or ({}, None).

    THE READ PATH NEVER PROBES — the `cached_budget` contract, byte for byte.
    A stale or absent snapshot yields nothing rather than an old colour, so no
    consumer can answer GREEN off a file nobody has refreshed."""
    now = time.time() if now is None else now
    bound = max_age_s() if max_age is None else max_age
    try:
        with pk.open_regular(path or snapshot_path(), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}, None
    if not isinstance(payload, dict) or payload.get("fold_version") != FOLD_VERSION:
        return {}, None
    if not isinstance(payload.get("families"), dict):
        return {}, None
    age = now - float(payload.get("ts") or 0)
    if age < 0 or age > bound:
        return {}, None
    return payload["families"], age


def cached_snapshot(max_age=None, now=None, path=None):
    """The whole payload plus its age, for the surfaces that render overall as
    well as the per-family rows."""
    now = time.time() if now is None else now
    flags, age = cached_flags(max_age=max_age, now=now, path=path)
    if not flags:
        return None, None
    with pk.open_regular(path or snapshot_path(), encoding="utf-8") as f:
        return json.load(f), age


def family_flag(family, max_age=None, now=None, path=None):
    """ONE family's flag, or None when the snapshot is absent or stale.

    THIS IS THE STABLE READ API the routing verb and the scheduler's admission
    function consume. Neither re-derives a colour; both read this."""
    flags, _age = cached_flags(max_age=max_age, now=now, path=path)
    return flags.get(family)


def can_spend(family, max_age=None, now=None, path=None):
    """Can this family spend one turn now -> yes/no/unknown, with why/until.

    This asks ``family_flag`` rather than a meter, so display, dispatch and this
    answer share one freshness decision. REACH ORANGE/RED and any folded RED
    are refusals. A fresh non-refusing flag is YES only when its money axis is
    a direct measurement; GREY, an absent cached fold and a derived ledger
    estimate are UNKNOWN, never permission.
    """
    flag = family_flag(family, max_age=max_age, now=now, path=path)
    if not flag:
        return {"family": family, "answer": "unknown",
                "why": "no fresh burn-flag snapshot exists", "until": None}
    reach = (flag.get("axes") or {}).get("reach")
    if reach in (ORANGE, RED):
        return {"family": family, "answer": "no",
                "why": flag.get("reach_cause") or flag.get("cause"),
                "until": flag.get("reach_expires_at")}
    if flag.get("colour") == RED:
        return {"family": family, "answer": "no", "why": flag.get("cause"),
                "until": flag.get("expires_at")}
    if flag.get("colour") == GREY:
        return {"family": family, "answer": "unknown",
                "why": flag.get("cause"), "until": flag.get("expires_at")}
    if flag.get("money_provenance") != "measured" \
            or flag.get("capped_by_coverage") \
            or (flag.get("axes") or {}).get("money") == GREY:
        why = ("money coverage is incomplete" if flag.get("capped_by_coverage")
               else "money is %s, not a fresh direct measurement" %
                    flag.get("money_provenance"))
        return {"family": family, "answer": "unknown",
                "why": flag.get("coverage_why") or why,
                "until": flag.get("expires_at")}
    return {"family": family, "answer": "yes", "why": flag.get("cause"),
            "until": flag.get("expires_at")}


# ------------------------------------------------------------------ surfaces

def _when(at, now):
    if not at:
        return "unknown when"
    secs = at - now
    if secs <= 0:
        return "now"
    if secs < 3600:
        return "in %dm" % (secs // 60)
    if secs < 86400:
        return "in %.1fh" % (secs / 3600.0)
    return "in %dd %02dh" % (secs // 86400, (secs % 86400) // 3600)


def line(snap, now=None):
    """THE ONE LINE, fleet-wide, capped — overall colour, the bottleneck family
    and where to look. Never one line per family: seven families crossing at
    once is still one thing to say, because the reader acts on the worst one."""
    if not snap:
        return "burn flags: NOT MEASURED — run helm proxywatch --post"
    now = time.time() if now is None else now
    over = snap.get("overall") or {}
    colour = over.get("colour") or GREY
    fam = over.get("family")
    text = "burn %s" % colour
    if fam:
        text += "; tightest %s, changes %s" % (fam, _when(over.get("until"),
                                                          now))
    greys = sorted(f for f, fl in (snap.get("families") or {}).items()
                   if fl.get("colour") == GREY)
    if greys:
        text += "; not measured: %s" % ", ".join(greys)
    return (text + "; read helm burn")[:180]


def line_key(snap):
    """WHAT THE ONE LINE SAYS, with the reader's clock taken out.

    THE LINE CARRIES A COUNTDOWN, so comparing rendered lines makes every pass
    a change: a change-latched board row written that way is written on every
    tick, which is the per-pass noise the latch exists to stop. The key is the
    READING — the colour, the bottleneck, the instant it changes and which
    families are unmeasured — so two ticks over one reading write once."""
    if not snap:
        return "unmeasured"
    over = snap.get("overall") or {}
    greys = sorted(f for f, fl in (snap.get("families") or {}).items()
                   if fl.get("colour") == GREY)
    return "|".join([str(over.get("colour")), str(over.get("family")),
                     str(over.get("until")), ",".join(greys)])


def watch_notice(flags, prior):
    """(body, colours) for the watchdog pass: the room post to make THIS pass,
    or (None, colours) when no family crossed.

    DEDUP IS ON THE COLOUR, NEVER ON THE NUMBERS — the rule this inherits from
    the announcer it replaces. Percentages move every pass by construction;
    what the room needs to hear once is that a family CROSSED.

    ONE POST, however many families crossed: the room gets a crossing report,
    not a per-family fan-out."""
    colours = {f: fl["colour"] for f, fl in (flags or {}).items()}
    prior = prior or {}
    crossed = []
    for family in sorted(colours):
        was, is_ = prior.get(family), colours[family]
        if was == is_:
            continue
        if is_ in (ORANGE, RED) or (was in (ORANGE, RED) and is_ != GREY):
            crossed.append((family, was, is_))
    if not crossed:
        return None, colours
    body = ["%s burn flags CHANGED" % NOTICE_TAG]
    for family, was, is_ in crossed:
        fl = flags[family]
        body.append("  %-11s %s -> %s (%s) %s"
                    % (family, was or "unknown", is_, fl["axis"] or "unmeasured",
                       fl["cause"]))
        body.append("      %s" % fl["behaviour"]["say"])
    return "\n".join(body) + "\n", colours


def render(snap, now=None):
    """The owner-facing table. NO IDENTITY, by construction — nothing in the
    snapshot carries one."""
    now = time.time() if now is None else now
    if not snap:
        return ["helm burn: NOT MEASURED — no fresh burn-flag snapshot. The "
                "writer is the POSTING watchdog pass, not the bare status "
                "read: `helm proxywatch --post`."]
    over = snap.get("overall") or {}
    out = ["OVERALL %s%s — %s (%s scope) — capacity %s project(s)"
           % (over.get("colour"),
              "" if not over.get("family") else " via " + over["family"],
              _when(over.get("until"), now), over.get("scope"),
              over.get("capacity")),
           "  %-11s %-7s %-9s %-11s %s"
           % ("family", "colour", "axis", "changes", "why")]
    for family in sorted(snap.get("families") or {}):
        fl = snap["families"][family]
        out.append("  %-11s %-7s %-9s %-11s %s"
                   % (family, fl["colour"], fl["axis"] or "-",
                      _when(fl["expires_at"], now), fl["cause"]))
        marks = []
        if fl["provenance"] == "owner-declared":
            marks.append("DECLARED by the owner, not measured")
        if fl["provenance"] == "derived":
            marks.append("DERIVED from a proxy measure, not a direct reading")
        if fl.get("money_provenance") == CERTIFIED_LOCAL:
            marks.append("money CERTIFIED by the seat that runs the local "
                         "hardware, not a vendor measurement")
        if fl.get("declared_ignored"):
            marks.append("an owner declaration is OUTRANKED by this measured "
                         "reading and did not apply")
        if fl.get("declared_unmeasured"):
            marks.append("the owner DECLARED this family unmeasured (%s); a "
                         "declared GREY is a coverage statement and is not "
                         "ranked against a reading"
                         % fl["declared_unmeasured"])
        if fl.get("capped_by_coverage"):
            cov = fl.get("coverage") or {}
            marks.append("GREEN unreachable: %d of %d accounts unread (%s)"
                         % (cov.get("unread", 0), cov.get("total", 0),
                            cov.get("unread_why") or "no reason recorded"))
        elif fl.get("abundance_read") is False:
            marks.append("GREEN not claimed: no abundance reading exists on "
                         "this host yet")
        for mark in marks:
            out.append("      %s" % mark)
    return out


def render_why(flag, now=None):
    """One family's reading, its axes, its provenance and its age."""
    now = time.time() if now is None else now
    if not flag:
        return ["helm burn why: NOT MEASURED — no fresh snapshot for that "
                "family"]
    out = ["%s %s (%s, set by the %s axis)"
           % (flag["family"], flag["colour"], flag["provenance"],
              flag["axis"] or "no"),
           "  do: %s" % flag["behaviour"]["say"],
           "  because: %s" % flag["cause"],
           "  handle: %s" % flag["cause_id"],
           "  changes: %s (%s; %s)"
           % (_when(flag["expires_at"], now), flag["expires_kind"],
              flag["expires_source"] or "no source recorded"),
           "  reading age: %s"
           % ("unknown" if flag["reading_age_s"] is None
              else "%ds" % flag["reading_age_s"])]
    for axis in ("money", "reach", "policy", "declared"):
        out.append("  axis %-9s %s" % (axis, flag["axes"][axis] or "silent"))
    if flag.get("declared_unmeasured"):
        out.append("  the owner declared this family unmeasured: %s (a "
                   "declared GREY states coverage and is not ranked)"
                   % flag["declared_unmeasured"])
    cov = flag.get("coverage")
    if cov:
        out.append("  coverage: %d of %d accounts read%s"
                   % (cov.get("measured", 0), cov.get("total", 0),
                      ("; unread: " + cov["unread_why"])
                      if cov.get("unread_why") else ""))
    return out


# ------------------------------------------------------------- the live reads

def read_inputs(now=None):
    """Gather every reading this host can answer, from FILES ONLY.

    NO NETWORK ON ANY PATH IN THIS MODULE. The probes belong to the watchdog
    pass and the creds cycle; this reads what they already wrote, which is why
    a vendor outage can never stall the fold."""
    now = time.time() if now is None else now
    inputs = {"money": {}, "money_measured_at": {}, "money_fresh": {},
              "upstream": {}, "anthropic_history": None, "abundance": {},
              "declarations": read_declarations(),
              "local_certifications": read_local_certifications(),
              "ceiling": None}
    try:
        from . import moneyread
        snapshot, error = moneyread.read_snapshot()
        generic = moneyread.inputs(snapshot, now=now, max_age_s=max_age_s(),
                                   error=error)
        for name in ("money", "money_measured_at", "money_fresh"):
            inputs[name].update(generic[name])
    except Exception:                       # noqa: BLE001
        pass
    try:
        from . import codexbudget
        rows, age = codexbudget.cached_budget(max_age_s=max_age_s(), now=now)
        if rows is not None:
            inputs["money"]["codex"] = rows
            inputs["money_measured_at"]["codex"] = now - (age or 0)
            inputs["money_fresh"]["codex"] = True
        inputs["ceiling"] = codexbudget.ceiling_pct()
    except Exception:                       # noqa: BLE001
        pass
    try:
        rows, latest = anthropic_money_rows(usage_history(), now=now)
        if rows:
            inputs["money"][NATIVE_FAMILY] = rows
            inputs["money_measured_at"][NATIVE_FAMILY] = latest
            inputs["money_fresh"][NATIVE_FAMILY] = bool(
                latest and (now - latest) <= max_age_s())
        inputs["anthropic_history"] = usage_history()
    except Exception:                       # noqa: BLE001
        pass
    try:
        inputs["upstream"] = upstream_block(now=now)
    except Exception:                       # noqa: BLE001
        pass
    try:
        from . import turnwall
        inputs["turn_walls"] = turnwall.family_walls(now=now)
    except Exception:                       # noqa: BLE001
        pass
    try:
        from . import codexpace
        runway = codexpace.cached_fold_input(now=now)
        if runway:
            inputs["runway"] = {codexpace.FAMILY: runway}
    except Exception:                       # noqa: BLE001
        pass
    try:
        # THE POOL'S WEEKLY PACE, read the same way the runway is: the
        # watchdog's own snapshot, best-effort. `fold` consumes it as
        # `pool_pace` and never reaches for it itself (claudepace imports
        # this module, so the fold stays pure).
        from . import claudepace
        snap = claudepace.cached(now=now)
        pool = (snap or {}).get("pool")
        if pool:
            inputs["pool_pace"] = {claudepace.FAMILY: pool}
    except Exception:                       # noqa: BLE001
        pass
    return inputs


def upstream_block(now=None, state=None):
    """The persisted family records, or {} when the watch state is absent,
    unreadable or STALE.

    A STALE WATCH STATE CONTRIBUTES NOTHING rather than an old dark reading:
    the four hours a seat spent written off on a reading nobody re-took is
    exactly the failure this refuses to reproduce."""
    from . import proxywatch
    now = time.time() if now is None else now
    if state is None:
        state, err = proxywatch._read_watch_state()
        if err or not state:
            return {}
    age = now - float(state.get("ts") or 0)
    if age < 0 or age > max_age_s():
        return {}
    records, err = proxywatch.upstream_records(state)
    return {} if err else records


def usage_history(path=None):
    """Freshest observation per NATIVE account from the creds probe cycle's
    history log. NEVER probes: no log means no rows, and no rows means GREY —
    which is the honest answer, unlike a fabricated one.

    THE PATH IS RESOLVED PER CALL, not at import: a caller under a fixture
    home must read that fixture's log and never this machine's."""
    best = {}
    for row in _history_lines(path):
        acct = row.get("account")
        if acct not in best or str(row.get("probed_at") or "") > str(
                best[acct].get("probed_at") or ""):
            best[acct] = row
    return [best[a] for a in sorted(best)]


def _history_lines(path=None, provider=NATIVE_FAMILY):
    """Every observation for one provider, oldest line first.

    Native history keeps its inherited cache path; generic readers share the
    redacted money history. The provider filter is mandatory provenance, not a
    caller-side convention that can merge two credentials with the same hash."""
    from . import brief, moneyread
    target = path or (brief.usage_history_path() if provider == NATIVE_FAMILY
                      else moneyread.history_path())
    try:
        fh = pk.open_regular(target, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for raw in fh:
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if (row.get("provider") or "") != provider:
                continue
            if not row.get("account"):
                continue
            yield row


def unread_streaks(path=None, now=None):
    """How long helm's own copy of each credential has been answering NO
    READING — one row of MASKED metadata per account in a streak.

    THE SEAM THIS MEASURES, and it is the one nothing in this tree watched.
    `keepalive` rolls the homes whose refresh chain it can still grant on, and
    the cadence rung reports THE WRITER: is the timer installed, did it grant
    recently. A home whose chain is SPENT is skipped by that loop forever — so
    the cadence rung stays green, every other home rolls forward, and that one
    copy rots. Nothing asked the other question, the one about the OUTCOME:
    how long has an account been answering no reading at all. A copy sat
    unreadable for weeks while the credential itself kept serving turns, and
    the only place it surfaced was a colour that then rationed the fleet.

    NEITHER HALF IS ASSERTED FROM ABSENCE. A streak is reported only where the
    probe is still WRITING rows for that account after the last readable one —
    `probes_since` is that count, and it is what separates "helm's copy is
    dead and nobody noticed" from "nothing has looked lately", which is a fact
    about the probe and not about the credential. An account with no readable
    row anywhere in the log carries `last_read_at: None` and its streak is
    measured from the OLDEST line, which is a floor on the truth and says so.

    NO IDENTITY LEAVES THIS FUNCTION. The caller is a doctor rung that prints,
    so the account is masked HERE rather than at the render — the module's own
    law, applied at the only door that could otherwise carry an address out."""
    from . import accounts
    now = time.time() if now is None else now
    seen = {}
    for row in _history_lines(path):
        acct = row["account"]
        probed = pk.parse_ts_epoch(row.get("probed_at"))
        if not probed:
            continue
        rec = seen.setdefault(acct, {"last_read_at": None, "newest_at": None,
                                     "oldest_at": probed, "status": "",
                                     "at": []})
        rec["oldest_at"] = min(rec["oldest_at"], probed)
        rec["at"].append(probed)
        readable = str(row.get("status") or "").startswith(READ_STATUSES)
        if readable:
            rec["last_read_at"] = max(rec["last_read_at"] or 0, probed)
        if rec["newest_at"] is None or probed >= rec["newest_at"]:
            rec["newest_at"] = probed
            rec["status"] = str(row.get("status") or "")
            rec["readable"] = readable
    out = []
    for acct in sorted(seen):
        rec = seen[acct]
        if rec.get("readable", True):
            continue
        # THE PROBES THAT RAN AND STILL SAW NOTHING. Counted from the instants
        # kept during the one pass, because a row only counts as "since" once
        # the last readable instant is known — and that is not known until the
        # whole log has been read.
        since_at = rec["last_read_at"]
        rec["probes_since"] = sum(1 for t in rec["at"] if t > (since_at or 0))
        base = since_at if since_at else rec["oldest_at"]
        out.append({"credential": accounts.mask_identity(acct),
                    "unread_s": max(0, int(now - base)),
                    "floor_only": since_at is None,
                    "last_read_at": since_at,
                    "newest_probe_at": rec["newest_at"],
                    "newest_probe_age_s": max(0, int(now - rec["newest_at"])),
                    "probes_since": rec["probes_since"],
                    "status": (redact_why(rec["status"]) if rec["status"]
                               else "no status recorded")})
    return out


#: THE NATIVE PROBE'S OWN TWO SPELLINGS FOR A ROW THAT CARRIES A READING.
#: `providers.NativeQuotaProvider._probe_one` writes `blocked` when the
#: account's own gauges are at 100% and `allowed` on every other successful
#: read; both arrive WITH gauges, and every other spelling it writes
#: (`reauth-needed`, `needs_reauth`, `http_*`, `network-error`,
#: `no-credentials`) arrives with none. The vocabulary is the PRODUCER'S, so
#: it is named here rather than guessed at.
READ_STATUSES = ("allowed", "blocked")


def _label_seconds(label):
    """A gauge label's own units -> seconds, or None.

    The native gauges carry no window LENGTH, only a name, and the binding
    window is the longest one — so the length is read out of the label rather
    than assumed. An unparseable label yields None and the row falls back to
    contributing no instant."""
    text = str(label or "").strip()
    units = {"h": 3600, "d": 86400, "m": 60, "w": 604800}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        return None
    return int(text[:-1]) * units[text[-1]]


def _utilization_pct(gauge):
    value = gauge.get("utilization")
    return round(value * 100, 1) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) and math.isfinite(value) else None


def anthropic_money_rows(history, now=None, fresh_s=None, model=None):
    """(rows in the verdict's shape, the freshest reading's instant).

    A ROW WHOSE STATUS IS NOT `allowed` CARRIES `longest_pct: None`. A rejected
    or expired token says nothing about headroom, and a zero there would read
    as wide open — the precise inversion that routes work into a wall. The
    STALE rows are unread too: a reading older than the bound is not evidence
    about now.

    AN EXHAUSTED ACCOUNT IS A READ ACCOUNT AT ITS CEILING, NOT AN UNREAD ONE.
    The native probe answers `blocked` with the vendor's own gauges attached
    when an account is at 100%; reading that as unreadable threw the measured
    wall into the coverage gap, and the family's colour IMPROVED from RED to
    GREY because the accounts that were certainly spent were the ones dropped.
    Every account-wide enforced plan window is retained. Overage is a spend
    meter rather than a wall, and a model-scoped window is included only when
    this row is requested for that model. A gauge without utilization remains
    unread rather than becoming a measured zero. `_money_rows` projects the worst
    included utilization into the compatibility `longest_pct` field, so a
    shorter hard wall cannot hide behind a greener long window."""
    now = time.time() if now is None else now
    fresh_s = max_age_s() if fresh_s is None else fresh_s
    rows, stamps = [], []
    for row in history or ():
        probed = pk.parse_ts_epoch(row.get("probed_at"))
        status = str(row.get("status") or "")
        account = _gauge(row, POLICY_ACCOUNT_LABEL)
        fresh = bool(probed) and (now - probed) <= fresh_s
        if fresh:
            stamps.append(probed)
        if not fresh:
            state = "stale"
        elif not status.startswith(READ_STATUSES):
            state = status.split(" ", 1)[0] or "unreadable"
        elif not account or account.get("utilization") is None:
            state = "no-window"
        else:
            state = "ok"
        # ONLY A FRESH READ CARRIES NUMBERS. The money fold classifies an
        # account by its windows before its status word, so a stale row's
        # old 100% must not reach it as a current wall: the labels stay for
        # diagnosis and the percentages become unread, as moneyread already
        # does for a stale reading.
        current = state in ("ok", "no-window")
        windows = [{"label": g.get("label"),
                    "used_percent": _utilization_pct(g) if current else None,
                    "reset_at": g.get("reset"),
                    "seconds": _label_seconds(str(g.get("label")).split("-", 1)[0]),
                    "kind": g.get("kind")}
                   for g in (row.get("gauges") or ())
                   if g.get("kind") != "overage"
                   and ("-" not in str(g.get("label") or "")
                        or str(g.get("label")).endswith("-" + str(model)))]
        rows.append({"state": state,
                     "longest_pct": (round(account["utilization"] * 100, 1)
                                     if state == "ok" else None),
                     "windows": windows})
    return rows, (max(stamps) if stamps else None)


# ---------------------------------------------------------------- the verb

# THE USAGE LINE CARRIES ITS OWN ROOT VERB. Interpolating the synopsis after
# the program name leaves a command whose root verb is a format placeholder,
# and the runnable-instructions sweep reads that as a promise no parser can
# keep. A placeholder belongs inside an argument, never where the verb goes.
_USAGE = ("helm burn [--json] | helm burn why <family> [--json] | "
          "helm burn burst [--json] | "
          "helm burn runway [--json] [--window <hours>] | "
          "helm burn calibrate [--json] | "
          "helm burn declare <family> <colour> --until <iso> [reason...] | "
          "helm burn certify-local <family> --until <iso|duration> [note...] "
          "| helm burn certify-local <family> --revoke [note...] | "
          "helm burn horizon [anthropic <utc-iso> [reason...] | "
          "anthropic --clear] | "
          "helm burn spend [--json] [--session <id>...]")


def cmd_burn(args):
    """burn [--json]|why <family>|calibrate|horizon [anthropic <utc-iso>
    [reason...]|anthropic --clear]|declare <family> <colour> --until
    <iso>|certify-local <family> --until <iso|duration>|--revoke — the
    fire-danger reading per model family."""
    import sys
    args = list(args or ())
    sub = args[0] if args and not args[0].startswith("-") else None
    if sub is not None and sub not in ("why", "declare", "burst", "runway",
                                       "certify-local", "calibrate",
                                       "horizon", "spend"):
        print("helm burn: unknown subcommand %r\nusage: %s"
              % (sub, _USAGE), file=sys.stderr)
        return 2
    if sub == "horizon":
        from . import claudepace
        return claudepace.cmd_horizon(args[1:])
    if sub == "spend":
        return _cmd_spend(args[1:])
    if sub == "declare":
        return _cmd_declare(args[1:])
    if sub == "certify-local":
        return _cmd_certify_local(args[1:])
    if sub == "burst":
        return _cmd_burst(args[1:])
    if sub == "runway":
        return _cmd_runway(args[1:])
    if sub == "calibrate":
        return _cmd_calibrate(args[1:])
    as_json = "--json" in args
    rest = [a for a in args if a != "--json"]
    now = time.time()
    snap, age = cached_snapshot(now=now)
    if sub == "why":
        if len(rest) != 2:
            print("usage: %s" % _USAGE, file=sys.stderr)
            return 2
        family = rest[1]
        if family not in families():
            print("helm burn why: unknown family %r (known: %s)"
                  % (family, ", ".join(families())), file=sys.stderr)
            return 2
        flag = (snap or {}).get("families", {}).get(family)
        if as_json:
            print(json.dumps(flag, indent=1, sort_keys=True))
        else:
            for text in render_why(flag, now=now):
                print(text)
        return 0 if flag else 3
    if as_json:
        print(json.dumps(snap, indent=1, sort_keys=True))
    else:
        for text in render(snap, now=now):
            print(text)
        if snap:
            print("  reading age %ds (bound %ds)" % (age, max_age_s()))
        # THE CODEX PACE RIDES THE SAME SURFACE: one line, read from the
        # watchdog pass's own snapshot, never a live probe.
        from . import codexpace
        print(codexpace.burn_line(now=now))
        # AND THE CLAUDE PACE (pace5h, task/3871): one line per account, the
        # switch pool's weekly pace against its horizon, and one line per
        # project with an authored light, from its own snapshot. Neither
        # pace moves a colour.
        from . import claudepace
        for text in claudepace.burn_lines(now=now):
            print(text)
    return 0 if snap else 3


def _cmd_burst(args):
    """burst [--json] — MAY THIS SEAT exceed the one-delegate rule on the
    credential its own home holds.

    THE SNAPSHOT IS NOT CONSULTED, deliberately. The fold answers a POOL
    question about a family; this answers a per-credential question about one
    seat, from that credential's own live row in the observation log. Reading
    the folded colour here would rebuild the exact conflation the exemption
    exists to undo. Exit 0 on a grant, 1 when there is no exemption — a
    refusal is an ANSWER, so it is not exit 3."""
    import sys
    from . import burst
    args = list(args or ())
    if [a for a in args if a != "--json"]:
        print("usage: %s" % _USAGE, file=sys.stderr)
        return 2
    rec = burst.exemption()
    if "--json" in args:
        print(json.dumps(rec, indent=1, sort_keys=True))
    else:
        for text in burst.render(rec):
            print(text)
    return 0 if rec["granted"] else 1


def _cmd_runway(args):
    """runway [--json] [--window <hours>] — percent per hour per codex
    account and the fleet's runway against the next Pro reset, read LIVE from
    the budget history and the proxy-usage ledger (no vendor call). Exit 0 on
    a reading, 3 when there is no history to read, 2 on usage."""
    import sys
    from . import codexpace
    args = list(args or ())
    as_json, window_h, i = False, codexpace.WINDOW_H, 0
    while i < len(args):
        token = args[i]
        if token == "--json":
            as_json = True
        elif token == "--window" and i + 1 < len(args):
            try:
                window_h = float(args[i + 1])
            except ValueError:
                window_h = 0.0
            if not 0 < window_h <= 168:
                print("helm burn runway: --window takes hours, more than 0 "
                      "and at most 168\nusage: %s" % _USAGE, file=sys.stderr)
                return 2
            i += 1
        else:
            print("helm burn runway: unknown argument %r\nusage: %s"
                  % (token, _USAGE), file=sys.stderr)
            return 2
        i += 1
    reading = codexpace.live_reading(window_h=window_h)
    if as_json:
        print(json.dumps(reading, indent=1, sort_keys=True))
    else:
        for text in codexpace.render(reading):
            print(text)
    return 0 if reading["accounts"] else 3


def _cmd_spend(args):
    """spend [--json] [--session <id>...] — this seat's own spend over its
    account's weekly window: the calls its sessions made and the context they
    re-read versus output, priced per model, beside the weekly percent of the
    account its own home holds. EXTENDS `helm burn` per the owner's scope line
    (Orca feeds helm's existing surfaces, not a replacement meter).

    The sessions are the running session by default; `--session` names others
    (a seat's earlier sessions, or a terminal with no session of its own).
    Unknown is UNKNOWN, never zero. The verb prints NO Max savings % — it
    states the confounds (shared account, rounding, resets)."""
    import sys
    from . import burnspend
    args = list(args or ())
    as_json, sessions, rest = False, [], args
    while rest:
        head, rest = rest[0], rest[1:]
        if head == "--json":
            as_json = True
        elif head == "--session" and rest and not rest[0].startswith("-"):
            sessions.append(rest[0])
            rest = rest[1:]
        else:
            print("helm burn spend: unknown argument %r\nusage: %s"
                  % (head, _USAGE), file=sys.stderr)
            return 2
    reading = burnspend.spend_reading(sessions or None)
    if as_json:
        print(json.dumps(reading, indent=1, sort_keys=True))
    elif not reading.get("source_ok"):
        # No readable log is an ANSWER, not an error: exit 0 with the why, so
        # the verb never claims the seat spent nothing.
        print("helm burn spend: %s" % reading.get("why", "no source"))
    else:
        for text in burnspend.render(
                reading, weekly_pct=reading.get("weekly_pct"),
                weekly_reset_at=reading.get("weekly_reset_at")):
            print(text)
    return 0


def _cmd_declare(args):
    import sys
    from . import freetext
    args = list(args)
    if len(args) < 2:
        print("usage: %s" % _USAGE, file=sys.stderr)
        return 2
    family, colour = args[0], args[1]
    rest = args[2:]
    until = None
    if "--until" in rest:
        at = rest.index("--until")
        if at + 1 >= len(rest):
            print("helm burn declare: --until needs an instant",
                  file=sys.stderr)
            return 2
        until = rest[at + 1]
        rest = rest[:at] + rest[at + 2:]
    rc = freetext.refuse_leading_flags(rest, 0, ("--until",),
                                       "helm burn declare", _USAGE,
                                       "helm burn declare", what="the reason")
    if rc is not None:
        return rc
    why, rc = freetext.tail("helm burn declare", "reason", rest, "the reason",
                            known=("--until",), usage=_USAGE)
    if rc is not None:
        return rc
    why_not = improving_declaration(colour, family_flag(family))
    if why_not:
        print("helm burn declare: %s" % why_not, file=sys.stderr)
        return 2
    ok, err = declare(family, colour, until, why=why)
    if not ok:
        print("helm burn declare: %s" % err, file=sys.stderr)
        return 2
    print("helm burn declare: %s declared %s until %s — a DECLARED colour "
          "renders as declared, never as measured"
          % (family, colour.upper(), until))
    return 0


def _cmd_certify_local(args):
    """certify-local <family> --until <iso|duration> [note...] | certify-local
    <family> --revoke [note...] — the operator seat that runs a LOCAL
    family's hardware attests that the family is served there, optimally,
    until the instant it names, or ends that attestation now.

    SESSION-BOUND AND OPERATOR-ONLY. The acting seat is admitted through
    `actors.resolve_actor_reason` on this process's harness session, which
    refuses a declared HELM_CHAT_NAME that no roster-bound session
    corroborates (UNCORROBORATED), one the roster binds to another seat
    (DISPUTED) and a name minted from nothing (UNRESOLVED): a declared name
    alone is a value any process can export, and an attestation is only
    worth the session standing behind it. `dispatches.acting_author` is NOT
    that door: it takes a declared name at its word when no session is
    presented. The admitted seat must then be the family's declared operator
    seat (`local_operator`), and the record stamps its session's key, which
    the fold checks against the roster on every pass. Exit 2 on usage, on a
    family that is not local, on a caller with no roster-bound session, on a
    seat that is not the operator, and on an `--until` that is unreadable,
    not in the future or past the ceiling."""
    import sys
    from . import actors, freetext
    label = "helm burn certify-local"
    args = list(args)
    if not args or args[0].startswith("-"):
        print("usage: %s" % _USAGE, file=sys.stderr)
        return 2
    family, rest = args[0], args[1:]
    revoke = "--revoke" in rest
    if revoke:
        rest.remove("--revoke")
    until = None
    if "--until" in rest:
        at = rest.index("--until")
        if at + 1 >= len(rest):
            print("%s: --until needs a duration or an instant" % label,
                  file=sys.stderr)
            return 2
        until = rest[at + 1]
        rest = rest[:at] + rest[at + 2:]
    if revoke == (until is not None):
        print("%s: give exactly one of --until <iso|duration> or --revoke\n"
              "usage: %s" % (label, _USAGE), file=sys.stderr)
        return 2
    why = _not_local(family)
    if why:
        print("%s: %s" % (label, why), file=sys.stderr)
        return 2
    known = ("--until", "--revoke")
    rc = freetext.refuse_leading_flags(rest, 0, known, label, _USAGE, label,
                                       what="the note")
    if rc is not None:
        return rc
    note, rc = freetext.tail(label, "note", rest, "the note", known=known,
                             usage=_USAGE)
    if rc is not None:
        return rc
    # THE SESSION IS THE PROOF. Admission refuses every caller whose session
    # the roster does not bind to the seat it resolves to, so an admitted
    # actor's session IS bound to it, and that is the session stamped below.
    session = home.session_id()
    actor, err, _reason = actors.resolve_actor_reason(
        session, act=("revoke a local certification" if revoke
                      else "certify a local family"))
    if actor is None:
        print("%s: %s. %s" % (label, str(err).rstrip(". "),
                              _how_to_certify(family)), file=sys.stderr)
        return 2
    by = actor.canonical_name
    why = _not_the_operator(family, by, session)
    if why:
        print("%s: refusing: %s. %s" % (label, why, _how_to_certify(family)),
              file=sys.stderr)
        return 2
    ok, err = (revoke_local(family, by, session, note=note) if revoke
               else certify_local(family, until, by, session, note=note))
    if not ok:
        print("%s: %s" % (label, err), file=sys.stderr)
        return 2
    rec = read_local_certifications()["families"].get(family) or {}
    if revoke:
        print("%s: %s certification revoked by %s (session key %s); it reads "
              "GREY from the next fold"
              % (label, family, by, rec.get("revoked_session_key")))
    else:
        print("%s: %s certified by %s (session key %s) until %s; its money "
              "reads GREEN (certified-local, never a vendor measurement) "
              "while that session stays bound to %s on the roster, until "
              "then, and GREY after"
              % (label, family, by, rec.get("session_key"),
                 _iso(rec.get("until") or 0), by))
    return 0


def _how_to_certify(family):
    """The repair a refused certifier is owed: where a certification of
    `family` can come from at all."""
    operator = local_operator(family)
    if not operator:
        return ("%s declares no operator seat: set the local-names key %r to "
                "the seat that runs its hardware, and that seat certifies it "
                "from its own session" % (family, _operator_key(family)))
    return ("A certification of %s comes only from its operator seat %s, in "
            "its own session: run `helm burn certify-local %s --until 2h` "
            "there, where the roster binds the session to %s; a declared "
            "name with no roster-bound session certifies nothing"
            % (family, operator, family, operator))


def _cmd_calibrate(args):
    """calibrate [--json] — backtest the codex runway model against its own
    history (codexpace), reporting bias and median abs error per trailing
    window (1/3/6/12/24h) and picking the best. Read-only: no ledger, no
    vendor call. Exit 0 on a reading, 3 when there is no history to backtest,
    2 on usage."""
    import sys
    args = list(args or ())
    as_json = "--json" in args
    rest = [a for a in args if a != "--json"]
    if rest:
        print("helm burn calibrate: unknown argument %r\nusage: %s"
              % (rest[0], _USAGE), file=sys.stderr)
        return 2
    from . import codexcal
    result = codexcal.calibrate()
    if as_json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        for text in codexcal.render(result):
            print(text)
    return 0 if result.get("lines") else 3
