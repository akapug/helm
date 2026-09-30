"""helm work checkout-watch — report a stray write on the shared checkout.

Parsing a shell for writes never closes. This watch reads the effect: `git
status --porcelain` on the shared checkout, one alert per distinct dirty
state. It does not refuse the write. The argv-guard still refuses Edit,
Write, NotebookEdit, and `git init` of the checkout or its lane parent.

Every alert names the integrator, the seat that restores the checkout, as
seats_integrator resolves it at post time. When none resolves, the alert
still posts and its first line says so.

A git operation in flight is not a stray write. HELM_WORK_INTEGRATOR is a
hook environment, invisible to a timer, so the only in-flight signal this
process can see is git's own: while the checkout's git dir holds a fresh
IN_FLIGHT marker or a fresh index.lock, the tick is skipped, nothing posts
and the latch does not move. The same grace covers every marker. Past it,
the marker is an alert: a conflicted cherry-pick or a Git crash can leave
one forever, and a watch that skipped on it would never report the stray
writes made meanwhile. A land between two git commands holds no marker and
can still alert; that false alert is cheap, and a missed stray write is not.

A checkout git cannot read is an alert too, once per distinct failure, and
the tick keeps its nonzero rc for the timer's journal.
"""
import hashlib
import json
import os
import time
import uuid

INTERVAL_S = 120
TIMER_ENV = "HELM_CHECKOUT_WATCH_TIMER"
TIMER_OFF_VALUES = ("0", "off", "no", "false")
SERVICE_NAME = "helm-checkout-watch.service"
TIMER_NAME = "helm-checkout-watch.timer"
FIX = ("the writer: copy your diff into a lane with `helm work claim`; "
       "the integrator restores the checkout")

#: What git leaves in a checkout's own git dir while one of its operations
#: runs. work/_lanes._OP_MARKERS and work/_gc._OP_MARKERS name the same
#: sequencer state for other questions and add BISECT_LOG: a bisect can sit
#: for hours on a clean tree, so a stray write during one is still a stray
#: write, and it is not here. A fresh marker or index.lock skips the tick.
#: After two cadences either is an unreadable-checkout alert: a conflicted
#: sequencer left in the shared checkout is not an operation still in flight.
#: The fixed threshold keeps the alert text stable for the latch.
IN_FLIGHT = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
             "rebase-merge", "rebase-apply")
INDEX_LOCK = "index.lock"
INDEX_LOCK_GRACE_S = INTERVAL_S * 2

#: A settled latch holds the porcelain for dirt or this prefix plus an
#: unreadable-checkout failure. A pending envelope also carries the immutable
#: alert prose and chat operation key until that settled state is durable.
#: No porcelain line starts with any prefix, so the states cannot collide.
UNREAD = "checkout-watch unread: "
CLEAN = "checkout-watch clean\n"
PENDING = "checkout-watch pending: "
POSTER = "checkout-watch"

_SERVICE = """[Unit]
Description=helm checkout watch — a stray write on the shared checkout

[Service]
WorkingDirectory=%(cwd)s
Type=oneshot
# Reports the effect. It does not refuse the write: shell grammar does not
# close over redirects, sed -i, tee, cp, mv and the rest. A fresh git
# operation (its marker in the checkout's git dir) skips the tick; a stale
# marker alerts. A land between two git commands holds no marker and may
# alert.
ExecStart=%(helm)s work checkout-watch --apply --repo %(cwd)s
Nice=15
"""

_TIMER = """[Unit]
Description=helm checkout watch every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=%(interval)ds
Persistent=true

[Install]
WantedBy=timers.target
"""


def timer_switched_off():
    """The HELM_CHECKOUT_WATCH_TIMER value when it turns the install off."""
    value = os.environ.get(TIMER_ENV)
    if value is not None and value.strip().lower() in TIMER_OFF_VALUES:
        return value
    return None


def timer_units(interval=INTERVAL_S, inputs=None):
    """(service_path, service_text, timer_path, timer_text). WorkingDirectory
    is derived from this checkout's shared root, never a literal; the unit
    directory is timerhealth.user_unit_dir(), every installer's (task/3307).
    `inputs` replaces per-install values (timerhealth.unit_values), which is
    how the drift census renders this template."""
    from . import timerhealth, work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = timerhealth.user_unit_dir()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % timerhealth.unit_values({"helm": helm_bin, "cwd": cwd},
                                               inputs),
            os.path.join(udir, TIMER_NAME),
            _TIMER % timerhealth.unit_values({"interval": interval}, inputs))


def ensure_timer(interval=INTERVAL_S):
    """(ok, detail) — install and enable the 2-minute cadence.

    IDEMPOTENT: unchanged unit files are not rewritten and systemd is not
    reloaded for them. `ok` is True (enabled), False (failed), or None
    (switched off by TIMER_ENV: nothing written, nothing run). The writes,
    the reload and the enable are timerhealth.install_user_timer's, the gate
    canary's steps (task/3307)."""
    import shutil
    from . import timerhealth
    off = timer_switched_off()
    if off is not None:
        return None, ("install skipped by %s=%s: no unit file written, no "
                      "systemctl run" % (TIMER_ENV, off))
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm work checkout-watch "
                       "--apply` from another scheduler")
    spath, service, tpath, timer = timer_units(interval)
    error, unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (TIMER_NAME,), systemctl,
        keep_unchanged=True, reload_unchanged=False)
    if error:
        return False, error
    return True, "%s every %ds (%s)" % (
        "already installed, unchanged," if unchanged else "installed",
        interval, tpath)


def latch_path(root):
    """Where the last posted porcelain lives. Outside the checkout: a latch
    inside it would be the next dirty state."""
    from . import home
    real = os.path.realpath(root)
    name = os.path.basename(real.rstrip(os.sep)) or "checkout"
    digest = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return os.path.join(home.project_dir("checkout-watch"), "%s-%s" % (name, digest))


def _read_latch(root):
    try:
        with open(latch_path(root), "rb") as fh:
            return fh.read().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return None


def _write_latch(root, text):
    from . import pk
    pk.atomic_write(latch_path(root), text.encode("utf-8", "surrogateescape"))


def _pending(event_id, latch, body, room):
    return PENDING + json.dumps({"body": body, "event": event_id,
                                 "latch": latch, "room": room},
                                separators=(",", ":"), sort_keys=True)


def _read_pending(text):
    if not text or not text.startswith(PENDING):
        return None
    try:
        row = json.loads(text[len(PENDING):])
    except (TypeError, ValueError):
        return None
    if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) and row[k]
            for k in ("body", "event", "latch", "room")):
        return None
    return row["event"], row["latch"], row["body"], row["room"]


def _event_id(root):
    digest = hashlib.sha256(root.encode(
        "utf-8", "surrogateescape")).hexdigest()[:16]
    return "checkout-watch-%s-%s" % (digest, uuid.uuid4().hex)


def _room(root):
    try:
        from . import seats
        room, _source = seats.resolve_homing(cwd=root)
        return room or "main"
    except Exception:
        return "main"


def _clear_latch(root):
    """None after the old state cannot suppress a recurrence, else why not.

    Write a clean tombstone before removing the file. If removal then fails,
    the tombstone is harmless: no dirty porcelain or unreadable prefix equals
    it. If the tombstone cannot be written, removal is still a safe fallback;
    only failure of both leaves an old state authoritative, and that is loud."""
    path = latch_path(root)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return "latch inspect failed (%s)" % exc
    try:
        _write_latch(root, CLEAN)
    except OSError as write_exc:
        try:
            os.remove(path)
        except FileNotFoundError:
            return None
        except OSError as remove_exc:
            return "latch clear failed (%s; %s)" % (write_exc, remove_exc)
        return None
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass                         # CLEAN cannot equal a later alert state
    return None


def in_flight(root):
    """(marker, None) while a fresh IN_FLIGHT marker or a fresh index.lock
    is in the checkout's git dir, ("", None) for none, or (None, failure)
    when the git dir cannot be read or a marker is older than
    INDEX_LOCK_GRACE_S. Could-not-look is not no-operation, so a failure is
    an alert. A marker with no age bound would skip every later tick.

    The git dir is the one git names (`rev-parse --absolute-git-dir`, through
    the vcs seam), never a `.git` join: a linked worktree's `.git` is a file,
    and its markers live in its own git dir."""
    from .work._lanes import _git
    rc, gitdir, err = _git(root, "rev-parse", "--absolute-git-dir",
                           timeout=15)
    if rc != 0 or not gitdir:
        return None, ("git rev-parse --absolute-git-dir exited %d: %s"
                      % (rc, err or "no git dir"))
    for marker in IN_FLIGHT + (INDEX_LOCK,):
        path = os.path.join(gitdir, marker)
        if not os.path.lexists(path):
            continue
        try:
            age = time.time() - os.stat(path).st_mtime
        except OSError as exc:
            return None, "%s cannot be read: %s" % (marker, exc)
        if age <= INDEX_LOCK_GRACE_S:
            return marker, None
        return None, "%s is stale (older than %ds)" % (
            marker, INDEX_LOCK_GRACE_S)
    return "", None


def porcelain(root):
    """(rc, text, err) from `git --no-optional-locks status --porcelain`.
    Text keeps a leading status space; the shared text helper strips it and
    would hide a worktree-only change. The flag is the point: a plain status
    refreshes the index and takes index.lock on the canonical checkout, and
    a checkout write in that window fails on the lock."""
    from .work._lanes import _git_bytes
    rc, out, err = _git_bytes(
        root, "--no-optional-locks", "status", "--porcelain",
        "--untracked-files=normal", timeout=15)
    return rc, os.fsdecode(out), os.fsdecode(err).strip()


def _paths(text):
    paths = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            path = path[1:-1]
        paths.append(path)
    return paths


def _mtime(root, path):
    try:
        return str(int(os.stat(os.path.join(root, path)).st_mtime))
    except OSError:
        return "unreadable"


def addressed(head):
    """HEAD with the integrator's mention in front, or HEAD saying none
    resolved and why. The mention is seats_integrator's answer at post time,
    never a literal."""
    from .seats_integrator import integrator_mention
    mention, why = integrator_mention()
    if mention:
        return mention + head
    return "%s (no integrator resolved: %s)" % (head, why or "no reason given")


def alert(root, text):
    """The one post: the files, their mtimes, and the repair."""
    lines = [addressed("shared checkout %s is dirty" % root) + ":"]
    for path in _paths(text):
        lines.append("  %s  mtime %s" % (path, _mtime(root, path)))
    lines.append(FIX)
    return "\n".join(lines)


def watch(root, apply=False):
    """(rc, lines). A git operation in flight skips the tick and leaves the
    latch. A clean tree clears the latch. The same porcelain, or the same
    failure, posts nothing more; clean, then dirty or unreadable again, posts
    again. An unreadable checkout is rc 1 on every tick. Dry-run prints the
    alert and does not post, and it does not move the latch."""
    root = os.path.realpath(root)
    marker, failure = in_flight(root)
    if marker:
        return 0, ["checkout-watch: skipped, the git dir holds %s: a git "
                   "operation is in flight" % marker]
    if failure is None:
        rc, text, err = porcelain(root)
        if rc != 0:
            failure = "git status exited %d: %s" % (rc, err or "no stderr")
    if failure is not None:
        body = "%s: %s" % (addressed("shared checkout %s is unreadable"
                                     % root), failure)
        return _report(root, body, UNREAD + failure, apply, 1,
                       "checkout-watch: unread, unchanged (%s)" % failure)
    if text.strip() == "":
        clear_failure = _clear_latch(root)
        if clear_failure:
            return 1, ["checkout-watch: %s" % clear_failure]
        return 0, ["checkout-watch: clean"]
    return _report(root, alert(root, text), text, apply, 0,
                   "checkout-watch: unchanged")


def _report(root, body, latch, apply, rc, unchanged):
    """(rc, lines) for one alert: posted once per distinct latch value.

    A pending record is durable before the post. A retry therefore carries the
    same chat event_id if posting succeeded but finalizing the latch failed;
    chat's operation-key seam returns the original row instead of appending a
    duplicate. Clean removes the record, so a later recurrence gets a new id."""
    if not apply:
        return rc, [body]
    try:
        prior = _read_latch(root)
    except OSError as exc:
        return 1, ["checkout-watch: not posted; latch unreadable (%s)" % exc]
    pending = _read_pending(prior)
    if prior and prior.startswith(PENDING) and not pending:
        return 1, ["checkout-watch: not posted; pending latch is malformed"]
    if pending:
        event_id, pending_latch, pending_body, room = pending
        if pending_latch != latch:
            pending = None
        else:
            body = pending_body
    elif prior == latch:
        return rc, [unchanged]
    if not pending:
        event_id, room = _event_id(root), _room(root)
        try:
            _write_latch(root, _pending(event_id, latch, body, room))
        except OSError as exc:
            return 1, ["checkout-watch: not posted; latch not armed (%s)" % exc]
    try:
        from . import chat
        chat.post(body, room=room, who=POSTER, event_id=event_id, sign=False)
    except Exception as exc:
        return 1, ["checkout-watch: not posted (%s)" % exc]
    try:
        _write_latch(root, latch)
    except OSError as exc:
        return 1, ["checkout-watch: posted; latch not finalized (%s)" % exc]
    return rc, [body]
