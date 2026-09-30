"""Is the installed binary the code its source checkout shows? (task/2963)

THE DEFECT. The owner read cli-proxy-api's source at its checkout's HEAD and
reasoned from it about the running proxy, while the installed binary had
been built from an older commit. Nothing said the two differ, so the code on
screen was taken for the code that runs.

THE READING, one per built tool: the commit the binary reports about itself,
and HEAD of the tool's primary source checkout. The same commit is SAME. A
different one is SKEW: the line names the installed commit and where the code
that runs can be read, a worktree whose HEAD is that commit when one exists,
else `git show <sha>:<path>` in the checkout. A commit carrying a suffix
("+cooldownverify", "-dirty") was built from a MODIFIED tree, which no
checkout shows, so it never reads SAME. A binary that is missing, cannot run,
times out or names no commit, or a checkout that has no record or is not a
repository, is UNKNOWN, never the same.

THE VERSION BANNER IS READ WHATEVER THE EXIT, AND ONLY OFF ITS OWN LINE.
The real cli-proxy-api prints "CLIProxyAPI Version: …, Commit: <sha>,
BuiltAt: …" on stdout BEFORE it parses flags, then refuses `--version` (it
has no such flag) and exits 2. The commit is taken off that banner line and
nowhere else: a "Commit: <sha>" in usage or error text is not the binary
speaking about itself. No banner line is UNKNOWN. Two candidates that
disagree (two banners, or a banner and a stray "Commit:") are UNKNOWN naming
both. A run that could not start or timed out is UNKNOWN.

A SHORT COMMIT CERTIFIES NOTHING. SAME needs the reported commit to be at
least MIN_CERTIFYING_HEX (12) hex AND HEAD to start with it. A 7-11 hex
abbreviation may be an unrelated commit that shares the prefix, so it reads
UNKNOWN: never SAME, and never SKEW either, since a commit that is not
identified cannot be said to differ.

THE PROBE IS BOUNDED BY ITS TIMEOUT, NOT BY TRUST. That the banner prints
and the binary exits before it serves is MEASURED for the installed version
(7.2.110-helm.16) only; nothing guarantees it of a future binary. So the
probe runs in its own session, and a timeout SIGKILLs its whole process
group (never helm's own), so every child still in that group dies with it.
A child that calls setsid leaves the group and is NOT reached: the guarantee
is group-limited, not absolute. That is what bounds the harm, and the
timeout reads UNKNOWN.

ONE DEADLINE OVER THE WHOLE READING (BUDGET_S). `helm seat status`, `helm
seat list` and the doctor pay for it on every run; per-call timeouts alone
allowed the 5 s probe plus three sequential 10 s git calls, 35 s. Each later
call gets only what is left, and a reading that runs out before its state is
decided is UNKNOWN ("ran out of its N s budget"). Once both commits are read
and differ, the SKEW stands; a budget spent on the pointer lookup that
follows only says the pointer did not read.

THE CHECKOUT IS THE PRIMARY FORK CHECKOUT, found through the project
registry's record for it (`helm show CLIProxyAPI`), never the upstream
checker's clone (`HELM_PROXY_FORK_DIR`, a replay branch nothing is built
from). No record is UNKNOWN naming the project.

READ-ONLY, AND HOST CONFIG STAYS HOST CONFIG. The binary resolves as helm
already finds it (HELM_PROXY_BIN). The registry is read with the STRICT
load, which never migrates a mixed-era registry, never saves and takes no
lock; the ordinary load can persist a migration under the write lock, which
would make this reading a writer. Nothing here fetches, checks out or
rebuilds. helm does NOT move the primary checkout to the installed commit:
it is a shared working checkout that lanes branch from, and checking it out
would rewrite someone's working state, so the rung and the worktree pointer
are the answer.
"""
import os
import re
import signal
import subprocess
import time

SAME, SKEW, MODIFIED, UNKNOWN = "SAME", "SKEW", "MODIFIED", "UNKNOWN"
# ONE deadline, in seconds, over the whole reading of every built tool
BUDGET_S = 10
# the `--version` probe's own cap, taken out of that budget
VERSION_TIMEOUT_S = 5
# THE SHORTEST REPORTED COMMIT THAT CERTIFIES WHICH COMMIT IT IS: 12 hex, 48
# bits (the installed binary reports exactly 12). The bar is ACCIDENT, NOT
# ADVERSARY: an unrelated HEAD that shares an incidental 48-bit prefix is
# about 1 in 2^48 per pair, where a 7-hex (28-bit) prefix is shared inside
# one large history. It is not a cryptographic proof of identity; a binary
# that lies about its own commit is out of scope.
MIN_CERTIFYING_HEX = 12
# the shortest hex run read as a commit at all (git's shortest
# abbreviation); fewer ("Commit: none", "Commit: dev") names no commit
MIN_COMMIT_HEX = 7
# the registry project that records cli-proxy-api's primary source checkout
PROXY_PROJECT = "CLIProxyAPI"

# THE BANNER LINE, anchored at the start of a line: "CLIProxyAPI Version:
# <version>, Commit: <token>". The token is the commit's hex and then
# anything glued on after it ("+cooldownverify", "-dirty"), which marks a
# modified build. The version may carry '+' parts of its own; it ends at
# the comma, so they never reach the token.
_BANNER = re.compile(r"^CLIProxyAPI Version:[^,\n]*,[ \t]*Commit:[ \t]*([^\s,]*)",
                     re.M)
# any "Commit: <token>" anywhere in the output, banner or not: a second
# candidate that disagrees with the banner makes the reading UNKNOWN
_ANY_COMMIT = re.compile(r"\bCommit:[ \t]*([^\s,]*)")
_HEX = re.compile(r"[0-9a-fA-F]*")
# NO TEXT THE BINARY CHOSE IS PRINTED BUT ITS HEX. A build marker glued on
# after the hex ("+cooldownverify", "-dirty") is named by its length alone,
# and so is a token whose hex is not commit-shaped, so a stray or
# secret-shaped value in the binary's output never reaches the doctor or
# `seat status` line. The marker itself is one `--version` away.


class _Spent(Exception):
    """The reading's one deadline has passed."""


def _left(deadline):
    """Seconds left before `deadline`; raises _Spent once none are."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise _Spent()
    return left


def _spent_why():
    return "the reading ran out of its %g s budget" % BUDGET_S


def _proxy_binary():
    # THROUGH THE SEAT FACADE, as every seat_paths reader goes
    from . import seat
    return seat._proxy_bin()


def _project_checkout(project):
    """(path, None) of the registry's record for `project`, else (None, why).
    The STRICT load: it never migrates, saves or locks (see the module)."""
    from . import registry
    try:
        rec = registry.load(strict=True)["projects"].get(project)
    except Exception as exc:                # noqa: BLE001 — unread, UNKNOWN
        return None, "the project registry did not read (%s)" % (
            exc.__class__.__name__)
    path = str((rec or {}).get("path") or "")
    if not path:
        return None, "no registry record for project %s (`helm show %s`)" % (
            project, project)
    return path, None


# (tool, binary resolver, the variable that places it, the registry project
# that records its source checkout). One row per built tool.
TOOLS = (
    ("cli-proxy-api", _proxy_binary, "HELM_PROXY_BIN", PROXY_PROJECT),
)


def _marker(suffix):
    """A build marker as the reading may print it: its length alone."""
    return "a %d-character build marker" % len(suffix)


def _shown(token):
    """A Commit token as the reading may print it: its hex when that is 7 to
    40 characters, then its build marker as _marker prints it; any other
    token by its length alone."""
    if not token:
        return "empty"
    hexes = _HEX.match(token).group(0)
    if not 7 <= len(hexes) <= 40:
        return "a %d-character value that is not a commit" % len(token)
    rest = token[len(hexes):]
    return hexes + (" plus %s" % _marker(rest) if rest else "")


def _banner(text):
    """(commit, suffix, None) off the one banner line, else (None, "", why)."""
    banners = _BANNER.findall(text)
    if not banners:
        return None, "", "names no version banner (a `CLIProxyAPI Version: " \
            "…, Commit: <sha>` line)"
    seen = sorted({t.lower() for t in banners + _ANY_COMMIT.findall(text)})
    if len(seen) > 1:
        return None, "", "names conflicting commits (%s)" % ", ".join(
            _shown(t) for t in seen)
    token = banners[0]
    hexes = _HEX.match(token).group(0)
    if len(hexes) < MIN_COMMIT_HEX:
        return None, "", "names no commit (Commit: %s)" % _shown(token)
    return hexes.lower(), token[len(hexes):], None


def _kill_group(proc):
    """SIGKILL the probe's whole process group: it leads its own session, so
    a child still in that group dies with it (one that called setsid has
    left and is not reached). Never helm's own group. The wait after
    it is BOUNDED and the pipes are closed rather than drained: a process
    stuck in the kernel, or one that left the group and still holds the
    pipes, must not hang the screen that asked."""
    try:
        group = os.getpgid(proc.pid)
        if group != os.getpgrp():
            os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
        proc.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except (OSError, AttributeError):
            pass


def _installed(binary, env_name, deadline):
    """(commit, suffix, None) off the binary's own banner, else
    (None, "", why). The exit code is not the answer: the real binary
    prints its banner and then exits 2 on the flag."""
    if not binary:
        return None, "", "no installed binary found (%s, or on PATH)" % env_name
    timeout = min(VERSION_TIMEOUT_S, _left(deadline))
    try:
        proc = subprocess.Popen(
            [binary, "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
            errors="replace", start_new_session=True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, "", "%s --version did not run (%s)" % (
            binary, exc.__class__.__name__)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return None, "", "%s --version did not run to its end in %.1f s; " \
            "its process group was killed" % (binary, timeout)
    commit, suffix, why = _banner((out or "") + "\n" + (err or ""))
    if why:
        return None, "", "%s --version %s (exit %d)" % (
            binary, why, proc.returncode)
    return commit, suffix, None


def _git(path, deadline, *args):
    """git through the vcs seam, given only what is left of the deadline."""
    from . import vcs
    try:
        return vcs.backend(path).proc(path, *args, timeout=_left(deadline))
    except subprocess.TimeoutExpired as exc:
        raise _Spent() from exc


def _head(path, project, deadline):
    """(HEAD, None) of the checkout, else (None, why)."""
    if not os.path.isdir(path):
        return None, "source checkout %s (registry project %s) is missing" % (
            path, project)
    try:
        r = _git(path, deadline, "rev-parse", "--show-toplevel", "HEAD")
    except _Spent:
        raise
    except Exception as exc:                # noqa: BLE001 — unread, UNKNOWN
        return None, "source checkout %s (registry project %s) did not read " \
            "(%s)" % (path, project, exc.__class__.__name__)
    lines = (r.stdout or "").split()
    if r.returncode != 0 or len(lines) != 2 \
            or os.path.realpath(lines[0]) != os.path.realpath(path):
        return None, "source checkout %s (registry project %s) is not a " \
            "repository" % (path, project)
    return lines[1].lower(), None


def _has(path, sha, deadline):
    """Does the checkout hold commit `sha`? None when that did not read."""
    try:
        r = _git(path, deadline, "cat-file", "-e", sha + "^{commit}")
    except _Spent:
        raise
    except Exception:                       # noqa: BLE001 — unread
        return None
    return r.returncode == 0


def _worktree_at(path, sha, deadline):
    """A worktree of the checkout whose HEAD is `sha`, else None."""
    try:
        r = _git(path, deadline, "worktree", "list", "--porcelain")
    except _Spent:
        raise
    except Exception:                       # noqa: BLE001 — no pointer
        return None
    if r.returncode != 0:
        return None
    here, own = None, os.path.realpath(path)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("worktree "):
            here = ln[len("worktree "):]
        elif ln.startswith("HEAD ") and here \
                and ln[len("HEAD "):].strip().lower().startswith(sha) \
                and os.path.realpath(here) != own:
            return here
    return None


def _compare(out, binary, bin_env, project, deadline):
    """Fill `out` up to its state. The SKEW pointer is the caller's."""
    out["installed"], out["suffix"], out["why"] = _installed(
        binary, bin_env, deadline)
    if out["why"]:
        return
    if out["suffix"]:
        out["state"] = MODIFIED
        return
    if len(out["installed"]) < MIN_CERTIFYING_HEX:
        out["why"] = "%s --version reports commit %s: an abbreviation of %d " \
            "hex is too short to certify; %d or more is needed" % (
                binary, out["installed"], len(out["installed"]),
                MIN_CERTIFYING_HEX)
        return
    _left(deadline)
    out["checkout"], out["why"] = _project_checkout(project)
    if out["why"]:
        return
    out["head"], out["why"] = _head(out["checkout"], project, deadline)
    if out["why"]:
        return
    out["state"] = SAME if out["head"].startswith(out["installed"]) else SKEW


def reading(tool, binary, bin_env, project, deadline=None):
    """{tool, state, installed, suffix, head, checkout, has, worktree, why},
    under `deadline` (time.monotonic(); BUDGET_S from now when None)."""
    if deadline is None:
        deadline = time.monotonic() + BUDGET_S
    out = {"tool": tool, "state": UNKNOWN, "installed": None, "suffix": "",
           "head": None, "checkout": None, "has": None, "worktree": None,
           "why": None}
    try:
        _compare(out, binary, bin_env, project, deadline)
    except _Spent:
        out.update(state=UNKNOWN, why=_spent_why())
        return out
    if out["state"] != SKEW:
        return out
    # THE STATE IS DECIDED: both commits read, and they differ. What follows
    # only points at where the running code can be read, so a budget spent
    # here keeps the SKEW and says the pointer did not read.
    try:
        out["has"] = _has(out["checkout"], out["installed"], deadline)
        if out["has"]:
            out["worktree"] = _worktree_at(out["checkout"], out["installed"],
                                           deadline)
    except _Spent:
        out["why"] = _spent_why()
    return out


def readings():
    """One reading per built tool. Never raises: a probe that breaks is one
    UNKNOWN reading, never a traceback and never a SAME. ONE deadline
    covers them all."""
    deadline = time.monotonic() + BUDGET_S
    out = []
    for tool, find_bin, bin_env, project in TOOLS:
        try:
            out.append(reading(tool, find_bin(), bin_env, project, deadline))
        except Exception as exc:            # noqa: BLE001 — one tool, UNKNOWN
            out.append({"tool": tool, "state": UNKNOWN, "installed": None,
                        "suffix": "", "head": None, "checkout": None,
                        "has": None, "worktree": None,
                        "why": "the reading broke (%s)" % exc.__class__.__name__})
    return out


def line(r):
    """One sentence for a reading, as the doctor and `seat status` print it."""
    tool = r["tool"]
    if r["state"] == SAME:
        return ("%s: the installed binary is commit %s, its source checkout's "
                "HEAD" % (tool, r["installed"][:12]))
    if r["state"] == MODIFIED:
        return ("%s: MODIFIED — built from commit %s plus local changes (%s): "
                "no checkout shows that code" % (tool, r["installed"][:12],
                                                 _marker(r["suffix"])))
    if r["state"] == SKEW:
        sha = r["installed"][:12]
        text = ("%s: SKEW — the installed binary is commit %s, but its source "
                "checkout %s is at %s, so that checkout's files are NOT the "
                "running code" % (tool, sha, r["checkout"], r["head"][:12]))
        if r.get("worktree"):
            return text + "; the running code is checked out at %s" % r["worktree"]
        text += "; read it with `git -C %s show %s:<path>`" % (r["checkout"], sha)
        if r.get("has") is False:
            text += " (commit %s is not in this checkout: fetch it first)" % sha
        elif r.get("has") is None:
            text += " (whether the checkout holds %s did not read)" % sha
        if r.get("why"):
            text += " (%s)" % r["why"]
        return text
    return "%s: build skew UNKNOWN — %s" % (tool, r.get("why") or "not read")


def status_lines():
    """The `helm seat status` lines: one per built tool. Never raises.
    INDENTED, because a column-0 line on that screen is a seat row."""
    try:
        return ["  build: " + line(r) for r in readings()]
    except Exception as exc:                # noqa: BLE001 — a status line
        return ["  build: skew UNKNOWN (%s)" % exc.__class__.__name__]
