"""Seat adoption, rebind, and liveness for :mod:`helm.seat`."""
import contextlib as _contextlib
import datetime
import glob
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

from . import seat_lifecycle_sessions as _sessions_impl
from .seat_lifecycle_sessions import (
    _seat_family,
    family_for,
    _unknown_seat_reason,
    NATIVE_FAMILY,
    spawn_families,
    numbered_families,
    project_families,
    spawn_identity,
    _named_seat_family,
    registered_seat_family,
    _split_seat,
    _SESSION_JSONL,
    _STUB_MAX_BYTES,
    _has_real_turn,
    _PRUNE_HEAD_BYTES,
    _prune_source,
    _newest_seat_session,
    _seat_session_path_by_id,
    _seat_session_by_id,
    _homing_from_launch,
    _room_from_launch,
    _multi_from_launch,
    _ensure_autocompact_timer,
    SPAWN_SEND_DELAY_S,
    onboarding_prompt,
    rearm_prompt,
)

from . import seat_lifecycle_runtime as _runtime_impl
from .seat_lifecycle_runtime import (
    ONBOARDING_ABSENT,
    ONBOARDING_INVALID,
    ONBOARDING_NONE,
    ONBOARDING_VALID,
    Onboarding,
    onboarding_invalid,
    parse_onboarding,
    _record_onboarding,
    _prove_spawned_pane,
    _pane_sendable,
    _SEND_ONLY_DETAIL,
    _live_session_orca_identity,
    _prove_orca_replacement,
    _live_seat_orca_identity,
    _prove_orca_rebind,
    rebind_seat,
    rebind_refusal_means_dead,
    NO_EXACT_LIVE_SESSION,
    NO_DISTINCT_LIVE_PANE,
    _repair_orca_handle,
    _resolve_registered_pane,
    _reap_stale,
    SEAT_ROLES,
    _LEAD_SETTINGS,
    _ROLE_ENV,
    _seat_role,
    _launch_argv,
    _launch_env,
    _launch_command,
    _native_launch_command,
    _resume_role,
    _headless_spawn,
    _register_spawn,
    SPAWN_ATTEMPT_PENDING,
    SPAWN_ATTEMPT_COMPLETE,
    SPAWN_ATTEMPT_INCOMPLETE,
    SPAWN_CHILD_FIELDS,
    SESSION_BINDING_HARNESSES,
    _new_spawn_attempt,
    SPAWN_ATTEMPT_ENV,
    ATTEMPT_OWN,
    ATTEMPT_LIVE,
    ATTEMPT_GONE,
    ATTEMPT_UNVERIFIABLE,
    spawn_attempt_token,
    _publish_spawn_attempt,
    _attempt_process_state,
    _pending_attempt_conflict,
    _finalize_spawn_attempt,
    _backfill_spawn_session,
    _sessionstart_pane_fields,
    _bind_spawn_session,
    _spawn_plan,
    _seat_home_cwd,
    _resume_cwd,
    _stale_resume_cwd,
    _report_rehome,
    _spawn_args,
)

_IMPL_MODULES = (_sessions_impl, _runtime_impl)

def _spawn_path(d):
    return os.path.join(d, "spawn.json")


# The lifecycle lock THIS process is holding for a seat, keyed by the instance
# directory it belongs to — the one thing `_seat_lifecycle_lock_released` needs
# and an flock has no other way to hand back.
_HELD_LIFECYCLE_LOCKS = {}


@_contextlib.contextmanager
def _seat_lifecycle_lock(d):
    """Serialize every read/prove/act/write transition for one seat."""
    import fcntl
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, ".spawn.lock"), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        key = os.path.realpath(d)
        outer = _HELD_LIFECYCLE_LOCKS.get(key)
        _HELD_LIFECYCLE_LOCKS[key] = lock
        try:
            yield
        finally:
            if outer is None:
                _HELD_LIFECYCLE_LOCKS.pop(key, None)
            else:
                _HELD_LIFECYCLE_LOCKS[key] = outer


@_contextlib.contextmanager
def _seat_lifecycle_lock_released(d):
    """Drop THIS process's held lifecycle lock for the body, then take it back;
    yields True when a lock was actually released.

    SHORTEN THE TRANSITION, NEVER REMOVE IT. The lock exists so that two
    concurrent spawns or a spawn and a resume cannot both act on one seat, and
    that is still its job. What it must not span is the part of a spawn that
    WAITS ON THE CHILD: the pane create, the pane-boot grace and the onboarding
    submit. The seat's own first SessionStart runs inside that window, and the
    binder it calls takes this same lock — under a 5s hook timeout. So a spawn
    holding the lock across a 5s send delay plus a submit could get its own
    child's hook killed before it bound the session AND before `chat join`
    initialized the seat's room cursors, which is a seat that starts deaf. The
    record published before the release is what keeps the serialization: the
    attempt on disk is PENDING and authoritative, so another actor arriving in
    this window is refused by that marker rather than by the flock.

    Reacquisition is in the `finally`, so an exception inside the body still
    leaves the caller holding the lock it was called with — every rollback below
    writes state, and writing it unlocked is the race this whole shape is about.
    """
    import fcntl
    lock = _HELD_LIFECYCLE_LOCKS.get(os.path.realpath(d))
    if lock is None:
        # NOT INSIDE THE LOCK AT ALL — a caller on the dry-run/plan path or a
        # direct unit call. Releasing nothing is the honest act, and saying so in
        # the yielded value is what lets an arm tell the two apart.
        yield False
        return
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    try:
        yield True
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)


def _spawn_record(d):
    from . import pk
    rec = pk.read_json(_spawn_path(d), None)
    return rec if isinstance(rec, dict) else None


def _persisted_model(d, seat):
    """The seat's EXPLICIT --model choice recovered from spawn.json; None =
    no choice, follow the family default. This is the ONE door every
    launch.sh writer re-derives through: launch/resume/add all REFRESH the
    assets, and a refresh that silently defaulted to the family rewrote a
    spark seat (model_context 76000) back to sol's 320k — the overstated
    window a wedged seat cannot compact its way out of. Only an explicit
    choice is sticky; a default-following seat (model None) keeps tracking
    the family so a catalog default change still propagates on refresh."""
    rec = _spawn_record(d)
    return rec.get("model") if rec and rec.get("seat") == seat else None


def _pane_live(row):
    """Whether an adapter inventory row is an input-capable live pane.

    `orphaned` is decisive and checked FIRST: orca states it directly — a pane
    can be connected=True and writable=True while its PTY has no live renderer
    (orphaned=True), and reads against it return empty. Keying only off
    `status` (derived from `connected`) is exactly the bug that made every pane
    read blind after the orca .46 remint: the orphaned panes ARE connected and
    writable, so status alone answers True. orca says which; believe it."""
    if row.get("orphaned"):
        return False
    status = str(row.get("status") or "").strip().lower()
    return status not in ("disconnected", "closed", "gone", "exited", "dead")


def _adopt(seat_name, rest):
    """seat adopt <seat> — give an orca-launched seat its OWN home worktree.

    This is the slice-0 coverage gap: per-seat homes were provisioned only at
    helm spawn, so the orca-launched claude seats kept sharing the main
    checkout. It creates the checkout and reports orca's visibility outcome; it
    never touches a running seat, because moving a live seat into its new home
    is a coordinated staggered reseed the integrator runs.
    """
    from . import harness, orcaadopt, seats
    repo = rest[rest.index("--repo") + 1] if "--repo" in rest else None
    base = rest[rest.index("--base") + 1] if "--base" in rest else "main"
    where = repo or seats.safe_cwd()
    root = harness.find_repo_root(where)
    if not root:
        print("helm seat: %s is not inside a git checkout — adopt needs a repo "
              "to make the seat's home in (pass --repo DIR)" % where,
              file=sys.stderr)
        return 2
    try:
        path, detail = orcaadopt.adopt_home(seat_name, root, base=base)
    except harness.HarnessError as e:
        print("helm seat: cannot provision a home for %s: %s" % (seat_name, e),
              file=sys.stderr)
        return 1
    print("helm seat: %s home worktree %s" % (seat_name, path))
    print("  " + detail)
    print("  NOT migrated — the seat keeps running where it is; moving it is a "
          "staggered reseed, not a side effect of this verb.")
    return 0


def _where_adopted(seat_name, rest, unknown_msg):
    """`seat where` for a pane the METAHARNESS launched, not helm.

    Provenance is printed, never elided: an orca-adopted seat has no launch.sh,
    no proxy and no spawn register, so an operator who cannot tell the two apart
    will reach for verbs that do not apply to it. When helm has never heard of
    the name at all, the original "unknown seat" message stands — a typo must
    still look like a typo."""
    from . import orcaadopt
    info = orcaadopt.resolve(seat_name)
    if info is None:
        print("helm seat: " + _unknown_seat_reason(seat_name, unknown_msg),
              file=sys.stderr)
        return 2
    if "--json" in rest:
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0
    print("%s: %s — %s" % (seat_name, info["provenance"], info["state"]))
    print("  evidence: " + info["evidence"])
    if info.get("handle"):
        print("  pane    : %s (orca pane key %s)"
              % (info["handle"], info.get("pane_key")))
    elif info.get("pane_error"):
        print("  pane    : unresolved — " + info["pane_error"])
    else:
        print("  pane    : none resolvable (no live process names this seat)")
    if info.get("pids"):
        print("  pids    : " + ", ".join(str(p) for p in info["pids"]))
    if info.get("session_refused"):
        # The refusal is the ANSWER here, not a footnote: a seat that reads
        # "pane: none resolvable" for this reason has a live pane helm declined
        # to address, and an operator who cannot see why will reach for --force.
        print("  session : DECLINED — " + info["session_refused"])
    if info.get("unidentified"):
        # The pids and the handle above are a REPORT, and this says the census
        # behind them had a hole in it. Printing them without this line would
        # let an operator read a partial census as a complete one — the same
        # overclaim the `unowned` note under `seat panes` prevents.
        print("  census  : INCOMPLETE — " + info["unidentified"])
    print("  sessions: " + (", ".join(s[:8] + "…" for s in info["sessions"])
                            or "(none in helm's chat roster)"))
    print("  note    : no helm spawn register / launch.sh — `helm seat resume "
          "%s` relaunches it from its transcript" % seat_name)
    return 0


REBIND_INTERVAL_S = 300

# THE UNIT RUNS THE SWEEP, NOT BARE REBIND (2026-08-22). `seat rebind --all
# --apply` was a correct guard with no actuator: after the 2026-08-22 reboot
# its journal read "0 rebindable, 0 already current, 9 refused" every pass,
# because every proxy seat's pane had come back as a bare shell (orca replays
# `claude --resume <sid>` with no env, and a proxy seat's session lives under
# its own CLAUDE_CONFIG_DIR). `seat resume --all --apply` performs the same
# rebind pass for every LIVE seat — so nothing the timer did before is lost —
# and relaunches the seats rebind refuses for lack of any process, under the
# fleet-hold marker (helm/seat_resume_all.py). The unit file NAME is unchanged
# so an installed `helm-seat-rebind.timer` keeps working after a re-render.
_REBIND_SERVICE = """[Unit]
Description=helm seat resume sweep (rebind live seats, relaunch reboot-dead ones, one idempotent pass)

[Service]
Type=oneshot
# see proxywatch: no WorkingDirectory => cwd is $HOME => no project => #main
WorkingDirectory=%(cwd)s
ExecStart=%(helm)s seat resume --all --apply
"""

_REBIND_TIMER = """[Unit]
Description=helm seat rebind cadence (external, no demons)

[Timer]
OnBootSec=%(interval)ss
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def rebind_timer_units(interval=REBIND_INTERVAL_S):
    # A persistent unit must never capture a disposable worktree's PATH entry.
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    # WorkingDirectory is DERIVED, never a literal: the unit needs a project
    # cwd (a unit without one runs in $HOME, derives no project, and posts to
    # #main — the reason the line exists), but an operator path baked into a
    # tracked template is both a never-track needle in history and a machine
    # identity this repo cannot carry. The shared checkout is the persistent
    # home; a disposable lane worktree must never become a unit's cwd.
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, "helm-seat-rebind.service"),
            _REBIND_SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-seat-rebind.timer"),
            _REBIND_TIMER % {"interval": interval})


def ensure_rebind_timer(interval=REBIND_INTERVAL_S):
    """(ok, detail) — install + enable the cadence that keeps seat registers
    from outliving their panes.

    THE REBOOT HOLE THIS CLOSES. A seat's register is keyed on its orca
    handle, which dies with the machine, so after a reboot `helm seat where`
    reports the fleet GONE while every seat is demonstrably alive. `seat
    rebind` fixes that — and on 2026-07-28 a HUMAN had to notice and run it,
    hours after the boot, which is not prevention.

    Every sibling RAM-hot store already had its boot leg: dregg-cave.service
    carries ExecStartPre=dregg-cave-restore, the chat node got one the same
    morning, chat history has restore-journal. The register was the last one
    whose repair existed as a verb nobody triggers.

    NOT ExecStartPre / not boot-only, deliberately: at boot the orca panes do
    not exist yet, so a single boot-time pass would find nothing to bind and
    report success over an empty fleet. OnBootSec + OnUnitActiveSec re-runs
    every interval, so the register heals whenever the panes actually come
    back — and equally when a pane is replaced mid-day, which is the same
    staleness arriving by a different route.

    SAFE TO AUTOMATE because rebind refuses rather than guesses: ambiguous
    (two panes claiming one seat), unprovable, or already-current seats are
    left untouched. An automated pass that cannot make a wrong binding is one
    that only ever removes a lie."""
    if interval < 1:
        return False, "interval must be at least 1 second"
    from . import pk
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl unavailable; run `helm seat resume --all --apply` from another scheduler"
    spath, service, tpath, timer = rebind_timer_units(interval)
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", "helm-seat-rebind.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "timer enabled every %ds (%s)" % (interval, tpath)


def _rebind(rest):
    """seat rebind <seat>|--all [--apply] — re-point registers at live panes.

    THE REBOOT VERB. helm keys a seat's register on its orca HANDLE, which is
    per-pane and dies with the machine. So after every reboot `helm seat where`
    reports the whole fleet GONE while the seats are demonstrably alive, and
    `resume`/`spawn` refuse work that needs no work. Measured 2026-07-28: 6 of
    7 seats read GONE with ds4pro and gemini posting in chat at that moment.

    The seat is durable; the handle is not. This walks each registered seat,
    proves the pane its LIVE process occupies now (session -> pid -> /proc, see
    _prove_orca_rebind), and re-stamps the register.

    DRY-RUN DEFAULT. A register rewrite leaves no trace to inspect afterwards,
    so it prints what it would bind and changes nothing until --apply."""
    if "--install-timer" in rest:
        ok, detail = ensure_rebind_timer()
        print("helm seat rebind: %s%s" % ("" if ok else "timer NOT installed — ",
                                          detail),
              file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1
    apply = "--apply" in rest
    names = [a for a in rest if not a.startswith("--")]
    # DECLARED WHERE IT IS READ, not where it is set. The orca refusal below
    # reports on this flag and is reached on the named-seat route too, where
    # the stamp block never runs — so binding it inside `--all` would trade a
    # false completion sentence for a NameError, which is the same defect with
    # a louder failure.
    stamp_ran = False
    if "--all" in rest:
        if names:
            print("helm seat rebind: --all takes no seat name", file=sys.stderr)
            return 2
        registered, blind = registered_seats()
        if blind:
            # A SHORT LIST HERE LOOKS EXACTLY LIKE A COMPLETE ONE. Rebinding
            # the seats we happened to see would silently skip the rest, which
            # is the same omission this walk was fixed to end — so refuse the
            # sweep instead of half-performing it.
            print("helm seat rebind: the seat tree is only partly readable — "
                  "refusing --all rather than rebinding a short list that "
                  "reads as complete; name seats explicitly to proceed",
                  file=sys.stderr)
            return 1
        names = sorted(set(FAMILIES) | set(registered))
        # THE IDENTITY STAMP RIDES HERE AND RUNS BEFORE THE ORCA GATE. It is
        # a ROSTER migration and has nothing to do with panes, so gating it on
        # a metaharness would leave the fleet's legacy rows unmigrated on
        # every box that has none — and an unmigrated row is REFUSED by the
        # lifecycle fence, which is a refusal nobody could then clear.
        from . import seats_roster
        from .seats_common import _clip, _scrub
        # WHAT THE SUFFIX BELOW IS ALLOWED TO CLAIM. Gated on the FLAGS that
        # reach this block, it says "the identity stamp above already ran"
        # over a stamp that printed SKIPPED — a completion sentence beside an
        # explicit failure, on the one route where the migration is the only
        # thing the operator needed. So it follows the OUTCOME instead: set
        # where the stamp actually completed, and nowhere else.
        try:
            stamped, marked, unread = seats_roster.migrate_incarnations(
                apply=apply)
        except OSError as e:
            print("helm seat rebind: identity stamp SKIPPED — the roster "
                  "could not be read (%s); the lifecycle fence will keep "
                  "refusing any unmigrated row" % e, file=sys.stderr)
        else:
            stamp_ran = not unread
            if unread:
                # UNKNOWN IS NOT NOTHING-TO-DO. Reporting an unreadable
                # roster as a clean preview reads identically to a fully
                # migrated fleet, which is the reassuring direction.
                print("helm seat rebind: identity stamp UNKNOWN — %s"
                      % unread, file=sys.stderr)
            elif stamped:
                print("helm seat rebind: identity stamp %s %d legacy row%s "
                      "(%s); %d already marked%s"
                      % ("STAMPED" if apply else "WOULD STAMP", len(stamped),
                         "s"[:len(stamped) != 1],
                         # THE STORED NAME STAYS RAW, THE DISPLAYED COPY DOES
                         # NOT. Migration deliberately supports legacy file
                         # keys, and json.load preserves whatever bytes they
                         # hold — including ESC and bidi format characters —
                         # so joining them straight into a terminal line hands
                         # the roster's content control of the line it rides
                         # in. `_scrub` is the reader-side defense every other
                         # seat label already goes through; identity is
                         # unchanged on disk.
                         ", ".join(_scrub(_clip(name)) for name in stamped),
                         marked,
                         "" if apply else
                         " — NOTHING WAS WRITTEN; re-run with --apply to "
                         "stamp them, which is what the lifecycle refusal "
                         "names"))
    if not names:
        print("usage: helm seat rebind <seat>|--all [--apply]", file=sys.stderr)
        return 2

    from . import harness
    ad = harness.detect()
    if ad is None or getattr(ad, "name", None) != "orca":
        # THE STAMP ALREADY RAN AND IT IS NOT AN ORCA REPAIR, so say what
        # DID happen before refusing the part that did not. A bare refusal
        # here read as "nothing happened" on the one route where the identity
        # migration is the only thing the operator needed.
        print("helm seat rebind: no orca metaharness detected — the pane "
              "rebind is the orca pane-identity repair and has nothing to do "
              "here%s" % (" (the identity stamp above already ran)"
                          if stamp_ran and apply else ""),
              file=sys.stderr)
        return 1

    rebound = failed = unchanged = 0
    for seat_name in dict.fromkeys(names):          # de-dup, keep order
        family, err = _seat_family(seat_name)
        if err:
            continue                                # not a multimodel seat
        d = _instance_dir(family, seat_name)
        if _spawn_record(d) is None:
            continue                                # never registered
        fields, err = rebind_seat(seat_name, d, ad, apply=apply)
        if err:
            failed += 1
            print("helm seat rebind: %-10s REFUSED — %s" % (seat_name, err),
                  file=sys.stderr)
        elif fields.get("unchanged"):
            unchanged += 1
            print("helm seat rebind: %-10s already current (%s)"
                  % (seat_name, fields["handle"][:20]))
        else:
            rebound += 1
            print("helm seat rebind: %-10s %s -> %s%s"
                  % (seat_name, "would bind" if not apply else "BOUND",
                     fields["handle"][:24],
                     "" if apply else "   (dry-run; --apply to write)"))
    print("helm seat rebind: %d rebindable, %d already current, %d refused%s"
          % (rebound, unchanged, failed,
             "" if apply else " — DRY RUN, nothing written"))
    # A refusal is not a failure of the verb: a seat whose process is genuinely
    # gone SHOULD refuse. rc reflects whether the command ran, not whether every
    # seat was rebindable — else a healthy fleet with one dead seat reads red.
    return 0


def registered_seats():
    """(names, blind) — every seat holding a SPAWN REGISTER, including the
    instance seats whose registers live NESTED under
    <family>/instances/<seat>/spawn.json, plus whether the walk could see the
    whole tree.

    THE FLAT SCAN WAS THE BUG, and it defeated the function's own purpose.
    The predecessor listed only <seats>/<name>/ and tested <name>/spawn.json,
    so it returned EXACTLY `FAMILIES` — the instance seats it existed to
    enumerate were the ones it could not see. Measured on the live box
    2026-08-02: registers sit at codex/spawn.json AND
    codex/instances/codex-2/spawn.json; the flat predicate hit five and
    missed two, and the two it missed (codex-2, codex-3) were the seats doing
    the most work on the fleet that night. `seat rebind --all` skipped them
    silently for the same reason.

    A FLAT FIXTURE CANNOT CATCH THIS. Two review rounds went green against
    this defect because their tests staged a flat layout, where the old and
    new walks agree by construction. Every test here stages the REAL nested
    shape; a flat-only fixture is not a weaker test, it is a vacuous one.

    BLINDNESS IS RETURNED, NEVER SWALLOWED. An unreadable directory used to
    collapse into `[]`, which no caller can tell apart from an empty fleet —
    so a discovery failure SHRANK the census instead of alarming it. Callers
    get the flag and must say so; absence of evidence stops being rendered as
    evidence of absence."""
    blind = []

    def failed(path, exc):
        # ABSENT IS AN ANSWER, UNREADABLE IS NOT. No seats directory means a
        # fleet that has never minted a seat — a DEFINITE zero, and the state
        # every clean home starts in; no instances directory is the same.
        # Reporting either as blindness would tell `doctor` a brand-new
        # machine cannot be trusted to be new. Any other listing failure is
        # present but unreadable: SAY SO.
        if not isinstance(exc, FileNotFoundError):
            blind.append(path)

    names = [name for name, path in _register_candidates(failed)
             if os.path.exists(path)]
    return names, bool(blind)


def _register_candidates(failed):
    """(name, spawn.json path) for every place the spawn register can hold a
    record: <seats>/<entry>/spawn.json and
    <seats>/<entry>/instances/<sub>/spawn.json, for every entry under the
    seats root, whether or not the file exists and whatever config sits
    beside it.

    ONE ENUMERATION, TWO READINGS. `registered_seats` keeps the candidates
    that exist and reads a failed listing as blindness; a caller that turns
    the register's silence into authority reads every candidate strictly.
    Both walk this domain, so neither can see a register the other misses.
    `failed(path, exc)` receives every listing that raised,
    FileNotFoundError included, and the walk goes on past it; whether a
    missing listing is absence is the caller's reading.

    A REGULAR FILE HOLDS NO REGISTER. helm writes its own files beside the
    seat directories: allocating a project instance's port creates
    `<seats>/.instance-ports.lock` and `<seats>/instance-ports.json`. Reading
    `spawn.json` or `instances` under such a file raises NotADirectoryError,
    which a strict caller must name as unreadable, so every host that ever
    allocated a port would read UNKNOWN forever. An entry `os.lstat` reports
    as a regular file is skipped at both levels. A directory, a link (a
    dangling one included) and an entry whose lstat raises stay candidates,
    so a strict caller still names what it cannot read."""
    base = os.path.join(home.global_dir(), "seats")
    for name, fam in _register_entries(base, failed):
        yield name, os.path.join(fam, "spawn.json")
        for sub, d in _register_entries(os.path.join(fam, "instances"),
                                        failed):
            yield sub, os.path.join(d, "spawn.json")


def _register_entries(path, failed):
    """[(name, entry path)] under `path`, sorted, without the regular files;
    a listing that raises goes to `failed` and yields nothing."""
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        failed(path, exc)
        return []
    out = []
    for n in names:
        p = os.path.join(path, n)
        try:
            regular = stat.S_ISREG(os.lstat(p).st_mode)
        except OSError:
            regular = False
        if not regular:
            out.append((n, p))
    return out


def _upstream_row(family):
    """(row, error) for one family's cached proxywatch verdict.

    Keep the interpretation beside the display helper so rich liveness and the
    prose suffix cannot disagree about whether the same cached row is usable.
    The snapshot reader owns missing/corrupt/stale; this seam owns the family
    lookup only. A missing family is UNKNOWN, never a healthy row."""
    from . import proxywatch
    up, err = proxywatch.upstream_snapshot()
    if err:
        return None, err
    row = (up or {}).get(family)
    if not isinstance(row, dict) or not row.get("state"):
        return None, "no verdict for %s" % family
    return row, None


def upstream_phrase(family, seat_name=None, compact=False, sample=()):
    """" upstream RATE-LIMITED since ..." for a seat's family, or the UNKNOWN
    form — never silence, and never a healthy-looking blank.

    THE FALSE PICTURE THIS ENDS. `helm seat where codex` printed "LIVE;
    liveness IDLE" while codex had been hard-walled by its provider for three
    hours, and `helm seat list` printed "proxy UP ... valid until 2026-08-13".
    Both true about what they measured — a live pane, a valid credential — and
    both read by four reviewers as "this seat is fine". Measured 2026-08-04:
    that cost them between 20 and 153 minutes each. proxywatch had classified
    the wall the whole time and no surface asked.

    UNKNOWN IS PRINTED, NOT OMITTED. A missing, corrupt, or STALE cache means
    helm cannot say whether the provider is answering — and a blank there reads
    exactly like health, which is the bug one layer over. So every path returns
    a phrase; only a genuinely HEALTHY upstream is quiet, because that is the
    one case where the other fields already tell the truth."""
    row, err = sample if sample else _upstream_row(family)
    # COMPACT is the LIST column, which already carries "⚠ STALE" / "⚠ drift
    # UNKNOWN" markers — a full sentence there would push the cred detail off
    # the line that operators actually scan. Same facts, one column's worth.
    # UNKNOWN IS SAID IN `where` AND WITHHELD IN `list`, AND THAT ASYMMETRY IS
    # THE ATTENTION-BUDGET LAW, NOT A CONVENIENCE. `seat where <seat>` is a
    # DELIBERATE question about ONE seat: silence there reads as health, so the
    # honest answer must be printed. `seat list` is a SCAN of every row — and
    # where proxywatch has never run (a fresh checkout, another box, any repo
    # without the timer) EVERY row would carry the same UNKNOWN badge. A marker
    # on all of them is not information; it is a column operators learn to skim
    # past, and then the one row that says RATE-LIMITED arrives to an audience
    # that has stopped reading.
    #
    # web.py reached the same rule from the other side: "a seat with no proxy
    # family gets NOTHING — not unknown ... a badge there would be noise on
    # every row that can never carry the condition".
    #
    # Caught by the WHOLE suite, not the two files I touched: two
    # test_proxy_staleness integration tests read the column as a contiguous
    # string and my badge split it. The failure was a test artifact; the noise
    # it exposed was not.
    if err:
        return "" if compact else "; upstream UNKNOWN (%s); helm cannot tell " \
            "whether a restart would help" % err
    from . import proxywatch
    state = {"upstream": {family: row}}
    aggregate, aggregate_err = proxywatch.upstream_record(state, family)
    measured, measured_err = proxywatch.upstream_seat_record(
        state, family, seat_name) if seat_name else (None, "no exact seat named")
    if aggregate_err or measured_err:
        why = aggregate_err or measured_err
        return "" if compact else "; upstream UNKNOWN (%s); helm cannot tell " \
            "whether a restart would help" % why
    if aggregate.get("dark") is not True or measured.get("dark") is not True \
            or aggregate.get("state") == "UNKNOWN" \
            or measured.get("state") == "UNKNOWN":
        return ""                       # this exact seat has no measured wall
    rem = upstream_remediation(state, family, seat_name)
    action = remediation_text(rem)
    if compact:
        return " ⚠ upstream %s" % measured["state"]
    return "; upstream %s since %s; %s (%s)" % (
        measured["state"], measured.get("since") or "?", action,
        rem["evidence"])


def measured_seat_route(seat_name):
    """{model, provider, upstream_model} this seat's own proof binds, or None.

    THE SEAT'S ANSWER ABOUT ITSELF HAS TO COME FROM THE PROOF. A seat asked
    which model it runs on reads its launch flag, its name or its family and
    answers with the thing it was NAMED for -- which is exactly right until a
    pool falls back, and then it is a confident wrong answer with no way to
    tell the two apart. proxywatch stamps the measured route against the
    session id; this reads that stamp and nothing else.
    """
    from . import seats
    try:
        rows = seats.roster()
        row = rows.get(seats.canonical_seat(seat_name)) or rows.get(seat_name)
        return seats.resolved_route(row)
    except Exception:                    # noqa: BLE001 — a read never raises
        return None


def seat_model_phrase(seat_name, family=None):
    """`model <alias -> provider/upstream (rung)>`, or why it is unknown."""
    from . import proxywatch
    resolved = measured_seat_route(seat_name)
    if not resolved:
        return ("model UNMEASURED (no proxy runtime proof on this seat's "
                "session; `helm proxywatch` stamps one)")
    return "model " + proxywatch.resolved_model_phrase(resolved, family)


def seat_quota_group_phrase(family):
    """The metered group this seat's family bills, or "" for a family that
    declares none.

    SEPARATE FROM THE MODEL PHRASE ABOVE, because they answer different
    questions and one of them is measured while the other is not. The model
    phrase reads a runtime proof this seat's own session produced; the group
    is a DECLARATION about a vendor's allowance shared with other families,
    and until task/2800 reads the vendor's quota endpoint its remaining
    percent has no reading at all. The antigravity account is why the
    distinction has teeth: three families on one credential, two metered
    groups, and one group has been measured exhausted while the other was
    untouched."""
    from . import seat as seatmod
    return seatmod.quota_group_phrase(family)


def _where(seat_name, rest):
    """seat where <seat> — resolve the spawn register: harness, handle/pid,
    worktree, room, and a liveness probe (headless: the pid; pane: the handle
    still listed by the SAME detected metaharness). The record is what a
    reaper needs; `helm seat spawn <seat>` reaps-then-replaces it."""
    if any(arg != "--json" for arg in rest) or rest.count("--json") > 1:
        print("usage: helm seat where <seat> [--json]", file=sys.stderr)
        return 2
    family, err = _seat_family(seat_name)
    if err:
        # NOT a multimodel family — but that is not the same as unknown. A claude
        # pane orca launched never went through `seat spawn`, so it has no row
        # here while helm's chat roster and the live process both know exactly
        # who it is. Ask the adoption seam before refusing the name.
        return _where_adopted(seat_name, rest, err)
    d = _instance_dir(family, seat_name)
    rec = _spawn_record(d)
    archived = False
    if rec is None:
        from . import seat_exit_owner
        rec, unavailable = seat_exit_owner.latest_archived_exit(d, seat_name)
        if unavailable:
            print("helm seat: archived spawn status for %s is UNKNOWN (%s)"
                  % (seat_name, unavailable), file=sys.stderr)
            return 1
        if rec is None:
            print("helm seat: no current or archived spawn record for %s — "
                  "`helm seat spawn %s` registers one"
                  % (seat_name, seat_name), file=sys.stderr)
            return 1
        archived = True
    alive = False if archived else None
    if not archived:
        if rec.get("seat") != seat_name:
            alive = False
        elif rec.get("harness") == "headless":
            alive = _recorded_pid_alive(rec)
        else:
            ad, handle, detail = _resolve_registered_pane(seat_name, d=d)
            if handle is not None:
                alive = True
                if handle != rec.get("handle"):
                    rec = _spawn_record(d) or rec
            elif ad is not None and "is not live" in detail:
                alive = False
    upstream = _upstream_row(family) if not archived else ()
    liveness = None if archived else seat_liveness(seat_name, upstream)
    resolved = None if archived else measured_seat_route(seat_name)
    if "--json" in rest:
        print(json.dumps(dict(rec, active=not archived, archived=archived,
                              alive=alive, liveness=liveness,
                              resolved=resolved),
                         indent=2, sort_keys=True))
        return 0
    if archived:
        terminal = rec["terminal"]
        ref = ("pid %s" % rec.get("pid")) \
            if rec.get("harness") == "headless" \
            else ("handle %s" % rec.get("handle"))
        print("%s: %s %s — TERMINAL %s at %s (%s); worktree %s, room %s"
              % (seat_name, rec.get("harness"), ref,
                 terminal.get("event"), terminal.get("ts"),
                 terminal.get("reason"), rec.get("worktree"),
                 rec.get("room") or "(derived at join)"))
        return 0
    ref = ("pid %s" % rec.get("pid")) if rec.get("harness") == "headless" \
        else ("handle %s" % rec.get("handle"))
    state = {True: "LIVE", False: "GONE (helm seat spawn %s respawns)"
             % seat_name}.get(alive, "unverified (metaharness %r not "
                              "detected here)" % rec.get("harness"))
    rich = liveness["state"]
    if liveness.get("blocked_on"):
        rich += " (%s)" % liveness["blocked_on"]
    role = _seat_role(rec.get("role")) or "UNKNOWN"
    group = seat_quota_group_phrase(family)
    print("%s: %s %s — %s; role %s; liveness %s%s; %s; %s%sworktree %s, "
          "room %s, spawned %s"
          % (seat_name, rec.get("harness"), ref, state, role, rich,
             upstream_phrase(family, seat_name, sample=upstream),
             seat_model_phrase(seat_name, family),
             group, "; " if group else "",
             rec.get("worktree"), rec.get("room") or "(derived at join)",
             rec.get("ts")))
    return 0


# ---------------------------------------------------------------------------
# seat liveness TRUTH — a richer state than LIVE/GONE
# ---------------------------------------------------------------------------
#
# THE DEFECT THIS CLOSES. `helm seat where` reported LIVE for at least three
# different states that need completely different responses, measured on the
# live fleet 2026-07-25→26: AGENT RUNNING (healthy), AGENT BLOCKED ON A HUMAN
# (codex sat ~14h at a plan-approval prompt holding 4 of 6 open reviews — LIVE,
# silent, needing one keystroke, and read as dead twice), and AGENT EXITED WITH
# THE PANE STILL ALIVE (gemini/grok printed "Resume this session with: claude
# --resume <id>" — the pane exists, the agent does not, `seat where` still said
# LIVE). Every watchdog keys off LIVE/GONE, so all of them are indistinguishable
# to `lr stalls`, the heartbeat, and the idle-dispatch watchdog: a seat blocked
# on a keypress is billed for delay it cannot act on, and a seat whose agent
# exited is assumed to be thinking.
#
# THE EVIDENCE IS THE PANE TAIL, and ONLY the pane tail. Do NOT infer liveness
# from /proc, pgrep, mtime, or cwd — every one of those probes lied tonight
# (/proc mtime is not process start; a pane cwd'd in a worktree says nothing
# about the agent). The pane tail is the BEHAVIOURAL surface: it is what the
# agent is actually doing, rendered by the agent itself.
#
# THE STATES. UNKNOWN is mandatory and never collapses into another value —
# the same discipline as `mix` keeping UNKNOWN in its own bucket and `lr
# withdraw` failing closed. If we cannot prove which state a seat is in, we say
# so; a guessed state is worse than an honest UNKNOWN because a watchdog acting
# on a guess acts wrong.
#
# THE PATTERNS ARE DATA, NOT CONTROL FLOW. A table of (state, patterns) so that
# adding herdr or a new harness is a data change, not a code change. Order
# matters: the FIRST matching state wins, and the order is most-specific-first
# so a pane that is BOTH at a prompt AND mid-turn classifies as mid-turn.

# Each entry: (state, (anchored regex patterns, ANY of which marks the state)).
# Matching is on the pane tail, case-insensitive, most-specific-first.
#: RUNNING's two affordances, named because `_classify_pane_tail` asks a
#: DIFFERENT question of each one (see `_current_turn_affordance`).
#:
#: The proxy families' in-flight affordance. It is drawn in the footer strip,
#: which the TUI repaints on every frame, so it is never left behind above
#: newer output.
_RUNNING_INTERRUPT = r"\besc to interrupt\b"
#: NATIVE CLAUDE RENDERS NO SUCH AFFORDANCE, and this table had never seen a
#: native pane. Measured on the seats that went dark: a turn in flight renders
#: a spinner glyph, a gerund, and an ELAPSED-PLUS-TOKEN counter —
#: "✽ Boondoggling… (3m 19s · ↓ 9.2k tokens)" — with the glyph rotating
#: between reads and the counter advancing.
#:
#: THE COUNTER IS THE ANCHOR, NOT THE GLYPH OR THE WORD. The glyph cycles and
#: the gerund is drawn from a word list nobody outside the vendor controls, so
#: matching either would be matching decoration. A parenthesised elapsed time
#: followed by a token count is the thing the vendor only draws mid-turn.
#:
#: BUT IT IS DRAWN IN THE TRANSCRIPT, NOT THE FOOTER, so unlike
#: `_RUNNING_INTERRUPT` a copy of it can be STRANDED in the viewport — see
#: `_counter_is_current` for the measurement and the discriminator.
_RUNNING_COUNTER = (r"\(\s*\d+(?:h\s*\d+m|m\s*\d+s|[hms])\b"
                    r"[^)]*·[^)]*\btokens?\b")


_LIVENESS_STATES = (
    # RUNNING FIRST, and the ORDER IS THE FIX. The pane tail is SCROLLBACK:
    # `100% context used` and `usage balance exhausted` are printed LINES that
    # persist in the buffer forever, while `esc to interrupt` is a live
    # status-bar affordance rendered ONLY while a turn is in flight. Ordering a
    # persistent line above a live one meant a seat that had recovered still
    # read as stuck.
    #
    # Caught by dogfooding, not by tests: codex hit 100% context, I injected
    # /compact, the compact RAN (esc-to-interrupt present, tasks completing) and
    # seat_liveness still said CONTEXT_FULL — because the historical line was
    # still in the buffer and outranked the live one. A watchdog reading that
    # would keep injecting /compact into a seat already compacting.
    #
    # Interactive WIDGETS are different from printed lines and stay above this:
    # an approval prompt is redrawn away the moment it is answered, and no turn
    # runs while one is open, so BLOCKED_ON_HUMAN and RUNNING cannot both match.
    # RUNNING — a turn is in flight. The `esc to interrupt` affordance renders
    # ONLY while the agent is mid-turn; observed live on exactly the working
    # panes (codex, kimi) and absent on idle/blocked ones (gemini, grok).
    ("RUNNING", (_RUNNING_INTERRUPT, _RUNNING_COUNTER)),
    # AGENT EXITED, PANE ALIVE — the pane offers to resume a dead session.
    # Pattern observed in the brief; gemini/grok have since respawned past it,
    # so it is pinned from the recorded tail, not a live fixture (marked so).
    ("EXITED_PANE_ALIVE", (r"resume this session with: claude --resume",
                           r"claude --resume [0-9a-f-]{8,}")),
    # BLOCKED ON A HUMAN — a plan-approval / permission prompt is the ONLY
    # thing between the agent and continuing, and no amount of waiting helps.
    ("BLOCKED_ON_HUMAN", (r"\b\d+\.\s+yes,?\s+and bypass permissions\b",
                          r"\bbypass permissions\b.*\bdo you want to\b",
                          r"\bdo you want to (proceed|continue|allow)\b")),
    # BLOCKED ON QUOTA/AUTH — a cred or quota wall; no keystroke helps, the
    # remedy is a credential or billing fix. Observed live on grok (402 + 503).
    # BLOCKED ON A VENDOR DIALOG — the seat is PARKED at an interactive menu
    # that only a keystroke clears. Distinct from BLOCKED_ON_QUOTA, which is a
    # statement about ENTITLEMENT: the wall can be over while the menu is still
    # on screen waiting, and that pair is exactly the state three native seats
    # sat in for seventeen hours after a cred switch had already fixed the
    # entitlement. The wall says the seat MAY not work; this says the seat
    # CANNOT, whatever the answer to the first question is.
    #
    # THE OPTIONS ARE COPIED FROM THE PRODUCER, NOT REMEMBERED. The pane tail
    # is a VIEWPORT — measured, ~35 lines, and `limit=40000` returns the same
    # 35 — so a dialog scrolls out of helm's sight the instant it is answered
    # and cannot be read back from a recovered pane. A TUI widget never reaches
    # the transcript either. These literals come from the shipped executable's
    # own strings (a versioned path under the user's claude install), so they
    # move when the vendor's wording moves and a version bump is what updates
    # them.
    ("BLOCKED_ON_VENDOR_PROMPT", (r"^\s*\d+\.\s+continue with usage credits\b",
                                  r"^\s*\d+\.\s+(yes,\s+)?buy usage credits\b",
                                  r"^\s*\d+\.\s+no, keep my current model\b",
                                  r"^\s*\d+\.\s+switch to .*\band continue\b",
                                  r"^\s*\d+\.\s+request more from your admin\b")),
    # BLOCKED ON QUOTA/AUTH — a cred or quota wall; no keystroke helps, the
    # remedy is a credential or billing fix. Observed live on grok (402 + 503).
    #
    # THE NATIVE WALL LINE CARRIES ITS OWN EXPIRY AND THAT IS WHAT MAKES IT
    # SAFE TO MATCH. "You've hit your weekly limit · resets <month day>,
    # <hour><am|pm> (<zone>)" is a PRINTED LINE, so it persists in the viewport
    # long after the wall is over — measured on two seats that were working
    # normally while still rendering it. Matching it alone would brand every
    # recovered seat walled forever, which is the ordering defect the comment
    # at the head of this table already exists for. `_wall_in_force` reads the
    # reset instant out of the matched line and answers UNKNOWN when it cannot
    # parse one, so a wall is claimed only when the producer's own timestamp
    # says it is still in force.
    ("BLOCKED_ON_QUOTA", (r"usage balance exhausted",
                          r"\b402\b.*\b(balance|usage|quota|billing)\b",
                          r"\b503\b.*\bauth_unavailable\b",
                          r"auth_unavailable: no auth available",
                          r"you(?:'|\u2019)?ve hit your (?:weekly|usage) limit\b",
                          r"you(?:'|\u2019)?ve reached your \w+ limit\b",
                          r"you(?:'|\u2019)?ve used your included \w+ usage for this week\b",
                          # THE PROXY POOL'S OWN REFUSAL, rendered by the pane
                          # as "API Error: Request rejected (429) · <this>".
                          # Its reset clause is a DURATION with no timestamp,
                          # so `_wall_in_force` dates it from an observation
                          # instant the caller supplies (helm/poolwall.py
                          # reads it off the seat's proxy.log) and answers
                          # UNANCHORED — never a wall — when none is given.
                          r"no available credential for \S+(?: via provider \S+)?: "
                          r"\d+ cooling down \(reset in [^)]*\)")),
    # CONTEXT FULL — the autocompact boundary; the next turn may hang. Observed
    # live ("100% context used"). Distinct from quota: the fix is a compact,
    # not a cred.
    ("CONTEXT_FULL", (r"100% context used",
                      r"\bcontext (window )?(is )?full\b",
                      r"\bcompact (the )?(conversation|context) to continue\b")),
    # IDLE — at the CURRENT composer with nothing in flight. A `❯` preserved in
    # scrollback is history, not state; `_current_prompt_line` below requires that
    # only recognized status chrome follows it. This remains the fallback for a
    # live pane that is not running, blocked, or exited.
    ("IDLE", (r"^\s*❯",)),
)

# LIVE (#141): process-evidence-only — a named process exists, which is WEAKER
# than RUNNING's pane-tail proof of a turn in flight. Consumers must not give
# LIVE the free pass RUNNING gets; every existing switch either names its
# states explicitly or whitelists RUNNING/IDLE, so LIVE falls through to the
# conservative arm by construction (matrix re-derived at admission).
# WALLED is availability evidence composed from proxywatch, not a pane-tail
# pattern: the pane may be sitting at a healthy empty composer while its family
# cannot complete a request. Keeping it out of _LIVENESS_STATES prevents an
# upstream verdict from pretending to be text rendered by the agent.
_STATE_NAMES = tuple(name for name, _ in _LIVENESS_STATES) + ("WALLED", "LIVE",
                                                              "GONE", "UNKNOWN")
RESTART_HELPFUL = "HELPFUL"
RESTART_NOT_HELPFUL = "NOT_HELPFUL"
RESTART_UNKNOWN = "UNKNOWN"


def _unknown_remediation(evidence="the upstream record cannot derive whether a restart would help"):
    return {"restart": RESTART_UNKNOWN, "target": None,
            "evidence": evidence, "action": None}


def remediation_text(remediation):
    """One operator sentence for every remediation consumer."""
    rem = remediation if isinstance(remediation, dict) else \
        _unknown_remediation()
    if rem.get("restart") == RESTART_HELPFUL and \
            rem.get("target") == "proxy" and rem.get("action"):
        return rem["action"]
    return "remediation UNKNOWN: cannot derive whether a restart would help (%s)" \
        % (rem.get("evidence") or "no readable remediation evidence")


def upstream_remediation(state, family, seat_name):
    """Restart capability derived from one exact persisted seat verdict."""
    from . import proxywatch
    rec, err = proxywatch.upstream_seat_record(state, family, seat_name)
    if not err and rec.get("dark") is True and \
            rec.get("state") == proxywatch._PROXY_LOCAL_403:
        # Ours, but no clock says a restart alone would clear it: the proxy
        # states its own reason, and that is the first thing to read.
        return _unknown_remediation(
            "our proxy refused this itself (PROXY-LOCAL-403); read the "
            "proxy's stated reason, fix it, restart and probe")
    if err or rec.get("dark") is not True or \
            rec.get("state") != proxywatch._PROXY_COOLDOWN:
        return _unknown_remediation(
            err or "the recorded seat wall has no measured local restart capability")
    due = rec.get("falsification_due")
    age, bar = rec.get("falsification_age_s"), rec.get("falsification_bar_s")
    measured_seat = rec.get("falsification_seat")
    numeric_age = isinstance(age, (int, float)) and not isinstance(age, bool) \
        and math.isfinite(age) and age >= 0
    valid_bar = type(bar) is int and bar > 0
    seat_family, seat_err = _seat_family(seat_name)
    if due is not True or not numeric_age or not valid_bar or age < bar or \
            measured_seat != seat_name or seat_err or seat_family != family:
        return _unknown_remediation(
            "the local cooldown record does not establish one internally-consistent stale falsification clock for this seat")
    fact = ("Local proxy cooldown evidence for %s is %dm old "
            "(falsification bar %dm) and is now stale" %
            (seat_name, int(age) // 60, int(bar) // 60))
    return {"restart": RESTART_HELPFUL, "target": "proxy", "evidence": fact,
            "action": "%s. PRESCRIBES: restart this exact proxy, then rerun "
                      "helm proxywatch." % fact}


_LIVENESS_REMEDIATION = {
    "BLOCKED_ON_QUOTA": (RESTART_UNKNOWN, None,
                         "the pane proves a quota/auth blocker but not whether its origin is local or upstream"),
    "BLOCKED_ON_HUMAN": (RESTART_NOT_HELPFUL, "seat",
                         "the measured blocker is a pending human prompt"),
    "CONTEXT_FULL": (RESTART_NOT_HELPFUL, "seat",
                     "the measured blocker is context pressure, whose recovery is compaction"),
    "RUNNING": (RESTART_NOT_HELPFUL, "seat",
                "the measured seat is taking a turn"),
    "EXITED_PANE_ALIVE": (RESTART_HELPFUL, "seat",
                          "the measured agent process is not running"),
    "GONE": (RESTART_HELPFUL, "seat",
             "the measured agent process is not running"),
}


def _with_remediation(row):
    """Attach one capability verdict derived from this exact liveness row."""
    state = row.get("state")
    if state == "WALLED":
        row["remediation"] = upstream_remediation(
            row.pop("upstream_remediation_state", None),
            row.pop("upstream_family", None), row.get("seat"))
        return row
    restart, target, why = _LIVENESS_REMEDIATION.get(
        state, (RESTART_UNKNOWN, None,
                "this liveness state cannot tell whether a restart would help"))
    row["remediation"] = {"restart": restart, "target": target,
                          "evidence": why, "action": None}
    return row


_PANE_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PANE_PROMPT = re.compile(r"^\s*❯")
_PANE_RULE = re.compile(r"^\s*[─━]{3,}\s*$")
_PANE_RULE_COMPOSER = re.compile(r"^\s*[─━]{2}([^─━].*)$")
# Status chrome Claude renders BELOW the live composer: the input-box rules,
# the ⏵⏵ permissions/status line, and — whenever the task list is visible
# (ctrl+t) — the agents strip (`● main`, `◯ claude  <desc>   12m 48s`).
# Measured live on a codex seat at exactly 100% context: the strip rows
# read as "newer semantic content", so a VERIFIED-EMPTY composer returned None,
# autocompact refused "composer contains unsent input", and the watchdog was
# blind at precisely its target state. The agent-row alternatives are pinned
# tight — a marker plus a bare agent name, or marker+name+2-plus-space gap
# before the description — because transcript bullets ("● API Error: 400 …",
# "● Monitor event: …") are single-spaced prose and MUST keep not matching:
# over-matching here lets a historical ❯ impersonate the live composer, the
# reverse defect.
#
# TWO MORE CHROME FAMILIES, measured live 2026-08-05 across all 26 fleet panes
# and both load-bearing: with only the rules above, `_current_prompt_line`
# returned None on SEVEN of the FOURTEEN readable panes — half the fleet — so
# every composer question (IDLE liveness, autocompact's unsent-input guard, and
# the submit read-back this file now backs) was silently unanswerable there.
#   * the MODEL/CWD row Claude prints under the input box:
#       `  opus-5 | ~/dev/akapug/helm`, `  fable-5 | ~/dev/akapug/other`,
#       `  opus-5[1m] | ~/dev/akapug/helm                            /rc`
#     Pinned to a model-shaped token, a pipe, and a path that must start `~/`
#     or `/` — prose with a stray pipe cannot reach it.
#   * the agents-strip row carrying a fan-out count, `◯ claude (+1)  <desc>`,
#     which the existing alternatives miss on the `(+N)` alone.
# The update toast is deliberately NOT a global line regex: only the measured
# `Update installed` + `Restart to update` pair (or its combined row) is chrome,
# so either exact phrase in transcript output cannot bless a historical prompt.
_PANE_CHROME = re.compile(
    r"^\s*(?:⏵⏵(?:\s|$)|[─━]{3,}\s*$"
    r"|[●◯◉]\s+\S+(?:\s+\(\+\d+\))?\s*$"           # `● main`, `◯ claude (+1)`
    r"|[●◯◉]\s+\S+(?:\s+\(\+\d+\))?\s{2,}\S.*$"    # marker+name+gap+desc
    r"|[\w.\-]+(?:\[[^\]\s]*\])?\s+\|\s+[~/]\S*\s*(?:\S+\s*)?$)")  # model | cwd


_UPDATE_TOAST = re.compile(
    r"^\s*Update installed\s*(?:·|—|-)\s*Restart to update\s*$")


def _pane_chrome_tail(lines):
    """True only for structural footer rows, including the paired update toast."""
    i = 0
    while i < len(lines):
        line = lines[i]
        if _UPDATE_TOAST.match(line):
            i += 1
            continue
        if line.strip() == "Update installed":
            if (i + 1 < len(lines)
                    and lines[i + 1].strip() == "Restart to update"):
                i += 2
                continue
            return False
        if line.strip() == "Restart to update" or not _PANE_CHROME.match(line):
            return False
        i += 1
    return True


def _current_prompt_line(tail):
    """The current composer line, never a prompt preserved in scrollback.

    Claude renders status chrome below the composer, so the prompt need not be
    the final non-empty line. Anything else after it is newer semantic content
    and makes the pane UNKNOWN rather than turning historical chrome into IDLE.

    Claude Code 2.1.266's narrow frame can lose the prompt glyph and render the
    draft after two rule glyphs, following two complete input-box rules. That
    exact measured structure is canonicalized back to a prompt line here; a
    bare ``──text`` transcript row never earns composer authority by itself.
    """
    lines = [_PANE_ANSI.sub("", line) for line in (tail or "").splitlines()]
    visible = [line for line in lines if line.strip()]
    for i in range(len(visible) - 1, -1, -1):
        line = visible[i]
        if _PANE_PROMPT.match(line):
            return line if _pane_chrome_tail(visible[i + 1:]) else None
        rule = _PANE_RULE_COMPOSER.match(line)
        # Unlike a classic prompt, the measured rule-attached draft has no
        # lower rule or footer: it is the BOTTOM visible row. Allowing chrome
        # after it lets an identical historical row in scrollback impersonate
        # the current composer. Structured Orca drafts use their own owner field
        # and never need this fallback relaxed.
        if not rule or i != len(visible) - 1 or i < 2 \
                or not _PANE_RULE.match(visible[i - 1]) \
                or not _PANE_RULE.match(visible[i - 2]):
            continue
        return "❯\xa0" + rule.group(1).strip()
    return None


#: THE ONE ROW THAT PROVES A COUNTER IS SPENT: the turn-landed summary the
#: producer writes IN PLACE OF the counter when a turn ends ("✻ Cogitated for
#: 10m 25s · done 10:51 AM"). Finding one BELOW a counter is that counter's own
#: replacement, one frame later, and the producer never writes it while a turn
#: is open — so it is the only row on the screen that means "over" rather than
#: "later".
#:
#: TWO SHAPES WERE TRIED AND BOTH WERE MEASURED WRONG, in opposite directions,
#: and the list is here so neither is re-proposed:
#:
#:   1. A WHITELIST of the furniture allowed to follow a live counter. The
#:      producer draws a different mix under every pane — a usage tip that
#:      WRAPS onto continuation rows, a compact PROGRESS BAR, a truncated
#:      context gauge, an update toast with and without a leading check, an
#:      input-box rule with and without a title — so every omission demoted a
#:      live turn. Two running seats read IDLE against it, one mid-compact.
#:   2. A BLACKLIST that added the ASSISTANT BULLET to this row, on the
#:      reasoning that output below a counter proves the TUI painted past it.
#:      That is the CL96 regression found on task/2808, and it made
#:      this predicate WORSE THAN THE TABLE IT REPLACED. A frame is painted in
#:      PIECES and assistant output lands BEFORE the counter repaints, so a
#:      live turn sampled in that window renders a counter with a bullet under
#:      it — the same rows a spent frame renders, in the same order, with the
#:      same input box below them. MAIN KEPT THAT TAIL RUNNING; the blacklist
#:      called it IDLE, and `autocompact._fire` injects `/compact` into an IDLE
#:      pane with no liveness re-read, so the cost is a live turn's work rather
#:      than a delayed compaction. A PARTIALLY PAINTED FRAME IS A THIRD STATE
#:      and a bullet cannot tell it from a completed one — store premise
#:      `unreadable-and-empty-must-never-share-a-value`.
#:
#: WHICH WAY THIS FAILS IS CHOSEN. Every row that is not the turn-landed
#: summary — decoration, output, an input box, an unfinished frame — leaves the
#: counter believed and the pane RUNNING, the same direction the wall rungs in
#: this module fail toward: a refusal to compact is visible and recoverable, a
#: demoted live turn is not.
#:
#: AND IT IS MATCHED AS THE PRODUCER'S WHOLE ROW, NEVER AS A PHRASE. The first
#: cut searched for "· done H:MM am" anywhere below the counter, and a phrase
#: can be QUOTED: a seat that reads this module, its fixtures or a chat row
#: about them prints that exact text as tool output, and in the partial-paint
#: window that output sits below a counter that has not repainted yet. Probed
#: on the reviewed tip: a live frame whose output quoted the fixture's own
#: turn-landed line classified as not RUNNING, where main kept it RUNNING — the
#: one failure direction this predicate exists to refuse, since `/compact`
#: then lands in live work. The producer draws its row at COLUMN ZERO as one
#: glyph, a word, "for", an elapsed time, then the phrase. Quoted output is
#: indented under its tool box, so it cannot start at column zero.
#:
#: THE GLYPH IS A WHITELIST, because a blacklist of bullets fails OPEN. "Any
#: glyph except the assistant bullet" admits every bullet nobody listed, and the
#: recorded panes draw U+25CF where a reader would type U+23FA from memory: an
#: assistant line quoting the row at column zero then takes the producer's
#: shape, and a live frame reads IDLE with an empty composer, which is the pair
#: that actuates `/compact`. A whitelist fails the way this predicate is built
#: to fail: a glyph the vendor adds later makes the pattern MISS, the counter
#: stays believed and the pane stays RUNNING, which is what main does. The set
#: is the asterisk dingbats the producer's spinner cycles through; the recorded
#: row uses the first of them.
_TURN_LANDED_GLYPHS = "✻✽✶✢✳"
_TURN_LANDED = re.compile(
    r"^[" + _TURN_LANDED_GLYPHS + r"][ \t]+\S+ for \d[^\n]*·\s*done\s+"
    r"\d{1,2}:\d{2}\s*[ap]m\b", re.I | re.M)


def _counter_is_current(tail, match):
    """Is this elapsed-plus-token counter THIS frame's, or stranded scrollback?

    THE COUNTER IS NOT A FOOTER AFFORDANCE, and that is the whole defect. The
    table above reasoned that the counter "cannot be present unless a turn is
    running" and therefore matched it anywhere in the tail. `esc to interrupt`
    really is repainted footer chrome, so anywhere-in-the-tail is safe for it.
    The counter is drawn INSIDE THE TRANSCRIPT, one row above the input box,
    and when the viewport scrolls a copy is left behind above newer output and
    is never repainted again.

    MEASURED LIVE ON THE FLEET, both directions in one sweep of all 26 panes:

      * an IDLE seat (a client lead, pane term_659ccc8b) whose last turn ended
        "✻ Cogitated for 10m 25s · done 10:51 AM", composer empty, footer
        carrying no interrupt affordance — and line 3 of its 38-line viewport
        still reading "- Dilly-dallying… (35m 5s · ↓ 97.3k tokens)", a whole
        stale frame (its own rule/❯/model rows included) stranded above the
        live transcript. `_classify_pane_tail` answered RUNNING. Read three
        times over minutes the counter was byte-identical at 35m 5s: it is
        FROZEN, so it never ages out and the misreading is permanent.
      * three seats genuinely mid-turn whose counters ADVANCED between reads
        and sat at the bottom of the transcript — one of them mid-compact,
        with a progress bar and a live agents strip under it.

    That is why autocompact can refuse a healthy idle seat forever (task/2808):
    the seat's own beacon Monitor firing is what repaints the pane and strands
    the frame, its context only climbs, and a turn that never lands never
    yields the empty composer that would make it eligible.

    THE DISCRIMINATOR IS POSITION, NOT TIME — the same rule
    `_current_prompt_line` applies to a composer glyph and `_worked_since`
    applies to a wall line. A counter with its own REPLACEMENT below it belongs
    to a frame the TUI has already finished with; see `_TURN_LANDED` for why
    that single row is the whole test, and for the two richer shapes that were
    built, measured wrong and removed.

    A PARTIALLY PAINTED FRAME IS THE THIRD STATE, and it is why nothing else
    below the counter may vote. A frame is painted in PIECES, and assistant
    output lands before the counter repaints, so a live turn sampled in that
    window shows a counter, fresh output under it, and the input box the
    previous paint left standing — position for position what a spent frame
    shows. No row ORDER separates them. Only a row whose MEANING is "the turn
    is over" does, and the producer writes exactly one. Everything else is
    unresolved, and unresolved is RUNNING: refusing to compact an idle seat
    costs a delayed compaction; compacting a live one costs the turn.
    """
    rest = tail[match.end():]
    cut = rest.find("\n")
    if cut == -1:
        return True                        # the counter line is the last line
    return not _TURN_LANDED.search(_PANE_ANSI.sub("", rest[cut + 1:]))


def _current_turn_affordance(tail):
    """The live RUNNING affordance in `tail`, or None when every one is stale.

    THE LAST COUNTER IS THE ONLY ONE THAT CAN BE LIVE, so this resolves the
    occurrence once and judges THAT one — the lesson BLOCKED_ON_QUOTA already
    paid for. `re.search` returns the FIRST match, and a viewport routinely
    carries a stranded counter above the live one; judging the first would
    demote a running turn while the real affordance sat lower and unexamined.
    """
    m = re.search(_RUNNING_INTERRUPT, tail, re.I | re.M)
    if m:
        return m
    last = None
    for m in re.finditer(_RUNNING_COUNTER, tail, re.I | re.M):
        last = m
    if last is None or not _counter_is_current(tail, last):
        return None
    return last


# The reset clause the native wall line carries, e.g.
# "resets <Mon> <day>, <hour><am|pm> (<zone>)". The YEAR IS ABSENT from the
# producer's rendering, which is the whole reason this is parsed rather than
# handed to a general date reader: the year has to be inferred, and inferring
# it wrong in December is how a live wall reads as ancient history.
# The remedy hints the producer prints directly UNDER a wall line. They are
# part of the wall message, so they are not evidence the seat worked past it.
# Copied from the shipped executable's strings alongside the wall lines
# themselves.
_WALL_CONTINUATION = re.compile(
    r"^\s*(?:/usage-credits\b|/rate-limit-options\b|/model\b"
    r"|run /usage-credits\b|switch to another model\b)", re.I)

_WALL_RESET_RE = re.compile(
    r"resets\s+([A-Z][a-z]{2})\s+(\d{1,2})\s*,\s*(\d{1,2})\s*([ap])m", re.I)
_WALL_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec")
WALL_IN_FORCE = "IN_FORCE"
WALL_EXPIRED = "EXPIRED"
WALL_UNDATED = "UNDATED"
#: A wall line whose expiry is a DURATION from an observation instant this
#: reader was not given, or a duration it cannot read. Distinct from UNDATED
#: on purpose: UNDATED keeps the historical "treat it as a wall" meaning for
#: spellings that never carried a clause, while UNANCHORED never walls — the
#: pool refusal is cheap to re-hit and loud when re-hit, and a wall with no
#: end would be a seat nobody can wake.
WALL_UNANCHORED = "UNANCHORED"
_POOL_RESET_RE = re.compile(r"cooling down \(reset in ([^)]*)\)", re.I)


def _pool_expiry(line, observed):
    """The reset instant of a pool refusal line, or None.

    ``observed`` is the instant the line was produced — a naive local
    datetime, or a callable ``line -> datetime | None`` (poolwall.pane_anchor
    resolves it from the seat's proxy.log by message equality). The duration
    is added to THAT instant; the read-time clock never enters."""
    m = _POOL_RESET_RE.search(line or "")
    if not m:
        return None
    from . import poolwall
    reset_s = poolwall.parse_reset(m.group(1))
    if reset_s is None:
        return None
    at = observed(line) if callable(observed) else observed
    if not isinstance(at, datetime.datetime):
        return None
    return at + datetime.timedelta(seconds=reset_s)


def _wall_in_force(line, now=None, observed=None):
    """IN_FORCE / EXPIRED / UNDATED / UNANCHORED for a matched quota line.

    A POOL REFUSAL LINE IS DATED FROM ITS OBSERVATION INSTANT. Its clause is
    "reset in 2h27m37s" — a duration — so the expiry is that duration past
    the instant the producer wrote the line, which ``observed`` supplies (see
    `_pool_expiry`). Without one, or with a clause this build cannot read,
    the answer is UNANCHORED and the classifier moves on: the pool wall's
    loud direction is a re-hit refusal, not a seat nobody can wake.

    WHY A PRINTED WALL LINE NEEDS AN EXPIRY AT ALL. The pane tail is
    SCROLLBACK, and the native wall line is a printed line rather than a
    redrawn widget, so it stays in the viewport after the wall is over.
    Measured on two seats that were working normally and still rendering
    "You've hit your weekly limit · resets <instant>". Matching the text
    alone would brand every recovered seat walled for as long as the line
    survives — the same ordering defect the head of this table records, where
    a compacting seat kept reading CONTEXT_FULL off a historical line.

    THE PRODUCER PUTS ITS OWN EXPIRY IN THE LINE, so this needs no threshold
    to tune and cannot mistake a long legitimate turn for a stall: a reset
    instant in the future means the wall is still in force, and one in the
    past means the line is history.

    UNDATED IS ITS OWN ANSWER and never silently becomes either of the others.
    The pre-existing patterns (`usage balance exhausted`, the 402 and 503
    lines) carry no reset clause and are UNDATED by construction — they keep
    their historical meaning, which is that the caller treats them as a wall.
    A DATED line whose clause this cannot parse is also UNDATED, and that is
    deliberate: a vendor rewording must fail toward the loud state, because a
    false wall is visible and recoverable while a false clean bill is the
    seventeen-hour silence this whole lane exists for.

    THE YEAR IS INFERRED AND THE RULE IS STATED: the producer prints no year,
    so this takes the interpretation that puts the instant nearest to now,
    which is what a reader means by "resets Sep 15" on September 12th and is
    still right for a wall printed on December 30th that resets January 2nd.
    """
    if _POOL_RESET_RE.search(line or ""):
        expiry = _pool_expiry(line, observed)
        if expiry is None:
            return WALL_UNANCHORED
        now = datetime.datetime.now() if now is None else now
        return WALL_IN_FORCE if expiry > now else WALL_EXPIRED
    m = _WALL_RESET_RE.search(line or "")
    if not m:
        return WALL_UNDATED
    mon, day, hour, half = m.group(1).lower(), m.group(2), m.group(3), m.group(4).lower()
    if mon not in _WALL_MONTHS:
        return WALL_UNDATED
    try:
        day, hour = int(day), int(hour)
    except (TypeError, ValueError):
        return WALL_UNDATED
    if hour == 12:
        hour = 0
    if half == "p":
        hour += 12
    now = datetime.datetime.now() if now is None else now
    best = None
    # THREE CANDIDATE YEARS, NEAREST WINS — the December-to-January case is
    # the one a naive "this year" reading gets wrong, and it is exactly when a
    # weekly wall is most likely to straddle the boundary.
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            cand = datetime.datetime(year, _WALL_MONTHS.index(mon) + 1, day, hour)
        except ValueError:
            continue                        # Feb 30 and friends: not a date
        if best is None or abs(cand - now) < abs(best - now):
            best = cand
    if best is None:
        return WALL_UNDATED
    return WALL_IN_FORCE if best > now else WALL_EXPIRED


def _classify_pane_tail(tail, observed=None):
    """(state, blocked_on) from a pane tail, most-specific-first.

    blocked_on is a short human string for BLOCKED_* and CONTEXT_FULL states —
    WHAT the seat is waiting on — or None. The remedy for a blocked seat is
    owner- or integrator-actionable only if it names the blocker, so a bare
    state without it is useless. The patterns are DATA (see _LIVENESS_STATES);
    a state with no matching pattern falls through to the next.

    ``observed`` dates a pool refusal line (see `_pool_expiry`); a caller
    that has no observation instant leaves it None and such a line reads
    UNANCHORED, which never walls.
    """
    low = _PANE_ANSI.sub("", tail or "")
    for state, patterns in _LIVENESS_STATES:
        current = _current_prompt_line(low) if state == "IDLE" else low
        if current is None:
            continue
        if state == "RUNNING":
            # ONE LIVE AFFORDANCE, RESOLVED ACROSS BOTH SPELLINGS, JUDGED ONCE
            # — and the two spellings are not the same kind of evidence. The
            # footer's interrupt affordance is repainted every frame; the
            # native counter is transcript content a scroll can strand. Asking
            # `re.search` for either one anywhere in the tail read a FROZEN
            # counter as a turn in flight on a demonstrably idle seat, and
            # that refusal is permanent because nothing ever repaints it.
            m = _current_turn_affordance(current)
            if m is None:
                continue
            return state, _blocked_detail(state, m, low)
        if state == "BLOCKED_ON_QUOTA":
            # ONE CURRENT WALL, RESOLVED ACROSS EVERY SPELLING, JUDGED ONCE.
            #
            # A quota wall has seven spellings in the table and a pane can
            # carry several of them at once — a credential wall, an answer, a
            # weekly-limit wall, a clean prompt. Asking each pattern its own
            # question makes the two rungs read DIFFERENT occurrences:
            # recovery already anchored on the last wall across ALL patterns,
            # while expiry dated whichever pattern happened to match first. On
            # a pane carrying an undated wall, real work, and a LATER wall
            # whose reset instant has passed, the undated line answered
            # BLOCKED and a demonstrably working seat read walled.
            #
            # So the occurrence is resolved ONCE, for the whole state, and
            # both rungs judge THAT match. Whichever wall is last in the tail
            # is the only one that can still be in force; everything above it
            # is scrollback by construction.
            m = _current_wall(current)
            if m is None:
                continue
            # WORK BELOW THE CURRENT WALL IS RECOVERY. Position, not time.
            if _worked_since(low, m):
                continue
            # A WALL LINE OUTLIVES THE WALL. The producer prints its own reset
            # instant, so an EXPIRED line is history and the pane must be
            # classified by whatever else is on it — the loop continues rather
            # than returning. UNDATED keeps the historical meaning for the
            # patterns that carry no reset clause, and fails loud for a dated
            # line this build can no longer parse.
            line = _matched_line(low, m)
            verdict = _wall_in_force(line) if observed is None else \
                _wall_in_force(line, observed=observed)
            if verdict in (WALL_EXPIRED, WALL_UNANCHORED):
                continue
            return state, _wall_detail(m, verdict, line, observed)
        for pat in patterns:
            m = re.search(pat, current, re.I | re.M)
            if not m:
                continue
            return state, _blocked_detail(state, m, low)
    return None, None


def _worked_since(tail, match):
    """Did the agent produce anything AFTER this wall line?

    THE RESET INSTANT IS NOT SUFFICIENT AND MEASUREMENT IS WHAT SAID SO. A
    reset clause dates the wall on the account that hit it, and a cred SWITCH
    moves the seat to a different account — so a seat can be working perfectly
    while the old wall's instant is still hours in the future. Measured: three
    seats were healthy while still rendering a reset instant three days out,
    and an expiry check alone would have called all three walled.

    WHAT ACTUALLY SETTLES IT IS POSITION, NOT TIME. A wall line is scrollback;
    anything the agent RENDERS BELOW IT is proof the wall is not binding now,
    whatever any timestamp says and whatever account it came from. That is the
    same rule `_current_prompt_line` already applies to a composer glyph —
    newer semantic content demotes an older line to history — and it needs no
    clock, no threshold and no knowledge of which credential is in play.

    THREE THINGS BELOW A WALL ARE NOT WORK, and the first cut counted all
    three — which cleared the wall on a genuinely parked pane, the one case
    this exists to catch.

    (1) THE WALL'S OWN CONTINUATION. The producer prints a remedy hint
    immediately under the limit line ("/usage-credits to finish what you're
    working on."). It is part of the message, not a reply to it.

    (2) A BARE COMPOSER GLYPH. An empty prompt under the wall IS the parked
    state; reading it as progress inverts the answer exactly when it matters.
    A composer carrying TEXT is different — that is a human or a hook having
    submitted something, which is real evidence the seat moved.

    (3) FOOTER CHROME. Rules, the model line, the permissions line and the
    update toast are redrawn continuously and are present under a parked pane
    too.
    """
    # DECIDE AGAINST THE LATEST BLOCKING EVIDENCE, NOT THE FIRST MATCH.
    # `match` is whatever the classifier's `re.search` found first, and a tail
    # routinely carries the wall more than once: an old wall, an answer, then
    # a NEW wall when the next attempt hits the same exhausted credential.
    # Scanning forward from the FIRST wall finds that intervening answer and
    # clears a seat that is walled RIGHT NOW — the newest wall is never even
    # examined. Skipping wall lines one at a time cured the adjacent-repeat
    # shape and left this one, which is the same defect with a line between.
    #
    # So the anchor is the LAST wall in the tail, across every spelling the
    # classifier knows, and only what follows THAT can be recovery.
    # THE CALLER ALREADY RESOLVED WHICH WALL THIS IS, and re-deriving it here
    # is what put two coordinate spaces in one comparison. `match` comes from
    # `wall_occurrences` over THIS text; its end is an offset into THIS text.
    rest = tail[match.end():]
    end = rest.find("\n")
    if end == -1:
        return False                        # the wall line is the last line
    for raw in rest[end + 1:].splitlines():
        line = _PANE_ANSI.sub("", raw)
        if not line.strip():
            continue
        if _WALL_CONTINUATION.match(line):
            continue
        if _PANE_CHROME.match(line) or _UPDATE_TOAST.match(line) \
                or line.strip() in ("Update installed", "Restart to update"):
            continue
        # (4) A WALL RE-RENDERED IS THE SAME WALL, NOT A REPLY TO IT. A pane
        # sitting on an exhausted credential prints its error again on the next
        # attempt, so the tail carries the line twice. Read positionally, the
        # SECOND copy sits below the first and satisfies every test above — it
        # is not the producer's hint, not chrome, not a composer — so it read
        # as work and cleared the very wall it re-states. Measured against the
        # base: `usage balance exhausted` twice over a bare composer classified
        # BLOCKED_ON_QUOTA before this function existed and IDLE after it,
        # which is a seat reporting itself available while it is walled.
        #
        # The patterns are READ FROM THE LIVE TABLE rather than restated here,
        # so a wall spelling added to the classifier cannot silently go missing
        # from this rung and re-open the same hole.
        if _is_wall_line(line):
            continue
        if _PANE_PROMPT.match(line):
            # A COMPOSER IS NEVER WORK, WITH OR WITHOUT TEXT. An empty one is
            # the parked state itself; one carrying text is an UNSUBMITTED
            # DRAFT — bytes sitting in front of a prompt that nobody sent. The
            # earlier reading called text a submission, which let a half-typed
            # line clear a live wall: the least-verified thing on the screen
            # was being treated as proof the seat had moved.
            continue
        return True
    return False


def wall_occurrences(tail):
    """Every quota wall in `tail`, ordered by where it ENDS.

    ONE SCAN, ONE TEXT, ONE COORDINATE SPACE, and that is the whole point of
    this function existing. Two rungs ask about the same walls — which one is
    current, and whether anything was rendered after it — and a SEPARATE scan
    per rung, one over the ANSI-cleaned text and one over a LOWERCASED copy,
    passes an offset from the second into the first.

    Offsets are not portable across `str.lower()`. Lowercasing is not
    length-preserving: a capital I-with-dot lowercases to TWO codepoints, so
    every offset after it shifts by one. Measured: the tail
    "Ipek / usage balance exhausted / a completed-work line / a bare composer"
    answers IDLE, and the SAME tail with the dotted capital answers
    BLOCKED_ON_QUOTA, because the shifted end consumed the wall's newline and
    the recovery rung then skipped the work line entirely. A seat that had
    plainly resumed read walled on the strength of one character in an
    unrelated word.

    So nobody re-derives an offset here. The scan happens once, the caller
    takes whichever occurrence it needs from this list, and both rungs are
    looking at the same object by construction. `re.I` still handles the
    case-insensitivity the lowercased copy was there for.
    """
    found = []
    for pat in _wall_patterns():
        found.extend(re.finditer(pat, tail or "", re.I | re.M))
    return sorted(found, key=lambda m: m.end())


def _current_wall(tail):
    """The LATEST quota wall in the tail, or None — the only one that can
    still be in force. Everything above it is scrollback by construction."""
    found = wall_occurrences(tail)
    return found[-1] if found else None


def _wall_patterns():
    """The BLOCKED_ON_QUOTA spellings, taken from the table that decides them."""
    for state, patterns in _LIVENESS_STATES:
        if state == "BLOCKED_ON_QUOTA":
            return patterns
    return ()


_WALL_LINE_RE = None

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_WALL_LINE_RE": "a pattern compiled once from a constant table",
}


def _is_wall_line(line):
    """True when this line is itself a quota wall — a restatement, not a reply."""
    global _WALL_LINE_RE
    if _WALL_LINE_RE is None:
        pats = _wall_patterns()
        # An EMPTY table would make this function answer False for everything
        # and silently restore the defect, so refuse to build a matcher from
        # nothing rather than compile `(?!)`-equivalent emptiness.
        if not pats:
            raise RuntimeError(
                "no BLOCKED_ON_QUOTA patterns in _LIVENESS_STATES; the "
                "repeated-wall rung cannot be derived from an empty table")
        _WALL_LINE_RE = re.compile("|".join("(?:%s)" % p for p in pats),
                                   re.I)
    return bool(_WALL_LINE_RE.search(line))


def _matched_line(tail, match):
    """The WHOLE line a pattern matched inside, never just the match.

    The reset clause sits after the part the wall pattern matches, so an
    expiry read against `match.group(0)` would never find a date and would
    call every wall UNDATED — a guard that cannot see its own input, which is
    the shape that reads as working.
    """
    start = tail.rfind("\n", 0, match.start()) + 1
    end = tail.find("\n", match.end())
    return tail[start:] if end == -1 else tail[start:end]


def _wall_detail(match, verdict, line=None, observed=None):
    """The operator sentence for a quota wall, carrying HOW it was decided.

    A pool refusal's own text carries a duration; the sentence carries the
    INSTANT it resolves to, because that is what a reader acts on."""
    text = match.group(0).strip()[:80]
    if verdict == WALL_UNDATED:
        return (text + " — reset instant UNREADABLE, so whether this wall is "
                       "still in force is UNKNOWN")
    expiry = _pool_expiry(line, observed) if line else None
    if expiry is not None:
        return text + " — reset at %s" % expiry.strftime("%Y-%m-%dT%H:%M:%S")
    return text


# ---------------------------------------------------------------------------
# the vendor-dialog escape: WHICH option a machine is allowed to press
# ---------------------------------------------------------------------------
#
# BLOCKED_ON_VENDOR_PROMPT is the one blocked state a keystroke can clear, which
# is exactly why choosing the keystroke is the dangerous part. The dialog's
# options are not interchangeable: some spend the owner's money, one asks a
# human for something, and one resumes work for free. A classifier that knows a
# seat is stuck says nothing about which of those is safe to press.
#
# SO THE RULE IS A WHITELIST OF ONE. The escape may select the option that
# continues on a different model at no cost, and nothing else. Every other
# option is a refusal with a reason, because the operator needs to know WHY a
# parked seat was left parked — "no escape offered" and "the only way out was a
# purchase" are different facts and only one of them is about the vendor.
ESCAPE_CONTINUE = "continue"          # free, resumes work — the only auto-press
ESCAPE_SPEND = "spend"                # costs money; a machine never buys
ESCAPE_HUMAN = "human"                # asks a person; not ours to send
ESCAPE_ABSENT = "absent"              # no option matched at all

# THE OPTION LIST IS NOT RE-PARSED HERE. `_prompt_options` already owns that
# and owns two properties a fresh scan of the tail would silently lack: it
# strips ANSI before matching, so a coloured dialog is still readable, and it
# takes only the LAST contiguous 1..N run, so numbered prose sitting above the
# dialog in the transcript cannot be mistaken for choices. A keystroke chosen
# off transcript prose would land in a composer.
#
# ITS DISCRIMINATOR IS INHERITED ALONG WITH IT: that helper returns a run only
# when some label reads affirmative or negative, which every dialog in this
# family carries. A vendor dialog that offered neither would be reported as no
# options at all — a refusal, which is the safe direction, and stated here so
# the dependency is visible rather than discovered.
#
# THE NUMBER COMES FROM THE RUN, NEVER FROM A CONSTANT. The producer assembles
# these options conditionally — the credits entry is spliced in or left out by
# account state — so an option does not keep its position between two panes,
# and a hardcoded "1" presses whatever is first, which on a credits dialog is
# the purchase.
#
# AND THE LABELS ARE TEMPLATES, NOT LITERALS: the forward option is built from
# two constants with the model name interpolated between them, which is why
# these match a prefix and a suffix around a wildcard.
_ESCAPE_CONTINUE_LABEL = re.compile(r"\s*switch to .*\band continue\b")
# "YES, RE-ENABLE AND CONTINUE" IS A SPEND, AND IT IS THE DANGEROUS ONE. It is
# the most forward-reading label in the set and is sometimes the dialog's only
# exit, but what it re-enables IS the credit charge — the producer's own
# consent line beside it says that continuing agrees to turn usage credits on.
_ESCAPE_SPEND_LABEL = re.compile(
    r"\s*(?:(?:yes,\s+)?buy usage credits"
    r"|continue with usage credits"
    r"|yes,\s+re-enable and continue"
    r"|adjust monthly limit"
    r"|buy more)\b")
# Both admin phrasings the producer carries. Asking a person for budget is an
# outward-facing act on the owner's behalf and is never ours to send.
_ESCAPE_HUMAN_LABEL = re.compile(
    r"\s*request (?:more|usage credits) from your admin\b")


def vendor_escape_choice(tail, options=None):
    """(digit, kind, seen) for a pane sitting at a vendor dialog.

    `options` lets a caller that has ALREADY parsed the tail hand in the run it
    decided was current, instead of letting this helper scan the same bytes a
    second time with its own rules. That matters where the two disagree:
    `_prompt_options` falls through to an OLDER qualifying run when the newest
    does not qualify, so a caller holding the newest run must pass it or this
    helper will answer about a menu that caller already rejected. Omitted, the
    scan is unchanged, which is what every existing caller still gets.

    `digit` is the string to type and is None for every refusal. `kind` is one
    of the ESCAPE_* constants and `seen` is the producer's own option label,
    quoted back so an operator reading the log sees what the pane actually
    offered rather than our paraphrase of it.

    REFUSING IS THE COMMON CASE AND IT IS NOT A FAILURE. Only one option in
    this family is both free and forward; the caller is expected to leave the
    seat parked and say why.
    """
    if options is None:
        options = _prompt_options(tail)
    # SEVERITY ORDER, not the dialog's layout. The forward option wins wherever
    # it sits, and a spend is reported ahead of an admin request because it is
    # the one an operator most needs to see attached to a parked seat.
    for pattern, kind in ((_ESCAPE_CONTINUE_LABEL, ESCAPE_CONTINUE),
                          (_ESCAPE_SPEND_LABEL, ESCAPE_SPEND),
                          (_ESCAPE_HUMAN_LABEL, ESCAPE_HUMAN)):
        for n, label in options:
            if pattern.match((label or "").lower()):
                return (str(n) if kind == ESCAPE_CONTINUE else None,
                        kind, label.strip()[:80])
    return None, ESCAPE_ABSENT, ""


# A plan file path printed in an approval prompt, e.g.
# ~/.helm/_global/seats/codex/claude/plans/sparkling-bouncing-meerkat.md.
# BLOCKED_ON_HUMAN must carry the WHERE (the plan being approved), not just the
# WHAT — without the path an integrator still has to open the pane by hand to
# learn what they are approving, which is the manual step this feature exists
# to delete.
_PLAN_PATH_RE = re.compile(r"(~/[^\s'\">]*\.md|/[^\s'\">]*plans/[^\s'\">]*\.md)")


def _blocked_detail(state, match, tail):
    """The WHAT (and for BLOCKED_ON_HUMAN, the WHERE) for a blocked state.

    RUNNING and IDLE carry None. BLOCKED_ON_HUMAN carries the plan path when it
    is printed in the prompt — extracted, never fabricated; when no path is
    present it falls back to the matched prompt text. Other blocked states carry
    the matched signal (the quota wall, the context gauge).
    """
    if state in ("RUNNING", "IDLE"):
        return None
    if state == "BLOCKED_ON_HUMAN":
        pm = _PLAN_PATH_RE.search(tail)
        if pm:
            return pm.group(1)
    return match.group(0).strip()[:80]


# THE ANSWERABLE PROMPT — the TYPE and WHICH, after the WHAT and WHERE.
#
# BLOCKED_ON_HUMAN deliberately includes BOTH Claude Code's plan-execution
# dialog and human permission questions: both freeze an unattended seat, but
# only the former may be peer-answered. A plans/ path is supporting evidence,
# not authority — an arbitrary permission question may quote one. The fixed
# sentence below is Claude Code 2.1.226's built-in plan-execution contract
# (binary-probed beside "Ready to code?" and "Here is Claude's plan:").
#
# The choice also has to be READ. `1` happens to be the affirmative on today's
# plan dialog, and that is an accident of rendering rather than a contract: an
# actuator that types a fixed number is typing into whatever the pane happens
# to be showing, which is worse than a seat that waits.
#
# THE PARSE LIVES HERE, beside the classifier, because the fleet keeps exactly
# ONE pane-tail classifier. Consumers take `prompt_type` and `options` off the
# liveness row and reuse these parsers for send-time revalidation; they never
# invent a second interpretation of what is on screen.
PLAN_EXECUTION_PROMPT = "plan-execution"
PERMISSION_PROMPT = "permission"
_PLAN_EXECUTION_RE = re.compile(
    r"Claude has written up a plan and is ready to execute\.\s*"
    r"Would you like to proceed\?", re.I)
_OPTION_RE = re.compile(r"^\s*(\d+)\.\s+(\S.*?)\s*$")
_AFFIRMATIVE_RE = re.compile(r"^yes\b", re.I)
_NEGATIVE_RE = re.compile(r"^no\b", re.I)


def _prompt_type(tail):
    """The current BLOCKED_ON_HUMAN dialog's authority class.

    Fail closed to permission: a generic approval question, even one quoting a
    real plans/ path and offering plan-shaped Yes/No options, is still a human
    decision. Only Claude Code's built-in execution sentence proves the narrow
    peer-answerable dialog.
    """
    text = _PANE_ANSI.sub("", tail or "")
    return PLAN_EXECUTION_PROMPT if _PLAN_EXECUTION_RE.search(text) else \
        PERMISSION_PROMPT


def _prompt_options(tail):
    """[(n, label)] for the CURRENT dialog's numbered choices, else [].

    THE LAST run of lines numbered 1..N, because a plan's own numbered steps
    render ABOVE the dialog and the dialog is what the keystroke lands in. A
    run that skips or repeats a number is not a rendered choice list.

    THE RUN MUST OFFER A `Yes`/`No` LABEL, and that discriminator is the whole
    difference between an actuator and a keystroke generator: ordinary numbered
    prose in a transcript ("1. Read the file  2. Patch it") is contiguous and
    sequential too, and answering it would be typing a digit into a composer.

    AND THE DISCRIMINATOR NEVER REACHES PAST THE NEWEST RUN. Searching upward
    for the newest run that HAPPENS to qualify resurrects an answered dialog:
    measured on shipped code, a tail carrying an answered proceed menu, then
    work, then a newer "Pick a target: 1. src/main.py 2. src/util.py 3. tests/"
    returned the old menu's options. That is the worst possible direction of
    error, because the guard that exists to catch a pane changing under a
    pending keystroke (planprompt._send_choice) re-reads the pane through THIS
    helper: both readings lose the newer list identically, the guard reports
    UNCHANGED, and the digit lands in whatever owns input now — measured end to
    end, a real plan-execution prompt with a newer pick-list below it still
    classified BLOCKED_ON_HUMAN with the same plan path and the digit was sent.
    So the newest run is the ONLY candidate, and a newest run that does not
    qualify is no options at all. REFUSING IS THE SAFE DIRECTION and it is what
    the callers are built for: `affirmative_choice` sees nothing, the send guard
    reports a changed or absent menu, and nothing is typed.
    """
    runs, run = [], []
    for line in _PANE_ANSI.sub("", tail or "").splitlines():
        if not line.strip():
            continue                     # a blank row never breaks a dialog
        m = _OPTION_RE.match(line)
        if not m:
            run = []                     # any other prose ends the run
            continue
        n = int(m.group(1))
        if n == 1:
            run = [(1, m.group(2))]
            runs.append(run)             # a fresh dialog starts here
        elif run and n == run[-1][0] + 1:
            run.append((n, m.group(2)))
        else:
            run = []
    if not runs:
        return []
    newest = runs[-1]
    if len(newest) >= 2 and any(_AFFIRMATIVE_RE.match(label) or
                                _NEGATIVE_RE.match(label)
                                for _, label in newest):
        return [(n, label) for n, label in newest]
    return []


def affirmative_choice(options):
    """The FIRST `Yes…` choice as (keystroke, label), else (None, None).

    FIRST, deliberately. A helm-launched seat already runs with permissions
    bypassed, so the first affirmative ("yes, and auto-accept edits" / "yes,
    and bypass permissions") is the option that MATCHES the posture the seat
    was born with. A later "yes, but ask me each time" re-arms an approval
    prompt on the seat's next tool call — the exact freeze this exists to end,
    reintroduced by the cure.

    Accepts pair sequences in either tuple or list form: a liveness row is
    cached as JSON by proxywatch, and a round trip turns every tuple into a
    list.
    """
    for opt in options or ():
        if not opt or len(opt) < 2:
            continue
        if _AFFIRMATIVE_RE.match(str(opt[1])):
            return str(opt[0]), str(opt[1])
    return None, None


def _liveness_from_orcaadopt(seat_name, census=None, read_pane=True):
    """The liveness row for a seat with NO family record, from orca's pane.

    PROCESS EVIDENCE ALONE CANNOT SEE A BLOCKED PANE, and this path returning
    it was a hole in exactly the population it exists for. An adopted seat has
    no spawn register, so it reaches this function instead of the registered
    reader — and answering LIVE from a running process says only that something
    holds the seat open. A seat parked at a vendor dialog IS a running process
    at a prompt; that is the whole premise of the state. So a row built here
    without reading the pane could never carry a blocked state, and the seats
    the session-join census newly made visible were the ones least able to be
    helped by it.

    THE PANE READ IS ADDITIVE AND NEVER UPGRADES. Process evidence still floors
    the answer: if the tail cannot be read or matches nothing, the row keeps
    the transport's own verdict rather than inventing one. Only a RECOGNISED
    pane state replaces it, which is the same direction the registered reader
    already travels.

    `read_pane=False` is the OBSERVE-ONLY door for a caller that must not touch
    the metaharness at all.

    A RESOLVE THAT RAISED IS NOT A SEAT THAT IS NOT ADOPTED. `None` means
    orca's census holds no such seat; a raise means the census could not be
    asked, and the caller's `no-record` row for the first would be a false
    statement about the second. So a raise answers UNKNOWN with its own
    evidence and names the exception, and still never raises into a display.
    """
    from . import orcaadopt
    try:
        info = orcaadopt.resolve(seat_name, census=census)
    except Exception as e:
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "adopt-read-failed",
                "detail": "orca adoption census could not be read: %s: %s"
                          % (type(e).__name__, e)}
    if info is None:
        return None
    # ONE translation, owned by the transport vocabulary's own module
    # (#141): LIVE stays LIVE — process evidence must not be upgraded to
    # RUNNING's turn-in-flight claim (a wedged process is still a process).
    st = orcaadopt.RICH_STATE.get(info.get("state"), "UNKNOWN")
    row = {"seat": seat_name, "state": st, "blocked_on": None,
           "evidence": "orca-adopted", "detail": info.get("evidence")}
    # THE AUTHORITY FIELDS TRAVEL WITH THE ROW OR NO ACTUATOR CAN USE IT.
    # An actuator needs the pane to type into and the processes to bind the
    # send against; re-deriving them from the seat name would be a second
    # census disagreeing with this one.
    handle = info.get("handle")
    if handle:
        row["handle"] = handle
    if info.get("pids"):
        row["pids"] = list(info["pids"])
    sessions = info.get("sessions") or []
    if sessions:
        row["session"] = sessions[0]
    if not (read_pane and handle):
        return row
    from . import harness
    ad = harness.detect()
    if ad is None:
        return row
    try:
        tail = ad.read(handle)
    except Exception:
        return row                      # unreadable pane keeps process truth
    if not (tail or "").strip():
        return row                      # an empty tail is not a tail
    state, blocked_on = _classify_pane_tail(tail)
    if state is None:
        return row
    row["state"] = state
    row["blocked_on"] = blocked_on
    row["evidence"] = "orca-adopted+pane-tail"
    if state == "BLOCKED_ON_VENDOR_PROMPT":
        digit, kind, seen = vendor_escape_choice(tail)
        row["escape"] = {"digit": digit, "kind": kind, "seen": seen}
    return row


def _seat_liveness_row(seat_name, upstream_sample=(), repair=True):
    """The rich liveness state of one seat: a dict with `state` (one of
    _STATE_NAMES), `blocked_on` (short string or None), and `evidence` (how we
    know — 'pane-tail', 'pane-tail+proxywatch', 'no-pane', 'read-failed',
    'stale-handle', 'adopt-read-failed').

    Lifecycle-walked, and each non-answer is an HONEST UNKNOWN, not a guess:
      - no spawn record          -> UNKNOWN (evidence no-record)
      - headless (no pane)       -> GONE if the recorded pid is dead, else
                                    UNKNOWN — a headless seat has no pane tail
                                    to read, and /proc is forbidden as a probe
      - pane read FAILS          -> UNKNOWN (evidence read-failed): a pane we
                                    cannot read is a pane we know nothing about
      - pane-tail IDLE + fresh dark upstream -> WALLED: local inactivity does
                                    not claim end-to-end availability
      - missing/corrupt/stale upstream -> keep the pane-tail verdict; cache
                                    blindness never fabricates a provider wall
      - stale handle             -> UNKNOWN (evidence stale-handle): the register
                                    names a pane the harness no longer lists
      - mid-restart / spawn lock -> UNKNOWN (evidence spawn-lock)
    """
    family, err = _seat_family(seat_name)
    if err:
        return _liveness_from_orcaadopt(seat_name,
                                        read_pane=True) or {
            "seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
            "evidence": "no-record", "detail": err}
    d = _instance_dir(family, seat_name)
    rec = _spawn_record(d)
    if rec is None:
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "no-record",
                "detail": "no spawn record — `helm seat spawn` registers one"}
    if rec.get("harness") == "headless":
        if not _recorded_pid_alive(rec):
            return {"seat": seat_name, "state": "GONE", "blocked_on": None,
                    "evidence": "pid-dead",
                    "detail": "recorded headless pid is dead"}
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "headless",
                "detail": "headless seat has no pane tail to read; /proc is "
                          "forbidden as a liveness probe"}
    ad, handle, detail = _resolve_registered_pane(seat_name, d=d,
                                                 repair=repair)
    if handle is None:
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "stale-handle",
                "detail": detail or "the register names a pane not listed"}
    # Read through the RESOLVED adapter, never a hardcoded OrcaAdapter — a
    # herdr-hosted seat read through the orca adapter falls to UNKNOWN for no
    # reason, and hardcoding the harness here defeats the property the pattern
    # table was built for (adding a harness is a data change, not a code
    # change). The adapter the register resolved IS the harness to read with.
    try:
        tail = ad.read(handle)
    except Exception as exc:
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "read-failed", "detail": str(exc)[:120]}
    # AN EMPTY TAIL IS NOT A TAIL. `read` converts a failed read into "" — a
    # stale handle, a dead runtime and a genuinely blank pane all arrive here
    # as the same empty string — so feeding it to the matcher reports "a live
    # pane whose tail matches no known state", which is a claim about the
    # CONTENT of something we never read.
    #
    # Live 2026-07-26: orca .46 reminted every terminal handle. All six seats
    # answered `terminal_handle_stale`, helm could read nothing, and this
    # surface said "unrecognized tail" for every one of them. Three different
    # truths — handle stale, read failed, rendering changed — collapsed into
    # one wrong answer, and each wants a DIFFERENT remedy (re-resolve the
    # register / retry orca / add a pattern). Distinguishing them is the whole
    # reason this function reports `evidence`.
    if not (tail or "").strip():
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "empty-read",
                "detail": "the pane read returned nothing — a stale handle or "
                          "an unreachable runtime, NOT an unrecognized state; "
                          "rerun `helm seat where %s` to measure it; helm cannot "
                          "derive a destructive recovery action" % seat_name}
    from . import poolwall
    state, blocked_on = _classify_pane_tail(
        tail, observed=poolwall.pane_anchor(family, seat_name))
    # THE ESCAPE DECISION IS COMPUTED WHERE THE TAIL IS, AND ONLY THE DECISION
    # TRAVELS. The actuator needs to know which option to press, and the option
    # text lives in the pane — but a pane viewport is raw external content, so
    # shipping it to a caller would put arbitrary screen bytes (and whatever a
    # seat happened to print) into every consumer of this dict. What leaves
    # here is a digit, a classification and one truncated option label.
    #
    # It is attached ONLY on the vendor state. No other state has an option to
    # press, and a field present everywhere invites a caller to consult it
    # where it means nothing.
    if state is None:
        return {"seat": seat_name, "state": "UNKNOWN", "blocked_on": None,
                "evidence": "pane-tail",
                "detail": "a live pane whose tail matches no known state"}
    # IDLE is local behavioural truth, not end-to-end availability. A fresh
    # proxywatch verdict can prove that the empty composer cannot accept useful
    # work because its provider family is dark. Compose only at this leaf:
    # RUNNING remains running, pane-derived blockers keep their precise remedy,
    # and an unreadable/missing/stale cache cannot overwrite observed pane truth.
    if state == "IDLE":
        # THE POOL WALL IS READ FROM THE PRODUCER'S LOG, NOT ONLY THE PANE.
        # The pane copy of the refusal can wrap, scroll out, or sit under a
        # bare composer; the seat's proxy.log holds the same refusal with a
        # timestamp, and the delivery pause already holds every keystroke on
        # it. The row says the same thing the pause does, with the instant.
        wall, _why = poolwall.seat_wall(seat_name, family=family)
        if wall is not None:
            row = {"seat": seat_name, "state": "BLOCKED_ON_QUOTA",
                   "blocked_on": poolwall.blocked_on(wall),
                   "evidence": "pane-tail+proxy-log",
                   "detail": "the pane is idle, but its proxy pool refused "
                             "its last request and the reset is ahead"}
            row["handle"] = handle
            if rec.get("session"):
                row["session"] = rec["session"]
            return row
        from . import proxywatch
        upstream, _err = upstream_sample if upstream_sample else _upstream_row(family)
        snapshot = {"upstream": {family: upstream}}
        aggregate, aggregate_err = proxywatch.upstream_record(
            snapshot, family) if upstream else (None, _err)
        measured, measured_err = proxywatch.upstream_seat_record(
            snapshot, family, seat_name) if upstream else (None, _err)
        aggregate_dark = not aggregate_err and aggregate.get("dark") is True
        seat_dark = not measured_err and measured.get("dark") is True
        if aggregate_dark and seat_dark and measured.get("state") != "UNKNOWN":
            wall = "upstream %s since %s" % (
                measured["state"], measured.get("since") or "?")
            return {"seat": seat_name, "state": "WALLED", "blocked_on": wall,
                    "evidence": "pane-tail+proxywatch",
                    "upstream_family": family,
                    "upstream_remediation_state": snapshot,
                    "detail": "the pane is idle, but its measured upstream "
                              "condition is refusing fresh work"}
    row = {"seat": seat_name, "state": state, "blocked_on": blocked_on,
           "evidence": "pane-tail", "detail": None}
    # THE AUTHORITY FIELDS TRAVEL WITH THE ROW. An actuator needs the pane it
    # must type into and the session that pane belongs to; without them a
    # delivery is refused before the key leaves, and a caller that guessed
    # these were present would report a keystroke that never happened.
    row["handle"] = handle
    if rec.get("session"):
        row["session"] = rec["session"]
    if state == "BLOCKED_ON_HUMAN":
        # TYPE + WHICH ride the SAME snapshot so an actuator never re-parses a
        # different shape. Only this state carries them: under any other state a
        # numbered list and plan-shaped sentence are transcript prose.
        row["prompt_type"] = _prompt_type(tail)
        row["options"] = _prompt_options(tail)
    if state == "BLOCKED_ON_VENDOR_PROMPT":
        # THE DECISION TRAVELS, THE VIEWPORT DOES NOT. An actuator needs to know
        # which option to press, and that lives in the pane — but a viewport is
        # raw external content, so what leaves here is a digit, a
        # classification and one truncated option label. Only this state
        # carries it: no other has an option to press, and a field present
        # everywhere invites a caller to consult it where it means nothing.
        digit, kind, seen = vendor_escape_choice(tail)
        row["escape"] = {"digit": digit, "kind": kind, "seen": seen}
    return row


def seat_liveness(seat_name, upstream_sample=(), repair=True):
    """Measured liveness plus remediation from the same upstream sample.

    `repair=False` makes this READ-ONLY. The registered pane resolver REPAIRS a
    stale handle by default, which rewrites the spawn register — so a caller
    that is only observing (a dry run, a survey, anything that promises to
    change nothing) must say so, or its promise is false on exactly the panes
    whose handles have drifted.
    """
    return _with_remediation(_seat_liveness_row(seat_name, upstream_sample,
                                                repair=repair))
