"""Spawn registration and runtime recovery for :mod:`helm.seat`."""
import calendar
import collections
import glob
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

from .seat_role import (
    SEAT_ROLES,
    SPAWN_ATTEMPT_ENV,
    _LEAD_SETTINGS,
    _ROLE_ENV,
    _launch_argv,
    _launch_command,
    _launch_env,
    _native_launch_command,
    _resume_role,
    _seat_role,
)
from . import pk


def _record_identity(rec):
    """Canonical runtime identity carried beside a storage-keyed register."""
    return rec.get("identity") or rec.get("seat")


def _identity_register(name):
    """(storage_seat, dir, rec, refusal) — the ONE spawn register whose
    CANONICAL identity is `name`, else (None, None, None, refusal-or-None).

    THE WALK IS THE HONEST INVERSE OF `_record_identity`, and a path built
    from the name is not. `_instance_dir(family, name)` answers where a seat
    STORES its register, which after a durable rename is the seat's ORIGINAL
    name — so the second rename of one seat has no derivable path at all, and
    a canonical name like `gt-codex` does not even resolve a family. The
    register's own declaration is the only thing that binds an identity to a
    storage key, so this reads it, from the tree's own enumerator.

    UNREADABLE IS NOT ABSENT, and the direction matters: a register this walk
    could not read might be the one declaring `name`, so blindness anywhere
    makes a NO-MATCH unknown — while it says nothing about a match that was
    found. Two registers declaring one identity refuse rather than guess.
    """
    from . import seat
    from .seats_common import _seat_label
    registered, blind = seat.registered_seats()
    want = str(name or "").casefold()
    hits, unreadable = [], []
    for storage in sorted(registered):
        family, err = seat._seat_family(storage)
        if err:
            unreadable.append(storage)
            continue
        d = seat._instance_dir(family, storage)
        rec = seat._spawn_record(d)
        if not isinstance(rec, dict) or rec.get("seat") != storage:
            unreadable.append(storage)      # corrupt, not merely different
            continue
        if str(_record_identity(rec) or "").casefold() == want:
            hits.append((storage, d, rec))
    if len(hits) > 1:
        return None, None, None, (
            "%d spawn registers declare %s (%s) — refusing to guess which one "
            "carries this identity; re-point it by hand"
            % (len(hits), _seat_label(name),
               ", ".join(_seat_label(s) for s, _d, _r in hits)))
    # A PARTIAL CENSUS MAY NOT NAME A UNIQUE OWNER, AND THIS CHECK HAS TO COME
    # BEFORE THE SINGLE-HIT RETURN. Ordered the other way, one readable
    # register short-circuited the blindness check — so a tree with a hidden
    # SECOND claimant returned the first as the unique one, and rereading the
    # file we happened to select proves a local declaration rather than
    # exclusive ownership. Two readable hits refuse; one readable hit beside
    # anything unreadable must refuse for the same reason.
    if blind or unreadable:
        return None, None, None, (
            "the seat tree is only partly readable (%s), so whether a spawn "
            "register still declares %s — and whether it is the ONLY one — is "
            "UNKNOWN"
            % ("the register walk reported blindness" if blind else
               "unreadable register(s): "
               + ", ".join(_seat_label(s) for s in unreadable),
               _seat_label(name)))
    if hits:
        return hits[0][0], hits[0][1], hits[0][2], None
    return None, None, None, None


def rename_register_identity(old, new, apply=False):
    """One clause about the surface a rename owns HERE: the spawn register.

    THE SENTENCE THAT WAS NEVER IMPLEMENTED (task/2444). `_record_identity`
    above and `_bind_spawn_session`'s docstring below each state the same
    settled design — a durable rename keeps the register KEYED by the seat's
    ORIGINAL name and carries the canonical one in `identity`. Two places
    described that field; no place ever wrote it. So `identity` was always
    absent, `_record_identity` fell through to `rec["seat"]` — the stale
    storage label — and every reader of the pair got the pre-rename name.

    THAT IS WHAT SILENTLY UNDID A RENAME. `helm-seat-rebind.timer` fires every
    300s into `seat resume --all --apply` -> `rebind_seat` -> `bind_runtime` ->
    `_bind_runtime_session(_record_identity(rec), ...)` -> `write_roster`, so
    the sweep RE-DECLARED the old name at the one door that mints roster rows.
    Measured on a live fleet: `helm chat seat rename` returned, and a bare
    old-name row was minted 280 seconds later — inside a 300s
    cadence, one process, two rows, and the admission retired the `renamed`
    alias that was covering the window. `write_roster` now resolves a live hop
    before it decides `admits`, which is the second half; this is the first,
    and neither alone is the cure. The roster's defence stops the mint; only
    this stops the register from going on declaring a name the fleet retired
    — through `_bind_spawn_session`, `rebind_seat`'s title re-stamp and
    `seat resume`'s launch identity, none of which the roster guards.

    IT NEVER RAISES AND NEVER FAILS THE RENAME, the contract
    `orcatitle.after_rename` and `seats_lineage.carry_holdings` already hold:
    the roster row has already moved and the roster lock is already released,
    so there is nothing to roll back and an exception here would abort a
    rename that HAPPENED. An unwritable or absent register is reported in the
    success sentence, which is where the operator is looking.

    THE LOCK ORDER IS SPAWN-THEN-ROSTER AND MAY NOT BE INVERTED: `rebind_seat`
    holds `.spawn.lock` while `_bind_runtime_session` takes the roster lock
    inside `write_roster`. So the apply call belongs where `rename_seat` makes
    it — AFTER the roster lock is released, beside the other post-lock
    surfaces. `apply=False` takes no lock at all: it reads, and the dry run
    prints what it found.
    """
    from .seats_common import _seat_label
    shown_old, shown_new = _seat_label(old), _seat_label(new)
    try:
        storage, d, _rec, refusal = _identity_register(old)
    except Exception as exc:            # noqa: BLE001 — fail-open law above
        return ("whether a spawn register still declares %s is UNKNOWN (%s)"
                % (shown_old, _one_line(exc)))
    if refusal:
        return refusal
    if storage is None:
        return ("no spawn register declares %s, so none needs re-pointing"
                % shown_old)
    shown_storage = _seat_label(storage)
    if not apply:
        return ("the register KEYED %s declares %s and would carry "
                "identity=%s instead; without it the 300s rebind sweep "
                "re-declares %s at the roster's mint door"
                % (shown_storage, shown_old, shown_new, shown_old))
    from . import pk, seat
    try:
        with seat._seat_lifecycle_lock(d):
            # RE-READ UNDER THE LOCK, because the walk above ran outside it:
            # a concurrent `rebind_seat` owns this same file and the only
            # honest write is one whose premise still holds at write time.
            rec = seat._spawn_record(d)
            if not isinstance(rec, dict) or rec.get("seat") != storage \
                    or str(_record_identity(rec) or "").casefold() \
                    != str(old).casefold():
                return ("the spawn register KEYED %s stopped declaring %s "
                        "before it could be re-pointed; it was NOT changed"
                        % (shown_storage, shown_old))
            rec["identity"] = new       # the ONE field: the key and every
            pk.write_json(seat._spawn_path(d), rec)      # other field stand
    except Exception as exc:            # noqa: BLE001 — fail-open law above
        # NO RETRY ADVICE, BECAUSE THE RETRY CANNOT REACH THIS SURFACE. By the
        # time this runs the roster and chat writes have COMMITTED, so both
        # spellings of a re-run are dead: `rename OLD NEW` refuses because no
        # roster row matches OLD any more, and `rename NEW NEW` returns
        # "already named" without touching anything. Advertising it would
        # promise a recovery that never runs — worse than saying the state is
        # split, because the operator stops looking.
        return ("the spawn register KEYED %s could NOT be re-pointed (%s). "
                "The roster and chat surfaces ARE committed to %s and this "
                "one is NOT, so `helm seat resume --all --apply` keeps "
                "declaring %s from it. RE-RUNNING THE RENAME DOES NOT REACH "
                "THIS SURFACE: the old name no longer has a roster row, and "
                "the new one renames to itself. This register needs repair on "
                "its own."
                % (shown_storage, _one_line(exc), shown_new, shown_old))
    return ("Re-pointed the spawn register KEYED %s: it declares %s now, so "
            "the rebind sweep stops re-declaring %s."
            % (shown_storage, shown_new, shown_old))


def _one_line(exc):
    """An exception rendered as ONE line — every string this module returns is
    appended to a sentence a terminal prints."""
    return " ".join(str(exc).split()) or exc.__class__.__name__


def _runtime_stamp(seat_name, storage_seat=None):
    """Launch-config runtime testimony shared by every lifecycle binder."""
    family, err = _seat_family(storage_seat or seat_name)
    if err:
        return None
    return {"agent_harness": "claude", "family": family, "backend": "proxy"}


def _bind_runtime_session(seat_name, session, storage_seat=None):
    """Host-bind one proven lifecycle session; return an error or None."""
    if not session:
        return None
    from . import seats as _seats
    # THE PROOF RUNS BEFORE THE WRITE, AND THE WRITE CONSUMES IT. Admission and
    # exact-session authority are still separate owners — the metadata-free
    # write may create the row, the locked lifecycle binder owns the session
    # append, cap, runtime evidence and synchronized pruning — but the order
    # they ran in was the whole defect: admitting first handed a stale name to
    # the door that MINTS, so the bare row appeared and the rename alias
    # covering the window was retired before the binder could refuse. The fence
    # answers the binder's questions with nothing written, and the binder
    # re-derives the same predicate under its own lock.
    expect, err = _seats.lifecycle_bind_refusal(seat_name, session)
    if err:
        return err
    # THE FENCE IS CONSUMED AT THE FIRST MUTATION, NOT THE SECOND. The mint
    # door is handed the state the proof actually observed — absent, or one
    # exact generation — and refuses when the world moved, so a row renamed
    # away in the window is not re-minted and a row that appeared in the
    # window is not refreshed on a proof about nothing.
    #
    # AND `lifecycle_sid` CARRIES THE OWNERSHIP QUESTION, on its own
    # parameter. It is NOT `session=`: passing it there would make this an
    # ordinary self-write claiming the sid and would move binder authority to
    # the mint door. The fence reads it, asks whether any OTHER row is
    # currently that session's owner, and writes none of it. Without it the
    # ownership arm was skipped precisely here, at the only production caller
    # that fences — an optional fact defaulting permissive where it is
    # load-bearing.
    _key, row, _admits = _seats.write_roster(
        seat_name, presence_beat=False, keyed=True, admission=True,
        expect=expect, lifecycle_sid=session)
    if row is None:
        return ("lifecycle seat %s is not the row this rebind proved, so the "
                "admission was refused before it wrote anything" % seat_name)
    # AND THE BINDER FENCES ON WHAT WAS ACTUALLY ADMITTED. Handing it the
    # PROOF's value would skip the check for a first bind, which is exactly
    # the window where a concurrent admission installs a row with a current
    # session the binder would then overwrite.
    _entry, err = _seats.bind_lifecycle_runtime(
        seat_name, session, _runtime_stamp(seat_name, storage_seat),
        expect_incarnation=row.get("incarnation"))
    return err


def _prove_spawned_pane(ad, handle):
    """Prove the exact handle returned by spawn is independently live."""
    detail = "returned handle %s was absent from %s inventory" % (handle, ad.name)
    for _ in range(20):
        try:
            matches = [row for row in ad.list() if row.get("handle") == handle]
        except Exception as e:
            detail = "%s pane inventory failed: %s" % (ad.name, e)
            matches = []
        if len(matches) == 1 and _pane_live(matches[0]):
            return True, None
        if len(matches) > 1:
            return False, "returned handle %s matched %d inventory rows" % (
                handle, len(matches))
        if matches:
            detail = "returned handle %s is not live" % handle
        time.sleep(0.05)
    return False, detail


def _pane_sendable(row):
    """Whether a row can RECEIVE input, which is a weaker claim than _pane_live.

    An orphaned pane is READ-BLIND but SEND-CAPABLE: `orphaned` means the PTY
    has no live RENDERER, so reads return "" — but the PTY itself is still
    attached and writable, so a send lands. Measured live: one seat rescued a
    codex seat stuck at 102% by sending /compact straight to its orphaned
    handle, and it compacted.

    This exists because making `_pane_live` honest about orphaned panes (the
    correct read-side fix) also made every ACTUATION refuse, since
    `_resolve_registered_pane` gates on it and the autocompact injection path
    resolves through there. 30 of 34 panes were orphaned, so the rescue path
    died for exactly the seats most likely to need rescuing. A send must not be
    required to prove a renderer it does not use.

    Identity is NOT weakened by this: callers prove the pane through the spawn
    register and the live session's own ORCA_TERMINAL_HANDLE, neither of which
    needs a readable pane."""
    if not (row.get("writable") is True):
        return False
    status = str(row.get("status") or "").strip().lower()
    return status not in ("disconnected", "closed", "gone", "exited", "dead")


_SEND_ONLY_DETAIL = ("SEND-ONLY (orphaned: writable, but reads return empty, "
                     "so this action cannot be confirmed by reading the pane)")

# THE TWO REFUSALS THAT TOGETHER MEAN "NOTHING IS RUNNING". `rebind` proves a
# seat through two routes — its recorded session's pid-keyed record, then its
# own name in a live /proc environ — and refuses with one sentence from each
# when both come up empty. They are constants because `seat resume --all`
# reads them BACK: a rebind refusal equal to both rendered at ZERO is the one
# refusal that licenses a relaunch (see `rebind_refusal_means_dead`), and a
# matcher built from a copy of the prose would drift from the producer the
# first time either sentence was edited, turning every dead seat into UNKNOWN
# with no test to notice. The count is IN the sentence on purpose: "2 exact
# live Claude processes" is the same shape and the opposite fact.
NO_EXACT_LIVE_SESSION = ("session %s has %d exact live Claude processes; pane "
                         "replacement requires exactly one")
NO_DISTINCT_LIVE_PANE = ("%d distinct live panes claim seat '%s'; refusing to "
                         "guess which one owns it")


def _session_record_root(d, record=None):
    """The directory THIS seat's Claude session records actually land in.

    `<instance>/claude/sessions` is not a convention, it is a CONSEQUENCE: a
    proxy seat's launch.sh pins CLAUDE_CONFIG_DIR at `<instance>/claude`, so the
    session records appear under it. A NATIVE seat has no launch.sh and no such
    pin — `helm launch` execs claude against the home the env names — so asking
    that path about a native seat lists an empty directory, and this module reads
    an empty listing as ZERO EXACT LIVE PROCESSES, which is the shape of proof
    that licenses calling a seat dead. The spawn record PINS the home its launch
    selected (`config_home`); that provenance is the authority when it is there,
    and the historical path is the answer for every record written before it,
    which is exactly the set of records whose home IS the instance directory.

    `record` IS THE RECORD THE CALLER IS DECIDING ON, and passing it is how a
    proof stays about one record. Every caller below already holds the seat's
    spawn record under the lifecycle lock; re-reading `spawn.json` here made the
    session root an answer about a SECOND, separately-timed read that a
    concurrent writer can have changed, so a rebind could compare a live process
    against one record while looking for its session records under the home
    another record pinned. Reading it here is the fallback for the one caller
    that has no record in hand (`_live_seat_orca_identity` starts from a seat
    NAME), never the normal path.
    """
    rec = record if isinstance(record, dict) else (_spawn_record(d) or {})
    pinned = rec.get("config_home")
    if isinstance(pinned, str) and pinned:
        return os.path.join(pinned, "sessions")
    return os.path.join(d, "claude", "sessions")


def _exact_live_session_processes(d, session, record=None):
    """(matches, unavailable); zero is proof only when unavailable is None.

    `record` is the caller's already-read spawn record, which names the claude
    home its launch selected (`_session_record_root`).
    """
    from . import sessions
    found, unavailable = [], []
    root = _session_record_root(d, record)
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return [], "session record directory is unreadable: %s" % e
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            with pk.open_regular(path) as f:
                rec = json.load(f)
        except (OSError, ValueError, TypeError) as e:
            unavailable.append("%s: %s" % (os.path.basename(path), e))
            continue
        if not isinstance(rec, dict):
            unavailable.append("%s: record is not an object"
                               % os.path.basename(path))
            continue
        if rec.get("sessionId") != session:
            continue
        try:
            pid = int(rec.get("pid") or 0)
        except (ValueError, TypeError) as e:
            unavailable.append("%s: invalid pid (%s)"
                               % (os.path.basename(path), e))
            continue
        start = rec.get("procStart")
        if not pid or not start:
            unavailable.append("%s: matching record has no complete process "
                               "identity" % os.path.basename(path))
            continue
        if sessions._pid_is_claude(pid, start):
            found.append((pid, str(start)))
    detail = "; ".join(unavailable) if unavailable else None
    return found, detail


def _live_session_orca_identity(d, session, seat_name=None, record=None):
    """The exact live Claude process's remint-stable Orca identity.

    The pid-keyed Claude session record proves session -> process incarnation;
    only the non-secret seat/Orca identity keys are then selected from /proc.
    When a seat is named, the exact Claude process itself must claim it. A helper
    inheriting HELM_CHAT_NAME or CLAUDE_CODE_SESSION_ID can nominate a candidate,
    but it can never complete this proof. Full process environments can carry
    credentials and are never returned/logged.
    """
    found, unavailable = _exact_live_session_processes(d, session, record)
    if unavailable:
        return None, "session process census is UNKNOWN: " + unavailable
    if len(found) != 1:
        return None, NO_EXACT_LIVE_SESSION % (session, len(found))
    pid, proc_start = found[0]
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            env = f.read().split(b"\0")
    except OSError as e:
        return None, "live session environment is unreadable: %s" % e
    wanted = {b"HELM_CHAT_NAME", b"HELM_SEAT_STORAGE", b"ORCA_PANE_KEY",
              b"ORCA_WORKTREE_ID"}
    vals = {}
    for item in env:
        key, sep, value = item.partition(b"=")
        if sep and key in wanted:
            vals[key.decode("ascii")] = value.decode("utf-8", "replace")
    # A LAUNCH NAME AND A CANONICAL NAME ARE TWO FACTS, AND THIS PROCESS CAN
    # ONLY EVER EXPORT THE FIRST. `HELM_CHAT_NAME` is stamped into the environ
    # AT LAUNCH and never changes for the life of the process, while a durable
    # rename moves the CANONICAL name — so after one, an otherwise-valid
    # original process fails a raw equality against its own seat's current
    # name. What that cost is the SESSION-ANCHORED proof and the name
    # fallback: repair, rebind and stale-handle maintenance all stopped
    # reaching a process that was alive and was this seat's (task/2444).
    #
    # THE PROOF IS NOT WIDENED, ITS VOCABULARY IS — AND THE VOCABULARY IS THE
    # PRODUCER'S. `launch_line` emits BOTH names into every seat it starts:
    # `HELM_CHAT_NAME` is the CANONICAL identity at the moment of launch, and
    # `HELM_SEAT_STORAGE` is the immutable storage key the register is filed
    # under. A NAME PAIR CANNOT CARRY A GENERATION. Reading only the register's
    # two current names accepts the storage key and today's canonical name and
    # rejects everything between them: storage A renamed to B, a process
    # legitimately launched as B, then B renamed to C leaves the register
    # holding seat=A / identity=C, and the live process — still this seat's,
    # still session-anchored to this very register — claims B and is refused.
    # Repair, rebind and stale-handle maintenance then stop reaching a process
    # that is alive and is ours, which is the whole of task/2444.
    #
    # SO THE STORAGE KEY IS READ FROM THE PROCESS, NOT INFERRED FROM ITS NAME.
    # A process exporting this register's storage key was started for this
    # register whatever generation its name belongs to, and that is a fact the
    # launch line already publishes rather than one invented here. Nothing is
    # read from a roster alias, a rename window or a historical membership
    # list — a fresh claimant of a released name has a DIFFERENT session and
    # never reaches this register at all, and an expired window or
    # `alias_hours=0` changes nothing, because both facts are durable rather
    # than time-boxed.
    claimed = vals.get("HELM_CHAT_NAME")
    if seat_name and claimed != str(seat_name):
        rec = _spawn_record(d)
        launch = (rec or {}).get("seat") if isinstance(rec, dict) else None
        storage = vals.get("HELM_SEAT_STORAGE")
        mine = bool(launch) and (claimed == str(launch)
                                 or storage == str(launch))
        if not mine:
            return None, ("exact live Claude process for session %s does not "
                          "claim seat %r (it was launched as %r, for storage "
                          "%r)" % (session, seat_name, claimed, storage))
    pane_key = vals.get("ORCA_PANE_KEY")
    worktree_id = vals.get("ORCA_WORKTREE_ID")
    if not pane_key or not worktree_id:
        return None, "live session has no complete Orca pane/worktree identity"
    return {"pid": pid, "proc_start": proc_start, "pane_key": pane_key,
            "worktree_id": worktree_id}, None


def _prove_orca_replacement(d, rec, ad, rows, for_send=False,
                            identity_session=None, for_reap=False):
    """Prove one current Orca handle with the caller's required capability.

    `identity_session` is a measured live transcript session for a caller whose
    ordinary pane action deliberately tolerates a stale register session. It may
    locate a reminted handle but never rebinds the durable session; destructive
    callers omit it and remain anchored to the register exactly as before.

    Reaping needs identity, not a readable renderer. An exact live process ->
    pane-key -> handle/pty chain may therefore authorize a handle omitted from
    inventory, but only when inventory contains no row mentioning either resolved
    identity. Partial or duplicate matches remain contradictions and fail closed.
    """
    session = identity_session or rec.get("session")
    if not session:
        return None, None, "stale Orca handle has no bound session identity"
    identity, err = _live_session_orca_identity(d, session,
                                               _record_identity(rec), rec)
    if err:
        return None, None, err
    recorded_key = rec.get("pane_key")
    recorded_worktree = rec.get("worktree_id")
    if identity_session and identity_session != rec.get("session") \
            and (not recorded_key or not recorded_worktree):
        return None, None, ("spawn has no complete pane/worktree identity to "
                            "bind a mismatched live session")
    if recorded_key and recorded_key != identity["pane_key"]:
        return None, None, "spawn pane key conflicts with the live session"
    if recorded_worktree and recorded_worktree != identity["worktree_id"]:
        return None, None, "spawn worktree identity conflicts with the live session"
    try:
        resolved = ad.resolve_pane(identity["pane_key"])
    except Exception as e:
        return None, None, str(e)
    handle, pty = resolved.get("handle"), resolved.get("pty_id")
    if not handle or not pty:
        return None, None, "Orca pane-key resolution returned no handle/pty identity"
    fields = {"handle": handle, "pane_key": identity["pane_key"],
              "pty_id": pty, "worktree_id": identity["worktree_id"]}
    mentions = [row for row in rows if row.get("handle") == handle or
                row.get("pty_id") == pty]
    matches = [row for row in mentions if row.get("handle") == handle and
               row.get("pty_id") == pty and
               row.get("worktree_id") == identity["worktree_id"] and
               (_pane_sendable(row) if for_send else
                row.get("writable") is True and _pane_live(row))]
    if len(matches) == 1:
        return matches[0], fields, None
    if for_reap and not mentions:
        return None, fields, None
    capability = "sendable" if for_send else "read-live"
    return None, fields, ("Orca pane-key resolution matched %d %s inventory "
                          "rows; refusing replacement"
                          % (len(matches), capability))


#: What `_pane_claimant_states` can say about ONE candidate pane.
CLAIMANT_LIVE = "live"              # orca resolves the pane key to a handle
CLAIMANT_STALE_RECORD = "stale-record"   # orca ANSWERED: no such pane
CLAIMANT_UNKNOWN = "unknown"        # orca did not answer the question


def _pane_claimant_states(adapter, candidates):
    """{(pane_key, worktree_id): (CLAIMANT_*, detail)} for each candidate.

    A PROCESS IS NOT A PANE. `_live_seat_orca_identity` finds claimants by
    walking /proc for `HELM_CHAT_NAME=<seat>`, which is the right liveness
    source — a register row, a roster row and an orca title are all copyable
    presentations that authorize nothing. But a pane's SHELLS OUTLIVE IT: when
    orca closes a terminal, the beacon waiter and its `bash -c` parent keep
    running with the closed pane's `ORCA_PANE_KEY` stamped in their environ,
    and the census counted them as a second pane claiming the seat.

    MEASURED on the grok seat: after its pane was closed, `helm seat rebind
    grok --apply` refused "2 distinct live panes claim seat 'grok'" while orca
    listed ONE grok terminal. The second claimant was two orphaned shells of
    the closed pane — live pids, dead pane.

    So the second conjunct is asked HERE: a candidate counts as live only when
    its pane key still resolves in orca. A candidate orca says is gone is a
    STALE-RECORD, named by its source, and the census proceeds against what is
    left. Anything short of that answer is UNKNOWN and fails closed — missing
    evidence is not evidence of death, and this census feeds
    `rebind_refusal_means_dead`, whose consumer relaunches.

    THE THREE ANSWERS ARE THE ADAPTER'S, NOT THIS FUNCTION'S. `OrcaAdapter.
    resolve_pane` (helm/harness.py) has exactly three outcomes and each maps to
    one state:

      a terminal with a handle   LIVE. The reply named the pane.
      an error REPLY carrying    STALE-RECORD. `_runtime_call` raises every
      `terminal_not_found`       `ok: false` reply as a HarnessError holding
                                 orca's own message, and this code is the one
                                 message in which the HOST states that the pane
                                 does not exist. It arrives as an exception, so
                                 the exception is where it has to be read.
      anything else              UNKNOWN: a transport failure, a deadline, a
                                 different error reply — and a SUCCESSFUL reply
                                 with no handle in it, which `resolve_pane`
                                 builds from `.get("terminal") or {}` and which
                                 is orca declining to say, not orca saying no.

    `seat resume --all` already reads the contract this way
    (`seat_resume_all.PANE_NOT_FOUND`, and its "resolved without a handle" is
    UNKNOWN). The constant is read from there rather than copied, so the two
    readers of one host answer cannot drift apart.
    """
    from .seat_resume_all import PANE_NOT_FOUND
    out = {}
    for pane in candidates:
        key = pane[0]
        try:
            handle = (adapter.resolve_pane(key) or {}).get("handle")
        except Exception as e:                                 # noqa: BLE001
            if PANE_NOT_FOUND in str(e):
                out[pane] = (CLAIMANT_STALE_RECORD,
                             "orca answers %s for pane key %s"
                             % (PANE_NOT_FOUND, key))
            else:
                out[pane] = (CLAIMANT_UNKNOWN,
                             "orca could not be asked about pane key %s (%s)"
                             % (key, e))
            continue
        out[pane] = (CLAIMANT_LIVE, handle) if handle else (
            CLAIMANT_UNKNOWN,
            "orca resolved pane key %s without a handle" % key)
    return out


def _stale_claimant_note(seat_name, stale, pids):
    """One sentence naming each ghost and the source that still carries it."""
    return "; ".join(
        "STALE-RECORD for seat '%s': %s — still claimed by pid(s) %s "
        "(processes that outlived the pane, not a live claimant)"
        % (seat_name, detail,
           ", ".join(str(p) for p in sorted(pids.get(pane, ()))) or "(none)")
        for pane, detail in stale)


def _live_seat_orca_identity(d, seat_name, record=None, adapter=None):
    """The exact live Claude identity for one reboot-fallback seat candidate.

    HELM_CHAT_NAME-bearing processes may nominate a session and pane, but helper,
    beacon, and hook environments are inherited testimony. The candidate is
    accepted only when exactly one session id is nominated and the ONE session
    proof door resolves it to exactly one live Claude process which itself claims
    the seat and carries the same Orca identity. Missing, duplicate, conflicting,
    or helper-only candidates fail closed.

    `adapter` is the second half of the liveness question — see
    `_pane_claimant_states`. Without one the census is the /proc walk alone,
    which is what a caller holding no orca adapter can honestly answer.
    """
    if not seat_name:
        return None, "no seat name to match"
    want = ("HELM_CHAT_NAME=" + str(seat_name)).encode("utf-8")
    found = []
    for entry in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(entry, "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue                      # gone or not ours — never fatal
        if want not in env:
            continue
        vals = {}
        for item in env:
            key, sep, value = item.partition(b"=")
            if sep and key in (b"ORCA_PANE_KEY", b"ORCA_WORKTREE_ID",
                               b"CLAUDE_CODE_SESSION_ID"):
                vals[key.decode("ascii")] = value.decode("utf-8", "replace")
        if vals.get("ORCA_PANE_KEY") and vals.get("ORCA_WORKTREE_ID"):
            found.append({"pane_key": vals["ORCA_PANE_KEY"],
                          "worktree_id": vals["ORCA_WORKTREE_ID"],
                          "session": vals.get("CLAUDE_CODE_SESSION_ID"),
                          "pid": entry.split("/")[2]})
    panes = {(item["pane_key"], item["worktree_id"]) for item in found}
    stale_note = None
    if adapter is not None and panes:
        pids = {}
        for item in found:
            pids.setdefault((item["pane_key"], item["worktree_id"]),
                            set()).add(item["pid"])
        states = _pane_claimant_states(adapter, panes)
        unknown = [d2 for _p, (s, d2) in states.items() if s == CLAIMANT_UNKNOWN]
        if unknown:
            return None, ("pane census for seat '%s' is UNKNOWN: %s"
                          % (seat_name, "; ".join(sorted(unknown))))
        stale = sorted((p, d2) for p, (s, d2) in states.items()
                       if s == CLAIMANT_STALE_RECORD)
        live = {p for p, (s, _d) in states.items() if s == CLAIMANT_LIVE}
        if stale:
            stale_note = _stale_claimant_note(seat_name, stale, pids)
            print("helm seat: " + stale_note, file=sys.stderr)
        if stale and not live:
            # NOT the death sentence: `rebind_refusal_means_dead` matches that
            # one by equality and its consumer relaunches over it. Every
            # claimant being a ghost means the panes are gone while processes
            # of this seat are still running — a state for an operator, not for
            # an automatic relaunch.
            return None, ("every process claiming seat '%s' sits on a pane "
                          "orca reports not found: %s" % (seat_name, stale_note))
        panes = live
    if len(panes) != 1:
        return None, NO_DISTINCT_LIVE_PANE % (len(panes), seat_name)
    sessions = {item["session"] for item in found
                if item.get("session")
                and (item["pane_key"], item["worktree_id"]) in panes}
    if len(sessions) != 1:
        detail = "conflicting sessions" if len(sessions) > 1 else "no session id"
        return None, ("live pane for seat '%s' carries %s; refusing to guess "
                      "which exact live Claude process owns it"
                      % (seat_name, detail))
    session = next(iter(sessions))
    identity, err = _live_session_orca_identity(d, session, seat_name, record)
    if err:
        return None, ("candidate session %s for seat '%s' has no unique exact "
                      "live Claude proof: %s" % (session, seat_name, err))
    pane_key, worktree_id = next(iter(panes))
    if identity["pane_key"] != pane_key \
            or identity["worktree_id"] != worktree_id:
        return None, ("candidate session %s conflicts with the exact live Claude "
                      "process Orca identity" % session)
    out = dict(identity, session=session)
    if stale_note:
        # A REPORT, never a register field: `_prove_orca_rebind` builds the
        # written `fields` from named keys, so the ghost's name reaches the
        # operator without reaching spawn.json.
        out["stale_claimants"] = stale_note
    return out, None


def _prove_orca_rebind(d, rec, ad, rows):
    """Prove a seat's CURRENT pane after a REBOOT, where the old pane is gone.

    WHY THIS IS NOT _prove_orca_replacement. That one repairs a handle change
    WITHIN a pane generation, so it guards `pane_key`/`worktree_id` drift — a
    changed pane key there means something redirected the seat and it refuses.
    A REBOOT changes both LEGITIMATELY: the machine came back, orca minted new
    panes, and every cached key in the register describes a pane that no longer
    exists. Feeding that case to the repair path returns "spawn pane key
    conflicts with the live session" — correct for its own question, and the
    reason a reboot leaves the whole register unusable.

    MEASURED 2026-07-28, the morning after an Ubuntu reboot: `helm seat where`
    reported 6 of 7 seats GONE while ds4pro and gemini were demonstrably alive
    and posting in chat. The seats never died; the REGISTER did. helm keyed a
    durable thing (the seat) on an ephemeral one (the orca handle) with no
    reconstruction path across a boot — the same shape as the chat-node data
    dir that tmpfs wiped and nothing re-inited.

    WHAT STILL ANCHORS IDENTITY, because dropping two guards must not drop
    safety: the primary path requires the recorded sessionId to match a LIVE
    claude process (pid + procStart proven). A reboot that legitimately minted
    a new session reads that id from the same exact live process environment as
    its Orca keys; disagreement across processes refuses. Titles and cwds remain
    non-authoritative, and the inventory match below still demands exactly one
    connected, writable row. pane_key and worktree_id were only ever cached
    copies of facts that the boot invalidated; comparing them across a reboot
    compares a seat to its own dead past."""
    session = rec.get("session")
    bound_session = session
    identity, err = (None, "seat has no bound session identity to rebind from")
    if session:
        identity, err = _live_session_orca_identity(d, session,
                                                   _record_identity(rec), rec)
    if err:
        # SESSION-ANCHORED PROOF CANNOT REACH A SEAT THAT CAME BACK ON A NEW
        # SESSION, which is the common reboot case: the pane was respawned
        # rather than resumed, so the register's sessionId names a process that
        # no longer exists ("0 exact live Claude processes"). Measured
        # 2026-07-28: gemini and codex resolved by session, while ds4pro, grok
        # and kimi refused — and ds4pro was provably alive the whole time.
        #
        # Fall back to the seat's OWN NAME as carried by its live process.
        # HELM_CHAT_NAME is stamped into the environ at launch and is exactly
        # as non-forgeable as the session route: it is read from /proc of a
        # live pid, not from a title, a cwd, or an inventory label — the
        # copyable-presentation sources that authorize nothing.
        # THE ADAPTER RIDES IN, because this caller holds one and the census
        # cannot ask orca whether a claimant's pane still exists without it:
        # a closed pane's surviving shells keep `HELM_CHAT_NAME` and its dead
        # `ORCA_PANE_KEY`, and were counted as a second live pane.
        identity, name_err = _live_seat_orca_identity(
            d, _record_identity(rec), rec, adapter=ad)
        if identity is None:
            # Report the SESSION error, not the fallback's: the session route is
            # the primary and its message says which seat and how many
            # candidates. Appending the fallback's reason keeps both visible.
            return None, None, "%s; and %s" % (err, name_err)
        bound_session = identity.get("session")
        if not bound_session:
            return None, None, ("%s; and the current live process carries no "
                                "session id to bind" % err)
    try:
        resolved = ad.resolve_pane(identity["pane_key"])
    except Exception as e:
        return None, None, str(e)
    handle, pty = resolved.get("handle"), resolved.get("pty_id")
    if not handle or not pty:
        return None, None, "Orca pane-key resolution returned no handle/pty identity"
    matches = [row for row in rows if row.get("handle") == handle and
               row.get("pty_id") == pty and
               row.get("writable") is True and _pane_live(row)]
    if len(matches) != 1:
        return None, None, ("Orca pane-key resolution matched %d connected, "
                            "writable inventory rows; refusing rebind"
                            % len(matches))
    fields = {"handle": handle, "pane_key": identity["pane_key"],
              "pty_id": pty, "worktree_id": identity["worktree_id"]}
    if bound_session:
        fields["session"] = bound_session
    return matches[0], fields, None


def rebind_refusal_means_dead(err, rec):
    """Whether one `rebind_seat` refusal says NO process holds this seat.

    `_prove_orca_rebind` refuses for many reasons — two panes claiming one
    name, a live process whose pane is not in inventory, an RPC failure — and
    only ONE of them is evidence of death: the session route found ZERO exact
    live Claude processes for the recorded session AND the name route found
    ZERO live panes claiming the seat, joined the way `_prove_orca_rebind`
    joins them. Matched by EQUALITY against the producer's own constants, so a
    count of 2 (ambiguity), a missing session ("no bound session identity"),
    or any other sentence reads as not-dead — the caller renders those UNKNOWN
    and never relaunches over them. Measured 2026-08-22 after a reboot: all 9
    registered seats refused with exactly this sentence pair, and a sweep with
    no actuator behind it printed it every five minutes."""
    if not isinstance(rec, dict) or not rec.get("session") or not rec.get("seat"):
        return False
    want = "%s; and %s" % (NO_EXACT_LIVE_SESSION % (rec["session"], 0),
                           NO_DISTINCT_LIVE_PANE %
                           (0, _record_identity(rec)))
    return err == want


def rebind_seat(seat_name, d, ad, apply=False, locked=False):
    """Re-point one seat's register at the pane its LIVE process occupies now.

    Returns (fields, err). apply=False proves the rebind and reports it without
    writing — the dry-run default every destructive helm verb carries, because
    a register rewrite is not observable after the fact. locked=True says the
    caller already holds this seat's lifecycle lock (`.spawn.lock`, the same
    file): flock is per open-file-description, so taking it again from the
    same process on a fresh fd is a self-deadlock, not a re-entry — measured
    2026-08-22, `seat resume <seat>` hung forever consulting the sweep's
    proof from inside its own lock.
    """
    import fcntl
    from . import pk

    def bind_runtime(session):
        try:
            rec = _spawn_record(d) or {}
            err = _bind_runtime_session(
                _record_identity(rec), session, storage_seat=seat_name)
        except Exception as e:
            return "rebind roster stamp failed: %s" % e
        return "rebind roster stamp failed: %s" % err if err else None

    def run():
        rec = _spawn_record(d)
        if not rec or rec.get("seat") != seat_name:
            return None, "no spawn register for this seat"
        if rec.get("harness") != "orca":
            return None, "rebind is orca-only (harness: %s)" % rec.get("harness")
        try:
            rows = ad.list()
        except Exception as e:
            return None, str(e)
        pane_row, fields, err = _prove_orca_rebind(d, rec, ad, rows)
        if err:
            return None, err
        # THE OTHER RE-STAMP, on the surface the OWNER reads. A reboot
        # invalidates the register AND reverts every hand-set pane title to an
        # orca-generated summary, so the verb that repairs one repairs the
        # other — this IS the reboot verb, and `seat resume --all` reaches it
        # through this same call, so one hook serves both. It costs nothing:
        # the seat is already PROVEN and `pane_row` is the inventory row that
        # proof matched, so there is no /proc walk and no extra CLI call. The
        # returned value is a REPORT and never enters `fields`, which is
        # what the register is updated from. It is `title_note` and not
        # `title` because it holds a SENTENCE ABOUT the title, never a title —
        # the pane-title read census in tests/test_orcatitle.py flagged the
        # shorter name here, correctly: in a pane-identity module a key called
        # `title` reads as a pane title to every scanner and every human.
        from . import orcatitle
        # THE IDENTITY, NEVER THE STORAGE KEY. A durable rename keeps the
        # register keyed by the ORIGINAL name and carries the canonical one in
        # `identity`; `_record_identity` is the single reader of that pair and
        # `bind_runtime` three lines above already uses it. Stamping
        # `seat_name` here would take a pane the spawn had titled correctly and
        # overwrite it with the stale label on the next rebind — a title that
        # disagrees with the board is worse than no title, because the owner
        # reads it as a checksum.
        title_note = orcatitle.note(
            orcatitle.restamp_one(ad, _record_identity(rec), pane_row,
                                  apply=apply))
        if all(rec.get(key) == value for key, value in fields.items()):
            if apply:
                err = bind_runtime(fields.get("session"))
                if err:
                    return None, err
            return dict(fields, unchanged=True, title_note=title_note), None
        if not apply:
            return dict(fields, dry_run=True, title_note=title_note), None
        rec.update(fields)
        try:
            pk.write_json(_spawn_path(d), rec)
        except OSError as e:
            return None, "rebind write failed: %s" % e
        err = bind_runtime(fields.get("session"))
        return (None, err) if err else (dict(fields,
                                                 title_note=title_note), None)

    if locked:
        return run()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return run()
    except OSError as e:
        return None, "rebind lock failed: %s" % e


def _repair_orca_handle(seat_name, d, ad, old_handle, locked=False,
                        for_send=False, identity_session=None, for_reap=False):
    """Re-prove and atomically replace one stale Orca handle in spawn.json."""
    import fcntl
    from . import pk

    def repair():
        rec = _spawn_record(d)
        if not rec or rec.get("seat") != seat_name or \
                rec.get("harness") != "orca":
            return None, "spawn identity changed before pane repair"
        if rec.get("handle") not in (old_handle,):
            return None, "spawn handle changed before pane repair"
        try:
            rows = ad.list()
        except Exception as e:
            return None, str(e)
        replacement, fields, err = _prove_orca_replacement(
            d, rec, ad, rows, for_send=for_send,
            identity_session=identity_session, for_reap=for_reap)
        if err:
            return None, err
        rec.update(fields)
        try:
            pk.write_json(_spawn_path(d), rec)
        except OSError as e:
            return None, "spawn handle repair write failed: %s" % e
        return (fields["handle"], replacement), None

    if locked:
        return repair()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return repair()
    except OSError as e:
        return None, "spawn handle repair lock failed: %s" % e


def _adopted_pane(seat_name, adapter, for_send, for_reap, unknown):
    """(adapter, handle, detail) for a metaharness-launched seat — OBSERVATION
    ONLY, and the asymmetry is the whole point.

    READING a pane needs a renderer and nothing else, so an adopted seat whose
    pane resolves is exactly as readable as a registered one and there is no
    reason a liveness read should be blind to it.

    ACTUATION IS NOT WIDENED HERE. `orcaadopt` owns its own send ladder —
    session evidence, process identity, an authorized handle — and answering
    `for_send` from this door would put a second, weaker path to a keystroke
    beside it. A caller that wants to type into an adopted seat goes through
    `orcaadopt.send_to_pane`, and the refusal below says so rather than
    leaving them to guess which door exists.

    A NAME HELM HAS NEVER HEARD OF KEEPS ITS ORIGINAL REFUSAL, so a typo still
    looks like a typo instead of becoming a confusing pane error.
    """
    from . import orcaadopt
    if for_send or for_reap:
        return None, None, (
            "%r is metaharness-adopted: reads resolve here, but a send or a "
            "reap must go through orcaadopt's own authorization ladder "
            "(orcaadopt.send_to_pane), not this register" % seat_name)
    try:
        info = orcaadopt.resolve(seat_name, adapter=adapter)
    except Exception as e:                  # noqa: BLE001 — a probe error is a reason
        return None, None, ("adopted-pane resolve failed for %r: %s: %s"
                            % (seat_name, e.__class__.__name__, e))
    if not info:
        return None, None, unknown         # never heard of it: keep the typo message
    handle = info.get("handle")
    if not handle:
        return None, None, ("%r is metaharness-adopted (%s) but no pane "
                            "resolves for it: %s"
                            % (seat_name, info.get("state") or "UNKNOWN",
                               info.get("evidence") or "no evidence recorded"))
    from . import harness
    ad = adapter if adapter is not None else harness.detect()
    if ad is None:
        return None, None, ("%r is metaharness-adopted but no metaharness is "
                            "detectable here, so its pane cannot be read"
                            % seat_name)
    return ad, handle, ("adopted pane %s via %s — %s"
                        % (handle, ad.name, info.get("provenance") or "adopted"))


def _resolve_registered_pane(seat_name, d=None, adapter=None, repair=True,
                             locked=False, for_send=False,
                             identity_session=None, for_reap=False):
    """Resolve the one pane authorized by this seat's spawn register.

    `for_send=True` resolves for ACTUATION rather than observation: an orphaned
    pane is accepted when it is still writable, because a send needs a PTY and
    not a renderer (see `_pane_sendable`). `for_reap=True` is narrower still:
    closing needs only the exact registered/live-process pane identity, so an
    inventory-omitted handle may be returned while every inventory contradiction
    still refuses. Neither capability weakens identity; each asks only for the
    substrate property its operation actually consumes.

    The register is the durable identity owner shared by every pane actuator
    (autocompact injection, resume, and duplicate-seat reap). Titles and pane
    content are mutable/copyable presentation and never authorize a write or
    stop. Orca handle remints are recovered through an exact live session's PID
    -> ORCA_PANE_KEY -> runtime resolvePane chain. By default that is the durable
    registered session; `identity_session` lets a non-destructive caller prove a
    newer measured transcript session without rebinding the register. The handle
    alone is atomically repaired. Returns (adapter, handle, detail);
    adapter/handle are both non-None only after identity is proven.
    """
    from . import harness
    family, err = _seat_family(seat_name)
    if err:
        # A NATIVE CLAUDE SEAT HAS NO FAMILY AND NO SPAWN RECORD, and this
        # refusal is why nothing could read one. `_seat_family` knows the
        # PROXY families only, so every pane-tail consumer — the liveness
        # read, the plan-prompt probe, the compact watchdog — was told
        # "unknown seat" for three claude seats and stopped there. The seats
        # that sat parked at a vendor dialog for seventeen hours were exactly
        # the three the readers could not address.
        #
        # `seat where` ALREADY HAD THE FALLBACK and kept it to itself. This
        # moves it down to the shared resolver so there is ONE join rather
        # than two of unequal power — the defect `orcaadopt` records against
        # itself, where `seat where` said LIVE with a resolved handle in the
        # same second `seat panes` called the pane unowned.
        return _adopted_pane(seat_name, adapter, for_send, for_reap, err)
    d = d or _instance_dir(family, seat_name)
    rec = _spawn_record(d)
    if not rec:
        return None, None, ("no authoritative spawn handle for %r; run `helm "
                            "seat spawn`/`resume` to register it" % seat_name)
    if rec.get("seat") != seat_name:
        return None, None, "spawn record identity mismatch for %r" % seat_name
    recorded_harness = rec.get("harness")
    if recorded_harness == "headless":
        return None, None, ("seat is registered headless (pid %s); no pane "
                            "input channel exists" % rec.get("pid"))
    handle = rec.get("handle")
    if not recorded_harness or not handle:
        return None, None, "spawn record for %r has no pane identity" % seat_name

    ad = adapter if adapter is not None and \
        adapter.name == recorded_harness else None
    if ad is None:
        detected = harness.detect()
        ad = detected if detected is not None and \
            detected.name == recorded_harness else None
    if ad is None:
        cls = harness.ADAPTERS.get(recorded_harness)
        path = shutil.which(cls.bin) if cls else None
        if not path:
            return None, None, "recorded %s adapter is unavailable" \
                % recorded_harness
        ad = cls(path)
    try:
        rows = ad.list()
    except harness.HarnessError as e:
        return None, None, str(e)
    row = next((row for row in rows if row.get("handle") == handle), None)
    if row is not None and _pane_live(row):
        return ad, handle, "registered pane %s via %s" % (handle, ad.name)
    # ACTUATION accepts what OBSERVATION must not: an orphaned pane is
    # read-blind and send-capable, so a caller that only needs to WRITE is not
    # made to prove a renderer it will never use. Said explicitly in the detail
    # so an operator reading a log knows this send went to a pane nobody can
    # read back — the send is authorized, the confirmation is not available.
    if for_send and row is not None and _pane_sendable(row):
        return ad, handle, ("registered pane %s via %s — %s"
                            % (handle, ad.name, _SEND_ONLY_DETAIL))
    status = str((row or {}).get("status") or "").strip().lower()
    if for_reap and row is not None and status not in \
            ("disconnected", "closed", "gone", "exited", "dead"):
        return ad, handle, "registered pane %s via %s" % (handle, ad.name)
    # Name the field that DECIDED the rejection, never the field that is fine.
    # For an orphaned pane `status` is "connected" — printing it tells the
    # operator the pane is connected (true and useless) and hides that it is
    # orphaned (the whole finding), which is the exact false diagnosis this
    # lane exists to fix. orca's `orphaned` is the decisive field; say so.
    if row is not None and row.get("orphaned"):
        why = "orphaned (PTY has no live renderer)"
    else:
        why = (row or {}).get("status") or "not live"
    stale = "registered handle %s is %s on %s" % (handle, why, ad.name)
    if ad.name == "orca" and hasattr(ad, "resolve_pane"):
        if not repair:
            replacement, fields, replacement_err = _prove_orca_replacement(
                d, rec, ad, rows, for_send=for_send,
                identity_session=identity_session, for_reap=for_reap)
            if not replacement_err and fields:
                current = fields["handle"]
                capability = (" — " + _SEND_ONLY_DETAIL
                              if for_send and replacement and
                              replacement.get("orphaned") else "")
                return ad, current, ("%s; identity-proven replacement pane %s%s "
                                     "(register unchanged in dry-run)"
                                     % (stale, current, capability))
        else:
            repaired, replacement_err = _repair_orca_handle(
                seat_name, d, ad, handle, locked=locked, for_send=for_send,
                identity_session=identity_session, for_reap=for_reap)
            if repaired:
                current, replacement = repaired
                capability = (" — " + _SEND_ONLY_DETAIL
                              if for_send and replacement and
                              replacement.get("orphaned") else "")
                return ad, current, ("%s; repaired spawn handle to %s via exact "
                                     "session pane-key identity%s"
                                     % (stale, current, capability))
        if replacement_err:
            stale += "; replacement unavailable: " + replacement_err
            if for_reap:
                return None, None, stale
    return ad, None, stale


_TERMINAL_PANE_STATES = ("disconnected", "closed", "gone", "exited", "dead")


def _dead_registered_pane_reason(d, rec, ad):
    """Positive terminal proof; a proved-live pane remains eligible for stop."""
    if not rec:
        return None, None
    if ad is None or ad.name != rec.get("harness"):
        return None, "registered pane adapter is unavailable or mismatched"
    from . import seat_exit_owner
    handle = rec.get("handle")
    state, inventory = seat_exit_owner.pane_inventory_state(ad, handle)
    if state == "unknown":
        return None, inventory
    if state == "live":
        return None, None
    if ad.name != "orca":
        return inventory, None
    reason, unavailable = _stopped_orca_absence_proof(
        d, rec, handle, live_is_nonterminal=state == "absent")
    if unavailable:
        return None, unavailable
    if not reason:
        return None, None
    return inventory + " and " + reason, None


def _stopped_orca_absence_proof(d, rec, handle, live_is_nonterminal=False):
    session = rec.get("session")
    if session is None:
        return None, ("registered handle %s is absent from orca but the spawn "
                      "record has no session census key" % handle)
    found, unavailable = _exact_live_session_processes(d, session, rec)
    if unavailable:
        return None, "session process census is UNKNOWN: " + unavailable
    if found:
        if live_is_nonterminal:
            return None, None
        return None, ("registered handle %s is absent from orca but session %s "
                      "still has %d exact live Claude process%s"
                      % (handle, session, len(found),
                         "" if len(found) == 1 else "es"))
    return ("registered handle %s is absent from orca and session %s has zero "
            "exact live Claude processes" % (handle, session)), None


def _reminted_pane_fields(d, rec, ad, handle):
    """(fields, err): the register fields a reminted Orca pane carries, PROVEN
    AND NOT WRITTEN.

    `_resolve_registered_pane(repair=False)` answers with the handle alone. A
    reap that later archives the runtime owes the archive the whole identity of
    the pane it stopped, so the same proof is taken again here and must name
    the same handle — a second answer that disagrees with the first is a pane
    that moved between two reads, and that refuses.
    """
    try:
        rows = ad.list()
    except Exception as e:
        return None, str(e)
    _row, fields, err = _prove_orca_replacement(d, rec, ad, rows, for_reap=True)
    if err:
        return None, err
    if fields["handle"] != handle:
        return None, ("pane-key resolution moved from %s to %s during the reap"
                      % (handle, fields["handle"]))
    return fields, None


def _reap_stale(seat_name, d, ad, allow_live=False, locked=False,
                defer_orca_terminal=False):
    """Terminate and archive only the exact runtime named by spawn.json.

    A hand resume defers a passively terminal Orca occurrence until the
    stronger reboot-death proof runs under the lifecycle lock. Spawn/replace
    callers keep the ordinary eager archival path.

    DECIDE FROM READ-ONLY EVIDENCE, THEN WRITE. Every caller treats a returned
    error as "nothing about this seat changed" — `_spawn_reap` prints that the
    register was not re-pointed, and a hand resume feeds the same register to
    its death proof. So no rung that can still return an error may have written
    first: the pane is resolved WITHOUT repairing the register, a reminted
    handle is proven and carried as `remint` rather than persisted, and the
    title-only refusal is answered before the archive instead of after it. The
    register and its archive are written in exactly one place, `close_runtime`,
    and only once the runtime it names is positively terminal — a stop that
    fails or cannot be proven leaves both byte-identical.
    """
    from . import seat_exit_owner

    notes, errors = [], []

    def close_runtime(current, reason, remint=None):
        if remint:
            if _spawn_record(d) != current:
                return [], ["spawn identity changed before pane repair"]
            current = dict(current, **remint)
            try:
                pk.write_json(_spawn_path(d), current)
            except OSError as e:
                return [], ["spawn handle repair write failed: %s" % e]
        return seat_exit_owner.close_spawn(seat_name, d, current, reason)

    rec = _spawn_record(d)
    if rec and rec.get("seat") != seat_name:
        return notes, ["spawn record identity mismatch for %r; refusing reap"
                       % seat_name]
    pid = (rec or {}).get("pid")
    if (rec or {}).get("harness") == "headless" and pid:
        live = _recorded_pid_alive(rec)
        if live is None:
            return notes, [
                "stale headless %s pid %s is live but its process identity is "
                "unverifiable; refusing to kill it" % (seat_name, pid)]
        if live and not allow_live:
            return notes, [
                "registered headless %s pid %s is LIVE; `seat spawn` refuses "
                "implicit replacement — pass --replace" % (seat_name, pid)]
        if not live:
            closed_notes, closed_errors = close_runtime(
                rec, "recorded headless process is absent")
            return notes + closed_notes, errors + closed_errors
        try:
            terminal, unavailable = seat_exit_owner.terminate_process(
                pid, lambda: _recorded_pid_alive(rec))
            if unavailable:
                errors.append("stale headless %s pid %s NOT reaped (%s)"
                              % (seat_name, pid, unavailable))
            else:
                notes.append("reaped stale headless %s (pid %d)"
                             % (seat_name, pid))
                closed_notes, closed_errors = close_runtime(rec, terminal)
                notes.extend(closed_notes)
                errors.extend(closed_errors)
        except OSError as e:
            errors.append("stale headless %s pid %s NOT reaped (%s)"
                          % (seat_name, pid, e))
        return notes, errors

    pane_ad, handle, detail = _resolve_registered_pane(
        seat_name, d=d, adapter=ad, locked=locked, for_reap=True,
        repair=False)
    if rec:
        current = _spawn_record(d)
        if not current or current.get("seat") != seat_name:
            return notes, ["spawn identity changed during pane resolution"]
        rec = current
    remint = None
    if rec and handle is not None and handle != rec.get("handle"):
        remint, remint_err = _reminted_pane_fields(d, rec, pane_ad, handle)
        if remint_err:
            return notes, ["recorded pane cannot be checked or reaped: "
                           + remint_err]
    # What the register WOULD say once repaired — every terminal-state question
    # below is about the pane the seat occupies now, asked of a projection the
    # disk never sees.
    view = dict(rec, **remint) if remint else rec
    adapters = []
    if pane_ad is not None:
        adapters.append(pane_ad)
    if ad is not None and all(a.name != ad.name for a in adapters):
        adapters.append(ad)
    title_only = []
    for adapter in adapters:
        try:
            rows = adapter.list()
        except Exception as e:
            errors.append("%s pane scan failed (%s)" % (adapter.name, e))
            continue
        title_only.extend(row.get("handle") for row in rows
                          if row.get("handle") and row.get("handle") != handle
                          and row.get("title") == seat_name)
    title_error = ("unregistered pane(s) %s have mutable title %r; refusing "
                   "identity-by-title reap" % (", ".join(title_only), seat_name)) \
        if title_only else None
    if rec and rec.get("harness") not in (None, "headless") and \
            (ad is None or ad.name != rec.get("harness")):
        errors.append("recorded pane cannot be checked or reaped: " + detail)
        return notes, errors
    dead_reason, dead_unavailable = _dead_registered_pane_reason(
        d, view, pane_ad or ad)
    if dead_reason:
        if defer_orca_terminal and rec.get("harness") == "orca":
            errors.append("recorded Orca pane terminal proof is deferred to "
                          "the under-lock reboot-death admission: " + dead_reason)
            return notes, errors
        if title_error:
            errors.append(title_error)
            return notes, errors
        closed_notes, closed_errors = close_runtime(rec, dead_reason, remint)
        notes.extend(closed_notes)
        errors.extend(closed_errors)
        return notes, errors
    if dead_unavailable:
        errors.append("recorded pane terminal state is UNKNOWN: " +
                      dead_unavailable)
        return notes, errors
    if title_error:
        errors.append(title_error)
        return notes, errors
    if rec and rec.get("harness") not in (None, "headless") and pane_ad is None:
        errors.append("recorded pane cannot be checked or reaped: " + detail)
        return notes, errors
    if handle is not None and not allow_live:
        errors.append("registered pane %s for %s is LIVE; `seat spawn` refuses "
                      "implicit replacement — pass --replace"
                      % (handle, seat_name))
        return notes, errors
    if rec and handle is None:
        errors.append("recorded pane terminal state is unproven; refusing to "
                      "overwrite its identity record")
        return notes, errors
    if handle is not None:
        try:
            proof = None if pane_ad.name != "orca" else lambda: \
                _stopped_orca_absence_proof(d, rec, handle)
            closed_reason, unavailable = seat_exit_owner.stop_pane(
                pane_ad, handle, absent_proof=proof)
            if unavailable:
                errors.append("stale %s pane %s terminal state NOT proven (%s)"
                              % (seat_name, handle, unavailable))
            else:
                notes.append("reaped stale %s pane %s" % (seat_name, handle))
                closed_notes, closed_errors = close_runtime(
                    rec, closed_reason, remint)
                notes.extend(closed_notes)
                errors.extend(closed_errors)
        except Exception as e:
            errors.append("stale %s pane %s NOT reaped (%s)"
                          % (seat_name, handle, e))
    return notes, errors


def _headless_spawn(launch_sh, onboarding, cwd, log_path, role="worker",
                    token=None):
    """The standalone path: launch.sh detached (start_new_session=True = its
    own setsid session — survives this CLI and any parent pane), stdin from
    /dev/null, stdout+stderr appended to spawn.log (the nohup shape). The
    onboarding is launch.sh's POSITIONAL ARG: the script execs
    `claude … "$@"`, so the prompt lands as the seat's first turn at boot —
    the launch-time delivery, since headless has no pane to inject into.

    Lead posture is launch state, not a family default: a lead gets Claude
    Code's durable ultracode setting plus HELM_SEAT_ROLE=lead; a worker strips
    any marker inherited from the operator launching it.

    `token` is the spawn ATTEMPT TOKEN; it is SET (never inherited) in the
    child's env, because the process running `seat spawn` may itself be a
    spawned seat carrying its own attempt's token, and a child that inherited
    that would present itself to every hook as the child of the wrong spawn."""
    env = _launch_env(role)
    if token:
        env[SPAWN_ATTEMPT_ENV] = str(token)
    else:
        env.pop(SPAWN_ATTEMPT_ENV, None)
    with open(log_path, "ab") as log, open(os.devnull, "rb") as devnull:
        p = subprocess.Popen(
            _launch_argv(launch_sh, role, tail=(onboarding,)), cwd=cwd,
            stdin=devnull, stdout=log, stderr=log, start_new_session=True,
            env=env)
    return p.pid


def _register_spawn(storage_seat, identity, d, rec):
    """Write the storage-keyed spawn.json, then canonical roster mirror.
    False means the spawn must be torn back down: an unregistered headless
    process cannot be found safely for the next duplicate-name reap."""
    from . import pk
    rec = dict(rec, seat=storage_seat, identity=identity)
    try:
        pk.write_json(_spawn_path(d), rec)
    except OSError as e:
        print("helm seat: spawn register write failed (%s): %s"
              % (_spawn_path(d), e), file=sys.stderr)
        return False
    try:
        from . import seats as _seats
        # Provenance rides into the mirror: a derived room stays derived (it
        # may NEVER overwrite an explicit/operator home — write_roster's law);
        # no room and no opinion writes NO home (join derives the real one at
        # SessionStart); and a room the spawn MEASURED away rides as `cleared`,
        # which removes a derived-tier home instead of leaving it standing.
        # That last case used to arrive as (None, None) and hit write_roster's
        # `elif home_room:` — a no-op — so the roster kept a home the launch
        # script had already dropped, and the two surfaces disagreed about
        # where the seat lived.
        _seats.write_roster(
            identity, cwd=rec.get("worktree"), home_room=rec.get("room"),
            home_room_source=_seats.ROOM_CLEARED
            if not rec.get("room")
            and rec.get("room_source") == _seats.ROOM_CLEARED
            else None if not rec.get("room")
            else "derived" if rec.get("room_source") == "derived"
            else "explicit", presence_beat=False)
        err = _bind_runtime_session(
            identity, rec.get("session"), storage_seat=storage_seat)
        if err:
            raise OSError(err)
    except Exception as e:
        print("helm seat: chat-roster mirror skipped (%s) — spawn.json is "
              "still authoritative for `helm seat where`" % e, file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# THE ONBOARDING REGISTER: ONE WRITER, ONE READER DOOR.
#
# The fields below travel TOGETHER or not at all. An outcome and a timestamp
# arriving as INDEPENDENT optionals leave every consumer to decide for itself
# what a missing half means, and those decisions do not agree — the same defect
# appears at each reader wearing that reader's face. The property that removes
# it is not another check at another reader: the register is parsed ONCE, into
# a CLOSED result, and no consumer may reinterpret a half.
#
# Closed means exactly three kinds and nothing else:
#
#   ABSENT   the record predates this field. LEGACY: every ladder behaves as
#            it did before the field existed, which is the reading every
#            register written by an older build gets.
#   VALID    ALL THREE keys, a recognised outcome, and a CANONICAL non-future
#            stamp. Carries `proven` and an ABSOLUTE `event_at`.
#   INVALID  present and unreadable, for a NAMED reason. Authorizes nothing,
#            and is loud rather than silent.
#
# ABSENT AND INVALID ARE NOT THE SAME READING, and collapsing them is the
# failure this door exists to prevent. Absent is a fact about a build; invalid
# is a fact about a record. Reading invalid as absent makes a corrupt register
# silent — the exact shape of the failure this whole subject started from, a
# seat no instrument surfaces.
# ---------------------------------------------------------------------------

ONBOARDING_ABSENT = "absent"
ONBOARDING_VALID = "valid"
ONBOARDING_INVALID = "invalid"

#: The ONE stamp format. The writer below emits it and the parser accepts
#: exactly what strftime produces from it — CANONICAL, not merely parseable,
#: because strptime is lenient enough to accept spellings this writer can
#: never emit. There is deliberately no second format, no heuristic and no
#: fallback: a stamp that is not this one is INVALID, never guessed at.
ONBOARDING_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: How far ahead of `now` a stamp may sit before it is INVALID. Writer and
#: reader share one host clock, so this is not a distributed-skew allowance —
#: it is ordinary NTP slew. Beyond it the record is not slightly-off, it is
#: describing an event that has not happened, and a negative age would flow
#: into comparisons that assume elapsed time.
ONBOARDING_FUTURE_GRACE_S = 60

class Onboarding(collections.namedtuple(
        "Onboarding", "kind proven event_at proof reason")):
    """The closed result. `proven` and `event_at` are populated ONLY for VALID;
    `reason` ONLY for INVALID. A consumer that reads `event_at` without first
    reading `kind` is reading None and has learned nothing — which is why the
    kind comes first in the tuple.

    THE PREDICATES EXIST SO NO CONSUMER TRANSCRIBES A KIND. A ladder that
    wrote `kind == "invalid"` would own a copy of this vocabulary, and a copy
    drifts; `unreadable` also lets a pure function ask the question without
    importing the module that defines the answer.
    """

    __slots__ = ()

    @property
    def unreadable(self):
        """PRESENT and does not parse — a positive contradiction."""
        return self.kind == ONBOARDING_INVALID

    @property
    def readable(self):
        """A usable reading: legacy-absent or fully valid."""
        return self.kind in (ONBOARDING_ABSENT, ONBOARDING_VALID)

    @property
    def refused(self):
        """A RECORDED, READ refusal: the launch tried to brief and could not
        prove it. Never true for ABSENT (nobody wrote it) and never for
        INVALID (nobody can read it) — only a fully valid record whose outcome
        is one of the two refusals says this."""
        return self.kind == ONBOARDING_VALID and self.proven is False

#: The legacy reading, shared so identity comparison works in the arms.
ONBOARDING_NONE = Onboarding(ONBOARDING_ABSENT, None, None, None, None)


def onboarding_invalid(reason):
    """The INVALID reading, built by the DOOR or by its loader.

    THE LOADER NEEDS THIS AND IT IS NOT A LEAK. "The register file is there and
    cannot be read" is a positive contradiction about the same record, and it
    is the door's vocabulary that must express it — the alternative is a loader
    inventing its own second reading, which is the drift this whole door exists
    to end.
    """
    return Onboarding(ONBOARDING_INVALID, None, None, None, reason)


def parse_onboarding(rec, now=None):
    """Parse a spawn record's onboarding fields into ONE closed result.

    PURE ON ITS INPUT. It opens nothing, so a caller owns the read, the
    identity guard and the exception boundary; this door owns only the schema.
    `now` is injectable because a rule about the future must be testable
    without waiting for one.

    NO FALLBACK, BY CONSTRUCTION. There is no reading of `ts` when the stamp is
    missing, no clamp of an unreadable age to zero, and no consumer-supplied
    default: each of those turns "I cannot tell" into a confident number, and a
    confident wrong number is what an operator acts on. Missing is INVALID and
    says so.
    """
    from . import harness
    if not isinstance(rec, dict):
        # No record at all is not a statement about onboarding. The caller
        # already treats a missing register as legacy everywhere else.
        return ONBOARDING_NONE
    fields = ("onboarding", "onboarding_at", "onboarding_proof")
    present = [k for k in fields if k in rec]
    if not present:
        return ONBOARDING_NONE
    # ALL THREE OR NONE, AND THE WRITER IS WHY. `_record_onboarding` writes the
    # three keys in ONE call and always writes all three — an empty proof is
    # written as "", which is a VALUE meaning no refusal text, not an absence.
    # So a record carrying only some of them was not written by this writer and
    # is half-written. Counting proof toward PRESENCE but not toward VALIDITY
    # would be the independent-optionals shape this door exists to abolish,
    # one field to the left.
    missing = [k for k in fields if k not in rec]
    if missing:
        return onboarding_invalid(
            "the register carries %s but not %s — the writer emits all three "
            "in one write, so this record is half-written"
            % ("/".join(present), "/".join(missing)))
    outcome = rec.get("onboarding")
    if not isinstance(outcome, str):
        return onboarding_invalid(
            "the register carries %s but its `onboarding` outcome is %r, not a "
            "string — outcome and stamp are written together or not at all"
            % ("/".join(present), outcome))
    if outcome not in (harness.DELIVERED, harness.NOT_DELIVERED,
                       harness.UNKNOWN):
        return onboarding_invalid(
            "recorded outcome %r is not one of this build's three harness "
            "outcomes, so whether the seat was briefed cannot be classified"
            % (outcome,))
    stamp = rec.get("onboarding_at")
    if not isinstance(stamp, str):
        return onboarding_invalid(
            "outcome %r is recorded but `onboarding_at` is %r — an outcome "
            "with no event time cannot be compared to anything"
            % (outcome, stamp))
    try:
        event_at = calendar.timegm(time.strptime(stamp, ONBOARDING_STAMP_FMT))
    except (ValueError, TypeError):
        return onboarding_invalid(
            "`onboarding_at` %r does not parse as %s — the stamp is not read "
            "by any other rule, so it is unreadable rather than approximate"
            % (stamp, ONBOARDING_STAMP_FMT))
    # PARSEABLE IS NOT THE BAR; CANONICAL IS. strptime is LENIENT — %d, %m and
    # %H each accept one OR two digits and %S accepts a leap second — so
    # "2026-9-9T1:2:3Z" and a ":60" second both parse, and neither is a string
    # this writer can emit. The round trip accepts exactly what strftime
    # produces and rejects every other spelling, with no second format list to
    # drift out of step with the first.
    if time.strftime(ONBOARDING_STAMP_FMT, time.gmtime(event_at)) != stamp:
        return onboarding_invalid(
            "`onboarding_at` %r parses but is not the canonical %s spelling "
            "this writer emits (it would be written %r) — a record no writer "
            "in this build could have produced"
            % (stamp, ONBOARDING_STAMP_FMT,
               time.strftime(ONBOARDING_STAMP_FMT, time.gmtime(event_at))))
    now = time.time() if now is None else now
    if event_at > now + ONBOARDING_FUTURE_GRACE_S:
        return onboarding_invalid(
            "`onboarding_at` %s is %ds in the FUTURE of this host's clock — "
            "writer and reader share that clock, so this record describes an "
            "event that has not happened"
            % (stamp, int(event_at - now)))
    proof = str(rec.get("onboarding_proof") or "") or None
    return Onboarding(ONBOARDING_VALID, outcome == harness.DELIVERED,
                      int(event_at), proof, None)


def _record_onboarding(d, seat_name, state, proof):
    """Persist whether the onboarding brief was PROVEN submitted. Fail-soft.

    A WARNING ON STDOUT IS NOT A STATE, and this is the field that fixes that.
    Both spawn and resume already refuse to press Enter when they cannot prove
    the composer still holds exactly what helm typed — a correct refusal — and
    both then print the refusal to whoever ran the command and record NOTHING.
    The seat is left registered, pane-live, proxy-up and upstream-HEALTHY, and
    every instrument reads it as idle because idle is exactly what it looks
    like — while THIS LAUNCH's brief never went in, so the seat has armed no
    wake beacon since it and no @mention, DM or dispatch can reach it.

    THE CLAIM IS ABOUT THIS LAUNCH, NEVER ABOUT A LIFETIME. Resume rewrites
    the spawn register, so a seat in this state may have run for hours before
    its last relaunch; "it never ran" is a sentence this field cannot support
    and does not make.

    RECORDED ON BOTH OUTCOMES, not only on failure. If only the failures were
    written, a record without the field would be ambiguous between "this spawn
    proved delivery" and "this spawn predates the field", and the second is the
    one that must never be read as the first — the same tri-state-on-presence
    rule `body_of` keeps for a dispatch brief. Absent means UNKNOWN here too.

    Fail-soft by construction: a spawn that briefed its seat correctly must not
    be failed by a register write, and a spawn that did not must still print.
    The caller's exit code is unchanged either way.
    """
    from . import harness, pk
    # THE WRITER WHITELISTS, and it is the same list `parse_onboarding` reads
    # back. An unrecognised state is DECLINED rather than persisted: the
    # register is the input to a verdict, and a value the door would have to
    # call INVALID is worse there than no value, because ABSENT is already a
    # defined reading while INVALID costs a pane read. The spawn still prints
    # its own refusal line, so declining here loses nothing an operator sees.
    if str(state) not in (harness.DELIVERED, harness.NOT_DELIVERED,
                          harness.UNKNOWN):
        print("helm seat: WARN — onboarding state %r is not one of the three "
              "harness outcomes; NOT recorded (the register would hold a value "
              "no reader can classify)" % (state,), file=sys.stderr)
        return False
    rec = _spawn_record(d)
    if not isinstance(rec, dict) or rec.get("seat") != seat_name:
        # ANOTHER SEAT'S RECORD, OR NONE — the same identity guard every other
        # reader of this file applies. Writing here would attribute this
        # spawn's outcome to whoever owns the register.
        return False
    try:
        # ALL THREE FIELDS IN ONE WRITE. They are one fact, and the door
        # refuses a record that carries some of them, so they must never be
        # able to land separately.
        pk.write_json(_spawn_path(d), dict(
            rec, onboarding=str(state), onboarding_proof=str(proof or ""),
            # time.time(), NOT a bare gmtime(): gmtime() reads time(NULL),
            # a coarse clock that can lag time.time() across a second
            # boundary, so the stamp could predate the write it records.
            onboarding_at=time.strftime(ONBOARDING_STAMP_FMT,
                                        time.gmtime(time.time()))))
    except OSError as e:
        print("helm seat: WARN — onboarding state not recorded (%s): %s"
              % (_spawn_path(d), e), file=sys.stderr)
        return False
    return True

# ---------------------------------------------------------------------------
# the spawn ATTEMPT — one identity for one spawn, from publication to settlement
# ---------------------------------------------------------------------------
# A spawn publishes its register BEFORE the child runs, because the child's own
# first SessionStart is the only authority for its session id and it needs a
# record to bind into. That makes the record exist across the whole child start,
# which is exactly the stretch the lifecycle lock must NOT span (the hook it
# waits on has a 5s budget). The attempt is what carries the two facts a held
# lock carries for free: WHICH spawn owns this record right now, and whether
# that spawn ever finished.
#
# THE ATTEMPT IS A TOKEN, minted ONCE, by `_publish_spawn_attempt`, at the moment
# the pending record goes to disk — and threaded UNCHANGED into the child's
# launch line (`SPAWN_ATTEMPT_ENV`), read back by the child's first hook, compared
# by the finalize and by the settlement. Every stage of a spawn's life asks the
# same question — "is this the attempt I am about?" — of the same value, so no
# stage improvises an identity of its own. That docstring is the one place the
# whole life is written down; read it before this section's functions.
#
# PENDING is an honest state, not a placeholder. It says: this identity was
# published by a process that is starting a child, the child's session is not
# bound yet, and no other actor may act on this seat. A reader that finds it has
# an incomplete seat rather than a working one or a missing one, and those two
# readings are the ones a spawn without this field leaves behind.
SPAWN_ATTEMPT_PENDING = "pending"
SPAWN_ATTEMPT_COMPLETE = "complete"
SPAWN_ATTEMPT_INCOMPLETE = "incomplete"

# What `_attempt_process_state` can say about the process that published a
# PENDING attempt. OWN and GONE open the door; LIVE and UNVERIFIABLE refuse it,
# and the two refusals are worded apart because an operator resolves them
# differently (wait for the live one; find the process behind the other).
ATTEMPT_OWN = "own"
ATTEMPT_LIVE = "live"
ATTEMPT_GONE = "gone"
ATTEMPT_UNVERIFIABLE = "unverifiable"

# The fields the CHILD's own SessionStart writes into the record. A finalize that
# wrote its in-hand dict straight back would erase them — which is the binding
# the pre-publication ordering exists to make possible.
SPAWN_CHILD_FIELDS = ("session", "session_pid", "session_pid_identity",
                      "pane_key", "pty_id", "worktree_id")

# The harnesses whose pane a SessionStart hook can actually bind a session for.
# `_sessionstart_pane_fields` is the one implementation of that capability, and
# this is its declared domain, so a caller that WARNS about session binding can
# state what is supported instead of promising what is not.
SESSION_BINDING_HARNESSES = ("orca", "headless")


def spawn_attempt_token(env=None):
    """The spawn attempt token THIS process carries, or None — THE ONE READER of
    `SPAWN_ATTEMPT_ENV`, for the child's hook and for `helm launch` alike, so a
    process that was handed no attempt reads None rather than minting one."""
    value = (os.environ if env is None else env).get(SPAWN_ATTEMPT_ENV)
    return str(value) if value else None


def _new_spawn_attempt():
    """The identity of THIS spawn attempt: a token nothing else can spell, plus
    the process and birth identity that make "is it still running?" answerable.

    Called by `_publish_spawn_attempt` and nothing else that spawns: the token is
    minted where the pending record is written, so there is exactly one producer
    of a spawn's identity. The pid ALONE cannot answer liveness — a pid is
    reused, and a reused pid would make an abandoned attempt look like a live one
    forever, which is a seat no verb can ever touch again. `_pid_identity` is the
    same process-birth proof the headless reap uses for the same reason.
    """
    import uuid
    from . import pk
    pid = os.getpid()
    return {"id": uuid.uuid4().hex, "pid": pid,
            "pid_identity": _pid_identity(pid),
            "state": SPAWN_ATTEMPT_PENDING, "at": pk.now_ts()}


def _publish_spawn_attempt(storage_seat, identity, d, rec):
    """Mint THE ATTEMPT TOKEN and publish `rec` as this seat's PENDING register
    under it; return the attempt, or None when the register could not be
    written (nothing has started, and `rec` is left carrying no attempt).

    THIS IS THE ONE PRODUCER OF A SPAWN'S IDENTITY, and the token it mints is the
    same value at every stage of the spawn's life:

      PUBLICATION (here, under the lifecycle lock, before any child exists): the
        record goes to disk as PENDING with the token, the publishing pid and its
        birth identity. `_pending_attempt_conflict` reads those three to keep a
        concurrent spawn or resume out while the lock is released — and a pending
        attempt whose process it cannot verify is a THIRD state, refused by name,
        never called abandoned (that was finding 1: an unreadable birth identity
        let a `--replace` run over a live spawn).
      THE LAUNCH LINE: the token rides in the child's command or env as
        `SPAWN_ATTEMPT_ENV` (`_launch_command`, `_native_launch_command`,
        `_headless_spawn`), unchanged, so the child and every hook it runs know
        which attempt they belong to.
      THE FIRST HOOK: `_sessionstart_pane_fields` lets a hook bind into a PENDING
        register that has no handle yet ONLY when that hook carries this token —
        a `helm launch --seat S` typed in another pane carries none and is refused
        with the pending attempt named (finding 2: without the token any
        same-seat hook's proven pane binds, so a manual launch's session lands
        in a spawn's record and is finalized under the spawn's handle). `helm
        launch` never
        mints one: it reads the token through `spawn_attempt_token` and pins it
        into claude's env, so there is no second producer.
      REGISTER AND FINALIZE: `_finalize_spawn_attempt` writes only over a record
        still carrying this token, merging the fields the child's hook wrote.
      SETTLEMENT: `_settle_spawn_attempt` stamps only this token's record
        INCOMPLETE when absence is unproven, KEEPING every fact the spawn already
        holds — the returned handle, the pid and its birth identity — so a known
        handle is never overwritten by the published None (finding 3), and its
        return value is what the failure prose derives the `helm seat where`
        claim from instead of asserting one (finding 5). Every mutating verb
        knocks on `_refuse_in_flight_spawn` BEFORE its first write, the manual
        resume included (finding 4: the no-adapter resume re-minted the launch
        assets and returned before it asked).

    One token, one producer, five doors that compare it — so the next reader of
    this life reads one paragraph rather than five patches.
    """
    attempt = _new_spawn_attempt()
    rec["attempt"] = attempt
    rec.setdefault("session", None)
    return attempt if _register_spawn(storage_seat, identity, d, rec) else None


def _attempt_process_state(attempt):
    """What can be PROVEN about the process that published a PENDING attempt.

    OWN: this very process (the spawn's own later rungs re-read their record).
    GONE: no process holds that pid (kill(pid, 0) says so — the same liveness
      door the proxy pidfile guard uses), or the pid is held by a process born at
      another time (birth identity saved and current both readable and unequal).
      Only these two are proof of absence; an abandoned attempt is exactly one of
      them.
    LIVE: pid alive and its birth identity equal to the saved one, BOTH READ IN
      THE SAME DOMAIN.
    UNVERIFIABLE: the pid is alive but the saved or the current birth identity is
      None, or the two readings come from DIFFERENT DOMAINS, or the record
      carries no usable pid at all. Helm can prove neither that the spawn is
      running nor that it is gone, and the proofs it lacks are the only ones
      that open this door.

    A BIRTH IDENTITY CARRIES ITS DOMAIN, and two domains do not compare.
    `seat_paths._pid_identity` has two producers behind one name: `proc:<starttime>`
    when /proc is readable and `ps:<lstart>` on the fallback. They describe the
    same birth in different languages — different units, different epochs,
    different text — so ONE living process reads as `proc:8419` at publication
    and `ps:Sat Sep 13 11:04:28 2026` a second later if that /proc read fails.
    The first cut compared the two strings and called every inequality GONE, so a
    concurrent `spawn --replace` treated a live in-flight spawn as abandoned and
    reaped its child (`_spawn_reap` stops the pane before any terminal proof, so
    a later refusal cannot undo it). Cross-domain readings are INCOMPARABLE, not
    unequal: they say nothing about absence, so they answer UNVERIFIABLE, which
    stops nothing and refuses `--replace` by name. Equal readings in one domain
    are still LIVE; unequal readings in one domain, and a pid nothing holds, are
    still the only two proofs of absence.
    """
    pid = attempt.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return ATTEMPT_UNVERIFIABLE
    if pid == os.getpid():
        return ATTEMPT_OWN
    if not _pid_alive(pid):
        return ATTEMPT_GONE
    saved, current = attempt.get("pid_identity"), _pid_identity(pid)
    if not saved or not current:
        return ATTEMPT_UNVERIFIABLE
    # The domain is the part before the first colon — what PRODUCED the reading.
    # `str()` so a corrupt record's non-string identity lands here (its "domain"
    # matches nothing) instead of reading as a different birth.
    if len({str(v).split(":", 1)[0] for v in (saved, current)}) != 1:
        return ATTEMPT_UNVERIFIABLE
    return ATTEMPT_LIVE if saved == current else ATTEMPT_GONE


def _pending_attempt_conflict(rec):
    """The refusal when another process's PENDING attempt owns this seat, else
    None — LIVE and UNVERIFIABLE are both refusals, worded apart.

    THIS IS THE SERIALIZATION THE SHORTENED LOCK GIVES BACK. While a spawn's
    child starts, its lock is released on purpose, so the flock no longer keeps a
    second spawn or a resume out — the PENDING attempt does, and it is strictly
    more informative than the lock was: it names the attempt, the pid and the
    time, so an operator can tell a real race from a crashed spawn.

    TWO THINGS ARE NOT CONFLICTS: an attempt this very process owns, and one
    whose process is PROVEN gone (`_attempt_process_state`) — an ABANDONED
    attempt, whose record `--replace` and reconciliation resolve; refusing it
    would leave the seat unspawnable. UNVERIFIABLE IS NEITHER OF THOSE. A birth
    identity that cannot be read is not evidence the process is gone, and the
    first cut read it as exactly that, so a `--replace` could run over a spawn
    that was still in flight. `--replace` does not override this refusal; the
    only thing that opens the door is proof of absence through the same two
    readings, and the sentence says which readings those are.
    """
    attempt = (rec or {}).get("attempt") or {}
    if attempt.get("state") != SPAWN_ATTEMPT_PENDING:
        return None
    state = _attempt_process_state(attempt)
    if state in (ATTEMPT_OWN, ATTEMPT_GONE):
        return None
    pid = attempt.get("pid")
    if state == ATTEMPT_LIVE:
        return ("a spawn of %s is IN FLIGHT — attempt %s, pid %d, started %s. "
                "Its lifecycle lock is released only while its child starts, so "
                "this is a live concurrent spawn and not a stale marker; wait "
                "for it, or resolve that process and retry"
                % (rec.get("seat"), attempt.get("id"), pid, attempt.get("at")))
    return ("a spawn of %s is PENDING and its process is UNVERIFIABLE — attempt "
            "%s (token %s), pid %s, started %s, saved birth identity %r. helm "
            "can prove neither that this spawn is still running nor that it is "
            "gone, so it is NOT treated as abandoned and --replace does not "
            "override it: the attempt is released only when its process is "
            "PROVEN gone (no such pid, or the pid reborn under another start "
            "time). Find pid %s; if it is not a `helm seat spawn`, stop it and "
            "retry"
            % (rec.get("seat"), attempt.get("id"), attempt.get("id"), pid,
               attempt.get("at"), attempt.get("pid_identity"), pid))


def _finalize_spawn_attempt(storage_seat, identity, d, rec, attempt,
                            handle=None):
    """Publish the FINISHED record of this attempt and return it, or None when it
    is no longer the attempt on disk or could not be written.

    ONLY THE SAME ATTEMPT. The lock was released while the child started, so the
    record on disk may have moved: the child's own SessionStart may have bound a
    session into it (the whole point of publishing first), or — if this attempt
    was abandoned and something replaced it — it may belong to someone else
    entirely. Comparing the attempt id is what separates those: a foreign record
    is left exactly as found, and a matching one is merged rather than replaced,
    so the fields the child wrote survive a finalize that never knew about them.
    """
    current = _spawn_record(d) or {}
    if current and (current.get("attempt") or {}).get("id") != attempt["id"]:
        return None
    out = dict(rec)
    if handle is not None:
        out["handle"] = handle
    for field in SPAWN_CHILD_FIELDS:
        if current.get(field) is not None:
            out[field] = current[field]
    out["attempt"] = dict(attempt, state=SPAWN_ATTEMPT_COMPLETE)
    return out if _register_spawn(storage_seat, identity, d, out) else None


def _backfill_spawn_session(seat_name, d, ad):
    """Recover a SessionStart that raced the initial spawn register write."""
    from . import pk
    rec = _spawn_record(d)
    if not rec or rec.get("seat") != seat_name or rec.get("harness") != "orca":
        return False
    sessions = [rec.get("session")] if rec.get("session") else []
    if not sessions:
        for path in glob.glob(os.path.join(_session_record_root(d, rec),
                                           "*.json")):
            try:
                with pk.open_regular(path) as f:
                    sid = json.load(f).get("sessionId")
            except (OSError, ValueError, AttributeError):
                continue
            if sid and sid not in sessions:
                sessions.append(sid)
    try:
        rows = ad.list()
    except Exception:
        return False
    candidates = []
    for sid in sessions:
        probe = dict(rec, session=sid)
        _, fields, err = _prove_orca_replacement(d, probe, ad, rows)
        if not err and fields and fields["handle"] == rec.get("handle"):
            candidates.append((sid, fields))
    if len(candidates) != 1:
        return False
    sid, fields = candidates[0]
    rec["session"] = sid
    rec.update(fields)
    try:
        pk.write_json(_spawn_path(d), rec)
    except OSError:
        return False
    try:
        if _bind_runtime_session(
                _record_identity(rec), sid, storage_seat=seat_name):
            return False
    except Exception:
        return False
    return True


def _sessionstart_pane_fields(rec):
    """Prove this hook process belongs to the registered pane/process."""
    kind = rec.get("harness")
    if kind == "headless":
        if os.getppid() != rec.get("pid") or _recorded_pid_alive(rec) is not True:
            return None, "SessionStart is not a child of the registered headless process"
        return {}, None
    if kind != "orca":
        return None, ("SessionStart binding is unsupported for harness %r — "
                      "helm can prove a pane's own session for: %s"
                      % (kind, ", ".join(SESSION_BINDING_HARNESSES)))
    pane_key = os.environ.get("ORCA_PANE_KEY")
    worktree_id = os.environ.get("ORCA_WORKTREE_ID")
    if not pane_key or not worktree_id:
        return None, "SessionStart has no complete Orca pane/worktree identity"
    if rec.get("pane_key") and rec["pane_key"] != pane_key:
        return None, "SessionStart pane key conflicts with the spawn register"
    if rec.get("worktree_id") and rec["worktree_id"] != worktree_id:
        return None, "SessionStart worktree identity conflicts with the spawn register"
    from . import harness
    path = shutil.which(harness.OrcaAdapter.bin)
    if not path:
        return None, "Orca adapter unavailable during SessionStart binding"
    ad = harness.OrcaAdapter(path)
    try:
        resolved = ad.resolve_pane(pane_key)
        rows = ad.list()
    except harness.HarnessError as e:
        return None, str(e)
    handle, pty = resolved.get("handle"), resolved.get("pty_id")
    matches = [row for row in rows if row.get("handle") == handle and
               row.get("pty_id") == pty and
               row.get("worktree_id") == worktree_id and
               row.get("writable") is True and _pane_live(row)]
    if len(matches) != 1:
        return None, ("SessionStart pane identity matched %d connected, writable "
                      "inventory rows" % len(matches))
    attempt = rec.get("attempt") or {}
    pending = attempt.get("state") == SPAWN_ATTEMPT_PENDING
    if not rec.get("pane_key") and rec.get("handle") != handle:
        if not (rec.get("handle") is None and pending):
            return None, "initial SessionStart pane does not match the spawned handle"
        # A PENDING ATTEMPT HAS NO HANDLE TO MATCH YET, and demanding one refused
        # the very first hook it was published for. The producer writes the
        # record before it calls the adapter, so between those two moments the
        # only handle that exists is the one THIS hook just proved against the
        # live Orca inventory. But a proven pane is a proof about A pane, not
        # about THIS spawn's pane: a `helm launch --seat S` typed in another
        # pane during that window proves its own pane just as well, and the
        # first cut let it bind — its session, pane key and PTY landed in the
        # spawn's record and were finalized under the spawn's handle. THE
        # RENDEZVOUS IS BY TOKEN: the spawn threaded its attempt token into the
        # child's launch line, so only a hook carrying that exact token is the
        # child this record was published for. A manual launch carries none.
        token = spawn_attempt_token()
        if token != attempt.get("id"):
            return None, (
                "%s's register is a PENDING spawn attempt %s (pid %s) and this "
                "SessionStart carries %s, so it is not that spawn's child — a "
                "manual `helm launch --seat %s` in another pane cannot bind into "
                "an attempt it did not come from; wait for that spawn to settle, "
                "or spawn through `helm seat spawn %s --replace`"
                % (rec.get("seat"), attempt.get("id"), attempt.get("pid"),
                   ("attempt token %s" % token) if token
                   else "no attempt token",
                   rec.get("seat"), rec.get("seat")))
    return {"handle": handle, "pane_key": pane_key, "pty_id": pty,
            "worktree_id": worktree_id}, None


def _bind_spawn_session(seat_name, session, source=None, storage_seat=None):
    """Bind SessionStart to a canonical identity in its storage-keyed register.

    Spawn cannot know the new Claude session before the process starts. The
    SessionStart hook is the first authoritative owner of that identity. A
    durable rename keeps config and spawn.json under ``storage_seat`` while the
    live process and roster bind under ``seat_name``; both must match the record
    under the same per-storage-seat lifecycle lock.

    Claude may mint a new session id INSIDE the same long-lived pane process
    (`/clear`, compaction, or a workflow transition). For Orca seats that change
    is authorized by process continuity, never by the hook's source label: the
    old and new sessions must resolve to the same live pid, or the register's
    saved pid + incarnation must match the new session. This keeps an unrelated
    SessionStart from stealing the pane while allowing the pane's own session id
    to advance. `source` remains only for the legacy headless clear contract.
    """
    if not session:
        return False
    storage_seat = storage_seat or seat_name
    family, err = _seat_family(storage_seat)
    if err or _seat_surface_error(family, storage_seat):
        return False
    import fcntl
    from . import pk
    d = _instance_dir(family, storage_seat)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rec = _spawn_record(d)
            if not rec:
                return None                 # direct/manual seat, nothing to bind
            if rec.get("seat") != storage_seat \
                    or _record_identity(rec) != seat_name:
                return False
            fields, identity_err = _sessionstart_pane_fields(rec)
            if identity_err:
                if (rec.get("attempt") or {}).get("state") \
                        == SPAWN_ATTEMPT_PENDING:
                    # NAMED, because the operator who typed the manual launch
                    # is reading this pane's hook output and the generic
                    # "could not bind" line does not say which spawn owns the
                    # seat or that it is still in flight.
                    print("helm seat: SessionStart bind REFUSED — %s"
                          % identity_err, file=sys.stderr)
                return False
            prior = rec.get("session")
            pid, pid_identity = None, None
            if rec.get("harness") == "orca":
                process, _ = _live_session_orca_identity(d, session, seat_name,
                                                         rec)
                pid = (process or {}).get("pid")
                proc_start = (process or {}).get("proc_start")
                pid_identity = "proc:" + proc_start if pid and proc_start else None
            if prior and prior != session:
                if rec.get("harness") == "orca":
                    recorded = pid and pid_identity and \
                        rec.get("session_pid") == pid and \
                        rec.get("session_pid_identity") == pid_identity
                    legacy = False
                    if pid and not recorded:
                        from . import sessions
                        legacy = sessions.live_sids().get(prior) == pid
                    if not (recorded or legacy):
                        return False
                elif source != "clear":
                    return False
            rec["session"] = session
            rec.update(fields)
            if pid and pid_identity:
                rec["session_pid"] = pid
                rec["session_pid_identity"] = pid_identity
            pk.write_json(_spawn_path(d), rec)
            return True
    except OSError:
        return False


def _spawn_plan(seat_name, d, launch_sh, room, cwd, onboard, ad,
                replace=False, role="worker", pane_onboard=None,
                storage_seat=None):
    """--print/--dry-run: the exact per-harness calls, nothing spawned,
    reaped, or re-minted."""
    storage_seat = storage_seat or seat_name
    print("helm seat spawn %s — plan (--print: nothing spawned, reaped, or "
          "re-minted):" % seat_name)
    rec = _spawn_record(d)
    would = []
    if (rec or {}).get("harness") == "headless" and rec.get("pid"):
        live = _recorded_pid_alive(rec)
        if live and replace:
            would.append("kill registered headless pid %d (explicit --replace)"
                         % rec["pid"])
        elif live:
            would.append("REFUSE live headless pid %d: pass --replace"
                         % rec["pid"])
        elif live is None:
            would.append("REFUSE live headless pid %d: process identity "
                         "unverifiable" % rec["pid"])
    if (rec or {}).get("harness") != "headless":
        pane_ad, handle, detail = _resolve_registered_pane(
            storage_seat, d=d, adapter=ad, repair=False, for_reap=True)
        adapters = []
        if pane_ad is not None:
            adapters.append(pane_ad)
        if ad is not None and all(a.name != ad.name for a in adapters):
            adapters.append(ad)
        title_only = []
        for adapter in adapters:
            try:
                title_only.extend(row.get("handle") for row in adapter.list()
                                  if row.get("handle") != handle
                                  and row.get("title") == seat_name)
            except Exception as e:
                would.append("(pane scan failed: %s)" % e)
        if title_only:
            would.append("REFUSE mutable-title-only pane match for %r (%s)"
                         % (seat_name, ", ".join(title_only)))
        elif handle is not None and replace:
            would.append("%s stop registered pane %s (explicit --replace)"
                         % (pane_ad.name, handle))
        elif handle is not None:
            would.append("REFUSE live registered pane %s: pass --replace"
                         % handle)
        elif rec and pane_ad is None:
            would.append("REFUSE " + detail)
    print("  reap:  " + ("; ".join(would) or "none (no stale same-name seat)"))
    # The home worktree is RESOLVED, never provisioned, by a plan — a dry run
    # has no side effects. It reads create-or-reuse because that is what the
    # real spawn does at this step.
    print("  cwd:   %s (%s)"
          % (cwd, "already checked out — reused" if cwd and os.path.isdir(cwd)
             else "per-seat home worktree, created at spawn"))
    print("  mint:  refresh %s (child-stamp stripped => persistence ON, "
          "--dangerously canonical, skills linked, hooks wired)" % launch_sh)
    print("  role:  %s%s" % (
        role, " (HELM_SEAT_ROLE=lead + Claude ultracode)"
        if role == "lead" else " (ordinary worker)"))
    q = _launch_command(launch_sh, role)
    if ad is None:
        print("  harness: headless (no metaharness detected — the standalone "
              "default)")
        print("  spawn: detached setsid: %s '<onboarding>'  "
              "(stdin /dev/null, log %s)" % (q, os.path.join(d, "spawn.log")))
        print("  onboard: delivered AT LAUNCH as the claude first-prompt "
              "positional arg")
    else:
        print("  harness: " + ad.name)
        print("  spawn: %s.spawn(command=%s, title=%s, cwd=%s) -> <handle>"
              % (ad.name, q, seat_name, cwd))
        print("  onboard: %s.submit(<handle>, %r)" %
              (ad.name, pane_onboard or onboard))
    print("  register: %s {harness, %s, role=%s, worktree=%s, room=%s}"
          % (_spawn_path(d), "pid" if ad is None else "handle", role, cwd,
             room or "(derived at join)"))
    label = ("onboarding first-prompt" if ad is None else
             "onboarding full brief (boot-brief expands after the first turn)")
    print("  %s:\n    %s" % (label, onboard))
    return 0


def _seat_home_cwd(seat_name, provision=True, base=None):
    """The seat's DEFAULT working directory: its OWN home worktree
    (`<repo>-wt/seats/<seat>`), never the shared main checkout.

    THE BUG this closes (LAYER 1 of the coordination substrate, seat isolation):
    the default was safe_cwd() — wherever the operator happened to stand, in
    practice the MAIN checkout — so every spawned seat edited, built and
    stashed the same tree. Dirty main blocks every land, seats collide, and
    `git stash` on a shared checkout is not swarm-safe. A default is a decision;
    this one is now owned. An explicit `--cwd` still wins.

    provision=False (a `--print` plan, or the pre-lock parse pass) resolves the
    path WITHOUT creating anything: a dry run must have no side effects, and the
    real provisioning belongs INSIDE the per-seat spawn lock so two concurrent
    spawns cannot race one `git worktree add`.

    Fails OPEN to `base` with a loud stderr note — a git/metaharness hiccup
    must degrade a spawn to the old shared-tree behaviour, never abort it.

    `base` is the reference directory: what to resolve the repo from and what to
    degrade to. It defaults to safe_cwd() (the SPAWN case — the operator's cwd is
    the only reference a brand-new seat has), and RESUME passes the seat's own
    last working directory instead, because degrading a resume to wherever the
    INTEGRATOR happens to be standing would move someone else's pane into my
    tree — a fallback must land where the caller's failure is survivable, not
    wherever the invoking process happens to sit."""
    from . import harness, seats
    here = base or seats.safe_cwd()
    root = harness.find_repo_root(here)
    if not root or not seat_name:
        return here      # outside a checkout there is nothing to isolate from
    if not provision:
        return harness.seat_worktree_path(root, seat_name)
    ad = harness.detect()
    ensure = getattr(ad, "ensure_home_worktree", None) if ad else None
    try:
        return ensure(seat_name, root) if ensure \
            else harness.ensure_home_worktree(seat_name, root)
    except harness.HarnessError as e:
        print("helm seat: WARN — %s home worktree unavailable (%s); falling "
              "back to the SHARED checkout %s (dirty-main collisions are back "
              "until it is fixed)" % (seat_name, e, here), file=sys.stderr)
        return here


def _resume_cwd(seat_name, sess_cwd):
    """Where a RESUMED pane lands. Follows the seat's own last working directory
    EXCEPT when that directory is a place no seat should be working.

    THE HOLE THIS CLOSES: slice 0 gave `spawn` a per-seat home worktree, but
    resume kept `cwd=sess_cwd or safe_cwd()` — the cwd SNIFFED from the seat's
    newest session. Every fleet seat here is a long-lived RESUME, not a fresh
    spawn, so a spawn-only default isolated almost nothing: resuming a seat that
    had been working in the shared checkout put it straight back into the shared
    checkout, which is the exact collision the home worktree exists to prevent.

    THE RULE IS NOT "resume always goes home", and getting that wrong would cost
    real continuity: a seat that was working in a LANE worktree must come back to
    it — that is its task, mid-flight. What is refused is a DEGRADED location:
    the shared main checkout (every seat's collision surface) and a throwaway
    temp dir. That is deliberately the same DOWNGRADE REFUSAL landed one layer up
    for roster rows, reusing the same `_is_temp_cwd` predicate rather than a
    second opinion about what "throwaway" means: never accept a degraded location
    when a real one is available.

    Fail-open in the direction that keeps a resume working: anything unreadable,
    outside a checkout, or already a real non-shared directory is kept as-is, and
    a home worktree that cannot be provisioned degrades to `sess_cwd` (the seat's
    OWN last tree), never to the invoking process's cwd."""
    from . import harness, seats
    if not sess_cwd or not seat_name:
        return sess_cwd or seats.safe_cwd()
    # ORDER MATTERS, and the first version of this got it wrong: the temp check
    # has to come BEFORE the repo lookup. A throwaway cwd is not inside any
    # checkout, so an early "outside a checkout, nothing to isolate" return sent
    # the seat straight back to /tmp — the exact bug being fixed (a live row read
    # cwd=/tmp). Caught by this function's own test, which is the only reason the
    # ordering is stated here rather than rediscovered later.
    if seats._is_temp_cwd(sess_cwd):
        # A temp dir names no repository, so the seat side carries no reference
        # to resolve a home from; the invoking operator's cwd is the only one
        # available. That is NOT the same as landing the pane in the invoker's
        # tree: the result is still the deterministic <repo>-wt/seats/<seat>.
        here = seats.safe_cwd()
        if not harness.find_repo_root(here):
            return sess_cwd      # no repo anywhere in reach — nothing better
        return _report_rehome(seat_name, sess_cwd,
                              _seat_home_cwd(seat_name, base=here),
                              "a throwaway temp dir")
    root = harness.find_repo_root(sess_cwd)
    if not root:
        return sess_cwd          # outside a checkout there is nothing to isolate
    if os.path.realpath(sess_cwd) != os.path.realpath(root):
        return sess_cwd          # a real lane room — continuity wins
    return _report_rehome(seat_name, sess_cwd,
                          _seat_home_cwd(seat_name, base=sess_cwd),
                          "the SHARED checkout every seat collides in")


def _stale_resume_cwd(path):
    """Why `path` must not receive a resumed pane, or None when it is usable.

    Two stale shapes, both measured putting seats in trees the operator could
    not find (row #155): a recorded cwd that NO LONGER EXISTS, and one that
    still exists but sits inside a REMOVED git worktree — its `.git` FILE
    names a gitdir that is gone (the worktree was pruned, or the checkout it
    hung off moved), so git verbs there fail while the directory itself looks
    fine. File reads only, no subprocess: this runs on every resume.

    Fail-open everywhere else (the `_resume_cwd` law): no path, an unreadable
    `.git`, a directory outside any checkout — none of that is EVIDENCE of
    staleness, and refusing on a guess would block real resumes."""
    if not path:
        return None
    if not os.path.isdir(path):
        return "it no longer exists"
    d = os.path.realpath(path)
    while True:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return None              # a real checkout root
        if os.path.isfile(g):
            try:
                with open(g, encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                return None          # unreadable is not evidence
            target = raw.partition("gitdir:")[2].strip().splitlines()
            target = target[0].strip() if target else ""
            if not target:
                return None
            if not os.path.isabs(target):
                target = os.path.join(d, target)
            if os.path.exists(target):
                return None
            return ("it sits in a REMOVED worktree (%s names gitdir %s, "
                    "which is gone)" % (g, target))
        parent = os.path.dirname(d)
        if parent == d:
            return None              # outside any checkout — nothing stale
        d = parent


def _report_rehome(seat_name, was, now, why):
    """Say it out loud when a resume moves a pane. A relocation the operator
    cannot see is indistinguishable from a bug, and this one changes which tree
    someone else's agent wakes up in."""
    if os.path.realpath(now) != os.path.realpath(was):
        print("  cwd: %s is %s — resuming %s into its own home worktree %s"
              % (was, why, seat_name, now))
    return now


def _spawn_args(rest, seat_name=None, provision=True):
    """Parse spawn's small option surface without letting a missing value raise
    IndexError or an unknown flag silently change the launch. The default cwd is
    the seat's OWN home worktree (`_seat_home_cwd` — the dirty-main collision
    cure), NOT safe_cwd()/the shared checkout; an explicit `--cwd` still wins.
    Resolution stays fail-open and tolerates None downstream (harness
    inherits). `provision` is False for the pre-lock and dry-run passes."""
    room, cwd, dry_run, replace = None, None, False, False
    model, role = None, "worker"
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--print", "--dry-run"):
            dry_run = True
            i += 1
            continue
        if arg == "--replace":
            replace = True
            i += 1
            continue
        if arg not in ("--room", "--cwd", "--model", "--role"):
            return None, "unknown option %s" % arg
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            return None, "%s wants a value" % arg
        value = rest[i + 1]
        if arg == "--room":
            room = value
        elif arg == "--model":
            # Spawn a non-default model (for example Codex Spark). The model
            # is independent of orchestration role: a lead or worker may use
            # either model, and both choices persist in spawn.json.
            model = value
        elif arg == "--role":
            role = _seat_role(value)
            if role is None:
                return None, "--role wants one of: %s" % ", ".join(SEAT_ROLES)
        else:
            cwd = os.path.abspath(os.path.expanduser(value))
        i += 2
    if cwd is None:
        cwd = _seat_home_cwd(seat_name, provision=provision and not dry_run)
    return (room, cwd, dry_run, replace, model, role), None
