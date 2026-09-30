"""THE LANDING TRAIN DRIVES ITSELF: `helm train auto` takes one train from
intent to land, one state per tick, and stops loudly wherever a land needs a
person.

WHAT IT REPLACES. Every land was a session running the same steps by hand:
list the READY cars, say which train will land, compose the room, run the
tree-wide audits on it, launch the whole-suite gate, read the receipt, check
the test count, push, fast-forward the shared checkout, reinstall the rail,
fold, close each car and each task, announce, and remove the room. Each step
waited on one seat's attention. This module is those steps as a state
machine a timer ticks every two minutes; each tick is idempotent, and a tick
that dies halfway resumes from the recorded state on the next one.

THE STATES, one JSON file per train under the project's `.state/autoland/`:

  IDLE       no file. When `helm train`'s own plan lists a car, the intent is
             recorded and posted to room `helm`, addressed to the integrator,
             naming the train, every car (lane, task, tip, row) and the veto
             line.
  INTENT     the veto window (HELM_AUTOLAND_VETO_S, 300 s). A veto recorded
             by `helm train veto <train> --reason R` ends it (VETOED). After
             the window the plan is asked again, and only the cars that are
             STILL READY at the SAME tip compose; a car that joined since
             waits for the next train, and none left ends the intent.
  COMPOSING  the room is minted and each car merged at its exact tip by
             `landwindow.mint_and_merge` (a conflicting car is dropped and
             posted, never resolved); the tree-wide audits run on the
             composed room; the gate launches through the landing-window door
             in the same step that proves trunk is still under the room.
             Audits that fail are blamed as a red gate is, by diff alone
             (`helm train blame --apply` reads their log, task/3674): the one
             car it names is ejected and the train composed again without it
             is tracked and gated; no car or two named, or no readable
             failure list, stops.
  GATING     the receipt for the room's head is read. GREEN must verify for
             a land (`foldcheck`'s tree-vs-gate rung) and its delta over
             trunk's own receipt must equal the test methods the diff adds
             less those it removes: the PLANNED delta when both receipts
             carry the slice runner's planned count (`planned_count`), else
             the Ran delta, and the words say which. RED is re-run alone on
             the red gate's own host first: a pass there is a flake
             (recorded, one re-gate), a fail goes to `helm train blame
             --apply`, whose composed b-room is tracked here. TRUNK-RED and
             UNKNOWN stop.
  LANDING    the push guard resolves the destination ONCE (the PRIVATE
             helm.trunkUrl and the helm.trunkRef branch, `PushTarget`) and
             foldcheck dry (tree-vs-gate and ff-able by rung) is asked of it,
             and a trunk that reads other than the one the train was
             composed and gated on stops the train; then THE LAST WORD under
             the store and dispatch-ledger locks: the train and switch read
             back, every car asked again for READY at its exact tip and door
             tier, the receipt's land authority asked again (the canary's
             DISABLE marker), and last the destination read again and
             required to be the one vetted (the writers of a land veto or
             authority that write under the same dispatch-ledger lock, and
             those that do not, are named in `landorder`); then a push
             FAST-FORWARD-ONLY AGAINST THE GATED TRUNK, its lease, pinned to
             exactly that destination (a car not ready, a moved destination
             or trunk, or a veto stops the train, never ejected). The push's
             lock holder inherits both locks and the tick's and starts git
             with none of them, in a process group of its own that a timeout
             kills whole; closed and never unlocked, the locks outlive a
             killed tick until git ends, and nothing git leaves running holds
             one; recorded `pushed` before either lock is let go, and a
             push git did not answer with success, or that a killed tick
             sent and never answered, read again under them (AN AMBIGUOUS
             PUSH, below); the shared checkout fast-forwarded only when it
             is clean, the rail reinstalled, the land's first ledger read
             timed by the landed checkout's own helm (`helm/postland.py`,
             never a stop), `helm lr foldcheck --apply` (all five rungs,
             failing closed),
             the LAND number and its line in the land log, each car closed
             (never its task: a land is not a re-read of the whole ask,
             task/3643), the announcement (the cars' plain words first), a console walk owed said once to the integrator, the
             rooms removed.
  DONE, VETOED, ABANDONED   terminal; the file moves to `done/`.
  STOPPED    a land that needs a person: posted once to the integrator, and
             no later train forms until `--resume` retries it from the step
             that stopped or `--abandon --reason R` ends it. An abandon
             refuses a train whose head is on origin, or whose origin cannot
             be read, and names the owed fold; `--force` ends it anyway and
             records the owed fold (`abandon`).

WHAT IS NEVER DONE HERE: a force push (the push's one lease flag,
`--force-with-lease=<trunk ref>:<gated trunk>`, is a compare-and-swap on a
head that descends from that trunk, so the remote takes only a fast-forward
of the trunk the train was gated on), a push to a remote that does not read
PRIVATE through the host-path guard's own visibility probe, a push to any URL
but the declared `helm.trunkUrl`, a push of a tree no verified whole-suite
receipt passed on, a stash, a conflict resolution, a trunk cure, a fleet op a
needs-restart car owes (it is posted to the integrator instead), and a
`helm gate canary clear`.

THE THREAT MODEL (task/3265 r4). Auto-land pushes only the exact approved,
gated, non-ejected tree to the reviewed trunk destination, and every veto and
land authority stays in force until the remote update is done. It defends
that against HONEST CONCURRENT ACTORS on this host: other seats, verbs and
timers that write vetoes, authority, config or refs while a land runs. It
does not defend against a hostile local process that controls auto-land's
own environment, its exec path or its helper binaries (such a process could
run `git push` itself), nor against a broken host whose own lock files
refuse a lock. `helm chat seat gc` is not a land-order writer (task/3265
r6): gc deletes only dead seats' rows, which is housekeeping and not a veto,
and the land acts on the snapshot its last read took. WHICH WRITERS TAKE
THE LAND ORDER, and which do not (proxywatch's watch state, git config and
refs, `burnflags.local_families`, gateimport's flip activation), is written
in `landorder`. A TICK KILLED MID-PUSH RELEASES NO LOCK: SIGKILL of the
tick, or `systemctl --user stop` or restart of its unit (KillMode=process
signals the tick alone), leaves the push's lock holder holding all three
until git ends, and git's alarm, armed by the holder, ends git
PUSH_TIMEOUT_S + PUSH_ORPHAN_SLACK_S after it starts. That is by design: a
unit stop lets an in-flight push finish while its holder keeps the locks,
and the alarm bounds how long (door read B2). AN AMBIGUOUS PUSH NEVER
RETRIES BY ITSELF (the codex read, B1 and B3). When git exits non-zero,
times out or is killed under a live tick, the update may have applied all
the same, so the tick, still holding all three locks (the holder has
exited), waits PUSH_SETTLE_S and reads trunk once through the same pinned,
vetted destination (`_Tick.settle`). Trunk at the head is recorded PUSHED
and the land goes on. Trunk at the lease STOPS the train for a person,
because an update that never applied and one that applied and was rewound
since read the same, and anything else STOPS it naming the reading. No tick
pushes a STOPPED train; `--resume` is a person's word. The locks go only
after that read. A TICK KILLED MID-PUSH LEAVES ITS PUSH AMBIGUOUS TOO, and
the same read settles it (the sibling of B1: the same ABA, reached through a
dead tick instead of a lost answer). The last word records `sending` on the
train under the three locks just before git runs, and whatever records the
push's outcome clears it. A tick that finds it (a predecessor sent the push
and never recorded what git answered) never pushes by itself: it takes the
same three locks, reads the destination again, and asks the same settle
read (`_Tick.unanswered`). Trunk at the head is recorded PUSHED, trunk at
the lease STOPS the train naming both readings, and anything else STOPS it.
A person's `--resume` after that stop pushes the head again, by that
person's order. The push still drops every ambient variable that names a
program git runs (`vcs._HELPER_SELECTION_ENV`), as hygiene. Two residuals
stand, known and not cured:
  * a writer whose land-order lock cannot be set up (EACCES, ENOLCK) writes
    its veto anyway, unserialized against the push (`landorder.locked`
    yields False); a writer waiting under an ambient projscope deadline that
    runs out gets `projscope.Expired` instead, and its veto is not written;
  * once the push's locks go (git answered, the timeout killed its group,
    git's alarm ended it, or the user manager stopped and ended every
    process in it), an update git already sent can still apply on the
    remote after a veto is written, and a helper that left the push's group
    (setsid) or that git's alarm did not end (an alarm ends git alone) may
    still be sending it. Such a helper holds no lock. The settle read under
    the locks, by the tick that saw git fail or by the next tick when that
    one was killed mid-push, narrows this and does not remove it: an update
    that applies after that read is not seen.

READINESS IS THE LEDGER'S. A car is exactly what `landwindow.plan` lists: a
live READY row with its approve, or a held source-clean row the one predicate
admits, at the held or reviewed tip, never a lane branch. One thing is asked
here that the plan does not ask: a source-clean car whose lane is a DOOR rides
only when `dispatches.approval_tier` admits its holder, and that admission
FAILS CLOSED (`Ops._door_admission`). Both are asked again in the last word
before the push, for the train's own cars at their exact tips.

ONE TRAIN IN FLIGHT. Auto-land composes nothing while a hand-composed train
room stands off trunk, and `helm train --apply` refuses while this module's
train is composing, gating, landing or STOPPED (`flight_refusal`): a stopped
train still holds its room, its cars and possibly a pushed head, so it
holds the flight until `--resume` or `--abandon`.

EVERY REFUSAL IS POSTED ONCE PER CAUSE and printed on every tick it stands.
Without --apply a tick prints what it would do and writes and posts nothing.
"""
import ast
import binascii
import collections
import contextlib
import getpass
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import eventledger, home, landwindow, pk, vcs
from .work import _lanes

PROG = "helm train auto"
VETO_PROG = "helm train veto"
USAGE = ("usage: helm train auto [--repo PATH] [--apply] [--status] "
         "[--pause [--reason TEXT] | --resume | --abandon [--force] "
         "--reason TEXT]\n"
         "       helm train auto seed <n> <sha> [--repo PATH]\n"
         "       helm train auto --install-timer\n"
         "       helm train veto <train> --reason TEXT [--repo PATH]")
VETO_USAGE = "usage: helm train veto <train> --reason TEXT [--repo PATH]"
SEED_USAGE = "usage: helm train auto seed <n> <sha> [--repo PATH]"

#: The room every post goes to, and the voice it speaks in.
ROOM = "helm"
WHO = "auto-land"

#: The veto window after the intent is posted, and how long a launched gate
#: may go without a receipt before the train stops and says so.
VETO_ENV = "HELM_AUTOLAND_VETO_S"
VETO_S = 300
GATE_WAIT_ENV = "HELM_AUTOLAND_GATE_S"
GATE_WAIT_S = 7200
#: How long the pre-gate audits and the push may run.
AUDIT_TIMEOUT_S = 3600
PUSH_TIMEOUT_S = 600
#: How long the post-land `helm lr foldcheck --apply` child may run
#: (`Ops.fold_apply`); a cold fold of the live ledger is about two minutes.
FOLD_APPLY_S = 1800
#: The checkout this helm runs from: `fold_apply` starts its `bin/helm`.
_HELM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: How much longer than PUSH_TIMEOUT_S the push's git may live once its tick
#: is gone: git's own alarm, armed by the lock holder before git execs,
#: ends git then, and the holder with it (`vcs.GitVcs.run_holding`).
PUSH_ORPHAN_SLACK_S = 60
#: AN AMBIGUOUS PUSH'S SETTLE (the codex read, B1 and B3): how long the
#: tick waits, still holding the push's three locks, after git exits
#: non-zero, times out or is killed, before it reads trunk once to learn
#: whether the update applied (`_Tick.settle`); and how long that one read
#: may run.
PUSH_SETTLE_S = 5
PUSH_SETTLE_READ_S = 60
#: A hand-composed train room older than this reads STALE: it is printed,
#: and it does not hold auto-land idle.
FLIGHT_STALE_S = 12 * 3600

#: The audits the integrator runs before every gate beside the tree-wide list
#: and each car's own test modules.
PRE_GATE_AUDITS = ("test_no_private_names", "test_delivery_truth",
                   "test_stop_seam", "test_hostpath_guard")

#: The confidence the announcement carries, beside what would falsify it.
CL = 90

#: How long a posted refusal that no longer stands is remembered, so one that
#: comes and goes inside it is not posted again.
FORGET_S = 6 * 3600

INTENT, COMPOSING, GATING, LANDING = "INTENT", "COMPOSING", "GATING", "LANDING"
DONE, VETOED, ABANDONED, STOPPED = "DONE", "VETOED", "ABANDONED", "STOPPED"
TERMINAL = (DONE, VETOED, ABANDONED)
#: The states in which a hand-composed `helm train --apply` refuses. A
#: STOPPED train is one of them: it holds its room and its cars (and, past
#: its push, a head trunk carries) until `--resume` or `--abandon`.
IN_FLIGHT = (COMPOSING, GATING, LANDING, STOPPED)

# The verdicts `trainblame` answers, and the colours a re-run reads.
EJECT, FLAKE = "EJECT", "FLAKE"
GREEN, RED, UNKNOWN, WAIT = "GREEN", "RED", "UNKNOWN", "WAIT"
#: What a red pre-gate audit run is recorded as where a red gate's id would
#: be (a blame in flight, an ejection): no gate ran (task/3674).
AUDITS = "audits"

STATE_SUBDIR = "autoland"
DONE_SUBDIR = "done"
CONTROL = "control.json"
COUNTER = "land-counter.json"
LAND_LOG = "land-log.jsonl"
#: How much of a task's title a merge subject and an announcement carry.
TITLE_CAP = 100
HISTORY_KEEP = 60

_TRAIN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
# a `.` or `..` path segment, or an encoded dot: git collapses it on the wire,
# while the privacy check reads the URL's literal segments (task/3265 door read)
_DOT_SEGMENT = re.compile(r"(?:^|[/:])\.\.?(?:/|$)|%2e", re.I)
_SHA_TOKEN = re.compile(r"\A[0-9a-f]{7,40}\Z")
_COUNT = re.compile(r"\A[0-9]{1,9}\Z")
# the one shape of trunk ref a fast-forward push may write: a branch
_TRUNK_REF = re.compile(
    r"\Arefs/heads/(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

#: THE DESTINATION OF ONE PUSH, resolved ONCE (task/3265 races F1): the vetted
#: URL, the declared trunk ref and the remote whose every URL was vetted.
#: `Ops.push_target` makes it in the read that vets it; foldcheck's ff-able
#: rung is asked of its ref and remote, the last word refuses one that reads
#: otherwise, and `Ops.push` writes exactly its ref at exactly its URL. Nothing
#: past the vetting reads helm.trunkRef again, so a rewrite of it mid-land
#: moves no push.
PushTarget = collections.namedtuple("PushTarget", "url ref remote")


def _dest(target):
    """A destination as a reader checks it: its ref at its URL."""
    if isinstance(target, PushTarget):
        return "%s at %s" % (target.ref, target.url)
    return str(target)


# ---------------------------------------------------------------- the store

def state_dir(root):
    """The project's auto-land store, beside the ejection and flake stores."""
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        ".state", STATE_SUBDIR)


def train_path(root, train):
    return os.path.join(state_dir(root), train + ".json")


def control_path(root):
    return os.path.join(state_dir(root), CONTROL)


def counter_path(root):
    """The LAND counter, {n, sha}: the last land's number and pushed head."""
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        ".state", COUNTER)


def land_log_path(root):
    """THE LAND LOG beside the counter: one line per numbered land. The
    counter keeps only the last land; the log keeps each, which is what
    `helm brief --report` numbers trunk's lands by."""
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        ".state", LAND_LOG)


def _read_json(path):
    """(obj, why). A missing file is (None, None); an unreadable one names
    why, and is never read as absent."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "%s is unreadable (%s)" % (path, exc)
    try:
        obj = json.loads(text)
    except ValueError as exc:
        return None, "%s is not JSON (%s)" % (path, exc)
    if not isinstance(obj, dict):
        return None, "%s holds no object" % path
    return obj, None


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pk.atomic_write(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


def _rewritten(root, be, env, url):
    """Why git would not push to `url` as written, or None.

    A push to a LITERAL URL is still rewritten by config that `git remote
    get-url` never shows: a url.<x>.pushInsteadOf or url.<x>.insteadOf whose
    value is a prefix of the URL sends the push to x, and a remote section
    NAMED by the URL (remote.<url>.pushurl) adds or replaces where it goes.
    Measured on git 2.53 (task/3265 door read): both get-url reads printed the
    trunk while the push landed on another repository, once on two. Any such
    key, in any config scope, refuses; an unreadable config refuses too."""
    rc, out, err = be.text(root, "config", "--get-regexp",
                           r"^(url\..*\.(push)?insteadof|remote\..*)$",
                           env=env)
    if rc not in (0, 1):
        return ("git config could not be read (%s), so whether a rewrite "
                "redirects the push is UNKNOWN" % (err or "rc %d" % rc).strip())
    hits = []
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key.startswith("url.") and value and url.startswith(value):
            hits.append("%s %s" % (key, value))
        elif key.startswith("remote.%s." % url):
            hits.append("%s %s" % (key, value))
    if hits:
        return ("git config rewrites the trunk URL %s at push time (%s): a "
                "push to it would not go only there" % (url, "; ".join(hits)))
    return None


#: The prefix of the one-time name a push is given for its URL (`_pinned`).
PUSH_ALIAS = "helm-autoland-push/"


def _pinned(env, url):
    """(name, env): what `git push` is told to push to, and the environment
    that makes git resolve that name to exactly `url`, whatever its config
    says, config written after every check included (task/3265 races R1).

    git picks a push URL's rewrite by the LONGEST url.<base>.insteadOf or
    pushInsteadOf value that is a prefix of it, in one pass, and reads a
    remote section NAMED by it. The name is fresh (128 random bits) and holds
    a '/', so no config names a remote by it and no config value can be a
    longer prefix of it than the whole name, which this environment maps to
    `url` for both kinds of rewrite (GIT_CONFIG_COUNT pairs;
    `vcs._authority_env` has removed the ambient ones). Measured on git 2.53
    in temp repositories: a pushInsteadOf or insteadOf rule on the URL, a
    remote section named by a file:// URL, and rules on prefixes of the name
    itself each redirected a push to the literal URL, and none redirected a
    push to the name.

    NO REF BUT THE TRUNK (task/3265 door read, extra refs): push.followTags
    and push.recurseSubmodules config would make the push write tags or a
    submodule's refs beside trunk, so both are pinned off here too."""
    name = PUSH_ALIAS + binascii.hexlify(os.urandom(16)).decode("ascii")
    return name, dict(env, GIT_CONFIG_COUNT="4",
                      GIT_CONFIG_KEY_0="url.%s.insteadOf" % url,
                      GIT_CONFIG_VALUE_0=name,
                      GIT_CONFIG_KEY_1="url.%s.pushInsteadOf" % url,
                      GIT_CONFIG_VALUE_1=name,
                      GIT_CONFIG_KEY_2="push.followTags",
                      GIT_CONFIG_VALUE_2="false",
                      GIT_CONFIG_KEY_3="push.recurseSubmodules",
                      GIT_CONFIG_VALUE_3="no")


class _Superseded(Exception):
    """The train's file was ended or changed by a verb (`--abandon`,
    `--resume`) after this tick read it: the tick stops acting and writes
    nothing over it."""


class CloseFailed(Exception):
    """The post-land close did not ANSWER: its child crashed, timed out,
    could not start, or exited nonzero with no refusal of its own. It is
    what an exception out of an in-process `landreq.close` was: the car is
    not closed, and the next tick asks again. A refusal the close returned
    is its answer, and never this."""


def _bump(st):
    """A verb's write: the train's revision moves, so a tick that read the
    file before it sees that it no longer holds the train (`_Tick._check`)."""
    st["rev"] = int(st.get("rev") or 0) + 1


@contextlib.contextmanager
def _flock(path, wait):
    """Yield True under an exclusive lock on `path`, False when `wait`
    seconds pass without it."""
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


def _lock_path(root, name):
    """A lock beside the store, never inside it: the store's own listing is
    the set of trains."""
    return os.path.join(os.path.dirname(state_dir(root)),
                        "%s-%s.lock" % (STATE_SUBDIR, name))


def _state_lock(root):
    return _flock(_lock_path(root, "state"), 30)


def _lock_fds(paths):
    """(fds, why): every descriptor this process has open on each lock
    file in `paths`, for the push's lock holder to inherit (task/3265 races
    F4, `vcs.GitVcs.run_holding`). `why` names a lock no descriptor of this
    process has open, or the listing that failed, and `fds` is None beside
    it.

    A flock belongs to the open file description, so a child that inherits
    the descriptor holds the lock with it: SIGKILL of this process closes
    only this process's copy, and the lock lives until the holder ends, when
    its git ends. git itself is started with them closed (door read B3)."""
    want = {}
    for path in paths:
        try:
            got = os.stat(path)
        except OSError as exc:
            return None, "the lock %s cannot be read (%s)" % (path, exc)
        want[(got.st_dev, got.st_ino)] = path
    names = None
    for where in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(where)
            break
        except OSError:
            continue
    if names is None:
        return None, "this process's descriptors cannot be listed"
    found = {}
    for name in names:
        try:
            fd = int(name)
            got = os.fstat(fd)
        except (ValueError, OSError):
            continue
        path = want.get((got.st_dev, got.st_ino))
        if path:
            found.setdefault(path, set()).add(fd)
    missing = [p for p in paths if p not in found]
    if missing:
        return None, ("no descriptor of this process has %s open"
                      % ", ".join(missing))
    return sorted(set().union(*found.values())), None


def compose_lock(root, wait):
    """THE SINGLE-FLIGHT LOCK a hand-composed `helm train --apply` and this
    module share: held by the hand compose from its flight check through its
    gate launch, and by auto-land from its last flight check through its
    move to COMPOSING. Neither composes while the other is mid-compose, and
    after that each sees the other's train (`flight_refusal`, `in_flight`).
    Yields whether it was taken within `wait` seconds."""
    return _flock(os.path.join(os.path.dirname(state_dir(root)),
                               "train-compose.lock"), wait)


def read_train(root, train):
    """The train's state while it is active, or None."""
    if not _TRAIN.match(str(train or "")):
        return None
    got, _why = _read_json(train_path(root, train))
    return got


def active(root):
    """(state, why) — the one train not yet archived, or (None, None).

    Two active files is UNKNOWN, never a pick: one train at a time is the
    rule every other answer here stands on."""
    try:
        names = sorted(n for n in os.listdir(state_dir(root))
                       if n.endswith(".json") and n != CONTROL)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "the auto-land store is unreadable (%s)" % exc
    if len(names) > 1:
        return None, ("the auto-land store holds %d active trains (%s); one "
                      "is the rule" % (len(names), ", ".join(names)))
    if not names:
        return None, None
    return _read_json(os.path.join(state_dir(root), names[0]))


def archived(root):
    """Every archived train, oldest first."""
    where = os.path.join(state_dir(root), DONE_SUBDIR)
    try:
        names = sorted(os.listdir(where))
    except OSError:
        return []
    rows = []
    for name in names:
        got, _why = _read_json(os.path.join(where, name))
        if got:
            rows.append(got)
    rows.sort(key=lambda r: (r.get("archived_ts") or 0, r.get("train") or ""))
    return rows


def _archive(root, st, now):
    """Move a terminal train into `done/`: the archive is written first, so
    a crash between the two leaves the active file, which the next tick
    archives again."""
    st["archived_ts"] = now
    nonce = binascii.hexlify(os.urandom(3)).decode("ascii")
    _write_json(os.path.join(state_dir(root), DONE_SUBDIR, "%s-%d-%s.json"
                             % (st["train"], int(now), nonce)), st)
    try:
        os.remove(train_path(root, st["train"]))
    except FileNotFoundError:
        pass


def read_control(root):
    """(control, why): {paused, posted}. Absent is the default switch."""
    got, why = _read_json(control_path(root))
    if why:
        return None, why
    got = got or {}
    got.setdefault("paused", None)
    got.setdefault("posted", {})
    return got, None


def pause(root, by, reason=None):
    """The integrator's switch: no tick acts until `resume`."""
    # UNDER THE STORE'S LOCK, the one a tick's `finish` rewrites the switch
    # under: a tick that read the switch before this write never writes
    # over it.
    with _state_lock(root) as held:
        if not held:
            return None, "the auto-land store is locked"
        control, why = read_control(root)
        if why:
            return None, why
        control["paused"] = {"by": by, "reason": reason, "ts": time.time()}
        _write_json(control_path(root), control)
    return control, None


def resume(root, by, now=None):
    """Clear the pause; a STOPPED train is put back at the step that
    stopped, so the next tick retries it. -> (message, why)."""
    said = []
    with _state_lock(root) as held:
        if not held:
            return None, "the auto-land store is locked"
        control, why = read_control(root)
        if why:
            return None, why
        if control.get("paused"):
            control["paused"] = None
            _write_json(control_path(root), control)
            said.append("the switch is ON again")
        st, why = active(root)
        if why:
            return None, why
        if st and st.get("state") == STOPPED:
            stop = st.get("stopped") or {}
            st["state"] = stop.get("state") or INTENT
            st["step"] = stop.get("step")
            _history(st, now or time.time(), "resumed by %s after: %s"
                     % (by, stop.get("why")))
            st["stopped"] = None
            _bump(st)
            _write_json(train_path(root, st["train"]), st)
            said.append("%s retries from %s%s" % (
                st["train"], st["state"],
                "/" + st["step"] if st.get("step") else ""))
    return "; ".join(said) or "nothing was paused or stopped", None


def head_on_origin(root, st, ops):
    """(pushed, why) for the train's head: True when the declared trunk
    carries it, False when it reads and does not, None (with why) when that
    cannot be read. A train with no head (it composed nothing yet) is False:
    nothing of it can be on origin."""
    head = st.get("head")
    if not head:
        return False, None
    remote = ops.remote_head(root)
    if not remote:
        return None, "the trunk remote cannot be read"
    if remote == head:
        return True, None
    state = ops.ancestry(root, head, remote)
    if state == vcs.ANCESTOR:
        return True, None
    if state == vcs.NOT_ANCESTOR:
        return False, None
    return None, ("whether trunk %s carries %s cannot be read (%s)"
                  % (_short(remote), _short(head), state))


def owed_fold(st):
    """The acts a train whose head is (or may be) on origin still owes, as
    the lines a person runs: the fold. No task close is owed: a land closes
    no task (see `_Tick.closes`)."""
    gid = (st.get("receipt") or {}).get("id") or "<the green gate's id>"
    return ["helm lr foldcheck %s --gate gate:%s --apply"
            % (st.get("head"), gid)]


def abandon(root, by, reason, ops=None, now=None, force=False):
    """End the active train, remove its rooms and archive it ABANDONED.
    -> (state, why).

    A TRAIN WHOSE HEAD IS ON ORIGIN IS NEVER ABANDONED QUIETLY (the
    integrator's ruling R3): its fold, its LAND number and its closes would
    never run. So the abandon refuses when the declared trunk carries the
    head, or when that cannot be read, and names the owed `helm lr foldcheck
    <head> --gate gate:<id> --apply`; `force` ends it anyway
    and records the owed lines in the archive and in a post to the
    integrator. A train LANDING past its push decision is also refused while
    a tick runs (the tick lock is held from here to the archive, so none
    starts), force or not: that tick may be folding it."""
    ops = ops or Ops()
    now = now if now is not None else ops.now()
    with _state_lock(root) as held, contextlib.ExitStack() as stack:
        if not held:
            return None, "the auto-land store is locked"
        st, why = active(root)
        if why:
            return None, why
        if not st:
            return None, "no auto-land train is active"
        name = st.get("name") or st.get("train")
        if st.get("state") == LANDING and st.get("step") not in (
                None, "verified"):
            if not stack.enter_context(_flock(_lock_path(root, "tick"), 0)):
                return None, ("%s is LANDING at %s and a tick is landing it "
                              "now; nothing is abandoned" % (name,
                                                             st.get("step")))
        pushed, unread = head_on_origin(root, st, ops)
        owed = owed_fold(st) if pushed is not False else None
        if owed and not force:
            return None, (
                "%s's head %s %s, so it is never abandoned quietly: its fold, "
                "LAND number and closes are owed. `%s --resume` lets the next "
                "tick finish it; by hand it is `%s`. `%s --abandon --force "
                "--reason R` ends it anyway and records what is owed" % (
                    name, _short(st.get("head")),
                    "is pushed (on the declared trunk)" if pushed
                    else "may be pushed: %s" % unread, PROG,
                    "`, then `".join(owed), PROG))
        left = _remove_rooms(root, st, ops)
        st["state"] = ABANDONED
        st["abandoned"] = {"by": by, "reason": reason, "ts": now,
                           "rooms_left": left}
        if owed:
            st["abandoned"].update(forced=True, owed=owed,
                                   pushed="yes" if pushed else unread)
        _history(st, now, "abandoned by %s: %s%s" % (
            by, reason, "; FORCED, owed: %s" % "; ".join(owed)
            if owed else ""))
        _archive(root, st, now)
    if owed:
        text = ops.address(
            "auto-land %s ABANDONED with --force by %s (%s), and its head %s "
            "%s. OWED, by hand: `%s`." % (
                name, by, reason, _short(st.get("head")),
                "is on the declared trunk" if pushed
                else "may be on it (%s)" % unread, "`, then `".join(owed)))
        _row, err = ops.post(text)
        if err:
            st["abandoned"]["post_failed"] = err
    return st, None


def veto(root, train, by, reason, now=None):
    """Record a veto on a train still inside its window. -> (state, why)."""
    if not _TRAIN.match(str(train or "")):
        return None, "%r is not a train name" % train
    with _state_lock(root) as held:
        if not held:
            return None, "the auto-land store is locked"
        st, why = _read_json(train_path(root, train))
        if why:
            return None, why
        if st is None:
            return None, ("no auto-land train %s is active: it was never "
                          "posted, or it already ended (`%s --status`)"
                          % (train, PROG))
        if st.get("state") != INTENT:
            return None, ("%s is past INTENT (%s): a veto stops a train only "
                          "inside its window; `%s --abandon --reason R` ends "
                          "one in flight" % (train, st.get("state"), PROG))
        if not st.get("veto"):
            st["veto"] = {"by": by, "reason": reason,
                          "ts": now if now is not None else time.time()}
            _bump(st)
            _write_json(train_path(root, train), st)
    return st, None


def flight_refusal(root):
    """Why a hand-composed `helm train --apply` must not compose now, or
    None: auto-land's own train is composing, gating, landing or STOPPED."""
    st, why = active(root)
    if why:
        # no verb reads past a damaged store (--resume and --abandon read it
        # too), so the refusal names what to repair (task/3265 door read)
        return ("auto-land's store cannot be read (%s), so whether its train "
                "is in flight is UNKNOWN. No auto-land verb reads past it: "
                "repair or move the damaged file out of %s, then retry"
                % (why, state_dir(root)))
    if not st or st.get("state") not in IN_FLIGHT:
        return None
    name = st.get("name") or st.get("train")
    if st.get("state") == STOPPED:
        stop = st.get("stopped") or {}
        return ("auto-land's %s is STOPPED at %s%s (room %s): %s. A stopped "
                "train still holds the flight, and one train flies at a "
                "time. `%s --resume --repo %s` retries it once the cause is "
                "cured, and `%s --abandon --repo %s --reason R` ends it" % (
                    name, stop.get("state"),
                    "/" + stop["step"] if stop.get("step") else "",
                    st.get("room") or "not minted", stop.get("why"), PROG,
                    root, PROG, root))
    # each verb carries --repo: `helm train --apply --repo X` run elsewhere
    # must not be told a verb that acts on the directory it stands in
    return ("auto-land's %s is in flight (%s%s, room %s); one train flies at "
            "a time. `%s --status --repo %s` follows it, and `%s --abandon "
            "--repo %s --reason R` ends it" % (
                name, st.get("state"),
                "/" + st["step"] if st.get("step") else "",
                st.get("room") or "not minted", PROG, root, PROG, root))


def _history(st, now, note):
    rows = st.setdefault("history", [])
    rows.append({"ts": now, "state": st.get("state"), "step": st.get("step"),
                 "note": note})
    del rows[:-HISTORY_KEEP]


# ------------------------------------------------------------- the counter

def read_counter(root):
    got, _why = _read_json(counter_path(root))
    return got


def record_land(root, n, sha, by=None, train=None, gate=None, ran=None,
                seeded=False, now=None):
    """Append LAND `n` = `sha` to the land log -> bool (False: the log would
    not take the line). The last line for a number is the one read, so a
    retry or a second seed of one number corrects it and never counts a
    second land."""
    return eventledger.append(land_log_path(root), {
        "id": "land-%d" % int(n), "n": int(n), "sha": sha, "by": by or WHO,
        "train": train, "gate": gate, "ran": ran, "seeded": bool(seeded),
        "ts": now if now is not None else time.time()})


def land_log(root):
    """({n: line}, why): every recorded land, the last line per number. The
    counter's own {n, sha} stands in for its number when no line names it
    (a counter seeded before the log existed). `why` names a log or a
    counter that does not read; the lands beside it are what did."""
    rows, why = eventledger.latest_checked(land_log_path(root))
    lands = {}
    for row in rows.values():
        n = row.get("n")
        if isinstance(n, int) and _SHA_TOKEN.match(str(row.get("sha") or "")):
            lands[n] = row
    got, counter_why = _read_json(counter_path(root))
    if got and isinstance(got.get("n"), int) and got["n"] not in lands \
            and _SHA_TOKEN.match(str(got.get("sha") or "")):
        lands[got["n"]] = dict(got, counter=True)
    return lands, why or counter_why


def seed_counter(root, n, sha, by=None, now=None):
    """Write the LAND counter as {n, sha}. The verb checks the sha first."""
    row = {"n": int(n), "sha": sha, "by": by or WHO,
           "ts": now if now is not None else time.time(), "seeded": True}
    _write_json(counter_path(root), row)
    return row


def take_land_number(root, head, trunk_sha, ops, train=None, now=None):
    """(n, why): the LAND number for pushed `head`, written once.

    Taken only after the fold proved `origin-has-it`. The counter's own sha
    must be an ancestor of trunk as observed (`trunk_sha`), or the counter
    is refused: a number whose last land is not on trunk counts nothing. A
    retry for the head already counted answers the same number."""
    got, why = _read_json(counter_path(root))
    if why:
        return None, why
    if not got or not isinstance(got.get("n"), int) \
            or not _SHA_TOKEN.match(str(got.get("sha") or "")):
        return None, ("no LAND counter is seeded here: `%s seed <n> <sha>` "
                      "records the last land" % PROG)
    if head.startswith(got["sha"]):
        return got["n"], None
    state = ops.ancestry(root, got["sha"], trunk_sha)
    if state != vcs.ANCESTOR:
        return None, ("the LAND counter's sha %s is not an ancestor of trunk "
                      "%s (%s), so its number %d counts a history trunk does "
                      "not carry; re-seed it" % (got["sha"][:12],
                                                 trunk_sha[:12], state,
                                                 got["n"]))
    n = got["n"] + 1
    _write_json(counter_path(root), {"n": n, "sha": head, "by": WHO,
                                     "train": train, "ts": now or time.time()})
    return n, None


# ------------------------------------------------- restart and test counts

# A changed line that writes a systemd unit or calls the timer installer.
_UNIT_LINE = re.compile(
    r"\[(?:Unit|Service|Timer|Install)\]|\b(?:ExecStart|OnCalendar|"
    r"OnUnitActiveSec|OnBootSec|WantedBy|EnvironmentFile|WorkingDirectory)="
    r"|\b(?:install_user_timer|ensure_timer|user_unit_dir)\b")

#: WHAT A LANDED CAR OWES THE RUNNING FLEET, by the paths it touches. A rule
#: matches a path (exact, prefix or a `hooks` directory) and, when it names a
#: `context` or `changed` pattern, the car's diff of that path too. Every
#: other car is live at land. `helm rearm` reports stale processes and
#: classifies no path, so the table is here.
RESTART_RULES = (
    {"what": "the hooks: re-sync them",
     "paths": ("helm/hooks.py", "bin/helm-hook"), "dir": "hooks"},
    {"what": "the seat_catalog sidecar pin: fast-forward the vendored bridge "
             "and restart it while its seat is idle",
     "paths": ("helm/seat_catalog.py",), "context": re.compile(r"sidecar")},
    {"what": "a systemd unit or timer installer: reinstall the unit",
     "prefix": ("helm/",), "changed": _UNIT_LINE},
    {"what": "the web board: restart the web service",
     "prefix": ("helm/web",)},
    {"what": "the chat node: restart it", "paths": ("helm/chatnode.py",)},
    {"what": "proxywatch: restart it", "paths": ("helm/proxywatch.py",)},
)


def _changed_lines(diff):
    return "\n".join(line[1:] for line in (diff or "").splitlines()
                     if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def _rule_matches(rule, path, diff):
    hit = path in rule.get("paths", ()) \
        or any(path.startswith(p) for p in rule.get("prefix", ())) \
        or (rule.get("dir") and "/%s/" % rule["dir"] in "/" + path)
    if not hit:
        return False
    if rule.get("context") and not rule["context"].search(diff or ""):
        return False
    if rule.get("changed") and not rule["changed"].search(
            _changed_lines(diff)):
        return False
    return True


def needs_restart(changes):
    """The restarts a car owes, in RESTART_RULES order, from `changes`
    {path: that path's diff}; [] means live at land. Tests owe none."""
    owed = []
    for rule in RESTART_RULES:
        for path, diff in sorted((changes or {}).items()):
            if path.startswith("tests/"):
                continue
            if _rule_matches(rule, path, diff):
                owed.append(rule["what"])
                break
    return owed


def split_diff(text):
    """{path: its unified diff} from one `git diff`, keyed by the new path."""
    found, path, chunk = {}, None, []
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            if path is not None:
                found[path] = "\n".join(chunk) + "\n"
            parts = line.split(" b/", 1)
            path, chunk = (parts[1] if len(parts) == 2 else line), []
            continue
        chunk.append(line)
    if path is not None:
        found[path] = "\n".join(chunk) + "\n"
    return found


def count_test_methods(text):
    """How many test methods `text` defines — a `test*` def directly in a
    class body, at any depth — or None when it does not parse. The same
    count the integrator took by hand over tests/."""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return None
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
               for item in node.body
               if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
               and item.name.startswith("test"))


def planned_count(row):
    """(n, None): the tests a whole-suite receipt's own runner PLANNED, or
    (None, why) when the receipt carries no count this reads.

    Ran is what ran on the gate's host. A class or module whose setUp skips
    there (setUpClass raising SkipTest) records one skip and runs none of its
    tests, so a test deleted inside it moves the diff and not Ran. The
    planned count is discovery's whole inventory, the same on every host.

    Only the sliced kind carries one: `slice_authority["planned"]`, which
    that kind's content id binds (`gate._receipt_id`). A count stored on any
    other kind is not bound, so it is not read. On the sliced kind the count
    reads only as an int (never a bool) that is positive and no less than
    the receipt's own Ran; anything else is UNKNOWN, never zero and never a
    match."""
    from . import gate
    gid = row.get("id")
    if row.get("v") != gate.SLICE_VERSION:
        return None, "gate:%s carries no planned count (it is not the " \
            "sliced kind)" % gid
    auth = row.get("slice_authority")
    n = auth.get("planned") if isinstance(auth, dict) else None
    if type(n) is not int or n <= 0:
        return None, "gate:%s's planned count %r is UNKNOWN (not a " \
            "positive integer)" % (gid, n)
    ran = row.get("ran")
    if type(ran) is not int:
        return None, "gate:%s's planned count %d is UNKNOWN (its own Ran %r " \
            "does not read)" % (gid, n, ran)
    if n < ran:
        return None, "gate:%s's planned count %d is UNKNOWN (below its own " \
            "Ran %d)" % (gid, n, ran)
    return n, None


# ------------------------------------------------------------ the fold proof

#: A line of `helm lr foldcheck` that stops a proof wherever it stands: a
#: rung that refused (STOP) or could not be measured (????), in the five or
#: in the composition proof, and the two verdict lines.
_FOLD_BAD = re.compile(r"^(?:STOP|\?\?\?\?)\s|^(?:REFUSED by|NOT PROVEN)\b",
                       re.M)


def fold_unproven(text):
    """Why `helm lr foldcheck --apply`'s output does NOT prove the fold, or
    None when it does. FAIL CLOSED: the proof is only the five rungs passed
    (`all five PASSED`, with `ok origin-has-it`) and, when a composition
    proof is printed, `composition proof PASSED`; a composition authority
    that reads UNKNOWN, or any rung marked `????` or STOP, anywhere in the
    output, is not proven, whatever other line says PASSED."""
    text = text or ""
    bad = [ln.strip() for ln in text.splitlines() if _FOLD_BAD.match(ln)]
    if "composition authority is UNKNOWN" in text:
        bad.insert(0, "the composition authority is UNKNOWN")
    if bad:
        return "; ".join(bad)
    if not re.search(r"^all five PASSED\b", text, re.M):
        return "no `all five PASSED` line: %s" % (_first(text) or "no output")
    if not re.search(r"^ok\s+origin-has-it\b", text, re.M):
        return "no `ok origin-has-it` line"
    if "composition proof" in text \
            and not re.search(r"^composition proof PASSED\b", text, re.M):
        return "a composition proof is printed and did not read PASSED"
    return None


# ---------------------------------------------------------------- the seams

def _first(text):
    return landwindow._first(text)


#: A quoted answer longer than head + tail lines keeps both ends: the head
#: says what ran, the tail is where a tool puts its cause.
_WHOLE_HEAD, _WHOLE_TAIL, _WHOLE_LINE = 3, 20, 400


def _whole(text):
    """EVERY LINE a door or tool answered, blank lines dropped: what a stop,
    a post or a refusal quotes when the answer runs to more than one line.

    MEASURED (task/3463 item 14): a gate launch that reached Fab prints where
    it routed the gate FIRST and why Fab would not take it AFTER, so a stop
    that kept `_first` kept "ROUTED to <host>" and threw the cause away,
    twice, on auto-land's first own train. The same shape holds for git's
    push answer (`To <url>` before the rejection), foldcheck (its passing
    rungs before the failing one), the rail (its checks before its refusal)
    and a crashed blame (`Traceback` before the exception).

    BOUNDED, because a stop and a wait post it to the room: a long traceback
    or push answer would land there whole. Past `_WHOLE_HEAD + _WHOLE_TAIL`
    lines it keeps the head and the tail, where the cause is, and says how
    many lines it cut; a line past `_WHOLE_LINE` characters is cut too."""
    lines = [ln.rstrip()[:_WHOLE_LINE] for ln in (text or "").splitlines()
             if ln.strip()]
    if len(lines) > _WHOLE_HEAD + _WHOLE_TAIL:
        cut = len(lines) - _WHOLE_HEAD - _WHOLE_TAIL
        lines = lines[:_WHOLE_HEAD] + ["... %d lines cut ..." % cut] \
            + lines[-_WHOLE_TAIL:]
    return "\n".join(lines) or "no output"


def _short(sha):
    return (sha or "?")[:12]


class Ops(object):
    """Every seam the machine touches. Each default is the real thing; an
    arm subclasses and replaces the ones a test must never really run."""

    # -- time, plan, flight, chat -----------------------------------------
    def now(self):
        return time.time()

    def identity(self, root):
        from . import rowworld
        return rowworld.repo_identity(root)

    def plan(self, root):
        return landwindow.plan(root)

    def in_flight(self, root, mine):
        """[(room, subject)] — every detached worktree of this repository
        other than `mine` whose head is a `<train>: merge lane` merge trunk
        does not carry: a train composed by hand and not yet landed. A room
        whose head is older than FLIGHT_STALE_S is left out as debris."""
        from . import trainblame
        be, env = vcs.backend(root), landwindow._env()
        rc, out, _err = be.text(root, "worktree", "list", "--porcelain",
                                env=env)
        if rc != 0:
            return [("?", "the worktree list is unreadable, so a train "
                          "composed by hand cannot be ruled out")]
        top = os.path.realpath(root)
        skip = {os.path.realpath(p) for p in mine or ()}
        trunk = be.trunk_ref(root)
        now, found = self.now(), []
        for block in out.split("\n\n"):
            lines = block.splitlines()
            path = next((ln[9:] for ln in lines
                         if ln.startswith("worktree ")), None)
            head = next((ln[5:] for ln in lines if ln.startswith("HEAD ")),
                        None)
            if not path or not head or "detached" not in lines:
                continue
            real = os.path.realpath(path)
            if real == top or real in skip or not os.path.isdir(path):
                continue
            rc, said, _err = be.text(root, "log", "-1", "--format=%ct%x1f%s",
                                     head, env=env)
            stamp, _sep, subject = said.partition("\x1f")
            if rc != 0 or not trainblame._MERGE.match(subject):
                continue
            if stamp.isdigit() and now - int(stamp) > FLIGHT_STALE_S:
                continue
            if be.ancestry(root, head, trunk) == vcs.ANCESTOR:
                continue
            found.append((path, subject))
        return found

    def post(self, text):
        from . import chat
        try:
            row = chat.post(text, room=ROOM, who=WHO)
        except Exception as exc:            # noqa: BLE001 — named, retried
            return None, "%s: %s" % (type(exc).__name__, exc)
        return (row, None) if row else (None, "it wrote nothing")

    def address(self, text):
        from . import seats_integrator
        return seats_integrator.integrator_addressed(text)

    def console_walk(self, root, post, say=None):
        """Post the console walk owed through `post`, once per owed state."""
        from . import console_walk
        return console_walk.surface(root, post, now=self.now(), say=say)

    def car_facts(self, root, car):
        """The car's task, priority, doors, author and reader, and whether
        its reader may carry it: {task, priority, doors, author, reader,
        read_at, model, family, admit: (ok, why)}. Every read that fails
        leaves its field None; only the admission refuses on one.

        `read_at` is the moment the reader's read was RECORDED: the hold's
        stamp for a source-clean car (task/3508). The holder's model is read
        AT it, never its newest turn: a native seat that held on Sonnet and
        switched to Opus afterwards is not an Opus holder. An approve car's
        model is the one its verdict froze, a label here; the lr row carries
        no verdict stamp, so a native reviewer's stays unread."""
        lr = car.get("lr") or {}
        clean = car.get("basis") == "source-clean"
        facts = {"task": None, "task_unknown": None, "title": None,
                 "priority": None, "doors": None,
                 "author": lr.get("author"),
                 "reader": lr.get("hold_actor") if clean
                 else lr.get("reviewer"),
                 "read_at": lr.get("hold_ts") if clean else None,
                 "model": None if clean else lr.get("reviewer_model"),
                 "family": None, "admit": (True, None)}
        from . import dispatches, review_door, reviewer_eligibility, tasks
        from . import trainblame
        try:
            facts["task"], facts["task_unknown"], refused = \
                trainblame.lane_task(car["id"])
            # A READ THAT WAS REFUSED IS UNKNOWN, NEVER "no task" (task/3643):
            # the merge line is the fact the sweep links by
            facts["task_unknown"] = facts["task_unknown"] or refused
            row = tasks.get(facts["task"]) if facts["task"] else None
            facts["priority"] = (row or {}).get("priority")
            facts["title"] = _title((row or {}).get("title"))
        except Exception as exc:            # noqa: BLE001 — said, not raised
            if not facts["task"]:
                facts["task_unknown"] = ("the task could not be read (%s)"
                                         % type(exc).__name__)
        try:
            current, unavailable = dispatches.snapshot()
            row = None if unavailable else (current or {}).get(car["id"])
            if row is not None:
                facts["doors"] = sorted({cls for cls, _ev in
                                         review_door.lane_doors(
                                             row, current)["doors"]})
        except Exception:                   # noqa: BLE001 — UNKNOWN below
            facts["doors"] = None
        if facts["reader"] and not facts["model"]:
            # "" when no moment is recorded: a RECORDED read, unplaced, which
            # a native seat answers None for (`dispatches._runtime_model`).
            facts["model"] = reviewer_eligibility.read_model(
                facts["reader"], at=facts["read_at"] or "")
        if clean and facts["doors"] != []:
            facts["family"] = self.reader_family(facts["reader"])
            facts["admit"] = self._door_admission(root, facts)
        return facts

    def reader_family(self, seat):
        """The one family the seat's verdict-time runtime evidence names
        (`dispatches._approval_identity_families`, the evidence the approval
        tier reads and `helm reviewers` joins), or None when it names none,
        several, or cannot be read."""
        from . import dispatches
        if not seat:
            return None
        try:
            families, why = dispatches._approval_identity_families(seat)
        except Exception:                   # noqa: BLE001 — unread is None
            return None
        found = sorted(families or ()) if not why else []
        return found[0] if len(found) == 1 else None

    def _door_admission(self, root, facts):
        """(ok, why) for a source-clean car that is a door, or whose doors
        could not be read. It FAILS CLOSED, because auto-land pushes it on no
        person's read (the integrator's ruling R2): the holder must be
        admitted by the approval tier (`dispatches.approval_tier` reads
        `ok`; a policy that is unreadable, UNKNOWN or `none` refuses), its
        RESOLVED model must be read, and `reviewer_eligibility.input_only`,
        asked with the holder's family so its spark, gemini and local rungs
        run, must answer False (True, or None for a read it cannot rule out
        as input only, refuses). The tier is asked AT the hold's recorded
        moment (`read_at`), as the model was (task/3508)."""
        from . import dispatches, reviewer_eligibility
        reader = facts["reader"]
        what = ("a DOOR (%s)" % ", ".join(facts["doors"])
                if facts["doors"] else "possibly a door (its doors could not "
                "be read)")
        if not reader:
            return False, "%s held by nobody named" % what
        try:
            state, why = dispatches.approval_tier(
                reader, repo=root, at=facts.get("read_at") or "")
        except Exception as exc:            # noqa: BLE001 — UNKNOWN refuses
            state, why = "unknown", "%s: %s" % (type(exc).__name__, exc)
        if state != "ok":
            return False, ("%s whose holder %s is not admitted by the "
                           "approval tier (%s: %s)" % (
                               what, reader, state,
                               why or ("no approval-tier policy admits it"
                                       if state == "none" else "unread")))
        model, family = facts.get("model"), facts.get("family")
        if not model:
            return False, ("%s whose holder %s has an unread resolved model, "
                           "so it cannot be shown to be approval tier"
                           % (what, reader))
        verdict, why = reviewer_eligibility.input_only(model, family)
        if verdict is not False:
            return False, ("%s whose holder %s (%s, family %s) %s (%s)" % (
                what, reader, model, family or "unresolved",
                "reads as input only" if verdict else
                "cannot be ruled out as input only", why))
        return True, None

    # -- compose, audits, gate --------------------------------------------
    def compose(self, got):
        buf = io.StringIO()
        result = landwindow.mint_and_merge(got, out=buf)
        result["text"] = buf.getvalue()
        return result

    def audits(self, root, room, trunk):
        """(ok, detail, log): the tree-wide audits, the integrator's four,
        and the lane census of the composed diff (`gateaudits.lane_census`:
        each test module it touches, the tests of each helm module it
        touches, the test modules importing a touched one, every test naming
        a touched path), run through fab on the composed room. The log names
        the rule that selected each census module, and a failure names each
        failing module with it. ok is None when the run could not happen,
        and when the census cannot be read: the list is refused, never
        shortened to what could be read (task/1090)."""
        from . import gateaudits, gatewindow
        gone = gateaudits.missing(room)
        if gone:
            return None, ("the audit list names tests the room does not "
                          "carry: %s" % ", ".join(gone)), None
        started = time.monotonic()
        try:
            census, why = gateaudits.lane_census(room, trunk, "HEAD")
        except Exception as exc:            # noqa: BLE001 — refused, named
            census, why = None, "%s: %s" % (type(exc).__name__, exc)
        took = time.monotonic() - started
        if why:
            return None, ("the lane census of the composed room cannot be "
                          "read (%s), so no shorter list runs in its place"
                          % why), None
        names, reasons = census["modules"], census["reasons"]
        runner, _reason = gateaudits.mode(room, names)
        argv = shlex.split(gateaudits.command(room, names, runner))
        rc, out, err = gatewindow._fab(argv, AUDIT_TIMEOUT_S)
        log = os.path.join(state_dir(root), "logs", "%s-audits.log"
                           % os.path.basename(room))
        head = ["census: %d extra from %d touched paths (merge-base %s, tip "
                "%s, %.1f s)" % (len(census["extra"]), len(census["touched"]),
                                 _short(census["merge_base"]),
                                 _short(census["tip"]), took)]
        head += ["%s: %s" % (m, reasons[m]) for m in census["extra"]]
        text = "%s\n$ %s\n%s\n%s" % ("\n".join(head), " ".join(
            shlex.quote(a) for a in argv), out, err)
        try:
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            log = None
        if rc is None:
            return None, "fab could not run: %s" % _first(err), log
        said = (out or "") + "\n" + (err or "")
        ok = rc == 0 and re.search(r"^OK\b", said, re.M) is not None \
            and re.search(r"^FAILED\b", said, re.M) is None
        summary = [ln.strip() for ln in said.splitlines()
                   if re.match(r"\s*(?:Ran \d+|OK\b|FAILED\b)", ln)]
        detail = "%d modules (%d audits + %d census), census %.1f s; %s " \
            "audits, fab exit %s: %s" % (
                len(names), len(names) - len(census["extra"]),
                len(census["extra"]), took, runner, rc,
                "; ".join(summary[-2:]) or "no unittest summary")
        bad = [] if ok else gateaudits.failing(said, names)
        if bad:
            shown = ["%s (%s)" % (m, reasons[m]) for m in bad[:12]]
            if len(bad) > len(shown):
                shown.append("and %d more" % (len(bad) - len(shown)))
            detail += "; failing: %s" % ", ".join(shown)
        return ok, detail, log

    def authority(self, root):
        ident, why = self.identity(root)
        if why:
            return {"ref": None, "remote": None, "sha": None, "why": why}
        return landwindow.trunk_authority(root, ident)

    def ancestry(self, root, older, newer):
        return vcs.backend(root).ancestry(root, older, newer)

    def head_of(self, room):
        if not os.path.isdir(room):
            return None
        rc, out, _err = vcs.backend(room).text(
            room, "rev-parse", "--verify", "-q", "HEAD",
            env=landwindow._env())
        return out if rc == 0 and _SHA.match(out) else None

    def launch(self, room, name, trunk):
        from . import gatewindow
        buf = io.StringIO()
        rc, request = gatewindow.launch(room, label=name, trunk_ref=trunk,
                                        out=buf)
        return rc, request, buf.getvalue()

    def gate_host(self, room):
        """The host the landing-window door recorded for `room`'s gate, or
        None: blame launches a b-room's gate itself, and its host is read
        back from the door's own store."""
        from . import gatewindow
        real = os.path.realpath(room)
        rows = [r for r in gatewindow.read_runs(gatewindow.runs_path())
                if r.get("room") == real]
        return rows[-1].get("host") if rows else None

    def receipts(self):
        from . import gate
        rows, unavailable, _skipped = gate.receipts()
        return rows, unavailable

    def verify(self, root, head, gid):
        """(ok, why): a VERIFIED whole-suite receipt on `head`'s exact tree,
        of a kind and provenance that may authorize a land."""
        from . import foldcheck
        rung = foldcheck._tree_matches_gate(vcs.backend(root), root, head,
                                            "gate:" + gid, land=True)
        return rung.verdict == foldcheck.PASS, rung.discriminator

    def ast_delta(self, root, trunk, head):
        """(n, why): test methods the diff adds less those it removes, over
        tests/, counted by `count_test_methods` at both ends."""
        be, env = vcs.backend(root), landwindow._env()
        rc, out, err = be.text(root, "diff", "--name-only", "-z", trunk, head,
                               "--", "tests", env=env)
        if rc != 0:
            return None, "the diff is unreadable (%s)" % _first(err)
        total = 0
        for path in (p for p in out.split("\0") if p.endswith(".py")):
            counts = []
            for rev in (trunk, head):
                spec = "%s:%s" % (rev, path)
                if be.text(root, "cat-file", "-e", spec, env=env)[0] != 0:
                    counts.append(0)
                    continue
                rc, blob, err = be.text(root, "show", spec, env=env,
                                        timeout=60)
                n = count_test_methods(blob) if rc == 0 else None
                if n is None:
                    return None, "%s does not read or parse" % spec
                counts.append(n)
            total += counts[1] - counts[0]
        return total, None

    def red_facts(self, room, gid):
        from . import gatewindow, trainblame
        train, why = trainblame.read_room(room)
        if why:
            return None, None, why
        red, why = trainblame.read_red(
            train, "gate:" + gid,
            logs=gatewindow.logs_dir(gatewindow.runs_path()))
        return train, red, why

    def recheck(self, st, red):
        """(GREEN | RED | UNKNOWN | WAIT, why): the red gate's failing test
        modules alone, on the host the red gate ran on, through blame's own
        runner. WAIT while that host runs a gate."""
        from . import gatewindow, trainblame
        host = (st.get("gate") or {}).get("host")
        if not host:
            return UNKNOWN, "the red gate's host was not recorded"
        path = gatewindow.runs_path()
        live, _retired, unread = gatewindow.live_runs(
            gatewindow.read_runs(path))
        _free, known, shut, busy = trainblame.hosts(
            os.environ, gatewindow.logs_dir(path),
            live + [{"host": h} for h in unread])
        if host in busy:
            return WAIT, "%s is running a gate" % host
        if host in shut:
            return UNKNOWN, "%s is excluded by %s" % (host, "FAB_EXCLUDE_HOSTS")
        ctx = {"known": known, "shut": shut, "busy": busy,
               "environ": dict(os.environ), "red": red,
               "fab": gatewindow._fab}
        got = trainblame._run_one(ctx, {"label": "failing tests alone on %s"
                                        % host}, host, st["room"])
        return got["status"], got.get("why") or (
            "the red gate's own failing tests %s alone on %s"
            % ("pass" if got["status"] == GREEN else "fail", host))

    def audit_recheck(self, st, log):
        """(GREEN | RED | UNKNOWN | WAIT, why): the red pre-gate audits'
        failing test modules alone, once, on the host their log says Fab
        placed them on, in the runner their log's command line ran (as
        slices when they ran as slices, `trainblame.audit_red`), through
        `recheck`, the red gate's own re-run. A log
        blame cannot read, or one naming no host, is UNKNOWN and runs
        nothing."""
        from . import trainblame
        train, why = trainblame.read_room(st["room"])
        if why:
            return UNKNOWN, why
        red, why = trainblame.audit_red(train, log)
        if why:
            return UNKNOWN, why
        if not red.get("host"):
            return UNKNOWN, "the audits' log names no host they ran on"
        return self.recheck(dict(st, gate={"host": red["host"]}), red)

    def record_flake(self, root, record):
        return landwindow.record_flake(root, record)

    def blame(self, room, gid, audits=None):
        """(rc, result) of `helm train blame --apply --json` on `room`: for
        gate `gid`'s red, or, given `audits`, for the red pre-gate audit run
        that log records (`trainblame.audit_red`), which no gate names."""
        from . import trainblame
        buf = io.StringIO()
        rc = trainblame.blame(room, apply=True, as_json=True, out=buf,
                              **({"audits": audits} if audits
                                 else {"gate": "gate:" + gid}))
        try:
            result = json.loads(buf.getvalue())
        except ValueError:
            result = {"refused": "blame printed no JSON: %s"
                                 % _whole(buf.getvalue())}
        return rc, result

    def next_name(self, root, name):
        from . import trainblame
        return trainblame.next_name(root, name)

    def room_path(self, root, name):
        return _lanes.lane_path(root, os.path.join(landwindow.BOX, name))

    # -- the land -----------------------------------------------------------
    def _config(self, root, key):
        """The one --local value of `key`, or None: absent, empty or
        declared twice all read None."""
        rc, out, _err = vcs.backend(root).text(
            root, "config", "--local", "--get-all", key,
            env=landwindow._env())
        values = out.splitlines() if rc == 0 else []
        return values[0].strip() if len(values) == 1 and values[0].strip() \
            else None

    def declared(self, root):
        """(ref, remote, branch) of the declared trunk authority."""
        ref = self._config(root, "helm.trunkRef")
        remote = self._config(root, "helm.trunkRemote")
        branch = ref[len("refs/heads/"):] if ref and ref.startswith(
            "refs/heads/") else None
        return ref, remote, branch

    def foldcheck(self, root, head, gid, target):
        """foldcheck dry on `head`, its ff-able rung asked of the branch and
        the remote `target` names: the destination the push guard vetted in
        the same step, never a second read of the declared trunk."""
        from . import foldcheck
        return foldcheck.check(root, head, gate_ref="gate:" + gid,
                               remote=target.remote,
                               branch=target.ref[len("refs/heads/"):])

    def push_target(self, root):
        """(PushTarget, why): the one destination this push may use, or
        None.

        The target is returned by the same read that proves the declared
        remote names only that URL and that it is PRIVATE, and it carries the
        trunk ref that read declared. The caller passes this value straight
        to `git push`: pushing by the remote's mutable name would reopen a
        set-url race after this check, and reading helm.trunkRef again would
        let a rewrite of it move the land to another branch (task/3265 races
        F1)."""
        from . import hostpath_guard
        ref, remote, _branch = self.declared(root)
        if not ref:
            return None, ("no trunk authority is declared: helm.trunkRef "
                          "names none")
        if not _TRUNK_REF.match(ref):
            return None, ("helm.trunkRef %s names no branch (refs/heads/"
                          "<name>), so there is no ref a fast-forward push "
                          "may write" % ref)
        if not remote or remote == ".":
            return None, ("no trunk remote is declared (helm.trunkRemote), "
                          "so there is nothing auto-land may push to")
        want = self._config(root, "helm.trunkUrl")
        if not want:
            return None, ("helm.trunkUrl is not declared: auto-land pushes "
                          "only to the URL the repository names as its "
                          "trunk (git config helm.trunkUrl <url>)")
        if _DOT_SEGMENT.search(want):
            return None, ("helm.trunkUrl %s has a . or .. path segment or an "
                          "encoded dot: git collapses it on the wire, so the "
                          "privacy check would judge one path and the push "
                          "reach another" % want)
        be, env = vcs.backend(root), landwindow._env()
        # EVERY URL, not the first: `git push <remote>` pushes to each url
        # (or each pushurl) the remote names, and `git remote get-url`
        # without --all prints only the first, so a mirror added as a
        # second url would receive the land unseen.
        urls = []
        for extra in (("--all",), ("--push", "--all")):
            rc, out, _err = be.text(root, "remote", "get-url", *extra, remote,
                                    env=env)
            listed = [u.strip() for u in out.splitlines() if u.strip()] \
                if rc == 0 else []
            urls += listed or [None]
        if any(u != want for u in urls):
            return None, ("remote %s points at %s, not only the declared "
                          "trunk URL (helm.trunkUrl): a fork or mirror is "
                          "not trunk, and a push goes to every url the "
                          "remote names" % (remote, " / ".join(
                              str(u) for u in urls)))
        why = _rewritten(root, be, env, want)
        if why:
            return None, why
        visibility, why = hostpath_guard._visibility(want)
        if visibility != hostpath_guard.PRIVATE:
            return None, ("remote %s reads %s%s: auto-land pushes only to a "
                          "PRIVATE remote; a public push is the owner's"
                          % (remote, visibility, " (%s)" % why if why
                             else ""))
        return PushTarget(want, ref, remote), ("remote %s is private and is "
                                               "the declared trunk" % remote)

    def push_guard(self, root):
        """Compatibility answer for preflight and dry-run callers."""
        target, why = self.push_target(root)
        return target is not None, why

    def remote_head(self, root):
        return self.authority(root)["sha"]

    def settled_trunk(self, root, target):
        """(sha, why): what `target`'s trunk ref reads once an AMBIGUOUS push
        has had PUSH_SETTLE_S to settle (`_Tick.settle`), read once
        (`trunk_at`)."""
        time.sleep(PUSH_SETTLE_S)
        return self.trunk_at(root, target)

    def trunk_at(self, root, target):
        """(sha, why): the sha `target.ref` reads at `target.url` now. `sha`
        is None exactly when `why` says why.

        READ WHERE THE PUSH WENT: through a one-time name its environment
        maps to the vetted URL (`_pinned`), as the push is, so no url
        rewrite or remote section can make it read another repository than
        the one the push wrote. Measured on git 2.53 in temp repositories: a
        url.<mirror>.insteadOf rule on the trunk URL made `git ls-remote
        <url>` read the mirror, and the pinned name read the trunk. It drops
        every variable that names a program git runs, as the push does."""
        if not isinstance(target, PushTarget) \
                or not _TRUNK_REF.match(target.ref or ""):
            return None, "no vetted destination (url and trunk ref) to read"
        name, env = _pinned(landwindow._env(), target.url)
        rc, out, err = vcs.backend(root).text(
            root, "ls-remote", name, target.ref,
            env=dict(env, **dict.fromkeys(vcs._HELPER_SELECTION_ENV)),
            timeout=PUSH_SETTLE_READ_S)
        if rc != 0:
            return None, "%s at %s could not be read (%s)" % (
                target.ref, target.url, _first(err) if err else "rc %d" % rc)
        exact = [row[0] for row in (ln.split("\t") for ln in out.splitlines())
                 if len(row) == 2 and row[1] == target.ref]
        if len(exact) != 1 or not _SHA.match(exact[0]):
            return None, "%s at %s reads %s" % (
                target.ref, target.url, "absent" if not exact
                else "not one sha (%s)" % ", ".join(exact))
        return exact[0], None

    def push(self, root, head, target, keep=(), lease=None):
        """(ok, detail): `head` to exactly `target`, the URL and the trunk
        ref `push_target` vetted and resolved in one read, FAST-FORWARD-ONLY
        AGAINST THE GATED TRUNK: `lease` is the trunk sha the train was
        composed and gated on, and it must be an ancestor of `head`.

        `ok` is True when git answered success; False when git ran and did
        not (it exited non-zero, timed out or was killed), which is
        AMBIGUOUS, since the update may have applied all the same
        (`_Tick.settle` reads trunk before anything is decided); and None
        when the push was refused before git ran, so nothing was sent.

        THE PUSH CARRIES ITS EXPECTED OLD VALUE (task/3265 door read B1). A
        trunk rewound to an ancestor of `head` (an integrator backing out a
        land) is still one `head` fast-forwards, so a plain push re-landed
        what the rewind removed. `--force-with-lease=<ref>:<lease>` makes the
        remote refuse, in the same update, unless its ref still reads
        `lease`. A matching lease also lifts git's own fast-forward refusal
        (measured, git 2.53), so `lease` is asked here to be an ancestor of
        `head` first: the one update the remote can then accept is a
        fast-forward of the gated trunk. There is no `+` refspec.

        THE PUSH RESOLVES NO URL OF ITS OWN (task/3265 races R1). git reads
        its config when it starts, after every check here, so a rewrite
        written in between redirected a push to the literal URL. It is given
        a one-time name that its own environment maps to the vetted URL
        (`_pinned`), which no config can rewrite.

        `last_word` holds the dispatch ledger lock from its final readiness
        read through this call. Resolving the URL, the ref or the approvals
        here would reopen one of those races, so nothing here reads the
        declared trunk again (task/3265 races F1).

        THE PUSH HOLDS ITS TICK'S LOCKS, AND GIT HOLDS NONE (task/3265 races
        F4, door read B3). `keep` is the descriptors of the locks `last_word`
        runs under. They are inherited by one process, the lock holder
        `vcs.GitVcs.run_holding` starts, which starts git with them closed and
        holds them until git ends: a tick SIGKILLed mid-push releases nothing
        while its push runs on, so no tick, verb, verdict or ejection acts
        beside a push it cannot see, and nothing git starts (a helper, a
        credential-cache daemon) holds a lock past it. Chosen over
        PR_SET_PDEATHSIG, which would kill git with its parent: a push
        already sent can still land on the remote after git dies, with the
        locks gone. git's own alarm, armed by the holder, ends git
        PUSH_TIMEOUT_S + PUSH_ORPHAN_SLACK_S after it starts, so an orphaned
        push holds them that long at most; it ends git alone, and a helper
        git started lives on holding no lock."""
        if not isinstance(target, PushTarget) \
                or not _TRUNK_REF.match(target.ref or "") \
                or not _SHA.match(head or "") or not _SHA.match(lease or ""):
            return None, "no vetted destination (url and trunk ref), no " \
                "full sha, or no gated trunk to lease"
        if self.ancestry(root, lease, head) != vcs.ANCESTOR:
            return None, ("%s is not a fast-forward of the gated trunk %s, "
                          "so it is never pushed" % (_short(head),
                                                    _short(lease)))
        # ASKED AGAIN HERE, beside the push: the pinned push below goes to
        # the vetted URL whatever the config says, and a config that would
        # rewrite a push of the trunk URL still stops the land, loudly.
        why = _rewritten(root, vcs.backend(root), landwindow._env(),
                         target.url)
        if why:
            return None, why
        holding = getattr(vcs.backend(root), "run_holding", None)
        if holding is None:
            return None, ("this repository's version control cannot hand "
                          "the push the locks it runs under")
        name, env = _pinned(landwindow._env(), target.url)
        rc, out, err = holding(
            root, "push", "--force-with-lease=%s:%s" % (target.ref, lease),
            name, "%s:%s" % (head, target.ref), keep=keep,
            alarm=PUSH_TIMEOUT_S + PUSH_ORPHAN_SLACK_S, env=env,
            timeout=PUSH_TIMEOUT_S)
        return rc == 0, "\n".join(x for x in (err, out) if x)

    def ff(self, root, head):
        """(ok, why): the shared checkout fast-forwarded to `head`, only on
        the trunk branch and only when nothing is dirty. Nothing is stashed
        and nothing is reset."""
        be, env = vcs.backend(root), landwindow._env()
        _ref, _remote, branch = self.declared(root)
        rc, on, _err = be.text(root, "symbolic-ref", "--short", "-q", "HEAD",
                               env=env)
        if rc != 0 or not branch or on != branch:
            return False, ("the shared checkout is on %s, not the trunk "
                           "branch %s" % (on or "a detached HEAD", branch))
        rc, dirt, err = be.text(root, "status", "--porcelain", env=env)
        if rc != 0:
            return False, "the shared checkout's status is unreadable (%s)" \
                % _first(err)
        dirty = [ln for ln in dirt.splitlines() if ln.strip()]
        if dirty:
            return False, ("the shared checkout is dirty (%d path(s), first: "
                           "%s); it is never stashed or reset, so clean it and "
                           "`%s --resume`" % (len(dirty), dirty[0].strip(),
                                              PROG))
        rc, cur, _err = be.text(root, "rev-parse", "HEAD", env=env)
        if rc == 0 and cur == head:
            return True, "already at %s" % _short(head)
        rc, _out, err = be.text(root, "merge", "--ff-only", "-q", head,
                                env=dict(env, HELM_WORK_INTEGRATOR="1"),
                                timeout=120)
        if rc != 0:
            return False, "the fast-forward refused: %s" % _first(err)
        return True, "fast-forwarded to %s" % _short(head)

    def install_guard(self, root):
        from .work import _guard
        rc, lines = _guard.install_guard(root, apply=True, profile="rail")
        return rc, "\n".join(lines)

    def postland(self, root, head):
        """The land's first ledger read, timed by the shared checkout's own
        helm in a NEW process and recorded once per head (`postland.take`,
        task/3538): this tick imported the tree the push replaced, so a read
        timed here would time the old code. It never stops the land: this
        process imported the tree the push replaced, so even the import may
        fail here, and that failure is swallowed with the rest."""
        try:
            from . import postland
            postland.take(root, head, "autoland")
        except Exception:                                # noqa: BLE001
            pass

    def fold_apply(self, root, head, gid):
        """(rc, text): `helm lr foldcheck <head> --gate gate:<gid> --apply`,
        run in a CHILD of the installed helm, never in this process
        (task/3562).

        THIS PROCESS NO LONGER NAMES ITS CODE BY NOW. The tick fast-forwarded
        the shared checkout (`ff`) a step ago, and when helm runs from that
        checkout its package changed under it, so `foldckpt.policy()` names
        no code for the rest of the process: every fold it reads is a cold
        whole-ledger replay that reads and saves no checkpoint, and a module
        it imports from here on is the landed code beside the old. Measured
        2026-09-28: the in-process foldcheck of LAND 449 sat 14+ minutes in
        such replays, the last of them under the dispatch ledger's write
        lock, while a hand foldcheck of LAND 448 took under 110 s. A fresh
        process of the landed tree names its code, pays one cold fold, saves
        its checkpoint, and every read after it restores and folds a tail."""
        _ref, remote, branch = self.declared(root)
        argv = [sys.executable, os.path.join(_HELM_ROOT, "bin", "helm"),
                "lr", "foldcheck", head, "--gate", "gate:" + gid,
                "--repo", root, "--remote", remote or "origin",
                "--branch", branch or "main", "--apply"]
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, encoding="utf-8",
                                  errors="replace", timeout=FOLD_APPLY_S)
        except subprocess.TimeoutExpired:
            return 124, ("`helm lr foldcheck --apply` ran past %d s and was "
                         "stopped" % FOLD_APPLY_S)
        except OSError as exc:
            return 127, "`helm lr foldcheck --apply` could not start: %s" % exc
        text = done.stdout or ""
        if done.returncode != 0 and (done.stderr or "").strip():
            text += done.stderr
        return done.returncode, text

    def lr_close(self, rid, live, restart):
        """(row, err): `helm lr close <rid> --reason landed --json`, with
        `--live` or `--needs-restart`, run in a CHILD of the installed helm
        for the reason `fold_apply` is (task/3562): it runs after `ff`, so
        an in-process close reads a process that no longer names its code
        and replays the ledger cold, under the write lock too. A refusal the
        close answered (its `--json` reason) is one line in `err`, the car's
        answer. A child that did not answer (a crash, a lock deadline, a
        timeout, no start) raises `CloseFailed` quoting all it said, so the
        tick retries it as it retried an in-process close that raised."""
        what = "`helm lr close %s --reason landed`" % rid[:12]
        argv = [sys.executable, os.path.join(_HELM_ROOT, "bin", "helm"),
                "lr", "close", rid, "--reason", "landed", "--json"]
        argv += ["--live"] if live else []
        argv += ["--needs-restart", restart] if restart else []
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, encoding="utf-8",
                                  errors="replace", timeout=FOLD_APPLY_S)
        except subprocess.TimeoutExpired:
            raise CloseFailed("%s ran past %d s and was stopped"
                              % (what, FOLD_APPLY_S))
        except OSError as exc:
            raise CloseFailed("%s could not start: %s" % (what,
                                                          _first(str(exc))))
        try:
            out = json.loads(done.stdout or "")
        except ValueError:
            out = None
        if done.returncode == 0:
            return (out if isinstance(out, dict) else {"id": rid}), None
        why = out.get("reason") if isinstance(out, dict) else None
        if why:
            return None, _first(str(why))
        said = "\n".join(t for t in (done.stderr, done.stdout)
                         if (t or "").strip())
        raise CloseFailed("%s exited %d: %s" % (what, done.returncode,
                                                _whole(said)))

    def changes(self, root, trunk, tip):
        rc, out, err = vcs.backend(root).text(
            root, "diff", "--no-color", "--no-ext-diff", "%s...%s"
            % (trunk, tip), env=landwindow._env(), timeout=120)
        if rc != 0:
            return None, _first(err)
        return split_diff(out), None

    def remove_room(self, root, room):
        be = vcs.backend(root)
        if not os.path.lexists(room):
            return True, None
        rc, _out, err = be.text(root, "worktree", "remove", "--force", room,
                                env=dict(landwindow._env(),
                                         HELM_WORK_INTEGRATOR="1"))
        be.text(root, "worktree", "prune", env=landwindow._env())
        return rc == 0, None if rc == 0 else _first(err)


def _remove_rooms(root, st, ops):
    """Remove every room the train minted; -> the ones that would not go."""
    left = []
    for room in st.get("rooms") or ():
        ok, why = ops.remove_room(root, room)
        if not ok:
            left.append("%s (%s)" % (room, why))
    return left


# ---------------------------------------------------------------- the tick

def _env_seconds(name, default):
    raw = os.environ.get(name)
    return int(raw) if raw and _COUNT.match(raw.strip()) else default


def _basis_words(car):
    """How the car's reader cleared it. The reader is NAMED, never
    mentioned: a post about a car must not wake the seat that read it."""
    who = car.get("reader") or "?"
    return ("held source-clean by %s" % who
            if car.get("basis") == "source-clean" else "APPROVE by %s" % who)


def _title(text):
    """A task title as one line of plain words, capped at TITLE_CAP, or
    None when there is none."""
    words = " ".join(str(text or "").split())
    if len(words) > TITLE_CAP:
        words = words[:TITLE_CAP - 1].rstrip() + "…"
    return words or None


def _task_words(car):
    """The car's task as a merge line says it: the task, or `task UNKNOWN:
    <why>` when the one join could not decide (task/3643: never read as "no
    task"), else "no task". The reason is one line with no parentheses or
    separators, so it stays inside the subject's parenthetical and a reader
    of the line (`taskhygiene.parse_train`) takes it as unknown, not a task."""
    if car.get("task"):
        return car["task"]
    why = car.get("task_unknown")
    if not why:
        return "no task"
    why = re.sub(r"[()\[\];,]", " ", " ".join(str(why).split()))
    return "task UNKNOWN: %s" % " ".join(why.split())[:TITLE_CAP]


def _car_words(car):
    """What a car changed, in the words its task was filed with: the task
    and its title, else the task, else the lane."""
    if car.get("title"):
        return "%s: %s" % (_task_words(car), car["title"])
    if car.get("task") or car.get("task_unknown"):
        return _task_words(car)
    return "lane %s" % car["lane"]


def _red_name(gid):
    """A red in words: `gate:<id>`, or the pre-gate audit run (AUDITS)."""
    return "the pre-gate audit run" if gid == AUDITS else "gate:%s" % gid


def _merge_detail(car):
    """The parenthesis the car's merge subject carries: task (and its title
    first, when it has one: trunk's subject is where the morning report
    reads a land's plain words), priority, whether it is a door, its author
    and its reader."""
    doors = car.get("doors")
    door = ("not a door" if doors == [] else "a DOOR: %s" % ", ".join(doors)
            if doors else "doors UNKNOWN")
    what = "%s;" % _car_words(car) if car.get("title") \
        else "%s," % _task_words(car)
    return "%s %s, %s; author %s; %s (%s) read %s %s" % (
        what, car.get("priority") or "P?", door,
        car.get("author") or "?", car.get("reader") or "?",
        car.get("model") or "model unread",
        "SOURCE-CLEAN" if car.get("basis") == "source-clean" else "APPROVE",
        _short(car["id"]))


class _Tick(object):
    """One tick over one project: the handlers, and the posting and saving
    they share. Every write and every post goes through `save` and `post`,
    and both do nothing but print without --apply."""

    def __init__(self, root, apply, ops, out):
        self.root, self.apply, self.ops, self.out = root, apply, ops, out
        self.now = ops.now()
        self.control = None
        self.seen = set()
        # Whether this tick holds the store's lock right now (`locked`): a
        # save inside it writes straight through and never takes the lock
        # twice, which would wait on itself.
        self.held = False

    def say(self, line):
        print("%s: %s" % (PROG, line), file=self.out)

    def would(self, line):
        self.say("DRY RUN — would %s; nothing written or posted" % line)
        return 0

    def save(self, st, note=None, create=False):
        """Write the train's state, under the store's lock and only while
        the train is still this tick's (`_check`): a verb that wrote the file
        since this tick read it wins, and the tick stops acting. `create` is
        the one write that makes the train's file (the intent). A lock not
        taken in time writes nothing: a write outside it could land over a
        verb's."""
        if not self.apply:
            return
        st["updated_ts"] = self.now
        if note:
            _history(st, self.now, note)
        with self.locked() as held:
            if not held:
                raise _Superseded("the auto-land store's lock was not taken "
                                  "in time, so %s is not written"
                                  % st["train"])
            self._write(st, create)

    @contextlib.contextmanager
    def locked(self):
        """Yield whether this tick holds the store's lock: taken here, or
        already held by an enclosing `locked`, which releases it."""
        if self.held:
            yield True
            return
        with _state_lock(self.root) as held:
            self.held = held
            try:
                yield held
            finally:
                self.held = False

    def _write(self, st, create=False):
        self._check(st, create)
        st["rev"] = int(st.get("rev") or 0) + 1
        _write_json(train_path(self.root, st["train"]), st)

    def _check(self, st, create=False):
        """THE TRAIN IS STILL THIS TICK'S, or `_Superseded` is raised: its
        file stands at the revision this tick read or last wrote, or, for
        the one `create` write (the intent), stands nowhere yet. The one
        write a verb may make under a tick is a veto on an INTENT train,
        which is taken into `st`, never written over. Asked under the
        store's lock, the lock every verb writes under."""
        disk, why = _read_json(train_path(self.root, st["train"]))
        if why:
            raise _Superseded("%s cannot be read back (%s)" % (st["train"],
                                                               why))
        if disk is None:
            if create:
                return
            raise _Superseded("%s was ended by another hand (`%s --abandon`) "
                              "while this tick ran" % (st["train"], PROG))
        if not create and int(disk.get("rev") or 0) == int(st.get("rev")
                                                           or 0):
            return
        if not create and disk.get("state") == INTENT and disk.get("veto") \
                and st.get("state") in (INTENT, VETOED):
            st["veto"] = st.get("veto") or disk["veto"]
            st["rev"] = disk.get("rev")
            return
        raise _Superseded("%s was changed by another hand while this tick "
                          "ran (it reads %s now)" % (st["train"],
                                                     disk.get("state")))

    def post(self, text):
        """(ok): one post; a failure is printed and never recorded as sent."""
        if not self.apply:
            self.say("would post: %s" % text)
            return True
        _row, err = self.ops.post(text)
        if err:
            self.say("POST FAILED (%s), retried next tick: %s" % (err, text))
            return False
        return True

    def post_once(self, st, key, text, integrator=True):
        """Post `text` once per `key` for this train."""
        posted = st.setdefault("posted", [])
        if key in posted:
            return True
        text = self.ops.address(text) if integrator else text
        if not self.post(text):
            return False
        if self.apply:
            posted.append(key)
            self.save(st)
        return True

    def walk_owed(self):
        """A land is when the console walk may have become owed (task/3444):
        said to the integrator once per owed state, beside the restarts a
        land owes, and never to @all. A walk that cannot be read is printed
        and never holds the land."""
        if not self.apply:
            return
        try:
            self.ops.console_walk(self.root, lambda text: self.post(
                self.ops.address(text)), say=self.say)
        except Exception as exc:            # noqa: BLE001 — named, not held
            self.say("whether a console walk is owed was not read: %s: %s"
                     % (type(exc).__name__, exc))

    def refuse(self, key, text):
        """A refusal with no train to record it on: posted once while it
        stands, printed on every tick. -> 1."""
        self.seen.add(key)
        self.say("REFUSED — %s" % text)
        posted = self.control.setdefault("posted", {})
        if key not in posted and self.post(self.ops.address(
                "auto-land REFUSED — %s" % text)) and self.apply:
            posted[key] = self.now
        return 1

    def finish(self):
        """Forget a refusal FORGET_S after it was posted once it no longer
        stands, so one that returns later is posted again; one that stands
        is never posted twice."""
        if not self.apply or self.control is None:
            return
        # THE SWITCH IS READ AGAIN UNDER THE STORE'S LOCK and only its
        # `posted` map is written: a `--pause` taken while this tick ran is
        # on disk, never in `self.control`, and must not be written over.
        with self.locked() as held:
            if not held:
                self.say("the auto-land store is locked; what this tick "
                         "posted is not remembered")
                return
            fresh, why = read_control(self.root)
            if why:
                self.say("the switch is unreadable (%s); what this tick "
                         "posted is not remembered" % why)
                return
            posted = dict(fresh.get("posted") or {})
            for key, ts in (self.control.get("posted") or {}).items():
                posted.setdefault(key, ts)
            kept = {k: v for k, v in posted.items()
                    if k in self.seen or self.now - (v or 0) < FORGET_S}
            if kept != (fresh.get("posted") or {}):
                fresh["posted"] = kept
                _write_json(control_path(self.root), fresh)

    def stop(self, st, why):
        """STOPPED: posted once to the integrator, with the room left as it
        stands. -> 0 (the stop is the answer)."""
        if not self.apply:
            return self.would("STOP %s: %s" % (st.get("name") or st["train"],
                                               why))
        st["stopped"] = {"why": why, "state": st.get("state"),
                         "step": st.get("step"), "ts": self.now}
        st["state"] = STOPPED
        self.save(st, "STOPPED: %s" % why)
        self.say("STOPPED %s — %s" % (st.get("name") or st["train"], why))
        self._stop_post(st)
        return 0

    def _stop_post(self, st):
        stop = st.get("stopped") or {}
        where = "%s%s" % (stop.get("state"), "/" + stop["step"]
                          if stop.get("step") else "")
        self.post_once(st, "stopped:%s:%s" % (where, stop.get("why")), (
            "auto-land STOPPED %s at %s: %s. Room %s (head %s) is left as it "
            "stands; nothing more is automated for it. `%s --resume` retries "
            "from %s once the cause is cured; `%s --abandon --reason R` ends "
            "it and removes its rooms." % (
                st.get("name") or st["train"], where, stop.get("why"),
                st.get("room") or "not minted", _short(st.get("head")), PROG,
                where, PROG)))

    def archive(self, st, state, note):
        if not self.apply:
            st["state"] = state
            return self.would("end %s as %s: %s" % (st["train"], state, note))
        # UNDER THE STORE'S LOCK FROM THE CHECK TO THE ARCHIVE, so a verb
        # never ends the same train beside it.
        with self.locked() as held:
            if not held:
                raise _Superseded("the auto-land store's lock was not taken "
                                  "in time, so %s is not ended" % st["train"])
            self._check(st)
            st["state"] = state
            left = _remove_rooms(self.root, st, self.ops)
            if left:
                st["rooms_left"] = left
                self.post_once(st, "rooms-left", "auto-land %s: these rooms "
                               "would not be removed: %s" % (
                                   st.get("name") or st["train"],
                                   "; ".join(left)))
            _history(st, self.now, note)
            _archive(self.root, st, self.now)
        self.say("%s %s — %s" % (st["train"], state, note))
        return 0

    # -- the loop -----------------------------------------------------------
    def run(self):
        self.control, why = read_control(self.root)
        if why:
            self.say("REFUSED — the switch is unreadable (%s)" % why)
            return 1
        paused = self.control.get("paused")
        if paused:
            self.say("paused by %s (%s) — no tick acts; `%s --resume` turns "
                     "it back on" % (paused.get("by"), paused.get("reason")
                                     or "no reason given", PROG))
            return 0
        st, why = active(self.root)
        if why:
            rc = self.refuse("store", "the auto-land store cannot be read: %s"
                             % why)
            self.finish()
            return rc
        if st and st.get("state") in TERMINAL:
            # A tick that died between a terminal write and its archive.
            if self.apply:
                with self.locked() as held:
                    if held:
                        _archive(self.root, st, self.now)
            st = None
        handler = {None: self.idle, INTENT: self.intent,
                   COMPOSING: self.composing, GATING: self.gating,
                   LANDING: self.landing, STOPPED: self.stopped}.get(
                       st.get("state") if st else None)
        if handler is None:
            rc = self.refuse("state", "%s holds a state this helm cannot "
                             "read (%r)" % (st.get("train"), st.get("state")))
        else:
            try:
                rc = handler(st) if st else handler()
            except _Superseded as exc:
                self.say("STOPPED ACTING — %s; this tick writes nothing more "
                         "and the next one reads the store afresh" % exc)
                rc = 0
        self.finish()
        return rc

    # -- IDLE ---------------------------------------------------------------
    def idle(self):
        flying = self.ops.in_flight(self.root, ())
        if flying:
            self.say("IDLE — a train composed by hand is in flight, so none "
                     "is composed beside it: %s" % "; ".join(
                         "%s (%s)" % pair for pair in flying))
            return 0
        got, why = self.ops.plan(self.root)
        if why:
            return self.refuse("plan", "`helm train` could not plan: %s"
                               % why)
        if not got["cars"]:
            self.say("IDLE — no car is READY on the ledger; nothing posted")
            return 0
        cars, barred = self.admitted(got["cars"])
        for car, why in barred:
            self.refuse("barred:%s:%s" % (car["id"], car["tip"]),
                        "lane %s (row %s, tip %s) does not ride: it is %s"
                        % (car["lane"], _short(car["id"]), _short(car["tip"]),
                           why))
        if not cars:
            self.say("IDLE — no READY car may ride; nothing posted")
            return 0
        key, why = self.preflight()
        if key:
            return self.refuse(key, "no train starts: %s" % why)
        st = {"v": 1, "train": got["train"], "name": got["train"],
              "state": INTENT, "step": None, "intent_ts": None,
              "created_ts": self.now, "ref": got["ref"],
              "trunk": got["trunk"], "cars": cars, "dropped": [],
              "rooms": [], "red": [], "flakes": 0, "posted": [],
              "veto": None, "history": []}
        if not self.apply:
            return self.would("post %s" % self.intent_text(st))
        self.save(st, "intent recorded", create=True)
        return self.intent(st)

    def preflight(self):
        """(key, why) for what would stop a land after its gate was spent,
        asked before a train starts, or (None, None): an unseeded LAND
        counter, and a push the guard would refuse."""
        got, why = _read_json(counter_path(self.root))
        if why or not got:
            return "counter", (why or "no LAND counter is seeded here; `%s "
                               "seed <n> <sha>` records the last land"
                               % PROG)
        ok, why = self.ops.push_guard(self.root)
        if not ok:
            return "push-guard", "the land could not be pushed: %s" % why
        return None, None

    def admitted(self, plan_cars):
        """([car record], [(car, why)]): each car with its facts, and the
        source-clean door cars whose reader the approval tier does not
        admit."""
        cars, barred = [], []
        for car in plan_cars:
            facts = self.ops.car_facts(self.root, car)
            ok, why = facts.get("admit") or (True, None)
            if not ok:
                barred.append((car, why))
                continue
            cars.append({"id": car["id"], "lane": car["lane"],
                         "tip": car["tip"], "basis": car.get("basis"),
                         "task": facts.get("task"),
                         "task_unknown": facts.get("task_unknown"),
                         "title": facts.get("title"),
                         "priority": facts.get("priority"),
                         "doors": facts.get("doors"),
                         "author": facts.get("author"),
                         "reader": facts.get("reader"),
                         "model": facts.get("model")})
        return cars, barred

    def intent_text(self, st):
        veto_s = _env_seconds(VETO_ENV, VETO_S)
        return ("auto-land INTENT: %s lands in %d min unless vetoed, over %s "
                "at %s. Cars (READY on the ledger): %s. Veto: `helm train "
                "veto %s --reason R`." % (
                    st["train"], max(1, veto_s // 60), st.get("ref") or
                    "trunk", _short(st["trunk"]), "; ".join(
                        "%d. lane %s, %s, tip %s, row %s, %s" % (
                            i, c["lane"], _task_words(c),
                            _short(c["tip"]), _short(c["id"]),
                            _basis_words(c))
                        for i, c in enumerate(st["cars"], 1)), st["train"]))

    # -- INTENT -------------------------------------------------------------
    def intent(self, st):
        if st.get("veto"):
            veto_ = st["veto"]
            text = ("auto-land %s VETOED by %s: %s. Nothing was composed; the "
                    "next tick plans afresh." % (st["train"], veto_.get("by"),
                                                 veto_.get("reason")))
            if not self.post_once(st, "vetoed", text, integrator=False):
                return 1
            return self.archive(st, VETOED, "vetoed by %s: %s"
                                % (veto_.get("by"), veto_.get("reason")))
        if st.get("intent_ts") is None:
            if not self.apply:
                return self.would("post %s" % self.intent_text(st))
            if not self.post(self.ops.address(self.intent_text(st))):
                return 1
            st["intent_ts"] = self.now
            self.save(st, "intent posted")
            self.say("INTENT %s posted; the veto window runs %d s"
                     % (st["train"], _env_seconds(VETO_ENV, VETO_S)))
            return 0
        left = st["intent_ts"] + _env_seconds(VETO_ENV, VETO_S) - self.now
        if left > 0:
            self.say("INTENT %s — %d s left in the veto window"
                     % (st["train"], left))
            return 0
        flying = self.ops.in_flight(self.root, ())
        if flying:
            self.say("INTENT %s waits: a train composed by hand is in "
                     "flight: %s" % (st["train"], "; ".join(
                         "%s (%s)" % pair for pair in flying)))
            return 0
        got, why = self.ops.plan(self.root)
        if why:
            return self.refuse("plan", "`helm train` could not plan: %s"
                               % why)
        stale = landwindow.authority_refusal(got)
        if stale:
            return self.refuse("authority", stale)
        if got.get("ejections_unknown"):
            return self.refuse("ejections", got["ejections_unknown"])
        still, dropped = self.still_ready(st, got)
        joined = [c for c in got["cars"]
                  if c["id"] not in {x["id"] for x in st["cars"]}]
        if joined:
            self.say("left for the next train (joined after the intent): %s"
                     % ", ".join(c["lane"] for c in joined))
        if not still:
            text = ("auto-land %s: no car of the intent is still READY at its "
                    "tip (%s); nothing was composed, and the next tick plans "
                    "afresh." % (st["train"], "; ".join(
                        "lane %s: %s" % (d["lane"], d["why"])
                        for d in dropped) or "none"))
            if not self.post_once(st, "empty", text):
                return 1
            st["dropped"] = st.get("dropped", []) + dropped
            return self.archive(st, ABANDONED, "no car still READY")
        if not self.apply:
            return self.would("compose %s with %s%s" % (
                got["train"], ", ".join(c["lane"] for c in still),
                "; dropped: %s" % ", ".join(d["lane"] for d in dropped)
                if dropped else ""))
        with compose_lock(self.root, 0) as free:
            if not free:
                self.say("INTENT %s waits: a train is being composed by hand "
                         "(the compose lock is held)" % st["train"])
                return 0
            flying = self.ops.in_flight(self.root, ())
            if flying:
                self.say("INTENT %s waits: a train composed by hand is in "
                         "flight: %s" % (st["train"], "; ".join(
                             "%s (%s)" % pair for pair in flying)))
                return 0
            vetoed = self.leave_intent(st, got, still, dropped)
            if vetoed is None:
                self.say("the auto-land store is locked; next tick")
                return 0
        if vetoed:
            st["veto"] = vetoed
            return self.intent(st)
        if dropped:
            self.post_once(st, "dropped", "auto-land %s: dropped since the "
                           "intent: %s. The rest compose." % (
                               st["name"], "; ".join(
                                   "lane %s (row %s): %s" % (
                                       d["lane"], _short(d["id"]), d["why"])
                                   for d in dropped)))
        return self.composing(st)

    def leave_intent(self, st, got, still, dropped):
        """Move the train to COMPOSING under the store's lock -> the veto
        found there instead (the train does not move), False once it moved,
        or None when the lock was not taken. THE LAST WORD BEFORE THE TRAIN
        LEAVES INTENT: a veto the verb wrote since this tick read the file
        wins, under the same lock the verb writes it under."""
        with self.locked() as held:
            if not held:
                return None
            fresh, _why = _read_json(train_path(self.root, st["train"]))
            vetoed = (fresh or {}).get("veto")
            if vetoed:
                return vetoed
            st["state"], st["step"] = COMPOSING, "minting"
            st["name"], st["trunk"], st["ref"] = (got["train"], got["trunk"],
                                                  got["ref"])
            st["room"] = self.ops.room_path(self.root, got["train"])
            st["cars"] = still
            st["dropped"] = st.get("dropped", []) + dropped
            self.save(st, "window closed: %d car(s) compose" % len(still))
        return False

    def still_ready(self, st, got, since="the intent"):
        """(still, dropped): the train's cars the fresh plan still lists at
        the same tip; each other car with why. A car the plan lists that the
        train did not name waits for the next train."""
        now = {(c["id"], c["tip"]) for c in got["cars"]}
        by_id = {c["id"]: c for c in got["cars"]}
        why_out = {c["id"]: c.get("why") for c in got.get("excluded") or ()}
        still, dropped = [], []
        for car in st["cars"]:
            if (car["id"], car["tip"]) in now:
                still.append(car)
                continue
            other = by_id.get(car["id"])
            why = ("re-tipped to %s since %s; the car is the tip %s named, "
                   "and the new one rides the next train"
                   % (_short(other["tip"]), since, since) if other else
                   why_out.get(car["id"]) or "no longer READY on the ledger")
            dropped.append({"id": car["id"], "lane": car["lane"],
                            "tip": car["tip"], "why": why})
        return still, dropped

    # -- COMPOSING ----------------------------------------------------------
    def composing(self, st):
        if st.get("step") == "minting":
            if not self.apply:
                return self.would("mint %s and merge %d car(s)"
                                  % (st["room"], len(st["cars"])))
            ident, why = self.ops.identity(self.root)
            if why:
                return self.refuse("identity", why)
            if st["room"] not in st["rooms"]:
                if os.path.lexists(st["room"]):
                    return self.stop(st, "%s already exists and auto-land "
                                     "did not mint it" % st["room"])
                # THE ROOM IS OURS FROM HERE, recorded before the mint, so a
                # tick that dies mid-merge leaves a room the next tick knows.
                st["rooms"].append(st["room"])
                self.save(st, "minting %s" % st["room"])
            elif os.path.lexists(st["room"]):
                # A tick that died mid-merge: nothing was gated on the room,
                # so it is minted again from trunk.
                self.ops.remove_room(self.root, st["room"])
            result = self.ops.compose({
                "root": self.root, "identity": ident, "trunk": st["trunk"],
                "train": st["name"], "room": st["room"],
                "cars": [dict(c, detail=_merge_detail(c))
                         for c in st["cars"]]})
            if result.get("stuck"):
                return self.stop(st, "the compose stopped: %s"
                                 % result["stuck"])
            refused = result.get("refused") or []
            for car, why in refused:
                st["dropped"].append({"id": car["id"], "lane": car["lane"],
                                      "tip": car["tip"],
                                      "why": "conflict, never resolved: %s"
                                             % why})
            if refused:
                self.post_once(st, "conflicts", "auto-land %s: dropped at "
                               "compose, owed a tip that merges onto %s: %s. "
                               "No conflict is ever resolved here." % (
                                   st["name"], _short(st["trunk"]),
                                   "; ".join("lane %s (row %s): %s" % (
                                       c["lane"], _short(c["id"]), why)
                                       for c, why in refused)))
            merged = {c["id"] for c in result.get("merged") or ()}
            st["cars"] = [c for c in st["cars"] if c["id"] in merged]
            if not st["cars"]:
                self.post_once(st, "none-merged", "auto-land %s: no car "
                               "merged, so nothing is gated; the room is "
                               "removed." % st["name"])
                return self.archive(st, ABANDONED, "no car merged")
            st["head"], st["step"] = result["head"], "composed"
            self.save(st, "composed at %s" % _short(result["head"]))
        if st.get("step") == "composed":
            blaming = st.get("blaming")
            if blaming:
                return self.interrupted_blame(st, blaming)
            if self.ops.head_of(st["room"]) != st["head"]:
                return self.stop(st, "the room %s no longer stands at the "
                                 "composed head %s" % (st["room"],
                                                       _short(st["head"])))
            if not self.apply:
                return self.would("run the pre-gate audits on %s"
                                  % st["room"])
            ok, detail, log = self.ops.audits(self.root, st["room"],
                                              st["trunk"])
            st["audits"] = {"ok": ok, "detail": detail, "log": log}
            if ok is None:
                self.save(st)
                return self.refuse("audits:%s" % st["name"],
                                   "the pre-gate audits of %s could not run: "
                                   "%s" % (st["name"], detail))
            if not ok:
                return self.audits_red(st, detail, log)
            st["step"] = "audited"
            self.save(st, "pre-gate audits OK: %s" % detail)
        return self.launch(st)

    def audits_red(self, st, detail, log):
        """THE PRE-GATE AUDITS FAILED, and a red gate's EJECT road is taken
        when it can be (task/3674): `helm train blame --apply` reads their
        log in place of a receipt (`trainblame.audit_red`), and when blame by
        diff names exactly one car, that car is told and recorded, the train
        is composed again without it, and its b-room is tracked and gated
        (`ejected`); a one-car train whose car is named ends. A run whose log
        was not written, a failure list blame cannot read, and no car or two
        named STOP the train as before: a red audit is never bisected. Blame
        runs once per red, as `red` runs it.

        THE FAILURE MUST REPEAT FIRST. A census module runs because a car's
        diff touches it, so blame by diff would pin a flake there on exactly
        that car. The failing modules are therefore re-run alone, once, on
        the host the audits ran on, through the gate's own re-run
        (`Ops.audit_recheck`), and only a RED re-run is blamed. A pass, a busy
        host or a re-run that cannot be read STOPS the train and blames
        nobody."""
        why = "the pre-gate audits failed on the composed room (%s; log %s)" \
            % (detail, log)
        if not log:
            return self.stop(st, why)
        status, said = self.ops.audit_recheck(st, log)
        if status != RED:
            return self.stop(st, "%s, and their failing tests re-run alone "
                             "read %s (%s): a failure that does not repeat "
                             "alone ejects no car" % (why, status, said))
        broom = self.ops.room_path(self.root, self.ops.next_name(
            self.root, st["name"]) or "")
        st["blaming"] = {"gate": AUDITS, "broom": broom}
        self.save(st, "blaming the pre-gate audits")
        rc, result = self.ops.blame(st["room"], None, audits=log)
        st["blaming"] = None
        verdict = result.get("verdict") or {}
        if verdict.get("kind") == EJECT:
            return self.ejected(st, AUDITS, rc, verdict, broom)
        return self.stop(st, "%s, and blame ejects no car: %s" % (
            why, verdict.get("why") or result.get("refused")
            or "no reason given"))

    def launch(self, st):
        """Prove trunk is still under the room, then launch the gate through
        the landing-window door, in one step."""
        if not self.apply:
            return self.would("launch the gate on %s" % st["room"])
        auth = self.ops.authority(self.root)
        if not auth.get("sha"):
            return self.refuse("authority", "the trunk authority is UNKNOWN "
                               "(%s), so whether the room still stands on "
                               "trunk cannot be asked" % auth.get("why"))
        if self.ops.ancestry(self.root, auth["sha"], st["head"]) \
                != vcs.ANCESTOR:
            self.post_once(st, "stranded", "auto-land %s: trunk moved to %s "
                           "under the room, so its gate would bind nothing "
                           "that can land; it is abandoned and the next tick "
                           "plans afresh." % (st["name"], _short(auth["sha"])))
            return self.archive(st, ABANDONED, "trunk moved under the room")
        rc, request, text = self.ops.launch(st["room"], st["name"],
                                            st["trunk"])
        st["state"] = GATING
        st["step"] = None
        if rc == 0:
            st["launched"] = True
            st["launch_ts"] = self.now
            st["gate"] = {"host": (request or {}).get("host"),
                          "job_id": (request or {}).get("job_id")}
            self.save(st, "gate launched on %s" % st["gate"]["host"])
            self.say("GATING %s — the gate is launched on %s" % (
                st["name"], st["gate"]["host"]))
            return 0
        if rc == 3:
            st["launched"] = False
            self.save(st, "the gate door waits")
            self.post_once(st, "door-waits:%s" % st["head"], "auto-land %s: "
                           "the gate door does not launch yet, so the train "
                           "waits and retries each tick: %s" % (
                               st["name"], _whole(text)))
            return 0
        st["launched"] = False
        return self.stop(st, "the gate door refused (exit %s): %s"
                         % (rc, _whole(text)))

    # -- GATING -------------------------------------------------------------
    def gating(self, st):
        blaming = st.get("blaming")
        if blaming:
            return self.interrupted_blame(st, blaming)
        if not st.get("launched"):
            return self.launch(st)
        rows, why = self.ops.receipts()
        if why:
            return self.refuse("receipts", "the gate ledger is unreadable: %s"
                               % why)
        mine = [r for r in rows or () if r.get("head") == st["head"]
                and r.get("id") not in st.get("red", ())]
        if not mine:
            waited = self.now - (st.get("launch_ts") or self.now)
            if waited > _env_seconds(GATE_WAIT_ENV, GATE_WAIT_S):
                return self.stop(st, "no receipt names the head %s %d s "
                                 "after its gate launched" % (
                                     _short(st["head"]), waited))
            self.say("GATING %s — waiting on the gate for %s (%d s)"
                     % (st["name"], _short(st["head"]), waited))
            return 0
        row = max(mine, key=lambda r: str(r.get("ts") or ""))
        if row.get("status") == "OK":
            return self.green(st, row, rows)
        if row.get("status") == "FAILED":
            return self.red(st, row)
        return self.stop(st, "gate:%s reads %s (%s), which is neither green "
                         "nor red" % (row.get("id"), row.get("status"),
                                      row.get("detail") or "no detail"))

    def green(self, st, row, rows):
        gid = str(row["id"])
        if not self.apply:
            return self.would("verify gate:%s and land %s" % (gid, st["name"]))
        ok, why = self.ops.verify(self.root, st["head"], gid)
        if not ok:
            return self.stop(st, "gate:%s is green but cannot authorize this "
                             "land: %s" % (gid, why))
        prev = [r for r in rows if r.get("head") == st["trunk"]
                and r.get("status") == "OK" and r.get("suite")]
        if not prev:
            return self.stop(st, "no whole-suite receipt names trunk %s, so "
                             "the test-count delta of gate:%s is UNKNOWN"
                             % (_short(st["trunk"]), gid))
        # THE PLANNED DELTA IS THE QUESTION, asked whenever both receipts
        # carry the runner's planned count (`planned_count`): Ran moves with
        # what the gate host skips, planned only with what the tree holds.
        # Trunk's exact tree can hold both kinds, so a train whose receipt
        # carries a count takes trunk's newest receipt that carries one too;
        # otherwise trunk's newest, and the Ran rule, as before.
        planned, lane_why = planned_count(row)
        counted = [r for r in prev if planned_count(r)[0] is not None] \
            if planned is not None else []
        base = max(counted or prev, key=lambda r: str(r.get("ts") or ""))
        ast_n, why = self.ops.ast_delta(self.root, st["trunk"], st["head"])
        if ast_n is None:
            return self.stop(st, "the test-method delta is UNKNOWN: %s" % why)
        base_planned, base_why = planned_count(base)
        ran = row.get("ran")
        if planned is not None and base_planned is not None:
            rule, delta = "planned", planned - base_planned
            said = "gate:%s planned %+d tests over trunk's gate:%s (%d " \
                "planned)" % (gid, delta, base.get("id"), base_planned)
            because = "the planned rule: both receipts carry the slice " \
                "runner's planned count"
        else:
            rule = "ran"
            delta = ran - base.get("ran") if isinstance(ran, int) \
                and isinstance(base.get("ran"), int) else None
            said = "gate:%s ran %s over trunk's gate:%s (%s)" % (
                gid, "%+d" % delta if delta is not None
                else "an UNKNOWN count", base.get("id"), base.get("ran"))
            because = "the Ran rule: %s" % "; ".join(
                w for w in (lane_why, base_why) if w)
        if delta != ast_n:
            return self.stop(st, "%s, but the diff adds %+d test methods: a "
                             "module stopped being collected or a test was "
                             "dropped (%s)" % (said, ast_n, because))
        st["receipt"] = {"id": gid, "ran": ran, "tree": row.get("tree"),
                         "base": base.get("id"), "base_ran": base.get("ran"),
                         "rule": rule, "planned": planned,
                         "base_planned": base_planned, "delta": delta,
                         "ast": ast_n}
        st["state"], st["step"] = LANDING, "verified"
        self.save(st, "gate:%s GREEN, planned %+d = AST %+d" % (
            gid, delta, ast_n) if rule == "planned" else
            "gate:%s GREEN, Ran %+d = AST %+d (%s)" % (gid, delta, ast_n,
                                                      because))
        return self.landing(st)

    def red(self, st, row):
        gid = str(row["id"])
        if not self.apply:
            return self.would("re-run gate:%s's failing tests alone on its "
                              "host, then blame" % gid)
        _train, red, why = self.ops.red_facts(st["room"], gid)
        if why:
            st["red"].append(gid)
            return self.stop(st, "gate:%s is RED and cannot be read for "
                             "blame: %s" % (gid, why))
        status, why = self.ops.recheck(st, red)
        if status == WAIT:
            self.say("RED gate:%s — its host is busy: %s; next tick"
                     % (gid, why))
            return 0
        if status == UNKNOWN and (st.get("gate") or {}).get("host"):
            return self.refuse("recheck:%s" % gid, "gate:%s is RED and its "
                               "failing tests could not be re-run alone on "
                               "its host: %s" % (gid, why))
        if status == GREEN:
            if st.get("flakes", 0) >= 1:
                st["red"].append(gid)
                return self.stop(st, "gate:%s is RED and its failing tests "
                                 "pass alone again (%s): a second flake"
                                 % (gid, why))
            _row, err = self.ops.record_flake(self.root, {
                "tree": red.get("tree"), "gate": gid, "train": st["name"],
                "tests": [t["id"] for t in red.get("tests") or ()],
                "why": "auto-land: %s" % why})
            if err:
                self.say("FLAKE NOT RECORDED — %s" % err)
            return self.regate(st, gid, "the failing tests pass alone on the "
                               "red gate's host (%s)" % why)
        broom = self.ops.room_path(self.root, self.ops.next_name(
            self.root, st["name"]) or "")
        # BLAME RUNS ONCE PER RED: it tells an author and records an
        # ejection, so a tick that dies inside it must not run it again.
        st["blaming"] = {"gate": gid, "broom": broom}
        self.save(st, "blaming gate:%s" % gid)
        rc, result = self.ops.blame(st["room"], gid)
        st["red"].append(gid)
        st["blaming"] = None
        verdict = result.get("verdict") or {}
        kind = verdict.get("kind")
        if kind == FLAKE:
            if st.get("flakes", 0) >= 1:
                return self.stop(st, "gate:%s is RED and blame reads a "
                                 "second FLAKE (%s)" % (gid, verdict.get(
                                     "why")))
            return self.regate(st, gid, "blame reads FLAKE: %s"
                               % verdict.get("why"))
        if kind == EJECT:
            return self.ejected(st, gid, rc, verdict, broom)
        return self.stop(st, "gate:%s is RED and blame reads %s: %s" % (
            gid, kind or "a refusal", verdict.get("why")
            or result.get("refused") or "no reason given"))

    def interrupted_blame(self, st, blaming):
        """A tick died inside `helm train blame --apply`: its b-room, when
        blame got that far, is tracked and gated; otherwise its outcome is
        UNKNOWN and the train stops. Blame is never run twice on one red."""
        if not self.apply:
            return self.would("read back the interrupted blame of %s"
                              % _red_name(blaming.get("gate")))
        gid, broom = blaming.get("gate"), blaming.get("broom")
        if gid != AUDITS:
            st["red"].append(gid)
        st["blaming"] = None
        head = self.ops.head_of(broom) if broom else None
        if not head:
            return self.stop(st, "the blame of %s was interrupted and left no "
                             "b-room, so whether it ejected a car is UNKNOWN; "
                             "read `helm train readmit`'s store and the room "
                             "before resuming" % _red_name(gid))
        # The cars are the ones the b-room carries; the one blame ejected
        # is not in its history.
        st["cars"] = [c for c in st["cars"] if self.ops.ancestry(
            self.root, c["tip"], head) == vcs.ANCESTOR]
        st["rooms"].append(broom)
        st["room"], st["head"] = broom, head
        st["name"] = os.path.basename(broom)
        st["launched"] = False
        self.save(st, "the interrupted blame of %s left %s; it is gated"
                  % (_red_name(gid), st["name"]))
        return self.launch(st)

    def regate(self, st, gid, why):
        st["flakes"] = st.get("flakes", 0) + 1
        if gid not in st["red"]:
            st["red"].append(gid)
        st["launched"] = False
        self.save(st, "gate:%s FLAKE — one re-gate: %s" % (gid, why))
        self.say("FLAKE gate:%s — %s; the room is gated once more" % (gid,
                                                                     why))
        return self.launch(st)

    def ejected(self, st, gid, rc, verdict, broom):
        """Blame ejected a car for red `gid` (a gate's id, or AUDITS): the
        train carries the cars its b-room merged and waits on that room's
        gate, which blame's compose launched."""
        car = verdict.get("car") or {}
        kept = [c for c in st["cars"] if c["id"] != car.get("id")]
        st.setdefault("ejected", []).append({
            "id": car.get("id"), "lane": car.get("lane"),
            "tip": car.get("tip"), "gate": gid, "why": verdict.get("why")})
        red = _red_name(gid)
        if not kept:
            self.post_once(st, "ejected-all", "auto-land %s: %s is RED and "
                           "blame ejected its only car, lane %s; nothing is "
                           "left to land." % (st["name"], red,
                                              car.get("lane")))
            return self.archive(st, ABANDONED, "its only car was ejected")
        head = self.ops.head_of(broom)
        if not head:
            return self.stop(st, "%s is RED and blame ejected lane %s, but "
                             "its b-room %s was not composed (blame exit %s)"
                             % (red, car.get("lane"), broom, rc))
        # A CAR WHOSE MERGE THE B-ROOM REFUSED IS NOT IN ITS HEAD. compose_room
        # gates the room with the cars that merged and exits 1, so the train
        # carries only those (as interrupted_blame does); the others' land
        # requests ride the next train. Carried, they reached the last word,
        # the close and the LAND announcement unlanded (task/3265 door read).
        merged = [c for c in kept if self.ops.ancestry(
            self.root, c["tip"], head) == vcs.ANCESTOR]
        if not merged:
            self.post_once(st, "ejected-unmerged", "auto-land %s: %s is RED, "
                           "blame ejected lane %s, and no other car merged in "
                           "its b-room %s; nothing is left to land."
                           % (st["name"], red, car.get("lane"), broom))
            return self.archive(st, ABANDONED, "blame ejected lane %s and no "
                                "other car merged" % car.get("lane"))
        if len(merged) < len(kept):
            self.say("NOT MERGED in %s, riding the next train: lane %s" % (
                os.path.basename(broom), ", ".join(
                    c["lane"] for c in kept if c not in merged)))
        kept = merged
        st["rooms"].append(broom)
        st["room"], st["head"], st["cars"] = broom, head, kept
        st["name"] = os.path.basename(broom)
        # A RED AUDIT EJECTS FROM COMPOSING: the b-room is gated either way.
        st["state"], st["step"] = GATING, None
        st["launched"] = rc == 0
        st["launch_ts"] = self.now
        st["gate"] = {"host": self.ops.gate_host(broom), "job_id": None}
        self.save(st, "%s RED — ejected lane %s; gating %s"
                  % (red, car.get("lane"), st["name"]))
        self.say("EJECTED lane %s; GATING %s at %s" % (
            car.get("lane"), st["name"], _short(head)))
        return 0

    # -- LANDING ------------------------------------------------------------
    def landing(self, st):
        rec = st["receipt"]
        gid, head = rec["id"], st["head"]
        step = st.get("step")
        if not self.apply:
            return self.would("take %s's land from step %s" % (st["name"],
                                                               step))
        # the destination this tick's foldcheck and push guard vetted, or
        # None until they have run in this tick
        checked = None
        if step == "verified":
            checked, why = self.prepush(head, gid)
            if why:
                return self.stop(st, why)
            st["step"] = step = "pushing"
            self.save(st, "foldcheck dry and the push guard pass")
        if step == "pushing" and st.get("sending"):
            # A PUSH A KILLED TICK SENT, its answer never recorded: never
            # pushed again by itself, only read under the three locks
            rc = self.unanswered(st, head)
            if rc is not None:
                return rc
            step = st["step"]
        if step == "pushing":
            remote = self.ops.remote_head(self.root)
            if remote != head:
                if remote and self.ops.ancestry(self.root, remote, head) \
                        == vcs.ANCESTOR:
                    # A TRUNK THAT MOVED SINCE THE GATE, a rewind to an
                    # ancestor of head included, is never pushed over (door
                    # read B1): asked here before the locks, and made to
                    # hold at the update itself by the push's lease.
                    if remote != st["trunk"]:
                        return self.stop(st, "%s is not pushed: %s" % (
                            _short(head), self.moved(st, remote)))
                    # THE CHECKS BIND THE PUSH, NOT THE STEP. A push retried
                    # from the recorded step (a refused push and `--resume`,
                    # or a tick that died before its push was sent) asks
                    # foldcheck and the push guard again: the remote's URLs,
                    # its visibility and the receipt can all change while a
                    # train waits.
                    if checked is None:
                        checked, why = self.prepush(head, gid)
                        if why:
                            return self.stop(st, why)
                    rc = self.last_word(st, head, checked)
                    if rc is not None:
                        return rc
                elif not remote or self.ops.ancestry(self.root, head,
                                                     remote) != vcs.ANCESTOR:
                    return self.stop(st, "trunk reads %s, which %s is no "
                                     "fast-forward of and which does not "
                                     "carry it; nothing was pushed"
                                     % (_short(remote), _short(head)))
                # else ALREADY PUSHED: trunk carries the head and no push of
                # it is unanswered (a person's `--resume` after a settle read
                # found trunk moved on past it); the fold is still owed.
            if st["step"] == "pushing":
                st["step"] = "pushed"
                st["pushed_ts"] = self.now
                self.save(st, "pushed %s" % _short(head))
            step = st["step"]
        if step == "pushed":
            ok, why = self.ops.ff(self.root, head)
            if not ok:
                return self.stop(st, "%s is PUSHED, but %s" % (_short(head),
                                                               why))
            st["step"] = step = "ff"
            self.save(st, "the shared checkout is at %s" % _short(head))
        if step == "ff":
            rc, text = self.ops.install_guard(self.root)
            if rc != 0:
                return self.stop(st, "%s is PUSHED, but the rail would not "
                                 "reinstall: %s" % (_short(head),
                                                    _whole(text)))
            st["step"] = step = "guarded"
            self.save(st, "the rail is reinstalled")
        if step == "guarded":
            # THE LAND'S FIRST LEDGER READ, by the landed code, before the fold
            # reads the ledger; recorded once per head (task/3538).
            self.ops.postland(self.root, head)
            fold_rc, text = self.ops.fold_apply(self.root, head, gid)
            if fold_rc != 0:
                return self.stop(st, "%s is PUSHED, but `helm lr foldcheck "
                                 "--apply` exited %d: %s"
                                 % (_short(head), fold_rc, _whole(text)))
            why = fold_unproven(text)
            if why:
                return self.stop(st, "%s is PUSHED, but `helm lr foldcheck "
                                 "--apply` did not prove the fold: %s"
                                 % (_short(head), why))
            st["fold"] = {
                "closed": re.findall(r"^\s+CLOSED\s+(\S+)", text, re.M),
                "refused": re.findall(r"^\s+(?:REFUSED|FAILED)\s+(\S+)", text,
                                      re.M)}
            st["step"] = step = "folded"
            self.save(st, "the fold is proven")
        if step == "folded":
            auth = self.ops.authority(self.root)
            n, why = take_land_number(self.root, head, auth.get("sha") or head,
                                      self.ops, train=st["name"],
                                      now=self.now)
            st["land"], st["land_why"] = n, why
            if why:
                self.post_once(st, "counter", "auto-land %s: %s is PUSHED "
                               "and folded, but the LAND counter refused: %s"
                               % (st["name"], _short(head), why))
            elif not record_land(self.root, n, head, train=st["name"],
                                 gate=st["receipt"]["id"],
                                 ran=st["receipt"]["ran"], now=self.now):
                self.post_once(st, "land-log", "auto-land %s: LAND %d is "
                               "counted, but the land log would not take its "
                               "line, so the morning report reads LAND %d "
                               "MISSING until `%s seed %d %s` records it"
                               % (st["name"], n, n, PROG, n, _short(head)))
            st["step"] = step = "numbered"
            self.save(st, "LAND %s" % (n or "?"))
        if step == "numbered":
            why = self.closes(st)
            if why:
                self.say("the post-land close of %s is retried next tick: %s"
                         % (_short(head), why))
                return 1
            st["step"] = step = "closed"
            self.save(st, "rows and tasks closed")
        if step == "closed":
            if not self.post_once(st, "announce", self.announce_text(st),
                                  integrator=False):
                return 1
            st["step"] = step = "announced"
            self.save(st, "announced")
            self.walk_owed()
        return self.archive(st, DONE, "LAND %s %s" % (st.get("land") or "?",
                                                      _short(head)))

    def prepush(self, head, gid):
        """(target, why): the destination the push guard vets and resolves
        ONCE, then foldcheck dry on `head` asked of that same destination
        (tip-exists, tree-vs-gate and ff-able by rung). `why` says what
        refused, and the target is None beside it."""
        target, why = self.ops.push_target(self.root)
        if not target:
            return None, "the push guard refuses: %s" % why
        rungs = self.ops.foldcheck(self.root, head, gid, target)
        want = ("tip-exists", "tree-vs-gate", "ff-able")
        seen = {r.name: r for r in rungs or ()}
        bad = [n for n in want if n not in seen or seen[n].verdict != "PASS"]
        if bad:
            return None, ("foldcheck refuses the push by rung (%s): %s"
                          % (", ".join(bad), "; ".join(
                              "%s %s" % (n, seen[n].discriminator)
                              for n in bad if n in seen)))
        return target, None

    def last_word(self, st, head, checked):
        """THE LAST WORD BEFORE THE PUSH, and the push, under both locks.

        The auto-land store lock serializes its verbs. The dispatch ledger
        lock (`landwindow.readiness_lock`) makes the final READY/admission
        read and the push one critical section with verdict, hold, retip and
        withdrawal writers, and with every ejection-store writer, which takes
        it first (task/3265 races F2), and with every other writer of a land
        veto or land authority, which takes it too (`landorder`, task/3265
        races R2). The receipt's own land authority is asked after the
        readiness (`land_veto`, task/3265 races F3). The last read of all is
        the destination: a value, not the mutable remote name or trunk ref,
        which must be the one `checked` names, the destination this tick's
        foldcheck and push guard vetted (task/3265 races F1, R1), and which
        the push is pinned to (`_pinned`). The push carries the trunk the
        train was gated on as its lease, so a trunk moved since, by a rewind
        too, is refused by the remote in the update itself (door read B1).
        The push's lock holder inherits every lock this section holds, so
        they outlive a killed tick (`push_locks`, task/3265 races F4) and
        end with git, never with anything git leaves running (door read B3);
        none is ever unlocked, only closed (task/3265 races R3). The pushed
        record lands before either lock is released, and a push git did not
        answer with success is read under them before anything is recorded
        or stopped (`settle`; the codex read, B1 and B3). The push is
        recorded `sending` before git runs and cleared with its outcome, so
        a push whose tick is killed before it records git's answer is read
        by the next tick in the same way, never pushed again by itself
        (`unanswered`)."""
        with self.locked() as held:
            if not held:
                self.say("the auto-land store is locked; the push of %s waits "
                         "for the next tick" % _short(head))
                return 0
            self._check(st)
            halt = self.halted()
            if halt:
                self.say("the push of %s is withheld: %s" % (_short(head),
                                                             halt))
                return 0
            with landwindow.readiness_lock(timeout=5) as ledger_held:
                if not ledger_held:
                    self.say("the dispatch ledger is locked or unreadable; "
                             "the push of %s waits for the next tick"
                             % _short(head))
                    return 0
                rc = self.ready_at_the_push(st, head)
                if rc is not None:
                    return rc
                why = self.land_veto(st, head)
                if why:
                    return self.stop(st, why)
                keep, why = _lock_fds(self.push_locks())
                if why:
                    return self.stop(st, "%s is not pushed: the locks it runs "
                                     "under cannot be handed to the push "
                                     "(%s), so a tick killed mid-push would "
                                     "let them go while its push ran on"
                                     % (_short(head), why))
                # THE LAST READ BEFORE THE PUSH (task/3265 races R1): the
                # destination, resolved again by the read that vets it,
                # after every other read, must be the one checked
                target, why = self.ops.push_target(self.root)
                if not target:
                    return self.stop(st, "the push guard refuses: %s" % why)
                if target != checked:
                    return self.stop(st, "%s is not pushed: the destination "
                                     "read in the last word (%s) is not the "
                                     "one foldcheck and the push guard "
                                     "vetted (%s); the declared trunk "
                                     "changed while the train landed" % (
                                         _short(head), _dest(target),
                                         _dest(checked)))
                # A PUSH IS SENT FROM HERE UNTIL ITS OUTCOME IS RECORDED: a
                # tick killed in between leaves this on the train, and the
                # next tick reads trunk before anything else (`unanswered`)
                st["sending"] = _dest(target)
                self.save(st, "sending %s to %s" % (_short(head),
                                                    _dest(target)))
                ok, detail = self.ops.push(self.root, head, target, keep,
                                           lease=st["trunk"])
                st["sending"] = None
                if ok is None:
                    return self.stop(st, "%s is not pushed, and git was not "
                                     "run: %s" % (_short(head),
                                                  _first(detail)))
                if not ok:
                    rc = self.settle(st, head, target, "git answered %s"
                                     % _whole(detail))
                    if rc is not None:
                        return rc
                st["step"] = "pushed"
                st["pushed_ts"] = self.now
                self.save(st, "pushed %s" % _short(head))
        return None

    def unanswered(self, st, head):
        """A PUSH A KILLED TICK SENT IS AMBIGUOUS TOO, and never pushed again
        by itself (the sibling of the codex read's B1: the same ABA, reached
        through a dead tick instead of a lost answer; the integrator's
        ruling). `last_word` writes `sending` under the three locks just
        before git runs, and whatever records the push's outcome clears it,
        so a train that still carries it was pushed by a tick killed before
        it recorded git's answer: the update may have applied, and been
        rewound since. This tick takes the same three locks (`tick` holds
        the tick's own for the whole tick), reads the destination again,
        which must be the one the push was sent to, and asks the same
        `settle` read under them. -> None when trunk reads the head: the land is recorded PUSHED
        and goes on. Else the tick's answer: a wait for a lock, or the stop.
        A stop before the read keeps `sending`, so a resumed tick reads it
        again and pushes nothing."""
        with self.locked() as held:
            if not held:
                self.say("the auto-land store is locked; the unanswered push "
                         "of %s is read on the next tick" % _short(head))
                return 0
            self._check(st)
            with landwindow.readiness_lock(timeout=5) as ledger_held:
                if not ledger_held:
                    self.say("the dispatch ledger is locked or unreadable; "
                             "the unanswered push of %s is read on the next "
                             "tick" % _short(head))
                    return 0
                said = ("the push of %s is AMBIGUOUS (the tick that sent it "
                        "to %s was killed before it recorded git's answer), "
                        "and trunk is not read: " % (_short(head),
                                                     st["sending"]))
                target, why = self.ops.push_target(self.root)
                if not target:
                    return self.stop(st, said + "the push guard refuses: %s"
                                     % why)
                if _dest(target) != st["sending"]:
                    return self.stop(st, said + "the destination reads %s "
                                     "now" % _dest(target))
                st["sending"] = None
                rc = self.settle(st, head, target, "the tick that sent it "
                                 "was killed before it recorded git's answer")
                if rc is not None:
                    return rc
                st["step"] = "pushed"
                st["pushed_ts"] = self.now
                self.save(st, "pushed %s" % _short(head))
        return None

    def settle(self, st, head, target, answered):
        """AN AMBIGUOUS PUSH NEVER RETRIES BY ITSELF (the codex read, B1 and
        B3; the integrator's ruling). git exited non-zero, timed out or was
        killed, or the tick that ran it was killed before it recorded git's
        answer (`unanswered`), and the update it sent may have applied all
        the same: its answer was lost, or the remote was still finishing it
        when the timeout killed git's group. `answered` says which, in words.
        Called with all three locks held (in `last_word` the holder has
        exited; in `unanswered` the next tick took them), the tick waits
        PUSH_SETTLE_S and reads trunk once through the destination the push
        was pinned to (`Ops.settled_trunk`). -> None when trunk reads `head`:
        the caller records the land PUSHED, and it goes on as a clean one
        does. Else the stop's answer. Trunk at the lease (the trunk the train
        was gated on) is an update that never applied or one that applied and
        was rewound since, and the two read the same; anything else is named
        as read. No later tick pushes a STOPPED train: `--resume` is a
        person's word. The locks are let go only after this read, so nothing
        acts between the read and what it records (nor, in `last_word`,
        between the push and the read); an update that applies after the
        read is the residual this narrows and does not remove (the module
        docstring)."""
        now, why = self.ops.settled_trunk(self.root, target)
        if now == head:
            _history(st, self.now, "%s, and trunk read %s after the settle"
                     % (answered, _short(head)))
            self.say("the push of %s is AMBIGUOUS and trunk reads it after "
                     "the settle, so it is recorded PUSHED: %s"
                     % (_short(head), answered))
            return None
        said = ("the push of %s is AMBIGUOUS, and it is never pushed again by "
                "itself: %s, and after a settle of %s s "
                % (_short(head), answered, PUSH_SETTLE_S))
        if now == st["trunk"]:
            return self.stop(st, said + (
                "trunk reads %s, the trunk this train was gated on. Two "
                "readings fit and cannot be told apart: the update never "
                "applied, or it applied and was rewound since. `%s --resume` "
                "pushes %s again, which lands it again if it was rewound; "
                "`%s --abandon --reason R` ends the train"
                % (_short(now), PROG, _short(head), PROG)))
        if not now:
            return self.stop(st, said + (
                "trunk could not be read (%s), so whether it carries %s is "
                "UNKNOWN" % (why, _short(head))))
        return self.stop(st, said + self.moved(st, now))

    def closes(self, st):
        """Every car's row (an approve closes `landed`, a source-clean hold
        was closed by the fold). A needs-restart car's ops item is posted to
        the integrator, never performed. -> None, or the `CloseFailed` text
        of a close that did not answer: that car is not done, nothing after
        it is closed, and the next tick retries.

        NO TASK IS CLOSED HERE (task/3643, the work-surface design's
        ruling): a land is not a re-read of the whole ask, and closing at
        land re-opened two tasks in one night (task/3626). The car's task is
        named on the merge line and the announcement, and `taskkey
        .lands_by_task` reports it landed; the task's owner closes it."""
        done = st.setdefault("closed", [])
        land = "LAND %s" % (st.get("land") or "?")
        for car in st["cars"]:
            if car["id"] in done:
                continue
            changes, why = self.ops.changes(self.root, st["trunk"],
                                            car["tip"])
            owed = needs_restart(changes) if changes is not None else \
                ["UNKNOWN: its diff could not be read (%s); the integrator "
                 "decides" % why]
            car["restart"] = owed
            if car.get("basis") == "source-clean":
                if car["id"][:12] not in {c[:12] for c in
                                          st["fold"]["closed"]}:
                    self.post_once(st, "fold:%s" % car["id"], "auto-land %s "
                                   "%s: row %s (lane %s) was not closed by "
                                   "`helm lr foldcheck --apply`; close it by "
                                   "hand." % (st["name"], land,
                                              _short(car["id"]), car["lane"]))
            else:
                try:
                    _row, err = self.ops.lr_close(car["id"], not owed,
                                                  "; ".join(owed) or None)
                except CloseFailed as exc:
                    self.post_once(st, "close-failed:%s" % car["id"],
                                   "auto-land %s %s: `helm lr close %s "
                                   "--reason landed` failed and is retried "
                                   "next tick: %s" % (st["name"], land,
                                                      _short(car["id"]), exc))
                    return str(exc)
                if err:
                    self.post_once(st, "close:%s" % car["id"], "auto-land %s "
                                   "%s: `helm lr close %s --reason landed` "
                                   "refused: %s" % (st["name"], land,
                                                    _short(car["id"]), err))
            if owed:
                self.post_once(st, "restart:%s" % car["id"], "auto-land %s "
                               "%s: %s (lane %s) needs a restart — %s. "
                               "Auto-land performs no fleet op; it is a "
                               "seat's act, timed to the seat being idle."
                               % (st["name"], land, car.get("task")
                                  or "row %s" % _short(car["id"]),
                                  car["lane"], "; ".join(owed)))
            done.append(car["id"])
            self.save(st)
        return None

    def announce_text(self, st):
        """The LAND line the owner reads: what the land changed, in its
        cars' plain words, first; the head, the gate and the provenance
        after it."""
        rec, head = st["receipt"], st["head"]
        restart = ["%s: %s" % (c.get("task") or c["lane"],
                               "; ".join(c["restart"]))
                   for c in st["cars"] if c.get("restart")]
        # A receipt record with no `rule` was verified by the Ran rule.
        return ("[MEASURED] @all LAND %s: %s. PUSHED %s (gate:%s whole-suite "
                "OK, Ran %s, %s%+d = AST %+d): %s. Auto-landed as %s with no "
                "integrator action%s%s. CL %d: falsified if %s is not on %s, "
                "or gate:%s does not read OK on its tree." % (
                    st.get("land") or "?",
                    "; ".join(_car_words(c) for c in st["cars"]), head[:11],
                    rec["id"], rec["ran"], "planned "
                    if rec.get("rule") == "planned" else "", rec["delta"],
                    rec["ast"], "; ".join(
                        "%s lane %s (row %s, %s)" % (
                            _task_words(c), c["lane"],
                            _short(c["id"]), _basis_words(c))
                        for c in st["cars"]), st["name"],
                    "; needs restart (posted to the integrator): %s"
                    % "; ".join(restart) if restart else "",
                    "; the LAND counter refused: %s" % st["land_why"]
                    if st.get("land_why") else "", CL, head[:11],
                    st.get("ref") or "trunk", rec["id"]))

    def ready_at_the_push(self, st, head):
        """None when every car of the train is still READY at its exact tip
        and admitted, else the tick's exit code (the integrator's ruling
        R1). The plan is asked again for the train's own cars: `still_ready`
        (the hold or approve at the EXACT tip the train merged) and
        `admitted` (the door tier). A car that is not (a FIX, a withdrawal,
        a new tip, a holder the tier no longer admits since the compose)
        STOPS the train at LANDING/verified, posted to the integrator naming
        the car and why: nothing is pushed, and no car is ejected
        automatically; `--resume` asks it all again from `verified`. A plan
        that cannot be read is a refusal, retried next tick, never a push."""
        got, why = self.ops.plan(self.root)
        if why or not got:
            return self.refuse("ready:%s" % st["train"], "%s is not pushed: "
                               "whether its cars are still READY cannot be "
                               "asked, because `helm train` could not plan: "
                               "%s" % (st.get("name") or st["train"],
                                       why or "no plan"))
        if got.get("ejections_unknown"):
            return self.refuse("ready:%s" % st["train"], "%s is not pushed: "
                               "the ejection store cannot be read (%s)"
                               % (st.get("name") or st["train"],
                                  got["ejections_unknown"]))
        still, dropped = self.still_ready(st, got, since="the compose")
        mine = {c["id"] for c in still}
        _cars, barred = self.admitted([c for c in got["cars"]
                                       if c["id"] in mine])
        unready = ["lane %s (row %s, %s, tip %s): %s" % (
            d["lane"], _short(d["id"]), self.car_task(st, d["id"]),
            _short(d["tip"]), d["why"]) for d in dropped]
        unready += ["lane %s (row %s, %s, tip %s): not admitted, it is %s"
                    % (car["lane"], _short(car["id"]),
                       self.car_task(st, car["id"]), _short(car["tip"]), why)
                    for car, why in barred]
        if not unready:
            return None
        st["step"] = "verified"
        return self.stop(st, "%s is not pushed: %d car(s) no longer READY at "
                         "the tip the train merged — %s. No car is ejected "
                         "automatically: cure it and `%s --resume` (the "
                         "readiness is asked again from verified), or "
                         "`%s --abandon --reason R`" % (
                             _short(head), len(unready), "; ".join(unready),
                             PROG, PROG))

    @staticmethod
    def moved(st, remote):
        """The words for a trunk that reads `remote`, not the trunk the
        train was composed and gated on (door read B1)."""
        return ("trunk reads %s, not %s, the trunk this train was composed "
                "and gated on: it moved since (a rewind to an ancestor of the "
                "head is one way), and a push over it would land again what "
                "the move took off. `%s --resume` once trunk reads %s again, "
                "or `%s --abandon --reason R`"
                % (_short(remote), _short(st["trunk"]), PROG,
                   _short(st["trunk"]), PROG))

    def push_locks(self):
        """The locks a push holds until its git ends, even when its tick is
        killed (task/3265 races F4), in the push's lock holder and nowhere
        else past the tick (`vcs.GitVcs.run_holding`): the tick's (no tick
        starts, and no `--abandon` ends a train LANDING past verified), the
        store's (no verb writes the train) and the readiness lock (no
        verdict, hold, retip, withdrawal or ejection is written)."""
        return (_lock_path(self.root, "tick"), _lock_path(self.root, "state"),
                landwindow.readiness_lock_path())

    def land_veto(self, st, head):
        """Why `head` must not be pushed on its receipt now, or None: THE
        VETO ASKED BESIDE THE PUSH (task/3265 races F3). The canary's DISABLE
        marker is a veto on every land a sliced receipt authorizes, and it can
        be written after the last foldcheck read the receipt as authorizing.
        So the receipt's whole land authority (`Ops.verify`: foldcheck's
        tree-vs-gate rung as a land reads it, which asks the marker, the
        canary's standing and the placing door) is asked again here, inside
        both locks and after every other read, and a veto seen here aborts
        with nothing pushed; `--resume` asks it all again."""
        gid = st["receipt"]["id"]
        ok, why = self.ops.verify(self.root, head, gid)
        if ok:
            return None
        return ("%s is not pushed: gate:%s no longer authorizes this land, "
                "asked again beside the push: %s" % (_short(head), gid, why))

    @staticmethod
    def car_task(st, rid):
        return next((c.get("task") for c in st.get("cars") or ()
                     if c["id"] == rid), None) or "no task"

    def halted(self):
        """Why a push must not go now, read under the store's lock: the
        switch was thrown (or cannot be read) since this tick began."""
        control, why = read_control(self.root)
        if why:
            return "the switch is unreadable (%s)" % why
        paused = control.get("paused")
        if paused:
            return "paused by %s (%s); `%s --resume` lands it" % (
                paused.get("by"), paused.get("reason") or "no reason given",
                PROG)
        return None

    # -- STOPPED ------------------------------------------------------------
    def stopped(self, st):
        stop = st.get("stopped") or {}
        self.say("STOPPED %s at %s%s: %s. `%s --resume` retries; `%s "
                 "--abandon --reason R` ends it." % (
                     st.get("name") or st["train"], stop.get("state"),
                     "/" + stop["step"] if stop.get("step") else "",
                     stop.get("why"), PROG, PROG))
        if self.apply:
            self._stop_post(st)
        return 0


def tick(repo, apply=False, ops=None, out=None):
    """One tick. -> exit code (0 acted, waited or had nothing to do; 1 a
    refusal or an unreadable input)."""
    out = out if out is not None else sys.stdout
    ops = ops or Ops()
    root = _lanes.find_root(repo)
    if not root:
        print("%s: %s is not inside a git repository" % (PROG, repo),
              file=out)
        return 1
    run = _Tick(root, apply, ops, out)
    if not apply:
        return run.run()
    with _flock(_lock_path(root, "tick"), 0) as held:
        if not held:
            print("%s: another tick holds the lock; this one does nothing"
                  % PROG, file=out)
            return 0
        return run.run()


# ------------------------------------------------------------ status, verbs

def status(root, out=None):
    out = out if out is not None else sys.stdout
    control, why = read_control(root)
    lines = ["%s: %s" % (PROG, root)]
    if why:
        lines.append("  switch: UNKNOWN (%s)" % why)
    elif control.get("paused"):
        p = control["paused"]
        lines.append("  switch: PAUSED by %s: %s" % (p.get("by"),
                                                     p.get("reason") or
                                                     "no reason given"))
    else:
        lines.append("  switch: ON")
    st, why = active(root)
    if why:
        lines.append("  train: UNKNOWN (%s)" % why)
    elif not st:
        lines.append("  train: none (IDLE)")
    else:
        lines.append("  train: %s (%s) — %s%s" % (
            st.get("name"), st.get("train"), st.get("state"),
            "/" + st["step"] if st.get("step") else ""))
        if st.get("state") == STOPPED:
            lines.append("    stopped: %s" % (st.get("stopped") or {}).get(
                "why"))
        if st.get("room"):
            lines.append("    room: %s at %s" % (st["room"],
                                                 _short(st.get("head"))))
        for car in st.get("cars") or ():
            lines.append("    car: lane %s, %s, tip %s, row %s" % (
                car["lane"], _task_words(car),
                _short(car["tip"]), _short(car["id"])))
        for note in (st.get("history") or [])[-5:]:
            lines.append("    %s %s" % (time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(note.get("ts") or 0)),
                note.get("note")))
    for done in archived(root)[-3:]:
        lines.append("  ended: %s %s%s" % (done.get("name") or
                                           done.get("train"),
                                           done.get("state"),
                                           " (LAND %s)" % done["land"]
                                           if done.get("land") else ""))
    counter = read_counter(root)
    lines.append("  LAND counter: %s" % (
        "%s = %s" % (counter.get("n"), _short(counter.get("sha")))
        if counter else "not seeded (`%s seed <n> <sha>`)" % PROG))
    lines.append("  timer: %s" % ("installed" if timer_installed() else
                                  "not installed (`%s --install-timer`)"
                                  % PROG))
    print("\n".join(lines), file=out)
    return 0


def _who():
    return home.chat_name() or "user:%s" % getpass.getuser()


def _root_of(repo, prog):
    root = _lanes.find_root(repo or os.getcwd())
    if not root:
        print("%s: %s is not inside a git repository" % (prog, repo
                                                         or os.getcwd()),
              file=sys.stderr)
    return root


def cmd(args):
    """helm train auto [--repo PATH] [--apply] [--status] [--pause [--reason
    R] | --resume | --abandon [--force] --reason R] | seed <n> <sha> |
    --install-timer"""
    from .cli import guard_tail
    args = list(args or ())
    if args[:1] == ["seed"]:
        return _cmd_seed(args[1:])
    flags = ("--apply", "--status", "--pause", "--resume", "--abandon",
             "--force", "--install-timer")
    rc = guard_tail(PROG, args, flags=flags, valued=("--repo", "--reason"),
                    usage=USAGE)
    if rc is not None:
        return rc
    acts = [a for a in ("--status", "--pause", "--resume", "--abandon",
                        "--install-timer") if a in args]
    if len(acts) > 1 or (acts and "--apply" in args):
        print("%s: %s take no other action (%s)" % (
            PROG, " and ".join(acts + (["--apply"] if "--apply" in args
                                       else [])), USAGE), file=sys.stderr)
        return 2
    if "--force" in args and "--abandon" not in args:
        print("%s: --force goes only with --abandon (%s)" % (PROG, USAGE),
              file=sys.stderr)
        return 2
    opts = {a: args[i + 1] for i, a in enumerate(args)
            if a in ("--repo", "--reason")}
    reason = " ".join(str(opts.get("--reason") or "").split()) or None
    if "--install-timer" in args:
        ok, detail = ensure_timer()
        print("%s: %s" % (PROG, detail),
              file=sys.stderr if ok is False else sys.stdout)
        return 1 if ok is False else 0
    root = _root_of(opts.get("--repo"), PROG)
    if not root:
        return 1
    if "--status" in args:
        return status(root)
    if "--pause" in args:
        _row, why = pause(root, _who(), reason)
        print("%s: %s" % (PROG, why or "PAUSED — no tick acts until "
                          "`%s --resume`" % PROG),
              file=sys.stderr if why else sys.stdout)
        return 1 if why else 0
    if "--resume" in args:
        said, why = resume(root, _who())
        print("%s: %s" % (PROG, why or said),
              file=sys.stderr if why else sys.stdout)
        return 1 if why else 0
    if "--abandon" in args:
        if not reason:
            print("%s: --abandon records why: --reason TEXT (%s)"
                  % (PROG, USAGE), file=sys.stderr)
            return 2
        st, why = abandon(root, _who(), reason, force="--force" in args)
        if why:
            print("%s: REFUSED — %s" % (PROG, why), file=sys.stderr)
            return 1
        gone = st["abandoned"]
        print("%s: %s ABANDONED%s%s" % (
            PROG, st.get("name") or st["train"],
            "; rooms left: %s" % "; ".join(gone["rooms_left"])
            if gone["rooms_left"] else "",
            "; OWED, by hand: `%s`" % "`, then `".join(gone["owed"])
            if gone.get("owed") else ""))
        if gone.get("post_failed"):
            print("%s: the post of what is owed FAILED (%s); tell the "
                  "integrator by hand" % (PROG, gone["post_failed"]),
                  file=sys.stderr)
        return 0
    return tick(root, apply="--apply" in args)


def _cmd_seed(args):
    """helm train auto seed <n> <sha> [--repo PATH]: the LAND counter's last
    land, checked to be a commit trunk carries, and its line in the land log.

    A LAND OLDER THAN THE COUNTER'S is recorded and moves nothing: a sha that
    is a proper ancestor of the counter's own is a land the counter already
    counted past (a hand land nobody seeded, named MISSING by `helm brief
    --report`), so only its land-log line is written, and its number must be
    below the counter's. Any other sha seeds the counter as it always did."""
    from .cli import guard_tail
    if args[:1] in (["-h"], ["--help"]):
        print(SEED_USAGE)
        return 0
    if len(args) < 2 or not _COUNT.match(args[0]) \
            or not _SHA_TOKEN.match(args[1].lower()):
        print("%s: name the last land's number and its sha (%s)"
              % (PROG, SEED_USAGE), file=sys.stderr)
        return 2
    rc = guard_tail("%s seed" % PROG, args[2:], valued=("--repo",),
                    usage=SEED_USAGE)
    if rc is not None:
        return rc
    rest = args[2:]
    root = _root_of(rest[rest.index("--repo") + 1] if "--repo" in rest
                    else None, PROG)
    if not root:
        return 1
    ops = Ops()
    be = vcs.backend(root)
    rc, sha, _err = be.text(root, "rev-parse", "--verify", "-q",
                            args[1].lower() + "^{commit}",
                            env=landwindow._env())
    if rc != 0 or not _SHA.match(sha):
        print("%s: %s is not a commit here" % (PROG, args[1]),
              file=sys.stderr)
        return 1
    auth = ops.authority(root)
    if not auth.get("sha") or ops.ancestry(root, sha, auth["sha"]) \
            != vcs.ANCESTOR:
        print("%s: REFUSED — %s is not on trunk (%s)" % (
            PROG, _short(sha), auth.get("why") or "not an ancestor of %s"
            % _short(auth.get("sha"))), file=sys.stderr)
        return 1
    n, counter = int(args[0]), read_counter(root) or {}
    last = counter.get("sha") if isinstance(counter.get("n"), int) else None
    if last and _SHA.match(str(last)) and last != sha \
            and ops.ancestry(root, sha, last) == vcs.ANCESTOR:
        if n >= counter["n"]:
            print("%s: REFUSED — %s is older than the counter's LAND %d = "
                  "%s, so its number is below %d" % (
                      PROG, _short(sha), counter["n"], _short(last),
                      counter["n"]), file=sys.stderr)
            return 1
        if not record_land(root, n, sha, by=_who(), seeded=True):
            print("%s: the land log (%s) would not take LAND %d"
                  % (PROG, land_log_path(root), n), file=sys.stderr)
            return 1
        print("%s: LAND %d = %s is in the land log; the counter stays at "
              "LAND %d = %s, so the next land is LAND %d"
              % (PROG, n, _short(sha), counter["n"], _short(last),
                 counter["n"] + 1))
        return 0
    row = seed_counter(root, n, sha, by=_who())
    print("%s: the LAND counter reads %d = %s; the next land is LAND %d"
          % (PROG, row["n"], _short(sha), row["n"] + 1))
    if not record_land(root, n, sha, by=_who(), seeded=True):
        print("%s: the land log (%s) would not take LAND %d, so the morning "
              "report reads it MISSING" % (PROG, land_log_path(root), n),
              file=sys.stderr)
        return 1
    return 0


def cmd_veto(args):
    """helm train veto <train> --reason TEXT [--repo PATH]"""
    from .cli import guard_tail
    args = list(args or ())
    if args[:1] in (["-h"], ["--help"]):
        print(VETO_USAGE)
        return 0
    if not args or args[0].startswith("-"):
        print("%s: name the train (%s)" % (VETO_PROG, VETO_USAGE),
              file=sys.stderr)
        return 2
    train, rest = args[0], args[1:]
    rc = guard_tail(VETO_PROG, rest, valued=("--reason", "--repo"),
                    usage=VETO_USAGE)
    if rc is not None:
        return rc
    opts = {a: rest[i + 1] for i, a in enumerate(rest)
            if a in ("--reason", "--repo")}
    reason = " ".join(str(opts.get("--reason") or "").split())
    if not reason:
        print("%s: a veto records why: --reason TEXT (%s)"
              % (VETO_PROG, VETO_USAGE), file=sys.stderr)
        return 2
    root = _root_of(opts.get("--repo"), VETO_PROG)
    if not root:
        return 1
    who = _who()
    st, why = veto(root, train, who, reason)
    if why:
        print("%s: REFUSED — %s" % (VETO_PROG, why), file=sys.stderr)
        return 1
    print("%s: %s is VETOED by %s: %s. The next tick ends it; nothing is "
          "composed." % (VETO_PROG, train, st["veto"]["by"],
                         st["veto"]["reason"]))
    return 0


# ---------------------------------------------------------------- the timer

SERVICE_NAME = "helm-autoland.service"
TIMER_NAME = "helm-autoland.timer"
INTERVAL_S = 120
#: THE SWITCH for a host that must not run the cadence: off means no unit
#: file is written and no systemctl runs.
TIMER_ENV = "HELM_AUTOLAND_TIMER"
TIMER_OFF_VALUES = ("0", "off", "no", "false")

# KillMode=process (task/3265 door read B2): a stop or restart of the unit
# signals the tick alone. The default, control-group, killed the push's lock
# holder and git with it, which let every lock go while an update git had
# already sent could still land; the holder keeps them until git ends, and
# git's own alarm bounds that.
_SERVICE = """[Unit]
Description=helm auto-land: drive one landing train a state per tick

[Service]
Type=oneshot
KillMode=process
WorkingDirectory=%(cwd)s
Environment=HELM_CHAT_NAME=auto-land
UnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID CODEX_SESSION_ID
ExecStart=%(helm)s train auto --apply --repo %(cwd)s
Nice=10
"""

_TIMER = """[Unit]
Description=helm auto-land cadence

[Timer]
OnBootSec=300
OnUnitActiveSec=%(interval)ds

[Install]
WantedBy=timers.target
"""


def timer_units(interval=INTERVAL_S, inputs=None):
    """(service_path, service_text, timer_path, timer_text): the stable
    binary, and the working directory folded back to the shared checkout,
    never a lane's disposable room. `inputs` replaces per-install values
    (timerhealth.unit_values), which is how the drift census renders it."""
    from . import timerhealth, work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    udir = timerhealth.user_unit_dir()
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % timerhealth.unit_values({"helm": helm_bin, "cwd": cwd},
                                               inputs),
            os.path.join(udir, TIMER_NAME),
            _TIMER % timerhealth.unit_values({"interval": interval}, inputs))


def timer_switched_off():
    value = os.environ.get(TIMER_ENV)
    if value is not None and value.strip().lower() in TIMER_OFF_VALUES:
        return value
    return None


def timer_installed():
    return os.path.exists(timer_units()[2])


def ensure_timer(interval=INTERVAL_S):
    """(ok, detail): install and enable the two-minute cadence through the
    shared installer. ok is None when TIMER_ENV switched it off."""
    import shutil
    from . import timerhealth
    off = timer_switched_off()
    if off is not None:
        return None, ("install skipped by %s=%s: no unit file written, no "
                      "systemctl run" % (TIMER_ENV, off))
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `%s --apply` every %d s "
                       "from another scheduler" % (PROG, interval))
    spath, service, tpath, timer = timer_units(interval)
    error, unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (TIMER_NAME,), systemctl,
        subprocess, keep_unchanged=True, reload_unchanged=False)
    if error:
        return False, error
    return True, "%s every %d s (%s)" % (
        "already installed, unchanged," if unchanged else "installed",
        interval, tpath)
