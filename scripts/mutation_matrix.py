#!/usr/bin/env python3
"""Run byte-exact source mutations from an immutable committed baseline."""

import json
import math
import os
import posixpath
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass


_MAX_TIMEOUT_SECONDS = 86400.0

_UNITTEST_HARNESS = r'''
import json
import os
import sys
import unittest


def run(receipt_fd):
    os.set_inheritable(receipt_fd, False)
    test_args = sys.argv[1:]
    sys.argv[:] = ["mutation-matrix"] + test_args
    program = unittest.main(module=None, argv=list(sys.argv), exit=False)
    result = program.result
    import_failed = any(
        test.__class__.__module__ == "unittest.loader"
        and test.__class__.__name__ == "_FailedTest"
        for test, _traceback in result.errors
    )
    payload = json.dumps({
        "status": "OK" if result.wasSuccessful() else "FAILED",
        "count": result.testsRun,
        "import_failed": import_failed,
    }, sort_keys=True).encode("utf-8")
    while payload:
        written = os.write(receipt_fd, payload)
        payload = payload[written:]
    os.close(receipt_fd)
    return 0 if result.wasSuccessful() else 1


raise SystemExit(run(%d))
'''


class MatrixError(Exception):
    """A matrix result cannot be trusted."""


@dataclass(frozen=True)
class HeadIdentity:
    commit: str
    symbolic_ref: str


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    find: bytes
    replace: bytes


@dataclass(frozen=True)
class Target:
    path: str
    mode: str
    oid: str
    index_tag: str
    worktree_bytes: bytes
    worktree_mode: int


@dataclass(frozen=True)
class TestResult:
    returncode: int
    status: str
    count: int
    import_failed: bool


def _one_line(value):
    return " ".join(str(value).split())


def _emit(line):
    print(line, flush=True)


def _git_process(root, args, stdout):
    try:
        return subprocess.run(
            ("git",) + args,
            cwd=root,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MatrixError("git %s could not run: %s" %
                          (args[0], _one_line(exc)))


def _git_failure(args, proc):
    detail = os.fsdecode(proc.stderr).strip() or "exit %d" % proc.returncode
    return MatrixError("git %s failed: %s" %
                       (args[0], _one_line(detail)))


def _git(root, *args):
    proc = _git_process(root, args, subprocess.PIPE)
    if proc.returncode != 0:
        raise _git_failure(args, proc)
    return proc.stdout


def _git_quiet(root, *args):
    proc = _git_process(root, args, subprocess.DEVNULL)
    if proc.returncode not in (0, 1):
        raise _git_failure(args, proc)
    return proc.returncode == 0


def _repo_root():
    root = os.fsdecode(_git(os.getcwd(), "rev-parse", "--show-toplevel")).strip()
    if not root:
        raise MatrixError("git returned an empty repository root")
    return os.path.realpath(root)


def _commit(root):
    value = os.fsdecode(
        _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    ).strip()
    if len(value) not in (40, 64) or not re.fullmatch(r"[0-9a-f]+", value):
        raise MatrixError("git returned a non-full HEAD identity: %r" % value)
    return value


def _head_identity(root):
    commit = _commit(root)
    args = ("symbolic-ref", "-q", "HEAD")
    proc = _git_process(root, args, subprocess.PIPE)
    if proc.returncode == 0:
        ref = os.fsdecode(proc.stdout).strip()
        if not ref:
            raise MatrixError("git returned an empty symbolic HEAD")
    elif proc.returncode == 1:
        ref = None
    else:
        raise _git_failure(args, proc)
    return HeadIdentity(commit, ref)


def _verify_head(root, expected):
    actual = _head_identity(root)
    if actual != expected:
        raise MatrixError(
            "HEAD identity changed from %s@%s to %s@%s" % (
                expected.symbolic_ref or "DETACHED",
                expected.commit,
                actual.symbolic_ref or "DETACHED",
                actual.commit,
            )
        )


def _literal(path):
    return ":(literal)" + path


def _single_entry(raw, path, error):
    rows = [row for row in raw.split(b"\0") if row]
    if len(rows) != 1:
        raise MatrixError(error % path)
    meta, tab, name = rows[0].partition(b"\t")
    if not tab or os.fsdecode(name) != path:
        raise MatrixError(error % path)
    return meta.split()


def _tree_entry(root, head, path):
    raw = _git(root, "ls-tree", "-z", head, "--", _literal(path))
    error = "target is not exactly one tracked HEAD blob: %s"
    fields = _single_entry(raw, path, error)
    if len(fields) != 3 or fields[1] != b"blob":
        raise MatrixError(error % path)
    return fields[0].decode("ascii"), fields[2].decode("ascii")


def _index_entry(root, path):
    raw = _git(root, "ls-files", "--stage", "-z", "--", _literal(path))
    error = "target has no single stage-0 index entry: %s"
    fields = _single_entry(raw, path, error)
    if len(fields) != 3 or fields[2] != b"0":
        raise MatrixError(error % path)
    return fields[0].decode("ascii"), fields[1].decode("ascii")


def _index_tag(root, path):
    raw = _git(root, "ls-files", "-v", "-z", "--", _literal(path))
    rows = [row for row in raw.split(b"\0") if row]
    if len(rows) != 1:
        raise MatrixError("target has no semantic index identity: %s" % path)
    tag, space, name = rows[0].partition(b" ")
    if not space or len(tag) != 1 or os.fsdecode(name) != path:
        raise MatrixError("target has no semantic index identity: %s" % path)
    return tag.decode("ascii")


def _target_path(root, path):
    return os.path.join(root, *path.split("/"))


def _target_clean(root, path):
    literal = _literal(path)
    if not _git_quiet(root, "diff", "--quiet", "--cached", "--", literal):
        raise MatrixError("target is dirty in the index: %s" % path)
    if not _git_quiet(root, "diff", "--quiet", "--", literal):
        raise MatrixError("target is dirty in the worktree: %s" % path)


def _load_spec(path):
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MatrixError("cannot read JSON spec: %s" % _one_line(exc))
    if not isinstance(spec, dict):
        raise MatrixError("JSON spec must be an object")
    command = spec.get("command")
    if not isinstance(command, list) or not command or \
            any(not isinstance(part, str) or not part or "\0" in part
                for part in command):
        raise MatrixError("command must be a nonempty string array")
    if len(command) < 3 or command[1:3] != ["-m", "unittest"]:
        raise MatrixError("command must be PYTHON -m unittest [ARGS...]")
    timeout_value = spec.get("timeout_seconds", 300)
    if isinstance(timeout_value, bool) or \
            not isinstance(timeout_value, (int, float)):
        raise MatrixError("timeout_seconds must be a finite positive number")
    try:
        timeout = float(timeout_value)
    except (ValueError, OverflowError):
        raise MatrixError("timeout_seconds must be a finite positive number")
    if not math.isfinite(timeout) or timeout <= 0 or \
            timeout > _MAX_TIMEOUT_SECONDS:
        raise MatrixError("timeout_seconds must be finite, positive, and at most %g" %
                          _MAX_TIMEOUT_SECONDS)
    rows = spec.get("mutations")
    if not isinstance(rows, list) or not rows:
        raise MatrixError("mutations must be a nonempty array")
    mutations = []
    names = set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise MatrixError("mutation %d must be an object" % number)
        name = row.get("name")
        path_value = row.get("path")
        find = row.get("find")
        replace = row.get("replace")
        if not isinstance(name, str) or not name or name != name.strip() or \
                any(ord(char) < 32 or ord(char) > 126 for char in name):
            raise MatrixError(
                "mutation %d needs a nonempty printable-ASCII one-line name" %
                number
            )
        if name in names:
            raise MatrixError("duplicate mutation name: %s" % name)
        names.add(name)
        if not isinstance(path_value, str) or not path_value:
            raise MatrixError("mutation %s needs a relative path" % name)
        try:
            path_value.encode("utf-8")
        except UnicodeEncodeError:
            raise MatrixError(
                "mutation %s path must be valid UTF-8 text" % name
            )
        if not all(char.isprintable() for char in path_value):
            raise MatrixError(
                "mutation %s path must contain only printable characters" % name
            )
        normalized = posixpath.normpath(path_value)
        if normalized != path_value or normalized in (".", "..") or \
                normalized.startswith("../") or normalized.startswith("/"):
            raise MatrixError("mutation %s path must be normalized and relative" %
                              name)
        if not isinstance(find, str) or not isinstance(replace, str):
            raise MatrixError("mutation %s find/replace must be JSON strings" % name)
        try:
            find_bytes = find.encode("utf-8")
            replace_bytes = replace.encode("utf-8")
        except UnicodeEncodeError:
            raise MatrixError(
                "mutation %s find/replace must be valid UTF-8 text" % name
            )
        mutations.append(Mutation(name, path_value, find_bytes, replace_bytes))
    return tuple(command), timeout, tuple(mutations)


def _prepare_targets(root, head, mutations):
    targets = {}
    for mutation in mutations:
        if mutation.path in targets:
            continue
        mode, oid = _tree_entry(root, head.commit, mutation.path)
        if mode not in ("100644", "100755"):
            raise MatrixError("target is not a regular source file: %s" %
                              mutation.path)
        _target_clean(root, mutation.path)
        if _index_entry(root, mutation.path) != (mode, oid):
            raise MatrixError("target index identity differs from HEAD: %s" %
                              mutation.path)
        tag = _index_tag(root, mutation.path)
        if tag != "H":
            raise MatrixError("target has semantic index flag %s: %s" %
                              (tag, mutation.path))
        full = _target_path(root, mutation.path)
        try:
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                raise MatrixError("target is not a regular worktree file: %s" %
                                  mutation.path)
            with open(full, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            raise MatrixError("cannot read target %s: %s" %
                              (mutation.path, _one_line(exc)))
        targets[mutation.path] = Target(
            mutation.path,
            mode,
            oid,
            tag,
            body,
            stat.S_IMODE(info.st_mode),
        )
    for mutation in mutations:
        target = targets[mutation.path]
        count = target.worktree_bytes.count(mutation.find)
        if count != 1:
            raise MatrixError("mutation %s anchor occurs %d times, expected 1" %
                              (mutation.name, count))
        if mutation.find == mutation.replace:
            raise MatrixError("mutation %s replacement is byte-identical" %
                              mutation.name)
    return targets


def _atomic_write(path, body, mode):
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".mutation-matrix-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise MatrixError("atomic mutation write failed: %s" % _one_line(exc))
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _apply(root, target, mutation):
    try:
        with open(_target_path(root, target.path), "rb") as handle:
            current = handle.read()
    except OSError as exc:
        raise MatrixError("cannot read target before mutation: %s" %
                          _one_line(exc))
    if current != target.worktree_bytes:
        raise MatrixError("target bytes changed before mutation: %s" % target.path)
    if current.count(mutation.find) != 1:
        raise MatrixError("mutation %s anchor is no longer exact-one" % mutation.name)
    mutated = current.replace(mutation.find, mutation.replace, 1)
    if mutated == current:
        raise MatrixError("mutation %s produced no byte change" % mutation.name)
    full = _target_path(root, target.path)
    _atomic_write(full, mutated, target.worktree_mode)
    try:
        with open(full, "rb") as handle:
            written = handle.read()
    except OSError as exc:
        raise MatrixError("cannot prove mutation bytes: %s" % _one_line(exc))
    if written != mutated or written == target.worktree_bytes:
        raise MatrixError("mutation %s bytes were not applied exactly" % mutation.name)


def _restore(root, head, target):
    _git(root, "restore", "--source=" + head.commit, "--staged", "--worktree",
         "--", _literal(target.path))


def _restore_all(root, head, targets):
    errors = []
    for target in targets.values():
        try:
            _restore(root, head, target)
        except MatrixError as exc:
            errors.append(str(exc))
    if errors:
        raise MatrixError("; ".join(errors))


def _verify_repository(root, head, targets, all_tracked):
    _verify_head(root, head)
    literals = tuple(_literal(target.path) for target in targets.values())
    paths = () if all_tracked else literals
    if not _git_quiet(root, "diff", "--quiet", "--cached", "--", *paths):
        raise MatrixError("cached diff remains after restore")
    if not _git_quiet(root, "diff", "--quiet", "--", *paths):
        raise MatrixError("worktree diff remains after restore")
    for target in targets.values():
        if _index_entry(root, target.path) != (target.mode, target.oid):
            raise MatrixError("target index blob/mode was not restored: %s" %
                              target.path)
        tag = _index_tag(root, target.path)
        if tag != target.index_tag:
            raise MatrixError("target semantic index flag changed %s -> %s: %s" %
                              (target.index_tag, tag, target.path))
        full = _target_path(root, target.path)
        try:
            info = os.lstat(full)
            with open(full, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            raise MatrixError("cannot verify restored target %s: %s" %
                              (target.path, _one_line(exc)))
        if not stat.S_ISREG(info.st_mode) or \
                stat.S_IMODE(info.st_mode) != target.worktree_mode:
            raise MatrixError("target worktree mode was not restored: %s" %
                              target.path)
        if body != target.worktree_bytes:
            raise MatrixError("target worktree bytes were not restored: %s" %
                              target.path)
        oid = os.fsdecode(
            _git(root, "hash-object", "--path=" + target.path, "--", target.path)
        ).strip()
        if oid != target.oid:
            raise MatrixError("target worktree blob was not restored: %s" %
                              target.path)


def _process_group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_process(proc):
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1
    while _process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MatrixError("test command timed out before receipt completed")
    return remaining


def _drain_receipt(fd, deadline):
    os.set_blocking(fd, False)
    chunks = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            ready, _write, _error = select.select([fd], [], [],
                                                   _remaining(deadline))
            if not ready:
                raise MatrixError("test command timed out before receipt completed")
            continue
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_receipt(payload, returncode):
    try:
        receipt = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MatrixError("runner-owned unittest receipt missing/unreadable: %s" %
                          _one_line(exc))
    if not isinstance(receipt, dict):
        raise MatrixError("runner-owned unittest receipt has invalid shape")
    status = receipt.get("status")
    count = receipt.get("count")
    import_failed = receipt.get("import_failed")
    if status not in ("OK", "FAILED") or isinstance(count, bool) or \
            not isinstance(count, int) or count < 0 or \
            not isinstance(import_failed, bool):
        raise MatrixError("runner-owned unittest receipt has invalid fields")
    return TestResult(returncode, status, count, import_failed)


def _run_test(root, command, timeout):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        read_fd, write_fd = os.pipe()
    except OSError as exc:
        raise MatrixError("test receipt pipe could not open: %s" %
                          _one_line(exc))
    proc = None
    try:
        harness = _UNITTEST_HARNESS % write_fd
        argv = (command[0], "-c", harness) + tuple(command[3:])
        try:
            proc = subprocess.Popen(
                argv,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                env=env,
                start_new_session=True,
                pass_fds=(write_fd,),
            )
        except (OSError, ValueError, OverflowError) as exc:
            raise MatrixError("test command could not run: %s" %
                              _one_line(exc))
        os.close(write_fd)
        write_fd = None
        deadline = time.monotonic() + timeout
        try:
            proc.communicate(timeout=_remaining(deadline))
        except subprocess.TimeoutExpired:
            _stop_process(proc)
            raise MatrixError("test command timed out after %gs" % timeout)
        except (ValueError, OverflowError) as exc:
            _stop_process(proc)
            raise MatrixError("test timeout failed at runtime: %s" %
                              _one_line(exc))
        except KeyboardInterrupt:
            _stop_process(proc)
            raise MatrixError("test command interrupted")
        except MatrixError:
            _stop_process(proc)
            raise
        if _process_group_alive(proc.pid):
            _stop_process(proc)
            raise MatrixError("test command left descendant processes running")
        try:
            payload = _drain_receipt(read_fd, deadline)
        except MatrixError:
            _stop_process(proc)
            raise
        os.close(read_fd)
        read_fd = None
        return _read_receipt(payload, proc.returncode)
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if read_fd is not None:
            os.close(read_fd)


def _validate_result(result, expected=None):
    if result.import_failed:
        raise MatrixError("test collection/import failed")
    if expected is not None and result.count != expected:
        raise MatrixError("test count changed from %d to %d" %
                          (expected, result.count))


def _green_count(result, expected=None):
    _validate_result(result, expected)
    if result.count <= 0:
        raise MatrixError("green control ran zero tests")
    if result.returncode != 0 or result.status != "OK":
        raise MatrixError("green control failed with exit %d and %s" %
                          (result.returncode, result.status))
    return result.count


def _classify(result, expected):
    _validate_result(result, expected)
    if result.returncode == 0 and result.status == "OK":
        return "SURVIVED"
    if result.returncode == 1 and result.status == "FAILED":
        return "KILLED"
    raise MatrixError("test exit %d disagrees with unittest status %s" %
                      (result.returncode, result.status))


def _materialize_checkout(source, head, checkout):
    _git(source, "clone", "--quiet", "--shared", "--no-checkout", source,
         checkout)
    _git(checkout, "remote", "remove", "origin")
    _git(checkout, "checkout", "--quiet", "--detach", head.commit)
    expected = HeadIdentity(head.commit, None)
    _verify_head(checkout, expected)
    return expected


def _observation(source, caller_head, targets, command, timeout, mutation=None):
    container = tempfile.mkdtemp(prefix="helm-mutation-checkout-")
    checkout = os.path.join(container, "repo")
    checkout_head = None
    result = None
    errors = []
    try:
        checkout_head = _materialize_checkout(source, caller_head, checkout)
        _verify_repository(checkout, checkout_head, targets, all_tracked=True)
        if mutation is not None:
            _apply(checkout, targets[mutation.path], mutation)
        result = _run_test(checkout, command, timeout)
    except MatrixError as exc:
        errors.append(str(exc))
    except KeyboardInterrupt:
        errors.append("matrix interrupted")
    finally:
        if checkout_head is not None:
            try:
                _restore_all(checkout, checkout_head, targets)
            except MatrixError as exc:
                errors.append(str(exc))
            try:
                _verify_repository(checkout, checkout_head, targets,
                                   all_tracked=True)
            except MatrixError as exc:
                errors.append(str(exc))
        try:
            shutil.rmtree(container)
        except OSError as exc:
            errors.append("disposable checkout cleanup failed: %s" %
                          _one_line(exc))
    caller_safe = True
    try:
        _verify_repository(source, caller_head, targets, all_tracked=False)
    except MatrixError as exc:
        errors.append(str(exc))
        caller_safe = False
    return result, errors, caller_safe


def _matrix_error(context, errors):
    detail = "; ".join(_one_line(error) for error in errors if error)
    _emit("MATRIX-ERROR %s: %s" % (context, detail or "unknown failure"))


def run(spec_path):
    try:
        command, timeout, mutations = _load_spec(spec_path)
        root = _repo_root()
        head = _head_identity(root)
        targets = _prepare_targets(root, head, mutations)
        _verify_repository(root, head, targets, all_tracked=False)
    except MatrixError as exc:
        _matrix_error("preflight", (str(exc),))
        return 2

    baseline, errors, caller_safe = _observation(
        root, head, targets, command, timeout
    )
    if not errors:
        try:
            count = _green_count(baseline)
        except MatrixError as exc:
            errors.append(str(exc))
    if errors:
        _matrix_error("baseline", errors)
        return 2
    _emit("BASELINE GREEN (%d tests)" % count)

    survived = 0
    matrix_failed = False
    for mutation in mutations:
        result, errors, caller_safe = _observation(
            root, head, targets, command, timeout, mutation
        )
        if not errors:
            try:
                verdict = _classify(result, count)
            except MatrixError as exc:
                errors.append(str(exc))
        if errors:
            _matrix_error(mutation.name, errors)
            matrix_failed = True
            break
        survived += verdict == "SURVIVED"
        _emit("%s %s (%d tests)" % (verdict, mutation.name, result.count))

    if caller_safe:
        control, errors, caller_safe = _observation(
            root, head, targets, command, timeout
        )
        if not errors:
            try:
                _green_count(control, count)
            except MatrixError as exc:
                errors.append(str(exc))
        if errors:
            _matrix_error("final-control", errors)
            matrix_failed = True
        else:
            _emit("CONTROL GREEN (%d tests)" % count)

    if matrix_failed or not caller_safe:
        return 2
    killed = len(mutations) - survived
    if survived:
        _emit("SUMMARY %d KILLED, %d SURVIVED" % (killed, survived))
        return 1
    _emit("ALL-KILLED %d/%d" % (killed, len(mutations)))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        stream = sys.stdout if argv and argv[0] in ("-h", "--help") else sys.stderr
        print("usage: mutation_matrix.py SPEC.json", file=stream)
        return 0 if stream is sys.stdout else 2
    return run(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
