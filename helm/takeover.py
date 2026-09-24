#!/usr/bin/env python3
"""Evidence-bound BUILD continuation for an unavailable task incumbent.

A takeover is an authorization gate, not a liveness dashboard. Diagnostic code
may treat an absent optional signal as "nothing found" so that a report stays
available; this gate does the opposite because its answer mutates ownership.
Missing, unreadable, stale, malformed, or mismatched register, proxywatch, claim,
lineage, or task evidence is UNKNOWN and therefore REFUSES. UNKNOWN is not a
claim that the incumbent is dead. No reply and claim age are deliberately absent
from the decision: neither is a measured contradiction.

The only admitted contradictions are proxywatch's typed ``hung`` and ``starved``
states, ``compact-needed`` normalized here to ``context-full``, and ``idle`` only
when pending and in-flight are both measured exactly zero and the same
register-bound worktree claim is expired. One fresh bundle binds the exact spawn
occurrence, typed socket/pending census, and complete claim identity. The bundle
is remeasured under the spawn and claim locks immediately before tasks.py
compare-and-swaps the task snapshot.

The transaction is intentionally one-way and narrow:

* append a durable PREPARED transfer row;
* idempotently send the incumbent a DM and a room @mention under event ids derived
  from the same caller-owned ``transfer_id``;
* remeasure evidence and lineage, then use an opaque BUILD-only authorization at
  the task owner boundary;
* append the terminal transfer phase (the task snapshot itself remains the
  authoritative commit record if that final receipt is interrupted).

Any failure before the task append remains visible in the prepare ledger and
moves no ownership. Source branches and worktrees are read only: WIP is carried
by a distinct descendant successor tip or by an explicit superseding-lane link;
this module never resets, rebases, deletes, releases, force-updates, signals,
resumes, or otherwise repairs the incumbent.
"""
import hashlib
import collections
import json
import math
import os
import re
import time

from . import chat, eventledger, home, proxywatch, seat, seat_lifecycle, seats
from . import tasks, work
from . import pk

EVIDENCE_MAX_AGE_S = 30
TRANSFER_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,96}\Z")
SEAT_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_MINT = object()


class TakeoverRefused(ValueError):
    pass


class BuildContinuationAuthorization(object):
    """Opaque authority for one exact task BUILD-owner CAS, and nothing else."""
    __slots__ = ("task_id", "before", "incumbent", "successor", "fields",
                 "record", "minted_at", "scope")

    def __init__(self, task_id=None, before=None, incumbent=None, successor=None,
                 fields=None, record=None, minted_at=None, *, mint=None):
        if mint is not _MINT:
            raise TakeoverRefused(
                "takeover authorization is minted only from a fresh measured "
                "bundle; direct construction cannot authorize ownership")
        self.task_id = task_id
        self.before = before
        self.incumbent = incumbent
        self.successor = successor
        self.fields = dict(fields)
        self.record = dict(record)
        self.minted_at = minted_at
        self.scope = "task-build-continuation"


def transfer_path(path=None):
    return path or os.path.join(home.global_dir(), "task-takeovers.jsonl")


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TakeoverRefused("evidence cannot be canonically bound (%s) — UNKNOWN"
                              % type(exc).__name__)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _task_digest(row):
    return _digest(row)


def _strict_task(token, path=None):
    tid = tasks.normalize_id(token)
    if not tid:
        return None, "unparseable task id %r" % (token,)
    known, unavailable = tasks.snapshot(path, strict=True)
    if unavailable:
        return None, "task ledger unreadable (%s)" % unavailable
    row = known.get(tid)
    if not row:
        return None, "%s does not exist" % tid
    return row, None


def _strict_spawn_record(d):
    path = seat_lifecycle._spawn_path(d)
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            rec = json.load(f, object_pairs_hook=seats._unique_json_object)
    except FileNotFoundError:
        raise TakeoverRefused("spawn register is absent — occurrence UNKNOWN")
    except (OSError, ValueError, TypeError) as exc:
        raise TakeoverRefused("spawn register is unreadable or malformed (%s) — "
                              "occurrence UNKNOWN" % type(exc).__name__)
    if not isinstance(rec, dict):
        raise TakeoverRefused("spawn register is malformed (expected object) — "
                              "occurrence UNKNOWN")
    return rec


def _occurrence(rec, incumbent, source_path):
    if rec.get("seat") != incumbent:
        raise TakeoverRefused("spawn register names %r, not incumbent %r — "
                              "occurrence mismatch" % (rec.get("seat"), incumbent))
    harness = rec.get("harness")
    session = rec.get("session")
    registered = rec.get("room_worktree") or rec.get("worktree")
    room = rec.get("room")
    if not isinstance(harness, str) or not harness:
        raise TakeoverRefused("spawn register has no typed harness — occurrence UNKNOWN")
    if not isinstance(session, str) or not session:
        raise TakeoverRefused("spawn register has no bound session — occurrence UNKNOWN")
    if not isinstance(registered, str) or not registered:
        raise TakeoverRefused("spawn register has no worktree — occurrence UNKNOWN")
    if os.path.realpath(registered) != os.path.realpath(source_path):
        raise TakeoverRefused("spawn register worktree does not match source lane — "
                              "occurrence mismatch")
    if not isinstance(room, str) or not room:
        raise TakeoverRefused("spawn register has no room for the owed @mention — "
                              "notification target UNKNOWN")
    out = {"seat": incumbent, "harness": harness, "session": session,
           "worktree": os.path.realpath(registered), "room": room}
    if harness == "headless":
        pid, identity = rec.get("pid"), rec.get("pid_identity")
        if type(pid) is not int or pid <= 0 or not isinstance(identity, str) \
                or not identity:
            raise TakeoverRefused("headless spawn register lacks complete pid "
                                  "identity — occurrence UNKNOWN")
        out.update({"pid": pid, "pid_identity": identity})
    else:
        handle = rec.get("handle")
        if not isinstance(handle, str) or not handle:
            raise TakeoverRefused("pane spawn register has no exact handle — "
                                  "occurrence UNKNOWN")
        out["handle"] = handle
        for key in ("pane_key", "pty_id"):
            if rec.get(key) is not None:
                if not isinstance(rec[key], str) or not rec[key]:
                    raise TakeoverRefused("spawn register %s is malformed — "
                                          "occurrence UNKNOWN" % key)
                out[key] = rec[key]
    return out


def _prove_occurrence(incumbent, d, occurrence):
    if occurrence["harness"] == "headless":
        try:
            identity = seat._pid_identity(occurrence["pid"])
        except Exception as exc:
            raise TakeoverRefused("headless occurrence cannot be measured (%s) — "
                                  "UNKNOWN" % type(exc).__name__)
        if identity is None:
            raise TakeoverRefused("registered headless occurrence is absent — UNKNOWN")
        if identity != occurrence["pid_identity"]:
            raise TakeoverRefused("registered headless pid was reused — occurrence "
                                  "mismatch")
        return "headless pid identity verified"
    try:
        adapter, handle, detail = seat._resolve_registered_pane(
            incumbent, d=d, repair=False, locked=True)
    except Exception as exc:
        raise TakeoverRefused("registered pane occurrence cannot be measured (%s) "
                              "— UNKNOWN" % type(exc).__name__)
    if adapter is None or handle is None:
        raise TakeoverRefused((detail or "registered pane occurrence unavailable") +
                              " — exact occurrence UNKNOWN")
    if handle != occurrence["handle"]:
        raise TakeoverRefused("identity resolved a replacement pane while the exact "
                              "register still names %s — occurrence mismatch"
                              % occurrence["handle"])
    return detail


def _claim(state, resource, incumbent, occurrence, now_mono, now_wall=None):
    row = state.get(resource)
    if not isinstance(row, dict):
        raise TakeoverRefused("bound source-lane claim is absent — claim UNKNOWN")
    if row.get("holder") != incumbent:
        raise TakeoverRefused("source-lane claim holder does not match incumbent")
    if not row.get("session") or row.get("session") != occurrence["session"]:
        raise TakeoverRefused("source-lane claim session does not match the exact "
                              "spawn occurrence")
    exp_mono, exp_wall = row.get("exp_mono"), row.get("exp_wall")
    numeric = lambda v: type(v) in (int, float) and math.isfinite(v)
    if not numeric(exp_mono) or not numeric(exp_wall):
        raise TakeoverRefused("source-lane claim expiry is malformed — claim UNKNOWN")
    now_wall = time.time() if now_wall is None else now_wall
    mono_expired, wall_expired = now_mono >= exp_mono, now_wall >= exp_wall
    if mono_expired != wall_expired:
        raise TakeoverRefused("claim clocks disagree about expiry — claim UNKNOWN")
    return {"resource": resource, "holder": row["holder"],
            "session": row["session"], "lease": row["lease"],
            "fence": row["fence"], "exp_mono": exp_mono,
            "exp_wall": exp_wall, "expired": mono_expired}


def _proxy(incumbent, now_wall):
    try:
        report = proxywatch.health(seats=[incumbent], include_upstream=False,
                                   include_probe=False)
    except Exception as exc:
        raise TakeoverRefused("proxywatch evidence is unreadable (%s) — UNKNOWN"
                              % type(exc).__name__)
    observed = report.get("ts") if isinstance(report, dict) else None
    rows = report.get("seats") if isinstance(report, dict) else None
    if type(observed) not in (int, float) or not math.isfinite(observed) \
            or observed > now_wall + 2 or now_wall - observed > EVIDENCE_MAX_AGE_S:
        raise TakeoverRefused("proxywatch evidence is absent or stale — UNKNOWN")
    if not isinstance(rows, list) or len(rows) != 1 \
            or not isinstance(rows[0], dict) or rows[0].get("seat") != incumbent:
        raise TakeoverRefused("proxywatch sample does not identify exactly the "
                              "incumbent — evidence mismatch")
    row = rows[0]
    raw_state = row.get("turn_state")
    state = "context-full" if raw_state == "compact-needed" else raw_state
    inflight = row.get("inflight")
    if state in ("hung", "starved", "context-full", "idle") \
            and (type(inflight) is not int or inflight < 0):
        raise TakeoverRefused("proxywatch socket census is unreadable — UNKNOWN")
    if row.get("pane_live") is not True:
        raise TakeoverRefused("proxywatch did not measure the incumbent pane LIVE "
                              "— UNKNOWN cannot authorize takeover")
    pending_after = row.get("pending_after")
    open_dispatches = row.get("open_dispatches")
    pending = None
    if type(pending_after) is bool and type(open_dispatches) is int \
            and open_dispatches >= 0:
        pending = int(pending_after) + open_dispatches
    reality = row.get("transcript_reality")
    semantic_ts = reality.get("semantic_ts") if isinstance(reality, dict) else None
    return {"reported_at": observed, "state": state,
            "proxywatch_state": raw_state, "pane_live": True,
            "inflight": inflight, "pending": pending,
            "pending_after": pending_after,
            "open_dispatches": open_dispatches,
            "turn_complete": row.get("turn_complete"),
            "turn_evidence": row.get("turn_evidence"),
            "semantic_ts": semantic_ts,
            "transcript_age_s": row.get("transcript_age_s"),
            "write_age_s": row.get("write_age_s"),
            "semantic_kind": row.get("semantic_kind"),
            "ctx_pct": row.get("ctx_pct"),
            "spawn_age_s": row.get("spawn_age_s"),
            "log": row.get("log")}


def _admissible(proxy, claim):
    state = proxy.get("state")
    if state in ("hung", "starved", "context-full"):
        if type(proxy.get("inflight")) is not int or proxy["inflight"] < 0:
            raise TakeoverRefused("proxywatch socket census is unreadable — UNKNOWN")
        return state
    if state == "idle" and proxy.get("pending") == 0 \
            and proxy.get("inflight") == 0 and claim.get("expired") is True:
        return "idle-expired"
    if state == "idle":
        raise TakeoverRefused("idle authorizes only with pending=0, inflight=0, "
                              "and the bound claim expired")
    if state == "unonboarded":
        # REFUSED ALREADY BY THE CATCH-ALL BELOW — this branch only makes the
        # SENTENCE true, and the sentence is what an operator acts on. Calling
        # this "not a measured contradiction" is false: it is one, and it is
        # more specific than anything takeover can offer. The seat's latest
        # launch never got its brief in, so replacing the pane would DISCARD an
        # unsent brief that one guarded submit can still recover.
        #
        # THIS IS ALSO THE SEAM IT PROTECTS. Without the unonboarded rung
        # such a seat reports compact-needed whenever its transcript sits over
        # the context bar, and `context-full` authorizes on a readable socket
        # census ALONE — no claim expiry, no pending census. A failed relaunch
        # onto an old high-context transcript would then be taken over, and
        # the pending brief destroyed, on evidence that never mentioned it.
        raise TakeoverRefused(
            "the incumbent has completed no turn since its latest launch "
            "recorded the onboarding brief as NOT PROVEN submitted, so there "
            "is nothing to take over: replacing the pane DISCARDS a brief that "
            "may still be recoverable. Read the pane first (`helm seat "
            "composers`) and repair it with `--submit`, which refuses any "
            "composer it cannot identify")
    if state == "onboarding-unreadable":
        # REFUSED ALREADY BY THE CATCH-ALL BELOW — this branch exists to make
        # the SENTENCE true, and the sentence is what an operator acts on.
        # "not a measured contradiction" is exactly wrong here: the register IS
        # present and does NOT parse, which is a contradiction helm measured.
        #
        # AND THIS IS THE SEAM IT PROTECTS. Without a ladder rung producing
        # this state, an unreadable register leaves the verdict to whatever
        # else the row says — and `context-full` authorizes on a readable
        # socket census ALONE, with no claim expiry and no pending census. A
        # seat whose transcript sits over the context bar would then be taken
        # over while helm cannot read whether its last brief was delivered,
        # and taking over replaces the pane.
        raise TakeoverRefused(
            "the incumbent's onboarding register is present and does not "
            "parse, so whether its latest brief was delivered is UNKNOWN — and "
            "replacing the pane would discard whatever that brief left in the "
            "composer. UNKNOWN cannot authorize takeover. Read the pane first "
            "(`helm seat composers`), which classifies the composer without "
            "typing into it")
    raise TakeoverRefused("proxywatch state %r is not a measured contradiction; "
                          "quiet, no reply, and claim age do not authorize"
                          % state)


def _source_worktree(root, source_lane):
    """The exact live source room whose uncommitted WIP must remain reachable."""
    path = work.lane_path(root, source_lane)
    rc, top, err = work._git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or os.path.realpath(top or "") != os.path.realpath(path):
        raise TakeoverRefused("source worktree cannot be verified (%s) — WIP "
                              "preservation UNKNOWN" % (err or top or "absent"))
    branch = work.lane_branch(source_lane)
    rc, current, err = work._git(path, "symbolic-ref", "--quiet", "--short",
                                 "HEAD")
    if rc != 0 or current != branch:
        raise TakeoverRefused("source worktree is not attached to branch %s (%s) — "
                              "WIP lineage UNKNOWN"
                              % (branch, err or current or "detached"))
    if _git_commit(path, "HEAD") != _git_commit(root, branch):
        raise TakeoverRefused("source worktree HEAD and source branch disagree — "
                              "WIP lineage UNKNOWN")
    return path


def _capture_locked(incumbent, root, source_lane, d):
    source_path = _source_worktree(root, source_lane)
    occurrence = _occurrence(_strict_spawn_record(d), incumbent, source_path)
    occurrence["proof"] = _prove_occurrence(incumbent, d, occurrence)
    try:
        claim_state = seats._claims_read(True)
    except OSError as exc:
        raise TakeoverRefused(str(exc) + " — claim UNKNOWN")
    now_mono, now_wall = seats._now_mono(), time.time()
    claim = _claim(claim_state, work.resource(root, source_lane), incumbent,
                   occurrence, now_mono, now_wall)
    proxy = _proxy(incumbent, now_wall)
    contradiction = _admissible(proxy, claim)
    return {"v": 1, "observed_at": now_wall, "incumbent": incumbent,
            "occurrence": occurrence, "proxywatch": proxy, "claim": claim,
            "contradiction": contradiction}


def _seat_dir(incumbent):
    family, err = seat._seat_family(incumbent)
    if err:
        raise TakeoverRefused("incumbent has no exact Helm seat register (%s) — "
                              "occurrence UNKNOWN" % err)
    return seat._instance_dir(family, incumbent)


def capture(incumbent, root, source_lane):
    """Capture one fresh bundle under the exact occurrence and claim locks."""
    d = _seat_dir(incumbent)
    with seat_lifecycle._seat_lifecycle_lock(d):
        with seats._claim_flocked() as lock:
            if lock.f is None:
                raise TakeoverRefused(seats._lock_unavailable() +
                                      " — claim UNKNOWN")
            return _capture_locked(incumbent, root, source_lane, d)


def _bundle_anchor(bundle):
    proxy = bundle["proxywatch"]
    return _digest({"incumbent": bundle["incumbent"],
                    "occurrence": bundle["occurrence"],
                    "claim": {k: bundle["claim"][k] for k in
                              ("resource", "holder", "session", "lease", "fence",
                               "exp_mono", "exp_wall")},
                    # Bind the typed state, exact socket/pending census, and the
                    # completed-turn identity — not ages that legitimately tick
                    # between prepare and final measurement. A new completed turn
                    # changes semantic_ts; mere passage of seconds does not turn a
                    # stable contradiction into a false race.
                    "proxywatch": {k: proxy.get(k) for k in
                                   ("state", "proxywatch_state", "pane_live",
                                    "inflight", "pending", "pending_after",
                                    "open_dispatches", "turn_complete",
                                    "semantic_ts", "semantic_kind", "log")},
                    "contradiction": bundle["contradiction"]})


def _git_commit(root, ref):
    rc, out, err = work._git(root, "rev-parse", "--verify", ref + "^{commit}")
    if rc != 0 or not re.match(r"\A[0-9a-f]{40,64}\Z", out or ""):
        raise TakeoverRefused("cannot resolve %s (%s) — lineage UNKNOWN"
                              % (ref, err or "no commit"))
    return out


def lineage(root, source_lane, successor_lane, superseding=False):
    if source_lane == successor_lane:
        raise TakeoverRefused("takeover needs a distinct successor lane")
    source_branch, target_branch = (work.lane_branch(source_lane),
                                    work.lane_branch(successor_lane))
    source_tip = _git_commit(root, source_branch)
    target_tip = _git_commit(root, target_branch)
    # `root` is the MAIN checkout by contract. Asking HEAD there measures main,
    # not the caller's successor room, and therefore refused every real takeover
    # while same-checkout tests passed. The inferred lane already binds the caller
    # to this deterministic room path; measure that worktree's attachment and HEAD.
    target_path = work.lane_path(root, successor_lane)
    rc, current, err = work._git(target_path, "symbolic-ref", "--quiet", "--short",
                                 "HEAD")
    if rc != 0 or current != target_branch:
        raise TakeoverRefused("successor worktree is not attached to branch %s — "
                              "lineage mismatch (%s)"
                              % (target_branch, err or current or "detached"))
    head = _git_commit(target_path, "HEAD")
    if head != target_tip:
        raise TakeoverRefused("successor branch moved away from current HEAD — "
                              "lineage UNKNOWN")
    mode = "superseding" if superseding else "descendant"
    if not superseding:
        if source_tip == target_tip:
            raise TakeoverRefused("successor has no descendant commit carrying the "
                                  "source tip; explicitly link a superseding lane "
                                  "instead if continuation is not descendant")
        rc, _out, err = work._git(root, "merge-base", "--is-ancestor",
                                  source_tip, target_tip)
        if rc == 1:
            raise TakeoverRefused("successor tip is not a descendant of the source "
                                  "tip; use an explicit superseding-lane link")
        if rc != 0:
            raise TakeoverRefused("cannot verify descendant lineage (%s) — UNKNOWN"
                                  % (err or "git failed"))
    return {"mode": mode, "source_lane": source_lane,
            "source_branch": source_branch, "source_tip": source_tip,
            "successor_lane": successor_lane,
            "successor_branch": target_branch, "successor_tip": target_tip}


def _transfer_id(value):
    if not isinstance(value, str):
        raise TakeoverRefused("transfer_id must be a string")
    value = value.strip()
    if not TRANSFER_ID_RE.match(value):
        raise TakeoverRefused("transfer_id must be 1-96 characters from "
                              "[A-Za-z0-9._-]")
    return value


def _latest(transfer_id, path=None):
    known, unavailable = eventledger.latest_checked(transfer_path(path), strict=True)
    if unavailable:
        raise TakeoverRefused("takeover ledger unreadable (%s) — transfer UNKNOWN"
                              % unavailable)
    return known.get("takeover/" + transfer_id)


def _append_phase(record, phase, path=None, **fields):
    p = transfer_path(path)
    with eventledger.locked(p) as held:
        if not held:
            raise TakeoverRefused("takeover ledger is not writable")
        known, unavailable = eventledger.latest_checked(p, strict=True)
        if unavailable:
            raise TakeoverRefused("takeover ledger unreadable (%s)" % unavailable)
        prior = known.get(record["id"])
        if prior and (prior.get("phase") == "committed" or phase == "prepared"):
            return prior
        row = dict(prior or record)
        row.update(fields)
        row["phase"] = phase
        row["last_updated"] = time.time()
        if not eventledger.append_unlocked(p, row):
            raise TakeoverRefused("takeover ledger refused the %s receipt" % phase)
    return row


def _mark_failed(record, why, path=None):
    try:
        _append_phase(record, "failed", path=path, failure=str(why)[:1000])
    except Exception:
        pass


def _validate_record(record):
    strings = ("id", "transfer_id", "task_id", "source_lane", "successor_lane",
               "successor", "incumbent", "room", "task_before",
               "evidence_anchor")
    if not isinstance(record, dict) or any(
            not isinstance(record.get(key), str) or not record[key]
            for key in strings) or record.get("id") != "takeover/" + record.get(
                "transfer_id", "") or type(record.get("superseding")) is not bool \
            or not isinstance(record.get("lineage"), dict) \
            or not isinstance(record.get("evidence"), dict) \
            or not isinstance(record.get("notification_events"), dict) \
            or set(record["notification_events"]) != {"direct_message", "mention"} \
            or any(not isinstance(value, str) or not value
                   for value in record["notification_events"].values()):
        raise TakeoverRefused("takeover prepare row is malformed — transfer UNKNOWN")


def _same_request(record, request):
    keys = ("task_id", "source_lane", "successor_lane", "successor",
            "superseding")
    return isinstance(record, dict) and all(
        record.get(k) == request.get(k) for k in keys)


def _committed_task(record, task_path=None):
    row, err = _strict_task(record["task_id"], task_path)
    if err:
        return None
    proof = row.get("takeover") or {}
    if proof.get("transfer_id") != record["transfer_id"]:
        return None
    if tasks.owner_of(row) != record["successor"] or row.get("status") != "in_progress":
        raise TakeoverRefused("task carries transfer %s but no longer has its "
                              "committed BUILD owner/state" % record["transfer_id"])
    return row


def _notify(record):
    text = ("BUILD continuation transfer %s PREPARED: %s will continue %s from "
            "lane %s in lane %s only if the immediate fresh evidence recheck and "
            "task compare-and-swap commit. Check the task's takeover record for "
            "the outcome. This can transfer task BUILD ownership only; it grants "
            "no review, fold, release, or land authority. The source branch and "
            "worktree are untouched."
            % (record["transfer_id"], record["successor"], record["task_id"],
               record["source_lane"], record["successor_lane"]))
    try:
        dm = chat.post(text, who=record["successor"], dm=record["incumbent"],
                       dm_display=record["incumbent"], sign=False,
                       event_id=record["notification_events"]["direct_message"])
        mention = chat.post("@%s %s" % (record["incumbent"], text),
                            room=record["room"], who=record["successor"], sign=False,
                            event_id=record["notification_events"]["mention"])
    except Exception as exc:
        raise TakeoverRefused("chat transfer incomplete (%s); retry the same "
                              "transfer_id" % type(exc).__name__)
    if not isinstance(dm, dict) or not dm.get("id") \
            or not isinstance(mention, dict) or not mention.get("id"):
        raise TakeoverRefused("chat transfer did not return both durable receipts")
    return {"direct_message": dm["id"], "mention": mention["id"],
            "room": record["room"],
            "events": dict(record["notification_events"])}


def _mint(task_id, before, incumbent, successor, fields, record):
    return BuildContinuationAuthorization(
        task_id, _task_digest(before), incumbent, successor, fields, record,
        time.time(), mint=_MINT)


_REASSIGN_MINT = object()

# A reassignment's evidence is a liveness measurement, which is exactly as
# perishable as takeover's bundle, so it expires on the same clock rather than
# inventing a second policy.
REASSIGN_MAX_AGE_S = EVIDENCE_MAX_AGE_S


_DISPOSITION_MINT = object()


class SourceDisposition(object):
    """ONE measurement of ONE seat's liveness, taken by the controller.

    TWO FINDINGS COLLAPSE INTO THIS OBJECT. The custody mint took
    `state` and `why` as PLAIN ARGUMENTS, so a caller could simply assert
    SOURCE_DEAD about a live seat — the evidence was whatever the caller
    typed. And the task mint went the other way, RE-MEASURING
    `source_disposition` per task, so a seat that died or revived mid-loop
    could split one command into two verdicts and move half a seat's rows
    under one disposition and half under another.

    The fix is the same for both: measure ONCE, carry the measurement, and
    make it unspellable. This object cannot be constructed outside this module
    — same shape as `_REASSIGN_MINT` and dispatches' `_MOVE_MINT` — so a door
    that requires one cannot be satisfied by a value.
    """
    __slots__ = ("seat", "state", "why", "measured", "measured_at")

    def __init__(self, seat=None, state=None, why=None, measured=False,
                 measured_at=None, *, mint=None):
        if mint is not _DISPOSITION_MINT:
            raise TakeoverRefused(
                "a source disposition is MEASURED, never asserted; it cannot "
                "be constructed by a caller")
        self.seat, self.state, self.why = seat, state, why
        self.measured, self.measured_at = bool(measured), measured_at


def mint_source_disposition(seat):
    """(disposition, error) — measure this seat ONCE, for a whole reassignment.

    An UNMEASURABLE seat still mints: live evidence is a VETO and never a
    requirement, because the seat nothing can measure is the orphan the whole
    verb exists for. What it may not do is claim death it did not observe, so
    `measured` records which of those happened.
    """
    name = str(seat or "").strip()
    if not name:
        return None, "a disposition needs a seat to measure"
    from . import seat_reassign
    try:
        state, why = seat_reassign.source_disposition(name)
        measured = True
    except Exception as exc:                 # noqa: BLE001
        state, why, measured = None, "liveness unmeasurable (%s)" % exc, False
    return SourceDisposition(seat=name, state=state, why=why,
                             measured=measured, measured_at=time.time(),
                             mint=_DISPOSITION_MINT), None


_CUSTODY_MINT = object()


class ReassignCustody(object):
    """Producer-bound proof that a CONTROLLER measured one reassignment.

    THE MINT DEFECT, ONE SURFACE OVER. `mark_custody` began life as
    a public function that validated its target as a bare token and asked for
    no proof at all — which is exactly the hole that let a live incumbent's
    task move before `mint_seat_reassign` started measuring. A second proofless
    mutation door does not become safe by being newer.

    So custody moves only behind this object, and the object cannot be built
    from outside: the sentinel is module-private, the same shape as
    `_REASSIGN_MINT` and dispatches' `_MOVE_MINT`. The capability IS the
    object, never a value a caller can spell.

    It carries the CONTROLLER'S measurement rather than re-deriving one at each
    door: state and why from the single `source_disposition` call the verb
    already made, plus the operator's force and reason. One measurement, made
    once, travelling honestly — instead of every door measuring again and
    disagreeing about a seat that may die between two of them.

    `target` is the RESOLVED seat, not the token the operator typed, so a
    ghost target is refused where it is resolvable and never reaches a write.
    """
    __slots__ = ("source", "target", "state", "why", "force", "reason",
                 "minted_at")

    def __init__(self, source=None, target=None, state=None, why=None,
                 force=False, reason=None, minted_at=None, *, mint=None):
        if mint is not _CUSTODY_MINT:
            raise TakeoverRefused(
                "reassign custody is minted from a measured controller "
                "disposition; direct construction cannot move a delivery leg")
        self.source, self.target = source, target
        self.state, self.why = state, why
        self.force, self.reason = bool(force), reason
        self.minted_at = minted_at


def mint_reassign_custody(source, target, force=False, reason=None,
                          disposition=None):
    """(auth, error) — the ONLY mint for a delivery-leg transfer.

    Refuses on the same measured CONTRADICTION the task mint refuses on, and
    on nothing else: a demonstrably LIVE source without an explicit override.
    An UNMEASURABLE source still mints, because the seat nothing can measure is
    the orphan this whole verb exists for.
    """
    src = str(source or "").strip()
    tgt = str(target or "").strip()
    if not src or not tgt:
        return None, "custody needs a resolved source and target seat"
    if src == tgt:
        return None, ("source and target are the same seat (%s); nothing to "
                      "move" % src)
    reason = str(reason or "").strip()
    if not reason:
        return None, ("custody needs a reason — a delivery leg that changed "
                      "hands with no stated cause is unauditable")
    from . import seat_reassign
    # THE PROOF IS REQUIRED, AND IT IS BOUND TO THE SEAT, THE SOURCE AND A
    # CLOCK. A type check alone left it caller-spellable in every way that
    # matters: an optional argument means the door still opens without one,
    # and an unbound object means a disposition measured for ANOTHER seat, or
    # measured an hour ago, authorizes this move. Bound on all three now.
    ok, why_bad = _disposition_binds(disposition, src)
    if not ok:
        return None, why_bad
    # STATE COMES FROM THE PROOF, NEVER FROM AN ARGUMENT. `state` and `why`
    # used to arrive as plain parameters beside the proof, so the central
    # fact — is this seat alive — was still whatever the caller typed, and the
    # object was decoration. Reading them off the disposition is what makes it
    # load-bearing.
    state, why = disposition.state, disposition.why
    # THE LIVE VETO READS THE DERIVED STATE, so it must come AFTER the proof is
    # bound. It sat above, from when `state` was a parameter; once the value
    # came from the disposition instead, the check referenced a name that did
    # not exist yet — an UnboundLocalError on EVERY custody mint. The file
    # parsed and the arms that cover this path had not run, so only probing the
    # function found it.
    if state == seat_reassign.SOURCE_LIVE and not force:
        return None, ("source %s is measurably LIVE (%s); moving its delivery "
                      "leg needs an explicit override" % (src, why))
    return ReassignCustody(source=src, target=tgt, state=state, why=why,
                           force=force, reason=reason,
                           minted_at=time.time(), mint=_CUSTODY_MINT), None


def _stamp_fresh(at, max_age):
    now = time.time()
    # Positive interval membership rejects NaN too. Compare the stamp rather
    # than subtracting it: a malformed, enormous integer must not overflow.
    return type(at) in (int, float) and math.isfinite(now) \
        and now - max_age <= at <= now + 2


def reassign_custody_error(auth):
    """The delivery-leg proof must still be fresh when the writer consumes it."""
    if not isinstance(auth, ReassignCustody):
        return ("custody moves only behind a minted reassignment "
                "authorization (takeover.mint_reassign_custody); a seat "
                "name is not evidence")
    if not _stamp_fresh(getattr(auth, "minted_at", None), REASSIGN_MAX_AGE_S):
        return "reassign custody proof is stale or clock-invalid — remeasure"
    return None


class SeatReassignAuthorization(object):
    """Opaque authority for one exact task OWNER move, and nothing else.

    A SIBLING OF `BuildContinuationAuthorization`, NOT A WIDENING OF IT, and
    the distinction is the point. Takeover authorizes a LIVE-but-wedged
    incumbent's build to continue under a successor: it requires the pane be
    measured LIVE, it is bound to a source lane and a descendant tip, and it
    sets status=in_progress because somebody is picking the work up NOW.

    THIS ONE AUTHORIZES THE OPPOSITE POPULATION — a seat that is dead, or
    whose name stopped resolving — and it therefore carries LESS: it moves the
    owner and touches nothing else. It must never set status, because a
    reassignment is a statement about custody, not about work having resumed.
    Folding the two into one capability by relaxing takeover's type check
    would have made the strict one reachable with the loose one's evidence.
    """
    __slots__ = ("task_id", "before", "incumbent", "successor", "fields",
                 "record", "minted_at", "scope", "disposition")

    def __init__(self, task_id=None, before=None, incumbent=None,
                 successor=None, record=None, minted_at=None, *, mint=None,
                 disposition=None):
        if mint is not _REASSIGN_MINT:
            raise TakeoverRefused(
                "seat-reassign authorization is minted only from a measured "
                "source disposition; direct construction cannot move custody")
        self.task_id = task_id
        self.before = before
        self.incumbent = incumbent
        self.successor = successor
        self.fields = {"owner": successor}
        self.record = dict(record or {})
        self.minted_at = minted_at
        self.scope = "task-seat-reassign"
        # THE MEASURED DISPOSITION TRAVELS WITH THE CAPABILITY. This class
        # already said it is "minted only from a measured source disposition"
        # and nothing measured one. A sentence in a constructor is
        # not a check; carrying the measurement is.
        self.disposition = dict(disposition or {})


DISPOSITION_MAX_AGE_S = 300


def _disposition_binds(disposition, seat):
    """(ok, error) — is this proof REQUIRED, TYPED, SEAT-BOUND and FRESH?

    Four separate refusals because a type check alone satisfied none of the
    others: an OPTIONAL argument still opens the door without a proof; an
    unbound object lets a disposition measured for a DIFFERENT seat authorize
    this move; and one with no clock lets an hour-old reading of a seat that
    has since revived authorize it now. The seat that was measured must be the
    seat being moved, and it must have been measured recently enough that the
    answer is still about the present.
    """
    if disposition is None:
        return False, ("a source disposition is REQUIRED — mint one with "
                       "takeover.mint_source_disposition(seat)")
    if not isinstance(disposition, SourceDisposition):
        return False, ("a source disposition is MEASURED, never asserted — "
                       "pass the object from mint_source_disposition")
    want = str(seat or "").strip()
    got = str(getattr(disposition, "seat", "") or "").strip()
    if not got or got != want:
        return False, ("this disposition measured %r, not %r — a proof about "
                       "another seat cannot authorize this move"
                       % (got or None, want))
    at = getattr(disposition, "measured_at", None)
    if type(at) not in (int, float):
        return False, "this disposition carries no measurement clock"
    if not _stamp_fresh(at, DISPOSITION_MAX_AGE_S):
        return False, ("this disposition is stale or clock-invalid (limit %ds) — "
                       "remeasure; a stale reading is a claim about the past"
                       % DISPOSITION_MAX_AGE_S)
    return True, None


def mint_seat_reassign(task_id, previous, incumbent, successor, record,
                       force=False, disposition=None):
    """(auth, error) — the ONLY mint, and it MEASURES rather than trusts.

    Callers hand it the row they measured against, so the `before` digest is
    taken here rather than trusted from the caller — the compare-and-swap is
    the capability's, not its holder's.

    THE LIVENESS CHECK IS HERE AND NOT AT THE CALLER (review's second
    finding). `seat_reassign.source_disposition` already owed a must-miss —
    "a LIVE seat mid-turn is not reassignable without an explicit override" —
    but it was a VERB-level courtesy, so anything reaching this mint could move
    a live incumbent's task and nothing would object. A capability whose safety
    property lives in one of its callers does not have that property. It is
    measured HERE, where custody changes, and a live incumbent refuses unless
    the caller states an override, which is recorded in the proof rather than
    forgotten.
    """
    from . import seat_reassign
    # ONE MEASUREMENT PER COMMAND, NOT ONE PER TASK. This mint used
    # to call `source_disposition` itself, once for every row it moved — so a
    # seat that died or revived mid-loop split ONE operator command into two
    # verdicts, moving some of a seat's rows under one disposition and the rest
    # under another, with nothing in the record saying which. The controller
    # measures once and the measurement travels; a caller that supplies one is
    # required to supply the MINTED object, which it cannot spell.
    # MEASURED ONCE, NEVER TWICE. The fallback that re-measured here was the
    # second measurement review named: with it present, the normal flow
    # measured in `mint_source_disposition` AND again per task, so one command
    # could still split across a mid-loop transition — the exact defect the
    # object was introduced to end. There is no fallback now; a caller without
    # a proof is refused rather than quietly re-deriving one.
    ok, why_bad = _disposition_binds(disposition, incumbent)
    if not ok:
        return None, why_bad
    state, why = disposition.state, disposition.why
    # LIVE EVIDENCE IS A VETO, NEVER A REQUIREMENT (review, correcting my
    # first cure — which refused whenever liveness could not be MEASURED and
    # thereby broke the PRIMARY population this verb exists for: the seat with
    # no roster row, or whose name stopped resolving, which is precisely the
    # seat nothing can measure). Requiring proof of death inverts the burden
    # onto the case that can never supply it. The refusal fires on a MEASURED
    # CONTRADICTION — the incumbent is demonstrably LIVE — and on nothing else.
    if state == seat_reassign.SOURCE_LIVE and not force:
        return None, ("seat-reassign refused: incumbent %s is measurably LIVE "
                      "(%s). Moving a task out from under a running agent is "
                      "how two builders land on one lane; pass an explicit "
                      "override if that is really intended" % (incumbent, why))
    return SeatReassignAuthorization(
        task_id=task_id, before=_task_digest(previous), incumbent=incumbent,
        successor=successor, record=record, minted_at=time.time(),
        mint=_REASSIGN_MINT,
        disposition={"state": state or "unmeasured", "why": why,
                     "forced": bool(force),
                     "measured": state is not None,
                     "incumbent": incumbent}), None


def _authorize_seat_reassign(auth, task_id, previous, fields):
    if not _stamp_fresh(getattr(auth, "minted_at", None), REASSIGN_MAX_AGE_S):
        return None, "seat-reassign proof is stale or clock-invalid — remeasure"
    # THE PROOF MUST STILL CARRY ITS MEASUREMENT AT THE BOUNDARY. Minting is
    # where liveness is measured; this is where a proof that never carried one
    # is refused, so an auth built before this field existed cannot authorize.
    disp = getattr(auth, "disposition", None)
    if not isinstance(disp, dict) or not disp.get("state"):
        return None, ("seat-reassign proof carries no measured source "
                      "disposition — it cannot authorize a custody change")
    if disp.get("incumbent") != auth.incumbent:
        return None, ("seat-reassign proof measured a DIFFERENT seat (%s) than "
                      "the incumbent it names (%s)"
                      % (disp.get("incumbent"), auth.incumbent))
    expected = {"owner": auth.successor}
    if auth.task_id != task_id or auth.before != _task_digest(previous) \
            or auth.incumbent != tasks.owner_of(previous) \
            or auth.fields != expected or fields != expected:
        return None, ("seat-reassign proof does not match this exact task "
                      "OWNER compare-and-swap; it cannot authorize other "
                      "fields or scopes")
    return dict(auth.record), None


# THE REFUSAL IS BUILT FROM THIS DISPATCH, NOT MAINTAINED BESIDE IT.
#
# tasks.py refuses an incumbent-owner change and then has to tell the operator
# what WOULD authorize it. It named ONE capability while this module accepted
# two, so the population most likely to hit that refusal — a row held by a seat
# no roster knows — was told only about the contract that does not cover them.
# The reader who wrote that refusal's neighbour then read it back and published
# "the row cannot be transferred or cleared by anyone", which is the measure of
# how a partial list reads: not as a partial list.
#
# Keyed by the CLASS, because that is what authorize_task_mutation dispatches
# on. A capability added to that function without a row here offers the
# operator no door, and the arm over this table is what says so.
# EVERY FACT A REFUSAL STATES ABOUT THESE DOORS IS CARRIED HERE, because a
# fact a SENTENCE states about a TABLE is a fact that table must supply. The
# incumbent refusal in tasks.py needs four things — how many doors there are,
# how each is opened, whether the holder may open it, and what to ask for —
# and prose that invents any of them is true only of the rows that existed
# when it was written. Each one that stayed in prose became wrong in turn as
# the sentence was reviewed: a hardcoded arity, a hardcoded characterisation,
# a hardcoded capability name. The cure is not another clause; it is that the
# row carries the fact and the sentence renders it.
#
# `opened_by` is the VERB IN THE PASSIVE, so several doors join into one
# clause without grammar work. `holder_may_open` is False for every row today
# and is not speculative: task/2026 is the open row for a holder-offerable
# hand-off, and the day it lands its row sets this True and the refusal stops
# saying NONE by itself.
def _build_contract_reach(incumbent):
    """Can the BUILD contract mint for THIS incumbent? -> (bool|None, why).

    ASKED OF THE CALL THE CONTRACT ITSELF MAKES FIRST. `capture` opens with
    `_seat_dir`, so running that same call is the only way to answer without
    a second copy of the rule — and a second copy would start at zero on
    every edge this one already handles.

    THE TWO ANSWERS ARE NOT THE SAME STRENGTH OF CLAIM, and a reader who
    treats them symmetrically will over-read the True. `capture` runs three
    gates in order — `_seat_dir`, then `_seat_lifecycle_lock`, then
    `_claim_flocked` — and this probe runs only the first. So:

        False  ASSERTS that the door is shut, because the very first gate
               the contract reaches refuses this incumbent and no later gate
               can un-refuse it.
        True   asserts only NOT-SHUT-AT-THE-FIRST-GATE. The two gates after
               it are locks, they are contended rather than static, and a
               probe that took them would be doing the capture rather than
               asking about it.

    That asymmetry is what the tri-state is for rather than a gap in it: the
    refusal DROPS a door on False and keeps one on True, so the strong answer
    is the one that removes a capability from the offer and the weak answer
    only leaves it there. Widening True to mean "will open" would need the
    probe to hold both locks, which is the act itself.
    """
    try:
        _seat_dir(incumbent)
    except TakeoverRefused as exc:
        return False, str(exc)
    return True, ""


def _seat_reassign_reach(incumbent):
    """Can `helm seat reassign` name this incumbent as a source? -> (True|None, why).

    Its own resolver, for the same reason the BUILD door asks _seat_dir: the
    verb resolves a source token through the roster rather than through the
    family table, and that difference IS the fact this answer exists to carry.

    THERE IS NO FALSE HERE, AND THAT IS A FACT ABOUT THE RESOLVER RATHER THAN
    A GAP. `resolve_source` returns a seat for every case where the door can
    open, INCLUDING a name no roster row answers to — its own docstring calls
    that the orphan case and says it is "not an error here", because moving
    those holdings is exactly the verb's job. So absence never arrives as a
    falsy seat. Every falsy return is instead one of: no token given, a roster
    that could not be READ, or an ambiguous token matching several seats.

    MAPPING ANY OF THOSE TO FALSE IS THE BUG THIS DOCSTRING EXISTS TO PREVENT,
    and it shipped once: an unreadable roster made the refusal say the door
    "cannot open at all" and DROPPED its ask, which is the tri-state's whole
    purpose inverted — the one moment the strongest evidence is unavailable is
    the one moment an operator must not be sent away. None of the three is a
    statement that this seat cannot be reassigned; each is a statement about
    the QUESTION, so each answers UNKNOWN and carries the resolver's own words.
    """
    from helm import seat_reassign
    seat_name, _session, how = seat_reassign.resolve_source(incumbent)
    return (True, "") if seat_name else (None, how)


# `reach` IS A QUESTION, NOT A FLAG. A door's availability is a property of
# the ENVIRONMENT the refusal is rendered in — which families are registered,
# which seats the roster carries — and the table cannot hold that as a
# constant without going stale the moment a seat is added. So each row
# carries the CALL that answers it, and the fact is measured at render time.
#
# A ROW THAT NAMES NO CHECK IS UNMEASURED, NEVER OPEN. `reach` defaults to
# None so a door added without one still renders — it simply cannot claim to
# have been checked, and the refusal keeps offering it. Defaulting to "open"
# would let a door assert an availability nobody ever asked about.
_Door = collections.namedtuple(
    "_Door", "cls text opened_by holder_may_open ask reach reachable blocked_by",
    defaults=(None, None, ""))

_TASK_OWNER_DOORS = (
    _Door(BuildContinuationAuthorization,
          "the evidence-bound BUILD takeover contract, for continuing a LIVE "
          "incumbent's work under a successor",
          "TAKEN", False,
          "ask a successor to take it under the BUILD contract, which is "
          "built for a live incumbent and will notify you",
          _build_contract_reach, None, ""),
    _Door(SeatReassignAuthorization,
          "`helm seat reassign %(incumbent)s --to <seat>`, for moving the "
          "holdings of a seat that is dead, renamed, or unknown to the roster "
          "— live evidence is a VETO there, never a requirement",
          "FORCED", False,
          "ask an authorized operator to move it",
          _seat_reassign_reach, None, ""),
)


def task_owner_doors(incumbent):
    """The operator doors that can authorize a task OWNER change -> [str].

    Derived from the same table `authorize_task_mutation` dispatches on, so a
    third capability cannot be reachable in code and absent from the sentence
    that tells an operator what to reach for.
    """
    return [d.text for d in task_owner_door_facts(incumbent)]


def task_owner_door_facts(incumbent):
    """The same doors with the FACTS a refusal would otherwise invent.

    -> [_Door] with `text` already rendered for this incumbent and `reachable`
    MEASURED for it. Callers that only print the descriptions want
    `task_owner_doors`; a caller that says anything ABOUT the set — its size,
    how its members open, whether the holder may open one, what to ask for,
    whether it can open at all for this seat — reads them from here so the
    sentence cannot outlive the table.

    REACHABILITY IS MEASURED PER CALL because it is a fact about the
    environment rather than about the capability: the same door is open for a
    seat whose family the register knows and shut for one it does not, and
    registering a seat changes the answer without changing this file. Each
    probe answers True, False, or None for could-not-be-checked; None keeps
    the door in the offer, since a broken probe is not a closed door.
    """
    name = str(incumbent or "<seat>").strip() or "<seat>"
    out = []
    for d in _TASK_OWNER_DOORS:
        # A PROBE THAT BREAKS ANSWERS UNMEASURED, AND THAT IS THIS CALLER'S
        # JOB RATHER THAN EACH PROBE'S. A probe answers about its own door;
        # asking every one of them to also be careful is the per-case shape
        # that leaves the next one uncovered. AN UNMEASURABLE DOOR IS NOT A
        # CLOSED ONE — reporting "cannot open" because the check itself threw
        # would send an operator away from a capability that may be working,
        # and this sentence is already rendered on an error path.
        try:
            reachable, blocked_by = (d.reach(name) if d.reach else
                                     (None, "this door names no way to check"))
        except Exception as exc:                            # noqa: BLE001
            reachable, blocked_by = None, "%s: %s" % (type(exc).__name__, exc)
        out.append(d._replace(text=d.text % {"incumbent": name},
                              reachable=reachable, blocked_by=blocked_by))
    return out


def authorize_task_mutation(auth, task_id, previous, fields):
    """Validate one capability at tasks.py's owner boundary -> (record, error).

    TWO CAPABILITIES, VALIDATED SEPARATELY AND NEVER INTERCHANGEABLY. The
    BUILD-continuation branch keeps its own scope and compare-and-swap;
    the reassign branch has its own rules because it answers a different
    question about a different population. Dispatching on TYPE rather than on
    a field means neither can be reached with the other's evidence.
    """
    if type(auth) is SeatReassignAuthorization \
            and auth.scope == "task-seat-reassign":
        return _authorize_seat_reassign(auth, task_id, previous, fields)
    if type(auth) is not BuildContinuationAuthorization \
            or auth.scope != "task-build-continuation":
        return None, "owner reassignment needs a module-minted BUILD takeover proof"
    if not _stamp_fresh(getattr(auth, "minted_at", None), EVIDENCE_MAX_AGE_S):
        return None, "takeover proof is stale or clock-invalid — remeasure"
    expected = {"owner": auth.successor, "status": "in_progress"}
    if auth.task_id != task_id or auth.before != _task_digest(previous) \
            or auth.incumbent != tasks.owner_of(previous) \
            or auth.fields != expected or fields != expected:
        return None, ("takeover proof does not match this exact task BUILD-owner "
                      "compare-and-swap; it cannot authorize other fields or scopes")
    return dict(auth.record), None


def _finalize(record, root, task_path=None):
    d = _seat_dir(record["incumbent"])
    with seat_lifecycle._seat_lifecycle_lock(d):
        with seats._claim_flocked() as lock:
            if lock.f is None:
                raise TakeoverRefused(seats._lock_unavailable() +
                                      " — claim UNKNOWN")
            final = _capture_locked(record["incumbent"], root,
                                    record["source_lane"], d)
            if _bundle_anchor(final) != record["evidence_anchor"]:
                raise TakeoverRefused("final evidence does not match the prepared "
                                      "same-incumbent bundle — race REFUSED")
            measured_lineage = lineage(root, record["source_lane"],
                                       record["successor_lane"],
                                       superseding=record["superseding"])
            if measured_lineage != record["lineage"]:
                raise TakeoverRefused("lineage moved after prepare — race REFUSED")
            before, err = _strict_task(record["task_id"], task_path)
            if err:
                raise TakeoverRefused(err)
            if _task_digest(before) != record["task_before"] \
                    or tasks.owner_of(before) != record["incumbent"]:
                raise TakeoverRefused("task snapshot or incumbent changed after "
                                      "prepare — compare-and-swap REFUSED")
            proof = {"v": 1, "transfer_id": record["transfer_id"],
                     "scope": "build-continuation",
                     "incumbent": record["incumbent"],
                     "successor": record["successor"],
                     "source_lane": record["source_lane"],
                     "successor_lane": record["successor_lane"],
                     "lineage": measured_lineage, "evidence": final,
                     "notifications": record["notifications"],
                     "prepared_at": record["prepared_at"],
                     "committed_at": time.time()}
            fields = {"owner": record["successor"], "status": "in_progress"}
            auth = _mint(record["task_id"], before, record["incumbent"],
                         record["successor"], fields, proof)
            row, err = tasks.update(record["task_id"], path=task_path,
                                    takeover_auth=auth, **fields)
            if err:
                raise TakeoverRefused(err)
            return row


def transfer(token, source_lane, transfer_id, superseding=False, task_path=None,
             ledger_path=None, root=None, successor=None, successor_lane=None):
    """Run or idempotently recover one evidence-bound BUILD transfer."""
    try:
        transfer_id = _transfer_id(transfer_id)
    except TakeoverRefused as exc:
        return None, str(exc)
    task_id = tasks.normalize_id(token)
    if not task_id:
        return None, "unparseable task id %r" % (token,)
    try:
        root = root or work.find_root()
        acting = seats.own_name()
        inferred_lane = work._infer_lane(root) if root else None
    except Exception as exc:
        return None, "successor identity or lineage cannot be measured (%s) — UNKNOWN" \
            % type(exc).__name__
    if not root:
        return None, "current directory has no main repository — lineage UNKNOWN"
    if successor is not None and successor != acting:
        return None, "requested successor does not match the caller's acting seat"
    successor = acting
    if not isinstance(successor, str) or not SEAT_RE.match(successor):
        return None, "caller declares no exact seat identity — successor UNKNOWN"
    if successor_lane is not None and successor_lane != inferred_lane:
        return None, "requested successor lane does not match the current worktree"
    successor_lane = inferred_lane
    if not isinstance(source_lane, str) or not work.LANE_RE.match(source_lane):
        return None, "source lane is absent or malformed"
    if not isinstance(successor_lane, str) or not work.LANE_RE.match(successor_lane):
        return None, "current worktree is not a successor lane"
    if type(superseding) is not bool:
        return None, "superseding must be an explicit boolean declaration"
    request = {"task_id": task_id, "source_lane": source_lane,
               "successor_lane": successor_lane, "successor": successor,
               "superseding": superseding}
    try:
        record = _latest(transfer_id, ledger_path)
        if record:
            _validate_record(record)
            if not _same_request(record, request):
                raise TakeoverRefused("transfer_id %s already names a different "
                                      "transfer request" % transfer_id)
            committed = _committed_task(record, task_path)
            if committed:
                if record.get("phase") != "committed":
                    try:
                        _append_phase(record, "committed", path=ledger_path,
                                      task_event_digest=_task_digest(committed))
                    except TakeoverRefused:
                        pass
                return committed, None
        else:
            before, err = _strict_task(task_id, task_path)
            if err:
                raise TakeoverRefused(err)
            incumbent = tasks.owner_of(before)
            if before.get("status") not in tasks.OPEN_STATUSES or not incumbent:
                raise TakeoverRefused("takeover requires live BUILD work with an "
                                      "incumbent owner")
            if incumbent == successor:
                raise TakeoverRefused("successor already owns the task")
            measured_lineage = lineage(root, source_lane, successor_lane,
                                       superseding=superseding)
            evidence = capture(incumbent, root, source_lane)
            now = time.time()
            record = {"v": 1, "id": "takeover/" + transfer_id,
                      "transfer_id": transfer_id, **request,
                      "incumbent": incumbent, "room": evidence["occurrence"]["room"],
                      "task_before": _task_digest(before),
                      "lineage": measured_lineage, "evidence": evidence,
                      "evidence_anchor": _bundle_anchor(evidence),
                      "notification_events": {
                          "direct_message": "task-takeover:%s:dm" % transfer_id,
                          "mention": "task-takeover:%s:mention" % transfer_id},
                      "prepared_at": now, "last_updated": now}
            record = _append_phase(record, "prepared", path=ledger_path)
            _validate_record(record)
            if not _same_request(record, request):
                raise TakeoverRefused("transfer_id %s raced with a different "
                                      "transfer request" % transfer_id)
        notifications = _notify(record)
        record = _append_phase(record, "notified", path=ledger_path,
                               notifications=notifications, failure=None)
        # Another identical caller may have committed while this one was
        # idempotently replaying chat. A committed phase wins over the requested
        # notified append; return its task instead of attempting a second CAS and
        # reporting a false race after the transaction already succeeded.
        if record.get("phase") == "committed":
            committed = _committed_task(record, task_path)
            if not committed:
                raise TakeoverRefused("takeover ledger says committed but the task "
                                      "commit record is absent — transfer UNKNOWN")
            return committed, None
        row = _finalize(record, root, task_path=task_path)
        try:
            _append_phase(record, "committed", path=ledger_path,
                          task_event_digest=_task_digest(row))
        except TakeoverRefused:
            pass
        return row, None
    except TakeoverRefused as exc:
        # A caller reusing somebody else's transfer_id must not be able to mark
        # that valid transaction FAILED merely by presenting a different request.
        # Failure receipts belong only to the immutable request this invocation
        # joined or prepared.
        if "record" in locals() and record and _same_request(record, request):
            _mark_failed(record, exc, ledger_path)
        return None, str(exc)
    except Exception as exc:
        why = "takeover evidence could not be measured (%s) — UNKNOWN" \
            % type(exc).__name__
        if "record" in locals() and record and _same_request(record, request):
            _mark_failed(record, why, ledger_path)
        return None, why
