"""The nightly serial canary: does a sliced run still agree with serial?

WHY IT EXISTS (task/3039). A sliced receipt (v10) runs helm's whole suite as
slices of one serial discovery, and the runner's leak audit now watches module
data as well as process state, so on every tree measured so far the two
runners agree. That is a claim about the suite as it stands. A later module
can add a channel no audit watches, and then a sliced run can be green where
serial is red. Before sliced receipts may authorize a land, something must
keep checking the claim against the run it stands for.

WHAT IT DOES. Once a night, on trunk's tip, it holds ONE serial and ONE
sliced whole-suite receipt of the same tree and compares them test by test:
every failure identity with its kind, plus the run and skip counts, which is
the only form a passing test takes in a receipt. The serial one is usually
the receipt trunk's own train gate minted on those bytes; whichever kind the
tree lacks is run through the launcher (default `fab gate`, the owner's route
for suites; HELM_GATE_CANARY_LAUNCH names another, split like a shell line)
under HELM_GATE_CANARY, which the one-suite-per-tree door admits for the kind
the tree's last receipt is not. A tree already judged is SKIPPED.

  AGREE      the same failures, the same counts. Recorded; nothing is posted.
  DIVERGED   some test's outcome differs, and blame did not record every one
             of them as a flake of this tree. The canary writes the DISABLE
             marker in the helm home and posts one alert to the helm room.
             `gate.sliced_land_disabled()` reads the marker, and a sliced
             receipt must not authorize a land while it stands.
  FLAKE-EXPLAINED
             every differing test was recorded FLAKE for this tree by
             `helm train blame`. Recorded, with those tests listed; no
             marker and no alert. It neither restarts the land-door count
             nor counts as an agreeing tree.
  UNKNOWN    either run is unreadable, or they are not the same tree. No
             marker, because nothing was compared; the alert says so, because
             a canary that silently stops measuring is no canary.

EVERY VERDICT IS RECORDED (`history_path`, one JSON line each, append-only):
this nightly run's, every `helm gate canary compare` of a serial and a sliced
receipt, every train's sliced shadow (helm/gateshadow.py), every `finder` run
of the one-pass finder (`gateslice.py --finder`) and every `catch` of a red
tree. The record is what the land door reads (`standing`) before a sliced
receipt may authorize a land; `standing` states the rule. A DIVERGED restarts
it, so a DIVERGED recorded after the last AGREE leaves nothing standing.

AFTER THE FLIP THIS RUN IS THE SAFETY NET. A land on a sliced receipt leaves
trunk's tree without a serial one, so the nightly run gates it serial, on the
host HELM_GATE_CANARY_HOST names, and its first DIVERGED writes the marker
that turns the next land gate serial (`gatewindow.land_mode`).

THE MARKER IS DURABLE. A later AGREE does not remove it: a divergence that
comes and goes is exactly the kind a single green night must not wash out. It
is cleared by a person who read it (`helm gate canary clear --reason ...`),
and clearing archives it beside itself with the reason, never deletes it. If
its original record write failed, clear first records that DIVERGED or refuses.

A slice-only failure is a divergence too, including the audit's own
`LeakAudit` errors: they fail no serial test, so the two runs disagree about
what is red, and an audit finding on trunk's tip means a mutation reached
trunk. The comparison names each kind.

THE MARKER AND THE RECORD ARE WRITTEN IN THE LAND ORDER (task/3265 races R2).
Both are land vetoes, and `helm train auto` asks them again beside its push.
So every write of either takes the readiness lock the push holds
(`landorder.locked`) before it writes: a veto lands before the push's last
read or waits until the push is done, never between the two.
"""
import calendar
import collections
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

from . import chat, eventledger, gate, gateslice, home, landorder, pk, vcs

STATE_SUBDIR = os.path.join(".state", "gate-canary")
LAST_NAME = "last.json"
HISTORY_NAME = "verdicts.jsonl"
HISTORY_VERSION = 1
# The integrator's evidence rule (`standing`): this many distinct trees whose
# serial and sliced receipts AGREE test for test since the newest DIVERGED,
# their sliced runs on this many distinct hosts, beside a clean finder run and
# a red tree whose real failures the sliced run caught.
STANDING_AGREE = 3
STANDING_HOSTS = 2
# (v) THE CANARY MUST STILL BE SPEAKING. Nothing in (i)-(iv) expires, so a
# canary that stops producing evidence would leave sliced lands admitted for
# good. The freshest evidence a compared verdict holds, the older of its two
# receipts' own stamps, must be no older than this many hours: one nightly
# run plus slack. The operator may only TIGHTEN it with MAX_AGE_ENV (a larger
# value is clamped to the default, and one that is not a positive finite
# number keeps it), so no setting can keep an old record fresh.
MAX_AGE_ENV = "HELM_GATE_CANARY_MAX_AGE_H"
DEFAULT_MAX_AGE_H = 36
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# Who recorded a verdict: the nightly run, a person's `compare`, a shadow, or
# the clear door preserving a marker whose original record write failed.
RUN, COMPARE, SHADOW, CLEAR = "run", "compare", "shadow", "clear"
SOURCES = (RUN, COMPARE, SHADOW, CLEAR)
LOG_NAME = "last-launch.log"
DEFAULT_LAUNCH = ("fab", "gate")
LAUNCH_TIMEOUT_S = 3 * 3600
ROOM = "helm"
# A machine sender (helm/machine_senders.py): its rows are pulled, never pushed.
WHO = "gate-canary"
TIMER_NAME = "helm-gate-canary.timer"
SERVICE_NAME = "helm-gate-canary.service"
AGREE, DIVERGED, UNKNOWN = "AGREE", "DIVERGED", "UNKNOWN"
# A divergence blame already recorded as a flake of THIS tree. Not a veto:
# no DISABLE marker, and `standing` neither restarts on it nor counts it.
FLAKE_EXPLAINED = "FLAKE-EXPLAINED"
# A night whose trunk tree was already judged: nothing is run or recorded.
SKIPPED = "SKIPPED"
MARKER_VERSION = 1
_SHOWN = 40


def state_dir(global_dir=None):
    return os.path.join(global_dir or home.global_dir(), STATE_SUBDIR)


def last_path(global_dir=None):
    return os.path.join(state_dir(global_dir), LAST_NAME)


def history_path(global_dir=None):
    return os.path.join(state_dir(global_dir), HISTORY_NAME)


# ------------------------------------------------------------------ compare

def failure_identities(row, ledger_rows):
    """Counter{(kind, test id)} of every failure the receipt records, or
    (None, why) when the record cannot be read whole. -> (counter, why)

    A v8+ record's full identities travel as sibling chunk events; a capped
    legacy list that dropped identities is incomplete, and incomplete is
    UNKNOWN here, never a shorter list."""
    if row.get("failures_unreadable") or row.get("status") not in (
            "OK", "FAILED"):
        return None, "receipt %s has no readable verdict (status %s)" % (
            row.get("id"), row.get("status"))
    items = row.get("failures")
    if gate._version_has(row, "failure_record"):
        chunks, errors, _skipped = gate._failure_chunks_with_errors(
            ledger_rows)
        err = gate._failure_record_error(row, chunks, errors)
        if err:
            return None, "receipt %s: %s" % (row.get("id"), err)
        if row.get("failure_chunks"):
            items = [item for ref in row["failure_chunks"]
                     for item in chunks[ref]["failures"]]
    if not isinstance(items, list) or any(
            not isinstance(item, dict) or "truncated" in item
            or not item.get("test") for item in items):
        return None, "receipt %s does not record every failure identity" \
            % row.get("id")
    return collections.Counter(
        (str(item.get("kind") or "FAIL"), str(item["test"]))
        for item in items), None


def _audit_row(test):
    return test.endswith("." + gateslice.LEAK_TEST)


def compare(serial, sliced, serial_failures, sliced_failures, why=None,
             flakes=None):
    """Judge one serial and one sliced receipt of the same tree.

    -> {"verdict", "reason", "divergences": [{"test", "serial", "sliced",
    "kind"}]}. `*_failures` are `failure_identities` counters, or None when
    that receipt's record is unreadable (UNKNOWN, for `why`).

    `flakes` is the caller's record, and this function only reads it. None
    explains nothing, which is every caller that has not seen a divergence.
    A str is a store that was needed and could not be read: a divergence then
    stays DIVERGED and the reason names that store. A dict {tree: set(test
    ids)} explains a divergence whose test blame recorded FLAKE for
    `serial["tree"]`. Every divergence explained is FLAKE-EXPLAINED, and the
    result lists those tests; one that is not stays DIVERGED, whole."""
    def unknown(reason):
        return {"verdict": UNKNOWN, "reason": reason, "divergences": []}

    if serial.get("v") == gate.SLICE_VERSION or not gate.stored_whole_suite(
            serial):
        return unknown("receipt %s is not a serial whole-suite receipt"
                       % serial.get("id"))
    if sliced.get("v") != gate.SLICE_VERSION or gate.slice_refusal(sliced):
        return unknown("receipt %s is not a well-formed sliced receipt"
                       % sliced.get("id"))
    if serial.get("tree") != sliced.get("tree") or not serial.get("tree"):
        return unknown("the receipts are of different trees (%s, %s)" % (
            str(serial.get("tree"))[:12], str(sliced.get("tree"))[:12]))
    if serial_failures is None or sliced_failures is None:
        return unknown(why or "a receipt's failure record is unreadable")
    divergences = []
    for key in sorted(set(serial_failures) | set(sliced_failures),
                      key=lambda k: (k[1], k[0])):
        kind, test = key
        s, l = serial_failures.get(key, 0), sliced_failures.get(key, 0)
        if s == l:
            continue
        divergences.append({
            "test": test,
            "serial": "%s x%d" % (kind, s) if s else "not %s" % kind,
            "sliced": "%s x%d" % (kind, l) if l else "not %s" % kind,
            "kind": "audit" if _audit_row(test) else (
                "serial-only" if s > l else "sliced-only")})
    # THE COUNTS ARE COMPARED ON ONE HOST ONLY. A receipt names no skipped
    # or passing test, so a count carries no test identity, and a skip can
    # be the HOST's: measured, every serial receipt on one build node skipped
    # one test more than every serial receipt on the other, at the same
    # interpreter. Across two hosts a count difference is kept as a note and
    # decides nothing; the failures and errors, compared by test id above,
    # decide. A host the receipts do not name is not a different host.
    hosts = [_node(row) for row in (serial, sliced)]
    cross = all(hosts) and hosts[0] != hosts[1]
    notes = []
    for field in ("ran", "skipped"):
        a, b = serial.get(field) or 0, sliced.get(field) or 0
        if a != b:
            (notes if cross else divergences).append({
                "test": "<%s count>" % field, "serial": str(a),
                "sliced": str(b), "kind": "host-count" if cross else "count"})
    note = ("; counts differ across hosts %s and %s (%s), which names no "
            "test and decides nothing" % (hosts[0], hosts[1], ", ".join(
                "%s serial=%s sliced=%s" % (n["test"], n["serial"],
                                            n["sliced"]) for n in notes))
            if notes else "")
    if divergences:
        recorded = flakes.get(serial.get("tree")) \
            if isinstance(flakes, dict) else None
        explained = [d["test"] for d in divergences
                     if d["kind"] != "audit" and recorded is not None
                     and d["test"] in recorded]
        if isinstance(flakes, dict) and len(explained) == len(divergences):
            return {"verdict": FLAKE_EXPLAINED, "divergences": divergences,
                    "explained": explained, "notes": notes,
                    "reason": "%d outcome(s) differ, each recorded FLAKE for "
                    "this tree (%s)" % (len(divergences), ", ".join(
                        explained))}
        worst = sum(d["kind"] == "serial-only" for d in divergences)
        closed = ("; the flake store could not be read (%s), so a recorded "
                 "flake cannot explain them" % flakes) \
            if isinstance(flakes, str) else ""
        return {"verdict": DIVERGED, "divergences": divergences,
                "notes": notes,
                "reason": "%d outcome(s) differ%s%s%s%s" % (
                    len(divergences),
                    "; %d failure(s) serial saw and slices did not" % worst
                    if worst else "",
                    "; the runs were on different hosts (%s, %s)" % tuple(
                        hosts) if cross else "", note, closed)}
    return {"verdict": AGREE, "divergences": [], "notes": notes,
            "shared_failures": sum(n for (_kind, test), n
                                   in serial_failures.items()
                                   if not _audit_row(test)),
            "reason": "the same %d failure(s), %s ran, %s skipped%s" % (
                sum(serial_failures.values()), serial.get("ran"),
                serial.get("skipped") or 0, note)}


# ------------------------------------------------------------------- marker

def write_marker(result, serial, sliced, global_dir=None):
    """The DISABLE marker, written whole or not at all. -> path

    A marker that already stands keeps its `since`: the newest divergence
    replaces the evidence, never the moment sliced-at-land was disabled.
    Written in the land order (the module docstring): a lock that cannot be
    taken never withholds the veto, so it is written anyway."""
    path = gate.sliced_land_marker_path(global_dir)
    with landorder.locked(global_dir=global_dir):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        prior = pk.read_json(path, default=None)
        pk.write_json(path, {
            "v": MARKER_VERSION,
            "since": prior.get("since") or now if isinstance(prior, dict)
            else now,
            "last": now,
            "tree": serial.get("tree"), "head": serial.get("head"),
            "serial": serial.get("id"), "sliced": sliced.get("id"),
            "reason": result["reason"],
            "divergences": result["divergences"][:_SHOWN],
            "divergence_total": len(result["divergences"]),
        })
    return path


def _preserve_marker(marker, reason, global_dir=None):
    """Ensure the marker's DIVERGED survives its removal. -> bool"""
    fields = (marker.get("tree"), marker.get("serial"), marker.get("sliced"))
    if marker.get("v") != MARKER_VERSION \
            or not all(isinstance(value, str) and value for value in fields):
        return False
    path = history_path(global_dir)
    if os.path.exists(path):
        rows, unavailable = eventledger.checked_events(path, strict=True)
        if unavailable:
            return False
        if any(row.get("event") not in (FINDER_ROW_EVENT, CATCH_EVENT)
               and row.get("verdict") == DIVERGED
               and tuple(row.get(key) for key in ("tree", "serial", "sliced"))
               == fields for row in rows):
            return True
    divergences = marker.get("divergences")
    result = {"verdict": DIVERGED,
              "reason": "marker cleared after %s: %s" % (
                  reason, marker.get("reason") or "no reason recorded"),
              "divergences": divergences if isinstance(divergences, list)
              else []}
    serial = {"tree": fields[0], "id": fields[1]}
    sliced = {"tree": fields[0], "id": fields[2]}
    return append_verdict(result, serial, sliced, CLEAR, global_dir)


def clear_marker(reason, global_dir=None):
    """Archive the marker beside itself with who-cleared-it-why. -> (rc, line)

    Never a deletion: the archived file keeps the divergence that disabled
    sliced-at-land and the reason it was judged cleared. Before removal, its
    DIVERGED must stand in the record so old green evidence cannot revive."""
    reason = str(reason or "").strip()
    if not reason:
        return 2, "helm gate canary clear: --reason is required"
    path = gate.sliced_land_marker_path(global_dir)
    if not os.path.exists(path):
        return 0, "helm gate canary: no sliced-land marker stands"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = "%s.cleared-%s" % (path, stamp)
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        try:
            marker = json.loads(body)
        except ValueError:
            marker = {"unreadable": body}
        if not isinstance(marker, dict):
            marker = {"unreadable": body}
        if not _preserve_marker(marker, reason, global_dir):
            return 1, ("helm gate canary: cannot clear %s — its DIVERGED is "
                       "not durable in %s; repair the marker or record, then "
                       "retry" % (path, history_path(global_dir)))
        marker["cleared"] = {"at": stamp, "reason": reason}
        pk.write_json(archive, marker)
        os.unlink(path)
    except OSError as exc:
        return 1, "helm gate canary: cannot clear %s — %s" % (path, exc)
    return 0, "helm gate canary: marker cleared, archived at %s" % archive


# ------------------------------------------------------------------ record

def _node(row):
    host = row.get("host") if isinstance(row, dict) else None
    return host.get("node") if isinstance(host, dict) else None


def history_row(result, serial, sliced, source):
    """The one line a verdict leaves in the canary record. `red` is an AGREE
    whose serial run FAILED on at least one real test (never a slice audit
    row) that the sliced run failed on too."""
    serial, sliced = serial or {}, sliced or {}
    auth = sliced.get("slice_authority")
    auth = auth if isinstance(auth, dict) else {}
    shared = result.get("shared_failures") or 0
    agree = result["verdict"] == AGREE
    return {"v": HISTORY_VERSION, "event": VERDICT_EVENT,
            "id": os.urandom(16).hex(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "evidence_at": evidence_at(serial, sliced),
            "source": source, "verdict": result["verdict"],
            "reason": str(result.get("reason") or "")[:500],
            "tree": serial.get("tree") or sliced.get("tree"),
            "serial": serial.get("id"), "sliced": sliced.get("id"),
            "serial_status": serial.get("status"),
            "hosts": [_node(serial), _node(sliced)],
            "leak_mode": auth.get("leak_mode"),
            "shared_failures": shared if agree else 0,
            "red": agree and serial.get("status") == "FAILED" and shared > 0,
            "divergence_total": len(result.get("divergences") or ()),
            "explained": [str(t) for t in (result.get("explained") or ())]}


def _append_record(row, global_dir=None):
    """Append one row to the canary record, in the land order (the module
    docstring). -> True when it is durable. A lock that cannot be taken
    never withholds the row: a DIVERGED or an unclean finder run is a veto."""
    with landorder.locked(global_dir=global_dir):
        return eventledger.append(history_path(global_dir), row)


def append_verdict(result, serial, sliced, source, global_dir=None):
    """Append one verdict to the canary record. -> True when it is durable.

    Never raises: a verdict the record could not keep is a verdict the land
    door cannot count, which is the safe direction."""
    try:
        return _append_record(history_row(result, serial, sliced, source),
                              global_dir)
    except Exception:           # noqa: BLE001 -- see the docstring
        return False


VERDICT_EVENT = "gate-canary-verdict"
FINDER_ROW_EVENT = "gate-canary-finder"
CATCH_EVENT = "gate-canary-catch"
EACH, WHOLE = "each", "whole"


def _common_ok(row):
    return isinstance(row, dict) and row.get("v") == HISTORY_VERSION \
        and isinstance(row.get("tree"), str) and bool(row["tree"]) \
        and isinstance(row.get("at"), str)


def _well_formed(row):
    if not _common_ok(row):
        return False
    event = row.get("event")
    if event == FINDER_ROW_EVENT:
        return row.get("scope") in (EACH, WHOLE) \
            and isinstance(row.get("clean"), bool) \
            and isinstance(row.get("host"), str)
    if event == CATCH_EVENT:
        return isinstance(row.get("caught"), bool) \
            and isinstance(row.get("real_failures"), int)
    return row.get("verdict") in (AGREE, DIVERGED, UNKNOWN,
                                   FLAKE_EXPLAINED) \
        and row.get("source") in SOURCES and isinstance(row.get("red"), bool)


def finder_rows(result, tree, global_dir=None):
    """Append the finder's two measurements (`gateslice.py --finder`) of
    `tree` to the canary record. -> True when both are durable."""
    ok = True
    for scope in (EACH, WHOLE):
        part = result.get(scope) or {}
        ok = _append_record({
            "v": HISTORY_VERSION, "event": FINDER_ROW_EVENT,
            "id": os.urandom(16).hex(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scope": scope, "tree": tree, "host": str(result.get("host")),
            "clean": part.get("clean") is True,
            "modules": part.get("modules"), "ran": part.get("ran"),
            "leaking": part.get("leaking") or {},
            "failing": part.get("failing") or {},
            "broken": part.get("broken") or {}}, global_dir) and ok
    return ok


def catch(serial, sliced, serial_failures, sliced_failures):
    """Did the sliced run catch every real failure the serial run caught?
    -> {"caught", "real_failures", "missing"}. Only what serial FAILED on is
    asked: slice-only rows (a tree older than a leak's cure carries its
    audit errors) are the compare's question, never this one."""
    real = sorted(key for key in (serial_failures or {})
                  if not _audit_row(key[1]))
    missing = [key for key in real
               if (sliced_failures or {}).get(key, 0) < serial_failures[key]]
    return {"real_failures": len(real),
            "missing": ["%s %s" % key for key in missing][:_SHOWN],
            "caught": bool(real) and not missing
            and serial.get("tree") == sliced.get("tree")
            and serial.get("status") == "FAILED"}


def max_age_h():
    """The freshness bound in hours: MAX_AGE_ENV when it is a positive finite
    number no larger than DEFAULT_MAX_AGE_H, which it may only tighten; the
    default otherwise."""
    try:
        hours = float(os.environ.get(MAX_AGE_ENV) or DEFAULT_MAX_AGE_H)
    except ValueError:
        return DEFAULT_MAX_AGE_H
    return min(hours, DEFAULT_MAX_AGE_H) \
        if math.isfinite(hours) and hours > 0 else DEFAULT_MAX_AGE_H


def _epoch(stamp):
    """A record or receipt stamp as epoch seconds, or None when unreadable."""
    try:
        return calendar.timegm(time.strptime(stamp, STAMP))
    except (TypeError, ValueError, OverflowError):
        return None


def _age_h(stamp):
    """Hours since a stamp, or None when the stamp cannot be read."""
    then = _epoch(stamp)
    return None if then is None else (time.time() - then) / 3600.0


def evidence_at(serial, sliced):
    """When the evidence of a compared pair was produced: the older of the
    two receipts' own `ts`, or None when either carries no readable stamp.
    (v) is measured by it, never by when the verdict was written, so a
    `compare` of an old pair refreshes nothing."""
    stamps = [_epoch((row or {}).get("ts")) for row in (serial, sliced)]
    if None in stamps:
        return None
    return time.strftime(STAMP, time.gmtime(min(stamps)))


def standing(global_dir=None):
    """What the canary record says about letting a sliced receipt authorize
    a land. -> {"finder", "interaction", "agree", "trees", "hosts", "red",
    "needed", "hosts_needed", "last_diverged", "newest", "max_age_h", "met",
    "why", "path"}; `why` is None exactly when `met`.

    THE RULE (the integrator's, door owner). Counted from the newest DIVERGED
    verdict, all four must hold:
      (i)   the newest one-pass finder run, every module alone in a fresh
            process, is clean;
      (ii)  the newest report-mode sliced whole suite of the finder shows no
            leak and no failure;
      (iii) STANDING_AGREE distinct trees whose serial and fail-mode sliced
            receipts AGREE test for test, their sliced runs on at least
            STANDING_HOSTS distinct hosts (an uncured tree cannot AGREE: its
            sliced run carries the leak audit's errors, which diverge);
      (iv)  a red tree whose every real failure the sliced run caught too (a
            `catch` row, or an AGREE whose serial run was RED);
      (v)   the canary is still speaking: the freshest evidence any verdict
            of any source compared (an AGREE or a DIVERGED; an UNKNOWN
            compared nothing, and a finder run or a catch is no verdict),
            measured by the older of its two receipts' own stamps
            (`evidence_at`), never by when the verdict was written, is no
            older than `max_age_h()` hours and not ahead of this host's
            clock. A verdict naming no evidence stamp refreshes nothing.
    A DIVERGED restarts all of it, so one recorded after the last AGREE
    leaves nothing standing; an UNKNOWN compared nothing and neither counts
    nor restarts it; a FLAKE-EXPLAINED compared tests blame already recorded
    as flakes of that tree, so it neither restarts the count nor adds a tree;
    a tree compared twice counts once.

    READ WHOLE OR NOT AT ALL. A record that cannot be read, or holds one line
    that is not a verdict, a finder run or a catch, stands for nothing: the
    reader cannot tell what the missing line said, and it may be the
    DIVERGED."""
    path = history_path(global_dir)
    out = {"finder": None, "interaction": None, "agree": 0, "trees": [],
           "hosts": [], "red": [], "needed": STANDING_AGREE,
           "hosts_needed": STANDING_HOSTS, "last_diverged": None,
           "newest": None, "max_age_h": max_age_h(), "met": False,
           "path": path}
    rows, unavailable = eventledger.checked_events(path, strict=True)
    bad = next((i for i, row in enumerate(rows) if not _well_formed(row)),
               None)
    if unavailable or bad is not None:
        out["why"] = ("the canary record %s cannot be read whole (%s), so it "
                      "stands for nothing until it is repaired" % (
                          path, unavailable or "entry %d is not a verdict, a "
                          "finder run or a catch" % (bad + 1)))
        return out
    verdicts = [(i, row) for i, row in enumerate(rows)
                if row.get("event") not in (FINDER_ROW_EVENT, CATCH_EVENT)]
    cut = max((i for i, row in verdicts if row["verdict"] == DIVERGED),
              default=-1)
    if cut >= 0:
        out["last_diverged"] = {k: rows[cut].get(k) for k in (
            "at", "tree", "serial", "sliced", "source", "reason")}
    compared = [row for _i, row in verdicts
                if row["verdict"] in (AGREE, DIVERGED)]
    stamped = [row for row in compared
               if _epoch(row.get("evidence_at")) is not None]
    newest = max(stamped, key=lambda row: _epoch(row["evidence_at"]),
                 default=compared[-1] if compared else None)
    if newest is not None:
        out["newest"] = dict({k: newest.get(k) for k in (
            "at", "evidence_at", "tree", "source", "verdict")},
            age_h=_age_h(newest.get("evidence_at")))
    after = rows[cut + 1:]
    for scope, key in ((EACH, "finder"), (WHOLE, "interaction")):
        newest = next((row for row in reversed(after)
                       if row.get("event") == FINDER_ROW_EVENT
                       and row["scope"] == scope), None)
        out[key] = None if newest is None else {k: newest.get(k) for k in (
            "at", "tree", "host", "clean", "leaking", "failing", "broken")}
    trees, hosts, red = [], [], []
    for row in after:
        if row.get("event") == CATCH_EVENT:
            if row["caught"] and row["tree"] not in red:
                red.append(row["tree"])
            continue
        if row.get("event") == FINDER_ROW_EVENT \
                or row["verdict"] != AGREE or row.get("leak_mode") != "fail":
            continue
        if row["tree"] not in trees:
            trees.append(row["tree"])
        node = (row.get("hosts") or [None, None])[1]
        if node and node not in hosts:
            hosts.append(node)
        if row["red"] and row["tree"] not in red:
            red.append(row["tree"])
    out.update(agree=len(trees), trees=trees, hosts=hosts, red=red)
    since = (" since the DIVERGED recorded at %s on tree %s (serial %s, "
             "sliced %s)" % (out["last_diverged"]["at"],
                             str(out["last_diverged"]["tree"])[:12],
                             out["last_diverged"]["serial"],
                             out["last_diverged"]["sliced"])
             if out["last_diverged"] else "")
    missing = []
    for key, what in (("finder", "one-pass finder run (every module alone "
                                 "in a fresh process)"),
                      ("interaction", "report-mode sliced whole suite")):
        got = out[key]
        if got is None:
            missing.append("(%s) no %s is recorded%s: `helm gate canary "
                           "finder --repo <checkout>` records one"
                           % ("i" if key == "finder" else "ii", what, since))
        elif not got["clean"]:
            named = sorted(set(got.get("leaking") or {})
                           | set(got.get("failing") or {})
                           | set(got.get("broken") or {}))
            missing.append("(%s) the newest %s, on tree %s on %s, is not "
                           "clean: %s" % (
                               "i" if key == "finder" else "ii", what,
                               str(got["tree"])[:12], got["host"],
                               ", ".join(str(n) for n in named[:8])
                               or "its run was unreadable"))
    if len(trees) < STANDING_AGREE or len(hosts) < STANDING_HOSTS:
        missing.append(
            "(iii) it holds %d tree(s) whose serial and fail-mode sliced "
            "receipts AGREE test for test%s, sliced on %d host(s) (%s), and "
            "a sliced land needs %d trees on %d hosts: record more with "
            "`helm gate canary compare <serial-id> <sliced-id>`" % (
                len(trees), since, len(hosts), ", ".join(hosts) or "none",
                STANDING_AGREE, STANDING_HOSTS))
    if not red:
        missing.append(
            "(iv) no red tree is recorded whose real failures the sliced run "
            "caught too: `helm gate canary catch <serial-id> <sliced-id>` on "
            "a tree whose suite failed on a real test")
    silence = _silence(out["newest"], out["max_age_h"])
    if silence:
        missing.append(silence)
    out["met"] = not missing
    out["why"] = None if out["met"] else "the canary record %s: %s" % (
        path, "; ".join(missing))
    return out



def _silence(newest, bound):
    """Why the record is too old to stand for slices, or None. (v)."""
    fix = ("only newly minted receipts refresh it: the nightly `helm gate "
           "canary run` (timer %s) gates trunk's tip afresh, and a `compare` "
           "counts only receipts minted within the bound" % TIMER_NAME)
    if newest is None:
        return ("(v) the canary has recorded no verdict that compared a "
                "serial and a sliced run: %s" % fix)
    if _epoch(newest["evidence_at"]) is None:
        return ("(v) no verdict in the canary record names when the "
                "receipts it compared were minted (the newest, %s %s on tree "
                "%s, was recorded at %s), so the record cannot show the "
                "canary still produces evidence: %s" % (
                    newest["source"], newest["verdict"],
                    str(newest["tree"])[:12], newest["at"], fix))
    age = newest["age_h"]
    if 0 <= age <= bound:
        return None
    # A STAMP AHEAD OF THE CLOCK IS NO EVIDENCE OF SPEAKING: a negative age
    # sits inside every bound, so a verdict written while the clock ran
    # ahead would admit slices until the clock caught up with it.
    if age < 0:
        return ("(v) the canary's newest verdict that compared a serial and "
                "a sliced run (%s %s, tree %s) is stamped %s, %.1f h ahead of "
                "this host's clock (the older of its receipts' own stamps); "
                "evidence dated ahead of the clock cannot show the canary is "
                "still speaking: %s" % (
                    newest["source"], newest["verdict"],
                    str(newest["tree"])[:12], newest["evidence_at"], -age,
                    fix))
    return ("(v) the canary has been silent since %s: the freshest evidence "
            "any verdict compared (%s %s, tree %s, recorded at %s; the older "
            "of its two receipts' own stamps) is %.1f h old, over the %g h "
            "bound; %s" % (
                newest["evidence_at"], newest["source"], newest["verdict"],
                str(newest["tree"])[:12], newest["at"], age, bound, fix))


# -------------------------------------------------------------------- run

# WHERE THE CANARY RUNS IS CONFIG. The nightly serial suite must not hold a
# node a land is waiting on, so the operator names the host it runs on:
# HELM_GATE_CANARY_HOST pins both gates there (`fab gate --host HOST`, the
# launcher the canary always runs, pinned: it never spills to another node),
# and HELM_GATE_CANARY_LAUNCH, when set, names any other launcher whole. The
# timer's service reads both from `config_env_path()`, which `--install-timer
# --host HOST` writes, so no host is ever a literal in helm.
HOST_ENV = "HELM_GATE_CANARY_HOST"
LAUNCH_ENV = "HELM_GATE_CANARY_LAUNCH"
FINDER_LAUNCH_ENV = "HELM_GATE_CANARY_FINDER_LAUNCH"
DEFAULT_FINDER_LAUNCH = ("fab", "test", "--slots", "fit", "--cores", "16")
_HOST_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")


def config_env_path():
    base = os.environ.get("XDG_CONFIG_HOME") or \
        os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "helm", "gate-canary.env")


def launcher():
    """The argv prefix that runs one whole-suite gate and imports its
    receipt home: `<prefix> --repo ROOM --serial|--sliced`."""
    raw = os.environ.get(LAUNCH_ENV, "").strip()
    if raw:
        return shlex.split(raw)
    host = os.environ.get(HOST_ENV, "").strip()
    if host and _HOST_ATOM.match(host):
        return list(DEFAULT_LAUNCH) + ["--host", host]
    return list(DEFAULT_LAUNCH)


def finder_launcher():
    """The argv prefix that runs one command on a build node against a
    room: `<prefix> --repo ROOM -- <command>`."""
    raw = os.environ.get(FINDER_LAUNCH_ENV, "").strip()
    return shlex.split(raw) if raw else list(DEFAULT_FINDER_LAUNCH)


def _newest(rows, tree, sliced):
    """The newest whole-suite receipt of `tree` of the kind, or None."""
    hits = [row for row in rows
            if row.get("tree") == tree and row.get("suite") is True
            and (row.get("v") == gate.SLICE_VERSION) == sliced]
    return hits[-1] if hits else None


def _alert(text, ambient=True):
    try:
        return bool(chat.post(text, room=ROOM, who=WHO, sign=False,
                              ambient=ambient))
    except Exception:           # noqa: BLE001 -- the record stands regardless
        return False


def no_veto(detail, global_dir=None):
    """A DIVERGED that reached NEITHER the DISABLE marker NOR the canary
    record: nothing durable closes sliced land. The freshness bound
    (`standing` (v)) does not rescue it either, because every later AGREE
    keeps the record fresh; only a person who repairs the state and records
    the divergence again closes the door. So the words go to stderr and to
    the room as a row that is not ambient, whichever path found it (the
    nightly run, a `compare`, a shadow). -> the line"""
    line = ("NO DURABLE VETO: %s. Sliced lands stay admitted, and every later "
            "AGREE keeps the record fresh, until a person repairs the "
            "canary's state under %s and records this divergence again "
            "(`helm gate canary compare <serial-id> <sliced-id>` writes the "
            "marker and the record)" % (detail, state_dir(global_dir)))
    print("helm gate canary: " + line, file=sys.stderr)
    _alert("[gate canary] " + line, ambient=False)
    return line


def _with_flakes(result, serial, sliced, failures, why, root):
    """Re-judge a DIVERGED pair against the flake store, and only then.

    An AGREE or an UNKNOWN never reads the store. A store that cannot be read
    fails closed: the divergence stays DIVERGED and the reason names it."""
    if result["verdict"] != DIVERGED or root is None:
        return result
    from . import landwindow
    by_tree, unavailable = landwindow.read_flakes(root)
    return compare(serial, sliced, failures[0], failures[1], why,
                   flakes=unavailable or by_tree)


def judge(serial, sliced, global_dir=None, post=True, source=RUN, repo=None):
    """Compare two stored receipts; on DIVERGED write the marker and alert,
    on UNKNOWN alert, always record the verdict. -> result

    `repo` is the project whose flake store a divergence is read against.
    None leaves a divergence unexplained, which is how every caller that
    predates the store behaves."""
    rows, unavailable = eventledger.checked_events(gate.receipts_path(),
                                                   strict=True)
    if unavailable:
        result = {"verdict": UNKNOWN, "divergences": [],
                  "reason": "receipt ledger unavailable: %s" % unavailable}
    else:
        s_fail, s_why = failure_identities(serial, rows)
        l_fail, l_why = failure_identities(sliced, rows)
        result = compare(serial, sliced, s_fail, l_fail, s_why or l_why)
        result = _with_flakes(result, serial, sliced, (s_fail, l_fail),
                              s_why or l_why, repo)
    record(result, serial, sliced, global_dir, post, source)
    return result


def record(result, serial, sliced, global_dir=None, post=True, source=RUN):
    """Keep one verdict: the canary record always, the DISABLE marker on
    DIVERGED, and `last.json` (what the nightly run skips by) for the nightly
    run only, so a `compare` of an older tree never tells tonight's run that
    trunk was judged."""
    marker, marker_error = None, None
    if result["verdict"] == DIVERGED:
        try:
            marker = write_marker(result, serial or {}, sliced or {}, global_dir)
        except Exception as exc:       # noqa: BLE001 — the record is the veto too
            marker_error = "%s: %s" % (type(exc).__name__, exc)
    recorded = not (serial or sliced) or append_verdict(
        result, serial, sliced, source, global_dir)
    vetoed = True
    if result["verdict"] == DIVERGED and marker_error:
        if recorded:
            marker = ("the DIVERGED recorded in %s (the marker write failed: "
                      "%s)" % (history_path(global_dir), marker_error))
            print("helm gate canary: the DISABLE marker could not be written "
                  "(%s); the DIVERGED is recorded in %s, which closes sliced "
                  "land" % (marker_error, history_path(global_dir)),
                  file=sys.stderr)
        else:
            vetoed = False
            marker = no_veto(
                "the DISABLE marker could not be written (%s) and the "
                "DIVERGED on tree %s (serial %s, sliced %s: %s) could not be "
                "recorded in %s" % (
                    marker_error, str((serial or {}).get("tree"))[:12],
                    (serial or {}).get("id"), (sliced or {}).get("id"),
                    result["reason"], history_path(global_dir)), global_dir)
    if source != RUN:
        return marker
    if post and result["verdict"] == DIVERGED and vetoed:
        shown = "; ".join("%s (serial %s, sliced %s)" % (
            d["test"], d["serial"], d["sliced"])
            for d in result["divergences"][:5])
        closed = ("Sliced-at-land is closed by %s; no marker stands, and "
                  "the record's DIVERGED holds until the rule stands again "
                  "(`helm gate canary` reads it)" % marker) if marker_error \
            else ("Sliced-at-land is DISABLED while %s stands (`helm gate "
                  "canary` reads it, `helm gate canary clear --reason ...` "
                  "archives it once understood)" % marker)
        _alert("[gate canary] DIVERGED on trunk tree %s: serial %s and sliced "
               "%s disagree — %s. %s. %s." % (
                   str((serial or {}).get("tree"))[:12], serial.get("id"),
                   sliced.get("id"), result["reason"], shown, closed))
    elif post and result["verdict"] == UNKNOWN:
        _alert("[gate canary] UNKNOWN: tonight's serial/sliced comparison "
               "measured nothing — %s. No marker was written; the last "
               "verdict stands." % result["reason"])
    # AFTER THE ALERT, AND NEVER RAISING: last.json only tells tomorrow's run
    # this tree was judged, so a write that fails costs a second judgement,
    # never the alert or the veto above it.
    try:
        pk.write_json(last_path(global_dir), {
            "at": time.strftime(STAMP, time.gmtime()),
            "verdict": result["verdict"], "reason": result["reason"],
            "tree": (serial or sliced or {}).get("tree"),
            "serial": (serial or {}).get("id"),
            "sliced": (sliced or {}).get("id"),
            "divergences": result["divergences"][:_SHOWN]})
    except OSError as exc:
        print("helm gate canary: could not write %s (%s: %s); the next run "
              "judges this tree again" % (last_path(global_dir),
                                          type(exc).__name__, exc),
              file=sys.stderr)
    return marker


def run(repo=None, global_dir=None, launch=None, runner=None):
    """Judge trunk's tip, gating it first for whichever kind it lacks.
    -> result

    ONE RECEIPT OF EACH KIND PER TREE. The one-suite-per-tree door refuses a
    second whole suite on a tree, so the serial half is usually the receipt
    trunk's own train gate minted on these very bytes, and only the sliced
    half is run; a kind the tree already holds is never run again. Each run
    names its mode (`--serial` / `--sliced`: a peek room's no-flag default is
    slices) and carries CANARY_ENV, which the door admits for the kind the
    tree's last receipt is not. A tree already judged is not judged twice.

    `runner(argv, log, env)` returns the launcher's exit code; tests plant
    it."""
    from . import work
    from .work import _gc
    root = work.find_root(os.path.realpath(repo or os.getcwd())) \
        or os.path.realpath(repo or os.getcwd())
    backend = vcs.backend(root)
    ok, why = _gc.refresh_trunk(root)
    trunk = backend.trunk_ref(root)
    sha = (backend.head_sha(root, ref=trunk) or "").strip() if ok else ""
    rc, tree, _err = backend.text(root, "rev-parse", sha + "^{tree}") \
        if sha else (1, "", "")
    tree = (tree or "").strip() if rc == 0 else ""
    if not tree:
        result = {"verdict": UNKNOWN, "divergences": [],
                  "reason": "cannot resolve trunk %s: %s" % (trunk, why)}
        record(result, None, None, global_dir)
        return result
    last = pk.read_json(last_path(global_dir), default=None)
    if isinstance(last, dict) and last.get("tree") == tree \
            and last.get("verdict") in (AGREE, DIVERGED):
        return {"verdict": SKIPPED, "divergences": [], "reason": (
            "trunk tree %s was already judged %s at %s" % (
                tree[:12], last["verdict"], last.get("at")))}
    held = _held(tree)
    if held is None:
        result = {"verdict": UNKNOWN, "divergences": [],
                  "reason": "the receipt ledger could not be read"}
        record(result, None, None, global_dir)
        return result
    missing = [kind for kind in (gate.SERIAL, gate.SLICED)
               if held[kind] is None]
    codes = {}
    log = os.path.join(state_dir(global_dir), LOG_NAME)
    if missing:
        rc, peeked = work.peek(root, sha)
        if rc != 0:
            result = {"verdict": UNKNOWN, "divergences": [],
                      "reason": peeked.get("error") or "peek refused"}
            record(result, None, None, global_dir)
            return result
        room = peeked["path"]
        prefix = list(launch or launcher())
        runner = runner or _launch
        os.makedirs(os.path.dirname(log), exist_ok=True)
        env = dict(os.environ, **{gate.CANARY_ENV: "1"})
        try:
            for kind in missing:
                codes[kind] = runner(prefix + ["--repo", room, "--" + kind],
                                     log, env)
        finally:
            work.peek_drop(root, room)
        held = _held(tree) or {gate.SERIAL: None, gate.SLICED: None}
    serial, sliced = held[gate.SERIAL], held[gate.SLICED]
    if serial is None or sliced is None:
        result = {"verdict": UNKNOWN, "divergences": [], "reason": (
            "no %s receipt of trunk tree %s came home (launcher exit codes "
            "%s; log %s)" % (" or ".join(
                k for k, row in ((gate.SERIAL, serial), (gate.SLICED, sliced))
                if row is None), tree[:12], codes, log))}
        record(result, serial, sliced, global_dir)
        return result
    return judge(serial, sliced, global_dir, repo=root)


def _tip(root, ref=None):
    """(sha, tree, why) of `ref`, else of trunk's tip after a refresh."""
    from .work import _gc
    backend = vcs.backend(root)
    if ref:
        ok, why = True, None
        rc, sha, _err = backend.text(root, "rev-parse", "--verify", "--quiet",
                                     ref + "^{commit}")
        sha = (sha or "").strip() if rc == 0 else ""
        why = None if sha else "%s names no commit here" % ref
    else:
        ok, why = _gc.refresh_trunk(root)
        trunk = backend.trunk_ref(root)
        sha = (backend.head_sha(root, ref=trunk) or "").strip() if ok else ""
        why = None if sha else "cannot resolve trunk %s: %s" % (trunk, why)
    if not sha:
        return None, None, why
    rc, tree, _err = backend.text(root, "rev-parse", sha + "^{tree}")
    tree = (tree or "").strip() if rc == 0 else ""
    return (sha, tree, None) if tree else (None, None,
                                           "%s names no tree" % sha[:12])


def finder(repo=None, ref=None, global_dir=None, runner=None):
    """Run the one-pass finder (`gateslice.py --finder`) on one tree through
    the finder launcher and record its two measurements. -> (rc, line)

    The tree is `ref`'s, else trunk's tip. A launch that brings back no
    finder result records NOTHING: an absent measurement is never a clean
    one, and the door then still lacks it."""
    from . import work
    root = work.find_root(os.path.realpath(repo or os.getcwd())) \
        or os.path.realpath(repo or os.getcwd())
    sha, tree, why = _tip(root, ref)
    if why:
        return 3, "helm gate canary finder: %s" % why
    rc, peeked = work.peek(root, sha)
    if rc != 0:
        return 3, "helm gate canary finder: %s" % (peeked.get("error")
                                                   or "peek refused")
    room = peeked["path"]
    log = os.path.join(state_dir(global_dir), "last-finder.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    argv = finder_launcher() + ["--repo", room, "--", "python3",
                                gate.SLICE_RUNNER,
                                gateslice.FINDER_FLAG]
    try:
        with open(log, "w", encoding="utf-8"):
            pass
        code = (runner or _launch)(argv, log, dict(os.environ))
    finally:
        work.peek_drop(root, room)
    result = _finder_result(log)
    if result is None:
        return 3, ("helm gate canary finder: the launch (exit %s) brought "
                   "back no finder result; nothing was recorded (log %s)"
                   % (code, log))
    if not finder_rows(result, tree, global_dir):
        return 3, ("helm gate canary finder: the result could not be "
                   "recorded in %s" % history_path(global_dir))
    parts = []
    for scope in (EACH, WHOLE):
        part = result.get(scope) or {}
        named = sorted(set(part.get("leaking") or {})
                       | set(part.get("broken") or {})
                       | set(part.get("failing") or {}
                             if isinstance(part.get("failing"), dict)
                             else part.get("failing") or []))
        parts.append("%s %s%s" % (scope, "CLEAN" if part.get("clean")
                                   else "NOT CLEAN",
                                   ": " + ", ".join(named[:8]) if named
                                   else ""))
    clean = all((result.get(scope) or {}).get("clean") for scope in
                (EACH, WHOLE))
    return (0 if clean else 1), ("helm gate canary finder: tree %s on %s — "
                                 "%s" % (tree[:12], result.get("host"),
                                         "; ".join(parts)))


def _finder_result(log):
    """The last finder JSON line in the launch log, or None."""
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == \
                gateslice.FINDER_EVENT and isinstance(row.get("each"), dict) \
                and isinstance(row.get("whole"), dict):
            return row
    return None


def catch_rows(serial, sliced, global_dir=None):
    """Judge and record whether `sliced` caught every real failure `serial`
    caught. -> (result or None, why)"""
    rows, unavailable = eventledger.checked_events(gate.receipts_path(),
                                                   strict=True)
    if unavailable:
        return None, "receipt ledger unavailable: %s" % unavailable
    s_fail, s_why = failure_identities(serial, rows)
    l_fail, l_why = failure_identities(sliced, rows)
    if s_why or l_why:
        return None, s_why or l_why
    if serial.get("v") == gate.SLICE_VERSION \
            or sliced.get("v") != gate.SLICE_VERSION:
        return None, "the pair is not a serial and a sliced receipt"
    result = catch(serial, sliced, s_fail, l_fail)
    ok = _append_record(dict(
        result, v=HISTORY_VERSION, event=CATCH_EVENT,
        id=os.urandom(16).hex(),
        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        tree=serial.get("tree"), serial=serial.get("id"),
        sliced=sliced.get("id"), hosts=[_node(serial), _node(sliced)]),
        global_dir)
    return result, None if ok else "the catch could not be recorded"


def _held(tree):
    """{SERIAL: row|None, SLICED: row|None} this ledger holds for `tree`, or
    None when the ledger cannot be read."""
    rows, unavailable, _skipped = gate.receipts()
    if unavailable:
        return None
    return {gate.SERIAL: _newest(rows, tree, False),
            gate.SLICED: _newest(rows, tree, True)}


def _launch(argv, log, env):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n$ %s\n" % " ".join(shlex.quote(a) for a in argv))
        fh.flush()
        try:
            return subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=env,
                                  timeout=LAUNCH_TIMEOUT_S).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            fh.write("canary launch failed: %s\n" % exc)
            return None


# ------------------------------------------------------------------ timer

_SERVICE = """[Unit]
Description=helm gate canary — does a sliced run still agree with serial?

[Service]
WorkingDirectory=%(cwd)s
Type=oneshot
# The host the suites run on is config (HELM_GATE_CANARY_HOST), never a
# literal here: `helm gate canary --install-timer --host HOST` writes it.
EnvironmentFile=-%(env)s
# One serial and one sliced whole suite of trunk's tip, compared test by test.
# A divergence writes the sliced-at-land DISABLE marker and posts to #helm.
ExecStart=%(helm)s gate canary run --repo %(cwd)s
Nice=15
"""

_TIMER = """[Unit]
Description=nightly helm gate canary (serial vs sliced on trunk's tip)

[Timer]
OnCalendar=*-*-* %(at)s
Persistent=true

[Install]
WantedBy=timers.target
"""

TIMER_AT = "03:30:00"


def timer_units(at=TIMER_AT, inputs=None):
    """(service_path, service_text, timer_path, timer_text), with the
    working directory derived from this checkout's shared root, never a
    literal (gc's timer is the precedent). `inputs` replaces per-install
    values (timerhealth.unit_values)."""
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    from . import timerhealth
    udir = timerhealth.user_unit_dir()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % timerhealth.unit_values(
                {"helm": helm_bin, "cwd": cwd, "env": config_env_path()},
                inputs),
            os.path.join(udir, TIMER_NAME),
            _TIMER % timerhealth.unit_values({"at": at}, inputs))


# THE SWITCH for a process that must not change this host's scheduler: the
# suite (tests/__init__ sets it), or a host that runs the canary from another
# scheduler. Off means no unit file is written and no systemctl runs.
TIMER_ENV = "HELM_GATE_CANARY_TIMER"
TIMER_OFF_VALUES = ("0", "off", "no", "false")


def timer_switched_off():
    """The HELM_GATE_CANARY_TIMER value when it turns the install off."""
    value = os.environ.get(TIMER_ENV)
    if value is not None and value.strip().lower() in TIMER_OFF_VALUES:
        return value
    return None


def timer_installed():
    """Is the timer's unit file on disk? Doctor asks it the way it asks the
    other cadences."""
    return os.path.exists(timer_units()[2])


def write_host(host):
    """Record the host the canary's suites run on. -> (ok, detail)"""
    if not _HOST_ATOM.match(str(host or "")):
        return False, "--host must be a plain host name, got %r" % host
    path = config_env_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, "%s=%s\n" % (HOST_ENV, host))
    except OSError as exc:
        return False, "cannot write %s: %s" % (path, exc)
    return True, "the canary's suites run on %s (%s)" % (host, path)


def ensure_timer():
    """(ok, detail) — install and enable the nightly cadence.

    IDEMPOTENT: unchanged unit files are not rewritten and systemd is not
    reloaded for them; `enable --now` of an enabled timer changes nothing.
    `helm work install-guard --apply` calls this with the rail, so re-running
    that install is the refresh path. `ok` is True (enabled), False (failed)
    or None (switched off by TIMER_ENV: nothing written, nothing run)."""
    import shutil
    from . import timerhealth
    off = timer_switched_off()
    if off is not None:
        return None, ("install skipped by %s=%s: no unit file written, no "
                      "systemctl run" % (TIMER_ENV, off))
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm gate canary run` "
                       "nightly from another scheduler")
    spath, service, tpath, timer = timer_units()
    error, unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (TIMER_NAME,), systemctl,
        subprocess, keep_unchanged=True, reload_unchanged=False)
    if error:
        return False, error
    return True, "%s nightly at %s (%s)" % (
        "already installed, unchanged," if unchanged else "installed",
        TIMER_AT, tpath)


# -------------------------------------------------------------------- cli

USAGE = ("usage: helm gate canary [status] | run [--repo PATH] | "
         "compare <serial-receipt> <sliced-receipt> | catch <serial-receipt> "
         "<sliced-receipt> | finder [--repo PATH] [--ref REF] | clear "
         "--reason TEXT | --install-timer [--host HOST]")


def _clean_word(run):
    return "none" if run is None else ("CLEAN" if run["clean"]
                                       else "NOT CLEAN")


def _print_result(result):
    print("helm gate canary: %s — %s" % (result["verdict"], result["reason"]))
    for d in (result["divergences"] + result.get("notes", []))[:_SHOWN]:
        print("  %-11s %s  serial=%s sliced=%s" % (
            d["kind"], d["test"], d["serial"], d["sliced"]))


def cmd(rest):
    rest = list(rest or ())
    if rest[:1] == ["--install-timer"] and (
            len(rest) == 1 or (len(rest) == 3 and rest[1] == "--host")):
        if len(rest) == 3:
            ok, detail = write_host(rest[2])
            print("helm gate canary: %s" % detail,
                  file=sys.stdout if ok else sys.stderr)
            if not ok:
                return 2
        ok, detail = ensure_timer()
        print("helm gate canary: %s" % detail,
              file=sys.stderr if ok is False else sys.stdout)
        return 1 if ok is False else 0
    if rest[:1] == ["finder"]:
        opts, bad = {}, None
        args = rest[1:]
        while args:
            if args[0] in ("--repo", "--ref") and len(args) > 1:
                opts[args[0]] = args[1]
                args = args[2:]
            else:
                bad, args = args[0], []
        if bad:
            print(USAGE, file=sys.stderr)
            return 2
        rc, line = finder(repo=opts.get("--repo"), ref=opts.get("--ref"))
        print(line, file=sys.stdout if rc in (0, 1) else sys.stderr)
        return rc
    if rest[:1] == ["catch"] and len(rest) == 3:
        rows = []
        for prefix in rest[1:]:
            row, err = gate.by_id(prefix)
            if err:
                print("helm gate canary: %s" % err, file=sys.stderr)
                return 2
            rows.append(row)
        result, why = catch_rows(rows[0], rows[1])
        if result is None:
            print("helm gate canary catch: %s" % why, file=sys.stderr)
            return 3
        print("helm gate canary catch: %s — %d real failure(s) serial caught"
              "%s" % ("CAUGHT" if result["caught"] else "NOT CAUGHT",
                      result["real_failures"],
                      "; missing in slices: " + ", ".join(result["missing"])
                      if result["missing"] else ""))
        return 0 if result["caught"] else 1
    sub = rest[0] if rest else "status"
    args = rest[1:]
    if sub == "status" and not args:
        why = gate.sliced_land_disabled()
        last = pk.read_json(last_path(), default=None)
        print("helm gate canary: sliced-at-land %s" % (
            "DISABLED — " + why if why else "not disabled by the canary"))
        if isinstance(last, dict):
            print("  last run %s: %s — %s" % (last.get("at"),
                                              last.get("verdict"),
                                              last.get("reason")))
        else:
            print("  no canary run recorded")
        held = standing()
        print("  record: finder %s, interaction %s, %d of %d agreeing "
              "tree(s) on %d of %d host(s), red caught: %s — the land door "
              "%s" % (
                  _clean_word(held["finder"]), _clean_word(
                      held["interaction"]), held["agree"], held["needed"],
                  len(held["hosts"]), held["hosts_needed"],
                  ", ".join(t[:12] for t in held["red"]) or "none",
                  "admits a sliced receipt" if held["met"] and not why
                  else "refuses a sliced receipt"))
        newest = held["newest"]
        age = (newest or {}).get("age_h")
        print("  freshness: %s; the bound is %g h (%s may only tighten it)"
              % ("no verdict has compared a serial and a sliced run"
                 if not newest else
                 "no compared verdict names when its receipts were minted "
                 "(the newest was recorded at %s)" % newest["at"]
                 if age is None else
                 "the freshest compared evidence was minted at %s (%s, %s), "
                 "%s; recorded at %s" % (
                     newest["evidence_at"], newest["source"],
                     newest["verdict"], "%.1f h ago" % age if age >= 0
                     else "%.1f h AHEAD of this host's clock" % -age,
                     newest["at"]),
                 held["max_age_h"], MAX_AGE_ENV))
        if held["why"]:
            print("  " + held["why"])
        return 1 if why else 0
    if sub == "run" and (not args or (len(args) == 2
                                      and args[0] == "--repo")):
        result = run(repo=args[1] if args else None)
        _print_result(result)
        return {AGREE: 0, SKIPPED: 0, DIVERGED: 1,
                FLAKE_EXPLAINED: 0}.get(result["verdict"], 3)
    if sub == "compare" and len(args) == 2:
        rows = []
        for prefix in args:
            row, err = gate.by_id(prefix)
            if err:
                print("helm gate canary: %s" % err, file=sys.stderr)
                return 2
            rows.append(row)
        ledger, unavailable = eventledger.checked_events(gate.receipts_path(),
                                                         strict=True)
        if unavailable:
            print("helm gate canary: receipt ledger unavailable: %s"
                  % unavailable, file=sys.stderr)
            return 3
        (s_fail, s_why), (l_fail, l_why) = (failure_identities(r, ledger)
                                            for r in rows)
        result = compare(rows[0], rows[1], s_fail, l_fail, s_why or l_why)
        if result["verdict"] == DIVERGED:
            from . import work
            root = work.find_root(os.getcwd()) or os.getcwd()
            result = _with_flakes(
                result, rows[0], rows[1], (s_fail, l_fail), s_why or l_why,
                root)
        # RECORDED, like every verdict: the land door counts it, and a
        # DIVERGED writes the marker exactly as the nightly run's does.
        record(result, rows[0], rows[1], post=False, source=COMPARE)
        _print_result(result)
        return {AGREE: 0, DIVERGED: 1, FLAKE_EXPLAINED: 0}.get(
            result["verdict"], 3)
    if sub == "clear" and len(args) == 2 and args[0] == "--reason":
        rc, line = clear_marker(args[1])
        print(line, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    print(USAGE, file=sys.stderr)
    return 2
