#!/usr/bin/env python3
"""Checked Seat identity and reachability from one replayable action snapshot.

This is the owner-layer answer for destructive liveness consumers.  It returns
exactly LIVE, QUIET, ABSENT, or UNKNOWN.  ABSENT is authority-bearing: only a
strictly replayed, finally-retired Seat whose canonical authority proved its
work, delivery, lease, and runtime boundaries clear can produce it.  A roster
miss, an old presence beat, an empty process census, or an exit archive cannot.

The current administrative-retirement ledger cannot clear a row merely because
its parties disappeared from the roster.  Every actionable party must resolve
through this authority and return ABSENT from the same snapshot; one UNKNOWN
refuses the action.
"""
import copy
import dataclasses
import math
import time
from collections.abc import Mapping
from types import MappingProxyType


LIVE = "LIVE"
QUIET = "QUIET"
ABSENT = "ABSENT"
UNKNOWN = "UNKNOWN"
SNAPSHOT_VERSION = 1
AUTHORITY_CAPABILITY = "helm.actors-authority/checked-state-v1"


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item)
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_thaw(item) for item in value), key=repr)
    return copy.deepcopy(value)


@dataclasses.dataclass(frozen=True)
class Party(object):
    """One independently checked action role and its exact Seat token."""

    role: str
    token: str


@dataclasses.dataclass(frozen=True)
class ReachabilityResult(object):
    """The checked result for one party; errors never share an absence value."""

    role: str
    requested: str
    actionable: str
    state: str
    actor_id: object
    seat_id: object
    chain: tuple
    evidence: tuple
    errors: tuple

    def as_dict(self):
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ActionReachability(object):
    """Every party result from one snapshot, without short-circuiting."""

    snapshot_version: int
    captured_at: float
    results: tuple

    @property
    def authorized_absent(self):
        return bool(self.results) and all(r.state == ABSENT for r in self.results)

    @property
    def refusal(self):
        if self.authorized_absent:
            return None
        failed = ["%s=%s" % (r.role, r.state) for r in self.results
                  if r.state != ABSENT]
        return ("the current ledger cannot clear unless canonical Seat authority "
                "proves every actionable party ABSENT from this action snapshot; "
                + ", ".join(failed or ["no parties were supplied"]))

    def as_dict(self):
        return {"snapshot_version": self.snapshot_version,
                "captured_at": self.captured_at,
                "authorized_absent": self.authorized_absent,
                "refusal": self.refusal,
                "results": [row.as_dict() for row in self.results]}


@dataclasses.dataclass(frozen=True)
class ReachabilitySnapshot(object):
    """Captured evidence.  No classifier performs I/O after this is minted."""

    version: int
    captured_at: float
    parties: tuple
    authority: Mapping
    roster_rows: Mapping
    presence: tuple
    agents: object
    successors: tuple
    successors_complete: bool
    errors: tuple

    def as_dict(self):
        return {"version": self.version, "captured_at": self.captured_at,
                "parties": [dataclasses.asdict(p) for p in self.parties],
                "authority": _thaw(self.authority),
                "roster": _thaw(self.roster_rows),
                "presence": [_thaw(row) for row in self.presence],
                "agents": _thaw(self.agents),
                "successors": [tuple(row) for row in self.successors],
                "successors_complete": self.successors_complete,
                "errors": [tuple(row) for row in self.errors]}


def _party(value):
    if isinstance(value, Party):
        role, token = value.role, value.token
    elif isinstance(value, Mapping):
        if set(value) != {"role", "token"}:
            raise ValueError("party fields are incomplete or unknown")
        role, token = value["role"], value["token"]
    else:
        role, token = value
    if not isinstance(role, str) or not role.strip():
        raise ValueError("party role is not a nonempty string")
    if not isinstance(token, str) or not token.strip() or token != token.strip():
        raise ValueError("party token is not one exact nonempty string")
    return Party(role, token)


def _successor_rows(value):
    """Sorted exact-token edges; malformed emptiness is never a clear frontier."""
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise TypeError("successor frontier is not a mapping or edge sequence")
    rows = []
    for row in items:
        try:
            source, targets = row
        except (TypeError, ValueError):
            raise ValueError("successor row is not one source/target pair")
        if not isinstance(source, str) or not source.strip() \
                or source != source.strip():
            raise ValueError("successor source is not one exact nonempty token")
        if isinstance(targets, str):
            targets = (targets,)
        elif not isinstance(targets, (list, tuple)) or not targets:
            raise ValueError("successor targets are absent or malformed")
        for target in targets:
            if not isinstance(target, str) or not target.strip() \
                    or target != target.strip():
                raise ValueError("successor target is not one exact nonempty token")
            rows.append((source, target))
    return tuple(sorted(set(rows), key=lambda row: (row[0].casefold(),
                                                    row[1].casefold(), row)))


def _agent_snapshot_error(agents):
    if not isinstance(agents, Mapping):
        return "agent snapshot is not an object"
    for field in ("by_seat", "by_pid", "home_by_pid"):
        if not isinstance(agents.get(field), Mapping):
            return "agent snapshot %s index is absent or malformed" % field
    for seat, pids in agents["by_seat"].items():
        if not isinstance(seat, str) or not seat.strip() \
                or not isinstance(pids, (list, tuple)) \
                or any(type(pid) is not int or pid <= 0 for pid in pids):
            return "agent snapshot by_seat row is malformed"
    for pid, seats in agents["by_pid"].items():
        if type(pid) is not int or pid <= 0 \
                or not isinstance(seats, (list, tuple)) \
                or any(not isinstance(seat, str) or not seat.strip()
                       for seat in seats):
            return "agent snapshot by_pid row is malformed"
    for pid, home in agents["home_by_pid"].items():
        if type(pid) is not int or pid <= 0 \
                or home is not None and not isinstance(home, str):
            return "agent snapshot home_by_pid row is malformed"
    if set(agents["by_pid"]) != set(agents["home_by_pid"]):
        return "agent snapshot pid domains disagree"
    expected = {}
    for pid, seats in agents["by_pid"].items():
        for seat in seats:
            expected.setdefault(seat.casefold(), set()).add(pid)
    actual = {}
    for seat, pids in agents["by_seat"].items():
        if seat != seat.casefold():
            return "agent snapshot by_seat key is not normalized"
        actual.setdefault(seat, set()).update(pids)
    if actual != expected:
        return "agent snapshot by_seat and by_pid indexes disagree"
    return None


def snapshot_from_evidence(parties, authority=None, roster=None, presence=(),
                           agents=None, successors=None, errors=(),
                           captured_at=0.0, successors_complete=None,
                           version=SNAPSHOT_VERSION):
    """Mint a deterministic snapshot from already-captured evidence.

    This is the replay/test seam and the integration seam for a caller that
    already owns a wider locked snapshot.  An explicit missing value stays
    missing; it is never replaced by a fresh ambient read.
    """
    captured_errors = []
    try:
        error_rows = tuple(errors)
    except TypeError:
        error_rows = ()
        captured_errors.append(("errors", "error collection is not iterable"))
    for row in error_rows:
        try:
            source, why = row
        except (TypeError, ValueError):
            captured_errors.append(("errors", "error row is malformed: %r" %
                                    (row,)))
            continue
        captured_errors.append((str(source), str(why)))
    if type(version) is not int:
        version = 0
        captured_errors.append(("version", "snapshot version is not an exact integer"))
    complete = successors is not None if successors_complete is None else \
        successors_complete is True
    if not complete:
        captured_errors.append((
            "successors", "no authoritative forwarding/custody frontier was "
            "captured; omission cannot prove no successor obligation"))
    if not isinstance(authority, Mapping):
        authority = {}
        captured_errors.append(("authority", "authority snapshot is not an object"))
    if not isinstance(roster, Mapping):
        roster = {}
        captured_errors.append(("roster", "roster snapshot is not an object"))
    if not isinstance(presence, (list, tuple)):
        presence = ()
        captured_errors.append(("presence", "presence snapshot is not a sequence"))
    agent_error = _agent_snapshot_error(agents)
    if agent_error:
        agents = {"by_seat": {}, "by_pid": {}, "home_by_pid": {}}
        captured_errors.append(("agents", agent_error))
    try:
        successor_rows = _successor_rows(successors)
    except (TypeError, ValueError) as exc:
        successor_rows, complete = (), False
        captured_errors.append(("successors", "successor frontier is malformed: %s"
                                % exc))
    captured_parties = []
    try:
        values = tuple(parties)
    except TypeError:
        values = ()
        captured_errors.append(("parties", "party collection is not iterable"))
    for value in values:
        try:
            captured_parties.append(_party(value))
        except (TypeError, ValueError):
            captured_errors.append(("parties", "party row is malformed: %r" %
                                    (value,)))
    try:
        stamp = float(captured_at)
        if not math.isfinite(stamp):
            raise ValueError("non-finite timestamp")
    except (TypeError, ValueError):
        stamp = 0.0
        captured_errors.append(("captured_at", "snapshot timestamp is unreadable"))
    return ReachabilitySnapshot(
        version=version,
        captured_at=stamp,
        parties=tuple(captured_parties),
        authority=_freeze(authority),
        roster_rows=_freeze(roster),
        presence=_freeze(presence),
        agents=_freeze(agents),
        successors=_freeze(successor_rows),
        successors_complete=complete,
        errors=_freeze(tuple(sorted(captured_errors))))


def capture(parties, successors=None, now=None, successors_complete=None,
            authority_provider=None):
    """Capture every source once.  Capability failures become named UNKNOWNs.

    ``authority_provider`` is an explicit capability with the same contract as
    ``actors_authority.checked_state``: ``(strict_state, unavailable)``.  The
    actors-authority implementation is a declared composition prerequisite on
    this exact trunk, so omitting the provider must not silently make this API
    operational or let hand-shaped authority authorize absence.
    """
    errors = []
    if authority_provider is None:
        authority = {}
        errors.append((
            "authority", "%s capability was not supplied; canonical Seat "
            "authority is an unlanded composition prerequisite" %
            AUTHORITY_CAPABILITY))
    else:
        try:
            authority, unavailable = authority_provider()
            if unavailable:
                errors.append(("authority", unavailable))
        except Exception as exc:             # noqa: BLE001 — a failed strict
            authority = {}                   # replay is UNKNOWN, never absence
            errors.append(("authority", "canonical Seat authority unavailable: %s"
                           % exc))

    try:
        from . import seats_roster
        roster, failed = seats_roster.roster_acquired()
        if failed:
            errors.append(("roster", "roster evidence is unreadable"))
    except Exception as exc:                 # noqa: BLE001 — captured as data
        roster = {}
        errors.append(("roster", "roster capture failed: %s" % exc))

    try:
        from . import seats_report
        presence = seats_report.presence_report()
    except Exception as exc:                 # noqa: BLE001 — captured as data
        presence = ()
        errors.append(("presence", "presence projection failed: %s" % exc))

    try:
        from . import beacons
        agents = beacons.agent_index()
        if agents is None:
            errors.append(("agents", "agent process census is unreadable"))
    except Exception as exc:                 # noqa: BLE001 — captured as data
        agents = None
        errors.append(("agents", "agent process census failed: %s" % exc))

    return snapshot_from_evidence(
        parties, authority=authority, roster=roster, presence=presence,
        agents=agents, successors=successors, errors=errors,
        captured_at=time.time() if now is None else now,
        successors_complete=successors_complete)


def dispatch_parties(row):
    """Action parties for a dispatch, with custody as a successor edge.

    The sender remains the durable origin.  When custody moved, its actionable
    delivery/chase leg follows the exact recorded custodian instead of checking
    both the obsolete holder and its successor.  Recipient rebind already writes
    the current recipient into the row.  Malformed identity fields never become
    plausible tokens by string coercion.
    """
    if not isinstance(row, Mapping):
        raise ValueError("dispatch row is not an object")
    values = []
    for field in ("sender", "recipient", "custodian"):
        value = row.get(field)
        if value is None or value == "":
            values.append("")
        elif not isinstance(value, str) or value != value.strip():
            raise ValueError("dispatch %s is not one exact token" % field)
        else:
            values.append(value)
    sender, recipient, custodian = values
    parties = [Party("sender", sender or "<missing sender>"),
               Party("recipient", recipient or "<missing recipient>")]
    successors = {sender: (custodian,)} \
        if sender and custodian and sender.casefold() != custodian.casefold() else {}
    return tuple(parties), successors


def capture_dispatch(row, forwarding=None, now=None,
                     authority_provider=None):
    """The retire/future-liveness adapter seam; no retire-local classifier.

    ``forwarding`` is the caller's complete authoritative forwarding frontier.
    Omitting it keeps the snapshot usable for diagnosis but unable to authorize
    ABSENT, even when the row itself carries a custody successor.  A malformed
    frontier is passed to the snapshot validator rather than raising or losing
    its UNKNOWN provenance while this adapter tries to merge custody.
    """
    try:
        parties, custody = dispatch_parties(row)
    except ValueError as exc:
        return snapshot_from_evidence(
            (("dispatch", "<malformed>"),), authority={}, roster={},
            presence=(), agents={"by_seat": {}, "by_pid": {},
                                 "home_by_pid": {}}, successors=None,
            errors=(("dispatch", str(exc)),),
            captured_at=time.time() if now is None else now,
            successors_complete=False)
    dispatch_errors = tuple(
        (party.role, "dispatch %s identity is absent" % party.role)
        for party in parties if party.token.startswith("<missing "))
    if isinstance(forwarding, Mapping):
        successors = list(forwarding.items()) + list(custody.items())
    elif isinstance(forwarding, (list, tuple)):
        successors = list(forwarding) + list(custody.items())
    elif forwarding is None:
        successors = list(custody.items())
    else:
        successors = forwarding
    snapshot = capture(parties, successors=successors, now=now,
                       successors_complete=forwarding is not None,
                       authority_provider=authority_provider)
    if not dispatch_errors:
        return snapshot
    replay = snapshot.as_dict()
    replay["errors"].extend(dispatch_errors)
    return snapshot_from_evidence(**replay)


def _errors(snapshot):
    return tuple("%s: %s" % row for row in snapshot.errors)


def _labels(seat):
    rows = seat.get("labels") if isinstance(seat, Mapping) else None
    return rows if isinstance(rows, (list, tuple)) else ()


def _identity_matches(snapshot, token):
    """Exact matches after a full stable-identity conflict census."""
    wanted = str(token).casefold()
    grouped = {}
    for authority_id, actor in sorted(snapshot.authority.items()):
        if not isinstance(actor, Mapping):
            continue
        actor_id = actor.get("actor_id") or authority_id
        seats = actor.get("seat_bindings")
        if not isinstance(seats, (list, tuple)):
            continue
        for seat in seats:
            if not isinstance(seat, Mapping) or not seat.get("seat_id"):
                continue
            key = str(actor_id), str(seat["seat_id"])
            if not any(existing == seat for existing in grouped.get(key, ())):
                grouped.setdefault(key, []).append(seat)
    matches, conflicts = {}, []
    for key, seats in grouped.items():
        exact = [seat for seat in seats if any(
            isinstance(row, Mapping) and
            str(row.get("normalized_label") or
                row.get("display_label") or "").casefold() == wanted
            for row in _labels(seat))]
        if not exact:
            continue
        matches[key] = exact[0]
        if len(seats) != 1:
            conflicts.append(key)
    return matches, tuple(sorted(conflicts))


def _canonical(seat, fallback):
    canonical = sorted(set(
        str(row.get("display_label")) for row in _labels(seat)
        if isinstance(row, Mapping) and row.get("role") == "canonical"
        and row.get("state") in ("active", "retired")
        and row.get("display_label")), key=str.casefold)
    return canonical[0] if len(canonical) == 1 else str(fallback)


def _identity(snapshot, token):
    matches, conflicts = _identity_matches(snapshot, token)
    if conflicts:
        return None, None, None, (
            "exact token %r has conflicting duplicate authority rows" % token)
    if len(matches) != 1:
        why = ("no canonical authority record for exact token %r" % token
               if not matches else
               "exact token %r matches %d durable Seat identities" %
               (token, len(matches)))
        return None, None, None, why
    (actor_id, seat_id), seat = next(iter(matches.items()))
    return actor_id, seat_id, seat, None


def _successors(snapshot, token):
    """Edges for the exact current chain token, never every historical alias."""
    source_token = str(token).casefold()
    return sorted(set(target for source, target in snapshot.successors
                      if source.casefold() == source_token), key=str.casefold)


def _presence_rows(snapshot, seat):
    names = {str(row.get("display_label") or "").casefold()
             for row in _labels(seat) if isinstance(row, Mapping)
             and row.get("display_label")}
    return [row for row in snapshot.presence if isinstance(row, Mapping)
            and str(row.get("seat") or "").casefold() in names]


def _roster_rows(snapshot, seat):
    names = {str(row.get("display_label") or "").casefold()
             for row in _labels(seat) if isinstance(row, Mapping)
             and row.get("display_label")}
    return [(name, row) for name, row in snapshot.roster_rows.items()
            if str(name).casefold() in names]


def _agent_pids(snapshot, seat):
    if not isinstance(snapshot.agents, Mapping):
        return []
    by_seat = snapshot.agents.get("by_seat")
    if not isinstance(by_seat, Mapping):
        return []
    names = {str(row.get("display_label") or "").casefold()
             for row in _labels(seat) if isinstance(row, Mapping)
             and row.get("display_label")}
    out = []
    for name in names:
        pids = by_seat.get(name) or ()
        if isinstance(pids, (list, tuple)):
            out.extend(pid for pid in pids if type(pid) is int and pid > 0)
    return sorted(set(out))


def _runtime(row):
    """One exact current-session runtime, or (None, False)."""
    if not isinstance(row, Mapping):
        return None, False
    session = row.get("session")
    entries = row.get("runtime_sessions")
    if session and isinstance(entries, Mapping) and session in entries:
        entry = entries[session]
        runtime = entry.get("runtime") if isinstance(entry, Mapping) else None
        return (runtime, entry.get("verified") is True) \
            if isinstance(runtime, Mapping) else (None, False)
    runtime = row.get("runtime")
    return (runtime, row.get("runtime_verified") is True) \
        if isinstance(runtime, Mapping) else (None, False)


def _runtime_conflict(row):
    """Whether exact sessions prove incompatible native/proxy identities."""
    entries = row.get("runtime_sessions") if isinstance(row, Mapping) else None
    if not isinstance(entries, Mapping):
        return False
    identities = set()
    for entry in entries.values():
        runtime = entry.get("runtime") if isinstance(entry, Mapping) else None
        if entry.get("verified") is True and isinstance(runtime, Mapping):
            identities.add((runtime.get("backend"), runtime.get("family")))
    return len(identities) > 1


def _living_state(snapshot, seat):
    presence = _presence_rows(snapshot, seat)
    roster = _roster_rows(snapshot, seat)
    pids = _agent_pids(snapshot, seat)
    evidence = []
    if len(presence) > 1 or len(roster) > 1:
        return UNKNOWN, evidence, ("current projections contain ambiguous exact "
                                   "Seat rows",)
    if presence:
        state = str(presence[0].get("presence") or "").casefold()
        if state == "unverified":
            return UNKNOWN, evidence, ("current presence is identity-unverified",)
        if state == "fresh":
            evidence.append("verified current presence is fresh")
        elif state == "quiet":
            evidence.append("verified current presence is quiet")
    if roster and _runtime_conflict(roster[0][1]):
        return UNKNOWN, tuple(evidence), (
            "exact sessions prove conflicting runtime identities and agent_index "
            "is family-blind",)
    if pids:
        runtime, verified = _runtime(roster[0][1]) if roster else (None, False)
        if runtime and runtime.get("backend") == "proxy" and not verified:
            return UNKNOWN, tuple(evidence), (
                "family-blind agent presence conflicts with unverified proxy identity",)
        evidence.append("exact agent declaration pid %s is live" % pids[0])
    if any("fresh" in item for item in evidence) or pids:
        return LIVE, tuple(evidence), ()
    if any("quiet" in item for item in evidence):
        return QUIET, tuple(evidence), ()
    return UNKNOWN, tuple(evidence), (
        "no positive LIVE or QUIET evidence exists; projection miss is not absence",)


def check_party(snapshot, party):
    """Resolve one complete exact-token chain, then classify its final Seat."""
    party = _party(party)
    common_errors = _errors(snapshot)
    if snapshot.version != SNAPSHOT_VERSION:
        common_errors += ("unsupported snapshot version %r" % snapshot.version,)
    token = party.token.strip()
    if not token:
        return ReachabilityResult(party.role, party.token, party.token, UNKNOWN,
                                  None, None, (), (), common_errors +
                                  ("party token is empty",))
    visited, chain = set(), []
    while True:
        folded = token.casefold()
        if folded in visited:
            return ReachabilityResult(
                party.role, party.token, token, UNKNOWN, None, None,
                tuple(chain + [token]), (), common_errors +
                ("successor chain is cyclic",))
        visited.add(folded)
        actor_id, seat_id, seat, why = _identity(snapshot, token)
        if why:
            return ReachabilityResult(
                party.role, party.token, token, UNKNOWN, actor_id, seat_id,
                tuple(chain + [token]), (), common_errors + (why,))
        canonical = _canonical(seat, token)
        if not chain or chain[-1].casefold() != token.casefold():
            chain.append(token)
        if chain[-1].casefold() != canonical.casefold():
            chain.append(canonical)
        successors = _successors(snapshot, token)
        if len(successors) > 1:
            return ReachabilityResult(
                party.role, party.token, canonical, UNKNOWN, actor_id, seat_id,
                tuple(chain), (), common_errors +
                ("successor chain is ambiguous: %s" % ", ".join(successors),))
        if successors:
            token = successors[0]
            continue
        if token.casefold() != canonical.casefold():
            token = canonical
            continue
        break

    current = bool(_roster_rows(snapshot, seat) or _presence_rows(snapshot, seat)
                   or _agent_pids(snapshot, seat))
    seat_state = seat.get("seat_state")
    if seat_state == "retired":
        if common_errors:
            state, evidence, errors = UNKNOWN, (), common_errors
        elif current:
            state, evidence, errors = UNKNOWN, (), (
                "terminal authority conflicts with current Seat presence",)
        else:
            state, evidence, errors = ABSENT, (
                "canonical authority finalized Seat retirement after strict "
                "obligation, delivery, lease, and runtime clearance",), ()
    elif seat_state in ("active", "retiring"):
        if common_errors:
            state, evidence, errors = UNKNOWN, (), common_errors
        else:
            state, evidence, errors = _living_state(snapshot, seat)
    else:
        state, evidence, errors = UNKNOWN, (), common_errors + (
            "canonical authority Seat state is unreadable",)
    return ReachabilityResult(
        party.role, party.token, canonical, state, actor_id, seat_id,
        tuple(chain), tuple(evidence), tuple(errors))


def check_action(snapshot):
    """Evaluate every party independently; one UNKNOWN blocks authorization."""
    return ActionReachability(
        snapshot.version, snapshot.captured_at,
        tuple(check_party(snapshot, party) for party in snapshot.parties))
