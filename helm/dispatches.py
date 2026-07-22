#!/usr/bin/env python3
"""Durable, event-sourced obligations for work handed to another seat.

A sent dispatch is PENDING until a verdict names both the exact reviewed tip
and its evidence.  ACK never closes it.  Deadlines only produce NEEDS CHECK-IN;
they never reassign work.  `send` is the first-class handoff path: it stages a
ledger event before an idempotent DM, then activates the same logical row.  A
retry reuses both ids, so it cannot create a second obligation or message.
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
STATUSES = ("posting", "aborted", "pending", "open", "acked", "verdict")
ACTIVE = ("pending", "open", "acked")
_ID = re.compile(r"[0-9a-f]{8,64}\Z")
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
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
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
    """Resolve exactly one commit.  Short object ids are accepted only when
    Git reports one object; symbolic shorthand is accepted only when exactly
    one of heads/tags/remotes owns it.  Ambiguity is never guessed through."""
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
    return tip if re.fullmatch(r"[0-9a-f]{40,64}", tip) else None


def _is_ancestor(repo, older, newer):
    try:
        p = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor",
                            older, newer], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _binding_valid(row, ref=None, tip=None):
    info = _repo_info(row.get("repo"))
    expected = tip or row.get("tip")
    return bool(info and info["repo_id"] == row.get("repo_id") and expected
                and _resolve_tip(info["repo"], ref or row.get("ref")) == expected)


def _valid(row, prior):
    """Replay validator.  A corrupt duplicate/update is skipped, preserving the
    preceding good snapshot instead of letting one bad tail erase an obligation."""
    if not _ID.fullmatch(str(row.get("id") or "")):
        return False
    if row.get("status") not in STATUSES or not _TOKEN.fullmatch(
            str(row.get("recipient") or "")):
        return False
    lane, err = _clean(row.get("lane"), "lane", 160)
    if err or lane != row.get("lane"):
        return False
    if row.get("note") is not None:
        note, err = _clean(row.get("note"), "note", 1000)
        if err or note != row.get("note"):
            return False
    for key in ("ref", "ack_ref", "verdict_ref"):
        if row.get(key) is not None and _clean(row.get(key), key, 256)[1]:
            return False
    for key in ("tip", "original_tip", "reviewed_tip"):
        if row.get(key) is not None and not re.fullmatch(
                r"[0-9a-f]{40,64}", str(row.get(key))):
            return False
    try:
        if not 1 <= int(row.get("deadline_s")) <= MAX_DEADLINE_S:
            return False
    except (TypeError, ValueError):
        return False
    if prior is None:
        seq = row.get("seq")
        if seq is None:  # v1 snapshot compatibility
            if row.get("status") == "acked" and not row.get("ack_ref"):
                return False
            if row.get("status") == "verdict" and not row.get("verdict_ref"):
                return False
            return row.get("status") in ("open", "acked", "verdict")
        if seq != 0:
            return False
        bound = bool(row.get("ref") and row.get("tip")
                     and row.get("original_ref") and row.get("original_tip")
                     and row.get("repo") and row.get("repo_id"))
        if row.get("event") == "add" and row.get("status") == "pending":
            legacy_v2_unbound = row.get("v") == 2 and not any(
                row.get(k) for k in ("ref", "tip", "original_ref",
                                     "original_tip", "repo", "repo_id"))
            return bound or legacy_v2_unbound
        if row.get("event") == "posting" and row.get("status") == "posting":
            return bool(bound and row.get("dispatch_key") and row.get("message_id")
                        and row.get("message_hash")
                        and _TOKEN.fullmatch(str(row.get("sender") or "")))
        return False
    immutable = ("id", "ts", "recipient", "lane", "note", "deadline_s",
                 "source", "original_ref", "original_tip", "repo", "repo_id", "dispatch_key",
                 "message_id", "message_hash", "sender")
    binding = {"original_ref", "original_tip", "repo", "repo_id"}
    for key in immutable:
        if row.get(key) == prior.get(key):
            continue
        if row.get("event") in ("bind", "retarget", "verdict") and key in binding \
                and prior.get(key) is None and row.get(key):
            continue
        return False
    seq = row.get("seq")
    prior_seq = prior.get("seq")
    if seq is None and prior_seq is None and row.get("event") is None:
        # Validated v1 snapshots: exact immutable work/ref, monotone status, and
        # evidence on ACK/verdict. This preserves old closures without letting a
        # seq-less duplicate retarget or rewrite the work identity.
        if row.get("ref") != prior.get("ref") or row.get("tip") != prior.get("tip"):
            return False
        legacy = {"open": {"acked", "verdict"},
                  "acked": {"acked", "verdict"}, "verdict": set()}
        if row.get("status") not in legacy.get(prior.get("status"), set()):
            return False
        if row.get("status") == "acked":
            return bool(row.get("ack_ref"))
        return bool(row.get("verdict_ref"))
    if seq is not None and seq != (prior_seq if isinstance(prior_seq, int) else -1) + 1:
        return False
    if seq is None or (prior_seq is not None and not isinstance(prior_seq, int)):
        return False
    allowed = {
        "posting": {("pending", "delivered"),
                    ("aborted", "delivery-failed")},
        "aborted": {("posting", "retry")},
        "pending": {("pending", "bind"), ("pending", "retarget"),
                    ("acked", "ack"), ("verdict", "verdict")},
        "open": {("open", "retarget"), ("acked", "ack"),
                 ("verdict", "verdict")},
        "acked": {("acked", "retarget"), ("acked", "ack"),
                  ("verdict", "verdict")},
        "verdict": set(),
    }
    event = row.get("event")
    transition = (row.get("status"), event)
    if transition not in allowed.get(prior.get("status"), set()):
        return False
    prior_tip = prior.get("tip")
    if event == "bind":
        if any(prior.get(k) for k in ("ref", "tip", "original_ref",
                                      "original_tip", "repo", "repo_id")) \
                or row.get("status") != prior.get("status") \
                or row.get("tip") != row.get("original_tip") \
                or row.get("ref") != row.get("original_ref") \
                or not _binding_valid(row):
            return False
    elif event == "retarget":
        adopted = prior_tip or row.get("original_tip")
        if not adopted or row.get("retarget_from") != adopted \
                or row.get("retarget_to") != row.get("tip") \
                or row.get("tip") == adopted or row.get("status") != prior.get("status") \
                or not _binding_valid(row, ref=row.get("ref"), tip=row.get("tip")) \
                or not _is_ancestor(row.get("repo"), adopted, row.get("tip")):
            return False
    elif event == "verdict":
        expected = prior_tip or row.get("original_tip")
        if row.get("ref") != prior.get("ref") or not expected \
                or row.get("tip") != expected or row.get("reviewed_tip") != expected:
            return False
    elif row.get("ref") != prior.get("ref") or row.get("tip") != prior_tip:
        return False
    mutable = {
        "delivered": {"status", "event", "seq", "last_updated",
                      "delivery_ref", "delivery_error"},
        "delivery-failed": {"status", "event", "seq", "last_updated",
                            "delivery_error"},
        "retry": {"status", "event", "seq", "last_updated", "delivery_error"},
        "ack": {"status", "event", "seq", "last_updated", "ack_ref"},
        "bind": {"status", "event", "seq", "last_updated", "ref", "tip",
                 "original_ref", "original_tip", "repo", "repo_id"},
        "retarget": {"status", "event", "seq", "last_updated", "ref", "tip",
                     "retarget_from", "retarget_to", "original_ref",
                     "original_tip", "repo", "repo_id"},
        "verdict": {"status", "event", "seq", "last_updated", "tip",
                    "verdict_ref", "reviewed_tip", "original_ref",
                    "original_tip", "repo", "repo_id"},
    }[event]
    if any(row.get(key) != prior.get(key) for key in set(row) | set(prior)
           if key not in mutable):
        return False
    if event == "delivered" and (
            not row.get("delivery_ref") or
            row.get("delivery_ref") != row.get("message_id")):
        return False
    if event == "delivery-failed" and not row.get("delivery_error"):
        return False
    if row.get("status") == "acked" and not row.get("ack_ref"):
        return False
    if row.get("status") == "verdict":
        return bool(row.get("verdict_ref") and row.get("reviewed_tip"))
    return True


def snapshot():
    """(logical rows, unavailable reason). Missing is known-empty; unsafe or
    unreadable storage is UNKNOWN and must surface rather than read as zero."""
    return eventledger.latest_checked(ledger_path(), _valid)


def rows():
    return snapshot()[0]


def history(rid):
    out, prior = [], None
    for row in eventledger.events(ledger_path()):
        if str(row.get("id")) != str(rid):
            continue
        if _valid(row, prior):
            out.append(row)
            prior = row
    return out


def _next(row, status=None, event=None, **patch):
    nxt = dict(row)
    nxt.update(patch)
    nxt["seq"] = (row.get("seq") if isinstance(row.get("seq"), int) else -1) + 1
    nxt["status"] = status or row["status"]
    nxt["event"] = event or nxt["status"]
    nxt["last_updated"] = pk.now_ts()
    return nxt


def _base(recipient, lane, ref, note, deadline_s, repo, status, rid=None,
          dispatch_key=None, message_id=None, message_hash=None, sender=None):
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
    original_ref, err = _clean(ref, "ref", 256)
    if err:
        return None, "ref is required so the eventual verdict can bind an exact tip"
    info = _repo_info(repo)
    if not info:
        return None, "ref needs a Git working tree (--repo PATH)"
    original_tip = _resolve_tip(info["repo"], original_ref)
    if not original_tip:
        return None, "ref is missing, ambiguous, or not a commit in this repository"
    if sender is not None and not _TOKEN.fullmatch(str(sender)):
        return None, "sender must be an exact 1-64 character seat token"
    ts = pk.now_ts()
    row = {"v": 2, "id": rid or os.urandom(16).hex(), "seq": 0,
           "event": "posting" if status == "posting" else "add", "ts": ts,
           "recipient": recipient, "lane": lane, "ref": original_ref,
           "tip": original_tip, "original_ref": original_ref,
           "original_tip": original_tip, "note": note,
           "deadline_s": deadline_s, "source": home.session_id() or "cli",
           "repo": info["repo"] if info else None,
           "repo_id": info["repo_id"] if info else None,
           "dispatch_key": dispatch_key, "message_id": message_id,
           "message_hash": message_hash, "sender": sender, "status": status,
           "ack_ref": None, "verdict_ref": None, "reviewed_tip": None,
           "delivery_ref": None, "delivery_error": None,
           "last_updated": ts}
    return row, None


def _add(recipient, lane, ref=None, note=None, deadline_s=DEFAULT_DEADLINE_S,
         repo=None):
    row, err = _base(recipient, lane, ref, note, deadline_s, repo, "pending")
    if err:
        return None, err
    if not eventledger.append(ledger_path(), row):
        return None, "ledger unwritable (%s) — dispatch NOT recorded" % ledger_path()
    pk.event("dispatch-add", row["id"], "%s -> %s" % (row["recipient"], row["lane"]))
    return row, None


def add(recipient, lane, ref=None, note=None, deadline_s=DEFAULT_DEADLINE_S,
        repo=None):
    """Compatibility API: row or None.  The CLI uses `_add` to surface why."""
    return _add(recipient, lane, ref, note, deadline_s, repo)[0]


def send(recipient, lane, message, ref, note=None, deadline_s=DEFAULT_DEADLINE_S,
         key=None, repo=None, sign=None):
    """Atomically stage one logical dispatch before delivering its idempotent
    DM.  Returns (row, reason, created_message).  On delivery failure the same
    row records ABORTED/NEEDS RETRY; retrying the same key/payload reuses both
    ids.  A ledger failure occurs before any DM side effect."""
    message = str(message or "").strip()
    if not message or len(message) > 16000 or "\x00" in message:
        return None, "message must be 1-16000 characters without NUL", False
    key = str(key or "").strip()
    if key:
        key, err = _clean(key, "idempotency key", 256)
        if err:
            return None, err, False
    from . import seats
    sender = seats.derive_seat(home.session_id())
    staged, err = _base(recipient, lane, ref, note, deadline_s, repo, "posting",
                        sender=sender)
    if err:
        return None, err, False
    if not key:
        canonical = "\0".join((staged["recipient"], staged["lane"],
                                staged["tip"], message, str(staged["deadline_s"]),
                                staged.get("note") or ""))
        key = "auto:" + hashlib.blake2b(
            canonical.encode("utf-8"), digest_size=16).hexdigest()
    namespace = "\0".join((staged["sender"], staged["repo_id"], key))
    staged["id"] = hashlib.blake2b(
        ("dispatch\0" + namespace).encode("utf-8"),
        digest_size=16).hexdigest()
    staged["message_id"] = hashlib.blake2b(
        ("message\0" + namespace).encode("utf-8"), digest_size=6).hexdigest()
    staged["message_hash"] = hashlib.blake2b(
        message.encode("utf-8"), digest_size=16).hexdigest()
    staged["dispatch_key"] = key
    rid = staged["id"]
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — nothing sent" % path, False
        current, unavailable = eventledger.latest_checked(path, _valid)
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable, False
        existing = current.get(rid)
        if existing:
            same = all(existing.get(k) == staged.get(k) for k in
                       ("recipient", "lane", "tip", "note", "deadline_s",
                        "repo_id", "sender", "dispatch_key", "message_id",
                        "message_hash"))
            if not same:
                return None, "idempotency key already names different work", False
            if existing.get("status") in ACTIVE or existing.get("status") == "verdict":
                return existing, None, False
            row = existing
            if row.get("status") == "aborted":
                row = _next(row, status="posting", event="retry",
                            delivery_error=None)
                if not eventledger.append_unlocked(path, row):
                    return None, "ledger retry stage failed — nothing sent", False
        else:
            row = staged
            if not eventledger.append_unlocked(path, row):
                return None, "ledger stage failed — nothing sent", False
        try:
            delivered, dm_err = seats.dm(
                row["recipient"], message, who=row["sender"], profile=row["sender"],
                sign=sign, session=home.session_id(), message_id=row["message_id"])
        except Exception as exc:
            delivered, dm_err = None, "%s: %s" % (type(exc).__name__, exc)
        if dm_err or not delivered:
            failed = _next(row, status="aborted", event="delivery-failed",
                           delivery_error=str(dm_err or "DM returned no row")[:500])
            if not eventledger.append_unlocked(path, failed):
                return row, "delivery failed and failure event could not append: %s" % (
                    dm_err or "no row"), False
            return failed, "delivery failed; row is NEEDS RETRY: %s" % (
                dm_err or "no row"), False
        active = _next(row, status="pending", event="delivered",
                       delivery_ref=delivered.get("id"), delivery_error=None)
        if not eventledger.append_unlocked(path, active):
            return row, ("DM %s landed but activation event failed; retry the same "
                         "command/key to reconcile it" % delivered.get("id")), False
    pk.event("dispatch-send", active["id"], active["delivery_ref"])
    return active, None, True


def _get_locked(rid):
    current, unavailable = snapshot()
    return current.get(str(rid or "")), unavailable


def mark_ack(rid, ref):
    ref, err = _clean(ref, "ack ref", 256)
    if err:
        return None, err
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — ACK NOT recorded" % path
        row, unavailable = _get_locked(rid)
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        if not row:
            return None, "no such dispatch: %s (helm dispatch list)" % rid
        if row.get("status") == "verdict":
            return None, "dispatch %s already has a verdict (closed)" % rid
        if row.get("status") not in ACTIVE:
            return None, "dispatch %s was not delivered; it NEEDS RETRY, not ACK" % rid
        nxt = _next(row, status="acked", event="ack", ack_ref=ref)
        if not eventledger.append_unlocked(path, nxt):
            return None, "ledger unwritable (%s) — ACK NOT recorded" % path
    pk.event("dispatch-ack", str(rid), ref)
    return nxt, None


def _repo_binding(row, caller_repo=None):
    """Return (caller worktree, current tip, migration patch, error).
    Pre-v2 rows are safely adopted only when their literal ref resolves in the
    caller's repository; that one retarget/verdict event records the binding."""
    caller = _repo_info(caller_repo)
    if not caller:
        return None, None, None, "caller is outside a Git working tree"
    if row.get("repo_id"):
        if caller["repo_id"] != row["repo_id"]:
            return None, None, None, "caller is outside the dispatch repository (foreign ref refused)"
        if not row.get("tip"):
            return None, None, None, "dispatch repository binding has no exact tip"
        return caller["repo"], row["tip"], {}, None
    legacy = row.get("ref")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", str(legacy or "")):
        return None, None, None, ("legacy dispatch ref is mutable or not an object id; "
                                  "open a new exact-ref dispatch")
    tip = _resolve_tip(caller["repo"], legacy)
    if not tip:
        return None, None, None, ("legacy dispatch object id is missing, ambiguous, or foreign; "
                                  "open a new exact-ref dispatch")
    patch = {"repo": caller["repo"], "repo_id": caller["repo_id"],
             "original_ref": legacy, "original_tip": tip}
    return caller["repo"], tip, patch, None


def bind(rid, ref, repo=None):
    """CAS-bind an e226-era v2 ref-less row to one exact repository commit so
    its verdict becomes closable without rewriting its original add event."""
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — bind NOT recorded" % path
        row, unavailable = _get_locked(rid)
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        if not row:
            return None, "no such dispatch: %s (helm dispatch list)" % rid
        if row.get("status") == "verdict":
            return None, "dispatch %s already has a verdict (closed)" % rid
        if any(row.get(k) for k in ("ref", "tip", "original_ref",
                                    "original_tip", "repo", "repo_id")):
            return None, "dispatch %s is already tip-bound" % rid
        info = _repo_info(repo)
        clean, err = _clean(ref, "ref", 256)
        tip = _resolve_tip(info["repo"], clean) if info and not err else None
        if not tip:
            return None, "bind ref is missing, ambiguous, or foreign"
        nxt = _next(row, event="bind", ref=clean, tip=tip,
                    original_ref=clean, original_tip=tip,
                    repo=info["repo"], repo_id=info["repo_id"])
        if not eventledger.append_unlocked(path, nxt):
            return None, "ledger unwritable (%s) — bind NOT recorded" % path
    pk.event("dispatch-bind", str(rid), tip)
    return nxt, None


def retarget(rid, old_ref, new_ref, repo=None):
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — retarget NOT recorded" % path
        row, unavailable = _get_locked(rid)
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        if not row:
            return None, "no such dispatch: %s (helm dispatch list)" % rid
        if row.get("status") == "verdict":
            return None, "dispatch %s already has a verdict (closed)" % rid
        if row.get("status") not in ACTIVE:
            return None, "dispatch %s NEEDS DELIVERY RETRY before retarget" % rid
        stored, current_tip, binding, err = _repo_binding(row, repo)
        if err:
            return None, err
        old_tip = _resolve_tip(stored, old_ref)
        new_tip = _resolve_tip(stored, new_ref)
        if not old_tip or not new_tip:
            return None, "old/new ref is missing, ambiguous, or foreign"
        if old_tip != current_tip:
            return None, "stale retarget: current tip is %s" % current_tip
        if new_tip == old_tip:
            return row, None
        if not _is_ancestor(stored, old_tip, new_tip):
            return None, "backward or divergent retarget refused"
        ref, err = _clean(new_ref, "new ref", 256)
        if err:
            return None, err
        nxt = _next(row, event="retarget", ref=ref, tip=new_tip,
                    retarget_from=old_tip, retarget_to=new_tip, **binding)
        if not eventledger.append_unlocked(path, nxt):
            return None, "ledger unwritable (%s) — retarget NOT recorded" % path
    pk.event("dispatch-retarget", str(rid), "%s -> %s" % (old_tip, new_tip))
    return nxt, None


def mark_verdict(rid, reviewed_ref, evidence, repo=None):
    evidence, err = _clean(evidence, "verdict evidence", 256)
    if err:
        return None, err
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
        row, unavailable = _get_locked(rid)
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        if not row:
            return None, "no such dispatch: %s (helm dispatch list)" % rid
        stored, current_tip, binding, err = _repo_binding(row, repo)
        if err:
            return None, err
        reviewed_tip = _resolve_tip(stored, reviewed_ref)
        if not reviewed_tip:
            return None, "reviewed tip is missing, ambiguous, or foreign"
        if row.get("status") == "verdict":
            if row.get("reviewed_tip") == reviewed_tip and row.get("verdict_ref") == evidence:
                return row, None
            return None, "dispatch %s already has a verdict (closed)" % rid
        if row.get("status") not in ACTIVE:
            return None, "dispatch %s was not delivered; it NEEDS RETRY" % rid
        if reviewed_tip != current_tip:
            return None, "stale verdict: reviewed %s but current tip is %s" % (
                reviewed_tip, current_tip)
        nxt = _next(row, status="verdict", event="verdict", tip=current_tip,
                    verdict_ref=evidence, reviewed_tip=reviewed_tip, **binding)
        if not eventledger.append_unlocked(path, nxt):
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
    pk.event("dispatch-verdict", str(rid), evidence)
    return nxt, None


def _age_s(row, now=None):
    """UTC calendar age.  Clock skew clamps to NEW; malformed timestamps are
    NEW too, never false-overdue."""
    try:
        stamp = time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ")
        return max(0, int((time.time() if now is None else now) - calendar.timegm(stamp)))
    except (OverflowError, ValueError, TypeError):
        return 0


def open_rows():
    out = [r for r in rows().values() if r.get("status") in ACTIVE]
    return sorted(out, key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))


def needs_retry():
    out = [r for r in rows().values() if r.get("status") in ("posting", "aborted")]
    return sorted(out, key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))


def _is_overdue(row, now=None):
    return row.get("status") in ACTIVE and _age_s(row, now) >= int(row["deadline_s"])


def overdue():
    now = time.time()
    return [r for r in open_rows() if _is_overdue(r, now)]


def oldest_overdue():
    out = overdue()
    return out[0] if out else None


def stop_candidate():
    """(row, kind, unavailable). Delivery retries outrank aged check-ins; an
    unreadable ledger is explicit UNKNOWN, never silent zero obligations."""
    current, unavailable = snapshot()
    if unavailable:
        return None, None, unavailable
    ordered = sorted(current.values(),
                     key=lambda r: (str(r.get("ts") or ""), r["id"]))
    unbound = next((r for r in ordered
                    if r.get("status") in ACTIVE and not r.get("tip")), None)
    if unbound:
        return unbound, "bind", None
    retry = next((r for r in ordered
                  if r.get("status") in ("posting", "aborted")), None)
    if retry:
        return retry, "retry", None
    now = time.time()
    late = next((r for r in ordered if _is_overdue(r, now)), None)
    return late, "overdue" if late else None, None


USAGE = ("usage: helm dispatch send <recipient> <lane> <message...> --ref TIP "
         "[--key K] [--note N] [--deadline SECONDS] [--repo PATH] | add "
         "<recipient> <lane> --ref TIP [--note N] [--deadline SECONDS] "
         "[--repo PATH] | ack <id> <ref> | bind <id> <tip> [--repo PATH] | "
         "retarget <id> <old-tip> <new-tip> "
         "[--repo PATH] | verdict <id> <reviewed-tip> <evidence> [--repo PATH] | "
         "list [--open|--overdue|--needs-retry] [--json]")


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


def _label(status, row=None):
    if row is not None and status in ACTIVE and not row.get("tip"):
        return "NEEDS TIP BINDING"
    return {"posting": "NEEDS DELIVERY RETRY", "aborted": "NEEDS DELIVERY RETRY",
            "pending": "PENDING VERDICT", "open": "PENDING VERDICT",
            "acked": "ACKED / PENDING VERDICT", "verdict": "VERDICT"}.get(
                status, "NEEDS INSPECTION")


def _fmt(row, late=False):
    age = _age_s(row) // 60
    deadline = int(row["deadline_s"]) // 60
    tip = row.get("tip") or row.get("ref") or "-"
    suffix = "  NEEDS CHECK-IN (OVERDUE)" if late else ""
    return "  %s  %-16s %-24s %-27s %3dm/%dm%s  %s" % (
        row["id"], row["recipient"], row["lane"], _label(row["status"], row),
        age, deadline, suffix, str(tip)[:12])


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
        deadline, err = _deadline(opts.get("--deadline", DEFAULT_DEADLINE_S))
        if err:
            print("helm dispatch: " + err, file=sys.stderr)
            return 2
        if not opts.get("--ref"):
            print("helm dispatch: %s requires --ref TIP" % verb, file=sys.stderr)
            return 2
        if verb == "send":
            row, why, posted = send(
                pos[0], pos[1], " ".join(pos[2:]), opts["--ref"],
                note=opts.get("--note"), deadline_s=deadline,
                key=opts.get("--key"), repo=opts.get("--repo"))
            if why:
                print("helm dispatch: " + why, file=sys.stderr)
                return 1
            print("helm dispatch: %s @%s %s — PENDING VERDICT%s" % (
                row["id"], row["recipient"], row["lane"],
                " (DM %s sent)" % row["delivery_ref"] if posted else
                " (idempotent retry: already sent)"))
            return 0
        row, why = _add(pos[0], pos[1], ref=opts.get("--ref"),
                        note=opts.get("--note"), deadline_s=deadline,
                        repo=opts.get("--repo"))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s -> @%s %s — PENDING VERDICT; CHECK IN after %dm" % (
            row["id"], row["recipient"], row["lane"], deadline // 60))
        return 0
    if verb == "ack":
        if len(rest) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        row, why = mark_ack(rest[0], " ".join(rest[1:]))
    elif verb == "bind":
        pos, opts, err = _parse(rest, {"--repo"})
        if err or len(pos) != 2:
            print("helm dispatch: " + (err or USAGE), file=sys.stderr)
            return 2
        row, why = bind(pos[0], pos[1], repo=opts.get("--repo"))
    elif verb == "retarget":
        pos, opts, err = _parse(rest, {"--repo"})
        if err or len(pos) != 3:
            print("helm dispatch: " + (err or USAGE), file=sys.stderr)
            return 2
        row, why = retarget(pos[0], pos[1], pos[2], repo=opts.get("--repo"))
    elif verb == "verdict":
        pos, opts, err = _parse(rest, {"--repo"})
        if err or len(pos) < 3:
            print("helm dispatch: " + (err or USAGE), file=sys.stderr)
            return 2
        row, why = mark_verdict(pos[0], pos[1], " ".join(pos[2:]),
                                repo=opts.get("--repo"))
    else:
        row = why = None
    if verb in ("ack", "bind", "retarget", "verdict"):
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — %s%s" % (
            row["id"], _label(row["status"]),
            " at %s" % row["tip"][:12] if row.get("tip") else ""))
        return 0
    if verb == "list":
        current, unavailable = snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; obligations UNKNOWN: %s"
                  % unavailable, file=sys.stderr)
            return 1
        flags = set(rest)
        if flags - {"--open", "--overdue", "--needs-retry", "--json"}:
            print(USAGE, file=sys.stderr)
            return 2
        if len(flags & {"--open", "--overdue", "--needs-retry"}) > 1:
            print("helm dispatch: choose one status filter", file=sys.stderr)
            return 2
        selected = sorted(current.values(),
                          key=lambda r: (str(r.get("ts") or ""), r["id"]))
        if "--overdue" in flags:
            now = time.time()
            selected = [r for r in selected if _is_overdue(r, now)]
        elif "--open" in flags:
            selected = [r for r in selected if r.get("status") in ACTIVE]
        elif "--needs-retry" in flags:
            selected = [r for r in selected
                        if r.get("status") in ("posting", "aborted")]
        if "--json" in flags:
            print(json.dumps(selected, ensure_ascii=False, indent=1))
            return 0
        if not selected:
            print("helm dispatch: no matching rows; no PENDING obligation in this view")
            return 0
        print("helm dispatch — %d logical row%s" % (
            len(selected), "" if len(selected) == 1 else "s"))
        now = time.time()
        for row in selected:
            print(_fmt(row, _is_overdue(row, now)))
        late = [r for r in selected if _is_overdue(r, now)]
        if late:
            print("%d NEEDS CHECK-IN (OVERDUE) — advisory only; do not reassign. "
                  "Verify at the exact recipient first." % len(late))
        retry = [r for r in selected if r.get("status") in ("posting", "aborted")]
        if retry:
            print("%d NEEDS DELIVERY RETRY — retry the same send/key; no verdict "
                  "obligation exists until delivery lands." % len(retry))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
