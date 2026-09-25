"""Route one whole-suite or focused gate to a consented inventory box (#225).

THE LAST FAB SEAM. Today `fab gate --repo DIR` ships a suite to a fab box and
its artifact rides home through `helm gate import` (#167). This module is the
helm-native spelling of that loop — pick a box from the #224 inventory chain
(`helm/boxes.py`), ship the EXACT committed tree, run the tree's OWN gate
there, fetch the receipt, import it through the same strict seam — so fab
drops from dependency to optional powerpack. Compose-alongside, not replace:
nothing here touches fab, and both paths mint receipts the one importer
validates identically.

EXECUTION IS A SEPARATE CONSENT, ABOVE PROBING (review a7235e643f64 P0).
`HELM_STORAGE_MATRIX_HOSTS` and `HELM_BOXES_SSH_CONSENT` authorize READING a
box — naming a host there never authorized running shipped code on it. A job
requires an explicit job capability, and there is exactly ONE: the owner
naming the actual ``ssh_host`` channel in ``HELM_BOXES_JOB_CONSENT`` (``all``,
or a comma list). A display/lookup host alias never grants a different channel.
ABSENT IS REFUSED, never defaulted: unknown consent must not mean yes when
yes means executing arbitrary code.

THE INVENTORY'S ``fab_accept`` IS NOT THAT CAPABILITY (review 6580ad1dc5e0).
Round one read the field NAME as an owner switch. Read at its SOURCE it is
fab's per-node ADMISSION PROXY, recomputed on every status render from `up ∧
cargo-ready ∧ build-storage free > floor` — fab's own banner says "YES =
eligible, not a routing guarantee", and `fab accept` is not a verb anyone can
run. So a `yes` is a transient disk reading and cannot authorize arbitrary
code, and a `no` means "busy or short of disk" and must not silently veto a
grant the owner did make (round one did both, and named a command that does
not exist). It survives as an ELIGIBILITY NOTE, surfaced beside the result
and decisive of nothing. A genuinely authoritative remote field can join the
grant side later; a scheduler hint cannot.

AN INCOMPLETE INVENTORY REFUSES BEFORE RESOLUTION, not only when nothing
matched. A provider that failed may hold a SECOND row for this name (the
identity would be ambiguous) or a stronger statement about it, so a surviving
lower provider's row proves neither uniqueness nor authority while the chain
is broken. Any provider warning is UNKNOWN, and UNKNOWN refuses.

IDENTITY IS UNIQUE, EXACT, AND CHANNEL-BOUND. A box name resolves against
``host`` and ``ssh_host`` only (labels are display, not address); a name that
matches more than one row refuses as ambiguous rather than routing to the
first. ``reachable`` must be the boolean True — a provider's ``'false'``
string is not reachability. The remote session reports its own ``uname -n``
over the SAME channel the tree shipped on, and the receipt's host.node must
equal it byte-for-byte: a receipt about some other machine — the review's
live repro imported node=unexpected-box — refuses before import. And every
marker line carries a per-invocation random challenge, so a replayed or
stale transcript parses to nothing and cannot mint (its receipt would import
as a byte-identical duplicate, which a fresh run ALSO refuses — an honest
re-run mints a NEW receipt, because ts is inside the content id).

THE CHALLENGE IS NOT THE FRAMING (review 6580ad1dc5e0). It rides in the
remote `sh -c` argv, so the suite running under this very session can read it
out of `/proc/<ancestor>/cmdline` and print it: randomness stops a STALE
transcript, never the live code under test. Three rungs make payload
structurally unable to become control. (a) Payload never travels as text —
gate stdout, the gate.err tail and the receipt lines are base64 on the far
side, an alphabet with no `:` in it, so no payload byte can spell a marker
whatever it knows. (b) Inside a section NOTHING is control but that section's
own end marker, so a marker-shaped payload line is data by position too.
(c) A control field arrives at most ONCE — the first writer wins and a repeat
REFUSES the whole transcript, which is the arm that catches an adversary
writing straight to the session's stdout while the suite runs (redefining
`node=` there would otherwise re-point host binding at a forged receipt).

WHAT SHIPS IS A COMMIT, NEVER A DIRECTORY. The receipt must bind the tree
byte-for-byte (#228), and `gate import` enforces that by resolving the cited
head IN THE IMPORTING REPO and comparing trees — so the remote checkout must
hold the SAME COMMIT OBJECT, not a look-alike. Only a git bundle reproduces
that. The bundle is created from a temp ref pinned at the CAPTURED sha
(``refs/helm-job/<challenge>``) — never from the symbolic HEAD a concurrent
commit could move — streamed to ssh stdin from disk (never read whole into
RAM) from a PRIVATE per-run directory — `mkdtemp`, so two routes of the same
HEAD cannot name the same file and unlink each other's live stdin mid-stream
(review 6580ad1dc5e0) — and the far side fetches the ref and detaches onto
the exact sha, re-verifying head and tree. A dirty worktree refuses up
front, and the caller tree is READ AGAIN after the minutes-long remote run:
a HEAD/tree/dirty that moved during transport refuses the import, because
the exit code would authorise a tree the operator is no longer standing on.

THE CAP CHECK RUNS THERE, BY CONSTRUCTION. The remote command is the shipped
tree's own `python3 -m helm gate run`, so admission — `suite_cap` over the
REMOTE box's /proc, its PSI floor, its FIFO — is judged by the only machine
that can see those facts. This module never re-derives a cap locally; a
remote refusal arrives as the gate's own `--json` reason and is relayed
verbatim, unimported. (MVP scope, stated: the shipped tree must CARRY helm —
gating a tree without a helm package refuses up front with the fab
alternative named, instead of failing minutes later with `No module named
helm` on the far box.)

SCRATCH IS BUDGETED BY THE RAM-SCRATCH LAW. The remote job dir prefers tmpfs
(/dev/shm) only while that mount is under `scratch.HEADROOM_PCT` on BOTH
percentage axes AND keeps `scratch.BIG_INODE_FLOOR` absolute free inodes —
the nr_inodes lesson, both spellings. The scratch root must be absolute
(a relative TMPDIR would strand the dir after `cd`). HUP/INT/PIPE/TERM clean
the dir and exit signal-derived; every blocking stage runs in a tracked
`setsid` group with bounded TERM→KILL. The far side also owns a hard deadline:
killing or timing out the local ssh client does not signal sshd's non-pty child,
so client loss can never make remote scratch immortal. SSH keepalives bound a
dead network path, output readers retain only a fixed memory ceiling while
continuing to drain, and the EXIT trap covers normal completion.

FAIL CLOSED, WITH NAMES. An unconsented box refuses naming the exact lever
that would consent it; an INCOMPLETE inventory (provider failure) refuses as
UNKNOWN rather than reading as absence; a dead transport names the box and
the ssh failure; a remote refusal names the remote reason; clone failures
carry a bounded laundered diagnostic instead of a generic code; and a FAILED
suite is not a failure of routing — its receipt imports honestly and binds
nothing, exactly as a local FAILED does. No partial artifact exists on any
error path (temp + atomic replace), and the import seam's ladder (content
id, head resolution, tree equality, idempotency) is the only door into the
ledger.
"""
import base64
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid

from . import boxes, gate, gateimport, home, pk, scratch, vcs

# The ssh option discipline is storage_matrix._remote_probe's: BatchMode so a
# missing key fails closed instead of prompting a pane that has no human, and
# a bounded connect. The RUN timeout is separate and generous — a whole suite
# is minutes, not seconds.
_SSH_CONNECT_TIMEOUT_S = 15
_SSH_KEEPALIVE_INTERVAL_S = 15
_SSH_KEEPALIVE_COUNT_MAX = 3
_REMOTE_TEARDOWN_GRACE_S = 30.0
_TRANSPORT_OUTPUT_CAP = 8 * 1024 * 1024
# The session ceiling accounts for the remote clocks SEPARATELY: the caller's
# --timeout bounds the SUITE and starts at remote child creation (after FIFO
# admission — the same point the local gate starts it); the queue allowance
# covers the wait for that admission; the slack covers transfer + clone.
# Folding queue into the suite clock was review finding 13: a queued box
# would have its ssh killed before the gate clock even started.
_DEFAULT_SESSION_CEILING_S = 3600.0
# PATH wrappers may cold-start an interpreter before it can publish pidfd,
# start-tick and PDEATHSIG identity. Five seconds is bounded and fail-closed,
# but avoids treating ordinary loader latency as a missing watchdog.
_DEADLINE_READY_TIMEOUT_S = 5.0
_DEADLINE_READY_POLL_S = 0.01
_DEADLINE_HELPER = r'''import ctypes
import os
import select
import signal
import sys

PR_SET_PDEATHSIG = 1


def proc_row(pid):
    with open("/proc/%d/stat" % pid, "rb") as f:
        fields = f.read().rsplit(b")", 1)[1].split()
    if len(fields) <= 19:
        raise RuntimeError("short-proc-stat")
    return int(fields[1]), int(fields[19]), fields[0].decode("ascii")


def record(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def fail(path, code):
    try:
        record(path + ".error", code + "\n")
    except OSError:
        pass
    raise SystemExit(70)


if not sys.flags.isolated or not sys.flags.no_site or "site" in sys.modules:
    raise SystemExit(70)
target, target_start = int(sys.argv[1]), int(sys.argv[2])
ceiling, ready = float(sys.argv[3]), sys.argv[4]
try:
    target_row = proc_row(target)
    if target_row[1] != target_start or target_row[2] in ("Z", "X"):
        fail(ready, "target-changed")
    target_fd = os.pidfd_open(target, 0)
except (AttributeError, OSError, RuntimeError):
    fail(ready, "no-pidfd")
parent = os.getppid()
try:
    parent_row = proc_row(parent)
except (OSError, RuntimeError):
    fail(ready, "parent-unreadable")
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
    fail(ready, "pdeathsig-failed")
if os.getppid() != parent:
    fail(ready, "parent-changed")
try:
    parent_now, target_now = proc_row(parent), proc_row(target)
    if (parent_now[:2] != parent_row[:2] or target_now[:2] != target_row[:2]
            or parent_now[2] in ("Z", "X") or target_now[2] in ("Z", "X")):
        fail(ready, "identity-changed")
except OSError:
    fail(ready, "identity-lost")
if parent == target:
    mode = "direct"
elif parent_row[0] == target:
    mode = "wrapper"
else:
    fail(ready, "unsupported-wrapper")
# Normal external setsid(1) already made direct Python the session leader.
# A fork/wait interpreter wrapper leaves its Python child in the wrapper's
# session, so this detach is defense for that accepted one-level shape.
try:
    if os.getsid(0) != os.getpid():
        os.setsid()
except OSError:
    fail(ready, "setsid-failed")
if os.getsid(0) != os.getpid() or os.getpgrp() != os.getpid():
    fail(ready, "setsid-failed")
helper_row = proc_row(os.getpid())
record(ready, "v1 %d %d %d %d %d %d %s\n" % (
    os.getpid(), helper_row[1], parent, parent_row[1], target,
    target_row[1], mode))
poller = select.poll()
poller.register(target_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
events = poller.poll(max(0, int(ceiling * 1000)))
if events:
    raise SystemExit(0)
try:
    current = proc_row(target)
except OSError:
    raise SystemExit(0)
if current[1] != target_start or current[2] in ("Z", "X"):
    raise SystemExit(0)
try:
    signal.pidfd_send_signal(target_fd, signal.SIGTERM)
except ProcessLookupError:
    pass
'''
_REMOTE_QUEUE_ALLOWANCE_S = 1800.0
_TRANSPORT_SLACK_S = 600.0
# Bundling a whole history is disk-bound minutes on a big repo; vcs.text's
# default 30s ceiling was sized for status reads (finding 10).
_BUNDLE_TIMEOUT_S = 300

JOB_CONSENT_ENV = "HELM_BOXES_JOB_CONSENT"

# Focus planning/minting normally comes from the pinned trunk tree so an old
# lane can use today's selector without rebasing away its receipt/review
# identity. These paths OWN that execution contract: a lane changing one must
# run its own code instead. An explicit tuple is intentional — a name pattern
# false-positives docs/tests and silently misses the next neutrally-named owner.
FOCUS_RUNNER_OWNING_PATHS = (
    "bin/helm",
    "helm/__main__.py",
    "helm/cli.py",
    "helm/gate.py",
    "helm/gatechild.py",
    "helm/gateimport.py",
    "helm/gateroute.py",
    # no such file ships: gate still ADMITS stored receipts naming this legacy
    # runner (gate._STORED_SUITE_RUNNERS), so a lane that re-creates it is
    # writing a runner and must run its own code
    "helm/gaterunner.py",
    "helm/gateshard.py",
)

# In-band remote failures, named. The script exits 0 on these — the ERROR is
# the payload; a nonzero ssh rc is reserved for the transport itself.
_REMOTE_ERRORS = {
    "no-git": "it has no git on PATH",
    "no-python3": "it has no executable python3 on PATH",
    "no-proc": "its Linux process identity cannot be read from /proc",
    "deadline-unarmed": "its far-side deadline helper did not become ready",
    "deadline-bad-ready": "its far-side deadline helper reported the wrong identity",
    "no-setsid": "it has no setsid on PATH, so remote process groups cannot "
                 "be cleaned safely",
    "no-base64": "it has no base64 on PATH, so payload cannot be framed "
                 "apart from protocol",
    "no-runtime-tools": "it lacks a required runtime tool on PATH",
    "no-scratch": "it could not create a scratch dir",
    "no-home": "it could not create the job's HELM_HOME (scratch exhausted?)",
    "bundle-transfer": "the tree bundle did not arrive intact",
    "head-unreadable": "the cloned tree's HEAD is unreadable",
    "tree-unreadable": "the cloned tree's HEAD^{tree} is unreadable",
    "runner-checkout": "the pinned trunk gate runner could not be checked out",
    "cd-failed": "the cloned tree is not enterable",
}


def _job_consent_err(name, row):
    """None when the ACTUAL ssh channel may execute a job, else refusal.

    Probing consent is not execution consent (the P0 of review
    a7235e643f64), and THE OWNER'S ENV IS THE ONLY GRANT (review
    6580ad1dc5e0): `fab_accept` is a transient scheduler eligibility reading,
    not a capability — see `_eligibility_note`. The grant binds `ssh_host`,
    never a display/lookup alias: authorising host=build-1 cannot ship code to
    that row's unrelated ssh_host=elsewhere. Absent refuses."""
    raw = (os.environ.get(JOB_CONSENT_ENV) or "").strip()
    if raw.lower() in ("all", "1"):
        return None
    channel = row.get("ssh_host")
    named = {h.strip() for h in raw.split(",") if h.strip()}
    if channel in named:
        return None
    return ("box %r resolves to ssh channel %r, which has PROBE consent but "
            "no JOB consent — HELM_STORAGE_MATRIX_HOSTS / "
            "HELM_BOXES_SSH_CONSENT authorize reading a box, never executing "
            "shipped code on it. Grant the actual channel explicitly: %s=%s "
            "(or =all). The inventory's fab_accept cannot grant it: that "
            "field is fab's transient eligibility proxy (up, cargo-ready, "
            "disk above the floor), which is not a decision about running "
            "arbitrary code"
            % (name, channel, JOB_CONSENT_ENV, channel))


def _eligibility_note(row):
    """The inventory's `fab_accept`, as the ADMISSION note it actually is.

    Never a consent decision in either direction (review 6580ad1dc5e0) — it
    is recomputed per render from up/cargo/disk, so it is stale by nature and
    withholding a granted job on it would void the owner's grant on a disk
    reading. Surfaced so the operator can read a likely-slow route, and
    judged for real by the remote gate's own admission."""
    accept = str(row.get("fab_accept") or "").strip().lower()
    if accept == "no":
        return ("the inventory says fab_accept=no for this box — that is "
                "fab's transient eligibility proxy (up, cargo-ready, "
                "build-storage above the floor), NOT a consent decision, so "
                "it does not withhold a job you granted; the box may be busy "
                "or short of disk, and its own gate is the admission judge")
    if accept == "n/a":
        return ("the inventory says fab_accept=n/a for this box (fab does "
                "not treat it as a build node) — an eligibility note only")
    return None


# THE DOOR THIS LADDER IS NOT, AND THE REASON EVERY REFUSAL NOW NAMES IT.
# Measured by walking into it three times in a row: box not in inventory ->
# set HELM_STORAGE_MATRIX_HOSTS; candidate -> set HELM_BOXES_SSH_CONSENT;
# granted, then PROBE consent but no JOB consent -> set HELM_BOXES_JOB_CONSENT.
# Three refusals, each honest, each naming its lever, and every one of them
# routing the caller DEEPER into the door they already chose. The ladder's own
# law says a refusal that does not name the lever is a hidden gate on the only
# person who could open it; the same law one level up is that a refusal which
# names only the lever for THIS door is a hidden gate at the DOOR level, when a
# door exists that needs no lever at all.
#
# The one exemption is the refusal that already routes — asking for THIS
# machine, which correctly says to drop --box and gains nothing from a second
# suggestion. It says so itself by returning a third element.
# AND IT NAMES THE REPOSITORY THE CALLER SELECTED, NEVER `.`. Routing takes an
# explicit --repo and never changes cwd, so advice spelled with a bare dot
# sends a caller who asked about repository B off to gate repository A — a
# refusal that is not merely unhelpful but actively wrong, and wrong in a way
# that looks like it worked. The path is shell-quoted because it reaches the
# operator as a command to paste and a repository path may hold spaces or
# shell metacharacters.
def _shell_target(path):
    """A bash word that decodes to EXACTLY the bytes of `path`, printable ASCII.

    A POSIX PATH IS BYTES, AND EVERY EARLIER SHAPE HERE TREATED IT AS TEXT.
    Three rounds went into widening a set of unsafe CHARACTERS — C0, then C1
    and DEL, then the Cc/Cf/Zl/Zp category table — and each round was correct
    about the characters it had and wrong about the object. Encoding a str and
    asking the reader's shell to re-derive bytes through its locale makes the
    exactness conditional on an environment nobody stated: measured, four
    specimens through a real bash in binary, the Unicode-escape form was exact
    4/4 under the inherited locale and 1/4 under LC_ALL=C.

    So there is no character test left, and no conditional branch. Every byte
    of os.fsencode(path) is written as a fixed-width \\xNN escape inside a bash
    ANSI-C word. Three properties fall out of the shape instead of being
    asserted about it:
      EXACT      — bash reproduces the bytes, and \\xNN is a BYTE escape, so no
                   locale participates in the decode.
      INERT      — the rendered word is printable ASCII by construction, so no
                   path can move a cursor or reorder the line reporting it.
      UNIFORM    — one code path for every input, so there is no boundary left
                   for a fourth character to be found on.

    The cost is that an ordinary path prints as escapes rather than as itself.
    That was weighed and chosen: one verifiable representation beats a readable
    one plus a second branch, and the second branch is the shape that failed
    three times. The bash requirement is stated on every command this emits.
    """
    return "$'" + "".join("\\x%02x" % b for b in os.fsencode(path)) + "'"


def _fab_door(repo=None):
    """The refusal's last line: the door that needs no consent flag.

    The target is always an encoded byte word, so the bash requirement is
    stated on EVERY command rather than on a subset — a note that appears
    only sometimes is a note the reader learns to skip.
    """
    if repo is None:
        return ("  There is also a door that needs no flag: `fab gate --repo .` "
                "ships the suite to a build node and auto-imports the receipt "
                "into the binding ledger.")
    return ("  There is also a door that needs no flag: `fab gate --repo %s` "
            "ships the suite to a build node and auto-imports the receipt "
            "into the binding ledger. The target is byte-encoded so it is "
            "exact and safe to print: paste it into bash, zsh or ksh — dash "
            "does not read $'...' and would take it literally."
            % _shell_target(os.path.realpath(repo)))


def pick_box(name, nodes=None, repo=None):
    """-> (row, err): the ONE inventory row this job may ride, or the refusal.

    Every refusal that leaves the caller with NO route also names the fab
    door, spelled with the repository THIS call selected; a refusal that
    already named the caller's next door says so with a third element and is
    left alone.
    """
    out = _pick_box(name, nodes=nodes)
    row, err = out[0], out[1]
    already_routed = len(out) > 2 and out[2]
    if err and not already_routed:
        err = err + _fab_door(repo)
    return row, err


def _pick_box(name, nodes=None):
    """-> (row, err) or (row, err, already_routed): the consent ladder.

    The ladder refuses in consent order and every rung NAMES the lever that
    would open it — a refusal that does not say which flag to set is a hidden
    gate on the only person who could set it. Resolution is unique-or-refuse
    over host/ssh_host (labels are display, never address), an incomplete
    inventory refuses as UNKNOWN instead of reading as absence, and probing
    consent alone never admits a job (see `_job_consent_err`)."""
    if not boxes.HOST_RE.match(name or ""):
        return None, "box name %r is not a valid host name" % (name,)
    warning = None
    if nodes is None:
        value, warning = boxes.inventory()
        nodes = value.get("nodes") or []
    # BEFORE RESOLUTION, not merely when nothing matched (review
    # 6580ad1dc5e0): a lower provider's surviving row cannot prove uniqueness
    # or authority while a richer provider is down — the missing rows may
    # hold a second identity for this same name, or a stronger word about it.
    # Incomplete is UNKNOWN, and UNKNOWN refuses whatever else matched.
    if warning:
        return None, ("the box inventory is INCOMPLETE (provider failure: "
                      "%s) — box %r cannot be proven unique, absent or "
                      "consented from a broken chain, so it does not carry a "
                      "job; repair the provider or declare the host in "
                      "HELM_STORAGE_MATRIX_HOSTS"
                      % (_launder(warning, "unnamed failure"), name))
    matches = [n for n in nodes if isinstance(n, dict)
               and name in (n.get("host"), n.get("ssh_host"))]
    if not matches:
        return None, ("box %r is not in the inventory — declare it in "
                      "HELM_STORAGE_MATRIX_HOSTS, or consent an ssh-config "
                      "candidate via HELM_BOXES_SSH_CONSENT (labels do not "
                      "address a box)" % name)
    if len(matches) > 1:
        return None, ("box name %r is AMBIGUOUS in the inventory — it "
                      "matches %d rows (%s); a job routes to one canonical "
                      "machine or none" % (
                          name, len(matches),
                          "; ".join(sorted(
                              "host=%r ssh_host=%r" % (n.get("host"),
                                                       n.get("ssh_host"))
                              for n in matches))))
    row = matches[0]
    mode = row.get("probe_mode")
    if mode == "local":
        # ALREADY ROUTED: this names the caller's next door, so the fab
        # suffix would be a second suggestion on a refusal that has one.
        return None, ("box %r is this machine — run `helm gate run` "
                      "without --box" % name), True
    if mode == "candidate":
        return None, ("box %r is listed but NOT consented — opt in with "
                      "HELM_BOXES_SSH_CONSENT=all or name it in that flag"
                      % name)
    if row.get("display_only"):
        return None, ("box %r is display-only in the inventory (probe_mode="
                      "%r) and cannot carry a job" % (name, mode))
    if mode != "ssh":
        return None, ("box %r has probe_mode=%r, which cannot carry a job "
                      "(needs ssh)" % (name, mode))
    # EXACT boolean: an external provider's 'false'/'no' STRING is truthy,
    # and truthiness here would ship code to a box the inventory called down.
    if row.get("reachable") is not True:
        return None, "box %r is not reachable per the inventory" % name
    ssh_host = row.get("ssh_host")
    if not isinstance(ssh_host, str) or not boxes.HOST_RE.match(ssh_host):
        return None, "box %r carries no usable ssh_host" % name
    # The row's own host string later names the fetched artifact file; an
    # external provider's '../../escaped' must die HERE, not in open().
    host_id = row.get("host")
    if host_id is not None and (not isinstance(host_id, str)
                                or not boxes.HOST_RE.match(host_id)):
        return None, ("box %r carries an unusable host identity %r in the "
                      "inventory" % (name, host_id))
    err = _job_consent_err(name, row)
    if err:
        return None, err
    return row, None


class SshTransport:
    """One remote sh session: script as the command, bundle STREAMED from
    disk on stdin (never read whole into RAM — finding 10)."""

    def __init__(self, ssh_host):
        self.ssh_host = ssh_host

    def run(self, script, stdin_path, timeout):
        """-> (rc, stdout_text, stderr_text, err).

        Keepalives bound a dead network path; the far-side watchdog owns remote
        cleanup because killing the local ssh client does NOT signal sshd's
        setsid child. Reader threads continue draining after the memory cap so
        a noisy remote cannot block ssh on a full pipe or allocate without
        bound in this process."""
        argv = ["ssh", "-T", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=%d" % _SSH_CONNECT_TIMEOUT_S,
                "-o", "ServerAliveInterval=%d" % _SSH_KEEPALIVE_INTERVAL_S,
                "-o", "ServerAliveCountMax=%d" % _SSH_KEEPALIVE_COUNT_MAX,
                "--", self.ssh_host, "sh -c " + shlex.quote(script)]
        held = {"stdout": [], "stderr": []}
        overflow = {"stdout": False, "stderr": False}

        def drain(name, stream):
            size = 0
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                room = max(0, _TRANSPORT_OUTPUT_CAP - size)
                if room:
                    held[name].append(chunk[:room])
                    size += min(len(chunk), room)
                if len(chunk) > room:
                    overflow[name] = True

        try:
            with open(stdin_path, "rb") as fh:
                proc = subprocess.Popen(argv, stdin=fh, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        start_new_session=True)
                readers = [threading.Thread(target=drain, args=(name, stream))
                           for name, stream in (("stdout", proc.stdout),
                                                ("stderr", proc.stderr))]
                for reader in readers:
                    reader.start()
                try:
                    rc = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    return None, None, None, (
                        "ssh session to %s exceeded its %ds ceiling"
                        % (self.ssh_host, timeout))
                except BaseException:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    raise
                finally:
                    for reader in readers:
                        reader.join()
                    proc.stdout.close()
                    proc.stderr.close()
        except OSError as exc:
            return None, None, None, "ssh could not start: %s" % exc
        if any(overflow.values()):
            streams = ", ".join(k for k, v in overflow.items() if v)
            return None, None, None, (
                "ssh session to %s exceeded the %d-byte output ceiling on %s"
                % (self.ssh_host, _TRANSPORT_OUTPUT_CAP, streams))
        return (rc, b"".join(held["stdout"]).decode("utf-8", "replace"),
                b"".join(held["stderr"]).decode("utf-8", "replace"), None)


def _remote_script(head, tree, label, timeout, challenge, focus=False,
                   trunk_name=None, trunk_sha=None, focus_runner="lane",
                   session_ceiling=None, sliced=False):
    """The one remote session, marker-structured so every outcome is in-band.

    EVERY MARKER CARRIES THE INVOCATION'S RANDOM CHALLENGE, so a REPLAYED
    transcript parses to nothing (findings 6 + 18) — but the challenge is a
    freshness proof, NOT the framing: it rides in this session's own argv and
    the suite child can read it from /proc (review 6580ad1dc5e0). PAYLOAD IS
    FRAMED BY ENCODING: every section body leaves base64, so no byte the job
    emits can spell a marker line whatever it knows. The script exits 0 on
    survivable failures — `error=<code>` IS that channel; a nonzero ssh rc
    always means the TRANSPORT. Signals clean the scratch and exit 128+sig
    (finding 15), and EVERY post-launch executable stage — readiness waits,
    probes, scratch, bundle, git, gate and payload framing — runs backgrounded
    and tracked through `stage` (or fd3-only `ship_stage`), because sh defers a
    trap until the foreground child exits and one blocked stage otherwise made
    the whole session unkillable."""
    if focus and sliced:
        raise ValueError("a routed gate is focused or sliced, never both")
    gate_args = " --focus" if focus else " --sliced" if sliced else ""
    trunk_fetch = "    'refs/helm-trunk/*:refs/helm-trunk/*' \\\n" \
        if focus else ""
    trunk_setup = ""
    runner_setup = 'gate_dir="$dir/tree"; gate_repo="$dir/tree"'
    if focus:
        if not trunk_name or not trunk_sha:
            raise ValueError("focused route needs a pinned trunk ref")
        if focus_runner not in ("lane", "trunk"):
            raise ValueError("focused route runner must be lane or trunk")
        if trunk_name.startswith("refs/"):
            remote_trunk = trunk_name
        elif trunk_name.startswith("origin/"):
            remote_trunk = "refs/remotes/" + trunk_name
        else:
            remote_trunk = "refs/heads/" + trunk_name
        trunk_setup = (
            'stage "$cmd_git" -C "$dir/tree" update-ref %s %s '
            '2>"$dir/git.err" || { gitdiag; err '
            '"trunk-ref:${git_diag:-cannot install pinned trunk}"; }'
            % (shlex.quote(remote_trunk), shlex.quote(trunk_sha)))
        if focus_runner == "trunk":
            runner_setup = (
                'stage "$cmd_git" -C "$dir/tree" worktree add -q --detach '
                '"$dir/runner" %s 2>"$dir/git.err" || { gitdiag; err '
                '"runner-checkout:${git_diag:-cannot checkout pinned trunk runner}"; }; '
                'gate_dir="$dir/runner"; gate_repo="$dir/tree"'
                % shlex.quote(trunk_sha))
    if label:
        gate_args += " --label %s" % shlex.quote(str(label))
    if timeout is not None:
        # `is not None`, never truthiness: --timeout 0 is an IMMEDIATE
        # deadline locally and must stay one remotely (finding 12).
        gate_args += " --timeout %s" % shlex.quote(repr(float(timeout)))
    session_ceiling = _DEFAULT_SESSION_CEILING_S if session_ceiling is None \
        else max(0.0, float(session_ceiling))
    return r"""
set -u
# The session's stdin (the streaming bundle) is DUPLICATED onto fd 3 before
# anything reads it, because every blocking stage below runs asynchronously
# and a POSIX shell assigns /dev/null to an async command's stdin unless the
# redirection is explicit — measured on dash: an async `cat >f` wrote ZERO
# bytes, while `ship_stage cat >f` with `<&3` wrote the payload. Without this
# the bundle would arrive
# empty and the far side would fail a stage later, blaming git.
exec 3<&0
say() { printf '%%s\n' "::helm-job-%(challenge)s $1"; }
err() { say "error=$1"; exit 0; }
root=${TMPDIR:-/tmp}
case "$root" in /*) : ;; *) root=/tmp ;; esac
kind=disk
dir=""
gpid=""
deadline_python=""
deadline_setsid=""
deadline_pid=""
deadline_start=""
deadline_ready=""
proc_start_value=""
proc_start() {
  [ -r "/proc/$1/stat" ] || return 1
  IFS= read -r proc_row < "/proc/$1/stat" || return 1
  proc_tail=${proc_row##*) }
  set -- $proc_tail
  [ "$#" -ge 20 ] || return 1
  proc_start_value=${20}
}
same_process() {
  proc_start "$1" 2>/dev/null || return 1
  [ "$proc_start_value" = "$2" ]
}
cancel_deadline() {
  if [ -n "$deadline_pid" ]; then
    # `$!` remains an unreaped child, so its numeric pid cannot be recycled:
    # cancellation is safe even in the gap before `deadline_start` is read.
    # External setsid(1) establishes the launcher group before exec. In direct
    # mode killing that stable pid kills Python; with a fork/wait PATH wrapper,
    # killing the wrapper triggers the detached helper's PDEATHSIG. Stopping the
    # launcher group also catches any wrapper child that has not detached yet.
    kill -KILL "$deadline_pid" 2>/dev/null
    stop_group "$deadline_pid"
    wait "$deadline_pid" 2>/dev/null
    deadline_pid=""
  fi
  [ -n "$deadline_ready" ] \
    && rm -f "$deadline_ready" "$deadline_ready.error" "$probe".*
}
# TERM is the courtesy; KILL is the bound. Run in a subshell so the helper's
# scratch variables cannot leak into the protocol shell. `kill -0 -PGID` tests
# the WHOLE group after its leader exits, not merely the pid we can wait on.
stop_group() (
  pg=$1
  kill -0 -"$pg" 2>/dev/null || exit 0
  kill -TERM -"$pg" 2>/dev/null
  n=0
  while kill -0 -"$pg" 2>/dev/null && [ "$n" -lt 20 ]; do
    "$deadline_sleep" 0.05
    n=$((n+1))
  done
  kill -0 -"$pg" 2>/dev/null && kill -KILL -"$pg" 2>/dev/null
)
finish() {
  trap '' HUP INT PIPE TERM
  cancel_deadline
  if [ -n "$gpid" ]; then
    stop_group "$gpid"
    wait "$gpid" 2>/dev/null
    gpid=""
  fi
  [ -n "$dir" ] && rm -rf "$dir"
  exit $((128+$1))
}
trap 'cancel_deadline; [ -n "$dir" ] && rm -rf "$dir"' EXIT
trap 'finish 1' HUP
trap 'finish 2' INT
trap 'finish 13' PIPE
trap 'finish 15' TERM
# ssh client death does not signal sshd's non-pty setsid child (measured over
# localhost ssh). The far side therefore owns a hard deadline of its own;
# local transport waits a grace beyond it so cleanup wins the race.
# Resolve ONE PATH interpreter before cd and use it for both watchdog and gate.
# Loader variables are unset by the already-running shell — `env -u` would be
# too late because loading env itself has already honored LD_PRELOAD.
deadline_python=$(command -v python3) || err no-python3
case "$deadline_python" in
  /*) : ;;
  */*) deadline_python="$PWD/${deadline_python#./}" ;;
  *) err no-python3 ;;
esac
deadline_setsid=$(command -v setsid) || err no-setsid
case "$deadline_setsid" in
  /*) : ;;
  */*) deadline_setsid="$PWD/${deadline_setsid#./}" ;;
  *) err no-setsid ;;
esac
deadline_sleep=$(command -v sleep) || err deadline-unarmed
case "$deadline_sleep" in
  /*) : ;;
  */*) deadline_sleep="$PWD/${deadline_sleep#./}" ;;
  *) err deadline-unarmed ;;
esac
[ -x "$deadline_python" ] || err no-python3
[ -x "$deadline_setsid" ] || err no-setsid
[ -x "$deadline_sleep" ] || err deadline-unarmed
unset LD_PRELOAD LD_AUDIT LD_LIBRARY_PATH PYTHONPATH PYTHONHOME
# One owner for every post-launch executable. `stage` gives non-shipment work
# /dev/null; only `ship_stage` may consume fd 3, preserving the streamed bundle.
# With every child in a known setsid group, dash may defer a trap only while
# `wait` is interruptible, and `finish` can kill the exact group before cleanup.
stage() {
  "$deadline_setsid" "$@" </dev/null & gpid=$!
  wait "$gpid"; rc=$?
  stop_group "$gpid"
  gpid=""
  return $rc
}
ship_stage() {
  "$deadline_setsid" "$@" <&3 & gpid=$!
  wait "$gpid"; rc=$?
  stop_group "$gpid"
  gpid=""
  return $rc
}
proc_start "$$" || err no-proc
shell_start=$proc_start_value
# The 128-bit challenge makes blind precreation in a world-writable TMPDIR
# infeasible. A hostile same-UID peer can read argv and deny availability, but
# that peer can already signal this shell directly; same-UID isolation is not a
# trust boundary. The verified pid/start fields still prevent stale acceptance.
deadline_ready="$root/.helm-deadline.%(challenge)s.$$"
probe="$root/.helm-probe.%(challenge)s.$$"
"$deadline_setsid" "$deadline_python" -I -S -c %(deadline_helper)s \
  "$$" "$shell_start" %(session_ceiling)s "$deadline_ready" &
deadline_pid=$!
proc_start "$deadline_pid" || err deadline-unarmed
deadline_start=$proc_start_value
n=0
while [ ! -s "$deadline_ready" ]; do
  if [ -s "$deadline_ready.error" ]; then
    IFS= read -r deadline_error < "$deadline_ready.error"
    err "deadline-unarmed:${deadline_error:-unknown}"
  fi
  kill -0 "$deadline_pid" 2>/dev/null || err deadline-unarmed
  [ "$n" -lt %(ready_attempts)d ] || err deadline-unarmed
  stage "$deadline_sleep" %(ready_poll)s
  n=$((n+1))
done
IFS=' ' read -r ready_v helper helper_start parent parent_start \
  target target_start helper_mode < "$deadline_ready" \
  || err deadline-bad-ready
[ "$ready_v" = v1 ] || err deadline-bad-ready
[ "$target" = "$$" ] && [ "$target_start" = "$shell_start" ] \
  || err deadline-bad-ready
same_process "$helper" "$helper_start" || err deadline-bad-ready
case "$helper_mode" in
  direct)
    [ "$helper" = "$deadline_pid" ] && [ "$parent" = "$$" ] \
      || err deadline-bad-ready ;;
  wrapper)
    [ "$parent" = "$deadline_pid" ] \
      && [ "$parent_start" = "$deadline_start" ] \
      && same_process "$parent" "$parent_start" \
      || err deadline-bad-ready ;;
  *) err deadline-bad-ready ;;
esac
# Only an acknowledged watchdog may precede executable substrate checks.
# Resolve each executable once, before cd, so a relative PATH entry cannot
# switch tools inside the shipped tree. `command -v` is a shell builtin; its
# output crosses no untracked child and is read back through the protocol file.
resolve_stage() {
  command -v "$1" > "$probe.path" 2>/dev/null || return 1
  IFS= read -r resolved < "$probe.path" || return 1
  case "$resolved" in
    /*) : ;;
    */*) resolved="$PWD/${resolved#./}" ;;
    *) return 1 ;;
  esac
  [ -x "$resolved" ]
}
resolve_stage git || err no-git; cmd_git=$resolved
resolve_stage base64 || err no-base64; cmd_base64=$resolved
resolve_stage df || err no-runtime-tools; cmd_df=$resolved
resolve_stage mktemp || err no-scratch; cmd_mktemp=$resolved
resolve_stage uname || err no-runtime-tools; cmd_uname=$resolved
resolve_stage mkdir || err no-scratch; cmd_mkdir=$resolved
resolve_stage cat || err bundle-transfer; cmd_cat=$resolved
resolve_stage tail || err no-runtime-tools; cmd_tail=$resolved
resolve_stage env || err no-runtime-tools; cmd_env=$resolved
cmd_systemd=""
if resolve_stage systemd-run; then cmd_systemd=$resolved; fi
# Read one df row without an awk pipeline. The df process itself is a tracked
# stage; parsing is shell-only, so no foreground descendant can defer TERM.
df_field() {
  df_flag=$1; df_column=$2; df_row=""; n=0
  stage "$cmd_df" "$df_flag" /dev/shm > "$probe.df" 2>/dev/null \
    || return 1
  while IFS= read -r df_line; do
    n=$((n+1))
    if [ "$n" -eq 2 ]; then df_row=$df_line; break; fi
  done < "$probe.df"
  [ -n "$df_row" ] || return 1
  set -- $df_row
  [ "$#" -ge 5 ] || return 1
  case "$df_column" in
    4) df_value=$4 ;;
    5) df_value=$5 ;;
    *) return 1 ;;
  esac
}
strip_pct() {
  stripped=$1
  case "$stripped" in *%%) stripped=${stripped%%?} ;; esac
}
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
  bpct=""; ipct=""; ifree=""
  if df_field -P 5; then strip_pct "$df_value"; bpct=$stripped; fi
  if df_field -Pi 5; then strip_pct "$df_value"; ipct=$stripped; fi
  if df_field -Pi 4; then ifree=$df_value; fi
  case "$bpct$ipct$ifree" in
    ''|*[!0-9]*) : ;;
    *) if [ "$bpct" -lt %(headroom)d ] && [ "$ipct" -lt %(headroom)d ] \
          && [ "$ifree" -ge %(inode_floor)d ]; then
         root=/dev/shm; kind=tmpfs
       fi ;;
  esac
fi
stage "$cmd_mktemp" -d "$root/helm-job.XXXXXXXX" > "$probe.value" \
  || err no-scratch
IFS= read -r dir < "$probe.value" || err no-scratch
stage "$cmd_uname" -n > "$probe.value" || err no-runtime-tools
IFS= read -r node < "$probe.value" || err no-runtime-tools
[ -n "$node" ] || err no-runtime-tools
say "node=$node"
say "scratch=$kind"
ship_stage "$cmd_cat" > "$dir/ship.bundle" || err bundle-transfer
gitdiag() {
  : > "$probe.diag"
  stage "$cmd_tail" -c 200 "$dir/git.err" > "$probe.diag" 2>/dev/null || :
  git_diag=""
  while IFS= read -r diag_line || [ -n "$diag_line" ]; do
    [ -n "$git_diag" ] && git_diag="$git_diag "
    git_diag="$git_diag$diag_line"
  done < "$probe.diag"
}
stage "$cmd_mkdir" "$dir/tree" 2>/dev/null || err no-scratch
if ! stage "$cmd_git" -C "$dir/tree" init -q 2>"$dir/git.err"; then
  gitdiag; err "clone-failed:${git_diag:-no git diagnostic}"
fi
if ! stage "$cmd_git" -C "$dir/tree" fetch -q "$dir/ship.bundle" \
    'refs/helm-job/*:refs/helm-job/*' \
%(trunk_fetch)s    2>"$dir/git.err"; then
  gitdiag; err "clone-failed:${git_diag:-no git diagnostic}"
fi
%(trunk_setup)s
if ! stage "$cmd_git" -C "$dir/tree" checkout -q --detach "%(head)s" \
    2>"$dir/git.err"; then
  gitdiag; err "clone-failed:${git_diag:-no git diagnostic}"
fi
stage "$cmd_git" -C "$dir/tree" rev-parse HEAD > "$probe.value" 2>/dev/null \
  || err head-unreadable
IFS= read -r got < "$probe.value" || err head-unreadable
[ "$got" = "%(head)s" ] || err "head-mismatch:$got"
stage "$cmd_git" -C "$dir/tree" rev-parse 'HEAD^{tree}' \
  > "$probe.value" 2>/dev/null || err tree-unreadable
IFS= read -r gtree < "$probe.value" || err tree-unreadable
[ "$gtree" = "%(tree)s" ] || err "tree-mismatch:$gtree"
stage "$cmd_mkdir" -p "$dir/home" || err no-home
%(runner_setup)s
cd "$gate_dir" || err cd-failed
say "runner=%(focus_runner)s"
# The gate process imports from gate_dir; the suite process runs with cwd=gate_repo,
# so ordinary lanes use trunk's selector/minting code while their tests import the
# shipped lane package. Owner-path lanes set both to the shipped tree instead.
# The gate's cgroup guard needs a USER-DELEGATED cgroup: a bare ssh session
# scope is root-owned (measured on a fab node 2026-08-05: mkdir denied), so the
# guard dies before its report. fab's spoke gate proved the cure — wrap the
# gate in `systemd-run --user --scope`, which lands it under the delegated
# user@.service/app.slice. Where that is unavailable the gate runs direct
# and its guard stays the honest judge (fail closed, never bypassed).
cg=direct
if [ -n "$cmd_systemd" ] \
   && stage "$cmd_systemd" --user --scope --quiet true 2>/dev/null; then
  cg=delegated
fi
say "cgroup=$cg"
if [ "$cg" = delegated ]; then
  stage "$cmd_env" "HELM_HOME=$dir/home" "$cmd_systemd" \
    --user --scope --quiet "$deadline_python" "$gate_dir/bin/helm" gate run \
    --repo "$gate_repo" --json%(gate_args)s \
    >"$dir/gate.json" 2>"$dir/gate.err"
else
  stage "$cmd_env" "HELM_HOME=$dir/home" "$deadline_python" \
    "$gate_dir/bin/helm" gate run --repo "$gate_repo" \
    --json%(gate_args)s >"$dir/gate.json" 2>"$dir/gate.err"
fi
grc=$?
say "gate-rc=$grc"
emit_file() {
  emit_name=$1; emit_path=$2
  say "$emit_name-begin"
  if [ -r "$emit_path" ]; then
    stage "$cmd_base64" "$emit_path" 2>/dev/null || :
  fi
  say "$emit_name-end"
}
emit_tail() {
  emit_name=$1; emit_path=$2
  : > "$probe.tail"
  if [ -r "$emit_path" ]; then
    stage "$cmd_tail" -c 2000 "$emit_path" > "$probe.tail" 2>/dev/null || :
  fi
  say "$emit_name-begin"
  stage "$cmd_base64" "$probe.tail" 2>/dev/null || :
  say "$emit_name-end"
}
emit_file gate-json "$dir/gate.json"
emit_tail gate-err "$dir/gate.err"
emit_file receipts "$dir/home/%(global)s/gate-receipts.jsonl"
say "done"
""" % {"head": head, "tree": tree, "gate_args": gate_args,
       "trunk_fetch": trunk_fetch, "trunk_setup": trunk_setup,
       "runner_setup": runner_setup, "focus_runner": focus_runner,
       "headroom": scratch.HEADROOM_PCT,
       "inode_floor": scratch.BIG_INODE_FLOOR,
       "session_ceiling": shlex.quote(repr(session_ceiling)),
       "deadline_helper": shlex.quote(_DEADLINE_HELPER),
       "ready_attempts": int(_DEADLINE_READY_TIMEOUT_S
                             / _DEADLINE_READY_POLL_S),
       "ready_poll": shlex.quote(repr(_DEADLINE_READY_POLL_S)),
       "challenge": challenge, "global": home.GLOBAL}


def _parse_markers(out, challenge):
    """-> (fields, sections, violation): the in-band protocol, read strictly.

    The challenge-bearing prefix selects control lines, but the challenge is
    NOT what keeps payload out of control (the suite child can read it from
    the ancestor's /proc/<pid>/cmdline — review 6580ad1dc5e0). Three
    structural rungs do that, and they answer DIFFERENT writers, which is
    why all three are here rather than whichever one reads best:

    POSITION — once a section opens, nothing inside it is control but that
    section's own end marker, so a marker-shaped payload line is data.
    ENCODING — section bodies arrive base64 (no ':' in the alphabet), so
    payload cannot spell a marker at all; a body that is not base64 is a
    VIOLATION, never read as text.
    Those two answer the job's OWN OUTPUT, which is the review's repro (a
    live-challenge line inside a payload section) — and either alone kills
    it. Neither touches the other writer:
    ARITY — a control field is written ONCE, first writer wins, a repeat
    refuses the transcript. That is the rung for a child writing straight to
    the session's stdout mid-run (a same-uid process can open the ancestor's
    fd), where NO section is open and position has nothing to say: redefining
    `node=` there would otherwise re-point the host binding at a receipt
    about a machine that never ran this job.
    """
    mark = "::helm-job-%s " % challenge
    fields, raw, current, violation = {}, {}, None, None
    for line in (out or "").splitlines():
        token = line[len(mark):].strip() if line.startswith(mark) else None
        if current is not None:
            if token == current + "-end":
                current = None
            else:
                raw[current].append(line)
            continue
        if token is None:
            continue
        if token.endswith("-begin"):
            current = token[:-len("-begin")]
            if current in raw:
                violation = violation or (
                    "section %r is opened twice" % current)
            raw.setdefault(current, [])
            continue
        if token.endswith("-end"):
            continue
        if token == "done":
            key, value = "done", True
        elif "=" in token:
            key, _, value = token.partition("=")
        else:
            continue
        if key in fields:
            violation = violation or (
                "the control field %r is written twice (%r then %r)"
                % (key, fields[key], value))
            continue
        fields[key] = value
    sections = {}
    for key, lines in raw.items():
        try:
            sections[key] = base64.b64decode(
                "".join(l.strip() for l in lines).encode("ascii"),
                validate=True).decode("utf-8", "replace")
        except (ValueError, UnicodeEncodeError):
            violation = violation or (
                "section %r did not arrive base64-framed" % key)
            sections[key] = ""
    return fields, sections, violation


_BUNDLE_DIR_PREFIX = "gate-route-job."


def _bundle(repo, head, challenge, trunk=None):
    """-> (path, err): captured head (+ focused trunk), packed for transit.

    Bundled from a temp ref pinned at that sha — `git bundle create` cannot
    take a raw sha (probed: 'Refusing to create empty bundle'), and bundling
    the symbolic HEAD would ship whatever a concurrent commit moved it to,
    wasting the whole transfer before the remote mismatch (finding 9). The
    far side fetches `refs/helm-job/*` and detaches onto the sha.

    THE FILE LIVES IN A PRIVATE PER-RUN DIRECTORY (review 6580ad1dc5e0). A
    name derived from HEAD alone is SHARED by every concurrent route of the
    same commit — the routine case, since that is the tree everyone is
    gating — and the second route's create truncated, or its cleanup
    unlinked, a file the first route was still streaming to ssh. `mkdtemp`
    makes the name unguessable and the claim atomic and exclusive by
    construction, so no two routes can collide however identical their tips.
    The caller owns `_bundle_cleanup` on the returned path."""
    dest_dir, why = scratch.resolve("small", name="gate-route")
    if dest_dir is None:
        return None, "no local scratch for the bundle: %s" % why
    try:
        job_dir = tempfile.mkdtemp(prefix=_BUNDLE_DIR_PREFIX, dir=dest_dir)
    except OSError as exc:
        return None, "no private scratch dir for the bundle: %s" % exc
    path = os.path.join(job_dir, "ship-%s-%s.bundle" % (head[:12], challenge))
    ref = "refs/helm-job/" + challenge
    trunk_ref = "refs/helm-trunk/" + challenge if trunk else None
    refs = [ref] + ([trunk_ref] if trunk_ref else [])
    git = vcs.backend(repo)
    for pinned, sha in ((ref, head), (trunk_ref, trunk)):
        if not pinned:
            continue
        rc, _out, err = git.text(repo, "update-ref", pinned, sha)
        if rc != 0:
            for made in refs:
                git.text(repo, "update-ref", "-d", made)
            _bundle_cleanup(path)
            return None, "cannot pin the job ref: %s" % (
                (err or "").strip()[:240] or "exit %s" % rc)
    try:
        rc, _out, err = git.text(repo, "bundle", "create", path, *refs,
                                 timeout=_BUNDLE_TIMEOUT_S)
        if rc != 0:
            _bundle_cleanup(path)
            return None, "git bundle create failed: %s" % (
                (err or "").strip()[:240] or "exit %s" % rc)
    finally:
        for made in refs:
            git.text(repo, "update-ref", "-d", made)
    return path, None


def _bundle_cleanup(path):
    """Remove THIS run's private bundle dir, and only ever one of those.

    The containment check is the point: cleanup runs in a `finally` beside a
    concurrent route's, so it must be structurally incapable of removing
    anything but a directory `_bundle` minted for this invocation."""
    job_dir = os.path.dirname(path or "")
    if os.path.basename(job_dir).startswith(_BUNDLE_DIR_PREFIX):
        shutil.rmtree(job_dir, ignore_errors=True)


def _artifact_write(box_name, receipt_id, jsonl_text):
    """The fetched ledger lines, verbatim, at a provenance-recordable path.

    The name component is slugged and the resolved path containment-checked
    (finding 8: an inventory row's host string is EXTERNAL data), and the
    write is temp + atomic replace so no error path leaves a partial
    provenance-shaped artifact behind (finding 11)."""
    dest_dir, why = scratch.resolve("small", name="gate-route")
    if dest_dir is None:
        return None, "no local scratch for the artifact: %s" % why
    name = pk.slug(str(box_name)) or "box"
    path = os.path.join(dest_dir, "%s-%s.jsonl" % (name, receipt_id))
    base = os.path.realpath(dest_dir)
    if os.path.commonpath((base, os.path.realpath(path))) != base:
        return None, "artifact path escapes the scratch dir — refused"
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(jsonl_text if jsonl_text.endswith("\n")
                     else jsonl_text + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None, "cannot write the fetched artifact: %s" % _launder(
            exc, "unknown write failure")
    return path, None


def _launder(text, absent):
    """Remote-produced diagnostic text, made terminal-safe at THIS sink.

    ssh stderr, the far gate's --json reason and gate.err tails are all
    produced on ANOTHER machine and land in a local terminal or JSON body;
    chat._safe_reason is the one laundering boundary for exactly that shape
    (secret-redact, control-strip via _dsan, bounded). `absent` keeps this
    module's own honest wording for the empty case instead of _safe_reason's
    signing-specific default."""
    from . import chat
    text = str(text or "").strip()
    return chat._safe_reason(text) if text else absent


def _tree_carries_helm(repo, head):
    """Does the COMMITTED tree ship its own gate runner? (finding 1: the
    remote command is `python3 -m helm`, resolved from the shipped tree —
    a tree without the package fails minutes later, on the far box, with
    `No module named helm`. Refuse here, with the alternative named.)"""
    git = vcs.backend(repo)
    rc, _out, _err = git.text(repo, "cat-file", "-e",
                              "%s:helm/__main__.py" % head)
    return rc == 0


def _focus_uses_lane_runner(repo, head, trunk):
    """-> (bool, err): did this lane change focused execution's owners?

    Trunk supplies focused planning/minting for ordinary lanes, including old
    trees that predate ``--focus``. A lane editing this explicit owner family
    must execute its own code or it could break the selector and pass under the
    selector it replaced. The merge-base diff is the lane's change population;
    an unreadable population is UNKNOWN and refuses rather than defaulting to
    the more convenient trunk runner."""
    git = vcs.backend(repo)
    rc, base, err = git.text(repo, "merge-base", trunk, head)
    base = (base or "").strip().lower()
    if rc != 0 or not gate._SHA.fullmatch(base):
        return None, ("cannot derive the focused runner owner diff between "
                      "trunk %s and head %s: %s" % (
                          trunk[:12], head[:12],
                          _launder(err, "merge-base unreadable")))
    rc, out, err = git.text(repo, "diff", "--name-only", "-z", base, head)
    if rc != 0:
        return None, ("cannot read the focused runner owner diff at %s: %s"
                      % (head[:12], _launder(err, "git diff failed")))
    changed = frozenset(p for p in (out or "").split("\0") if p)
    return bool(changed.intersection(FOCUS_RUNNER_OWNING_PATHS)), None


def route(repo=None, box=None, label=None, timeout=None, transport=None,
          nodes=None, focus=False, sliced=False):
    """Run this repo's whole or focused gate ON `box` and import the receipt.

    Focus ships the pinned trunk object too and installs the same trunk ref in
    the remote scratch repository before planning; otherwise a clean lane bundle
    can hold HEAD without the merge-base object and the remote planner refuses.

    -> (result, err). result = {receipt, verdict, box, ssh_host, node,
    scratch, artifact, cgroup, eligibility, remote_rc}. Every err names its layer:
    consent, local tree, transport, remote substrate, remote gate, protocol
    framing, identity binding, or the
    import ladder — and every REMOTE string inside one is laundered through
    `_launder` before it can reach a terminal (the display-launder
    tripwire's transport-projection law)."""
    repo = os.path.realpath(repo or os.getcwd())
    if focus and sliced:
        return None, ("a routed gate is focused or sliced, never both — "
                      "--sliced runs helm's whole suite")
    row, err = pick_box(box, nodes=nodes, repo=repo)
    if err:
        return None, err
    head, tree, dirty, err = gate.tree_state(repo)
    if err:
        return None, err
    if dirty:
        return None, ("the worktree is dirty — job routing ships the "
                      "COMMITTED tree (HEAD %s), and a receipt about a tree "
                      "your worktree does not hold would authorise the wrong "
                      "land; commit first" % head[:12])
    if not _tree_carries_helm(repo, head):
        return None, ("the tree at HEAD %s carries no helm package "
                      "(helm/__main__.py) — job routing runs the SHIPPED "
                      "tree's own gate, so it can only gate trees that ship "
                      "helm; use `fab gate --repo` for arbitrary repos"
                      % head[:12])
    trunk_name = trunk_sha = None
    focus_runner = "lane"
    if focus:
        git = vcs.backend(repo)
        trunk_name = git.trunk_ref(repo)
        trunk_sha = (git.head_sha(repo, ref=trunk_name) or "").strip().lower()
        if not trunk_sha:
            return None, ("the focused route cannot resolve trunk (%s) — its "
                          "remote scope would have no reproducible base"
                          % trunk_name)
        lane_runner, err = _focus_uses_lane_runner(repo, head, trunk_sha)
        if err:
            return None, err
        focus_runner = "lane" if lane_runner else "trunk"
    challenge = uuid.uuid4().hex
    bundle_path, err = _bundle(repo, head, challenge, trunk=trunk_sha)
    if err:
        return None, err
    ceiling = (float(timeout) + _REMOTE_QUEUE_ALLOWANCE_S
               + _TRANSPORT_SLACK_S) if timeout is not None \
        else _DEFAULT_SESSION_CEILING_S
    transport = transport or SshTransport(row["ssh_host"])
    try:
        rc, out, errout, err = transport.run(
            _remote_script(head, tree, label, timeout, challenge, focus=focus,
                           trunk_name=trunk_name, trunk_sha=trunk_sha,
                           focus_runner=focus_runner,
                           session_ceiling=ceiling, sliced=sliced),
            bundle_path, ceiling + _REMOTE_TEARDOWN_GRACE_S)
    finally:
        _bundle_cleanup(bundle_path)
    if err:
        return None, "box %r: %s" % (box, err)
    if rc != 0:
        return None, ("ssh to box %r failed (exit %s): %s" % (
            box, rc, _launder(errout, "no stderr")))
    fields, sections, violation = _parse_markers(out, challenge)
    if violation:
        # The transcript is not a session this module can read as protocol —
        # a repeated control field or an unframed body means payload and
        # control are no longer separable, so NOTHING in it is evidence.
        return None, ("the transcript from box %r is not a framed session "
                      "(%s) — refusing to read protocol state out of bytes "
                      "the job under test could have written"
                      % (box, _launder(violation, "malformed")))
    if not fields and "::helm-job" in (out or ""):
        return None, ("the transcript from box %r carries marker lines but "
                      "NOT this invocation's challenge — a replayed or "
                      "stale transcript cannot mint" % box)
    remote_err = fields.get("error")
    if remote_err:
        code, _, detail = remote_err.partition(":")
        if code in ("head-mismatch", "tree-mismatch"):
            return None, ("box %r cloned the bundle to the WRONG %s (%s, "
                          "expected %s) — refusing before anything ran"
                          % (box, code.split("-")[0], _launder(detail, "?"),
                             head if code == "head-mismatch" else tree))
        if code == "clone-failed":
            return None, ("box %r could not clone the shipped bundle: %s"
                          % (box, _launder(detail, "no git diagnostic")))
        reason = _REMOTE_ERRORS.get(code)
        if reason and detail:
            reason += ": " + _launder(detail, "unknown detail")
        return None, "box %r cannot take this job: %s" % (
            box, reason or "remote error %r" % _launder(remote_err, "?"))
    if not fields.get("done"):
        return None, ("the remote session on box %r ended without its DONE "
                      "marker — the job did not complete: %s" % (
                          box, _launder(errout, "no stderr")))
    node = fields.get("node")
    if not node:
        return None, ("the remote session on box %r never reported its "
                      "node identity — refusing an unattributable receipt"
                      % box)
    if fields.get("runner") != focus_runner:
        return None, ("the remote session on box %r reported gate runner %r, "
                      "but this route selected %r — runner provenance changed "
                      "across the transport" % (
                          box, _launder(fields.get("runner"), "<absent>"),
                          focus_runner))
    try:
        verdict_json = json.loads(sections.get("gate-json") or "")
    except ValueError:
        return None, ("the remote gate on box %r did not answer: %s" % (
            box, _launder(sections.get("gate-err"), "no output")))
    if not verdict_json.get("minted"):
        refused = "the remote gate on box %r refused: %s" % (
            box, _launder(verdict_json.get("reason"), "no reason given"))
        # NOT RUN ON THAT BOX'S CAPACITY only when BOTH of the remote gate's
        # own answers say so, its --json and its exit status. A remote helm
        # that predates the code says neither and keeps the old exit 1.
        if verdict_json.get("not_run") == gate.CapacityRefusal.kind \
                and fields.get("gate-rc") == str(gate.EXIT_NOT_RUN_CAPACITY):
            return None, gate.CapacityRefusal(refused)
        return None, refused
    receipt = verdict_json.get("receipt") or {}
    receipt_id = str(receipt.get("id") or "")
    if not gate._ID.fullmatch(receipt_id):
        return None, ("the remote gate on box %r claims a mint but names an "
                      "invalid receipt id %r" % (
                          box, _launder(receipt.get("id"), "<absent>")))
    # CHANNEL-BOUND HOST IDENTITY (finding 5): the receipt must be about the
    # machine this session actually spoke to — its own uname over the same
    # ssh channel the tree shipped on — not about whatever the artifact says.
    host_block = receipt.get("host")
    receipt_node = host_block.get("node") \
        if isinstance(host_block, dict) else None
    if receipt_node != node:
        return None, ("the receipt names host %r but the session on box %r "
                      "reports node %r — a receipt about another machine "
                      "does not import" % (
                          _launder(receipt_node, "<absent>"), box,
                          _launder(node, "<absent>")))
    if focus:
        if receipt.get("v") != gateimport.FOCUSED_VERSION \
                or receipt.get("suite") is not False \
                or not isinstance(receipt.get("focus"), dict):
            return None, ("the remote focused command returned a non-focused "
                          "receipt — mode changed across the transport")
    elif receipt.get("v") == gateimport.FOCUSED_VERSION:
        return None, ("the remote whole-suite command returned a focused "
                      "receipt — mode changed across the transport")
    elif sliced != (receipt.get("v") == gate.SLICE_VERSION):
        # THE ASKED KIND IS THE KIND THAT IMPORTS. A --sliced route that came
        # back serial would pass off a slower answer as the one asked for,
        # and a serial route that came back sliced would import a receipt no
        # land door accepts where a serial one was wanted.
        return None, ("the remote %s command returned a %s receipt — mode "
                      "changed across the transport" % (
                          "sliced" if sliced else "serial whole-suite",
                          "sliced" if receipt.get("v") == gate.SLICE_VERSION
                          else "serial"))
    # THE CALLER TREE, READ AGAIN (finding 7): minutes passed. An exit code
    # is read as "what I am standing on passed"; if the room moved, refuse.
    head2, tree2, dirty2, err = gate.tree_state(repo)
    if err:
        return None, "the caller repo became unreadable during the run: %s" \
            % err
    if dirty2 or head2 != head or tree2 != tree:
        return None, ("the caller worktree MOVED during the remote run "
                      "(shipped %s, now %s%s) — the receipt is about a tree "
                      "you are no longer standing on; re-run from the state "
                      "you want authorised" % (
                          head[:12], head2[:12],
                          " DIRTY" if dirty2 else ""))
    artifact, err = _artifact_write(row.get("host") or box, receipt_id,
                                    sections.get("receipts") or "")
    if err:
        return None, err
    # ONE CUSTODY RECORD FOR BOTH KINDS: what this session read off its own
    # challenge-framed channel. A focused receipt needs it to import at all;
    # a whole suite needs it to be a receipt helm can prove it ran, which is
    # the only kind a land takes (task/3066, gateimport.land_provenance).
    custody = {"receipt": receipt, "head": head, "tree": tree,
               "node": node, "challenge": challenge}
    door = gateimport.import_routed_focus if focus \
        else gateimport.import_routed_suite
    imported, verdict, err = door(artifact, repo, custody, want_id=receipt_id)
    if err and imported is None:
        return None, "receipt fetched from box %r but REFUSED at import: %s" \
            % (box, _launder(err, "unknown import refusal"))
    warning = _launder(err, "unknown import warning") if err else None
    if verdict in ("duplicate", "audit-repaired"):
        # A FRESH challenge-bound run mints a NEW receipt (ts is inside the
        # content id). Byte-identity with a stored row means the box handed
        # back an OLD artifact — replay, not evidence (finding 6).
        return None, ("box %r returned a receipt already in the ledger "
                      "byte-for-byte (%s) — a fresh run cannot re-mint an "
                      "identical receipt; replayed artifact refused"
                      % (box, receipt_id))
    return {"receipt": imported, "verdict": verdict, "box": box,
            "import_warning": warning,
            "ssh_host": _launder(row.get("ssh_host"), "?"),
            "node": _launder(node, "?"),
            "scratch": _launder(fields.get("scratch"), "?"),
            "runner": focus_runner,
            "artifact": artifact,
            "cgroup": _launder(fields.get("cgroup"), "?"),
            "eligibility": _eligibility_note(row),
            "remote_rc": _launder(fields.get("gate-rc"), "?")}, None


def cmd_route(box, repo=None, label=None, timeout=None, as_json=False,
              focus=False, sliced=False):
    """The `helm gate run --box` arm: route, import, bind, and report."""
    repo = os.path.realpath(repo or os.getcwd())
    kwargs = {"repo": repo, "box": box, "label": label, "timeout": timeout}
    if focus:
        kwargs["focus"] = True
    if sliced:
        kwargs["sliced"] = True
    result, err = route(**kwargs)
    if err:
        return gate.refusal_exit(err, as_json, routed=box)
    receipt = result["receipt"]
    evidence = gate.evidence_line(receipt)
    bind_kw = {"repo_id": repo}
    if focus:
        bind_kw["need"] = gate.NEED_FOCUSED
    state, _rid, why = gate.bind(
        evidence, receipt.get("head") or "", **bind_kw)
    if as_json:
        print(json.dumps({"minted": True, "routed": box,
                          "ssh_host": result.get("ssh_host") or box,
                          "node": result["node"],
                          "scratch": result["scratch"],
                          "verdict": result["verdict"],
                          "artifact": result["artifact"],
                          "eligibility": result["eligibility"],
                          "import_warning": result.get("import_warning"),
                          "receipt": receipt, "evidence": evidence},
                         ensure_ascii=False, indent=1))
    else:
        print(evidence)
        if result["eligibility"]:
            print("helm gate: note — %s" % result["eligibility"],
                  file=sys.stderr)
        if result.get("import_warning"):
            print("helm gate: WARNING — %s" % result["import_warning"],
                  file=sys.stderr)
        print("helm gate: routed to %s via ssh_host %s (node %s, %s "
              "scratch), receipt %s %s"
              % (box, result.get("ssh_host") or box, result["node"],
                 result["scratch"] or "?", receipt.get("id"),
                 result["verdict"]), file=sys.stderr)
        if state != "VERIFIED":
            print("helm gate: %s — %s" % (state, why), file=sys.stderr)
    # Same polarity as the local arm: the exit IS bind, so a routed FAILED
    # (imported honestly, binding nothing) exits nonzero here too.
    return 0 if state == "VERIFIED" else 1
