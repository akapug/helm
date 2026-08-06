#!/usr/bin/env python3
"""helm seats — claims: the advisory TTL lease, and who is really holding it.

A CLAIM ANSWERS A NARROWER QUESTION THAN PEOPLE ASK OF IT. It guards a
resource — a worktree, a branch, a named thing — so it says "is anyone else in
this ROOM", never "is anyone else fixing this DEFECT". Two seats can hold two
valid uncontested leases aimed at one bug and the board reads perfectly
healthy. That gap is not a flaw in the lease; it is the lease's scope, and the
disclosure at the claim seam exists because of it.

ADVISORY IS DELIBERATE. Nothing here can stop a process from editing a file.
What it can do is make the collision VISIBLE and give the holder a name, so
the resolution is a conversation rather than a race. A lease that pretended to
be a lock would be a lie the first time a process died holding one.

THE CENSUS HALF IS WHERE THE HARD PART LIVES. A lease outlives the process
that took it, so "is this holder still alive" is a question about a pid, a
session, and a start time — and every one of those can be unreadable. The
identity fields are an ALLOWLIST rather than a denylist of bad ones, and the
row schema is pinned by a test, because a census that quietly invents a state
is worse than one that says UNKNOWN.

The deferred import in own_leases reaches the CLI for _env_session — one call
site, the thinner leg of the only cycle left in this file.
"""

import json
import math
import os
import time

from . import chat, home, pk
from .seats_common import (DEFAULT_TTL, STATUS_BYTES, STRICT_CLAIM_LOCK_POLL_S,
                           STRICT_CLAIM_LOCK_WAIT_S, _clip, _flocked, _now_mono,
                           _proc_stat_link, _scrub, _sweep, claims_path,
                           process_sid_scan)
from .seats_identity import acting_seat, safe_cwd
from .seats_delegation import _unlink_delegation_activity

def _flock_holder(path, proc_dir="/proc"):
    """(pid, state) of the process holding an FLOCK on `path`, else None.

    A WEDGED claims lock and a merely BUSY one are identical at the call
    site: both time out after STRICT_CLAIM_LOCK_WAIT_S. The difference is
    the HOLDER. A running peer finishes; a STOPPED one never does, and no
    lease TTL rescues it, because a TTL governs the lease RECORD while a
    flock governs FILE ACCESS one layer below it. /proc/locks names the
    holder by inode, which is the only fact that separates the two.

    THE PID NAMES AN OPEN FILE DESCRIPTION, NOT A CULPRIT. An flock is held
    by the fd, and processes that inherit that fd across fork() share ONE
    lock — so the pid the kernel records can be a PARENT, a child, or a
    sibling of whichever process someone suspects. Killing the named pid can
    therefore leave the lock standing, because another holder of the same fd
    is still alive. That is not this function lying: it reports what
    /proc/locks records, and any pid we substituted by reasoning about
    ancestry would be OUR GUESS wearing a measurement's clothes. Report the
    recorded holder; let the reader walk the tree.
    """
    try:
        ino = os.stat(path).st_ino
    except OSError:
        return None
    me = os.getpid()
    try:
        with open(os.path.join(proc_dir, "locks")) as f:
            for line in f:
                col = line.split()
                if len(col) < 6 or col[1] != "FLOCK":
                    continue
                if col[5].rpartition(":")[2] != str(ino):
                    continue
                try:
                    pid = int(col[4])
                except ValueError:
                    continue
                if pid == me:
                    continue
                link = _proc_stat_link(pid, proc_dir=proc_dir, with_state=True)
                return pid, (link[2] if link else None)
    except OSError:
        return None
    return None
def _lock_unavailable(path=None, proc_dir="/proc"):
    """The claims-lock refusal, naming the holder when /proc can name it.

    "claim lock is unavailable" is true and unactionable — it says a wait
    ended, not why, so a wedge reads exactly like contention and an
    operator's next move is a guess. Naming a STOPPED holder turns it into
    an instruction. Falls back to the bare sentence wherever /proc cannot
    answer, because a diagnostic that raises is worse than a vague one.
    """
    base = "claim lock is unavailable"
    held = _flock_holder(path if path else claims_path() + ".lock",
                         proc_dir=proc_dir)
    if not held:
        return base
    pid, state = held
    if state in ("T", "t"):
        return ("%s — held by STOPPED pid %d (state %s), which never releases "
                "it; no lease TTL covers a flock, so this wedges every strict "
                "claim in this project until that process resumes or dies"
                % (base, pid, state))
    return "%s — held by pid %d (state %s)" % (base, pid, state or "unknown")
class _claim_flocked:
    """Bounded exclusive lock for the serializer's strict claim operations."""
    def __init__(self, strict):
        self.strict, self.lock, self.f = strict, None, None

    def __enter__(self):
        if not self.strict:
            self.lock = _flocked(claims_path() + ".lock")
            self.lock.__enter__()
            self.f = self.lock.f
            return self
        deadline = _now_mono() + STRICT_CLAIM_LOCK_WAIT_S
        while True:
            lock = _flocked(claims_path() + ".lock", blocking=False)
            lock.__enter__()
            if lock.f is not None:
                self.lock, self.f = lock, lock.f
                return self
            lock.__exit__(None, None, None)
            if _now_mono() >= deadline:
                return self
            time.sleep(STRICT_CLAIM_LOCK_POLL_S)

    def __exit__(self, *exc):
        if self.lock is not None:
            return self.lock.__exit__(*exc)
        return False
def _unique_json_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate object key")
        out[key] = value
    return out
def _claims_read(strict=False):
    if not strict:
        return pk.read_json(claims_path(), {}) or {}
    try:
        with open(claims_path(), encoding="utf-8") as f:
            state = json.load(f, object_pairs_hook=_unique_json_object)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise OSError("claim state is unreadable: %s" % type(exc).__name__)
    if not isinstance(state, dict):
        raise OSError("claim state is unreadable: expected an object")
    rows = [row for resource, row in state.items() if resource != "_fence"]
    fence = state.get("_fence")
    if rows and type(fence) is not int:
        raise OSError("claim state is unreadable: malformed fence")
    highest = 0
    for resource, row in state.items():
        if resource == "_fence":
            continue
        numbers = (row.get("exp_mono"), row.get("exp_wall")) \
            if isinstance(row, dict) else ()
        valid = isinstance(resource, str) and bool(resource) \
            and isinstance(row, dict) \
            and isinstance(row.get("holder"), str) and bool(row["holder"]) \
            and (row.get("session") is None
                 or isinstance(row.get("session"), str)) \
            and isinstance(row.get("lease"), str) and bool(row["lease"]) \
            and type(row.get("fence")) is int and row["fence"] > 0 \
            and all(type(value) is int or (type(value) is float
                                           and math.isfinite(value))
                    for value in numbers) \
            and isinstance(row.get("ts"), str) and bool(row["ts"])
        if not valid:
            raise OSError("claim state is unreadable: malformed claim row")
        highest = max(highest, row["fence"])
    if fence is not None and (type(fence) is not int or fence < highest):
        raise OSError("claim state is unreadable: malformed fence")
    return state
def _binding_ok(row, seat, lease, session):
    """The composite check, validated TOGETHER: the lease
    nonce is a CONFIRMATION TOKEN, the supplied seat must be the recorded
    holder, and when both the grant and the caller carry a session they must
    agree. Never lease-OR-session: a roster-visible session id alone must
    open nothing.

    What the nonce IS, stated exactly: proof that the
    caller is the DELIBERATE holder — not a stray sweep, not a fat-fingered
    `release` aimed at the wrong resource, not a stale holder whose grant
    already expired and was re-granted (the ABA case). It is NOT a secret and
    NOT a capability: every seat on this box runs as the same uid and can read
    the whole ledger out of `.claims.json`, so no refusal here can stop a
    hostile or buggy seat. Calling it a capability was false comfort AND
    actively harmful — for three months the only party it inconvenienced was
    the honest holder who lost their own token to compaction, whose lane then
    sat locked for the full TTL. `own_leases` is the cure; this message names
    it."""
    if not lease or row.get("lease") != lease:
        return False, ("the lease id (yours prints in `helm chat claims` and "
                       "`helm work list`)")
    if seat != row.get("holder"):
        return False, "the holding seat (%s)" % row.get("holder")
    if row.get("session") and session and row["session"] != str(session):
        return False, "the granting session"
    return True, None
def claim(resource, seat, ttl=DEFAULT_TTL, lease=None, session=None,
          strict=False):
    """(ok, message, lease_id). A fresh grant mints a random lease nonce +
    an increasing fence and records the caller's ambient session (display /
    extra binding — never an authorizer). EXTENDING a live lease requires
    the full binding {lease, seat, session-if-recorded}; a display name or
    a copied session id alone extends nothing. Expiry is
    monotonic (tmpfs state dies with the boot; wall time only displays).
    Check+sweep+write hold one flock. `strict=True` is the serializer seam:
    it waits boundedly through contention, then raises on persistent lock
    unavailability instead of preserving this API's historical fail-open."""
    chat._ensure_dir()
    with _claim_flocked(strict) as lock:
        if strict and lock.f is None:
            raise OSError(_lock_unavailable())
        c = _sweep(_claims_read(strict))
        row = c.get(resource)
        if row:
            ok, needs = _binding_ok(row, seat, lease, session)
            if not ok:
                return False, "%s is held by %s for %ds more (extend needs %s)" % (
                    resource, row.get("holder"),
                    int(row["exp_mono"] - _now_mono()), needs), None
            lease_id, fence = row["lease"], row["fence"]
        else:
            lease_id = os.urandom(8).hex()
            fence = int(c.get("_fence", 0)) + 1
            c["_fence"] = fence
        c[resource] = {"holder": seat, "session": str(session) if session else None,
                       "lease": lease_id, "fence": fence,
                       "exp_mono": _now_mono() + ttl,
                       "exp_wall": time.time() + ttl, "ts": pk.now_ts()}
        pk.write_json(claims_path(), c)
        return True, "%s claimed by %s for %ds (lease %s, fence %d)" % (
            resource, seat, ttl, lease_id, fence), lease_id
def refresh_claim(resource, seat, lease=None, session=None, ttl=DEFAULT_TTL,
                  strict=False):
    """Strictly validate and extend an EXISTING grant.

    Unlike `claim`, absence never mints a replacement lease. Lifecycle code uses
    this before an authorized pre-release mutation (for example a park commit),
    closing the gap where a wrong or expired confirmation token could change
    room bytes before `release()` finally refused it. `strict=True` waits
    boundedly through contention and refuses persistent lock unavailability
    instead of refreshing without exclusion."""
    with _claim_flocked(strict) as lock:
        if strict and lock.f is None:
            raise OSError(_lock_unavailable())
        c = _sweep(_claims_read(strict))
        row = c.get(resource)
        if not row:
            pk.write_json(claims_path(), c)
            return False, "%s is not claimed" % resource
        ok, needs = _binding_ok(row, seat, lease, session)
        if not ok:
            return False, "%s stays held — refresh needs %s" % (resource, needs)
        row["exp_mono"] = _now_mono() + ttl
        row["exp_wall"] = time.time() + ttl
        row["ts"] = pk.now_ts()
        c[resource] = row
        pk.write_json(claims_path(), c)
        return True, "%s lease refreshed for %ds" % (resource, ttl)
def claim_guard(resource, seat, lease, session, action):
    """Run `action` while the exact live claim remains exclusively validated.

    This is the compatibility serializer's commit point: the callback is short
    (one receipt append), lock acquisition is bounded/fail-closed, and no missing
    or replaced lease can be laundered into a durable result.
    """
    with _claim_flocked(True) as lock:
        if lock.f is None:
            return None, _lock_unavailable()
        try:
            c = _sweep(_claims_read(True))
        except OSError as exc:
            return None, str(exc)
        row = c.get(resource)
        if not row:
            return None, "%s is not claimed" % resource
        ok, needs = _binding_ok(row, seat, lease, session)
        if not ok:
            return None, "%s claim changed — guard needs %s" % (resource, needs)
        return action(), None
def rebind_claim_sessions(seat, old_session, new_session):
    """Move one seat's live claim bindings across a proven session replacement.

    Returns the exact resource keys moved, which scopes a registration rollback.
    Every grant byte except `session` is preserved: nonce, fence, expiry, holder
    and timestamp do not become a fresh lease merely because its process resumed.
    """
    old, new = str(old_session or ""), str(new_session or "")
    if not seat or not old or not new or old == new:
        return []
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        moved = []
        for resource, row in list(c.items()):
            if resource == "_fence" or not isinstance(row, dict):
                continue
            if row.get("holder") != seat or row.get("session") != old:
                continue
            row = dict(row)
            row["session"] = new
            c[resource] = row
            moved.append(resource)
        pk.write_json(claims_path(), c)
    for resource in moved:
        _unlink_delegation_activity(resource)
    return moved
def rollback_claim_sessions(seat, old_session, new_session, resources):
    """Undo only rows moved by one failed resume registration transaction."""
    old, new = str(old_session or ""), str(new_session or "")
    wanted = set(resources or ())
    if not seat or not old or not new or old == new or not wanted:
        return 0
    with _flocked(claims_path() + ".lock"):
        c = _sweep(pk.read_json(claims_path(), {}) or {})
        rolled = []
        for resource in wanted:
            row = c.get(resource)
            if not isinstance(row, dict) or row.get("holder") != seat \
                    or row.get("session") != new:
                continue
            row = dict(row)
            row["session"] = old
            c[resource] = row
            rolled.append(resource)
        pk.write_json(claims_path(), c)
    for resource in rolled:
        _unlink_delegation_activity(resource)
    return len(rolled)
def release(resource, seat, lease=None, session=None, strict=False):
    """(ok, message). Release demands the SAME composite binding as extend —
    {lease token, holding seat, session-if-recorded}. A stale holder whose
    lease expired-and-was-regranted fails on the fresh nonce (ABA), and a
    caller who copied a session id out of the roster fails on the lease
    (the exact reproduced case). The token is a deliberate-holder proof,
    never a secret — see `_binding_ok` and `own_leases`. `strict=True` waits
    boundedly through contention and refuses persistent lock unavailability
    instead of releasing without exclusion."""
    with _claim_flocked(strict) as lock:
        if strict and lock.f is None:
            raise OSError(_lock_unavailable())
        c = _sweep(_claims_read(strict))
        row = c.get(resource)
        if not row:
            pk.write_json(claims_path(), c)
            return False, "%s is not claimed" % resource
        ok, needs = _binding_ok(row, seat, lease, session)
        if not ok:
            return False, "%s stays held — release needs %s" % (resource, needs)
        del c[resource]
        pk.write_json(claims_path(), c)
    _unlink_delegation_activity(resource)
    return True, "%s released" % resource
# The keys `_proc_claude_census` contracts to return. A result missing any of
# them is not a census — it is something else — and `or {}` used to launder
# exactly that into a COMPLETE negative (meld producer shape A).
_CENSUS_KEYS = ("rows", "listing_failed", "who_failed", "census_partial")
# Every documented per-row uncertainty marker. THE SET IS PINNED AGAINST THE
# CENSUS ROW SCHEMA BY A TEST, and that pin is the anti-spiral device: the five
# serialized review rounds each happened because completeness was decided by a
# HAND-PICKED pair of booleans (`probe_failed|who_probe_failed`), which is right
# for the cases you thought of and generates a new round for every case you did
# not. A marker added upstream now fails the test instead of silently widening
# the set of claims helm will delete.
_ROW_UNCERTAIN = ("probe_failed", "who_probe_failed", "who_context_mismatch")
# The producer's DECLARED identity enum (session.py: "declared" if declared else
# "resume" if resume else "who" if attributed else "unknown"). ATTRIBUTED is an
# ALLOWLIST over these, never a denylist of the bad ones: `identity="garbage"`
# passed a denylist and read as attributed, which is the SAME hand-picked-set
# defect the marker pin exists to end — committed one function away from it.
# Pinned against the producer by a test, exactly like _ROW_UNCERTAIN.
_IDENTITY_ATTRIBUTED = frozenset(("declared", "resume", "who"))
_IDENTITY_ENUM = _IDENTITY_ATTRIBUTED | frozenset(("unknown",))
def row_attributed(row):
    """Can this census row be EXCLUDED as a holder of somebody's session?

    Contract-derived, not enumerated. The census resolves each row to an
    `identity` of declared / resume / who / unknown, and its own law is that it
    "never turns a failed probe into a proven-empty row". So a row is
    excludable only when it carries a POSITIVE identity, no documented
    uncertainty marker, and a trusted config root — `root is None` means
    config-untrusted, and an identity we do not trust cannot exclude anything.

    An unattributed LIVE Claude process is the case that matters: nobody could
    say whose it is, so it may be holding the very session we are about to
    declare dead."""
    if not isinstance(row, dict):
        return False
    # ALLOWLIST over the producer's enum. A value outside it is a row this
    # module does not understand, and an ununderstood row cannot exclude a
    # holder — meld case (B), identity="garbage".
    # TOTAL BY CONSTRUCTION, like `_ident_of`: an UNHASHABLE identity (a list,
    # a dict) raised TypeError straight out of the membership test, so one
    # malformed row could crash claims_list and every surface reading it. A
    # value that is not even a string is not an attribution.
    identity = row.get("identity")
    if not isinstance(identity, str) or identity not in _IDENTITY_ATTRIBUTED:
        return False
    if any(row.get(k) for k in _ROW_UNCERTAIN):
        return False
    return row.get("root") is not None
def liveness_snapshot(sids=()):
    """ONE reading of both censuses, shared by every claim in one pass.

    -> {"rows", "complete", "scanned", "scan_readable"}.

    WHY A SNAPSHOT AND NOT A PROBE PER CLAIM. `claim_holder_liveness` used to
    read the whole census itself, and `claims_list` calls it once PER ROW: N
    claims meant N /proc walks, and — worse than the cost — two rows in ONE
    rendered table could be classified from DIFFERENT evidence, so the same
    session could read live on one line and stale on the next. A verdict that
    depends on when in the loop you were reached is not a verdict.

    `complete` is FALSE for any incompleteness at all, global or per-row: a
    partial listing and an unprobed `who` rung each hide exactly the process
    that would have proven a holder alive. `scan_readable` is the same fact for
    the secondary name scan, which can raise rather than answer.

    Built by a cross-family meld matrix after five serialized review
    rounds found five SITES of one invariant: an unreadable input must never
    become a definite answer."""
    schema_ok = True
    try:
        from . import session as sess_mod
        census = sess_mod._proc_claude_census()
    except Exception:                           # noqa: BLE001
        census, schema_ok = {}, False
    # SHAPE A: `or {}` laundered a malformed or missing result into a COMPLETE
    # negative. A result that is not a census cannot certify an empty estate.
    # KEY PRESENCE IS NOT VALUE VALIDITY — meld case (A). A census whose
    # flags are all present but None passed the old check, and `not None` then
    # read as False, so a completely unknown estate certified itself complete.
    # A flag that is not a bool is not an answer.
    if not isinstance(census, dict) or any(k not in census
                                           for k in _CENSUS_KEYS):
        census, schema_ok = (census if isinstance(census, dict) else {}), False
    elif not isinstance(census.get("rows"), (list, tuple)) or any(
            not isinstance(census.get(k), bool)
            for k in _CENSUS_KEYS if k != "rows"):
        schema_ok = False
    rows = census.get("rows") or []
    # SHAPE B: row completeness comes from the census CONTRACT (`identity`,
    # its documented uncertainty markers, and config trust), not from two
    # hand-picked booleans — see row_attributed.
    complete = bool(schema_ok) and not (
        census.get("listing_failed")
        or census.get("census_partial")
        or census.get("who_failed")
        or any(not row_attributed(r) for r in rows))
    want = {s for s in (sids or ()) if s}
    if want:
        try:
            scanned, scan_readable = process_sid_scan(want)
        except Exception:                       # noqa: BLE001 — a scan that
            scanned, scan_readable = set(), False  # cannot run proves nothing
    else:
        scanned, scan_readable = set(), True
    return {"rows": rows, "complete": complete,
            "scanned": scanned, "scan_readable": scan_readable}
def claim_liveness_mark(claim):
    """The ONE rendering of a claim's liveness for every human surface.

    THREE STATES, THREE SENTENCES, and the remedy rides ONLY with the state
    that has one. A surface that prints nothing for `unknown` says the claim is
    healthy, which is the collapse this whole meld exists to end — and offering
    `--stale` on an unknown would invite a release the verb itself refuses.

    Factored out because the same ternary lived at three call sites and I had
    already shipped a version that updated two of them: three copies of a
    two-state test is how the third state gets forgotten at one site."""
    state = (claim or {}).get("liveness")
    if state == "stale":
        return "  STALE — holder is dead; `helm work release <lane> --stale`"
    if state == "unknown":
        return "  UNKNOWN — liveness unprovable; NOT releasable"
    return ""
def claim_holder_liveness(holder, session=None, snap=None):
    """Probe whether a claim holder is demonstrably alive or dead.

    Returns (liveness, reason). liveness is one of:
      - "live"   — the holder is demonstrably alive (session live, or
                   process carrying their name)
      - "stale"  — the holder is provably dead (recorded session is dead
                   and no process carries it)
      - "unknown" — cannot prove either way (no session recorded, or the
                   evidence systems are unreadable)

    "stale" is the actionable negative: a claim whose holder is stale can
    be released by another seat through `release_stale`. "unknown" is
    deliberately NOT actionable — an uncertain proof must never authorize
    releasing a live holder's claim."""

    # THE STRONGEST EVIDENCE: a recorded session. If the session is live,
    # the holder is live. If the session is dead and no process carries it,
    # the holder is stale.
    if session:
        # AN UNREADABLE CENSUS IS NOT AN EMPTY ONE. Swallowing the failure into
        # `live = {}` made "I could not look" and "I looked and it is absent"
        # the same value, and the absence then fell through to "stale" — which
        # AUTHORIZES ANOTHER SEAT TO RELEASE A LIVE HOLDER'S CLAIM. The
        # docstring above already says unreadable evidence must be unknown;
        # this is the code finally agreeing with it. Found in cross-family
        # review with a deterministic probe (live_sids unreadable, no substring
        # hit -> stale, and release_stale then deleted the claim).
        # ONE SNAPSHOT, taken by the caller when there is a batch. Every
        # rung below reads the SAME evidence, so two claims in one rendered
        # table can no longer disagree about the same session.
        snap = snap or liveness_snapshot({session})
        rows = snap.get("rows") or []
        from . import session as sess_mod
        try:
            live = set(sess_mod.live_sids(rows))
            # A pid that COULD hold this sid but whose identity is unproven is
            # exactly the row live_sids drops — live_sids is PROVEN-POSITIVE
            # only, and its omission is not a negative. open_pids keeps it.
            possible_holder = bool(sess_mod.open_pids(session, rows)) \
                and session not in live
        except Exception:                       # noqa: BLE001
            live, possible_holder = set(), True
        census_readable = bool(snap.get("complete"))
        if session in live:
            return "live", "session %.8s is live" % session
        # POSITIVE EVIDENCE STANDS ON ITS OWN and is checked before the bail:
        # a live process proves the holder alive whether or not the census
        # could be read, so an unreadable census never downgrades a "live".
        # THE SECONDARY SCAN CAN RAISE, AND IT SAT OUTSIDE THE GUARD.
        # `_sessions_with_a_process` does a bare os.listdir on HELM_PROC, so a
        # missing or unreadable root threw FileNotFoundError straight out of a
        # function whose whole contract is three states — crashing claims_list,
        # the roster, the web read and release_stale alike.
        # A positive hit still stands on its own; only the NEGATIVE
        # needs the scan to have actually happened.
        alive = snap.get("scanned") or set()
        scan_readable = bool(snap.get("scan_readable"))
        if session in alive:
            return "live", "session %.8s has a live process" % session
        if not scan_readable:
            return "unknown", (
                "session %.8s: the process census could not be READ (HELM_PROC "
                "unreadable), so nothing was looked at — an absence nobody "
                "measured is not a death" % session)
        if possible_holder:
            return "unknown", (
                "session %.8s: a live process is CAPABLE of holding it but its "
                "identity is unproven, so it is neither proven live nor proven "
                "dead — a stale release needs proof of DEATH" % session)
        if not census_readable:
            return "unknown", (
                "session %.8s: the session census is UNREADABLE and no process "
                "carries it, so its absence proves NOTHING — a stale release "
                "needs proof of death, not a failed lookup" % session)
        return "stale", ("session %.8s is dead (no live claude process "
                        "or /proc evidence)" % session)

    # No session recorded — the claim was minted outside a harness, or
    # predates session recording. Check /proc for the holder name.
    try:
        from . import home as home_mod
        proc_dir = home_mod.env("PROC") or "/proc"
    except Exception:
        proc_dir = "/proc"
    try:
        me_uid = os.getuid()
        needle = holder.encode("utf-8")
        for pid in os.listdir(proc_dir):
            if not pid.isdigit():
                continue
            try:
                if os.stat(os.path.join(proc_dir, pid)).st_uid != me_uid:
                    continue
                with open(os.path.join(proc_dir, pid, "cmdline"), "rb") as f:
                    if needle in f.read(1 << 20):
                        return "live", "holder name found in a live /proc entry"
            except OSError:
                continue
    except Exception:
        pass

    return "unknown", "no session recorded; cannot prove liveness"
def release_stale(resource, seat, session=None):
    """Release a claim whose holder is demonstrably dead.

    Returns (ok, message). Only releases when `claim_holder_liveness`
    returns "stale" — never on "live" or "unknown". An uncertain proof
    must never authorize releasing a live holder's claim.

    The lease token is deliberately NOT required here: the whole point is
    that the holder is dead and cannot produce it. The safety is in the
    liveness proof, not in a token a dead process will never hand over."""
    chat._ensure_dir()
    with _claim_flocked(True) as lock:
        if lock.f is None:
            return False, _lock_unavailable()
        c = _sweep(_claims_read(True))
        row = c.get(resource)
        if not row:
            return False, "%s is not claimed" % resource
        holder = row.get("holder", "")
        claim_session = row.get("session")
        liveness, why = claim_holder_liveness(holder, claim_session)
        if liveness != "stale":
            return False, ("%s holder %s is %s — %s; stale release refused"
                          % (resource, holder, liveness, why))
        del c[resource]
        pk.write_json(claims_path(), c)
    _unlink_delegation_activity(resource)
    return True, ("%s released (stale — holder %s: %s)"
                 % (resource, holder, why))
def _pub_res(r):
    """The ONE publish transform for a claims resource key — scrub + clip,
    exactly as `claims_list` emits it. Factored out so a surface that joins
    another dict against a published row (`own_leases` under `helm chat
    claims`) keys on the same string instead of re-deriving the transform and
    drifting from it."""
    return _clip(_scrub(str(r)).strip(), STATUS_BYTES)
def own_leases(seat=None):
    """{resource-as-STORED: lease token} for the LIVE rows `seat` holds —
    the holder's own token handed back. `seat` defaults to this process's
    ambient identity (`acting_seat`, THE identity law); rows held by anyone
    else are never included.

    This discloses NOTHING. Every seat on this box is the same uid and can
    already read the whole ledger out of `.claims.json` (mode 664 inside a
    0700 dir — the dir mode stops another user, not another seat). What it
    closes is the STRAND: no read surface handed a holder their own token, so
    a holder who lost it to compaction or a restart had `.claims.json` as the
    ONLY route — a live holder walked exactly that route — while the lane
    stayed locked and the branch undeletable for the rest of the TTL
    (measured: 11868s). The refusal burdened only the party it was meant to
    serve.

    Two things it deliberately does NOT do. It never returns another seat's
    token: the token's real job is proving a DELIBERATE holder, so publishing
    the whole column would make the fat-fingered `release <someone-else>`
    trivial again. And it matches `holder` EXACTLY — never case-folded —
    because `_binding_ok` compares the same way, so any token this prints is
    a token `release` will accept.

    A TRUE read: no lock, no write. `claims_list` owns the GC-on-read leg;
    a lease lookup must not churn the file. Values ride the same reader-side
    scrub/clip as every other published claim field."""
    # DEFERRED, and it is the LAST cycle in this file: claims needs one
    # name from the CLI (_env_session, a one-line session-id read) while
    # the CLI needs seven from claims. One call site against seven — the
    # thinner leg pays, measured rather than guessed. Imported through
    # the facade so it keeps resolving after the CLI is extracted too.
    from .seats import _env_session
    seat = seat or acting_seat(_env_session(), safe_cwd())
    out = {}
    for r, v in _sweep(pk.read_json(claims_path(), {}) or {}).items():
        if r == "_fence" or not isinstance(v, dict) or not v.get("lease"):
            continue
        if v.get("holder") == seat:
            out[str(r)] = _clip(_scrub(str(v["lease"])).strip(), 64)
    return out
def claims_list():
    """The public table: holder/fence/remaining only — neither the lease
    token nor the bound session is ever published here. Holders read their
    OWN token through `own_leases`, which the CLI surfaces join in; this
    row shape stays token-free because the web ledger serves it too.
    A poll is a TRUE read: no lock, no write — write_json is atomic
    (tmp + os.replace) so a lockless read never sees a torn file. Only
    when a row actually expired does the GC leg take the flock, re-read,
    and persist the sweep — a watched roster (web polls every 3s) must
    never churn .claims.json or contend with real claim/release traffic.

    Reader-side law (same as status_line): resource + holder leave here
    scrubbed (Cc/Cf incl. bidi, Zl/Zp) + clipped — this is the ONE publish
    boundary every claim surface reads (the seats footer, `helm chat
    claims`, the web ledger), so a hostile claim("evil\\x1b[2J…") cannot
    clear/retitle the operator's terminal through any of them. The stored
    file keeps the raw key: release/extend match on the dict itself, never
    on this table."""
    raw = pk.read_json(claims_path(), {}) or {}
    c = _sweep(raw)
    if len(c) != len(raw):  # sweep only ever drops rows
        with _flocked(claims_path() + ".lock"):
            raw = pk.read_json(claims_path(), {}) or {}
            c = _sweep(raw)
            if len(c) != len(raw):
                pk.write_json(claims_path(), c)
    now = _now_mono()
    rows = [(r, v) for r, v in sorted(c.items()) if r != "_fence"]
    # ONE SNAPSHOT FOR THE WHOLE TABLE. Per-row probing walked /proc once per
    # claim AND let two rows in one render disagree about the same session.
    snap = liveness_snapshot({v.get("session") for _r, v in rows})
    out = []
    for r, v in rows:
        state, _why = claim_holder_liveness(v.get("holder", ""),
                                            v.get("session"), snap=snap)
        # THE THREE-STATE IS THE CANONICAL FIELD; `stale` stays only as a
        # derived compatibility flag. Emitting the bool ALONE is what collapsed
        # UNKNOWN into stale=false, so a claim nobody could classify rendered
        # exactly like a healthy one on every surface — the same invariant
        # violation as the four sites before it, at the publish boundary
        # (meld matrix case 1).
        out.append({"resource": _pub_res(r),
                    "holder": _clip(_scrub(str(v.get("holder") or "")).strip(),
                                    40) or None,
                    "fence": v.get("fence"),
                    "remaining": int(v.get("exp_mono", now) - now),
                    "liveness": state,
                    "stale": state == "stale"})
    return out
