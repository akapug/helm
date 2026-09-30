#!/usr/bin/env python3
"""THE DISPATCH FOLD AS A MAINTAINED CHECKPOINT, NEVER A WHOLE-LEDGER REPLAY.

THE DEFECT, MEASURED (task/2770). `dispatches.snapshot()` replayed every event
of the dispatch ledger on every call: 17,539 events and 12.2 MB, 15.8 s warm,
about 13 s of it in 1,018 git spawns re-proving 127 `carried` closes that were
all older than a day. The stop guard's dispatch-ledger rung yielded at its
7.5 s wall on every stop, so every stop reported COVERAGE UNKNOWN; `lr close`
took about 70 s. A read side has to be a maintained index, not a replay.

WHAT IS STORED. The WHOLE fold at an absolute event position K — every
accumulator `dispatches._fold_into` keeps (the row states, the accepted-verdict
index, the positions of every event the fold took, and the per-seat activity
index) — plus K and the byte offset where event K ends. A reader restores it
and folds ONLY the events after K, with positions continuing from K, because
`close_seq`, verdict indices and gate-epoch comparisons are ledger positions.

THE KEY: ALL OF IT MUST HOLD, OR THE READ IS A FULL REPLAY.
  * the ledger PREFIX: sha256 of the bytes up to the offset, taken from the
    same bytes the tail is then parsed from (`eventledger.read_bytes`). A
    rewritten, truncated or compacted ledger misses here.
  * the CODE: a digest of every .py file under this package, taken only while
    the files still match the ones this process imported (`policy`). Three
    closes flipped in one night on validator drift, so a hand-bumped constant
    is not enough; any deploy invalidates once.
  * the GATE-EPOCH MARKER's bytes, read through the module's own path.
  * the RESULT of every `os.path.realpath` the fold's close arms took.
  * THE HOME REPOSITORY, when the fold asked for it (`home_repo_id`): the
    repository the RUNNING package belongs to, which a legacy row with no
    `repo_id` resolves to in the chain join a source-clean close's replay
    runs (task/3053). Two checkouts running identical code share a code
    digest and can belong to different repositories, so the answer is part
    of the key and every restore re-asks it.
  * THE EPOCH LENS, when one owns `gate_epoch` for the fold. The fold
    consumes the epoch while it validates a close, so the term a lens serves
    is part of the policy exactly as the code is: it names the checkpoint's
    FILE, so a lensed fold and a plain one over one ledger keep separate
    stores, and it is re-compared on every read and at the save (a fold the
    lens answered with a second term is refused). A lensed fold is keyed only
    when the lens agrees with the marker-derived epoch, so no store ever holds
    a fold judged against a boundary no file backs.
  * THE LIVE-CONTINGENT DECISIONS. Every git question the fold asked — seen at
    the one door every seamed read passes (`vcs.observed`), memo and
    `gitfacts` answers included — is recorded with its answer, and re-verified
    on every read by ONE `git cat-file --batch-check` per repository over every
    object and ref expression the fold used (each answer line must be byte-equal
    to the recorded one, so a pruned object, an object that reappeared and a
    trunk that moved all miss), plus a fingerprint of what git's merge machinery
    reads beside the objects: the git binary, shallowness, the config it lists,
    replacement refs, and the attributes and grafts files.
  * THE FINGERPRINTS NAME THE FILE (`checkpoint_name`, `git_key`), beside
    the code and the lens: ONE definition decides both which file a reader
    uses and whether it may use it. Each is taken in the env the checkpoint
    records, the one the fold's own git ran under, never the ambient one. So
    seats git answers identically share a file, and seats git answers
    differently never can: neither replays against, replaces or starves the
    other's checkpoint.
A land moves trunk, so one full replay per land is expected; between lands
every read restores and folds only its tail.

FAIL-SAFE IN ONE DIRECTION. Any read, parse, version or verify failure is a
full replay, never a partial or stale answer. A fold that asked git something
this module cannot re-verify (`Unplannable`), a git read outside the seam, or
a read that stopped inside an event writes NO checkpoint at all, and a refusal
every fold of the same bytes would meet again is named on the session
(`Session.recurs`), so a reader waiting for another fold knows it gains
nothing. A budgeted read that cannot finish saves the events it folded, once,
at an event boundary, and still reports that it did not finish
(`progress_saver`). A failure to WRITE is a breadcrumb through
`record.swallow`, never a failed read.

THE DOOR IS REUSED, NOT COPIED. A reader with its own derived answer to keep
records it with `recording`, plans it with `plan`, and re-verifies it with
`fingerprint`, `batch_check` and `agrees` — the same key, measured the same way
(`helm/carriageckpt.py`, the carriage replay witness's answer). Two
implementations of "does this recorded git answer still stand" would be two
strengths of one proof, and the weaker would define it. `recording_active` is
how such a reader refuses to serve into a fold being recorded here: this
checkpoint keys on the reads a fold TOOK, so an answer served without them
would leave a close keyed on inputs nothing re-verifies.

WHAT IT DOES NOT DO. It never repairs one row: a close whose proof flipped is
not a per-row fact (its `seq` sticks and later rows read it), so doubt about any
recorded input discards the whole checkpoint. It never keys on wall time. And
it is per CODE VERSION on disk — one file per `policy` digest, at most
`MAX_FILES` kept — so a lane's own `./bin/helm` and the hub binary each
maintain their own file instead of invalidating each other's on every read.

AND IT IS PER LEDGER, WHICH IS AN ADDRESS AND NOT A LABEL. Every durable
ledger on this box shares ONE directory, so a store addressed by code and lens
alone gives two ledgers one file: each read finds a header whose prefix digest
belongs to the other ledger, calls it stale, and overwrites it — so neither
fold's checkpoint ever holds and the only symptom is that both readers stay
slow. That is the mutual eviction `checkpoint_name` prevents for the lens,
left open for the ledger, and it is why a SECOND customer could not be added
without this: the defect is invisible while there is only one. So the ledger
names a subdirectory of its own, which also scopes `MAX_FILES` — a busy ledger's
churn must not evict a quiet one's — and its path is a header field the
restore compares, so no digest collision can serve one fold's state to
another fold's reader.
"""
import contextlib
import fcntl
import hashlib
import json
import marshal
import os
import re
import sys
import threading
import time

from . import eventledger, gitfacts, hooklatency, pk, projscope, record, vcs

FORMAT = "helm-ledger-fold-checkpoint"
VERSION = 2
DIRNAME = "ledger-fold"
SUFFIX = ".ckpt"
# One file per CODE VERSION, LENS TERM and GIT KEY (the fingerprints), WITHIN
# ONE LEDGER's directory, so a deploy, a lensed reader, a seat git answers
# differently and a second ledger each maintain their own rather than
# invalidating another's on every read. Each FILE is evicted on its own, by
# RECENCY: its mtime, which a save sets and every successful restore
# refreshes (`Session._recent`), so recency is LAST USE. What `_prune`
# guarantees, and no more:
#   (a) a file used within RETIRE_S survives unless the count or the byte
#       bound forces eviction, and those evict less recently used files first;
#   (b) eviction is exact LRU by last use, up to a refresh skipped because the
#       save lock stayed held past REFRESH_WAIT_S;
#   (c) the directory holds at most MAX_FILES files and MAX_BYTES bytes, the
#       file just saved excepted: it is never removed, even alone over
#       MAX_BYTES.
# Per file, not per group: a lane's ./bin/helm writes the newest plain file
# under its own code all day, so any rule keeping one file per (lens, key)
# took the hub's, the one every seat reads. Retirement by age removes a code
# version, a key (a repository's config was edited) or a lens term nobody
# uses any more; wall time decides only what is deleted, never whether a
# checkpoint is valid.
#
# THE BOUNDS, AND WHY THESE. Eight (code, lens, key) files were measured live
# at once on the hub, 97 MB in all, so MAX_FILES = 24 is three times that for
# lane worktrees. A count alone stops being a size bound as the ledger, and
# so every file, grows; MAX_BYTES stays one, and 384 MiB is under 0.1% of the
# 558 GB free on the store's disk. Per ledger BY CONSTRUCTION, because
# `_prune` lists the ledger's own directory.
RETIRE_S = 24 * 3600
MAX_FILES = 24
MAX_BYTES = 384 * 1024 * 1024
#: How long a restore waits for the save lock to record its use before it
#: skips the refresh; a skip linearises after the holder's prune.
REFRESH_WAIT_S = 1.0

_PKG = os.path.dirname(os.path.abspath(__file__))


def ledger_key(ledger):
    """The store's address for ONE ledger: a digest of its absolute path.

    THE PATH AND NOT THE BASENAME. Two homes hold a `dispatches.jsonl` each,
    and a basename would give them one store whose prefix digest can only ever
    match one of them."""
    return hashlib.sha256(
        os.path.abspath(ledger).encode("utf-8", "surrogateescape")
    ).hexdigest()[:16]


def store_dir(ledger):
    """Where every checkpoint of `ledger` lives, and nothing else's."""
    return os.path.join(os.path.dirname(os.path.abspath(ledger)),
                        ".state", DIRNAME, ledger_key(ledger))


# ---------------------------------------------------------------------------
# the code identity
# ---------------------------------------------------------------------------

def _source_stats():
    """{path under the package: (ino, size, mtime_ns, ctime_ns)} for every .py
    file. ctime is in it because no userspace call restores it."""
    out, cut = {}, len(_PKG) + 1
    for base, dirs, files in os.walk(_PKG):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(base, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                # A SLICE, NOT os.path.relpath: this runs on every read, and
                # relpath's normalisation was four fifths of its cost.
                out[path[cut:]] = (st.st_ino, st.st_size, st.st_mtime_ns,
                                   st.st_ctime_ns)
    return out


# THE TREE THIS PROCESS IMPORTED FROM, taken when the fold's own module loads.
# A long-running process (helm web) keeps executing the code it imported while a
# deploy rewrites the files under it; a digest of the files on disk would then
# name code this process is not running, and a checkpoint written by the old
# code would be served to every new process as if the new code had made it.
_LOADED = _source_stats()
_DIGEST = []
# The load recorder's memo event (helm/gateloads.py MEMO_EVENT, spelled out
# rather than imported): the digest reads every loaded source file for its
# FIRST caller only, so each reuse says so and a recording gate charges it
# with those files too.
_MEMO_EVENT = "helm.memo"

# THE TREE IS COMPARED AT MOST ONCE PER SECOND, AND A MISMATCH IS FINAL
# (task/3039, the design ruling). Every fold read asked `policy()`, and every
# ask stat-walked the ~490 files: 6,198 walks in tests.test_lr_close, 16.6% of
# its profile. The cost of the TTL is bounded and one-sided: a long-running
# process may write ONE checkpoint under its old digest up to POLICY_TTL_S
# after a deploy rewrites the tree, and no new process reads it, because a new
# process names a new digest. Once this process has seen its tree differ it
# never names its code again, even if the files are put back: stricter than
# the per-call check, which answered with the old digest again.
#
# The clock is bound at import so a test that patches `time.monotonic` for
# its own subject does not move this one; an arm about the TTL patches
# `_monotonic` itself.
POLICY_TTL_S = 1.0
_POLICY = {"checked": None, "drifted": False}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_DIGEST": (
        "the digest of the source this process imported, taken once by "
        "design"),
    "_POLICY": (
        "this process's once-a-second drift check of its own source; the "
        "arms about it patch it (test_foldckpt)"),
}
_monotonic = time.monotonic


def _tree_still_loaded():
    """Is the package still the one this process imported? Walks at most
    once per POLICY_TTL_S; False is remembered for the process."""
    if _POLICY["drifted"]:
        return False
    now = _monotonic()
    checked = _POLICY["checked"]
    if checked is not None and 0 <= now - checked < POLICY_TTL_S:
        return True
    if _source_stats() != _LOADED:
        _POLICY["drifted"] = True
        return False
    _POLICY["checked"] = now
    return True


def policy():
    """The identity of the code the fold executes. -> hex, or None when this
    process cannot name it (the tree changed under it since import), in which
    case the caller neither reads nor writes a checkpoint."""
    try:
        if not _tree_still_loaded():
            return None
        if _DIGEST:
            sys.audit(_MEMO_EVENT, "foldckpt.policy", "hit")
        else:
            h = hashlib.sha256(("%s v%d python %s marshal %d\0" % (
                FORMAT, VERSION, sys.version, marshal.version)).encode())
            sys.audit(_MEMO_EVENT, "foldckpt.policy", "miss")
            try:
                for rel in sorted(_LOADED):
                    with open(os.path.join(_PKG, rel), "rb") as f:
                        h.update(rel.encode("utf-8", "surrogateescape")
                                 + b"\0" + hashlib.sha256(f.read()).digest())
            finally:
                sys.audit(_MEMO_EVENT, "foldckpt.policy", "done")
            # THE DIGEST WAS READ OVER TIME, so the files are compared again
            # after it, whatever the TTL says.
            _POLICY["checked"] = None
            if not _tree_still_loaded():
                return None
            _DIGEST.append(h.hexdigest())
        return _DIGEST[0]
    except OSError:
        return None


# ---------------------------------------------------------------------------
# the recorder: what one fold read outside the ledger
# ---------------------------------------------------------------------------

_LOCAL = threading.local()


class Recorder(object):
    """Every outside input one fold consumed. Filled only while `recording`."""

    __slots__ = ("calls", "realpaths", "taints", "epochs", "homes")

    def __init__(self):
        self.calls = []        # (cwd, args, env items, fed stdin, rc, stdout)
        self.realpaths = {}    # path -> what os.path.realpath answered
        self.taints = []       # inputs nothing here can re-verify
        self.epochs = []       # every gate-epoch term a LENS served this fold
        self.homes = []        # every home-repository answer this fold read


def _stack():
    stack = getattr(_LOCAL, "stack", None)
    if stack is None:
        stack = _LOCAL.stack = []
    return stack


@contextlib.contextmanager
def recording():
    """Record every outside input the fold reads on this thread. A nested
    recording hands what it saw to the one around it on exit: the outer
    fold's answer depended on it too."""
    rec = Recorder()
    stack = _stack()
    stack.append(rec)
    try:
        with vcs.observed(_observe):
            yield rec
    finally:
        stack.pop()
        if stack:
            outer = stack[-1]
            outer.calls.extend(rec.calls)
            outer.realpaths.update(rec.realpaths)
            outer.taints.extend(rec.taints)
            outer.epochs.extend(rec.epochs)
            outer.homes.extend(rec.homes)


def recording_active():
    """Is a fold being recorded on this thread?

    A SIBLING STORE MUST NOT SERVE AN ANSWER INTO A FOLD BEING RECORDED. This
    module keys a checkpoint on every git answer the fold read, seen at the
    `vcs.observed` door; an answer served from another store does no git reads
    at all, so the fold would key on a question set that is missing the inputs
    that decided one of its closes and would reuse it after they moved. The
    honest direction is the one every failure here takes: derive live, so the
    reads happen and the recorder sees them.
    """
    return bool(_stack())


def _observe(cwd, args, env, stdin, answer):
    stack = _stack()
    if not stack:
        return
    rec = stack[-1]
    if answer is None:
        rec.taints.append("a git read outside the vcs seam (%s)"
                          % (args[0] if args else "?"))
        return
    rec.calls.append((str(cwd), tuple(str(a) for a in args),
                      tuple(sorted((env or {}).items())),
                      stdin is not None, answer[0], answer[1]))


def realpath(path):
    """`os.path.realpath(path)`, remembered while a fold is recorded."""
    real = os.path.realpath(path)
    stack = _stack()
    if stack:
        stack[-1].realpaths[str(path)] = real
    return real


def home_repo_id():
    """`dispatches.home_repo_id()`, remembered while a fold is recorded.

    THE ONE ENVIRONMENT ANSWER A FOLD MAY READ THAT IS NEITHER A GIT OBJECT
    NOR A REALPATH: which repository the running helm package belongs to. It
    is two raw git spawns outside the `vcs` seam, so without this the fold
    consumed it unseen and a checkpoint could serve one checkout's answer to
    another running identical code (task/3053). Recorded, the key carries it
    and `Session._stale` re-asks it on every restore."""
    from . import dispatches               # DEFERRED — dispatches imports us.
    answer = dispatches.home_repo_id()
    stack = _stack()
    if stack:
        stack[-1].homes.append(answer[0])
    return answer


def _home_now():
    """The home repository NOW, as a key term: [repo_id] — or None when it
    cannot be asked, which no recorded term ever equals."""
    try:
        from . import dispatches           # DEFERRED — dispatches imports us.
        return [dispatches.home_repo_id()[0]]
    except Exception:                      # noqa: BLE001 — unknown is a miss
        return None


def taint(reason):
    """This fold read something no checkpoint can re-verify."""
    stack = _stack()
    if stack:
        stack[-1].taints.append(str(reason))


def epoch_served(identity):
    """A gate-epoch term a LENS answered this fold with, as its key identity.

    The caller names the identity because only it knows the term's vocabulary;
    this module compares the recorded identities against the one the session
    is keyed on and refuses a save that saw any other. A fold under no lens
    records none, so a plain fold's store can never receive a lensed one."""
    stack = _stack()
    if stack:
        stack[-1].epochs.append(str(identity))


# ---------------------------------------------------------------------------
# what each read did, for a caller that times one (`helm/postland.py`)
# ---------------------------------------------------------------------------

_READS = threading.local()

#: What one checkpointed read did (`reads`): restored a checkpoint, missed
#: one (and why), or could not touch the store at all (`begin` said None).
RESTORED = "restored"
MISSED = "missed"
UNKEYED = "unkeyed"


@contextlib.contextmanager
def reads():
    """Yield a list that collects (road, why) for every checkpointed read on
    this thread while it is open: RESTORED, MISSED with the miss's reason, or
    UNKEYED with why no checkpoint could be touched. It only observes: the
    key, the store and the answer are the same without it."""
    prev = getattr(_READS, "log", None)
    log = _READS.log = []
    try:
        yield log
    finally:
        _READS.log = prev


def _told(road, why=None):
    log = getattr(_READS, "log", None)
    if log is not None:
        log.append((road, why))


# ---------------------------------------------------------------------------
# the plan: which git answers a checkpoint must re-verify
# ---------------------------------------------------------------------------

_OID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OPERAND = re.compile(r"[^\s-]\S*\Z")
_MERGE_BASE = "--merge-base="
PRESENT = "present"
MISSING = "missing"

# THE QUESTIONS THE CARRIED-CLOSE WITNESSES ASK (`dispatches.carriage_proof`,
# `rowworld._carriage`, `rowworld._reached_trunk`) AND NOTHING ELSE. Each one's
# answer is a function of the objects its operands name, the git that computes
# it and the config and attributes that git reads — which is exactly what the
# re-verification covers. Leading words -> (the exit codes that are ANSWERS,
# how many revision operands follow). Any other git question in a fold is
# `Unplannable` and that fold writes no checkpoint: an allowlist, because a
# question admitted wrongly is a stale answer served forever, and one refused
# wrongly is only a slower read.
_DERIVED = (
    (("merge-base", "--is-ancestor"), (0, 1), 2),
    (("cherry",), (0,), 2),
    (("ls-tree", "-r", "-z"), (0,), 1),
    (("diff", "--raw", "-z", "--abbrev=40", "--no-renames"), (0,), 2),
    (("merge-tree", "--write-tree"), (0, 1), 3),
)
# The two that run git's merge/diff machinery, whose answers attributes move.
_MACHINERY = ("merge-tree", "diff")


class Unplannable(Exception):
    """A recorded input whose answer this module cannot re-verify."""


class Unsettled(Unplannable):
    """A refusal about ONE MOMENT of a fold: a git read that did not finish,
    or an input that moved while the fold read it. The next fold of the same
    bytes may not meet it. Every other `Unplannable` is a property of what
    the fold asks, so every fold that asks it again is refused again
    (`Session.recurs`)."""


def _text(out):
    if isinstance(out, bytes):
        return out.decode("utf-8", "surrogateescape").strip()
    return str(out or "").strip()


def _classify(args, rc, out):
    """("shallow", answer) or ("exprs", {expression: observation}) for ONE
    recorded call, where an observation is MISSING, PRESENT, the full id it
    resolved to, or None for "an operand the call needed to exist"."""
    if rc == -1:
        raise Unsettled("a git read did not complete (%s)"
                        % (args[0] if args else "?"))
    text = _text(out)
    if args == ("rev-parse", "--is-shallow-repository"):
        if text in ("true", "false"):
            return "shallow", text
    elif len(args) == 3 and args[:2] == ("cat-file", "-e") \
            and _OPERAND.match(args[2]):
        if rc == 0:
            return "exprs", {args[2]: PRESENT}
        if rc in (1, 128):     # 1 for a bare absent id, 128 for a failed peel
            return "exprs", {args[2]: MISSING}
    elif len(args) == 4 and args[:3] == ("rev-parse", "--verify", "--quiet") \
            and _OPERAND.match(args[3]):
        if rc == 0 and _OID.match(text):
            return "exprs", {args[3]: text}
        if rc == 1 and not text:
            return "exprs", {args[3]: MISSING}
    elif len(args) == 2 and args[0] == "rev-parse" \
            and _OPERAND.match(args[1]) and args[1].endswith("^{tree}"):
        if rc == 0 and _OID.match(text):
            return "exprs", {args[1]: text}
        if rc == 128:
            return "exprs", {args[1]: MISSING}
    else:
        for lead, answers, arity in _DERIVED:
            if args[:len(lead)] != lead:
                continue
            operands = list(args[len(lead):])
            if len(operands) != arity or rc not in answers:
                break
            if lead[0] == "merge-tree":
                if not operands[0].startswith(_MERGE_BASE):
                    break
                operands[0] = operands[0][len(_MERGE_BASE):]
            if all(_OPERAND.match(o) for o in operands):
                return "exprs", dict.fromkeys(operands)
            break
    raise Unplannable("git %s (rc %s) is not a question this checkpoint can "
                      "re-verify" % (" ".join(args[:2]), rc))


def _merge_observation(have, new):
    """One expression observed twice in one fold. The more specific answer
    wins; two answers that disagree mean git moved DURING the fold."""
    if have == new or new is None:
        if have == MISSING and new is None:
            raise Unsettled("an operand was used after git called it missing")
        return have
    if have is None:
        if new == MISSING:
            raise Unsettled("an operand was used after git called it missing")
        return new
    if MISSING in (have, new):
        raise Unsettled("git answered one question two ways during the fold")
    if have == PRESENT:
        return new
    if new == PRESENT:
        return have
    raise Unsettled("git resolved one expression to two objects in one fold")


def plan(rec):
    """{cwd: {"env", "exprs", "shallow", "machinery"}} for everything one
    recorded fold asked git. Raises `Unplannable` for anything it cannot
    re-verify, including every taint."""
    if rec.taints:
        raise Unplannable(rec.taints[0])
    out = {}
    for cwd, args, env, fed, rc, stdout in rec.calls:
        if fed:
            raise Unplannable("a git read fed stdin (%s)" % args[0])
        entry = out.setdefault(cwd, {"env": env, "exprs": {}, "shallow": None,
                                     "machinery": False})
        if entry["env"] != env:
            raise Unplannable("one repository was read under two environments")
        kind, found = _classify(args, rc, stdout)
        if kind == "shallow":
            if entry["shallow"] not in (None, found):
                raise Unsettled("shallowness changed during the fold")
            entry["shallow"] = found
            continue
        if args[0] in _MACHINERY:
            entry["machinery"] = True
        for expr, obs in found.items():
            entry["exprs"][expr] = obs if expr not in entry["exprs"] \
                else _merge_observation(entry["exprs"][expr], obs)
    return out


def agrees(expr, obs, line):
    """Does one `--batch-check` line say what a recorded read observed?

    PUBLIC BECAUSE THE PLAN IS. A sibling that records its own derivation
    through `recording` and `plan` holds the same {expression: observation}
    map this module builds, and the comparison against a batch-check line is
    the half that decides whether the recorded answer still describes the
    world. A second implementation of it would be a second, weaker reading of
    the same evidence — the failure `rowworld._carriage`'s own docstring
    records for a duplicated relation — so there is one.
    """
    if line == expr + " missing":
        return obs == MISSING
    head = line.split(" ", 1)[0]
    if not _OID.match(head) or line.endswith(" ambiguous"):
        return False
    return obs in (PRESENT, None) or obs == head


# ---------------------------------------------------------------------------
# re-verification: one batch-check and one fingerprint per repository
# ---------------------------------------------------------------------------

# `branch.<name>.*` is the one config family lane creation rewrites all day
# (upstream tracking for every new lane branch), and no question in `_DERIVED`
# resolves `@{upstream}` or names a branch implicitly. Hashing it would discard
# the checkpoint on every lane a seat opens.
#
# `credential.*` configures how git AUTHENTICATES to a remote, and no question
# in `_DERIVED` contacts one: every one of them reads local objects. Orca
# launches its seats with `GIT_CONFIG_COUNT=2` naming `credential.interactive`
# and `credential.guiPrompt`, which `git config --list` reports like any
# other entry, so hashing the family gave an Orca seat and a plain seat two
# fingerprints for one repository. Each then judged the other's checkpoint
# stale ("git's view changed") and replaced it through `_keeps`.
#
# Everything else git lists is kept: a precondition that enumerates the config
# surface a git release reads is one the next release edits, so the list of
# what is DROPPED is the short one.
_CONFIG_DROPPED = (b"branch.", b"credential.")
_ATTR_ENV = ("GIT_ATTR_NOSYSTEM", "GIT_ATTR_SOURCE")


def _file_digest(path):
    """sha256 of one file, "absent" when it does not exist. Raises otherwise:
    a file git reads that this cannot read is not a file it can vouch for."""
    try:
        with pk.open_regular(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return "absent"


def _global_attributes(config):
    """The path git reads core.attributesFile from, per git's own default."""
    chosen = None
    for entry in config:
        key, _nl, value = entry.partition(b"\n")
        if key.lower() == b"core.attributesfile":
            chosen = value.decode("utf-8", "surrogateescape")
    if chosen:
        return os.path.expanduser(chosen)
    xdg = os.environ.get("XDG_CONFIG_HOME") or \
        os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "git", "attributes")


def fingerprint(cwd, env):
    """(hex, {"shallow", "inside"}) for what git reads beside the objects in
    one repository, or (None, why).

    A DIRECTORY THAT IS NOT THERE IS A FACT, NOT A FAILURE. A close whose
    repository was deleted asks git once (`cat-file -e`, rc 128) and is refused;
    without this the refusal could never be checkpointed and one deleted clone
    named on the ledger would make every later read a full replay. Its
    fingerprint is the absence itself, so the directory reappearing misses."""
    if not os.path.isdir(cwd):
        return (hashlib.sha256(("absent directory %s" % cwd).encode(
            "utf-8", "surrogateescape")).hexdigest(),
            {"shallow": None, "inside": "false"})
    be = vcs.backend(cwd)
    rc, out, _err = be.run(cwd, "rev-parse", "--is-shallow-repository",
                           "--is-inside-work-tree", "--git-common-dir",
                           env=env)
    lines = _text(out).splitlines()
    if rc != 0 or len(lines) != 3:
        return None, "git could not describe %s" % cwd
    shallow, inside, common = lines
    rc, config, _err = be.run(cwd, "config", "--list", "-z", env=env)
    if rc != 0:
        return None, "git could not list the config of %s" % cwd
    entries = [e for e in config.split(b"\0") if e]
    rc, replaced, _err = be.run(cwd, "for-each-ref",
                                "--format=%(refname) %(objectname)",
                                "refs/replace/", env=env)
    if rc != 0:
        return None, "git could not list the replacement refs of %s" % cwd
    common = os.path.normpath(os.path.join(cwd, common))
    files = (os.path.join(common, "info", "attributes"),
             os.path.join(common, "info", "grafts"),
             _global_attributes(entries), "/etc/gitattributes")
    h = hashlib.sha256()
    h.update(gitfacts._version() or b"git version UNKNOWN")
    h.update(b"\0" + _text(out).encode("utf-8", "surrogateescape"))
    for entry in entries:
        if not entry.lower().startswith(_CONFIG_DROPPED):
            h.update(b"\0config " + entry)
    h.update(b"\0replace " + replaced)
    for path in files:
        h.update(("\0file %s %s" % (path, _file_digest(path))).encode(
            "utf-8", "surrogateescape"))
    for name in _ATTR_ENV:
        h.update(("\0env %s=%r" % (name, os.environ.get(name))).encode(
            "utf-8", "surrogateescape"))
    return h.hexdigest(), {"shallow": shallow, "inside": inside}


def _commit_oid(line):
    """The commit id a `cat-file --batch-check` line names, or None.

    ONLY A COMMIT COUNTS. A missing expression, a tree, a tag or a blob is not
    something ancestry can be asked about, so each takes the None path and its
    caller discards rather than guessing.
    """
    parts = str(line or "").split()
    if len(parts) == 3 and parts[1] == "commit" and _OID.match(parts[0]):
        return parts[0]
    return None


def _advanced_only(cwd, env, recorded, got):
    """None when every expression that MOVED moved to a DESCENDANT of what the
    fold recorded, else the reason this checkpoint is stale.

    THIS IS THE ONE MISMATCH A RESTORE MAY SURVIVE (task/2860). A land moves
    trunk, every recorded ref expression that names it resolves to a new
    commit, and before this the checkpoint was discarded and the next reader
    paid a full cold fold -- measured at 105 to 130 seconds and recurring at
    every land.

    IT IS SOUND BECAUSE OF WHAT THE FOLD READS GIT FOR, WHICH WAS MEASURED
    RATHER THAN ASSUMED. Two instruments agree that the CARRIED close replay
    is the only trunk-dependent thing in the whole fold: a static census
    attributing git calls to each close reason's own branch finds carriage
    only under `carried`, and a cold fold with that one witness stubbed spawns
    ZERO git processes while 1,177 non-carried closes across nine other
    reasons replay unchanged. Everything else is arithmetic over immutable
    ids. And task/2863 made the carried answer stable across exactly this
    move: a proven carried close stays proven when its own work reaches trunk.
    Since task/3056 the carried close is replayed against the trunk commit it
    RECORDED while that commit is still history of trunk, and a descendant
    keeps every commit its ancestor held, so this move cannot send a close
    back to the head.
    `tests/test_foldckpt.py` holds the scenario set, one arm per way trunk can
    move, and the ones this admits say `restored` by name.

    A DESCENDANT, NEVER MERELY A DIFFERENT COMMIT. Trunk being force-moved,
    rewritten or rolled back is not a land and gets no licence here: the
    recorded commit must be an ANCESTOR of the one that replaced it, which is
    what makes "the work the fold proved on trunk is still on trunk" true by
    construction rather than by hope.

    EVERY OTHER SHAPE OF CHANGE DISCARDS, and the refusals are separate on
    purpose: an expression that stopped resolving, one that now names a tree
    or a tag instead of a commit, a recorded commit git can no longer read,
    and a replacement that does not descend are four different facts and a
    reader chasing a slow fold should be told which one it was.
    """
    if got is None:
        return "an object or ref the fold read could not be re-read in %s" % cwd
    moved = [e for e in sorted(recorded) if got.get(e) != recorded[e]]
    if not moved:
        return None
    pairs = set()
    for expr in moved:
        was, now = _commit_oid(recorded[expr]), _commit_oid(got.get(expr))
        if was is None or now is None:
            return ("an object or ref the fold read changed in %s (%s)"
                    % (cwd, expr))
        pairs.add((was, now))
    backend = vcs.backend(cwd)
    for was, now in sorted(pairs):
        # REV-PARSE FIRST. A recorded commit git can no longer read cannot be
        # asked about, and `merge-base --is-ancestor` would answer 128 for it
        # -- an error, not a no. Discarding here keeps that distinction.
        rc, _out, _err = backend.run(cwd, "rev-parse", "--verify", "--quiet",
                                     was + "^{commit}", env=env)
        if rc != 0:
            return ("the trunk %s the fold proved against is gone from %s"
                    % (was[:12], cwd))
        rc, _out, _err = backend.run(cwd, "merge-base", "--is-ancestor",
                                     was, now, env=env)
        if rc == 1:
            return ("%s no longer descends from the %s the fold proved "
                    "against in %s" % (now[:12], was[:12], cwd))
        if rc != 0:
            return ("could not ask whether %s descends from %s in %s"
                    % (now[:12], was[:12], cwd))
    return None


def batch_check(cwd, env, exprs):
    """{expression: its `git cat-file --batch-check` line} from ONE process,
    or None when git did not answer every line."""
    ordered = sorted(exprs)
    if not ordered:
        return {}
    if not os.path.isdir(cwd):
        return {expr: expr + " missing" for expr in ordered}
    rc, out, _err = vcs.backend(cwd).run(
        cwd, "cat-file", "--batch-check", env=env,
        stdin=("\n".join(ordered) + "\n").encode("utf-8", "surrogateescape"))
    if rc != 0:
        return None
    lines = out.decode("utf-8", "surrogateescape").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != len(ordered):
        return None
    return dict(zip(ordered, lines))


# ---------------------------------------------------------------------------
# the session: one read's inputs, the restore, the save
# ---------------------------------------------------------------------------

def _crumb(where, exc):
    record.swallow("foldckpt." + where, exc)


class StoreOverBound(Exception):
    """A prune ended at its pass cap with the store still over MAX_FILES or
    MAX_BYTES. Never raised: the type its breadcrumb carries."""


class CheckpointMiss(Exception):
    """Why one read could not use a checkpoint. Never raised: it is the type
    a miss breadcrumb carries, so a reader of the swallow ledger can tell a
    miss from a fault."""


#: A read that found no checkpoint of its own code while another code's
#: checkpoint is there: the state after every land, and on a lane worktree.
WRITTEN_BY_OTHER_CODE = "written by other code: none for this code"
#: A read that found no checkpoint at all for this ledger.
NO_CHECKPOINT = "no checkpoint for this ledger yet"
#: The ABSENCES are ordinary states, not faults, so they stay out of the
#: swallow ledger, which a flood of expected states makes unreadable.
_ORDINARY = (WRITTEN_BY_OTHER_CODE, NO_CHECKPOINT)
MISS_LOG_CAP = 256 * 1024


def misses_path(ledger):
    """The miss log of `ledger`: one row per checkpoint the fold could not
    use, and why. Beside the store, like the save lock, so the store holds
    checkpoints only."""
    return os.path.join(os.path.dirname(store_dir(ledger)),
                        ledger_key(ledger) + ".misses.jsonl")


def _missed(ledger, code, lens, reason, at="restore"):
    """Write ONE miss where a reader chasing a slow fold will look: the miss
    log beside the store (every process, CLI and hook alike, so misses can be
    COUNTED by reason) and the swallow ledger for every reason that is not an
    ordinary absence. The hook-latency span is told at the restore itself
    (`Session.restore`), in memory.

    CALLED ONLY BY A READER THAT REACHED THE STORE (`Session.save`, past
    its key). A read that saves nothing, such as `helm doctor` over a home
    with no ledger, must leave the tree's bytes exactly as it found them, and
    a miss log is a write; the one mark a read leaves is the recency of the
    checkpoint it restored (`Session._recent`). A read that replayed and came to save records the miss it
    paid for, whether its checkpoint is then written or kept out. Best effort
    in every leg: a miss that cannot be recorded is still a miss, and the
    save goes on."""
    if reason not in _ORDINARY:
        _crumb("miss-" + at, CheckpointMiss(reason))
    try:
        path = misses_path(ledger)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        row = (json.dumps({"ts": pk.now_ts(), "pid": os.getpid(), "at": at,
                           "reason": pk.cut_marked(reason, 240),
                           # the store's own file-name prefix, an identity
                           "policy": str(code or "")[:16],  # noqa: SILENT_CAP — the 16-hex prefix every store file is named by
                           "lens": lens is not None},
                          sort_keys=True) + "\n").encode("utf-8")
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            size = 0
        if size + len(row) > MISS_LOG_CAP:
            os.replace(path, path + ".1")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                     | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, row)
        finally:
            os.close(fd)
    except Exception:                                    # noqa: BLE001
        pass


def doctor_rows(ledger):
    """[(level, msg)] for helm doctor: why the dispatch fold replayed, from the
    miss log. READ-ONLY: it opens the log and nothing else.

    OK with the counts by reason, or with "none recorded" when there is no
    log. WARN when a save REPLACED a checkpoint: its file is named by the
    fingerprints, so a replacement means a key every reader of that file
    shares went stale under it (a rewritten ledger prefix, a pruned object),
    which is a fault to look at and not a reader's ordinary miss."""
    rows = misses(ledger)
    if not rows:
        return [("OK", "fold checkpoint: no miss recorded")]
    counts = {}
    for row in rows:
        reason = str(row.get("reason") or "?")
        counts[reason] = counts.get(reason, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    text = "fold checkpoint: %d miss(es) retained — %s" % (
        len(rows), "; ".join("%d x %s" % (n, r) for r, n in ranked[:4]))
    replaced = sum(n for r, n in counts.items() if r.startswith("replaced: "))
    if replaced:
        return [("WARN", text + " — %d replacement(s) of a checkpoint that "
                 "went stale on a key its readers share" % replaced)]
    return [("OK", text)]


def misses(ledger):
    """[miss row] from the miss log of `ledger`, oldest first, [] when there
    is none. The read API: a census and a test read the same rows."""
    rows = []
    for path in (misses_path(ledger) + ".1", misses_path(ledger)):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return rows


def marker_key(path):
    """The gate-epoch marker's identity: sha256 of its bytes, "absent", or
    None when it exists and cannot be read."""
    try:
        return _file_digest(path)
    except (OSError, ValueError):
        return None


def _state_ok(state, count):
    """The restored payload has the fold's own shape, positions in range."""
    if not isinstance(state, dict) or set(state) != {
            "out", "verdicts", "taken", "actors"}:
        return False
    out, verdicts, taken, actors = (state["out"], state["verdicts"],
                                    state["taken"], state["actors"])
    if not (isinstance(out, dict) and isinstance(verdicts, dict)
            and isinstance(taken, dict) and isinstance(actors, dict)):
        return False
    if set(actors) != {"validated", "unresolved"} \
            or not all(isinstance(v, dict) for v in actors.values()):
        return False
    if not all(isinstance(k, str) and isinstance(v, dict)
               for k, v in out.items()):
        return False
    for pair in verdicts.values():
        if not (isinstance(pair, tuple) and len(pair) == 2
                and type(pair[0]) is int and 0 <= pair[0] < count):
            return False
    for at in taken.values():
        if not isinstance(at, list) or not all(
                type(i) is int and 0 <= i < count for i in at):
            return False
    return True


class Session(object):
    """The inputs ONE checkpointed read is keyed on, each read once."""

    __slots__ = ("ledger", "marker", "data", "end", "code", "epoch", "lens",
                 "why", "git", "fps", "recurs")

    def __init__(self, ledger, marker, data, code, epoch, lens=None):
        self.why = None     # why `restore` missed, written by `save`
        # Why the last `save` wrote nothing, when EVERY fold of these bytes
        # under this code is refused the same way: a taint, a question
        # `plan` cannot re-verify, merge machinery run in a work tree. None
        # when it saved, or when the refusal was about one moment of the
        # fold (`Unsettled`), a key that moved, or anything else.
        self.recurs = None
        self.git = None     # the git section last restored or saved
        self.fps = {}       # (cwd, env) -> this process's fingerprint
        self.ledger = ledger
        self.marker = marker
        self.data = data
        self.end = data.rfind(b"\n") + 1     # past the last COMPLETE line
        self.code = code
        self.epoch = epoch
        self.lens = lens

    def store(self, git=None):
        """The file a checkpoint of this session lives at, for the git
        section `git`: by default the one it last restored or saved, and for
        a session that has done neither, a fold that read no git."""
        if git is None:
            git = self.git or {}
        return os.path.join(store_dir(self.ledger), checkpoint_name(
            self.code, self.lens, git_key(git)))

    def restore(self):
        """(header, state) of this code's checkpoint when EVERY key still
        holds, else None. Never raises except `projscope.Expired`.

        WHICH FILE is decided by the same fingerprints that decide whether it
        is usable: of this code's and lens's files, the ones whose recorded
        fingerprints this process reproduces, judged longest first until one
        holds. A candidate refused on a key its readers share (the ledger
        rewritten shorter than it, a trunk that did not advance) is refused
        for every reader and stays until it retires, because a save subsumes
        only what is no longer than itself; judging it alone made every read
        under its shadow a full replay. The fingerprints are memoised for the
        read (`_fingerprint`), so choosing costs one header line per
        candidate and no git spawn the judgement would not make anyway.

        EVERY None IS A NAMED MISS (`_missed`): a replay nobody can explain
        is the one nobody fixes, and the reason was always computed here and
        then dropped."""
        self.fps = {}
        root = store_dir(self.ledger)
        try:
            names = os.listdir(root)
        except FileNotFoundError:
            names = []
        except OSError as exc:
            _crumb("restore-read", exc)
            self._miss("the checkpoint could not be read (%s)"
                       % type(exc).__name__)
            return None
        mine = []
        for name in names:
            terms = _name_terms(name)
            if terms and terms[2] is not None and terms[:2] == (
                    self.code[:16], _lens_digest(self.lens)):
                path = os.path.join(root, name)
                try:
                    mine.append((os.stat(path).st_mtime_ns, path))
                except OSError:
                    continue
        if not mine:
            self._miss(WRITTEN_BY_OTHER_CODE if any(map(_name_terms, names))
                       else NO_CHECKPOINT)
            return None
        why, usable = [], []
        try:
            for mtime, path in sorted(mine, reverse=True):
                events = self._reproduced(path, why)
                if events is not None:
                    usable.append((events, mtime, path))
            if not usable:
                self._miss(why[0])
                return None
            why, got = [], None
            for _events, _mtime, path in sorted(usable, reverse=True):
                try:
                    with pk.open_regular(path, "rb") as f:
                        blob = f.read()
                except FileNotFoundError:
                    continue             # pruned between the scan and here
                got = self._judged(blob, why)
                if got is not None:
                    break
        except projscope.Expired:
            raise
        except Exception as exc:                         # noqa: BLE001
            _crumb("restore", exc)
            self._miss("the checkpoint could not be judged (%s)"
                       % type(exc).__name__)
            return None
        if got is None:
            self._miss(why[0] if why else "the checkpoint was refused")
            return None
        self.git = got[0]["git"]
        self._recent(path)
        _told(RESTORED)
        return got

    def _recent(self, path):
        """Mark the checkpoint at `path` USED now, so `_prune` evicts by last
        use: a view that only restores never saves, and aged by its last save
        another view's save would retire it while it is read every minute.
        Every successful restore does it; a throttle left a file used now
        older than one saved after it, and LRU evicted the wrong one.

        UNDER THE SAVE LOCK, waited for at most REFRESH_WAIT_S, which is what
        linearises a restore against a prune: the prune runs inside the same
        lock, so the refresh either lands first and the prune's listing sees
        it, or finds the lock held, skips, and the reader already holds its
        state from a file that prune may then remove. It never lands between
        the prune's snapshot and its unlink. A file already gone is no fault.

        THE RECENCY IS THE FILE'S MTIME, and moving it misorders nothing that
        reads it: `_prune`'s LRU wants use, a restore breaks a tie between
        equally long checkpoints by it, and doctor's freshness horizon wants
        use; `_keeps`, `_supersede`, the miss log and doctor's miss rows read
        the header and the log, never the mtime. The bytes do not change."""
        try:
            with _save_lock(self.ledger, wait_s=REFRESH_WAIT_S) as held:
                if held:
                    os.utime(path)
        except OSError:
            pass

    def _reproduced(self, path, why):
        """The event count of the checkpoint at `path` when this process
        fingerprints every repository it read as its writer did, else None
        with the reason appended to `why`. Reads the header line only.

        THE SNAPSHOT CONTRACT. A restore is valid as of the fingerprint this
        scan takes; the judgement reuses it (`_fingerprint`). Every version of
        the fold reads git live after its check, so a config change after
        that point is not detected by any version, and choosing by this scan
        moves the snapshot earlier by at most one header read per candidate.
        A change between the scan and the judgement is that same case."""
        try:
            with pk.open_regular(path, "rb") as f:
                header = json.loads(f.readline().decode("utf-8"))
            stale = self._unseen(header.get("git"))
            events = header["ledger"]["events"]
        except FileNotFoundError:
            return None                  # pruned between the listing and here
        except projscope.Expired:
            raise
        except Exception as exc:                         # noqa: BLE001
            why.append("the checkpoint could not be judged (%s)"
                       % type(exc).__name__)
            return None
        if stale:
            why.append(stale)
            return None
        return events if type(events) is int else 0

    def _unseen(self, git):
        """Why this process does not fingerprint every repository in the
        recorded `git` section as its writer did, or None. Each fingerprint
        is taken IN THE ENV THE SECTION RECORDS, the one the fold's own git
        ran under, so an ambient variable that env removes (GIT_DIR and the
        other selectors) is never part of the answer."""
        if not isinstance(git, dict):
            return "malformed git section"
        for cwd, entry in sorted(git.items()):
            fp = self._fingerprint(cwd, dict(entry["env"]))
            if fp is None or fp != entry["fingerprint"]:
                return "git's view of %s changed" % cwd
        return None

    def _fingerprint(self, cwd, env):
        """`fingerprint(cwd, env)[0]`, once per restore."""
        key = (cwd, tuple(sorted(env.items(), key=repr)))
        if key not in self.fps:
            self.fps[key] = fingerprint(cwd, env)[0]
        return self.fps[key]

    def _miss(self, reason, at="restore"):
        """A restore's miss is told to the hook span now and HELD for the
        save; a save's own finding (`at="replace"`) is written at once,
        because the save is already writing the store."""
        if at != "restore":
            _missed(self.ledger, self.code, self.lens, reason, at)
            return
        _told(MISSED, reason)
        self.why = reason
        try:
            hooklatency.fold_miss(reason)
        except Exception:                                # noqa: BLE001
            pass

    def _judged(self, blob, why=None):
        """(header, state) when checkpoint bytes `blob` are usable for this
        session's ledger bytes, else None, with the reason appended to `why`
        when one is given. May raise. ONE JUDGE, used by the restore and by
        the save's grow-only rule (`_keeps`), so the two can never disagree
        about which checkpoint a reader can use."""
        head, _nl, payload = blob.partition(b"\n")
        header = json.loads(head.decode("utf-8"))
        stale = self._stale(header, payload)
        if stale:
            if why is not None:
                why.append(stale)
            return None
        state = marshal.loads(payload)
        if not _state_ok(state, header["ledger"]["events"]):
            if why is not None:
                why.append("the payload is not a fold of this shape")
            return None
        return header, state

    def _stale(self, header, payload):
        """Why this header no longer describes the world, cheapest key first,
        or None. Every git answer is re-asked; nothing is trusted for age.

        ONE JUDGEMENT FOR A RESTORE AND A SAVE. A save judging a checkpoint
        another reader wrote judges one whose fingerprints name its file, and
        the save's own fingerprints named the same file, so a fingerprint
        that moved there moved for its writer too.

        THE KEYS NO READER CAN PASS COME FIRST (`_universal`), the same
        function a save's sweep removes dead files by (`_bury`), so the two
        can never disagree about which refusal is universal."""
        universal = self._universal(header, len(payload), payload)
        if universal:
            return universal
        if header.get("policy") != self.code:
            return "written by other code"
        if header.get("lens") != self.lens:
            return "folded under a different gate-epoch lens"
        if header.get("epoch") != self.epoch:
            return "the gate-epoch marker moved"
        for path, real in header["realpaths"].items():
            if os.path.realpath(path) != real:
                return "a repository path resolves differently"
        home = header.get("home")
        if home is not None and home != _home_now():
            return "the running helm belongs to another repository"
        git = header.get("git")
        unseen = self._unseen(git)
        if unseen:
            return unseen
        for cwd, entry in git.items():
            env = dict(entry["env"])
            got = batch_check(cwd, env, entry["exprs"])
            if got != entry["exprs"]:
                # A TRUNK THAT ADVANCED IS NOT A TRUNK THAT CHANGED. Every
                # other difference still discards; see `_advanced_only`.
                why = _advanced_only(cwd, env, entry["exprs"], got)
                if why:
                    return why
        return None

    def _universal(self, header, size, payload=None, prefix=None):
        """Why `header` is refused for EVERY reader of this ledger as this
        session read it, or None: a key no reader can ever pass, whatever
        its code, lens or view. `size` is the payload's length in bytes;
        `payload`, when given, is checked against its digest too; `prefix`,
        when given, is the digest of the ledger up to the header's offset.
        Never raises.

        UNIVERSAL: not this format (a malformed header), another ledger's
        path, an offset past this ledger's last complete line or off a line
        boundary, a prefix whose bytes were rewritten, a payload that is not
        the one the header describes, a malformed realpaths or git section.
        NOT UNIVERSAL, and never judged here: the code, the lens, the
        gate-epoch marker, the fingerprints and the objects, since another
        reader, or this one later, may still pass them."""
        if not isinstance(header, dict) or header.get("format") != FORMAT \
                or header.get("v") != VERSION:
            return "not a checkpoint of this format"
        ledger = header.get("ledger")
        if not isinstance(ledger, dict):
            return "not a checkpoint of this format"
        # THE LEDGER'S OWN NAME, BEFORE ANYTHING DERIVED FROM ITS BYTES. The
        # store is addressed per ledger, so this can only fire on a copied,
        # moved or digest-colliding store — which is exactly when a prefix
        # check is no defence, because a fold of ANOTHER ledger that happens
        # to share a prefix would pass it and hand back that ledger's state.
        if ledger.get("path") != os.path.abspath(self.ledger):
            return "the checkpoint describes another ledger"
        count, offset = ledger.get("events"), ledger.get("offset")
        if type(count) is not int or type(offset) is not int \
                or count < 0 or not 0 <= offset <= self.end \
                or (offset and self.data[offset - 1:offset] != b"\n"):
            return "the ledger is shorter than the checkpoint"
        if prefix is None:
            prefix = hashlib.sha256(self.data[:offset]).hexdigest()
        if prefix != ledger.get("sha256"):
            return "the ledger prefix was rewritten"
        body = header.get("payload")
        if not isinstance(body, dict) or body.get("bytes") != size or (
                payload is not None and hashlib.sha256(payload).hexdigest()
                != body.get("sha256")):
            return "the payload is not the one the header describes"
        if not isinstance(header.get("realpaths"), dict):
            return "malformed realpaths"
        git = header.get("git")
        if not isinstance(git, dict) or not all(
                isinstance(e, dict) and "env" in e and "fingerprint" in e
                and "exprs" in e for e in git.values()):
            return "malformed git section"
        return None

    def _bury(self, path):
        """Remove every checkpoint of this ledger, of any code, lens and key,
        that `_universal` refuses against the ledger as it is NOW: dead for
        every reader, so its 12.5 MB serves nobody until RETIRE_S, and a
        longer dead file shadows a shorter live one at every restore. Never
        `path`, the file this save just wrote. Called under `_save_lock`.

        THE LEDGER IS READ AGAIN HERE, as `_keeps` reads it: this save's own
        bytes can be older than a checkpoint another save wrote since, and
        judged against them that newer file would look longer than the
        ledger. Each header is read, and not its payload, so the payload is
        judged by its length alone; one pass over the ledger digests every
        offset the headers name."""
        try:
            self._bury_unguarded(path)
        except Exception as exc:                         # noqa: BLE001
            _crumb("bury", exc)

    def _bury_unguarded(self, path):
        data, unavailable = eventledger.read_bytes(self.ledger)
        if unavailable:
            return
        now = Session(self.ledger, self.marker, data, self.code, self.epoch,
                      self.lens)
        root = os.path.dirname(path)
        found = []
        for name in os.listdir(root):
            other = os.path.join(root, name)
            if other == path or _name_terms(name) is None:
                continue
            try:
                with pk.open_regular(other, "rb") as f:
                    head = f.readline()
                    size = os.fstat(f.fileno()).st_size - len(head)
            except OSError:
                continue
            try:
                header = json.loads(head.decode("utf-8"))
            except ValueError:
                header = None
            found.append((other, header, size))
        offsets = {_offset_of(h, now.end) for _p, h, _s in found}
        digests = _prefix_digests(now.data, offsets - {None})
        for other, header, size in found:
            why = now._universal(header, size, prefix=digests.get(
                _offset_of(header, now.end)))
            if why is None:
                continue
            try:
                os.unlink(other)
            except OSError:
                continue
            _missed(self.ledger, self.code, self.lens, "removed: " + why,
                    at="remove")

    def save(self, state, count, offset, base, rec):
        """Write the checkpoint that describes EXACTLY `state` — the fold of
        the first `count` events, ending at byte `offset` — or write nothing.
        `base` is the restored header the fold continued from (None for a full
        replay) and `rec` what the fold read since. Returns whether it wrote;
        never raises. A refusal every later fold would meet too is named in
        `recurs`."""
        self.recurs = None
        try:
            header = self._header(count, offset, base, rec)
            if header is None:
                return False
            if self.why is not None:
                # THE MISS THIS SAVE ANSWERS, recorded once, by a reader that
                # reached the store, whether its checkpoint is then written
                # or the one there is kept (`_keeps`).
                _missed(self.ledger, self.code, self.lens, self.why)
                self.why = None
            payload = marshal.dumps(state)
            header["payload"] = {"bytes": len(payload),
                                 "sha256": hashlib.sha256(payload).hexdigest()}
            blob = json.dumps(header, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
            # THE FINGERPRINTS THIS SAVE MEASURED NAME ITS FILE.
            path = self.store(header["git"])
            self.git = header["git"]
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            # THE CHECKPOINT ONLY GROWS. See `_keeps`. The decision and the
            # write are made under one lock, so no other writer can land
            # between them.
            with _save_lock(self.ledger) as held:
                if not held or self._keeps(path, count):
                    return False
                pk.atomic_write(path, blob + b"\n" + payload, mode=0o600)
                self._supersede(path, header)
                self._bury(path)
                self._prune(path)
            return True
        except Exception as exc:                         # noqa: BLE001
            # EXPIRED INCLUDED: the answer this save describes is already
            # computed, and a budget that ran out while writing it is a reason
            # not to write, never a reason to lose the answer. The caller's
            # own next spend still raises if the budget is gone.
            _crumb("save", exc)
            return False

    def _keeps(self, path, count):
        """Must the checkpoint at `path` stay, instead of a new one of `count`
        events? Called under `_save_lock`.

        A BUDGETED READ SAVES A PREFIX (`progress_saver`), AND A DELAYED SAVE
        CAN ARRIVE LATE. If stop A folds 12,000 events and stalls before it
        writes, and reader B saves all 18,900 in between, an unconditional
        replace puts A's prefix back over B's whole fold. The next stop must
        then fold thousands of events again. Answers stay correct, because a
        prefix is keyed like any checkpoint, but concurrent stops can keep the
        ledger UNKNOWN. So the rule binds every writer, because every writer
        comes through `save`: a new checkpoint replaces one that is SHORTER, or
        one that no reader can use. A shorter or equal save is a no-op when the
        checkpoint there is valid for the ledger as it is NOW.

        VALID IS JUDGED BY THE SAME `_judged` A RESTORE USES, over the ledger
        read again here, because the checkpoint there can describe more bytes
        than this save's read saw. The file at `path` is named by the
        fingerprints this save measured, so its writer fingerprinted the same
        way and every key, the fingerprint included, is one the writer would
        answer as this save does: a longer checkpoint stale on any of them is
        replaced, and the rule never keeps what no reader can use. Validation that cannot finish keeps the
        checkpoint there: this save is only progress, and the next reader
        decides again."""
        try:
            with pk.open_regular(path, "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return False
        head, _nl, _payload = blob.partition(b"\n")
        try:
            there = json.loads(head.decode("utf-8"))["ledger"]["events"]
        except Exception:                                # noqa: BLE001
            return False
        if type(there) is not int or there < count:
            return False
        try:
            data, unavailable = eventledger.read_bytes(self.ledger)
            if unavailable:
                return True
            now = Session(self.ledger, self.marker, data, self.code,
                          self.epoch, self.lens)
            why = []
            if now._judged(blob, why) is not None:
                return True
            # THE LONGER CHECKPOINT IS REPLACED, AND WHY IS SAID: two writers
            # that judge each other's checkpoint stale replace it back and
            # forth, and only this reason shows which key they disagree on.
            self._miss("replaced: " + (why[0] if why else
                                       "the checkpoint was refused"),
                       at="replace")
            return False
        except Exception as exc:                         # noqa: BLE001
            _crumb("keeps", exc)
            return True

    def _supersede(self, path, header):
        """Remove this code's and lens's checkpoints the one just written at
        `path` subsumes: every repository one recorded is recorded in
        `header` with the same fingerprint, and it holds no more events.
        Called under `_save_lock`, so nothing is rewritten between the read
        of a header here and its removal.

        A NEW REPOSITORY IN THE FOLD MOVES THE KEY. A fold that read no git
        and then folds its first carried close records one repository more,
        so its next save lands under a new name; the file it grew from is
        then read by nobody the new one does not serve, and is removed here
        instead of occupying a file until it retires (`RETIRE_S`)."""
        root = os.path.dirname(path)
        git, events = header["git"], header["ledger"]["events"]
        own = (self.code[:16], _lens_digest(self.lens))
        for name in os.listdir(root):
            other = os.path.join(root, name)
            terms = _name_terms(name)
            if other == path or not terms or terms[2] is None \
                    or terms[:2] != own:
                continue
            try:
                with pk.open_regular(other, "rb") as f:
                    theirs = json.loads(f.readline().decode("utf-8"))
                count = theirs["ledger"]["events"]
                if type(count) is int and count <= events and all(
                        cwd in git
                        and git[cwd]["fingerprint"] == entry["fingerprint"]
                        for cwd, entry in theirs["git"].items()):
                    os.unlink(other)
            except Exception as exc:                     # noqa: BLE001
                _crumb("supersede", exc)

    def _header(self, count, offset, base, rec):
        """The key for `state`, measured NOW and cross-checked against what the
        fold itself observed; None when anything moved while it folded."""
        if policy() != self.code or marker_key(self.marker) != self.epoch:
            return None
        if any(term != self.lens for term in rec.epochs):
            # The fold consumed a gate epoch this key does not name: either a
            # lens appeared or changed its answer mid-fold, or a plain fold
            # was answered by one at all. Its result is not the result this
            # file would be read back as.
            _crumb("unplannable", Unplannable(
                "the gate-epoch lens served a term this checkpoint is not "
                "keyed on"))
            return None
        try:
            fresh = plan(rec)
        except Unplannable as exc:
            _crumb("unplannable", exc)
            if not isinstance(exc, Unsettled):
                self.recurs = str(exc)
            return None
        realpaths = dict(base["realpaths"]) if base else {}
        for path, real in rec.realpaths.items():
            if os.path.realpath(path) != real:
                return None
            realpaths[path] = real
        # THE HOME REPOSITORY, measured NOW like the realpaths above: every
        # answer this fold read, and the base's term if it carried one, must
        # be the one the running helm gives, or the key would name a home the
        # fold did not consistently see.
        home = base.get("home") if base else None
        if rec.homes:
            now = _home_now()
            if now is None or any([h] != now for h in rec.homes) \
                    or home not in (None, now):
                return None
            home = now
        git = dict(base["git"]) if base else {}
        for cwd, entry in fresh.items():
            env = [list(kv) for kv in entry["env"]]
            old = git.get(cwd)
            if old is not None and old["env"] != env:
                return None
            fp, facts = fingerprint(cwd, dict(entry["env"]))
            if fp is None or (old is not None and old["fingerprint"] != fp):
                return None
            if entry["shallow"] not in (None, facts["shallow"]):
                return None
            if entry["machinery"] and facts["inside"] != "false":
                # A work tree's own .gitattributes files steer merge-tree
                # (measured: an untracked `* merge=union` turned rc 1 into 0
                # under `-C <worktree>` and changed nothing under `-C .git`).
                # They are not in the fingerprint, so this fold is not reused.
                self.recurs = "merge machinery ran inside the work tree %s" \
                    % cwd
                _crumb("unplannable", Unplannable(self.recurs))
                return None
            exprs = set(entry["exprs"]) | set((old or {}).get("exprs") or ())
            lines = batch_check(cwd, dict(entry["env"]), exprs)
            if lines is None:
                return None
            if not all(agrees(e, o, lines[e])
                       for e, o in entry["exprs"].items()):
                return None
            if old is not None and any(lines[e] != line for e, line
                                       in old["exprs"].items()):
                return None
            git[cwd] = {"env": env, "fingerprint": fp, "exprs": lines}
        return {"format": FORMAT, "v": VERSION, "policy": self.code,
                "epoch": self.epoch, "lens": self.lens,
                "ledger": {"path": os.path.abspath(self.ledger),
                           "events": count, "offset": offset,
                           "sha256": hashlib.sha256(
                               self.data[:offset]).hexdigest()},
                "realpaths": realpaths, "home": home, "git": git,
                "writer": {"pid": os.getpid(), "ts": pk.now_ts()}}

    @staticmethod
    def _prune(keep):
        """Evict THIS LEDGER's checkpoints after a save wrote `keep`, per
        file and by last use, over every code version, lens term and key:
        first every file not used within RETIRE_S, then the least recently
        used until both the count (MAX_FILES) and the size (MAX_BYTES), `keep`
        included, hold; never `keep`. The contract is the module's (a) to
        (c).

        THE BOUNDS HOLD ON RETURN. Eviction is a loop: each pass lists the
        directory again and takes the least recently used file, which is kept
        if its recency moved since that listing, and the next pass picks
        another. So a victim refreshed by a restore outside the lock (an
        older code version's) costs a pass, never the bound. Passes are
        capped at twice the files found on entry, so concurrent refreshes
        cannot spin it; at the cap it stops, records StoreOverBound naming
        the bound left violated, and does not raise.

        CALLED UNDER THE SAVE LOCK, in the save's own critical section, and a
        restore refreshes recency under the same lock (`Session._recent`), so
        no refresh lands between a listing below and an unlink. Each victim
        is stat'ed again right before its unlink and kept if its recency
        moved: a path that bypasses the lock cannot lose a file it just
        used.

        PER FILE, NOT PER GROUP. A file is already one (code, lens, key), and
        any rule keeping one file per (lens, key) across code versions took
        the hub's plain checkpoint whenever a lane's ./bin/helm had written a
        newer one under its own code.

        Per-ledger by construction: `keep` is inside the ledger's own
        directory, so the listing below cannot reach another ledger's store
        and the bounds are per ledger rather than a budget two ledgers
        share."""
        root = os.path.dirname(keep)
        try:
            size = os.stat(keep).st_size
        except OSError:
            return
        horizon = time.time_ns() - RETIRE_S * 1_000_000_000
        entry = None
        blocked = set()
        while True:
            files = []
            try:
                for name in os.listdir(root):
                    path = os.path.join(root, name)
                    if path == keep or _name_terms(name) is None:
                        continue
                    try:
                        st = os.stat(path)
                    except FileNotFoundError:
                        continue
                    files.append((st.st_mtime_ns, st.st_size, path))
            except OSError:
                return
            if entry is None:
                # THE AGE PASS, ONCE: every file not used within RETIRE_S.
                entry = 2 * (len(files) + 1)
                files = [f for f in files if not (
                    f[0] < horizon and Session._unlink_unmoved(f))]
            count = len(files) + 1
            total = size + sum(f[1] for f in files)
            if not files or (count <= MAX_FILES and total <= MAX_BYTES):
                return
            candidates = [f for f in files if f[2] not in blocked]
            if entry <= 0 or not candidates:
                reason = ("the remaining files could not be unlinked"
                          if not candidates else
                          "victims kept moving as they were picked")
                _crumb("prune", StoreOverBound(
                    "the store keeps %d files and %d bytes against MAX_FILES "
                    "%d and MAX_BYTES %d: %s"
                    % (count, total, MAX_FILES, MAX_BYTES, reason)))
                return
            entry -= 1
            # A moved victim is reconsidered at its new recency; an unlink
            # failure at the same path must not monopolize every rescan.
            victim = min(candidates)
            if Session._unlink_unmoved(victim) is None:
                blocked.add(victim[2])

    @staticmethod
    def _unlink_unmoved(victim):
        """Unlink an unchanged victim: True if removed, False if its recency
        moved, None if the path could not be unlinked."""
        mtime, _size, path = victim
        try:
            if os.stat(path).st_mtime_ns != mtime:
                return False
            os.unlink(path)
        except OSError:
            return None
        return True


def _offset_of(header, end):
    """The ledger offset a checkpoint header names when it is an int within
    [0, end], else None."""
    ledger = header.get("ledger") if isinstance(header, dict) else None
    offset = ledger.get("offset") if isinstance(ledger, dict) else None
    return offset if type(offset) is int and 0 <= offset <= end else None


def _prefix_digests(data, offsets):
    """{offset: sha256 of data[:offset]} for every offset, in ONE pass over
    `data`, so a sweep over many checkpoints hashes the ledger once."""
    h, at, out, view = hashlib.sha256(), 0, {}, memoryview(data)
    for offset in sorted(offsets):
        h.update(view[at:offset])
        at = offset
        out[offset] = h.copy().hexdigest()
    return out


def _lens_digest(lens):
    """The 16-hex term a lens contributes to a file name; "" for none."""
    if lens is None:
        return ""
    return hashlib.sha256(
        lens.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def git_key(git):
    """The 16-hex key of a checkpoint's `git` section: a digest of the
    fingerprint recorded for each repository it read, so the key is exactly
    what `Session._unseen` compares."""
    return hashlib.sha256(repr(sorted(
        (cwd, entry["fingerprint"]) for cwd, entry in git.items())).encode(
            "utf-8", "surrogateescape")).hexdigest()[:16]


def checkpoint_name(code, lens, key):
    """The file ONE KEY is stored in: `<code>[.<lens>].k<key>.ckpt`, the
    first 16 hex of the code digest, the lens term, and the git key
    (`git_key`).

    THE LENS TERM IS IN THE NAME because a lensed fold and a plain fold of
    one ledger answer differently — outside a lens each close is judged
    against the verdict index as it stands AT ITS OWN POSITION, so an event
    before the founding verdict reads a lost boundary, while a lens serves
    one term to every row — so they are two folds and they get two files.

    THE FINGERPRINTS ARE IN THE NAME because a seat can use no checkpoint
    whose fingerprints it does not reproduce. Sharing a file with a seat git
    answers differently, the second seat replays on every read while the
    ledger stands still, then, once its fold is longer, replaces the first
    seat's checkpoint and starves it. In each case the split is what lets
    both checkpoints hold, and naming the file by the fingerprints rather
    than by the environment is what keeps seats git answers identically
    (Orca's credential entries, say) on one."""
    term = _lens_digest(lens)
    return code[:16] + ("." + term if term else "") + ".k" + key + SUFFIX


def _name_terms(name):
    """(code, lens term, key) of a checkpoint file name, the key None for a
    name written before the key was part of it; None for any other name.
    A lens term is hex and a key term starts with "k" (a "v" term is an
    earlier spelling, and reads as no key), so the two never read as each
    other."""
    if not name.endswith(SUFFIX):
        return None
    parts = name[:-len(SUFFIX)].split(".")
    key = None
    if len(parts) > 1 and parts[-1][:1] in ("k", "v"):
        term = parts.pop()
        key = term[1:] if term[0] == "k" else None
    if len(parts) not in (1, 2):
        return None
    return parts[0], (parts[1] if len(parts) == 2 else ""), key


def save_lock_path(ledger):
    """The one lock every checkpoint save of `ledger` takes. It is BESIDE the
    ledger's store directory, not inside it, so a listing of the store holds
    checkpoints only."""
    return os.path.join(os.path.dirname(store_dir(ledger)),
                        ledger_key(ledger) + ".lock")


@contextlib.contextmanager
def _save_lock(ledger, wait_s=None):
    """Yield True while this process holds the save lock of `ledger`, or
    False when it could not get it inside the ambient budget, or inside
    `wait_s` seconds when that is given. A budgeted reader never waits past
    its deadline for a save, which is optional."""
    path = save_lock_path(ledger)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = None if wait_s is None else time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = projscope.remaining()
                if deadline is not None:
                    mine = deadline - time.monotonic()
                    left = mine if left is None else min(left, mine)
                if left is not None and left <= 0:
                    yield False
                    return
                time.sleep(0.01 if left is not None else 0.05)
        yield True
    finally:
        os.close(fd)


# THE WRITE IS OPTIONAL AND THE ANSWER IS NOT. A save re-asks git for the
# fold's fresh expressions and serialises the whole state (about 0.1s at 17,542
# events); a reader whose budget is nearly gone — the stop guard's rung — keeps
# its answer and leaves the save to the next reader or writer.
SAVE_RESERVE_S = 1.0


def may_save():
    """May this read spend time writing a checkpoint?"""
    left = projscope.remaining()
    return left is None or left > SAVE_RESERVE_S


# A BUDGETED READ THAT CANNOT FINISH STILL BANKS WHAT IT FOLDED (task/2949).
#
# THE DEFECT, MEASURED on the live ledger (18,924 events, 13.6 MB) through the
# guard's own read: a checkpoint hit is 0.11s median of five, and a miss is a
# full replay at 25.96s median of five (22.9-35.0s). The key includes the code,
# so every land into the shared checkout is a miss. The Stop guard's
# dispatch-ledger slice is about 7.4s, so its replay could never finish, and a
# read that did not finish wrote nothing. Every stop after a land therefore
# reported the ledger UNKNOWN, until an unbudgeted reader (`helm dispatch
# list`) paid the whole replay and saved it.
#
# THE CURE KEEPS THE ANSWER RULE AND CHANGES ONLY WHAT IS KEPT. A fold of the
# first K events is exactly the state the checkpoint stores at position K; the
# ordinary tail read already depends on that. So when a budgeted fold reaches
# SAVE_RESERVE_S before its deadline, it saves the fold so far ONCE, at a row
# boundary, and continues. If it then finishes, the answer is the full answer.
# If it does not, it raises `Expired` as before, so the reader still reports
# UNKNOWN with its reason. The next budgeted read restores at K and continues.
# A few stops after a deploy the checkpoint is whole again. The banked prefix
# has the same key as every other checkpoint, so a rewritten ledger, a new
# code version or a moved git fact discards it, and that read is a full replay.
def progress_saver(session, offset, count, base, rec):
    """-> bank(acc, index), called before the fold takes event `index`, or
    None for an unbudgeted read, which does not need it.

    `offset` and `count` are where this fold started (the restored
    checkpoint's byte offset and event count, or 0 and 0), `base` its header
    and `rec` the recorder of this fold. The caller passes a saver only when
    every line past `offset` is one event, so event `index` ends at the
    (index - count)-th newline after `offset`."""
    left = projscope.remaining()
    if left is None:
        return None
    horizon = time.monotonic() + left - SAVE_RESERVE_S
    banked = []

    def bank(acc, index):
        if banked or index <= count or time.monotonic() < horizon:
            return
        banked.append(index)
        end = offset
        for _ in range(index - count):
            end = session.data.index(b"\n", end) + 1
        # THE SAVE'S OWN GIT READS ARE NOT INPUTS OF THE FOLD. Recorded, they
        # are questions `plan` cannot verify, and the complete fold would then
        # write no checkpoint.
        stack = _stack()
        held = stack[:]
        del stack[:]
        try:
            session.save(acc.state(), index, end, base, rec)
        finally:
            stack[:] = held

    return bank


def begin(ledger, marker, data, lens=None):
    """The Session for one read of `ledger` whose bytes are `data`, or None
    when this read must not touch a checkpoint at all: this process cannot
    name its code, or the gate-epoch marker cannot be read.

    `lens` is the identity of the gate-epoch term a lens owns for this fold,
    or None when the fold resolves the epoch itself. It is part of the key in
    both directions — the file name and a header field the restore compares —
    and the caller supplies it only for a term it has already proved is the
    marker's own, so no store holds a fold judged against a boundary no file
    backs."""
    projscope.spend_or_raise("ledger fold checkpoint key")
    code = policy()
    if code is None:
        _told(UNKEYED, "this process cannot name its code")
        return None
    epoch = marker_key(marker)
    if epoch is None:
        _told(UNKEYED, "the gate-epoch marker cannot be read")
        return None
    return Session(ledger, marker, data, code, epoch, lens)
