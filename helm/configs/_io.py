import os
import stat
import errno
import time
import json
import ctypes
import hashlib
import secrets
import threading

from ._common import (
    tomllib, BACKUP_DIR, CWD_ROOTS, HOME_ROOTS, _DENY_FILES,
    _MAX_CONFIG_BYTES, _MISSING_REVISION, _RENAMEAT2, _RENAME_EXCHANGE,
)
from .. import openflags
from ._classify import (
    _real, _absolute, _candidate, _path_below, _root_match, _under,
    _home_holder, _is_seat_home, _is_recognized_config, _lstat_regular,
    classify_path, _error,
)


class _ConfigIOError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _stat_identity(st):
    return st.st_dev, st.st_ino


def _revision(st, data):
    """Opaque edit identity stable across our own atomic rename."""
    h = hashlib.sha256()
    h.update(("%d:%d:%d:%d:%d:%d:%d:" % (
        st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
        stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)).encode())
    h.update(data)
    return h.hexdigest()


def _same_displaced_snapshot(a, b):
    """Compare the inode exchanged out of place with the pre-save snapshot.

    rename/exchange itself updates ctime on Linux, so ctime cannot participate
    here. Identity, bytes, mtime, ownership and mode still catch replacement,
    content changes and metadata changes in the final race window.
    """
    sa, sb = a["stat"], b["stat"]
    return (a["data"] == b["data"] and _stat_identity(sa) == _stat_identity(sb)
            and sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns
            and stat.S_IMODE(sa.st_mode) == stat.S_IMODE(sb.st_mode)
            and sa.st_uid == sb.st_uid and sa.st_gid == sb.st_gid)


def _snapshot_at(dirfd, name):
    """Open one regular leaf without following it and return a stable snapshot."""
    try:
        before = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise _ConfigIOError("io", "config file could not be inspected")
    if not stat.S_ISREG(before.st_mode):
        raise _ConfigIOError("not-regular", "config entry is not a regular file")
    try:
        flags = openflags.flags(os.O_RDONLY, "O_NOFOLLOW", cloexec=True)
        fd = os.open(name, flags, dir_fd=dirfd)
    except OSError:
        raise _ConfigIOError("open", "config file could not be opened safely")
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before):
            raise _ConfigIOError("conflict", "config file changed concurrently")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CONFIG_BYTES:
                raise _ConfigIOError("too-large", "config file exceeds the editor size limit")
        after = os.fstat(fd)
        stable = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
                  _stat_identity(opened))
        if stable != (after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                      _stat_identity(after)):
            raise _ConfigIOError("conflict", "config file changed concurrently")
        data = b"".join(chunks)
        return {"data": data, "stat": after, "revision": _revision(after, data)}
    finally:
        os.close(fd)


def _snapshot(path):
    parent, name = os.path.dirname(path), os.path.basename(path)
    try:
        flags = openflags.flags(
            os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW", cloexec=True)
        dfd = os.open(parent, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise _ConfigIOError("parent", "config parent could not be opened safely")
    try:
        return _snapshot_at(dfd, name)
    finally:
        os.close(dfd)


def _decode(data):
    encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        raise _ConfigIOError("encoding", "config file is not valid UTF-8")


def _newline_style(content):
    crlf = content.count("\r\n")
    rest = content.replace("\r\n", "")
    kinds = sum(bool(n) for n in (crlf, rest.count("\n"), rest.count("\r")))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if "\n" in rest:
        return "lf"
    if "\r" in rest:
        return "cr"
    return "none"


def _encode(content, encoding, newline):
    if newline == "mixed":
        raise _ConfigIOError("newline", "mixed newline styles are read-only")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "crlf":
        normalized = normalized.replace("\n", "\r\n")
    elif newline == "cr":
        normalized = normalized.replace("\n", "\r")
    return normalized.encode(encoding)


def _readable(path, rp):
    holder = _home_holder(rp)
    return (os.path.basename(rp) not in _DENY_FILES
            and _is_recognized_config(rp)
            and _root_match(path) is not None
            and (_under(rp, CWD_ROOTS + HOME_ROOTS) or _is_seat_home(holder)))


def read_file(path):
    """Content plus an opaque revision for one safely-opened config file."""
    rp = _candidate(path)
    typ, editable, reason = classify_path(path)
    if rp is None or not _readable(path, rp):
        return {"path": "", "type": typ, "editable": False, "reason": reason,
                "exists": False, "content": "", "revision": None,
                **_error("refused", "only recognized config files are readable")}
    st = _lstat_regular(rp)
    exists = st is not None and st is not False
    if st is False:
        return {"path": rp, "type": typ, "editable": False,
                "reason": "not a regular file", "exists": True, "content": "",
                "revision": None, **_error("not-regular", "config entry is not a regular file")}
    if not exists:
        return {"path": rp, "type": typ, "editable": editable, "reason": reason,
                "exists": False, "content": "", "revision": _MISSING_REVISION,
                "encoding": "utf-8", "newline": "none", "error": None}
    try:
        snap = _snapshot(rp)
        content, encoding = _decode(snap["data"])
    except _ConfigIOError as e:
        return {"path": rp, "type": typ, "editable": False, "reason": e.message,
                "exists": True, "content": "", "revision": None,
                **_error(e.code, e.message)}
    newline = _newline_style(content)
    if newline == "mixed":
        editable, reason = False, "mixed newline styles are read-only"
    return {"path": rp, "type": typ, "editable": editable, "reason": reason,
            "exists": True, "content": content, "revision": snap["revision"],
            "encoding": encoding, "newline": newline, "error": None}


def _validate(typ, content):
    """(ok, error). Reject an edit that would make a parseable type unparseable."""
    if typ == "json":
        try:
            json.loads(content)
        except ValueError as e:
            return False, f"invalid JSON at line {getattr(e, 'lineno', '?')} column {getattr(e, 'colno', '?')}"
    elif typ == "toml":
        if tomllib is None:
            return True, "toml not validated (python < 3.11)"
        try:
            tomllib.loads(content)
        except Exception as e:
            return False, f"invalid TOML ({e.__class__.__name__})"
    return True, None


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[n:]


def _backup_snapshot(rp, snap, encoding, newline):
    """Durably record the exact bytes about to be displaced."""
    try:
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        os.chmod(BACKUP_DIR, 0o700)
        dfd = os.open(BACKUP_DIR, openflags.flags(
            os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW", cloexec=True))
    except OSError:
        raise _ConfigIOError("backup", "config backup could not be created")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) \
        + f"-{time.time_ns() % 1_000_000_000:09d}"
    tag = hashlib.sha256(os.fsencode(rp)).hexdigest()[:20]
    name = f"{stamp}__{tag}"
    meta = json.dumps({"orig": rp, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "encoding": encoding, "newline": newline}, ensure_ascii=False).encode()
    made = []
    try:
        for leaf, data in ((name, snap["data"]), (name + ".orig", meta)):
            fd = os.open(leaf, openflags.flags(
                os.O_WRONLY | os.O_CREAT | os.O_EXCL, cloexec=True),
                0o600, dir_fd=dfd)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            made.append(leaf)
        os.fsync(dfd)
        return os.path.join(BACKUP_DIR, name)
    except OSError:
        for leaf in made:
            try:
                os.unlink(leaf, dir_fd=dfd)
            except OSError:
                pass
        raise _ConfigIOError("backup", "config backup could not be created")
    finally:
        os.close(dfd)


def _renameat2(dfd, old, new, flags):
    """The same-directory form, DELEGATING its semantics to the filesystem
    owner (item 10).

    This kept its own copy of the error contract beside helm.fsops, and the
    copy carried both bugs the owner had already fixed: `errno.ENOTSUP` is
    absent on some interpreters, so reporting an unavailable syscall raised
    AttributeError out of write_file's OSError handling; and a nonzero result
    with errno 0 became OSError(0, "Success"), which reads as neither an error
    nor a success to anyone who catches it.

    Extracting the binding and leaving this behind created TWO contracts for
    one syscall — the shape where a fix lands in one copy and the other keeps
    the defect. The signature and the module-level name are unchanged, so the
    `mock.patch.object(configs, "_renameat2", ...)` seam its tests rely on
    still works; only the semantics move."""
    from .. import fsops
    fsops.renameat2(dfd, old, dfd, new, flags)


def _remove_quiet(dfd, name):
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def _write_file_impl(path, content, expected_revision=None, encoding_override=None,
                     newline_override=None):
    """Validated, conflict-detecting, durable atomic edit of one regular file.

    Existing saves use renameat2(RENAME_EXCHANGE): the displaced inode remains
    staged until its identity and bytes match the editor revision and the parent
    directory is fsynced. Any mismatch or durability failure exchanges it back.
    Creates use an atomic no-clobber hard link. No arbitrary path is opened by
    name before the shared recognition/containment/lstat gate accepts it.
    """
    rp = _candidate(path)
    typ, editable, reason = classify_path(path)
    if rp is None or not editable:
        return _error("refused", "refused: " + reason)
    if not isinstance(content, str):
        return _error("content", "refused: content must be text")
    ok, verr = _validate(typ, content)
    if not ok:
        return _error("validation", f"refused: {verr} — no change written")
    parent, name = os.path.dirname(rp), os.path.basename(rp)
    created_parent = False
    if not os.path.isdir(parent):
        if expected_revision not in (None, _MISSING_REVISION):
            return _error("conflict", "config file changed concurrently; reload before saving")
        if os.path.basename(parent) not in (".claude", "commands", "rules"):
            return _error("parent", "config parent does not exist")
        try:
            os.mkdir(parent, 0o700)
            created_parent = True
        except OSError:
            return _error("parent", "config parent could not be created safely")
    try:
        flags = openflags.flags(
            os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW", cloexec=True)
        dfd = os.open(parent, flags)
    except OSError:
        return _error("parent", "config parent could not be opened safely")
    tmp = f".helm-config-{os.getpid()}-{secrets.token_hex(8)}"
    backup = None
    try:
        try:
            current = _snapshot_at(dfd, name)
        except _ConfigIOError as e:
            return _error(e.code, e.message)
        current_revision = current["revision"] if current else _MISSING_REVISION
        if expected_revision is not None and expected_revision != current_revision:
            return _error("conflict", "config file changed concurrently; reload before saving")
        if current:
            try:
                current_content, current_encoding = _decode(current["data"])
            except _ConfigIOError as e:
                return _error(e.code, e.message)
            current_newline = _newline_style(current_content)
        else:
            current_encoding, current_newline = "utf-8", "lf"
        encoding = encoding_override or current_encoding
        newline = newline_override or current_newline
        try:
            data = _encode(content, encoding, newline)
        except (_ConfigIOError, UnicodeEncodeError) as e:
            code = e.code if isinstance(e, _ConfigIOError) else "encoding"
            message = e.message if isinstance(e, _ConfigIOError) else "content cannot use the original encoding"
            return _error(code, message)
        if len(data) > _MAX_CONFIG_BYTES:
            return _error("too-large", "config content exceeds the editor size limit")
        if current:
            try:
                backup = _backup_snapshot(rp, current, current_encoding, current_newline)
            except _ConfigIOError as e:
                return _error(e.code, e.message)
            mode = stat.S_IMODE(current["stat"].st_mode)
            uid, gid = current["stat"].st_uid, current["stat"].st_gid
        else:
            holder = _home_holder(rp)
            mode = 0o600 if (_under(rp, HOME_ROOTS) or _is_seat_home(holder)) else 0o644
            uid, gid = os.geteuid(), os.getegid()
        try:
            fd = os.open(tmp, openflags.flags(
                os.O_WRONLY | os.O_CREAT | os.O_EXCL, cloexec=True),
                mode, dir_fd=dfd)
            try:
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
                _write_all(fd, data)
                os.fsync(fd)
                staged_stat = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError:
            _remove_quiet(dfd, tmp)
            return _error("stage", "config update could not be staged safely")
        if current:
            try:
                _renameat2(dfd, tmp, name, _RENAME_EXCHANGE)
            except OSError:
                _remove_quiet(dfd, tmp)
                return _error("atomic", "atomic config exchange is unavailable")
            try:
                displaced = _snapshot_at(dfd, tmp)
                if displaced is None or not _same_displaced_snapshot(displaced, current):
                    raise _ConfigIOError("conflict", "config file changed concurrently; reload before saving")
                os.fsync(dfd)
            except (_ConfigIOError, OSError) as e:
                try:
                    _renameat2(dfd, tmp, name, _RENAME_EXCHANGE)
                    os.fsync(dfd)
                except OSError:
                    return _error("rollback", "config update failed and rollback could not be confirmed")
                _remove_quiet(dfd, tmp)
                if isinstance(e, _ConfigIOError):
                    return _error(e.code, e.message)
                return _error("durability", "config update was rolled back after a durability failure")
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                try:
                    _renameat2(dfd, tmp, name, _RENAME_EXCHANGE)
                    os.fsync(dfd)
                except OSError:
                    return _error("rollback", "config cleanup failed and rollback could not be confirmed")
                _remove_quiet(dfd, tmp)
                return _error("cleanup", "config update was rolled back after cleanup failed")
            try:
                os.fsync(dfd)
            except OSError:
                pass  # target rename was already durably fsynced; only temp cleanup may replay
        else:
            try:
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                os.fsync(dfd)
            except FileExistsError:
                _remove_quiet(dfd, tmp)
                return _error("conflict", "config file appeared concurrently; reload before saving")
            except OSError:
                _remove_quiet(dfd, tmp)
                return _error("atomic", "config create could not be committed atomically")
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                try:
                    os.unlink(name, dir_fd=dfd)
                    os.fsync(dfd)
                except OSError:
                    return _error("rollback", "config create failed and rollback could not be confirmed")
                _remove_quiet(dfd, tmp)
                return _error("cleanup", "config create was rolled back after cleanup failed")
            try:
                os.fsync(dfd)
            except OSError:
                pass  # the linked target was already durably fsynced
    finally:
        os.close(dfd)
    note = ("created" if current is None else "updated") + (f" ({verr})" if verr else "")
    # "created" as a FACT beside the display sentence that renders it. A write
    # that put a file where there was none can have made its directory config-
    # bearing for the first time, which is the one thing about a save that the
    # config-tree scan cannot already know; a caller deciding that must not
    # have to parse the prefix off a string meant for a human.
    return {"ok": True, "path": rp, "backup": backup, "created_parent": created_parent,
            "created": current is None,
            "revision": _revision(staged_stat, data), "note": note}


def write_file(path, content, expected_revision=None):
    """Public writer contract; private format overrides are restore-only."""
    return _write_file_impl(path, content, expected_revision=expected_revision)


def _transform_error(code, message, attempts, before="", after="", metadata=None):
    return {"ok": False, "code": code, "error": message, "action": "fail",
            "attempts": attempts, "before": before, "after": after,
            "metadata": metadata or {}, "revision": None, "backup": None}


def _verify_transform(verify, candidate, before, metadata):
    if verify is None:
        return True, None
    try:
        verdict = verify(candidate, before, metadata)
    except (TypeError, ValueError) as exc:
        return False, str(exc)
    if isinstance(verdict, tuple):
        ok, reason = verdict
        return bool(ok), reason
    return bool(verdict), None if verdict else "candidate verification failed"


def transform_json_file(path, transform, verify=None, max_attempts=3, dry_run=False):
    """Bounded exact-revision transform for a JSON-object config file.

    Every retry re-reads and re-runs the domain merge. A post-commit foreign
    write is preserved and becomes the next attempt's input; no backup is ever
    restored over a revision Helm did not read. Semantic no-ops preserve the
    file's exact formatting. Successful results keep three distinct reports:
    initial_metadata is first intent, metadata describes the winning snapshot,
    and committed_metadata lists only successful own writes, in commit order.
    Dry runs and no-own-write results have an empty committed_metadata list.
    """
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) \
            or max_attempts < 1:
        return _transform_error("attempts", "max_attempts must be a positive integer", 0)
    last = _transform_error("conflict", "config file changed concurrently", 0)
    initial_metadata = None
    committed_metadata = []
    committed_once, last_backup = False, None
    for attempt in range(1, max_attempts + 1):
        got = read_file(path)
        if got.get("error"):
            return _transform_error(got.get("code") or "read", got["error"], attempt)
        raw = got["content"]
        try:
            before = json.loads(raw) if got["exists"] else {}
        except ValueError as exc:
            return _transform_error(
                "validation", "cannot parse config JSON: %s — refusing to touch it" % exc,
                attempt, before=raw)
        if not isinstance(before, dict):
            return _transform_error("shape", "config root is not a JSON object",
                                    attempt, before=raw)
        source = json.loads(json.dumps(before))
        try:
            made = transform(source)
        except (TypeError, ValueError) as exc:
            return _transform_error("transform", str(exc), attempt, before=raw)
        if isinstance(made, tuple) and len(made) == 2:
            candidate, metadata = made
        else:
            candidate, metadata = made, {}
        if initial_metadata is None:
            initial_metadata = metadata
        if not isinstance(candidate, dict):
            return _transform_error("shape", "JSON transform did not return an object",
                                    attempt, before=raw, metadata=metadata)
        after = json.dumps(candidate, indent=2) + "\n"
        ok, reason = _verify_transform(verify, candidate, before, metadata)
        if not ok:
            return _transform_error("verification", reason or "candidate verification failed",
                                    attempt, before=raw, after=after, metadata=metadata)
        if candidate == before:
            return {"ok": True,
                    "action": "updated" if committed_once else "ok",
                    "attempts": attempt, "before": raw, "after": raw,
                    "metadata": metadata, "initial_metadata": initial_metadata,
                    "committed_metadata": committed_metadata,
                    "revision": got["revision"], "backup": last_backup,
                    "path": got["path"], "wrote": committed_once}
        if dry_run:
            return {"ok": True, "action": "dry", "attempts": attempt,
                    "before": raw, "after": after, "metadata": metadata,
                    "initial_metadata": initial_metadata,
                    "committed_metadata": committed_metadata,
                    "revision": got["revision"], "backup": None,
                    "path": got["path"], "wrote": False}
        saved = write_file(path, after, expected_revision=got["revision"])
        if saved.get("error"):
            if saved.get("code") != "conflict":
                return _transform_error(saved.get("code") or "write", saved["error"],
                                        attempt, before=raw, after=after,
                                        metadata=metadata)
            last = _transform_error("conflict", saved["error"], attempt,
                                    before=raw, after=after, metadata=metadata)
            continue
        committed_once = True
        committed_metadata.append(metadata)
        last_backup = saved.get("backup") or last_backup
        committed = read_file(path)
        if committed.get("error"):
            return _transform_error(committed.get("code") or "read", committed["error"],
                                    attempt, before=raw, after=after, metadata=metadata)
        if committed["revision"] != saved.get("revision"):
            last = _transform_error(
                "conflict", "config file changed after Helm committed; newer revision preserved",
                attempt, before=committed.get("content", ""), after=after,
                metadata=metadata)
            continue
        try:
            exact = json.loads(committed["content"])
        except ValueError as exc:
            return _transform_error("verification", "committed JSON is unreadable: %s" % exc,
                                    attempt, before=raw, after=committed["content"],
                                    metadata=metadata)
        exact_ok, exact_reason = _verify_transform(verify, exact, before, metadata)
        if exact != candidate or not exact_ok:
            return _transform_error(
                "verification", exact_reason or "exact committed revision failed verification",
                attempt, before=raw, after=committed["content"], metadata=metadata)
        return {"ok": True, "action": "created" if not got["exists"] else "updated",
                "attempts": attempt, "before": raw, "after": committed["content"],
                "metadata": metadata, "initial_metadata": initial_metadata,
                "committed_metadata": committed_metadata,
                "revision": saved["revision"],
                "backup": saved.get("backup"), "path": saved.get("path"),
                "wrote": True}
    last["error"] = ("config file changed during all %d attempts; refusing to overwrite "
                     "or restore a foreign revision" % max_attempts)
    return last


# ── structured entry ops (safer than raw-file editing for common toggles) ─────

def entry_op(action, path, kind, name, value=None):
    """Add/remove one MCP entry against the exact safely-read revision."""
    typ, editable, reason = classify_path(path)
    if not editable:
        return _error("refused", "refused: " + reason)
    if typ != "json":
        return _error("type", "structured entry ops apply to JSON config files only")
    got = read_file(path)
    if got.get("error"):
        return _error(got.get("code") or "read", got["error"])
    try:
        data = json.loads(got["content"]) if got["exists"] else {}
    except ValueError as e:
        return _error("validation", f"cannot parse config JSON: {e}")
    if not isinstance(data, dict):
        return _error("shape", "config root is not a JSON object")
    if kind == "mcp":
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            return {"error": "mcpServers is not an object"}
        if action == "add":
            if not isinstance(value, dict):
                return {"error": "add needs a server config object"}
            servers[name] = value
        elif action == "remove":
            servers.pop(name, None)
        else:
            return {"error": f"unsupported action {action!r} for mcp"}
    else:
        return {"error": f"unsupported kind {kind!r} (mcp only, for now)"}
    return write_file(path, json.dumps(data, indent=2) + "\n",
                      expected_revision=got["revision"])


# ── backups / undo ────────────────────────────────────────────────────────────

def _backup_meta(bp):
    """Validated sidecar for one regular backup leaf."""
    try:
        if not _lstat_regular(bp):
            return None
        snap = _snapshot(bp + ".orig")
        if snap is None:
            return None
        raw, _ = _decode(snap["data"])
        data = json.loads(raw)
        orig = data.get("orig")
        rp = _candidate(orig)
        if rp is None or not _readable(orig, rp):
            return None
        return data
    except (OSError, ValueError, TypeError, _ConfigIOError):
        return None


def _backup_orig(bp):
    meta = _backup_meta(bp)
    return meta.get("orig") if meta else None


# ── the backup listing, and why it is memoised on the directory itself ─────
# MEASURED on the owner's own box: 6,948 entries under BACKUP_DIR — 3,474
# backups plus their sidecars — and the walk below lstats each leaf, reads
# and json-parses its `.orig` sidecar, and resolves the origin path through
# `_candidate`/`_readable`. 1.65s fastest, 2.22s median, 2.52s slowest over
# six consecutive calls, and NOTHING held the answer: every
# `/api/configs/backups` call paid the whole walk, and the editor calls it on
# every file open and again after every save and every restore.
#
# THE KEY IS THE DIRECTORY'S OWN IDENTITY, NOT A CLOCK, and that is the point.
# A TTL here would have to be guessed against a 2.5s walk, and this tree has
# twice shipped a TTL SHORTER than the fill it was caching — a 46s /api/ready
# under a 30s ttl, a 6s roster build under a 2.5s one — which can never serve
# warm and blocks every caller instead. A fingerprint cannot be wrong in that
# direction: a new backup file, or a removed one, CHANGES the directory's
# mtime, so the very next call rebuilds. Read-your-own-writes therefore holds
# unconditionally — the owner saves a config, the save mints a backup, the
# directory moves, and the listing beside his editor already shows it — with
# no invalidation hook anywhere that a later writer could forget to call.
#
# THE AGE CAP IS FOR WHAT THE FINGERPRINT CANNOT SEE. A row is dropped when
# its ORIGIN becomes unreadable, and an origin disappearing does not touch
# this directory at all. 300s is two orders of magnitude above the measured
# walk, so it bounds that one blind spot without ever being the thing that
# decides an ordinary call.
_BACKUPS_MAX_AGE_S = 300

_BACKUPS_CACHE = {}       # backup-root -> (built_at, fingerprint, rows)

_BACKUPS_LOCK = threading.Lock()


def _backups_fingerprint():
    """The backup directory's identity, or None when it cannot be read.

    None is UNREADABLE and is never a fingerprint: two calls that both failed
    to stat the directory must not compare equal and serve each other's body.
    """
    try:
        st = os.stat(BACKUP_DIR)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def _list_backups_uncached():
    out = []
    try:
        for b in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if b.endswith(".orig"):
                continue
            bp = os.path.join(BACKUP_DIR, b)
            st = _lstat_regular(bp)
            meta = _backup_meta(bp)
            if not st or not meta:
                continue
            out.append({"backup": bp, "orig": meta["orig"], "size": st.st_size,
                        "at": meta.get("at") or time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except OSError:
        pass
    return out


def list_backups():
    """Every readable backup, newest first — memoised on the directory.

    THE ROWS ARE THE SAME ROWS. This wrapper changes WHEN the walk runs and
    nothing about what it answers; `_list_backups_uncached` is the whole
    listing and is still what every miss calls. A COPY is handed out, so a
    caller that mutates its result cannot edit the memo the next caller reads.
    """
    fp = _backups_fingerprint()
    key = str(BACKUP_DIR)
    now = time.time()
    hit = _BACKUPS_CACHE.get(key)
    if hit and fp is not None and hit[1] == fp \
            and now - hit[0] < _BACKUPS_MAX_AGE_S:
        return [dict(r) for r in hit[2]]
    with _BACKUPS_LOCK:
        # RE-CHECK UNDER THE LOCK — the single-flight gate. Without it, N
        # callers arriving into one cold window each run the 2.5s walk, which
        # is the pile-up the memo exists to stop rather than a cheap miss.
        hit = _BACKUPS_CACHE.get(key)
        if hit and fp is not None and hit[1] == fp \
                and time.time() - hit[0] < _BACKUPS_MAX_AGE_S:
            return [dict(r) for r in hit[2]]
        started = _backups_fingerprint()
        rows = _list_backups_uncached()
        # STAMPED WITH THE IDENTITY THE WALK STARTED FROM. A backup written
        # DURING the walk may or may not be in `rows`, and stamping with the
        # identity the directory has AFTERWARDS would publish that uncertain
        # listing as a confident answer for the new state — it would be
        # served until something else happened to move the directory again.
        # The starting identity cannot do that: the live directory no longer
        # matches it, so the very next call rebuilds. An UNREADABLE directory
        # (None) is never stamped at all.
        if started is not None:
            _BACKUPS_CACHE[key] = (time.time(), started, rows)
        return [dict(r) for r in rows]


def restore(backup):
    """Restore exact backup text/newline encoding through the normal write gate."""
    ap = _absolute(backup)
    root = _real(BACKUP_DIR)
    if ap is None or not _path_below(ap, root) or _candidate(ap) != ap:
        return _error("backup", "unknown backup")
    meta = _backup_meta(ap)
    if not meta:
        return _error("backup", "unknown backup")
    try:
        snap = _snapshot(ap)
        if snap is None:
            raise _ConfigIOError("backup", "backup disappeared concurrently")
        content, detected = _decode(snap["data"])
    except _ConfigIOError as e:
        return _error(e.code, "backup could not be read safely")
    encoding = meta.get("encoding") if meta.get("encoding") in ("utf-8", "utf-8-sig") else detected
    newline = meta.get("newline") if meta.get("newline") in ("none", "lf", "crlf", "cr") \
        else _newline_style(content)
    return _write_file_impl(meta["orig"], content, encoding_override=encoding,
                            newline_override=newline)
