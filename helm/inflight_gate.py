#!/usr/bin/env python3
"""Pre-commit rung: REFUSE a commit while THIS room's gate is running.

A commit inside a gate's bracket moves HEAD under a suite whose receipt has
already recorded the OLD head, so the receipt describes no single tree and
`gate bind` refuses it afterward — the run is spent and the lane needs a fresh
one. Three seats hit that inside one hour: a kimi seat committed r2
while r1 was mid-run, and I killed two of my own gates mid-amend to avoid
minting the same thing. The bracket rule catches it AFTER. This is the BEFORE.

ROOM-SCOPED BY CONSTRUCTION, which is why it reads a marker instead of the
lock. `gatelock:<project>` records the HOLDER and never the REPO — measured
2026-08-03, the claim carries holder/session/lease/fence/exp and nothing else —
so a guard built on the lock would refuse commits in EVERY room whenever ANY
seat gated ANYWHERE. The marker lives in the room it describes.

SELF-CONTAINED, STDLIB ONLY, AND THAT IS THE LAW RATHER THAN A PREFERENCE.
Every sibling rung `_scanner_assets` snapshots — nevertrack, vacuous_assertion,
hardcode, conflict_marker, hostpath_guard — imports nothing from helm, and this
one is the only one that ever did. Two consequences of that import, both
measured by a review of 469e4b8: from the snapshot's own directory
`import helm` simply FAILS, so the rung was inert everywhere it was installed;
and baking the installer's source path into the hook to fix that made the guard
DIE WITH THE TREE IT WAS INSTALLED FROM — install from a lane, drop the lane,
and a live marker returns rc0. A snapshot that needs a source tree is not a
snapshot. So this file duplicates a small, stable read (resolve the git dir,
list live owners) rather than reaching for helm.gate, and tests/test_inflight_gate
pins the two readers against each other so the duplication cannot drift.

REFUSES RATHER THAN WARNS, because there is no legitimate commit here: any
commit in this window voids the receipt, so a warning would only document the
loss. The escape is real and cheap — let the gate finish, or kill it — and it
is exactly what the seats who hit this did by hand.
"""
import json
import os
import stat
import subprocess
import sys

INFLIGHT = ".helm-gate-in-flight"


def _marker(path):
    """One marker's JSON, opened without blocking. `open()` on a FIFO with no
    writer never returns, so a FIFO where a marker belongs hung every commit
    in the room (task/2543). The same door as `helm.pk.open_regular`, written
    out here because this snapshot imports nothing from helm (see above); a
    file that is not regular raises OSError, which both callers skip."""
    with open(path, encoding="utf-8",
              opener=lambda p, flags: os.open(p, flags | os.O_NONBLOCK)) as fh:
        mode = os.fstat(fh.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise OSError("%s is not a regular file (%s)"
                          % (path, stat.filemode(mode)))
        return json.load(fh)


def _git_dir(root):
    """The room's REAL admin dir, or None.

    NOT `<root>/.git`: in a lane worktree — which is every room this guard
    exists for — that is a FILE holding `gitdir: ...`, and joining onto it
    raises NotADirectoryError. Dogfooding caught exactly that on 2026-08-03
    while seven unit tests stayed green, because their fixture built `.git` as
    a real directory and could not express the state the bug lives in.

    `--git-dir` is PER-WORKTREE; `--git-common-dir` is shared by every worktree
    and would make this marker project-wide, the precise property it must not
    have."""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    gitdir = (out.stdout or "").strip()
    if not gitdir:
        return None
    return gitdir if os.path.isabs(gitdir) else os.path.join(root, gitdir)


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _proc_start(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="utf-8") as fh:
            stat = fh.read()
    except (OSError, ValueError, TypeError):
        return None
    # comm (field 2) may contain spaces and parentheses — split after the LAST
    # ')' or a process named "a b) c" makes every field after it lie.
    tail = stat.rpartition(")")[2].split()
    try:
        return tail[19]
    except IndexError:
        return None


def _owner_live(row):
    """A LIVE PID IS NOT A LIVE GATE: a marker left by a crash or a reboot
    names a pid the kernel has since reissued, so os.kill reports a STRANGER
    alive. (boot, start) must still agree. Markers lacking them fall back to
    the bare check, so an upgrade cannot disarm an in-flight gate."""
    try:
        pid = int(row.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    boot, start = row.get("boot"), row.get("start")
    if boot is not None and boot != _boot_id():
        return None
    if start is not None and start != _proc_start(pid):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass                      # alive, owned by another uid
    except OSError:
        return None
    return pid


def inflight(root):
    """(pid, ts) for a LIVE gate in this room, or None.

    ONE FILE PER OWNER, so two overlapping gates cannot erase each other; the
    OLDEST live owner is reported so the answer does not flap. The legacy
    single file is still honoured — a gate already running when the directory
    scheme landed wrote one, and calling that absent would un-guard the exact
    window this rung exists to close."""
    gitdir = _git_dir(root)
    if not gitdir:
        return None
    live = []
    d = os.path.join(gitdir, INFLIGHT + ".d")
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            row = _marker(os.path.join(d, name))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(row, dict) and _owner_live(row):
            live.append(row)
    try:
        row = _marker(os.path.join(gitdir, INFLIGHT))
        if isinstance(row, dict) and _owner_live(row):
            live.append(row)
    except (OSError, ValueError, TypeError):
        pass
    if not live:
        return None
    oldest = min(live, key=lambda r: str(r.get("ts") or ""))
    return int(oldest.get("pid") or 0), str(oldest.get("ts") or "")


def main(argv=None):
    root = os.environ.get("GIT_WORK_TREE") or os.getcwd()
    live = inflight(root)
    if not live:
        return 0
    pid, ts = live
    sys.stderr.write(
        "[helm in-flight-gate] REFUSED: a gate is RUNNING in this room "
        "(pid %s, started %s).\n" % (pid, ts or "unknown"))
    sys.stderr.write(
        "[helm in-flight-gate] Committing now moves HEAD under that run: the "
        "receipt already recorded the OLD head, so it will bracket a moving "
        "tree and `gate bind` will REFUSE it. The run is spent either way — "
        "this only decides whether you find out now or at land time.\n")
    sys.stderr.write(
        "[helm in-flight-gate] Let it finish, or kill pid %s and re-gate "
        "after committing. Override for a commit you know is outside the "
        "bracket: HELM_INFLIGHT_GATE_SKIP=1\n" % pid)
    return 1


if __name__ == "__main__":
    sys.exit(main())
