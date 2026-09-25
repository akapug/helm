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
  DIVERGED   some test's outcome differs. The canary writes the DISABLE
             marker in the helm home and posts one alert to the helm room.
             `gate.sliced_land_disabled()` reads the marker, and a sliced
             receipt must not authorize a land while it stands.
  UNKNOWN    either run is unreadable, or they are not the same tree. No
             marker, because nothing was compared; the alert says so, because
             a canary that silently stops measuring is no canary.

THE MARKER IS DURABLE. A later AGREE does not remove it: a divergence that
comes and goes is exactly the kind a single green night must not wash out. It
is cleared by a person who read it (`helm gate canary clear --reason ...`),
and clearing archives it beside itself with the reason, never deletes it.

A slice-only failure is a divergence too, including the audit's own
`LeakAudit` errors: they fail no serial test, so the two runs disagree about
what is red, and an audit finding on trunk's tip means a mutation reached
trunk. The comparison names each kind.
"""
import collections
import json
import os
import shlex
import subprocess
import sys
import time

from . import chat, eventledger, gate, gateslice, home, pk, vcs

STATE_SUBDIR = os.path.join(".state", "gate-canary")
LAST_NAME = "last.json"
LOG_NAME = "last-launch.log"
DEFAULT_LAUNCH = ("fab", "gate")
LAUNCH_TIMEOUT_S = 3 * 3600
ROOM = "helm"
# A machine sender (helm/machine_senders.py): its rows are pulled, never pushed.
WHO = "gate-canary"
TIMER_NAME = "helm-gate-canary.timer"
SERVICE_NAME = "helm-gate-canary.service"
AGREE, DIVERGED, UNKNOWN = "AGREE", "DIVERGED", "UNKNOWN"
# A night whose trunk tree was already judged: nothing is run or recorded.
SKIPPED = "SKIPPED"
MARKER_VERSION = 1
_SHOWN = 40


def state_dir(global_dir=None):
    return os.path.join(global_dir or home.global_dir(), STATE_SUBDIR)


def last_path(global_dir=None):
    return os.path.join(state_dir(global_dir), LAST_NAME)


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


def compare(serial, sliced, serial_failures, sliced_failures, why=None):
    """Judge one serial and one sliced receipt of the same tree.

    -> {"verdict", "reason", "divergences": [{"test", "serial", "sliced",
    "kind"}]}. `*_failures` are `failure_identities` counters, or None when
    that receipt's record is unreadable (UNKNOWN, for `why`)."""
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
    for field in ("ran", "skipped"):
        a, b = serial.get(field) or 0, sliced.get(field) or 0
        if a != b:
            divergences.append({"test": "<%s count>" % field,
                                "serial": str(a), "sliced": str(b),
                                "kind": "count"})
    if divergences:
        worst = sum(d["kind"] == "serial-only" for d in divergences)
        hosts = [((row.get("host") or {}).get("node") or "?")
                 for row in (serial, sliced)]
        return {"verdict": DIVERGED, "divergences": divergences,
                "reason": "%d outcome(s) differ%s%s" % (
                    len(divergences),
                    "; %d failure(s) serial saw and slices did not" % worst
                    if worst else "",
                    "; the runs were on different hosts (%s, %s)" % tuple(
                        hosts) if hosts[0] != hosts[1] else "")}
    return {"verdict": AGREE, "divergences": [],
            "reason": "the same %d failure(s), %s ran, %s skipped" % (
                sum(serial_failures.values()), serial.get("ran"),
                serial.get("skipped") or 0)}


# ------------------------------------------------------------------- marker

def write_marker(result, serial, sliced, global_dir=None):
    """The DISABLE marker, written whole or not at all. -> path

    A marker that already stands keeps its `since`: the newest divergence
    replaces the evidence, never the moment sliced-at-land was disabled."""
    path = gate.sliced_land_marker_path(global_dir)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prior = pk.read_json(path, default=None)
    pk.write_json(path, {
        "v": MARKER_VERSION,
        "since": prior.get("since") or now if isinstance(prior, dict) else now,
        "last": now,
        "tree": serial.get("tree"), "head": serial.get("head"),
        "serial": serial.get("id"), "sliced": sliced.get("id"),
        "reason": result["reason"],
        "divergences": result["divergences"][:_SHOWN],
        "divergence_total": len(result["divergences"]),
    })
    return path


def clear_marker(reason, global_dir=None):
    """Archive the marker beside itself with who-cleared-it-why. -> (rc, line)

    Never a deletion: the archived file keeps the divergence that disabled
    sliced-at-land and the reason it was judged cleared."""
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
        marker["cleared"] = {"at": stamp, "reason": reason}
        pk.write_json(archive, marker)
        os.unlink(path)
    except OSError as exc:
        return 1, "helm gate canary: cannot clear %s — %s" % (path, exc)
    return 0, "helm gate canary: marker cleared, archived at %s" % archive


# -------------------------------------------------------------------- run

def launcher():
    """The argv prefix that runs one whole-suite gate and imports its
    receipt home: `<prefix> --repo ROOM --serial|--sliced`."""
    raw = os.environ.get("HELM_GATE_CANARY_LAUNCH", "").strip()
    return shlex.split(raw) if raw else list(DEFAULT_LAUNCH)


def _newest(rows, tree, sliced):
    """The newest whole-suite receipt of `tree` of the kind, or None."""
    hits = [row for row in rows
            if row.get("tree") == tree and row.get("suite") is True
            and (row.get("v") == gate.SLICE_VERSION) == sliced]
    return hits[-1] if hits else None


def _alert(text):
    try:
        return bool(chat.post(text, room=ROOM, who=WHO, sign=False,
                              ambient=True))
    except Exception:           # noqa: BLE001 -- the record stands regardless
        return False


def judge(serial, sliced, global_dir=None, post=True):
    """Compare two stored receipts; on DIVERGED write the marker and alert,
    on UNKNOWN alert, always record the run. -> result"""
    rows, unavailable = eventledger.checked_events(gate.receipts_path(),
                                                   strict=True)
    if unavailable:
        result = {"verdict": UNKNOWN, "divergences": [],
                  "reason": "receipt ledger unavailable: %s" % unavailable}
    else:
        s_fail, s_why = failure_identities(serial, rows)
        l_fail, l_why = failure_identities(sliced, rows)
        result = compare(serial, sliced, s_fail, l_fail, s_why or l_why)
    record(result, serial, sliced, global_dir, post)
    return result


def record(result, serial, sliced, global_dir=None, post=True):
    marker = None
    if result["verdict"] == DIVERGED:
        marker = write_marker(result, serial or {}, sliced or {}, global_dir)
    pk.write_json(last_path(global_dir), {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": result["verdict"], "reason": result["reason"],
        "tree": (serial or sliced or {}).get("tree"),
        "serial": (serial or {}).get("id"), "sliced": (sliced or {}).get("id"),
        "divergences": result["divergences"][:_SHOWN]})
    if post and result["verdict"] == DIVERGED:
        shown = "; ".join("%s (serial %s, sliced %s)" % (
            d["test"], d["serial"], d["sliced"])
            for d in result["divergences"][:5])
        _alert("[gate canary] DIVERGED on trunk tree %s: serial %s and sliced "
               "%s disagree — %s. %s. Sliced-at-land is DISABLED while %s "
               "stands (`helm gate canary` reads it, `helm gate canary clear "
               "--reason ...` archives it once understood)." % (
                   str((serial or {}).get("tree"))[:12], serial.get("id"),
                   sliced.get("id"), result["reason"], shown, marker))
    elif post and result["verdict"] == UNKNOWN:
        _alert("[gate canary] UNKNOWN: tonight's serial/sliced comparison "
               "measured nothing — %s. No marker was written; the last "
               "verdict stands." % result["reason"])
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
    return judge(serial, sliced, global_dir)


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


def timer_units(at=TIMER_AT):
    """(service_path, service_text, timer_path, timer_text), with the
    working directory derived from this checkout's shared root, never a
    literal (gc's timer is the precedent)."""
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, TIMER_NAME), _TIMER % {"at": at})


def ensure_timer():
    """(ok, detail) — install and enable the nightly cadence."""
    import shutil
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm gate canary run` "
                       "nightly from another scheduler")
    spath, service, tpath, timer = timer_units()
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as exc:
        return False, "unit write failed: %s" % exc
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", TIMER_NAME]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "nightly at %s (%s)" % (TIMER_AT, tpath)


# -------------------------------------------------------------------- cli

USAGE = ("usage: helm gate canary [status] | run [--repo PATH] | "
         "compare <serial-receipt> <sliced-receipt> | clear --reason TEXT | "
         "--install-timer")


def _print_result(result):
    print("helm gate canary: %s — %s" % (result["verdict"], result["reason"]))
    for d in result["divergences"][:_SHOWN]:
        print("  %-11s %s  serial=%s sliced=%s" % (
            d["kind"], d["test"], d["serial"], d["sliced"]))


def cmd(rest):
    rest = list(rest or ())
    if rest == ["--install-timer"]:
        ok, detail = ensure_timer()
        print("helm gate canary: %s" % detail,
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
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
        return 1 if why else 0
    if sub == "run" and (not args or (len(args) == 2
                                      and args[0] == "--repo")):
        result = run(repo=args[1] if args else None)
        _print_result(result)
        return {AGREE: 0, SKIPPED: 0, DIVERGED: 1}.get(result["verdict"], 3)
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
        _print_result(result)
        return {AGREE: 0, DIVERGED: 1}.get(result["verdict"], 3)
    if sub == "clear" and len(args) == 2 and args[0] == "--reason":
        rc, line = clear_marker(args[1])
        print(line, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    print(USAGE, file=sys.stderr)
    return 2
