#!/usr/bin/env python3
"""The seat's own session transcript: when did its last RESPONSE terminate?

WHAT THIS PROVES, AND THE NAME IS THE WHOLE POINT. It reports the timestamp
of the last assistant record whose `stop_reason` is `end_turn` — a TERMINAL
RESPONSE. It does NOT prove the harness accepted the stop, and no source in
this tree does, because nothing observable from inside helm can:

  * helm's own aggregated Stop allow is not acceptance. A foreign plugin Stop
    hook outside helm's dispatcher can veto after every helm handler has
    allowed, and helm never sees that answer.
  * A terminal record cannot be told apart from a VETOED stop whose
    continuation has not been appended yet. THE ONE FAILURE SCHEDULE THIS
    MODULE CANNOT DISTINGUISH, written here rather than discovered later: a
    Stop hook vetoes within its timeout, and the harness then stalls, errors
    or exits before appending the continuation. The old `end_turn` stays
    terminal indefinitely and reads as a response at time T. A seat in that
    state older than the threshold reads DARK, which is the correct answer
    for a harness that died; younger than the threshold it reads HOLDING for
    at most the threshold, which is the conservative direction.

So the predicate this feeds asks about ABSENCE over a threshold — no terminal
response in two hours AND no live descendant work — and never about
certifying one particular turn.

THE PENDING WINDOW, which removes the cheap false positive. A terminal record
with nothing after it that is YOUNGER than the longest configured Stop-hook
timeout has not settled: a veto would still be running, and a hook that
exceeds its timeout is treated as allow. Younger than that bound is UNKNOWN,
never HOLDING and never DARK. The bound is READ FROM THE SETTINGS THE SEAT
LAUNCHED WITH rather than hardcoded, because a seat that raises its timeout
would otherwise be certified early by a constant nobody updated.

THE WINDOW IS NOT HYPOTHETICAL, AND THE ORDER IS MEASURED — ON ONE SEAT, IN
ONE SESSION, AGAINST HELM'S OWN STOP GUARD, which is the scope of the claim and
not a fleet-wide constant. There, the harness persists the assistant `end_turn`
record BEFORE it invokes the Stop hooks: every one of 151 response terminals
carries its Stop hook records AFTER it. So there is a real interval in which a terminal record
is last, nothing follows it yet, and a hook is still free to veto; 34 of those
151 were in fact refused, and the turn continued. Reading EOF without this
window would have certified every one of them.

WHAT THE INTERVAL COSTS, observed on those same 34 refusals and carrying the
same one-seat scope: from the persisted terminal record to the refusal record,
min 2.9s, median 6.7s, p90 10.9s, max 14.2s. The configured bound on that seat
is 60s, so the window covered the observed worst case about four times over.
THAT MARGIN IS NOT LOAD-BEARING AND MUST NOT BE READ AS ONE: the bound is
whatever the seat's own settings declare, and this distribution is evidence
that the mechanism is calibrated against something real rather than a licence
to shrink it. A seat whose hooks are slower raises its own bound by raising its
timeout, and no seat is certified by this sample.

WHAT IT STILL DOES NOT COVER, and it is the schedule named above: a veto
followed by the harness stalling before the continuation is appended leaves a
stale terminal record that no waiting period distinguishes. That is why the
claim is recency and not acceptance.

TWO SHAPES IN THE FORMAT ARE MEASURED RATHER THAN ASSUMED, because reading
them wrong produces a confident answer rather than an error:

  * ONE RESPONSE EMITS SEVERAL `end_turn` RECORDS. A response with a thinking
    block and a text block writes two records, BOTH stamped `end_turn` (23 of
    174 in that sample). The run must be collapsed or the "is anything after
    it" question answers itself wrongly on the first record of its own run.
  * A REFUSED STOP DOES NOT APPEND `tool_use` CONTINUATION to the same
    response. Of 151 distinct response terminals, ZERO were followed by an
    assistant `tool_use` record before the next turn marker; the stop-guard's
    refusal arrives as a USER-role record and the assistant then emits a new
    response. So "followed by a continuation" is not a discriminator for
    acceptance on this harness, which is a second reason the claim is
    terminal-response recency rather than acceptance.
"""
import glob
import json
import math
import os
import re
from . import pk

#: The name `ownership.TERMINAL_RESPONSE_SOURCES` allows. Kept beside the
#: reader so the two cannot drift apart silently.
SOURCE = "session transcript terminal response"

#: Claude Code's default per-hook timeout, in seconds, used for any Stop hook
#: that does not declare one. Only a DEFAULT — `stop_hook_timeout_s` reads the
#: real settings and takes the maximum.
DEFAULT_HOOK_TIMEOUT_S = 60.0

#: THE TOTAL a scan may spend. The whole file is read up to this; past it the
#: reading is marked INCOMPLETE and can no longer carry a revert. A live
#: transcript reached 96 MB in one session, which is what sets the number.
#:
#: AN EARLIER DESIGN INFERRED INSTEAD OF READING, and it was wrong twice in
#: the same way: it treated a record's POSITION, and then a window's SPAN, as
#: evidence about bytes outside the window. Both rest on timestamps rising
#: with append order, and NOTHING GUARANTEES THAT — a record bearing an old
#: timestamp can be appended at any moment, so neither position nor span
#: places an unread byte outside the horizon. The only honest basis for "there
#: was no terminal response" is having LOOKED at every record that could have
#: been one. Past this budget the scan says UNKNOWN and names what it could
#: not reach.
MAX_SCAN_BYTES = 96 * 1024 * 1024

#: A session id becomes a FILENAME, and it arrives from a roster row that
#: anybody can write. Anchored, and no separators or dots can survive it.
_SESSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")


class Reading(object):
    """Exactly one of these four is what the transcript had to say.

    `covered` False means NO TRANSCRIPT FOR THIS SESSION — the seat may be a
    proxy family that writes none at all, which is not evidence that it has
    done nothing. `err` means a read failed. `pending` means a terminal
    record was found but is too young to have settled. `ts` is a settled
    terminal response.
    """

    __slots__ = ("ts", "pending", "err", "covered", "path", "incomplete")

    def __init__(self, ts=None, pending=None, err=None, covered=True,
                 path=None, incomplete=None):
        self.ts = ts
        self.pending = pending
        self.err = err
        self.covered = covered
        self.path = path
        #: Why this reading's COVERAGE is partial, or None. A hole can only
        #: hide a NEWER terminal response, so it can only make a seat look
        #: older than it is — which means an incomplete reading may support a
        #: LIVE answer and may never support a revert. The predicate enforces
        #: that; this field is how it knows.
        self.incomplete = incomplete


def _claude_root(root=None):
    """Where the harness keeps its transcripts. `~/.claude` unless overridden.

    THE OVERRIDE IS NOT A TEST CONVENIENCE, it is the hermetic seam this
    surface was missing. The suite pins `HELM_HOME`, NOT `HOME`, so an
    unqualified `expanduser("~")` here reads THE OPERATOR'S REAL TRANSCRIPTS
    from inside a test run — a live seat's session id colliding with a
    fixture's would hand an arm real data, and the arm would pass or fail on
    what the box happened to be doing. Every other durable surface in this
    tree takes an env override for the same reason.
    """
    if root:
        return root
    env = os.environ.get("HELM_CLAUDE_DIR")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".claude")


def _timeout_of(hook, where):
    """-> (seconds, err) for ONE hook entry. Every value is untrusted."""
    if not isinstance(hook, dict):
        return None, "%s: a hook entry is not an object" % where
    raw = hook.get("timeout")
    if raw is None:
        # NOT ZERO. A hook that declares no timeout runs under the harness
        # default, and treating it as zero would shrink the window to the
        # shortest DECLARED one and certify inside it.
        return DEFAULT_HOOK_TIMEOUT_S, None
    if isinstance(raw, bool):
        return None, "%s: a Stop hook timeout is a boolean" % where
    try:
        secs = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None, "%s: a Stop hook timeout is not a number (%r)" % (
            where, raw)
    # NaN AND inf ARE THE DANGEROUS ONES, not the obviously-bad strings: every
    # comparison against NaN is False, so a NaN bound makes the PENDING test
    # silently answer "settled" for every record. An infinity does the
    # opposite and pends forever. Neither is a duration.
    if not math.isfinite(secs) or secs < 0:
        return None, "%s: a Stop hook timeout is not a finite non-negative " \
                     "duration (%r)" % (where, raw)
    return secs, None


def stop_hook_timeout_s(root=None, project_dirs=()):
    """-> (seconds, err). The LONGEST Stop hook the seat's settings configure.

    THE MAXIMUM, not the minimum and not the first: the window is not settled
    until every Stop hook has either answered or been timed out, so the
    slowest one sets the bound.

    EVERY CONSUMED CONTAINER IS SHAPE-CHECKED, because this is JSON anybody can
    edit and it is read on a path with no per-seat exception boundary around
    it. A root object alone is not enough: `{"hooks": [1]}` makes `.get`
    raise on a list, and a raise here does not degrade one seat's evidence, it
    ABORTS THE WHOLE CENSUS. Malformed shape is reported as an acquisition
    ERROR so the caller can render UNKNOWN.

    An unreadable or unparseable settings file is an ERROR, not a quiet
    fallback to the default: silently substituting a shorter bound would
    certify responses early on exactly the seats whose configuration could not
    be checked.

    `project_dirs` are additional `.claude` directories whose settings the
    harness merges into the launched session. Helm supports a per-project
    `.claude/settings.local.json`, so a bound derived from the user root alone
    can be shorter than the session's effective bound.
    """
    roots = [_claude_root(root)] + [d for d in project_dirs if d]
    longest = None
    for cdir in roots:
        for name in ("settings.json", "settings.local.json"):
            path = os.path.join(cdir, name)
            where = os.path.join(os.path.basename(cdir.rstrip(os.sep)), name)
            try:
                with pk.open_regular(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                return None, "%s: %s: %s" % (where, e.__class__.__name__, e)
            if not isinstance(data, dict):
                return None, "%s: root is not an object" % where
            hooks = data.get("hooks")
            if hooks is None:
                continue
            if not isinstance(hooks, dict):
                return None, "%s: hooks is not an object" % where
            stop = hooks.get("Stop")
            if stop is None:
                continue
            if not isinstance(stop, (list, tuple)):
                return None, "%s: hooks.Stop is not a list" % where
            for matcher in stop:
                if not isinstance(matcher, dict):
                    return None, "%s: a Stop matcher is not an object" % where
                entries = matcher.get("hooks")
                if entries is None:
                    continue
                if not isinstance(entries, (list, tuple)):
                    return None, "%s: a Stop matcher's hooks is not a list" \
                        % where
                for hook in entries:
                    secs, err = _timeout_of(hook, where)
                    if err:
                        return None, err
                    longest = secs if longest is None else max(longest, secs)
    if longest is None:
        # No Stop hooks configured at all: nothing can veto, so the default
        # is the honest bound rather than zero.
        return DEFAULT_HOOK_TIMEOUT_S, None
    return longest, None


def transcript_path(session, root=None):
    """-> (path, err). Found by SESSION ID IN THE FILENAME, never by mtime.

    Newest-by-mtime picks whichever session wrote last, which on a box running
    twenty seats is somebody else's. The session id is the binding, and two
    files carrying one session id is an AMBIGUITY to report rather than a
    choice to make.
    """
    s = str(session or "")
    if not _SESSION_RE.match(s):
        return None, "session id is not a usable transcript name"
    hits = sorted(glob.glob(os.path.join(
        _claude_root(root), "projects", "*", "%s.jsonl" % s)))
    if not hits:
        return None, None
    if len(hits) > 1:
        return None, "%d transcripts carry session %r" % (len(hits), s)
    return hits[0], None


def _epoch(stamp):
    """ISO-8601 `2026-09-11T18:29:35.964Z` -> epoch seconds, or None."""
    if not isinstance(stamp, str) or not stamp:
        return None
    import datetime
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _tail_records(path, tail_bytes):
    """-> (records, truncated, trailing_partial, unreadable, err).

    `unreadable` counts SLOTS whose content could not be read as a record.
    They are not skipped quietly: a caller concluding that the window holds no
    terminal response has to know that some of it could not be looked at.

    A PARTIAL FINAL LINE IS NOT A RECORD. The harness appends while this runs,
    so the last bytes can be half a JSON object; parsing it would either raise
    or, worse, succeed on a prefix that means something else.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            chunk = fh.read()
    except OSError as e:
        return None, None, None, None, "%s: %s" % (e.__class__.__name__, e)
    truncated = size > tail_bytes
    trailing_partial = bool(chunk) and not chunk.endswith(b"\n")
    lines = chunk.split(b"\n")
    if trailing_partial:
        lines = lines[:-1]
    if truncated and lines:
        # The first line began before the window, so it is a fragment too.
        lines = lines[1:]
    out, unreadable = [], 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            out.append(None)            # unparseable, but it IS a record slot
            unreadable += 1
            continue
        if not isinstance(rec, dict):
            out.append(None)
            unreadable += 1
            continue
        out.append(rec)
    return out, truncated, trailing_partial, unreadable, None


def last_terminal_response(session, now=None, timeout_s=None, root=None,
                           max_bytes=None):
    """-> Reading. The last settled terminal response for THIS session.

    `timeout_s` overrides the settings-derived pending window; it exists so a
    caller that already read the settings does not re-read them per seat, and
    so an arm can drive a raised timeout without writing a settings file.

    `max_bytes` overrides `MAX_SCAN_BYTES` for one call. There is deliberately
    no tail or horizon parameter: a caller supplying either would be buying an
    inference this module refuses to make.
    """
    import time

    path, err = transcript_path(session, root=root)
    if err:
        return Reading(err=err)
    if not path:
        return Reading(covered=False)

    if timeout_s is None:
        timeout_s, terr = stop_hook_timeout_s(root=root)
        if terr:
            return Reading(err="stop-hook timeout unreadable: %s" % terr,
                           path=path)

    # THE WHOLE FILE, UP TO THE BUDGET — there is no cheap first window, and
    # the reason is the same premise that killed the horizon rule. A tail
    # holds the bytes written LAST, which is a fact about WRITE order, not
    # about the `timestamp` FIELD: a record written before the cutoff can
    # carry any timestamp at all. So "the latest terminal record in the tail"
    # is not "the latest terminal record", and an 8 MB window could hide a
    # RECENT one just below it. Position orders nothing here; only reading
    # does.
    budget = MAX_SCAN_BYTES if max_bytes is None else max_bytes
    records, truncated, trailing_partial, unreadable, err = _tail_records(
        path, budget)
    if err:
        return Reading(err=err, path=path)

    at = now if now is not None else time.time()

    # ANY UNREADABLE SLOT, ANYWHERE, BLOCKS A CONFIDENT ANSWER. It could have
    # been a terminal record — and since position says nothing about its
    # timestamp, it could have been a MORE RECENT one than anything read. That
    # is true whether or not a terminal record was found, so there is one
    # check rather than a before/after pair, and the earlier "a hole BEFORE
    # the record is harmless" reasoning is withdrawn: it rested on position
    # ordering timestamps, which is exactly what is not available.
    #
    # A TRAILING PARTIAL LINE IS SUCH A SLOT, and the one most likely to
    # matter: it is the newest thing in the file and a half-written assistant
    # terminal record looks exactly like it.
    # A HOLE CAN ONLY HIDE A **NEWER** TERMINAL RESPONSE, so it can only make
    # this seat look OLDER than it is. That asymmetry is the whole cure: an
    # incomplete reading may still support a LIVE answer, and may never
    # support a revert. It is reported rather than raised, and
    # `ownership.owner_state` refuses to reach DARK on it.
    #
    # BLANKET-REFUSING HERE WAS MEASURED AND UNUSABLE: a live transcript is
    # almost always mid-write, so a trailing fragment is the NORMAL state and
    # every rostered seat read UNKNOWN — 46 holding went to 0 on the live
    # fleet. A predicate that answers UNKNOWN for everyone is not conservative,
    # it is silent.
    holes = unreadable + (1 if trailing_partial else 0)
    incomplete = None
    if holes or truncated:
        parts = []
        if holes:
            parts.append("%d record(s) could not be read" % holes)
        if truncated:
            parts.append("the transcript exceeds the %d byte scan budget"
                         % budget)
        incomplete = ("%s, so a MORE RECENT terminal response may exist that "
                      "this scan did not see" % "; ".join(parts))

    # THE LATEST TERMINAL RESPONSE IS THE ONE WITH THE GREATEST TIMESTAMP, not
    # the last one in the file. Ties keep the later POSITION, which is the
    # only thing position is good for: of two records claiming one instant,
    # the one written second is the later event.
    last, best = None, None
    for i, rec in enumerate(records):
        if rec is None:
            continue
        if rec.get("type") != "assistant":
            continue
        if ((rec.get("message") or {}).get("stop_reason")) != "end_turn":
            continue
        ts_i = _epoch(rec.get("timestamp"))
        if ts_i is None:
            return Reading(err="a terminal record carries no readable "
                               "timestamp", path=path)
        if best is None or ts_i >= best:
            last, best = i, ts_i

    if last is None:
        # No terminal response among the records that were readable. With
        # complete coverage that is a real absence; with holes it is only an
        # absence IN WHAT WAS SEEN, which cannot carry a revert.
        return Reading(path=path, incomplete=incomplete)

    ts = best

    # WAS ANYTHING APPENDED AFTER IT? THIS is the one question position can
    # answer, and it is a different question from "is anything NEWER": append
    # order is WRITE order and that IS guaranteed, so a slot after this one
    # was written after it whatever timestamp it carries. ONE response emits
    # several `end_turn` records (measured: a thinking block and a text
    # block), and the tie-break above keeps the later position, so `last` is
    # the end of its own run rather than its first record.
    #
    # A HOLE OR A TRAILING FRAGMENT WOULD HAVE MARKED THIS READING INCOMPLETE
    # above, and the PENDING window is not asked on those — an incomplete
    # reading is already barred from a revert, and a fragment is itself a
    # successor. So among the slots this question is asked about, a slot
    # existing is the whole answer.
    followed = bool(records[last + 1:])
    if not followed:
        age = at - ts
        if age < timeout_s:
            return Reading(
                pending="a terminal record %.0fs old with nothing after it, "
                        "inside the %.0fs Stop-hook window" % (age, timeout_s),
                path=path)
    return Reading(ts=ts, path=path, incomplete=incomplete)
