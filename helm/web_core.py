"""Core projections for :mod:`helm.web`."""
import sys

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})


# One store entry projects to these keys on the wire — the strip never needs bodies.
ENTRY_KEYS = ("id", "type", "confidence", "load_class", "scope")

ENTRY_CAP = 500

REVIEW_STMT_CAP = 400  # the review panel shows the statement head, not the body



def _api_registry():
    return registry.load()



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
            out["entries"] = [
                {k: e.get(k) for k in ENTRY_KEYS}
                for e in list(fn())[:ENTRY_CAP] if isinstance(e, dict)
            ]
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
        from . import store
        rows = []
        for e in store.reviewable():
            rows.append({
                "id": str(e.get("id") or ""),
                "type": e.get("type") or "",
                "status": e.get("status") or "",
                "statement": (e.get("statement") or "")[:REVIEW_STMT_CAP],
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
    cards whose DM EVIDENCE IS GONE (dm_lost — found in review: without this
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



def _api_tasks():
    """The team task BACKLOG — the second section the work
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
                    "why": "helm.tasks not landed on this trunk yet"}
        rows, unavailable = tasks.snapshot()
        if unavailable:
            return {"unavailable": True, "why": str(unavailable)}
        ordered = sorted(rows.values(), key=tasks.sort_key)
        entries = [{
            "id": str(r.get("id") or ""),
            "title": str(r.get("title") or ""),
            "status": str(r.get("status") or ""),
            "ts": str(r.get("ts") or ""),
            "last_updated": str(r.get("last_updated") or "") or None,
            "owner": str(r.get("owner") or "") or None,
            "note": str(r.get("note") or "") or None,
            "refs": [str(x) for x in (r.get("refs") or [])],
            "source": str(r.get("source") or "") or None,
            # PROVENANCE ON THE OWNER'S OWN SURFACE. The field is what the
            # owner asked to see ("major requests FROM ME"), so shipping it
            # to the CLI glyph alone would have been the AX-gain-without-a-
            # UX-gain the standing canon names. TRI-STATE ON THE WIRE: None
            # is UNKNOWN — the legacy rows nobody witnessed as
            # provenance — and the browser must not draw it as "agent".
            # THROUGH tasks.origin_of, NOT the raw field: a real ledger
            # can hold rows stamped with a corpus-migration tag, and
            # forwarding that raw would hand the browser a FOURTH state while
            # every consumer here is written against three. Normalized on
            # read; the record itself is never rewritten.
            "origin": tasks.origin_of(r),
            "closed_reason": str(r.get("closed_reason") or "") or None,
            "comments": len(r.get("comments") or []),
        } for r in ordered]
        counts = {st: sum(1 for e in entries if e["status"] == st)
                  for st in ("open", "in_progress", "closed")}
        out = {"entries": entries, "counts": counts}
        json.dumps(out)   # unserializable shapes degrade too
        return out
    except Exception:
        return {"unavailable": True}



def _api_task_notes(qs):
    """ONE task's comments, in FULL, fetched on demand -> (obj, status).

    THE OWNER COULD SEE THAT A DECISION EXISTED AND NOT READ IT. `/api/tasks`
    sends `len(comments)` and the card renders "3 comments", so every ruling
    an agent files onto a row — including a model panel's whole ruling, filed
    onto a task row precisely BECAUSE a chat line scrolls away — reached
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
    notes = [{"ts": str(c.get("ts") or ""),
              "by": str(c.get("by") or "") or None,
              "text": str(c.get("text") or "")}
             for c in (row.get("comments") or []) if isinstance(c, dict)]
    out = {"id": str(row.get("id") or rid), "comments": notes}
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
    and the retry button (or `helm decide deliver`) finishes the job."""
    from . import ownerasks, seats
    rid = str(payload.get("id") or "").strip()
    choice = str(payload.get("choice") or "").strip()
    if not rid or not choice:
        return {"error": "id and choice are required"}, 400
    row, err = ownerasks.decide(rid, choice,
                                comment=str(payload.get("comment") or ""),
                                by=seats.owner_name())
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
    failure reported in the response)."""
    from . import ownerasks
    rid = str(payload.get("id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not rid or not text:
        return {"error": "id and text are required"}, 400
    row, problem = ownerasks.comment_decision(rid, text)
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
    harness), project-lensed; same degrade law."""
    try:
        from . import sessions
        rows = []
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



def _api_ready():
    """READINESS — `helm ready`'s five signals + composed verdict for the
    console card. Read-only, ADVISORY (the gauge renders, it never
    gates), fail-open at 200 with a named `unavailable` — the roster read's
    law — because a console that 500s its readiness card is unreadable at
    exactly the post-reboot moment the card exists for."""
    def build():
        from . import ready
        return ready.gauge()
    try:
        return _cached("ready", _READY_TTL_S, build)
    except Exception as e:
        return {"unavailable": "%s: %s" % (e.__class__.__name__, e)}
del _web
