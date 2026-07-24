#!/usr/bin/env python3
"""Durable dispatch obligations with immutable, write-time-resolved rows.

Shipping state is deliberately small:

* ``dispatch`` opens one exact-tip obligation (persisted before delivery),
* ``delivered`` records that the local DM call returned a row,
* ``verdict`` closes an obligation by naming the exact reviewed tip,
* ``cancel`` honestly ABANDONS an open obligation with a reason (no reviewed
  tip) — the truthful terminal when a verdict will never come (recipient gone,
  work moot), so a stranded row need never be closed by a false verdict.

Both ``verdict`` and ``cancel`` are terminal and mutually exclusive.
No ACK, retarget, bind, replay-time branch/worktree lookup, automatic retry, or
cross-crash exactly-once claim. Ambiguous delivery remains open as NEEDS
CONFIRMATION; retrying the same operation never sends again. Deadlines are
advisory NEEDS CHECK-IN only.
"""
import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

from . import eventledger, home, pk

LEDGER = "dispatches.jsonl"
DEFAULT_DEADLINE_S = 2700
MAX_DEADLINE_S = 31 * 24 * 60 * 60
# The reduced core landed 2026-07-22 ~19:22Z; the newest legacy row on the
# live ledger is 09:13Z. Compat branches replay ONLY rows stamped before this
# boundary, so an event appended after landing can never drive the removed
# machinery — replay distinguishes an old retarget from a fresh one by when
# it claims to have been written (fail-closed: a missing or NON-STRING ts is never compat — the
# same type-corruption guard seq gets).
LEGACY_COMPAT_BOUNDARY = "2026-07-22T12:00:00Z"
_ID = re.compile(r"[0-9a-f]{8,64}\Z")
_TIP = re.compile(r"[0-9a-f]{40,64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def _clean(value, label, cap):
    value = str(value or "").strip()
    if not value:
        return None, "%s is required" % label
    if len(value) > cap or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
                               for c in value):
        return None, "%s must be one printable line of at most %d characters" % (label, cap)
    return value, None


def _deadline(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None, "deadline takes SECONDS"
    if not 1 <= value <= MAX_DEADLINE_S:
        return None, "deadline must be between 1 and %d seconds" % MAX_DEADLINE_S
    return value, None


def _repo_info(path=None):
    cwd = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--git-common-dir"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if top.returncode or common.returncode:
        return None
    return {"repo": os.path.realpath(top.stdout.strip()),
            "repo_id": os.path.realpath(common.stdout.strip())}


def _resolve_tip(repo, ref):
    """Resolve exactly one commit at WRITE time; replay never calls Git."""
    ref, err = _clean(ref, "tip", 256)
    if err or not repo or ref.startswith("-"):
        return None
    candidates = []
    try:
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", ref):
            p = subprocess.run(["git", "-C", repo, "rev-parse",
                                "--disambiguate=" + ref.lower()],
                               capture_output=True, text=True, timeout=5)
            candidates = [x.strip() for x in p.stdout.splitlines() if x.strip()]
            if p.returncode or len(candidates) != 1:
                return None
            ref = candidates[0]
        elif ref != "HEAD" and not ref.startswith("refs/"):
            for name in ("refs/heads/" + ref, "refs/tags/" + ref,
                         "refs/remotes/" + ref):
                p = subprocess.run(["git", "-C", repo, "show-ref", "--verify",
                                    "--hash", name], capture_output=True,
                                   text=True, timeout=5)
                if p.returncode == 0:
                    candidates.append(name)
            if len(candidates) != 1:
                return None
            ref = candidates[0]
        p = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--end-of-options",
             ref + "^{commit}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = p.stdout.splitlines()
    tip = lines[0].strip().lower() if p.returncode == 0 and len(lines) == 1 else ""
    return tip if _TIP.fullmatch(tip) else None


def _pre_boundary(ts):
    """True only for an honest pre-boundary timestamp: a non-empty string
    that LOOKS like one (year 2xxx) and sorts before the boundary. Numeric,
    empty, whitespace, and low-sorting garbage ts all fail CLOSED — the r3
    isinstance guard alone let '' through (fable delta MED), and the r2
    or-tilde let numerics through; the shape check closes the class."""
    return isinstance(ts, str) and ts.startswith("20") \
        and ts < LEGACY_COMPAT_BOUNDARY


def _int_seq(value, fallback):
    """Adopt a row's seq only when it is a real integer — a type-corrupt seq
    must never enter replay state, where int(state.seq)+1 would crash."""
    return value if type(value) is int else fallback   # bool is an int subclass


def _valid_identity(row):
    if not _ID.fullmatch(str(row.get("id") or "")):
        return False
    if not _TOKEN.fullmatch(str(row.get("recipient") or "")):
        return False
    if row.get("seq") is not None and type(row.get("seq")) is not int:
        return False              # type-corrupt seq would crash replay
    lane, err = _clean(row.get("lane"), "lane", 160)
    if err or lane != row.get("lane"):
        return False
    try:
        return 1 <= int(row.get("deadline_s")) <= MAX_DEADLINE_S
    except (TypeError, ValueError):
        return False


def _new_state(row):
    """Normalize one first event, including historical schemas.

    Historical ref-less or short-ref open rows remain visible as
    ``needs-redispatch``; they are never silently dropped or rebound at replay.
    """
    if not _valid_identity(row):
        return None
    event = row.get("event")
    status = row.get("status")
    if event == "dispatch" and type(row.get("v")) is int and row.get("v") == 3 \
            and type(row.get("seq")) is int and row.get("seq") == 0:
        if status != "open" or not _TIP.fullmatch(str(row.get("tip") or "")):
            return None
        out = dict(row)
        out.update(status="open", delivery="needs-confirmation",
                   migration=None, seq=0)
        return out
    # v1 snapshots and the short-lived v2 add/posting schemas.
    if event not in (None, "add", "posting"):
        return None
    if status not in ("open", "pending", "posting", "acked", "verdict"):
        return None
    tip = str(row.get("tip") or "").lower()
    exact = tip if _TIP.fullmatch(tip) else None
    closable = _pre_boundary(row.get("ts"))
    out = {"v": row.get("v") or 1, "id": str(row["id"]),
           "event": "dispatch", "seq": int(row.get("seq") or 0),
           "ts": row.get("ts"), "recipient": row.get("recipient"),
           "lane": row.get("lane"), "tip": exact,
           "ref": row.get("ref"), "note": row.get("note"),
           "deadline_s": int(row.get("deadline_s")),
           "source": row.get("source"), "sender": row.get("sender"),
           "repo_id": row.get("repo_id"),
           "operation_key": row.get("dispatch_key"),
           "message_hash": row.get("message_hash"),
           "status": "verdict" if status == "verdict" and closable else "open",
           "delivery": "observed" if row.get("delivery_ref") else "needs-confirmation",
           "delivery_ref": row.get("delivery_ref"),
           "verdict_ref": row.get("verdict_ref"),
           "reviewed_tip": row.get("reviewed_tip"),
           "migration": None if exact or status == "verdict" and closable
           else "needs-redispatch"}
    if out["status"] == "verdict" and not out["verdict_ref"]:
        return None
    return out


# A dispatch is CLOSED once it carries a VERDICT (a review happened) or a
# CANCEL (honestly abandoned with a reason). Both are TERMINAL and drop the row
# from the open / overdue / stop / land reads, so a stranded dispatch stops
# nagging the watchdog the moment it is cancelled — no false verdict needed.
CLOSED_STATES = ("verdict", "cancelled")


def _open(row):
    return row.get("status") not in CLOSED_STATES


def _apply(state, row):
    """Apply only immutable evidence events; malformed later rows preserve the
    preceding good obligation.

    Every compatibility branch is gated to LEGACY-opened states (v1/v2): an
    obligation opened by a v3 dispatch row accepts nothing but strict
    seq-ordered ``delivered``/``verdict``/``cancel`` events, so a well-shaped
    forged snapshot, ack, or retarget row can never close it or move its tip."""
    if str(row.get("id") or "") != state["id"]:
        return state
    # TERMINAL IS IMMUTABLE: once a dispatch carries a verdict or a cancel, NO
    # later event — v3 OR a legacy-compat retarget/snapshot-verdict — may mutate
    # its status or tip. This guard runs BEFORE the compat branches below, which
    # do not each re-check terminality (codex-3 xrev: a compat verdict could
    # otherwise convert cancelled->verdict, a retarget could move a closed tip).
    if state.get("status") in CLOSED_STATES:
        return state
    event = row.get("event")
    legacy = state.get("v") != 3
    # Compat replays ONLY rows stamped before the reduced core landed: an
    # event appended today can never drive the removed machinery, however
    # well-shaped. Missing ts is never compat (fail-closed, "~" sorts high).
    compat = legacy and _pre_boundary(row.get("ts"))
    # Historical full-snapshot compatibility.
    if event is None:
        if compat and row.get("status") == "verdict" and row.get("verdict_ref"):
            out = dict(state)
            out.update(status="verdict", verdict_ref=row.get("verdict_ref"),
                       reviewed_tip=row.get("reviewed_tip") or state.get("tip"),
                       migration=None)
            return out
        return state
    if event == "retarget" and compat \
            and _TIP.fullmatch(str(row.get("tip") or "")):
        # Compatibility only for already-written rows; there is no shipping verb.
        out = dict(state)
        out.update(tip=str(row["tip"]).lower(), ref=row.get("ref"),
                   migration=None,
                   seq=_int_seq(row.get("seq"), state.get("seq", 0)))
        return out
    expected = int(state.get("seq") or 0) + 1
    strict = type(row.get("v")) is int and row.get("v") == 3 \
        and type(row.get("seq")) is int and row.get("seq") == expected
    if event == "delivered" and state["status"] == "open" \
            and (strict or compat):
        ref, err = _clean(row.get("delivery_ref"), "delivery ref", 256)
        if err:
            return state
        out = dict(state)
        out.update(delivery="observed", delivery_ref=ref, seq=expected)
        return out
    if event == "verdict" and state["status"] == "open" \
            and (strict or compat):
        reviewed = str(row.get("reviewed_tip") or "").lower()
        evidence, err = _clean(row.get("verdict_ref"), "verdict evidence", 256)
        if state.get("tip") and reviewed == state["tip"] and not err:
            out = dict(state)
            out.update(status="verdict", reviewed_tip=reviewed,
                       verdict_ref=evidence, seq=expected)
            return out
        return state
    # CANCEL: honest terminal abandonment of an OPEN dispatch (reviewer gone,
    # work moot). v3-native — no compat history exists — and unlike a verdict
    # it binds NO reviewed tip, only a reason. Terminal: a cancelled or
    # verdict'd obligation ignores every later event.
    if event == "cancel" and state["status"] == "open" and strict:
        reason, err = _clean(row.get("reason"), "cancel reason", 256)
        if err or not reason:
            return state
        out = dict(state)
        out.update(status="cancelled", cancel_reason=reason, seq=expected)
        return out
    # Historical snapshot verdicts/retarget-derived verdicts (compat only).
    if compat and row.get("status") == "verdict" and row.get("verdict_ref"):
        reviewed = str(row.get("reviewed_tip") or state.get("tip") or "").lower()
        if not state.get("tip") or reviewed == state.get("tip"):
            out = dict(state)
            out.update(status="verdict", reviewed_tip=reviewed or None,
                       verdict_ref=row.get("verdict_ref"), migration=None,
                       seq=_int_seq(row.get("seq"), state.get("seq", 0)))
            return out
    if compat and row.get("delivery_ref") and state["status"] == "open":
        out = dict(state)
        out.update(delivery="observed", delivery_ref=row.get("delivery_ref"),
                   seq=_int_seq(row.get("seq"), state.get("seq", 0)))
        return out
    return state


def snapshot():
    events, unavailable = eventledger.checked_events(ledger_path())
    if unavailable:
        return {}, unavailable
    out = {}
    for row in events:
        # One malformed row must never blind the whole ledger: a crash here
        # would turn every obligation into "no usable obligations" — worse
        # than skipping the bad row and keeping every good state intact.
        try:
            rid = str(row.get("id") or "")
            if rid not in out:
                state = _new_state(row)
                if state:
                    out[rid] = state
            else:
                out[rid] = _apply(out[rid], row)
        except Exception:
            continue
    return out, None


def rows():
    return snapshot()[0]


def history(rid):
    return [row for row in eventledger.events(ledger_path())
            if str(row.get("id") or "") == str(rid)]


def events_by_id():
    """Every ledger event grouped by row id in a SINGLE pass — the read a
    whole-ledger projection wants instead of one history() reparse per row
    (that per-row reparse is O(N^2) on an append-only ledger)."""
    out = {}
    for row in eventledger.events(ledger_path()):
        out.setdefault(str(row.get("id") or ""), []).append(row)
    return out


def _base(recipient, lane, ref, note, deadline_s, repo, sender=None,
          operation_key=None, message_hash=None, rid=None):
    from . import seats
    recipient, err = seats.resolve_recipient(recipient)
    if err:
        return None, err
    lane, err = _clean(lane, "lane", 160)
    if err:
        return None, err
    if note is not None:
        note, err = _clean(note, "note", 1000)
        if err:
            return None, err
    deadline_s, err = _deadline(deadline_s)
    if err:
        return None, err
    if sender is not None and not _TOKEN.fullmatch(str(sender)):
        return None, "sender must be an exact 1-64 character seat token"
    info = _repo_info(repo)
    if not info:
        return None, "ref needs a Git working tree (--repo PATH)"
    display, err = _clean(ref, "ref", 256)
    if err:
        return None, "ref is required so the verdict can bind an exact tip"
    tip = _resolve_tip(info["repo"], display)
    if not tip:
        return None, "ref is missing, ambiguous, or not a commit in this repository"
    ts = pk.now_ts()
    return {"v": 3, "event": "dispatch", "seq": 0,
            "id": rid or os.urandom(16).hex(), "ts": ts,
            "recipient": recipient, "lane": lane, "tip": tip, "ref": display,
            "note": note, "deadline_s": deadline_s,
            "source": home.session_id() or "cli", "sender": sender,
            "repo_id": info["repo_id"], "operation_key": operation_key,
            "message_hash": message_hash, "status": "open"}, None


def _append_dispatch(row):
    """(row, err, existed) — existed=True means the operation was already on
    the ledger; the caller must treat that as NEVER-SEND-AGAIN."""
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — dispatch NOT recorded" % path, False
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable, False
        existing = current.get(row["id"])
        if existing:
            semantic = ("recipient", "lane", "tip", "note", "deadline_s",
                        "sender", "repo_id", "operation_key", "message_hash")
            if all(existing.get(k) == row.get(k) for k in semantic):
                return existing, None, True
            return None, "operation key already names different work", True
        if not eventledger.append_unlocked(path, row):
            return None, "ledger unwritable (%s) — dispatch NOT recorded" % path, False
    pk.event("dispatch-add", row["id"], "%s -> %s" % (row["recipient"], row["lane"]))
    out = dict(row)
    out.update(delivery="needs-confirmation", migration=None,
               delivery_ref=None, verdict_ref=None, reviewed_tip=None)
    return out, None, False


def add(recipient, lane, ref=None, note=None, deadline_s=DEFAULT_DEADLINE_S,
        repo=None):
    row, err = _base(recipient, lane, ref, note, deadline_s, repo)
    return _append_dispatch(row)[0] if row else None


def _mark_delivered(rid, delivery_ref):
    ref, err = _clean(delivery_ref, "delivery ref", 256)
    if err:
        return None, err
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "delivery observed but ledger update failed"
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row = current.get(str(rid))
        if not row:
            return None, "no such dispatch: %s" % rid
        if row.get("delivery") == "observed" or not _open(row):
            return row, None            # already observed, OR CLOSED (verdict or
                                        # cancelled) — never append a late
                                        # delivered event onto a terminal row
        event = {"v": 3, "event": "delivered", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "delivery_ref": ref}
        if not eventledger.append_unlocked(path, event):
            return None, "delivery observed but ledger update failed"
    out = dict(row)
    out.update(delivery="observed", delivery_ref=ref, seq=event["seq"])
    return out, None


def send(recipient, lane, message, ref, note=None, deadline_s=DEFAULT_DEADLINE_S,
         key=None, repo=None, sign=None):
    """Persist first, attempt one DM, never auto-retry an existing operation.

    Returns (row, reason, sent_now). Existing rows return NEEDS CONFIRMATION
    without another side effect.
    """
    message = str(message or "").strip()
    if not message or len(message) > 16000 or "\x00" in message:
        return None, "message must be 1-16000 characters without NUL", False
    key = str(key or "").strip()
    if key:
        key, err = _clean(key, "operation key", 256)
        if err:
            return None, err, False
    from . import seats
    try:
        sender = seats.derive_seat(home.session_id())
    except home.SeatNameError:
        # A hostile HELM_CHAT_NAME is rejected at the validated source
        # (home.chat_name raises); surface it as the same graceful sender
        # refusal the exact-token check gives, never an uncaught raise.
        return None, "sender must be an exact 1-64 character seat token", False
    probe, err = _base(recipient, lane, ref, note, deadline_s, repo,
                       sender=sender, message_hash=hashlib.blake2b(
                           message.encode("utf-8"), digest_size=16).hexdigest())
    if err:
        return None, err, False
    if not key:
        key = "auto:" + hashlib.blake2b(
            "\0".join((probe["recipient"], probe["lane"], probe["tip"],
                        probe.get("note") or "", str(probe["deadline_s"]),
                        probe["message_hash"])).encode("utf-8"),
            digest_size=16).hexdigest()
    namespace = "\0".join((sender, probe["repo_id"], key))
    probe["id"] = hashlib.blake2b(
        ("dispatch\0" + namespace).encode("utf-8"), digest_size=16).hexdigest()
    probe["operation_key"] = key
    row, why, existed = _append_dispatch(probe)
    if why:
        return None, why, False
    if existed:
        # One operation = at most one send, ever. A prior attempt whose
        # delivery evidence is missing is AMBIGUOUS, not absent — resending
        # here is exactly the duplicate-message hazard the reduced core
        # refuses to automate away.
        return row, ("dispatch already recorded; delivery is %s — confirm at the "
                     "recipient, do not resend automatically" % row["delivery"]), False
    try:
        delivered, dm_err = seats.dm(row["recipient"], message, who=sender,
                                     profile=sender, sign=sign,
                                     session=home.session_id())
    except Exception as exc:
        delivered, dm_err = None, "%s: %s" % (type(exc).__name__, exc)
    if dm_err or not delivered:
        return row, ("dispatch persisted but delivery is NEEDS CONFIRMATION: %s"
                     % (dm_err or "DM returned no row")), False
    observed, err = _mark_delivered(row["id"], delivered.get("id"))
    if err:
        return row, err + "; NEEDS CONFIRMATION", False
    # The lock is released for the DM, so a concurrent cancel/verdict may have
    # terminalized the row mid-flight — report the TRUE state, never a false
    # "delivery observed" on a dispatch that is already closed.
    return observed, None, _open(observed)


def mark_verdict(rid, reviewed_tip, evidence):
    reviewed = str(reviewed_tip or "").strip().lower()
    if not _TIP.fullmatch(reviewed):
        return None, "verdict needs the full exact reviewed commit id"
    evidence, err = _clean(evidence, "verdict evidence", 256)
    if err:
        return None, err
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row = current.get(str(rid or ""))
        if not row:
            return None, "no such dispatch: %s (helm dispatch list)" % rid
        if row["status"] == "verdict":
            if row.get("reviewed_tip") == reviewed and row.get("verdict_ref") == evidence:
                return row, None
            return None, "dispatch %s already has a verdict (closed)" % rid
        if row["status"] == "cancelled":
            return None, ("dispatch %s was cancelled (abandoned) — a verdict "
                          "asserts a review happened, so it is refused" % rid)
        if not row.get("tip"):
            return None, "historical dispatch lacks an exact tip; redispatch it"
        if reviewed != row["tip"]:
            return None, "stale verdict: reviewed %s but dispatched tip is %s" % (
                reviewed, row["tip"])
        event = {"v": 3, "event": "verdict", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed, "verdict_ref": evidence}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
    out = dict(row)
    out.update(status="verdict", reviewed_tip=reviewed,
               verdict_ref=evidence, seq=event["seq"])
    pk.event("dispatch-verdict", row["id"], evidence)
    return out, None


def mark_cancel(rid, reason):
    """Honestly ABANDON an open dispatch with a reason — the only truthful
    terminal event when a verdict will never come (recipient gone, work moot,
    superseded). Idempotent on the same reason; refuses a dispatch that already
    carries a verdict (that one is already honestly closed)."""
    reason, err = _clean(reason, "cancel reason", 256)
    if err:
        return None, err
    if not reason:
        return None, "cancel needs a reason (why the dispatch is abandoned)"
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — cancel NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row = current.get(str(rid or ""))
        if not row:
            return None, "no such dispatch: %s (helm dispatch list)" % rid
        if row["status"] == "cancelled":
            if row.get("cancel_reason") == reason:
                return row, None            # idempotent re-cancel
            return None, "dispatch %s already cancelled" % rid
        if row["status"] == "verdict":
            return None, ("dispatch %s already has a verdict (closed) — a "
                          "reviewed dispatch is not cancelled" % rid)
        event = {"v": 3, "event": "cancel", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "reason": reason}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — cancel NOT recorded" % path
    out = dict(row)
    out.update(status="cancelled", cancel_reason=reason, seq=event["seq"])
    pk.event("dispatch-cancel", row["id"], reason)
    return out, None


def _age_s(row, now=None):
    try:
        stamp = time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ")
        return max(0, int((time.time() if now is None else now)
                          - calendar.timegm(stamp)))
    except (OverflowError, ValueError, TypeError):
        return 0


def open_rows():
    out = [r for r in rows().values() if _open(r)]
    return sorted(out, key=lambda r: (str(r.get("ts") or ""), r["id"]))


def _is_overdue(row, now=None):
    return _open(row) and _age_s(row, now) >= int(row["deadline_s"])


def overdue():
    now = time.time()
    return [r for r in open_rows() if _is_overdue(r, now)]


def stop_candidate():
    current, unavailable = snapshot()
    if unavailable:
        return None, None, unavailable
    ordered = sorted((r for r in current.values() if _open(r)),
                     key=lambda r: (str(r.get("ts") or ""), r["id"]))
    migrate = next((r for r in ordered if r.get("migration")), None)
    if migrate:
        return migrate, "redispatch", None
    uncertain = next((r for r in ordered
                      if r.get("delivery") != "observed"), None)
    if uncertain:
        return uncertain, "confirm", None
    now = time.time()
    late = next((r for r in ordered if _is_overdue(r, now)), None)
    return late, "overdue" if late else None, None


USAGE = ("usage: helm dispatch send <recipient> <lane> <message...> --ref TIP "
         "[--key K] [--note N] [--deadline SECONDS] [--repo PATH] | add "
         "<recipient> <lane> --ref TIP [--note N] [--deadline SECONDS] "
         "[--repo PATH] | verdict <id> <full-reviewed-tip> <evidence> | "
         "cancel <id> <reason...> | list [--open|--overdue] [--json]")


def _parse(rest, names):
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if not arg.startswith("--"):
            pos.append(arg)
            i += 1
            continue
        if arg not in names:
            return None, None, "unknown option %s" % arg
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            return None, None, "%s wants a value" % arg
        opts[arg] = rest[i + 1]
        i += 2
    return pos, opts, None


def _label(row):
    if row.get("status") == "verdict":
        return "VERDICT"
    if row.get("status") == "cancelled":
        return "CANCELLED"
    if row.get("migration"):
        return "NEEDS REDISPATCH"
    if row.get("delivery") != "observed":
        return "PENDING VERDICT / NEEDS CONFIRMATION"
    return "PENDING VERDICT"


def _fmt(row, late=False):
    age = _age_s(row) // 60
    deadline = int(row["deadline_s"]) // 60
    suffix = "  NEEDS CHECK-IN (OVERDUE)" if late else ""
    return "  %s  %-16s %-24s %-36s %3dm/%dm%s  %s" % (
        row["id"], row["recipient"], row["lane"], _label(row),
        age, deadline, suffix, str(row.get("tip") or row.get("ref") or "-")[:12])


def cmd_dispatch(args):
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb in ("add", "send"):
        names = {"--ref", "--note", "--deadline", "--repo"}
        if verb == "send":
            names.add("--key")
        pos, opts, err = _parse(rest, names)
        if err or len(pos) < (3 if verb == "send" else 2):
            print("helm dispatch: " + (err or USAGE), file=sys.stderr)
            return 2
        if not opts.get("--ref"):
            print("helm dispatch: %s requires --ref TIP" % verb, file=sys.stderr)
            return 2
        deadline, err = _deadline(opts.get("--deadline", DEFAULT_DEADLINE_S))
        if err:
            print("helm dispatch: " + err, file=sys.stderr)
            return 2
        if verb == "send":
            row, why, sent = send(pos[0], pos[1], " ".join(pos[2:]),
                                  opts["--ref"], note=opts.get("--note"),
                                  deadline_s=deadline, key=opts.get("--key"),
                                  repo=opts.get("--repo"))
            if row is None:
                print("helm dispatch: " + why, file=sys.stderr)
                return 1
            if why:
                print("helm dispatch: " + why, file=sys.stderr)
                return 1
            print("helm dispatch: %s @%s %s — %s%s" % (
                row["id"], row["recipient"], row["lane"], _label(row),
                " (delivery observed)" if sent else ""))
            return 0
        row = add(pos[0], pos[1], opts["--ref"], note=opts.get("--note"),
                  deadline_s=deadline, repo=opts.get("--repo"))
        if row is None:
            print("helm dispatch: dispatch NOT recorded", file=sys.stderr)
            return 1
        print("helm dispatch: %s -> @%s %s — PENDING VERDICT / NEEDS CONFIRMATION"
              % (row["id"], row["recipient"], row["lane"]))
        return 0
    if verb == "verdict":
        if len(rest) < 3:
            print(USAGE, file=sys.stderr)
            return 2
        row, why = mark_verdict(rest[0], rest[1], " ".join(rest[2:]))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — VERDICT at %s" % (row["id"], row["tip"][:12]))
        return 0
    if verb == "cancel":
        if len(rest) < 2:
            print("usage: helm dispatch cancel <id> <reason...>  "
                  "(honestly abandon a stranded dispatch — recipient gone / "
                  "work moot; never a substitute for a real verdict)",
                  file=sys.stderr)
            return 2
        row, why = mark_cancel(rest[0], " ".join(rest[1:]))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — CANCELLED (%s)" % (
            row["id"], row.get("cancel_reason") or ""))
        return 0
    if verb == "list":
        current, unavailable = snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; obligations UNKNOWN: %s"
                  % unavailable, file=sys.stderr)
            return 1
        flags = set(rest)
        if flags - {"--open", "--overdue", "--json"} \
                or len(flags & {"--open", "--overdue"}) > 1:
            print(USAGE, file=sys.stderr)
            return 2
        selected = sorted(current.values(),
                          key=lambda r: (str(r.get("ts") or ""), r["id"]))
        if "--open" in flags:
            selected = [r for r in selected if _open(r)]
        elif "--overdue" in flags:
            now = time.time()
            selected = [r for r in selected if _is_overdue(r, now)]
        if "--json" in flags:
            print(json.dumps(selected, ensure_ascii=False, indent=1))
            return 0
        if not selected:
            print("helm dispatch: no matching rows")
            return 0
        print("helm dispatch — %d logical row%s" % (
            len(selected), "" if len(selected) == 1 else "s"))
        now = time.time()
        for row in selected:
            print(_fmt(row, _is_overdue(row, now)))
        if any(_is_overdue(r, now) for r in selected):
            print("NEEDS CHECK-IN is advisory only; do not reassign on age alone")
        return 0
    print(USAGE, file=sys.stderr)
    return 2
