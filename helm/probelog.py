"""ONE GRAMMAR FOR EVERY INSTRUMENT THAT APPENDS WHAT IT OBSERVED.

`<iso> EVENT pid=<n> | seat=<s> | k=v | k=v` — the shape hookprobe.log
established and stopprobe adopted, stated in stopprobe's own docstring as
"anything reading one file can read the other". Each instrument keeps its OWN
file and its own event names; what they share is how a line is written, how a
line is read back, and what counts as a line that arrived torn.

WHY THIS IS A MODULE AND NOT A CONVENTION. A convention is re-implemented, and
the second implementation is where the properties below stop being true. Every
one of them was bought by an incident rather than designed in:

  a write that raises takes down the hook it was observing, so it never raises;
  a writer that emits what its own reader refuses reports a record it has
    already made unreadable, so required fields are checked BEFORE the write;
  a payload key that collides with a header field silently rewrites the event
    the line announced, so header keys are reserved;
  a repeated key read as "the later value wins" turns a torn record into a
    plausible one, so a repeat is torn;
  float() accepts nan, inf and negatives, so a bare parse is not a validation
    and a duration that is not finite and non-negative is uncertainty;
  a missing FILE and an empty one answer the same zero rows, so absence is
    reported on the error channel and never rendered as a clean bill.

A SECOND COPY WOULD INHERIT THE COMMENTS AND NOT THE FIXES. The next defect
found in any of those lands in one copy, and the instrument that did not get it
keeps reporting confidently. helm has measured that class four times in one
day — two config-scalar readers disagreeing on 7 of 19 inputs, four retry-
horizon parsers of which one is bounded, and one module holding two rules about
what an unreadable provider means.

WHAT STAYS WITH THE INSTRUMENT: its path, its event names, which fields each
event requires, which fields are durations, and everything it concludes from
the rows. This module has no opinion about any of that.
"""
import datetime
import os
import re
import time

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

# THE PAYLOAD MAY NOT REWRITE THE HEADER. A record carrying `event=X` among its
# fields would otherwise overwrite the event the line actually announced, so a
# boundary record could be read as an end by a value it merely quotes. `pid` is
# excluded because it is written as a payload-shaped token in the header itself.
HEADER = ("ts", "event", "pid", "raw")


def seat(known=None):
    """THE SEAT THE CALLER ALREADY RESOLVED, else the validated seam.

    A KNOWN SEAT IS STILL LAUNDERED. It arrives from a resolver, not from a
    validated ingestion seam, and a log file is a sink somebody later greps —
    so it goes through the same scrub-and-clip every other direct-read display
    surface uses, and a name that survives it unchanged was already safe.

    home.chat_name is where every read of the seat name is routed so a name
    carrying controls or bidi overrides is REJECTED at the source rather than
    becoming a roster key, a chat from-field, or a line in a log somebody later
    greps. A raw environ read here would be a new sink for a name nothing
    validated, and a source tripwire enforces that.
    """
    from . import home
    try:
        if known:
            from .seats_common import _seat_label
            return _seat_label(known) or "unknown"
        return home.chat_name() or "unknown"
    except Exception:
        # A rejected name is not a reason to lose the record: what was measured
        # is still true and the seat is simply unknown.
        return "unknown"


def write(path, event, fields, required=(), known_seat=None):
    """Append one line. NEVER RAISES: this runs inside instruments that are
    observing something else, and an instrument that can take its subject down
    is worse than no instrument.

    AND IT NEVER WRITES WHAT ITS OWN READER WOULD REFUSE. A blank required
    field wrote successfully and then came back MALFORMED, so the writer
    reported a record it had already made unreadable — a success that is a lie
    about the file. Anything the parser requires is required here.

    Returns True when a line was written, False when it was refused or failed,
    so a caller can assert the SILENCE as easily as the record.
    """
    # ABSENT AND BLANK ARE THE SAME REFUSAL, and the first cut only caught the
    # blank one. It iterated the FIELDS, so a required key that was simply not
    # supplied was never examined -- and a caller that OMITS empty values (the
    # natural way to write "this episode had no identity") sailed straight past
    # the check and filed a record the parser then reported as MALFORMED. That
    # is the exact lie this function exists to prevent, reintroduced by looking
    # at the wrong collection. The parser asks `not row.get(k)`, which is true
    # for both, so the writer asks the same question of the same keys.
    supplied = dict(fields)
    for key in required:
        if not str(supplied.get(key, "")).strip():
            return False
    try:
        line = "%s %s pid=%d | seat=%s | %s\n" % (
            time.strftime("%FT%T"), event, os.getpid(), seat(known_seat),
            " | ".join("%s=%s" % (k, v) for k, v in fields))
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(line)
            fh.flush()
        return True
    except Exception:
        return False


def parse(line, events, required, numeric=(), malformed="BAD"):
    """-> a row, a MALFORMED row, or None for a line this instrument did not write.

    THREE OUTCOMES, NOT TWO. A foreign line — a sibling instrument's records
    may live next door — is not ours and is skipped. But a line that NAMES ONE
    OF OUR EVENTS and then fails to carry its fields is OUR OWN torn or
    truncated record, and dropping it renders a damaged log exactly like a
    clean one.

    `required` maps each event to the fields that event must carry; `numeric`
    names the fields that are durations, which are finite and non-negative or
    they are uncertainty and never a silent zero.
    """
    parts = line.strip().split(" | ")
    head = parts[0].split()
    if len(head) < 2 or head[1] not in events:
        return None
    row = {"ts": head[0], "event": head[1], "raw": line.strip()}
    bad = []
    for chunk in head[2:] + parts[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in HEADER and key != "pid":
            bad.append("reserved:" + key)
            continue
        if key in row:
            bad.append("repeated:" + key)
            continue
        row[key] = value
    for key in numeric:
        if key in row:
            # float() ACCEPTS nan, inf AND NEGATIVES, so a bare parse is not a
            # validation: `total=nan` would compare false against every bound.
            try:
                seconds = float(row[key])
            except (TypeError, ValueError):
                bad.append("not-a-number:" + key)
                continue
            if seconds != seconds or seconds in (float("inf"), float("-inf")) \
                    or seconds < 0:
                bad.append("not-a-duration:" + key)
    missing = [k for k in ("pid",) + tuple(required.get(head[1], ()))
               if not row.get(k)]
    if missing or bad:
        row["event"] = malformed
        row["missing"] = ",".join(missing + bad)
    return row


#: WHAT `write` STAMPS. `time.strftime("%FT%T")` is the LOCAL wall clock with
#: NO zone marker, so every row in every probelog file carries a naive local
#: timestamp. Declared as a constant because a cut has to be able to ASK.
CLOCK = "naive-local"

#: The clock a store keeps when it records absolute instants instead of local
#: wall-clock text — `hooklatency`'s `time_ns` is the live example.
CLOCK_ABSOLUTE = "epoch"

_AWARE = ("Z", "z", "+", "UTC", "utc")


def cut(value, clock=CLOCK):
    """(comparable cut, refusal) for a time boundary against `clock`'s rows.

    THE REFUSAL IS THE WHOLE POINT AND IT IS NOT A CONVENIENCE. A cut string
    and a log line are both `2026-09-13T19:00:00`-shaped, they compare without
    complaint, and the comparison is MEANINGLESS when they are kept on
    different clocks. The failure is silent and it is directional: a UTC cut
    against a naive-LOCAL log selects a window hours away from the one the
    caller named, and the usual result is a clean, believable ZERO. A zero is
    the one answer nobody re-checks, because "no samples in that window" reads
    as a fact about the fleet rather than as a fact about the cut.

    MEASURED: a seam measurement was split at UTC over `stopprobe.log`, whose
    rows this module stamps in local time, and returned NO SAMPLES on a
    populated file. It was caught only because a clean zero is suspicious,
    which is not a mechanism (task/2458).

    SO A CUT MUST DECLARE THE CLOCK IT IS CUTTING, and a mismatch REFUSES
    rather than converting. Converting would be worse: it would require this
    function to guess which zone an unmarked string meant, and the guess is
    exactly the thing that was wrong.

    `naive-local` rows accept a naive cut and refuse a zone-aware one.
    `epoch` rows accept an AWARE cut (an absolute instant compares to an
    absolute instant) and refuse a naive one, which is the same rule read from
    the other side.
    """
    text = str(value or "").strip()
    if not text:
        return None, None                # no cut asked for is not a bad cut
    # PARSE FIRST, THEN ASK THE PARSED OBJECT. A hand-written awareness test
    # has to enumerate ISO-8601's zone spellings, and the first cut of this
    # function matched only `+HH:MM` — so `+HHMM` and `+HH`, which
    # `fromisoformat` accepts as AWARE, read as naive here and sailed through
    # the very check they had to fail. `tzinfo is not None` is the datetime
    # module's own answer and no spelling escapes it.
    # A CUT FINER THAN THE PARSER IS A BOUNDARY THAT MOVES SILENTLY.
    # `fromisoformat` accepts any number of fractional digits and TRUNCATES to
    # microseconds without complaint — `...00.000000001Z` parses to microsecond
    # ZERO, so a nanosecond cut is swallowed whole and the caller gets a
    # window they did not ask for. Refusing is the same rule as the clock
    # mismatch one clause up: this function never silently relocates a
    # boundary, in either unit.
    # BOTH SEPARATORS, AND THE LONGEST RUN. ISO-8601 allows a COMMA as the
    # decimal mark and `fromisoformat` accepts it — measured:
    # `...19:00:00,000000001+00:00` parses to microsecond ZERO. A dot-only
    # search found nothing there and let exactly the case this clause exists
    # for through, silently. Taking the longest of ALL fraction runs also
    # stops this depending on WHICH field carries one.
    runs = [len(d) for d in re.findall(r"[.,](\d+)", text)]
    if runs and max(runs) > 6:
        return None, ("the cut %r carries %d fractional digits and this parser "
                      "keeps 6 — the rest would be dropped silently and the "
                      "boundary would not be where you put it. Give the cut to "
                      "microseconds." % (text, max(runs)))
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00")
                                                 .replace("z", "+00:00"))
    except ValueError:
        return None, ("the cut %r is not an ISO-8601 timestamp" % text)
    # utcoffset(), NOT tzinfo PRESENCE. A tzinfo whose `utcoffset()` answers
    # None is nominally naive under the datetime contract, so the attribute
    # being set is not the same fact as the value being placed on a scale.
    aware = parsed.utcoffset() is not None
    if clock == CLOCK and aware:
        return None, ("the cut %r names a zone, and these rows are stamped on "
                      "the LOCAL wall clock with no zone (probelog.write uses "
                      "time.strftime). Comparing them selects a window hours "
                      "from the one you named and usually returns a clean "
                      "zero. Give the cut in local time, without a zone."
                      % text)
    if clock == CLOCK_ABSOLUTE and not aware:
        return None, ("the cut %r carries no zone, and this store records "
                      "ABSOLUTE instants. An unmarked string cannot be placed "
                      "on that scale without guessing which zone you meant, "
                      "and that guess is the defect. Give the cut with an "
                      "explicit zone (a trailing Z, or an offset)." % text)
    if clock == CLOCK_ABSOLUTE:
        # NANOSECONDS, BY INTEGER ARITHMETIC, WITH NO FLOAT ON THE PATH. These
        # rows are `time_ns`, and an epoch instant needs nineteen significant
        # digits while a float64 carries about sixteen — so `timestamp()`
        # followed by a nanosecond scale SHIFTS the cut by up to hundreds of
        # nanoseconds. A boundary that moves is the same defect this function
        # exists to refuse, one unit down.
        #
        # AND THE OUTPUT UNIT IS NOT A CLAIM ABOUT THE INPUT: `fromisoformat`
        # parses to MICROSECONDS, so a cut carrying finer digits is rejected
        # or truncated by the parser, never honoured here. Integer nanoseconds
        # out means the BOUNDARY does not drift; it does not mean nanosecond
        # cuts are accepted.
        delta = parsed - _EPOCH
        return ((delta.days * 86400 + delta.seconds) * 1_000_000_000
                + delta.microseconds * 1000), None
    # NAIVE ROWS ARE COMPARED AS TEXT, not as datetimes, because that is what
    # they are: `parse` hands back the stamped string. `isoformat` renders the
    # same `%FT%T` shape the writer stamps AND KEEPS A FRACTION when the caller
    # gave one — `strftime("%FT%T")` floored it, silently widening the window
    # by up to a second. Rows carry no fraction, so a fractional cut sorts
    # immediately after its own second, which is the correct boundary in both
    # directions rather than an approximation of it.
    return parsed.isoformat(), None


def window(rows, since=None, until=None, clock=CLOCK, key="ts"):
    """(rows in [since, until), refusal). BOTH CUTS GO THROUGH `cut`.

    RETURNED BESIDE THE ROWS, never raised and never folded into them: a
    caller that drops the refusal at a subscript gets the UNFILTERED rows and
    a story about a window that was never applied, which is the same silent
    wrongness one layer up.
    """
    lo, why = cut(since, clock)
    if why:
        return [], why
    hi, why = cut(until, clock)
    if why:
        return [], why
    out = []
    for row in rows:
        stamp = (row or {}).get(key)
        if stamp is None:
            continue
        if lo is not None and stamp < lo:
            continue
        if hi is not None and stamp >= hi:
            continue
        out.append(row)
    return out, None


def read(path, events, required, numeric=(), malformed="BAD",
         absent=None, since=None, until=None):
    """-> (rows, err). `err` is a sentence when the log could not be READ, or
    when a time cut was asked for on the wrong clock.

    A MISSING FILE IS NOT AN EMPTY ONE. An instrument that has observed nothing
    worth recording and an instrument that was never wired produce the same
    zero rows; only the error channel can separate them, so absence of the file
    is reported and never rendered as a clean bill. `absent` is the caller's
    sentence for that case, because only the caller knows what its own silence
    would have meant.
    """
    if not os.path.exists(path):
        return [], (absent or "no log at %s — UNMEASURED, not quiet" % path)
    try:
        with open(path, "r", errors="replace") as fh:
            rows = [r for r in (parse(line, events, required, numeric,
                                      malformed) for line in fh) if r]
    except OSError as exc:
        return [], "log at %s unreadable (%s) — UNMEASURED" % (
            path, type(exc).__name__)
    # A REFUSED CUT RETURNS NO ROWS, not every row. Handing back the unfiltered
    # set beside a refusal invites the caller to use the rows and drop the
    # sentence, which reproduces the defect one layer up.
    return window(rows, since, until)
