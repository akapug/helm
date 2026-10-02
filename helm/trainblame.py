"""A RED TRAIN NAMES ITS OWN CULPRIT: `helm train blame` reads a train's red
gate, finds the car that broke it, and ejects that car, never silently.

THE MEASURED DEFECT. The first sliced land gate (train283) answered in 236 s,
where the serial suite takes about 1,125 s. So the slowest part of a land is
no longer the gate: it is what happens when a train goes RED. The integrator
read the receipt, guessed which car broke it, composed the train again by hand
and gated it again, and each of those steps waited on one session's attention.

THE RULINGS, accepted by the integrator and binding here:

  R2  Blame by diff only when it names exactly one car; otherwise bisect the
      train's prefixes across hosts. Ejection is never silent: a DM with the
      failing test, the log and the car it clashed with, plus a task comment.
  R1  One gate per HOST is the landing window's default (helm/gatehost.py
      `window_key`), so the train composed again without its culprit gates on
      a second host while nothing else waits on the first.

HOW IT FINDS THE CULPRIT, in order:

  READ THE ROOM. A train room's FIRST-PARENT chain is exactly its prefixes:
  `helm train` merges each car with `merge --no-ff` and the message
  `<train>: merge lane <lane>`, so P0 is the trunk the train stands on and Pk
  is trunk plus cars 1..k. The room is asserted the way every merge asserts it
  (`landwindow.room_refusal`), and a head that is not a car merge (a
  composition cure on top) is refused by name: blame reads a room that is
  exactly trunk plus its cars.

  READ THE RED. The gate receipt for the room's head (or the one `--gate`
  names, which must have gated that head) gives the failing test ids, each
  placed in its test module and file by the room's own tree, the helm paths
  its traceback names, the gate's mode (serial, or the sliced kind), and the
  window's job log that bound it. A receipt that is green, has no readable
  verdict or names no failing test is refused by name.

  BLAME BY DIFF. A car is NAMED when what its merge added to the room (`git
  diff P(k-1) Pk`, never a merge-base diff of its lane: a lane that merged
  trunk back in is charged only with its own changes, task/4145) touches a
  failing test's file or a `helm/*.py` path that failure's traceback names
  (a frame, or a dotted `helm.module` in its message). The slice runner's
  leak verdict carries a frame of its own (`helm/gateslice.py`, line 1, in
  the leak test); that frame is the runner speaking for a unit, not a
  traceback, so it names nobody. Exactly one named car, with every car's
  diff readable, is the culprit and no prefix is bisected. Zero named, two
  or more, or a diff git could not give: bisect.

  TRUNK ON THE RED'S OWN HOST LICENSES EVERY EJECTION (task/4145). Before
  any car is ejected, by diff or by bisect, the failing modules run on P0,
  trunk alone, pinned to the host the red ran on (the window job log's node,
  else the receipt's; the audits' placement line for a red audit), through
  the same runner the prefixes use. A host that fails every tree reads as
  a car's fault to a diff, and auto-land's re-run of the red tests on that
  host repeats the fault rather than clearing it (train561: one host's
  CPython 3.14.7 failed 9 tests that passed on another at the same tree).
  Trunk RED there is TRUNK-RED, naming the host: nothing is ejected. A trunk
  run that cannot be made (no host recorded, the host excluded or busy, a run
  Fab placed or moved elsewhere or that could not be read) is UNKNOWN: nothing
  is ejected. Only a trunk pass there licenses the ejection. The bisect's own
  P0 run is placed on that host first, so it is the same run.

  BISECT THE PREFIXES. Only the failing modules run, through the gate's own
  runner in the gate's mode: the slice runner's `--modules` scope with the
  land gate's fail-mode leak audit for a sliced gate (the line `helm gate
  audits` prints), so a sliced-only failure reproduces, and one serial
  unittest process for a serial gate. Each prefix runs in its own detached
  worktree at its commit, minted with the integrator's sanction and removed
  after its run, as `fab test` pinned to one host by FAB_EXCLUDE_HOSTS: every
  other known host joins the caller's own exclusions, which are extended and
  never replaced. The hosts are gatehost's candidates less the excluded and
  the busy ones, one run per host at a time. The first round runs P0 and Pn;
  each round after it runs as many prefixes between the last green and the
  first red as there are hosts, evenly spread, so one host is a binary search
  and enough hosts answer in one round. RED means at least one of the red
  gate's own failing tests fails there (or its module will not import); a
  failure of any other test is reported beside it and decides nothing.

  THE VERDICTS, each one explicit:
    TRUNK-RED  P0, trunk alone, fails the failing modules (in the bisect,
               or on the red's own host before an ejection). Nothing is
               ejected; the failure is on trunk or its host, and a
               composition cure is owed (train283: leaked process state from
               modules on trunk) or the host is at fault (train561).
    FLAKE      Pn, the whole train, passes them on a re-run. Nothing is
               ejected; the re-gate line is printed.
    EJECT      The first red prefix Pk names car k. When k > 1 its tip is
               also run ALONE on trunk: green there makes it a CLASH, and the
               earlier cars whose diffs overlap car k's files, the failing
               test files or the traceback's paths are named beside it.
    UNKNOWN    A prefix the verdict turns on could not be read. Nothing is
               ejected, and the prefix and its reason are named.

EJECT (`--apply`). The train is composed again without the culprit through
`landwindow.compose_room`, the one compose path `helm train` uses, as the next
free name after the train's own (`train283` -> `train283b`), and its gate is
launched through `gatewindow.launch`. BEFORE it composes, the ejection is told:

  THE DM goes to the car's author (its land request's `author`, the dispatch
  row's `sender`) through `seats.dm`, with the failing tests, the window log,
  the receipt, blame's own run log, the evidence and the cars it clashed with.
  THE TASK COMMENT goes to the task proven by both the car's own lane and
  its chain (`lane_task` through `taskkey.car_key`). If they conflict or a
  record cannot be read, task UNKNOWN is said; no other task is commented.
  Either one failing REFUSES the ejection loudly, prints the undelivered text,
  and composes nothing. A lane that names no task gets no comment, and the
  output says so: helm has no note door for a verdicted dispatch row (the
  findings-note admits open and held rows only).
  THE RECORD then binds the ejection to the car's exact tip in the project's
  ejection store (`landwindow.record_ejection`), so the next `helm train`
  leaves that tip out until the lane re-tips or `helm train readmit` clears
  it; a record that cannot be written refuses and composes nothing.
  THE ROOM LINE is one line to room `helm`, posted after the launch.

A RED PRE-GATE AUDIT (task/3674). `helm train auto` runs the tree-wide audits
and the lane census on the composed room before any gate, and when they fail
it hands blame their log (`blame(audits=...)`, `audit_red`) in place of a
receipt. The log's unittest report is read by the gate's own parser, and each
failure placed as a gate's is, so blame by diff decides exactly as above.
Exactly one named car is ejected the same way, told first and recorded, and
the train composed again without it and gated. Anything else refuses and
ejects nothing: no car or two named (a red audit is NEVER bisected, since no
gate ran whose runner a prefix could repeat), and a log with no readable
failure list. The record names the log as `audits`, with no gate.

WHAT IS NOT HERE: invoking blame when a train goes red. `helm train auto`
(helm/autoland.py) does that, after it re-runs the failing modules alone on
the red gate's own host, so a host-confounded red is read as a flake before
any car is ejected.
"""
import binascii
import concurrent.futures
import io
import json
import os
import re
import shlex
import sys

from . import gate as gatemod
from . import gatehost, gatewindow, home, landwindow, rowworld, vcs
from .work import _lanes

PROG = "helm train blame"
USAGE = ("usage: helm train blame <train-room> [--gate gate:<id>] [--apply] "
         "[--json]")

TRUNK_RED = "TRUNK-RED"
FLAKE = "FLAKE"
EJECT = "EJECT"
UNKNOWN = "UNKNOWN"
GREEN = "GREEN"
RED = "RED"
BY_DIFF = "diff"
BY_BISECT = "bisect"

# The prefix rooms: <repo>-wt/blame/<train>-<probe>-<nonce>, beside the
# compose rooms, each removed after its run.
BOX = "blame"
# The voice every telling leg speaks in, and the room the one line goes to.
WHO = "train-blame"
ROOM = "helm"
# How far down the room's first-parent chain the car merges are read.
CHAIN_SCAN = 400
# A focused run of a few modules that takes an hour is a run nobody watches.
RUN_TIMEOUT_S = 3600
# THE MARKERS a prefix run prints around its runner's output, with a nonce, so
# what Fab prints about its placement and its exit never reads as the suite's.
MARK = "helm-train-blame"
# Where blame's own run log is kept, under the helm home's _global.
EVIDENCE_SUBDIR = os.path.join(".state", "train-blame")
# Failing test ids a DM names before it counts the rest.
SHOWN = 5

# A CAR MERGE'S SUBJECT, as `helm train` writes it (`<train>: merge lane
# <lane>`) and as the integrator writes it by hand, with a description after
# the lane (`train283: merge lane cursor-bridge-resume-refusal: the vetted
# ...`, `train281: merge lane sliced-land-shadow-w4 (W4, ...)`): every train
# merge on helm's trunk so far is the hand-written kind.
_MERGE = re.compile(r"\A(?P<train>[A-Za-z0-9][A-Za-z0-9._-]{0,63}): merge "
                    r"lane (?P<lane>[^\s:]+)(?:[:\s].*)?\Z")
_CAR = re.compile(r"land request (?P<id>\S+), (?P<basis>source-clean held|"
                  r"landed before review, unread|reviewed) tip "
                  r"(?P<tip>[0-9a-f]{40})")
# THE BASIS each merge body's words name (landwindow._BODY_WORD).
_BASIS = {"source-clean held": "source-clean",
          "landed before review, unread": "land-first"}
_TOKEN = re.compile(r"\A(?:gate:)?([0-9a-f]{4,64})\Z")
_LOADER = "unittest.loader._FailedTest."
_FRAME = re.compile(r'File "([^"]+)"')
_PATHISH = re.compile(r"[\w./-]*helm/[\w./-]+\.py")
_DOTTED = re.compile(r"\bhelm(?:\.[A-Za-z_]\w*)+")
# THE SLICE RUNNER'S LEAK VERDICT WEARS A FRAME: `File "helm/gateslice.py",
# line 1, in test_leaves_no_process_state`. It is the runner speaking for the
# unit it audited, so it names no culprit; the leak's own module is in the
# message beside it, which the dotted reading takes.
_LEAK_FRAME = re.compile(r'File "[^"]*helm/gateslice\.py", line 1, in '
                         r'test_leaves_no_process_state')
# THE SLICE RUNNER'S TRAILER: its summary line and one `gateslice leak:` line
# per leaking module, printed after the last failure block, so they ride that
# block and the receipt's condensed diagnosis of it. They belong to no one
# failure, and each failure's text is cut where they begin.
_TRAILER = re.compile(r"(?:^|\| )gateslice(?: leak)?: ", re.M)
_BLOCK_HEAD = re.compile(r"^(?:FAIL|ERROR): \S+ \((?P<id>[\w.<>]+)\)", re.M)
_BLOCK_RULE = re.compile(r"^={20,}$", re.M)
# unittest's summary line, the one a run's report ends on (`audit_red`)
_SUMMARY = re.compile(r"^(?:OK(?: \(.*\))?|FAILED \(.*\))[ \t]*$", re.M)
# THE HOSTS FAB'S OWN LINES SAY A RUN WENT TO (`placements`): the placement
# line, the line a re-placed job's MOVE prints, and the closing line, which
# names the node the job ended on. Each is anchored at a line's start.
_WENT = re.compile(r"^fab: (?:role=\S+ -> (\S+) \(|id=\S+\s+MOVED \S+ -> "
                   r"(\S+)\s|exit=\S+\s+LOG: ([^:\s]+):)")
# the command line `autoland.Ops.audits` writes above the audits' run
_RAN = re.compile(r"^\$ (.*)$", re.M)


def _env():
    return landwindow._env()


def _short(sha):
    return (sha or "?")[:12]


def _first(text):
    return landwindow._first(text)


def _nonce():
    return binascii.hexlify(os.urandom(6)).decode("ascii")


# -- the room ----------------------------------------------------------------

def read_room(room):
    """(train, refusal) — the red train exactly as its room records it.

    `train` carries `room`, `root`, `identity`, `train` (the name its merges
    carry), `trunk` (P0), `head`, `prefixes` (P0..Pn) and `cars`, each car
    with `n` (1-based), `id` (its land request), `lane`, `tip` (the merge's
    second parent, the reviewed sha), `merge` (Pn's commit for it) and
    `basis`; `files` is the head's tree listing, which places each failing
    test in its module."""
    room = os.path.realpath(room)
    if not os.path.isdir(room):
        return None, "no such room: %s" % room
    root = _lanes.find_root(room)
    if not root:
        return None, "%s is not inside a git repository" % room
    identity, err = rowworld.repo_identity(root)
    if err:
        return None, err
    be = vcs.backend(root)
    why = landwindow.room_refusal(be, room, identity)
    if why:
        return None, why
    rc, out, err = be.text(room, "log", "--first-parent", "-z",
                           "--format=%H%x1f%P%x1f%s%x1f%b",
                           "-n", str(CHAIN_SCAN), "HEAD", env=_env())
    if rc != 0:
        return None, "the room's history is unreadable (%s)" % _first(err)
    name, cars, trunk = None, [], None
    for record in out.split("\0"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) < 3 or not fields[0].strip():
            continue
        sha, parents, subject = fields[0].strip(), fields[1].split(), fields[2]
        hit = _MERGE.match(subject)
        if hit and len(parents) == 2 and name in (None, hit.group("train")):
            name = hit.group("train")
            said = _CAR.search(fields[3] if len(fields) > 3 else "")
            cars.append({
                "id": said.group("id") if said else "?",
                "lane": hit.group("lane"), "tip": parents[1], "merge": sha,
                "basis": _BASIS.get(said.group("basis") if said else None,
                                    "approved")})
            continue
        if not cars:
            return None, ("the room's head %s is not a `<train>: merge lane` "
                          "merge (%s): it carries commits past its last car, "
                          "and blame reads a room that is exactly trunk plus "
                          "its cars" % (_short(sha), subject))
        trunk = sha
        break
    if trunk is None:
        return None, ("no trunk commit stands below the room's car merges "
                      "within %d commits" % CHAIN_SCAN)
    cars.reverse()
    for n, car in enumerate(cars, 1):
        car["n"] = n
    rc, listing, err = be.text(room, "ls-tree", "-r", "-z", "--name-only",
                               "HEAD", env=_env())
    if rc != 0:
        return None, "the room's tree is unreadable (%s)" % _first(err)
    return {"room": room, "root": root, "identity": identity, "train": name,
            "trunk": trunk, "head": cars[-1]["merge"],
            "prefixes": [trunk] + [car["merge"] for car in cars],
            "cars": cars, "files": {f for f in listing.split("\0") if f}}, None


# -- the red -----------------------------------------------------------------

def module_of(test, files):
    """(module, file) of one failing test id in a tree listing `files`, or
    (None, None). The longest dotted prefix that is a file names it; a loader
    failure's id carries the module it could not import."""
    name = test[len(_LOADER):] if test.startswith(_LOADER) else test
    parts = name.split(".")
    for k in range(len(parts), 0, -1):
        path = "/".join(parts[:k]) + ".py"
        if path in files:
            return ".".join(parts[:k]), path
    return None, None


def _failure_text(failure, row):
    """The receipt's diagnosis of one failure, plus its whole block from the
    output tail the receipt kept, when the tail still holds it, each cut
    where the slice runner's trailer begins (`_TRAILER`)."""
    def own(text):
        cut = _TRAILER.search(text)
        return text[:cut.start()] if cut else text
    text = own(str(failure.get("traceback") or ""))
    for block in _BLOCK_RULE.split(str(row.get("stderr_tail") or "")):
        hit = _BLOCK_HEAD.search(block)
        if hit and str(failure["test"]).startswith(hit.group("id")):
            text += "\n" + own(block)
    return text


def _suffixes(raw, files):
    """Every `helm/...py` suffix of one path that this tree holds."""
    parts = raw.replace("\\", "/").split("/")
    return {"/".join(parts[i:]) for i, part in enumerate(parts)
            if part == "helm" and "/".join(parts[i:]) in files}


def helm_paths(text, files):
    """The `helm/*.py` paths of this tree one failure's text names: its
    frames, any path-shaped mention, and a dotted `helm.module` (the leak
    audit names the module whose data a unit left changed). The slice
    runner's own leak frame names nobody (see `_LEAK_FRAME`)."""
    text = _LEAK_FRAME.sub("", text)
    found = set()
    for raw in _FRAME.findall(text) + _PATHISH.findall(text):
        found |= _suffixes(raw, files)
    for dotted in _DOTTED.findall(text):
        parts = dotted.split(".")
        for k in range(len(parts), 1, -1):
            path = "/".join(parts[:k]) + ".py"
            if path in files:
                found.add(path)
                break
    return {p for p in found if p.startswith("helm/") and p.endswith(".py")}


def window_log(gid, logs):
    """The window's job log that bound receipt `gid`, or None. The detached
    client ends each log with Fab's terminal event and the import's line, and
    both name the receipt (gatewindow.follow_argv)."""
    try:
        names = [n for n in os.listdir(logs)
                 if n.startswith("gate-") and n.endswith(".log")]
    except OSError:
        return None
    dated = []
    for name in names:
        path = os.path.join(logs, name)
        try:
            dated.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _when, path in sorted(dated, reverse=True)[:gatehost.HISTORY_SCAN]:
        try:
            with open(path, "rb") as fh:
                blob = fh.read(1 << 20)
        except OSError:
            continue
        for line in blob.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            snap = row.get("snapshot") if isinstance(row, dict) else None
            if isinstance(row, dict) and gid in (
                    row.get("receipt"),
                    snap.get("receipt") if isinstance(snap, dict) else None):
                return path
    return None


def read_red(train, token=None, receipts=None, logs=None):
    """(red, refusal) — the red gate this train is blamed for.

    `token` (`gate:<id>` or a unique prefix) names it, and it must have gated
    this room's head; without one it is the newest receipt for that head.
    `red` carries the receipt `id`, its `mode`, the failing `tests` (each with
    its `kind`, `module`, `file` and the helm `paths` its traceback names),
    the `modules` to run, how many identities the receipt `omitted` from its
    diagnostics, the window `log` that bound it and the `host` it ran on
    (`red_host`)."""
    rows, unavailable, _skipped = (receipts or gatemod.receipts)()
    if unavailable:
        return None, ("the gate ledger could not be read (%s), so there is "
                      "no red to blame" % unavailable)
    head = train["head"]
    if token:
        hit = _TOKEN.match(str(token).strip().lower())
        if not hit:
            return None, "%r is not a gate receipt token (gate:<id>)" % token
        rid = hit.group(1)
        found = [r for r in rows if str(r.get("id") or "").startswith(rid)]
        if not found:
            return None, ("no readable receipt gate:%s is in the gate ledger"
                          % rid)
        if len(found) > 1:
            return None, ("gate:%s names %d receipts; give more of its id"
                          % (rid, len(found)))
        row = found[0]
        if row.get("head") != head:
            return None, ("gate:%s gated %s, not this room's head %s"
                          % (row.get("id"), _short(row.get("head")),
                             _short(head)))
    else:
        mine = [r for r in rows if r.get("head") == head]
        if not mine:
            return None, ("no receipt names this room's head %s; pass --gate "
                          "gate:<id>" % _short(head))
        row = max(mine, key=lambda r: str(r.get("ts") or ""))
    gid = str(row.get("id"))
    status = row.get("status")
    if status == "OK":
        return None, "gate:%s is GREEN: this train has nothing to blame" % gid
    if status != "FAILED":
        return None, ("gate:%s is %s (%s): a run with no readable verdict "
                      "names no failing test"
                      % (gid, status or "UNKNOWN",
                         row.get("detail") or "no detail"))
    if row.get("failures_unreadable"):
        return None, ("gate:%s is FAILED but its failures are unreadable, so "
                      "no failing test can be named" % gid)
    tests, why = _placed(train, row, "gate:%s" % gid)
    if why:
        return None, why
    total = row.get("failure_total")
    total = total if type(total) is int and total >= len(tests) \
        else len(tests)
    log = window_log(gid, logs) if logs else None
    return {"id": gid, "head": head, "label": row.get("label"),
            "tree": row.get("tree"),
            "mode": gatehost.SLICED if row.get("v") == gatemod.SLICE_VERSION
            else gatehost.SERIAL,
            "tests": tests,
            "modules": list(dict.fromkeys(t["module"] for t in tests)),
            "omitted": total - len(tests), "log": log,
            "host": red_host(row, log)}, None


def red_host(row, log):
    """The host a red gate ran on, or None: the node the window's job log
    names for it (Fab's own word, `gatehost._terminal`), else the node the
    receipt itself records. A name that is not a host token is no host."""
    event = gatehost._terminal(log) if log else None
    for node in ((event or {}).get("node"),
                 gatemod._host_of(row).get("node")):
        if isinstance(node, str) and gatehost._ATOM.match(node):
            return node
    return None


def _placed(train, row, who):
    """(tests, refusal): each failure a receipt-shaped `row` names, placed
    in its test module and file by the room's own tree, with the helm paths
    its text names. `who` names the red in a refusal."""
    failures = [f for f in row.get("failures") or ()
                if isinstance(f, dict) and f.get("test")]
    if not failures:
        return None, "%s is FAILED and names no failing test" % who
    tests = []
    for failure in failures:
        test = str(failure["test"])
        module, path = module_of(test, train["files"])
        if module is None:
            return None, ("%s names %s, which no test module of this tree "
                          "holds" % (who, test))
        tests.append({"id": test, "kind": failure.get("kind") or "FAILURE",
                      "module": module, "file": path,
                      "paths": sorted(helm_paths(_failure_text(failure, row),
                                                 train["files"]))})
    return tests, None


def audit_red(train, log):
    """(red, refusal) — the red PRE-GATE AUDIT run a train is blamed for in
    place of a gate receipt (task/3674): the log auto-land's audits wrote on
    the train's composed room (`autoland.Ops.audits`).

    The log's unittest report is read by the gate's own parser
    (`gate.parse_result`) up to its LAST summary line, so Fab's words after
    the run are not read as the runner's, and each failure is placed as
    `read_red` places a gate's, its block read from that same report: the
    job's stdout, flushed as the runner exits, and Fab's closing lines follow
    the summary, a gate's receipt holds neither, and a helm path only they
    name would name a car no failure names. A log that
    cannot be read, a run that is not a readable FAILED list (no summary, a
    pass, a failure count its blocks do not match) or a failure no test
    module of this tree holds is refused: nobody is blamed from a list that
    cannot be read. `red` carries the log as `audits` and `log`, the host
    Fab ran it on as `host` (the last host Fab's own lines name, `placements`:
    its `fab: role=... -> HOST` line, or the node a MOVE took it to; None when
    the log has none), the runner its `$ ` command line ran as `mode` (SLICED
    when that line runs the slice runner, so the re-run before blame is the
    audits' own line and a failure only slices show repeats; None when the log
    has no command line), and no receipt `id` or `tree`."""
    said = "the pre-gate audits' log %s" % log
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, TypeError, ValueError) as exc:
        return None, "%s cannot be read (%s)" % (said, exc)
    placed, ran = placements(text), _RAN.search(text)
    mode = None if ran is None else gatehost.SLICED \
        if gatemod.SLICE_RUNNER in ran.group(1).split() else gatehost.SERIAL
    ends = [hit.end() for hit in _SUMMARY.finditer(text)]
    text = text[:ends[-1]] if ends else text
    parsed = gatemod.parse_result(text)
    if parsed["status"] != "FAILED" or parsed["failures_unreadable"]:
        return None, "%s holds no readable failure list (%s)" % (
            said, parsed.get("unreadable_reason")
            or "its summary reads %s" % parsed["status"])
    tests, why = _placed(train, {"failures": parsed["failures"],
                                 "stderr_tail": text}, said)
    if why:
        return None, why
    return {"id": None, "audits": log, "head": train["head"],
            "label": train["train"], "tree": None, "mode": mode,
            "tests": tests,
            "modules": list(dict.fromkeys(t["module"] for t in tests)),
            "omitted": 0, "log": log,
            "host": placed[-1] if placed else None}, None


def _red_name(red):
    """What went RED, where a gate would be named: `gate:<id>`, or the
    pre-gate audits (`audit_red`)."""
    return "its pre-gate audits" if red.get("audits") \
        else "gate:%s" % red["id"]


# -- blame by diff -----------------------------------------------------------

def lane_files(be, root, before, merge):
    """The files car merge `merge` changed in the room over `before`, the
    prefix it was merged onto (`git diff before merge`), or None.

    WHAT THE CAR ADDED, NEVER ITS LANE'S HISTORY (task/4145). A merge-base
    diff (`trunk...tip`) charged a lane that back-merged trunk with trunk's
    own changes: lane/train-resume-wording-4133 was cut from a lane trunk
    later merged, then merged trunk itself, so it had two merge bases with
    trunk, git picked one, and its 2-file change read as 43 files, one of
    them the red module trunk had changed. Its merge over the prefix below it
    holds only what the lane brought that the room did not already have."""
    rc, out, _err = be.text(root, "diff", "--name-only", "-z", before, merge,
                            env=_env())
    return {f for f in out.split("\0") if f} if rc == 0 else None


def blame_by_diff(train, red, be):
    """Mark each car: `files` (what its merge added to the room over the
    prefix below it, `lane_files`; None when git could not give it) and
    `named` (the failing files and traceback paths those touch; None for
    UNKNOWN). Returns the named cars."""
    failing = {t["file"] for t in red["tests"]}
    paths = set().union(*(t["paths"] for t in red["tests"]))
    for car in train["cars"]:
        files = lane_files(be, train["root"], train["prefixes"][car["n"] - 1],
                           car["merge"])
        car["files"] = None if files is None else sorted(files)
        car["named"] = None if files is None \
            else sorted(files & (failing | paths))
    return [car for car in train["cars"] if car["named"]]


# -- hosts and runs ----------------------------------------------------------

def hosts(environ, logs, live):
    """(free, known, shut, busy) — where prefix runs may go.

    `known` is gatehost's candidate list (HELM_GATE_HOSTS in order, else the
    hosts the window's logs name, fastest first), `shut` the caller's
    FAB_EXCLUDE_HOSTS, `busy` every host a live window row runs on, and `free`
    the known hosts left: one prefix run per free host at a time."""
    order, _ignored = gatehost.configured(environ)
    known = order or gatehost.discovered(gatehost.history(logs))
    shut = gatehost.excluded(environ)
    busy = {row["host"] for row in live if row.get("host")}
    free = [h for h in known if h not in shut and h not in busy]
    return free, known, shut, busy


def pin(host, known, shut, busy):
    """FAB_EXCLUDE_HOSTS for one run pinned to `host`: the caller's own
    exclusions, EXTENDED by every other known and busy host, never replaced.
    With no host known, Fab places the run with every busy host excluded."""
    return sorted((set(known) | set(shut) | set(busy)) - {host})


def run_argv(worktree, modules, mode, nonce):
    """The one `fab test` a prefix run spends: the failing modules through the
    gate's own runner, between the markers, the runner's stderr (where
    unittest reports) on stdout and its stdout dropped.

    A SLICED gate's runs are the slice runner's `--modules` scope with the
    land gate's fail-mode leak audit, the exact line `helm gate audits`
    prints for slices (gateaudits.command), so a failure only slices show is
    reproduced; a SERIAL gate's runs are one unittest process, its own line."""
    from . import gateaudits, gateslice
    if mode == gatehost.SLICED:
        runner = (["env"] + list(gateaudits.SLICED_ENV)
                  + ["python3", gatemod.SLICE_RUNNER, gateslice.MODULES_FLAG]
                  + list(modules))
        head = [gatewindow.FAB_BINARY, "test", "--slots", "fit", "--cores",
                str(gatemod.SLICE_SLOTS * gatemod._CORES_PER_SUITE),
                "--repo", worktree, "--"]
    else:
        runner = ["python3", "-m", "unittest", "-v"] + list(modules)
        head = [gatewindow.FAB_BINARY, "test", "--repo", worktree, "--"]
    mark = "%s-%s" % (MARK, nonce)
    script = ("printf '%%s\\n' %s; %s 2>&1 >/dev/null; rc=$?; "
              "printf '%%s %%d\\n' %s \"$rc\"; exit \"$rc\""
              % (shlex.quote(mark + " BEGIN"),
                 " ".join(shlex.quote(a) for a in runner),
                 shlex.quote(mark + " END")))
    return head + ["sh", "-c", script]


def read_run(rc, out_text, err_text, nonce, red):
    """One prefix run's colour: {status, failed, other, why}.

    GREEN: the runner's own summary is OK and it exited 0, or it FAILED
    readably on none of the red gate's own failing tests. RED: one of those
    tests fails here, or a failing module will not import. UNKNOWN: no end
    marker (Fab could not place or finish it) or no readable summary."""
    mark = "%s-%s" % (MARK, nonce)
    lines = (out_text or "").splitlines()
    begin = next((i for i, line in enumerate(lines)
                  if line.strip() == mark + " BEGIN"), None)
    end = code = None
    ending = re.compile(re.escape(mark) + r" END (\d+)\Z")
    for i in range(len(lines) - 1, -1, -1):
        hit = ending.match(lines[i].strip())
        if hit:
            end, code = i, int(hit.group(1))
            break
    if begin is None or end is None or end < begin:
        said = [ln for ln in ((err_text or "") + "\n" + (out_text or ""))
                .splitlines() if ln.strip()]
        return {"status": UNKNOWN, "failed": [], "other": [],
                "why": "the run never reached its end marker (fab exit %s): "
                       "%s" % (rc, said[-1].strip() if said
                               else "it said nothing")}
    parsed = gatemod.parse_result("\n".join(lines[begin + 1:end]) + "\n")
    failed = [str(e.get("test")) for e in parsed["failures"]
              if isinstance(e, dict) and e.get("test")]
    if parsed["status"] == "OK" and code == 0:
        return {"status": GREEN, "failed": [], "other": [], "why": None}
    if parsed["status"] == "FAILED" and not parsed["failures_unreadable"]:
        ids = {t["id"] for t in red["tests"]}
        modules = set(red["modules"])
        ours = [t for t in failed if t in ids or (
            t.startswith(_LOADER) and t[len(_LOADER):] in modules)]
        other = [t for t in failed if t not in ours]
        if ours:
            return {"status": RED, "failed": ours, "other": other,
                    "why": None}
        return {"status": GREEN, "failed": [], "other": other,
                "why": "the red gate's own failing tests pass here; %d other "
                       "test(s) failed: %s" % (len(other),
                                               ", ".join(other[:SHOWN]))}
    return {"status": UNKNOWN, "failed": failed, "other": [],
            "why": "no readable verdict (%s, runner exit %s)"
                   % (parsed.get("unreadable_reason") or parsed["status"],
                      code)}


def spread(lo, hi, width):
    """The prefixes one round runs between a green `lo` and a red `hi`:
    every one when the hosts allow, else `width` of them evenly spaced, so a
    single host bisects."""
    inner = list(range(lo + 1, hi))
    if len(inner) <= width:
        return inner
    return sorted({lo + (j + 1) * (hi - lo) // (width + 1)
                   for j in range(width)})


def search(n, probe, width):
    """(kind, culprit, note) over prefixes P0..Pn. `probe(points)` returns
    {point: {"status": ...}} for the prefixes it ran; `width` is how many run
    at once. The ends run first and decide TRUNK-RED and FLAKE; then each
    round narrows the gap between the last green and the first red."""
    got = probe([0, n])
    if got[0]["status"] == RED:
        return TRUNK_RED, None, ("P0, trunk alone, fails the red gate's own "
                                 "tests")
    for p in (0, n):
        if got[p]["status"] not in (GREEN, RED):
            return UNKNOWN, None, "P%d could not be read: %s" % (
                p, got[p].get("why") or "no reason given")
    if got[n]["status"] == GREEN:
        return FLAKE, None, ("P%d, the whole train, passes the red gate's own "
                             "tests on a re-run" % n)
    lo, hi, notes = 0, n, []
    while hi - lo > 1:
        points = spread(lo, hi, width)
        got = probe(points)
        reds = [p for p in points if got[p]["status"] == RED]
        top = min(reds) if reds else hi
        unread = [p for p in points
                  if p < top and got[p]["status"] not in (GREEN, RED)]
        if unread:
            return UNKNOWN, None, "P%d could not be read: %s" % (
                unread[0], got[unread[0]].get("why") or "no reason given")
        greens = [p for p in points if got[p]["status"] == GREEN]
        above = [p for p in greens if p > top]
        if above:
            notes.append("P%d passed above the red P%d (a flaky prefix)"
                         % (above[0], top))
        hi = top
        lo = max([p for p in greens if p < hi] + [lo])
    return EJECT, hi, "; ".join(["P%d green, P%d red" % (lo, hi)] + notes)


def _mint(ctx, probe):
    """(worktree, why): the probe's detached room, at its commit, with its car
    merged alone when it is the ALONE run."""
    be, root = ctx["be"], ctx["root"]
    where = _lanes.lane_path(root, os.path.join(
        BOX, "%s-%s-%s" % (ctx["train"]["train"], probe["name"],
                           ctx["nonce"])))
    os.makedirs(os.path.dirname(where), exist_ok=True)
    # THE SAME SANCTION `helm train` HANDS THE REF GUARD for its rooms.
    rc, _out, err = be.text(root, "worktree", "add", "--detach", where,
                            probe["at"],
                            env=dict(_env(), HELM_WORK_INTEGRATOR="1"))
    if rc != 0:
        return None, "cannot mint %s: %s" % (where, _first(err))
    car = probe.get("car")
    if car:
        outcome, detail, _head = landwindow.merge_car(
            be, where, car, "%s-alone" % ctx["train"]["train"],
            identity=ctx["train"]["identity"])
        if outcome != landwindow.MERGED:
            return where, ("car %d does not merge alone onto trunk: %s"
                           % (car["n"], detail))
    return where, None


def _remove(ctx, where):
    ctx["be"].text(ctx["root"], "worktree", "remove", "--force", where,
                   env=dict(_env(), HELM_WORK_INTEGRATOR="1"))


def _run_one(ctx, probe, host, where):
    nonce = _nonce()
    exclude = pin(host, ctx["known"], ctx["shut"], ctx["busy"])
    env = dict(ctx["environ"], **{gatehost.EXCLUDE_ENV: " ".join(exclude)}) \
        if exclude else None
    argv = run_argv(where, ctx["red"]["modules"], ctx["red"]["mode"], nonce)
    try:
        rc, text, err = ctx["fab"](argv, RUN_TIMEOUT_S, env=env)
    except Exception as exc:                # noqa: BLE001 — named, UNKNOWN
        rc, text, err = None, "", "%s: %s" % (type(exc).__name__, exc)
    got = read_run(rc, text, err, nonce, ctx["red"])
    placed = placements((text or "") + "\n" + (err or ""),
                        "%s-%s" % (MARK, nonce))
    off = [h for h in placed if h != host]
    if host and off:
        # A RUN PINNED TO ONE HOST THAT FAB PLACED, OR MOVED, ONTO ANOTHER
        # answers for the other host, never for the one asked about.
        got = {"status": UNKNOWN, "failed": [], "other": [],
               "why": "pinned to %s, Fab placed it on %s" % (host, off[-1])}
    got.update({"label": probe["label"],
                "host": host or (placed[-1] if placed else None),
                "exclude": exclude, "output": (text or "") + (err or "")})
    return got


def placements(text, mark=None):
    """Every host Fab's own lines in `text` say a run went to, in order
    (`_WENT`): placed, MOVED to another node while it queued, and ended on.
    The last is where it ran. A line between a run's `mark` BEGIN and END is
    the run's own output, never Fab's, so a test that prints Fab's words
    cannot speak for it.

    THE FIRST LINE IS NOT THE ANSWER: a job that waits in one node's slot
    queue can be withdrawn and launched on another, whose placement Fab
    prepares out of sight and reports only as `MOVED a -> b` and in its
    closing `LOG: b:` line, so a pin read from the first line alone passes a
    run that ran elsewhere."""
    went, inside = [], False
    for line in (text or "").splitlines():
        if mark and line.strip() == mark + " BEGIN":
            inside = True
        elif mark and line.strip().startswith(mark + " END"):
            inside = False
        elif not inside:
            hit = _WENT.match(line)
            if hit:
                went.append(next(g for g in hit.groups() if g))
    return went


def run_probes(ctx, probes):
    """{key: result} for `probes`, in waves of one run per slot. Each wave's
    rooms are minted first (git takes one worktree lock at a time), run in
    parallel, and removed whatever happened."""
    results, slots = {}, ctx["slots"]
    for start in range(0, len(probes), len(slots)):
        wave = list(zip(probes[start:start + len(slots)], slots))
        rooms, jobs = [], []
        try:
            for probe, host in wave:
                where, why = _mint(ctx, probe)
                if where:
                    rooms.append(where)
                if why:
                    results[probe["key"]] = {
                        "status": UNKNOWN, "failed": [], "other": [],
                        "why": why, "label": probe["label"], "host": host,
                        "exclude": [], "output": ""}
                else:
                    jobs.append((probe, host, where))
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, len(jobs))) as pool:
                done = [(probe, pool.submit(_run_one, ctx, probe, host,
                                            where))
                        for probe, host, where in jobs]
                for probe, future in done:
                    results[probe["key"]] = future.result()
        finally:
            for where in rooms:
                _remove(ctx, where)
            ctx["be"].text(ctx["root"], "worktree", "prune", env=_env())
        ctx["rounds"].append([
            {"label": results[p["key"]]["label"],
             "host": results[p["key"]]["host"],
             "status": results[p["key"]]["status"],
             "why": results[p["key"]].get("why")} for p, _h in wave])
        for probe, host in wave:
            got = results[probe["key"]]
            if host and probe["at"] == ctx["train"]["trunk"] \
                    and not probe.get("car"):
                ctx.setdefault("trunk_runs", {})[host] = got
            print("  %-22s on %s: %s%s" % (
                got["label"], got["host"] or "a host Fab placed", got["status"],
                " — %s" % got["why"] if got.get("why") else ""),
                file=ctx["say"])
            ctx["outputs"].append(got)
    return results


def _prefix_probe(ctx, k):
    return {"key": k, "name": "p%d" % k, "at": ctx["train"]["prefixes"][k],
            "label": "P%d%s" % (k, " (trunk)" if k == 0 else
                                " (+ car %d, lane %s)"
                                % (k, ctx["train"]["cars"][k - 1]["lane"]))}


def overlapping(train, red, car):
    """The cars before `car` whose lane diff overlaps its files, a failing
    test's file or a path the traceback names: the cars it clashed with."""
    look = set(car["files"] or ()) | {t["file"] for t in red["tests"]} \
        | set().union(*(t["paths"] for t in red["tests"]))
    return [dict(other, overlap=sorted(set(other["files"]) & look))
            for other in train["cars"][:car["n"] - 1]
            if other["files"] and set(other["files"]) & look]


def bisect(ctx):
    """The verdict of the prefix bisect, with the ALONE run of the culprit."""
    train = ctx["train"]
    n = len(train["cars"])
    kind, k, note = search(
        n, lambda points: run_probes(ctx, [_prefix_probe(ctx, p)
                                           for p in points]),
        len(ctx["slots"]))
    verdict = {"kind": kind, "by": BY_BISECT, "why": note, "car": None,
               "alone": None, "clash": []}
    if kind != EJECT:
        return verdict
    car = train["cars"][k - 1]
    verdict["car"] = car
    if k == 1:
        verdict["alone"] = RED
        verdict["why"] = note + "; car 1 alone on trunk is P1"
        return verdict
    alone = run_probes(ctx, [{
        "key": "alone", "name": "alone%d" % k, "at": train["trunk"],
        "car": car, "label": "car %d alone on trunk" % k}])["alone"]
    verdict["alone"] = alone["status"]
    if alone["status"] == GREEN:
        verdict["clash"] = overlapping(train, ctx["red"], car)
        verdict["why"] = note + ("; car %d alone on trunk is GREEN, so it "
                                 "CLASHES with the cars before it" % k)
    elif alone["status"] == RED:
        verdict["why"] = note + "; car %d fails alone on trunk" % k
    else:
        verdict["why"] = note + ("; car %d alone on trunk could not be read "
                                 "(%s), so a clash is UNKNOWN"
                                 % (k, alone.get("why")))
    return verdict


def trunk_check(ctx, host):
    """(GREEN | RED | UNKNOWN, why): the red's failing modules on P0, trunk
    alone, pinned to `host`, the host the red ran on, through the prefixes'
    own runner: the run that licenses an ejection (task/4145). The bisect's
    own P0 run on that host is that run, and is not run twice."""
    said = "trunk %s on %s" % (_short(ctx["train"]["trunk"]), host)
    if not host:
        return UNKNOWN, ("no host is recorded for %s, so trunk cannot be run "
                         "where it went red" % _red_name(ctx["red"]))
    if host in ctx["shut"]:
        return UNKNOWN, "%s: %s excludes it" % (said, gatehost.EXCLUDE_ENV)
    if host in ctx["busy"]:
        return UNKNOWN, "%s: it is running a gate" % said
    got = (ctx.get("trunk_runs") or {}).get(host)
    if got is None:
        got = run_probes(dict(ctx, slots=[host]), [{
            "key": "trunk", "name": "trunk", "at": ctx["train"]["trunk"],
            "label": "P0 (trunk)"}])["trunk"]
    status = got["status"]
    if status == RED:
        return RED, ("%s fails the red's own tests too (%s): a host or trunk "
                     "fault, not a car's" % (said, ", ".join(
                         got["failed"][:SHOWN])))
    if status == GREEN:
        return GREEN, "%s passes the red's own tests" % said
    return UNKNOWN, "%s could not be read: %s" % (
        said, got.get("why") or "no reason given")


# -- telling -----------------------------------------------------------------

def lane_task(rid, snapshot=None, lane=None):
    """(task, unknown, refusal): the car's own lane proof reconciled with
    its chain's first row; disagreement or unreadable proof is UNKNOWN.

    `unknown` is the join's reason when it could not decide (two records
    that disagree, a record that cannot be read, two tasks named): said on
    the merge line and the ejection, never read as "no task". Both are None
    for work that names no task. `refusal` is for a dispatch ledger that
    cannot be read. Auto-land names the task on the merge line and train
    blame comments on it; neither closes it."""
    from . import dispatches, taskkey
    current, unavailable = (snapshot or dispatches.snapshot)()
    if unavailable:
        return None, None, ("the dispatch ledger could not be read (%s), so "
                            "the task this lane names is UNKNOWN"
                            % unavailable)
    row = current.get(rid)
    if row is None:
        return None, None, None
    key = taskkey.car_key(row, current, lane=lane)
    unknown = None if key.task or key.why == taskkey.NO_TASK else key.why
    return key.task, unknown, None


def land_request(lrs, car):
    """(lr, why): the land request a car rides for. The one its merge names
    (`helm train` writes `land request <id>` in the body); else, for a
    hand-composed merge that names none, the row whose reviewed (or held
    source-clean) tip IS the car's tip, a live row before a closed one. Rows
    of that tip with different authors are refused: whoever is told must be
    the one author."""
    lr = lrs.get(car["id"]) if car["id"] != "?" else None
    if lr:
        return lr, None
    hits = [row for row in lrs.values() if car["tip"] in (
        row.get("reviewed_tip"), row.get("source_clean_tip"))]
    live = [row for row in hits if not row.get("terminal")] or hits
    if not live:
        return None, ("no land request's reviewed tip is %s"
                      % _short(car["tip"]))
    authors = {row.get("author") for row in live}
    if len(authors) > 1:
        return None, ("%d land requests on tip %s name different authors (%s)"
                      % (len(live), _short(car["tip"]),
                         ", ".join(sorted(str(a) for a in authors))))
    return max(live, key=lambda row: str(row.get("entered_ts") or "")), None


def next_name(root, train):
    """The name the train is composed again under: the next letter after its
    own (`train283` -> `train283b`, `train283b` -> `train283c`), the first
    whose compose room does not exist; None when none is left."""
    hit = re.fullmatch(r"(.*\d)([a-y]?)", train)
    base, letter = (hit.group(1), hit.group(2)) if hit else (train, "")
    for code in range(ord(letter or "a") + 1, ord("z") + 1):
        name = base + chr(code)
        if not os.path.lexists(_lanes.lane_path(
                root, os.path.join(landwindow.BOX, name))):
            return name
    return None


def _car_words(car):
    return "car %d (lane %s, land request %s, tip %s)" % (
        car["n"], car["lane"], _short(car["id"]), _short(car["tip"]))


def _clash_words(verdict):
    if verdict["by"] == BY_DIFF:
        return ("not measured: blame by diff named this car alone, so no "
                "car's prefix ran")
    if verdict["alone"] == RED:
        return "none: it fails alone on trunk"
    if verdict["alone"] != GREEN:
        return "UNKNOWN: its alone-on-trunk run could not be read"
    if not verdict["clash"]:
        return ("the cars before it, though no earlier car's diff overlaps "
                "its files or the traceback")
    return "; ".join("lane %s (land request %s; overlaps %s)" % (
        c["lane"], _short(c["id"]), ", ".join(c["overlap"]))
        for c in verdict["clash"])


def telling(train, red, verdict, name, runlog):
    """The DM and the task comment: the failing tests, the logs, the
    evidence and the cars it clashed with."""
    car = verdict["car"]
    shown = [t["id"] for t in red["tests"]][:SHOWN]
    more = len(red["tests"]) + red["omitted"] - len(shown)
    return "\n".join((
        "%s: your lane %s (land request %s, tip %s) was EJECTED from %s, "
        "whose %s." % (PROG, car["lane"], _short(car["id"]),
                       _short(car["tip"]), train["train"],
                       "pre-gate audits are RED" if red.get("audits")
                       else "gate gate:%s is RED" % red["id"]),
        "  failing: %s%s" % (", ".join(shown),
                             " (+%d more)" % more if more else ""),
        "  log: %s; %s; blame's runs: %s" % (
            red["log"] or "no window log names this receipt",
            "the audits ran before any gate, so no receipt names them"
            if red.get("audits") else
            "receipt: `helm gate show %s`" % red["id"],
            runlog or "none (no run was logged)"),
        "  evidence: %s" % verdict["why"],
        "  clashed with: %s" % _clash_words(verdict),
        "  %s composes the train again without it, on trunk %s. Your lane "
        "owes a tip that passes %s there%s; until then `helm train` leaves "
        "tip %s out (a new tip rides again; `helm train readmit` clears it "
        "by hand)." % (
            name, _short(train["trunk"]), " ".join(red["modules"]),
            " beside the cars it clashed with" if verdict["clash"] else "",
            _short(car["tip"])),
    ))


def _tell_defaults():
    from . import chat, seats, tasks
    return {"message": lambda to, text: seats.dm(to, text, who=WHO),
            "comment": lambda task, text, by=None: tasks.comment(task, text,
                                                                 by=by),
            "post": lambda text, room=None: chat.post(text, room=room,
                                                      who=WHO)}


def _called(leg, *args, **kw):
    """(row, error) from one telling leg; whatever it raises is its error."""
    try:
        got = leg(*args, **kw)
    except Exception as exc:                # noqa: BLE001 — named, refused
        return None, "%s: %s" % (type(exc).__name__, exc)
    if isinstance(got, tuple):
        return got[0], got[1] if len(got) > 1 else None
    return got, None if got else "it wrote nothing"


def write_runlog(train, red, outputs):
    """blame's own run log: every prefix run's host, colour and output. Its
    path, or None when it could not be written (the DM says so)."""
    if not outputs:
        return None
    path = os.path.join(home.global_dir(), EVIDENCE_SUBDIR, "%s-%s.log" % (
        train["train"], _nonce()))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%s: %s at %s, %s (%s)\n" % (
                PROG, train["train"], train["head"], _red_name(red),
                red["mode"]))
            for got in outputs:
                fh.write("\n=== %s on %s: %s%s\n%s\n" % (
                    got["label"], got["host"], got["status"],
                    " — %s" % got["why"] if got.get("why") else "",
                    got.get("output") or ""))
    except OSError:
        return None
    return path


def eject(ctx, verdict, door=None, tell=None, project=None, snapshot=None):
    """Tell, then compose the train again without the culprit. -> exit code.

    THE TELLING COMES FIRST and either leg failing REFUSES: an ejection
    nobody was told about is the silence R2 forbids, so the DM and the task
    comment must land before anything is composed. The undelivered text is
    printed so a person can deliver it. The room line is posted after the
    launch, and its failure is loud too."""
    from . import landreq
    say, train, red = ctx["say"], ctx["train"], ctx["red"]
    car = verdict["car"]
    root, identity = train["root"], train["identity"]
    auth = landwindow.trunk_authority(root, identity)
    stale = landwindow.authority_refusal(
        {"authority": auth, "trunk": train["trunk"],
         "ref": auth["ref"] or "the trunk"})
    if stale:
        print("%s: REFUSED — %s. The train stands on a trunk that has moved, "
              "so `helm train --apply` composes a fresh one; nothing was "
              "told, minted or launched." % (PROG, stale), file=say)
        return 1
    lrs, unavailable = (project or landreq.project)()
    lr, why = (None, "the land-request ledger is unreadable (%s)"
               % unavailable) if unavailable else land_request(lrs or {}, car)
    author = (lr or {}).get("author")
    if not author:
        print("%s: REFUSED — %s: %s, so nobody can be told and the car is "
              "not ejected." % (PROG, _car_words(car),
                                why or "its land request names no author"),
              file=say)
        return 1
    car["id"] = lr.get("id") or car["id"]
    task, unknown, why = lane_task(car["id"], snapshot, lane=car["lane"])
    if why:
        print("%s: REFUSED — %s; the car is not ejected." % (PROG, why),
              file=say)
        return 1
    name = next_name(root, train["train"])
    if not name:
        print("%s: REFUSED — every compose room %sb..z already exists; "
              "nothing was told." % (PROG, train["train"]), file=say)
        return 1
    text = telling(train, red, verdict, name, ctx.get("runlog"))
    legs = dict(_tell_defaults(), **(tell or {}))
    _row, err = _called(legs["message"], author, text)
    if err:
        print("%s: REFUSED — the DM to @%s was not delivered (%s). The "
              "ejection is never silent, so nothing was composed. The "
              "undelivered text:\n%s" % (PROG, author, err, text), file=say)
        return 1
    print("  told @%s by DM" % author, file=say)
    if task:
        _row, err = _called(legs["comment"], task, text, by=WHO)
        if err:
            print("%s: REFUSED — the comment on %s was not written (%s). The "
                  "DM to @%s went out; nothing was composed. The unwritten "
                  "text:\n%s" % (PROG, task, err, author, text), file=say)
            return 1
        print("  commented on %s" % task, file=say)
    else:
        print("  no task comment: lane %s %s, and a verdicted dispatch row "
              "takes no note (helm dispatch's findings-note admits open and "
              "held rows only); the DM and the room line carry the evidence"
              % (car["lane"], "task UNKNOWN: %s" % unknown if unknown
                 else "names no task"), file=say)
    # THE RECORD, bound to the car's exact tip: without it the car's row is
    # still READY and the next `helm train` merges it again. It is written
    # only once the author was told, so an untold ejection never keeps a car
    # out; a record that cannot be written refuses, since the ejection would
    # undo itself on the next train.
    _row, err = landwindow.record_ejection(root, {
        "tip": car["tip"], "lr": car["id"], "lane": car["lane"],
        "train": train["train"], "gate": red["id"],
        "audits": red.get("audits"), "verdict": EJECT,
        "by_diff": verdict["by"] == BY_DIFF, "evidence": verdict["why"],
        "tests": [t["id"] for t in red["tests"]][:SHOWN],
        "clash": [c["lane"] for c in verdict["clash"]],
        "by": home.chat_name() or WHO})
    if err:
        print("%s: REFUSED — the ejection of %s could not be recorded (%s), "
              "so the next `helm train` would merge it again. @%s was told; "
              "nothing was composed." % (PROG, _car_words(car), err, author),
              file=say)
        return 1
    print("  recorded: `helm train` leaves tip %s out until the lane re-tips "
          "or `helm train readmit %s --reason ...` clears it"
          % (_short(car["tip"]), _short(car["tip"])), file=say)
    kept = [c for c in train["cars"] if c["n"] != car["n"]]
    rc = 0
    if kept:
        print("  composing %s without %s:" % (name, _car_words(car)), file=say)
        rc = landwindow.compose_room(
            {"root": root, "identity": identity, "trunk": train["trunk"],
             "train": name,
             "room": _lanes.lane_path(root, os.path.join(landwindow.BOX,
                                                         name)),
             "cars": [{"id": c["id"], "lane": c["lane"], "tip": c["tip"],
                       "basis": c["basis"]} for c in kept]},
            out=say, door=door)
    else:
        print("  %s was the train's only car, so nothing is left to compose"
              % _car_words(car), file=say)
    line = "%s: %s is RED at %s — EJECTED lane %s (%s); %s" % (
        PROG, train["train"], _red_name(red), car["lane"],
        "blame by diff" if verdict["by"] == BY_DIFF else verdict["why"],
        "%s composes without it (exit %d)" % (name, rc) if kept
        else "nothing is left to compose")
    _row, err = _called(legs["post"], " ".join(line.split()), room=ROOM)
    if err:
        print("%s: the line to #%s was NOT posted (%s): %s"
              % (PROG, ROOM, err, line), file=say)
        return rc or 1
    print("  posted to #%s" % ROOM, file=say)
    return rc


# -- the verb ----------------------------------------------------------------

def _routing(doorkw, path, logs, environ):
    """(free, known, shut, busy, environ): where blame's runs may go now.

    LIVENESS IS ASKED, NEVER REMEMBERED, in the dry run too: the red train's
    own finished gate still has its row in the window store, and read as
    busy it would hide the fastest host from the plan. A host whose node
    cannot be read is not routed to."""
    environ = os.environ if environ is None else environ
    live, _retired, unknown = gatewindow.live_runs(
        gatewindow.read_runs(path), inflight=doorkw.get("inflight"),
        observe=doorkw.get("observe"), pid_alive=doorkw.get("pid_alive"),
        now=doorkw.get("now"))
    free, known, shut, busy = hosts(
        environ, logs, live + [{"host": h} for h in unknown])
    return free, known, shut, busy, environ


def _context(say, train, red, be, known, shut, busy, environ, fab, result):
    """The run context every run of one blame shares (`run_probes`)."""
    return {"say": say, "train": train, "red": red, "be": be,
            "root": train["root"], "nonce": _nonce(), "slots": [],
            "known": known, "shut": shut, "busy": busy,
            "environ": dict(environ), "fab": fab or gatewindow._fab,
            "rounds": result["rounds"], "outputs": [], "trunk_runs": {},
            "runlog": None}


def _car_json(car):
    return {k: car.get(k) for k in ("n", "id", "lane", "tip", "basis",
                                    "named", "overlap") if k in car} \
        if car else None


def blame(room, gate=None, apply=False, as_json=False, fab=None, door=None,
          tell=None, project=None, snapshot=None, receipts=None, out=None,
          environ=None, audits=None):
    """The verb. Returns the exit code.

    Read-only unless `apply`: it reads the room and the red, blames by diff
    and prints the plan. With `apply` it bisects (when the diff does not name
    exactly one car), runs trunk on the red's own host (`trunk_check`), and
    ejects only when trunk passes there. `audits` is the log of the room's red
    PRE-GATE AUDIT run (`audit_red`), read in place of a gate receipt by
    `helm train auto`: that red is blamed by diff alone, and a diff that does
    not name exactly one car refuses, since a red audit is never bisected.
    `fab` is the prefix runs' Fab seam (argv, timeout, env), `door` the
    landing-window door's own seams for the train
    it composes again (and the window store its hosts are read from), and
    `tell` the telling seams (`message`, the DM through seats.dm; `comment`;
    `post`, the room line); each default is the real thing, so an arm
    observes the shipped path.

    EXIT: 0 a verdict and everything it owes (the dry run's plan; an ejection
    told, composed and dispatched; TRUNK-RED and FLAKE reported); 1 refused,
    UNKNOWN, or a telling leg failed; 2 no such room (a usage error, as `helm
    gate window launch` answers one); 3 every known host is busy or excluded;
    the door's own code when the composed train's gate is refused (2, 3, 4).
    """
    out = out if out is not None else sys.stdout
    say = io.StringIO() if as_json else out
    doorkw = dict(door or {})
    result = {"exit": None}

    def done(rc):
        if as_json:
            result["exit"] = rc
            result["text"] = say.getvalue()
            print(json.dumps(result, indent=2, sort_keys=True, default=str),
                  file=out)
        return rc

    def refuse(why, rc=1):
        result["refused"] = why
        print("%s: REFUSED — %s" % (PROG, why), file=say)
        return done(rc)

    if not os.path.isdir(room):
        return refuse("no such room: %s (%s)" % (room, USAGE), 2)
    train, why = read_room(room)
    if why:
        return refuse(why)
    path = doorkw.get("path") or gatewindow.runs_path()
    logs = gatewindow.logs_dir(path)
    red, why = audit_red(train, audits) if audits else read_red(
        train, gate, receipts=receipts, logs=logs)
    if why:
        return refuse(why)
    be = vcs.backend(train["root"])
    named = blame_by_diff(train, red, be)
    unread = [car for car in train["cars"] if car["named"] is None]
    result.update({
        "train": train["train"],
        "room": {"path": train["room"], "trunk": train["trunk"],
                 "head": train["head"], "prefixes": train["prefixes"]},
        "gate": {k: red[k] for k in ("id", "mode", "modules", "log",
                                     "omitted")},
        "failing": red["tests"], "cars": [_car_json(c) for c in train["cars"]],
        "named": [c["n"] for c in named], "rounds": []})
    print("%s: %s at %s over trunk %s" % (PROG, train["train"],
                                          _short(train["head"]),
                                          _short(train["trunk"])), file=say)
    print("  red: the pre-gate audits on the composed room, log %s"
          % red["log"] if audits else "  red gate: gate:%s (%s), log %s" % (
              red["id"], red["mode"], red["log"] or "none names it"),
          file=say)
    print("  failing: %d test(s) in %s%s" % (
        len(red["tests"]), ", ".join(red["modules"]),
        " (%d more identities the receipt did not diagnose)" % red["omitted"]
        if red["omitted"] else ""), file=say)
    for test in red["tests"]:
        print("    %s %s%s" % (test["kind"], test["id"],
                               " — names %s" % ", ".join(test["paths"])
                               if test["paths"] else ""), file=say)
    print("  cars, in merge order:", file=say)
    for car in train["cars"]:
        print("    %d. %s  lane %s  tip %s  %s" % (
            car["n"], _short(car["id"]), car["lane"], _short(car["tip"]),
            "diff UNKNOWN" if car["named"] is None else
            "NAMED (touches %s)" % ", ".join(car["named"]) if car["named"]
            else "not named (%d file(s))" % len(car["files"])), file=say)
    if len(named) == 1 and not unread:
        car = named[0]
        verdict = {"kind": EJECT, "by": BY_DIFF, "car": car, "alone": None,
                   "clash": [],
                   "why": "blame by diff: lane %s's diff touches %s, and no "
                          "other car's does" % (car["lane"],
                                                ", ".join(car["named"]))}
        print("  blame by diff names exactly one car, %s: no bisect; it is "
              "EJECTED only if trunk passes on the red's own host"
              % _car_words(car), file=say)
        if not apply:
            result["verdict"] = dict(verdict, car=_car_json(car))
            print("  dry run: nothing told, minted or launched. `%s %s "
                  "--apply` first runs the failing tests on trunk on %s, the "
                  "host %s ran on, and only a pass there ejects it and "
                  "composes the train again without it." % (
                      PROG, train["room"], red.get("host") or "(no host "
                      "recorded: UNKNOWN, nothing is ejected)",
                      _red_name(red)), file=say)
            return done(0)
        _free, known, shut, busy, environ = _routing(doorkw, path, logs,
                                                     environ)
        ctx = _context(say, train, red, be, known, shut, busy, environ, fab,
                       result)
    else:
        unsure = " and %d car's diff is UNKNOWN" % len(unread) \
            if unread else ""
        if audits:
            return refuse("blame by diff names %d car(s)%s for the pre-gate "
                          "audits' failures, and a red pre-gate audit is "
                          "never bisected, so nothing is ejected"
                          % (len(named), unsure))
        print("  blame by diff names %d car(s)%s, so the verb bisects the "
              "prefixes P0..P%d" % (len(named), unsure,
                                    len(train["cars"])), file=say)
        free, known, shut, busy, environ = _routing(doorkw, path, logs,
                                                    environ)
        # THE RED'S OWN HOST TAKES P0 when it is free, so the bisect's trunk
        # run is the one that licenses an ejection (`trunk_check`).
        free = sorted(free, key=lambda h: h != red.get("host"))
        slots = free or ([] if known else [None])
        print("  hosts: %s%s%s" % (
            ", ".join(free) or ("none known: Fab places each run" if not known
                                else "none free"),
            "; busy %s" % ", ".join(sorted(busy)) if busy else "",
            "; excluded %s (%s)" % (", ".join(sorted(shut)),
                                    gatehost.EXCLUDE_ENV) if shut else ""),
            file=say)
        if not slots:
            return refuse("every known host (%s) is busy or excluded, so no "
                          "prefix can run without stacking on a gate; run it "
                          "again when one frees" % ", ".join(known), 3)
        if not apply:
            print("  bisect plan: the first round runs P0 and P%d, then %d "
                  "prefix(es) between the last green and the first red per "
                  "round, one per host, each in its own worktree; a red car "
                  "past the first also runs alone on trunk" % (
                      len(train["cars"]), len(slots)), file=say)
            print("  dry run: nothing minted, run or told. `%s %s --apply` "
                  "runs the bisect and ejects the culprit." % (
                      PROG, train["room"]), file=say)
            return done(0)
        ctx = dict(_context(say, train, red, be, known, shut, busy, environ,
                            fab, result), slots=slots)
        verdict = bisect(ctx)
    if verdict["kind"] == EJECT:
        status, why = trunk_check(ctx, red.get("host"))
        print("  trunk on the red's own host: %s — %s" % (status, why),
              file=say)
        if status == GREEN:
            verdict["why"] += "; %s" % why
        else:
            verdict = {"kind": TRUNK_RED if status == RED else UNKNOWN,
                       "by": verdict["by"], "car": None, "alone": None,
                       "clash": [], "why": why, "spared": verdict["car"]}
    ctx["runlog"] = write_runlog(train, red, ctx["outputs"])
    result["runlog"] = ctx["runlog"]
    result["verdict"] = dict(verdict, car=_car_json(verdict["car"]),
                             clash=[_car_json(c) for c in verdict["clash"]],
                             **({"spared": _car_json(verdict["spared"])}
                                if verdict.get("spared") else {}))
    kind = verdict["kind"]
    if kind == TRUNK_RED and verdict.get("spared"):
        print("  TRUNK-RED — %s. Nothing is ejected (%s is spared): the "
              "failure is on trunk %s or on that host, not in any car; a "
              "composition cure is owed on trunk, or the host is at fault "
              "and the same tree gated on another host tells which."
              % (verdict["why"], _car_words(verdict["spared"]),
                 _short(train["trunk"])), file=say)
        return done(0)
    if kind == TRUNK_RED:
        print("  TRUNK-RED — %s: the failure is on trunk %s, not in any car. "
              "Nothing is ejected; a composition cure is owed on trunk."
              % (verdict["why"], _short(train["trunk"])), file=say)
        return done(0)
    if kind == FLAKE:
        # BEFORE the print: the canary reads this store, and a flake that was
        # never written still vetoes a later sliced-only miss of the same test.
        # A write that fails is said out loud and changes nothing else: a flake
        # ejects no car either way.
        _row, err = landwindow.record_flake(train["root"], {
            "tree": red.get("tree"), "gate": red["id"],
            "train": train["train"],
            "tests": [t["id"] for t in red["tests"]],
            "why": verdict["why"]})
        if err:
            print("  FLAKE NOT RECORDED — %s. The canary cannot explain a later "
                  "divergence on these tests." % err, file=say)
        print("  FLAKE — %s. Nothing is ejected. Re-gate it: %s" % (
            verdict["why"], gatewindow.relaunch(
                {"room": train["room"], "label": train["train"]})), file=say)
        return done(0)
    if kind != EJECT:
        return refuse("the verdict is UNKNOWN (%s), so nothing is ejected"
                      % verdict["why"])
    print("  EJECT %s — %s" % (_car_words(verdict["car"]), verdict["why"]),
          file=say)
    if verdict["alone"] == GREEN:
        print("  clashed with: %s" % _clash_words(verdict), file=say)
    return done(eject(ctx, verdict, door=doorkw, tell=tell, project=project,
                      snapshot=snapshot))


def cmd(args):
    """helm train blame <train-room> [--gate gate:<id>] [--apply] [--json]"""
    from .cli import guard_tail
    args = list(args or ())
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not args or args[0].startswith("-"):
        print("%s: name the train's room (%s)" % (PROG, USAGE),
              file=sys.stderr)
        return 2
    room, rest = args[0], args[1:]
    rc = guard_tail(PROG, rest, flags=("--apply", "--json"),
                    valued=("--gate",), usage=USAGE)
    if rc is not None:
        return rc
    if not os.path.isdir(room):
        # A USAGE ERROR, ON STDERR, like every dispatcher's unknown token.
        print("%s: no such room: %s (%s)" % (PROG, room, USAGE),
              file=sys.stderr)
        return 2
    token = rest[rest.index("--gate") + 1] if "--gate" in rest else None
    return blame(room, gate=token, apply="--apply" in rest,
                 as_json="--json" in rest)
