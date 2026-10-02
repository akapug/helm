"""The scratch archive: RAM scratch leaves for DISK, never for nowhere.

THE INCIDENT (task/4183). The reaper in helm/scratch.py removed a live seat's
aged, unheld scratchpad unit (answer keys, review reports, scripts, a page the
owner had been sent) under host-memory pressure, with os.unlink and
shutil.rmtree, and wrote no record of what it removed. The work came back
only from other databases. The owner's model was that whatever sits in RAM is
mirrored to disk, so nothing in it can be lost. The host has terabytes of disk;
it has gigabytes of RAM. So the reaper's job is to move scratch OFF RAM, not
to destroy it.

THE CONTRACT, per unit the reaper has already decided may leave RAM:

  1. COPY the unit to the archive: <root>/<session>/<UTC stamp>-<name>.
     <root> is HELM_SCRATCH_ARCHIVE, else <helm global dir>/scratch-evicted.
     A unit already on the archive's filesystem is RENAMED instead: a copy
     there would put its bytes on the disk the pass is relieving twice.
  2. VERIFY: the file count, directory count and total bytes of the copy
     must equal the source's, read before AND after the copy (a write during
     the copy fails the check). A copy that fails is discarded and the RAM
     copy is KEPT, and the pass says so. Copied files are fsync'd before the
     RAM copy goes.
  3. RECORD one row in <root>/index.jsonl BEFORE the RAM copy is removed:
     {event: evicted, path, session, tier, reason, bytes, files, ts,
     archive_path}. A removal that then fails appends `remove_failed`.
  4. Only then does the caller remove the RAM copy, with the remover it has
     always used.

THE ARCHIVE NEVER PRUNES ITSELF. The pass evicts to disk and deletes nothing,
the archive included: when the archive filesystem is under its free floor
(HELM_SCRATCH_ARCHIVE_FREE_PCT of it, default FREE_FLOOR_PCT), a unit that
would have to be copied there STAYS IN RAM and the pass says so loudly. Space
in the archive is the operator's to free.

AN ARCHIVE IN RAM IS NO ARCHIVE. A root on tmpfs or ramfs (read from the
longest mount-point prefix in /proc/mounts) would free nothing and survive no
reboot, and a unit on that same mount would be "evicted" by a rename that
leaves every byte in RAM. Such a root is refused as unavailable: every unit
stays in RAM, and the pass names the root and its filesystem type.

THE BOTTLENECK. Copying out of tmpfs costs I/O and time, and the pass runs
exactly when memory is short. So a pass copies at most PASS_BYTES
(HELM_SCRATCH_EVICT_PASS_BYTES), biggest-by-inodes first, and a unit that
would put the archive filesystem under its floor STAYS IN RAM with one line
saying so. Only a copy spends that allowance: a unit on the archive's own
filesystem is one rename, so it goes even after the copies have spent it.
There is no delete fallback: a unit that cannot be archived is kept.

A SPECIAL FILE IS NEVER A UNIT TO COPY. A unit that is itself a FIFO, a
socket or a device holds no data, and opening one to copy it blocks (a FIFO)
or reads without end (a character device). It is refused before anything
opens it, and it stays where it is.

`helm scratch evicted [--session S] [--json]` lists the index and `helm
scratch restore <path-or-archive-path> [--force]` copies the newest archived
copy back. A restore that fails removes its partial copy and puts back the
copy --force moved aside.
"""
import json
import os
import re
import shutil
import stat
import time

from . import home, pk, projscope

ARCHIVE_ENV = "SCRATCH_ARCHIVE"                 # HELM_SCRATCH_ARCHIVE
FREE_PCT_ENV = "SCRATCH_ARCHIVE_FREE_PCT"       # HELM_SCRATCH_ARCHIVE_FREE_PCT
PASS_BYTES_ENV = "SCRATCH_EVICT_PASS_BYTES"     # HELM_SCRATCH_EVICT_PASS_BYTES
FREE_FLOOR_PCT = 5.0
PASS_BYTES = 4 * 1024 ** 3
INDEX = "index.jsonl"
LEAF = "scratch-evicted"

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_disk_usage = shutil.disk_usage     # a seam: tests fake the archive disk


def archive_root():
    """Where evicted scratch goes: the operator's path, else the global dir."""
    override = home.env(ARCHIVE_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(home.global_dir(), LEAF)


def _number(name, default):
    try:
        v = float(home.env(name) or default)
    except ValueError:
        return default
    return v if v >= 0 else default


def free_floor_pct():
    return _number(FREE_PCT_ENV, FREE_FLOOR_PCT)


def pass_bytes():
    """The bytes one gc pass may copy out of RAM."""
    return int(_number(PASS_BYTES_ENV, PASS_BYTES))


def _floor(usage):
    return usage.total * free_floor_pct() / 100.0


def _human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" % (n, unit)) if unit == "B" else \
                "%.1f%s" % (n, unit)
        n /= 1024.0


def measure(path):
    """{files, dirs, bytes, special} of a unit, never following a link.
    `files` counts regular files and symlinks; `bytes` is regular-file
    content. Sockets, FIFOs and devices are counted in `special`: they hold
    no data and are not copied."""
    out = {"files": 0, "dirs": 0, "bytes": 0, "special": 0}
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        _count(st, out)
        return out
    for base, dirs, files in os.walk(path, onerror=_unreadable):
        projscope.spend_or_raise("measuring scratch unit")
        for name in dirs + files:
            sub = os.lstat(os.path.join(base, name))
            if stat.S_ISDIR(sub.st_mode):
                out["dirs"] += 1
            else:
                _count(sub, out)
    return out


def _unreadable(exc):
    """A subtree the walk cannot list is not an empty one: raise, and the
    unit stays in RAM."""
    raise exc


def _count(st, out):
    if stat.S_ISREG(st.st_mode):
        out["files"] += 1
        out["bytes"] += st.st_size
    elif stat.S_ISLNK(st.st_mode):
        out["files"] += 1
    else:
        out["special"] += 1


def _volatile_fs(root):
    """The RAM-backed filesystem type under `root` (tmpfs, ramfs), else None,
    read from the longest mount-point prefix of its nearest existing
    ancestor. An unreadable mount table reads None: the archive is then
    judged by the floor alone."""
    from . import scratch
    probe = os.path.abspath(root)
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    _mp, fstype, _opts = scratch._mount_of(probe, scratch.mount_table())
    return fstype if fstype in scratch.VOLATILE_FS else None


def unavailable(root):
    """Why `root` cannot hold evicted scratch, or None."""
    fstype = _volatile_fs(root)
    if not fstype:
        return None
    return ("the archive root %s is on %s, which is RAM: evicting there frees "
            "nothing and survives no reboot (point HELM_SCRATCH_ARCHIVE at a "
            "disk path)" % (root, fstype))


def _same_fs(path, root):
    return os.lstat(path).st_dev == os.stat(root).st_dev


_SPECIAL = ((stat.S_ISFIFO, "FIFO"), (stat.S_ISSOCK, "socket"),
            (stat.S_ISCHR, "character device"), (stat.S_ISBLK, "block device"))


def special_kind(mode):
    """What kind of special file a mode is, or None for a directory, a
    regular file or a symlink: the three kinds a unit can be copied as."""
    if stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        return None
    return next((name for test, name in _SPECIAL if test(mode)),
                "special file")


def _copy_file(src, dst):
    """One file into the archive, durable before the RAM copy can go."""
    projscope.spend_or_raise("archiving scratch file")
    shutil.copy2(src, dst, follow_symlinks=False)
    if not os.path.islink(dst):
        fd = os.open(dst, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _skip_special(base, names):
    out = []
    for name in names:
        mode = os.lstat(os.path.join(base, name)).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)
                or stat.S_ISLNK(mode)):
            out.append(name)
    return out


def _copy(src, dst):
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True, ignore=_skip_special,
                        copy_function=_copy_file)
    else:
        _copy_file(src, dst)


def _stamp(now=None):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _dest(root, session, path):
    base = os.path.join(root, _SAFE.sub("_", session or "") or "unknown")
    name = "%s-%s" % (_stamp(), _SAFE.sub("_", os.path.basename(
        path.rstrip("/"))) or "unit")
    dest, n = os.path.join(base, name), 1
    while os.path.lexists(dest):
        dest, n = os.path.join(base, "%s.%d" % (name, n)), n + 1
    return dest


def discard(path):
    """Remove an archive-side copy this module made. True when it is gone."""
    if os.path.isdir(path) and not os.path.islink(path):
        from . import scratch
        scratch.reap_owned(path)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass
    return not os.path.lexists(path)


def _failure(exc):
    """What failed, readable: shutil.Error carries one tuple per file, and
    its str() is a list repr whose first path pushes the cause off a line."""
    errs = exc.args[0] if isinstance(exc, shutil.Error) and exc.args else None
    if isinstance(errs, list) and errs and len(errs[0]) == 3:
        return "%d file%s failed; first %s: %s" % (
            len(errs), "s"[:len(errs) != 1], errs[0][0], errs[0][2])
    return str(exc) or exc.__class__.__name__


def _seen(m):
    return (m["files"], m["dirs"], m["bytes"])


def archive(path, session, root=None, no_copy=None):
    """Prepare one unit's eviction -> {archive_path, files, dirs, bytes,
    special, moved, why}.

    `why` set means the unit must STAY IN RAM, and says why; `blocked` then
    says whether the archive itself refused (`volatile`, or `full`: the
    filesystem is already under its free floor), which no later copy of the
    pass can cure. A unit merely too big for the room left above the floor
    is kept with no `blocked`: a smaller unit after it may still fit.
    `moved` means the archive is on the unit's own filesystem: nothing was
    copied, and the caller's removal step is `move(path, archive_path)`. Otherwise the copy
    exists and verified, and the caller records it and then removes the RAM
    copy. Nothing here removes the source.

    `no_copy`, when set, is why this unit may not be COPIED (the pass's copy
    allowance is spent): a unit that would need a copy is kept with that
    reason, and a unit that needs only a rename still goes."""
    root = root or archive_root()
    out = {"archive_path": None, "moved": False, "why": None,
           "blocked": None, "files": 0, "dirs": 0, "bytes": 0, "special": 0}
    bad = unavailable(root)
    if bad:
        out["why"], out["blocked"] = bad + ", so it stays in RAM", "volatile"
        return out
    try:
        kind = special_kind(os.lstat(path).st_mode)
        if kind:
            out["why"] = ("%s is a %s, and a special file holds no data, so "
                          "it stays where it is" % (path, kind))
            return out
        os.makedirs(root, mode=0o700, exist_ok=True)
        moved = _same_fs(path, root)
        if no_copy and not moved:
            out["why"] = no_copy
            return out
        src = measure(path)
        out.update(src)
        out["archive_path"] = dest = _dest(root, session, path)
        if moved:
            out["moved"] = True
            return out
        usage = _disk_usage(root)
        if usage.free - src["bytes"] < _floor(usage):
            if usage.free < _floor(usage):
                out["blocked"] = "full"
            out["why"] = ("the archive filesystem (%s) has %s free, under its "
                          "%g%% free floor once this unit's %s land, so it "
                          "stays in RAM" % (root, _human(usage.free),
                                            free_floor_pct(),
                                            _human(src["bytes"])))
            return out
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    except OSError as exc:
        out["why"] = "the unit or the archive could not be read (%s), so " \
                     "it stays in RAM" % exc
        return out
    try:
        _copy(path, dest)
        got, again = measure(dest), measure(path)
    except projscope.Expired:
        discard(dest)
        raise
    except (OSError, shutil.Error) as exc:
        discard(dest)
        out["why"] = "the copy to the archive failed (%s), so it stays in " \
                     "RAM" % _failure(exc)
        return out
    if not _seen(src) == _seen(got) == _seen(again):
        discard(dest)
        out["why"] = ("the archive copy did not verify (RAM %d files, %d "
                      "dirs, %d bytes; copy %d, %d, %d; RAM after %d, %d, "
                      "%d), so it stays in RAM"
                      % (_seen(src) + _seen(got) + _seen(again)))
    return out


def move(path, dest):
    """The same-filesystem eviction: one rename into the archive."""
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    os.rename(path, dest)


def record(root, row):
    """Append one durable row to the archive index (fsync'd)."""
    row = dict(row)
    row.setdefault("epoch", time.time())
    row.setdefault("ts", pk.epoch_ts(row["epoch"]))
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(root, INDEX),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


def read_index(root=None):
    """Every index row, oldest first; a garbled line is skipped."""
    path = os.path.join(root or archive_root(), INDEX)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _failed(rows):
    """(path, archive_path) of every unit whose RAM removal failed."""
    return {(r.get("path"), r.get("archive_path")) for r in rows
            if r.get("event") == "remove_failed"}


def _never_moved(row, failed):
    """A same-filesystem unit whose rename failed: its row was written, but
    the archive path never received anything and the unit is still in RAM."""
    return bool(row.get("moved")) and \
        (row.get("path"), row.get("archive_path")) in failed


def held(rows):
    """The `evicted` rows whose archived copy is still on disk."""
    failed = _failed(rows)
    return [r for r in rows if r.get("event") == "evicted"
            and not _never_moved(r, failed)
            and os.path.lexists(r.get("archive_path") or "")]


def restore(target, force=False, root=None):
    """Copy the newest archived copy of `target` (its original path or its
    archive path) back to its original path -> (rc, message)."""
    root = root or archive_root()
    want = os.path.abspath(os.path.expanduser(target))
    rows = [r for r in held(read_index(root))
            if want in (r.get("path"), r.get("archive_path"))]
    if not rows:
        return 1, ("no archived copy of %s in %s (see `helm scratch evicted`)"
                   % (want, os.path.join(root, INDEX)))
    row = max(rows, key=lambda r: r.get("epoch", 0))
    src, dest = row["archive_path"], row["path"]
    aside = None
    if os.path.lexists(dest):
        if not force:
            return 1, ("%s exists; `helm scratch restore %s --force` moves it "
                       "aside first" % (dest, target))
        aside = "%s.pre-restore-%s" % (dest.rstrip("/"), _stamp())
        os.rename(dest, aside)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _copy(src, dest)
        same = _seen(measure(src)) == _seen(measure(dest))
    except projscope.Expired:
        _undo_restore(dest, aside)
        raise
    except (OSError, shutil.Error) as exc:
        return 1, "restore of %s failed (%s)%s" % (
            dest, _failure(exc), _undo_restore(dest, aside))
    if not same:
        return 1, "restored %s does not match its archived copy %s%s" % (
            dest, src, _undo_restore(dest, aside))
    record(root, {"event": "restored", "path": dest, "archive_path": src,
                  "session": row.get("session"), "bytes": row.get("bytes"),
                  "files": row.get("files"), "aside": aside})
    return 0, "restored %s from %s (%d files, %s)%s" % (
        dest, src, row.get("files") or 0, _human(row.get("bytes") or 0),
        "; the copy that was there is now %s" % aside if aside else "")


def _undo_restore(dest, aside):
    """A failed restore leaves dest as it found it: the partial copy goes and
    the copy --force moved aside comes back. Returns the sentence tail that
    says where everything is."""
    if not discard(dest):
        return ("; its partial copy at %s could not be removed%s" % (
            dest, ", and the copy that was there is %s" % aside
            if aside else ""))
    if not aside:
        return "; its partial copy was removed"
    try:
        os.rename(aside, dest)
    except OSError as exc:
        return ("; its partial copy was removed, and the copy that was there "
                "is still %s (it could not be put back: %s)" % (aside, exc))
    return "; its partial copy was removed and the original was put back"


def listing(session=None, root=None):
    """The `evicted` rows, newest first, each with its state: restored,
    archived with its RAM copy not removed, archived, or its archived copy
    missing (removed outside helm: the pass never removes one)."""
    rows = read_index(root)
    restored = {r.get("archive_path") for r in rows
                if r.get("event") == "restored"}
    failed = _failed(rows)
    out = []
    for r in rows:
        if r.get("event") != "evicted":
            continue
        if session and r.get("session") != session:
            continue
        ap = r.get("archive_path")
        state = ("NOT archived: its move into the archive failed, and it "
                 "stays in RAM" if _never_moved(r, failed) else
                 "restored" if ap in restored else
                 "archived, RAM copy NOT removed"
                 if (r.get("path"), ap) in failed else
                 "archived" if os.path.lexists(ap or "") else
                 "archived copy MISSING")
        out.append(dict(r, state=state))
    out.sort(key=lambda r: -r.get("epoch", 0))
    return out


def cmd_evicted(rest):
    import sys
    session, as_json, args = None, False, list(rest)
    while args:
        a = args.pop(0)
        if a == "--json":
            as_json = True
        elif a == "--session" and args:
            session = args.pop(0)
        else:
            print("usage: helm scratch evicted [--session S] [--json]",
                  file=sys.stderr)
            return 2
    rows = listing(session)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    root = archive_root()
    print("helm scratch evicted: %d unit%s in %s (index %s)"
          % (len(rows), "s"[:len(rows) != 1], root,
             os.path.join(root, INDEX)))
    for r in rows:
        print("  %s  %-8s %9s %6d files  tier %s  %s\n      -> %s  [%s]"
              % (r.get("ts", "?"), (r.get("session") or "?")[:8],
                 _human(r.get("bytes") or 0), r.get("files") or 0,
                 r.get("tier", "?"), r.get("path"), r.get("archive_path"),
                 r["state"]))
    return 0


def cmd_restore(rest):
    import sys
    force = "--force" in rest
    paths = [a for a in rest if a != "--force"]
    if len(paths) != 1 or paths[0].startswith("-"):
        print("usage: helm scratch restore <path-or-archive-path> [--force]",
              file=sys.stderr)
        return 2
    rc, msg = restore(paths[0], force=force)
    print("helm scratch: " + msg, file=sys.stderr if rc else sys.stdout)
    return rc
