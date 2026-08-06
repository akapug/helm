"""Seat adoption, rebind, and liveness for :mod:`helm.seat`."""
import contextlib as _contextlib
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

from . import seat_lifecycle_sessions as _sessions_impl
from .seat_lifecycle_sessions import (
    _seat_family,
    family_for,
    _unknown_seat_reason,
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
    _prove_spawned_pane,
    _pane_sendable,
    _SEND_ONLY_DETAIL,
    _live_session_orca_identity,
    _prove_orca_replacement,
    _live_seat_orca_identity,
    _prove_orca_rebind,
    rebind_seat,
    _repair_orca_handle,
    _resolve_registered_pane,
    _reap_stale,
    _headless_spawn,
    _register_spawn,
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


@_contextlib.contextmanager
def _seat_lifecycle_lock(d):
    """Serialize every read/prove/act/write transition for one seat."""
    import fcntl
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, ".spawn.lock"), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _spawn_record(d):
    from . import pk
    rec = pk.read_json(_spawn_path(d), None)
    return rec if isinstance(rec, dict) else None


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

_REBIND_SERVICE = """[Unit]
Description=helm seat rebind (re-point stale orca registers, one idempotent pass)

[Service]
Type=oneshot
# see proxywatch: no WorkingDirectory => cwd is $HOME => no project => #main
WorkingDirectory=%(cwd)s
ExecStart=%(helm)s seat rebind --all --apply
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
        return False, "systemctl unavailable; run `helm seat rebind --all --apply` from another scheduler"
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
    if not names:
        print("usage: helm seat rebind <seat>|--all [--apply]", file=sys.stderr)
        return 2

    from . import harness
    ad = harness.detect()
    if ad is None or getattr(ad, "name", None) != "orca":
        print("helm seat rebind: no orca metaharness detected — rebind is the "
              "orca pane-identity repair and has nothing to do here",
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
    base = os.path.join(home.global_dir(), "seats")
    try:
        families = sorted(os.listdir(base))
    except FileNotFoundError:
        # ABSENT IS AN ANSWER, UNREADABLE IS NOT. No seats directory means a
        # fleet that has never minted a seat — a DEFINITE zero, and the state
        # every clean home starts in. Reporting it as blindness would tell
        # `doctor` a brand-new machine cannot be trusted to be new, and it
        # would render every unrealized projection as an orphan.
        return [], False
    except OSError:
        return [], True                  # present but unreadable: SAY SO
    names, blind = [], False
    for name in families:
        fam = os.path.join(base, name)
        if os.path.exists(os.path.join(fam, "spawn.json")):
            names.append(name)
        inst = os.path.join(fam, "instances")
        try:
            subs = sorted(os.listdir(inst))
        except FileNotFoundError:
            continue                     # no instances dir: not blindness
        except OSError:
            blind = True                 # present but unreadable: SAY SO
            continue
        for sub in subs:
            if os.path.exists(os.path.join(inst, sub, "spawn.json")):
                names.append(sub)
    return names, blind


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


def upstream_phrase(family, compact=False):
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
    row, err = _upstream_row(family)
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
        return "" if compact else "; upstream UNKNOWN (%s)" % err
    if not row.get("dark"):
        return ""                       # healthy: the other fields suffice
    if compact:
        return " ⚠ upstream %s" % row["state"]
    return "; upstream %s since %s" % (row["state"], row.get("since") or "?")


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
    if rec is None:
        print("helm seat: no spawn record for %s (%s missing) — `helm seat "
              "spawn %s` registers one" % (seat_name, _spawn_path(d),
                                           seat_name), file=sys.stderr)
        return 1
    alive = None
    if rec.get("seat") != seat_name:
        alive = False
    elif rec.get("harness") == "headless":
        alive = _recorded_pid_alive(rec)
    else:
        ad, handle, detail = _resolve_registered_pane(seat_name, d=d)
        if handle is not None:
            alive = True
            if handle != rec.get("handle"):
                rec = _spawn_record(d) or rec  # resolver repaired the register
        elif ad is not None and "is not live" in detail:
            alive = False
    liveness = seat_liveness(seat_name)
    if "--json" in rest:
        print(json.dumps(dict(rec, alive=alive, liveness=liveness),
                         indent=2, sort_keys=True))
        return 0
    ref = ("pid %s" % rec.get("pid")) if rec.get("harness") == "headless" \
        else ("handle %s" % rec.get("handle"))
    state = {True: "LIVE", False: "GONE (helm seat spawn %s respawns)"
             % seat_name}.get(alive, "unverified (metaharness %r not "
                              "detected here)" % rec.get("harness"))
    rich = liveness["state"]
    if liveness.get("blocked_on"):
        rich += " (%s)" % liveness["blocked_on"]
    print("%s: %s %s — %s; liveness %s%s; worktree %s, room %s, spawned %s"
          % (seat_name, rec.get("harness"), ref, state, rich,
             upstream_phrase(family),
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
    ("RUNNING", (r"\besc to interrupt\b",)),
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
    ("BLOCKED_ON_QUOTA", (r"usage balance exhausted",
                          r"\b402\b.*\b(balance|usage|quota|billing)\b",
                          r"\b503\b.*\bauth_unavailable\b",
                          r"auth_unavailable: no auth available")),
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
_PANE_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PANE_PROMPT = re.compile(r"^\s*❯")
# Status chrome Claude renders BELOW the live composer: the input-box rules,
# the ⏵⏵ permissions/status line, and — whenever the task list is visible
# (ctrl+t) — the agents strip (`● main`, `◯ claude  <desc>   12m 48s`).
# Measured live 2026-08-04 on codex-3 at exactly 100% context: the strip rows
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
#       `  opus-5 | ~/dev/akapug/helm`, `  fable-5 | ~/dev/akapug/notes`,
#       `  opus-5[1m] | ~/dev/akapug/helm                            /rc`
#     Pinned to a model-shaped token, a pipe, and a path that must start `~/`
#     or `/` — prose with a stray pipe cannot reach it.
#   * the agents-strip row carrying a fan-out count, `◯ claude (+1)  <desc>`,
#     which the existing alternatives miss on the `(+N)` alone.
_PANE_CHROME = re.compile(
    r"^\s*(?:⏵⏵(?:\s|$)|[─━]{3,}\s*$"
    r"|[●◯◉]\s+\S+(?:\s+\(\+\d+\))?\s*$"           # `● main`, `◯ claude (+1)`
    r"|[●◯◉]\s+\S+(?:\s+\(\+\d+\))?\s{2,}\S.*$"    # marker+name+gap+desc
    r"|[\w.\-]+(?:\[[^\]\s]*\])?\s+\|\s+[~/]\S*\s*(?:\S+\s*)?$)")  # model | cwd


def _current_prompt_line(tail):
    """The current composer line, never a prompt preserved in scrollback.

    Claude renders status chrome below the composer, so the prompt need not be
    the final non-empty line. Anything else after it is newer semantic content
    and makes the pane UNKNOWN rather than turning historical chrome into IDLE.
    """
    lines = [_PANE_ANSI.sub("", line) for line in (tail or "").splitlines()]
    visible = [line for line in lines if line.strip()]
    for i in range(len(visible) - 1, -1, -1):
        if not _PANE_PROMPT.match(visible[i]):
            continue
        return visible[i] if all(
            _PANE_CHROME.match(line) for line in visible[i + 1:]) else None
    return None


def _classify_pane_tail(tail):
    """(state, blocked_on) from a pane tail, most-specific-first.

    blocked_on is a short human string for BLOCKED_* and CONTEXT_FULL states —
    WHAT the seat is waiting on — or None. The remedy for a blocked seat is
    owner- or integrator-actionable only if it names the blocker, so a bare
    state without it is useless. The patterns are DATA (see _LIVENESS_STATES);
    a state with no matching pattern falls through to the next.
    """
    low = _PANE_ANSI.sub("", tail or "")
    for state, patterns in _LIVENESS_STATES:
        current = _current_prompt_line(low) if state == "IDLE" else low
        if current is None:
            continue
        for pat in patterns:
            m = re.search(pat, current, re.I | re.M)
            if m:
                return state, _blocked_detail(state, m, low)
    return None, None


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


def _liveness_from_orcaadopt(seat_name):
    from . import orcaadopt
    try:
        info = orcaadopt.resolve(seat_name)
    except Exception:
        return None
    if info is not None:
        # ONE translation, owned by the transport vocabulary's own module
        # (#141): LIVE stays LIVE — process evidence must not be upgraded to
        # RUNNING's turn-in-flight claim (a wedged process is still a process).
        st = orcaadopt.RICH_STATE.get(info.get("state"), "UNKNOWN")
        return {"seat": seat_name, "state": st, "blocked_on": None,
                "evidence": "orca-adopted", "detail": info.get("evidence")}
    return None


def seat_liveness(seat_name):
    """The rich liveness state of one seat: a dict with `state` (one of
    _STATE_NAMES), `blocked_on` (short string or None), and `evidence` (how we
    know — 'pane-tail', 'pane-tail+proxywatch', 'no-pane', 'read-failed',
    'stale-handle').

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
        return _liveness_from_orcaadopt(seat_name) or {
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
    ad, handle, detail = _resolve_registered_pane(seat_name, d=d)
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
                          "re-resolve with `helm seat where` / respawn"}
    state, blocked_on = _classify_pane_tail(tail)
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
        from . import proxywatch
        upstream, _err = _upstream_row(family)
        if upstream and upstream.get("dark") \
                and upstream.get("state") in proxywatch._UPSTREAM_DARK:
            wall = "upstream %s since %s" % (
                upstream["state"], upstream.get("since") or "?")
            return {"seat": seat_name, "state": "WALLED", "blocked_on": wall,
                    "evidence": "pane-tail+proxywatch",
                    "detail": "the pane is idle, but its provider is refusing "
                              "fresh work"}
    return {"seat": seat_name, "state": state, "blocked_on": blocked_on,
            "evidence": "pane-tail", "detail": None}
