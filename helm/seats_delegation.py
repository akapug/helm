#!/usr/bin/env python3
"""helm seats — delegation proof: evidence that a held lease is being WORKED.

THE STOP LADDER NEEDS A POSITIVE ANSWER, NOT AN ABSENCE. "No subagent has
reported" and "a subagent is building right now" look identical from outside,
and treating them alike is what makes a stop guard either useless (it excuses
every idle seat) or intolerable (it blocks every delegating one). Everything
here exists to turn that into evidence: activity records written by the
delegate itself, a PostToolUse leg for the in-process case, an enclosing-
holder walk for the ancestry case, and pid liveness read through the same
/proc primitives the rest of the fleet uses.

RECORDS ARE KEYED AND VALIDATED, not merely present. A file that says work is
happening is worth nothing if any process could have written it and no reader
checks who or when — so the key carries the agent identity and the validator
checks the window. An unvalidated activity record is a way for a stale file to
excuse an idle seat forever, which is worse than having no record at all.

The four deferred imports at the bottom reach into the CLI, and that is the
thin leg of the delegation<->cli cycle — five call sites, measured. The CLI
holds the hook legs that write these records; this module holds the reading
and the judging. They will always know about each other, so the only question
was which direction pays, and the smaller side pays.
"""

import hashlib
import json
import os
import shlex
import time

from . import chat, home, pk, vcs
from .seats_common import (STATUS_BYTES, _clip, _flocked, _get_pid_starttime,
                           _now_mono, _proc_stat_link, _scrub, _sweep,
                           claims_path, own_name)
from .seats_identity import acting_seat, safe_cwd
# The predicted delegation<->cli cycle did NOT materialise: every name this
# module reaches for resolved in the already-extracted floor or in the stop
# signals below it. The five edges the section-level graph attributed to the
# CLI were to names that had since moved — which is why the graph gets
# re-measured after every extraction rather than planned once.
from .seats_stop_signals import _off, _stop_fp_path

def _lease_worktree(resource, cwd=None):
    """The lane room a `worktree:<project>:<lane>` lease names, or None.

    Resolved through work._lanes' pure functions (lane_path IS the registry
    key — helm work's own law) from the supplied observation cwd, or this
    process's cwd for Stop-time reads. Both producer and consumer therefore
    reduce through the same primitives `helm work claim` used. None is
    UNKNOWN, and every caller blocks on UNKNOWN: a resource that is not
    lane-shaped, a cwd that anchors to no repo, or a project basename that
    does not match all stay guarded."""
    parts = str(resource).split(":", 2)
    if len(parts) != 3 or parts[0] != "worktree" or not parts[1] or not parts[2]:
        return None
    try:
        from .work import _lanes
        root = _lanes.find_root(cwd or safe_cwd())
    except Exception:
        return None
    if not root or os.path.basename(root.rstrip(os.sep)) != parts[1]:
        return None
    return _lanes.lane_path(root, parts[2])


def lease_foreign_project(resource, cwd=None):
    """The repository a lane lease NAMES when this process stands in another.

    `_lease_worktree` answers None for four different worlds — not
    lane-shaped, no repository under the cwd, a project that does not match,
    and an unreadable registry — and a reader downstream renders all four as
    "this lease resolves to no lane room". THREE of those are about the
    lease; the third is about WHERE THE READER IS STANDING, and it is the
    only one that is not a defect in anything.

    THE DIFFERENCE IS NOT COSMETIC. A cross-repository lease cannot be proved
    idle from here at ANY future stop, so the sentence repeats for the whole
    TTL and a by-hand confirmation has nowhere to be recorded. "No lane room"
    invites the holder to look for a missing directory; the room is fine and
    in another checkout. This does not widen what can be READ — the reads
    stay cwd-scoped — it only lets the sentence name its own scope limit.

    Returns the PROJECT NAME from the lease key, never a path: the path is
    exactly what this process cannot resolve, and guessing one from the
    current root's siblings would be identity inferred from path shape."""
    parts = str(resource).split(":", 2)
    if len(parts) != 3 or parts[0] != "worktree" or not parts[1] or not parts[2]:
        return None
    try:
        from .work import _lanes
        root = _lanes.find_root(cwd or safe_cwd())
    except Exception:
        return None
    if not root:
        return None
    return parts[1] if os.path.basename(root.rstrip(os.sep)) != parts[1] else None
DELEGATION_WINDOW_S = 300  # 5-minute interval progress window for delegation proof
# The two delegation-file namespaces, named once. The solo-load whisper rung
# reads this directory by PREFIX to answer "did this session delegate at all",
# and a literal restated at each site is precisely how a reader silently stops
# matching its producer — the failure would be a rung that never fires again.
def v2_records(raw):
    """Records out of a v2 envelope, else {} — the ONE door for a marker file.

    A marker file on disk can be absent, empty, truthy-and-NOT-a-dict (a JSON
    list or bare scalar from a torn write), the wrong version, or fine, and
    `raw.get("v")` RAISES on the third of those rather than reading as
    invalid. Every reader of these envelopes goes through here so the check
    is written once: fixing the sites one at a time is what produced four
    review rounds on this file.
    """
    if not isinstance(raw, dict) or raw.get("v") != 2 \
            or not isinstance(raw.get("records"), dict):
        return {}
    return raw["records"]


def json_dict(raw):
    """A read_json result that a caller intends to .get() — {} unless dict."""
    return raw if isinstance(raw, dict) else {}


_ACTIVITY_PREFIX = ".deleg_activity."
_STOPPED_PREFIX = ".deleg_stopped."
def _delegation_activity_path(session, resource, room=None):
    """Activity path names resource + canonical room + session namespaces.

    Resource keys contain only the project basename, so two clones can share
    `worktree:proj:lane-x`. The room hash prevents one clone's producer from
    overwriting or exempting the other's proof; exact identities remain inside
    the v2 document and authorize every read."""
    r_hash = hashlib.sha256(str(resource).encode("utf-8")).hexdigest()[:16]
    room = room or _lease_worktree(resource)
    room_id = os.path.realpath(room) if room else ""
    w_hash = hashlib.sha256(room_id.encode("utf-8")).hexdigest()[:16]
    s_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()
    return chat.state_path("deleg", _ACTIVITY_PREFIX + "%s.%s.%s"
                           % (r_hash, w_hash, s_hash))
def _delegation_stop_path(session, agent):
    s_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()
    a_hash = hashlib.sha256(str(agent).encode("utf-8")).hexdigest()
    return chat.state_path("deleg",
                           _STOPPED_PREFIX + "%s.%s" % (s_hash, a_hash))
def _mark_agent_stopped(session, agent):
    """Durable-for-this-boot lifecycle tombstone, independent of lock state."""
    if not isinstance(session, str) or not session \
            or not isinstance(agent, str) or not agent.strip():
        return False
    try:
        chat._ensure_dir()
        pk.atomic_write(_delegation_stop_path(session, agent), json.dumps({
            "session": session, "agent": agent,
        }, sort_keys=True))
        return True
    except OSError:
        return False
def _delegation_stop_read_path(session, agent):
    """READ location, new-then-legacy. The WRITE path is the family subdir;
    a point reader that looked only there would miss every tombstone an
    un-relaunched writer already left flat, and answer NOT-STOPPED for an
    agent that has stopped."""
    s_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()
    a_hash = hashlib.sha256(str(agent).encode("utf-8")).hexdigest()
    return chat.state_read_path("deleg",
                                _STOPPED_PREFIX + "%s.%s" % (s_hash, a_hash))


def _agent_stopped(session, agent):
    if not isinstance(agent, str) or not agent:
        return False
    data = json_dict(pk.read_json(_delegation_stop_read_path(session, agent),
                                  {}))
    return data.get("session") == str(session) and data.get("agent") == agent
def _get_claim_lease(resource):
    """Return stored lease token for resource from claims.json, else None."""
    try:
        c = pk.read_json(claims_path(), {}) or {}
        row = c.get(resource)
        if isinstance(row, dict):
            lease = row.get("lease")
            if isinstance(lease, str) and lease.strip() and lease.strip().lower() != "none":
                return lease.strip()
    except Exception:
        pass
    return None
def _enclosing_claude_holder(start_pid=None, proc_dir=None, max_hops=25):
    """Nearest same-uid `claude` ancestor as (pid, starttime), or None.

    `agent_id` in the hook payload proves the event is a subagent event; this
    ancestry walk does NOT. It supplies only the stable process incarnation
    that must still enclose a later Stop hook. The whole observed chain is
    re-read before acceptance so an exited/reused wrapper cannot splice two
    generations into one proof."""
    proc_dir = proc_dir or home.env("PROC") or "/proc"
    pid, seen, chain = start_pid or os.getpid(), set(), []
    holder = None
    for _ in range(max_hops):
        if not isinstance(pid, int) or pid <= 1 or pid in seen:
            break
        seen.add(pid)
        pdir = os.path.join(proc_dir, str(pid))
        try:
            if os.stat(pdir).st_uid != os.getuid():
                return None
            link = _proc_stat_link(pid, proc_dir=proc_dir)
            if not link:
                return None
            start, ppid = link
            with open(os.path.join(pdir, "comm"), "rb") as f:
                comm = f.read(64).strip()
        except OSError:
            return None
        chain.append((pid, start, ppid))
        if comm == b"claude":
            holder = (pid, start)
            break
        pid = ppid
    if not holder:
        return None
    for pid, start, ppid in chain:
        if _proc_stat_link(pid, proc_dir=proc_dir) != (start, ppid):
            return None
    return holder
# The one spelling of "this record came from a DOCUMENTED SUBAGENT", named so
# the whisper table's solo-load rung and this producer cannot drift apart: the
# rung's whole correctness rests on telling an `agent:` record from a
# `live_scan` one, and a literal in each place is how that goes silently wrong.
_AGENT_KEY_PREFIX = "agent:"
def _activity_record_key(source, agent=None):
    if source == "live_scan":
        return source
    if source == "post_tool_use" and isinstance(agent, str) and agent.strip():
        return _AGENT_KEY_PREFIX + hashlib.sha256(
            agent.strip().encode("utf-8")).hexdigest()
    return None
def _activity_record_valid(entry, key, resource, session, row, room):
    """One v2 source entry exactly bound to this claim and observed room."""
    if not isinstance(entry, dict):
        return False
    source = entry.get("source")
    if _activity_record_key(source, entry.get("agent")) != key:
        return False
    if entry.get("resource") != resource or entry.get("session") != str(session):
        return False
    if entry.get("seat") != row.get("holder") or entry.get("lease") != row.get("lease"):
        return False
    if type(entry.get("pid")) is not int or type(entry.get("starttime")) is not int:
        return False
    if type(entry.get("mono")) not in (int, float):
        return False
    agent = entry.get("agent")
    if source == "post_tool_use":
        if not isinstance(agent, str) or not agent.strip() \
                or _agent_stopped(session, agent):
            return False
    elif source == "live_scan":
        if agent is not None:
            return False
    else:
        return False
    cwd = entry.get("cwd")
    return bool(isinstance(cwd, str) and room
                and os.path.realpath(cwd) == os.path.realpath(room))
def _record_delegation_activity(session, resource, pid, proc_dir="/proc",
                                source="live_scan", seat=None, agent=None,
                                agent_type=None, cwd=None, starttime=None):
    """Claim-linearized positive activity from one verified producer source."""
    if source not in ("live_scan", "post_tool_use"):
        return False
    if source == "post_tool_use" and (not isinstance(agent, str)
                                      or not agent.strip()
                                      or _agent_stopped(str(session), agent)):
        return False
    if source == "live_scan" and agent is not None:
        return False
    room = _lease_worktree(resource, cwd=cwd)
    if not room or (cwd and os.path.realpath(cwd) != os.path.realpath(room)):
        return False
    st = _get_pid_starttime(pid, proc_dir=proc_dir)
    if st is None or (starttime is not None and starttime != st):
        return False
    chat._ensure_dir()
    path = _delegation_activity_path(session, resource, room=room)
    try:
        with _flocked(claims_path() + ".lock", blocking=False) as lock:
            if lock.f is None:
                return False
            c = _sweep(pk.read_json(claims_path(), {}) or {})
            row = c.get(resource)
            if not isinstance(row, dict) or row.get("session") != str(session):
                return False
            holder, lease = row.get("holder"), row.get("lease")
            if not isinstance(holder, str) or not holder or seat and seat != holder:
                return False
            if not isinstance(lease, str) or not lease.strip() \
                    or lease.strip().lower() == "none":
                return False
            if _get_pid_starttime(pid, proc_dir=proc_dir) != st:
                return False
            if source == "post_tool_use" and _agent_stopped(str(session), agent):
                return False
            raw = pk.read_json(path, {}) or {}
            records = {}
            if v2_records(raw):
                for name, old in v2_records(raw).items():
                    if _activity_record_valid(old, name, resource, session,
                                              row, room):
                        records[name] = old
            key = _activity_record_key(source, agent)
            if not key:
                return False
            records[key] = {
                "source": source,
                "resource": resource,
                "session": str(session),
                "seat": holder,
                "lease": lease.strip(),
                "pid": pid,
                "starttime": st,
                "mono": _now_mono(),
                "cwd": os.path.realpath(room),
                "agent": agent.strip() if isinstance(agent, str) else None,
                "agent_type": (agent_type if isinstance(agent_type, str)
                               and agent_type.strip() else None),
            }
            pk.atomic_write(path, json.dumps({"v": 2, "records": records},
                                             sort_keys=True))
            return True
    except OSError:
        return False
def _unlink_delegation_activity(resource, session=None):
    """Clean up delegation activity for one resource identity."""
    try:
        if session:
            path = _delegation_activity_path(session, resource)
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        else:
            r_hash = hashlib.sha256(str(resource).encode("utf-8")).hexdigest()[:16]
            prefix = _ACTIVITY_PREFIX + "%s." % r_hash
            # both roots: an un-relaunched writer still produces flat
            for d in chat.state_scan_dirs("deleg"):
                try:
                    names = os.listdir(d)
                except OSError:
                    continue
                for name in names:
                    if name.startswith(prefix):
                        try:
                            os.unlink(os.path.join(d, name))
                        except OSError:
                            pass
    except OSError:
        pass
def _get_delegation_activity(session, resource):
    """Valid v2 entries for this exact claim and Stop-time room, else None."""
    room = _lease_worktree(resource)
    path = _delegation_activity_path(session, resource, room=room)
    # MERGE, never pick: records can exist in BOTH roots at once while
    # relaunched and un-relaunched writers coexist, and dropping either half
    # loses live delegation proof. The write-back below lands on the NEW
    # path, so every read migrates its own file forward.
    legacy = os.path.join(chat.chat_dir(), os.path.basename(path))
    if not room or not (os.path.exists(path) or os.path.exists(legacy)):
        return None
    try:
        with _flocked(claims_path() + ".lock", blocking=False) as lock:
            if lock.f is None:
                return None
            c = _sweep(pk.read_json(claims_path(), {}) or {})
            row = c.get(resource)
            raw_new = pk.read_json(path, {}) or {}
            raw_old = pk.read_json(legacy, {}) or {}
            # VALIDATE EACH ROOT, THEN UNION — never let root precedence
            # decide. A plain new-wins update discards a VALID legacy record
            # whenever the new root holds an invalid one under the same key,
            # which is evidence loss dressed as a merge. Both sides are
            # filtered first, so a collision is only ever between two records
            # that already passed, and the forward location wins that.
            _envelope = v2_records          # the one door, not a local copy

            if not isinstance(row, dict):
                records = {}
            else:
                # PRESERVE THE CANDIDATE THAT CAN SURVIVE THE PROOF.
                # _activity_record_valid is STRUCTURAL only — the temporal
                # (mono window) and process (pid alive) proof runs at the
                # consumer. So ranking a collision by ROOT can hand the proof
                # a stale-or-dead record while discarding the live one under
                # the same key, and the rung then reads NOT-DELEGATED off a
                # seat that is delegating right now. On a collision keep the
                # FRESHER record — the same dimension the downstream proof
                # keys on — never the newer directory.
                # DO NOT PICK. The proof this feeds is TWO-dimensional —
                # mono inside the window AND the pid still alive — so ANY
                # single-axis tiebreak here discards a candidate the proof
                # would have kept: a NEWER record whose pid is dead or
                # recycled beats an OLDER one that is live, and max(mono)
                # chooses exactly wrong. Every structurally-valid candidate
                # from BOTH roots is carried to the consumer under its own
                # key; the consumer already ignores the key and evaluates
                # each record, so one downstream proof stays the only judge.
                records, collided = {}, False
                for tag, src in (("old", _envelope(raw_old)),
                                 ("new", _envelope(raw_new))):
                    for n, e in src.items():
                        if not _activity_record_valid(e, n, resource, session,
                                                      row, room):
                            continue
                        if n in records and records[n] != e:
                            collided = True
                            records["%s\x00%s" % (n, tag)] = e
                        else:
                            records[n] = e
            if records:
                # compare against the NEW file, not the merged set: an
                # old-only read has nothing to diff against itself and would
                # skip the migration this promised.
                # MIGRATE ONLY WHEN IT IS LOSSLESS. The on-disk envelope is
                # one record per key and cannot hold two, so a genuine
                # collision has no faithful representation — writing one back
                # would persist exactly the arbitrary pick this stopped
                # making. Leave BOTH roots in place instead; nothing is lost
                # and the next read carries both again.
                # MIGRATION IS OPPORTUNISTIC AND MUST NEVER COST THE PROOF.
                # This write sits inside the function's outer `except OSError:
                # return None`, so a disk hiccup here discarded records that
                # were already read, already validated and already the answer
                # — a failed convenience erasing a measured fact. It gets its
                # own handler: if the migration cannot be written, the caller
                # still gets what we proved, and the next read tries again.
                canonical = {"v": 2, "records": records}
                if not collided and canonical != raw_new:
                    try:
                        pk.atomic_write(path, json.dumps(canonical,
                                                         sort_keys=True))
                    except OSError:
                        pass
                return records
            try:
                os.unlink(path)
            except OSError:
                pass
    except OSError:
        return None
    return None
def _is_pid_alive(pid, starttime=None, require_starttime=False, proc_dir="/proc"):
    if not pid or not isinstance(pid, int):
        return False
    pdir = os.path.join(proc_dir, str(pid))
    try:
        if not (os.path.isdir(pdir) and os.stat(pdir).st_uid == os.getuid()):
            return False
        link = _proc_stat_link(pid, proc_dir=proc_dir, with_state=True)
        if not link:
            return False
        cur_st, _ppid, state = link
        if state in ("Z", "X"):
            return False
        if require_starttime or starttime is not None:
            if not isinstance(starttime, int) or cur_st != starttime:
                return False
        return True
    except OSError:
        return False
def _record_posttool_delegation(payload, proc_dir=None):
    """Record one documented subagent PostToolUse event, fail-closed to False.

    Claude Code supplies `agent_id` only inside a subagent hook. The inherited
    session/environment cannot distinguish sidechains and is deliberately not
    consulted. Payload cwd must be the exact canonical lane root whose current
    claim binds this process's seat and session."""
    if _off("STOP_GUARD_DELEGATION") or not isinstance(payload, dict):
        return False
    agent = payload.get("agent_id")
    session = payload.get("session_id")
    cwd = payload.get("cwd")
    if payload.get("hook_event_name") != "PostToolUse" \
            or not isinstance(agent, str) or not agent.strip() \
            or not isinstance(session, str) or not session \
            or not isinstance(cwd, str) or not cwd:
        return False
    try:
        from . import work
        root = work.find_root(cwd)
        lane = work._infer_lane(root, cwd) if root else None
        if not root or not lane:
            return False
        resource = work.resource(root, lane)
        room = work.lane_path(root, lane)
        if os.path.realpath(cwd) != os.path.realpath(room):
            return False
        holder = _enclosing_claude_holder(proc_dir=proc_dir)
        if not holder:
            return False
        # THE CLAIM BINDING ABOVE IS THE AUTHORITY, so this line only labels.
        # The check three lines up already required the payload's cwd to be the
        # canonical lane root whose CURRENT claim binds this process's seat and
        # session — a stronger statement than any name test, and it is what
        # makes the record evidence.
        #
        # ROUTING THIS THROUGH THE IDENTITY LAYER WAS TRIED AND REVERTED: a
        # legitimate SESSION REBIND is indistinguishable from a claim-jump to
        # `identity_disagreement` (the old sid still holds the name, the new
        # one is rostered nowhere), so `rebind_claim_sessions` followed by a
        # PostToolUse silently stopped recording — the guard DELETED the
        # evidence instead of misattributing it. tests/test_seats
        # StopGuardDelegationTest.test_rebind_and_rollback_purge_posttool_activity
        # is the pin.
        seat = acting_seat(session, cwd)
        return _record_delegation_activity(
            session, resource, holder[0],
            proc_dir=proc_dir or home.env("PROC") or "/proc",
            source="post_tool_use", seat=seat, agent=agent,
            agent_type=payload.get("agent_type"), cwd=cwd,
            starttime=holder[1])
    except Exception:
        return False
def _clear_posttool_delegation(payload):
    """Tombstone one completed subagent independently of claim contention.

    A reader rejects the tombstone before accepting evidence and then prunes it
    under the claims lock. SubagentStop deliberately performs no lockless
    activity-file rewrite: that would race another agent's producer and could
    erase still-live evidence."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SubagentStop":
        return False
    agent = payload.get("agent_id")
    session = payload.get("session_id")
    if not isinstance(agent, str) or not agent.strip() \
            or not isinstance(session, str) or not session:
        return False
    return _mark_agent_stopped(session, agent)
class ProcScan:
    """ONE read of the process table, shared by every lane lease in one stop.

    `_delegated_build`'s immediate proof asks the table one question -- which
    same-uid processes, excluding this one's own line, have their cwd at path
    X -- and only X changes from lane to lane. Asked once per claim that is
    O(leases x processes); once per stop it is O(processes), same answers.
    MEASURED here: 0.0198s per walk over 1150 pids, so the 18 lane leases this
    seat holds cost 0.356s of table-scanning per stop before a single git read,
    inside a rung whose reserve is a constant that knows nothing about the
    lease count.

    UNREADABLE AND EMPTY MAY NOT SHARE A VALUE, which is why this is a typed
    object and not a bare dict. An unlistable table is UNKNOWN and blocks every
    lane that consults it; an empty one is a measured "nobody is in any room".
    Collapsing them turns this scan's one failure into a fleet-wide
    un-guarding, the exact direction `_delegated_build`'s law forbids.

    A SNAPSHOT IS ONE INSTANT, AND THAT CUTS ONE WAY ONLY. A process that
    STARTS after the scan is missed, which withholds an exemption and BLOCKS --
    the safe direction. A process that EXITS is not laundered into proof
    either: `candidates` re-reads the cwd link live, so a dead or moved pid is
    dropped at the moment of use rather than trusted from the snapshot. Per
    claim the world was sampled N times and lane 1 could disagree with lane 18
    about the same host; one scan makes them consistent."""

    __slots__ = ("ok", "proc_dir", "uid", "mine", "_by_cwd")

    def __init__(self, ok, proc_dir, uid=None, mine=(), by_cwd=None):
        self.ok = ok
        self.proc_dir = proc_dir
        self.uid = uid
        self.mine = frozenset(mine)
        self._by_cwd = by_cwd or {}

    def candidates(self, want):
        """Live pids whose cwd IS `want`, in process-table order.

        RE-READ AT THE MOMENT OF USE. The list is zero or one for almost every
        lane, so re-resolving buys back the exactness a snapshot would cost for
        nothing. Order is preserved because the caller returns the FIRST pid
        whose ancestry carries the session."""
        live = []
        for pid in self._by_cwd.get(want, ()):
            try:
                cwd = os.path.realpath(
                    os.path.join(self.proc_dir, str(pid), "cwd")).rstrip(os.sep)
            except OSError:
                continue          # exited since the scan: not proof
            if cwd == want:
                live.append(pid)
        return live


def proc_scan(proc_dir=None):
    """Take the shared table read. One per stop, not one per claim.

    A caller that threads no scan gets identical answers and pays for its own
    walk, which is what every non-Stop consumer should keep doing."""
    proc_dir = proc_dir or home.env("PROC") or "/proc"

    def ppid_of(pid):
        link = _proc_stat_link(pid, proc_dir=proc_dir)
        return link[1] if link else None

    me = os.getuid()
    mine, cur = {os.getpid()}, os.getpid()
    for _ in range(25):           # self + own ancestry: the session's OWN
        cur = ppid_of(cur)        # line (harness, this very hook) is never a
        if not cur or cur <= 1:   # delegate — without this a session that
            break                 # cd'd ITSELF into the room self-exempts
        mine.add(cur)
    try:
        pids = [int(n) for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError:
        # unlistable table: UNKNOWN. Every lane that reads this scan blocks.
        return ProcScan(False, proc_dir)
    by_cwd = {}
    for pid in pids:
        pdir = os.path.join(proc_dir, str(pid))
        try:
            if pid in mine or os.stat(pdir).st_uid != me:
                continue
            cwd = os.path.realpath(os.path.join(pdir, "cwd")).rstrip(os.sep)
        except OSError:
            continue              # exited between listdir and stat: not proof
        by_cwd.setdefault(cwd, []).append(pid)
    return ProcScan(True, proc_dir, uid=me, mine=mine, by_cwd=by_cwd)


def _delegated_build(resource, session, proc_dir=None,
                     window_s=DELEGATION_WINDOW_S, scan=None):
    """(proof_info, room) positively proving delegated work, else None.

    `scan` is one `proc_scan()` shared across the lane leases of one stop. Left
    out, this function takes its own and behaves exactly as it always did --
    the threading is a COST change, and the arms that matter assert the two
    spellings agree lane for lane.

    Immediate proof remains a live same-uid process — never this process or its
    ancestry — whose cwd is the exact claimed room and whose tree carries this
    session. Interval proof is source-specific: a sampled child keeps its own
    PID/starttime requirement; a documented subagent PostToolUse keeps the exact
    claim generation and enclosing Claude holder incarnation, independently per
    agent until SubagentStop removes it.

    UNKNOWN → BLOCK is load-bearing. Unlistable process state, unreadable or
    foreign-clone evidence, holder/child death or reuse, expired activity, and
    lease/session drift all return None. The failure mode is an exemption that
    un-guards idle stops fleet-wide, so no declaration or mtime substitutes for
    positive evidence."""
    room = _lease_worktree(resource)
    if not room or not session:
        return None
    want = os.path.realpath(room).rstrip(os.sep)
    proc_dir = proc_dir or home.env("PROC") or "/proc"
    needle = str(session).encode("utf-8")

    def marked(pid):
        # the census idiom: a live process whose cmdline/environ references
        # the session id IS the session's tree (a Bash-tool child carries
        # CLAUDE_CODE_SESSION_ID; a resumed harness carries --resume <sid>).
        # Unreadable leaves read as unmarked — never as proof.
        blob = b""
        for leaf in ("cmdline", "environ"):
            try:
                with open(os.path.join(proc_dir, str(pid), leaf), "rb") as f:
                    blob += f.read(1 << 20)
            except OSError:
                continue
        return needle in blob

    def ppid_of(pid):
        link = _proc_stat_link(pid, proc_dir=proc_dir)
        return link[1] if link else None

    # A SCAN BUILT AGAINST ANOTHER TABLE ANSWERS ANOTHER QUESTION. `proc_dir`
    # is redirected by HELM_PROC and by fixtures, so a threaded scan whose root
    # is not the one resolved here is not a cheaper answer, it is a DIFFERENT
    # one. Rebuilding is the only safe reconciliation: ignoring the mismatch
    # would read the wrong table, and refusing would turn a caller's
    # bookkeeping slip into a fleet-wide block.
    if scan is None or scan.proc_dir != proc_dir:
        scan = proc_scan(proc_dir)
    if not scan.ok:
        return None               # unlistable table: UNKNOWN, blocks
    for pid in scan.candidates(want):
        # the poach-shield is the scan's own key: cwd IS the room, exactly
        hop, hops = pid, 0
        while hop and hop > 1 and hops < 25:
            if marked(hop):
                _record_delegation_activity(
                    session, resource, pid, proc_dir=proc_dir,
                    source="live_scan", cwd=room)
                return pid, room
            hop, hops = ppid_of(hop), hops + 1

    # Interval judgment: two independent producers share one claim-bound v2
    # envelope. A sampled child remains proof only while that child incarnation
    # lives; a PostToolUse event remains proof only under the same enclosing
    # Claude holder incarnation. Neither source can clobber the other.
    records = _get_delegation_activity(session, resource) or {}
    valid = []
    holder = None
    for _key, act in records.items():
        source = act.get("source")
        last_pid = act.get("pid")
        last_mono = act.get("mono")
        last_st = act.get("starttime")
        if type(last_pid) is not int or type(last_mono) not in (int, float):
            continue
        delta = _now_mono() - last_mono
        if not 0 <= delta <= window_s:
            continue
        if source == "live_scan":
            if _is_pid_alive(last_pid, starttime=last_st,
                             require_starttime=True, proc_dir=proc_dir):
                valid.append((last_mono,
                              "sampled child pid %d (recent activity %ds ago)"
                              % (last_pid, int(delta))))
        elif source == "post_tool_use":
            if holder is None:
                holder = _enclosing_claude_holder(proc_dir=proc_dir)
            if holder == (last_pid, last_st):
                valid.append((last_mono,
                              "subagent %s, holder pid %d "
                              "(recent subagent activity %ds ago)"
                              % (act.get("agent"), last_pid, int(delta))))
    if valid and os.path.exists(want):
        return max(valid, key=lambda item: item[0])[1], room
    return None
def _lane_stem(name):
    """ONE normalisation, owned by landreq (#156): this module's own copy
    stripped only -review/-rN/-xrev and kept the lane/ prefix, so the
    gate-pending exemption below family-matched DIFFERENTLY from the
    discharge admit — the same silent half-match, one module over. The
    stem one work family travels under is landreq._lane_stem's answer:
    family prefixes AND round/role suffixes stripped, case-folded."""
    from . import landreq
    return landreq._lane_stem(name)
def _gate_pending(resource, snap=None):
    """(dispatch_id, reviewer, stage) POSITIVELY proving lease `resource`
    backs a lane that is PARKED ON SOMEONE ELSE'S VERB, else None. Two
    stages, one TWO-PART proof — the dispatch row must correspond to THE
    LANE (its recorded lane is the lease's lane, stem-family matched) AND
    its --ref must match the claimed worktree's current HEAD (full or as
    its abbreviation). Ref alone never suffices: every fresh lane starts
    at some existing tip, so ANY open review row at that tip would
    otherwise exempt every lane freshly branched from it — measured live
    2026-07-31, lane approved-lane-exempts-stop branched at an unlanded
    stack tip was exempted by ANOTHER lane's review row (1267f509,
    verdict-polarity-consolidated) purely because the refs coincided.
    Lane name alone never suffices either (a NAME is not proof of a gate
    at THIS tip); the exemption needs BOTH. The row is a kind=review
    dispatch that is either

      * OPEN (stage "pending") — a reviewer holds the verdict, or
      * a VERDICT row with polarity=approve AND a bound gate token
        (stage "approved") — the review is DONE, the gate is proven, and
        the only remaining verb is the INTEGRATOR's land.

    The second arm exists because the first died exactly one lifecycle
    stage after it fired (bug class verb-designed-noun-lifecycle-holed,
    measured live 2026-07-31 on lane lr-land-ack-deletions-reachable: the
    moment the reviewer APPROVED — with a VERIFIED gate token — the row
    left the OPEN set, the exemption lapsed, and the holder's every stop
    exited 2 with "finish the work" while the work WAS finished and
    landing was never the holder's verb). A FIX or SUPERSEDE verdict does
    NOT exempt — the holder owes rework, so blocking is then the nudge
    doing its job — and an UNGATED approve authorizes nothing (settled
    law: mark_verdict refuses to write one, and historical replayed rows
    without a bound token stay blocking).

    Same verifiable-saturated-state law as _delegated_build, one lifecycle
    stage later (LEASE-HELD-WHILE-GATE-PENDING, measured live 2026-07-29:
    three seats force-continued all evening on gate-pending leases, and
    release-or-finish are both wrong — release opens the lane mid-gate,
    finishing is the reviewer's move). UNKNOWN → BLOCK at every rung: an
    unparseable lease, an unrev-parseable HEAD, an unreadable ledger, no
    matching row, a CANCELLED row, a fix/supersede/ungated verdict, a ref
    that does not match the CURRENT head (a moved head is new work, not a
    pending gate), a row recorded for a DIFFERENT lane family, and rows
    from ANY OTHER repo all return None and the claims block stands
    exactly as before. Recomputed on EVERY stop, never cached. Neither
    half of the proof is evidence alone: lane NAME without the ref↔HEAD
    match is a coincidence of naming, ref without the lane is a
    coincidence of branching.

    `snap` is the caller's ALREADY-READ ledger state, threaded rather than
    re-read: one stop asks this ledger twice (here, and again for the release
    advice's review read) and the read measured 190ms on a 1,577-event
    ledger. Omitted, it reads its own — the contract is unchanged."""
    room = _lease_worktree(resource)
    if not room:
        return None
    family = _lane_stem(str(resource).split(":", 2)[2])
    try:
        from . import dispatches, vcs
        head = vcs.backend(room).head_sha(room)
    except Exception:
        return None
    if not head:
        return None
    if snap is None:
        try:
            rows = dispatches.rows()
        except Exception:
            return None           # unreadable ledger: UNKNOWN, blocks
    else:
        rows = snap
    snap = rows if isinstance(rows, dict) else None
    if isinstance(rows, dict):    # snapshot() keys rows by dispatch id
        rows = rows.values()
    # THE OWED FRONTIER, resolved once and lazily. A superseded parent and the
    # successor carrying its work BOTH match this lane and head, so scanning
    # raw rows saw "two live gates for one head", returned ambiguous, and
    # withheld a stop exemption that was validly earned. Only the PENDING arm
    # is filtered: a closed row still feeds the approved arm, which is what
    # actually grants the exemption.
    #
    # FAILURE KEEPS THE OLD ANSWER. If the frontier cannot be computed, every
    # row stays eligible — a stop-guard that dropped rows on an internal error
    # would permit stops it should block, and blocking is the safe direction.
    _owed_ids = None

    def _is_owed(row):
        nonlocal _owed_ids
        if snap is None:
            return True
        if _owed_ids is None:
            try:
                _owed_ids = {r.get("id") for r in dispatches.owed(snap)}
            except Exception:
                _owed_ids = None
                return True
        return row.get("id") in _owed_ids

    found = stage = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") != "review":
            continue
        # LANE CORRESPONDENCE, half of the two-part proof: the row must be
        # THIS lane's (stem-family match — round/role renames are one
        # family), or a sibling lane freshly branched at the same tip
        # inherits a gate it was never in
        if _lane_stem(row.get("lane")) != family:
            continue
        status = row.get("status")
        if status in dispatches.CLOSED_STATES:
            # the APPROVED arm: only a polarity=approve verdict carrying a
            # BOUND gate token survives closure — cancelled rows, fix and
            # supersede verdicts (rework owed), and ungated approves (they
            # authorize nothing) all fall through and keep blocking
            if not (status == "verdict"
                    and row.get("polarity") == "approve"
                    and dispatches._GATE_ID.fullmatch(
                        str(row.get("gate") or ""))):
                continue
            row_stage = "approved"
        else:
            if not _is_owed(row):
                continue          # a successor already carries this obligation
            row_stage = "pending"
        ref = str(row.get("ref") or "")
        # the ledger stores the ref AS TYPED at dispatch time — full or
        # abbreviated; a match is exact-or-prefix in EITHER direction of
        # the head string, never a lane-name coincidence
        if ref and (head == ref or head.startswith(ref)
                    or ref.startswith(head)):
            if found is not None:
                return None       # two live gates for one head: ambiguous
            found, stage = row, row_stage
    if not found:
        return None
    return (str(found.get("id") or ""), str(found.get("recipient") or ""),
            stage)
def release_hint(resource, row):
    """The EXACT, copy-pasteable command that discharges ONE held lease, or
    None when this row carries no token to discharge it with.

    LEASES GUARD COLLISIONS, THEY NEVER OWN PROBLEMS (the owner's rule). The
    stop-guard used to print `helm chat release <resource> --lease <id>` with
    `<id>` a LITERAL PLACEHOLDER, which made it an instruction its own
    addressee could not follow: a lease minted by the ACTUATOR's auto-claim
    (_stop_whisper's mint-on-win) is never handed to the seat, so there was no
    id to substitute. The affected seat reported it live against
    dispatch:6c351ce6 — "the lease was minted by the actuator's auto-claim, so
    I never held the printed lease id". (The report called that id a release
    CAPABILITY; it is not one, and `_binding_ok` says why — but the seat was
    right that it could not act.) A lease that demands something its holder
    is STRUCTURALLY INCAPABLE of doing has
    stopped being a collision guard and become a problem the holder owns. An
    instruction that cannot be followed is worse than silence: it teaches
    people to ignore the channel it arrived on.

    So the guard prints what it already has in hand. Three things the caller
    cannot otherwise be sure of, every one read off the row the caller has
    ALREADY matched on `session == this session`:

      * THE LEASE TOKEN. No new disclosure: `own_leases` documents that the
        token is a DELIBERATE-HOLDER PROOF and not a secret — every seat on
        this box runs as the same uid and can read the whole ledger out of
        `.claims.json` — and this is strictly NARROWER than `own_leases`,
        being session-bound rather than merely holder-bound and covering only
        rows the block already names by resource and TTL.

      * `--seat <holder>`, but ONLY when this process's declared name is not
        the holder. `helm chat claims` hands a token back by ACTING_SEAT
        (own_name FIRST) while this guard resolves its seat ROSTER first
        (`seat_for_session`). A session the roster remembers under another
        name therefore mints under the ROSTER name and then reads an EMPTY
        lease column — MEASURED 2026-07-31, and it is the exact strand
        `own_leases` was built to close, reopening through a different door.
        Naming the holder closes it without touching `_binding_ok`: the
        holder is ALREADY public in `claims_list`, so this reveals nothing a
        `helm chat claims` did not.

      * THE RIGHT VERB. A `worktree:<project>:<lane>` lease belongs to `helm
        work release <lane>`, which also unlocks the git worktree, retires a
        landed room and triages an unlanded branch. `helm chat release` would
        drop the lease and leave the room git-LOCKED and the branch
        untriaged — a second unfollowable instruction hiding inside the
        first, and the more dangerous kind, because this one IS followable
        and quietly does the wrong thing.

    NOTHING HERE WEAKENS THE COLLISION GUARD. `_binding_ok`, `claim` and
    `release` are untouched: release still demands {lease token, holding seat,
    session-if-recorded} VALIDATED TOGETHER, a second claimant is still
    refused with the holder and the remaining TTL, and a seat that does not
    hold the lease is refused exactly as before. This function only ever
    describes a row; it never authorizes one."""
    lease = _clip(_scrub(str(row.get("lease") or "")).strip(), 64)
    if not lease:
        return None        # nothing to hand over — the caller says so plainly
    # every interpolated value is shell-quoted AT THIS AUTHORITY: the hint is
    # copy-pasteable by contract, and scrub+clip never made a metacharacter
    # lane name paste-safe — shlex.quote stays bare exactly when safe
    parts = str(resource).split(":", 2)
    if len(parts) == 3 and parts[0] == "worktree" and parts[1] and parts[2]:
        cmd = "helm work release " + shlex.quote(
            _clip(_scrub(parts[2]).strip(), 64))
    else:
        cmd = "helm chat release " + shlex.quote(
            _clip(_scrub(str(resource)).strip(), STATUS_BYTES))
    cmd += " --lease " + shlex.quote(lease)
    holder = _clip(_scrub(str(row.get("holder") or "")).strip(), 64)
    if holder and holder != (own_name() or ""):
        cmd += " --seat " + shlex.quote(holder)
    return cmd
def _claim_evidence_warning(transcript, room, seat, session):
    """One latched advisory from one claim-evidence transcript snapshot.

    TOTAL failure isolation lives around the whole rung — parse, message
    identity, fingerprint and latch I/O — because an advisory failure must not
    discard independently established inbox or lease blocks."""
    if not transcript or _off("STOP_GUARD_CLAIME"):
        return None
    try:
        from . import claimev
        lines, readable, mid = claimev.assessment(transcript)
        if not readable:
            lines = ["CLAIM-EVIDENCE SKIPPED — the transcript is unreadable; "
                     "no outgoing claim was reported clean."]
            try:
                mid = "unreadable:%d" % os.path.getsize(transcript)
            except OSError:
                mid = "unreadable"
        if not lines:
            return None
        fp = hashlib.blake2b(
            (mid + "\n" + "\n".join(lines)).encode("utf-8"),
            digest_size=8).hexdigest()
        fpp = _stop_fp_path(room, seat, session, kind="stopclaime")
        from .seats_cursor import seat_state_lock
        with seat_state_lock(seat, session=session) as current:
            if not current:
                return None
            try:
                with open(fpp) as f:
                    last = f.read().strip()
            except OSError:
                last = None
            if last == fp:
                return None
            try:
                chat._ensure_dir()
                pk.atomic_write(fpp, fp)
            except OSError:
                pass      # a failed WARN latch may repeat; it never blocks
        return "[helm stop-guard] " + "\n  ".join(lines)
    except Exception:
        return None
