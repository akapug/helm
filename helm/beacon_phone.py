"""THE ONE DEFINITION OF FLEET-DOWN, and the cutover's last pager latch.

The office weather is the only writer of the fleet-down page (task/3939):
`officeweather._fleet` turns the weather stormy on fleet-down by calling the
qualifier here, and nothing in this module can reach the phone (no notify
import, no push). So there is one definition of fleet-down and one writer of
its page:

  * `qualify` judges one census against the roster's attendance register: the
    steward unreachable, or a strict majority (at least two) of the eligible
    seats unreachable, over a COMPLETE census whose verdicts match the
    register. An incomplete census, or one whose probes did not all run, is
    never fleet-down; one that lists fewer seats than the roll (or none)
    says so ("the beacons census lists N of M seats"). An open episode keeps the cohort it froze when it
    opened; once the phone has heard it, it ends only when every seat of that
    cohort (and its steward) is COVERED again, or RESTING (the owner paused
    it, read on the census, never inferred), so a seat leaving the roll
    never manufactures a recovery and a seat the owner rested never holds
    the episode open. A cohort seat that left the roster stays owed and is
    named. An episode the phone never heard ends as soon as the fleet is no
    longer down. (How long a heard episode may hold is the weather's bound,
    `officeweather.FLEET_HOLD_S`, not this qualifier's.)
  * `fleet` is `qualify` of the census `beacons --post` ATTENDED (written by
    `beacons.attend` beside the register, under the roster lock, with the
    register's own `at`), read with the roster under that lock. ONE
    OBSERVATION, ONE OWNER: the census that wrote the register is the census
    judged against it, so a seat attendance HELD at DEAF-IN-EFFECT, or one
    that flapped between two censuses, matches by construction. A recorded
    census older than STALE_S, missing or unreadable is "unknown" and says
    how old it is: it never establishes fleet-down, nor its end.
  * `_latch` reads the episode file the retired pager left beside the roster,
    which the weather adopts once per helm home at the cutover.
"""
import math
import os

from . import beacons, pk, seats_roster

#: No open episode: the latch shape of a fleet that is not down.
CLEAR = {"phase": "clear"}
#: A recorded census older than this says nothing about now: two passes of
#: the `beacons --post` timer, whose cadence is `beacons.INTERVAL_S` (its
#: timer units are written from that name), so one late or skipped pass is not
#: an instrument outage and a timer that stopped is named within two passes.
#: The same two passes are `beacons.REARM_GRACE_S`; derived, never a number.
STALE_S = 2 * beacons.INTERVAL_S


def _stamp(value, now):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value <= now)


def _dead_before(att, instant):
    """A seat already DEAF before this episode — it joined the outage's DEAF
    roll *before* the alarm, so it is not a participant in it.

    A participant (its spell began within one census interval, the trip
    interval) fell *during* the outage and stays in the cohort. A seat that was
    already unreachable and has been DEAF longer ago — dead three hours before
    the trip, say — can never cover, so it leaves the frozen cohort: keeping it
    would demand a verdict no one can ever raise and hold the episode DOWN
    forever. An unreadable `since` fails closed — the seat is dropped only on a
    positive, finite `since` that predates the alarm instant by more than one
    census interval.
    """
    if not isinstance(att, dict) or not att.get("alarm"):
        return False
    since = att.get("since")
    return (isinstance(since, (int, float)) and not isinstance(since, bool)
            and math.isfinite(since) and since <= instant - beacons.INTERVAL_S)


def latch_path():
    """Where the retired pager kept its episode: beside the roster."""
    return seats_roster.roster_path() + ".phone.json"


def _latch(path):
    value = pk.read_json(path, None, strict=True)
    if value is None:
        if os.path.lexists(path):
            raise ValueError("invalid phone latch")
        return dict(CLEAR)
    return episode(value)


def episode(value):
    """`value` as a valid episode (CLEAR, or a down latch with its frozen
    cohort, steward and whether the phone heard it), else ValueError."""
    if not isinstance(value, dict) or value.get("phase") not in ("clear", "down"):
        raise ValueError("invalid phone latch")
    if value["phase"] == "clear":
        if set(value) != {"phase"}:
            raise ValueError("invalid clear phone latch")
        return value
    cohort = value.get("cohort")
    if (set(value) != {"phase", "cohort", "steward", "delivered"}
            or not isinstance(cohort, list) or not cohort
            or not all(isinstance(s, str) and s for s in cohort)
            or len({s.casefold() for s in cohort}) != len(cohort)
            or (value["steward"] is not None
                and not isinstance(value["steward"], str))
            or not isinstance(value["delivered"], bool)):
        raise ValueError("invalid down phone latch")
    return value


def _steward(rows):
    """An explicit valid roster seat, or the sole declared -steward seat."""
    named = (os.environ.get("HELM_STEWARD_SEAT") or "").strip()
    if named:
        return named if named in rows else None  # a typo is not a substitute
    found = [name for name in rows if name.endswith("-steward")]
    return found[0] if len(found) == 1 else None


def _read():
    """(rows, census, why) from ONE hold of the roster lock: the roster, and
    the census `beacons.attend` recorded beside it under that same lock (None
    and why when it cannot be read; None and None when none is recorded).
    Raises when the roster cannot be read."""
    path = seats_roster.roster_path()
    with seats_roster._flocked(path + ".lock", check=True) as lock:
        if lock.f is None:
            raise OSError("roster lock unavailable")
        rows, failed = seats_roster.roster_acquired()
        if failed:
            raise ValueError("roster unreadable")
        if any(not isinstance(name, str) or not isinstance(row, dict)
               for name, row in rows.items()):
            raise ValueError("roster row unreadable")
        try:
            rep = pk.read_json(beacons.attended_path(path), None, strict=True)
        except Exception as exc:             # noqa: BLE001 — named, not clear
            return rows, None, "the beacons census could not be read (%s)" % (
                type(exc).__name__)
        return rows, rep, None


def _report(rep, rows):
    """Map a census to its roster, refusing duplicate or malformed identities."""
    sample = rep.get("seats") if isinstance(rep, dict) else None
    if not isinstance(sample, list):
        return None
    by_name = {}
    for row in sample:
        if (not isinstance(row, dict) or not isinstance(row.get("seat"), str)
                or not isinstance(row.get("verdict"), str)):
            return None
        key = row["seat"].casefold()
        if key in by_name:
            return None
        by_name[key] = row
    roster = {name.casefold(): name for name in rows}
    # A census row for a seat that is no longer on the roll (a withdrawn seat
    # that keeps re-arming its beacon) must not silence the roster majority:
    # it is dropped, not the whole census (task/busy-is-not-deaf).
    by_name = {key: row for key, row in by_name.items() if key in roster}
    if len(roster) != len(rows) or not set(by_name).issubset(roster):
        return None
    return by_name


def _roll(rows, eligible, steward, now, skip=frozenset()):
    """The casefolded seats a whole census must list. The roster also
    contains historic graveyard seats that the census roll deliberately
    excludes: the roll is the enrolled, previously COVERED seats and the
    explicit steward, not every roster tombstone."""
    required = {name.casefold() for name in eligible} - skip
    # RESTING has no vote, but still belongs to the measured roll; omitting it
    # must not let an explicitly partial report masquerade as complete.
    required.update(name.casefold() for name, row in rows.items()
                    if isinstance(row.get("attendance"), dict)
                    and name.casefold() not in skip
                    and _stamp(row["attendance"].get("covered"), now)
                    and beacons.within_grace(row["attendance"], now=now))
    if steward is not None:
        required.add(steward.casefold())
    return required


def _same(rows, sample, required, now):
    """Does every census row match the roster's attendance register, all
    stamped at one instant? (`required` is already known to be listed.)"""
    at = None
    names = {name.casefold(): name for name in rows}
    for key, found in sample.items():
        row = rows[names[key]]
        att = row.get("attendance")
        if (not isinstance(att, dict) or att.get("state") != found["verdict"]
                or att.get("alarm") is not beacons.unreachable(found)
                or not _stamp(att.get("at"), now)):
            return False
        if at is None:
            at = att["at"]
        elif at != att["at"]:
            return False
    return True


def complete(rep):
    """Did both of the census's probes run? A census taken blind is never
    whole, whatever its rows say."""
    return isinstance(rep, dict) and bool(rep.get("live_probe")
                                          and rep.get("agent_probe"))


def qualify(rep, rows, now, whole_census, latch=None):
    """THE ONE DEFINITION OF FLEET-DOWN -> {phase, why, latch, looks_down}.

    `rep` is a census, `rows` the roster it is judged against, `whole_census`
    whether its probes all ran (`complete`), and `latch` the open episode
    (CLEAR when there is none). `phase` is "down", "clear" or "unknown" (the
    census cannot establish either); `why` says which qualifier fired, or why
    nothing could be established; `latch` is the episode to keep (a newly
    frozen cohort on the clear->down edge, CLEAR once it ends); `looks_down`
    says whether the rows alone would qualify, complete or not. Once the
    census maps to the roster it also says `down` (the eligible seats
    unreachable), `short` (it lists fewer seats than the roll holds, or none:
    why is "the beacons census lists N of M seats"), and, for an open
    episode on a whole census, `owed` (its cohort seats and steward not
    COVERED or RESTING, in cohort order) and `gone` (those off the roster).
    """
    latch = episode(CLEAR if latch is None else latch)
    out = {"phase": "unknown", "why": "", "latch": latch, "looks_down": False}
    sample = _report(rep, rows)
    if sample is None:
        out["why"] = "the census and the roster disagree on seat identity"
        return out
    names = {name.casefold(): name for name in rows}
    steward = _steward(rows)
    eligible = [name for name, row in rows.items()
                if ((att := row.get("attendance")) is not None
                    and isinstance(att, dict)
                    and _stamp(att.get("covered"), now)
                    and beacons.within_grace(att, now=now)
                    and (name.casefold() not in sample
                         or sample[name.casefold()]["verdict"] != beacons.RESTING)
                    and att.get("state") != beacons.RESTING)]
    down = [name for name in eligible
            if name.casefold() in sample
            and beacons.unreachable(sample[name.casefold()])]
    steward_down = (steward is not None
                    and steward.casefold() in sample
                    and steward in eligible
                    and beacons.unreachable(sample[steward.casefold()]))
    kept = {s.casefold() for s in latch["cohort"]} \
        if latch["phase"] == "down" else None
    skip = frozenset(n.casefold() for n in eligible
                     if n.casefold() not in kept) \
        if kept is not None else frozenset()
    required = _roll(rows, eligible, steward, now, skip)
    listed = len(required & set(sample))
    # A census with no rows, or short of a seat the roll holds, establishes
    # nothing, and says how short it is (an empty one only once a register
    # exists: a home where no seat has attended has no roll to list).
    short = listed < len(required) or (not sample and any(
        isinstance(row.get("attendance"), dict) for row in rows.values()))
    whole = bool(whole_census and not short
                 and _same(rows, sample, required, now))
    looks = steward_down or (len(down) >= 2 and len(down) * 2 > len(eligible))
    is_down = whole and looks
    said = ("the steward seat is unreachable" if steward_down else
            "%d of %d seats are unreachable" % (len(down), len(eligible)))
    out.update(looks_down=looks, down=down, short=short)
    if not whole:
        out["why"] = ("the beacons census lists %d of %d seats"
                      % (listed, len(required))) if short else \
            "the census is incomplete (%s)" % (
                "its probes did not all run" if not whole_census else
                "it does not match the roster's attendance register")
        return out
    if latch["phase"] == "clear":
        if not is_down:
            return dict(out, phase="clear", why="no fleet outage")
        cohort = sorted(n for n in eligible
                        if not _dead_before(rows[n]["attendance"], now)) \
            or sorted(eligible)
        return dict(out, phase="down", why=said, latch={
            "phase": "down", "cohort": cohort, "steward": steward,
            "delivered": False})
    # The denominator was frozen at alarm time: dropping a seat from today's
    # roll must never manufacture a recovery, and a cohort seat that left the
    # roster stays owed BY NAME (`gone`). A cohort seat the census READS as
    # RESTING owes none: the owner paused it, so no wake is owed and only
    # his explicit resume brings it back. Requiring it to cover would hold
    # the storm (and every storm after it) open on his own act.
    owed, seen = [], set()
    for seat in latch["cohort"] + ([latch["steward"]]
                                   if latch["steward"] else []):
        key = seat.casefold()
        if key in seen or (key in sample and key in names
                           and sample[key]["verdict"] in (beacons.COVERED,
                                                          beacons.RESTING)):
            continue
        seen.add(key)
        owed.append(seat)
    out.update(owed=owed, gone=[s for s in owed if s.casefold() not in names])
    if not latch["delivered"]:
        # The phone never heard it: it ends as soon as the fleet is up, and
        # nothing owes an all-clear for it.
        return dict(out, phase="down", why=said) if is_down else \
            dict(out, phase="clear", latch=dict(CLEAR),
                 why="the outage ended before the phone heard it")
    if is_down:
        return dict(out, phase="down", why=said)
    if owed:
        return dict(out, phase="down", why="%d of the episode's seats %s not "
                    "covered yet: %s" % (len(owed), "is" if len(owed) == 1
                                         else "are", named(owed, out["gone"])))
    return dict(out, phase="clear", latch=dict(CLEAR),
                why="every seat of the episode is covered again "
                "(or resting)")


def named(seats, gone=()):
    """`seats` for a reader, each seat that left the roster marked so."""
    off = {s.casefold() for s in gone}
    return ", ".join(s + (" (off the roster)" if s.casefold() in off else "")
                     for s in seats)


def fleet(now, latch=None):
    """`qualify` of the census `beacons --post` last recorded, against the
    roster it was recorded with, both read under the roster lock -> qualify's
    answer plus `stale` (True when the census could not speak for now).

    A recorded census that is missing, unreadable, stamped with no readable
    instant or older than STALE_S is phase "unknown" with `stale` True, and
    its `why` says so ("the beacons census is N min old"): it establishes
    neither fleet-down nor its end, and the open episode is kept as it was.
    Raises when the roster cannot be read: unreadable is not clear."""
    now = float(now)
    rows, rep, why = _read()
    latch = episode(CLEAR if latch is None else latch)
    if rep is None and why is None:
        # A home where no seat has ever attended has no roll to be down (an
        # install, a test); a register with rows and no census is a beacons
        # timer that never recorded one, and that is named.
        if latch["phase"] == "clear" and not any(
                isinstance(row.get("attendance"), dict)
                for row in rows.values()):
            return {"phase": "clear", "why": "no seat has attended yet",
                    "latch": latch, "looks_down": False, "stale": False}
        why = ("no beacons census is recorded yet (helm beacons --post "
               "records one)")
    at = rep.get("at") if isinstance(rep, dict) else None
    if why is None and not _stamp(at, now):
        why = "the beacons census carries no readable time"
    elif why is None and now - at > STALE_S:
        why = "the beacons census is %d min old" % ((now - at) // 60)
    if why is not None:
        return {"phase": "unknown", "why": why, "latch": latch,
                "looks_down": False, "stale": True}
    return dict(qualify(rep, rows, now, complete(rep), latch), stale=False)
