"""helm inject — the ledger cluster: hook-JSON parsing, cwd->project scoping,
the fire-ledger append/read primitives, the per-session cooldown seen-state,
and the lane-split cohort report (lane_report/--lane-report).

Moved verbatim from the pre-split helm/inject.py. _cohort lives HERE (its
only consumer is lane_report) so the cluster graph stays one-way.
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time

from .. import home, injection_schema, openflags, pk
from ._common import LEDGER_MAX, REPEAT_WINDOW_TURNS, SEEN_TTL, WHO_ID
from ._entries import _entry_line, _who_lines, load_entries


def parse_hook_json(raw):
    """Claude Code UserPromptSubmit hook JSON -> (prompt, cwd, session). Unknown
    keys are tolerated (the hook payload grows); missing keys read as empty.
    Malformed/non-object input -> (None, None, None): the caller must FAIL OPEN
    (inject nothing, rc 0) — a garbled payload never blocks a turn."""
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, None
    if not isinstance(d, dict):
        return None, None, None
    return (str(d.get("prompt") or ""),
            str(d.get("cwd") or "") or None,
            str(d.get("session_id") or "") or None)


def project_for_cwd(cwd, projects=None, strict=False):
    """cwd -> registry project name by LONGEST-prefix match over project paths
    (drain's longest-first law: the deepest registered path that contains cwd
    wins). None = no project claims it — the caller stays global-only.
    Read-only registry access.

    `strict=False` FAILS OPEN (any trouble -> None); `strict=True` RE-RAISES.
    A strict caller has asked for an authority snapshot, and the two ways this
    can fail are not the same fact: "no project claims this path" is an answer,
    "the registry could not be read" is the absence of one. The strict arm
    therefore validates the population (`registry.load(strict=True)`) and each
    record's cwd scope, and lets a malformed registry, an unreadable file or an
    unusable scope come out as the exception it is — the caller decides what an
    UNKNOWN authority means for its own polarity. `dispatches.write_scope` is
    that caller: it turns the raise into a refusal naming the registry error,
    because for a write door an unreadable authority admits nothing. A
    docstring here that promised None for every trouble is what let that door
    ship believing it could never see an exception."""
    if not cwd:
        return None
    from .. import registry
    try:
        want = os.path.abspath(os.path.expanduser(str(cwd)))
        best = None
        if projects is None:
            projects = (registry.load(strict=True) if strict else registry.load()).get("projects") or {}
        for key, rec in projects.items():
            # the FULL cwd lens, not just the canonical path — the registry
            # records sibling-worktree and alternate cwds in cv_scope
            # (codex-seat review: a hook fired in a worktree fell back to
            # global-only, silently dropping project premises + reflexes)
            prefixes = (rec.get("cv_scope") or {}).get("cwd_prefixes") \
                or [rec.get("path")]
            if strict and (not isinstance(prefixes, list) or
                           any(not isinstance(pre, str) or not pre for pre in prefixes)):
                raise ValueError("registry project has no readable scope")
            for pre in list(prefixes):
                pre = str(pre or "").rstrip("/")
                if not pre:
                    continue
                # A LANE WORKTREE BELONGS TO ITS REPO'S PROJECT, by
                # construction rather than by registration. `helm work claim`
                # mints rooms at <repo>-wt/<lane> and peeks at
                # <repo>-wt/peeks/<sha> — a SIBLING of the repo path, so no
                # registered prefix matched and the nearest ancestor project
                # won instead. Measured 2026-08-05: every helm lane worktree
                # resolved to the UMBRELLA project, which registers the
                # parent directory holding every repo. Four surfaces — turn
                # premises and reflexes, `helm store add` project inference,
                # the handoff journal shelf, and dispatch scoping — so a seat
                # in the room it is REQUIRED to work in got another project's
                # answer for all four, and `helm handoff check` reported
                # "contract satisfied" against the wrong shelf.
                #
                # The suffix is matched with its separator, so a sibling repo
                # whose name merely starts the same (helmet next to helm-wt)
                # cannot be captured.
                # rank 1 = a REGISTERED path; rank 0 = a DERIVED worktree
                # root. A registration always outranks a convention at the same
                # depth: a repo genuinely NAMED "<x>-wt" sitting beside "<x>"
                # matches both, and the row someone actually registered is the
                # one that means it. Without the rank the winner depended on
                # registry iteration order.
                for cand, rank in ((pre, 1), (pre + "-wt", 0)):
                    if want == cand or want.startswith(cand + "/"):
                        score = (len(cand), rank)
                        if best is None or score > best[0]:
                            best = (score, str(rec.get("name") or key))
        return best[1] if best else None
    except Exception:
        if strict:
            raise
        return None


def _ledger_path():
    return os.path.join(home.global_dir(), ".state", "inject-ledger.jsonl")


_LEDGER_PROTOCOL = 1
_LOCK_WAIT = 0.05
_RECOVERY_CAP = 64
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_HEADER = re.compile(
    br"\A# helm-inject-ledger protocol=1 generation=([0-9a-f]{32})\n")
_INTENT_BYTES = b"helm-inject-ledger intent protocol=1\n"
_MARKER_BYTES = b"helm-inject-ledger witness protocol=1\n"
_META = "_helm_inject_ledger"
_INTENT_NAME = re.compile(r"^([0-9a-f]{32})\.intent$")
_READY_NAME = re.compile(r"^([0-9a-f]{32})\.ready$")
_INFLIGHT_NAME = re.compile(
    r"^([0-9a-f]{32})\.inflight\.([0-9a-f]{32})$")
_COMMITTED_NAME = re.compile(
    r"^([0-9a-f]{32})\.committed\.([0-9a-f]{32})$")
_UNKNOWN_NAME = re.compile(
    r"^([0-9a-f]{32})\.unknown\.([0-9a-f]{32})\.([a-z0-9-]+)$")


class _Attempt:
    __slots__ = ("attempt", "path", "queue", "lock_fd")

    def __init__(self, attempt, path, queue, lock_fd):
        self.attempt = attempt
        self.path = path
        self.queue = queue
        self.lock_fd = lock_fd


class LedgerRows(list):
    """Canonical readable rows plus the source census verdict.

    complete=True is an exact protocol snapshot, False is known incomplete, and
    None means the lock/list/read boundary itself was unavailable. ``skipped``
    counts affected attempts, never sidecar files belonging to the same attempt.
    """

    def __init__(self, rows=(), complete=True, skipped=0):
        super().__init__(rows)
        self.complete = complete
        self.skipped = skipped


def _queue_dir(path):
    return path + ".queue"


def _header(generation):
    return ("# helm-inject-ledger protocol=1 generation=%s\n" %
            generation).encode("ascii")


def _generation(data):
    found = _HEADER.match(data)
    return found.group(1).decode("ascii") if found else None


def _new_token():
    return secrets.token_hex(16)


def _legacy_attempt(value):
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _fsync_dir(path):
    fd = os.open(path, openflags.flags(os.O_RDONLY, "O_DIRECTORY"))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(path):
    existed = os.path.isdir(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not existed:
        _fsync_dir(os.path.dirname(path))


def _write_all(fd, data):
    offset = 0
    while offset < len(data):
        wrote = os.write(fd, data[offset:])
        if wrote <= 0:
            raise OSError(errno.EIO, "short ledger write")
        offset += wrote


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    failed = True
    try:
        _write_all(fd, data)
        os.fsync(fd)
        failed = False
    finally:
        try:
            os.close(fd)
        except OSError:
            failed = True
        if failed:
            raise OSError(errno.EIO, "ledger file durability failed")


def _pread_exact(fd, size):
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, size - offset, offset)
        if not chunk:
            raise OSError(errno.EIO, "short ledger read")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _open_stable_lock(path):
    directory = os.path.dirname(path)
    _ensure_dir(directory)
    lock_path = path + ".lock"
    created = False
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(lock_path, os.O_RDWR)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "ledger lock is not a regular file")
        if created:
            os.fsync(fd)
            _fsync_dir(directory)
        return fd
    except Exception:
        os.close(fd)
        raise


def _acquire(fd, operation):
    deadline = time.monotonic() + _LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.001)


def _release(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _ledger_begin(path=None):
    """Durably admit one injection attempt and hold its shared generation lock.

    No caller may construct or mutate an injection turn unless this succeeds.
    The fixed ASCII intent is the cross-process evidence that survives row
    serialization failure or process death. Any setup, open, lock, file, or
    directory durability failure suppresses the turn instead of delivering an
    unaccounted injection.
    """
    path = path or _ledger_path()
    fd = None
    try:
        fd = _open_stable_lock(path)
        if not _acquire(fd, fcntl.LOCK_SH):
            _release(fd)
            return None
        queue = _queue_dir(path)
        _ensure_dir(queue)
        attempt = _new_token()
        _write_new(os.path.join(queue, attempt + ".intent"), _INTENT_BYTES)
        _fsync_dir(queue)
        return _Attempt(attempt, path, queue, fd)
    except Exception:
        _release(fd)
        return None


def _ledger_abort(attempt):
    """Release admission while deliberately retaining every durable artifact."""
    if attempt is not None:
        fd, attempt.lock_fd = attempt.lock_fd, None
        _release(fd)


def _ready_bytes(attempt, row):
    if not isinstance(row, dict) or _META in row:
        raise ValueError("invalid injection ledger row")
    return (json.dumps({"protocol": _LEDGER_PROTOCOL, "attempt": attempt,
                        "row": row}, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _decode_ready(data, attempt=None):
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict) or set(envelope) != {
            "protocol", "attempt", "row"} \
            or envelope.get("protocol") != _LEDGER_PROTOCOL \
            or not isinstance(envelope.get("attempt"), str) \
            or not _TOKEN.fullmatch(envelope["attempt"]) \
            or attempt is not None and envelope["attempt"] != attempt \
            or not isinstance(envelope.get("row"), dict) \
            or _META in envelope["row"]:
        return None
    return envelope


def _committed_bytes(envelope):
    row = dict(envelope["row"])
    row[_META] = {"protocol": _LEDGER_PROTOCOL,
                  "attempt": envelope["attempt"]}
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) +
            "\n").encode("utf-8")


def _marker_path(queue, attempt, generation, reason):
    return os.path.join(queue, "%s.unknown.%s.%s" % (
        attempt, generation, reason))


def _plant_unknown(queue, attempt, generation, reason):
    marker = _marker_path(queue, attempt, generation, reason)
    try:
        _write_new(marker, _MARKER_BYTES)
    except FileExistsError:
        pass
    _fsync_dir(queue)
    return marker


def _plant_committed(queue, attempt, generation):
    name = "%s.committed.%s" % (attempt, generation)
    marker = os.path.join(queue, name)
    try:
        _write_new(marker, _MARKER_BYTES)
    except FileExistsError:
        pass
    _fsync_dir(queue)
    return name


def _read_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return _pread_exact(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)


def _replace_with_header(path, generation, data):
    tmp = "%s.header.%s.%s.tmp" % (path, generation, _new_token())
    _write_new(tmp, _header(generation) + data)
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _rotation_temps(path):
    prefix = os.path.basename(path) + ".rotate."
    try:
        names = os.listdir(os.path.dirname(path))
    except OSError:
        return []
    return [os.path.join(os.path.dirname(path), name) for name in names
            if name.startswith(prefix) and name.endswith(".tmp")]


def _recover_rotation_locked(path):
    temps = sorted(_rotation_temps(path))
    if not os.path.exists(path) and len(temps) == 1:
        data = _read_path(temps[0])
        if _generation(data):
            os.replace(temps[0], path)
            _fsync_dir(os.path.dirname(path))
            temps = []
    if os.path.exists(path):
        for tmp in temps:
            try:
                os.remove(tmp)
                _fsync_dir(os.path.dirname(path))
            except OSError:
                pass


def _path_generation(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        size = os.fstat(fd).st_size
        probe = os.pread(fd, min(size, 128), 0)
        if len(probe) != min(size, 128):
            raise OSError(errno.EIO, "short generation read")
        return _generation(probe)
    finally:
        os.close(fd)


def _existing_generation(path):
    try:
        data = _read_path(path)
    except FileNotFoundError:
        return None, None
    return _generation(data), data


def _migration_generation(queue):
    try:
        names = os.listdir(queue)
    except OSError:
        return None
    found = [m.group(2) for name in names
             for m in [_UNKNOWN_NAME.fullmatch(name)]
             if m and m.group(3) == "migration"]
    return sorted(found)[0] if found else None


def _ensure_protocol_locked(path, queue):
    current_generation = _path_generation(path)
    prior_generation = _path_generation(path + ".1")
    current_exists = os.path.exists(path)
    prior_exists = os.path.exists(path + ".1")
    migrating = current_generation is None or \
        prior_exists and prior_generation is None
    if not migrating:
        return current_generation
    current = _existing_generation(path)[1] if current_exists else None
    prior = _existing_generation(path + ".1")[1] if prior_exists else None
    generation = current_generation or _migration_generation(queue) or _new_token()
    attempt = _legacy_attempt("migration:" + generation)
    _plant_unknown(queue, attempt, generation, "migration")
    if prior is not None and prior_generation is None:
        _replace_with_header(path + ".1", _new_token(), prior)
    if current_generation is None:
        _replace_with_header(path, generation, current or b"")
    return generation


def _is_legacy_name(path, name):
    prefix = os.path.basename(path) + ".incomplete."
    return name == prefix + "pending" \
        or name.startswith(prefix + "pending.") \
        or bool(re.fullmatch(re.escape(prefix) + r"\d+\.\d+(?:\..+)?",
                             name))


def _legacy_names(path):
    try:
        names = os.listdir(os.path.dirname(path))
    except OSError:
        return []
    return [name for name in names if _is_legacy_name(path, name)]


def _convert_legacy_locked(path, queue, lock_fd, generation):
    directory = os.path.dirname(path)
    for name in _legacy_names(path):
        reason = "legacy-pending" if ".pending" in name else "legacy-inode"
        attempt = _legacy_attempt("legacy-file:" + name)
        try:
            _plant_unknown(queue, attempt, generation, reason)
            os.remove(os.path.join(directory, name))
            _fsync_dir(directory)
        except OSError:
            pass
    try:
        size = os.fstat(lock_fd).st_size
        data = _pread_exact(lock_fd, size) if size else b""
    except OSError:
        return
    if not data:
        return
    lines = data.splitlines() or [data]
    try:
        for index, line in enumerate(lines):
            attempt = _legacy_attempt("legacy-lock:%d:" % index +
                                      line.decode("utf-8", "surrogateescape"))
            _plant_unknown(queue, attempt, generation, "legacy-lock")
        os.ftruncate(lock_fd, 0)
        os.fsync(lock_fd)
        _fsync_dir(directory)
    except OSError:
        pass


def _rotate_locked(path, max_bytes):
    try:
        if os.path.getsize(path) <= max_bytes:
            return _path_generation(path)
    except OSError:
        return _path_generation(path)
    generation = _new_token()
    tmp = "%s.rotate.%s.tmp" % (path, generation)
    _write_new(tmp, _header(generation))
    os.replace(path, path + ".1")
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))
    return generation


def _repair_tail_locked(path, expected):
    """Remove only a proven prefix of this inflight row; never scan a generation."""
    fd = os.open(path, os.O_RDWR)
    try:
        size = os.fstat(fd).st_size
        if not size:
            return
        width = min(size, len(expected))
        tail = os.pread(fd, width, size - width)
        if len(tail) != width:
            raise OSError(errno.EIO, "short ledger tail read")
        if tail.endswith(b"\n"):
            return
        newline = tail.rfind(b"\n")
        suffix = tail[newline + 1:]
        if suffix and expected.startswith(suffix):
            os.ftruncate(fd, size - len(suffix))
            os.fsync(fd)
            return
        raise OSError(errno.EIO, "unowned torn ledger tail")
    finally:
        os.close(fd)


def _tail_matches(path, data):
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        size = os.fstat(fd).st_size
        if size < len(data):
            return False
        tail = os.pread(fd, len(data), size - len(data))
        return tail == data
    finally:
        os.close(fd)


def _durabilize_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(os.path.dirname(path))


def _decode_committed(raw):
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None
    if not isinstance(row, dict):
        return None, None
    meta = row.get(_META)
    if meta is None:
        return row, None
    if not isinstance(meta, dict) or set(meta) != {"protocol", "attempt"} \
            or meta.get("protocol") != _LEDGER_PROTOCOL \
            or not isinstance(meta.get("attempt"), str) \
            or not _TOKEN.fullmatch(meta["attempt"]):
        return None, None
    clean = dict(row)
    del clean[_META]
    return clean, meta["attempt"]


def _append_committed(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    failed = True
    try:
        _write_all(fd, data)
        os.fsync(fd)
        failed = False
    finally:
        try:
            os.close(fd)
        except OSError:
            failed = True
        if failed:
            raise OSError(errno.EIO, "ledger append durability failed")
    _fsync_dir(os.path.dirname(path))


_EVIDENCE_NAMES = (_INTENT_NAME, _READY_NAME, _INFLIGHT_NAME,
                   _COMMITTED_NAME, _UNKNOWN_NAME)


def _evidence_match(name):
    return next((matched for pattern in _EVIDENCE_NAMES
                 for matched in [pattern.fullmatch(name)] if matched), None)


def _queue_groups(names):
    groups = {}
    for name in names:
        matched = _evidence_match(name)
        if matched:
            groups.setdefault(matched.group(1), []).append(name)
    return groups


def _cleanup_attempt(queue, names):
    changed = False
    complete = True
    for name in names:
        try:
            os.remove(os.path.join(queue, name))
            changed = True
        except FileNotFoundError:
            pass
        except OSError:
            complete = False
    if changed:
        try:
            _fsync_dir(queue)
        except OSError:
            complete = False
    return complete


def _finalize_committed(queue, attempt, evidence, committed):
    others = [name for name in evidence if name != committed]
    if not _cleanup_attempt(queue, others):
        return False
    try:
        names = os.listdir(queue)
    except OSError:
        return False
    if any(name != committed and (matched := _evidence_match(name))
           and matched.group(1) == attempt for name in names):
        return False
    try:
        os.remove(os.path.join(queue, committed))
        _fsync_dir(queue)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _prune_stale_unknown(queue, names, live):
    changed = False
    for name in names:
        matched = _UNKNOWN_NAME.fullmatch(name)
        if not matched or matched.group(2) in live:
            continue
        try:
            os.remove(os.path.join(queue, name))
            changed = True
        except OSError:
            pass
    if changed:
        try:
            _fsync_dir(queue)
        except OSError:
            pass


def _recover_locked(path, queue, lock_fd, max_bytes, priority=None):
    _recover_rotation_locked(path)
    generation = _ensure_protocol_locked(path, queue)
    _convert_legacy_locked(path, queue, lock_fd, generation)
    try:
        names = sorted(os.listdir(queue))
    except OSError:
        return
    groups = _queue_groups(names)

    def rank(attempt):
        evidence = groups[attempt]
        if any(_COMMITTED_NAME.fullmatch(name) for name in evidence):
            return 0, attempt
        if any(_INFLIGHT_NAME.fullmatch(name) for name in evidence):
            return 1, attempt
        return (2 if attempt == priority else 3), attempt

    for attempt in sorted(groups, key=rank)[:_RECOVERY_CAP]:
        evidence = groups[attempt]
        committed = [name for name in evidence
                     if _COMMITTED_NAME.fullmatch(name)]
        if committed:
            _finalize_committed(queue, attempt, evidence, sorted(committed)[0])
            continue
        ready = [name for name in evidence if _READY_NAME.fullmatch(name)]
        inflight = [name for name in evidence if _INFLIGHT_NAME.fullmatch(name)]
        unknown = [name for name in evidence if _UNKNOWN_NAME.fullmatch(name)]
        intent = [name for name in evidence if _INTENT_NAME.fullmatch(name)]
        if not ready and not inflight and not unknown and intent:
            try:
                _plant_unknown(queue, attempt, generation, "intent")
                _cleanup_attempt(queue, intent)
            except OSError:
                pass
            continue
        if unknown and intent and not ready and not inflight:
            _cleanup_attempt(queue, intent)
            continue
        source = sorted(inflight)[0] if inflight else None
        if source is None and ready:
            try:
                generation = _rotate_locked(path, max_bytes) or generation
                selected = sorted(ready)[0]
                source = "%s.inflight.%s" % (attempt, generation)
                os.rename(os.path.join(queue, selected),
                          os.path.join(queue, source))
                _fsync_dir(queue)
                evidence = [source if name == selected else name
                            for name in evidence]
            except OSError:
                continue
        if source is None:
            continue
        try:
            envelope = _decode_ready(
                _read_path(os.path.join(queue, source)), attempt=attempt)
            if envelope is None:
                continue
            committed_data = _committed_bytes(envelope)
            matched = next((candidate for candidate in (path, path + ".1")
                            if _tail_matches(candidate, committed_data)), None)
            if matched:
                _durabilize_path(matched)
                target = _path_generation(matched) or \
                    _INFLIGHT_NAME.fullmatch(source).group(2)
            else:
                _repair_tail_locked(path, committed_data)
                generation = _rotate_locked(path, max_bytes) or generation
                _append_committed(path, committed_data)
                target = generation
            tombstone = _plant_committed(queue, attempt, target)
        except OSError:
            # No later append may cover this attempt's tail before a future
            # writer can prove whether its exact bytes reached the ledger.
            return
        _finalize_committed(queue, attempt,
                            evidence + [tombstone], tombstone)
    live = {generation for generation in
            (_path_generation(path), _path_generation(path + ".1"))
            if generation}
    try:
        latest = os.listdir(queue)
    except OSError:
        return
    _prune_stale_unknown(queue, latest, live)


def _commit_spool(path, max_bytes, priority=None):
    fd = None
    try:
        fd = _open_stable_lock(path)
        if not _acquire(fd, fcntl.LOCK_EX):
            return False
        queue = _queue_dir(path)
        _ensure_dir(queue)
        _recover_locked(path, queue, fd, max_bytes, priority=priority)
        return True
    except Exception:
        return False
    finally:
        _release(fd)


def _ledger_finish(attempt, row, max_bytes=LEDGER_MAX):
    """Materialize immutable READY, release SH, then opportunistically commit.

    READY durability is the delivery gate. Once it and its directory are synced,
    an EX-lock/open/append/cleanup failure cannot erase the cross-process row, so
    output may proceed while a later writer recovers it. Before that boundary any
    failure suppresses output and leaves intent/partial evidence for recovery.
    """
    if attempt is None:
        return False
    try:
        data = _ready_bytes(attempt.attempt, row)
        _write_new(os.path.join(attempt.queue,
                                attempt.attempt + ".ready"), data)
        _fsync_dir(attempt.queue)
    except Exception:
        _ledger_abort(attempt)
        return False
    _ledger_abort(attempt)
    _commit_spool(attempt.path, max_bytes, priority=attempt.attempt)
    return True


def _append_jsonl(path, row, max_bytes):
    """Legacy fail-open append used by non-injection telemetry ledgers."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _ledger_append(row):
    """Synchronously admit and finish one fire-ledger row.

    The gather path uses begin/finish explicitly so no injection mutation can
    precede admission. This wrapper remains for producer-shaped tests and any
    non-delivering maintenance caller.
    """
    attempt = _ledger_begin()
    return bool(attempt and _ledger_finish(attempt, row))


# ---------------------------------------------------------------------------
# session cooldown — the habituation guard extended to the JIT lane
# ---------------------------------------------------------------------------

def _seen_dir():
    return os.path.join(home.global_dir(), ".state", "inject-seen")


def _seen_path(session):
    return os.path.join(_seen_dir(), pk.slug(str(session)) + ".json")


def _seen_lock_path(session):
    return _seen_path(session) + ".lock"


EPOCH_VOUCHED = "vouched"
EPOCH_UNVOUCHED = "unvouched"
EPOCH_UNKNOWN = "unknown"
EPOCH_VOUCHES = (EPOCH_VOUCHED, EPOCH_UNVOUCHED, EPOCH_UNKNOWN)


def _epoch_path(session):
    """The reset marker BESIDE the seen file, never inside it.

    THE SEEN FILE'S ABSENCE IS ITSELF A GUARDED PROPERTY: "the suppression is
    gone" is asserted as "that path does not exist" in several places, and a
    marker written back into it would answer that question with content
    instead — a weaker observable for the one correctness property in this
    module whose failure direction is a MUTE seat. A sibling path costs one
    file and keeps the drop provable by absence.

    It deliberately does NOT end in `.json`: the stale-sibling sweep in
    `_seen_save` walks this directory by that suffix and must go on reading
    exactly the seen files it was written for."""
    return _seen_path(session) + ".epoch"


def _mark_epoch(session, epoch):
    """Record one reset's PROVENANCE for the first row of the new epoch.

    THE LEDGER COULD SAY THAT AN EPOCH RESET AND NEVER WHY. `turn` restarts
    at 1 after every `forget_session`, so a reader sees the boundary and then
    has to cross-reference other files to learn which SessionStart caused it
    and whether anything vouched for that — which is how one census of these
    boundaries cost three files and an instrument that had to be retracted.
    This leaves the answer where the next turn's `_seen_load` will pick it up.

    NO `epoch` REMOVES THE MARKER rather than leaving the last one standing.
    A caller that drops a session's state without saying why has not made the
    previous reason true again, and a stale marker read as this boundary's is
    a confident wrong answer — the failure this whole field exists to end.

    THE OLD MARKER GOES FIRST, ALWAYS, and that ordering is the whole of the
    staleness guarantee. A write that fails after a successful removal leaves
    NOTHING, which reads as no provenance; a write that failed over a
    surviving predecessor would hand the next turn the PREVIOUS boundary's
    reason as this one's — a confident wrong answer, which is worse than the
    silence it replaces and is the failure this field exists to end.

    THE DIRECTORY MAY NOT EXIST YET. A seat's first SessionStart can land
    before its first injection turn, and that seat — new, and resetting
    before it ever fired — is exactly the one a reader most wants the
    provenance for. Three arms went red on precisely that.

    Entirely fail-open, and written only AFTER the drop it describes was
    judged: provenance is a diagnosis, so losing it costs a diagnosis, while
    the drop is the correctness property and reports its own failure itself.
    The write needs the DIRECTORY, so the truncation leg — which exists
    precisely for a writable file under an unwritable parent — records no
    provenance, and records it as absent rather than as unknown-by-claim."""
    path = _epoch_path(session)
    try:
        os.remove(path)          # the previous boundary's, never this one's
    except OSError:
        pass
    if not isinstance(epoch, dict) or not epoch:
        return
    try:
        os.makedirs(_seen_dir(), exist_ok=True)
        pk.write_json(path, epoch)
    except Exception:
        pass
    _sweep_epochs()


def _sweep_epochs():
    """Age the reset markers out, ON THE BOUNDARY CLOCK AND NOT THE TURN'S.

    The seen files beside them are swept from `_seen_save`, which runs on
    every turn that mutates; putting this there too would have made every
    such turn walk one more file per session. Measured on 200 markers that
    cost 1.7ms a save — a hot-path bill for housekeeping that has nothing to
    do with the turn. Here it runs once per context boundary, which is the
    rate markers are MADE at, and boundaries are counted in tens per day.

    No lock: every failure to read a marker already answers None, which is
    an absence of provenance, so a torn or vanished one lands on the same
    fail-open arm an absent one does. A marker is written a moment before
    this walks past it, so the sweep cannot reach one while it is current."""
    try:
        now = time.time()
        d = _seen_dir()
        for n in os.listdir(d):
            if not n.endswith(".epoch"):
                continue
            try:
                p = os.path.join(d, n)
                if now - os.path.getmtime(p) > SEEN_TTL:
                    os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


def forget_session(session, epoch=None):
    """Drop a session's seen-state so the NEXT turn re-fires everything.

    THE SESSION ID SURVIVES A COMPACTION AND THE CONTEXT DOES NOT. Measured
    2026-08-04 on the fire-ledger: ONE session carried 306 rows spanning
    2026-07-30 to 2026-08-04 — five days and several compactions under ONE id.
    So a suppression keyed on the session outlives the thing it is suppressing
    against, and without this call a deduped pinned lane would go silent for a
    seat that had just lost every premise it was suppressing. THAT IS STRICTLY
    WORSE THAN THE WASTE IT REPLACES, because a seat that lost its premises
    does not know it lost them.

    Called from the SessionStart legs, which are the only place helm learns
    that a context boundary happened. Idempotent: a missing file is already the
    state this wants.

    -> True when the suppression is gone, False when it provably SURVIVED.

    THIS ONE CANNOT FAIL SILENTLY, and it used to. Every OSError from unlink
    was swallowed, so a readable seen file under a NON-WRITABLE PARENT left the
    seat suppressed against content it had just lost — measured by a probe at
    6f7cb572 (dir chmod 0500, SessionStart source=startup: seen_survived=True,
    remained_suppressed=True). Everywhere else in this module fail-open is
    correct because the failure direction is a wasted re-delivery; HERE the
    directions invert, and a swallowed error is a MUTE SEAT.

    So it escalates instead of shrugging. Unlink needs write on the DIRECTORY;
    truncation needs write on the FILE, which is a different permission and the
    one that survives exactly this storage class. A truncated file reads as a
    fresh session (_seen_load fail-opens on empty), so it is not a lesser
    outcome — it is the same outcome by another syscall. Only when BOTH fail
    does this report, and the caller must be loud, because nothing downstream
    can infer that a boundary went unrecorded.

    `epoch` is the optional PROVENANCE of this reset — why the context went
    away — recorded by `_mark_epoch` on the way out so the first row of the
    new epoch can say it. Purely additive and purely diagnostic: omit it and
    this behaves exactly as it did, returning the same answer on the same
    legs. It NEVER decides anything here; nothing in this module reads it
    back, and the drop is attempted, judged and reported before the marker is
    considered at all."""
    path = _seen_path(session)
    try:
        with open(_seen_lock_path(session), "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            dropped = False
            try:
                os.remove(path)
                dropped = True
            except FileNotFoundError:
                dropped = True        # already the state this wants
            except OSError:
                pass                  # unlink refused — the parent, not the file
            if not dropped:
                try:
                    with open(path, "w"):
                        pass          # same meaning, different permission owner
                except OSError:
                    return False
            _mark_epoch(session, epoch)
            return True
    except FileNotFoundError:
        # NO DIRECTORY MEANS NO STATE TO DROP, and the drop is already done
        # by definition — but a boundary still happened, and this is the seat
        # that most needs it said: one resetting before it has ever injected.
        # Three arms went red here, because the lock file lives in the very
        # directory that does not exist, so the open above raises before the
        # marker is ever considered.
        _mark_epoch(session, epoch)
        return True
    except OSError:
        return False


def _seen_load(session):
    """Per-session suppression state: JIT cooldown plus content identities for
    the base pinned lane and the codex-only nudge tail.

    Fail-open: an absent/torn/alien-shaped file reads as a FRESH session (no
    suppression) — seen-state trouble must never block or crash the hook.
    `nudges` is optional for rolling compatibility: an older file re-delivers
    each line once, then records its exact content by ledger id. So is
    `lines`, the reflex lane's per-line repeat map (fingerprint -> turn).

    `epoch` is the reset provenance `_mark_epoch` may have left beside this
    file, and it is present only on a FRESH read — the turns immediately
    after a boundary, whose rows are the ones that show the reset. A loaded
    seen file means the epoch's first mutation already saved, so the
    boundary is behind us and the key is None.

    NONE IS AN ABSENCE AND NEVER A NEGATIVE. It is what an older marker-less
    fleet, a marker that could not be written, and a reset that carried no
    provenance all read as, and it says nothing whatever about the source or
    the vouch — which is the whole reason those two are recorded as words
    rather than as booleans."""
    path = _seen_path(session)
    fresh = {"turn": 0, "fired": {}, "pinned": None, "nudges": {},
             "lines": {}, "epoch": None, "who": None, "subs": [],
             "posture": None}
    if not os.path.exists(path):
        fresh["epoch"] = _epoch_load(session)
        return fresh
    try:
        with open(_seen_lock_path(session), "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            d = pk.read_json(path)
    except Exception:
        return fresh
    if not isinstance(d, dict) or not isinstance(d.get("turn"), int) \
            or d["turn"] < 0 or not isinstance(d.get("fired"), dict):
        # NO MARKER HERE, DELIBERATELY. A seen file that exists at all means
        # the last `forget_session` did NOT reach its unlink — the truncation
        # leg — and that leg needs the directory it could not write to in
        # order to replace or remove a marker. So a marker found beside a
        # present file may belong to an EARLIER boundary, and this branch
        # would read it as this one's. The unlink leg has no such window:
        # unlinking proves the directory is writable, so the marker there was
        # replaced or removed by the same call. Absence of provenance on a
        # degraded leg, rather than a confident wrong answer on it.
        return fresh
    fired = {str(i): r for i, r in d["fired"].items()
             if isinstance(r, list) and len(r) == 2
             and isinstance(r[0], int) and isinstance(r[1], (int, float))}
    pinned = d.get("pinned") if isinstance(d.get("pinned"), str) else None
    nudges = d.get("nudges") if isinstance(d.get("nudges"), dict) else {}
    nudges = {str(i): fp for i, fp in nudges.items() if isinstance(fp, str)}
    raw_lines = d.get("lines") if isinstance(d.get("lines"), dict) else {}
    lines = {str(fp): list(r) for fp, r in raw_lines.items()
             if isinstance(r, list) and len(r) == 2
             and all(isinstance(v, int) for v in r)}
    # THE ARRIVAL'S MEMORY (trigger design lane 2), each optional and typed
    # like every field above: the operator digest's content identity (WHO
    # rides once per context, typed turns only) and the recent notice
    # substances (a duplicate is silent).
    who = d.get("who") if isinstance(d.get("who"), str) else None
    subs = [f for f in (d.get("subs") or ()) if isinstance(f, str)] \
        if isinstance(d.get("subs"), list) else []
    # THE OWNER'S POSTURE AS THIS CONTEXT LAST SAW IT (ownernotice.turn_lines):
    # {"away": [kind, fp], "notice": [kind, fp]}, typed like every field here.
    # A malformed memo reads as none, which re-renders the current posture
    # once: the fail direction that never hides his word.
    posture = _posture_memo(d.get("posture"))
    return {"turn": d["turn"], "fired": fired, "pinned": pinned,
            "nudges": nudges, "lines": lines, "epoch": None, "who": who,
            "subs": subs, "posture": posture}


def _posture_memo(raw):
    """The posture memo, typed, or None."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k in ("away", "notice"):
        v = raw.get(k)
        if isinstance(v, list) and len(v) == 2 \
                and all(x is None or isinstance(x, str) for x in v):
            out[k] = list(v)
    return out or None


def _epoch_load(session):
    """The reset marker for a session whose seen file is ABSENT -> the
    provenance, or None when there is none to read.

    Read present-and-typed, like every other field here: an absent or
    unreadable file, a non-dict, a missing or non-string `source`, or a
    `vouch` outside EPOCH_VOUCHES all become None — an ABSENCE, which every
    reader already handles, rather than a fourth value nobody has an arm for.

    Consulted ONLY where the seen file is missing, which is the whole of its
    cost AND the whole of its staleness guarantee. The cost: once the epoch's
    first mutation saves a seen file, `_seen_load` takes the loaded path and
    never opens this one again, so this is read on the turns just after a
    boundary — the turns whose rows are the ones that need it — and on no
    other. The guarantee: the seen file is missing only because an unlink
    succeeded, which proves the directory was writable, which means the same
    `forget_session` replaced or removed whatever marker stood there. So a
    marker read here belongs to the boundary that made this file missing."""
    try:
        e = pk.read_json(_epoch_path(session))
    except Exception:
        return None
    if not isinstance(e, dict) or not isinstance(e.get("source"), str):
        return None
    if e.get("vouch") not in EPOCH_VOUCHES:
        return None
    return {"source": e["source"], "vouch": e["vouch"]}


def _seen_save(session, state):
    """Atomic write of one session's seen-state; stale SIBLING session files
    (mtime beyond SEEN_TTL) are pruned opportunistically. Entirely fail-open.

    THE FIRE MAP IS NEVER EVICTED, and that is deliberate. Every record in it
    is ACTIVELY SUPPRESSING content the seat still has, so ANY eviction policy
    silently re-enables delivery of whatever it drops — which is the exact
    defect this lane exists to remove, just at a different threshold. The old
    code pruned past COOLDOWN_TURNS and that is what made the window
    unremovable from _cooled alone; a count cap only moves the same hole to the
    Nth distinct id (a review of 66a58b1c: "those records are still
    suppressing content, so the 2001st distinct JIT id re-enables delivery
    without compaction").

    Nothing needs to bound it: the keys are STORE ENTRY IDS that actually
    fired, and forget_session drops the whole file at every context boundary,
    so its lifetime is one context rather than one session. Precisely (a
    review of 6f7cb572): the map can exceed the CURRENT store cardinality during
    same-context id churn, because an id whose entry was since removed is still
    retained — the bound is ids-fired-this-context, not entries-in-the-store-now.
    Operationally identical at helm's scale, and still bounded by construction,
    which is the only kind of bound that does not trade correctness for size."""
    try:
        turn = state["turn"]
        # THE LINE MAP IS THE ONE THING HERE THAT IS PRUNED, and the paragraph
        # above says why the fire map is not: a record past the repeat window
        # suppresses NOTHING (_unrepeated only consults the window), so
        # dropping it re-enables no delivery. Keying on line CONTENT rather
        # than on a store id is also what makes the pruning necessary — a
        # counter reflex mints a new fingerprint every turn it fires.
        lines = {fp: r for fp, r in (state.get("lines") or {}).items()
                 if isinstance(r, list) and len(r) == 2
                 and isinstance(r[1], int) and turn - r[1] < REPEAT_WINDOW_TURNS}
        # THE EPOCH MARKER IS DELIBERATELY NOT CARRIED FORWARD. It describes
        # the BOUNDARY, so it belongs to the first row after it and to no
        # other; re-writing it here would stamp the same provenance on every
        # row of the epoch, which is bytes on every turn to answer a question
        # asked once. Dropping it is what bounds it to the reset row.
        state = {"v": 1, "ts": pk.now_ts(), "turn": turn,
                 "pinned": state.get("pinned"),
                 "nudges": state.get("nudges", {}), "fired": state["fired"],
                 "lines": lines, "who": state.get("who"),
                 "subs": list(state.get("subs") or ())[-16:],
                 "posture": state.get("posture")}
        target = _seen_path(session)
        os.makedirs(_seen_dir(), exist_ok=True)
        with open(_seen_lock_path(session), "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            pk.write_json(target, state)
        now = time.time()
        d = _seen_dir()
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if p == target or not n.endswith(".json"):
                continue
            try:
                with open(p + ".lock", "a+") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if now - os.path.getmtime(p) > SEEN_TTL:
                        os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# lane-split eval — the read-only cohort analyzer (--lane-report)
# ---------------------------------------------------------------------------

def _cohort(e):
    """The lane-split-eval razor (ember's-claude vision review, 2026-07-19):
    'facts' = knowledge that makes a capable model fluent — lexicon terms,
    certain priors (premises / decisions-of-record), references; 'judgment' =
    steering that could anchor it — heuristic moves, sub-certain belief
    priors. None = not cohortable (reflex machinery, unknown ids)."""
    t = e.get("type")
    if t in ("lexicon", "reference", "capability"):
        # a wired capability is substrate TRUTH the agent should be fluent in
        return "facts"
    if t == "prior":
        return "facts" if e.get("class") == "certain" else "judgment"
    if t == "heuristic":
        return "judgment"
    return None


def _capture_generation(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return {"path": path, "fd": None, "size": 0, "exists": False,
                "generation": None}
    try:
        size = os.fstat(fd).st_size
        probe = os.pread(fd, min(size, 128), 0)
        if len(probe) != min(size, 128):
            raise OSError(errno.EIO, "short generation header read")
        return {"path": path, "fd": fd, "size": size, "exists": True,
                "generation": _generation(probe)}
    except Exception:
        os.close(fd)
        raise


def _capture_spool(path, strict=False):
    """One EX-locked generation and witness linearization boundary."""
    lock_fd = None
    captures = []
    try:
        lock_fd = _open_stable_lock(path)
        if not _acquire(lock_fd, fcntl.LOCK_EX):
            raise OSError(errno.EAGAIN, "ledger snapshot lock unavailable")
        queue = _queue_dir(path)
        try:
            queue_names = tuple(sorted(os.listdir(queue)))
        except FileNotFoundError:
            queue_names = ()
        state_names = tuple(sorted(os.listdir(os.path.dirname(path))))
        lock_size = os.fstat(lock_fd).st_size
        lock_data = _pread_exact(lock_fd, lock_size) if lock_size else b""
        captures = [_capture_generation(path + ".1"),
                    _capture_generation(path)]
    except OSError:
        for capture in captures:
            if capture["fd"] is not None:
                try:
                    os.close(capture["fd"])
                except OSError:
                    pass
        _release(lock_fd)
        if strict:
            raise
        return None
    _release(lock_fd)
    try:
        for capture in captures:
            fd = capture["fd"]
            capture["data"] = _pread_exact(fd, capture["size"]) \
                if fd is not None and capture["size"] else b""
            if fd is not None:
                os.close(fd)
                capture["fd"] = None
    except OSError:
        for capture in captures:
            if capture.get("fd") is not None:
                try:
                    os.close(capture["fd"])
                except OSError:
                    pass
        if strict:
            raise
        return None
    return captures, queue_names, state_names, lock_data


def _parse_generation(capture, canonical, rows, ledger_attempts,
                      bad_attempts, anonymous):
    data = capture["data"]
    header = _HEADER.match(data)
    if header:
        lines = data[header.end():].splitlines(keepends=True)
    else:
        lines = data.splitlines(keepends=True)
    for index, raw in enumerate(lines):
        if not raw.endswith(b"\n"):
            anonymous.add("torn:%s:%d" % (capture["path"], index))
            continue
        physical = raw[:-1]
        row, attempt = _decode_committed(physical)
        if row is None:
            envelope = _decode_ready(raw)
            if envelope is not None:
                row, attempt = envelope["row"], envelope["attempt"]
        if row is None:
            anonymous.add("malformed:%s:%d" % (capture["path"], index))
            continue
        if attempt and attempt in ledger_attempts:
            bad_attempts.add(attempt)
            continue
        if canonical is not None and not canonical(row):
            if attempt:
                bad_attempts.add(attempt)
            else:
                anonymous.add("noncanonical:%s:%d" % (capture["path"], index))
            continue
        rows.append(row)
        if attempt:
            ledger_attempts.add(attempt)


def _spool_rows(path, canonical=None, strict=False):
    captured = _capture_spool(path, strict=strict)
    if captured is None:
        return LedgerRows(complete=None, skipped=1)
    generations, queue_names, state_names, lock_data = captured
    rows, ledger_attempts, bad_attempts, anonymous = [], set(), set(), set()
    source_canonical = True
    for generation in generations:
        if generation["exists"] and generation["generation"] is None:
            source_canonical = False
        _parse_generation(generation, canonical, rows, ledger_attempts,
                          bad_attempts, anonymous)
    live = {generation["generation"] for generation in generations
            if generation["generation"]}
    tombstoned = {matched.group(1) for name in queue_names
                  for matched in [_COMMITTED_NAME.fullmatch(name)] if matched}
    for name in queue_names:
        matched = _evidence_match(name)
        if matched and (matched.group(1) in ledger_attempts
                        or matched.group(1) in tombstoned):
            continue
        matched = _INTENT_NAME.fullmatch(name) or _READY_NAME.fullmatch(name)
        if matched:
            bad_attempts.add(matched.group(1))
            continue
        matched = _INFLIGHT_NAME.fullmatch(name)
        if matched:
            bad_attempts.add(matched.group(1))
            continue
        matched = _UNKNOWN_NAME.fullmatch(name)
        if matched:
            if matched.group(2) in live:
                bad_attempts.add(matched.group(1))
            continue
        if _COMMITTED_NAME.fullmatch(name):
            continue
        anonymous.add("queue:" + name)
    base = os.path.basename(path)
    for name in state_names:
        if name.startswith(base + ".rotate.") \
                or name.startswith(base + ".header."):
            anonymous.add("state:" + name)
        elif _is_legacy_name(path, name):
            bad_attempts.add(_legacy_attempt("legacy-file:" + name))
    for index, line in enumerate(lock_data.splitlines()):
        bad_attempts.add(_legacy_attempt(
            "legacy-lock:%d:" % index + line.decode("utf-8", "surrogateescape")))
    current = generations[1]
    initialized = current["exists"] and current["generation"] is not None
    skipped = len(bad_attempts | {"anonymous:" + item for item in anonymous})
    complete = bool(initialized and source_canonical and not skipped)
    return LedgerRows(rows, complete=complete, skipped=skipped)


def _legacy_jsonl(path, canonical=None, strict=False):
    rows, skipped, unavailable = [], 0, False
    for candidate in (path + ".1", path):
        try:
            data = _read_path(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            if strict:
                raise
            unavailable = True
            skipped += 1
            continue
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                skipped += 1
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                skipped += 1
                continue
            if isinstance(row, dict) and (canonical is None or canonical(row)):
                rows.append(row)
            else:
                skipped += 1
    return LedgerRows(rows, complete=None if unavailable else not skipped,
                      skipped=skipped)


def _read_jsonl(path, canonical=None, strict=False):
    """Readable rows oldest-first, preserving exact/incomplete/unavailable truth."""
    if path == _ledger_path() or os.path.isdir(_queue_dir(path)):
        return _spool_rows(path, canonical=canonical, strict=strict)
    return _legacy_jsonl(path, canonical=canonical, strict=strict)


def _valid_fired(fired):
    required = {"pinned", "jit", "reflex"}
    keys = set(fired) if isinstance(fired, dict) else set()
    return isinstance(fired, dict) \
        and keys in (required, set(injection_schema.V2_LANES)) \
        and all(isinstance(ids, list)
                and all(isinstance(value, str) for value in ids)
                for ids in fired.values()) \
        and ("whisper" not in fired or bool(fired["whisper"])) \
        and any(fired.values())


def _valid_exact_sample(row):
    if row.get("v") not in injection_schema.EXACT_VERSIONS:
        return False
    sample = row.get("sample")
    if not isinstance(sample, dict) or sample.get("encoding") != "utf-8":
        return False
    lanes = sample.get("lane_bytes")
    rendered = sample.get("rendered_bytes")
    return isinstance(lanes, dict) \
        and set(lanes) == set(injection_schema.V2_LANES) \
        and all(type(lanes[lane]) is int and lanes[lane] >= 0
                for lane in injection_schema.V2_LANES) \
        and type(rendered) is int \
        and rendered == sum(lanes.values()) + sum(
            value > 0 for value in lanes.values())


def _valid_exact_context(row):
    context, sources = row.get("context"), row.get("context_sources")
    if not isinstance(context, dict) or not isinstance(sources, dict) \
            or set(context) != set(sources) \
            or not set(context).issubset({
                "session", "cwd", "harness", "config_home"}):
        return False
    if "session" in context and (not isinstance(context["session"], str)
                                 or context["session"] != row.get("session")
                                 or row["v"] == injection_schema.V2
                                 and sources["session"] !=
                                 injection_schema.SESSION_SOURCE):
        return False
    if "cwd" in context and (not isinstance(context["cwd"], str)
                             or not os.path.isabs(context["cwd"])
                             or row["v"] == injection_schema.V2
                             and sources["cwd"] != injection_schema.CWD_SOURCE):
        return False
    if row["v"] == injection_schema.V2:
        harness = context.get("harness")
        if harness is not None and (not isinstance(harness, str)
                                    or sources.get("harness") not in
                                    injection_schema.HARNESS_SOURCES.get(
                                        harness, ())):
            return False
        if "config_home" in context:
            return bool(harness) \
                and isinstance(context["config_home"], str) \
                and os.path.isabs(context["config_home"]) \
                and sources["config_home"] == \
                injection_schema.CONFIG_HOME_ENV.get(harness)
        return "config_home" not in sources
    if not context:
        return not sources and "runtime" not in row
    return all(name in context for name in
               ("session", "cwd", "harness", "config_home")) \
        and injection_schema.context_value(row, "session") is not None \
        and injection_schema.context_value(row, "cwd") is not None \
        and injection_schema.context_value(row, "harness") is not None \
        and injection_schema.config_home(row) is not None


def _canonical_ledger_row(row):
    version = row.get("v") if isinstance(row, dict) else None
    if type(version) is not int \
            or not isinstance(row.get("ts"), str) or not row["ts"] \
            or version != 1 and "project" not in row \
            or not ("project" not in row or row["project"] is None
                    or isinstance(row["project"], str)) \
            or not ("session" not in row or isinstance(row["session"], str)) \
            or not ("suppressed" not in row
                    or isinstance(row["suppressed"], list)
                    and all(isinstance(value, str)
                            for value in row["suppressed"])):
        return False
    fired = row.get("fired")
    if version == 1:
        if _valid_fired(fired) and "silent" not in row:
            measured = row.get("bytes")
            return measured is None or isinstance(measured, dict) \
                and all(key in measured for key in
                        ("pinned", "jit", "reflex")) \
                and all(type(value) is int and value >= 0
                        for value in measured.values())
        return row.get("silent") is True and "fired" not in row \
            and "bytes" not in row
    if not _valid_exact_sample(row) or not _valid_exact_context(row):
        return False
    lanes = row["sample"]["lane_bytes"]
    if _valid_fired(fired) and "silent" not in row:
        return all(bool(fired.get(lane)) == bool(lanes[lane])
                   for lane in injection_schema.V2_LANES)
    return row.get("silent") is True and "fired" not in row \
        and row["sample"]["rendered_bytes"] == 0


def _ledger_rows(strict=False):
    """Canonical fire-ledger rows plus tri-state completeness, oldest-first."""
    return _spool_rows(_ledger_path(), canonical=_canonical_ledger_row,
                       strict=strict)


def lane_report(project=None):
    """The lane-split eval's accumulating instrument, READ-ONLY: every fired
    id in the ledger classified via _cohort against the CURRENT store (the
    operator digest who:operator = facts/profile), with per-cohort fires,
    distinct ids, byte estimate (today's rendering x fires — per-entry bytes
    are not ledgered), session spread, cooldown suppression, and the
    silent-rate first-half vs second-half trend. DELIVERY ONLY: the ledger
    logs fires, not heeds — anchoring is NOT measurable here; it needs
    per-turn outcome markers, which the ledger does not record."""
    rows = _ledger_rows()
    try:
        by_id = {str(e["id"]): e for e in load_entries(project)}
    except Exception:
        by_id = {}
    # UTF-8 BYTES, not characters: this feeds a field named bytes and a
    # bytes~ column. len() on a str is the right magnitude in the wrong
    # unit and diverges on any non-ASCII entry. task/2691.
    who_bytes = sum(len(l.encode("utf-8", "replace")) for l in _who_lines())

    def cohort(i):
        if i == WHO_ID:
            return "facts"  # the operator profile
        e = by_id.get(i)
        return (e and _cohort(e)) or "other"

    c = {k: {"fires": 0, "ids": set(), "bytes": 0, "sessions": set(),
             "suppressed": 0} for k in ("facts", "judgment", "other")}
    fired_rows = silent = 0
    sessions = set()
    halves = [[0, 0], [0, 0]]  # [silent, rows] per ledger half
    for n, r in enumerate(rows):
        half = halves[n * 2 // len(rows)]
        half[1] += 1
        s = r.get("session")
        if s:
            sessions.add(s)
        if r.get("silent"):
            silent += 1
            half[0] += 1
        else:
            fired_rows += 1
        for ids in (r.get("fired") or {}).values():
            for i in map(str, ids):
                d = c[cohort(i)]
                d["fires"] += 1
                d["ids"].add(i)
                e = by_id.get(i)
                d["bytes"] += len(_entry_line(e).encode("utf-8", "replace")) \
                    if e else (who_bytes if i == WHO_ID else 0)
                if s:
                    d["sessions"].add(s)
        for i in map(str, r.get("suppressed") or ()):
            c[cohort(i)]["suppressed"] += 1
    return {"rows": len(rows), "fired_rows": fired_rows, "silent": silent,
            "sessions": len(sessions), "halves": halves,
            "complete": rows.complete, "skipped": rows.skipped,
            "cohorts": {k: {"fires": d["fires"], "ids": len(d["ids"]),
                            "bytes": d["bytes"],
                            "sessions": len(d["sessions"]),
                            "suppressed": d["suppressed"]}
                        for k, d in c.items()}}


def _pct(num, den):
    return 100.0 * num / den if den else 0.0


def _lane_report(project=None):
    """--lane-report: lane_report() rendered as the cohort table. No ledger
    row, no state mutation — an analyzer must never count as a turn."""
    r = lane_report(project)
    if r["complete"] is not True:
        print("lane-report: source census incomplete — totals, rates, trends, "
              "and cohort absence are UNKNOWN.")
        print("Readable canonical rows remain available through the ledger API "
              "as lower-bound evidence only.")
        return 0
    if not r["rows"]:
        print("lane-report: no ledger rows yet — the instrument is unfired.")
        return 0
    print("lane-split cohorts — %d rows (%d fired, %d silent), %d sessions" % (
        r["rows"], r["fired_rows"], r["silent"], r["sessions"]))
    total_f = sum(d["fires"] for d in r["cohorts"].values())
    total_b = sum(d["bytes"] for d in r["cohorts"].values())
    fmt = "%-9s %6s %6s %5s %8s %6s %5s %5s %6s"
    print(fmt % ("cohort", "fires", "share", "ids", "bytes~", "share",
                 "sess", "supp", "supp%"))
    for k in ("facts", "judgment", "other"):
        d = r["cohorts"][k]
        print("%-9s %6d %5.1f%% %5d %8d %5.1f%% %5d %5d %5.1f%%" % (
            k, d["fires"], _pct(d["fires"], total_f), d["ids"], d["bytes"],
            _pct(d["bytes"], total_b), d["sessions"], d["suppressed"],
            _pct(d["suppressed"], d["fires"] + d["suppressed"])))
    h1, h2 = r["halves"]
    print("silent rate: %.1f%% overall | first half %.1f%% -> second half %.1f%%" % (
        _pct(r["silent"], r["rows"]), _pct(h1[0], h1[1]), _pct(h2[0], h2[1])))
    print("bytes~ = today's rendering x fires (per-entry bytes are not ledgered).")
    print("DELIVERY ONLY: fires are not heeds — the anchoring verdict needs")
    print("per-turn outcome markers, which the ledger does not record.")
    return 0
