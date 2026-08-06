#!/usr/bin/env python3
"""helm proxy-fork-watch — announce semantic upstream debt in the durable proxy fork.

The Go checker in the durable CLIProxyAPI clone owns merge/test truth. Helm owns
only the host-side transaction around it: validate the clone, run and parse the
checker, compare a semantic fingerprint, post one compact room line, and persist
the attempt atomically. Read-only invocations never touch notification state.
"""
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys

from . import home, pk, vcs

BRANCH = "helm/upstream-tracking"
CHECKER = ("go", "run", "-mod=readonly", "./cmd/helm-upstream-check")
CHECK_TIMEOUT_S = 40 * 60
GIT_TIMEOUT_S = 15
SYSTEMCTL_TIMEOUT_S = 30
_STATE = "proxy-fork-watch.json"
_SERVICE_NAME = "helm-proxy-fork-watch.service"
_TIMER_NAME = "helm-proxy-fork-watch.timer"
_STATUSES = frozenset(("CLEAN", "CONFLICT", "TEST-FAIL"))
_FIELDS = (
    "schema", "branch", "branch_tip", "upstream_tip", "status",
    "conflict_files", "failing_packages", "test_targets",
)

_SERVICE = """[Unit]
Description=helm proxy fork upstream watch (one pass)

[Service]
Type=oneshot
ExecStart=%h/.local/bin/helm proxy-fork-watch --post
SuccessExitStatus=1
TimeoutStartSec=45m
Nice=10
IOSchedulingClass=idle
"""

_TIMER = """[Unit]
Description=helm proxy fork upstream watch daily cadence

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
"""

_USAGE = """usage: helm proxy-fork-watch [--json] [--post] [--force] [--install-timer]

  Run the Go upstream checker in the durable CLIProxyAPI clone. Bare and --json
  are read-only. --post serializes checker + state decision + one #helm post +
  atomic state write; it posts first run, semantic changes, and one recovery.
  --force posts regardless. --install-timer installs the daily user timer.
"""


class WatchError(Exception):
    """A checker-boundary failure with a stable fingerprint category."""

    def __init__(self, category, detail):
        super().__init__(detail)
        self.category = category
        self.detail = detail


def clone_dir():
    """The one durable clone path. HELM_PROXY_FORK_DIR is strict: no legacy."""
    if "HELM_PROXY_FORK_DIR" in os.environ:
        return os.path.expanduser(os.environ["HELM_PROXY_FORK_DIR"])
    return os.path.join(home.helm_home(), home.GLOBAL, "proxy-fork", "CLIProxyAPI")


def state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def _safe_detail(value, limit=300):
    return " ".join(str(value or "").split())[:limit]


def _inspect_clone(path=None):
    """Return (clone, HEAD), proving the checker exists and HEAD names BRANCH."""
    path = clone_dir() if path is None else path
    if not path or not os.path.isdir(path):
        raise WatchError("missing-clone", "durable clone is missing: %s" % (path or "<empty>"))
    checker = os.path.join(path, "cmd", "helm-upstream-check")
    if not os.path.isdir(checker):
        raise WatchError("missing-checker", "checker directory is missing: %s" % checker)
    try:
        r = vcs.backend(path).proc(
            path, "status", "--porcelain=v2", "--branch", "--untracked-files=no",
            timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise WatchError("clone-timeout", "git clone validation timed out")
    except UnicodeError as e:
        raise WatchError("invalid-clone", "git clone identity is not valid text: %s" % e)
    except OSError as e:
        raise WatchError("clone-check", "git clone validation could not start: %s" % e)
    if r.returncode != 0:
        raise WatchError("invalid-clone", "git clone validation failed: %s" %
                         _safe_detail(r.stderr or r.stdout or "exit %d" % r.returncode))
    fields = {}
    for line in (r.stdout or "").splitlines():
        if line.startswith("# branch."):
            key, _, value = line[2:].partition(" ")
            fields[key] = value.strip()
    branch, commit = fields.get("branch.head"), fields.get("branch.oid")
    if not branch or not commit or commit == "(initial)":
        raise WatchError("invalid-clone", "git clone validation returned an incomplete identity")
    if branch != BRANCH:
        raise WatchError("wrong-branch", "clone branch is %s; want %s" %
                         (branch or "<detached>", BRANCH))
    return path, commit


def _text(value, field):
    if not isinstance(value, str) or not value:
        raise WatchError("malformed-jsonl", "%s must be a non-empty string" % field)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise WatchError("malformed-jsonl", "%s contains a control character" % field)
    return value


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise WatchError("malformed-jsonl", "%s must be a string array" % field)
    for v in value:
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise WatchError("malformed-jsonl", "%s contains a control character" % field)
    return list(value)


def parse_checker(stdout, exit_code):
    """Validate complete checker JSONL and return canonical sorted records."""
    if exit_code not in (0, 1):
        raise WatchError("checker-exit", "checker exited %s (only 0 and 1 carry reports)" % exit_code)
    if not stdout or not stdout.endswith("\n"):
        raise WatchError("malformed-jsonl", "checker stdout is not complete newline-terminated JSONL")
    rows = []
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line:
            raise WatchError("malformed-jsonl", "checker stdout contains a blank record at line %d" % number)
        try:
            raw = json.loads(line)
        except (TypeError, ValueError) as e:
            raise WatchError("malformed-jsonl", "checker line %d is not JSON: %s" % (number, e))
        if not isinstance(raw, dict):
            raise WatchError("malformed-jsonl", "checker line %d is not an object" % number)
        missing = [f for f in _FIELDS if f not in raw]
        if missing:
            raise WatchError("malformed-jsonl", "checker line %d lacks %s" %
                             (number, ", ".join(missing)))
        if raw["schema"] != 1 or isinstance(raw["schema"], bool):
            raise WatchError("malformed-jsonl", "checker line %d has unsupported schema" % number)
        status = _text(raw["status"], "status")
        if status not in _STATUSES:
            raise WatchError("malformed-jsonl", "checker line %d has unknown status %s" %
                             (number, status))
        conflicts = _strings(raw["conflict_files"], "conflict_files")
        packages = _strings(raw["failing_packages"], "failing_packages")
        targets = _strings(raw["test_targets"], "test_targets")
        contradictory = (
            status == "CLEAN" and (conflicts or packages)
            or status == "CONFLICT" and (not conflicts or packages)
            or status == "TEST-FAIL" and (conflicts or not packages)
        )
        if contradictory:
            raise WatchError(
                "malformed-jsonl",
                "checker line %d status contradicts its conflict/package evidence" % number)
        rows.append({
            "schema": 1,
            "branch": _text(raw["branch"], "branch"),
            "branch_tip": _text(raw["branch_tip"], "branch_tip"),
            "upstream_tip": _text(raw["upstream_tip"], "upstream_tip"),
            "status": status,
            "conflict_files": conflicts,
            "failing_packages": packages,
            "test_targets": targets,
        })
    if not rows:
        raise WatchError("malformed-jsonl", "checker produced no branch records")
    branches = [r["branch"] for r in rows]
    if len(set(branches)) != len(branches):
        raise WatchError("duplicate-branch", "checker produced duplicate branch records")
    upstreams = {r["upstream_tip"] for r in rows}
    if len(upstreams) != 1:
        raise WatchError("mixed-upstream", "checker records disagree on upstream tip")
    return sorted(rows, key=lambda r: r["branch"])


def _error(category, detail, checker_exit=None, checker_commit=None):
    return {
        "overall": "ERROR",
        "checker_exit": checker_exit,
        "checker_commit": checker_commit,
        "upstream": None,
        "results": [],
        "error": {"category": category, "detail": _safe_detail(detail)},
    }


def check():
    """Read-only checker pass. Every boundary failure becomes a stable report."""
    commit = None
    try:
        path, commit = _inspect_clone()
        env = os.environ.copy()
        env["GOWORK"] = "off"
        try:
            r = subprocess.run(
                ["go", "run", "-mod=readonly", "./cmd/helm-upstream-check"],
                cwd=path, env=env, capture_output=True, text=True,
                timeout=CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _error("checker-timeout", "checker exceeded %ds" % CHECK_TIMEOUT_S,
                          checker_commit=commit)
        except UnicodeError as e:
            return _error("malformed-jsonl", "checker stdout is not valid text: %s" % e,
                          checker_commit=commit)
        except OSError as e:
            return _error("checker-launch", "checker could not start: %s" % e,
                          checker_commit=commit)
        try:
            rows = parse_checker(r.stdout, r.returncode)
        except WatchError as e:
            detail = e.detail
            stderr = _safe_detail(r.stderr)
            if stderr:
                detail += "; stderr: " + stderr
            return _error(e.category, detail, checker_exit=r.returncode,
                          checker_commit=commit)
        return {
            "overall": "CLEAN" if all(row["status"] == "CLEAN" for row in rows) else "DEBT",
            "checker_exit": r.returncode,
            "checker_commit": commit,
            "upstream": rows[0]["upstream_tip"],
            "results": rows,
            "error": None,
        }
    except WatchError as e:
        return _error(e.category, e.detail, checker_commit=commit)


def fingerprint(report):
    """Semantic state only: no times, SHAs, checker commit, or test targets."""
    if report["overall"] == "ERROR":
        value = {"error": report["error"]["category"]}
    else:
        value = {"branches": [{
            "branch": row["branch"],
            "status": row["status"],
            "conflict_files": sorted(set(row["conflict_files"])),
            "failing_packages": sorted(set(row["failing_packages"])),
        } for row in sorted(report["results"], key=lambda r: r["branch"])]}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


def _line_text(value):
    """One physical, non-mentioning chat token even for surprising checker text."""
    return " ".join(str(value).replace("@", "[at]").split())


def report_line(report, reason=None, path=None):
    prefix = "proxy-fork-watch"
    if reason:
        prefix += " " + reason
    if report["overall"] == "ERROR":
        detail = "ERROR[%s]" % report["error"]["category"]
    else:
        parts = []
        for row in sorted(report["results"], key=lambda r: r["branch"]):
            extras = []
            if row["conflict_files"]:
                extras.append("files=" + ",".join(sorted(set(row["conflict_files"]))))
            if row["failing_packages"]:
                extras.append("pkgs=" + ",".join(sorted(set(row["failing_packages"]))))
            parts.append("%s=%s%s" % (row["branch"], row["status"],
                                      "(" + ";".join(extras) + ")" if extras else ""))
        detail = "%s: %s" % (report["overall"], "; ".join(parts))
    line = "%s %s" % (prefix, detail)
    if path:
        line += " | state " + path
    return _line_text(line)


def _last_run(report, started_at, finished_at, decision, outcome, post_error=None):
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "overall": report["overall"],
        "checker": {
            "exit": report["checker_exit"],
            "commit": report["checker_commit"],
            "upstream": report["upstream"],
        },
        "results": report["results"],
        "error_category": report["error"]["category"] if report["error"] else None,
        "post": {
            "decision": decision,
            "outcome": outcome,
            "error": _safe_detail(post_error) if post_error else None,
        },
    }


def post_once(force=False):
    """Exclusive checker/read/decide/post/write transaction for one cadence."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        started = pk.now_ts()
        report = check()
        fp = fingerprint(report)
        state = pk.read_json(path, {}) or {}
        if not isinstance(state, dict):
            state = {}
        announced = state.get("announced_fingerprint")
        pending = state.get("pending_fingerprint")
        pending_decision = state.get("pending_decision")
        if force:
            decision = "FORCED"
        elif pending == fp and pending_decision in ("FIRST", "CHANGED", "FORCED"):
            decision = pending_decision
        elif announced is None:
            decision = "FIRST"
        elif announced != fp:
            decision = "CHANGED"
        else:
            decision = "UNCHANGED"
        outcome, post_error = "SKIPPED", None
        pending = pending_decision = None
        if decision != "UNCHANGED":
            try:
                from . import chat
                chat.post(report_line(report, decision, path), room="helm",
                          who="proxy-fork-watch")
                outcome = "POSTED"
                announced = fp
            except Exception as e:  # persist due notification; never consume it
                outcome, post_error = "FAILED", e
                pending, pending_decision = fp, decision
        finished = pk.now_ts()
        state["schema"] = 1
        state["last_run"] = _last_run(report, started, finished, decision,
                                      outcome, post_error)
        if announced is not None:
            state["announced_fingerprint"] = announced
        else:
            state.pop("announced_fingerprint", None)
        if pending is not None:
            state["pending_fingerprint"] = pending
            state["pending_decision"] = pending_decision
        else:
            state.pop("pending_fingerprint", None)
            state.pop("pending_decision", None)
        pk.write_json(path, state)
    return report, state


def timer_units():
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, _SERVICE_NAME), _SERVICE,
            os.path.join(udir, _TIMER_NAME), _TIMER)


def install_timer():
    """Validate the durable source checkout, atomically install, then enable."""
    try:
        _inspect_clone()
    except WatchError as e:
        return False, "%s: %s" % (e.category, e.detail)
    if not shutil.which("go"):
        return False, "go unavailable; checker cannot run"
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    if not os.path.isfile(helm_bin) or not os.access(helm_bin, os.X_OK):
        return False, "installed helm is missing or not executable: %s" % helm_bin
    if not shutil.which("systemctl"):
        return False, "systemctl unavailable"
    spath, service, tpath, timer = timer_units()
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    reload_cmd = ["systemctl", "--user", "daemon-reload"]
    enable_cmd = ["systemctl", "--user", "enable", "--now",
                  "helm-proxy-fork-watch.timer"]
    try:
        reload_result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True,
            text=True, timeout=SYSTEMCTL_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "%s failed: %s" % (" ".join(reload_cmd), e)
    if reload_result.returncode != 0:
        return False, "%s failed: %s" % (
            " ".join(reload_cmd), _safe_detail(
                reload_result.stderr or reload_result.stdout or
                "exit %d" % reload_result.returncode))
    try:
        enable_result = subprocess.run(
            ["systemctl", "--user", "enable", "--now",
             "helm-proxy-fork-watch.timer"], capture_output=True, text=True,
            timeout=SYSTEMCTL_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "%s failed: %s" % (" ".join(enable_cmd), e)
    if enable_result.returncode != 0:
        return False, "%s failed: %s" % (
            " ".join(enable_cmd), _safe_detail(
                enable_result.stderr or enable_result.stdout or
                "exit %d" % enable_result.returncode))
    return True, "enabled daily timer %s" % tpath


def _rc(report):
    return {"CLEAN": 0, "DEBT": 1, "ERROR": 2}[report["overall"]]


def cmd_proxy_fork_watch(args):
    args = list(args or [])
    from .cli import guard_tail
    rc = guard_tail("helm proxy-fork-watch", args,
                    flags=("--json", "--post", "--force", "--install-timer"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    if "--install-timer" in args:
        ok, detail = install_timer()
        print("helm proxy-fork-watch: %s" % detail,
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    try:
        report, state = (post_once(force="--force" in args)
                         if "--post" in args or "--force" in args
                         else (check(), None))
    except OSError as e:
        print("helm proxy-fork-watch: state write failed: %s" % e, file=sys.stderr)
        return 2
    if "--json" in args:
        value = {"report": report}
        if state is not None:
            value["last_run"] = state["last_run"]
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(report_line(report))
    if state is not None and state["last_run"]["post"]["outcome"] == "FAILED":
        print("helm proxy-fork-watch: chat post failed: %s" %
              state["last_run"]["post"]["error"], file=sys.stderr)
        return 2
    return _rc(report)


if __name__ == "__main__":
    raise SystemExit(cmd_proxy_fork_watch(sys.argv[1:]))
