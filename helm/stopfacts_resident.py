#!/usr/bin/env python3
"""The stop-facts leg of the `helm web` resident: the one writer of
`_global/web-cache/stop-facts.json`.

A STOP CANNOT AFFORD TO BUILD THESE FACTS. Each Stop is a new interpreter,
and building them there means folding the whole dispatch ledger, asking git
eight to twelve questions per held lane and reading the room census, against a
wall-clock budget on a box whose load decides the wall clock. The facts are the
same for every stop until an input moves, so they are computed HERE, once per
change, by the
guard's own functions — `seats_delegation._gate_pending`,
`seats_room_advice._room_unfinished`, `_dispatch_advice`,
`dispatches.review_spiral` and `dispatches.owed` — and written behind this
process for `helm/stopfacts.py` to read. There is no second oracle: the
reader never re-derives a fact, it only checks whether the inputs the fact
was computed from have moved. (The beacon, check-in and work-offer rungs read
the owed frontier through their own functions in the hook; `owed` of the owed
rows is those rows, so that answer is the fold's — see `View.owed_pair`.)

ONE WRITER. Two `helm web` processes can be alive at once; the leg takes
`stop-facts.lock` with LOCK_NB and holds it for the life of the process, and
a loser serves its pages and never writes. A process whose code on disk no
longer matches the code it imported does not write either: its facts would
carry a policy the hooks already reject.

IT RE-EXECS ITSELF ONTO A NEW TREE. After a land the tree's code digest
(`stopfacts.code_policy`, the one the hooks compare) no longer matches what
this process imported, and every claims exemption on the fleet is refused
until the facts are recomputed by the new code. Nothing else restarts this
process — no path unit, no land step, no supervisor is relied on — so the
leg does it: once the new digest has read the same for REEXEC_SETTLE_S (a
checkout still being written is not the tree to start on) and the new tree
imports in a child interpreter (a broken tree is never exec'd onto, so the
console keeps serving), it waits out any snapshot write in flight, releases
the writer lock and calls `os.execv` on its own command line: same
interpreter, same argv (so the same port), same environment. The window a
land opens is then about one full compute of the new image. A digest the
check refused, or an exec that failed, is not retried until the tree moves
again; `helm doctor` names a resident whose loaded code is older than the
tree.

WHEN IT RECOMPUTES. Every POLL_S the leg stats the ledger, the claims file
and the roster, reads each held lane's HEAD and each repository's trunk ref by
file, and recomputes when any of them moved, or when FULL_S passed (a working
tree's write age and a live gate pid move without any of those). The run goes
through `web_cache._read_behind`, the resident's one read-behind primitive,
with a `changed` predicate — so a refresh is single-flight, a failure is
recorded where every other resident read records its failures, and a slow
refresh never stacks behind itself.

THE COMPOSITION SEAM HAS ITS OWN LEG. The untested-composition rung walked
every worktree of the repository, asked git about every live pair and read the
whole gate-receipt ledger on every Stop. `compute_seam` does that here, per
repository, with the rung's own functions (`work._gc.seam_rooms`,
`green_receipts`, `seam_candidates` once per live room), and the stop
assembles its own answer from those rows (`work._gc.seam_assemble`). It runs
on its own read-behind key, so a slow seam refresh never holds back the lease
facts a claims exemption waits on; both legs write the one snapshot under one
lock.

THE MECHANICAL LEG MOVED HERE TOO. The memory-index cap and the scratch
reaper ran at the end of every Stop, where they cost a seat seconds and did
nothing a periodic job cannot do better. They run every MECHANICAL_S from the
same resident, under the same kill switches.
"""
import fcntl
import os
import subprocess
import sys
import threading
import time

from . import home, pk, stopfacts

POLL_S = 1.0
FULL_S = 60.0
MECHANICAL_S = 60.0
#: `green_receipts`' legacy-placement budget in the resident. The stop path
#: bounded it (`gateimport.BINDING_CANDIDATE_BUDGET_S`) because a Stop has a
#: wall clock; the resident has none, so it finishes the compatibility scan
#: (None) and the seat gets the complete answer, not a legacy UNKNOWN.
SEAM_BINDING_BUDGET_S = None
#: `_read_behind`'s ceiling for serving a reading; the leg never serves one to
#: anybody, so this only bounds how long a failed refresh is left standing.
HARD_TTL_S = stopfacts.HARD_AGE_S
KEY = "stop-facts"
SEAM_KEY = "stop-facts-seam"
MECHANICAL_KEY = "stop-mechanical"
#: The producer's reason when the fold itself raised, named once: the lease
#: facts carry it into the room advice verbatim, and an arm pins THIS constant.
LEDGER_RAISED = "the dispatch ledger raised"

#: The code this process imported, taken when the leg is first loaded.
LOADED_POLICY = stopfacts.code_policy(fresh=True)
#: How long a changed tree's digest must read the same before the leg
#: re-execs onto it: one poll, so a checkout or rebase still writing files is
#: waited out rather than started on half-written.
REEXEC_SETTLE_S = 1.0
#: The child interpreter's bound for importing the new tree before the exec.
PREFLIGHT_S = 60.0
#: How long after the tree changes a resident may still be on its way onto it
#: (the settle, the import check, a new image's start): past it, a resident
#: still running older code is a fault `helm doctor` fails, not a re-exec.
REEXEC_WITHIN_S = 60.0
#: THE COMMAND LINE THIS PROCESS WAS STARTED WITH, interpreter options and
#: all (`sys.orig_argv`), under this interpreter: a re-exec is the same
#: `helm web`, on the same port, run the same way.
ARGV = [sys.executable] + (list(sys.orig_argv[1:])
                            if getattr(sys, "orig_argv", None)
                            else list(sys.argv))


def _eager_imports():
    """Import every module the facts are computed with NOW, so the code this
    process runs is the code LOADED_POLICY names — a lazy import after a
    deploy would mix trees under one policy."""
    from . import (dispatches, gate, landreq, seats_delegation,  # noqa: F401
                   seats_room_advice, seats_roster, seats_stop_signals, vcs)
    from .work import _gc, _lanes  # noqa: F401


def _claims():
    """{resource: row} for every live claim, or ({}, why)."""
    from .seats_common import _sweep, claims_path
    try:
        raw = pk.read_json(claims_path(), {}, strict=True) or {}
    except Exception as exc:                 # noqa: BLE001 — named below
        return {}, "the claims file could not be read (%s)" \
            % type(exc).__name__
    rows = _sweep(raw) if isinstance(raw, dict) else {}
    return {r: v for r, v in rows.items()
            if r != "_fence" and isinstance(v, dict)}, None


def _anchor(row):
    """Where a lease's own repository is: the root of the common dir the claim
    recorded, else this process's cwd (the only anchor a claim without a
    recorded repository ever had)."""
    repo = row.get("repo") if isinstance(row, dict) else None
    if isinstance(repo, str) and repo.startswith(os.sep) \
            and os.path.basename(repo.rstrip(os.sep)) == ".git":
        return os.path.dirname(repo.rstrip(os.sep))
    from .seats_identity import safe_cwd
    return safe_cwd()


def _trunk_ref_path(name):
    name = str(name or "").strip()
    if not name:
        return None
    return ("refs/remotes/" + name) if name.startswith("origin/") \
        else ("refs/heads/" + name)


def _ref_matches(ref, sha):
    ref, sha = str(ref or ""), str(sha or "")
    return bool(ref and sha) and (ref == sha or sha.startswith(ref)
                                  or ref.startswith(sha))


def _table(value):
    """A snapshot table as this module may index it: the dict, else empty.
    A prior snapshot can be the one found on disk at start, which another
    process wrote; a table of the wrong shape must cost a recompute, never a
    leg that raises on every refresh until the process is restarted."""
    return value if isinstance(value, dict) else {}


def _jsonable(value):
    """A row as plain JSON: sets become sorted lists, anything unknown its
    string. The fold's rows are JSON already; this keeps one odd value from
    costing the whole snapshot."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _packed_sha(common, ref):
    """The sha packed-refs records for `ref`, read in the resident only."""
    try:
        with open(os.path.join(common, "packed-refs"), "rb") as f:
            for line in f:
                parts = line.decode("utf-8", "replace").strip().split(" ", 1)
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


def head(room):
    """(sha, witness) for a lane room, reading packed-refs when the branch is
    packed so the resident can BIND a fact to a sha the hook only witnesses."""
    sha, witness = stopfacts.head_witness(room)
    if witness and not sha and "\x1f" in witness:
        gitdir, common = stopfacts.git_dirs(room)
        ref = witness.split("\x1f", 1)[0][len("ref:"):].strip()
        sha = _packed_sha(common, ref) if common else None
    return sha, witness


def lease_facts(resource, row, snap, note, seats_for=(), now=None):
    """The facts one stop needs about one held lease, computed with the
    guard's own functions. Never raises; an input that cannot be read is an
    UNKNOWN in the facts, exactly as the guard would have printed it."""
    from . import seats_delegation, seats_room_advice
    now = time.time() if now is None else now
    usable = snap if isinstance(snap, dict) and not note else None
    parts = str(resource).split(":", 2)
    out = {"computed_at": now}
    if parts[0] == "dispatch":
        rid8 = parts[1].strip().lower() if len(parts) > 1 else ""
        out["ids"] = sorted(str(k) for k in (usable or {})
                            if rid8 and str(k).lower().startswith(rid8))
        out["stem"] = rid8
        advice = {}
        for seat in sorted({s for s in seats_for if s}):
            advice[seat] = {
                "brief": seats_room_advice._dispatch_advice(
                    resource, seat, snap=snap, ledger_note=note, brief=True),
                "long": seats_room_advice._dispatch_advice(
                    resource, seat, snap=snap, ledger_note=note, brief=False)}
        out["advice"] = advice
        return out
    if parts[0] != "worktree" or len(parts) != 3:
        return out
    anchor = _anchor(row)
    room = seats_delegation._lease_worktree(resource, cwd=anchor)
    out["room"] = room
    out["room_exists"] = bool(room) and os.path.isdir(room)
    sha0, wit0 = head(room) if room else (None, None)
    family = seats_delegation._lane_stem(parts[2])
    out["stem"] = family
    out["ids"] = sorted(str(rid) for rid, r in (usable or {}).items()
                        if isinstance(r, dict)
                        and seats_delegation._lane_stem(r.get("lane"))
                        == family)
    try:
        gate = seats_delegation._gate_pending(
            resource, snap=usable if usable is not None else {}, cwd=anchor)
    except Exception:                        # noqa: BLE001 — UNKNOWN blocks
        gate = None
    try:
        findings, unknowns = seats_room_advice._room_unfinished(
            resource, snap=usable,
            ledger_note=note or (None if usable is not None
                                 else "no snapshot"),
            cwd=anchor)
    except Exception as exc:                 # noqa: BLE001
        findings, unknowns = [], seats_room_advice._missed(
            seats_room_advice._ROOM_READS,
            "the room read raised %s" % type(exc).__name__)
    sha1, wit1 = head(room) if room else (None, None)
    witnessed = bool(wit0) and wit0 == wit1 and bool(sha0)
    # POSITIVE PROOF IS BOUND TO THE HEAD THE HOOK WILL WITNESS. The gate row's
    # ref must match the sha read by file before AND after the fold, so a HEAD
    # that moved and moved back mid-compute cannot lend its proof to another.
    if gate and not (witnessed and _ref_matches(
            (usable or {}).get(gate[0], {}).get("ref"), sha0)):
        gate = None
    out["head"] = sha0 if witnessed else None
    out["head_witness"] = wit0 if witnessed else None
    out["gate"] = list(gate) if gate else None
    out["findings"] = [str(f) for f in findings]
    out["unknowns"] = [str(u) for u in unknowns]
    if room:
        _gitdir, common = stopfacts.git_dirs(room)
        try:
            from .work import _gc
            ref = _trunk_ref_path(_gc._trunk(room))
        except Exception:                    # noqa: BLE001
            ref = None
        if common and ref:
            out["trunk"] = {"common": common, "ref": ref,
                            "witness": stopfacts.ref_witness(common, ref)}
    return out


def seat_facts(seat, pair, owed_rows, now=None):
    """The review-spiral answer one seat's stop reads, from its owner
    `dispatches.review_spiral` over the whole fold. (The rungs that read only
    the owed frontier — beacon obligation, the check-in candidate, the work
    offer — take the frontier itself: see `stopfacts.View.owed_pair`.)"""
    from . import dispatches
    now = time.time() if now is None else now
    out = {"computed_at": now}
    try:
        info, err = dispatches.review_spiral(seat, snap=pair)
    except Exception as exc:                 # noqa: BLE001
        info, err = None, "review rounds could not be read (%s)" \
            % type(exc).__name__
    out["spiral"] = [_jsonable(info), err]
    keys = {str(r.get("id")) for r in owed_rows or () if isinstance(r, dict)}
    if isinstance(info, dict):
        for k in ("lane", "chain"):
            if info.get(k):
                keys.add(str(info[k]))
    out["ids"] = sorted(keys)
    return out


def _seat_set(claims, roster_rows, owed_rows, usable=None):
    """Every seat a stop could be asked about: the roster, every claim holder,
    every party an owed row names, and every sender the ledger records (the
    string `review_spiral` keys a seat on)."""
    names = {str(s) for s in roster_rows}
    if usable is not None:
        from . import dispatches
        try:
            names.update(dispatches._sender_strings(snap=usable))
        except Exception:                    # noqa: BLE001 — the rest stands
            pass
    for row in claims.values():
        if row.get("holder"):
            names.add(str(row["holder"]).strip())
    from . import dispatches
    for r in owed_rows or ():
        for k in ("recipient", "sender"):
            if r.get(k):
                names.add(str(r[k]).strip())
        try:
            c = dispatches.custodian_of(r)
        except Exception:                    # noqa: BLE001
            c = None
        if c:
            names.add(str(c).strip())
    return sorted(n for n in names if n)


def _tail(prior, ledger, before):
    """The lowered ledger bytes appended since `prior` was written, or None
    when `prior` cannot be extended (no prior, other code, a replaced or
    unread ledger) and everything must be recomputed."""
    if not isinstance(prior, dict) or prior.get("policy") != LOADED_POLICY \
            or not before:
        return None
    head = _table(prior.get("ledger"))
    offset = head.get("offset")
    if head.get("unavailable") or head.get("path") != ledger \
            or head.get("ino") != before[0] or type(offset) is not int \
            or offset > before[1]:
        return None
    try:
        with open(ledger, "rb") as f:
            f.seek(offset)
            return f.read(before[1] - offset).lower()
    except OSError:
        return None


def _keeps(facts, tail):
    """May a prior fact stand? Only when the bytes appended since it name
    none of its keys — the reader's own staleness rule (`stopfacts.names`)."""
    if tail is None or not isinstance(facts, dict):
        return False
    return stopfacts.names(tail, (facts.get("stem"),)
                           + tuple(facts.get("ids") or ())) is None


def _claim_mark(row):
    """What about a claim row a lease's facts depend on: who holds it, which
    session minted it, which repository it anchors in."""
    return [str(row.get(k) or "") for k in ("holder", "session", "repo")]


def _lease_unmoved(facts):
    """Is a prior lane fact's HEAD and trunk what the files say now?"""
    room = facts.get("room")
    if not room:
        return True
    if head(room)[1] != facts.get("head_witness"):
        return False
    trunk = facts.get("trunk")
    if isinstance(trunk, dict) and stopfacts.ref_witness(
            trunk.get("common"), trunk.get("ref")) != trunk.get("witness"):
        return False
    return True


def compute(prior=None, now=None, seam=True):
    """The whole snapshot -> dict. Never raises.

    With `prior` (the last snapshot this process wrote) only the facts the
    appended ledger bytes name, the lanes whose HEAD or trunk moved and the
    claims that are new are recomputed; every other fact stands, which is the
    reader's own rule for keeping it EXACT. Without it, everything.

    `seam` computes the composition-seam facts too (`compute_seam`); the
    resident's lease leg passes False and carries its own seam leg's answer
    instead, so the two refresh on their own clocks."""
    from . import dispatches
    from .seats_roster import roster_acquired, roster_indexes
    now = time.time() if now is None else now
    ledger = dispatches.ledger_path()
    before = stopfacts.ledger_mark(ledger)
    try:
        snap, note = dispatches.snapshot()
    except Exception as exc:                 # noqa: BLE001
        snap, note = {}, "%s %s" % (LEDGER_RAISED, type(exc).__name__)
    after = stopfacts.ledger_mark(ledger)
    if before and after and before[0] != after[0]:
        note = note or "the dispatch ledger was replaced during the fold"
    pair = (snap, note)
    usable = snap if isinstance(snap, dict) and not note else None
    tail = _tail(prior, ledger, before) if usable is not None else None
    old_leases = _table(prior.get("leases")) if tail is not None else {}
    old_seats = _table(prior.get("seats")) if tail is not None else {}
    claims, claims_why = _claims()
    try:
        roster_rows, roster_failed = roster_acquired()
    except Exception:                        # noqa: BLE001
        roster_rows, roster_failed = {}, True
    session_seats, _holders = roster_indexes(roster_rows) \
        if not roster_failed else ({}, {})
    try:
        owed_rows = dispatches.owed(usable) if usable is not None else []
    except Exception:                        # noqa: BLE001
        owed_rows = []
    leases = {}
    for resource, row in sorted(claims.items()):
        was = old_leases.get(resource)
        claim = _claim_mark(row)
        if isinstance(was, dict) and was.get("claim") == claim \
                and _keeps(was, tail) and _lease_unmoved(was):
            leases[resource] = was
            continue
        who = {str(row.get("holder") or "").strip()}
        minted = session_seats.get(str(row.get("session") or ""), ())
        who.update(str(s) for s in minted[:1])
        leases[resource] = lease_facts(resource, row, snap, note,
                                       seats_for=who, now=now)
        leases[resource]["claim"] = claim
    seats = {}
    for seat in _seat_set(claims, roster_rows, owed_rows, usable):
        was = old_seats.get(seat)
        if isinstance(was, dict) and _keeps(dict(was, stem=seat), tail):
            seats[seat] = was
            continue
        seats[seat] = seat_facts(seat, pair, owed_rows, now=now)
    # A SEAT THE LEDGER DOES NOT NAME gets the answer every such seat gets:
    # the set above holds every recorded sender, so a seat outside it has no
    # reviews to count. Computed once, as that seat, by the same
    # function; the reader hands it to any seat it has no row for.
    unnamed = seat_facts(stopfacts.UNNAMED, pair, owed_rows, now=now)
    # THE OWED FRONTIER, whole: every dispatch rung but the spiral reads the
    # ledger only through `dispatches.owed`, which is idempotent over its own
    # output, so this map answers them exactly (`stopfacts.View.owed_pair`).
    fleet = {"computed_at": now, "unavailable": note, "unnamed": unnamed,
             "owed": ({str(r.get("id")): _jsonable(r) for r in owed_rows
                       if isinstance(r, dict) and r.get("id")}
                      if usable is not None else None)}
    # THE OFFSET IS WHAT THE FOLD PROVABLY COVERED. A ledger that grew while
    # the fold read it is recorded at the size seen BEFORE, so the bytes the
    # fold may or may not have seen are a tail the reader treats as unread.
    return {
        "schema": stopfacts.SCHEMA,
        "written_at": time.time(),
        "policy": LOADED_POLICY,
        "ledger": {"path": ledger,
                   "ino": before[0] if before else None,
                   "size": before[1] if before else None,
                   "mtime_ns": before[2] if before else None,
                   "offset": before[1] if before else 0,
                   "unavailable": note},
        "claims_unavailable": claims_why,
        "resident": _resident(),
        "leases": leases,
        "seats": seats,
        "fleet": fleet,
        "recomputed": {"leases": sum(1 for r in leases
                                     if leases[r] is not old_leases.get(r)),
                       "seats": sum(1 for x in seats
                                    if seats[x] is not old_seats.get(x)),
                       "full": tail is None},
        "seam": (compute_seam(now=now)[0] if seam
                 else (prior or {}).get("seam")),
    }


# ---------------------------------------------------------------------------
# the composition seam
# ---------------------------------------------------------------------------

def receipt_paths():
    """The ledgers `green_receipts` answers from: the receipts, the import
    pointers and the canonical import bindings."""
    from . import gate, gateimport
    return (gate.receipts_path(), gateimport.imports_path(),
            gateimport.bindings_path())


def _roster_rows():
    from .seats_common import roster_path
    try:
        rows = pk.read_json(roster_path(), {}) or {}
    except Exception:                        # noqa: BLE001 — no roots from it
        return {}
    return rows if isinstance(rows, dict) else {}


def seam_roots(claims, roster):
    """Every repository a stop's seam rung could be asked about, by real
    path: each worktree claim's recorded repository and each rostered seat's
    cwd. A seat that stops in a repository neither names is told its seam
    facts are ABSENT; it is never answered with another repository's."""
    from . import automap
    from .work import _lanes
    roots = set()
    for resource, row in (claims or {}).items():
        if not str(resource).startswith("worktree:") \
                or not isinstance(row, dict):
            continue
        repo = row.get("repo")
        if isinstance(repo, str) and repo.startswith(os.sep) \
                and os.path.basename(repo.rstrip(os.sep)) == ".git":
            # `find_root`'s own rule on the recorded common dir, so a root
            # named by a claim and one named by a cwd are one key.
            roots.add(os.path.realpath(automap._strip_worktree(
                os.path.dirname(repo.rstrip(os.sep)))))
    cwds = {row.get("cwd") for row in (roster or {}).values()
            if isinstance(row, dict)}
    for cwd in sorted(c for c in cwds if isinstance(c, str) and c):
        if not os.path.isdir(cwd):
            continue
        try:
            root = _lanes.find_root(cwd)
        except Exception:                    # noqa: BLE001 — not a root
            root = None
        if root:
            roots.add(os.path.realpath(root))
    return sorted(roots)


def _green(root, marks, cache):
    """`green_receipts(root)` -> {marks, trees, local, err, warn}, reused while
    the receipt ledgers are the ones it was read from."""
    from .work import _gc
    if isinstance(cache, dict) and cache.get("marks") == marks:
        return cache
    trees, local, err, warn = _gc.green_receipts(
        root, binding_budget_s=SEAM_BINDING_BUDGET_S)
    return {"marks": marks, "trees": trees, "local": local, "err": err,
            "warn": warn}


def _rows_unmoved(prior, facts):
    """May a prior root's rows stand? Only when every input a row is computed
    from reads the same: the census, each live room's HEAD, the trunk, the
    receipt ledgers and the trunk check."""
    if not isinstance(prior, dict) or prior.get("unavailable"):
        return False
    return all(prior.get(k) == facts.get(k)
               for k in ("census", "heads", "trunk", "receipts", "root_err",
                         "green_err", "green_warn")) \
        and isinstance(prior.get("rooms"), dict) \
        and not prior.get("root_raised")


def seam_root_facts(root, prior=None, green=None, now=None):
    """(facts, green cache) for ONE repository, computed with the rung's own
    functions. Never raises for a failed read: every one lands where the rung
    would have put it (`cerr`, `cdeg`, `root_err`, `green_err`).

    EVERY WITNESS IS TAKEN BEFORE THE READ IT WITNESSES — the registry,
    leases and roster before the census, each live HEAD, the trunk and the
    receipt ledgers before the rows — so an input that moves mid-read leaves
    a witness the stop finds moved (STALE), never one that vouches for
    facts computed from something newer. A HEAD that moved while its rows
    were computed is recorded as unwitnessed for the same reason.

    `prior` (this root's last facts) lets rows stand whose inputs have not
    moved; the census itself is re-read every time, since occupancy has no
    witness a stop could check."""
    from .seats_common import roster_path
    from .work import _gc, _lanes
    now = time.time() if now is None else now
    _gitdir, common = stopfacts.git_dirs(root)
    registry_mark = stopfacts.registry_mark(common)
    roster = {"path": roster_path(),
              "mark": stopfacts.roster_mark(roster_path())}
    live_claims = _gc._live()
    registered = _lanes.worktrees(root)
    registry = [os.path.abspath(w["path"]) for w in registered]
    facts = {"computed_at": now, "root": root, "common": common,
             "registry": registry, "registry_mark": registry_mark,
             "roster": roster,
             "leases": _gc.seam_lease_marks(root, registry, live_claims),
             "rooms": {}, "raised": {}, "heads": {}}
    census, cerr, cdeg = _gc.seam_rooms(root, registered=registered)
    facts.update(census=_jsonable(census), cerr=cerr,
                 cdeg=[str(d) for d in cdeg or ()])
    if cerr:
        return facts, green
    live = [r["path"] for r in facts["census"] if r.get("live")]
    facts["heads"] = {p: stopfacts.head_witness(p)[1] for p in live}
    ref = _trunk_ref_path(_gc._trunk(root))
    facts["trunk"] = ({"common": common, "ref": ref,
                       "witness": stopfacts.ref_witness(common, ref)}
                      if common and ref else None)
    facts["receipts"] = {p: stopfacts._mark_list(stopfacts.ledger_mark(p))
                         for p in receipt_paths()}
    # THE TRUNK CHECK IS THE FUNCTION'S OWN, asked with no room of mine: it
    # is what the one-call version answers before it looks at any room. A
    # raise here is the rung's own swallow (the stop keeps its census and its
    # disclosure, and says nothing about rows), recorded as such.
    try:
        _rows, facts["root_err"], _warn = _gc.seam_candidates(
            root, set(), rooms=census, green=(frozenset(), frozenset()))
    except Exception as exc:                 # noqa: BLE001 — named below
        facts["root_err"], facts["root_raised"] = None, type(exc).__name__
        return facts, green
    if facts["root_err"]:
        return facts, green
    green = _green(root, facts["receipts"], green)
    facts["green_err"], facts["green_warn"] = green["err"], green["warn"]
    if green["err"]:
        return facts, green
    if _rows_unmoved(prior, facts):
        facts["rooms"], facts["raised"] = prior["rooms"], \
            prior.get("raised") or {}
        return facts, green
    memo = {}
    for path in live:
        try:
            rows, _err, _warn = _gc.seam_candidates(
                root, {path}, rooms=census,
                green=(green["trees"], green["local"]), memo=memo)
        except Exception as exc:             # noqa: BLE001 — named per room
            facts["raised"][path] = type(exc).__name__
            continue
        facts["rooms"][path] = _jsonable(rows)
    for path, witness in list(facts["heads"].items()):
        if stopfacts.head_witness(path)[1] != witness:
            facts["heads"][path] = None
    return facts, green


def compute_seam(prior=None, green=None, now=None, only=None):
    """({"computed_at", "roots": {root: facts}}, {root: green cache}) for
    every root `seam_roots` names. One root's failure is that root's
    ABSENT, never the others'.

    `only` recomputes just those roots and keeps every other root of `prior`
    as it stands — the resident's answer to one lane's HEAD moving, which
    moves nothing in another repository. None recomputes all of them and
    asks again which repositories there are."""
    now = time.time() if now is None else now
    old = _table(prior.get("roots")) if isinstance(prior, dict) else {}
    green = dict(green or {})
    if only is None:
        claims, _why = _claims()
        names = seam_roots(claims, _roster_rows())
    else:
        names = sorted(old)
    roots = {}
    for root in names:
        if only is not None and root not in only:
            roots[root] = old[root]
            continue
        try:
            roots[root], green[root] = seam_root_facts(
                root, old.get(root), green.get(root), now=now)
        except Exception as exc:             # noqa: BLE001 — this root only
            roots[root] = {"computed_at": now, "root": root,
                           "unavailable": "the resident's seam census "
                                          "raised %s" % type(exc).__name__}
    return ({"computed_at": now, "roots": roots},
            {r: g for r, g in green.items() if r in roots and g})


_STARTED = time.time()


def _resident(replaying=None):
    return {"pid": os.getpid(), "starttime": stopfacts.own_starttime(),
            "started_at": _STARTED, "replaying_since": replaying,
            "code_root": stopfacts.code_root()}


def _preflight():
    """None when the tree on disk imports the modules a re-exec starts, else
    why not. Run in a child interpreter, so a tree that does not import costs
    a line in this process's log and never the console."""
    parent = os.path.dirname(stopfacts.code_root())
    probe = ("import sys; sys.path.insert(0, %r); "
             "import helm.cli, helm.web_server, helm.stopfacts_resident"
             % parent)
    try:
        r = subprocess.run([sys.executable, "-c", probe], cwd=parent,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=PREFLIGHT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return "the new tree's import check could not run (%s)" \
            % type(exc).__name__
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()
        return "the new tree does not import (%s)" \
            % (tail[-1][:200] if tail else "exit %d" % r.returncode)
    return None


def _say(line):
    """One line to this process's log (the unit's journal), never raised."""
    try:
        print("helm web: stop-facts: %s" % line, file=sys.stderr, flush=True)
    except Exception:                        # noqa: BLE001 — a log line only
        pass


def write(snapshot, p=None):
    """Atomic, private write of one snapshot. -> None, or why it was not
    written."""
    try:
        import json
        body = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    except Exception as exc:                 # noqa: BLE001
        return "the snapshot does not serialise (%s)" % type(exc).__name__
    if len(body) > stopfacts.MAX_BYTES:
        return "the snapshot is %d bytes, over the %d a hook may read" \
            % (len(body), stopfacts.MAX_BYTES)
    try:
        pk.atomic_write(p or stopfacts.path(), body, mode=0o600)
    except OSError as exc:
        return "the snapshot could not be written (%s)" % exc.strerror
    return None


class Leg(object):
    """The leg's state across polls: the writer lock, the inputs last seen,
    and when the last whole refresh ran."""

    def __init__(self, p=None, lock=None):
        self.path = p or stopfacts.path()
        self.lock = lock or stopfacts.lock_path()
        self._fd = None
        self.seen = None
        self.last_full = 0.0
        self.last = None
        # THE SEAM LEG'S OWN STATE. It refreshes on its own read-behind key,
        # and the two legs meet only in `_publish`, under `_write`.
        self._write = threading.Lock()
        self.seam = None
        self.seam_last_full = 0.0
        self.green = {}
        # THE RE-EXEC'S STATE: the tree digest first seen off LOADED_POLICY
        # and when, and every digest a re-exec was refused on, with why.
        self._moved = None
        self.no_exec = {}

    def writer(self):
        """Is this process THE writer? Takes the lock once, keeps it."""
        if self._fd is not None:
            return True
        try:
            os.makedirs(os.path.dirname(self.lock), exist_ok=True)
            fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def inputs(self):
        """Every input a fact is computed from that a stat or a small read can
        witness: ledger, claims, roster, each lane's HEAD, each trunk."""
        from . import dispatches
        from .seats_common import claims_path, roster_path
        marks = [stopfacts.ledger_mark(dispatches.ledger_path()),
                 stopfacts.ledger_mark(claims_path()),
                 stopfacts.ledger_mark(roster_path())]
        for resource, facts in sorted(_table((self.last or {}).get(
                "leases")).items()):
            room = facts.get("room") if isinstance(facts, dict) else None
            marks.append((resource, stopfacts.head_witness(room)[1]
                          if room else None))
            trunk = facts.get("trunk") if isinstance(facts, dict) else None
            if isinstance(trunk, dict):
                marks.append(stopfacts.ref_witness(trunk.get("common"),
                                                   trunk.get("ref")))
        return marks

    def changed(self):
        """True when an input moved or the whole refresh is due."""
        if time.time() - self.last_full >= FULL_S:
            return True
        try:
            return self.inputs() != self.seen
        except Exception:                    # noqa: BLE001 — refresh then
            return True

    def _refused(self):
        """Why this process may not write now, or None."""
        if not self.writer():
            return "another resident holds %s" % self.lock
        digest = stopfacts.code_policy(fresh=True)
        if digest != LOADED_POLICY:
            why = self.no_exec.get(digest)
            return ("this process's code was replaced on disk; %s"
                    % ("it cannot re-exec onto the new tree: %s" % why
                       if why else "it re-execs onto the new tree"))
        return None

    def code_moved(self, now=None):
        """The tree's code digest once it has moved off LOADED_POLICY and
        read the same for REEXEC_SETTLE_S, else None — unmoved, still
        settling, or a digest a re-exec was already refused on."""
        now = time.monotonic() if now is None else now
        digest = stopfacts.code_policy(fresh=True)
        if digest == LOADED_POLICY:
            self._moved = None
            return None
        if self._moved is None or self._moved[0] != digest:
            self._moved = (digest, now)
            return None
        if digest in self.no_exec or now - self._moved[1] < REEXEC_SETTLE_S:
            return None
        return digest

    def reexec(self, digest, execv=None):
        """Replace this process with the same command line on the new tree.
        -> why it did not (the new tree does not import, the exec failed);
        a real exec never returns.

        IN THIS ORDER: the new tree is import-checked first, so a broken one
        leaves this process serving; then `_write` is taken, so a snapshot
        write in flight finishes and no other starts; then the writer lock is
        released, so the new image (or another resident) can take it; then
        the exec. The interpreter, argv (and with it the port) and the
        environment are this process's own."""
        why = _preflight()
        if why:
            self.no_exec[digest] = why
            _say("not re-exec'ing onto the changed tree: %s" % why)
            return why
        with self._write:
            self.release()
            _say("the tree changed; re-exec'ing onto it: %s" % " ".join(ARGV))
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:            # noqa: BLE001 — exec regardless
                    pass
            try:
                (execv or os.execv)(ARGV[0], ARGV)
            except OSError as exc:
                why = "the exec failed (%s)" % (exc.strerror or exc)
        why = why or "the exec returned"
        self.no_exec[digest] = why
        _say("not re-exec'ing onto the changed tree: %s" % why)
        return why

    def _publish(self, snapshot=None, seam=None):
        """Write the one snapshot from both legs' latest answers. -> why it
        was not written, or None. A seam answer with no lease snapshot yet
        waits for the lease leg's first write, which carries it."""
        with self._write:
            if seam is not None:
                self.seam = seam
            if snapshot is not None:
                self.last = snapshot
            if self.last is None:
                return "no lease facts yet"
            # THE HEADER STAYS THE LEASE LEG'S: `written_at` is when the lease
            # facts were written (the reader's hard age), and the seam facts
            # carry their own `computed_at`.
            body = dict(self.last, seam=self.seam)
            why = write(body, self.path)
            if not why:
                self.last = body
            return why

    def refresh(self):
        """Recompute and write, as the one writer. -> a small receipt dict."""
        refused = self._refused()
        if refused:
            return {"written": False, "why": refused}
        seen = self.inputs()
        full = self.last is None or time.time() - self.last_full >= FULL_S
        snapshot = compute(prior=None if full else self.last, seam=False)
        why = self._publish(snapshot=snapshot)
        self.seen = seen if not why else None
        if full:
            self.last_full = time.time()
        return {"written": why is None, "why": why,
                "leases": len(snapshot.get("leases") or {}),
                "seats": len(snapshot.get("seats") or {}),
                "recomputed": snapshot.get("recomputed"),
                "at": snapshot.get("written_at")}

    def seam_moved(self):
        """The repositories whose seam facts the stop would now call STALE —
        the stop's own rule (`stopfacts.seam_census_moved`,
        `seam_pairs_moved` over every room the facts recorded), so the leg
        recomputes exactly what a reader would refuse. A repository whose
        facts could not be computed waits for the whole refresh."""
        from .work import _gc
        live = _gc._live()
        moved = set()
        for root, facts in sorted(_table((self.seam or {}).get(
                "roots")).items()):
            if not isinstance(facts, dict) or facts.get("unavailable"):
                continue
            if stopfacts.seam_census_moved(facts, live) \
                    or stopfacts.seam_pairs_moved(facts,
                                                  facts.get("heads") or {}):
                moved.add(root)
        return moved

    def seam_changed(self):
        """True when a seam input moved or the whole refresh is due — the
        only way a room's occupancy, and the set of repositories, is
        re-read."""
        if self.seam is None \
                or time.time() - self.seam_last_full >= FULL_S:
            return True
        try:
            return bool(self.seam_moved())
        except Exception:                    # noqa: BLE001 — refresh then
            return True

    def seam_refresh(self):
        """Recompute the seam facts whose inputs moved (all of them, and the
        set of repositories, on the whole refresh) and write them into the
        snapshot."""
        refused = self._refused()
        if refused:
            return {"written": False, "why": refused}
        full = self.seam is None \
            or time.time() - self.seam_last_full >= FULL_S
        only = None if full else self.seam_moved()
        seam, self.green = compute_seam(prior=self.seam, green=self.green,
                                        only=only)
        why = self._publish(seam=seam)
        if full:
            self.seam_last_full = time.time()
        return {"written": why is None, "why": why, "full": full,
                "roots": len(seam.get("roots") or {}),
                "recomputed": sorted(only) if only is not None else None,
                "at": seam.get("computed_at")}

    def announce(self):
        """At start, mark the snapshot on disk as being refolded by this
        process without discarding its facts — they stay usable as advice
        (and exact where the code has not changed) until the first refresh."""
        if not self.writer():
            return
        old, _why = stopfacts.load(self.path)
        if not old:
            return
        old["resident"] = _resident(replaying=time.time())
        write(old, self.path)
        self.last = old
        self.seam = old.get("seam") if isinstance(old.get("seam"),
                                                  dict) else None


def mechanical():
    """The Stop ladder's old tail: the memory-index cap and the scratch
    reaper, best-effort and silent. -> a receipt."""
    from .seats_stop_fp import _off
    out = {"at": time.time()}
    if _off("STOP_GUARD_INDEX"):
        out["skipped"] = "HELM_STOP_GUARD_INDEX=0"
        return out
    try:
        from . import store
        store.index_cap(apply=True)
        out["index"] = "ran"
    except Exception as exc:                 # noqa: BLE001 — best effort
        out["index"] = type(exc).__name__
    try:
        from . import scratch
        out["scratch"] = scratch.auto_gc() or "idle"
    except Exception as exc:                 # noqa: BLE001
        out["scratch"] = type(exc).__name__
    return out


def enabled(port=None):
    """Does this `helm web` run the leg? The server on the console port does;
    `HELM_STOP_FACTS_LEG=1` or `=0` decides for any other."""
    flag = home.env("STOP_FACTS_LEG")
    if flag is not None and str(flag).strip() != "":
        return str(flag).strip() not in ("0", "off", "false", "no")
    from .web_common import DEFAULT_PORT
    return port == DEFAULT_PORT


def tick(leg):
    """One poll: re-exec onto a changed tree, else kick a refresh when the
    inputs moved and the mechanical job when it is due. The refreshes run
    behind the caller on `_read_behind` threads."""
    from . import web_cache
    try:
        moved = leg.code_moved()
        if moved:
            leg.reexec(moved)
    except Exception:                        # noqa: BLE001 — never the server
        pass
    try:
        web_cache._read_behind(KEY, FULL_S, HARD_TTL_S, leg.refresh,
                               changed=leg.changed)
    except Exception:                        # noqa: BLE001 — never the server
        pass
    try:
        web_cache._read_behind(SEAM_KEY, FULL_S, HARD_TTL_S, leg.seam_refresh,
                               changed=leg.seam_changed)
    except Exception:                        # noqa: BLE001 — never the server
        pass
    if leg.writer():
        try:
            web_cache._read_behind(MECHANICAL_KEY, MECHANICAL_S, HARD_TTL_S,
                                   mechanical)
        except Exception:                    # noqa: BLE001
            pass


def start(port=None, poll_s=POLL_S):
    """Start the leg on a daemon thread, or None when this server does not
    run it. Never raises into the server."""
    if not enabled(port):
        return None
    leg = Leg()

    def run():
        try:
            _eager_imports()
            leg.announce()
        except Exception:                    # noqa: BLE001
            pass
        while True:
            tick(leg)
            time.sleep(poll_s)

    t = threading.Thread(target=run, name="helm-stop-facts", daemon=True)
    try:
        t.start()
    except BaseException:                    # noqa: BLE001 — never the serve
        return None
    return t
