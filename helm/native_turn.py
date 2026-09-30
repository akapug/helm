#!/usr/bin/env python3
"""Which model a NATIVE Claude seat was answering with at one moment.

`seats_runtime.native_session_model` answers the SESSION, and refuses (UNKNOWN)
when the session named two models: right for a session census, and 16 of 62
live sessions span a model change. This module answers a MOMENT from the same
transcript (task/3508), for two callers with different questions:

  ROUTING   (`at=None`)  who answers next: the newest model entry.
  ADMISSION (`at=T`)     which model a RECORDED read (a hold, a verdict) was
                         made on: the newest entry at or before T. Never the
                         newest turn: a seat that held on Sonnet and switched
                         to Opus afterwards would be admitted as Opus, which
                         fails OPEN.

IT READS THE TRANSCRIPT BACKWARDS, in bounded chunks. Both questions reach
only recent turns, while a long-running native seat's transcript passes
600 MiB, so a forward scan pays for the whole file to learn its tail and
gives up past the byte bound.

A SUBAGENT ACTS IN ITS SEAT'S NAME: it runs `helm dispatch hold` and
`verdict` as the seat (measured: a subagent recorded a verdict for its seat).
So ADMISSION also reads what the seat's subagents wrote around the moment,
the isSidechain lines of the main file and each `<session>/subagents/`
transcript (a Workflow's agents one level down) touched in the ten minutes
before it: another model named there is UNKNOWN, because the read may have
been that model's. This is a window, not the agent that ran the command.
ROUTING reads the seat alone.

Like its sibling it is SELF-REPORTED, never MEASURED: the seat's own process
wrote the file. It lives outside `seats_runtime` because that module sits at
the seats-split 1000-line budget (helm/splitbudget.py).
"""
import json
import math
import os
import re

from . import turnresponse
from .seats_runtime import (MODEL_ABSENT, MODEL_SELF_REPORTED, MODEL_UNKNOWN,
                            _RUNTIME_MODEL, _TRANSCRIPT_NON_MODEL,
                            _TRANSCRIPT_READ_BYTES)


#: No entry within this long before the moment: the seat may have switched
#: since, so the moment is not pinned to one id and the answer is UNKNOWN.
_TURN_FRESHNESS_S = 30 * 60
#: Two models named within this long before the moment: the seat was
#: switching right then, so neither is chosen and the answer is UNKNOWN.
_TURN_UNAMBIGUOUS_S = 10 * 60
#: The sibling's bound, counted back from the END: past it the walk has not
#: established its answer, and says UNKNOWN rather than a partial reading.
_TURN_READ_BYTES = _TRANSCRIPT_READ_BYTES
#: One backward read. A performance choice that bounds memory, not a fact.
_TURN_CHUNK_BYTES = 1 << 20
#: What the backward walk yields when the byte bound ends it early.
_CUT = object()
#: A `"timestamp"` key and its instant, in a line's raw bytes. An escaped
#: occurrence inside a string (`\"timestamp\"`) does not match.
_STAMP = re.compile(rb'"timestamp"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d'
                    rb'(?:\.\d+)?Z)"')


def _moment(at):
    """(epoch seconds or None, placed) for one `at`.

    None is ROUTING and stays None. A number is the moment; a string is a
    recorded stamp, the ledger's (`...T06:55:00Z`) or a transcript's
    (fractional seconds). Anything else, and a stamp that does not parse, is
    a RECORDED READ WHOSE MOMENT CANNOT BE PLACED: `placed` is False and the
    reader answers UNKNOWN, never routing's newest turn."""
    if at is None:
        return None, True
    if isinstance(at, bool):
        return None, False
    if isinstance(at, (int, float)):
        return (float(at), True) if math.isfinite(at) else (None, False)
    epoch = turnresponse._epoch(at) if isinstance(at, str) else None
    return epoch, epoch is not None


def _turn_entry(line):
    """(epoch seconds, model, sidechain) one transcript line records, else
    None.

    The substring test runs before the parser, as in the sibling: most lines
    cannot carry the key. A line that does not parse is the normal state of a
    file the harness is still appending to, so it is skipped. A `<synthetic>`
    turn is the harness's own composition, no model's. An `isSidechain` turn
    is a subagent's written into the main file: never the seat's model, and
    for ADMISSION a model acting in the seat's name."""
    if b'"model"' not in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    name = message.get("model")
    name = name.strip() if isinstance(name, str) else ""
    if not name or name in _TRANSCRIPT_NON_MODEL:
        return None
    epoch = turnresponse._epoch(entry.get("timestamp"))
    return None if epoch is None \
        else (epoch, name, entry.get("isSidechain") is True)


def _newest_stamp(line):
    """The newest instant any `"timestamp"` key in one raw line names, or
    None. A bytes scan, never the parser, because it runs on EVERY line the
    walk passes, including the large ones the substring test lets skip it.

    IT ONLY ENDS A WALK. A line whose newest stamp is older than a window
    has its own top-level stamp older than it too, so under the file order
    the reader already assumes, no later-read entry can lie inside it."""
    newest = None
    for match in _STAMP.finditer(line):
        epoch = turnresponse._epoch(match.group(1).decode("ascii"))
        if epoch is not None and (newest is None or epoch > newest):
            newest = epoch
    return newest


def _lines_newest_first(handle):
    """Every line of an open binary file, newest first, reading at most
    `_TURN_READ_BYTES` back from its end in `_TURN_CHUNK_BYTES` reads. Yields
    `_CUT` and stops when the bound ends the walk before the file's start.

    A line split by a read boundary is carried into the next (older) read and
    judged whole: the first piece of each read is the tail of a line that
    began earlier, unless the read reached the file's start."""
    end = handle.seek(0, os.SEEK_END)
    pos, carry = end, b""
    while pos > 0:
        step = min(_TURN_CHUNK_BYTES, pos, _TURN_READ_BYTES - (end - pos))
        if step <= 0:
            yield _CUT
            return
        pos -= step
        handle.seek(pos)
        parts = (handle.read(step) + carry).split(b"\n")
        carry = parts[0]
        for line in reversed(parts[1:]):
            yield line
    yield carry


def _touched_since(directory, floor):
    """The `*.jsonl` files directly in `directory` last written at or after
    `floor`, or None when the directory or a file's stat cannot be read (a
    directory that does not exist holds none; a file that vanished mid-list
    is skipped)."""
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return []
    except OSError:
        return None
    found = []
    for entry in entries:
        if not entry.name.endswith(".jsonl"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if mtime >= floor:
            found.append(entry.path)
    return found


def _subagent_transcripts(path, sid, floor):
    """Every subagent transcript of session `sid` touched at or after
    `floor`, beside its main transcript `path`: `<sid>/subagents/*.jsonl`
    and a Workflow's agents in `<sid>/subagents/workflows/<run>/*.jsonl`
    (`runrecord`'s layout). None when any of it cannot be listed."""
    base = os.path.join(os.path.dirname(path), sid, "subagents")
    try:
        runs = [e.path for e in os.scandir(os.path.join(base, "workflows"))
                if e.is_dir()]
    except FileNotFoundError:
        runs = []
    except OSError:
        return None
    found = []
    for directory in [base] + runs:
        more = _touched_since(directory, floor)
        if more is None:
            return None
        found += more
    return found


def _other_model_in(path, moment, answer):
    """True when one transcript holds an assistant entry stamped within
    `_TURN_UNAMBIGUOUS_S` before `moment` that names a model other than
    `answer`, False when it holds none, None when the byte bound ends the
    walk first. Read backwards like the main file, stopping at the first
    line older than the window."""
    with open(path, "rb") as handle:
        for line in _lines_newest_first(handle):
            if line is _CUT:
                return None
            got = _turn_entry(line)
            epoch = got[0] if got else _newest_stamp(line)
            if epoch is None or epoch > moment:
                continue
            if moment - epoch > _TURN_UNAMBIGUOUS_S:
                return False
            if got and got[1] != answer:
                return True
    return False


def _answer(name):
    """The pattern check lands on the entry this reader answers with: an id it
    cannot recognise there is UNKNOWN, never the neighbouring readable one."""
    if not _RUNTIME_MODEL.fullmatch(name):
        return None, MODEL_UNKNOWN
    return name, MODEL_SELF_REPORTED


def _acted_in_seats_name(path, sid, moment, answer, sidechain):
    """True when ANOTHER model than `answer` may have made a read recorded at
    `moment` in the seat's name: a subagent's turn stamped within
    `_TURN_UNAMBIGUOUS_S` before it names one, in the main file's
    isSidechain lines (`sidechain`, the models the walk collected there) or
    in a subagent transcript touched in that window. Also True when that
    could not be settled: a transcript that cannot be listed or read, or a
    walk the byte bound ends first. Only files touched in the window are
    opened, each read backwards under the main file's bound."""
    if any(name != answer for name in sidechain):
        return True
    try:
        files = _subagent_transcripts(path, sid, moment - _TURN_UNAMBIGUOUS_S)
        return files is None or any(
            _other_model_in(f, moment, answer) is not False for f in files)
    except OSError:
        return True


def native_turn_model(session, at=None, root=None):
    """(model, source) a NATIVE seat was answering with at one moment.

    `at=None` is ROUTING: the newest model entry, even if the seat was on
    another model earlier. `at=<epoch or stamp>` is ADMISSION: the newest
    entry AT OR BEFORE the moment. It FAILS CLOSED to (None, MODEL_UNKNOWN)
    when that moment cannot be pinned to one id -- no entry within
    `_TURN_FRESHNESS_S` before it, two models named within
    `_TURN_UNAMBIGUOUS_S` before it, a moment that does not parse, or a walk
    the byte bound ends first. Entries are taken in the order the harness
    wrote them, newest first; the walk stops as soon as it has passed the
    answer's windows, so a recent moment costs one read of the tail.

    The answer is the SEAT's: the main transcript (the path
    `transcript_path` names) less its isSidechain lines. ADMISSION then
    fails closed as well when a subagent of the seat named another model
    within `_TURN_UNAMBIGUOUS_S` before the moment (`_acted_in_seats_name`),
    because a subagent records holds and verdicts in its seat's name.
    ROUTING never reads a subagent. ABSENT when there is no transcript or it
    names no model at all.
    """
    sid = str(session or "")
    if not sid:
        return None, MODEL_ABSENT
    moment, placed = _moment(at)
    if not placed:
        return None, MODEL_UNKNOWN
    path, err = turnresponse.transcript_path(sid, root)
    if err:
        # AMBIGUOUS OR UNUSABLE, never absent: two files carrying one session
        # id is a record this reader refuses to choose between.
        return None, MODEL_UNKNOWN
    if not path:
        return None, MODEL_ABSENT
    best, seen, sidechain = None, False, set()
    try:
        with open(path, "rb") as handle:
            for line in _lines_newest_first(handle):
                if line is _CUT:
                    return None, MODEL_UNKNOWN
                got = _turn_entry(line)
                own = got is not None and not got[2]
                if own and moment is None:
                    return _answer(got[1])
                seen = seen or own
                if moment is None:
                    continue
                # ANY stamped line places the walk, not only a model entry: a
                # seat idle for an hour before the moment wrote no entry in
                # it, and its other lines say so without a walk back to the
                # next entry. Such a line has no model (name None).
                epoch, name, side = got or (_newest_stamp(line), None, False)
                if epoch is None or epoch > moment:
                    # Unplaced, or written after the moment: the model the
                    # seat reached LATER, which a recorded read was never
                    # made on.
                    continue
                window = _TURN_FRESHNESS_S if best is None \
                    else _TURN_UNAMBIGUOUS_S
                if moment - epoch > window:
                    # No entry within 30 min before the moment is UNKNOWN;
                    # past the 10-min window of a found answer, it is settled.
                    if best is None:
                        return None, MODEL_UNKNOWN
                    break
                if name is None:
                    continue
                if side:
                    # A subagent's turn: never the answer, and judged
                    # against it only inside the ten-minute window.
                    if moment - epoch <= _TURN_UNAMBIGUOUS_S:
                        sidechain.add(name)
                    continue
                if best is None:
                    best = name
                elif name != best:
                    return None, MODEL_UNKNOWN
    except OSError:
        return None, MODEL_UNKNOWN
    if best is None:
        # Entries exist and none lies at or before the moment: a record this
        # reader could not reduce, never a seat with nothing to say.
        return None, MODEL_UNKNOWN if seen else MODEL_ABSENT
    model, source = _answer(best)
    if model is not None and \
            _acted_in_seats_name(path, sid, moment, best, sidechain):
        return None, MODEL_UNKNOWN
    return model, source


def evidence_turn_model(evidence, at=None):
    """The model a NATIVE claude seat's own transcript names at `at`, for one
    family-evidence record (`dispatches._approval_identity_family_evidence`),
    or None.

    ONLY a session-bound (v5) verified native claude runtime that stamps no
    model: the transcript is read on the session that record is bound to, so
    the model speaks for the same session the family does. The seat's helm
    instance directory is not the way in, because a native Claude seat has
    none (`seat._seat_family` answers "unknown seat" for one): the roster's
    SessionStart bind is what names its current session. A stamp is the
    caller's to read first and is never replaced here. A proxy route, a
    sessionless (v4) record and any other family answer None, unchanged."""
    if not isinstance(evidence, dict) or evidence.get("v") != 5:
        return None
    runtime = evidence.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("family") != "claude" \
            or runtime.get("backend") not in (None, "native") \
            or runtime.get("model"):
        return None
    model, _source = native_turn_model(evidence.get("session"), at)
    return model
