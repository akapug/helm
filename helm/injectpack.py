#!/usr/bin/env python3
"""WHAT HELM PUT INTO THIS SEAT'S LAST TURNS, ENTRY BY ENTRY, AND WHAT AN EDIT
WOULD DO TO THE NEXT ONE.

THE SUBJECT IS ONE HOOK EVENT AND THAT IS A MEASUREMENT, NOT A SIMPLIFICATION.
Across seven whole transcripts totalling 3.95 GB, hook records are 29.46%
of transcript bytes ON DISK and 1.71% of CONTEXT; of every hook byte that
reached a context window, UserPromptSubmit was 95.9%. PostToolUse was 0.2% and
Stop was 0.0% -- 47,881 fires for 18 KB of context in total. A surface giving
the five hook events equal room would be true to the config file and false to
the cost, so this module reports the UserPromptSubmit premise pack and says so.

THE PRODUCER IS HELM'S OWN FIRE LEDGER, NOT THE TRANSCRIPT, and the neighbours
divide the question three ways so no two of them can publish a contested
number:

  * `inject._ledger` WRITES the ledger: one row per admitted gather(), with
    the ids that fired per lane, the exact UTF-8 bytes per lane, the ids the
    cooldown suppressed, and the bytes that suppression saved.
  * `injectbudget` (task/2679) reads the HARNESS TRANSCRIPT and answers what
    SHARE of a seat's context all hooks occupy. Its census reads up to 64 MB
    backwards per call, which is why its own docstring keeps it to `helm
    doctor` and a CLI verb. This module never asks that question and never
    restates its answer.
  * THIS module reads the ledger and answers WHICH ENTRIES, at what cost, how
    often repeated -- the only one of the three keyed by something the owner
    can click. You cannot demote a hook event; you can demote a premise.

  The two readings reconcile at one point and a future reader should check it
  there: injectbudget's `by_event["UserPromptSubmit"]` for a window against
  this module's `bytes` for the same window -- the harness's receipt and
  helm's claim for the same delivery. TWO KNOWN DIFFERENCES SEPARATE THEM AND
  A READER WHO DOES NOT KNOW THEM WILL READ SYSTEMATIC DRIFT AS A DEFECT.
  First, the harness files an injection inside its own reminder envelope, so
  the receipt is the larger of the two by that wrapper. Second, and this is
  the one worth knowing, THE TWO ARE NOT IN THE SAME UNIT: injectbudget sizes
  with `len(text)` and contains no `.encode()` at all, so its "bytes" are
  CHARACTERS, exactly as `lane_report`'s are (see below). This module's are
  UTF-8 bytes, which is the unit the ledger itself declares. The difference is
  zero while the store stays ASCII and grows with every accented character in
  it. That mismatch is task/2691 and the fix runs toward this module rather
  than away from it -- do not re-spell these figures to agree with the two
  landed instruments.

SEGMENTED AT THE CONTEXT BOUNDARY, BECAUSE A SESSION TOTAL IS THE WRONG NUMBER.
At a compaction or a /clear the seat genuinely loses its premises and helm
re-fires them ON PURPOSE (`inject._ledger.forget_session` drops the fire-record
file at exactly that boundary). Summing a session that spans twenty boundaries
scores the design working as intended as waste -- the error that inflated an
earlier reading of this same population from 72% to 97%. The boundary is
OBSERVABLE in the ledger without a second source: the per-session turn counter
lives in the seen-state that forget_session deletes, so a row whose `turn` is 1
after a previous row is a new window. MEASURED over the live ledger's four
busiest sessions: every one showed exactly one reset apiece to turn 1, plus one
session carrying a repeated turn number (19 after 19) which is two gathers
inside ONE turn and NOT a boundary. So the split is on `turn == 1` alone and a
bare decrease is counted as out-of-order and REPORTED rather than folded, since
an over-eager split would manufacture windows and hide a real repeat.
The split stays on `turn == 1`, which is the only field every row in this
population carries. Rows written since the reset provenance landed also carry
`epoch` -- why that boundary happened -- and a reader asking whether a window
is a REAL loss rather than a spurious reset wants that field; its absence on
an older row means the row cannot say, never that nothing vouched, so it can
narrow a question here but must never decide this split.

PER-ENTRY BYTES ARE DERIVED, NOT LEDGERED, AND THE DERIVATION IS DECLARED. The
ledger carries bytes per LANE and ids per lane, never bytes per id. So an
entry's cost here is today's rendering of that entry times its fires --
`inject.lane_report`'s own method, reused rather than re-spelled so the two
instruments cannot drift. TWO WAYS IT IS AN ESTIMATE, both surfaced rather than
smoothed: an entry whose text changed since it fired is priced at today's
length, and an entry the store no longer holds prices at zero. `derived` beside
each window's ledgered `bytes` is the CONTROL on exactly that -- a reader who
sees them diverge knows the derivation has stopped tracking, instead of reading
a per-entry table that silently stopped adding up.

IT COUNTS UTF-8 BYTES AND BOTH NEIGHBOURS COUNT CHARACTERS. `lane_report`'s
`bytes~` column is `len(str)`, and `injectbudget` sizes every population it
reports with `len(text)`; neither calls `.encode()` anywhere. A character count
wearing the name of a byte count agrees with a real one only while the store
stays ASCII. The ledger's own `rendered_bytes` is declared UTF-8, so this
module encodes before it measures -- which is what makes its derived total
comparable to the ledgered figure it is the control for, and what makes it the
odd one out of three. Reconciling that is task/2691.

WHAT THE DERIVED TOTAL CANNOT SEE, REPORTED RATHER THAN THRESHOLDED. A window's
rendered bytes also carry lines that belong to NO entry id -- the day's brief
whisper, reflex steer text, a rule's gate rider, and the newline joining every
line to the next. The per-entry sum therefore sits UNDER the ledgered total
always and never over it, so `unattributed` carries that remainder as a number
on every read instead of a threshold deciding whether to mention it. MEASURED
across the six busiest live sessions it ran 0.6% to 8.7% of a window, of which
the newlines are about 0.3 points; an alarm banded to catch that would fire on
every healthy window, and one banded above it stays silent forever while the
bias grows. So `derived_note` speaks for the two readings that are NOT ordinary:
any overcount at all, which the model above says is impossible, and an
undercount past a band set well above that measured population.
"""
from . import injection_schema


# How many per-turn rows the detail view carries back. The owner reads the
# TOTALS at the top; the turn list is the evidence behind them, and a list long
# enough to scroll is a list nobody checks.
TURN_SAMPLES = 12

# The lanes an entry can fire in, in the order the injector walks them.
FIRED_LANES = ("pinned", "jit", "reflex")


def _exact(row):
    """A ledger row this module may read: an exact-byte v2/v3 sample."""
    return isinstance(row, dict) and injection_schema.valid_exact(row)


def windows(rows):
    """-> [[row, ...], ...], oldest window first, split at each context loss.

    The split is `turn == 1` after a previous row, never a bare decrease --
    see the module docstring for the measurement behind that choice.
    """
    out, cur = [], []
    for row in rows:
        turn = row.get("turn")
        if cur and type(turn) is int and turn == 1:
            out.append(cur)
            cur = []
        cur.append(row)
    if cur:
        out.append(cur)
    return out


def _blank():
    return {"turns": 0, "silent": 0, "bytes": 0,
            "lanes": {lane: 0 for lane in injection_schema.V2_LANES},
            "fires": 0, "ids": {}, "suppressed_fires": 0,
            "suppressed_bytes": 0, "candidates": None, "out_of_order": 0,
            "first_ts": None, "last_ts": None, "last_turn": None}


def fold(rows):
    """-> one window's totals, folded from its ledger rows."""
    win = _blank()
    prev = None
    for row in rows:
        win["turns"] += 1
        if row.get("silent") is True:
            win["silent"] += 1
        sample = row.get("sample") or {}
        win["bytes"] += sample.get("rendered_bytes") or 0
        for lane, size in (sample.get("lane_bytes") or {}).items():
            if lane in win["lanes"]:
                win["lanes"][lane] += size or 0
        for lane in FIRED_LANES:
            for ident in (row.get("fired") or {}).get(lane) or ():
                ident = str(ident)
                win["fires"] += 1
                slot = win["ids"].setdefault(ident, {"fires": 0, "lane": lane,
                                                     "last_turn": None})
                slot["fires"] += 1
                slot["lane"] = lane
                slot["last_turn"] = row.get("turn")
        for key in ("suppressed", "suppressed_pinned"):
            # A SUPPRESSED ID IS THE COOLDOWN WORKING, NOT A COST. It is
            # counted so the owner can see what the guard already spared him;
            # reading it as waste would recommend removing the cure.
            win["suppressed_fires"] += len(row.get(key) or ())
        for size in (row.get("suppressed_utf8_bytes") or {}).values():
            win["suppressed_bytes"] += size or 0
        if type(row.get("candidates")) is int:
            win["candidates"] = row["candidates"]
        turn = row.get("turn")
        if prev is not None and type(turn) is int and turn <= prev:
            win["out_of_order"] += 1
        if type(turn) is int:
            prev = turn
            win["last_turn"] = turn
        if row.get("ts"):
            win["first_ts"] = win["first_ts"] or row["ts"]
            win["last_ts"] = row["ts"]
    return win


def repeat(win):
    """-> the share of this window's fires the seat already had, or None.

    WITHIN ONE WINDOW ONLY, for the reason the module docstring gives: a
    re-delivery after a context loss is the injector restoring what the seat
    provably lost, and counting it here would score the cure as the disease.
    """
    fires = win["fires"]
    if fires <= 0:
        return None
    return float(fires - len(win["ids"])) / fires


def per_turn(win):
    """-> mean rendered bytes per turn in this window, or None on no turns."""
    return round(float(win["bytes"]) / win["turns"], 1) if win["turns"] else None


def _entry_index(project=None):
    """-> ({id: entry}, who_bytes, unavailable). The store as the injector
    itself loads it, so an entry this page offers to change is an entry that
    actually fires."""
    try:
        from . import inject
        by_id = {str(e["id"]): e for e in inject.load_entries(project)}
        who = sum(len(line.encode("utf-8")) for line in inject._who_lines())
        return by_id, who, None
    except Exception:
        return {}, 0, "store"


def _line_bytes(entry, ident, who_bytes):
    """-> the UTF-8 length of the line this entry fires as, today."""
    if ident == _who_id():
        return who_bytes
    if not entry:
        return 0
    try:
        from . import inject
        return len(inject._entry_line(entry).encode("utf-8"))
    except Exception:
        return 0


def _who_id():
    try:
        from . import inject
        return inject.WHO_ID
    except Exception:
        return "who:operator"


def _scope(entry):
    """-> (recorded project or fleet-or-derived, how) for one entry."""
    try:
        from .store import entry_scope
        owner, how = entry_scope(entry)
        return str(owner or ""), str(how or "")
    except Exception:
        return str(entry.get("project") or ""), "unknown"


def _demoted_receipt(entry):
    """True iff this entry carries the receipt `demote --undo` replays."""
    return any(isinstance(r, dict) and r.get("type") == "demoted"
               and isinstance(r.get("was"), dict)
               for r in entry.get("evidence_log") or ())


def _actions(entry):
    """The verbs THIS entry can actually take, decided by the WRITERS' OWN
    admission rules rather than by the renderer's optimism.

    A BUTTON THAT ALWAYS REFUSES TEACHES THE OWNER THAT THE PAGE LIES. The
    trap here is `undemote`: it looks like it belongs on every entry outside
    the always lane, which is most of them, and `demote --undo` refuses a prior
    with no demote receipt to replay -- so that reading puts a button that
    cannot work on nearly every row. The admission rules below are
    write.demote's, read off write.demote rather than remembered:

      demote    always-lane prior/reference only (heuristic and lexicon
                parse-force jit, so they can never hold the lane to leave it)
      undemote  prior/reference already OUT of the lane, and for a prior only
                when a `demoted` receipt exists to restore it from
      rescope    any entry -- the one lever that works on a JIT entry, which
                is where the measured cost actually is
      retire     prior/heuristic/reference

    `rescope` leads because of what the ledger says rather than what reads
    tidiest: across the live windows sampled while this was written, the pinned
    lane cost 0 bytes per turn (its content fingerprint suppresses it after one
    delivery) and the JIT lane carried essentially the whole per-turn figure.
    Demotion moves an entry OUT of the lane that already costs nothing, so it
    is not the owner's lever here even though it is the reversible one.
    """
    kind = entry.get("type")
    always = entry.get("load_class") == "always"
    out = ["rescope"]
    if kind in ("prior", "reference"):
        if always:
            out.append("demote")
        elif kind == "reference" or _demoted_receipt(entry):
            out.append("undemote")
    if kind in ("prior", "heuristic", "reference"):
        out.append("retire")
    return out


def entries(win, project=None):
    """-> ([entry row, ...] costliest first, derived_total, unavailable).

    Costliest FIRST and never alphabetical: the question the owner brought is
    which entries are worth changing, and a list ordered by name answers a
    different one.
    """
    by_id, who_bytes, missing = _entry_index(project)
    rows, derived = [], 0
    for ident, slot in win["ids"].items():
        entry = by_id.get(ident)
        line = _line_bytes(entry, ident, who_bytes)
        total = line * slot["fires"]
        derived += total
        row = {"id": ident, "lane": slot["lane"], "fires": slot["fires"],
               "last_turn": slot["last_turn"], "line_bytes": line,
               "bytes": total, "present": bool(entry) or ident == _who_id(),
               "type": (entry or {}).get("type"),
               "load_class": (entry or {}).get("load_class"),
               "text": "", "project": "", "scope_how": "", "actions": []}
        if entry:
            # A GLOSS THAT READS AS WHOLE IS THE ANSWER TO THE WRONG QUESTION.
            # The row exists so the owner can decide whether an entry is worth
            # changing, and a preview cut where the sentence turns can invert
            # what the entry appears to say. Bounded still — this is a table —
            # but the value names both sizes when it lost anything.
            from . import pk        # local, as everywhere in this module
            row["text"] = pk.cut_marked(
                entry.get("gloss") or entry.get("statement") or "", 400)
            row["project"], row["scope_how"] = _scope(entry)
            row["actions"] = _actions(entry)
        rows.append(row)
    rows.sort(key=lambda r: (-r["bytes"], -r["fires"], r["id"]))
    return rows, derived, missing


def turns(rows, cap=TURN_SAMPLES):
    """-> the last `cap` per-turn rows of one window, newest last."""
    out = []
    for row in rows[-cap:]:
        sample = row.get("sample") or {}
        out.append({
            "turn": row.get("turn"), "ts": row.get("ts"),
            "bytes": sample.get("rendered_bytes") or 0,
            "lanes": {lane: (sample.get("lane_bytes") or {}).get(lane) or 0
                      for lane in injection_schema.V2_LANES},
            "fired": {lane: [str(i) for i in
                             (row.get("fired") or {}).get(lane) or ()]
                      for lane in FIRED_LANES},
            "suppressed": len(row.get("suppressed") or ())
                          + len(row.get("suppressed_pinned") or ()),
            "silent": row.get("silent") is True,
        })
    return out


def _rows_for(session, rows=None):
    """-> (this session's exact rows oldest-first, unavailable-name)."""
    if rows is not None:
        return [r for r in rows if _exact(r) and r.get("session") == session], None
    try:
        from . import inject
        all_rows = inject._ledger_rows(strict=True)
    except Exception:
        return [], "ledger"
    return [r for r in all_rows
            if _exact(r) and r.get("session") == session], None


def _bind(seat, session):
    """-> (seat label, session, project, unavailable-name).

    THE BINDING IS THE CONFIG VIEW'S, NOT A SECOND ONE. `injection_config`
    already resolves a seat name to the session the roster believes it holds,
    and two resolvers for one question is how the two panes of one page come
    to disagree about which seat the owner is looking at.
    """
    try:
        from . import injection_config
        label, roster, err = injection_config._roster_row(seat, session)
    except Exception:
        return seat, session, None, "roster"
    if not session and roster:
        session = roster.get("session")
    return (label or seat), session, (roster or {}).get("project"), err


def view(seat=None, session=None, rows=None, cap=TURN_SAMPLES):
    """-> the owner's pack model for one seat/session. Never raises: an
    unreadable source is reported as unavailable, because a page that 500s
    teaches the owner nothing about what is in his context."""
    seat = str(seat or "").strip() or None
    session = str(session or "").strip() or None
    seat, session, project, bind_err = _bind(seat, session)
    unavailable = [name for name in (bind_err,) if name]
    out = {"requested": {"seat": seat, "session": session},
           "session": session, "project": project, "state": "unknown",
           "window": None, "session_total": None, "windows": 0,
           "entries": [], "turns": [], "unavailable": unavailable,
           "derived": 0, "unattributed": 0, "derived_note": "", "why": ""}
    if not session:
        out["why"] = ("no session bound to this seat yet — the pack is "
                      "recorded per session, so there is nothing to read")
        return out
    mine, ledger_err = _rows_for(session, rows)
    if ledger_err:
        unavailable.append(ledger_err)
    if not mine:
        out["why"] = ("no injection recorded for this session — either it has "
                      "not taken a turn yet or its hook is not wired")
        return out
    segments = windows(mine)
    current = fold(segments[-1])
    whole = fold(mine)
    rows_out, derived, store_err = entries(current, project)
    if store_err:
        unavailable.append(store_err)
    out.update({
        "state": "observed", "windows": len(segments),
        "window": _public(current, len(segments) - 1, len(segments)),
        "session_total": _public(whole, None, len(segments)),
        "entries": rows_out, "turns": turns(segments[-1], cap),
        "derived": derived,
        "unattributed": unattributed(derived, current["bytes"]),
        "derived_note": _derived_note(derived, current["bytes"]),
        "scope": scope_split(rows_out, project),
    })
    return out


def scope_split(rows, project):
    """-> what this window cost, split by whether the entry is ABOUT the
    project the seat is working in.

    THIS IS THE LEVER THE LEDGER ACTUALLY POINTS AT, and it is visible in the
    live data rather than inferred: a seat working in one project receives
    every entry that records no project at all, because FLEET means owner
    policy that applies everywhere. Some of that is right -- a rule about how
    the owner works travels with him. Some of it is task/2435, an entry about
    another codebase riding along because nobody ever recorded what it was
    about. The page cannot tell those apart and does not try; it reports the
    split and gives the owner the door (`rescope`) to decide one row at a time.

    `off_project` is a DIFFERENT and stronger reading: an entry recorded as
    being about some OTHER project should not reach this seat at all, because
    load_entries fences by scope. A non-zero count is evidence about the fence,
    not about the entry, so it is reported separately instead of being folded
    into the fleet number it would otherwise inflate.
    """
    try:
        from .store import FLEET
    except Exception:
        FLEET = "fleet"
    fleet = [r for r in rows if r["present"] and r["project"] == FLEET]
    off = [r for r in rows if r["present"] and r["project"]
           and r["project"] not in (FLEET, project)]
    owned = [r for r in rows if r["present"] and project
             and r["project"] == project]
    return {"project": project, "fleet": len(fleet),
            "fleet_bytes": sum(r["bytes"] for r in fleet),
            "owned": len(owned), "owned_bytes": sum(r["bytes"] for r in owned),
            "off_project": len(off),
            "off_project_bytes": sum(r["bytes"] for r in off),
            "off_project_ids": [r["id"] for r in off[:8]]}


def _public(win, index, of):
    """One window rendered for a reader: the folded counts plus the figures
    derived from them, resolved HERE so a consumer reads the same repeat rate
    this module's own text used instead of recomputing it and diverging."""
    out = {k: v for k, v in win.items() if k != "ids"}
    out.update({"distinct": len(win["ids"]), "repeat": repeat(win),
                "per_turn": per_turn(win), "index": index, "of": of})
    return out


# The undercount band. Set ABOVE the measured population (0.6-8.7% across the
# six busiest live sessions) so a breach means something CHANGED rather than
# that the window contains its ordinary share of id-less lines. Raising it
# without re-measuring turns it into a number that only ever agrees.
UNDERCOUNT_BAND = 0.25


def unattributed(derived, ledgered):
    """-> the window bytes no entry id accounts for, never negative.

    NOT AN ERROR TERM. These are real lines the seat really received -- the
    brief whisper, reflex steers, gate riders, and the newline between every
    pair -- which simply carry no id to hang a row on. Naming them keeps the
    per-entry table honest about being a table of the ATTRIBUTED share.
    """
    return max(0, (ledgered or 0) - (derived or 0))


def _derived_note(derived, ledgered):
    """The CONTROL, in words, for the two readings the model calls impossible.

    A ROUTINE UNDERCOUNT IS NOT ONE OF THEM and is reported as `unattributed`
    instead: a note printed on every run is a note nobody reads on the run that
    matters, and a band drawn through the middle of the healthy population
    would print on every run.
    """
    if not ledgered:
        return ""
    if derived > ledgered:
        # Per-entry lines cannot outweigh the delivery that contained them, so
        # this is never a big-or-small question -- any amount is the model
        # being wrong about something.
        return ("the per-entry costs sum to %d B, MORE than the %d B the "
                "ledger recorded for this whole window — the per-entry "
                "derivation is counting something the delivery did not carry"
                % (derived, ledgered))
    if (ledgered - derived) / float(ledgered) > UNDERCOUNT_BAND:
        return ("only %d B of this window's %d B could be attributed to an "
                "entry — far beyond the id-less lines that normally account "
                "for the difference, so entries whose text changed since they "
                "fired, or which the store no longer holds, are being priced "
                "at today's length or at zero"
                % (derived, ledgered))
    return ""


# ---------------------------------------------------------------------------
# the edit side: the owner clicks, helm runs the writer
# ---------------------------------------------------------------------------

# EVERY ACTION GOES THROUGH THE STORE'S OWN WRITER and none of them touches a
# file under ~/.helm directly. An entry's tier and scope are canon ABOUT canon:
# they get the same _commit path, the same validation and the same event
# receipt as every other write, so a change made from the browser is
# indistinguishable in the record from one made at the CLI.
ACTIONS = ("demote", "undemote", "rescope", "retire")


def act(action, eid, reason="", owner=None, project=None):
    """-> (result, error). One owner click, one store write."""
    action = str(action or "").strip()
    eid = str(eid or "").strip()
    if action not in ACTIONS:
        return None, "unknown action %r (one of: %s)" % (action,
                                                         ", ".join(ACTIONS))
    if not eid:
        return None, "need the entry id"
    reason = str(reason or "").strip()
    if action in ("demote", "undemote", "retire") and not reason:
        # THE WRITERS DEMAND A REASON FOR A TIER FLIP and refusing here rather
        # than at the writer means the refusal names the button the owner
        # pressed instead of an internal argument he never supplied.
        return None, "a reason is required — say why in one line"
    try:
        from . import pk
        from .store import write
    except Exception as exc:
        return None, "store unavailable: %s" % exc
    ts = pk.now_ts()
    if action in ("demote", "undemote"):
        entry, err = write.demote(eid, ts, reason, project=project,
                                  undo=action == "undemote")
    elif action == "rescope":
        entry, err = write.rescope(eid, ts, "" if owner in (None, "-") else
                                   str(owner), project=project)
    else:
        entry, err = write.retire(eid, ts, reason, project=project)
    if err:
        return None, err
    return {"id": str(entry["id"]), "action": action,
            "type": entry.get("type"),
            "status": entry.get("status"),
            "load_class": entry.get("load_class"),
            "project": str(entry.get("project") or "")}, None
