"""The count of review rows a model family was sent, printed beside the
family's burn reading.

THE SUM, NOT THE SEND. The recipient rungs print a family's burn colour on a
row sent to one of its seats, and each such line is true about one send and
silent about all of them together: a family can be sent a day's review rows
in one night while every line reads the same. So every review row whose
recipient resolves to a model family gets one RECIPIENT line with the review
rows that family's seats were sent in the last 24 h and the last 5 h, and the
family's burn reading beside it, on one terminal line:

    kimi: N review rows sent to kimi in the last 24 h (M in the last 5 h)
    · burn RED money: the vendor refused on its usage quota (...)

A COUNT, NEVER A CAP. Nothing here refuses a send or changes a row. A fixed
number of sends per family is a guess about a budget that the burn flags
measure (`helm burn`), so the sender reads the count beside the measured
reading and decides.

ONE LEDGER READ. The count is taken from the snapshot the writer
(`dispatches._append_dispatch`) already reads for its duplicate check, so it
adds no ledger fold. That read is the one the append is proven against, not
one taken under the lock: every try but the last reads with no lock held, and
a ledger that moved before the append sends the write round again from a
fresh read (`dispatches._ledger_write`). So a second writer counts the first,
and the count runs inside the lock only on the last try.

THE BURN READING IS THE CACHED FOLD, NEVER A MEASUREMENT. The reader is
`burnflags.family_flag`: it opens the snapshot the watchdog pass wrote and
answers None when it is absent, past its bound or unreadable, so a send never
reads the burn inputs, folds them or writes a snapshot. No reading is said as
"burn unread", never as GREEN and never by leaving the reading off the line.

WHAT COUNTS: every review row whose genesis stamp is inside the window and
whose recipient resolves to the family through `seat.family_for` (the verified
launch metadata first, then the name), whatever state the row is in now. A row
that was sent spent the family's budget whether or not it was later cancelled.
"""
import time

DAY_S = 24 * 3600
RECENT_S = 5 * 3600
UNREAD = "burn unread"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _family_resolver():
    """name -> family or None, off ONE roster read for the whole count."""
    from . import seat, seat_usability
    runtimes = seat_usability.roster_runtimes()
    cache = {}

    def family_of(name):
        name = str(name or "")
        if name not in cache:
            runtime, verified = runtimes.get(name, (None, False))
            family, err = seat.family_for(name, runtime, verified)
            cache[name] = None if err else family
        return cache[name]
    return family_of


def count(current, family, family_of, now):
    """(24 h, 5 h) review rows in `current` sent to `family`'s seats.

    The string compare against the cutoff's DATE is only a cheap reject for
    rows days older than the window; the window itself is decided on the
    parsed instant, and a stamp that does not parse is not counted."""
    from . import pk
    day_from, recent_from = now - DAY_S, now - RECENT_S
    floor = time.strftime(_STAMP, time.gmtime(day_from))[:10]
    day = recent = 0
    for state in (current or {}).values():
        stamp = state.get("ts")
        if state.get("kind") != "review" or not isinstance(stamp, str) \
                or stamp < floor:
            continue
        when = pk.parse_ts_epoch(stamp)
        if when is None or when < day_from \
                or family_of(state.get("recipient")) != family:
            continue
        day += 1
        recent += when >= recent_from
    return day, recent


def _rows(n):
    return "%d review row%s" % (n, "" if n == 1 else "s")


def burn(family, now=None):
    """The family's burn reading off the CACHED fold, as one clause.

    `burnflags.family_flag` is the read the budget rung beside this one asks:
    it opens the persisted snapshot and never probes, folds or writes. A
    snapshot that is absent, past its bound or unreadable (including one the
    reader raises on), or a family the fold does not mint, is UNREAD."""
    try:
        from . import burnflags
        flag = burnflags.family_flag(family, now=now)
    except Exception:                                   # noqa: BLE001
        flag = None
    if not isinstance(flag, dict) or not flag.get("colour"):
        return UNREAD
    head = " ".join(str(part) for part in ("burn", flag["colour"],
                                           flag.get("axis")) if part)
    cause = flag.get("cause")
    return head + (": %s" % cause if cause else "")


class Tally(object):
    """One write's family count, taken by the ledger writer.

    `send` and `add` build one before the append and hand it to
    `dispatches._append_dispatch` as `family_count`. The writer calls it with
    the row it is about to write and the snapshot it holds, after the
    duplicate check and only for a row it is about to append. It returns
    nothing and changes nothing, so it cannot refuse a send. `line()` is the
    RECIPIENT note afterwards, or None when nothing was counted.

    A COUNT THAT FAILS SAYS SO, like the recipient rungs beside it: the row is
    written either way, and the note tells the sender the count is unknown
    rather than printing nothing."""

    def __init__(self, now=None):
        self.now = now
        self.recipient = self.family = self.clause = self.counted = None
        self._family_of = None

    def __call__(self, row, current):
        self.recipient = row.get("recipient_display") or row.get("recipient")
        self.counted = row.get("id")
        self.family = self.clause = None
        if row.get("kind") != "review":
            return
        try:
            # ONE ROSTER READ PER WRITE, not per try: a try the ledger sent
            # round again re-counts its fresh snapshot with the same resolver.
            if self._family_of is None:
                self._family_of = _family_resolver()
            self.family = self._family_of(row.get("recipient"))
            if self.family is None:
                return
            now = time.time() if self.now is None else self.now
            day, recent = count(current, self.family, self._family_of, now)
            self.clause = ("%s sent to %s in the last 24 h (%d in the last "
                           "5 h)" % (_rows(day + 1), self.family, recent + 1))
        except Exception as exc:                        # noqa: BLE001
            self.clause = ("review rows sent to %s UNKNOWN — the count itself "
                           "failed (%s: %s); this row was admitted uncounted"
                           % (self.family or "this family",
                              type(exc).__name__, exc))

    def line(self, row):
        """The RECIPIENT note for the row this write returned, or None.

        Only for the row this tally counted: a writer that retried and found
        its operation already on the ledger returns a row the tally never
        saw, and a count from the abandoned try is not about it. The burn
        reading is read here, after the append, so the writer's lock is never
        held across it. Whitespace is folded, so the note is one line."""
        if not self.clause or (row or {}).get("id") != self.counted:
            return None
        reading = burn(self.family, now=self.now) if self.family else UNREAD
        return " ".join(("%s: %s · %s" % (self.recipient, self.clause,
                                          reading)).split())
