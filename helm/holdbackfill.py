"""The holder a pre-stamp SOURCE-CLEAN hold never recorded (task/3131).

A source-clean hold written before the ledger lock stamped `hold_actor`
records no hand, so `helm lr close <id> --reason source-clean-landed` refuses
it with "the hold records NO HOLDER". The hand is still on record in the one
place that seat wrote down what it did: its own Claude Code session
transcript. This module reads that record and, only when every proof holds,
puts the answer on the ledger through `dispatches.record_hold_actor_backfill`.

THE PROOFS, each named when it fails:
  * the recipient's transcript store is a seat home UNDER the helm home, found
    by helm's own seat census (`hooks.seat_homes`), that no other seat's home
    resolves to. A store anywhere else is never opened: the owner's credential
    tree and the default Claude store are refused by path before any read. A
    store two seats share is never read as either one's own, because no
    transcript line names the seat that wrote it;
  * EXACTLY ONE tool call in that store runs `dispatch hold <id>` with
    `--source-clean <tip>` (the id by a 12+ hex prefix, the tip whole or by a
    12+ hex prefix, anywhere in the command, because seats pass them through
    shell variables), its result prints the line `helm dispatch hold` prints
    on success, and it started inside the 60 s before the hold's stamp;
  * NO OTHER seat's store under the helm home holds such a call that started
    before the hold's stamped second ended, whatever its result, and every
    file there that opens before that second ended was read. The pre-stamp
    hold door checked no recipient, so it printed its success line for an
    idempotent re-run by any hand: a second call, or the recipient's own call
    copied into another seat's store, leaves the writer unproven (a Fable
    review). The window and the success line NAME a writer; they cannot CLEAR
    one: a hold call can write minutes after it starts, and a call moved to
    the background prints no success line into its transcript;
  * the recipient wrote no round of the lane (`landreq.source_clean_author_error`,
    the reading the hold door and the close already ask).

THE STORES ARE HUNDREDS OF MEGABYTES, so every file is read one bounded line
at a time (`_lines`), a line is JSON-parsed only after a substring test finds
the row's id prefix (or a pending tool call's id) in it, and a file in the
recipient's store last written before a hold's window opened is not opened
for that hold at all. Another seat's store gets no such skip, because a call
there that started long before the window can still have written the hold;
a file there whose first instant comes after the hold is read no further than
its head.

`census()` is a dry run unless asked to apply; the verb is
`helm lr backfill-hold-actor [<id>...] [--apply] [--json]`.
"""
import hashlib
import json
import os
import re
import sys

from . import dispatches, home

#: The refusal kinds, each with the sentence the census prints for it.
OUTSIDE_HELM = "store-outside-helm"
NO_MATCH = "no-transcript-match"
OTHER_SEAT = "match-only-in-another-seat"
AMBIGUOUS = "more-than-one-candidate"
OUT_OF_WINDOW = "outside-the-window"
NO_SUCCESS = "tool-result-not-a-success"
LANE_AUTHOR = "seat-wrote-a-round"
UNREADABLE = "unreadable"
NOT_PENDING = "not-a-pre-stamp-hold"
RIVAL = "call-in-another-seat"
SHARED = "store-shared-with-another-seat"

PROOFS = {
    OUTSIDE_HELM: "unrecoverable: transcript store outside ~/.helm",
    SHARED: "unrecoverable: transcript store shared with another seat",
    NO_MATCH: "no transcript match",
    OTHER_SEAT: "a match only in another seat's home",
    AMBIGUOUS: "more than one candidate",
    OUT_OF_WINDOW: "ts outside the window",
    NO_SUCCESS: "tool_result missing or not a success",
    LANE_AUTHOR: "the seat wrote a round of the lane",
    UNREADABLE: "the proof could not be read",
    NOT_PENDING: "not a source-clean hold that records no holder",
    RIVAL: "a call in another seat's store that started before the hold",
}

RECOVERABLE, OWED, WRITTEN, REFUSED = "RECOVERABLE", "OWED", "WRITTEN", "REFUSED"

#: The longest line read whole. A longer one is drained unread: no tool call
#: or result this looks for is that long, and a line is the unit of memory.
LINE_CAP = 1 << 20

#: How many opening lines are read for a file's first instant (other seats'
#: stores only: a file there that opens after the hold holds no rival).
_HEAD_LINES = 32

#: The two lines `helm dispatch hold --source-clean` prints only when the hold
#: is recorded (dispatches_cli, since the source-clean claim shipped): the
#: first names the row and the tip; the second, printed when the held tip is
#: not the dispatched one, names the row's dispatched tip and the held tip.
SUCCESS_LINE = ("helm dispatch: %s — HELD SOURCE-CLEAN at %s — ON "
                "THE INTEGRATOR'S LAND GATE")
DECLARES_LINE = ("helm dispatch: the row was dispatched at %s; this hold "
                 "declares %s clean")

_HOLD_VERB = re.compile(r"\bdispatch\s+hold\b")
_TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*"([^"]{10,40})"')
_SOURCE_CLEAN = b"--source-clean"


def _inside(path, root):
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root + os.sep)


def _forbidden_roots():
    """The stores no census opens: the owner's credential homes and the
    default Claude store. Spelled as paths and never resolved, so naming
    them touches nothing."""
    user = os.path.expanduser("~")
    return (os.path.join(user, ".claude-homes"), os.path.join(user, ".claude"))


def admissible_store(path):
    """Is `path` (a resolved directory) a store this census may open: inside
    the helm home and inside no forbidden root."""
    root = os.path.realpath(home.helm_home())
    return _inside(path, root) and not any(
        _inside(path, os.path.normpath(f)) for f in _forbidden_roots())


def seat_stores():
    """([(seat, projects dir)], unread) — every seat's transcript store helm's
    own seat census knows, resolved, with the census's unreadable entries. A
    config dir two seats' dirs resolve to is listed under EACH of them, so
    `stores_for` sees its second holder."""
    from . import hooks
    homes, unread = hooks.seat_homes(every=True)
    out = []
    for seat, claude in homes:
        out.append((seat, os.path.realpath(os.path.join(claude, "projects"))))
    return out, unread


def stores_for(recipient, stores):
    """(mine, others, outside) for one recipient.

    mine: its stores under the helm home that no other seat's home resolves
    to. others: every other seat's store under the helm home, a shared one
    included, because a success there is still another hand's. outside: its
    own stores that are not its own alone, as (seat, path, [the other seats
    holding it]): outside the helm home (never opened; the list is empty), or
    shared with another seat (never read as its own; the list names them)."""
    from . import landreq
    holders = {}
    for seat, path in stores:
        holders.setdefault(path, []).append(seat)
    mine, others, outside = [], [], []
    for seat, path in stores:
        ok = admissible_store(path)
        if landreq._same_seat(seat, recipient):
            sharers = sorted({h for h in holders[path]
                              if not landreq._same_seat(h, recipient)})
            if ok and not sharers:
                mine.append((seat, path))
            else:
                outside.append((seat, path, sharers if ok else []))
        elif ok:
            others.append((seat, path))
    return mine, others, outside


def pending_rows(rows):
    """Every HELD source-clean row whose standing hold records no holder."""
    out = []
    for row in rows.values():
        if row.get("status") != "held" or row.get("owner_gated") is True:
            continue
        if not dispatches._clean_tip_of(row) \
                or str(row.get("hold_actor") or "").strip():
            continue
        if dispatches.unknown_event_kinds(row) \
                or dispatches._retired_admin_by(row):
            continue
        out.append(row)
    return sorted(out, key=lambda r: (str(r.get("hold_ts") or ""), r["id"]))


class _Want(object):
    """One row's question: which tool call wrote its standing hold."""

    def __init__(self, row):
        self.row = row
        self.rid = row["id"]
        self.tip = dispatches._clean_tip_of(row)
        self.row_tip = str(row.get("tip") or "").lower()
        self.hold_ts = row.get("hold_ts")
        held = dispatches.instant_epoch(self.hold_ts)
        self.lo = (held if held is not None else 0) \
            - dispatches.HOLD_ACTOR_WINDOW_S
        self.hi = (held if held is not None else 0) + 1
        self.needle = self.rid[:12].encode("ascii")
        self.success = (SUCCESS_LINE % (self.rid, self.tip),
                        DECLARES_LINE % (self.row_tip, self.tip))


def _names(text, full):
    """Does `text` carry a 12+ hex prefix of `full` as a whole hex token?"""
    for match in re.finditer(r"(?<![0-9a-f])%s[0-9a-f]*" % full[:12], text):
        if full.startswith(match.group(0)):
            return True
    return False


def _command_matches(command, want):
    return bool(_HOLD_VERB.search(command)) and "--source-clean" in command \
        and _names(command, want.rid) and _names(command, want.tip)


def _lines(handle):
    """(line number, bytes or None) for each line of an open binary file, read
    one bounded line at a time; a line longer than LINE_CAP is drained and
    yields None, so no read ever holds more than LINE_CAP bytes."""
    number = 0
    while True:
        chunk = handle.readline(LINE_CAP)
        if not chunk:
            return
        number += 1
        if chunk.endswith(b"\n") or len(chunk) < LINE_CAP:
            yield number, chunk
            continue
        while True:
            more = handle.readline(LINE_CAP)
            if not more or more.endswith(b"\n") or len(more) < LINE_CAP:
                break
        yield number, None


def _first_instant(handle):
    """The first transcript instant in a file's opening lines, or None."""
    for number, raw in _lines(handle):
        if number > _HEAD_LINES:
            return None
        found = _TIMESTAMP.search(raw or b"")
        if found:
            return dispatches.instant_epoch(found.group(1).decode(
                "ascii", "replace"))
    return None


def _transcripts(root):
    """(path, entry) for every .jsonl file under a store, symlinks never
    followed; (path, None) for a directory that exists and cannot be listed.
    An absent store holds nothing, which is not the same as unreadable."""
    stack = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            yield path, None
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False) and depth < 6:
                stack.append((entry.path, depth + 1))
            elif entry.name.endswith(".jsonl") \
                    and entry.is_file(follow_symlinks=False):
                yield entry.path, entry


def _result_text(item):
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict))
    return ""


def _scan_file(seat, path, stat, wants, found, bound_head):
    """Add every candidate tool call in one transcript to `found[rid]` ->
    None, or (why, the wants it could hold a call for) when it cannot be
    opened.

    A file in the recipient's store last written before a hold's window
    opened holds no call inside it. Another seat's store (`bound_head`) gets
    no such skip: a call there counts whenever it started before the hold,
    and a call can write the hold minutes after its file was last written."""
    live = list(wants) if bound_head \
        else [w for w in wants if stat.st_mtime >= w.lo]
    if not live:
        return None
    try:
        handle = open(path, "rb")
    except OSError as exc:
        return "%s: %s" % (path, exc.strerror or exc), live
    with handle:
        if bound_head:
            first = _first_instant(handle)
            if first is not None:
                live = [w for w in live if first < w.hi]
            if not live:
                return None
            handle.seek(0)
        pending = {}
        for number, raw in _lines(handle):
            if raw is None:
                continue
            hits = [w for w in live if w.needle in raw] \
                if _SOURCE_CLEAN in raw else []
            waiting = [t for t in pending if t in raw] if pending else []
            if not hits and not waiting:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            items = message.get("content") if isinstance(message, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if waiting and item.get("type") == "tool_result":
                    tid = str(item.get("tool_use_id") or "").encode("utf-8")
                    for cand in pending.pop(tid, ()):
                        text = _result_text(item)
                        cand["result"] = "success" if any(
                            line in text for line in cand["_want"].success) \
                            else "failure"
                if hits and item.get("type") == "tool_use" \
                        and entry.get("type") == "assistant":
                    command = (item.get("input") or {}).get("command") \
                        if isinstance(item.get("input"), dict) else None
                    tid = item.get("id")
                    if not isinstance(command, str) or not isinstance(tid, str):
                        continue
                    for want in hits:
                        if not _command_matches(command, want):
                            continue
                        cand = {
                            "_want": want, "seat": seat, "transcript": path,
                            "line": number, "tool_use_id": tid,
                            "command_sha256": hashlib.sha256(
                                command.encode("utf-8")).hexdigest(),
                            "tool_ts": entry.get("timestamp"),
                            "result": None}
                        cand["in_window"] = dispatches.hold_window_error(
                            cand["tool_ts"], want.hold_ts) is None
                        found[want.rid].append(cand)
                        pending.setdefault(tid.encode("utf-8"), []).append(cand)
    return None


def _scan(store_wants, found, bound_head=False):
    """Scan each (seat, store) for its wants -> {rid: [what was unreadable
    that could hold a call for that row]}. A file that cannot be opened
    counts for the rows it was live for (it was last written before every
    other row's window opened); a path that cannot be listed or stat'd
    counts for every row of its store."""
    unread = {}
    for (seat, root), wants in store_wants:
        for path, entry in _transcripts(root):
            why, whom = None, wants
            if entry is None:
                why = "%s: not listable" % path
            else:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    why = "%s: %s" % (path, exc.strerror or exc)
                else:
                    got = _scan_file(seat, path, stat, wants, found,
                                     bound_head)
                    if got:
                        why, whom = got
            if why:
                for want in whom:
                    unread.setdefault(want.rid, []).append(why)
    return unread


def _distinct(cands):
    """One candidate per tool call: a session copied into another file carries
    the same call, and one call is one candidate. A copy whose result was
    read as a success wins, then the first by path and line."""
    best = {}
    for cand in sorted(cands, key=lambda c: (c["transcript"], c["line"])):
        held = best.get(cand["tool_use_id"])
        if held is None or (cand["result"] == "success"
                            and held["result"] != "success"):
            best[cand["tool_use_id"]] = cand
    return list(best.values())


def _after_hold(cand, want):
    """Did this call provably start after the hold's stamped second ended?
    Only that clears a call from writing the hold: an unreadable instant
    does not."""
    tool = dispatches.instant_epoch(cand["tool_ts"])
    held = dispatches.instant_epoch(want.hold_ts)
    return None not in (tool, held) and tool >= held + 1


def _evidence(cand):
    return {"transcript": cand["transcript"], "line": cand["line"],
            "tool_use_id": cand["tool_use_id"],
            "command_sha256": cand["command_sha256"],
            "tool_ts": cand["tool_ts"]}


def _where(cands):
    return ", ".join("%s:%d (%s)" % (c["transcript"], c["line"],
                                     c["tool_ts"]) for c in cands[:3]) \
        + (" and %d more" % (len(cands) - 3) if len(cands) > 3 else "")


_RESULT_WORDS = {"success": "prints the hold recorded",
                 "failure": "lacks the success line",
                 None: "was not read"}


def _judge(want, mine, unread, others, others_unread):
    """(kind or None, sentence, candidate) for one row's transcript proof:
    `mine` and `unread` from the recipient's own store, `others` and
    `others_unread` from every other seat's store under the helm home."""
    rid12, tip12 = want.rid[:12], want.tip[:12]
    asked = "`dispatch hold %s --source-clean %s`" % (rid12, tip12)
    if unread:
        return (UNREADABLE, "%s: %d path(s) in the recipient's store could "
                "not be read, so a second candidate cannot be ruled out (%s)"
                % (PROOFS[UNREADABLE], len(unread), "; ".join(unread[:3])),
                None)
    cands = _distinct(mine)
    if not cands:
        seats = sorted({c["seat"] for c in others})
        if seats:
            return (OTHER_SEAT, "%s: no tool call in @%s's transcripts runs "
                    "%s, and %s do" % (PROOFS[OTHER_SEAT],
                                       want.row.get("recipient"), asked,
                                       ", ".join("@" + s for s in seats)),
                    None)
        return (NO_MATCH, "%s: no tool call in @%s's transcripts, nor in any "
                "other seat's under the helm home, runs %s" % (
                    PROOFS[NO_MATCH], want.row.get("recipient"), asked), None)
    inside = [c for c in cands if c["in_window"]]
    if not inside:
        held = dispatches.instant_epoch(want.hold_ts)
        nearest = min(cands, key=lambda c: abs(
            (dispatches.instant_epoch(c["tool_ts"]) or 0) - held))
        return (OUT_OF_WINDOW, "%s: %d tool call(s) run %s, none inside the "
                "%d s before the hold; the nearest: %s (%s:%d; its result %s)"
                % (PROOFS[OUT_OF_WINDOW], len(cands), asked,
                   dispatches.HOLD_ACTOR_WINDOW_S,
                   dispatches.hold_window_error(nearest["tool_ts"],
                                                want.hold_ts),
                   nearest["transcript"], nearest["line"],
                   _RESULT_WORDS[nearest["result"]]), None)
    good = [c for c in inside if c["result"] == "success"]
    if not good:
        return (NO_SUCCESS, "%s: %d tool call(s) inside the window run %s and "
                "none shows the hold recorded (%s): %s" % (
                    PROOFS[NO_SUCCESS], len(inside), asked,
                    "no result read" if all(c["result"] is None
                                            for c in inside)
                    else "the result lacks the success line",
                    _where(inside)), None)
    if len(good) > 1:
        return (AMBIGUOUS, "%s: %d successful tool calls inside the window "
                "run %s: %s" % (PROOFS[AMBIGUOUS], len(good), asked,
                                _where(good)), None)
    # ONE SUCCESS NAMES ITS WRITER ONLY WHEN NO OTHER STORE HOLDS A CALL
    # THAT COULD HAVE WRITTEN THE HOLD. The pre-stamp hold door checked no
    # recipient, so it printed its success line for an idempotent re-run by
    # any hand; and the recipient's own call found in another seat's store is
    # a copied session, which does not say which way it was copied (a Fable
    # review). ONLY A START AFTER THE HOLD'S STAMPED SECOND CLEARS A CALL
    # THERE. The window and the success line name a writer and clear none:
    # 11a9a9574b18's hold was written by a call that started 171 s before its
    # stamp, and its transcript result says only that the call was moved to
    # the background (the success line is in the task's output file).
    own = {c["tool_use_id"] for c in cands}
    # EVERY COPY IS JUDGED, before one is chosen to speak for its call: a
    # copy stamped after the hold must not clear the same call's earlier one.
    rivals = _distinct([c for c in others
                        if c["tool_use_id"] in own or not _after_hold(c, want)])
    if rivals:
        ids = {c["tool_use_id"] for c in rivals}
        copied = [c for c in rivals if c["tool_use_id"] in own]
        second = [c for c in rivals if c["tool_use_id"] not in own]
        said = []
        if second:
            said.append("%d more tool call(s) run %s there and started before "
                        "the hold's stamped second ended (results: %s), and "
                        "the pre-stamp hold door printed the hold recorded "
                        "for an idempotent re-run by any hand, so no one "
                        "success names the writer: %s"
                        % (len(second), asked, ", ".join(
                            _RESULT_WORDS[c["result"]] for c in second[:3]),
                           _where(second)))
        if copied:
            said.append("the same tool call as @%s's own sits there too, a "
                        "copied session that does not say which seat made "
                        "it: %s" % (want.row.get("recipient"), _where(copied)))
        return (RIVAL, "%s: %s: %s" % (
            PROOFS[RIVAL], ", ".join("@" + s for s in sorted(
                {c["seat"] for c in others if c["tool_use_id"] in ids})),
            "; ".join(said)), None)
    if others_unread:
        return (UNREADABLE, "%s: %d path(s) in other seats' stores could not "
                "be read, so a rival call that started before the hold "
                "cannot be ruled out (%s)" % (
                    PROOFS[UNREADABLE], len(others_unread),
                    "; ".join(others_unread[:3])), None)
    return None, "", good[0]


def _entry(row):
    return {"id": row["id"], "recipient": row.get("recipient"),
            "lane": row.get("lane"), "hold_seq": row.get("hold_seq"),
            "hold_ts": row.get("hold_ts"),
            "source_clean_tip": dispatches._clean_tip_of(row) or None,
            "verdict": OWED, "kind": None, "reason": "", "evidence": None}


def census(ids=None, apply=False):
    """(report, err). The report lists, for every source-clean hold that
    records no holder (or for the rows `ids` names), RECOVERABLE with its
    evidence or OWED with the proof that failed. With `apply`, each
    recoverable row is written through `dispatches.record_hold_actor_backfill`
    and reads WRITTEN, or REFUSED with the writer's words."""
    from . import landreq
    rows, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    pending = pending_rows(rows)
    entries, todo = [], []
    if ids:
        for rid in ids:
            row, err = dispatches._resolve_row(rows, rid,
                                               allow_unknown_kinds=True,
                                               allow_retired=True)
            if err:
                return None, err
            entry = _entry(row)
            entries.append(entry)
            if row["id"] in {r["id"] for r in pending}:
                todo.append((entry, _Want(row)))
            else:
                entry.update(kind=NOT_PENDING, reason="%s: the row is %s%s" % (
                    PROOFS[NOT_PENDING], row.get("status"),
                    ", held by @%s" % row["hold_actor"]
                    if row.get("hold_actor") else ""))
    else:
        for row in pending:
            entry = _entry(row)
            entries.append(entry)
            todo.append((entry, _Want(row)))
    stores, census_unread = seat_stores()
    placed = []
    for entry, want in todo:
        if dispatches.instant_epoch(want.hold_ts) is None \
                or isinstance(want.row.get("hold_seq"), bool) \
                or not isinstance(want.row.get("hold_seq"), int):
            entry.update(kind=UNREADABLE, reason="%s: the hold's instant "
                         "(%r) or seq (%r) is unreadable" % (
                             PROOFS[UNREADABLE], want.hold_ts,
                             want.row.get("hold_seq")))
            continue
        mine, others, outside = stores_for(want.row.get("recipient"), stores)
        shared = [o for o in outside if o[2]]
        if not mine:
            if census_unread:
                entry.update(kind=UNREADABLE, reason="%s: the seat census "
                             "could not read %s, so @%s's store may be there"
                             % (PROOFS[UNREADABLE], census_unread[0][0],
                                want.row.get("recipient")))
            elif shared:
                entry.update(kind=SHARED, reason="%s: @%s's seat home "
                             "resolves to %s, and so does %s's, so no "
                             "transcript there is provably @%s's" % (
                                 PROOFS[SHARED], want.row.get("recipient"),
                                 shared[0][1], ", ".join(
                                     "@" + s for s in shared[0][2]),
                                 want.row.get("recipient")))
            else:
                entry.update(kind=OUTSIDE_HELM, reason="%s: %s" % (
                    PROOFS[OUTSIDE_HELM],
                    "@%s's seat home resolves to %s, which is never opened"
                    % (want.row.get("recipient"), outside[0][1]) if outside
                    else "no seat home for @%s under %s"
                    % (want.row.get("recipient"), home.helm_home())))
            continue
        placed.append((entry, want, mine, others))
    # THE PROOF READS ONLY EACH RECIPIENT'S OWN STORE, every file last
    # written inside or after its hold's window, whole.
    found = {want.rid: [] for _e, want, _m, _o in placed}
    by_store = {}
    for _e, want, mine, _o in placed:
        for store in mine:
            by_store.setdefault(store, []).append(want)
    unread = _scan(sorted(by_store.items()), found)
    # OTHER SEATS' STORES under the helm home are read for EVERY placed row,
    # every file that opens before its hold. For a row the proof found a
    # success for, a call there that started before the hold's stamped
    # second ended is a RIVAL, whatever its result: the pre-stamp hold door
    # printed its success line for an idempotent re-run by any hand, so two
    # calls name no writer, and a file there that cannot be read cannot rule
    # one out (a Fable review). For a row the proof found nothing for, the
    # same read is the diagnosis.
    elsewhere = {want.rid: [] for _e, want, _m, _o in placed}
    by_store = {}
    for _e, want, _m, others in placed:
        for store in others:
            by_store.setdefault(store, []).append(want)
    unread_elsewhere = _scan(sorted(by_store.items()), elsewhere,
                             bound_head=True)
    for entry, want, _m, _o in placed:
        kind, why, cand = _judge(
            want, found[want.rid], unread.get(want.rid, []),
            elsewhere[want.rid],
            list(dict.fromkeys(unread_elsewhere.get(want.rid, ()))))
        if kind is None:
            aerr = landreq.source_clean_author_error(
                want.row, want.row.get("recipient"),
                doors=dispatches.held_tip_doors(
                    want.row, want.row.get("source_clean_tip"), rows))
            if aerr:
                kind = LANE_AUTHOR if getattr(aerr, "kind", None) \
                    == landreq.SourceCleanRefusal.LANE_AUTHOR else UNREADABLE
                why = "%s: %s" % (PROOFS[kind], aerr)
        if kind is not None:
            entry.update(kind=kind, reason=why)
            continue
        entry.update(verdict=RECOVERABLE, evidence=_evidence(cand))
    report = {"rows": entries, "applied": bool(apply), "written": 0,
              "refused": 0}
    if apply:
        for entry in entries:
            if entry["verdict"] != RECOVERABLE:
                continue
            _row, err = dispatches.record_hold_actor_backfill(
                entry["id"], entry["hold_seq"], entry["evidence"])
            if err:
                entry.update(verdict=REFUSED, reason=err)
                report["refused"] += 1
            else:
                entry["verdict"] = WRITTEN
                report["written"] += 1
    report["recoverable"] = sum(1 for e in entries
                                if e["verdict"] in (RECOVERABLE, WRITTEN))
    report["owed"] = len(entries) - report["recoverable"] - report["refused"]
    return report, None


def _age(entry):
    """Where the call started against the hold's stamp, which the ledger
    floors to its second: a call inside that second reads as inside it."""
    held = dispatches.instant_epoch(entry["hold_ts"])
    tool = dispatches.instant_epoch((entry["evidence"] or {}).get("tool_ts"))
    if None in (held, tool):
        return "?"
    if tool >= held:
        return "inside the hold's stamped second"
    return "%.1f s before the hold" % (held - tool)


def render(report):
    """The census as the operator reads it."""
    rows = report["rows"]
    head = ("helm lr backfill-hold-actor — %d source-clean hold%s recording "
            "no holder: %d recoverable, %d owed" % (
                len(rows), "" if len(rows) == 1 else "s",
                report["recoverable"], report["owed"]))
    if report["applied"]:
        head += ", %d written, %d refused" % (report["written"],
                                              report["refused"])
    else:
        head += "  [dry run: nothing written; --apply records the recoverable]"
    out = [head]
    for entry in rows:
        lead = "  %-11s %s  @%s  hold seq %s at %s  tip %s" % (
            entry["verdict"], entry["id"][:12], entry["recipient"],
            entry["hold_seq"], entry["hold_ts"],
            str(entry["source_clean_tip"] or "-")[:12])
        if entry["evidence"] and entry["verdict"] in (RECOVERABLE, WRITTEN):
            ev = entry["evidence"]
            out.append("%s\n      tool call %s (%s) %s:%d %s" % (
                lead, ev["tool_ts"], _age(entry), ev["transcript"],
                ev["line"], ev["tool_use_id"]))
        else:
            out.append("%s\n      %s" % (lead, entry["reason"]))
    return "\n".join(out)


USAGE = ("usage: helm lr backfill-hold-actor [<id>...] [--apply] [--json]  "
         "(a dry run by default: lists every source-clean hold that records "
         "no holder, RECOVERABLE from its recipient's own transcript or owed "
         "with the proof that failed; --apply records the recoverable ones)")


def cmd(rest):
    """`helm lr backfill-hold-actor` -> rc: 0 when every listed row is
    recoverable (or written), 1 when any stays owed or a write is refused,
    2 on a usage error."""
    from .cli import guard_tail
    ids = [a for a in rest if not a.startswith("-")]
    rc = guard_tail("helm lr backfill-hold-actor",
                    [a for a in rest if a.startswith("-")],
                    flags=("--apply", "--json"), usage=USAGE)
    if rc is not None:
        return rc
    report, err = census(ids=ids or None, apply="--apply" in rest)
    if err:
        print("helm lr backfill-hold-actor: %s" % err, file=sys.stderr)
        return 1
    if "--json" in rest:
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    else:
        print(render(report))
    return 0 if report["owed"] == 0 and report["refused"] == 0 else 1
