import os
import stat
import errno
import time
import json
import ctypes
import hashlib
import secrets

from ._common import (
    tomllib, BACKUP_DIR, CWD_ROOTS, HOME_ROOTS, _DENY_FILES,
    _MAX_CONFIG_BYTES, _MISSING_REVISION, _RENAMEAT2, _RENAME_EXCHANGE,
)
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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
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
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    try:
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
        dfd = os.open(BACKUP_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
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
            fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
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
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOTSUP, "renameat2 unavailable")
    if _RENAMEAT2(dfd, os.fsencode(old), dfd, os.fsencode(new), flags) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


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
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    try:
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
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=dfd)
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
    return {"ok": True, "path": rp, "backup": backup, "created_parent": created_parent,
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
    file's exact formatting.
    """
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) \
            or max_attempts < 1:
        return _transform_error("attempts", "max_attempts must be a positive integer", 0)
    last = _transform_error("conflict", "config file changed concurrently", 0)
    initial_metadata = None
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
                    "revision": got["revision"], "backup": last_backup,
                    "path": got["path"], "wrote": committed_once}
        if dry_run:
            return {"ok": True, "action": "dry", "attempts": attempt,
                    "before": raw, "after": after, "metadata": metadata,
                    "initial_metadata": initial_metadata,
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


def list_backups():
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
