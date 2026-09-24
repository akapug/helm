"""Core projections for :mod:`helm.web`."""
import sys
# EXPLICIT, THOUGH THE GLOBALS SPLICE BELOW WOULD ALSO SUPPLY IT. A route that
# stamps a read_at onto its payload must not depend on whichever modules
# `helm.web` happened to import; the name it calls is named here.
import time
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import calendar
import json

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})


# One store entry projects to these keys on the wire — the strip never needs bodies.
# `scope` is WHERE THE ENTRY IS KEPT and `project` is WHAT GOVERNS ITS
# INJECTION; they differ for exactly the entries someone has rescoped, so a
# reader given only the first cannot tell a classified row from an unclassified
# one. Both ride, additively: `scope` keeps its meaning for every existing
# reader and `project` answers the question the CLI surfaces now answer too.
ENTRY_KEYS = ("id", "type", "confidence", "load_class", "scope", "project")

ENTRY_CAP = 500

REVIEW_STMT_CAP = 400  # the review panel shows the statement head, not the body



# ── WHAT THE PROJECT LIST IS ALLOWED TO COST A PHONE ─────────────────────────
# MEASURED on the owner's own registry: /api/registry shipped 85,266 bytes over
# 234 projects. Two whole fields in that are read by NOTHING on the page.
#
# `state` is the stronger case of the two, because the page is FORBIDDEN to
# read it: `light()` in the shipped script says so in as many words — "Reading
# `p.state` off the merged record would let a projection byte pass for the
# owner's word". `light` beside it is the resolver's answer and is what every
# renderer takes. Serving `state` anyway shipped the raw block next to its own
# resolution and invited exactly the read the page refuses to make.
#
# `kind` ("git"/"dir"/"external") has no reader at all — not a board row, not
# the detail pane, not a filter, not a chip.
REGISTRY_UNREAD_KEYS = ("state", "kind")


# ── AND WHAT ONLY AN OPEN ROW DRAWS ──────────────────────────────────────────
# After the two drops above the list still shipped 80,149 bytes over the same
# 234 projects, and the rest of it is DRAWN — but not by the row. The project
# card grid (retired for the burn board, task/2975) drew the pane for every
# project at render time, so nine more fields have live readers that only
# ever run once the owner has opened one project:
#     first_seen 7,378 · cv_scope 2,584 · edges 2,338 · cwds 1,364 ·
#     memory_dir 1,114 · harness_refs 1,049 · home 652 · active_days 293 ·
#     notes 235
# That is ~17 KB of panes for the 233 projects he did not open, on every load
# of the home tab. The cure is the shape `/api/task/notes` already set for task
# prose: a cheap list, and one row's own weight on its own route when a reader
# asks for it.
#
# THE CARD KEYS ARE THE CLOSED READER SET, not a guess. The project record
# reaches the browser through exactly one door — `projects = Object.values(
# reg.projects)` — and from there through the burn board's rows and the
# sessions tab. Every property access on a project across them: `name`,
# `path`, `status`, `retired`, `last_seen`, `sessions`, `light` (the board row,
# its sort and filter, the home stats, the sessions tab's scope list), `edges`
# (a LENGTH, the open row's lineage mark), and the nine above (the open row's
# pane alone). `retired` is on this list though no project in the owner's
# registry carries it today — `renderBoard()` filters on it, and a key with a
# reader stays whether or not the data happens to exercise it.
REGISTRY_CARD_KEYS = ("name", "path", "status", "retired", "last_seen",
                      "sessions", "light")

# WHERE THE REST OF A ROW LIVES — ON THE PAYLOAD, ONCE, NOT ON EVERY ROW.
# It rides beside `generated_ts`, which the footer already reads off `reg`
# rather than off a project. The first cut stamped it onto all 234 rows and
# MEASURED THE COST: 32 bytes × 234 = 7.5 KB, which ate the entire trim it was
# announcing. A fact about the door is not a fact about a row.
#
# IT IS ALSO THE VERSION HANDSHAKE. A page served by a door that still sends
# whole records sees no `detail_route`, and draws the pane straight from the
# row exactly as it always did. So "detail is elsewhere" is something the
# browser is TOLD, never something it assumes — the one reading that could
# otherwise turn a full row into a blank pane.
REGISTRY_DETAIL_ROUTE = "/api/project/detail"


def _project_on_the_wire(rec, lit):
    """One project row as the board draws it, stamped with its resolved light.

    A DROP, NOT A TRUNCATION: no field here is served shorter than it is. Every
    key the ROW reads is carried whole, so there is nothing a reader could
    mistake for a complete answer. The keys missing are of exactly two kinds,
    and the row names both rather than leaving a reader to infer them: ones no
    reader asks for at all (`REGISTRY_UNREAD_KEYS`), and ones only an OPEN row
    reads — those are not gone, they are at `detail`, which rides on the row.
    A new dict is built rather than the loaded record mutated, so this handler
    cannot narrow what the in-process readers of `registry.load` see.

    `light.key` STAYS, and that is a measured refusal rather than an oversight:
    it is 5,944 of these bytes and it repeats the row's own name on every
    project, and the shipped page would compute it back from `p.name` through
    the fallback it already has. It has a READER — the projects-verb suite
    asserts the resolved light is handed over WITH its key — and a field with a
    reader is not a field with no reader, however derivable it looks."""
    row = {k: v for k, v in rec.items()
           if k in REGISTRY_CARD_KEYS and k not in REGISTRY_UNREAD_KEYS}
    # THE LINEAGE MARK IS A COUNT, NOT THE EDGES. `lineageN()` asks only
    # whether there is any lineage at all, while the pane below it renders
    # every edge's rel, target and note. Serving the array to
    # answer a yes/no is the same shape `/api/tasks` already refused for
    # comments, and it answers it the same way: a number on the list, the rows
    # themselves one fetch away. `edges_n` is a DIFFERENT NAME on purpose — a
    # shortened `edges` would be an array a reader could iterate and believe.
    #
    # AND IT IS ABSENT AT ZERO, WHICH IS MEASURED, NOT TIDINESS: 10 of the
    # owner's 234 projects have any lineage at all, so `"edges_n": 0` on the
    # other 224 cost more bytes than the arrays it replaced — the trim would
    # have been a net LOSS. Absence is the registry's own word for no lineage
    # (a project with none carries no `edges` key either), and it is the exact
    # thing `(p.edges_n || 0)` already reads. No count can be mistaken for the
    # rows, and no absence for a count that was withheld.
    if rec.get("edges"):
        row["edges_n"] = len(rec["edges"])
    if lit is None:
        # A PROJECT THE RESOLVER DID NOT ANSWER FOR keeps having no `light`,
        # which is the one state the page's own fallback is written for. A
        # light stamped here out of nothing would be a colour with no reading
        # behind it, drawn as if someone had decided it.
        row.pop("light", None)
        return row
    row["light"] = dict(lit)
    return row


def _project_detail_on_the_wire(rec):
    """Everything an OPEN board row draws and a closed one never does -> dict.

    THE COMPLEMENT OF THE ROW, NOT A SECOND HAND-PICKED LIST. Anything that
    is neither a card key nor an unread key belongs here, so a field added to
    the registry tomorrow reaches this route by default rather than falling
    down the gap between two allowlists and silently ceasing to be served.

    AN EMPTY DICT IS AN ANSWER. Most projects carry none of these keys at all,
    and `{}` from a door that found the row is "there is nothing else on file"
    — a different fact from a fetch that never landed, which is why the caller
    is handed this under its own key rather than merged into the row."""
    return {k: v for k, v in rec.items()
            if k not in REGISTRY_CARD_KEYS and k not in REGISTRY_UNREAD_KEYS}


def _api_registry():
    """The merged registry, each project stamped with its resolved `light`.

    The page draws the light and must not decide it: authorship lives in the
    authored layer, which the merged record alone cannot prove (an inline
    `state` block in the projection looks identical). So the one resolver
    answers here and the board row renders what it is handed.
    """
    try:
        # STRICT, BECAUSE THIS IS A REQUEST HANDLER. A strict load is the one
        # spelling that cannot write, cannot migrate and cannot create a lock
        # sidecar, so a page refresh can never be the thing holding the fleet's
        # registry flock (task/2703). The except below already answers an
        # unreadable registry with an empty one, which is what strict adds: a
        # malformed population now fails closed here instead of being drawn.
        reg = registry.load(strict=True)
        lights = registry.lights(reg)
        projects = {}
        for key, rec in (reg.get("projects") or {}).items():
            lit = lights.get(key)
            projects[key] = _project_on_the_wire(
                rec, None if lit is None else dict(lit, key=key))
        return dict(reg, projects=projects,
                    detail_route=REGISTRY_DETAIL_ROUTE)
    except (OSError, ValueError):
        return {"version": 1, "projects": {}}



def _api_project_detail(qs):
    """ONE project's pane, in FULL, fetched when its board row opens -> (obj,
    status).

    ON DEMAND, NOT ON THE LIST — the shape `/api/task/notes` set, one surface
    over. The retired card grid drew this pane for all 234 projects at paint
    time, so every pane was paid for 234 times on every load of the home tab
    whether or not one was ever opened. The row keeps what it draws and asks
    for one project's pane when the owner opens it.

    PARTIAL MUST NOT READ AS COMPLETE, and that is the whole reason `detail` is
    a key of its own instead of the fields being spread across the reply:
      · `{"detail": {...}}` — read, and this is what is on file
      · `{"detail": {}}`    — read, and there is nothing else on file
      · 404                 — no such project; nothing was read
      · no reply at all     — the browser never got here, and its pane says so
    The second and the fourth are the pair a merged shape could not tell apart,
    and they are the pair that matters: "this project has nothing else" and "I
    could not find out" must never draw the same empty pane.

    NEVER TRUNCATED. Notes and lineage are prose and lists the owner is reading
    to make a decision; a pane that stops early is the defect this route exists
    to cure, one layer down."""
    key = str((qs.get("name") or [""])[0]).strip()
    if not key:
        return {"error": "name is required"}, 400
    # ONLY THE REGISTRY READ IS WRAPPED, AND THE SCOPE IS THE POINT. A
    # malformed record or a serialization bug below is MY fault with the
    # registry perfectly readable, and reporting that as "cannot see the
    # registry" sends the owner to check his own files while the fault is
    # here. `unavailable` is a claim about the STORE and nothing else may
    # borrow it; a bug past this line raises and the server answers 500.
    try:
        # STRICT for the same reason `_api_registry` is strict: a request
        # handler must not write, migrate, or mint a lock sidecar.
        reg = registry.load(strict=True)
    except (OSError, ValueError) as exc:
        return {"unavailable": True, "why": str(exc) or "registry unreadable"}, 200
    rec = (reg.get("projects") or {}).get(key)
    if rec is None:
        # THE KEY IS THE REGISTRY KEY, which is what `light.key` carries and
        # what the light-setter POSTs — the same spelling on both doors, so a
        # row can never open one project's pane and set another's colour.
        return {"error": "%s is not a project in this registry" % key}, 404
    return {"name": key, "detail": _project_detail_on_the_wire(rec)}, 200


def _api_projects_state(payload):
    """Set a project's light from its board row, GUI-first (the owner does not
    run CLI commands): the same one-writer-path law as the task and decision doors
    — this routes through the exact `registry.state` that `helm projects state`
    calls, so every refusal the CLI has, the page has, in the same words."""
    name = str(payload.get("name") or "").strip()
    colour = str(payload.get("colour") or "").strip()
    if not name or not colour:
        return {"error": "name and colour are required"}, 400
    try:
        row, problem = registry.state(name, colour,
                                      reason=str(payload.get("reason") or "").strip(),
                                      by="owner", apply=True)
    except (OSError, ValueError) as exc:
        return {"error": "registry UNKNOWN: %s" % exc}, 500
    if problem:
        return {"error": problem}, 400
    return {"ok": True, "name": row["name"], "was": row["was"],
            "state": row["state"]}, 200


def _api_friction():
    """The guard-refusal card: `helm friction`'s own census beside the owner's
    dial. The page draws these and computes neither: the census is
    `friction.report` over the verb's default window, so the card and the
    command line cannot print different numbers, and an unreadable ledger
    arrives as `unreadable` with a null total, never as zero."""
    from . import friction
    return {"report": friction.report(days=friction.WINDOW_DAYS),
            "dial": friction.dial()}


def _api_friction_dial(payload):
    """Turn the dial from its card, through the exact `friction.set_dial` that
    `helm friction dial N` calls, so the page refuses what the verb refuses in
    the same words.

    `expected` is the number the card was showing when he pressed. A press made
    against a number that has since moved is answered 409 `stale` with the dial
    as it now stands, and writes nothing: two tabs, or a seat at the verb, must
    not turn one press into a jump he never saw. NOTHING HERE ANSWERS `ok`
    WITHOUT A WRITE THAT LANDED AND READ BACK."""
    from . import friction
    value, expected = payload.get("value"), payload.get("expected")
    if expected is not None and friction._dial_value(expected) is None:
        return {"error": friction.DIAL_REFUSAL, "code": "refused"}, 400
    try:
        row, problem, code = friction.set_dial(value, by="owner",
                                               expected=expected)
    except (OSError, ValueError) as exc:
        # `detail` is for whoever repairs the file; the sentence is his
        return {"error": "The number was not saved, because the saved settings "
                         "could not be read. An agent can repair the settings "
                         "file.",
                "detail": "%s: %s" % (type(exc).__name__, exc),
                "code": "failed"}, 500
    if problem:
        return {"error": problem, "code": code, "dial": friction.dial()}, \
            (409 if code == "stale" else 400)
    now = row["dial"]
    if now["problem"] or now["value"] != value:
        return {"error": "The number was written, but reading it back did not "
                         "show it, so it may not be in use. %s"
                         % (now["problem"] or ""),
                "code": "stale", "dial": now}, 409
    return {"ok": True, "was": row["was"], "dial": now}, 200



def _api_posture():
    """The owner's away card (task/3018): his away flag and his fleet notice,
    through the one read `helm away status` and the per-turn inject make
    (ownernotice.web_model). An unreadable flag or notice arrives as
    `unknown` with its reason, never as "here" or "no notice"."""
    from . import ownernotice
    return ownernotice.web_model()


#: What each press says back to him, by the writer's own outcome word. Plain
#: sentences: he reads this card, he does not decode it.
_POSTURE_SAID = {
    ("away", "away"): "You are marked away. Agents learn it at their next "
                      "turn; nobody was woken.",
    ("away", "already"): "You were already marked away.",
    ("back", "back"): "Welcome back. Agents learn it at their next turn.",
    ("back", "already"): "You were not marked away.",
    ("notice", "set"): "Notice saved. Agents see it once, at their next turn; "
                       "nobody was woken.",
    ("clear", "cleared"): "Notice cleared. Agents are told once, at their "
                          "next turn.",
    ("clear", "already"): "There was no notice to clear.",
}


def _api_posture_post(payload):
    """One press on the away card: {"action": "away" | "back" | "notice" |
    "clear", "text": "..."} for a notice.

    THE OWNER BY DESIGN: this handler is his browser door, so it mints the
    OwnerDoor that away.declare and ownernotice require; no other production
    code may (the census in tests/test_ownerdecisions.py). The bearer and the
    loopback origin are checked before this runs, and the argv-guard refuses
    an agent's own POST here, as it does for the decision door.

    NOTHING IS WOKEN. The writes are two files beside each other; no chat row
    is posted, no seat is mentioned, and the per-turn inject carries the
    change to each seat at the turn it was already going to take. Every answer
    carries the card as it now reads, so the page never draws a press as saved
    that the files do not show."""
    from . import away, ownerasks, ownernotice
    action = str(payload.get("action") or "").strip()
    door = ownerasks.owner_door("web")
    if action == "away":
        outcome, detail = away.declare(door=door)
    elif action == "back":
        outcome, detail = away.lift()
    elif action == "notice":
        rec, err = ownernotice.write_notice(payload.get("text"), door)
        outcome, detail = ("refused", err) if err else ("set", None)
    elif action == "clear":
        outcome, detail = ownernotice.clear_notice(door)
    else:
        return {"error": 'action must be "away", "back", "notice" or "clear"',
                "code": "refused", "posture": ownernotice.web_model()}, 400
    model = ownernotice.web_model()
    if outcome == "refused":
        detail = str(detail or "the writer gave no reason")
        return {"error": "Not saved. %s%s." % (detail[:1].upper(),
                                               detail[1:].rstrip(".")),
                "code": "refused", "posture": model}, 400
    return {"ok": True, "outcome": outcome,
            "said": _POSTURE_SAID.get((action, outcome), "Saved."),
            "posture": model}, 200



def _api_store():
    """Typed-store summary: counts + a bounded entry projection. The store
    module lands in a parallel lane — absent/raising -> unavailable, never 500."""
    try:
        from . import store
        per_root = store.counts()
        by_type = {}
        for row in per_root.values():
            if isinstance(row, dict):
                for t, n in row.items():
                    if isinstance(n, int):
                        by_type[t] = by_type.get(t, 0) + n
        out = {"counts": {"by_type": by_type, "total": sum(by_type.values())},
               "per_root": per_root}
        for name in ("entries", "list_entries", "all_entries", "scan"):
            fn = getattr(store, name, None)
            if not callable(fn):
                continue
            rows = [e for e in fn() if isinstance(e, dict)]
            kept = rows[:ENTRY_CAP]
            out["entries"] = [{k: e.get(k) for k in ENTRY_KEYS} for e in kept]
            # THE WINDOW TRAVELS WITH THE LIST. A served array cut at a cap
            # reads to its consumer as the whole store, and `counts` cannot
            # stand in for it: that number comes from a different producer
            # and the two can disagree. Same shape as the owed projection —
            # total, shown, truncated — so one reader learns one spelling.
            out["entries_window"] = {"total": len(rows), "shown": len(kept),
                                     "truncated": len(rows) - len(kept),
                                     "cap": ENTRY_CAP}
            break
        json.dumps(out)  # unserializable shapes degrade too
        return out
    except Exception:
        return {"unavailable": True}



def _api_store_review():
    """The owner store-review queue: candidate (fires NOTHING) + provisional
    (xrev-cleared, fires WITH a [provisional] tag) entries — the browse/approve/
    reject surface near the configs view (owner canon: the technical auto-learns
    may go provisionally live once a cross-family review clears them, and the
    owner still gets a way to ratify/reject in the WEB UI). Same graceful degrade
    as the store: absent/raising -> unavailable, never 500."""
    try:
        from . import pk, store
        rows = []
        for e in store.reviewable():
            rows.append({
                "id": str(e.get("id") or ""),
                "type": e.get("type") or "",
                "status": e.get("status") or "",
                "statement": pk.cut_marked(e.get("statement") or "",
                                           REVIEW_STMT_CAP),
                "source": e.get("source") or "",       # captured-by (inferred/asked/explicit)
                "captured_ts": str(e.get("stated_ts") or e.get("updated_ts") or ""),
                "scope": e.get("scope") or "",
                "confidence": e.get("confidence"),
                "xrev_by": e.get("xrev_by") or "",     # who attested the /x review
                "xrev_ts": e.get("xrev_ts") or "",
            })
        counts = {"candidate": 0, "provisional": 0}
        for r in rows:
            if r["status"] in counts:
                counts[r["status"]] += 1
        out = {"entries": rows, "counts": counts}
        json.dumps(out)  # unserializable shapes degrade too
        return out
    except Exception:
        return {"unavailable": True}



def _api_store_confirm(payload):
    """Owner ratify: candidate/provisional -> live, through the SAME store.confirm
    the CLI calls (one writer path, atomic-write rails inside the store). id
    required; optional statement (an inline --edit) + type (disambiguate a slug
    shared across reviewable types)."""
    from . import pk, store
    eid = str(payload.get("id") or "").strip()
    if not eid:
        return {"error": "id is required"}, 400
    e, err = store.confirm(eid, pk.now_ts(),
                           new_statement=(payload.get("statement") or None),
                           project=(payload.get("project") or None),
                           ctype=(payload.get("type") or None))
    if err:
        return {"error": err}, 400
    return {"ok": True, "id": str(e["id"]), "type": e["type"],
            "status": e["status"]}, 200



def _api_store_reject(payload):
    """Owner reject: candidate/provisional -> retired IN PLACE (the record law:
    the file stays), through the SAME store.reject the CLI calls. id required;
    optional reason (the reject reason box) + type."""
    from . import pk, store
    eid = str(payload.get("id") or "").strip()
    if not eid:
        return {"error": "id is required"}, 400
    e, err = store.reject(eid, pk.now_ts(),
                          why=str(payload.get("reason") or "").strip(),
                          project=(payload.get("project") or None),
                          ctype=(payload.get("type") or None))
    if err:
        return {"error": err}, 400
    return {"ok": True, "id": str(e["id"]), "type": e["type"],
            "status": e["status"], "retired_why": e.get("retired_why") or ""}, 200



DECISION_CTX_CAP = 4000   # the queue serves the WHOLE context (it is the point)


# The GET-side loss projection is RECENCY-BOUNDED. `delivered` binds to the
# ledger; the DM row is tmpfs evidence, and lanes ROTATE — for a card
# delivered months ago, a vanished row is overwhelmingly a seen-then-rotated
# one, and flagging it would flood the queue with stale re-deliver noise
# forever (SENT != SEEN cuts both ways: absence of old evidence is not
# evidence of non-receipt). So the queue re-verifies evidence only for cards
# delivered inside this window; older losses stay reachable through
# `helm decide show` (which warns read-only) and `helm decide deliver`.
DECISION_LOSS_WINDOW_S = 48 * 3600



def _decision_recent(r, now):
    ts = str(r.get("last_updated") or r.get("ts") or "")
    try:
        t = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return False                       # unparseable age: not provably recent
    return (now - t) <= DECISION_LOSS_WINDOW_S



def _api_decisions():
    """The owner DECISION queue (ownerasks generalized): every card the fleet
    filed that the owner has not answered — open cards, decided-but-
    undelivered ones (a verdict whose DM failed must stay VISIBLE, or the
    asker waits forever on a ruling that exists), and recently-delivered
    cards whose DM EVIDENCE IS GONE (dm_lost — r3: without this
    projection a reboot-eaten DM left the browser no way to discover or
    retry the loss; the retry button is the recovery path, and the POST it
    fires reopens durably before resending). Same graceful degrade as the
    store surfaces: absent/raising -> unavailable, never 500."""
    try:
        from . import ownerasks
        current, unavailable = ownerasks.decisions_snapshot()
        if unavailable:
            return {"unavailable": True, "why": str(unavailable)}
        now = time.time()
        rows = []
        for r in current.values():
            lost = (r.get("status") == "delivered"
                    and _decision_recent(r, now)
                    and not ownerasks._dm_row_present(r))
            if r.get("status") != "delivered" or lost:
                rows.append((r, lost))
        rows.sort(key=lambda p: (str(p[0].get("ts") or ""),
                                 str(p[0].get("id") or "")))
        entries = [{
            "id": str(r.get("id") or ""),
            "title": str(r.get("title") or ""),
            "context": str(r.get("context") or "")[:DECISION_CTX_CAP],
            "options": [{"key": str(o.get("key") or ""),
                         "label": str(o.get("label") or ""),
                         "consequence": str(o.get("consequence") or ""),
                         "recommended": bool(o.get("recommended"))}
                        for o in (r.get("options") or []) if isinstance(o, dict)],
            "asker": str(r.get("asker") or ""),
            "refs": [str(x) for x in (r.get("refs") or [])],
            "status": str(r.get("status") or ""),
            "ts": str(r.get("ts") or ""),
            "verdict": r.get("verdict") or None,
            "comments": len(r.get("comments") or []),
            "dm_lost": lost,
        } for r, lost in rows]
        counts = {"open": sum(1 for e in entries if e["status"] == "open"),
                  "undelivered": sum(1 for e in entries
                                     if e["status"] == "decided"
                                     or e["dm_lost"])}
        out = {"entries": entries, "counts": counts}
        json.dumps(out)   # unserializable shapes degrade too
        return out
    except Exception:
        return {"unavailable": True}



# The longest a note's first line may reach on THIS wire. Generous enough that
# a real opening sentence survives whole, small enough that 477 rows of them
# stay a payload rather than a download.
_TASK_NOTE_LINE = 240


def _task_last_note(tasks, row):
    """One row's newest comment as {ts, by, line}, or None when it has none.

    THROUGH THE STORE'S OWN READER when it publishes one — `tasks.last_note`
    owns which end of `comments[]` is newest, the same settled-contract rule
    this route already follows for `origin_of` and the rank key. The inline
    form is the LAND-ORDER fallback this surface is built on, and it is field
    normalization only: last element, no second opinion about order.
    """
    reader = getattr(tasks, "last_note", None)
    note = reader(row) if callable(reader) else next(
        (c for c in reversed(list(row.get("comments") or ()))
         if isinstance(c, dict)), None)
    if not isinstance(note, dict):
        return None
    text = str(note.get("text") or "")
    line = text.split("\n", 1)[0].strip()[:_TASK_NOTE_LINE]
    return {"ts": str(note.get("ts") or "") or None,
            "by": str(note.get("by") or "") or None,
            "line": line or None}


# ── what this list may weigh on a phone ────────────────────────────────────
# MEASURED, on the owner's own ledger, before any of the three bounds below
# existed: 2,772 rows, 4,361,132 bytes of JSON on ONE call, no paging and no
# field trim. Server time was 0.43-0.59s, so this was never seconds — it was
# BYTES, and bytes are what a phone on a cellular link actually pays for.
# The census of that payload, per field over all rows:
#     note 2,208,812 (50.1%) · title 417,800 · closed_reason 362,153 ·
#     last_note 254,210 · refs 167,134 · source 129,811 · last_updated 107,352
# Three bounds answer it, and each one is VISIBLE on the wire rather than a
# silent cut — the law this endpoint is under is that a reader must never
# mistake a short answer for a complete one.

# ONE: the row's own `note` is unbounded prose, and this list carries EVERY
# row. That is the exact growth `_api_task_notes` was built to refuse one
# field over ("ON DEMAND, NOT ON THE LIST") — `comments` was moved off this
# payload for it while `note`, four times heavier, stayed. The list now
# carries a bounded opening and says so with `note_more`; the whole text is
# one fetch away on `/api/task/notes`, where the row's other prose already
# lives. Measured on the live ledger: 1,726 of 1,917 live rows carry a note,
# median 316 chars, p90 2,040, longest 12,886.
_TASK_NOTE_CAP = 240

# AND THE CUT IS NEVER MID-WORD. A sentence that stops inside a word reads as
# a rendering fault; one that stops at a space reads as an excerpt, which is
# what it is. The trailing partial word is dropped only when a space exists
# late enough in the window to leave most of the excerpt standing.
_TASK_NOTE_CAP_FLOOR = 160

# TWO: a CLOSED row renders as a tombstone — id, title and why it closed —
# and nothing else on it is ever drawn. Serving the other fourteen fields for
# 855 closed rows cost 1,674,369 bytes to render 532,189 bytes of tombstone.
# The trimmed keys are ABSENT rather than null, because null is this file's
# word for UNKNOWN and a closed row's age is perfectly well known — it is
# simply not on this wire. `tombstone: true` is what says so, so no reader
# can read an absent field as an unknown one.
_TASK_TOMBSTONE_FIELDS = ("id", "title", "status", "closed_reason", "project")

# THREE: two fields nobody reads. `source` and `last_updated` are serialized
# on every row and consumed by NOTHING — no renderer under web_ui, no
# assertion in the suites. 234,391 bytes of wire for a field with no reader.
_TASK_WIRE_DROP = ("source", "last_updated")


def _task_note_excerpt(text):
    """A row's note bounded to `_TASK_NOTE_CAP` -> (text, more?).

    `more` IS THE WHOLE CONTRACT: False means THAT IS ALL OF IT and True means
    THERE IS MORE, and it rides every live row so its absence can never be
    read as either. A note that fits is returned untouched and flagged False —
    the excerpt path and the whole-note path answer the same field, so a
    reader never has to guess which one it got.
    """
    text = text or ""
    if len(text) <= _TASK_NOTE_CAP:
        return text, False
    cut = text[:_TASK_NOTE_CAP]
    space = cut.rfind(" ")
    return (cut[:space] if space >= _TASK_NOTE_CAP_FLOOR else cut), True


def _task_wire_row(entry):
    """One projected row, bounded for the wire — see the three bounds above.

    APPLIED AT EMIT AND NOWHERE EARLIER. `counts`, the project scoping and
    `tasks.queue_totals` all read the FULL projection, so every number this
    payload carries is computed over exactly the rows it always was; only the
    bytes leaving the process are bounded. Trimming before the counts would
    have made the headline describe a population the trim invented.
    """
    if entry.get("status") == "closed":
        row = {k: entry[k] for k in _TASK_TOMBSTONE_FIELDS if k in entry}
        row["tombstone"] = True
        return row
    row = {k: v for k, v in entry.items() if k not in _TASK_WIRE_DROP}
    excerpt, more = _task_note_excerpt(row.get("note"))
    row["note"], row["note_more"] = (excerpt or None), more
    return row


def _api_tasks():
    """The team task BACKLOG (#218 Half D) — the second section the work
    tab's socket was built for (worksections' recorded intent). Renders the
    task ledger the fleet files into; NEVER the owner-gate queue — the home
    badge counts decisions only, twice-enforced (this surface never feeds
    board_queue, and helm.tasks exports no board projection at all; the
    19-row collapse at ownerasks.py:687-691 is the precedent both guards
    answer). Land-order independent: helm.tasks may land after this surface,
    and absent -> unavailable, never 500 and never an empty-looking backlog.
    THE STATE TRIAD IS LOAD-BEARING: empty means "no tasks"; unavailable
    means "cannot SEE the tasks" — drawing them the same tells the owner the
    fleet is idle at the exact moment it lost the ability to answer."""
    try:
        try:
            from . import tasks
        except ImportError:
            return {"unavailable": True,
                    "why": "helm.tasks not landed on this trunk yet",
                    "read_at": None, "queue": None,
                    "entries": [], "unscoped": [], "counts": {},
                    "withheld_foreign": 0, "project": None}
        # ONE READ, AND ITS read_at TRAVELS WITH IT (task/2622). The counts,
        # the ordered rows and every age on this wire are one projection of
        # ONE snapshot taken at ONE instant, stamped so the two cells that
        # render it — the board home's band and the work tab's card — can
        # prove they are showing the same version of the queue. Two reads,
        # or one read with the browser supplying its own clock, is how a
        # headline and the rows under it come to disagree.
        read_at = time.time()
        rows, unavailable = tasks.snapshot()
        if unavailable:
            # UNKNOWN IS NOT ZERO, AND IT MUST BE UNKNOWN IN EVERY FIELD.
            # `queue: None` is what makes the headline render UNKNOWN rather
            # than keep the last totals it drew: a consumer handed no queue
            # at all cannot accidentally fall through to a stale one.
            return {"unavailable": True, "why": str(unavailable),
                    "read_at": None, "queue": None,
                    "entries": [], "unscoped": [], "counts": {},
                    "withheld_foreign": 0, "project": None}
        # RANK FIRST, THEN THE NUMBERING — the owner asked whether tasks are
        # prioritized, and a board that renders them in id order answers no
        # whatever the field holds. `tasks.rank_key` is the published
        # composition (UNRANKED sorts last, never as P3); the LAND-ORDER
        # fallback is the numbering this surface has always used, because
        # helm.tasks may reach this trunk before the key does.
        # AND AGE INSIDE THE RANK (task/2622). `rank_key` falls through to the
        # NUMBERING, which is filing order only for rows minted by this ledger:
        # a migrated row keeps the number it was born with, so a P1 filed this
        # morning could sit above a P1 that has been waiting since the private
        # list. The owner asked how he is supposed to cross-check the ordering
        # of a 477-row backlog, and OLDEST-FIRST is the part of it he can check
        # at a glance. `tasks.board_key` is that composition and it is the
        # store's, not this route's — the same reason the rank table is not
        # here. Both fallbacks stay: helm.tasks may reach this trunk before
        # either key does, and this surface's founding law is that it renders
        # rather than 500s when the module below it is older than it is.
        # AND THE ORDERING IS ASKED FOR AS AN ORDERING, NOT REBUILT FROM A
        # KEY. A published key is an invitation each caller answers its own
        # way, and that is exactly what happened: this route sorted by
        # `board_key` while `helm task list` went on sorting by the
        # numbering, so one snapshot came out of the browser and out of the
        # terminal as two different lists. `tasks.board_order` is the one
        # answer and both doors call it.
        _order = getattr(tasks, "board_order", None)
        ordered = (_order(rows) if callable(_order) else
                   sorted(rows.values(),
                          key=(getattr(tasks, "rank_key", None)
                               or tasks.sort_key)))
        # THE ROW'S PROJECT through the store's one reader when it exists;
        # the inline form is the LAND-ORDER fallback (helm.tasks may predate
        # the project axis on this trunk — this surface's founding law), and
        # it is field-normalization only, never a second cwd→project
        # derivation.
        of_row = getattr(tasks, "project_of_row", None) or (
            lambda r: str(r.get("project") or "").strip() or None)
        # THE CLOCK IS THE SERVER'S AND THE PARSE IS THE STORE'S (task/2622).
        # A timestamp reader in the browser is a SECOND answer with its own
        # rules — "a date is any number over 1e9" against the store's "any
        # number over 0" — and the two disagree over a whole window of stamps
        # while each renders a confident age out of its own opinion. Every age
        # on this wire is a NUMBER OR null computed by
        # `tasks.row_ages` at `read_at`; null means UNKNOWN and is the only
        # thing a client can render for it, because the client never sees the
        # stamp. The LAND-ORDER fallback leaves the ages absent rather than
        # guessing them here: an older helm.tasks has no parser to borrow,
        # and this route inventing one is the second reader all over again.
        _ages = getattr(tasks, "row_ages", None)
        ages = ((lambda r: _ages(r, read_at)) if callable(_ages)
                else (lambda r: {"ts_epoch": None, "age_s": None,
                                 "noted_age_s": None, "stale": False}))
        entries = [dict(ages(r), **{
            "id": str(r.get("id") or ""),
            "title": str(r.get("title") or ""),
            "status": str(r.get("status") or ""),
            "ts": str(r.get("ts") or ""),
            "last_updated": str(r.get("last_updated") or "") or None,
            "owner": str(r.get("owner") or "") or None,
            "note": str(r.get("note") or "") or None,
            "refs": [str(x) for x in (r.get("refs") or [])],
            "source": str(r.get("source") or "") or None,
            # WHOSE ROW THIS IS (task/974) — the label that makes two
            # projects' consoles distinguishable. None = UNSCOPED legacy.
            "project": of_row(r),
            # PROVENANCE ON THE OWNER'S OWN SURFACE. The field is what the
            # owner asked to see ("major requests FROM ME"), so shipping it
            # to the CLI glyph alone would have been the AX-gain-without-a-
            # UX-gain the standing canon names. TRI-STATE ON THE WIRE: None
            # is UNKNOWN — the 251 legacy rows nobody witnessed as
            # provenance — and the browser must not draw it as "agent".
            # THROUGH tasks.origin_of, NOT the raw field: the real ledger
            # holds 251 rows stamped `corpus-2026-08-05` by a migration, and
            # forwarding that raw would hand the browser a FOURTH state while
            # every consumer here is written against three. Normalized on
            # read; the record itself is never rewritten.
            "origin": tasks.origin_of(r),
            # RANK ON THE WIRE, TRI-STATE LIKE ORIGIN ABOVE IT. None is
            # UNRANKED — nobody has judged this row — and the browser must
            # not draw it as P3: judged-lowest and unjudged are different
            # answers, which is the whole reason the field refuses an eighth
            # spelling. An unrecognised value normalizes to None here rather
            # than travelling, so the card is written against four states
            # plus absence and can never meet a fifth.
            "priority": (str(r.get("priority"))
                         if str(r.get("priority") or "")
                         in getattr(tasks, "PRIORITIES", ()) else None),
            "closed_reason": str(r.get("closed_reason") or "") or None,
            "comments": len(r.get("comments") or []),
            # THE LAST NOTE, AS ONE LINE AND A STAMP (task/2622). The count
            # above answers "is there anything written here"; it cannot answer
            # "has anybody touched this in a month", which is the question the
            # owner asked of a backlog he suspects is being ignored. So the
            # card needs WHEN the newest note was written and enough of it to
            # recognise — and the full prose stays on `/api/task/notes`, where
            # that route's own docstring puts it: a first line is bounded, and
            # folding whole arguments into a payload that carries every row is
            # the growth this endpoint refuses.
            #
            # FIRST LINE, NEVER A TRUNCATED SENTENCE. A cut mid-word reads as
            # a rendering fault; a first line is a thing the author wrote. The
            # hard cap is the runaway guard for a note with no newline at all.
            "last_note": _task_last_note(tasks, r),
        }) for r in ordered]
        # THE PROJECT AXIS ON THE OWNER SURFACE (task/974): this console
        # renders ONE project's work and NAMES IT — the leak the owner
        # measured was another project's row rendering as CONTRARY inside helm's
        # own pipeline count. Scope = the same cwd→project lens the store and
        # the task CLI use (tasks.current_project, guarded for land-order
        # independence). FOREIGN rows are withheld as a COUNT, never served —
        # the API leg must not leak them into this project's card. The
        # UNSCOPED legacy bucket rides SEPARATELY, disclosed as such: every
        # row filed before the axis is legacy, so hiding the bucket would
        # blank the live board the owner reads today, and guessing a scope
        # would retro-stamp history nobody witnessed. No derivable project =
        # no scope: serve everything, project: null (the store's global-only
        # fail-open).
        axis = getattr(tasks, "current_project", None)
        scope = axis() if callable(axis) else None
        if scope:
            shown = [e for e in entries if e["project"] == scope]
            bucket = [e for e in entries if e["project"] is None]
            foreign = len(entries) - len(shown) - len(bucket)
        else:
            shown, bucket, foreign = entries, [], 0
        # counts describe what the card RENDERS (scoped + unscoped bucket),
        # so the meta numbers and the visible rows cannot disagree; the
        # withheld population travels as its own named count.
        counts = {st: sum(1 for e in shown + bucket if e["status"] == st)
                  for st in ("open", "in_progress", "closed")}
        # THE HEADLINE, COUNTED ONCE, SERVER SIDE (task/2622). The same five
        # numbers render in two places — the board home's band and the work
        # tab's card — and counting them twice in the browser over two
        # arrivals of one payload is two versions of the queue waiting to
        # disagree. Counted over the population the card RENDERS (scoped plus
        # the unscoped legacy bucket), which is the population `counts` and
        # the meta line already describe, so the headline and the visible
        # rows cannot fall out of step. `tasks.queue_totals` owns the rule;
        # the LAND-ORDER fallback is None, which every consumer reads as
        # UNKNOWN — never as a queue of zeroes.
        _totals = getattr(tasks, "queue_totals", None)
        queue = (_totals(shown + bucket, read_at) if callable(_totals)
                 else None)
        # AND ONLY NOW IS THE WIRE BOUNDED. Every number above — `counts`,
        # the scoping, `queue` — was computed over the FULL projection, so
        # the headline and the visible rows still describe one population;
        # `_task_wire_row` shrinks the bytes and nothing else. Read the three
        # bounds beside it before touching this line.
        out = {"project": scope,
               "entries": [_task_wire_row(e) for e in shown],
               "unscoped": [_task_wire_row(e) for e in bucket],
               "withheld_foreign": foreign, "counts": counts,
               "read_at": read_at, "queue": queue}
        json.dumps(out)   # unserializable shapes degrade too
        return out
    except Exception:
        # THE DEGRADED SHAPE IS THE UNKNOWN SHAPE, FIELD FOR FIELD. A payload
        # that says `unavailable` and simply omits `queue` lets a consumer
        # fall through to whatever it drew last; `queue: None` and an empty
        # `entries` say the number is not known RIGHT NOW, which is the whole
        # difference between "cannot see the backlog" and "the backlog is
        # clear". Same keys as the read-failure branch above, deliberately:
        # one absent-shape for one route, so a client written against either
        # can never meet the other.
        return {"unavailable": True, "why": "task projection failed",
                "read_at": None, "queue": None,
                "entries": [], "unscoped": [], "counts": {},
                "withheld_foreign": 0, "project": None}



def _api_task_notes(qs):
    """ONE task's comments, in FULL, fetched on demand -> (obj, status).

    THE OWNER COULD SEE THAT A DECISION EXISTED AND NOT READ IT. `/api/tasks`
    sends `len(comments)` and the card renders "3 comments", so every ruling
    an agent files onto a row — including a review panel's whole ruling, filed
    onto task/341 tonight precisely BECAUSE a chat line scrolls away — reached
    his console as a NUMBER. That is the ax-gain-owes-a-ux-gain law failing on
    the surface built to carry owner decisions, and it is a WIRED-not-WORKING
    defect: the data is there, the route is there, the content is unreachable.

    ON DEMAND, NOT ON THE LIST. Comments are unbounded prose and the list
    carries every row; folding full text into `/api/tasks` would make the
    backlog payload grow with the length of the argument written on it. The
    card keeps its cheap count and asks for one row's notes when the reader
    opens them — the same shape the sessions view already uses.

    NEVER TRUNCATED. A capped ruling is the same defect one layer down: he
    would be reading a decision that stops mid-sentence and could not tell
    that it had. If a row is big, it is big."""
    try:
        from . import tasks
    except ImportError:
        return {"unavailable": True,
                "why": "helm.tasks not landed on this trunk yet"}, 200
    rid = str((qs.get("id") or [""])[0]).strip()
    if not rid:
        return {"error": "id is required"}, 400
    # ONLY THE STORE READ IS WRAPPED, AND THE SCOPE IS THE POINT.
    # The first cut wrapped the whole body, so a malformed row or a
    # serialization bug — MY code failing, with the store perfectly available
    # — surfaced as HTTP 200 {unavailable: true}: "CANNOT SEE your comments".
    # That sends the reader to check the ledger while the fault is here, and
    # it is the same class as a surface blaming the store for reaching past
    # its own contract. UNAVAILABLE is a claim about the STORE and nothing
    # else may borrow it; a bug below raises and the server answers 500,
    # which is loud and TRUE.
    try:
        rows, unavailable = tasks.snapshot()
    except Exception:
        return {"unavailable": True}, 200
    if unavailable:
        # CANNOT SEE is not "no notes" — the state triad this file already
        # enforces for the backlog holds one row down too.
        return {"unavailable": True, "why": str(unavailable)}, 200
    # THE SETTLED CONTRACT IS snapshot/_sort_key/comment AND NOTHING ELSE.
    # My first cut called tasks.normalize_id and the surface stub — which
    # implements exactly the contract — raised AttributeError, which the
    # except below then reported as "ledger unavailable". A surface that
    # reaches past the agreed seam breaks on the day that module is
    # reorganised, and reports it as the STORE's fault. The id we are
    # handed came from the payload this same endpoint family emitted, so
    # a direct lookup is right; the bare-number spelling is the only
    # other thing a human types.
    row = rows.get(rid) or rows.get("task/%s" % rid.lstrip("#"))
    if not row:
        return {"error": "%s does not exist" % rid}, 404
    # THE STAMP IS RESOLVED HERE, BY THE STORE'S PARSER (task/2622). The card
    # renders a date beside each note, and a card parsing the raw `ts` itself
    # is a SECOND timestamp reader with its own rules about what counts as a
    # date. `ts_epoch` is a number or null from `tasks.stamp_epoch`
    # — null is UNKNOWN and is the only thing the browser can draw for it —
    # while the raw `ts` stays on the wire for any reader that wants what the
    # record literally holds. LAND-ORDER fallback: no parser, no claim.
    _parse = getattr(tasks, "stamp_epoch", None)
    notes = [{"ts": str(c.get("ts") or ""),
              "ts_epoch": _parse(c.get("ts")) if callable(_parse) else None,
              "by": str(c.get("by") or "") or None,
              "text": str(c.get("text") or "")}
             for c in (row.get("comments") or []) if isinstance(c, dict)]
    # AND THE ROW'S OWN NOTE, WHOLE, FOR THE SAME REASON THE COMMENTS ARE
    # HERE. `/api/tasks` now carries a bounded opening of `note` and flags the
    # rest with `note_more`, because unbounded prose on a payload that carries
    # every row is the growth this route's docstring refuses. That bound owes
    # the reader a door, and this is it: the excerpt on the card, the whole
    # text one fetch away. NEVER TRUNCATED here — if a row is big, it is big.
    out = {"id": str(row.get("id") or rid), "comments": notes,
           "note": str(row.get("note") or "")}
    json.dumps(out)
    return out, 200


def _api_tasks_comment(payload):
    """An owner note on a task, GUI-first (the owner does not run CLI
    commands): same one-writer-path law as decisions — the POST routes
    through the exact tasks function the CLI calls, and the comment lives
    IN the task row (the decisions precedent, ownerasks.py:532-533)."""
    try:
        from . import tasks
    except ImportError:
        return {"error": "helm.tasks not landed on this trunk yet"}, 400
    rid = str(payload.get("id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not rid or not text:
        return {"error": "id and text are required"}, 400
    row, problem = tasks.comment(rid, text)
    if not row:
        return {"error": problem}, 400
    out = {"ok": True, "id": str(row.get("id") or rid),
           "comments": len(row.get("comments") or [])}
    if problem:
        out["warning"] = problem
    return out, 200



def _api_decisions_verdict(payload):
    """Owner ruling from the web queue, through the SAME ownerasks.decide the
    CLI calls (one writer path), then delivery — the DM back to the FILING
    SEAT is attempted in the same breath and its failure is REPORTED, never
    silent: `delivered: false` + delivery_error means the card stays decided
    and the retry button (or `helm decide deliver`) finishes the job.

    THE OWNER BY DESIGN (task/2997): this handler is his browser door, so it
    mints the OwnerDoor the ledger requires; no other production code may.
    The bearer is CSRF protection and proves a browser on loopback, not the
    owner — the argv-guard refuses an agent's own POST here, and the residual
    is stated in ownerasks' module docstring."""
    from . import ownerasks
    rid = str(payload.get("id") or "").strip()
    choice = str(payload.get("choice") or "").strip()
    if not rid or not choice:
        return {"error": "id and choice are required"}, 400
    row, err = ownerasks.decide(rid, choice,
                                comment=str(payload.get("comment") or ""),
                                by=ownerasks.owner_door("web"))
    if err:
        return {"error": err}, 400
    delivered, derr = ownerasks.deliver_verdict(rid)
    out = {"ok": True, "id": rid, "choice": row["verdict"]["choice"],
           "label": row["verdict"]["label"],
           "status": (delivered or row)["status"],
           "delivered": bool(delivered)}
    if derr:
        out["delivery_error"] = derr
    _ok, berr = ownerasks.sync_board()
    if berr:
        out["board_warning"] = str(berr)
    return out, 200



def _api_decisions_deliver(payload):
    """Retry the verdict DM for a decided-but-undelivered card."""
    from . import ownerasks
    rid = str(payload.get("id") or "").strip()
    if not rid:
        return {"error": "id is required"}, 400
    row, err = ownerasks.deliver_verdict(rid)
    if err:
        return {"error": err}, 400
    _ok, berr = ownerasks.sync_board()
    out = {"ok": True, "id": rid, "status": row["status"],
           "delivered_ref": row.get("delivered_ref") or ""}
    if berr:
        out["board_warning"] = str(berr)
    return out, 200



def _api_decisions_comment(payload):
    """A NON-CLOSING owner note on a card — the 'not yet / tell me more'
    lane. The card stays open; the asker gets the note as a DM (best-effort,
    failure reported in the response).

    THE OWNER BY DESIGN (task/2997): this is his browser door, so it mints
    the OwnerDoor rather than resolving the process, which may be a seat's
    pane that launched `helm web` and is not the person pressing the button.
    Same bearer caveat and argv-guard as the verdict handler above."""
    from . import ownerasks
    rid = str(payload.get("id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not rid or not text:
        return {"error": "id and text are required"}, 400
    row, problem = ownerasks.comment_decision(rid, text,
                                              by=ownerasks.owner_door("web"))
    if not row:
        return {"error": problem}, 400
    out = {"ok": True, "id": rid, "status": row["status"]}
    if problem:
        out["warning"] = problem
    return out, 200



def _api_whoami():
    """Operator profile + notes summary; same graceful degrade as the store."""
    try:
        from . import whoami
        out = {}
        for key, names in (("profile", ("profile", "summary", "load_profile", "load")),
                           ("notes", ("notes", "list_notes", "load_notes"))):
            for name in names:
                fn = getattr(whoami, name, None)
                if not callable(fn):
                    continue
                v = fn()
                if v is not None:
                    out[key] = v
                break
        json.dumps(out)
        # the console asks whoami on every load, so it is where the server
        # answers "what am I running" as well as "who is the operator"
        drift = code_drift()
        if drift:
            out["code_drift"] = drift
        return out if out else {"unavailable": True}
    except Exception:
        return {"unavailable": True}



SESSION_KEYS = ("h", "i", "t", "u", "mt", "project")

SESSION_CAP = 200



def _api_sessions():
    """Newest claude + codex sessions (the catalog's scope, not every
    harness), project-lensed; same degrade law.

    THE LOOP RUNS UNDER ONE `sessions.snapshot()`. Each `resume_command` has to
    name the session's credential home, and resolving that reads the live-pane
    map, the credential-home list and the latch index — three whole-machine
    reads that are the same for every row in the page. Unscoped, a full page
    repeats all three per row and the live-pane read alone is a walk of every
    process on the box; it is pure CPU, so it holds the GIL and the cost lands
    on every other request in flight, not only on this one.

    THE SCOPE IS SAFE HERE BECAUSE THE ROWS ARE KEYED BY SESSION ID AND THOSE
    KEYS ARE DISTINCT. The per-row part of the answer — which pid holds this
    sid, what home that pid states, which home claims this sid — is still
    computed per row; only the machine-wide reads it indexes into are shared.
    A latch written while resolving one row could never have been read by
    another, because no other row asks about that sid.

    It also makes the page describe a single instant instead of smearing the
    whole build across the payload.

    NO TTL CACHE ON THIS ROUTE, deliberately. Scoped, the build measures ~0.09s
    warm; the part that is expensive to produce is the CATALOG, and that already
    sits behind a single-flight 5-minute cache in `transcripts.get_catalog`
    whose own fill is ~1.8s — a cache whose TTL is two orders of magnitude
    LONGER than its fill, which is the shape that actually serves warm. Wrapping
    a 0.09s build in a second TTL would buy nothing and cost something real: the
    live-pane rung is the column an operator reads to decide whether a resume
    would double-open a session, and a cache would age exactly that answer.
    A cache is worth adding here only if this build is measured slow AGAIN, and
    then only with a TTL chosen from the measured fill."""
    try:
        from . import sessions
        rows = []
        with sessions.snapshot():
            for r in sessions.rows_for(limit=SESSION_CAP):
                row = {k: r.get(k) for k in SESSION_KEYS}
                row["cmd"] = sessions.resume_command(r)
                rows.append(row)
        return {"sessions": rows}
    except Exception:
        return {"unavailable": True}



# The readiness card's poll floor. The gauge composes a beacon census (two
# /proc walks) plus per-seat pane resolution (subprocesses through the
# metaharness), so a browser poll must never drive it directly — the
# single-flight TTL cache makes N tabs cost one gauge per window.
_READY_TTL_S = 30

# THE LAST MEASURED FILL, and the floor it buys.
#
# A CACHE WHOSE TTL IS SHORTER THAN ITS OWN FILL IS A TRAP, and this one sat in
# it: filling took ~46s against a 30s TTL (task/2887). `_cached` stamps the
# entry at COMPLETION, so the entry was not born expired — the failure is
# subtler and worse. Every poll that arrives after a 30s gap pays the full
# fill, and the fill is longer than the browser's deadline, so the owner's card
# timed out on every cold draw while the server was working correctly. A cache
# that costs more to fill than it is allowed to live never pays for itself.
#
# So the TTL cannot be shorter than the fill BY CONSTRUCTION: the effective ttl
# is floored at twice what the last fill actually took, measured, not assumed.
# With the read-only bounded-parallel gauge the fill measured 8.7s cold on this
# fleet, so the floor is inert (max(30, 17.4) == 30) — it is a guard against
# the relation regressing, not a tuning knob, and it needs no maintenance
# because it reads the real number instead of a copy of it.
_READY_FILL_S = 0.0



def _api_ready():
    """READINESS — `helm ready`'s five signals + composed verdict for the
    console card (#163). Read-only, ADVISORY (the gauge renders, it never
    gates), fail-open at 200 with a named `unavailable` — the roster read's
    law — because a console that 500s its readiness card is unreadable at
    exactly the post-reboot moment the card exists for."""
    def build():
        from . import ready
        global _READY_FILL_S
        started = time.time()
        try:
            return ready.gauge()
        finally:
            # RECORDED EVEN WHEN THE GAUGE DIES: a build that crashed after 40s
            # still proves the fill is long, and the floor exists for exactly
            # the runs nobody is timing.
            _READY_FILL_S = time.time() - started
    try:
        gauge = _cached("ready", max(_READY_TTL_S, 2 * _READY_FILL_S), build)
    except Exception as e:
        gauge = {"unavailable": "%s: %s" % (e.__class__.__name__, e)}
    # THE BUILD THE SERVER WOULD SERVE NOW, riding the one poll every view
    # already makes. The owner's tab ran a page for hours that a newer build had
    # replaced, and reported what the old one showed — nothing on the console
    # could tell him, because nothing on the console knew. It rides here rather
    # than on a route of its own because the question is asked from every view
    # and this is the only heartbeat all of them share.
    #
    # OUTSIDE THE CACHE, deliberately: the gauge is 30s stale by contract and a
    # stale build id is the one field that would defeat the purpose.
    try:
        from . import web_ui_loader
        return dict(gauge, build=web_ui_loader.build_id())
    except Exception:                      # noqa: BLE001 — the gauge still renders
        return gauge
del _web
