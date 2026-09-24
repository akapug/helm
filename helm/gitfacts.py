"""ANSWERS GIT DERIVES FROM THE OBJECT GRAPH ALONE, CACHED ACROSS PROCESSES.

WHAT THIS IS FOR — the measured cost. One `helm dispatch list` over 4381 rows
forks git 1272 times and spends ~28 of its ~38 seconds starting processes and
waiting for them. `projscope` already collapses repeats WITHIN one pass, but
it is a per-pass dict: it is dropped at the end of the projection, so the next
read of an unchanged repository re-asks every one of those questions from
scratch. Measured on this ledger, with a scope open: 786 spawns, 784 of them a
DISTINCT question. There is nothing left for a per-pass memo to collapse. The
only thing that can remove that work is a cache that OUTLIVES the process, and
the only sound basis for one is a key that names something that cannot change.

THE KEY IS THE WHOLE ARGUMENT. A git object id is a content address: the
commit `ad7ee52c…` is the same commit in every process, forever. `merge-base
--is-ancestor A B` for two full object ids is a walk of the graph those ids
root, and it has ONE answer for all time; `cherry A B` is patch identity over
the range those two ids fix, and so does it. Neither is a fact about the
repository's current state. So this cache needs no ttl, no mtime check, no
invalidation and no epoch proof: an entry is correct for as long as sha
collisions do not exist.

BEING DERIVED FROM FIXED IDS IS NECESSARY AND IT IS NOT SUFFICIENT, which is
the correction this module is built around and the reason the table below is
two rows rather than seven. Four kinds of answer take nothing but object ids
and are still not functions of content, and each was MEASURED to move while
every operand stood still:

  * EXISTENCE IS A PROPERTY OF AN OBJECT DATABASE. `cat-file -e A^{commit}`
    answers 0 here and 128 in a repository that never fetched A — and after
    `gc --prune=now` in this one, which is not hypothetical on a fleet that
    lands REBASED: a reviewed tip is then reachable from nothing and is
    exactly what gc prunes. A stored 0 is a claim about an odb, served into
    an odb that does not hold the object. It is the same claim in three
    spellings: `rev-parse --verify --quiet A^{commit}` (measured 128 for an
    absent object, and its stdout is the id it was handed, so the exit code is
    the ENTIRE content of the answer) and `rev-parse A^{tree}` (measured 128)
    are existence probes wearing a different verb. None of the three is here.
    A liveness check would not rescue them: re-reading the odb is the spawn
    this module exists to remove.
  * MERGE MACHINERY RESOLVES CONTENT THROUGH THINGS THAT ARE NOT IN THE
    QUESTION, and helm had already ruled on it, in the module this cache
    serves — `dispatches._carriage_replay_witness` states that merge-tree's
    answer "IS NOT A FUNCTION OF THE IDS IT WAS HANDED, which is why nothing
    in this module has ever been allowed to persist it". Measured, every
    operand id fixed: an `.git/info/attributes` line reading `merge=union`
    turns rc 1 into rc 0; `merge.renames=false` turns rc 0 into rc 1; so does
    `diff.renames=false`. The first build of this table stored the rc 1 half
    anyway, on the ground that a conflicted merge-tree "produces nothing" —
    which is FALSE: measured, the rc 1 run printed a real tree id and left two
    new loose objects in the odb. Both the entry and that reasoning are gone.
  * A RENDERING IS NOT AN ANSWER. `diff --raw A B` prints a listing whose
    SHAPE is the reader's, not the content's: ambient `diff.renames` turns one
    `R100` line into a `D` plus an `A`, ambient `core.abbrev` changes how many
    hex digits each id is printed with, and ambient `diff.orderFile` reorders
    the lines. The one caller in this tree pins the first two on its argv
    (`--no-renames --abbrev=40`) — but orderFile moves that exact argv anyway,
    and a precondition that must enumerate a config surface is a precondition
    that the next release of git edits. Not admitted at any spelling.
  * A REF IS A QUESTION ABOUT NOW. `rev-parse refs/remotes/origin/main`
    answers differently after every fetch and `rev-parse
    --is-shallow-repository` is a property of the checkout. These are refused
    the same way, and refused MECHANICALLY rather than by a verb allowlist:
    every operand must be a full object id.

AND THE GATE READS OPTION VALUES, NOT ONLY BARE WORDS. A word glued to a `-`
is still an operand — `--merge-base=refs/remotes/origin/main` names a ref that
moves under a key that will never notice. So an `=`-bearing option is admitted
only when the half after the first `=` is itself an object id. The rule is
stated that way round, as a REFUSAL of everything else, because the safe/unsafe
split depends on which options take a NAME, that list is per-verb, and it
changes between git releases: a refusal costs one spawn and a wrong admission
is wrong forever. ITS BOUND, said out loud: a value glued WITHOUT an `=`
(`-O<orderfile>`, `-S<string>`) is invisible to this check. That is not a hole
today because neither admitted verb takes one — a fact about the two verbs
that remain, which the next verb added here owes an answer to.

A HISTORY REWRITER DEFEATS THE WHOLE ARGUMENT, so it is a precondition and not
a footnote. `refs/replace/<oid>` and a graft file both make git return a
different answer for the same immutable id, which is exactly the property this
cache is built on. helm's read paths already pin both off — `rowworld.
_history_view_env` sets `GIT_NO_REPLACE_OBJECTS` and points `GIT_GRAFT_FILE`
at /dev/null, `landreq._object_view` does the same with the argv spelling —
and a call that does NOT pin them is refused.

AND THE THIRD REWRITER HAS NO OVERLAY, which is the clause `seats_stop_budget`
predicted this module would owe: it rules that a durable memo of the
ancestry/patch-identity verdict "cannot be made correct" without "a view held
immutable", naming `refs/replace`, `info/grafts` AND a shallow boundary as the
three that "reinterpret the same immutable ids without changing one of them".
The first two are pinned; a shallow boundary cannot be, because the parent
objects are genuinely absent rather than hidden. It is REFUSED instead, and it
had to be: measured in a `--depth=1` clone holding both operands, `merge-base
--is-ancestor A B` answers 1 where the complete repository answers 0, and
`cherry A B` lists one commit where the complete repository lists two — both of
them exit codes this table calls answers. Shallowness is read from the
FILESYSTEM (the `shallow` file beside the common dir) rather than from `git
rev-parse --is-shallow-repository`, because a probe per lookup is the spawn
this module exists to remove; a layout this cannot resolve is refused rather
than assumed complete. Both doors check, so an entry written by a complete
repository is never served by a repository that has since been truncated.

SO DOES THE REPOSITORY-SELECTION ENVIRONMENT, and requiring it here is the
difference between a precondition and a coincidence. The key names a
repository by PATH; `GIT_DIR`, `GIT_OBJECT_DIRECTORY` and
`GIT_ALTERNATE_OBJECT_DIRECTORIES` select a different one from outside the key,
and `git -C <path>` does not beat them. Today every call that reaches this
table comes through `rowworld._scrubbed_env`, which removes all eight of
`dispatches._GIT_SELECTION_ENV` — but that is a property of ONE CALLER.
`landreq._object_view` pins both rewriters and scrubs nothing, and it would be
admitted the moment `landreq._git` is routed through the `vcs` seam, which is
where this tree says git is spelled. So the scrub is required HERE, of every
caller, and the variable list is IMPORTED rather than retyped for the reason
that module states about it: two copies of a security-relevant list drift, and
the drift is invisible.

AND ONE PRECONDITION IS NOT A PROPERTY OF THE QUESTION AT ALL. Two callers
can ask this table the identical question — same repository, same argv, same
overlay — and want opposite things from one exit code, because a verb's 128
is an ODB fact riding on an answer that is otherwise a content fact. The
projection wants the ancestry of two fixed ids. `dispatches.
_patch_tip_ancestry` is a security proof and reads 128 as "this repository
cannot measure it", so for that reader a stored 0 or 1 served after an operand
leaves the odb answers a question the repository can no longer answer. Nothing
in (repository, argv, overlay) tells the two apart, so this table does not try
to: the re-measuring caller DECLARES itself, by putting `UNCACHED` in the
overlay it already builds once and hands to every read it makes. A declaration
spelled as a word on one call's argv does not hold — `_declined` says why the
overlay is where it belongs.

THE GIT BINARY IS AN OPERAND OF EVERY STORED ANSWER, so it is in the key. Both
admitted questions are computed by code that has changed across releases —
merge-ort replaced the recursive merge strategy, and `cherry`'s verdict is a
patch-identity comparison whose hashing has been revised — so an entry written
by one git and served to another is an answer to a question nobody asked. It
is resolved ONCE per process into a module global: a version probe per lookup
would be the spawn this module exists to remove, and an entry that cannot name
the git that produced it is refused rather than stored under a guess.

ONLY AN ANSWER IS STORED, NEVER A FAILURE TO READ. `_spawn` returns rc -1 for
any spawn trouble, and both admitted verbs return 128 when an operand is not in
the odb — a state the next fetch can end. Caching either would let one
transient blip, or one not-yet-fetched commit, answer forever; and a stored
UNKNOWN is indistinguishable from a measured one at every read site
downstream. So each question declares WHICH exit codes are answers:
`merge-base --is-ancestor` means its rc 1 ("no"), and 128 is nobody's answer.

ONE FILE PER FACT, not one table. A single JSON table would have to be parsed
in full on every read (the working set is ~250 entries and grows with trunk),
rewritten on every miss, and would lose one of two concurrent writers. A
content-addressed file is a single ~20µs open against a ~30ms fork, needs no
lock because two writers of the same key write identical bytes, and cannot
lose an entry it never rewrites.

THE TIMEOUT IS DELIBERATELY NOT IN THE KEY, which is the opposite of the rule
`vcs.run`'s memo states one layer up, for a reason that only holds here: that
memo can cache a -1, so a question asked at timeout=1 and at timeout=30 really
are different questions. This one stores only completed answers, and a
completed answer does not depend on how long the caller was willing to wait.
Leaving it out is what lets `dispatches` (timeout=10) and `rowworld`
(timeout=180) share one answer to the same question.

AND IT IS BOUNDED BY CAPACITY, NEVER BY A CLOCK — the one shape of eviction
everything above permits. An age horizon here would be a ttl wearing a
retention costume: it would rule that an answer whose operands are still the
same immutable objects has EXPIRED, which is the claim these paragraphs spend
their length refusing, and this docstring would be false about its own store
the day it was added. A CAPACITY bound decides nothing about correctness. A
pruned entry is re-derived by exactly the one git spawn the uncached world
pays, so the ceiling is a DISK decision and the table may be cut anywhere
without a wrong answer becoming possible.

AND ONE LAND ORPHANS THE WHOLE TABLE, which is what makes the cheap eviction
order the right one rather than a tolerated approximation. Measured over one
`helm dispatch list` on this ledger: 246 distinct cacheable questions, 123
`merge-base --is-ancestor` and 123 `cherry`, and EVERY ONE of the 246 names
trunk's head — the ancestry question in its second operand, the patch-identity
question in its first. Replaying that population against three consecutive
trunk generations wrote 246 entries and 112,553 bytes for each generation and
reused NOTHING across them. So a land does not churn part of this table; it
orphans all of it, and newest-first by WRITE time is not an approximation of
least-recently-used here — it IS the live generation. `entry_paths` says what
enforces the ceiling, what the order knows, and what it does not.
"""
import hashlib
import os
import re
import threading

_V = 2                          # derivation version: bump to orphan every
                                # entry when the KEY or the value encoding
                                # changes meaning

# A full object id, optionally peeled. sha1 and sha256 repositories both.
_OID = re.compile(r"\A[0-9a-f]{40}(\^\{(commit|tree)\})?\Z|"
                  r"\A[0-9a-f]{64}(\^\{(commit|tree)\})?\Z")

# (leading command words) -> the exit codes that are ANSWERS for that question.
# Longest match wins, so a more specific form can narrow a broader one. Every
# row here is an answer git reads OFF THE OBJECT GRAPH: `--is-ancestor` walks
# it, `cherry` compares patch identity across the range two ids fix. Measured
# against a pile of hostile ambient state at once — `* -diff` and `* merge=
# union` in `.git/info/attributes`, `diff.renames=false`, `core.abbrev=12`,
# `diff.algorithm=patience`, `diff.orderFile`, `diff.ignoreSubmodules=all`,
# `diff.noprefix`, `core.quotePath=false` — both verbs returned the identical
# exit code and the identical bytes, on a patch-equivalent pair and on a
# different one. That is the property; the module docstring says what failed it.
_ANSWERS = (
    (("merge-base", "--is-ancestor"), (0, 1)),
    (("cherry",), (0,)),
)

# A WORD CANNOT CARRY AN INTENT, so this set is kept for what it does cover
# and is no longer trusted for what it does not. `--end-of-options` is argv
# hygiene: it marks where a `-`-leading operand stops being read as an option.
# EVERY read in this tree that spells it is a `rev-parse`, and `rev-parse` is
# in no row of `_ANSWERS`, so this frozenset refuses nothing that would
# otherwise be stored. It stays because a verb admitted later may take the
# word and the refusal costs one `in`. It is NOT how a caller declares that it
# re-measures — `UNCACHED` is, and `_declined` says why.
_NEVER = frozenset(("--end-of-options",))

# THE CALLER'S DECLARATION, a reserved key of the SEAM'S OVERLAY DIALECT and
# not a variable git reads: `vcs._spawn` removes it before it builds a child
# environment, the way that overlay already reads a `None` value as "unset
# this one". It rides on the OVERLAY rather than on an argv or a per-call
# keyword because an overlay is built ONCE by a read path and handed to every
# call that path makes, so a call added to that path later inherits the
# declaration instead of having to remember a word.
UNCACHED = "HELM_GITFACTS_UNCACHED"

_MAX_BYTES = 1 << 20            # a stdout larger than this is not worth a file

MAX_ENTRIES = 4096              # the CAPACITY ceiling — see `entry_paths`

_LOCK = threading.Lock()
_DISABLED = [False]
_VERSION = []                   # one element once resolved: bytes, or None
_SELECTION = []                 # one element once resolved: the scrub's names


def _root():
    from . import registry
    return os.path.join(registry.cache_root(), "gitfacts")


def _version():
    """The git binary's own version line, resolved ONCE per process. -> bytes
    or None when it could not be read, which refuses the table outright.

    A probe per lookup would cost exactly the spawn this module removes, so
    the answer is held in a module global for the life of the process. A git
    swapped under a running process therefore keeps the old string until that
    process ends — bounded by one process, where a stale KEY is a miss and
    never a wrong answer."""
    if not _VERSION:
        with _LOCK:
            if not _VERSION:
                _VERSION.append(_resolve_version())
    return _VERSION[0]


def _resolve_version():
    """Asked THROUGH THE SEAM rather than spawned here: a direct git spawn
    outside `vcs` is the debt this tree measures and pins."""
    from . import vcs                    # DEFERRED — vcs imports gitfacts.
    return vcs.git_version()


def _selection_env():
    """The repository-selection variables every admitted call must have
    removed. IMPORTED from `dispatches`, which is this tree's authority for
    the list, rather than retyped beside it."""
    if not _SELECTION:
        from . import dispatches         # DEFERRED — dispatches imports vcs.
        _SELECTION.append(tuple(dispatches._GIT_SELECTION_ENV))
    return _SELECTION[0]


def _common_dir(gitdir):
    """The directory the `shallow` file lives beside, or None when this path's
    layout cannot be resolved without asking git. -> path or None

    Three shapes reach this module: a worktree root whose `.git` is a
    DIRECTORY, a linked worktree root whose `.git` is a FILE naming its gitdir,
    and a gitdir itself (which is what `rowworld` hands down). A linked
    worktree's gitdir carries a `commondir` file pointing at the repository all
    of them share, and the shallow boundary belongs to that shared one."""
    dot = os.path.join(gitdir, ".git")
    if os.path.isfile(dot):
        try:
            with open(dot) as handle:
                named = handle.read(4096).strip()
        except OSError:
            return None
        if not named.startswith("gitdir:"):
            return None
        gitdir = os.path.join(gitdir, named[len("gitdir:"):].strip())
    elif os.path.isdir(dot):
        gitdir = dot
    if not os.path.exists(os.path.join(gitdir, "HEAD")):
        return None                     # not a git directory this can read
    link = os.path.join(gitdir, "commondir")
    if not os.path.exists(link):
        return gitdir
    try:
        with open(link) as handle:
            shared = handle.read(4096).strip()
    except OSError:
        return None
    if not shared:
        return None
    shared = os.path.normpath(os.path.join(gitdir, shared))
    return shared if os.path.isdir(shared) else None


def _complete(gitdir, env):
    """Is this repository's history the whole history? -> True, else refuse.

    False for a shallow boundary and None when the layout cannot be resolved,
    and the caller treats both the same way: an answer derived from a truncated
    graph is a different answer to the same question."""
    overlay = env or {}
    if "GIT_SHALLOW_FILE" in overlay:
        if overlay["GIT_SHALLOW_FILE"] is not None:
            return False                # the boundary was moved by the caller
    elif "GIT_SHALLOW_FILE" in os.environ:
        return False                    # …or by the ambient environment
    common = _common_dir(gitdir)
    if common is None:
        return None
    return not os.path.exists(os.path.join(common, "shallow"))


def _declined(env):
    """Has the CALLER declared this read out of the table? -> bool

    THE QUESTION IS NOT ENOUGH TO TELL, which is the correction this clause
    exists for. `dispatches._patch_tip_ancestry` asks `merge-base
    --is-ancestor <oid> <oid>` in the bound common dir under an overlay it
    builds from the same two pieces `rowworld._scrubbed_env` builds its own
    from — the selection scrub and the history view — so the repository, the
    argv and the overlay of the proof's question and of the projection's are
    indistinguishable here. Nothing this module can read separates them, and
    they want OPPOSITE things from one exit code.

    THEY DIFFER IN WHAT THE EXIT CODE IS BEING READ AS. The projection wants
    the ancestry of two fixed ids and an entry gives it that. The proof reads
    git's 128 as "this repository cannot measure it" — an ODB claim, the one
    class this module refuses at every other spelling, and the distinction its
    refusal sentence is made of: a MISSING PARENT OBJECT must come back
    unmeasured, never as a measured non-descendant. A stored 0 or 1 served
    after that object leaves the odb answers a question the repository can no
    longer answer, and `gc --prune=now` on a fleet that lands rebased is how
    the object leaves.

    SO THE CALLER DECLARES ITSELF, ON THE OVERLAY RATHER THAN PER CALL, and a
    word on one argv cannot do this job. The word `_NEVER` holds guards nothing
    here: the proof spells `--end-of-options` on its `rev-parse` reads, which
    this table refuses for their verb whatever the word does, and not on the
    one read the table admits. A marker each call must repeat is a marker the
    next call added to that path does not carry."""
    return bool(env) and UNCACHED in env


def _pinned(argv, env):
    """Is every ambient influence a reader CAN disable, disabled?

    Two clauses. THE HISTORY REWRITERS, in either spelling: the env one
    (`rowworld._history_view_env`) and the argv one (`landreq._NO_REPLACE`).
    A graft file must be pinned at /dev/null either way, because there is no
    argv form for it. AND THE REPOSITORY SELECTION, which no argv form can
    pin at all — a `None` value is the seam's spelling for "remove this one",
    and an ABSENT key means "leave the ambient value alone", so presence is
    asserted as well as emptiness."""
    env = env or {}
    if env.get("GIT_GRAFT_FILE") != os.devnull:
        return False
    for name in _selection_env():
        if name not in env or env[name] is not None:
            return False
    if env.get("GIT_NO_REPLACE_OBJECTS"):
        return True
    return "--no-replace-objects" in argv


def _answers_for(argv):
    """The exit codes that are answers for this question, or None when the
    question itself is not cacheable.

    THE VERB IS FOUND THROUGH `vcs._command_argv`, not by reading argv[0].
    `landreq._object_view` spells the replacement pin as a git-GLOBAL option,
    so its argv leads with `--no-replace-objects` and a naive prefix match
    called every one of those reads uncacheable — the exact caller the pin
    exists to make safe. The strip is IMPORTED rather than retyped for the
    reason the rest of this tree states about `_GIT_SELECTION_ENV`: two copies
    of an option list drift, and the drift is invisible. The import is
    deferred because `vcs` imports THIS module at its top.

    AN OPTION'S VALUE IS AN OPERAND. A word beginning with `-` is not thereby
    exempt: `--merge-base=refs/remotes/origin/main` names a ref, and reading
    only the bare words admitted it. The value half of every `=`-bearing
    option is held to the same object-id rule as a bare operand, and the
    module docstring states what that check does and does not reach.

    The KEY still covers the whole argv, globals included — a global option
    can change the answer (`--no-replace-objects` is the proof), so it is
    stripped for CLASSIFICATION only and never for identity."""
    from . import vcs                    # DEFERRED — vcs imports gitfacts.
    argv = list(vcs._command_argv(argv))
    words = [w for w in argv if not w.startswith("-")]
    if len(words) < 2:                  # a verb with no operand asks about NOW
        return None
    if any(w in _NEVER for w in argv):
        return None
    if not all(_OID.match(w) for w in words[1:]):
        return None                     # a ref, a path or a range: about NOW
    for word in argv:
        if not word.startswith("-"):
            continue
        _name, glued, value = word.partition("=")
        if glued and not _OID.match(value):
            return None                 # a name behind an option is still a name
    best = None
    for prefix, codes in _ANSWERS:
        if tuple(argv[:len(prefix)]) == prefix:
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), codes)
    return best[1] if best else None


def _key(gitdir, argv, env, version):
    """sha256 over everything the answer depends on.

    The repository is keyed by the PATH AS ASKED rather than by its resolved
    common dir, because resolving it costs the spawn this module exists to
    avoid. Two worktrees of one repository therefore keep separate entries —
    a miss, never a wrong answer, and the miss costs exactly what today costs."""
    h = hashlib.sha256()
    h.update(b"helm.gitfacts\0%d\0" % _V)
    h.update(version + b"\0")
    h.update(os.fsencode(gitdir))
    h.update(b"\0")
    for word in argv:
        h.update(os.fsencode(word) + b"\0")
    h.update(b"\0env\0")
    for name, value in sorted((env or {}).items()):
        h.update(os.fsencode(name) + b"=" +
                 (b"\0unset" if value is None else os.fsencode(value)) + b"\0")
    return h.hexdigest()


def _path(digest):
    return os.path.join(_root(), digest[:2], digest[2:])


def lookup(gitdir, args, env):
    """-> (rc, stdout_bytes, stderr_bytes) for a stored answer, else None.

    Never raises into the read: an unreadable, truncated or half-written entry
    is a MISS, and a miss costs exactly what this module is removing."""
    if _DISABLED[0] or _declined(env):
        return None
    argv = [str(a) for a in args]
    if _answers_for(argv) is None or not _pinned(argv, env):
        return None
    if not _complete(gitdir, env):
        return None
    version = _version()
    if version is None:
        return None
    try:
        with open(_path(_key(gitdir, argv, env, version)), "rb") as f:
            blob = f.read()
    except OSError:
        return None
    head, _nl, rest = blob.partition(b"\n")
    try:
        stamp, rc, out_len = head.split(b" ")
        if int(stamp) != _V:
            return None
        rc, out_len = int(rc), int(out_len)
    except ValueError:
        return None
    if out_len > len(rest):
        return None                     # a writer that died mid-write
    return rc, rest[:out_len], rest[out_len:]


def record(gitdir, args, env, rc, out, err):
    """Store one completed answer. Silent on every failure — a cache that
    cannot write is a slow cache, never a broken read."""
    if _DISABLED[0] or _declined(env):
        return
    argv = [str(a) for a in args]
    codes = _answers_for(argv)
    if codes is None or rc not in codes or not _pinned(argv, env):
        return
    if not _complete(gitdir, env):
        return
    version = _version()
    if version is None:
        return
    out = out or b""
    err = err or b""
    if len(out) + len(err) > _MAX_BYTES:
        return
    path = _path(_key(gitdir, argv, env, version))
    blob = b"%d %d %d\n" % (_V, rc, len(out)) + out + err
    tmp = "%s.%d.%x.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def entry_paths():
    """Every file the table currently holds, for the retention plane.
    -> [path], raising on a store it cannot read.

    THE CEILING IS ENFORCED FROM `helm gc`, which is a door and not an
    afterthought. gc already owns the declared-budget table, the class gate
    that keeps a reaper off authored bytes, a dry-run default and an hourly
    timer; this store is one more row in it (`gitfacts`, class exhaust, count
    axis, MAX_ENTRIES). The three doors it was weighed against all cost more
    and prove less: a sampled sweep inside `record` would put a directory walk
    on the MISS path this module exists to shorten and would have to be
    written not to raise into it; a `doctor --ensure` action mutates on a verb
    operators run to READ; and a verb of its own would be a retention policy
    with no scheduler, which is the exact state gc itself was found in after
    it had grown 11,192 items past budget without ever once running.

    IT IS AGE AND IT IS NOT USE, said here so no reader takes the row for an
    LRU. The order is the file's mtime — set by `record` when it writes the
    entry, and never touched by a hit. atime cannot carry it (relatime and
    noatime both make it a lie), and touching a file on every hit would turn a
    ~20µs read into a write, which is the cost this module exists to remove.
    What the order therefore cannot see is an old entry that is still being
    asked for. The module docstring's measurement is why that is harmless
    here and not a compromise: one land orphans the entire generation, so the
    newest MAX_ENTRIES by write time and the entries anything still asks for
    are the same set.

    NOBODY LOCKS, AND NOBODY NEEDS TO. Several helm processes read, write and
    prune this table at the same time. Unlinking an entry a reader is mid-read
    of is safe — `lookup` holds an open descriptor and the inode outlives its
    name. Unlinking one a writer is about to `os.replace` over is safe too:
    the rename re-creates the name whether or not a pruner just removed it,
    and a writer whose temp file is removed first falls into `record`'s silent
    OSError path. Every one of those outcomes costs one git spawn, which is
    what the uncached world costs, so a lock would buy no correctness and
    would put a contended file on the read path.

    A HALF-WRITTEN TEMP IS A FILE THE CEILING COUNTS. `record` writes beside
    the entry and renames over it; a process killed between the two leaves the
    temp behind, and nothing in this tree has ever removed one. Counting the
    STORE rather than the answers inside it is what bounds that residue too,
    on the same write-time axis.

    IT RAISES RATHER THAN REPORTING AN EMPTY STORE, which is gc's own law
    about a victim list: a shard this cannot read, handed back as [], is
    printed inside `helm gc`'s in-budget line and the ceiling quietly stops
    existing. A store that was never written is a different fact and is
    legitimately empty. A shard name is the first two hex digits of the key,
    so there are at most 256 of them, this pruner removes files and never
    directories, and a shard `listdir` has just named is a shard that is still
    there."""
    root = _root()
    try:
        shards = os.listdir(root)
    except FileNotFoundError:
        return []
    found = []
    for shard in sorted(shards):
        path = os.path.join(root, shard)
        if os.path.isdir(path):
            found.extend(os.path.join(path, name)
                         for name in os.listdir(path))
        else:
            found.append(path)          # a stray at the top still holds a block
    return found


def disable():
    """Turn the table off for this process. For a test that must observe the
    uncached behaviour, and for `--no-cache` style callers."""
    with _LOCK:
        _DISABLED[0] = True


def enable():
    with _LOCK:
        _DISABLED[0] = False
