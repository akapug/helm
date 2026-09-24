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
A land moves trunk, so one full replay per land is expected; between lands
every read restores and folds only its tail.

FAIL-SAFE IN ONE DIRECTION. Any read, parse, version or verify failure is a
full replay, never a partial or stale answer. A fold that asked git something
this module cannot re-verify (`Unplannable`), a git read outside the seam, or
a read that stopped inside an event writes NO checkpoint at all. A budgeted
read that cannot finish saves the events it folded, once, at an event boundary,
and still reports that it did not finish (`progress_saver`). A failure to WRITE
is a breadcrumb through `record.swallow`, never a failed read.

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
it is per CODE VERSION on disk — one file per `policy` digest, the newest
`KEEP` kept — so a lane's own `./bin/helm` and the hub binary each maintain
their own file instead of invalidating each other's on every read.

AND IT IS PER LEDGER, WHICH IS AN ADDRESS AND NOT A LABEL. Every durable
ledger on this box shares ONE directory, so a store addressed by code and lens
alone gives two ledgers one file: each read finds a header whose prefix digest
belongs to the other ledger, calls it stale, and overwrites it — so neither
fold's checkpoint ever holds and the only symptom is that both readers stay
slow. That is the mutual eviction `_flavour` already prevents for the lens,
left open for the ledger, and it is why a SECOND customer could not be added
without this: the defect is invisible while there is only one. So the ledger
names a subdirectory of its own, which also scopes `KEEP` — a busy ledger's
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

from . import eventledger, gitfacts, pk, projscope, record, vcs

FORMAT = "helm-ledger-fold-checkpoint"
VERSION = 2
DIRNAME = "ledger-fold"
SUFFIX = ".ckpt"
# One file per CODE VERSION and per LENS TERM, WITHIN ONE LEDGER's directory,
# so a deploy, a lensed reader and a second ledger each maintain their own
# rather than invalidating another's on every read. The bound is over that
# product, not over code alone — and it is per ledger BY CONSTRUCTION rather
# than by a filter, because `_prune` lists the ledger's own directory and can
# therefore see nothing else to delete.
KEEP = 8

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


def policy():
    """The identity of the code the fold executes. -> hex, or None when this
    process cannot name it (the tree changed under it since import), in which
    case the caller neither reads nor writes a checkpoint."""
    try:
        if _source_stats() != _LOADED:
            return None
        if not _DIGEST:
            h = hashlib.sha256(("%s v%d python %s marshal %d\0" % (
                FORMAT, VERSION, sys.version, marshal.version)).encode())
            for rel in sorted(_LOADED):
                with open(os.path.join(_PKG, rel), "rb") as f:
                    h.update(rel.encode("utf-8", "surrogateescape") + b"\0"
                             + hashlib.sha256(f.read()).digest())
            if _source_stats() != _LOADED:
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

    __slots__ = ("calls", "realpaths", "taints", "epochs")

    def __init__(self):
        self.calls = []        # (cwd, args, env items, fed stdin, rc, stdout)
        self.realpaths = {}    # path -> what os.path.realpath answered
        self.taints = []       # inputs nothing here can re-verify
        self.epochs = []       # every gate-epoch term a LENS served this fold


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
# the plan: which git answers a checkpoint must re-verify
# ---------------------------------------------------------------------------

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
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


def _text(out):
    if isinstance(out, bytes):
        return out.decode("utf-8", "surrogateescape").strip()
    return str(out or "").strip()


def _classify(args, rc, out):
    """("shallow", answer) or ("exprs", {expression: observation}) for ONE
    recorded call, where an observation is MISSING, PRESENT, the full id it
    resolved to, or None for "an operand the call needed to exist"."""
    if rc == -1:
        raise Unplannable("a git read did not complete (%s)"
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
            raise Unplannable("an operand was used after git called it missing")
        return have
    if have is None:
        if new == MISSING:
            raise Unplannable("an operand was used after git called it missing")
        return new
    if MISSING in (have, new):
        raise Unplannable("git answered one question two ways during the fold")
    if have == PRESENT:
        return new
    if new == PRESENT:
        return have
    raise Unplannable("git resolved one expression to two objects in one fold")


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
                raise Unplannable("shallowness changed during the fold")
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
# the checkpoint on every lane a seat opens. Everything else git lists is kept:
# a precondition that enumerates the config surface a git release reads is one
# the next release edits, so the list of what is DROPPED is the short one.
_CONFIG_DROPPED = (b"branch.",)
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

    __slots__ = ("ledger", "marker", "data", "end", "code", "epoch", "lens")

    def __init__(self, ledger, marker, data, code, epoch, lens=None):
        self.ledger = ledger
        self.marker = marker
        self.data = data
        self.end = data.rfind(b"\n") + 1     # past the last COMPLETE line
        self.code = code
        self.epoch = epoch
        self.lens = lens

    def store(self):
        return os.path.join(store_dir(self.ledger),
                            self.code[:16] + self._flavour() + SUFFIX)

    def _flavour(self):
        """The LENS TERM's place in the file name. A lensed fold and a plain
        fold of one ledger answer differently — outside a lens each close is
        judged against the verdict index as it stands AT ITS OWN POSITION, so
        an event before the founding verdict reads a lost boundary, while a
        lens serves one term to every row — so they are two folds and they get
        two files. Without the split each read would discard the other's
        checkpoint and neither would ever hold."""
        if self.lens is None:
            return ""
        return "." + hashlib.sha256(
            self.lens.encode("utf-8", "surrogateescape")).hexdigest()[:16]

    def restore(self):
        """(header, state) of this code's checkpoint when EVERY key still
        holds, else None. Never raises except `projscope.Expired`."""
        try:
            with pk.open_regular(self.store(), "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            _crumb("restore-read", exc)
            return None
        try:
            return self._judged(blob)
        except projscope.Expired:
            raise
        except Exception as exc:                         # noqa: BLE001
            _crumb("restore", exc)
            return None

    def _judged(self, blob):
        """(header, state) when checkpoint bytes `blob` are usable for this
        session's ledger bytes, else None. May raise. ONE JUDGE, used by the
        restore and by the save's grow-only rule (`_keeps`), so the two can
        never disagree about which checkpoint a reader can use."""
        head, _nl, payload = blob.partition(b"\n")
        header = json.loads(head.decode("utf-8"))
        if self._stale(header, payload):
            return None
        state = marshal.loads(payload)
        if not _state_ok(state, header["ledger"]["events"]):
            return None
        return header, state

    def _stale(self, header, payload):
        """Why this header no longer describes the world, cheapest key first,
        or None. Every git answer is re-asked; nothing is trusted for age."""
        if not isinstance(header, dict) or header.get("format") != FORMAT \
                or header.get("v") != VERSION:
            return "not a checkpoint of this format"
        if header.get("policy") != self.code:
            return "written by other code"
        if header.get("lens") != self.lens:
            return "folded under a different gate-epoch lens"
        if header.get("epoch") != self.epoch:
            return "the gate-epoch marker moved"
        ledger = header.get("ledger") or {}
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
        if hashlib.sha256(self.data[:offset]).hexdigest() \
                != ledger.get("sha256"):
            return "the ledger prefix was rewritten"
        body = header.get("payload") or {}
        if body.get("bytes") != len(payload) \
                or hashlib.sha256(payload).hexdigest() != body.get("sha256"):
            return "the payload is not the one the header describes"
        realpaths = header.get("realpaths")
        if not isinstance(realpaths, dict):
            return "malformed realpaths"
        for path, real in realpaths.items():
            if os.path.realpath(path) != real:
                return "a repository path resolves differently"
        git = header.get("git")
        if not isinstance(git, dict):
            return "malformed git section"
        for cwd, entry in git.items():
            env = dict(entry["env"])
            fp, _facts = fingerprint(cwd, env)
            if fp is None or fp != entry["fingerprint"]:
                return "git's view of %s changed" % cwd
            got = batch_check(cwd, env, entry["exprs"])
            if got != entry["exprs"]:
                # A TRUNK THAT ADVANCED IS NOT A TRUNK THAT CHANGED. Every
                # other difference still discards; see `_advanced_only`.
                why = _advanced_only(cwd, env, entry["exprs"], got)
                if why:
                    return why
        return None

    def save(self, state, count, offset, base, rec):
        """Write the checkpoint that describes EXACTLY `state` — the fold of
        the first `count` events, ending at byte `offset` — or write nothing.
        `base` is the restored header the fold continued from (None for a full
        replay) and `rec` what the fold read since. Returns whether it wrote;
        never raises."""
        try:
            header = self._header(count, offset, base, rec)
            if header is None:
                return False
            payload = marshal.dumps(state)
            header["payload"] = {"bytes": len(payload),
                                 "sha256": hashlib.sha256(payload).hexdigest()}
            blob = json.dumps(header, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
            path = self.store()
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            # THE CHECKPOINT ONLY GROWS. See `_keeps`. The decision and the
            # write are made under one lock, so no other writer can land
            # between them.
            with _save_lock(self.ledger) as held:
                if not held or self._keeps(path, count):
                    return False
                pk.atomic_write(path, blob + b"\n" + payload, mode=0o600)
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
        than this save's read saw. A longer checkpoint that is stale-keyed is
        replaced, so the rule never keeps what no reader can use. Validation
        that cannot finish keeps the checkpoint there: this save is only
        progress, and the next reader decides again."""
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
            return now._judged(blob) is not None
        except Exception as exc:                         # noqa: BLE001
            _crumb("keeps", exc)
            return True

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
            return None
        realpaths = dict(base["realpaths"]) if base else {}
        for path, real in rec.realpaths.items():
            if os.path.realpath(path) != real:
                return None
            realpaths[path] = real
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
                _crumb("unplannable", Unplannable(
                    "merge machinery ran inside the work tree %s" % cwd))
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
                "realpaths": realpaths, "git": git,
                "writer": {"pid": os.getpid(), "ts": pk.now_ts()}}

    @staticmethod
    def _prune(keep):
        """Keep the newest KEEP checkpoints of THIS LEDGER (one per code
        version and lens term).

        Per-ledger by construction: `keep` is inside the ledger's own
        directory, so the listing below cannot reach another ledger's store
        and `KEEP` is a bound per ledger rather than a budget two ledgers
        share. A shared directory would make a busy ledger's churn evict a
        quiet one's checkpoint, which reads as a slow reader and never as a
        fault."""
        root = os.path.dirname(keep)
        try:
            names = [n for n in os.listdir(root) if n.endswith(SUFFIX)]
            paths = sorted((os.path.join(root, n) for n in names),
                           key=lambda p: os.stat(p).st_mtime_ns, reverse=True)
        except OSError:
            return
        for path in paths[KEEP:]:
            if path != keep:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def save_lock_path(ledger):
    """The one lock every checkpoint save of `ledger` takes. It is BESIDE the
    ledger's store directory, not inside it, so a listing of the store holds
    checkpoints only."""
    return os.path.join(os.path.dirname(store_dir(ledger)),
                        ledger_key(ledger) + ".lock")


@contextlib.contextmanager
def _save_lock(ledger):
    """Yield True while this process holds the save lock of `ledger`, or
    False when it could not get it inside the ambient budget. A budgeted
    reader never waits past its deadline for a save, which is optional."""
    path = save_lock_path(ledger)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = projscope.remaining()
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
        return None
    epoch = marker_key(marker)
    if epoch is None:
        return None
    return Session(ledger, marker, data, code, epoch, lens)
