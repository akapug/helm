#!/usr/bin/env python3
"""The local findings pass: every review row gets one read on the owner's own
model (qwen27 unless a knob names another reader family), and the result
lands on the row as a NOTE for the approving reviewer to adjudicate.

THE OWNER'S CONTRACT (task/2960), which every choice below serves:

  * It runs on EVERY review row, and it is always on. `HELM_QWEN27_FINDINGS`
    set to `0`, `off` or `no` switches it off.
  * It is NEVER an approval, NEVER a gate, and NEVER the different-model read.
    The note is projected as `findings_notes` on the row, nothing that reads a
    verdict, an advisory read or a family reads that key, and the advisory-read
    door refuses the reader's model outright — qwen27 always, and the family
    the reader knob names (`dispatches._on_behalf_shape`).
  * An empty result is NOT a clean review, and the note says so.
  * If the reader is down or slow the row gets ONE line naming why, never a
    hold. Filing never waits for it and never fails because of it.
  * It costs no credential: the model runs on the owner's own box, and only
    a catalog pool row declared `rung: free` is ever sent a diff.

WHO OWNS WHAT. The script that does the reading, `local-review.py`, belongs to
another project on the operator's host and runs IN PLACE from its checkout:
`HELM_LOCAL_REVIEW_SCRIPT` names it, else `local-review-script` in this host's
local names (helm/localnames.py). Named nowhere, the pass notes why and reads
nothing. This module starts it, queues
it, bounds it and maps its answer to one note. The ledger event and its reducer live in
`helm/dispatches.py`, because that module owns every event kind on the
dispatch ledger.

DETACHED AND QUEUED. `queue` starts `python3 -m helm.findingspass <row id>` in
its own session, so the filing verb returns at once and the pass outlives it.
The worker then takes ONE flock under the helm home before it reads anything,
so one pass runs at a time on this host: the reader's server has four slots
shared with other users, and a second concurrent pass would only slow both.
Everything heavy is imported AFTER the lock, so a queued worker costs a
sleeping interpreter and nothing more.

THE READER IS A CATALOG FAMILY, NAMED BY A KNOB. `HELM_QWEN27_FINDINGS_READER`
names the family, and qwen27 reads when it names none. The endpoint AND the
model id both come off ONE pool row of that family's seat_catalog entry on
every run — the provider `pool_default` names, else the first one the entry
lists — the URL plus `/chat/completions`, the model its `upstream_model`. No
host and no model is written here, so a move of the serving stack is one
catalog edit, and a family the catalog lacks is one NOT RUN line with the
resolver's reason, never a crash and never a read of the wrong model.

THE ROW IS RE-READ BEFORE EACH READ (task/3382). The pass looked at its row
once, when it took the queue lock, and measured over 24 h 19 of 137 passes
went on reading a row that had meanwhile reached a verdict or been cancelled:
2,248 s of local GPU on the 14 the census matched. So the pass asks the
lock's question again when the script prints its header (before its first
read) and each time it prints a finished read: can this row still take the
note (`standing`)? A row that can never take one again stops the script
before its next read finishes: every status outside
`dispatches.FINDINGS_NOTE_STATES` (open, held), and a retirement, which no
later event undoes. The reads that finished are stored by reference, and one
`stopped: row reached <state> at <ts>` record under the pass's store names
them for triage (`stop_record`). A held row, a row a successor carries and a
row retipped to another tip are read to their end, as they always were: each
of those can come back, and the ledger takes a note on the first two. The
ledger is never written for a stop. An unreadable ledger is UNKNOWN, never an
ending: the pass goes on and its note says so.

A CHECK COSTS NO FOLD UNLESS THE LEDGER NAMED THE ROW (`RowSight`). The
ledger is append-only, so a check reads only the lines appended since its
last answer, and a line that does not name the row changes nothing. A line
that does is folded, from the checkpoint, in a process that can still name
its code; in one whose helm tree changed on disk it is UNKNOWN and no fold is
taken, because that fold would be a whole cold replay of the ledger.

NO QUEUE IS EVER LOST, AND A ROW AND TIP ARE READ ONCE. `queue` always
starts the worker, and the worker waits on the fleet's queue lock as every
pass always has. At that lock it reads the tip the row names THEN, never the
one it was queued at, so any worker that lives covers one that died before
its lock (task/3382 F16): a row retipped A to B whose worker at B was killed
is read at B by the worker queued at A. Every check that decides whether it
reads runs under that lock (`_run_locked`): a row that ended, and a row this
reader already read, complete or partial, at the tip it names, are each read
no further. So a second pass of a row and tip while the first reads waits,
and at its own lock finds the read the first one wrote and reads nothing; a
holder killed by any signal lets go through the kernel, so the waiter goes
on.

THE READ IS ASKED TO CONCLUDE. The pass names the read's output budget and
its thinking on every run, and never leaves them to the script's defaults:
see DEFAULT_MAX_TOKENS for the budgets and DEFAULT_TIMEOUT_S for the bound
that covers them.

Import-safe and stdlib-only.
"""
import collections
import fcntl
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import unicodedata

from . import home

#: The reader family when the knob names none: seat_catalog's entry of this
#: name.
READER = "qwen27"
#: The switch, the script path, the run's wall bound, the reader family, the
#: thinking switch and the read's two budgets. New knobs, so no legacy
#: spelling is read (docs/ENVIRONMENT.md). The prefix names the pass, so every
#: knob carries it whichever family reads.
SWITCH = "HELM_QWEN27_FINDINGS"
SCRIPT = "HELM_LOCAL_REVIEW_SCRIPT"
TIMEOUT = "HELM_QWEN27_FINDINGS_TIMEOUT_S"
READER_KNOB = "HELM_QWEN27_FINDINGS_READER"
THINK = "HELM_QWEN27_FINDINGS_THINK"
MAX_TOKENS = "HELM_QWEN27_FINDINGS_MAX_TOKENS"
THINK_MAX_TOKENS = "HELM_QWEN27_FINDINGS_THINK_MAX_TOKENS"
#: THE READ'S OUTPUT BUDGET, which is what lets a read conclude. The defaults
#: are the model makers' recommended output lengths (the Qwen model cards):
#: 16384 tokens with thinking off, 32768 with it on, because a thinking read
#: draws its reasoning from the SAME budget. The script's own default is
#: 4096, and on the stored outputs of 423 qwen27 reads at 4096, 98 ran to
#: exactly the cap while still reasoning (97 of them with no repeated-line
#: loop), and each such read made its note PARTIAL. The reads
#: that did conclude ran p50 993 and p99 3845 tokens: a read stops at its
#: answer, so a larger budget costs only the reads that need it. Both budgets
#: fit the served window beside the largest measured prompt: 14982 + 32768 =
#: 47750 tokens of qwen27's 131072.
DEFAULT_MAX_TOKENS = 16384
DEFAULT_THINK_MAX_TOKENS = 32768
#: THE WALL BOUND IS 3600 s PER 16384 TOKENS OF BUDGET, never less than
#: 3600 s. A read that runs to its cap takes time in proportion to the cap:
#: qwen27 decodes 74.6 tok/s at one stream, so a capped 16384-token read is
#: 220 s, and the largest measured run, 13 reads, is 13 x 220 = 2860 s, inside
#: 3600 even if every read ran to the cap. With thinking on, a capped
#: 32768-token read is 439 s and 13 of them are 5707 s, which 3600 would
#: stop; the scaled bound is 7200. The script's own per-request bound, 1800 s,
#: covers a 32768-token read down to 18.2 tok/s, and the slowest measured
#: read ran 4096 tokens in 220 s (18.6 tok/s). A run past its bound is
#: stopped and noted, never waited on.
DEFAULT_TIMEOUT_S = 3600
CHECKLIST = os.path.join("docs", "preread-checklist.md")
STORE = os.path.join(".state", "findings-pass")
LOCK = "queue.lock"
LOG = "worker.log"
#: One JSON record per row whose pass stopped, or finished after its row
#: ended (`stop_record`): the newest word the pass has on that row.
STOPS = "stopped"
_ID = re.compile(r"[0-9a-f]{8,64}")
#: A tip `examine` will read: the full hex name of a commit, 40 characters
#: (sha1) or 64 (sha256) and nothing between — no object has a name of 41 to
#: 63. The dispatches._FULL_TIP grammar, bound here because this module reads
#: dispatches only inside functions.
_FULL_TIP = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
#: How long a stopped or timed-out script's output is drained for once it
#: has been signalled: its group is dead, so only a straggler holding the
#: pipe could make this wait, and it is not waited on past this.
DRAIN_S = 10
#: How long the script's stdout must be quiet before a pass the re-read
#: ended stops it: long enough for the rest of a finished read's text to
#: arrive when a pipe read split it, and far shorter than any read.
QUIET_S = 0.25
LOG_CAP = 1 << 20
#: The script's exit code for each answer it gives (its calling contract).
EXIT_STATUS = {0: "complete", 2: "partial", 3: "unread"}
STATUS = re.compile(r"^LOCAL-REVIEW-STATUS (complete|partial|unread)"
                    r"((?: [a-z_]+=\d+)*)\s*$")
#: A kept finding, verbatim: the judge's REAL marker and the reader's text,
#: up to the next block the script writes. Nothing else in the output is a
#: finding (the script's own contract).
FINDING = re.compile(r"\*\*\[JUDGE: REAL \d+/\d+\]\*\*\n.*?"
                     r"(?=\n\*\*\[|\n<details>|\n## |\n---\n|\Z)", re.S)
_SECTION = re.compile(r"^## (.+)$", re.M)
#: The tail lines that name what a PARTIAL read left unread.
_PARTIAL_LINES = ("READER ERRORS", "EMPTY ANSWERS", "CUT AT --max-tokens",
                  "TRUNCATED, so")


def enabled():
    """The switch. On unless it says 0, off or no."""
    return str(os.environ.get(SWITCH, "1")).strip().lower() \
        not in ("0", "off", "no")


def store_dir():
    return os.path.join(home.global_dir(), STORE)


def log_path():
    return os.path.join(store_dir(), LOG)


def script_path():
    """The reading script's path, or "" when nothing names one."""
    from . import localnames
    named = os.environ.get(SCRIPT) or localnames.value("local-review-script")
    return os.path.expanduser(named) if named else ""


def checklist_path():
    return os.path.join(_package_root(), CHECKLIST)


def _positive(knob, default):
    """A knob's positive integer, or `default` when it names none. A value
    that is not a positive integer is ignored and the default stands."""
    raw = str(os.environ.get(knob) or "").strip()
    return int(raw) if raw.isdecimal() and int(raw) > 0 else default


def thinking():
    """The thinking switch. Off unless it says 1, on, yes or true."""
    return str(os.environ.get(THINK) or "").strip().lower() \
        in ("1", "on", "yes", "true")


def max_tokens():
    """The read's output budget in tokens: the thinking budget when thinking
    is on, else the plain one, each from its own knob."""
    return (_positive(THINK_MAX_TOKENS, DEFAULT_THINK_MAX_TOKENS) if thinking()
            else _positive(MAX_TOKENS, DEFAULT_MAX_TOKENS))


def timeout_s():
    """The run's wall bound in seconds: the knob's, else 3600 s per 16384
    tokens of the read's budget and never less than 3600 s
    (DEFAULT_TIMEOUT_S says why)."""
    return _positive(TIMEOUT, max(DEFAULT_TIMEOUT_S, DEFAULT_TIMEOUT_S
                                  * max_tokens() // DEFAULT_MAX_TOKENS))


def reader():
    """The reader family's catalog name: the knob's, else qwen27. A value
    that is not a family name is ignored and qwen27 reads; a family name the
    catalog lacks is the resolver's to refuse (`endpoint`), so it is one NOT
    RUN line that names it."""
    from . import dispatches
    name = str(os.environ.get(READER_KNOB) or "").strip().lower()
    return name if dispatches.FINDINGS_READER_NAME.fullmatch(name) else READER


def _package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def endpoint(table=None, name=None):
    """(url, model, None) or (None, None, why) — the reader's chat endpoint
    and the model id to ask it for, both off ONE pool row of the catalog's
    entry for the reader family (`name`, else `reader()`). The provider is
    the one `pool_default` names, else the first the entry lists: the rule
    `helm seat add` already uses. The model is that row's `upstream_model`,
    else the entry's own `model`; a row naming neither is not a provider.

    ONLY A ROW DECLARED `rung: free` IS A PROVIDER. The script posts the
    row's diff to the endpoint with no key, so a vendor's metered row (rung
    `paid`), or a row that declares no rung, is never sent one: the pass
    reads only on the owner's own models, whichever family the knob names.

    EACH ROW IS RESOLVED BY `seat_catalog.pool_base_url`, never by reading
    `base_url` here. A row on the operator's own box carries `base_url_from`
    and no host: its URL lives in the helm home's endpoints file. Reading the
    key directly saw no provider at all for such a row, so every note read
    NOT RUN while the endpoint was configured. An unconfigured endpoint is
    still NOT RUN, with the resolver's own reason, which names the file."""
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    families = seat_catalog.FAMILIES if table is None else table
    name = name or reader()
    fam = families.get(name) if isinstance(families, dict) else None
    if not isinstance(fam, dict):
        return None, None, "seat_catalog has no %s entry" % name
    pool = fam.get("pool_providers")
    rows, whys = [], []
    for key, row in (pool.items() if isinstance(pool, dict) else ()):
        if not isinstance(row, dict):
            continue
        url, why = seat_catalog.pool_base_url(row)
        model = str(row.get("upstream_model") or fam.get("model") or "").strip()
        if not url.startswith(("http://", "https://")):
            if why:
                whys.append("%s: %s" % (key, why))
        elif not model:
            whys.append("%s: the row names no model" % key)
        elif row.get("rung") != "free":
            whys.append("%s: its rung is %s, and the pass sends a diff only "
                        "to the owner's own model (rung free)"
                        % (key, row.get("rung") or "undeclared"))
        else:
            rows.append((key, url, model))
    if not rows:
        return None, None, ("seat_catalog's %s entry resolves no pool provider "
                            "to an http endpoint%s" % (name, "".join(
                                " — " + w for w in whys)))
    url, model = next(((u, m) for key, u, m in rows
                       if key == fam.get("pool_default")), rows[0][1:])
    return url + "/chat/completions", model, None


# ---------------------------------------------------------------- queue

#: THE WORKER IS NOBODY'S CHILD. A shell in its own session backgrounds the
#: worker and exits at once, and the filing process reaps that shell: so a
#: long-lived caller collects no zombie, and the worker is adopted by init
#: rather than left to a parent that may exit or signal its own group.
WORKER_ARGV = ["/bin/sh", "-c", 'exec "$@" &', "helm-findings-pass"]


def queue(row, popen=None):
    """(started, why) — start the pass for a review row, DETACHED.

    (True, None) started; (False, None) nothing to do — the switch is off or
    the row is not a review; (False, why) it could not be started, and the
    caller records that on the row."""
    if not enabled() or not isinstance(row, dict) \
            or row.get("kind") != "review":
        return False, None
    rid = str(row.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", rid):
        return False, "the row carries no usable id"
    root = _package_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (root, env.get("PYTHONPATH")) if p)
    try:
        os.makedirs(store_dir(), exist_ok=True)
        with open(os.devnull, "rb") as null, open(log_path(), "ab") as sink:
            shell = (popen or subprocess.Popen)(
                WORKER_ARGV + [sys.executable, "-m", "helm.findingspass",
                               rid],
                stdin=null, stdout=sink, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, cwd=root, env=env)
            shell.wait(timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, None


def _lock():
    """The fleet's one-at-a-time queue: an exclusive flock, held until the
    returned descriptor is closed. Blocks while another pass runs."""
    os.makedirs(store_dir(), exist_ok=True)
    fd = os.open(os.path.join(store_dir(), LOCK),
                 os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd


# ---------------------------------------------------------------- the pass


def run(rid, timeout=None):
    """(row, why) — one pass on one row, under the queue lock. The row comes
    back when a note was recorded; `why` says what happened otherwise."""
    fd = _lock()
    try:
        return _run_locked(rid, timeout)
    finally:
        os.close(fd)


def _run_locked(rid, timeout):
    """`run`'s body, under the queue lock. It reads the tip the row names
    now, whatever tip the row named when the pass was queued (task/3382
    F16), and every record it writes names that tip."""
    from . import dispatches
    # THE MARK IS TAKEN BEFORE THE FOLD it stands on, so a line that lands
    # while that fold reads the ledger is read again at the first check.
    sight = RowSight()
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err
    rid = str(row.get("id") or rid)
    tip = str(row.get("tip") or "")
    # THE LOCK-TIME CHECK IS THE ONE EVERY LATER CHECK ASKS (`standing`):
    # open or held, the resolver above having refused a retired row. It never
    # raises: a row the classifier cannot read is UNKNOWN and the pass goes
    # on (task/3382 F5).
    now = sight.settle(rid, row)
    if now.answer == ENDED:
        return None, ("row %s is %s by now: nobody is left to adjudicate a "
                      "note, so the pass is skipped" % (rid[:12], now.word))
    # A READ IS ONE READER'S: a row another family read is still owed this
    # reader's read of the same tip.
    name = reader()
    if any(n.get("reviewed_tip") == tip and n.get("reader") == name
           and n.get("outcome") in ("complete", "partial")
           for n in row.get("findings_notes") or () if isinstance(n, dict)):
        return None, "row %s already carries a read of %s by %s" % (
            rid[:12], tip[:12], name)
    seen = len(row.get("findings_notes") or ())
    # `row_now` is looked up at each call, never bound here: the re-read is
    # the module's seam, and a caller that wraps it wraps every check.
    bound = timeout or timeout_s()
    fields, output = examine(tip, row.get("repo_root"), bound,
                             recheck=lambda: row_now(sight))
    if fields.get("outcome") == STOPPED:
        stopped = fields["standing"]
        counts = {k: fields[k] for k in ("reads", "errors", "kept", "checks",
                                         "unknown") if k in fields}
        return None, _record_stop(rid, tip, name, stopped,
                                  _stop_line(stopped, counts), output, counts,
                                  seen)
    if now.answer == UNKNOWN:
        fields["reason"] = "; ".join(p for p in (
            fields.get("reason"),
            "the row's state could not be read when the pass took its lock "
            "(%s); the pass went on" % _one_line(now.why)) if p)
    if output:
        ref, nbytes, why = dispatches.write_brief_file(output)
        if why:
            fields["reason"] = "; ".join(
                p for p in (fields.get("reason"),
                            "the whole output was NOT stored: " + why) if p)
        else:
            fields.update(output_ref=ref, output_bytes=nbytes)
    out, err = dispatches.record_findings_note(rid, tip, fields)
    if out is not None or not output:
        return out, err
    # THE READS FINISHED AND THE ROW ENDED BEFORE THEIR NOTE COULD LAND. The
    # ledger refuses the note, as it always has; the output it would have
    # named is already stored, so the pass names it where triage reads. The
    # re-read is the checks' own, so it folds only when the ledger named the
    # row, and never in a process whose tree changed: there the ledger's own
    # refusal is the word the record keeps.
    late = sight.now()
    counts = _late_counts(fields, output)
    how = "%s (%s)" % (_ran(fields, bound), _finished(counts))
    # A RETIPPED ROW TAKES A NOTE OF ITS OWN TIP ONLY (task/3382 F12): the
    # ledger refuses this pass's note with FINDINGS_OTHER_TIP, and that
    # refusal IS the retip, so the record names it, and the reads that
    # finished, even where the re-read cannot fold the row. A held or
    # carried row takes a note, so any other refusal there is the ledger's
    # own, and the line never says the row's state stopped it.
    if dispatches.FINDINGS_OTHER_TIP in str(err or ""):
        late = Standing(ENDED, "retipped", _retipped_at(sight, late, tip),
                        "the ledger refused the note as another tip's")
    if late.answer == ENDED:
        line = ("%s, but the row reached %s at %s before its note could land"
                % (how, late.word, late.at or _seen_at())
                + (" (%s)" % _one_line(late.why) if late.why else ""))
    elif dispatches.FINDINGS_NOT_OWED in str(err or ""):
        line = "%s, but the ledger refused its note: %s" % (how,
                                                             _one_line(err))
        late = Standing(UNKNOWN, None, None, late.why)
    else:
        return out, err
    return None, "%s; %s" % (err, _record_stop(
        rid, tip, name, late, line, output, counts, seen, kind=FINISHED))


def _retipped_at(sight, late, tip):
    """When the row was retipped off `tip`: its last retip's time, read off
    the row the sight folded, or None when that row is not the row now (the
    re-read answered UNKNOWN) or still names `tip`."""
    row = sight.row if late.answer != UNKNOWN else None
    if not isinstance(row, dict) or str(row.get("tip") or "") == tip:
        return None
    hops = [h for h in row.get("retips") or () if isinstance(h, dict)]
    return hops[-1].get("ts") if hops else None


def _stop_line(now, counts):
    """The line a stop between reads leaves: which state ended the row, and
    what finished before it did."""
    return "%s, after %s" % (ended_line(now), _finished(counts))


def _late_counts(fields, output):
    """The reads a pass that ran on past its checks finished: the script's
    own counts when it answered, else the finished reads its stored output
    holds (a timed-out or failed run gave no count of its own)."""
    if fields.get("outcome") in EXIT_STATUS.values():
        return {"reads": fields.get("reads") or 0,
                "kept": fields.get("kept") or 0}
    return _counts(output)


def _ran(fields, bound):
    """How the script ended, in the words a late record uses."""
    outcome = fields.get("outcome")
    if outcome in EXIT_STATUS.values():
        return "the pass ran to its end"
    if outcome == "timeout":
        return "the pass ran past its %s s bound and was stopped" % bound
    return "the pass ended %s" % (outcome or "without an answer")


def _finished(counts):
    """How many reads finished, in the words a stop line uses."""
    reads = counts.get("reads") or 0
    kept = counts.get("kept") or 0
    return "%d read(s) finished%s" % (
        reads, ", kept whole with %d finding(s)" % kept if kept
        else (", kept whole" if reads else ""))


# ---------------------------------------------------------------- the re-read

#: What one re-read of the row answers. OWED: the row can still take the
#: read's note. ENDED: it never can again. UNKNOWN: the ledger or the row
#: could not be read, which is NEVER ENDED: a pass that stopped on a blind
#: read would drop a row that may well still be owed.
OWED, ENDED, UNKNOWN = "owed", "ended", "unknown"
Standing = collections.namedtuple("Standing", "answer word at why")
#: The pass's own outcome for a script it stopped. It never reaches the
#: ledger: a stop is recorded under the pass's store (`stop_record`).
STOPPED = "stopped"
#: A stop record's other kind: every read finished, and the row ended before
#: the note could land.
FINISHED = "finished"
#: The time each way of ending records on the row, the latest act first: a
#: retirement or a withdrawal is written AFTER the verdict it ends.
_ENDED_AT = ("retire_ts", "withdraw_ts", "discharge_ts", "close_ts",
             "verdict_ts")


def standing(row):
    """Standing(answer, word, at, why): can this folded row still take the
    pass's note?

    ONE PREDICATE, ASKED AT THE LOCK AND BEFORE EVERY READ (task/3382): a
    row is owed while its status is one `dispatches.FINDINGS_NOTE_STATES`
    names (open or held) and it is not retired, which is the row
    `record_findings_note` takes a note on. Every other row has ENDED, for
    good: the reducer's one move back to open is the release of a HELD row,
    and a retirement freezes a row whatever is appended after it. The ended
    set is derived from the owed one, never listed beside it, so a status
    the reducer learns later ends a pass here and skips it at the lock
    alike. Its word is the one `closed_state` gives. A held row, a row a
    successor carries and a row retipped to another tip are OWED: each of
    those can come back, and the ledger takes a note on the first two. A
    row this helm cannot read in full is UNKNOWN: its status stopped at an
    event this binary has no arm for."""
    from . import dispatches
    if not isinstance(row, dict):
        return Standing(UNKNOWN, None, None, "the row could not be read")
    kinds = dispatches.unknown_event_kinds(row)
    if kinds:
        return Standing(UNKNOWN, None, None, "the row carries event kinds this "
                        "helm cannot read (%s)" % ", ".join(kinds))
    if row.get("status") in dispatches.FINDINGS_NOTE_STATES \
            and not dispatches._retired_admin_by(row):
        return Standing(OWED, None, None, None)
    word = dispatches.closed_state(row)
    # a cancel records no time of its own, and an advisory close is a
    # cancel over a verdict: its verdict's time is not the cancel's
    at = None if word == "cancelled" else next(
        (row[k] for k in _ENDED_AT if row.get(k)), None)
    return Standing(ENDED, word, at, None)


#: Why a check whose ledger named the row answers UNKNOWN without a fold.
DRIFTED = ("the helm tree changed on disk under this pass, so its fold has no "
           "checkpoint and a re-read would replay the whole ledger cold; no "
           "fold was taken")


class RowSight(object):
    """THE PASS'S RE-READ OF ITS ROW, AND WHAT A CHECK COSTS (task/3382 F1).

    EVERY CHECK WAS A WHOLE `dispatches.snapshot()`. Served from the fold
    checkpoint that is cheap, but a pass runs for up to an hour or two, and
    once any helm/*.py changes on disk under it this process can no longer
    name its code (`foldckpt.policy()` answers None for the rest of the
    process), so every snapshot after that is a COLD fold: 95-140 s and
    about 1,460 git spawns on the live ledger, about 27 of them on the
    largest pass where main took about 2, each landing its stop a fold late.

    THE LEDGER IS APPEND-ONLY, AND A ROW MOVES ONLY BY A LINE THAT NAMES IT.
    A fold writes each event only to the row it names, and whether a row can
    still take a note (`standing`) is its own status and retirement, so it
    changes only by a line naming the row itself (a verdict, cancel, close,
    retirement, hold, release or retip). A check therefore reads only the
    complete lines appended since its last answer (`eventledger.tail_since`)
    and looks for the row's id, quoted, as the ledger writes it. Not there:
    the last answer stands and nothing is folded. There: the row is folded
    again, from the checkpoint, in a process that can still name its code;
    in one whose tree changed that fold would be the cold replay, so the
    answer is UNKNOWN and none is taken, which is what main did: the pass
    goes on, and the ledger refuses a note on a row that ended."""

    def __init__(self):
        from . import dispatches, eventledger
        self.path = dispatches.ledger_path()
        #: The row's id, and the row the last answer stands on.
        self.rid = self.row = None
        #: The last whole answer, or None when there is none to trust.
        self.answer = None
        _lines, self.mark, _why = eventledger.tail_since(self.path)

    def settle(self, rid, row):
        """The answer from a fold in hand, kept as the one later checks stand
        on. Never raises: a classifier that fails is UNKNOWN, and no answer
        is kept, so the next check asks again."""
        self.rid, self.row = rid, row
        try:
            now = standing(row)
        except Exception as exc:              # noqa: BLE001 — unread is UNKNOWN
            self.answer = None
            return Standing(UNKNOWN, None, None,
                            "%s: %s" % (type(exc).__name__, exc))
        self.answer = now
        return now

    def now(self):
        """Standing: the row now, folded only when the ledger named it."""
        from . import eventledger, foldckpt
        lines, mark, why = eventledger.tail_since(self.path, self.mark)
        if why:
            return Standing(UNKNOWN, None, None,
                            "dispatch ledger unavailable: %s" % why)
        self.mark = mark
        if lines is None or ('"%s"' % self.rid).encode("ascii") in lines:
            self.answer = None
        if self.answer is not None:
            return self.answer
        if foldckpt.policy() is None:
            return Standing(UNKNOWN, None, None, DRIFTED)
        return self._whole()

    def _whole(self):
        """The row folded again, the mark moved to before that fold."""
        from . import dispatches, eventledger
        _lines, self.mark, why = eventledger.tail_since(self.path)
        if why:
            return Standing(UNKNOWN, None, None,
                            "dispatch ledger unavailable: %s" % why)
        try:
            current, unavailable = dispatches.snapshot()
            if unavailable:
                return Standing(UNKNOWN, None, None,
                                "dispatch ledger unavailable: %s" % unavailable)
            row, err = dispatches._resolve_row(current, self.rid,
                                               allow_retired=True,
                                               allow_unknown_kinds=True)
        except Exception as exc:                   # noqa: BLE001 — unread is UNKNOWN
            return Standing(UNKNOWN, None, None,
                            "%s: %s" % (type(exc).__name__, exc))
        if err:
            return Standing(UNKNOWN, None, None, err)
        return self.settle(self.rid, row)


def row_now(sight):
    """Standing: the check the pass makes before each read (`examine` calls
    it through `recheck`), through the pass's `RowSight`."""
    return sight.now()


def _seen_at():
    from . import pk
    return "%s (when the pass saw it; the row records no time)" % pk.now_ts()


def ended_line(now):
    """`stopped: row reached <state> at <ts>` and, when there is one, why."""
    line = "stopped: row reached %s at %s" % (now.word, now.at or _seen_at())
    return line + (" (%s)" % _one_line(now.why) if now.why else "")


def _one_line(text, cap=160):
    line = " ".join(_printable(str(text or "")).split())
    return line if len(line) <= cap else line[:cap - 6] + " [cut]"


# ---------------------------------------------------------------- the stops


def stop_path(rid):
    return os.path.join(store_dir(), STOPS, "%s.json" % rid)


def _record_stop(rid, tip, name, now, line, output, counts, seen,
                 kind=STOPPED):
    """Write the pass's word on a row it stopped on (or finished after the
    row ended): one JSON record per row, the newest replacing the last, with
    the reads that finished stored by reference. Returns the line, with a
    clause saying so when the record or the output could not be written:
    the worker log still has it."""
    from . import dispatches, pk
    record = {"v": 1, "kind": kind, "id": rid, "reader": name,
              "reviewed_tip": tip, "ts": pk.now_ts(), "state": now.word,
              "state_at": now.at, "line": line, "notes_seen": seen}
    record.update(counts)
    problems = []
    if output:
        ref, nbytes, why = dispatches.write_brief_file(output)
        if why:
            problems.append("the reads that finished were NOT stored: " + why)
            record["output_problem"] = why
        else:
            record.update(output_ref=ref, output_bytes=nbytes)
    if not _ID.fullmatch(str(rid or "")):
        problems.append("the row id cannot name a stop record")
    else:
        try:
            pk.atomic_write(stop_path(rid), json.dumps(record, sort_keys=True)
                            + "\n", mode=0o600)
        except OSError as exc:
            problems.append("the stop record was NOT written: %s" % exc)
    return "; ".join([line] + problems)


def stop_record(rid):
    """The pass's stop record for this row, or None when it has none or the
    one on disk is not a record this module wrote."""
    rid = str(rid or "")
    if not _ID.fullmatch(rid):
        return None
    try:
        with open(stop_path(rid), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("id") != rid \
            or record.get("kind") not in (STOPPED, FINISHED) \
            or not isinstance(record.get("line"), str) \
            or type(record.get("notes_seen")) is not int:
        return None
    return record


def _stop_speaks(stop, notes):
    """Is the stop the newest word on the row? Decided by the ledger's own
    order, never the clock: a stop and a note written in one second are
    common, and the record counts the notes the row carried when it was
    written."""
    return bool(stop) and len(notes) <= stop["notes_seen"]


def _stop_lines(stop, row, notes):
    """The lines a reader sees for a row whose newest word is a stop."""
    from . import dispatches
    tip = str(stop.get("reviewed_tip") or "")
    lines = ["findings pass (%s, tip %s): %s: %s. Not a review."
             % (stop.get("ts") or "?", tip[:12], stop.get("reader") or READER,
                _printable(stop["line"]))]
    if tip and tip != str(row.get("tip") or ""):
        lines.append("  this note read an EARLIER tip; the row now names %s"
                     % str(row.get("tip") or "?")[:12])
    ref = stop.get("output_ref")
    if ref:
        _text, problem = dispatches.read_brief_file(ref,
                                                    stop.get("output_bytes"))
        path = dispatches.brief_file_path(ref)
        lines.append("  whole output of the reads that finished: %s (%s bytes)"
                     % (path, stop.get("output_bytes")) if not problem else
                     "  whole output UNAVAILABLE at %s — it is missing or is "
                     "not the text this stop recorded" % path)
    if notes:
        lines.append("  (%d earlier note%s on this row)"
                     % (len(notes), "" if len(notes) == 1 else "s"))
    return lines


def examine(tip, repo, timeout=None, recheck=None):
    """({note field: value}, output text) — run the script once and map its
    answer. Every way it can fail is a note with a reason, never an
    exception and never a silence.

    `recheck`, when given, is asked before each read (`_Watch`): an ENDED
    answer stops the script and the fields say STOPPED, with the reads that
    finished counted and their output returned whole."""
    started = time.monotonic()
    bound = timeout or timeout_s()
    name = reader()

    def done(fields, output=""):
        fields["reader"] = name
        fields["wall_s"] = int(round(time.monotonic() - started))
        return fields, output

    script = script_path()
    if not script:
        return done(not_run("no local-review script is named: set %s or "
                            "`local-review-script` in the helm home's local "
                            "names" % SCRIPT))
    if not os.path.isfile(script):
        return done(not_run("the local-review script is missing at %s"
                            % script))
    url, model, why = endpoint(name=name)
    if why:
        return done(not_run("no %s endpoint: %s" % (name, why)))
    checklist = checklist_path()
    if not os.path.isfile(checklist):
        return done(not_run("the checklist is missing at %s" % checklist))
    if not _FULL_TIP.fullmatch(str(tip or "")):
        return done(not_run("the row names no full tip to read"))
    if not repo or not os.path.isdir(repo):
        return done(not_run("the row's checkout %s is not readable here"
                            % (repo or "(none)")))
    # THE SCRIPT WRITES --out LAST, after every read, so a directory that is
    # not there costs the whole run: measured against the real script, it
    # read all five files and then died writing its output with exit 1.
    try:
        os.makedirs(store_dir(), exist_ok=True)
    except OSError as exc:
        return done(not_run("the pass's store %s is not writable: %s"
                            % (store_dir(), exc)))
    out = os.path.join(store_dir(), "run-%d.md" % os.getpid())
    # THE JUDGE ASKS THE READER'S ENDPOINT (the script's --judge-endpoint
    # defaults to --endpoint), so it names the model that endpoint serves.
    argv = [sys.executable, script, tip, "--judge", "--repo", repo,
            "--checklist", checklist, "--endpoint", url, "--model", model,
            "--judge-model", model, "--max-tokens", str(max_tokens()),
            "--out", out] + (["--think"] if thinking() else [])
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as exc:
        return done(not_run("local-review.py could not be started: %s" % exc))
    watch = _Watch(recheck)
    stdout, stderr, timed_out = _collect(proc, started + bound, watch)
    text = _decode(stdout)
    written = _read_and_remove(out)
    if watch.ended is not None and not timed_out \
            and not _answered(proc.returncode, text):
        return done(watch.fields(text), text)
    if written and not timed_out:
        text = written
    fields = classify(proc.returncode, text, _decode(stderr), timed_out,
                      bound)
    if watch.unknown:
        fields["reason"] = "; ".join(
            p for p in (fields.get("reason"), watch.unknown_line()) if p)
    return done(fields, text)


def _answered(rc, text):
    """Did the script give its own answer? A stop that raced the script's
    last line leaves a whole run behind, and that run is classified as
    one, never as a stop."""
    return rc in EXIT_STATUS and parse_status(text)[0] is not None


#: The script's tail begins with its MEASURED line and ends with its status
#: line: once either is out, every read is over and no check is owed.
_TAIL = re.compile(rb"^(?:MEASURED: |LOCAL-REVIEW-STATUS )", re.M)
#: One finished read, and one failed read, as the script prints them.
_READ_DONE = re.compile(r"^`prompt \d+ tok, ", re.M)
_READ_ERROR = re.compile(r"^READER ERROR: ", re.M)


def _counts(text):
    """The reads a stretch of the script's output finished, failed and kept."""
    text = str(text or "")
    return {"reads": len(_READ_DONE.findall(text)),
            "errors": len(_READ_ERROR.findall(text)),
            "kept": len(FINDING.findall(text))}


class _Watch(object):
    """THE RE-READ BETWEEN READS. The script prints its header before its
    first read and each read whole, flushed, as it finishes (its own
    `emit`), so each complete write to its stdout before the tail is a point
    where one read is over and the next has not finished: the pass asks
    `recheck` there. The script is not paused while the ledger is read; a
    stop signals it within one re-read of the boundary, before the read it
    has begun can finish."""

    def __init__(self, recheck):
        self.recheck = recheck
        self.data = b""
        self.checks = 0
        self.unknown = []
        self.ended = None
        self.over = False

    def feed(self, chunk):
        """True when this output ends the pass: the row can take no note."""
        self.data += chunk
        if self.recheck is None or self.ended is not None or self.over:
            return False
        if _TAIL.search(self.data, max(0, len(self.data) - len(chunk) - 64)):
            self.over = True
            return False
        if not self.data.endswith(b"\n"):
            return False
        self.checks += 1
        try:
            now = self.recheck()
        except Exception as exc:              # noqa: BLE001 — unread is UNKNOWN
            now = Standing(UNKNOWN, None, None,
                           "%s: %s" % (type(exc).__name__, exc))
        if now.answer == ENDED:
            self.ended = now
            return True
        if now.answer != OWED:
            self.unknown.append(now.why or "no reason was given")
        return False

    def fields(self, text):
        """The STOPPED answer: which state ended the row, and what finished
        before it did."""
        return dict(_counts(text), outcome=STOPPED, standing=self.ended,
                    checks=self.checks, unknown=len(self.unknown))

    def unknown_line(self):
        return ("the row's state could not be re-read at %d of %d checks "
                "(%s); the pass went on, because a ledger that cannot be "
                "read is not a row that is no longer owed"
                % (len(self.unknown), self.checks, self.unknown[0]))


def _collect(proc, deadline, watch):
    """(stdout, stderr, timed_out) — both pipes read as the script writes
    them, each stdout chunk fed to `watch`. Past `deadline` the script's
    group is stopped at once. Once `watch` ends the pass, the script is
    stopped when its stdout has been QUIET for QUIET_S: a read whose output
    was still arriving when the check ran has FINISHED, and stopping in the
    middle of its text would drop part of a finished read. Either way, what
    it already wrote is drained for at most DRAIN_S more."""
    chunks = {proc.stdout: [], proc.stderr: []}
    timed_out = stopping = False
    quiet = None
    with selectors.DefaultSelector() as sel:
        for pipe in chunks:
            sel.register(pipe, selectors.EVENT_READ)
        while sel.get_map():
            now = time.monotonic()
            if not stopping and (now >= deadline
                                 or (quiet is not None and now >= quiet)):
                timed_out = now >= deadline and quiet is None
                stopping = True
                _stop(proc)
                deadline = time.monotonic() + DRAIN_S
                continue
            if now >= deadline:
                break
            wake = deadline if quiet is None or stopping \
                else min(deadline, quiet)
            for key, _mask in sel.select(timeout=max(0.0, wake - now)):
                data = os.read(key.fd, 65536)
                if not data:
                    sel.unregister(key.fileobj)
                    continue
                chunks[key.fileobj].append(data)
                if key.fileobj is not proc.stdout or stopping:
                    continue
                if watch.feed(data) or quiet is not None:
                    quiet = time.monotonic() + QUIET_S
    for pipe in chunks:
        pipe.close()
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()) or 0.1)
    except subprocess.TimeoutExpired:
        timed_out = timed_out or not stopping
        _stop(proc)
    return b"".join(chunks[proc.stdout]), b"".join(chunks[proc.stderr]), \
        timed_out


def not_run(reason):
    return {"outcome": "not-run", "reason": reason}


def classify(rc, text, stderr="", timed_out=False, bound=None):
    """{note field: value} for one run of the script.

    FAIL-CLOSED, and this is the function the owner's "an empty result is not
    a clean review" rests on: `complete` is returned only when the exit code
    is 0 AND the last status line says complete AND it carries both counts.
    Any disagreement between the exit code and the status line is `failed`,
    never the more reassuring of the two."""
    if timed_out:
        return {"outcome": "timeout",
                "reason": "the pass ran past its %s s bound and was stopped"
                % bound}
    if rc == 1:
        return {"outcome": "not-run", "rc": 1,
                "reason": "local-review.py could not start: %s"
                % (_last_line(stderr) or "it printed no reason")}
    if rc not in EXIT_STATUS:
        return {"outcome": "failed", "rc": rc if isinstance(rc, int) else -1,
                "reason": "local-review.py exited %s, outside its 0/1/2/3 "
                "contract%s" % (rc, (": " + _last_line(stderr))
                                if _last_line(stderr) else "")}
    status, counts, line = parse_status(text)
    if status != EXIT_STATUS[rc] or "reads" not in counts \
            or "kept" not in counts:
        return {"outcome": "failed", "rc": rc,
                "reason": "exit %d says %s, but the status line %s — the "
                "result cannot be read" % (
                    rc, EXIT_STATUS[rc],
                    "is missing" if status is None
                    else "says %s" % status if status != EXIT_STATUS[rc]
                    else "lacks its reads or kept count")}
    fields = {"outcome": status, "rc": rc, "status_line": line,
              "reads": counts["reads"], "kept": counts["kept"]}
    if status == "partial":
        fields["reason"] = partial_reason(counts, text)
    elif status == "unread":
        fields["reason"] = unread_reason(text)
    findings = extract_findings(text)
    if findings:
        fields["findings"] = findings
    return fields


def parse_status(text):
    """(status, {name: count}, line) from the LAST status line, else
    (None, {}, None). The script promises it is the last line; reading the
    last one means a quoted status line inside a finding cannot win."""
    for raw in reversed(str(text or "").splitlines()):
        match = STATUS.match(raw.strip())
        if match:
            counts = {k: int(v) for k, v in
                      re.findall(r"([a-z_]+)=(\d+)", match.group(2))}
            return match.group(1), counts, raw.strip()
    return None, {}, None


def partial_reason(counts, text):
    """What a PARTIAL read left unread: the non-zero counts, then the lines
    the script wrote naming the files."""
    named = [line.strip() for line in str(text or "").splitlines()
             if line.strip().startswith(_PARTIAL_LINES)]
    parts = ["%s=%d" % (k, counts[k]) for k in
             ("errors", "empty", "cut", "truncated") if counts.get(k)]
    return "; ".join(p for p in [" ".join(parts)] + named if p) \
        or "the script called it partial and named nothing"


def unread_reason(text):
    """Why nothing was read: the first reader error the script printed, else
    its own no-read line."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    for line in lines:
        if line.startswith("READER ERROR:"):
            return line
    for line in lines:
        if line.startswith("NO FILE WAS READ"):
            return line
    return "no read succeeded and the script named no error"


def extract_findings(text, cap=None):
    """The kept findings, verbatim, each under the section header it was
    written in, bounded to what a ledger row may carry. A cut is MARKED with
    the size it came from; the whole output is stored by reference beside
    the note. Control characters other than newline and tab are replaced,
    so a model's answer cannot carry a terminal escape onto a screen."""
    from . import dispatches
    cap = cap or dispatches.FINDINGS_TEXT_CAP
    text = str(text or "")
    blocks = []
    for match in FINDING.finditer(text):
        heads = _SECTION.findall(text, 0, match.start())
        head = ("## %s\n" % heads[-1].strip()) if heads else ""
        blocks.append(head + match.group(0).strip())
    if not blocks:
        return None
    whole = _printable("\n\n".join(blocks))
    if len(whole) <= cap:
        return whole
    mark = ("\n[... cut: %d of %d characters shown; the whole output is "
            "stored by reference on this note]")
    room = cap - len(mark % (cap, len(whole))) - 8
    return whole[:room] + mark % (room, len(whole))


def _printable(text):
    return "".join(c if c in "\n\t" or unicodedata.category(c) not in
                   ("Cc", "Cf", "Zl", "Zp") else "?" for c in text)


def _last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines()
             if line.strip()]
    return lines[-1] if lines else ""


def _decode(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data or "")


def _read_and_remove(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    try:
        os.remove(path)
    except OSError:
        pass
    return text


def _stop(proc):
    """Stop the script and everything it started: TERM to its group, then
    KILL if it has not gone within five seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------- the note


def summary(note):
    """The ONE line a reader sees first. "No findings" appears only for a
    complete read that kept nothing, and it always says what it is not."""
    reader = note.get("reader") or READER
    outcome = note.get("outcome")
    reads, kept = note.get("reads"), note.get("kept")
    reason = note.get("reason") or "no reason was recorded"
    if outcome == "complete" and kept == 0:
        return ("%s: no findings (complete, %s reads). Not a review, not an "
                "approval." % (reader, reads))
    if outcome == "complete":
        return ("%s: %s finding(s) for the approving reviewer to adjudicate "
                "(complete, %s reads). Not a review, not an approval."
                % (reader, kept, reads))
    if outcome == "partial":
        return ("%s: PARTIAL read, not clean — %s. %s. Not a review, not an "
                "approval." % (reader, reason,
                               "%s finding(s) kept from what was read" % kept
                               if kept else "Nothing was kept from what WAS "
                               "read, which says nothing about the rest"))
    if outcome == "unread":
        return ("%s: ABSENT review (reader down or erroring), not clean — %s."
                % (reader, reason))
    if outcome == "timeout":
        return "%s: NOT FINISHED — %s. Not a review." % (reader, reason)
    if outcome == "not-run":
        return "%s: NOT RUN — %s. Not a review." % (reader, reason)
    if outcome == "failed":
        return ("%s: FAILED — %s. No findings can be read from it; not a "
                "review." % (reader, reason))
    return ("%s: a note with an unknown outcome %r — read it as absent, never "
            "as clean." % (reader, outcome))


def note_lines(row):
    """Every line a reader of this row sees about its findings pass, or []
    when the row carries no note. The newest note speaks; an older tip and
    the count of earlier notes are said, never hidden."""
    from . import dispatches
    notes = [n for n in (row or {}).get("findings_notes") or ()
             if isinstance(n, dict)]
    # A STOP IS THE NEWEST WORD when no note landed after it (task/3382):
    # it lives under the pass's store, because the ledger takes a note only
    # on a row still owed one.
    stop = stop_record((row or {}).get("id"))
    if _stop_speaks(stop, notes):
        return _stop_lines(stop, row, notes)
    if not notes:
        return []
    note = notes[-1]
    tip = str(note.get("reviewed_tip") or "")
    lines = ["findings pass (%s, tip %s): %s"
             % (note.get("ts") or "?", tip[:12], summary(note))]
    if tip and tip != str(row.get("tip") or ""):
        lines.append("  this note read an EARLIER tip; the row now names %s"
                     % str(row.get("tip") or "?")[:12])
    for line in _printable(str(note.get("findings") or "")).splitlines():
        lines.append("    " + line)
    # A COMPLETE NOTE'S REASON is not in its summary line, and the one a
    # complete note carries says what its read could not check.
    if note.get("outcome") == "complete" and note.get("reason"):
        lines.append("  " + _printable(str(note["reason"])))
    if note.get("status_line"):
        lines.append("  " + _printable(note["status_line"]))
    ref = note.get("output_ref")
    if ref:
        _text, problem = dispatches.read_brief_file(ref, note.get("output_bytes"))
        path = dispatches.brief_file_path(ref)
        lines.append("  whole output: %s (%s bytes)" % (path,
                                                        note.get("output_bytes"))
                     if not problem else
                     "  whole output UNAVAILABLE at %s — it is missing or is "
                     "not the text this note recorded" % path)
    if len(notes) > 1:
        lines.append("  (%d earlier note%s on this row)"
                     % (len(notes) - 1, "" if len(notes) == 2 else "s"))
    return lines


# ---------------------------------------------------------------- the worker


def _log(text):
    """One line in the worker log, rotated once past LOG_CAP."""
    path = log_path()
    try:
        if os.path.getsize(path) > LOG_CAP:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime()), text))
    except OSError:
        pass


def _main(argv):
    """`python3 -m helm.findingspass <row id>` — the detached worker, and the
    hand-run form for re-reading a row once the reader is back."""
    if len(argv) != 1 or argv[0].startswith("-"):
        print("usage: python3 -m helm.findingspass <dispatch-id>",
              file=sys.stderr)
        return 2
    row, why = run(argv[0])
    if row is None:
        _log("%s: no note recorded: %s" % (argv[0][:12], why))
        print("findings pass: no note recorded: %s" % why, file=sys.stderr)
        return 1
    note = (row.get("findings_notes") or ({},))[-1]
    _log("%s: %s" % (argv[0][:12], summary(note)))
    print(summary(note))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
