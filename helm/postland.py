#!/usr/bin/env python3
"""THE FIRST LEDGER READ AFTER EVERY LAND, TIMED AND RECORDED (task/3538).

THE AXIS HAD NO INSTRUMENT. The Ledger reads bar is "post-land warm read under
30 s, measured on a real land", and no post-land read was recorded anywhere:
each earlier figure came from a hand script that timed `helm dispatch list`
after a pull, and it stopped being run. A bar nobody measures is scored from a
proxy.

WHAT IS RECORDED. The read `helm dispatch list` makes (`dispatches.snapshot`),
timed, with whether it restored the fold checkpoint (WARM) or replayed the
ledger (COLD, and the miss that said why: `foldckpt.reads`), the box's load
and core count, the landed head and who took it. One JSON line per landed
head, beside the fold checkpoint's own miss log (`log_path`), so the store
that explains a slow read and the record of it sit together.

WHO TAKES IT. Both land paths: `helm lr foldcheck <head> --apply` once its
rungs pass (a head they refuse has not landed), and auto-land right after its fast-forward (`autoland.Ops.postland`).
Each goes through `take`, which starts the LANDED CHECKOUT'S OWN `bin/helm lr
postland --record <head>` as a new process: that process imports the tree the
land put there, and the one that asked may not have (auto-land's tick
imported the tree its push replaced, and a hand foldcheck may be any helm).

THREE RULES, each a way the figure could lie:

  * THE CODE THAT RAN IS THE HEAD'S (`record`, `_code_at`). A read is timed
    only in a process whose own helm checkout stands AT the head, compared as
    a whole commit id, with nothing under its package or entry script that
    differs from that commit, tracked or not. It counts only when that still
    holds after the read and the package on disk is still the one the process
    imported (`foldckpt.policy`). Otherwise the row says which, and has no
    seconds.
  * ONE ROW PER HEAD. A row is keyed by the head's FULL commit id: an
    abbreviation is resolved first, and a row keyed any other way stands for
    no head. Whether the head has a row is asked again under the log's lock
    at the append (`_append_once`), so two recorders racing on one head leave
    one row, and the second answers the first's.
  * A READ THAT DID NOT HAPPEN IS NEVER A TIMING. A read that was not made
    against the head's code, found the ledger unavailable, raised, did not
    finish in TIMEOUT_S, or left no row is UNMEASURED with its reason, and
    `summary` counts MEASURED rows only.

AN INSTRUMENT NEVER STOPS A LAND. `take` and `record` never raise; a failure
is a row that says so, and `take` says one line on stderr.

`helm lr postland` prints the rows of a window with the median and max of the
measured ones against the 30 s bar; `--json` gives the same as data.
"""
import fcntl
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time

from . import dispatches, foldckpt, pk, vcs

BAR_S = 30.0
LOG_CAP = 256 * 1024
#: How long the recorder's process may take (`take`); a cold replay of the
#: live ledger measured 45-140 s.
TIMEOUT_S = 600
#: how long a record waits for the log's lock before it gives up and says so:
#: a recorder that holds it longer must never stall the land behind it.
LOCK_WAIT_S = 10.0
#: the recorder's exit when the log's lock stayed held: the parent that
#: started it then records nothing and waits no second time.
EXIT_LOCK_HELD = 75                      # EX_TEMPFAIL: try again later


class LockHeld(Exception):
    """The post-land log's lock stayed held past LOCK_WAIT_S."""


#: what `record` answers, and `_run` passes on, for a lock that stayed held
LOCK_HELD = "lock-held"
MEASURED, UNMEASURED = "measured", "unmeasured"
WARM, COLD = "warm", "cold"
#: The code a helm checkout runs: its package and its entry script.
CODE_PATHS = ("helm", "bin")
USAGE = ("usage: helm lr postland [--hours N] [--json] | "
         "postland --record <head> [--by NAME] [--repo PATH]")
_FULL = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REASON_CAP = 300


def log_path(ledger):
    """Beside the fold checkpoint's miss log (`foldckpt.misses_path`)."""
    return os.path.join(os.path.dirname(foldckpt.store_dir(ledger)),
                        foldckpt.ledger_key(ledger) + ".postland.jsonl")


def _lock_path(ledger):
    return os.path.join(os.path.dirname(foldckpt.store_dir(ledger)),
                        foldckpt.ledger_key(ledger) + ".postland.lock")


def rows(ledger, since=None):
    """[row] oldest first, from the log and its one rolled generation: the
    FIRST row of each head, and only rows keyed by a full commit id; only
    those at or after epoch seconds `since` when it is given. A log that is
    there and cannot be read raises OSError: the rows it hides are not no
    rows."""
    out, seen = [], set()
    for path in (log_path(ledger) + ".1", log_path(ledger)):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            continue
        for line in data.splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            head = row.get("head") if isinstance(row, dict) else None
            if not isinstance(head, str) or not _FULL.match(head) \
                    or head in seen:
                continue
            seen.add(head)
            if since is None or (_number(row.get("at"))
                                 and row["at"] >= since):
                out.append(row)
    return out


def row_for(ledger, head):
    """The row that stands for `head`, a full commit id, or None."""
    return next((r for r in rows(ledger) if r["head"] == head), None)


def _locked(fd, wait):
    """Take `fd`'s exclusive lock, polling for at most `wait` seconds. ->
    True once held, False when another holder kept it the whole time."""
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _append_once(ledger, row, wait=None):
    """Append `row` unless its head already has one. -> the row that stands
    for the head: `row`, or the one another recorder appended first. Raises
    LockHeld when the log's lock stays held past LOCK_WAIT_S, so no caller
    waits on another holder longer than that. The check and the append are
    made under one lock."""
    path = log_path(ledger)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(_lock_path(ledger), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                 0o600)
    try:
        wait = LOCK_WAIT_S if wait is None else wait
        if not _locked(fd, wait):
            raise LockHeld("the log's lock %s stayed held for %g s"
                           % (_lock_path(ledger), wait))
        have = row_for(ledger, row["head"])
        if have is not None:
            return have
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            size = 0
        if size + len(line) > LOG_CAP:
            os.replace(path, path + ".1")
        out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                      | os.O_CLOEXEC, 0o600)
        try:
            os.write(out, line)
        finally:
            os.close(out)
        return row
    finally:
        os.close(fd)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _git(root, *args):
    """(rc, out) for one git question about the checkout at `root`, through
    the seam, with the repository-selecting variables removed
    (`selfrepo._git_env`) and no index refresh written by a status."""
    from . import selfrepo
    env = dict(selfrepo._git_env(), GIT_OPTIONAL_LOCKS="0")
    rc, out, _err = vcs.backend(root).text(root, *args, timeout=60, env=env)
    return rc, out


def _toplevel(repo):
    rc, out = _git(repo, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else None


def _full(root, head):
    """`head` as a full commit id: what the checkout at `root` resolves it
    to, or `head` itself when it is already one. None when neither holds: a
    row keyed by a partial id could stand for two heads."""
    head = str(head or "").strip()
    if root and head and not head.startswith("-"):
        rc, out = _git(root, "rev-parse", "--verify", "-q",
                       head + "^{commit}")
        if rc == 0 and _FULL.match(out):
            return out
    return head.lower() if _FULL.match(head.lower()) else None


def _code_root():
    """The checkout this process's helm package was imported from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_at(head):
    """Why the code this process's checkout holds is not `head`'s tree, or
    None when it is: the checkout stands at `head`, and nothing under
    CODE_PATHS differs from that commit, tracked or not."""
    root = _code_root()
    rc, out = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if rc != 0 or not _FULL.match(out):
        return "the running helm's checkout %s has no HEAD git can read" % root
    if out != head:
        return ("the running helm's checkout is at %s, not the landed head"
                % out)
    rc, out = _git(root, "status", "--porcelain", "--untracked-files=normal",
                   "--", *CODE_PATHS)
    if rc != 0:
        return ("git could not read the status of the running helm's "
                "checkout %s" % root)
    if out:
        return ("the running helm's checkout has uncommitted changes to its "
                "code (%s)" % out.splitlines()[0].strip())
    return None


def _load():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _row(head, by, repo, seconds=None, road=None, why=None, reason=None,
         load=None):
    """One row: MEASURED with its seconds when there is no `reason`, else
    UNMEASURED with the reason and no seconds."""
    measured = reason is None
    return {"at": time.time(), "ts": pk.now_ts(), "head": head,
            "by": str(by or "hand"),
            "repo": os.path.abspath(repo) if repo else None,
            "pid": os.getpid(),
            "status": MEASURED if measured else UNMEASURED,
            "seconds": round(seconds, 3) if measured else None,
            "road": road if measured else None,
            "why": pk.cut_marked(why, _REASON_CAP) if why else None,
            "reason": None if measured
            else pk.cut_marked(reason, _REASON_CAP),
            "load": load if load is not None else _load(),
            "cpus": os.cpu_count()}


def _timed_read(head, by, repo, clock):
    """The row for this process's read of the dispatch ledger, timed by
    `clock`, once `_code_at(head)` said the checkout is the head's."""
    load = _load()
    with foldckpt.reads() as seen:
        start = clock()
        try:
            _out, unavailable = dispatches.snapshot()
        except Exception as exc:                         # noqa: BLE001
            return _row(head, by, repo, reason="the read raised %s: %s"
                        % (type(exc).__name__, exc))
        seconds = clock() - start
    if unavailable:
        return _row(head, by, repo, reason="the read found the ledger "
                    "unavailable: %s" % unavailable)
    foldckpt._POLICY["checked"] = None
    if foldckpt.policy() is None:
        return _row(head, by, repo, reason="this process's helm changed on "
                    "disk since it started, so the code that read is not "
                    "the checkout's")
    moved = _code_at(head)
    if moved:
        return _row(head, by, repo, reason="the checkout moved during the "
                    "read: " + moved)
    warm = bool(seen) and all(road == foldckpt.RESTORED for road, _w in seen)
    why = None if warm else next(
        (w for road, w in seen if road != foldckpt.RESTORED and w),
        "the read touched no checkpoint")
    return _row(head, by, repo, seconds=seconds, road=WARM if warm else COLD,
                why=why, load=load)


def record(head, by, repo=None, clock=None):
    """Time THIS process's read of the dispatch ledger as the first after the
    land of `head` into the checkout at `repo`, and append its row once per
    head. -> the row that stands for the head (this one, or the one already
    there); LOCK_HELD when the log's lock stayed held past LOCK_WAIT_S (the
    verb then exits EXIT_LOCK_HELD, so the parent waits no second time); or
    None when none can be written: `head` names no commit this helm's
    checkout can spell whole, or the log cannot be read or written.

    Never raises: an instrument that fails is a missing row, never a failed
    land."""
    try:
        full = _full(_code_root(), head)
        if full is None:
            return None
        ledger = dispatches.ledger_path()
        have = row_for(ledger, full)
        if have is not None:
            return have
        why = _code_at(full)
        row = _row(full, by, repo, reason=why) if why else \
            _timed_read(full, by, repo, clock or time.monotonic)
        return _append_once(ledger, row)
    except LockHeld:
        return LOCK_HELD
    except Exception:                                    # noqa: BLE001
        return None


def _last_line(data):
    lines = [ln.strip() for ln in os.fsdecode(data or b"").splitlines()]
    return next((ln for ln in reversed(lines) if ln), None)


def _start(argv, cwd, timeout):
    """The recorder's process: `argv` run in `cwd`, waited for at most
    `timeout` seconds, its output kept."""
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout)


def _run(root, head, by, timeout):
    """Start `root`'s own helm to record `head`'s read, and wait for it. ->
    what to record when it leaves no row."""
    helm = os.path.join(root, "bin", "helm") if root else None
    if helm is None or not (os.path.isfile(helm) and os.path.isfile(
            os.path.join(root, "helm", "postland.py"))):
        return ("the landed checkout %s carries no post-land recorder "
                "(bin/helm and helm/postland.py)"
                % (root or "(no git work tree)"))
    try:
        done = _start([sys.executable, helm, "lr", "postland", "--record",
                       head, "--by", by, "--repo", root], root, timeout)
    except subprocess.TimeoutExpired:
        return "the read did not finish in %d s" % timeout
    except (OSError, subprocess.SubprocessError) as exc:
        return "the recorder could not start: %s" % exc
    if done.returncode == EXIT_LOCK_HELD:
        return LOCK_HELD
    said = _last_line(done.stderr) or _last_line(done.stdout) or "no output"
    if done.returncode != 0:
        return "the recorder exited %d: %s" % (done.returncode, said)
    return "the recorder exited 0 and left no row: %s" % said


def take(repo, head, by, timeout=TIMEOUT_S):
    """The land's first ledger read, for the land of `head` into the checkout
    at `repo`, timed in a NEW process of that checkout's own helm (`record`,
    through `helm lr postland --record`). -> the row that stands for the
    head, or None when none could be written. Starts nothing when the head
    already has its row.

    Never raises, and says one line on stderr: a recorder that fails is a
    row that says why, never a stopped land."""
    full = None
    try:
        root = _toplevel(repo)
        full = _full(root, head)
        if full is None:
            _say("nothing recorded: %s names no commit %s can spell whole"
                 % (head, root or repo))
            return None
        ledger = dispatches.ledger_path()
        row = row_for(ledger, full)
        if row is None:
            why = _run(root, full, by, timeout)
            if why is LOCK_HELD:
                _say("nothing recorded for %s: the recorder found the log's "
                     "lock held for %g s" % (full, LOCK_WAIT_S))
                return None
            row = row_for(ledger, full) or _append_once(
                ledger, _row(full, by, root or repo, reason=why))
        _say(_line(row))
        return row
    except LockHeld as exc:
        _say("nothing recorded for %s: %s" % (full or head, exc))
        return None
    except Exception as exc:                             # noqa: BLE001
        why = "the recorder failed: %s: %s" % (type(exc).__name__, exc)
        row = None
        if full is not None:
            try:
                row = _append_once(dispatches.ledger_path(),
                                   _row(full, by, repo, reason=why))
            except Exception:                            # noqa: BLE001
                row = None
        _say(_line(row) if row else "nothing recorded for %s: %s"
             % (head, why))
        return row


def _say(text):
    try:
        print("helm lr postland: " + text, file=sys.stderr)
    except Exception:                                    # noqa: BLE001
        pass


def _timed(row):
    """Is `row` a timing: MEASURED, with a finite, non-negative number?"""
    return row.get("status") == MEASURED and _number(row.get("seconds")) \
        and row["seconds"] >= 0


def summary(got):
    """The figures over rows `got`: the MEASURED rows' median and max, how
    many of them were COLD and how many under the bar; every other row is
    counted UNMEASURED and nothing else."""
    secs = [float(r["seconds"]) for r in got if _timed(r)]
    return {"rows": len(got), "measured": len(secs),
            "unmeasured": len(got) - len(secs),
            "median_s": round(statistics.median(secs), 3) if secs else None,
            "max_s": round(max(secs), 3) if secs else None,
            "cold": sum(1 for r in got if _timed(r) and r.get("road") != WARM),
            "under_bar": sum(1 for s in secs if s < BAR_S), "bar_s": BAR_S}


def _line(row):
    ts, head = row.get("ts") or "?", str(row.get("head") or "?")[:12]
    by = row.get("by") or "?"
    if not _timed(row):
        return "%s  %s  UNMEASURED  by %s  (%s)" % (
            ts, head, by, row.get("reason") or "no reason recorded")
    load = row.get("load")
    return "%s  %s  %-4s %7.1f s  load %s/%s  by %s%s" % (
        ts, head, row.get("road") or "?", float(row["seconds"]),
        load[0] if isinstance(load, list) and load else "?",
        row.get("cpus") or "?", by,
        ("  (%s)" % row["why"]) if row.get("why") else "")


def cmd(args):
    """helm lr postland [--hours N] [--json] |
    helm lr postland --record <head> [--by NAME] [--repo PATH]"""
    from .cli import guard_tail
    args = list(args or ())
    if args[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if args[:1] == ["--record"]:
        if len(args) < 2 or args[1].startswith("-"):
            print(USAGE, file=sys.stderr)
            return 2
        rc = guard_tail("helm lr postland --record", args[2:],
                        valued=("--by", "--repo"), usage=USAGE)
        if rc is not None:
            return rc
        rest = args[2:]
        by = rest[rest.index("--by") + 1] if "--by" in rest else "hand"
        repo = rest[rest.index("--repo") + 1] if "--repo" in rest else None
        row = record(args[1], by, repo=repo)
        if row is LOCK_HELD:
            print("helm lr postland: nothing recorded for %s: the log's lock "
                  "stayed held for %g s" % (args[1], LOCK_WAIT_S),
                  file=sys.stderr)
            return EXIT_LOCK_HELD
        if row is None:
            print("helm lr postland: nothing recorded for %s: it names no "
                  "commit this helm's checkout can spell whole, or the log "
                  "could not be written" % args[1], file=sys.stderr)
            return 1
        print("helm lr postland: " + _line(row))
        return 0
    rc = guard_tail("helm lr postland", args, flags=("--json",),
                    valued=("--hours",), usage=USAGE)
    if rc is not None:
        return rc
    since = None
    if "--hours" in args:
        try:
            hours = float(args[args.index("--hours") + 1])
        except ValueError:
            hours = None
        if hours is None or not math.isfinite(hours) or hours <= 0:
            print(USAGE, file=sys.stderr)
            return 2
        since = time.time() - hours * 3600
    try:
        got = rows(dispatches.ledger_path(), since)
    except OSError as exc:
        print("helm lr postland: the post-land log cannot be read (%s)" % exc,
              file=sys.stderr)
        return 1
    total = summary(got)
    if "--json" in args:
        print(json.dumps({"rows": got, "summary": total}, indent=1,
                         sort_keys=True))
        return 0
    if not got:
        print("helm lr postland: no post-land read recorded%s"
              % (" in the last %s h" % args[args.index("--hours") + 1]
                 if since is not None else ""))
        return 0
    text = "%d land(s): " % total["rows"]
    if total["measured"]:
        text += ("%d measured, median %.1f s, max %.1f s, %d cold, %d of %d "
                 "under the %.0f s bar" % (
                     total["measured"], total["median_s"], total["max_s"],
                     total["cold"], total["under_bar"], total["measured"],
                     BAR_S))
    else:
        text += "none measured"
    if total["unmeasured"]:
        text += "; %d UNMEASURED" % total["unmeasured"]
    print("helm lr postland: " + text)
    for row in got:
        print("  " + _line(row))
    return 0
