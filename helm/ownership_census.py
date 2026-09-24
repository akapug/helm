#!/usr/bin/env python3
"""The census that runs BEFORE anything reverts, and the reason it runs first.

A reversion is a WRITE ACROSS TWO LEDGERS driven by a liveness judgement, and
the judgement's threshold is defensible only while the fleet's last_seen
distribution stays bimodal (see `ownership.DEAD_AFTER_S`). So the census is
not a progress report bolted onto a mutation: it is the INSTRUMENT THAT SAYS
WHETHER THE MUTATION'S PREMISE STILL HOLDS, and reading it after the write
would be reading it too late.

IT RE-DERIVES, IT NEVER QUOTES. The numbers that motivated this module (209
open dispatch rows, 442 owned-open task rows) were true when they were
measured and were already stale hours later — both ledgers move constantly as
rows verdict and close. Every figure here is computed from the ledgers at the
moment of the call, which is the only kind of number a write may be based on.

EVERY EXCLUSION NAMES ITSELF AND ITS SIZE. A census that silently drops the
categories it does not judge is a census that reports a smaller world than
the one it is about, and the reader cannot tell a category that is empty from
one that was never looked at. So each ledger reports its set-aside counts
beside its judged ones, and they sum to the whole file.
"""
import os
import sys

from . import ownership

# WHICH ROWS ARE STILL OWED, PER LEDGER, AND THE VOCABULARIES DIFFER BECAUSE
# THE LEDGERS DO. Read off the live stores rather than assumed: dispatch rows
# carry open/held/verdict/closed/cancelled and name their target `recipient`;
# task rows carry open/in_progress/closed and name theirs `owner`.
#
# `held` IS EXCLUDED ON PURPOSE AND IT IS NOT A ROUNDING ERROR. A HELD row was
# parked by a seat that stated a named external dependency, so it is the one
# category where an absent owner is the DOCUMENTED condition rather than a
# symptom — reverting it would discard a deliberate act. It is counted and
# printed as a set-aside so the exclusion is visible at its true size.
LEDGERS = (
    # name        rows_key     owner_key     owed states        set aside
    ("dispatch", "recipient", ("open",), ("held",)),
    ("task", "owner", ("open", "in_progress"), ()),
)


class LedgerCensus(object):
    """One ledger's answer: counts by verdict, and the rows behind each."""

    def __init__(self, name, unavailable=None):
        self.name = name
        # AVAILABILITY IS A FIELD, NOT AN ABSENCE: an unreadable ledger must
        # never render as zero owed rows, which is a measured-looking
        # all-clear from a store nobody could read.
        self.unavailable = unavailable
        self.owed = 0                  # rows owned AND in an owed state
        self.unowned = 0               # owed rows naming nobody at all
        self.set_aside = {}            # state -> count, each named and sized
        self.by_verdict = {ownership.HOLDING: 0, ownership.DARK: 0,
                           ownership.UNKNOWN: 0}
        self.seats = {}                # seat -> [verdict, why, row count]

    @property
    def judged(self):
        return sum(self.by_verdict.values())

    def account(self):
        """-> (judged + unowned + set_aside, total) so a caller can ASSERT the
        arithmetic rather than trust the renderer's columns to add up."""
        return (self.judged + self.unowned + sum(self.set_aside.values()),
                self.owed + self.unowned + sum(self.set_aside.values()))


def ledger_census(name, rows, owner_key, owed_states, aside_states, state_of,
                  unavailable=None, owner_of=None):
    """Classify one ledger's owed rows through `state_of`, ONE CALL PER SEAT.

    `state_of(seat) -> (verdict, why)` is asked once per distinct name and the
    answer reused for every row that names it. That is not only a saving: the
    descendant probe behind it reads a process table, so asking per ROW would
    let one seat's answer change midway through its own rows and produce a
    census that is internally inconsistent — some of a seat's rows holding and
    the rest dark, from one reading of one fleet.
    """
    out = LedgerCensus(name, unavailable=unavailable)
    for state in aside_states:
        out.set_aside.setdefault(state, 0)
    if unavailable:
        return out
    seen = {}
    for row in (rows or {}).values():
        status = row.get("status")
        if status in aside_states:
            out.set_aside[status] = out.set_aside.get(status, 0) + 1
            continue
        if status not in owed_states:
            continue
        # THE LEDGER'S OWN OWNER ACCESSOR where it has one: `tasks.owner_of`
        # recognises historical placeholders (UNOWNED, '-') that a raw field
        # read counts as owned-and-therefore-possibly-dead.
        seat = str((owner_of(row) if owner_of else row.get(owner_key))
                   or "").strip()
        if not seat:
            out.unowned += 1
            continue
        out.owed += 1
        if seat not in seen:
            seen[seat] = state_of(seat)
        verdict, why = seen[seat]
        out.by_verdict[verdict] = out.by_verdict.get(verdict, 0) + 1
        cell = out.seats.setdefault(seat, [verdict, why, 0])
        cell[2] += 1
    return out


def gap_seats(seats_quiet, floor_s=None, band=0.5):
    """Seats sitting NEAR the threshold — the tripwire on the whole design.

    `ownership.DEAD_AFTER_S` is defensible because the measured distribution
    was bimodal with a wide empty gap: the busiest live seat was minutes quiet
    and the quietest dark seat was hours. That is a fact about a fleet on one
    day, not a law, and it is the constant's ONLY justification.

    So this asks the question that would retire it: is anybody in the gap? A
    seat whose silence is within `band` of the threshold is one the constant
    cannot confidently classify, and its appearance means the number needs
    re-deriving rather than trusting. Printing it is the point — the census
    is how that change announces itself instead of being discovered by a
    wrongly reverted row.
    """
    floor = ownership.DEAD_AFTER_S if floor_s is None else floor_s
    lo, hi = floor * (1.0 - band), floor * (1.0 + band)
    return sorted((s, q) for s, q in (seats_quiet or {}).items()
                  if q is not None and lo <= q <= hi)


def _label(seat):
    """THE ONE EMIT DOOR FOR A NAME, and it covers all three sources.

    `gap_seats` carries ROSTER KEYS, which are unvalidated at the join seam —
    a hostile HELM_CHAT_NAME can hold ESC or bidi and this renders it in the
    most prominent column of a tripwire an operator is meant to trust. The
    seat names in the per-ledger lines are LEDGER-sourced rather than roster
    keys, and the beacon map is the caller's, but they are free text too and
    laundering them costs nothing: `_seat_label` leaves a legitimate seat
    BYTE-IDENTICAL.

    It is one door rather than a call at each format site because laundering
    per-field is a bet re-placed at every branch — the next name added to
    this renderer would have to remember, and the one that forgets is the one
    that ships.
    """
    from .seats_common import _seat_label
    return _seat_label(seat)[:22]


def _seat_lines(census, limit=12):
    rows = sorted(census.seats.items(),
                  key=lambda kv: (kv[1][0] != ownership.DARK, -kv[1][2]))
    dark = [(s, c) for s, c in rows if c[0] == ownership.DARK]
    out = []
    for seat, (_verdict, why, count) in dark[:limit]:
        out.append("      %-22s %4d  %s" % (_label(seat), count, why))
    if len(dark) > limit:
        out.append("      ... and %d more dark seats" % (len(dark) - limit))
    return out


def render(censuses, gaps=(), beacons=None, distribution_known=True,
           distribution_source=None, distribution_proves_terminal_response=False):
    """The text the verb prints. A BEACON APPEARS HERE AND NOWHERE ELSE.

    The measurement this whole module was built from is that a beacon does not
    keep a row: one seat read COVERED, had completed no turn in eleven hours,
    and held 23 rows. So the beacon is reported as EVIDENCE beside the verdict
    — useful to a human deciding whether to go look — while `owner_state`,
    which makes the decision, takes no beacon argument at all.
    """
    out = ["helm ownership census — re-derived now, not quoted"]
    for c in censuses:
        out.append("")
        if getattr(c, "unavailable", None):
            out.append("  %-10s UNREADABLE — %s" % (c.name, c.unavailable))
            out.append("             NOT reported as empty: nothing about "
                       "this ledger is known")
            continue
        total_shown, total = c.account()
        out.append("  %-10s %4d owed and owned" % (c.name, c.owed))
        for verdict in (ownership.HOLDING, ownership.DARK, ownership.UNKNOWN):
            out.append("    %-12s %4d" % (verdict, c.by_verdict.get(verdict, 0)))
        for state, n in sorted(c.set_aside.items()):
            out.append("    %-12s %4d  SET ASIDE, not judged and not revertible"
                       % (state, n))
        if c.unowned:
            out.append("    %-12s %4d  owed but naming nobody" % ("unowned", c.unowned))
        if total_shown != total:
            out.append("    ARITHMETIC OFF: %d shown vs %d rows — do not revert"
                       % (total_shown, total))
        lines = _seat_lines(c)
        if lines:
            out.append("    dark seats holding these rows:")
            out.extend(lines)
        # WHICH SOURCE EACH UNKNOWN SEAT LACKS. An UNKNOWN is not a shrug: it
        # names the reader that could not see this seat, which is the fact an
        # operator needs to decide whether to go look or to wait. Ruled at
        # task/2293 — "the census says which source each seat lacks".
        unk = sorted(((k, v) for k, v in c.seats.items()
                      if v[0] == ownership.UNKNOWN),
                     key=lambda kv: -kv[1][2])
        if unk:
            out.append("    unknown seats — the evidence that could not see them:")
            for seat, (_v, why, count) in unk[:10]:
                out.append("      %-22s %4d  %s" % (_label(seat), count, why))
            if len(unk) > 10:
                out.append("      ... and %d more unknown seats" % (len(unk) - 10))
    if beacons:
        out.append("")
        out.append("  BEACON EVIDENCE (never a condition — a covered beacon on a")
        out.append("  silent seat is exactly the case that motivated this verb):")
        for seat, state in sorted(beacons.items()):
            out.append("    %-22s %s" % (_label(seat), state))
    out.append("")
    if not distribution_known:
        out.append("  THRESHOLD TRIPWIRE: UNKNOWN — the population or its clock")
        out.append("  could not be read reliably, so nothing certifies the %.1fh line."
                   % (ownership.DEAD_AFTER_S / 3600.0))
    elif not distribution_proves_terminal_response:
        # Presence can describe its own clock, never the completion threshold.
        out.append("  THRESHOLD TRIPWIRE: NOT APPLICABLE — the available")
        out.append("  distribution comes from %s, which is not a TERMINAL"
                   % (distribution_source or "a corroboration-only source"))
        out.append("  RESPONSE. This clock certifies nothing about the %.1fh line."
                   % (ownership.DEAD_AFTER_S / 3600.0))
        if gaps:
            out.append("  CORROBORATION-ONLY gaps (not terminal-response silence):")
            for seat, quiet in gaps:
                out.append("    %-22s clock age %.1fh"
                           % (_label(seat), quiet / 3600.0))
    elif gaps:
        out.append("  THRESHOLD TRIPWIRE — %d seat(s) sit NEAR the %.1fh line, so the"
                   % (len(gaps), ownership.DEAD_AFTER_S / 3600.0))
        out.append("  distribution is no longer bimodal and the constant needs")
        out.append("  re-deriving before it decides anything:")
        for seat, quiet in gaps:
            out.append("    %-22s silent %.1fh" % (_label(seat), quiet / 3600.0))
    else:
        out.append("  threshold tripwire: clear — no seat sits near the %.1fh line"
                   % (ownership.DEAD_AFTER_S / 3600.0))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# WIRING. Everything above takes its inputs as arguments and can be driven by
# a test; everything below reaches for the live fleet. The split is the same
# one `ownership.owner_state` makes and for the same reason: a liveness
# instrument nobody can exercise without a fleet is one that ships wrong.


def _rooms_by_seat(root, claims=None, project=None):
    """({seat casefolded: [room path]}, err) — which rooms each seat holds.

    TRI-STATE, LIKE EVERY OTHER READ ON THIS PATH. An unreadable claims file
    returns an error rather than an empty map: empty would mean "this seat
    holds no room", which makes a working seat look idle and its rows
    revertible.

    SCHEMA FAILURE IS UNREADABLE TOO, not just an IO error. A JSON-error-only
    catch lets a decoded `[]` or `null` fold into `{}` and report no held
    rooms, and lets a malformed claim row raise straight past it — both
    contradicting the tri-state this function advertises. `seats_claims._claims_read(strict=True)` is the
    door that already validates the envelope, the fence and each row.

    AND A CLAIM KEY CARRIES A PROJECT, WHICH MUST NOT BE DISCARDED. The key is
    `worktree:<project>:<lane>`, and mapping every one onto `<this root>-wt/
    <lane>` means a lane label used in TWO repositories resolves to the wrong
    room — which can read gate-EMPTY while the seat's actual room holds a live
    gate, turning working into dark. A claim for another project is not
    resolvable from here, so it is reported as UNMEASURED rather than pointed
    at a plausible local path.
    """
    from .work._lanes import lane_path
    want = os.path.basename(root.rstrip(os.sep)) if root else None
    if not want or (project is not None and str(project) != want):
        return None, "claim project has no matching resolved repository root"
    try:
        from .seats_claims import _claims_read
        rows = _claims_read(strict=True) or {}
    except Exception as e:                  # noqa: BLE001
        return None, "%s: %s" % (e.__class__.__name__, e)
    if claims is not None:                  # test seam: an explicit payload
        import json
        try:
            with open(claims, encoding="utf-8") as fh:
                rows = json.load(fh)
            if not isinstance(rows, dict):
                raise ValueError("claim state is not an object")
        except Exception as e:              # noqa: BLE001
            return None, "%s: %s" % (e.__class__.__name__, e)
    out, foreign = {}, {}
    for key, row in rows.items():
        parts = str(key).split(":")
        if len(parts) != 3 or parts[0] != "worktree":
            continue
        holder = str((row or {}).get("holder") or "").strip()
        if not holder:
            continue
        proj, lane = parts[1], parts[2]
        if proj != want:
            foreign.setdefault(holder.casefold(), []).append(proj)
            continue
        out.setdefault(holder.casefold(), []).append(lane_path(root, lane))
    return (out, foreign), None


def _descendant_probe(rooms, census_of, rooms_err=None):
    """seat -> True | False | None, the second half of the DEAD conjunction.

    LIVE DESCENDANT WORK IS A REMOTE GATE **OR A SUBAGENT**, and only the
    first is measurable from here. A gate running in a room the seat holds is
    read through `gate.inflight_census`, which already carries an UNREADABLE
    third state. THE SUBAGENT HALF HAS NO FOREIGN-SEAT READER: the delegation
    evidence in `seats_delegation._delegated_build` is Stop-context and
    session-bound, and aiming it at another seat's session is exactly the
    misuse its own docstring warns about.

    SO FALSE IS RETURNED ONLY WHEN BOTH HALVES ARE MEASURED AND EMPTY, which
    today means it is not returned at all: no gate live plus an unmeasurable
    subagent population is UNKNOWN, not free. That is deliberate and costly —
    it makes DARK unreachable through this half until a reader exists — and
    it is the correct direction, because the alternative reverts the rows of
    a seat whose subagent is mid-build.
    """
    from . import gate

    def probe(seat):
        if rooms_err:
            return None
        by_seat, foreign = rooms if isinstance(rooms, tuple) else (rooms, {})
        key = str(seat or "").casefold()
        if (foreign or {}).get(key):
            return None                 # holds rooms in a project we cannot read
        unreadable = False
        for path in (by_seat or {}).get(key) or []:
            c = census_of(path)
            if c.state == gate.GATE_LIVE:
                return True
            if c.state == gate.GATE_UNREADABLE:
                unreadable = True
        if unreadable:
            return None
        # BOTH HALVES OR NOTHING: the gate half says empty, the subagent half
        # cannot be read for a foreign seat, so the conjunction is unproven.
        return None
    return probe


def live_inputs(now=None):
    """The real readers, bound. -> (state_of, quiet, distribution_known, gaps_src)."""
    from . import gate, ownership
    from .seats_roster import last_seen, roster_checked
    from .work._lanes import find_root

    rows, unreadable = roster_checked()
    if unreadable:
        rows = None
    root = find_root()
    rooms, rooms_err = _rooms_by_seat(root)
    probe = _descendant_probe(rooms, gate.inflight_census, rooms_err)

    from . import turnresponse, turnstamp

    # READ ONCE PER CENSUS, NOT PER SEAT. The pending window comes from the
    # seat's launch settings, and re-reading them twenty-three times to get
    # the same number is the kind of cost that makes an operator stop running
    # the verb. An unreadable settings file is carried as an ERROR into every
    # seat's evidence rather than silently replaced by the default, because
    # substituting a shorter window certifies responses early on exactly the
    # seats whose configuration could not be checked.
    # THE PENDING WINDOW IS BOUND TO THE SESSION, NOT TO THIS PROCESS.
    # helm merges a per-project `.claude/settings.local.json` into a launched
    # session, so a bound derived from the user root alone can be SHORTER than
    # the session's effective one — a project Stop timeout of 600 against a
    # user 60 makes a 100s-old terminal record read settled when policy says
    # PENDING. The scope comes from the seat's OWN recorded cwd; a row that
    # does not carry one has an unresolvable scope, which is UNKNOWN rather
    # than a borrowed number. Reviewer settings are never a seat's settings.
    #
    # MEMOISED BY SCOPE, not read per row: twenty-three seats mostly share a
    # handful of cwds, and re-reading the same files per row is the cost that
    # makes an operator stop running the verb.
    _window_cache = {}

    def _window_for(row):
        """-> (seconds, err). The effective Stop-hook bound for THIS seat."""
        cwd = str((row or {}).get("cwd") or "")
        if not cwd:
            return None, ("the roster row carries no cwd, so the session's "
                          "effective settings scope cannot be resolved")
        if cwd not in _window_cache:
            project = os.path.join(cwd, ".claude")
            try:
                _window_cache[cwd] = turnresponse.stop_hook_timeout_s(
                    project_dirs=[project])
            except Exception as e:            # noqa: BLE001
                # THE ACQUISITION BOUNDARY. This reader touches JSON anybody
                # can edit; a raise here must degrade ONE seat's evidence to
                # UNKNOWN, never abort the census for every seat.
                _window_cache[cwd] = (None, "%s: %s" % (
                    e.__class__.__name__, e))
        return _window_cache[cwd]

    def turn_evidence(seat, row):
        """THE TRANSCRIPT FIRST; THE STOP STAMP ONLY CORROBORATES.

        `turnresponse` reads the seat's own session transcript and reports the
        last TERMINAL RESPONSE — the one source in the tree allowed to carry a
        row to DARK. It is bound by the session id the ROSTER currently
        carries, never by whichever transcript was written last: on a box
        running twenty seats, newest-by-mtime is somebody else's turn.

        `turnstamp` records that helm's own Stop dispatch allowed, which is
        neither harness acceptance nor a terminal response. It is consulted
        ONLY when the transcript cannot cover the seat, and it arrives with
        `proves_terminal_response` unset, so it can hold a seat HOLDING and
        can never revert its rows.

        `.seen` is passed to nobody. It is beacon-touched presence, and
        falling back to it here would undo the whole cure — the predicate
        would silently return to measuring beacons while reporting activity.
        """
        row = row or {}
        session = str(row.get("session") or "")
        if not session:
            # THE ROSTER ROW CARRIES NO CURRENT SESSION, so there is no
            # transcript to bind and no stamp to filter. Unbound evidence is
            # exactly what item C refuses; this is uncovered, not absent.
            return ownership.TurnEvidence(
                covered=False, source=turnresponse.SOURCE)
        window_s, window_err = _window_for(row)
        if window_err:
            return ownership.TurnEvidence(
                err="stop-hook timeout unreadable: %s" % window_err,
                source=turnresponse.SOURCE)

        try:
            reading = turnresponse.last_terminal_response(
                session, now=now, timeout_s=window_s)
        except Exception as e:                    # noqa: BLE001
            return ownership.TurnEvidence(
                err="%s: %s" % (e.__class__.__name__, e),
                source=turnresponse.SOURCE)
        if reading.err:
            return ownership.TurnEvidence(err=reading.err,
                                          source=turnresponse.SOURCE)
        if reading.pending:
            return ownership.TurnEvidence(pending=reading.pending,
                                          source=turnresponse.SOURCE)
        if reading.covered:
            return ownership.TurnEvidence(
                ts=reading.ts, covered=True, source=turnresponse.SOURCE,
                proves_terminal_response=True,
                incomplete=reading.incomplete)

        # NO TRANSCRIPT FOR THIS SESSION — a proxy family that writes none, or
        # a session id nothing on disk answers to. Fall back to helm's own
        # Stop-dispatch record, which is corroboration only.
        row_, err = turnstamp.last_allowed(seat, session=session)
        if err:
            return ownership.TurnEvidence(err=err, source=turnstamp.SOURCE)
        if row_ is None:
            return ownership.TurnEvidence(
                covered=False, source=turnresponse.SOURCE)
        return ownership.TurnEvidence(
            ts=row_.get("allowed_at"), covered=True, source=turnstamp.SOURCE)

    memo = {}

    def state_of(seat):
        if seat not in memo:
            memo[seat] = ownership.owner_state(seat, rows, turn_evidence,
                                               probe, now=now)
        return memo[seat]

    quiet = {}
    distribution_known = rows is not None
    if rows:
        import time as _time
        at = now if now is not None else _time.time()
        for seat, row in rows.items():
            raw = last_seen(seat, row)
            ls = ownership._finite(raw) if raw is not None else None
            if raw is not None and ls is None:
                distribution_known = False
            quiet[seat] = None if not ls else at - ls
    # A bad presence clock is unmeasured, not an ordinary gap-free population.
    return state_of, quiet, distribution_known


def cmd_ownership(args):
    """ownership census — who still holds open rows, and is anybody working?"""
    args = list(args or ())
    if not args or args[0] != "census":
        print("usage: helm ownership census", file=sys.stderr)
        return 2
    # ONCE THE SUBVERB IS MATCHED, EVERY REMAINING TOKEN MUST BE KNOWN. This
    # verb has no flags, so ANY tail is junk — and without this, `ownership
    # census --json` printed the plain census and exited 0, telling a caller
    # who asked for JSON that they got it. That is the same silence the work
    # CLI's unknown-flag scan exists to end: a call that returns 0 while the
    # thing you asked for was dropped.
    from .cli import guard_tail
    rc = guard_tail("helm ownership census", args[1:],
                    usage="usage: helm ownership census")
    if rc is not None:
        return rc
    from . import dispatches, tasks

    state_of, quiet, distribution_known = live_inputs()
    # SNAPSHOT READERS, NOT rows(). `dispatches.rows()` and `tasks.rows()`
    # discard the unavailable channel, so an unreadable ledger renders as a
    # measured-looking zero backlog and a clean exit code.
    d_rows, d_un = dispatches.snapshot()
    t_rows, t_un = tasks.snapshot()
    stores = {"dispatch": (d_rows, d_un, None),
              "task": (t_rows, t_un, tasks.owner_of)}
    censuses = []
    for name, owner_key, owed, aside in LEDGERS:
        rows, un, owner_of = stores[name]
        censuses.append(ledger_census(name, rows, owner_key, owed, aside,
                                      state_of, unavailable=un,
                                      owner_of=owner_of))
    print(render(censuses, gaps=gap_seats(quiet) if distribution_known else (),
                 distribution_known=distribution_known,
                 distribution_source="roster .seen (beacon-touched presence)",
                 distribution_proves_terminal_response=False))
    # AN UNREADABLE LEDGER IS NOT A CLEAN RUN. Exit non-zero so a caller that
    # checks status cannot read "no owed rows" off a store nobody could read.
    return 0 if not (d_un or t_un) else 1
