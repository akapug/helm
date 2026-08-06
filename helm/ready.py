#!/usr/bin/env python3
"""helm ready — the fleet-readiness gauge: may forward work resume after a
crash/reboot? (#163, owner-ratified 2026-08-04.)

FIVE SIGNALS, ratified, and every one is a READER of an authority that
already exists — the gauge composes them and re-implements nothing:

  daemon    harness.detect() + the adapter's own pane probe (orca: one
            AF_UNIX round-trip, 5s-bounded, no subprocess)
  seats     seat.registered_seats() (the spawn register) resolved per seat
            through seat.seat_liveness() — the declared pane authority
  beacons   beacons.census() — the attendance question: a beacon is a
            seat's ONLY wake path
  families  proxywatch.upstream_snapshot() — the LATEST recorded sweep,
            NEVER a fresh canary (a sweep spends 8 upstream tokens per
            family and half a minute; the gauge must be fast). A stale or
            missing record answers UNKNOWN with the recorder's own reason.
  checkout  the shared checkout — the MAIN root of the tree this helm came
            from — clean and at origin/main. Two git reads, no fetch, so
            "origin/main" means origin/main as last fetched and says so.

ADVISORY-FIRST, ratified: the gauge RENDERS red/green per signal and
overall; it never refuses and never gates a dispatch in this iteration —
enforcement is a later decision, after one real reboot has exercised it.
It is the INSTRUMENT for 0.3's gate 4 (the fleet reboot-and-resume test
from a 0000 state).

TRI-STATE LAW, everywhere: a signal whose instrument cannot answer yields
UNKNOWN carrying the reason — never a confident green from an unchecked
signal — and UNKNOWN never qualifies as READY. The one deliberate
asymmetry: a family proxywatch records DARK WITH A NAMED CAUSE (QUOTA-402,
RATE-LIMITED, AUTH-401, ... — proxywatch's _UPSTREAM_DARK set) reads
ready-WITH-NOTE, not red. A wall is a fact about the provider, not
unreadiness of the fleet; work routes around it. Only an UNMEASURED family
blocks READY.

Exit codes, distinct on purpose (beacons' precedent — a fleet measured
broken and a gauge that could not measure must never be the same signal):
0 READY · 1 NOT READY (at least one red) · 2 cannot prove (no red, at
least one UNKNOWN; also any usage refusal, before the gauge runs).
"""
import json
import os
import time

GREEN, RED, UNKNOWN = "GREEN", "RED", "UNKNOWN"
READY, NOT_READY = "READY", "NOT READY"

_USAGE = "ready [--json] — the five-signal fleet-readiness gauge (advisory: renders, never gates)"


def _row(signal, state, evidence, repair=None, note=None):
    """One signal's verdict. `repair` is the operator's next verb and is
    rendered only when the state is not green — honest-refusals style: the
    line says what was measured, then what to run about it."""
    return {"signal": signal, "state": state, "evidence": evidence,
            "repair": repair, "note": note}


def signal_daemon(detect=None):
    """Signal 1 — the metaharness daemon answers. The adapter is the
    authority: orca's `panes()` is one 5s-bounded socket round-trip and
    fail-open by contract (([], reason) — never an exception); herdr has no
    RPC surface, so its CLI `list()` is the probe there. No metaharness
    installed at all is UNKNOWN, not red: pane reachability is then
    unmeasurable, and helm is metaharness-agnostic by design."""
    from . import harness
    detect = detect or harness.detect
    try:
        ad = detect()
    except Exception as e:
        return _row("daemon", UNKNOWN, "metaharness detection failed (%s: %s)"
                    % (e.__class__.__name__, e))
    if ad is None:
        return _row("daemon", UNKNOWN,
                    "no metaharness detected — pane reachability unmeasurable",
                    repair="optional companion: orca (recommended), herdr also "
                           "supported; HELM_METAHARNESS overrides detection")
    probe = getattr(ad, "panes", None)
    if probe is None:
        try:
            rows, err = ad.list(), None
        except Exception as e:
            rows, err = [], "%s" % e
    else:
        rows, err = probe()
    if err:
        return _row("daemon", RED, "%s daemon not answering — %s"
                    % (ad.name, err),
                    repair="start %s, then re-run `helm ready`" % ad.name)
    n = len(rows)
    return _row("daemon", GREEN, "%s daemon answering — %d pane%s listed"
                % (ad.name, n, "s"[:n != 1]))


# The two seat_liveness states that mean the pane does NOT resolve to a live
# agent: the pane (or its recorded pid) is gone, or the pane shell survives
# with the agent process exited under it. Both repair with the same verb.
_PANE_DOWN = ("GONE", "EXITED_PANE_ALIVE")


def signal_seats(registered=None, liveness=None):
    """Signal 2 — every REGISTERED seat's pane resolves live. The spawn
    register (seat.registered_seats) enumerates who owes a pane; the
    declared authority seat.seat_liveness answers per seat. `blind` refuses
    the whole signal — a partly-readable register would render a short list
    as complete, exactly the overclaim this gauge exists to prevent
    (seat._rebind and doctor._is_genesis hold the same line). A liveness
    UNKNOWN stays UNKNOWN keyed by its evidence word; it is never folded
    into pass or fail."""
    from . import seat as smod
    registered = registered or smod.registered_seats
    liveness = liveness or smod.seat_liveness
    try:
        names, blind = registered()
    except Exception as e:
        return _row("seats", UNKNOWN, "spawn register unreadable (%s: %s)"
                    % (e.__class__.__name__, e))
    if blind:
        return _row("seats", UNKNOWN, "spawn register PARTLY unreadable — a "
                    "short list would read as complete, so no list is offered")
    names = sorted(set(names))
    if not names:
        return _row("seats", GREEN, "no registered seats — nothing owes a pane",
                    note="an empty register is a fact about the register, "
                         "not proof the fleet is staffed")
    down, dark, live = [], [], []
    for name in names:
        try:
            lv = liveness(name)
        except Exception as e:
            dark.append("%s: liveness probe crashed (%s)" % (name, e))
            continue
        state = lv.get("state")
        if state in _PANE_DOWN:
            down.append("%s %s (%s)" % (name, state, lv.get("evidence")))
        elif state == UNKNOWN:
            dark.append("%s: %s" % (name, lv.get("evidence")))
        else:
            live.append(name)
    if down:
        return _row("seats", RED, "%d of %d registered seat pane%s down — %s"
                    % (len(down), len(names), "s"[:len(names) != 1],
                       "; ".join(down)),
                    repair="`helm seat resume <seat>` re-seats each via the "
                           "detected metaharness",
                    note=("unprovable besides: " + "; ".join(dark))
                    if dark else None)
    if dark:
        return _row("seats", UNKNOWN, "%d of %d pane%s unprovable — %s"
                    % (len(dark), len(names), "s"[:len(names) != 1],
                       "; ".join(dark)),
                    repair="`helm seat where <seat>` has the per-seat detail")
    return _row("seats", GREEN, "%d registered seat%s, every pane resolves live"
                % (len(names), "s"[:len(names) != 1]))


def signal_beacons(census=None):
    """Signal 3 — every seat's inbox beacon armed: the attendance question,
    beacons.census() verbatim. The alarm classes are the census's own —
    UNREACHABLE (deaf, or unproven with no live beacon), VACANT (a live wake
    path with nobody home) and GHOST waiters (a dead session eating a seat's
    rows) — because `helm beacons --post` already exits 1 on exactly this
    set. A failed session or process-table probe makes every verdict below
    it a non-claim, so the signal reads UNKNOWN, never red."""
    from . import beacons
    census = census or beacons.census
    try:
        rep = census()
    except Exception as e:
        return _row("beacons", UNKNOWN, "beacon census failed (%s: %s)"
                    % (e.__class__.__name__, e))
    if not rep.get("live_probe") or not rep.get("agent_probe"):
        gap = "session liveness" if not rep.get("live_probe") \
            else "the process table"
        return _row("beacons", UNKNOWN, "%s could not be probed — no verdict "
                    "below it would be a death claim" % gap,
                    repair="`helm beacons` has the census detail")
    unreachable = rep.get("unreachable") or []
    vacant = rep.get("vacant") or []
    ghosts = rep.get("ghosts") or []
    if unreachable or vacant or ghosts:
        parts = []
        if unreachable:
            parts.append("unreachable: " + ", ".join(
                beacons.label(r.get("seat")) for r in unreachable))
        if vacant:
            parts.append("vacant: " + ", ".join(
                beacons.label(r.get("seat")) for r in vacant))
        if ghosts:
            parts.append("%d ghost waiter%s"
                         % (len(ghosts), "s"[:len(ghosts) != 1]))
        return _row("beacons", RED, "; ".join(parts),
                    repair="`helm beacons` names each fault; a seat re-arms "
                           "its own beacon on its next turn (`helm seat "
                           "resume <seat>` for a dead one)")
    n = len(rep.get("seats") or [])
    unproven = len(rep.get("unproven") or [])
    return _row("beacons", GREEN, "%d seat%s on the roll — nothing "
                "unreachable, vacant, or ghosted" % (n, "s"[:n != 1]),
                note=("%d beacon-live seat%s read UNPROVEN (non-claude "
                      "families prove no session even when healthy)"
                      % (unproven, "s"[:unproven != 1])) if unproven else None)


def signal_families(snapshot=None, minted=None):
    """Signal 4 — every represented family HEALTHY or EXPLICITLY WALLED,
    read from proxywatch's LATEST RECORDED sweep (upstream_snapshot), never
    by sweeping: a fresh pass spends 8 upstream tokens per family and this
    gauge must stay cheap enough to run reflexively. The recorder's four
    refusals (unreadable / no verdict yet / no timestamp / stale beyond the
    40m bar) each surface here as UNKNOWN carrying the recorder's own
    reason — the reason string deliberately holds no verdict word; this
    caller owns the word UNKNOWN (tests/test_proxywatch.py pins that
    split). Coverage is checked against the minted-family census, because a
    hardcoded family list is how grok starved for two days.

    A family dark WITH A NAMED CAUSE (proxywatch._UPSTREAM_DARK) is a WALL:
    ready-with-note, never red — ratified. Only UNKNOWN blocks READY."""
    from . import proxywatch
    from . import seat as smod
    snapshot = snapshot or proxywatch.upstream_snapshot
    try:
        snap, err = snapshot()
    except Exception as e:
        snap, err = None, "proxywatch snapshot failed (%s: %s)" \
            % (e.__class__.__name__, e)
    if err:
        return _row("families", UNKNOWN, err,
                    repair="one `helm proxywatch` pass records a fresh "
                           "verdict; `--install-timer` keeps it fresh")
    try:
        fams = sorted({f for f, _ in (minted or smod._minted_seats)()})
    except Exception as e:
        return _row("families", UNKNOWN, "minted-family census failed (%s: "
                    "%s) — snapshot coverage unverifiable"
                    % (e.__class__.__name__, e))
    healthy, walled, dim = [], [], []
    for fam in sorted(set(fams) | set(snap)):
        rec = snap.get(fam)
        if rec is None:
            dim.append("%s: minted but no recorded verdict" % fam)
            continue
        state = rec.get("state")
        if state == "HEALTHY":
            healthy.append(fam)
        elif state in proxywatch._UPSTREAM_DARK:
            walled.append("%s %s since %s"
                          % (fam, state, rec.get("since") or "?"))
        else:
            dim.append("%s: %s" % (fam, state or "?"))
    note = ("WALLED: " + "; ".join(walled) +
            " — a wall is a fact, not unreadiness") if walled else None
    if dim:
        return _row("families", UNKNOWN, "; ".join(dim),
                    repair="one `helm proxywatch` pass re-measures the "
                           "unmeasured families", note=note)
    return _row("families", GREEN, ("%d famil%s HEALTHY%s"
                % (len(healthy), "y" if len(healthy) == 1 else "ies",
                   (": " + ", ".join(healthy)) if healthy else ""))
                if healthy else "every measured family is walled, cause named",
                note=note)


def signal_checkout(root=None, text=None):
    """Signal 5 — the shared checkout clean and at origin/main. The shared
    checkout is the MAIN root of the tree THIS helm came from (worktrees
    fold via work._lanes.find_root — automap's resolver), because that is
    the tree every rebooted seat boots onto. Both reads go through the VCS
    seam (vcs.backend), not raw subprocess — the direct-spawn audit pins
    that debt, and a jj estate later gets this signal for free. No fetch is
    issued — the gauge must not touch the network — so origin/main means
    origin/main AS LAST FETCHED, and the green line says so."""
    if root is None:
        try:
            from .work import _lanes
            root = _lanes.find_root(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            root = None
    if not root:
        return _row("checkout", UNKNOWN, "this helm did not come from a git "
                    "checkout — shared-checkout state unmeasurable")
    if text is None:
        from . import vcs
        text = vcs.backend(root).text
    try:
        st = text(root, "status", "--porcelain")
        rp = text(root, "rev-parse", "HEAD", "origin/main")
    except Exception as e:
        return _row("checkout", UNKNOWN, "git could not answer for %s (%s)"
                    % (root, e))
    if st[0] or rp[0]:
        why = (rp[2] or st[2] or "").splitlines()
        return _row("checkout", UNKNOWN, "git could not answer for %s (%s)"
                    % (root, why[0][:120] if why else "no detail"))
    dirty = [ln for ln in st[1].splitlines() if ln.strip()]
    shas = rp[1].split()
    if len(shas) != 2:
        return _row("checkout", UNKNOWN, "git rev-parse answered %d ref%s "
                    "for %s, expected 2" % (len(shas), "s"[:len(shas) != 1],
                                            root))
    head, main = shas
    if dirty:
        return _row("checkout", RED, "shared checkout %s DIRTY (%d path%s) "
                    "— a reboot boots every seat onto uncommitted state"
                    % (root, len(dirty), "s"[:len(dirty) != 1]),
                    repair="rescue the edits into a lane room (`helm work "
                           "claim <lane>`, or `helm work gc` for lease-less "
                           "leftovers); never land them loose on trunk")
    if head != main:
        return _row("checkout", RED, "shared checkout %s at %s but "
                    "origin/main is %s" % (root, head[:12], main[:12]),
                    repair="reconcile: push the landed work, or `git -C %s "
                           "pull --ff-only`" % root)
    return _row("checkout", GREEN, "shared checkout %s clean at origin/main "
                "(%s, as last fetched)" % (root, head[:12]))


# Late-resolved like doctor.CHECKS, so every signal is a test seam: a test
# that patches ready.signal_beacons patches what gauge() runs.
SIGNALS = ("signal_daemon", "signal_seats", "signal_beacons",
           "signal_families", "signal_checkout")


def verdict(rows):
    """One red = NOT READY; else one UNKNOWN = UNKNOWN (a gauge that could
    not measure must never read READY); else READY. Walled families ride a
    green row's note, so they keep READY — ratified."""
    states = [r["state"] for r in rows]
    if RED in states:
        return NOT_READY
    if UNKNOWN in states:
        return UNKNOWN
    return READY


def gauge():
    """All five signals + the composed verdict. A signal that CRASHES
    becomes an UNKNOWN row naming the crash — never a missing row (which
    would read as green-by-absence) and never the gauge's own death."""
    rows = []
    for name in SIGNALS:
        try:
            rows.append(globals()[name]())
        except Exception as e:
            rows.append(_row(name.replace("signal_", ""), UNKNOWN,
                             "gauge leg crashed (%s: %s)"
                             % (e.__class__.__name__, e)))
    return {"ready": verdict(rows), "signals": rows, "ts": int(time.time())}


def _print_gauge(rep):
    print("helm ready — may forward work resume? ADVISORY: this gauge "
          "renders; it never gates a dispatch")
    for r in rep["signals"]:
        print("  %-7s %-9s %s" % (r["state"], r["signal"], r["evidence"]))
        if r.get("note"):
            print("  %-7s %-9s %s" % ("", "", r["note"]))
        if r.get("repair") and r["state"] != GREEN:
            print("  %-7s %-9s -> %s" % ("", "", r["repair"]))
    tally = {s: sum(1 for r in rep["signals"] if r["state"] == s)
             for s in (GREEN, RED, UNKNOWN)}
    print("helm ready: %s — %d green, %d red, %d unknown"
          % (rep["ready"], tally[GREEN], tally[RED], tally[UNKNOWN]))


def cmd_ready(args):
    """ready [--json] — render the five-signal gauge. Exit 0 READY, 1 NOT
    READY, 2 cannot-prove (UNKNOWN) — see the module docstring."""
    import sys
    from .cli import guard_tail
    args = list(args or [])
    rc = guard_tail("helm ready", args, flags=("--json",), usage=_USAGE)
    if rc is not None:
        return rc
    rep = gauge()
    if "--json" in args:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print_gauge(rep)
    sys.stdout.flush()
    return {READY: 0, NOT_READY: 1}.get(rep["ready"], 2)
