"""THE SLICED SHADOW OF EVERY TRAIN GATE: measured beside the land authority,
never in its place.

WHY IT EXISTS (W4). On the fleet's build boxes the serial whole suite takes
several times the wall of the same tree run as slices (helm/gateslice.py).
Every train's SERIAL window gate gets ONE sliced run of the same tree, and the
two receipts are compared test by test. Each finished shadow is one verdict in
the canary record (helm/gatecanary.py), which the land door reads before a
sliced receipt may authorize a land (`gatecanary.standing`). This module's own
clock (`streak`) counts consecutive AGREE trains in the LAND LEDGER: a train
whose shadow was never recorded, or cannot be read, stops it, and so does any
disagreement.

WHAT IT MAY NOT DO. The serial window gate stays the only land authority, and
the shadow is built so that it cannot become one by accident:

  * ITS RECEIPTS NEVER ENTER THE LAND LEDGER. The sliced job's receipt is
    imported into a helm home of its own (`shadow_home`), so the one-suite-
    per-tree door, `gate window show`'s stranded join, every land door and
    every bind read a ledger the shadow never writes. A sliced receipt in the
    land ledger would be the newest whole-suite receipt of the train's tree,
    and "the last one decides" there.
  * IT NEVER SHARES A NODE WITH A LAND GATE. It starts only after its train's
    serial job is over and that receipt is home, and only while no land gate
    is in flight. It declares `--shadow` to Fab; a window gate declares
    `--land-authority`, and Fab's admission owner retires shadows before it
    chooses authority placement. A cancelled shadow is UNKNOWN, which stops
    the clock like a disagreement does and is never counted as agreement.
  * IT IS SCHEDULED, NOT SPAWNED, BY THE LAUNCH. The window door writes one
    record and returns; `helm gate shadow run` (a timer, one pass at a time)
    does the work. Every call the door makes into this module is fenced, so a
    shadow fault is a missing record, never a failed or slower launch.

THE COMPARISON IS THE CANARY'S (helm/gatecanary.py `compare`): every failure
identity with its kind, plus the run and skip counts. PER TEST means as fine as
the serial receipt records outcomes: a failure or an error by test id, a pass
or a skip by count, because a serial receipt names no passing or skipped test
(`PER_TEST`). A crash, a timeout, a cancel, an unreadable receipt or slice
evidence that does not re-derive from the tree is UNKNOWN, never AGREE.

A DISAGREE WRITES THE CANARY'S MARKER (`gatecanary.write_marker`, the file
`gate.sliced_land_disabled()` reads), so whatever later lets a sliced receipt
authorize a land already fails closed on the first train whose slices and
serial disagreed. Nothing writes the marker on AGREE or UNKNOWN, and nothing
here clears it: `helm gate canary clear --reason` is the one door out.
"""
import fcntl
import glob
import json
import os
import subprocess
import sys
import time

from . import eventledger, fabgate, gate, gatewindow, home, pk

MODE = "sliced-shadow"
STORE_SUBDIR = os.path.join(".state", "gate-shadow")
RECORD_VERSION = 1
SCHEDULED, RUNNING, CANCELING, MARKING, DONE = (
    "SCHEDULED", "RUNNING", "CANCELING", "MARKING", "DONE")
AGREE, DISAGREE, UNKNOWN = "AGREE", "DISAGREE", "UNKNOWN"
# The integrator's switch criterion: this many consecutive agreeing trains,
# one of them RED on a failure both runs caught.
SWITCH_STREAK = 10
POLL_S = 15
# A sliced job not over this long after its submit is cancelled and UNKNOWN.
RUN_TIMEOUT_S = 3 * 3600
# A shadow whose serial result has not come home this long after the launch
# stops waiting for it.
PENDING_LIMIT_S = 12 * 3600
KILL_TIMEOUT_S = 120
LABEL_PREFIX = "shadow "
TIMER_NAME = "helm-gate-shadow.timer"
SERVICE_NAME = "helm-gate-shadow.service"
TIMER_INTERVAL_S = 300
_SHOWN = 200
_YIELD = "yield"
PER_TEST = {
    "fail": "by test id", "error": "by test id",
    "pass": "by count: the serial receipt names no passing test",
    "skip": "by count: the serial receipt names no skipped test",
}
PER_SLICE_EXIT_NOTE = (
    "not recorded: a sliced receipt (v10) carries the run's exit and each "
    "module's seconds, not each worker's exit; the runner refuses any result "
    "in which a worker's exit disagrees with its own counts")


def store_dir(global_dir=None):
    return os.path.join(global_dir or home.global_dir(), STORE_SUBDIR)


def records_dir(global_dir=None):
    return os.path.join(store_dir(global_dir), "records")


def shadow_home(global_dir=None):
    """The helm home a sliced job's receipt is imported into. No land door,
    bind or window read ever opens `<shadow_home>/_global`."""
    return os.path.join(store_dir(global_dir), "home")


def shadow_adopted_dir(global_dir=None):
    """The adopted-memory root shadow subprocesses may read or write."""
    return os.path.join(shadow_home(global_dir), "adopted")


def shadow_ledger(global_dir=None):
    return os.path.join(shadow_home(global_dir), home.GLOBAL, gate.RECEIPTS)


def land_ledger(global_dir=None):
    return os.path.join(global_dir or home.global_dir(), gate.RECEIPTS)


def record_path(generation, global_dir=None):
    return os.path.join(records_dir(global_dir), generation + ".json")


def _well_formed(row):
    return isinstance(row, dict) and row.get("v") == RECORD_VERSION \
        and isinstance(row.get("serial_job"), dict) \
        and isinstance(row["serial_job"].get("generation"), str)


def records(global_dir=None):
    """Every well-formed shadow record, in the order the window launched their
    trains. A malformed one is left out HERE and named by `streak`, which
    finds its file by the generation the land ledger's train carries."""
    out = []
    for path in glob.glob(os.path.join(records_dir(global_dir), "*.json")):
        row = pk.read_json(path, default=None)
        if _well_formed(row):
            out.append(row)
    return sorted(out, key=lambda r: (r.get("scheduled_at") or 0,
                                      r["serial_job"]["generation"]))


def _save(rec, global_dir=None):
    pk.write_json(record_path(rec["serial_job"]["generation"], global_dir),
                  rec)


def sliceable(room):
    """Why `room`'s tree cannot run helm's suite as slices, or None."""
    missing = [path for path in gate.SLICE_RUNNER_FILES
               if not os.path.isfile(os.path.join(room, *path.split("/")))]
    if missing:
        return ("the tree does not ship the slice runner (%s missing)"
                % ", ".join(missing))
    return gate._slice_kind_missing(room)


# --------------------------------------------------------------- the door

def schedule(row, global_dir=None):
    """Record ONE shadow for the serial run `row` the window just launched.
    -> the record, or None when the tree cannot run slices.

    A second launch that JOINED the same generation finds its record already
    standing and changes nothing, so one serial run has exactly one shadow."""
    if sliceable(row["room"]):
        return None
    job = {k: row[k] for k in ("key", "job_id", "host", "generation",
                                "tree")}
    path = record_path(job["generation"], global_dir)
    standing = pk.read_json(path, default=None)
    if isinstance(standing, dict) and standing.get("v") == RECORD_VERSION:
        return standing
    rec = {"v": RECORD_VERSION, "mode": MODE, "state": SCHEDULED,
           "scheduled_at": row["ts"], "label": row.get("label") or "",
           "project": row["project"], "room": row["room"],
           "head": row["head"], "tree": row["tree"], "trunk": row["trunk"],
           "serial_job": job, "shadow_job": None, "verdict": None,
           "reason": "scheduled: runs once this serial job is over, its "
                     "receipt is home and no land gate is in flight"}
    _save(rec, global_dir)
    return rec


# ------------------------------------------------------------- comparison

def _ledger(path):
    """(receipts, rows, unavailable) of one ledger file, filtered exactly as
    `gate.receipts()` filters the land ledger: a row whose id does not
    recompute from its own content is not a receipt."""
    rows, unavailable = eventledger.checked_events(path, strict=True)
    if unavailable:
        return [], [], unavailable
    chunks, _skipped = gate._failure_chunks(rows)
    siblings = (gate._FAILURE_CHUNK_EVENT, gate._TIMING_EVENT)
    return [r for r in rows if isinstance(r, dict)
            and r.get("event") not in siblings
            and gate._id_matches(r, chunks)], rows, None


def _summary(row):
    return {"id": row.get("id"), "status": row.get("status"),
            "ran": row.get("ran"), "skipped": row.get("skipped") or 0,
            "failures": row.get("failure_total", len(row.get("failures")
                                                     or ())),
            "host": (row.get("host") or {}).get("node"),
            "label": row.get("label"), "ts": row.get("ts")}


def _outcomes(failures):
    """{test id: outcome} for every test the receipt records by id."""
    if failures is None:
        return None
    out = {}
    for kind, test in sorted(failures, key=lambda k: (k[1], k[0])):
        out[test] = ",".join(filter(None, (out.get(test), kind.lower())))
    return dict(list(out.items())[:_SHOWN])


def slice_evidence(row, repo=None):
    """(evidence, refusal) — what a sliced receipt proves about its slices,
    re-derived rather than believed, and why it cannot stand beside serial.

    EVERY MODULE EXACTLY ONCE is re-derived from the receipt's own tree
    (`gate.slice_tree_refusal`): discovery's module walk over the tree must be
    the modules the run timed, one entry each. The runner itself refuses a
    result in which any unit ran other than once."""
    auth = row.get("slice_authority") if isinstance(row, dict) else None
    if not isinstance(auth, dict):
        return None, "receipt %s carries no slice evidence" % (
            (row or {}).get("id"))
    once = gate.slice_tree_refusal(row, repo) if repo else (
        "no repository holds tree %s to re-derive its modules from"
        % str(row.get("tree"))[:12])
    refusal = once
    if refusal is None and auth.get("leak_mode") != "fail":
        refusal = ("sliced receipt %s ran its leak audit in %r mode; only a "
                   "fail-mode run is compared with serial"
                   % (row.get("id"), auth.get("leak_mode")))
    return {"mode": MODE, "workers": auth.get("workers"),
            "modules": auth.get("units"),
            "modules_digest": auth.get("modules_digest"),
            "each_module_once": once is None, "module_refusal": once,
            "assignment_digest": auth.get("assignment_digest"),
            "schedule": auth.get("schedule"),
            "leak_mode": auth.get("leak_mode"), "leaks": auth.get("leaks"),
            "outcome": auth.get("outcome"), "run_exit": row.get("rc"),
            "per_slice_exit": None,
            "per_slice_exit_note": PER_SLICE_EXIT_NOTE}, refusal


def compare_rows(serial, serial_rows, sliced, sliced_rows, repo=None):
    """Judge one train's serial receipt against its shadow's sliced receipt.

    -> {"verdict": AGREE|DISAGREE|UNKNOWN, "reason", "differing": [test ids],
    "differing_counts": ["<ran count>", ...], "outcomes": {"serial": {id:
    outcome}, "sliced": {...}}, "red", "slice_evidence", ...}. `*_rows` are
    each receipt's own ledger rows, which hold its failure identities.

    UNPROVEN SLICE EVIDENCE NEVER AGREES: an AGREE whose modules do not
    re-derive from the tree is UNKNOWN. A DISAGREE stays a DISAGREE and says
    so too, because the clock stopping is the safe direction. A divergence
    blame recorded as a flake of this tree is UNKNOWN, not a DISAGREE: no
    DISABLE marker, and the reason names those tests. One that is not, or a
    flake store that cannot be read, stays a DISAGREE."""
    from . import gatecanary
    s_fail, s_why = gatecanary.failure_identities(serial, serial_rows)
    l_fail, l_why = gatecanary.failure_identities(sliced, sliced_rows)
    judged = gatecanary.compare(serial, sliced, s_fail, l_fail, s_why or l_why)
    judged = gatecanary._with_flakes(
        judged, serial, sliced, (s_fail, l_fail), s_why or l_why, repo)
    if judged["verdict"] == gatecanary.FLAKE_EXPLAINED:
        named = ", ".join(judged.get("explained") or ())
        verdict = UNKNOWN
        reason = "the divergence was flake-explained (%s)" % named
    else:
        verdict = {gatecanary.AGREE: AGREE, gatecanary.DIVERGED: DISAGREE}.get(
            judged["verdict"], UNKNOWN)
        reason = judged["reason"]
    evidence, refusal = slice_evidence(sliced, repo)
    if refusal and verdict == AGREE:
        verdict, reason = UNKNOWN, refusal
    elif refusal and verdict == DISAGREE:
        reason = "%s; and %s" % (reason, refusal)
    found = judged["divergences"]
    return {"verdict": verdict, "reason": reason,
            "divergences": found[:_SHOWN], "divergence_total": len(found),
            "differing": sorted({d["test"] for d in found
                                 if d["kind"] != "count"})[:_SHOWN],
            "differing_counts": [d["test"] for d in found
                                 if d["kind"] == "count"],
            "outcomes": {"serial": _outcomes(s_fail),
                         "sliced": _outcomes(l_fail)},
            "per_test": PER_TEST,
            "shared_failures": judged.get("shared_failures") or 0,
            "red": verdict == AGREE and serial.get("status") == "FAILED"
            and bool(s_fail),
            "serial": _summary(serial), "sliced": _summary(sliced),
            "slice_evidence": evidence}


# ---------------------------------------------------------------- counter

def _name(rec):
    return rec.get("label") or rec["serial_job"]["generation"]


def _is_train(row):
    """A train's land gate: a serial whole-suite receipt under a train's label
    (`gate._TRAIN_LABEL`, the predicate that forces that gate serial)."""
    label = row.get("label")
    return row.get("event") == "gate" and row.get("suite") is True \
        and "slice_authority" not in row and isinstance(label, str) \
        and bool(gate._TRAIN_LABEL.match(label))


def _completions(global_dir):
    """(rows, unavailable) of the Fab completion ledger, every row validated
    as the land doors validate it; one malformed row fails it as a whole."""
    from . import gateimport
    rows, unavailable = eventledger.checked_events(
        os.path.join(global_dir, gateimport.FAB_COMPLETIONS), strict=True)
    if unavailable:
        return None, "the Fab completion ledger is unreadable: %s" % unavailable
    for row in rows:
        err = gateimport._fab_completion_err(row)
        if err:
            return None, "the Fab completion ledger is malformed: %s" % err
    return rows, None


def land_trains(global_dir=None):
    """(trains, unavailable) — THE LAND LEDGER'S TRAINS, in the order their
    receipts came home. The switch clock walks these, never the records.

    A TRAIN is one receipt the land ledger holds (filtered as
    `gate.receipts()` filters it) that `_is_train` admits. A train gated
    again after a RED is two trains: two serial runs, two shadows.

    Each carries `completions`, [(importing repository, generation)] from the
    Fab completion rows naming its receipt. That row is the join: the import
    writes it BEFORE the receipt, so a window train is never home without it;
    its generation is the key a shadow record is filed under (`record_path`),
    and its repository is the project identity the record carries
    (`gatewindow.project_id`, the same canonical common dir)."""
    gd = global_dir or home.global_dir()
    receipts, _rows, unavailable = _ledger(land_ledger(gd))
    if unavailable:
        return None, "the land ledger is unreadable: %s" % unavailable
    completions, why = _completions(gd)
    if why:
        return None, why
    owners = {}
    for row in completions:
        owners.setdefault(row["receipt"], []).append(
            (row["importing_repo"], row["authority"]["generation"]))
    return [{"id": row["id"], "label": row["label"], "tree": row.get("tree"),
             "status": row.get("status"),
             "completions": owners.get(row["id"], [])}
            for row in receipts if _is_train(row)], None


def _bad_record(path):
    """Why the record file at `path` stood for no record."""
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    except (OSError, ValueError) as exc:
        return "unreadable (%s: %s)" % (type(exc).__name__, exc)
    return ("malformed: not a v%d record naming its serial job's generation"
            % RECORD_VERSION)


def _shadow_of(train, project, by_gen, by_serial, global_dir):
    """(record, None) for this train's shadow, (None, why) when a record file
    stands at its generation but holds no record, or (None, None)."""
    for repo, gen in train["completions"]:
        if repo != project:
            continue
        if gen in by_gen:
            return by_gen[gen], None
        path = record_path(gen, global_dir)
        if os.path.lexists(path):
            return None, "its record file %s is %s" % (path, _bad_record(path))
    return by_serial.get(train["id"]), None


def _unmeasured(train, why):
    return {"label": train["label"], "verdict": UNKNOWN,
            "tree": train.get("tree"), "reason": why}


def streak(recs, trains, global_dir=None, unavailable=None):
    """The switch clock of one project: `recs` are its shadow records, and
    `trains` the land ledger's trains (`land_trains`), oldest first.

    WALKED ALONG THE LAND LEDGER, NEVER ALONG THE RECORDS. A record that was
    never written (`schedule` is fenced), is malformed or is unreadable would
    simply be absent from the records, and ten surviving AGREE records would
    then read MET across a train nobody measured. So the clock counts back
    from the newest of this project's trains: each train whose shadow is a
    finished AGREE of that very receipt adds one, and the first train that is
    not — a DISAGREE, an unfinished shadow, no record, an unreadable or
    malformed one, a record of another receipt — stops the count and is named
    with its cause. An unreadable land or completion ledger (`unavailable`)
    stops it before the first train.

    THE JOIN. A train's record is the one filed under the generation its Fab
    completion names for this project; a train no completion names is joined
    by the serial receipt id a record compared (`serial.id`).

    THIS PROJECT'S TRAINS are those a completion attributes to it, and every
    train no completion attributes to anyone, because a train that may be
    this project's must never drop out of its sequence.

    THE START: trains older than this project's FIRST SHADOWED TRAIN — the
    oldest train whose generation or receipt a record file names — are from
    before shadows existed and do not stop the clock. Every train after it
    counts, so a missing record there is UNKNOWN. The start can only move
    later if records are removed, and that only shortens the count: every
    older train has no record and would have stopped it anyway.

    The criterion is met at SWITCH_STREAK agreeing trains, one of them a RED
    both runs caught."""
    project = next((r["project"] for r in recs if r.get("project")), None)
    by_gen = {r["serial_job"]["generation"]: r for r in recs}
    by_serial = {(r.get("serial") or {}).get("id"): r for r in recs
                 if (r.get("serial") or {}).get("id")}
    count, red, stopper = 0, [], None
    if unavailable:
        stopper = {"label": "the land ledger", "verdict": UNKNOWN,
                   "tree": None, "reason": unavailable}
    mine = [] if unavailable else [
        t for t in trains if not t["completions"]
        or project in {repo for repo, _gen in t["completions"]}]
    looked = [(t,) + _shadow_of(t, project, by_gen, by_serial, global_dir)
              for t in mine]
    first = next((i for i, (_t, rec, why) in enumerate(looked)
                  if rec is not None or why), len(looked))
    for train, rec, why in reversed(looked[first:]):
        name = "%s (serial receipt %s)" % (train["label"], train["id"])
        if rec is None:
            stopper = _unmeasured(train, "no readable shadow record for %s: "
                                  "%s" % (name, why) if why else
                                  "no shadow record for %s" % name)
            break
        if rec.get("state") != DONE:
            stopper = dict(rec, verdict=UNKNOWN,
                           reason=rec.get("reason") or
                           "the shadow measurement is not finished")
            break
        if rec.get("verdict") != AGREE:
            stopper = rec
            break
        compared = (rec.get("serial") or {}).get("id")
        if compared != train["id"]:
            stopper = _unmeasured(train, "the shadow record of %s compared "
                                  "serial receipt %s, not this train's"
                                  % (name, compared))
            break
        count += 1
        if rec.get("red"):
            red.append(_name(rec))
    done = [r for r in recs if r.get("state") == DONE]
    last = next((r for r in reversed(done)
                 if r.get("verdict") == DISAGREE), None)
    return {"streak": count, "needed": SWITCH_STREAK,
            "red": red, "met": count >= SWITCH_STREAK and bool(red),
            "stopped_by": None if stopper is None else _cause(stopper),
            "last_disagreement": None if last is None else _cause(last),
            "scheduled": sum(r.get("state") == SCHEDULED for r in recs),
            "running": sum(r.get("state") == RUNNING for r in recs)}


def _cause(rec):
    return {"train": _name(rec), "verdict": rec.get("verdict"),
            "tree": rec.get("tree"), "reason": rec.get("reason"),
            "differing": rec.get("differing") or [],
            "differing_counts": rec.get("differing_counts") or [],
            "marker": rec.get("marker")}


def _cause_text(cause):
    return "none" if cause is None else "%s %s tree %s — %s%s" % (
        cause["train"], cause["verdict"], str(cause["tree"])[:12],
        cause["reason"], "; differing: %s" % ", ".join(
            cause["differing"] + cause["differing_counts"])
        if cause["differing"] or cause["differing_counts"] else "")


def _line(rec):
    serial, sliced = rec.get("serial") or {}, rec.get("sliced") or {}
    return "%-22s %-9s %s%s" % (
        _name(rec)[:22], rec.get("verdict") or rec.get("state"),
        "serial %s %s ran %s | sliced %s %s ran %s — " % (
            str(serial.get("id"))[:8], serial.get("status"),
            serial.get("ran"), str(sliced.get("id"))[:8],
            sliced.get("status"), sliced.get("ran")) if sliced else "",
        rec.get("reason") or "")


def status(global_dir=None, as_json=False, out=None):
    """helm gate shadow — the clock, per project. Reads the shadow store and,
    for the trains the clock walks, the land and Fab completion ledgers."""
    out = out if out is not None else sys.stdout
    projects = {}
    for rec in records(global_dir):
        projects.setdefault(rec.get("project") or "?", []).append(rec)
    trains, unavailable = land_trains(global_dir) if projects else ([], None)
    clocks = {project: streak(recs, trains, global_dir, unavailable)
              for project, recs in projects.items()}
    if as_json:
        print(json.dumps({"mode": MODE, "projects": {
            project: dict(clocks[project], records=[
                {k: rec.get(k) for k in (
                    "label", "state", "verdict", "reason", "tree", "head",
                    "red", "differing", "differing_counts", "marker",
                    "serial", "sliced", "slice_evidence")} for rec in recs])
            for project, recs in projects.items()}},
            ensure_ascii=False, indent=1), file=out)
        return 0
    print("helm gate shadow: a %s run of every train's tree beside its "
          "serial gate; the serial gate is the only land authority" % MODE,
          file=out)
    if not projects:
        print("  no shadow recorded yet", file=out)
    for project, recs in sorted(projects.items()):
        clock = clocks[project]
        print("  project %s" % project, file=out)
        print("  streak: %d consecutive train(s) AGREE per test; switch "
              "criterion %s (needs %d, and a RED both runs caught: %s)" % (
                  clock["streak"], "MET" if clock["met"] else "NOT MET",
                  clock["needed"], ", ".join(clock["red"]) or "none yet"),
              file=out)
        print("  stopped by: %s" % _cause_text(clock["stopped_by"]), file=out)
        print("  last disagreement: %s" % _cause_text(
            clock["last_disagreement"]), file=out)
        print("  pending: %d scheduled, %d running" % (clock["scheduled"],
                                                       clock["running"]),
              file=out)
        for rec in recs[-10:]:
            print("    " + _line(rec), file=out)
    return 0


# ----------------------------------------------------------------- runner

def _fab(argv, timeout=None, env=None):
    """(rc, stdout, stderr); an OSError is rc None, as gatewindow._fab."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                              timeout=timeout or gatewindow.SUBMIT_TIMEOUT_S,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", "%s: %s" % (type(exc).__name__, exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _root(rec):
    """The shared checkout a peek of this record's head is cut from."""
    from . import work
    room = rec.get("room") or ""
    return work.find_root(room) if os.path.isdir(room) else \
        work.find_root(os.path.dirname(rec.get("project") or ""))


def _peek(root, sha):
    from . import work
    return work.peek(root, sha)


def _drop(root, path):
    from . import work
    return work.peek_drop(root, path)


class _Pass:
    """One pass of `helm gate shadow run`. Every seam is a parameter whose
    default is the real thing, so an arm spies the shipped path."""

    def __init__(self, global_dir=None, fab=None, observe=None, peek=None,
                 drop=None, live_land=None, sleep=None, clock=None, out=None):
        self.global_dir = global_dir or home.global_dir()
        self.fab = fab or _fab
        self.observe = observe or gatewindow._fab
        self.peek, self.drop = peek or _peek, drop or _drop
        self.live_land = live_land or self._live_land
        self.sleep, self.clock = sleep or time.sleep, clock or time.time
        self.out = out if out is not None else sys.stdout
        # THE SHADOW'S OWN HOME rides every Fab call: the submit's sliced
        # plan question reads its one-suite door there, and the import binds
        # the receipt there, so neither touches the land ledger.
        os.makedirs(os.path.dirname(shadow_ledger(self.global_dir)),
                    exist_ok=True)
        os.makedirs(shadow_adopted_dir(self.global_dir), exist_ok=True)
        self.env = dict(os.environ, HELM_HOME=shadow_home(self.global_dir),
                        HELM_ADOPTED_DIR=shadow_adopted_dir(self.global_dir))

    def _live_land(self):
        live, _retired, _unknown = gatewindow.live_runs(
            gatewindow.read_runs(gatewindow.runs_path(self.global_dir)),
            observe=self.observe)
        return live

    def run(self):
        finished = []
        for rec in records(self.global_dir):
            if rec.get("state") == DONE:
                continue
            try:
                result = self.one(rec)
            except Exception as exc:        # noqa: BLE001 — a crash is UNKNOWN
                result = self.crashed(rec, exc)
            if result == _YIELD:
                print("helm gate shadow: a land gate is in flight; the shadow "
                      "waits for it", file=self.out)
                break
            if result is not None:
                finished.append(result)
        return finished

    def one(self, rec):
        if rec.get("state") == MARKING:
            return self.retry_marker(rec)
        if rec.get("state") == CANCELING:
            return self.retry_cancel(rec)
        if rec.get("state") == SCHEDULED:
            serial, why = self.serial(rec)
            if serial is None:
                return None if why is None else self.finish(rec, UNKNOWN, why)
            if self.live_land():
                return _YIELD
            if not self.start(rec, serial):
                return rec
        return self.follow(rec)

    def serial(self, rec):
        """(receipt, None) once the serial result is home; (None, None) to
        wait; (None, why) when it will never come."""
        age = self.clock() - (rec.get("scheduled_at") or 0)
        late = age > PENDING_LIMIT_S
        state = gatewindow.job_state(rec["serial_job"], observe=self.observe)
        if state is None or not gatewindow._job_over(state):
            return None, ("the serial job was still %s %ds after its launch"
                          % ("unreadable" if state is None
                             else state["state"], age) if late else None)
        if state["state"] != "COMPLETED":
            return None, ("the serial job ended %s: there is no serial result "
                          "to compare" % state["state"])
        rid = state["receipt"] if state["receipt_state"] == "EXACT" else None
        if not rid:
            return None, ("the serial job ended exit %s and named no receipt"
                          % state["exit"])
        found = next((r for r in _ledger(land_ledger(self.global_dir))[0]
                      if r.get("id") == rid), None)
        if found is None:
            return None, ("serial receipt %s never came home to the land "
                          "ledger" % rid if late else None)
        return found, None

    def start(self, rec, serial):
        """Cut the peek, measure, submit. -> True with the record RUNNING, or
        False with it finished UNKNOWN."""
        rec["serial"] = _summary(serial)
        rc, peeked = self.peek(_root(rec), rec["head"])
        if rc != 0:
            self.finish(rec, UNKNOWN, "no room for the shadow: %s"
                        % peeked.get("error"))
            return False
        room = rec["shadow_room"] = peeked["path"]
        rc, text, err = self.fab(
            [gatewindow.FAB_BINARY, "gate", "measure", "--repo", room,
             "--tree", rec["tree"], "--scope-json",
             gatewindow._canonical(fabgate.slice_scope()), "--json"],
            gatewindow.MEASURE_TIMEOUT_S, self.env)
        measured = gatewindow.last_event(text)
        if rc != 0 or not isinstance(measured, dict) \
                or measured.get("event") != "gate-measure" \
                or not isinstance(measured.get("interpreter"), dict) \
                or not isinstance(measured.get("runner"), dict) \
                or not gatewindow._ATOM.fullmatch(
                    str(measured.get("host") or "")):
            self.finish(rec, UNKNOWN, "`fab gate measure` named no host, "
                        "interpreter and runner for the slice scope (exit %s): "
                        "%s" % (rc, (err or text).strip()[-300:]))
            return False
        if not gatewindow.forwards_option(measured, gatewindow.SHADOW_OPTION):
            self.finish(rec, UNKNOWN, "the Fab admission door does not "
                        "advertise %s; starting this shadow could alter a land "
                        "gate's placement" % gatewindow.SHADOW_OPTION)
            return False
        job, why = fabgate.request(room, rec["tree"], fabgate.slice_scope(),
                                   measured["interpreter"], measured["runner"])
        if why:
            self.finish(rec, UNKNOWN, "no sliced gate-job request: %s" % why)
            return False
        label = (LABEL_PREFIX + rec.get("label", "")).strip()[:120] \
            if gatewindow.forwards_label(measured) else None
        rc, text, err = self.fab(gatewindow.submit_argv(
            room, job, label, shadow=True), gatewindow.SUBMIT_TIMEOUT_S,
            self.env)
        event = gatewindow.last_event(text)
        event = event if isinstance(event, dict) else {}
        handle, why = fabgate._handle(event.get("handle"), job["key"])
        if why or event.get("disposition") not in fabgate.DISPOSITIONS - {
                "UNKNOWN"}:
            self.finish(rec, UNKNOWN, "`fab gate submit` gave the sliced job "
                        "no exact handle (exit %s): %s" % (
                            rc, fabgate.refusal_text(why, event) if why
                            else (err or text).strip()[-300:]))
            return False
        rec.update(state=RUNNING, submitted_at=self.clock(),
                   reason="the sliced job is running",
                   shadow_job=dict({k: handle[k] for k in (
                       "key", "job_id", "host", "generation")},
                                   tree=rec["tree"]))
        _save(rec, self.global_dir)
        return True

    def follow(self, rec):
        job = dict(rec["shadow_job"], room=rec["shadow_room"])
        deadline = (rec.get("submitted_at") or self.clock()) + RUN_TIMEOUT_S
        while True:
            state = gatewindow.job_state(job, observe=self.observe)
            now = self.clock()
            if now > deadline:
                why = ("timed out: the sliced job's completion was not observed "
                       "within %ds of its submit" % RUN_TIMEOUT_S)
                if state is not None and gatewindow._job_over(state):
                    return self.finish(rec, UNKNOWN, why)
                return self.begin_cancel(rec, why)
            if state is not None and gatewindow._job_over(state):
                break
            blocking = self.live_land()
            if blocking:
                return self.begin_cancel(
                    rec, "yielded to the land gate %s: a shadow never shares the "
                    "fabric with the authority" % (
                        blocking[0].get("label") or blocking[0].get("job_id")))
            self.sleep(POLL_S)
        if state["state"] != "COMPLETED":
            return self.finish(rec, UNKNOWN, "the sliced job ended %s (exit "
                               "%s)" % (state["state"], state["exit"]))
        rc, text, err = self.fab(gatewindow.import_argv(job),
                                 gatewindow.SUBMIT_TIMEOUT_S, self.env)
        rid = state["receipt"] if state["receipt_state"] == "EXACT" else None
        found, rows, _unavailable = _ledger(shadow_ledger(self.global_dir))
        sliced = next((r for r in found if r.get("id") == rid), None)
        if sliced is None:
            return self.finish(rec, UNKNOWN, "sliced receipt %s did not come "
                               "home to the shadow ledger (import exit %s): %s"
                               % (rid or "(none named)", rc,
                                  (err or text).strip()[-300:]))
        land, land_rows, _unavailable = _ledger(land_ledger(self.global_dir))
        serial = next((r for r in land if r.get("id") == rec["serial"]["id"]),
                      None)
        if serial is None:
            return self.finish(rec, UNKNOWN, "serial receipt %s is no longer "
                               "readable in the land ledger" % rec["serial"]["id"])
        result = compare_rows(serial, land_rows, sliced, rows, _root(rec))
        verdict, reason = result.pop("verdict"), result.pop("reason")
        if verdict == DISAGREE:
            rec.update(result, state=MARKING, verdict=DISAGREE, reason=reason)
            _save(rec, self.global_dir)
            return self.retry_marker(rec)
        return self.finish(rec, verdict, reason, **result)

    def retry_marker(self, rec):
        """Retry the one durable act that makes a measured DISAGREE fail closed.

        The comparison already stands in the record. A failed marker write is
        therefore neither DONE nor re-run: the next pass retries from those
        durable facts until the canary marker is whole on disk."""
        from . import gatecanary
        # THE RECORD FIRST, AND INDEPENDENTLY OF THE MARKER: the canary
        # record is the land door's other veto, so a DISAGREE whose marker
        # cannot be written still closes sliced land through it. Retried on
        # every pass until it is durable; `finish` never records it twice.
        if not rec.get("canary_recorded"):
            rec["canary_recorded"] = _to_canary_record(rec, self.global_dir)
            _save(rec, self.global_dir)
        result = {"reason": "the shadow of %s disagreed: %s"
                            % (_name(rec), rec["reason"]),
                  "divergences": rec.get("divergences") or []}
        serial = {"id": (rec.get("serial") or {}).get("id"),
                  "tree": rec.get("tree"), "head": rec.get("head")}
        sliced = {"id": (rec.get("sliced") or {}).get("id")}
        try:
            marker = gatecanary.write_marker(result, serial, sliced,
                                              self.global_dir)
        except Exception as exc:      # noqa: BLE001 — as record() catches it
            # any failure is a pending marker with the record's veto beside it
            rec.update(state=MARKING, verdict=DISAGREE,
                       marker_error="%s: %s" % (type(exc).__name__, exc),
                       marker_attempted_at=self.clock())
            _save(rec, self.global_dir)
            print("helm gate shadow: marker pending for %s — %s" % (
                _name(rec), rec["marker_error"]), file=self.out)
            if not rec["canary_recorded"] and not rec.get("no_veto_said"):
                gatecanary.no_veto(
                    "the shadow of %s disagreed on tree %s, its DISABLE "
                    "marker could not be written (%s) and its DIVERGED could "
                    "not be recorded in %s" % (
                        _name(rec), str(rec.get("tree"))[:12],
                        rec["marker_error"],
                        gatecanary.history_path(self.global_dir)),
                    self.global_dir)
                rec["no_veto_said"] = True
                _save(rec, self.global_dir)
            return None
        return self.finish(rec, DISAGREE, rec["reason"], marker=marker,
                           marker_error=None)

    def begin_cancel(self, rec, reason):
        """Persist cancellation before trying it, so a failed kill is retried."""
        rec.update(state=CANCELING, verdict=UNKNOWN, reason=reason)
        _save(rec, self.global_dir)
        return self.retry_cancel(rec)

    def retry_cancel(self, rec):
        """Retry cancellation of the exact recorded generation until accepted."""
        try:
            rc = self.cancel(rec)
            detail = "cancel exited %s" % rc
        except Exception as exc:             # noqa: BLE001 — retry stays durable
            rc = None
            detail = "%s: %s" % (type(exc).__name__, exc)
        if rc == 0:
            return self.finish(rec, UNKNOWN, rec["reason"], cancel_error=None)
        rec.update(state=CANCELING, verdict=UNKNOWN, cancel_error=detail,
                   cancel_attempted_at=self.clock())
        _save(rec, self.global_dir)
        print("helm gate shadow: cancellation pending for %s — %s" % (
            _name(rec), detail), file=self.out)
        return None

    def crashed(self, rec, exc):
        """A crash is UNKNOWN, never AGREE; a live job stays cancellation-pending."""
        why = "the shadow run crashed: %s: %s" % (type(exc).__name__, exc)
        if rec.get("state") == MARKING:
            rec.update(marker_error=why, marker_attempted_at=self.clock())
            _save(rec, self.global_dir)
            return None
        if rec.get("shadow_job"):
            return self.begin_cancel(rec, why)
        return self.finish(rec, UNKNOWN, why)

    def cancel(self, rec):
        job = rec.get("shadow_job")
        if not job:
            return 0
        rc, _text, _err = self.fab(gatewindow._kill_argv(job), KILL_TIMEOUT_S,
                                   self.env)
        return rc

    def finish(self, rec, verdict, reason, **fields):
        rec.update(fields, state=DONE, verdict=verdict, reason=reason,
                   finished_at=self.clock())
        _save(rec, self.global_dir)
        if not rec.get("canary_recorded"):
            _to_canary_record(rec, self.global_dir)
        if rec.get("shadow_room"):
            try:
                self.drop(_root(rec), rec["shadow_room"])
            except Exception:               # noqa: BLE001 — a kept peek is
                pass                        # the peek cadence's to reap
        print("helm gate shadow: " + _line(rec), file=self.out)
        return rec


def _to_canary_record(rec, global_dir):
    """Every finished shadow is one verdict in the CANARY RECORD, the one
    record the land door reads (`gatecanary.standing`): a DISAGREE as the
    canary's DIVERGED, an UNKNOWN as UNKNOWN, which that record neither
    counts nor lets restart its count. -> True when the verdict is durable
    (`gatecanary.append_verdict`, which never raises)."""
    from . import gatecanary
    verdict = {AGREE: gatecanary.AGREE, DISAGREE: gatecanary.DIVERGED}.get(
        rec.get("verdict"), gatecanary.UNKNOWN)
    serial, sliced = (dict(rec.get(k) or {}) for k in ("serial", "sliced"))
    serial.update(tree=rec.get("tree"), host={"node": serial.get("host")})
    sliced.update(tree=rec.get("tree"), host={"node": sliced.get("host")},
                  slice_authority={"leak_mode": (
                      rec.get("slice_evidence") or {}).get("leak_mode")})
    return gatecanary.append_verdict(
        {"verdict": verdict, "reason": rec.get("reason"),
         "shared_failures": rec.get("shared_failures") or 0,
         "divergences": rec.get("divergences") or []},
        serial, sliced, gatecanary.SHADOW, global_dir)


def run_pass(global_dir=None, **seams):
    """ONE pass over every shadow not yet DONE, oldest launch first.
    -> [finished records], or None when another pass holds the lock."""
    lock = os.path.join(store_dir(global_dir), "run.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        return _Pass(global_dir, **seams).run()


# ------------------------------------------------------------------ timer

_SERVICE = """[Unit]
Description=helm gate shadow — a sliced run beside each train's serial gate

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
# One pass: each train whose serial gate is over gets its sliced run, compared
# test by test; the serial gate stays the only land authority.
ExecStart=%(helm)s gate shadow run
Nice=15
"""

_TIMER = """[Unit]
Description=helm gate shadow cadence

[Timer]
OnBootSec=600
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def timer_units(interval=TIMER_INTERVAL_S, inputs=None):
    """(service_path, service_text, timer_path, timer_text), the working
    directory derived from this checkout's shared root, never a literal.
    `inputs` replaces per-install values (timerhealth.unit_values)."""
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    from . import timerhealth
    udir = timerhealth.user_unit_dir()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % timerhealth.unit_values({"helm": helm_bin, "cwd": cwd},
                                               inputs),
            os.path.join(udir, TIMER_NAME),
            _TIMER % timerhealth.unit_values({"interval": interval}, inputs))


def ensure_timer():
    """(ok, detail) — install and enable the cadence."""
    import shutil
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm gate shadow run` "
                       "from another scheduler")
    spath, service, tpath, timer = timer_units()
    from . import timerhealth
    error, _unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (TIMER_NAME,), systemctl,
        subprocess)
    if error:
        return False, error
    return True, "every %ds (%s)" % (TIMER_INTERVAL_S, tpath)


# -------------------------------------------------------------------- cli

USAGE = ("usage: helm gate shadow [status] [--json] | run | --install-timer")


def cmd(rest):
    rest = list(rest or ())
    if rest == ["--install-timer"]:
        ok, detail = ensure_timer()
        print("helm gate shadow: %s" % detail,
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if rest in ([], ["status"], ["--json"], ["status", "--json"]):
        return status(as_json="--json" in rest)
    if rest == ["run"]:
        if run_pass() is None:
            print("helm gate shadow: another pass holds the lock")
        return 0
    print(USAGE, file=sys.stderr)
    return 2
