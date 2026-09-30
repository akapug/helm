"""helm remote — the RELAY: the helm-side identity of every driven remote session.

A driven remote session (helm/remote_session.py) cannot reach helm, so a local
relay acts for it. It is addressed like any seat —
`helm dispatch send cloud-opus <lane> --ref TIP --kind review --new-work` —
and on each tick it:

  * picks up the OPEN review and build rows addressed to a configured remote
    seat;
  * starts a read for a row (credit gate, pacing, the workspace), or, for a
    row that supersedes one a live session already read, delivers the delta
    as a re-read. The workspace is, by default, a scratch clone of the
    project's PRIVATE drop repository with the reviewed tip pushed as a
    namespaced branch, or, as an explicit option, a bundle. The task goes,
    by default, into the paying account's STANDING session (one the owner
    created in the web UI with the repository selected), because a session
    `claude --cloud` creates comes up with no GitHub access (MEASURED
    2026-09-28); a seat opts into that CLI launch (trust, token, launch)
    with `"transport": "cli"`, and a seat with no usable standing session is
    refused, never launched;
  * runs the BUILD lane end to end: a build row's brief goes to a standing
    session on its own start branch, the pushed `-build` branch is fetched
    into a local lane branch, a review row goes to a DIFFERENT standing
    session, its report is recorded as below, and a FIX goes back to the
    builder's session as a cure;
  * reads the project's drop, parses the session's report strictly, and
    nudges on silence, corrects a malformed report, and retires the session
    when a send is refused as archived;
  * reads each billed account's credit once a tick, checks in ONCE with a
    session whose account went flat with no report on the drop, and
    escalates (a hold, a DM and a room post naming the session's address)
    when the account stays flat after that — the relay cannot read the
    session's own reply;
  * records the read on the row UNDER THE SEAT'S NAME, as the policy table
    (helm/remote_policy.py) classifies it, and tells the row's sender;
  * keeps the two falsifiers, and reverts the same-model arm the moment one
    trips, with a journal line and a chat post.

HOW A READ IS RECORDED, and why each door. The seat has no pane, so it has no
runtime proof and cannot mint an APPROVE — and that is right: no reviewer's
clean read mints one. A REVIEW-LEG APPROVE is the source-clean HOLD every
reviewer uses (`dispatch hold --source-clean`), stamped with the seat as its
recipient-holder, and the integrator's land gate mints the approve on the
tree that lands. A CONCUR-class APPROVE is an ordinary hold whose reason names
the different-model read still owed, so the row stays visible and nobody can
land on it. A FIX (or an APPROVE that carries a cure) is a FIX verdict
recorded without a seat proof (`mark_verdict(bind_author=False)`, which the
ledger reads as nonauthorizing), with the cure fetched as a review branch and
named as its --patch-tip. Nothing here changes who may approve a land.

THE IDENTITY. Every ledger write happens inside `as_seat`: HELM_CHAT_NAME is
the seat, and every harness session id and cell profile is removed for the
block, so the caller-identity law reads the seat (not the process that ran the
tick) and no chat post is signed with another seat's key. A remote seat holds
no dregg identity; its attestations are unsigned rows, and the ledger event is
the record.

WHY A TIMER. A session answers asynchronously, often an hour after launch and
while no local seat is working, and a Stop hook fires only when some local
seat ends a turn, on that seat's turn path. The relay therefore runs as its
own oneshot systemd user timer (`helm remote ensure-timer`), the owed-push
pattern: nothing on a seat's turn waits on gh or the vendor, and a relay that
fails does not take another periodic leg down with it.
"""
import contextlib
import fcntl
import json
import os
import re
import sys

from . import home, pk, remote_credit, remote_policy, remote_session as rs

BOT = "remote-relay"
MAX_LAUNCHES_PER_ROW = 2
#: refused launches one row may take before it is held: a refused delivery
#: into a standing session or a refused checkout starts no session, so the
#: launch cap never counts it. The daily credit refusal is not counted: it
#: defers the row to a day with credit and never loops within one.
MAX_STANDING_REFUSALS_PER_ROW = 3
MAX_RECORD_TRIES = 3
MAX_ACTIVE_ENV = "HELM_REMOTE_MAX_ACTIVE"
INTERVAL_ENV = "HELM_REMOTE_TICK_INTERVAL_S"

_USAGE = """usage: helm remote status [--json]
       helm remote tick [--dry-run] [--json]
       helm remote policy
       helm remote parse <file|->
       helm remote calibrate <label> --found N --of M [--sample NAME] [--tip SHA] [--why TEXT]
       helm remote falsifier [reset --why TEXT]
       helm remote ensure-timer
  the relay for driven remote sessions (docs/REMOTE_SESSIONS.md)"""


def _max_active():
    return rs._int_env(MAX_ACTIVE_ENV, 3)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def as_seat(seat):
    """Act as the remote seat `seat` for one block (see the module note).
    The name is validated through the seam every seat name enters by, so a
    config key carrying a control character never becomes an identity."""
    from . import cell
    name = home.validate_seat_arg(seat)
    if not name:
        raise home.SeatNameError("a remote seat needs a name")
    keys = ("HELM_CHAT_NAME", "MELD_CHAT_NAME") + tuple(home._SESSION_ENV) \
        + tuple(cell.PROFILE_ENV)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(HELM_CHAT_NAME=name)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# facts the policy needs
# ---------------------------------------------------------------------------

def author_facts(row):
    """(model, family) of the row's sender from its runtime record, either
    None when the record does not say. A native Claude seat stamps no model,
    which the policy reads as same-model (the stricter arm). THE STAMP ALONE:
    the seat's transcript model (task/3508) would move a native author onto
    the looser cross-model arm, a policy change this relay has not made."""
    from . import dispatches
    sender = row.get("sender")
    model = dispatches._runtime_model(sender, stamped_only=True)
    families = {dispatches._family_lineage(f)
                for f in dispatches._runtime_families(sender)}
    return model, (next(iter(families)) if len(families) == 1 else None)


def lane_diff(repo, base, tip, cap=5000):
    """(paths, {path: [added lines]}) for base..tip, or (None, {}) when the
    diff cannot be read — which the policy reads as irreversible."""
    rc, names, _err = rs._git(repo, "diff", "--name-only", "--no-renames",
                              base, tip)
    if rc != 0:
        return None, {}
    rc, patch, _err = rs._git(repo, "diff", "-U0", "--no-color", "--no-renames",
                              base, tip, timeout=180)
    if rc != 0:
        return None, {}
    added, current = {}, None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and line.startswith("+"):
            rows = added.setdefault(current, [])
            if len(rows) < cap:
                rows.append(line[1:])
    return [p for p in names.splitlines() if p.strip()], added


def verdict_family(row):
    """The family of the reader that recorded a verdict: the family its
    recorded author proof resolved, else the recipient's runtime family, else
    None (unknown, which the contradiction arm never counts)."""
    from . import dispatches
    proof = row.get("verdict_author_runtime_evidence")
    family = ((proof or {}).get("resolved") or {}).get("family") \
        if isinstance(proof, dict) else None
    if isinstance(family, str) and family:
        return dispatches._family_lineage(family)
    families = {dispatches._family_lineage(f)
                for f in dispatches._runtime_families(row.get("recipient"))}
    return next(iter(families)) if len(families) == 1 else None


# ---------------------------------------------------------------------------
# the falsifiers and the arm
# ---------------------------------------------------------------------------

def falsifier_state(journal):
    """(latched trip event or None, the time evidence counts from). A reset
    re-arms the arm and starts the evidence over."""
    since, latched = "", None
    for e in journal:
        if e.get("event") == "falsifier-reset":
            since, latched = e.get("ts") or "", None
        elif e.get("event") == "falsifier-tripped":
            latched = e
    return latched, since


def arm_state(journal):
    """(ARM_ON|ARM_OFF, why): the switch AND no latched falsifier."""
    if remote_policy.arm_switch() == remote_policy.ARM_OFF:
        return remote_policy.ARM_OFF, "%s is off" % remote_policy.SWITCH_ENV
    latched, _since = falsifier_state(journal)
    if latched:
        return remote_policy.ARM_OFF, "falsifier %s tripped at %s: %s" % (
            latched.get("arm"), latched.get("ts"), latched.get("detail"))
    return remote_policy.ARM_ON, "the integrator's ruling, no falsifier tripped"


def _fixes(current):
    """{tip, ts, family} for every FIX on the ledger: verdicts and advisory
    reads alike."""
    out = []
    for row in (current or {}).values():
        if row.get("status") == "verdict" and row.get("polarity") == "fix":
            out.append({"tip": row.get("reviewed_tip"),
                        "ts": pk.parse_ts_epoch(row.get("verdict_ts")),
                        "family": verdict_family(row)})
        for read in row.get("advisory_reads") or ():
            if read.get("polarity") == "fix":
                out.append({"tip": read.get("reviewed_tip"),
                            "ts": pk.parse_ts_epoch(read.get("ts")),
                            "family": read.get("reviewer_family")})
    return out


def check_falsifier(journal, current, dry=False):
    """The latched trip, a new one, or None. Records each contradiction it can
    read once, and trips the arm the moment either falsifier says so."""
    latched, since = falsifier_state(journal)
    if latched:
        return latched
    approves = [{"tip": e.get("tip"), "ts": pk.parse_ts_epoch(e.get("ts"))}
                for e in journal if e.get("event") == "recorded"
                and str(e.get("ts") or "") >= since
                and e.get("relation") == remote_policy.SAME_MODEL
                and e.get("class") == remote_policy.REVIEW_LEG
                and e.get("action") == "source-clean-hold"]
    found = remote_policy.contradictions(approves, _fixes(current))
    known = {e.get("tip") for e in journal if e.get("event") == "contradiction"}
    for c in found:
        if c["tip"] not in known and not dry:
            rs.append({"event": "contradiction", "tip": c["tip"],
                       "approve_ts": pk.epoch_ts(c["approve_ts"]),
                       "fix_ts": pk.epoch_ts(c["fix_ts"])})
    tripped, detail = remote_policy.contradiction_trip(found)
    arm = "contradictions"
    if not tripped:
        records = [e for e in journal if e.get("event") == "calibration"
                   and str(e.get("ts") or "") >= since]
        tripped, _share, detail = remote_policy.calibration_trip(records)
        arm = "calibration"
    if not tripped:
        return None
    return trip(arm, detail, dry)


def trip(arm, detail, dry=False):
    """Latch the revert: one journal line and one chat post. VISIBLE is the
    point — an arm that turns itself off in silence is a policy change nobody
    made."""
    event = {"event": "falsifier-tripped", "arm": arm, "detail": detail,
             "ts": pk.now_ts()}
    if dry:
        return event
    rs.append(event)
    text = ("REMOTE REVIEW FALSIFIER TRIPPED (%s): %s. Same-model remote reads "
            "are CONCUR-class from now on, and a different-model read is owed "
            "on every lane one reads. `helm remote falsifier` shows it; `helm "
            "remote falsifier reset --why TEXT` re-arms it." % (arm, detail))
    try:
        _post_room(text, "remote-falsifier:%s:%s" % (arm, event["ts"]))
    except Exception as exc:              # noqa: BLE001 — the journal line stands
        rs.append({"event": "post-failed", "what": "falsifier-tripped",
                   "error": "%s: %s" % (type(exc).__name__, exc)})
    return event


def _post_room(text, event_id):
    """One room post as the relay, unsigned (the relay holds no dregg key),
    idempotent on `event_id`."""
    from . import chat
    return chat.post(text, who=BOT, sign=False, event_id=event_id)


def _dm(seat_name, to, text):
    """One DM from the remote seat to `to`: None, or why it did not land."""
    from . import seats
    with as_seat(seat_name):
        _row, err = seats.dm(to, text, who=seat_name)
    return err


# ---------------------------------------------------------------------------
# the rows
# ---------------------------------------------------------------------------

def label_of(row):
    return "%s-%s" % (pk.slug(row.get("lane") or "lane", 40), row["id"][:8])


def _once(journal, row, event, key):
    """Whether this row already has a `key` event that took effect: a write
    the ledger refused is not done, so it is tried again next tick."""
    return any(e.get("event") == event and e.get("row") == row["id"]
               and e.get("key") == key and not e.get("error") for e in journal)


def _hold(row, seat, reason, journal, dry, key=None):
    """An ordinary hold naming why the relay cannot serve the row, recorded
    once per reason. The row stays visible; nothing is dropped in silence."""
    from . import dispatches
    key = key or reason[:60]
    if _once(journal, row, "held", key):
        return "held"
    reason = _one_line(reason, 250)
    if dry:
        return "would hold: " + reason
    with as_seat(seat):
        _out, err = dispatches.mark_hold(row["id"], reason)
    rs.append({"event": "held", "row": row["id"], "seat": seat, "key": key,
               "reason": reason, "error": err})
    return "held: " + reason if not err else "hold refused: " + err


def _one_line(text, cap):
    text = re.sub(r"[^\x20-\x7e]+", " ", str(text or "")).strip()
    return text if len(text) <= cap else text[:cap - 3] + "..."


def tick(dry=False, now=None):
    """One relay pass -> {actions: [(row, action)], notes: [...]}."""
    from . import dispatches
    out = {"actions": [], "notes": []}
    if not rs.enabled():
        out["notes"].append("remote sessions are switched off (%s)"
                            % rs.SWITCH_ENV)
        return out
    cfg, why = rs.load_config()
    if why:
        out["notes"].append("remote-session config unreadable: " + why)
        return out
    if not cfg["seats"]:
        out["notes"].append("no remote seat is configured (%s)" % cfg["path"])
        return out
    with _tick_lock() as held:
        if not held:
            out["notes"].append("another relay tick holds the lock")
            return out
        current, unavailable = dispatches.snapshot()
        if unavailable:
            out["notes"].append("dispatch ledger unavailable: %s" % unavailable)
            return out
        # the tick's rows, for walking supersedes chains; cleared on the way
        # out, so nothing of one tick outlives it in the module
        _ROWS.clear()
        _ROWS.update(current)
        try:
            _tick_rows(out, cfg, current, dry, now)
        finally:
            _ROWS.clear()
    return out


def _tick_rows(out, cfg, current, dry, now):
    journal = rs.read_journal()
    tripped = check_falsifier(journal, current, dry)
    probe_accounts(journal, now, dry)
    if tripped:
        out["notes"].append("same-model arm OFF: falsifier %s: %s"
                            % (tripped.get("arm"), tripped.get("detail")))
    drops = {}
    rows = sorted((r for r in current.values()
                   if str(r.get("recipient") or "").casefold() in cfg["seats"]
                   and r.get("status") == "open"),
                  key=lambda r: str(r.get("ts") or ""))
    open_ids = {r["id"] for r in rows}
    for row in rows:
        seat = str(row["recipient"]).casefold()
        try:
            action = handle_row(row, seat, cfg, rs.read_journal(), drops,
                                dry, now, open_ids)
        except Exception as exc:         # noqa: BLE001 — one row, not the pass
            action = "error: %s: %s" % (type(exc).__name__, exc)
        out["actions"].append((row["id"], action))


@contextlib.contextmanager
def _tick_lock():
    os.makedirs(rs.state_dir(), exist_ok=True)
    fh = open(os.path.join(rs.state_dir(), "tick.lock"), "a")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


def handle_row(row, seat_name, cfg, journal, drops, dry=False, now=None,
               open_ids=None):
    """What the relay does for ONE open row this tick, as a short phrase.
    `open_ids` are the open rows addressed to remote seats: only their
    sessions hold a concurrency slot."""
    seat, why = rs.seat_of(seat_name, cfg)
    if why:
        return _hold(row, seat_name, "remote seat cannot be driven: " + why,
                     journal, dry)
    if row.get("kind") not in ("review", "build"):
        return _hold(row, seat_name, "a remote seat takes review and build "
                     "rows only; this is a %s row" % row.get("kind"), journal,
                     dry)
    name, project = rs.project_for(cfg, row.get("repo_id"))
    if not project:
        return _hold(row, seat_name, "no remote-session project is configured "
                     "for this row's repository (%s)" % cfg["path"],
                     journal, dry)
    if not rs.parse_drop(project.get("drop")):
        return _hold(row, seat_name, "remote-session project %s names no drop "
                     "(owner/repo#N)" % name, journal, dry)
    why = rs.transport_of(project)[4]
    if why:
        return _hold(row, seat_name, "remote-session project %s: %s" % (
            name, why), journal, dry)
    if row.get("kind") == "build":
        return handle_build(row, seat_name, seat, project, journal, drops, dry,
                            now, open_ids)
    by_sid, by_row = rs.sessions(journal)
    sid = by_row.get(row["id"])
    if sid is None:
        return start(row, seat_name, seat, project, journal, by_sid, by_row,
                     dry, now, open_ids)
    events = by_sid.get(sid, [])
    target = rs.target_of(events)
    if target and target[0] != label_of(row):
        # THE SESSION MOVED ON: a superseding row's re-read now owns it, and
        # its report belongs to that row. Recording it here would put one
        # read on two rows.
        return "superseded in %s by %s" % (sid, target[0])
    state, why = rs.infer_state(events, now, flat=_flat(journal, events))
    if state == rs.ANSWERED or state in rs.LIVE or state == rs.UNDELIVERED:
        return attend(row, seat_name, seat, project, sid, events, state,
                      journal, drops, dry, now)
    if state == rs.ARCHIVED:
        launches = [e for e in journal if e.get("event") == "launch"
                    and e.get("row") == row["id"]]
        if len(launches) < MAX_LAUNCHES_PER_ROW:
            return launch_row(row, seat_name, seat, project, journal, by_sid,
                              dry, now, _slots(by_row, open_ids))
        return _hold(row, seat_name, "the remote session was archived without "
                     "a report after %d launch(es); reroute this read "
                     "(`helm remote status`)" % len(launches), journal, dry)
    return _escalate(row, seat_name, sid, events, why, journal, dry)


def start(row, seat_name, seat, project, journal, by_sid, by_row, dry, now,
          open_ids=None):
    """A row with no session yet: continue its parent's live session with a
    re-read when that is possible, else launch."""
    parent = row.get("supersedes")
    psid = by_row.get(parent) if parent else None
    # A BUILD'S REVIEW IS NEVER A RE-READ: the parent's session is the one
    # that built the lane, and a review never runs there.
    if psid in by_sid and _launch_of(by_sid[psid]).get("kind") != "build":
        pstate, _why = rs.infer_state(by_sid[psid], now,
                                      flat=_flat(journal, by_sid[psid]))
        if pstate == rs.ANSWERED or pstate in rs.LIVE:
            done = reread(row, seat_name, project, psid, by_sid[psid], dry)
            if done:
                return done
    return launch_row(row, seat_name, seat, project, journal, by_sid, dry, now,
                      _slots(by_row, open_ids))


def _flat(journal, events):
    """The idle signal for one session: a function of a start time that
    answers how long the session's ACCOUNT has spent nothing since then."""
    account, _home = _session_account(events)
    if not account:
        return None
    readings = [e for e in journal if e.get("event") == "credit"]
    return lambda since: remote_credit.flat_for(readings, account, since)


#: The shortest gap between two credit readings of one account the relay
#: takes on its own: the usage endpoint answers 429 to a token probed several
#: times in a few minutes.
PROBE_MIN_S = 300


def probe_accounts(journal, now=None, dry=False):
    """Take one credit reading of every account a live session bills, so a
    flat account can be told from a busy one. A dead token is not refreshed
    for this (a refresh is a model run); that account simply gives no signal
    this tick. Returns the accounts read."""
    import time
    now = time.time() if now is None else now
    by_sid, _by_row = rs.sessions(journal)
    homes = {}
    for events in by_sid.values():
        if rs.infer_state(events, now)[0] in rs.LIVE:
            account, claude_home = _session_account(events)
            if account and claude_home:
                homes[account] = claude_home
    read = []
    for account, claude_home in sorted(homes.items()):
        last = next((r for r in reversed(journal) if r.get("event") == "credit"
                     and str(r.get("account") or "").casefold()
                     == account.casefold() and not r.get("error")), None)
        at = pk.parse_ts_epoch((last or {}).get("at"))
        if at is not None and now - at < PROBE_MIN_S:
            continue
        if dry or not remote_credit.token_live(claude_home):
            continue
        reading = remote_credit.read_credit(claude_home)
        reading["account"] = reading.get("account") or account
        rs.append(dict(reading, event="credit"))
        read.append(account)
    return read


def _undelivered(row, seat_name, sid, events, journal, dry):
    """The report has not reached the drop after the nudge and idle bounds.

    Post ONE dispatcher row naming the session and how to recover its reply.
    Keep the source row OPEN: account flatness proves no billed generation, not
    process completion, and a late report must still be recorded on a later
    tick. UNDELIVERED is outside LIVE, so the session holds no launch slot and
    receives no more nudges while that recovery row stands."""
    launch = next((e for e in events if e.get("event") == "launch"
                   and e.get("sid")), {})
    url = launch.get("url") or sid
    workdir = launch.get("workdir") or "its workdir"
    text = ("Session %s reached the nudge and idle bounds, but its report "
            "cannot reach the drop; completion status is UNKNOWN and it may "
            "still be running. Session URL: %s. Next: claude --teleport %s "
            "from %s, then read its last reply." % (sid, url, sid, workdir))
    why = _post_undelivered(row, seat_name, sid, text, journal, dry)
    if why:
        return "undelivered notice refused: " + why
    return "undelivered notice posted; source review remains open"


def _post_undelivered(row, seat_name, sid, text, journal, dry):
    """None once the one dispatcher row exists, else why it does not."""
    if any(e.get("event") == "undelivered-posted" and e.get("sid") == sid
           and not e.get("error") for e in journal):
        return None
    if dry:
        return None
    recipient = row.get("sender")
    if not recipient:
        return "the review row names no sender to tell"
    from . import dispatches
    with as_seat(seat_name):
        posted, err, _existed = dispatches.send(
            recipient, "undelivered-%s" % sid, text, row["tip"],
            repo=row.get("repo_root"), kind="review", sign=False,
            new_work=True, key="undelivered:" + sid, force=True)
    rs.append({"event": "undelivered-posted", "row": row["id"], "sid": sid,
               "dispatch": (posted or {}).get("id"),
               "error": None if posted else err})
    return None if posted else (err or "the dispatcher refused the row")


def _escalate(row, seat_name, sid, events, why, journal, dry):
    """UNKNOWN is a session nobody can say anything true about: hold the row
    once with the reason, and tell the author and the room where the session
    can be opened by hand. The relay cannot read a session's own reply: the
    CLI gives none, and `claude --teleport` is interactive and needs a checkout
    whose GitHub remote is the session's repository and a branch the session
    pushed, and it hung on a bundle session (MEASURED)."""
    key = "unknown:" + sid
    launch = next((e for e in events if e.get("event") == "launch"
                   and e.get("sid")), {})
    url = launch.get("url") or sid
    done = _hold(row, seat_name, "remote session state UNKNOWN (%s); open %s "
                 "or reroute this read" % (why, url), journal, dry, key=key)
    if dry or not done.startswith("held: "):
        return done
    text = ("@%s read of %s at %s is stuck: %s. Its own reply is not readable "
            "from here; open %s in a browser, and reroute the read if it "
            "cannot be recovered (`helm remote status`)." % (
                seat_name, row.get("lane"), row["tip"][:12], why, url))
    for send in (lambda: _dm(seat_name, row.get("sender"), text)
                 if row.get("sender") else None,
                 lambda: _post_room(text, "remote-stuck:%s" % sid)):
        try:
            send()
        except Exception as exc:          # noqa: BLE001 — the hold stands
            rs.append({"event": "post-failed", "row": row["id"],
                       "what": "escalation",
                       "error": _one_line("%s: %s" % (type(exc).__name__, exc),
                                          200)})
    return done


def _address(events, sid):
    """The session a message for `sid` goes to: a standing session's real id
    (the journal keys each of its reads by row), else `sid` itself."""
    return _launch_of(events).get("standing_sid") or sid


def _deliver(claude_home, events, sid, message, row):
    """(ok, archived, why) for one follow-up. An archived STANDING session is
    recorded on its account, so no later row is delivered to it."""
    address = _address(events, sid)
    why = rs.refresh_token(claude_home)
    ok, archived, err = (False, False, why) if why else \
        rs.deliver(claude_home, address, message)
    if archived and address != sid:
        account, _home = _session_account(events)
        rs.append({"event": "standing-archived", "account": account,
                   "sid": address, "row": row["id"], "error": err})
    return ok, archived, err


def _session_account(events):
    launch = next((e for e in events if e.get("event") == "launch"
                   and e.get("sid")), {})
    return launch.get("account"), launch.get("home")


def reread(row, seat_name, project, sid, events, dry):
    """Deliver the delta to the parent's session, or None to launch instead:
    the new tip must descend from the one it read. On the branch transport
    the new tip is pushed as its own branch and the session fetches it; on
    the bundle transport the patch travels in the message and must fit it."""
    _label, old, _since, _kind = rs.target_of(events)
    launch = _launch_of(events)
    new = row["tip"]
    repo = project["repo"]
    rc, _o, _e = rs._git(repo, "merge-base", "--is-ancestor", old, new)
    if rc != 0:
        return None
    label = label_of(row)
    extra = {}
    if launch.get("transport") == rs.TRANSPORT_BRANCH:
        workdir = launch.get("workdir")
        if not workdir or not os.path.isdir(workdir):
            return None
        branch = rs.transport_of(project)[3] + label
        message = rs.reread_branch_message(label, old, new, branch)
        if dry:
            return "would push %s and re-read it in %s" % (branch, sid)
        base = launch.get("base") or old
        owned, why = rs.push_reread(workdir, repo, new, base, branch,
                                    launch.get("push_repo"))
        if why:
            return "re-read refused: " + why
        extra = {"branch": branch, "base": base, "owned_refs": owned}
    else:
        rc, patch, _err = rs._git(repo, "format-patch", "--stdout", "%s..%s"
                                  % (old, new), timeout=120)
        if rc != 0 or not patch.strip():
            return None
        message = rs.reread_message(label, old, new, patch)
        if len(message.encode("utf-8")) > rs.MAX_MESSAGE_BYTES:
            return None
        if dry:
            return "would re-read %s in %s" % (new[:12], sid)
    if launch.get("builder_session"):
        message = FRESH_READ + "\n\n" + message
    account, claude_home = _session_account(events)
    ok, archived, err = _deliver(claude_home, events, sid, message, row)
    rs.append(dict({"event": "deliver", "kind": "reread", "row": row["id"],
                    "seat": seat_name, "sid": sid, "label": label, "tip": new,
                    "account": account, "ok": ok, "error": err}, **extra))
    if archived:
        rs.append({"event": "archived", "row": row["id"], "sid": sid,
                   "error": err})
        return None
    return ("re-read %s delivered to %s" % (new[:12], sid)) if ok \
        else "re-read send failed: %s" % err


def _slots(by_row, open_ids):
    """The sessions that may hold a concurrency slot: those an OPEN row still
    reads. A session whose row was cancelled or recorded never answers the
    relay again, and counting it would block every launch after it."""
    if open_ids is None:
        return None
    return {by_row[r] for r in open_ids if r in by_row}


def _active(by_sid, now, sids=None, journal=None):
    """Sessions that still hold a concurrency slot. The flat reading is what
    makes a max-nudged session UNDELIVERED; without it that session still looks
    NUDGED and would keep the slot."""
    return sum(1 for sid, events in by_sid.items()
               if (sids is None or sid in sids)
               and rs.infer_state(
                   events, now,
                   flat=_flat(journal, events) if journal is not None else None
               )[0] in rs.LIVE)


def _gate(row, seat_name, journal, by_sid, dry, now, sids):
    """None when a new session may start for `row`, else the phrase that
    defers or holds it: the per-row launch cap, the concurrency cap and the
    daily budget."""
    launches = [e for e in journal if e.get("event") == "launch"
                and e.get("row") == row["id"]]
    if len(launches) >= MAX_LAUNCHES_PER_ROW:
        return _hold(row, seat_name, "launched %d times with no report; "
                     "reroute this read" % len(launches), journal, dry)
    refused = [e for e in journal if e.get("event") == "launch-refused"
               and e.get("row") == row["id"]
               and not str(e.get("key") or "").startswith("refused:")]
    if len(refused) >= MAX_STANDING_REFUSALS_PER_ROW:
        return _hold(row, seat_name, "%d launches were refused before any "
                     "session started (last: %s); reroute this row" % (
                         len(refused), refused[-1].get("why")), journal, dry)
    active = _active(by_sid, now, sids, journal)
    if active >= _max_active():
        return "deferred: %d sessions active (%s=%d)" % (
            active, MAX_ACTIVE_ENV, _max_active())
    readings = [e for e in journal if e.get("event") == "credit"]
    budget, bwhy = remote_credit.daily_budget(
        readings, now, scarce=remote_credit.max_pool_scarce())
    spent = remote_credit.spent_today(readings, now)
    if budget is not None and spent >= budget:
        return "deferred: $%.2f spent today of a $%.2f budget (%s)" % (
            spent, budget, bwhy)
    return None


def _payers(seat_name, seat, journal, exclude=None, pin=None):
    """(accounts in order, refusal). On the CLI transport every account may
    pay. On the STANDING transport (the default) only an account whose
    standing session is configured and not archived may, with the builders'
    sessions LAST for a review (`exclude`: load-spreading only, never a
    refusal, because the review brief has the session hand the read to one
    fresh-context subagent that holds none of the build's working context,
    and owner canon (2026-09-27) counts that instance's read even when its
    seat is on the chain), or only the builder's for a cure (`pin`). No usable
    one is a plain refusal: the CLI launch is measured broken and is never the
    fallback."""
    accounts = rs.accounts_of(seat)
    if rs.seat_transport(seat) == rs.SEAT_CLI:
        return accounts, None
    gone = rs.archived_standing(journal)
    usable = [a for a in accounts if a.get("standing")
              and a["standing"] not in gone]
    if pin:
        usable = [a for a in usable if a["standing"] == pin]
        if not usable:
            return [], ("the builder's standing session %s is archived or no "
                        "longer configured, and a cure goes only to its "
                        "builder; send a new build row" % pin)
    if exclude:
        usable = sorted(usable, key=lambda a: a["standing"] in exclude)
    if not usable:
        return [], ("no standing session is configured for @%s, or every one "
                    "is archived: name one per account as standing_session "
                    "in remote-sessions.json. A CLI launch is not the "
                    "fallback (it comes up with no GitHub access); a seat opts "
                    "into it with \"transport\": \"cli\"" % seat_name)
    return usable, None


def _bound(accounts, push_repo):
    """(accounts, refusal): only the standing sessions created on the
    project's push repository. The relay's PRIVATE check covers its own
    pushes; a standing session pushes to the repository the web UI attached,
    so a session not declared bound to `push_repo` is never used, and with
    none left the refusal names each session's binding and the push repo."""
    usable = [a for a in accounts if not rs.bound_refusal(a, push_repo)]
    if usable:
        return usable, None
    return [], "; ".join(rs.bound_refusal(a, push_repo) for a in accounts) \
        or "no standing session is bound to %s" % push_repo


def _choose(row, seat_name, accounts, journal, dry, now):
    """(account, reading, None) for the first account above the credit floor
    whose home is logged into it, or (None, None, the refusal phrase)."""
    readings = [e for e in journal if e.get("event") == "credit"]

    def read(account):
        if dry:
            # A DRY RUN READS BUT NEVER ACTS: a live token is read (one GET)
            # and nothing is journaled; a dead one is not refreshed, because
            # the refresh is a model run.
            if remote_credit.token_live(account["home"]):
                return remote_credit.read_credit(account["home"])
            return {"error": "dry run: the token is not live, so no refresh"}
        why = rs.refresh_token(account["home"])
        if why:
            reading = {"at": pk.now_ts(), "account": account["email"],
                       "error": why}
        else:
            reading = remote_credit.read_credit(account["home"])
            reading["account"] = reading.get("account") or account["email"]
        rs.append(dict(reading, event="credit"))
        return reading

    account, reading, refusals = remote_credit.choose_account(
        accounts, read, readings, remote_credit.floor_usd(), now)
    if not account:
        key = "refused:%s" % pk.now_ts()[:10]
        if not dry and not _once(journal, row, "launch-refused", key):
            rs.append({"event": "launch-refused", "row": row["id"],
                       "seat": seat_name, "key": key,
                       "why": "; ".join(refusals)})
        return None, None, "launch refused: " + "; ".join(refusals)
    logged = remote_credit.home_email(account["home"])
    if str(logged or "").casefold() != account["email"].casefold():
        return None, None, "launch refused: %s is logged into %s, not %s" % (
            account["home"], logged, account["email"])
    return account, reading, None


def _standing_start(row, seat_name, account, dest, task, owned, push_repo):
    """Deliver a new task into the account's standing session -> (journal
    session id, url, None) or (None, None, why). The journal keys the read by
    the standing session AND the row, so several rows can share one standing
    session without one's report reading as another's. A refused send takes
    back the branches this attempt claimed, so the next try can claim them
    again; an archived session is recorded on the account and never used
    again."""
    import shutil
    real = account["standing"]
    ok, archived, err = rs.deliver(account["home"], real, task)
    if ok:
        return "%s~%s" % (real, row["id"][:12]), \
            "https://claude.ai/code/" + real, None
    if archived:
        rs.append({"event": "standing-archived", "account": account["email"],
                   "sid": real, "row": row["id"], "error": err})
    if owned:
        rs.delete_branches(dest, owned, push_repo)
    shutil.rmtree(dest, ignore_errors=True)
    rs.append({"event": "launch-refused", "row": row["id"], "seat": seat_name,
               "standing": real,
               "why": _one_line("delivery to standing session %s failed: %s"
                                % (real, err), 300)})
    return None, None, ("standing session %s archived: %s" % (real, err)
                        if archived else
                        "delivery to standing session %s failed: %s"
                        % (real, err))


def launch_row(row, seat_name, seat, project, journal, by_sid, dry, now,
               sids=None):
    """Credit gate, pace, workspace (a pushed branch after the PRIVATE check,
    or a bundle), then the task: delivered into the paying account's STANDING
    session (the default), or, on a seat's explicit `"transport": "cli"`,
    trust, token and a CLI launch. Every outcome is a journal line."""
    from . import dispatches, vcs
    label, tip = label_of(row), row["tip"]
    deferred = _gate(row, seat_name, journal, by_sid, dry, now, sids)
    if deferred:
        return deferred
    standing = rs.seat_transport(seat) == rs.SEAT_STANDING
    transport, push_repo, push_url, prefix, _why = rs.transport_of(project)
    if standing and transport != rs.TRANSPORT_BRANCH:
        return _hold(row, seat_name, "a standing session reads a pushed "
                     "branch, and this project uses the bundle transport; a "
                     "bundle needs the seat's \"transport\": \"cli\"",
                     journal, dry)
    builders = _builders_of(journal, row)
    accounts, refusal = _payers(seat_name, seat, journal, exclude=builders)
    if not refusal and standing:
        accounts, refusal = _bound(accounts, push_repo)
    if refusal:
        return _hold(row, seat_name, refusal, journal, dry)
    account, reading, refusal = _choose(row, seat_name, accounts, journal, dry,
                                        now)
    if refusal:
        return refusal
    mode, mwhy = rs.permission_mode()
    if mwhy and not standing:
        return "launch refused: " + mwhy
    email, claude_home = account["email"], account["home"]
    builder_session = bool(standing and account["standing"] in builders)
    if dry:
        return "would %s %s at %s on %s ($%.2f left, %s)" % (
            "deliver" if standing else "launch", label, tip[:12], email,
            reading.get("left") or 0, reading.get("basis"))
    base_ref = project.get("base") or vcs.backend(project["repo"]).trunk_ref(
        project["repo"])
    dest = os.path.join(rs.work_dir(), "sessions", "%s-%s" % (label, tip[:12]))
    author = rs.parse_author(project.get("cure_author"))
    branch = (prefix + label) if prefix else None
    if transport == rs.TRANSPORT_BRANCH:
        info, why = rs.build_branch_checkout(
            project["repo"], tip, base_ref, dest, author, push_repo, push_url,
            branch)
    else:
        info, why = rs.build_bundle(project["repo"], tip, base_ref, dest, author)
    if why:
        rs.append({"event": "launch-refused", "row": row["id"],
                   "seat": seat_name, "why": why})
        return "launch refused: " + why
    if not standing:
        why = rs.mark_trusted(claude_home, dest)
        if why:
            rs.append({"event": "launch-refused", "row": row["id"],
                       "seat": seat_name, "why": why})
            return "launch refused: " + why
    rc, count, _err = rs._git(project["repo"], "rev-list", "--count",
                              "%s..%s" % (info["base"], tip))
    brief, _absent, _problem = dispatches.brief_of(row)
    protocol = None
    if project.get("brief"):
        try:
            with open(os.path.expanduser(project["brief"]),
                      encoding="utf-8") as f:
                protocol = f.read()
        except OSError as exc:
            return "launch refused: the project brief did not read: %s" % exc
    task = rs.task_text(label, tip, count if rc == 0 else "?", brief,
                        protocol, project["drop"], email, transport, branch)
    if standing:
        names = rs.branch_names(branch)
        # EVERY standing review task carries the fresh read, not only the
        # relay's own brief: a superseding row or a new review of the same
        # tip may land on the builder's session too.
        if FRESH_READ not in task:
            task = FRESH_READ + "\n\n" + task
        task = rs.standing_preface(seat["model"], seat.get("effort"), branch,
                                   names["base"], push_repo) + task
        sid, url, why = _standing_start(row, seat_name, account, dest, task,
                                        info.get("owned_refs"), push_repo)
        if not sid:
            return why
    else:
        sid, url, why = rs.launch(claude_home, seat["model"], dest, task,
                                  label, mode=mode,
                                  bundle=transport == rs.TRANSPORT_BUNDLE,
                                  effort=seat.get("effort"))
    after = remote_credit.read_credit(claude_home)
    after["account"] = after.get("account") or email
    rs.append(dict(after, event="credit"))
    rs.append({"event": "launch", "row": row["id"], "seat": seat_name,
               "label": label, "tip": tip, "base": info["base"],
               "account": email, "home": claude_home, "model": seat["model"],
               "effort": seat.get("effort"),
               "permission_mode": "(standing session)" if standing
               else mode or "(account default)",
               "transport": transport, "branch": branch,
               "owned_refs": info.get("owned_refs"),
               "push_repo": push_repo, "push_url": push_url,
               "sid": sid, "url": url, "workdir": dest, "why": why,
               "standing_sid": account["standing"] if standing else None,
               "builder_session": builder_session,
               "credit_left_before": reading.get("left"),
               "credit_basis": reading.get("basis")})
    if standing:
        return "delivered %s to standing session %s (%s)" % (
            label, account["standing"], url)
    return ("launched %s (%s)" % (sid, url)) if sid else "launch failed: " + why


def _branch_comment(events, target):
    """The fallback report branch read as one more drop comment, or None. It
    is the same untrusted data and meets the same parse and checks."""
    launch = _launch_of(events)
    workdir = launch.get("workdir")
    if launch.get("transport") != rs.TRANSPORT_BRANCH or not workdir \
            or not os.path.isdir(workdir):
        return None
    branch = _branch_for(events, target[0])
    text, sha, _why = rs.branch_report(workdir, branch, target[1]) \
        if branch else (None, None, None)
    if not text:
        return None
    name = rs.branch_names(branch)["report"]
    return {"id": "branch:" + name, "body": text,
            "url": "%s@%s" % (launch.get("push_repo"), name),
            "author": None, "branch": True,
            "branch_name": name, "branch_sha": sha}


def _look(row, seat, project, target, account, drops, seen, events=()):
    """('report', comment, report) | ('malformed', comment, why) | None for the
    session's current label and tip on the project's drop, or, on the branch
    transport, on its fallback report branch."""
    label, tip = target[0], target[1]
    spec = project["drop"]
    if spec not in drops:
        drops[spec] = rs.read_drop(spec)
    comments, why = drops[spec]
    if why:
        return ("unread", None, why)
    authors = project.get("drop_authors")
    fallback = _branch_comment(events, target)
    for c in list(comments) + ([fallback] if fallback else []):
        head = rs.header_of(c["body"])
        if not head or head["label"] != label or head["tip"] != tip[:12]:
            continue
        if authors and c.get("author") not in authors \
                and not c.get("branch"):
            continue
        report, why = rs.parse_report(c["body"])
        if not why and str(report["account"]).casefold() \
                != str(account or "").casefold():
            why = "the report names another account"
        if not why and remote_policy.model_line(report["model"]) \
                != remote_policy.model_line(seat.get("model")):
            why = "the report names a model other than the one launched"
        if why:
            if c.get("id") not in seen:
                return ("malformed", c, why)
            continue
        return ("report", c, report)
    return None


def attend(row, seat_name, seat, project, sid, events, state, journal, drops,
           dry, now):
    """A session that has not been recorded: find its report, correct it,
    nudge it, or wait."""
    if any(e.get("event") == "recorded" and e.get("row") == row["id"]
           and e.get("tip") == row["tip"] for e in journal):
        return "recorded"
    tries = [e for e in journal if e.get("event") == "record-refused"
             and e.get("row") == row["id"]]
    if len(tries) >= MAX_RECORD_TRIES:
        return _hold(row, seat_name, "the remote report could not be recorded "
                     "after %d tries (`helm remote status`)" % len(tries),
                     journal, dry)
    target = rs.target_of(events)
    account, claude_home = _session_account(events)
    seen = {e.get("comment") for e in events
            if e.get("event") == "report-malformed"}
    found = _look(row, seat, project, target, account, drops, seen, events)
    if found and found[0] == "unread":
        return "drop unread: " + found[2]
    if found and found[0] == "report":
        _kind, comment, report = found
        if dry:
            return "would record %s (%s)" % (report["verdict"], comment.get("url"))
        if state != rs.ANSWERED:
            rs.append({"event": "report", "row": row["id"], "seat": seat_name,
                       "sid": sid, "comment": comment.get("id"),
                       "url": comment.get("url"), "label": report["label"],
                       "tip": row["tip"], "verdict": report["verdict"],
                       "count": report["count"], "blocking": report["blocking"],
                       "model": report["model"],
                       "branch_name": comment.get("branch_name"),
                       "branch_sha": comment.get("branch_sha"),
                       "patch": bool(report.get("patch")),
                       "findings": [_one_line(f["text"].split("\n", 1)[0], 160)
                                    for f in report["findings"]][:20]})
        return record(row, seat_name, seat, project, sid, report, comment,
                      rs.read_journal(), dry)
    if state == rs.ANSWERED:
        return "answered; waiting to re-read the report from the drop"
    if found and found[0] == "malformed":
        _kind, comment, why = found
        if dry:
            return "would correct a malformed report (%s)" % why
        rs.append({"event": "report-malformed", "row": row["id"], "sid": sid,
                   "comment": comment.get("id"), "why": why,
                   "record": UNMEASURED})
        return "%s (the report did not parse: %s); " % (UNMEASURED, why) + \
            _send(row, seat_name, sid, claude_home, "correction",
                     rs.correction_message(target[0], target[1], why), target, events)
    if state == rs.UNDELIVERED:
        return _undelivered(row, seat_name, sid, events, journal, dry)
    if state == rs.SILENT_IDLE:
        if dry:
            return "would check in with idle %s" % sid
        return _send(row, seat_name, sid, claude_home, "idle-nudge",
                     rs.idle_message(target[0], target[1]), target, events)
    if rs.nudge_due(state, events, now):
        if dry:
            return "would nudge %s" % sid
        return _send(row, seat_name, sid, claude_home, "nudge",
                     rs.nudge_message(target[0], target[1]), target, events)
    return "waiting (%s)" % state


def _send(row, seat_name, sid, claude_home, kind, message, target,
          events=()):
    ok, archived, err = _deliver(claude_home, events, sid, message, row)
    rs.append({"event": "deliver", "kind": kind, "row": row["id"],
               "seat": seat_name, "sid": sid, "label": target[0],
               "tip": target[1], "ok": ok, "error": err})
    if archived:
        rs.append({"event": "archived", "row": row["id"], "sid": sid,
                   "error": err})
        return "archived: " + err
    return ("%s sent to %s" % (kind, sid)) if ok else "%s failed: %s" % (kind, err)


def _evidence(report, verdict, sid, comment, builder_session=False):
    return _one_line("remote read by %s (%s%s) at %s: %s, %d finding(s), %d "
                     "blocking; %s (%s); %s" % (
                         report["model"], sid,
                         "; the builder's own session, read by a fresh-context "
                         "subagent" if builder_session else "", report["tip"],
                         report["verdict"], report["count"],
                         report["blocking"], verdict["class"],
                         verdict["why"], comment.get("url") or "the drop"), 256)


def record(row, seat_name, seat, project, sid, report, comment, journal, dry):
    """Classify the read and record it on the row under the seat's name."""
    from . import dispatches, vcs
    tip = row["tip"]
    repo = project["repo"]
    base_ref = project.get("base") or vcs.backend(repo).trunk_ref(repo)
    rc, base, _err = rs._git(repo, "merge-base", base_ref, tip)
    paths, added = lane_diff(repo, base, tip) if rc == 0 else (None, {})
    brief, _absent, _problem = dispatches.brief_of(row)
    lane, hits = remote_policy.reversibility(paths, added, brief,
                                             project.get("doors") or ())
    author_model, author_family = author_facts(row)
    rel, rel_why = remote_policy.relation(report["model"], author_model,
                                          author_family)
    arm, arm_why = arm_state(journal)
    verdict = remote_policy.classify(report["model"], rel, lane, arm)
    builder_session = bool(_launch_of(events_of_sid(journal, sid)).get(
        "builder_session"))
    klass, evidence = verdict["class"], _evidence(report, verdict, sid, comment,
                                                  builder_session)
    rid, action, polarity, cure, err = row["id"], None, None, None, None
    no_patch = None
    with as_seat(seat_name):
        if klass == remote_policy.REFUSED:
            action, polarity = "hold", "refused"
            _o, err = dispatches.mark_hold(rid, _one_line(
                "remote read REFUSED: %s; the review leg is still owed"
                % verdict["why"], 250))
        else:
            patch, no_patch, observed = _cure_source(
                report, events_of_sid(journal, sid), tip)
            if observed:
                rs.append({"event": "branch-observed", "row": rid, "sid": sid,
                           "branch_name": observed[0],
                           "branch_sha": observed[1]})
            if patch:
                author = rs.parse_author(project.get("cure_author"))
                if not author:
                    no_patch = "no cure author is configured for this project"
                else:
                    cure, why = rs.apply_cure(
                        repo, tip, patch, author,
                        "review/%s/%s" % (seat_name, label_of(row)))
                    if why:
                        no_patch = _one_line("the remote cure did not apply: "
                                             + why, 250)
            if report["verdict"] == "APPROVE" and not cure:
                if klass == remote_policy.REVIEW_LEG:
                    action, polarity = "source-clean-hold", "approve"
                    _o, err = dispatches.mark_hold(
                        rid, _one_line("remote read clean: " + evidence, 250),
                        source_clean_tip=tip)
                    if err:
                        action = "hold"
                        _o, err2 = dispatches.mark_hold(rid, _one_line(
                            "remote read APPROVE at %s could not be held "
                            "source-clean: %s" % (tip[:12], err), 250))
                        err = err2
                else:
                    action, polarity = "concur-hold", "concur"
                    _o, err = dispatches.mark_hold(rid, _one_line(
                        "remote read CONCUR-class: APPROVE at %s; still owed: "
                        "%s (send it --supersedes %s)" % (
                            tip[:12], verdict["owed"], rid[:12]), 250))
            else:
                action, polarity = "fix-verdict", "fix"
                imperfect = report["verdict"] == "APPROVE"
                worse = None if imperfect else [
                    p for p in rs.finding_paths(report) if p in (paths or ())]
                # HOW THE READER KNOWS: measured only when every finding
                # the verdict rests on (the blocking ones, else all) was.
                rests = [f for f in report["findings"]
                         if f["severity"] == "BLOCKING"] or report["findings"]
                basis = "measured" if rests and all(
                    f["basis"] == "MEASURED" for f in rests) else "inferred"
                _o, err = dispatches.mark_verdict(
                    rid, tip, evidence, polarity="fix", basis=basis,
                    bind_author=False, finding_count=report["count"],
                    patch_tip=cure["patch_tip"] if cure else None,
                    no_patch_because=None if cure else no_patch,
                    worse_than_main_paths=worse or None, imperfect=imperfect)
    if err:
        rs.append({"event": "record-refused", "row": rid, "seat": seat_name,
                   "sid": sid, "why": _one_line(err, 400)})
        return "record refused: " + _one_line(err, 200)
    rs.append({"event": "recorded", "row": rid, "seat": seat_name, "sid": sid,
               "tip": tip, "class": klass, "relation": rel,
               "relation_why": rel_why, "lane": lane, "hits": hits[:8],
               "arm": arm, "arm_why": arm_why, "action": action,
               "polarity": polarity, "comment": comment.get("id"),
               "patch_tip": (cure or {}).get("patch_tip"),
               "cure_ref": (cure or {}).get("ref"),
               "cure_error": None if cure or not no_patch
               or no_patch == NO_CURE else no_patch})
    _cleanup(rs.read_journal(), sid, rid)
    _calibration_candidate(row, seat_name, report)
    _tell_sender(row, seat_name, report, action, klass, verdict, comment)
    said = "recorded %s as %s (%s)" % (report["verdict"], action, klass)
    if action == "fix-verdict":
        cure = cure_back(row, seat_name, project, report, comment,
                         rs.read_journal())
        if cure:
            said += "; " + cure
    return said


NO_CURE = "the remote session sent no cure"
#: What a report that cannot be read as a verdict or a hand-back is recorded
#: as: never a verdict, never done.
UNMEASURED = "UNMEASURED"


def events_of_sid(journal, sid):
    return [e for e in journal if e.get("sid") == sid]


def _launch_of(events):
    return next((e for e in events if e.get("event") == "launch"
                 and e.get("sid")), {})


def _branch_for(events, label):
    """The branch a session was given for `label`: its launch's, or the
    re-read's that named the label."""
    branch = None
    for e in events:
        if e.get("label") == label and e.get("branch") and (
                e.get("event") == "launch" or (
                    e.get("event") == "deliver" and e.get("kind") == "reread")):
            branch = e["branch"]
    return branch


def _cure_source(report, events, tip):
    """(mbox, why-there-is-none, observed ref) through the one cure door."""
    launch = _launch_of(events)
    if launch.get("transport") == rs.TRANSPORT_BRANCH and launch.get("workdir"):
        branch = _branch_for(events, report["label"])
        if branch:
            mbox, sha, why = rs.branch_cure(launch["workdir"], tip, branch)
            observed = (rs.branch_names(branch)["cure"], sha) if sha else None
            if why:
                return None, _one_line("the cure branch was refused: " + why,
                                       250), observed
            if mbox:
                return mbox, None, observed
    if report.get("patch"):
        return report["patch"], None, None
    return None, NO_CURE, None


def _cleanup(journal, sid, rid):
    """Delete only refs this launch owned, at exact owned/observed shas.

    The atomic launch/re-read claims are the ownership proof. Cure/report
    observations may advance their expected sha. A pre-existing ref never has
    a claim; a concurrently moved ref fails its remote compare-and-delete lease
    and is preserved with the checkout.
    """
    import shutil
    events = events_of_sid(journal, sid)
    launch = _launch_of(events)
    workdir = launch.get("workdir")
    if launch.get("transport") == rs.TRANSPORT_BRANCH and workdir \
            and os.path.isdir(workdir):
        expected = {}
        for e in events:
            if isinstance(e.get("owned_refs"), dict):
                expected.update(e["owned_refs"])
            name, sha = e.get("branch_name"), e.get("branch_sha")
            if name and sha:
                expected[name] = sha
        gone = rs.delete_branches(workdir, expected,
                                  launch.get("push_repo")) if expected else {}
        rs.append({"event": "branches-deleted", "row": rid, "sid": sid,
                   "result": gone})
        if any(gone.values()):
            return              # keep evidence when any exact lease refuses
    root = os.path.realpath(rs.work_dir())
    for e in journal:
        path = e.get("workdir") if e.get("event") == "launch" \
            and e.get("row") == rid else None
        if path and os.path.realpath(path).startswith(root + os.sep):
            shutil.rmtree(path, ignore_errors=True)


def _calibration_candidate(row, seat_name, report):
    """Record what can be read of arm (i) on this tip: the remote read's own
    counts beside every other reader's recorded count. Which findings match is
    a judgement the relay does not make; `helm remote calibrate` records it."""
    from . import dispatches
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return
    others = [{"seat": r.get("recipient"), "polarity": r.get("polarity"),
               "finding_count": r.get("finding_count")}
              for r in current.values()
              if r.get("reviewed_tip") == row["tip"]
              and r.get("status") == "verdict"
              and str(r.get("recipient") or "").casefold() != seat_name
              and isinstance(r.get("finding_count"), int)]
    if others:
        rs.append({"event": "calibration-candidate", "row": row["id"],
                   "label": label_of(row), "tip": row["tip"],
                   "remote": {"count": report["count"],
                              "blocking": report["blocking"]},
                   "others": others})


def _tell_sender(row, seat_name, report, action, klass, verdict, comment):
    """The wake-back edge to the author: one DM naming what was recorded."""
    sender = row.get("sender")
    if not sender or sender == BOT:
        return
    text = ("@%s read %s at %s: %s, %d finding(s) — recorded as %s (%s%s). "
            "Findings: %s" % (
                seat_name, row.get("lane"), row["tip"][:12], report["verdict"],
                report["count"], action, klass,
                ("; still owed: " + verdict["owed"]) if verdict.get("owed")
                and klass != remote_policy.REVIEW_LEG else "",
                comment.get("url") or "the drop"))
    try:
        err = _dm(seat_name, sender, text)
    except Exception as exc:              # noqa: BLE001 — the ledger is the record
        err = "%s: %s" % (type(exc).__name__, exc)
    if err:
        rs.append({"event": "post-failed", "row": row["id"], "what": "dm",
                   "error": _one_line(err, 200)})


# ---------------------------------------------------------------------------
# the BUILD lane (task/3517): deliver, fetch, review elsewhere, record, cure
# ---------------------------------------------------------------------------
#
# A build row addressed to a cloud seat is delivered like a review, onto its
# own start branch at the row's tip. The relay then watches the push
# repository for the session's `-build` branch, fetches it into a LOCAL lane
# branch (a fetch: nothing is pushed anywhere but the PRIVATE push repository),
# and mints a review row for the same seat; that row prefers a standing
# session other than the builder's and, when only the builder's can serve it,
# is read there by one fresh-context subagent (FRESH_READ, carried by every
# standing review task). Its report is recorded by the ordinary
# path above. A FIX goes back to the builder's own session as a CURE build row
# on the same branches; an APPROVE is the source-clean hold auto-land already
# reads. The relay lands nothing itself.

#: How many cure rounds one build gets before the relay stops sending them.
MAX_CURE_ROUNDS = 3

#: what every standing review task carries (launch_row), so the read is one
#: fresh-context instance's wherever the row lands
FRESH_READ = (
    "Give the read to ONE fresh subagent, a fresh-context one that holds none "
    "of the build's working context (it did not build this lane, whichever "
    "session it runs in).")

REVIEW_BRIEF = (
    "Review the cloud build of lane %(lane)s: fetched from %(branch)s into "
    "%(ref)s at %(tip)s. " + FRESH_READ.replace("%", "%%") + " It reads the "
    "touched modules and their neighbours at the tip and at the base, runs "
    "the tests the change reaches, and reports every finding with file:line "
    "and severity (BLOCKING or MINOR), then APPROVE or FIX.")

CURE_BRIEF = (
    "CURE round %(round)d of lane %(lane)s (label %(label)s). The review of "
    "%(tip)s recorded FIX with %(count)d finding(s), %(blocking)d blocking: "
    "read comment %(comment)s on issue %(drop)s for them. Cure every BLOCKING "
    "finding (and the MINOR ones you agree with) on top of the reviewed tip, "
    "test what you change, and hand back exactly as before.")


#: the dispatch rows of the running tick, for walking a row's supersedes
#: chain back to the build it reads
_ROWS = {}


def _chain_ids(row):
    """`row`'s id and every id its supersedes chain names."""
    ids, cur = set(), row
    while cur is not None and cur.get("id") not in ids:
        ids.add(cur["id"])
        parent = cur.get("supersedes")
        if not parent:
            break
        cur = _ROWS.get(parent) or {"id": parent}
    return ids


def _builders_of(journal, row):
    """Every standing session that built the tip or lane `row` reads: the
    builder a hand-back names for any row of its supersedes chain, or for the
    same tip, ref or label, and the session of any build launch in that
    chain. A review prefers any other usable session, whichever row minted
    it; with none, the builder's own serves it through a fresh subagent."""
    ids, tip = _chain_ids(row), row.get("tip")
    ref, label = row.get("ref"), label_of(row)
    out = set()
    for e in journal:
        if e.get("event") == "handback" and (
                e.get("review_row") in ids or e.get("row") in ids
                or (tip and e.get("tip") == tip)
                or (ref and e.get("ref") == ref)
                or e.get("label") == label) and e.get("builder"):
            out.add(e["builder"])
        elif e.get("event") == "launch" and e.get("kind") == "build" \
                and e.get("row") in ids:
            sid = e.get("standing_sid") or e.get("sid")
            if sid:
                out.add(sid)
    return out


def _cure_of(journal, rid):
    """The cure a build row carries ({label, round, builder}), or None."""
    return next((e for e in journal if e.get("event") == "cure-sent"
                 and e.get("row") == rid), None)


def handle_build(row, seat_name, seat, project, journal, drops, dry, now,
                 open_ids):
    """What the relay does for ONE open build row this tick."""
    if rs.transport_of(project)[0] != rs.TRANSPORT_BRANCH:
        return _hold(row, seat_name, "a cloud build hands back a pushed branch, "
                     "and this project uses the bundle transport", journal, dry)
    back = next((e for e in journal if e.get("event") == "handback"
                 and e.get("row") == row["id"]), None)
    if back:
        return _close_build(row, seat_name, back, journal, dry)
    by_sid, by_row = rs.sessions(journal)
    sid = by_row.get(row["id"])
    if sid is None:
        return launch_build(row, seat_name, seat, project, journal, by_sid,
                            dry, now, _slots(by_row, open_ids))
    events = by_sid.get(sid, [])
    state, why = rs.infer_state(events, now, flat=_flat(journal, events))
    if state == rs.ARCHIVED:
        launch = _launch_of(events)
        workdir = launch.get("workdir")
        if workdir and os.path.isdir(workdir) and launch.get("build_branch"):
            # THE SESSION MAY HAVE PUSHED BEFORE IT WAS ARCHIVED: a moved
            # -build branch is the hand-back, never deleted by a relaunch.
            sha, why = rs.fetch_branch(workdir, launch["build_branch"])
            if why:
                return "push repository unread: " + why
            if sha and sha != launch.get("tip"):
                found = _look_handback(project, launch.get("label"),
                                       launch.get("round") or 0, drops, set())
                return handback(row, seat_name, project, sid, launch, sha,
                                found, journal, dry)
        if not dry and launch.get("workdir") and launch.get("owned_refs"):
            # the new launch claims the same start branch again
            rs.delete_branches(launch["workdir"], launch["owned_refs"],
                               launch.get("push_repo"))
        return launch_build(row, seat_name, seat, project, journal, by_sid,
                            dry, now, _slots(by_row, open_ids))
    if state == rs.ANSWERED or state in rs.LIVE or state == rs.UNDELIVERED:
        return attend_build(row, seat_name, project, sid, events, state,
                            journal, drops, dry, now)
    return _escalate(row, seat_name, sid, events, why, journal, dry)


def launch_build(row, seat_name, seat, project, journal, by_sid, dry, now,
                 sids=None):
    """The build's start branch at the row's tip, after the PRIVATE check,
    then its task: into the paying account's standing session, or, on the
    CLI opt-in, trust, token and a launch from the ordinary scratch clone."""
    from . import dispatches
    tip = row["tip"]
    cure = _cure_of(journal, row["id"])
    label = cure["label"] if cure else label_of(row)
    round_ = cure["round"] if cure else 0
    deferred = _gate(row, seat_name, journal, by_sid, dry, now, sids)
    if deferred:
        return deferred
    standing = rs.seat_transport(seat) == rs.SEAT_STANDING
    _transport, push_repo, push_url, prefix, _why = rs.transport_of(project)
    accounts, refusal = _payers(seat_name, seat, journal,
                                pin=(cure or {}).get("builder"))
    if not refusal and standing:
        accounts, refusal = _bound(accounts, push_repo)
    if refusal:
        return _hold(row, seat_name, refusal, journal, dry)
    account, reading, refusal = _choose(row, seat_name, accounts, journal, dry,
                                        now)
    if refusal:
        return refusal
    mode, mwhy = rs.permission_mode()
    if mwhy and not standing:
        return "launch refused: " + mwhy
    email, claude_home = account["email"], account["home"]
    names = rs.build_names(prefix, label)
    author = rs.build_author(project)
    if not author:
        return _hold(row, seat_name, "the project names no build_author (or "
                     "cure_author) as `Name <email>`, the identity every cloud "
                     "build commit carries", journal, dry)
    if dry:
        return "would %s the %s of %s on %s at %s ($%.2f left)" % (
            "deliver" if standing else "launch", rs.handback_header(round_),
            label, names["start"], tip[:12], reading.get("left") or 0)
    dest = os.path.join(rs.work_dir(), "sessions", "build-%s-%s" % (
        label_of(row), tip[:12]))
    info, why = rs.build_checkout(project["repo"], tip, dest, None, push_repo,
                                  push_url, names)
    if why:
        rs.append({"event": "launch-refused", "row": row["id"],
                   "seat": seat_name, "why": why})
        return "launch refused: " + why
    brief, _absent, _problem = dispatches.brief_of(row)
    task = rs.build_task_text(label, tip, brief, names, project["drop"], email,
                              seat["model"], seat.get("effort"), author,
                              round_, push_repo=push_repo)
    if standing:
        sid, url, why = _standing_start(row, seat_name, account, dest, task,
                                        info["owned_refs"], push_repo)
        if not sid:
            return why
    else:
        why = rs.mark_trusted(claude_home, dest)
        if why:
            rs.append({"event": "launch-refused", "row": row["id"],
                       "seat": seat_name, "why": why})
            return "launch refused: " + why
        sid, url, why = rs.launch(claude_home, seat["model"], dest, task,
                                  "build-" + label_of(row), mode=mode,
                                  bundle=False, effort=seat.get("effort"))
    rs.append({"event": "launch", "kind": "build", "row": row["id"],
               "seat": seat_name, "label": label, "round": round_, "tip": tip,
               "base": tip, "account": email, "home": claude_home,
               "model": seat["model"], "effort": seat.get("effort"),
               "transport": rs.TRANSPORT_BRANCH, "branch": names["start"],
               "build_branch": names["build"], "owned_refs": info["owned_refs"],
               "push_repo": push_repo, "push_url": push_url, "sid": sid,
               "url": url, "workdir": dest, "why": why,
               "standing_sid": account["standing"] if standing else None,
               "credit_left_before": reading.get("left"),
               "credit_basis": reading.get("basis")})
    if standing:
        return "delivered the %s of %s to standing session %s (%s)" % (
            rs.handback_header(round_), label, account["standing"], url)
    return ("launched build %s (%s)" % (sid, url)) if sid \
        else "launch failed: " + why


def _look_handback(project, label, round_, drops, seen):
    """('report', comment, hand-back) | ('malformed', comment, why) |
    ('unread', None, why) | None: the newest hand-back comment for this label
    and round on the project's drop. The comment is untrusted data."""
    spec = project["drop"]
    if spec not in drops:
        drops[spec] = rs.read_drop(spec)
    comments, why = drops[spec]
    if why:
        return ("unread", None, why)
    authors = project.get("drop_authors")
    found = None
    for c in comments:
        first = str(c.get("body") or "").lstrip("\ufeff").split("\n", 1)[0]
        m = rs.HANDBACK.match(first.rstrip("\r "))
        if not m or m.group("label") != label:
            continue
        n = 0 if m.group("kind") == "BUILD" else int(m.group("n") or 1)
        if n != round_ or (authors and c.get("author") not in authors):
            continue
        handback, why = rs.parse_handback(c["body"])
        if why:
            if c.get("id") not in seen:
                found = ("malformed", c, why)
            continue
        found = ("report", c, handback)
    return found


def attend_build(row, seat_name, project, sid, events, state, journal, drops,
                 dry, now):
    """A build that has not handed back: its pushed branch is the hand-back;
    a report without it is UNMEASURED; else correct, nudge or wait."""
    launch = _launch_of(events)
    label, round_ = launch.get("label"), launch.get("round") or 0
    names = {"start": launch.get("branch"), "build": launch.get("build_branch")}
    workdir = launch.get("workdir")
    if not workdir or not os.path.isdir(workdir):
        return _hold(row, seat_name, "the build's scratch checkout is gone; "
                     "send a new build row", journal, dry)
    account, claude_home = _session_account(events)
    target = rs.target_of(events)
    sha, why = rs.fetch_branch(workdir, names["build"])
    if why:
        return "push repository unread: " + why
    seen = {e.get("comment") for e in journal if e.get("row") == row["id"]
            and e.get("event") in ("report-malformed", "build-unmeasured")}
    found = _look_handback(project, label, round_, drops, seen)
    if sha and sha != launch.get("tip"):
        return handback(row, seat_name, project, sid, launch, sha, found,
                        journal, dry, events=events)
    if found and found[0] == "report":
        _kind, comment, report = found
        why = "the report names %s at %s, and %s is not on the push " \
            "repository" % (report["branch"], report["tip"][:12], names["build"])
        if comment.get("id") in seen:
            return "%s: %s" % (UNMEASURED, why)
        if dry:
            return "would record %s: %s" % (UNMEASURED, why)
        rs.append({"event": "build-unmeasured", "row": row["id"], "sid": sid,
                   "comment": comment.get("id"), "branch": report["branch"],
                   "tip": report["tip"], "record": UNMEASURED, "why": why})
        return "%s: %s; %s" % (UNMEASURED, why, _send(
            row, seat_name, sid, claude_home, "correction",
            rs.build_correction_message(label, names, "the branch it names "
                                        "is not on the push repository",
                                        round_), target, events))
    if found and found[0] == "malformed":
        _kind, comment, why = found
        if dry:
            return "would correct a malformed hand-back (%s)" % why
        rs.append({"event": "report-malformed", "row": row["id"], "sid": sid,
                   "comment": comment.get("id"), "why": why,
                   "record": UNMEASURED})
        return "%s (the hand-back did not parse: %s); %s" % (
            UNMEASURED, why, _send(
                row, seat_name, sid, claude_home, "correction",
                rs.build_correction_message(label, names, why, round_), target,
                events))
    if state == rs.UNDELIVERED:
        return _undelivered(row, seat_name, sid, events, journal, dry)
    if state == rs.SILENT_IDLE or rs.nudge_due(state, events, now):
        kind = "idle-nudge" if state == rs.SILENT_IDLE else "nudge"
        if dry:
            return "would nudge %s" % sid
        return _send(row, seat_name, sid, claude_home, kind,
                     rs.build_nudge_message(label, names, round_), target,
                     events)
    return "waiting (%s)" % state


def handback(row, seat_name, project, sid, launch, sha, found, journal, dry,
             events=None):
    """Fetch the pushed build into the local lane, mint its review row, then
    delete the build's remote branches and hold the build row naming the tip.
    In that order: a review row that could not be minted is tried again next
    tick with the branch still there."""
    import shutil
    label, start = launch.get("label"), launch.get("tip")
    workdir = launch["workdir"]
    names = {"start": launch.get("branch"), "build": launch.get("build_branch")}
    rc, _o, _e = rs._git(workdir, "merge-base", "--is-ancestor", start, sha)
    if rc != 0:
        return _hold(row, seat_name, "the build branch %s at %s does not "
                     "descend from its start %s" % (names["build"], sha[:12],
                                                     start[:12]), journal, dry)
    author = rs.build_author(project)
    bad = rs.verify_cure(workdir, start, author, head=sha) if author else \
        "the project names no build_author (or cure_author)"
    if bad:
        key, told = "verify:" + sha[:12], ""
        if not dry and not _once(journal, row, "build-unmeasured", key):
            rs.append({"event": "build-unmeasured", "row": row["id"],
                       "sid": sid, "key": key, "tip": sha,
                       "record": UNMEASURED, "why": _one_line(bad, 300)})
            # the builder hears why, once, under the same key
            if events:
                _account, claude_home = _session_account(events)
                told = "; " + _send(
                    row, seat_name, sid, claude_home, "correction",
                    rs.build_correction_message(
                        label, names, _one_line(bad, 200),
                        launch.get("round") or 0),
                    rs.target_of(events), events)
        return _hold(row, seat_name, "%s: the build %s at %s is not handed "
                     "back: %s" % (UNMEASURED, names["build"], sha[:12], bad),
                     journal, dry, key=key) + told
    if dry:
        return "would fetch %s at %s into lane/%s and hand it back" % (
            names["build"], sha[:12], label)
    ref, why = rs.fetch_lane(project["repo"], workdir, sha, label)
    if why:
        return _hold(row, seat_name, "the build could not be fetched: " + why,
                     journal, dry)
    review, why = _mint_review(row, seat_name, sha, names["build"], ref)
    if why:
        rs.append({"event": "handback-refused", "row": row["id"], "sid": sid,
                   "why": _one_line(why, 300)})
        return "hand-back review row refused: " + _one_line(why, 200)
    report = found[2] if found and found[0] == "report" else None
    gone = rs.delete_branches(workdir, {names["start"]: start,
                                        names["build"]: sha},
                              launch.get("push_repo"))
    back = {"event": "handback", "row": row["id"], "sid": sid, "label": label,
            "round": launch.get("round") or 0, "tip": sha, "ref": ref,
            "seat": seat_name,
            "review_row": review, "builder": _address([launch], sid),
            "report": found[1].get("id") if report else None,
            "report_tip": report["tip"] if report else None,
            "report_model": report["model"] if report else None,
            "report_effort": report["effort"] if report else None,
            "deleted": gone}
    rs.append(back)
    if not any(gone.values()):
        shutil.rmtree(workdir, ignore_errors=True)
    done = _close_build(row, seat_name, back, rs.read_journal(), dry)
    sender = row.get("sender")
    if sender and sender != BOT:
        try:
            _dm(seat_name, sender, "@%s built %s: %s at %s, fetched as %s; "
                "review row %s is out to another standing session." % (
                    seat_name, row.get("lane"), names["build"], sha[:12], ref,
                    review[:12]))
        except Exception as exc:          # noqa: BLE001 — the ledger is the record
            rs.append({"event": "post-failed", "row": row["id"], "what": "dm",
                       "error": _one_line("%s: %s" % (type(exc).__name__, exc),
                                          200)})
    return "handed back %s at %s as %s; review row %s; %s" % (
        names["build"], sha[:12], ref, review[:12], done)


def _close_build(row, seat_name, back, journal, dry):
    return _hold(row, seat_name, "cloud build handed back: %s at %s; review "
                 "row %s" % (back.get("ref"), back.get("tip"),
                             str(back.get("review_row"))[:12]), journal, dry,
                 key="handback:%s" % str(back.get("tip"))[:12])


def review_seat_of(seat_name):
    """The seat a build's review row is addressed to: the build seat's
    `review_seat` when it names one, else the build seat itself.

    WHY A SECOND NAME CAN MATTER. The ledger records the build seat as an
    author of the chain, so a review under the SAME seat name is, to the
    ledger, the author's own read: its APPROVE cannot be a source-clean hold
    and is recorded as an ordinary hold naming that refusal. A distinct
    review seat (its own accounts or the same ones; the relay prefers a
    session other than the builder's, and on the builder's own session the
    read is one fresh-context subagent's) is the chain's outsider."""
    seat, _why = rs.seat_of(seat_name)
    return str((seat or {}).get("review_seat") or seat_name).casefold()


def _mint_review(row, seat_name, sha, branch, ref):
    """(review row id, None) or (None, why): a review row for the review
    seat, minted by the relay (a seat never addresses itself), superseding
    the build row it reads. Keyed, so a retry reaches the same row."""
    from . import dispatches
    brief = REVIEW_BRIEF % {"lane": row.get("lane"), "branch": branch,
                            "ref": ref, "tip": sha[:12]}
    reviewer = review_seat_of(seat_name)
    if reviewer != seat_name:
        _seat, why = rs.seat_of(reviewer)
        if _seat is None:
            return None, "review_seat %s is not a remote seat that can be " \
                "driven: %s" % (reviewer, why or "not configured")
    with as_seat(BOT):
        posted, err, _existed = dispatches.send(
            reviewer, row.get("lane") or label_of(row), brief, sha,
            repo=row.get("repo_root"), kind="review", sign=False,
            supersedes=row["id"], key="cloud-review:" + row["id"])
    return ((posted or {}).get("id"), None) if posted else (
        None, err or "the dispatcher refused the review row")


def cure_back(row, seat_name, project, report, comment, journal):
    """A FIX on a build's review goes back to the BUILDER's session as a cure
    build row on the same branches, or None when this review read no cloud
    build. The brief names the comment by id: nothing from the drop's text is
    sent back into a session."""
    from . import dispatches
    back = next((e for e in journal if e.get("event") == "handback"
                 and e.get("review_row") == row["id"]), None)
    if not back:
        return None
    if any(e.get("event") == "cure-sent" and e.get("review_row") == row["id"]
           for e in journal):
        return None
    round_ = (back.get("round") or 0) + 1
    if round_ > MAX_CURE_ROUNDS:
        rs.append({"event": "cure-cap", "row": row["id"],
                   "label": back.get("label"), "round": round_})
        return "no cure sent: %d cure rounds is the cap" % MAX_CURE_ROUNDS
    brief = CURE_BRIEF % {"round": round_, "lane": row.get("lane"),
                          "label": back.get("label"), "tip": row["tip"][:12],
                          "count": report["count"],
                          "blocking": report["blocking"],
                          "comment": comment.get("id"),
                          "drop": project["drop"]}
    with as_seat(BOT):
        posted, err, _existed = dispatches.send(
            back.get("seat") or seat_name, row.get("lane") or back.get("label"),
            brief, row["tip"],
            repo=row.get("repo_root"), kind="build", sign=False,
            supersedes=row["id"], key="cloud-cure:" + row["id"])
    if not posted:
        rs.append({"event": "post-failed", "row": row["id"], "what": "cure",
                   "error": _one_line(err, 200)})
        return "cure row refused: " + _one_line(err, 200)
    rs.append({"event": "cure-sent", "row": posted["id"],
               "review_row": row["id"], "label": back.get("label"),
               "round": round_, "builder": back.get("builder")})
    return "cure round %d sent to the builder as %s" % (round_,
                                                        posted["id"][:12])


# ---------------------------------------------------------------------------
# status, calibration, timer, CLI
# ---------------------------------------------------------------------------

def status(now=None):
    cfg, why = rs.load_config()
    journal = rs.read_journal()
    readings = [e for e in journal if e.get("event") == "credit"]
    budget, bwhy = remote_credit.daily_budget(
        readings, now, scarce=remote_credit.max_pool_scarce())
    arm, arm_why = arm_state(journal)
    by_sid, by_row = rs.sessions(journal)
    rows = []
    for rid, sid in by_row.items():
        events = by_sid.get(sid, [])
        state, swhy = rs.infer_state(events, now, flat=_flat(journal, events))
        target = rs.target_of(events) or (None, None, None, None)
        recorded = next((e for e in reversed(journal)
                         if e.get("event") == "recorded"
                         and e.get("row") == rid), None)
        rows.append({"row": rid, "sid": sid, "state": state, "why": swhy,
                     "label": target[0], "tip": target[1],
                     "recorded": (recorded or {}).get("action")})
    accounts, standing = {}, {}
    gone = rs.archived_standing(journal)
    for seat_name, seat in ((cfg or {}).get("seats") or {}).items():
        for a in (seat.get("accounts") or ()) if isinstance(seat, dict) else ():
            last = next((r for r in reversed(readings)
                         if str(r.get("account") or "").casefold()
                         == str(a.get("email") or "").casefold()), None)
            accounts[a.get("email")] = last
            if a.get("standing_session"):
                standing[a.get("email")] = {
                    "sid": a["standing_session"],
                    "archived": a["standing_session"] in gone}
    return {"config": (cfg or {}).get("path"), "config_error": why,
            "enabled": rs.enabled(),
            "seats": sorted(((cfg or {}).get("seats") or {}).keys()),
            "accounts": accounts, "standing": standing,
            "floor": remote_credit.floor_usd(),
            "budget": budget, "budget_why": bwhy,
            "spent_today": remote_credit.spent_today(readings, now),
            "arm": arm, "arm_why": arm_why, "sessions": rows}


def calibrate(label, found, of, sample, tip, why, dry=False):
    """Record one blind calibration read, then evaluate the falsifiers."""
    if not (isinstance(found, int) and isinstance(of, int) and 0 <= found <= of
            and of > 0):
        return None, "--found N --of M needs 0 <= N <= M and M > 0"
    event = {"event": "calibration", "label": label, "tip": tip,
             "found": found, "of": of, "sample": sample or
             remote_policy.MAIN_SAMPLE, "why": _one_line(why, 300)}
    if not dry:
        rs.append(event)
    from . import dispatches
    current, _unavailable = dispatches.snapshot()
    tripped = check_falsifier(rs.read_journal(), current, dry)
    return {"calibration": event, "tripped": tripped}, None


_UNIT_SERVICE = """[Unit]
Description=helm remote — the relay for driven remote sessions

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
Environment=HELM_CHAT_NAME=%(bot)s
%(knobs)sUnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID CODEX_SESSION_ID
ExecStart=%(helm)s remote tick
"""
_UNIT_TIMER = """[Unit]
Description=helm remote relay cadence

[Timer]
OnBootSec=300
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _unit_knobs(environ=None):
    """The unit's Environment= lines: every HELM_REMOTE_* knob set where the
    timer is installed, and the absolute claude and gh programs, because a
    user unit's PATH is not a shell's. Values are the operator's own knobs,
    each written by timerhealth.env_assignment so systemd reads it exactly
    (task/3423); a value no Environment= line can carry (a control
    character) is left out rather than written into a unit file that would
    then mean something else.

    PADDING IS NOT PART OF A KNOB. Each value loses exactly the whitespace
    systemd strips from a line's ends (timerhealth._SYSTEMD_SPACE: space,
    tab, CR, LF; never an NBSP or a form feed, which systemd keeps), as the
    raw line on main let systemd strip it and as upstream_watch._knob strips
    its own: quoted exactly, `/usr/bin/gh ` would be a path that does not
    resolve. A program knob that is all padding falls back to PATH."""
    import shutil
    from . import timerhealth
    environ = os.environ if environ is None else environ
    lines = {}
    for key, value in sorted(environ.items()):
        if key.startswith("HELM_REMOTE_"):
            lines[key] = value.strip(timerhealth._SYSTEMD_SPACE)
    for key, prog in ((rs.CLAUDE_ENV, "claude"), (rs.GH_ENV, "gh")):
        found = lines.get(key) or shutil.which(prog)
        if found:
            lines[key] = found
    words = (timerhealth.env_assignment(k, v) for k, v in lines.items())
    return "".join("Environment=%s\n" % w for w in words if w)


def timer_units(interval=None, inputs=None):
    """(service path, service, timer path, timer): the relay's cadence, its
    interval HELM_REMOTE_TICK_INTERVAL_S's (else 600s) unless `interval` names
    one. `inputs` replaces per-install values (timerhealth.unit_values): the
    drift census renders this template with wildcards (task/3405)."""
    from . import timerhealth, work
    if interval is None:
        interval = rs._int_env(INTERVAL_ENV, 600)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = timerhealth.user_unit_dir()
    return (os.path.join(udir, "helm-remote-relay.service"),
            _UNIT_SERVICE % timerhealth.unit_values(
                {"cwd": cwd, "bot": BOT, "helm": helm_bin,
                 "knobs": _unit_knobs()}, inputs),
            os.path.join(udir, "helm-remote-relay.timer"),
            _UNIT_TIMER % timerhealth.unit_values({"interval": interval},
                                                  inputs))


def ensure_timer():
    """Install/refresh and enable the relay's cadence -> (ok, detail)."""
    import shutil
    from . import timerhealth
    interval = rs._int_env(INTERVAL_ENV, 600)
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl unavailable; run `helm remote tick` from " \
                      "another scheduler"
    spath, service, tpath, timer = timer_units(interval)
    error, _unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), ("helm-remote-relay.timer",),
        systemctl)
    if error:
        return False, error
    return True, "remote relay cadence enabled every %ds" % interval


def _opt(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def cmd_remote(args):
    from . import cli
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    verb, rest = args[0], args[1:]
    prog = "helm remote " + verb
    if verb == "status":
        rc = cli.guard_tail(prog, rest, flags=("--json",), usage=_USAGE)
        if rc is not None:
            return rc
        st = status()
        if "--json" in rest:
            print(json.dumps(st, indent=1, sort_keys=True))
            return 0
        print("remote sessions %s; config %s%s" % (
            "on" if st["enabled"] else "OFF", st["config"],
            (" (UNREADABLE: %s)" % st["config_error"]) if st["config_error"]
            else ""))
        print("seats: %s" % (", ".join(st["seats"]) or "none"))
        for email, r in sorted(st["accounts"].items()):
            print("  " + (remote_credit.reading_line(r) if r
                          else "%s: never read" % email))
        for email, one in sorted(st["standing"].items()):
            print("  %s: standing session %s%s" % (
                email, one["sid"],
                " (standing session archived)" if one["archived"] else ""))
        print("floor $%.2f; today $%.2f spent of %s (%s)" % (
            st["floor"], st["spent_today"],
            "$%.2f" % st["budget"] if st["budget"] is not None else "no budget",
            st["budget_why"]))
        print("same-model arm %s: %s" % (st["arm"], st["arm_why"]))
        for s in st["sessions"]:
            print("  %s %-9s %s %s %s%s" % (
                s["row"][:12], s["state"], s["sid"], s["label"] or "",
                (s["tip"] or "")[:12],
                (" recorded:" + s["recorded"]) if s["recorded"] else ""))
        return 0
    if verb == "tick":
        rc = cli.guard_tail(prog, rest, flags=("--dry-run", "--json"),
                            usage=_USAGE)
        if rc is not None:
            return rc
        out = tick(dry="--dry-run" in rest)
        if "--json" in rest:
            print(json.dumps(out, indent=1))
            return 0
        for note in out["notes"]:
            print("helm remote: " + note)
        for rid, action in out["actions"]:
            print("  %s %s" % (rid[:12], action))
        return 0
    if verb == "policy":
        rc = cli.guard_tail(prog, rest, usage=_USAGE)
        if rc is not None:
            return rc
        for line in remote_policy.table_lines():
            print(line)
        return 0
    if verb == "parse":
        if len(rest) != 1:
            print(_USAGE, file=sys.stderr)
            return 2
        try:
            if rest[0] == "-":
                body = sys.stdin.read()
            else:
                with open(rest[0], encoding="utf-8") as f:
                    body = f.read()
        except OSError as exc:
            print("helm remote parse: %s" % exc, file=sys.stderr)
            return 1
        report, why = rs.parse_report(body)
        if why:
            print("helm remote parse: REFUSED: %s" % why, file=sys.stderr)
            return 1
        report = dict(report, patch=bool(report.get("patch")))
        print(json.dumps(report, indent=1))
        return 0
    if verb == "calibrate":
        if not rest or rest[0].startswith("-"):
            print(_USAGE, file=sys.stderr)
            return 2
        rc = cli.guard_tail(prog, rest[1:], valued=(
            "--found", "--of", "--sample", "--tip", "--why"), usage=_USAGE)
        if rc is not None:
            return rc
        try:
            found, of = int(_opt(rest, "--found")), int(_opt(rest, "--of"))
        except (TypeError, ValueError):
            print("helm remote calibrate: --found N --of M are required "
                  "integers", file=sys.stderr)
            return 2
        out, why = calibrate(rest[0], found, of, _opt(rest, "--sample"),
                             _opt(rest, "--tip"), _opt(rest, "--why") or "")
        if why:
            print("helm remote calibrate: " + why, file=sys.stderr)
            return 2
        print("helm remote: calibration recorded (%d of %d)%s" % (
            found, of, "; FALSIFIER TRIPPED: %s" % out["tripped"]["detail"]
            if out["tripped"] else ""))
        return 0
    if verb == "falsifier":
        if rest[:1] == ["reset"]:
            rc = cli.guard_tail(prog + " reset", rest[1:], valued=("--why",),
                                usage=_USAGE)
            if rc is not None:
                return rc
            why = _opt(rest, "--why")
            if not why:
                print("helm remote falsifier reset: --why TEXT is required",
                      file=sys.stderr)
                return 2
            rs.append({"event": "falsifier-reset", "why": _one_line(why, 300)})
            print("helm remote: same-model arm re-armed; evidence counts from "
                  "now")
            return 0
        rc = cli.guard_tail(prog, rest, usage=_USAGE)
        if rc is not None:
            return rc
        journal = rs.read_journal()
        arm, why = arm_state(journal)
        print("same-model arm %s: %s" % (arm, why))
        _latched, since = falsifier_state(journal)
        counted = [e for e in journal if str(e.get("ts") or "") >= since]
        records = [e for e in counted if e.get("event") == "calibration"]
        print("calibration: %s" % remote_policy.calibration_trip(records)[2])
        print("contradictions counted: %d" % sum(
            1 for e in counted if e.get("event") == "contradiction"))
        return 0
    if verb == "ensure-timer":
        rc = cli.guard_tail(prog, rest, usage=_USAGE)
        if rc is not None:
            return rc
        ok, detail = ensure_timer()
        print("helm remote: " + detail, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    print("helm remote: unknown subverb %r\n%s" % (verb, _USAGE),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cmd_remote(sys.argv[1:]))
