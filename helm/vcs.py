#!/usr/bin/env python3
"""helm vcs — the seam the worktree/ship/capsule/handoff git calls go through.

SCOPE, stated honestly up front: this module owns the version-control calls of
`helm work` (the worktree + lane lifecycle), `helm ship`, `helm capsule`, the
handoff/now probes, the lane gc verdict reads, and automap's root resolver. It
is NOT yet every git call in helm — 51 direct spawns remain outside it.
Script-run hook scanners are standing exceptions because an installed snapshot
cannot import helm; every remaining entry is declared migration debt. Those
are ENUMERATED AND PINNED by `DirectSpawnAuditTest` in tests/test_vcs.py, whose
own detector coverage is itself tested; docs/ARCHITECTURE.md carries the same
count and the same bounded guarantee. Do not describe this seam as total —
describe it as growing, and shrink the audit allowlist to prove it.

Six private `_git` helpers existed inside that scope: capsule.py, landreq.py,
handoff.py, ship.py and work/_lanes.py (twice), plus the worktree add/lock/
remove argv inline in work/_claims.py and work/_gc.py. FIVE are migrated here;
landreq.py's `--git-dir` helper is a declared exception and still stands, so
this module replaced five of six, not six. Even so: six spellings, four
different return shapes, no abstraction — while `helm doctor` has been telling
operators for months that "jj/jujutsu is a git-compatible alternative on the
radar — a future helm may accept either" (doctor.py check_git). This is that
intent's foundation: consolidation FIRST, zero behavior change, so a jj backend
is later an ADDITION rather than a rewrite of every call site.

Two layers, because the call sites genuinely need both:

  * the call primitives — `run` / `text` / `probe` / `capture` / `proc`. Four
    return shapes, and they are NOT redundant: each is one existing helper's
    exact contract, differing in whether output is bytes or text, whether a
    nonzero exit reads as None or as data, and whether a missing binary fails
    OPEN (a probe that must never block a compaction) or LOUD (an operator
    verb like `helm ship`). Unifying them would BE the behavior change this
    slice exists to avoid.
  * the semantic ops — worktrees, add/remove/lock/unlock_worktree, dirty,
    head_sha, base_branch, has_branch, ancestry (with its boolean projection
    is_ancestor), delete_branch, wip_commit, ahead_behind. Every one of them
    has a real call site in helm today, and every one is what a second
    backend must actually reimplement (`jj workspace add` is not
    `git worktree add` with different flags).

Laws:
  * Stdlib only (helm's zero-dependency law) — `subprocess` and nothing else.
  * ZERO behavior change at consolidation time. Where two call sites differed
    subtly, BOTH semantics are kept as separate named ops (see `probe` vs
    `capture`) rather than averaged into one.
  * Selection is per-TARGET, never per-cwd. `backend(target)` takes the repo
    or checkout the operation is about; every caller in this module's scope
    passes one. A cwd-derived answer would pick git for a jj repo the moment a
    verb was invoked from elsewhere — which is most of the time.
  * The DEFAULT is spelled out, never implicit: `git`. Selection order is
    `HELM_VCS=git|jj`, else the nearest `.jj`/`.git` marker walking UP from the
    target, else the default.
  * An unimplemented or garbage selection DEGRADES to git with a one-line
    note on stderr. helm never crashes over a VCS preference.
"""
import collections
import contextlib
import os
import threading
import re
import shutil
import stat
import subprocess
import sys
import time

from . import gitfacts, openflags, projscope

# WHAT A FOLD ASKED GIT, SEEN AT THE ONE DOOR EVERY SEAMED READ PASSES
# (task/2770). The dispatch fold's checkpoint may reuse a close decision only
# while the git facts that decision read still stand, so it must know every
# question the fold put to git and every answer it got — including the answers
# a memo or `gitfacts` served without a spawn, which is why the witness sits on
# `GitVcs.run` and not on `_spawn`. PER-THREAD, like the projection memo: helm
# web folds on several threads at once and one fold's witness must never hear
# another's reads. With no observer installed this costs one attribute lookup.
_OBSERVER = threading.local()


@contextlib.contextmanager
def observed(fn):
    """Route every `GitVcs.run` answer on this thread through
    `fn(cwd, args, env, stdin, answer)` for the duration. Restores the previous
    observer rather than clearing it, so two nested folds compose."""
    prev = getattr(_OBSERVER, "fn", None)
    _OBSERVER.fn = fn
    try:
        yield
    finally:
        _OBSERVER.fn = prev


def _observed(cwd, args, env, stdin, answer):
    fn = getattr(_OBSERVER, "fn", None)
    if fn is not None:
        fn(cwd, args, env, stdin, answer)
    return answer


def unseamed(where):
    """Tell an installed observer that a git read happened OUTSIDE this seam.

    The observer cannot see what such a read asked, so the only honest thing
    it can do with the news is refuse to reuse anything the read decided —
    `answer` None is that signal. A no-op when nothing observes."""
    fn = getattr(_OBSERVER, "fn", None)
    if fn is not None:
        fn(None, (str(where),), None, None, None)


_MISS = object()
_COMMON_DIR_CAP = 512
_COMMON_DIR = collections.OrderedDict()
# THE INSTANT EACH MEMOISED ANSWER WAS MEASURED, keyed like `_COMMON_DIR` and
# evicted with it — what `validated_at` reads and a fresh read re-stamps.
_COMMON_DIR_AT = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_COMMON_DIR": "a bounded LRU of git common dirs keyed by path",
    "_COMMON_DIR_AT": (
        "when each _COMMON_DIR answer was measured, evicted with it"),
}
# helm web is a ThreadingHTTPServer, so every COMPOUND operation on the LRU
# (get-then-move_to_end, insert-then-popitem) is a race: review drove a
# deterministic interleaving where thread A takes a hit, thread B evicts that
# key, and A's move_to_end raises KeyError. The lock covers only the dict
# work — it is NEVER held across the git probe, because a spawn under a global
# lock would serialise every caller in the process.
_COMMON_DIR_LOCK = threading.Lock()

GIT = "git"
JJ = "jj"
DEFAULT = GIT          # explicit — an unset default is what burned helm before


def git_version():
    """The git binary's own version line. -> bytes, or None when unreadable.

    IT LIVES HERE BECAUSE THIS IS WHERE GIT IS SPELLED. `gitfacts` needs it —
    the binary is an operand of every answer that module stores, since merge-ort
    replaced the recursive strategy and patch identity has been revised across
    releases — and a module that spawned git for itself would be a direct spawn
    outside the seam, which is the debt `tests.test_vcs` exists to stop growing.

    IT IS NOT CACHED HERE, DELIBERATELY. The caller needs it once per process
    and holds it at its own door; a second cache would be a second place to be
    wrong about when a swapped binary is noticed.

    No repository is named because none is read: `--version` answers about the
    binary, so it is the one git question with no subject — and therefore the
    one that needs no `-C`, no overlay and no scope.
    """
    try:
        done = subprocess.run([GIT, "--version"], capture_output=True,
                              timeout=30)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or b"").strip() or None

# THE VERBS `GitVcs.run` MAY MEMOISE inside a projection scope. An ALLOWLIST,
# never a denylist of write verbs: a new git verb this file has never heard of
# must default to SPAWNING, because the failure of a too-wide read set is a
# stale answer served to a caller that changed the repo, and the failure of a
# too-narrow one is only a slower pass. Every entry here is a pure query with
# no working-tree, index, ref or object side effect. `worktree` and `config`
# are deliberately ABSENT — both have mutating subcommands (`worktree add`,
# `config --set`) that share the verb with their read forms, and a memo keyed
# on the verb alone cannot tell them apart.
_READ_VERBS = frozenset((
    "merge-base", "cat-file", "cherry", "log", "rev-list", "rev-parse",
    "patch-id", "for-each-ref", "show", "diff-tree", "name-rev",
))


_GLOBAL_VALUE_OPTIONS = frozenset((
    "-c", "-C", "--config-env", "--exec-path", "--git-dir",
    "--namespace", "--super-prefix", "--work-tree",
))
_GLOBAL_FLAG_OPTIONS = frozenset((
    "--bare", "--html-path", "--info-path", "--literal-pathspecs",
    "--man-path", "--no-pager", "--no-replace-objects", "--paginate",
    "--version", "--help",
))


def _command_argv(args):
    """Strip Git-global options and return the command argv."""
    words = tuple(str(a) for a in args)
    i = 0
    while i < len(words):
        word = words[i]
        if word in _GLOBAL_VALUE_OPTIONS:
            i += 2
            continue
        if (word in _GLOBAL_FLAG_OPTIONS
                or any(word.startswith(option + "=")
                       for option in _GLOBAL_VALUE_OPTIONS
                       if option.startswith("--"))):
            i += 1
            continue
        break
    return words[i:]


def _observation_argv(args):
    """May this completed Git call be interrupted by a post-spend check?

    A mutating call admitted before expiry must publish its actual result even if
    the clock crosses while it runs. These exact additional forms are reads but
    cannot join `_READ_VERBS`: their verbs also own mutating subcommands; for
    merge-tree, the read materializes a derived object; and ls-remote observes a
    subject other machines can change, beyond the local-writes-bypass-memo
    freshness argument that makes `_READ_VERBS` safe."""
    words = _command_argv(args)
    symbolic = words[:1] == ("symbolic-ref",) \
        and not any(word in ("--delete", "-m") for word in words[1:]) \
        and len([word for word in words[1:] if not word.startswith("-")]) == 1
    reflog = words[:1] == ("reflog",) \
        and (len(words) == 1 or words[1].startswith("-")
             or words[1] in ("show", "exists"))
    return bool(words) and (words[0] in _READ_VERBS
                            or words[0] in ("diff", "ls-remote", "merge-tree",
                                            "status")
                            or words[:2] == ("worktree", "list")
                            or symbolic or reflog)

# HELM_VCS is read DIRECTLY, not through home.env: the env2 legacy-fallback
# pattern exists for variables that REPLACED a predecessor tool's spelling, and
# this knob is new — there has never been a MELD_VCS. Reading one would
# contradict the "legacy fallback: —" row in docs/ENVIRONMENT.md, and a table
# that disagrees with the code is how an operator gets lied to.
ENV_VAR = "HELM_VCS"

# The marker directories backend detection walks up looking for.
MARKERS = ((JJ, ".jj"), (GIT, ".git"))
_MAX_WALK = 64         # a bounded walk: no pathological mount can spin here

_WT_LIST = ("worktree", "list", "--porcelain", "-z")   # GitVcs-internal argv


# ---------------------------------------------------------------------------
# the interface — the checklist a second backend implements
# ---------------------------------------------------------------------------

# A full object id in either hash size — the observation is only ever
# compared against what git itself printed, so a short or partial answer is
# an unreadable one rather than a shorter true one.
_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

TRUNK_LOCAL, TRUNK_FETCHED = "local", "fetched"


# A trunk SOURCE is a branch on the authority, spelled in full. Nothing else.
_SOURCE_REF = re.compile(r"refs/heads/[^\x00-\x20~^:?*\[\\]+\Z")


def canonical_source_refusal(source_ref):
    """None when `source_ref` is a canonical branch source, else the reason.

    `startswith("refs/")` IS NOT A VALIDATOR and treating it as one reopens
    the hole this seam exists to close. Two measured ways:

    REVISION SYNTAX RESOLVES. `refs/heads/main~1` is not a ref, but `rev-parse`
    happily answers it — with main's PARENT. A declaration nobody can read as a
    branch would then pin a trunk one commit behind, permanently and silently.

    AND A REMOTE-TRACKING REF IS THE STALE SNAPSHOT ITSELF.
    `refs/remotes/origin/main` with no `helm.trunkRemote` behind it is read as a
    LOCAL authority and stamped with a local receipt — the exact cache-as-trunk
    reading the whole freshness argument rejects, smuggled back in through the
    declaration. The source names a branch ON the authority; the destination a
    fetch happens to write locally is not it.

    So: `refs/heads/<name>`, and no revision operators. Tags, remote-tracking
    refs, `~`, `^`, `@{`, `:` and the rest are refused by shape rather than
    discovered to misbehave later."""
    ref = str(source_ref or "")
    if not ref:
        return "no trunk source is declared"
    if not _SOURCE_REF.fullmatch(ref) or ".." in ref or "@{" in ref \
            or ref.endswith(".lock") or "//" in ref or "/." in ref:
        return ("the declared trunk source %r must be a canonical "
                "refs/heads/<branch> — not a remote-tracking ref, a tag, or a "
                "revision expression" % ref[:64])
    return None


# EVERY VARIABLE THAT CAN REDIRECT WHICH REPOSITORY GIT TOUCHES, or inject
# configuration into it. `run` overlays `env` onto the ambient environment
# rather than replacing it, so an authority observation inherits whatever the
# process happens to be carrying — and this seam FETCHES, so a redirected call
# does not merely answer wrongly, it reaches out from the wrong repository.
# MEASURED: with GIT_DIR pointing elsewhere, an observation of one repository
# returned the OTHER repository's tip.
_REPO_SELECTION_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CONFIG", "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT",
    "GIT_ALTERNATE_REFS", "GIT_REPLACE_REF_BASE",
)


def _authority_env():
    """The env overlay every authority call runs under: repository selection
    and config injection REMOVED, prompts disabled.

    `run` pops a key whose value is None, so this is a scrub rather than a
    blanking — an empty GIT_DIR is not reliably the same as no GIT_DIR.
    GIT_CONFIG_KEY_n/VALUE_n are removed by count, because git reads them only
    up to GIT_CONFIG_COUNT and leaving orphaned pairs behind is untidy rather
    than unsafe once the count itself is gone."""
    out = {k: None for k in _REPO_SELECTION_ENV}
    for i in range(32):
        out["GIT_CONFIG_KEY_%d" % i] = None
        out["GIT_CONFIG_VALUE_%d" % i] = None
    out["GIT_TERMINAL_PROMPT"] = "0"
    return out


def _first_line(blob):
    """The first non-empty line of a command's stderr, as text."""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    for line in (blob or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def observe_trunk_authority(root, source_ref, remote=None, timeout=20):
    """(sha, receipt, failure) — the DECLARED trunk authority, observed NOW.

    A REMOTE-TRACKING REF IS A LOCAL SNAPSHOT AND CANNOT TESTIFY TO ITS OWN
    CURRENCY. Reading `refs/remotes/<name>/<branch>` says what the last fetch
    left behind, not what the remote holds, so a predicate that treats it as
    the trunk is proving its claim against a cache. Recording the read as
    honest provenance was considered and rejected: provenance is not freshness
    and it leaves the stale-authority hole exactly where it was.

    SO FRESHNESS LIVES HERE, AT THE COMMAND BOUNDARY, NEVER INSIDE THE
    PREDICATE THAT CONSUMES IT. Two authorities, two receipts:

    LOCAL (`remote` unset or `.`) — resolving the explicit `refs/heads/*`
    source ref IS the receipt. Nothing is over a network, so nothing can be
    stale; `.` means THIS repository and is a declaration, not a remote name to
    be dialled.

    REMOTE — a bounded, non-interactive fetch of the EXACT declared source ref,
    then `ls-remote` for what the remote advertises now, then a check that the
    advertised object EXISTS LOCALLY. The last step is the race: the remote can
    move between the fetch and the advertisement, and an advertised sha we do
    not hold means the observation is torn rather than current. All three must
    agree or the answer is UNKNOWN.

    THE SOURCE REF IS DECLARED, NOT DERIVED FROM A REFSPEC. Fetch refspecs map
    source to destination however the operator configured them, so inferring
    one from the other is the same discovery problem that produced four failed
    trunk resolvers. `--no-write-fetch-head` keeps the observation from
    disturbing FETCH_HEAD, `--no-tags` keeps it from dragging in refs nobody
    asked about, and GIT_TERMINAL_PROMPT=0 plus a timeout keep a dispatch from
    hanging on a credential prompt in a headless seat.

    Every failure — offline, unreachable, unauthenticated, malformed
    advertisement, raced object — returns UNKNOWN with a reason. The callers
    that need proof refuse on it; none of them may fall through."""
    be = backend(root)
    bad = canonical_source_refusal(source_ref)
    if bad:
        return None, None, bad
    if not remote or remote == ".":
        rc, out, _err = be.text(root, "rev-parse", "--verify", "--quiet",
                                str(source_ref), timeout=timeout,
                                env=_authority_env())
        sha = (out or "").strip()
        if rc != 0 or not _FULL_SHA.fullmatch(sha):
            return None, None, ("the declared local trunk authority %s does "
                                "not resolve" % source_ref)
        return sha, TRUNK_LOCAL, None
    env = _authority_env()
    rc, _out, err = be.run(root, "fetch", "--no-write-fetch-head", "--no-tags",
                           str(remote), str(source_ref), timeout=timeout,
                           env=env)
    if rc != 0:
        # ONE LINE OF GIT'S OWN WORDS, rendered as text. The first cut sliced a
        # list and interpolated it, so an operator read `[b"fatal: ..."]` — a
        # Python repr standing where the reason belonged.
        detail = _first_line(err) or "no detail"
        return None, None, ("observing %s on remote %s failed: %s"
                            % (source_ref, remote, detail))
    rc, adv, _err = be.text(root, "ls-remote", str(remote), str(source_ref),
                            timeout=timeout, env=env)
    rows = [l.split() for l in (adv or "").splitlines() if l.strip()]
    exact = [r for r in rows if len(r) >= 2 and r[1] == str(source_ref)]
    if rc != 0 or len(exact) != 1 or not _FULL_SHA.fullmatch(exact[0][0]):
        return None, None, ("remote %s did not advertise exactly one %s"
                            % (remote, source_ref))
    sha = exact[0][0]
    rc, _out, _err = be.text(root, "cat-file", "-e", sha + "^{commit}",
                             timeout=timeout, env=env)
    if rc != 0:
        return None, None, ("remote %s advertises %s which is not present "
                            "locally after the fetch — the observation raced "
                            "a push and is torn, not current"
                            % (remote, sha[:12]))
    return sha, TRUNK_FETCHED, None


class Vcs:
    """What helm needs from a version-control system, and nothing more.

    Subclasses implement every method; `NotImplementedError` here means a
    backend forgot one rather than silently answering wrong.
    """
    name = "?"

    # --- call primitives ---------------------------------------------------
    def run(self, cwd, *args, **kw):
        """(rc, stdout_bytes, stderr_bytes). Byte-preserving: paths are
        filesystem bytes, not UTF-8 text. `timeout`/`env` keywords; `env`
        OVERLAYS the ambient environment for the one call. Spawn trouble is
        rc -1, never an exception."""
        raise NotImplementedError

    def text(self, cwd, *args, **kw):
        """(rc, stdout, stderr) — `run` fsdecoded and stripped, for output
        that is not a path record."""
        raise NotImplementedError

    def probe(self, cwd, *args, **kw):
        """Stripped stdout on success, else None. '' IS an answer (a clean
        status); None means the probe could not speak. Fails OPEN."""
        raise NotImplementedError

    def probe_outcome(self, cwd, *args, **kw):
        """(ran, rc, out) — what the ATTEMPT did. Claims NOTHING about the
        question you asked.

        ran False means NOTHING RAN (no binary, a timeout); rc and out are
        None and the result must never be cached at any TTL. ran True means
        the process completed: rc is its exit status and out is stripped
        stdout on rc 0, else None. Read rc yourself — this op deliberately
        does not interpret it, because the backend's exit statuses are not
        a semantic answer to your question."""
        raise NotImplementedError

    def capture(self, cwd, *args, **kw):
        """Stripped stdout or None, exit status NOT consulted. Fails OPEN."""
        raise NotImplementedError

    def proc(self, cwd, *args, **kw):
        """The CompletedProcess itself, for callers that read returncode,
        stdout AND stderr. Fails LOUD — a missing binary raises."""
        raise NotImplementedError

    # --- worktrees ---------------------------------------------------------
    def worktrees(self, root, read=None):
        """(rows, error). Each row: {path, branch, locked, reason}.

        THE AUTHORITY, not a parser: the backend owns both the argv it issues
        and the format it parses (`jj workspace list` is not `git worktree
        list` with different flags). `read` optionally overrides the raw spawn
        with a `run`-shaped callable — that is a spawn-mechanism override, not
        a format one, so it stays backend-agnostic. work/_lanes.py hands in its
        own git boundary so that module keeps ONE place to reason about its
        spawns (and the suite keeps one place to inject a raw registry)."""
        raise NotImplementedError

    def add_worktree(self, root, path, branch, base=None, env=None):
        """(rc, out, err). base None attaches an EXISTING branch; a base
        creates the branch off it."""
        raise NotImplementedError

    def add_worktree_detached(self, root, path, committish):
        """(rc, out, err) — a DISPOSABLE read-only checkout: detached HEAD at
        exactly `committish`, no branch minted, none attached. Deliberately
        takes NO env overlay: the peek door must pass the ref guard on the
        transaction's own structure, and an env hole here would quietly
        become the bypass the guard exists to not have."""
        raise NotImplementedError

    def remove_worktree(self, root, path):
        """(rc, out, err) — remove the checkout directory and its record."""
        raise NotImplementedError

    def remove_worktree_record(self, root, path):
        """(rc, out, err) — remove exactly one administrative record only.

        The checkout path is never deleted. This is the phantom-record operation:
        policy has already proven the path absent, but a resurrection race must
        still cost metadata repair rather than authored bytes."""
        raise NotImplementedError

    def lock_worktree(self, root, path, reason):
        """(rc, out, err) — do-not-disturb, native enough that even a raw
        prune/remove refuses."""
        raise NotImplementedError

    def unlock_worktree(self, root, path):
        """(rc, out, err)."""
        raise NotImplementedError

    # --- working copy + refs ----------------------------------------------
    def dirty(self, path):
        """Uncommitted/untracked bytes present? Unreadable reads as DIRTY."""
        raise NotImplementedError

    def head_sha(self, path, ref="HEAD", before=None, timeout=20):
        """The sha `ref` names, or None. `before` (a wall-clock string)
        resolves the era sha as of that instant."""
        raise NotImplementedError

    def common_dir(self, path, timeout=10, fresh=False):
        """The MAIN repo's admin dir (absolute) for a path inside a repo or a
        linked worktree, or None — the fold every helm root resolver needs.
        `fresh` asks the filesystem NOW and never a memo: what a mutating act
        asks, so the answer is about the tree as it stands at the act."""
        raise NotImplementedError

    def validated_at(self, path):
        """The monotonic instant a memoised `common_dir` answer for `path` was
        measured, or None when nothing is memoised for it."""
        raise NotImplementedError

    def base_branch(self, root):
        """The integration branch NAME — for creating branches and for naming
        the branch in a hook template. NOT the landedness authority: see
        `trunk_ref`, which answers a different question."""
        raise NotImplementedError

    def trunk_ref(self, root):
        """The ref that answers 'does the FLEET have this?' — remote-tracking
        when a remote exists, else the local branch name."""
        raise NotImplementedError

    def has_branch(self, root, branch):
        """Does this named branch exist at all? (a parked lane re-opens on it.)"""
        raise NotImplementedError

    def ancestry(self, root, tip, ref):
        """ANCESTOR / NOT_ANCESTOR / UNKNOWN — is `tip` contained in `ref`?
        (the merged question, tri-state like `marker_state`: a backend that
        cannot SEE the answer says UNKNOWN, never a verdict. GitVcs.ancestry
        carries the law and the incident.)"""
        raise NotImplementedError

    def is_ancestor(self, root, tip, ref):
        """Boolean PROJECTION of `ancestry` — concrete on purpose: one
        question, one authority. A backend implements the tri-state and gets
        this for free (and a backend that forgot still raises, through
        `ancestry`). True is PROOF of containment; False is only 'no proof' —
        a clean negative OR an unreadable one. A caller whose negative branch
        does anything load-bearing must switch on `ancestry` itself."""
        return self.ancestry(root, tip, ref) == ANCESTOR

    def patch_sequence(self, root, base, tip, timeout=30):
        """Ordered stable patch ids in ``base..tip``, or None when unreadable."""
        raise NotImplementedError

    def patch_sequence_containment(self, root, reviewed, candidate, timeout=30):
        """Typed verdict: does candidate contain reviewed's ordered patches?"""
        raise NotImplementedError

    def landed_state(self, root, tip, ref, cap=None, timeout=30):
        """ANCESTOR / PATCH_EQUIVALENT / NOT_ANCESTOR / UNKNOWN — did the WORK
        in `tip` reach `ref`, by object identity OR by content? (GitVcs carries
        the law, the guard and the measurement.)"""
        raise NotImplementedError

    def delete_branch(self, root, branch, force=False, expect=None):
        """(rc, out, err) — SAFE delete: refuses an unmerged branch. `force`
        is admissible ONLY behind an independent PATCH_EQUIVALENT proof; see
        GitVcs.delete_branch for why that is not a hole in the law.

        `expect` is a COMPARE-AND-DELETE: the delete must refuse unless the
        branch still points at that exact sha. Any backend that cannot offer
        that atomically must refuse the delete outright rather than fall back
        to an unconditional one — a caller passing `expect` is stating that a
        stale decision MUST NOT be spent, and silently ignoring it turns the
        strongest request into the weakest behaviour."""
        raise NotImplementedError

    def wip_commit(self, path, msg):
        """(rc, out, err) — stage everything and commit under the janitor
        identity, hooks bypassed. Strictly loss-reducing."""
        raise NotImplementedError

    def ahead_behind(self, root, base, tip):
        """(behind, ahead) as printed, or None when the read failed."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# git porcelain parsing — git-format knowledge, owned by the git backend
# ---------------------------------------------------------------------------

def parse_worktree_records(rc, out, err):
    """(rows, error) from `git worktree list --porcelain -z` output.

    Moved verbatim from work/_lanes.py `_worktree_records`: `-z` disables C
    quoting and preserves spaces, newlines, backslashes and non-UTF8 path
    bytes, so a truncated or non-NUL-framed stream is an ERROR rather than a
    short success — a registry that looks empty authorizes deletions.
    """
    if rc != 0:
        return [], os.fsdecode(err) or "git worktree list failed"
    if out and not out.endswith(b"\0"):
        return [], "truncated git worktree porcelain"
    rows, cur = [], None
    try:
        for field in out.split(b"\0"):
            if not field:
                cur = None
            elif field.startswith(b"worktree "):
                cur = {"path": os.fsdecode(field[9:]), "branch": None,
                       "head": None, "locked": False, "reason": ""}
                rows.append(cur)
            elif cur is None:
                raise ValueError("worktree field before record")
            elif field.startswith(b"branch "):
                cur["branch"] = os.fsdecode(field[7:])
            elif field.startswith(b"HEAD "):
                # THE CHECKED-OUT COMMIT, which a DETACHED room has and a
                # branch name cannot carry. A room checked out on no branch
                # is invisible to every name-keyed probe, and it is still a
                # place the work lives — so the one field that identifies it
                # is kept rather than dropped on the floor.
                cur["head"] = os.fsdecode(field[5:]).strip().lower()
            elif field == b"locked" or field.startswith(b"locked "):
                cur["locked"] = True
                cur["reason"] = os.fsdecode(field[7:]) if len(field) > 7 else ""
    except (ValueError, UnicodeError) as exc:
        return [], "invalid git worktree porcelain: %s" % exc
    return rows, None


# ---------------------------------------------------------------------------
# GitVcs — today's exact behavior, in one place
# ---------------------------------------------------------------------------

class GitVcs(Vcs):
    """git, spelled once. Every method below is an existing helper's body or
    an existing call site's argv, moved — not rewritten."""
    name = GIT

    # --- call primitives ---------------------------------------------------
    def run(self, cwd, *args, timeout=30, env=None, stdin=None):
        """(rc, stdout_bytes, stderr_bytes) — work/_lanes.py `_git_bytes`.

        `env` OVERLAYS the ambient environment for this one call (never
        replaces it — git needs HOME/PATH/GIT_CONFIG_*). It is how the
        ref-guard is handed the single bit distinguishing the sanctioned
        branch creator from a forbidden one, scoped to the call rather than
        leaked into the process.

        `stdin` is BYTES fed to the child, for the one git verb that reads a
        stream rather than a revision: `git patch-id`. It exists so the
        byte-exact content check can be a normal call through this seam
        instead of a shell pipeline — no shell, no quoting, argv stays a list.

        ONE ANSWER PER READ QUESTION PER PROJECTION, and ONLY for the verbs in
        `_READ_VERBS` and only inside a `projscope.scope()`. Every write verb
        and every call outside a scope goes straight to the spawn, so this
        cannot serve a stale answer to anything that changes the repo.

        THE ENV IS PART OF THE QUESTION, NOT A REASON TO SKIP IT, and the
        distinction the shorter rule collapses is MEASURED. "An `env=` call is
        a WRITE signal, never a read" is true of the ref-guard bit and false of
        the population that actually arrives here: `rowworld._git` hands EVERY read in that module a CONSTANT
        read-view overlay (`_scrubbed_env` — repository selection removed,
        replacement objects and grafts pinned off), and `obligation` and
        `gateimport` do the same with their own constant scrubs. A gate keyed
        on the overlay's PRESENCE excludes every one of them, inside a scope or
        out. MEASURED over one cold `helm lr list` on the off-frontier census: 2587 of 4089 git spawns came through this door, 265 of them
        one `rev-parse --is-shallow-repository` and 265 one
        `rev-parse <trunk>^{commit}` — the same question, per row, all build.
        A write signal is excluded by the verb it rides, not by the presence of
        an overlay: `_READ_VERBS` holds no verb with a mutating subcommand
        (see its own note), so the ref-guard bit reaches `_spawn` because it
        rides `branch`/`update-ref`, and would do so with any env at all.
        So the overlay goes in the KEY, which is the law `landreq._git_key`
        already states one module over: two callers asking one argv under
        different views are asking DIFFERENT questions, and a key that ignored
        the view would serve the first answer to the second.

        WHAT IT IS FOR — the superlinear term in `helm lr list`, measured
        2026-08-06. `_range_is_verbatim_identical` asks `_verbatim_ids` for
        `<merge-base>..<trunk>`: EVERY COMMIT LANDED ON TRUNK SINCE THAT LANE
        FORKED, diffed and patch-id'd in full. That range is identical for
        every lane sharing a merge-base, and it was recomputed once per lane —
        one 2.4 MB diff spawned twelve times in a single pass. The cost per
        row therefore grows with trunk itself, so the projection got slower
        with every land even when no row was added: 865 spawns over 312 rows
        on 2026-07-31, 4140 over 1222 rows on 2026-08-06.
        """
        projscope.spend_or_raise("git memo lookup")
        if args and str(args[0]) in _READ_VERBS:
            # TWO THINGS THE FIRST CUT GOT WRONG, both found in
            # this lane's review and both the same law stated twice.
            #
            # (1) `timeout` IS PART OF THE QUESTION. Omitting it let one argv
            # asked at timeout=1 answer the same argv asked at timeout=30 —
            # projscope.memo's own docstring names this exactly: "an
            # under-specified key is how a memo starts answering a question it
            # was not asked."
            #
            # (2) AN UNKNOWN IS NEVER AN ANSWER, SO IT IS NEVER CACHED. `_spawn`
            # returns rc -1 for ANY spawn trouble — timeout, OSError, a missing
            # binary — and `text`'s contract is that "-1 so every caller fails
            # toward its SAFE verdict". Memoising a -1 lets ONE transient blip
            # poison every identical question for the rest of the projection,
            # and each subsequent caller then fails safe on a stale non-answer
            # rather than on its own reading. A cached UNKNOWN is worse than no
            # cache: it is indistinguishable from a measured one.
            # (3) AND THE ENV OVERLAY IS PART OF THE QUESTION, sorted so two
            # dicts that say the same thing key the same. A value may be None
            # (the seam's spelling for "remove this variable"), which is
            # hashable and distinct from the absent key, so "unset GIT_DIR"
            # and "leave GIT_DIR alone" are two questions here as they are two
            # questions to git.
            key = ("vcs.run", cwd, tuple(str(a) for a in args), stdin, timeout,
                   tuple(sorted(env.items())) if env else None)
            hit = projscope.memo(
                key, lambda: self._durable(cwd, args, timeout, env, stdin))
            projscope.spend_or_raise("git memo result")
            if hit[0] == -1:
                projscope.forget(key)
            return _observed(cwd, args, env, stdin, hit)
        return _observed(cwd, args, env, stdin,
                         self._durable(cwd, args, timeout, env, stdin))

    def _durable(self, cwd, args, timeout, env, stdin):
        """The CROSS-PROCESS half of the same idea, under the per-pass memo.

        WHY BOTH, rather than one cache. The memo above collapses repeats
        within one projection and is dropped when it ends; `gitfacts` answers
        the questions whose answer cannot change at all, and outlives the
        process. Measured on one `dispatches.rows()` over 4381 rows:
        with a scope open, 786 spawns of which 784 were a DISTINCT
        question, so there was nothing left for the memo to collapse and every
        one of those 784 was re-asked by the next read of an unchanged
        repository.

        IT SITS BELOW THE MEMO AND BESIDE IT, DELIBERATELY, and the second arm
        is not decoration. The memo's gate reads `args[0]` against
        `_READ_VERBS`, so a call whose argv LEADS WITH A GIT-GLOBAL OPTION
        matches no verb at all: `landreq._object_view` spells its replacement
        pin as `--no-replace-objects` before the verb, and every such read
        falls straight through. So does any read taken with no scope open.
        Routing this layer from BOTH arms is what lets those be answered once
        ever, without widening `_READ_VERBS` — which would change what the
        per-pass memo caches too, and that set was reasoned about separately.

        A CALL WITH STDIN IS NOT OFFERED. `git patch-id` is the one such
        caller, its answer IS pure, and hashing its input would be sound; it
        is left out because it did not appear in the measurement that
        motivated this, and a cache admits a question only once the question
        has been counted.

        `gitfacts` decides what is cacheable — see its module docstring for
        the whole argument. Nothing here needs to know, and a call it refuses
        costs one dict lookup and a regex."""
        if stdin is not None:
            return self._spawn(cwd, args, timeout, env, stdin)
        hit = gitfacts.lookup(cwd, args, env)
        if hit is not None:
            return hit
        rc, out, err = self._spawn(cwd, args, timeout, env, stdin)
        gitfacts.record(cwd, args, env, rc, out, err)
        return rc, out, err

    def _spawn(self, cwd, args, timeout, env, stdin):
        """The bare spawn — the memoising door above is the only reason this
        is a separate method.

        A `None` VALUE IN THE OVERLAY MEANS REMOVE (helm task/744,
        T13). The overlay could previously only ADD a variable, and there are
        variables whose danger is their PRESENCE: `GIT_DIR` and its siblings
        SELECT the repository, and `git -C <dir>` does not beat them — so a
        caller reading an explicitly named repository under an ambient
        `GIT_DIR` silently read a different one. There was no spelling for
        "run this without that variable" short of leaving the seam and
        spawning directly, which is what `dispatches` had to do.

        An overlay that unsets is still an overlay: HOME, PATH and
        GIT_CONFIG_* survive, because replacing the environment wholesale is
        how git reads get subtly wrong on a box configured differently from
        the test that proved them.

        AND ONE KEY IS THE SEAM'S, NOT GIT'S. `gitfacts.UNCACHED` is how a
        read path that must RE-MEASURE declares itself to the durable table,
        and it rides on this overlay so that every call that path makes
        carries it. It names nothing git reads, so it is removed here rather
        than exported: an unknown variable in a child environment is a thing a
        reader has to rule out, and the next tool spawned under it inherits
        it."""
        child = None
        if env:
            child = dict(os.environ)
            for key, value in env.items():
                if key == gitfacts.UNCACHED:
                    child.pop(key, None)
                elif value is None:
                    child.pop(key, None)
                else:
                    child[key] = value
        left = projscope.spend_or_raise("git subprocess")
        observation = _observation_argv(args)
        ambient_limited = observation and left is not None \
            and (timeout is None or left <= timeout)
        child_timeout = left if ambient_limited else timeout
        try:
            r = subprocess.run([GIT, "-C", cwd] + list(args),
                               capture_output=True, timeout=child_timeout,
                               input=stdin, env=child)
        except subprocess.TimeoutExpired as exc:
            if ambient_limited:
                raise projscope.Expired(
                    "budget spent during git subprocess") from exc
            if observation:
                projscope.spend_or_raise("git caller timeout result")
            return -1, b"", os.fsencode(str(exc))
        except Exception as exc:
            if observation:
                projscope.spend_or_raise("git subprocess failure result")
            return -1, b"", os.fsencode(str(exc))
        if observation:
            projscope.spend_or_raise("git subprocess result")
        return r.returncode, r.stdout, r.stderr

    def text(self, cwd, *args, timeout=30, env=None, stdin=None):
        """(rc, out, err) — work/_lanes.py `_git`. Spawn trouble reads as
        rc -1 so every caller fails toward its SAFE verdict."""
        rc, out, err = self.run(cwd, *args, timeout=timeout, env=env,
                                stdin=stdin)
        return rc, os.fsdecode(out).strip(), os.fsdecode(err).strip()

    def _capture(self, cwd, args, timeout):
        """(rc, stripped stdout), or None when the spawn itself failed."""
        left = projscope.spend_or_raise("git capture subprocess")
        ambient_limited = left is not None and (timeout is None or left <= timeout)
        child_timeout = left if ambient_limited else timeout
        try:
            p = subprocess.run([GIT, "-C", cwd] + list(args),
                               capture_output=True, text=True,
                               timeout=child_timeout)
        except subprocess.TimeoutExpired as exc:
            if ambient_limited:
                raise projscope.Expired(
                    "budget spent during git capture subprocess") from exc
            projscope.spend_or_raise("git capture caller timeout result")
            return None
        except Exception:
            projscope.spend_or_raise("git capture failure result")
            return None
        projscope.spend_or_raise("git capture result")
        return p.returncode, p.stdout.strip()

    def probe_outcome(self, cwd, *args, timeout=None):
        """(ran, rc, out) — the distinction `probe` throws away, and NO MORE
        than that.

        `probe` folds a completed-nonzero and a failed spawn into one None.
        `_capture` has always known which happened; this exposes it.

        WHAT THIS DOES NOT TELL YOU. Each of these is measurable in two lines
        and each one defeats a reading this op invites:

        * ran True is NOT "git answered your question" — only "the process
          completed". An expected negative and an operational fault are not
          separated by it.
        * rc 128 covers BOTH "not a repository" AND "that cwd does not
          exist": both answer (128, '') byte-identically. A non-repo and a
          missing directory are indistinguishable here, so an unmounted home
          is NOT diagnosable through this op.
        * rc 0 does NOT validate argv: `rev-parse --not-a-real-flag` exits
          ZERO and echoes the flag, so a malformed question yields
          (True, 0, '--not-a-real-flag') — the caller's own flag, not data.
        * a completed nonzero is a fact about NOW, not an immutable one: a
          plain directory answers (True, 128, None) and the SAME PATH answers
          (True, 0, <root>) once `git init` runs in it, so anything caching
          it owes invalidation on the directory and .git lifecycle.

        THE ONE GUARANTEE, and the only reason this op exists: ran False
        means nothing spoke, so it must never be cached at any TTL — a cached
        unreadable is indistinguishable from one that was measured.
        Everything else here is raw material for a caller that knows which rc
        its own question earns.
        Class: premise negative-and-unreadable-must-not-share-a-value."""
        got = self._capture(cwd, args, timeout)
        if got is None:
            return False, None, None
        rc, out = got
        return True, rc, (out if rc == 0 else None)

    def probe(self, cwd, *args, timeout=None):
        """handoff.py `_git`: stripped stdout on rc 0 — '' is a LEGITIMATE
        answer (a clean `status --porcelain`) and must not fold to None;
        nonzero, missing git and timeout all read as None. A probe must never
        block a compaction, so every exception is swallowed. Deliberately
        UNCHANGED: a caller that needs the outcome apart asks probe_outcome,
        and every existing caller keeps its exact semantics."""
        return self.probe_outcome(cwd, *args, timeout=timeout)[2]

    def capture(self, cwd, *args, timeout=None):
        """capsule.py `_git`: stripped stdout OR None, with the exit status
        deliberately NOT consulted — the era read takes whatever git printed,
        and empty output folds to None.

        Kept as its own op rather than a flag on `probe`: the two helpers
        really do disagree on 'exit 0 with empty output' (None here, '' there),
        and that difference is load-bearing at both call sites.
        """
        got = self._capture(cwd, args, timeout)
        return None if got is None else (got[1] or None)

    def proc(self, cwd, *args, timeout=None):
        """ship.py `_git`: the CompletedProcess itself (its callers read
        returncode, stdout AND stderr), no timeout by default and NO exception
        guard — `helm ship` is an operator verb that must fail loudly, never
        fail open on a box with no git."""
        left = projscope.spend_or_raise("git process")
        observation = _observation_argv(args)
        ambient_limited = observation and left is not None \
            and (timeout is None or left <= timeout)
        child_timeout = left if ambient_limited else timeout
        try:
            answer = subprocess.run(
                (GIT, "-C", cwd) + args, capture_output=True,
                text=True, timeout=child_timeout)
        except subprocess.TimeoutExpired as exc:
            if ambient_limited:
                raise projscope.Expired("budget spent during git process") from exc
            raise
        if observation:
            projscope.spend_or_raise("git process result")
        return answer

    # --- worktrees ---------------------------------------------------------
    def worktrees(self, root, read=None):
        """(rows, error) — the git-native registry, git's argv and git's parse
        both owned here. ZERO new state files: registry drift is
        unrepresentable when git IS the registry."""
        return parse_worktree_records(
            *(read or self.run)(root, *_WT_LIST, timeout=10))

    def add_worktree(self, root, path, branch, base=None, env=None):
        """(rc, out, err). Two forms, because work.claim distinguishes them: a
        PARKED lane branch re-opens in place (base None), a new lane mints its
        branch off the integration base (`-b`)."""
        args = (["worktree", "add", path, branch] if base is None
                else ["worktree", "add", "-b", branch, path, base])
        return self.text(root, *args, env=env)

    def add_worktree_detached(self, root, path, committish):
        return self.text(root, "worktree", "add", "--detach", path, committish)

    def remove_worktree(self, root, path):
        return self.text(root, "worktree", "remove", path)

    def remove_worktree_record(self, root, path):
        """Remove one linked-worktree admin record without touching its files.

        `git worktree remove` recursively deletes a checkout that reappears after
        the caller's absence proof. The metadata record is therefore resolved by
        its exact `gitdir` pointer, atomically moved outside Git's `worktrees/`
        scan, checked once more for a racing lock, and only then discarded. A
        crash after the move leaves a recoverable quarantine, never lost bytes."""
        common = self.common_dir(root)
        if not common:
            return -1, "", "git common directory is unreadable"
        records = os.path.join(common, "worktrees")
        target = os.path.normcase(os.path.normpath(
            os.path.abspath(os.path.join(path, ".git"))))
        try:
            entries = list(os.scandir(records))
        except FileNotFoundError:
            return 1, "", "worktree record not found"
        except OSError as exc:
            return -1, "", "worktree registry unreadable: %s" % exc
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                flags = openflags.flags(
                    os.O_RDONLY, "O_NOFOLLOW", "O_NONBLOCK", cloexec=True)
                fd = os.open(os.path.join(entry.path, "gitdir"), flags)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        continue
                    raw = os.read(fd, 8193)
                finally:
                    os.close(fd)
                if len(raw) > 8192 or b"\0" in raw:
                    continue
                pointer = os.fsdecode(raw.rstrip(b"\r\n"))
                if not os.path.isabs(pointer):
                    pointer = os.path.join(entry.path, pointer)
                got = os.path.normcase(os.path.normpath(os.path.abspath(pointer)))
            except (OSError, UnicodeError):
                continue
            if got != target:
                continue
            if os.path.lexists(os.path.join(entry.path, "locked")):
                return 1, "", "worktree record is locked"
            quarantine_root = os.path.join(common, "helm-pruned-worktrees")
            quarantine = None
            moved = False
            try:
                os.makedirs(quarantine_root, mode=0o700, exist_ok=True)
                quarantine = os.path.join(
                    quarantine_root, "%s.%d" % (entry.name, os.getpid()))
                suffix = 0
                while os.path.lexists(quarantine):
                    suffix += 1
                    quarantine = os.path.join(
                        quarantine_root, "%s.%d.%d" %
                        (entry.name, os.getpid(), suffix))
                os.replace(entry.path, quarantine)
                moved = True
                if os.path.lexists(os.path.join(quarantine, "locked")):
                    os.replace(quarantine, entry.path)
                    return 1, "", "worktree record became locked"
                shutil.rmtree(quarantine)
            except OSError as exc:
                suffix = ("; partial administrative quarantine retained"
                          if moved else "")
                return -1, "", "worktree record removal failed: %s%s" % (
                    exc, suffix)
            return 0, "", ""
        return 1, "", "worktree record not found"

    def lock_worktree(self, root, path, reason):
        return self.text(root, "worktree", "lock", path, "--reason", reason)

    def unlock_worktree(self, root, path):
        return self.text(root, "worktree", "unlock", path)

    # --- working copy + refs ----------------------------------------------
    def dirty(self, path):
        """Uncommitted/untracked bytes in the checkout? An unreadable checkout
        reads as DIRTY — callers must fail toward rescue, never toward
        discard."""
        rc, out, _err = self.text(path, "status", "--porcelain")
        return True if rc != 0 else bool(out)

    def head_sha(self, path, ref="HEAD", before=None, timeout=20):
        """The sha `ref` resolves to, or None. `before` is capsule's era read
        (`rev-list -1 --before <when>`): the commit the session's repo was on
        at its last activity. Goes through `capture`, so a nonzero git is not
        specially handled — that is the pre-existing contract."""
        args = ["rev-list", "-1"] + (["--before", before] if before else []) \
            + [ref]
        return self.capture(path, *args, timeout=timeout)

    def common_dir(self, path, timeout=10, fresh=False):
        """`rev-parse --path-format=absolute --git-common-dir` — the MAIN repo's
        `.git` even when `path` is a linked worktree, which is how helm folds a
        worktree back to the project that owns it. None when the read fails.

        Selection does NOT depend on this: `detect()` reads markers off the
        filesystem and calls nothing here, so a root resolver may sit behind the
        seam without a cycle.

        MEMOISED, AND THE THREE REASONS IT IS SAFE HERE WHEN A REF READ WOULD
        NOT BE. (1) The answer is a property of the PATH-TO-REPO BINDING, not
        of repository CONTENT: a commit, a fetch, a branch move, a rebase — none
        of them change which .git owns a path. A ref memo has to reason about a
        trunk moving underneath it; this one does not. (2) ONLY SUCCESSES ARE
        CACHED. `probe` folds a missing git, a nonzero exit and a timeout all to
        None, so None is UNREADABLE rather than "no common dir" — caching it
        would make a transient failure permanent and indistinguishable from a
        measured answer, which is the one thing a cache must never do here.
        (3) BOUNDED AND LOCKED. The web server lives for days and a test run mints
        thousands of temp repos, so this is an LRU with a hard cap, never a
        dict that grows for the life of the process.

        THE HAZARD THAT SURVIVES A READ, stated because it is real: the key is
        the RESOLVED path, so a path DELETED — or deleted and RECREATED as a
        DIFFERENT repository — inside one process returns the binding measured
        while the old tree stood. A READER may live with that (mkdtemp
        uniqueness makes it vanishingly rare, and reuse of a live repo path is
        not a thing production does). AN ACT MAY NOT: a receipt was admitted
        for a checkout that no longer existed because the door validating that
        checkout asked this memo, which still held the answer from when it did.
        So the memo CARRIES ITS VALIDATION INSTANT (`validated_at`) and a
        mutating act asks with `fresh=True`, which never reads the memo, probes
        the filesystem now, and re-stamps the entry with what it measured —
        the memo is re-read at the moment of the act, never trusted for it.

        MEASURED 2026-08-26: 1,197 spawns in tests/test_lr_close alone, via
        automap._git_root <- work/_lanes.find_root, all re-resolving the SAME
        root."""
        key = (type(self), os.path.realpath(path))
        if not fresh:
            with _COMMON_DIR_LOCK:
                hit = _COMMON_DIR.get(key, _MISS)
                if hit is not _MISS:
                    _COMMON_DIR.move_to_end(key)
                    return hit
        got = self.probe(path, "rev-parse", "--path-format=absolute",
                         "--git-common-dir", timeout=timeout)
        if got is None:
            return None            # UNREADABLE is never cached — see (2) above
        with _COMMON_DIR_LOCK:
            # DOUBLE-CHECK: another thread may have resolved the same path
            # while this one was probing. Prefer the entry already there so
            # two threads cannot disagree about one binding — unless THIS read
            # was the fresh one, whose whole point is to replace what stands.
            cur = _COMMON_DIR.get(key, _MISS)
            if cur is not _MISS and not fresh:
                _COMMON_DIR.move_to_end(key)
                return cur
            _COMMON_DIR[key] = got
            _COMMON_DIR.move_to_end(key)
            _COMMON_DIR_AT[key] = time.monotonic()
            if len(_COMMON_DIR) > _COMMON_DIR_CAP:
                evicted, _value = _COMMON_DIR.popitem(last=False)
                _COMMON_DIR_AT.pop(evicted, None)
        return got

    def validated_at(self, path):
        """The monotonic instant the memo's answer for `path` was measured, or
        None when the memo holds no answer — the reading a caller compares to
        prove an act re-read the filesystem rather than the entry."""
        key = (type(self), os.path.realpath(path))
        with _COMMON_DIR_LOCK:
            if _COMMON_DIR.get(key, _MISS) is _MISS:
                return None
            return _COMMON_DIR_AT.get(key)

    def base_branch(self, root):
        """The integration branch: main when it exists, else the shared
        checkout's own HEAD.

        THE SHARED CHECKOUT'S HEAD, NOT THE ONE `root` HAPPENS TO BE. Inside a
        linked lane worktree a bare `symbolic-ref HEAD` answers that room's
        own branch, so on a repo trunked on anything but `main` the lane
        became its own trunk: every tip read as already landed on it, its own
        commits read as belonging to another lane, and `trunk_ref` handed that
        branch to every landedness question asked from inside the room.

        ASKED LIVE OF THE COMMON GIT DIR, AND SHORTENED BY GIT. The common dir's
        HEAD is the shared checkout's (or a bare repository's own) whichever
        worktree asks. It is resolved by a fresh `rev-parse --git-common-dir`,
        never the path-keyed `common_dir` memo, which keeps a deleted path's old
        binding when the path is recreated as another repository. The name comes
        from `symbolic-ref --short`, which qualifies a branch a same-named tag
        would shadow (`heads/master`), so every ancestry read built on it names
        the branch and never the tag, and a bare repository's symbolic HEAD is
        read like any other. A detached HEAD answers `main`, as it always has;
        a common dir git cannot report falls back to this checkout's own HEAD."""
        if self.text(root, "rev-parse", "--verify", "-q",
                     "refs/heads/main")[0] == 0:
            return "main"
        rc, common, _err = self.text(root, "rev-parse", "--path-format=absolute",
                                     "--git-common-dir")
        shared = ("--git-dir=" + common,) if rc == 0 and common else ()
        rc, out, _err = self.text(root, *shared, "symbolic-ref", "--short", "HEAD")
        return out if rc == 0 and out else "main"

    def trunk_ref(self, root):
        """`origin/<base>` when it exists, else `<base>` — THE landedness ref.

        LOCAL `main` AND `origin/main` ARE DIFFERENT CLAIMS AND ONLY THE SECOND
        MEANS LANDED. A merge you performed sits on local main immediately; the
        trunk everyone else reads does not have it until a PUSH. Asking the
        local ref fails in BOTH directions and the dangerous one is silent:

          FALSE UNLANDED — the trunk has the work but this checkout is behind,
          so proven-landed rooms read 'landing unproven' and accumulate forever
          (measured 2026-07-30: 3 lanes on the trunk, 14 rooms, and a ledger
          where 89 of 97 rows pointed at branches that no longer existed).

          FALSE LANDED — work merged into local main and NEVER PUSHED reads
          ANCESTOR, so `_merged` authorizes `branch -d` and room removal for a
          fix the fleet does not have. That window was open for hours on
          2026-07-30, when a seat merged an approved lane to local main,
          verified with `--is-ancestor <tip> main` -> TRUE, released its
          landlock, let the branch auto-delete as PROVEN, and told the room the
          red was closed. origin/main was 4 commits behind and still red.

        The tri-state in `ancestry` is built so 'cannot see' never authorizes a
        delete — and it is defeated entirely by asking the wrong ref, because a
        confident WRONG answer walks straight past a guard that only defends
        against uncertainty.

        STALENESS IS A THIRD WRONG ANSWER THAT LOOKS RIGHT: a remote-tracking
        ref is itself a local snapshot, so callers that act destructively must
        fetch first. Naming the ref (and its sha) in the verdict is what makes
        a stale answer diagnosable instead of merely wrong.

        Same resolution `dispatches.py` already uses for the trunk-name probe;
        this lifts it to the seam so the landedness path stops re-deciding it.
        Falls back to the local name when there is no remote at all — a
        LOCAL-audience repo with no remote has no other trunk, and refusing
        there would break the only case where local main IS the trunk."""
        base = self.base_branch(root)
        if not base:
            return "main"
        if self.text(root, "rev-parse", "--verify", "-q",
                     "refs/remotes/origin/" + base)[0] == 0:
            return "origin/" + base
        return base

    def has_branch(self, root, branch):
        return self.text(root, "rev-parse", "--verify", "-q",
                         "refs/heads/" + branch)[0] == 0

    def ancestry(self, root, tip, ref):
        """ANCESTOR / NOT_ANCESTOR / UNKNOWN — `merge-base --is-ancestor`
        with its exit code spelled out instead of folded.

        TRI-STATE on purpose — the same law as `marker_state` below, on the
        object-store substrate: a check that cannot see a case must return
        UNKNOWN, never a verdict. git exits 0 for an ancestor, 1 for a GENUINE
        non-ancestor, and 128 for a question it could not answer (bad/missing
        object, unreadable repo — e.g. a tip orphaned by a history rewrite);
        spawn trouble reads as rc -1 through `text` and is equally UNKNOWN.

        The old boolean here folded 1 and 128 together, and that fold IS the
        phantom-unlanded-lanes incident: a land-audit read tips whose objects
        were simply GONE as "not merged" and reported finished lanes as
        unlanded. "I cannot see this tip" and "this tip is not merged" are
        different facts, and only the CALLER knows which way its decision must
        fail — a reap must treat UNKNOWN as not-safe, a warning as worth
        saying. So the seam hands over all three states and folds nothing.

        INSIDE A PROJECTION SCOPE the answer comes from two batched sets
        (reachable-from-`ref`, object-exists) instead of one spawn per pair —
        `helm lr list` asked this question 4449 times through subprocesses at
        ~30ms each and spent 140 of its 155 seconds there (cProfile,
        2026-08-07). The batch preserves the tri-state EXACTLY: reachable is
        ANCESTOR, present-but-unreachable is NOT_ANCESTOR, and a sha in
        NEITHER set is UNKNOWN — the vanished-object case that merge-base
        answers with 128, kept distinct because folding it into "not an
        ancestor" IS the phantom-unlanded-lanes incident above. Either set
        failing to build falls back to the per-pair spawn, never to a guess.
        """
        got = self._batched_ancestry(root, tip, ref)
        if got is not None:
            return got
        rc = self.text(root, "merge-base", "--is-ancestor", tip, ref)[0]
        if rc == 0:
            return ANCESTOR
        return NOT_ANCESTOR if rc == 1 else UNKNOWN

    def _batched_ancestry(self, root, tip, ref):
        """The projection-scoped batch answer, or None for "ask per pair".

        None on: no open scope (a lone caller must not pay a full rev-list),
        a tip that is not one full sha (only exact object names can use set
        membership), or either set unavailable. The POSITIVE set is keyed by
        (root, ref) because it answers reachability from that ref; the object
        set is keyed by root alone because existence has no ref."""
        from . import projscope
        if not projscope.active():
            return None
        sha = str(tip).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            return None
        reach = self._sha_set(root, ("rev-list", str(ref)),
                              ("vcs.revset", root, str(ref)))
        if reach is None:
            return None
        if sha in reach:
            return ANCESTOR
        objs = self._sha_set(root, ("cat-file", "--batch-check=%(objectname)",
                                    "--batch-all-objects", "--unordered"),
                             ("vcs.objset", root))
        if objs is None:
            return None
        return NOT_ANCESTOR if sha in objs else UNKNOWN

    def _sha_set(self, root, args, key):
        """frozenset of shas from one git listing, memoized per projection.

        A failed or EMPTY listing memoizes as None and is forgotten — an empty
        set would answer every membership question NOT_ANCESTOR-ward, which is
        the wrong-scope-model failure (a guard that false-negatives is trusted
        while blind), and rc-space already has run()'s never-cache-an-UNKNOWN
        law. Forgetting keeps a transient failure from poisoning the scope."""
        from . import projscope
        def compute():
            rc, out, _err = self.run(root, *args, timeout=60)
            if rc != 0:
                return None
            got = frozenset(out.decode("ascii", "replace").lower().split())
            return got or None
        hit = projscope.memo(key, compute)
        if hit is None:
            projscope.forget(key)
        return hit

    def patch_sequence(self, root, base, tip, timeout=30):
        """The complete ordered stable patch-id sequence in ``base..tip``.

        The base must be an ancestor of the tip. Every commit in the range must
        emit exactly one patch id: an empty commit, merge commit, malformed
        stream, missing object, over-cap range or spawn failure returns None. A
        partial sequence is worse than no answer because containment would become
        easier to prove.
        """
        if self.ancestry(root, base, tip) != ANCESTOR:
            return None
        rc, out, _err = self.text(root, "rev-list", "--reverse",
                                  "%s..%s" % (base, tip), timeout=timeout)
        commits = out.split() if rc == 0 else []
        if rc != 0 or any(not _FULL_SHA.fullmatch(c) for c in commits) \
                or len(commits) != len(set(commits)) \
                or len(commits) > PATCH_SEQUENCE_CAP:
            return None
        if not commits:
            return ()
        rc, diff, _err = self.run(
            root, "-c", "diff.noprefix=false", "log", "--reverse", "-p",
            "--no-renames", "--diff-algorithm=myers", "--unified=3",
            "--inter-hunk-context=0", "--src-prefix=a/", "--dst-prefix=b/",
            "--no-ext-diff", "--no-textconv", "--pretty=format:commit %H",
            "%s..%s" % (base, tip), timeout=timeout)
        if rc != 0 or not diff:
            return None
        rc, out, _err = self.text(root, "patch-id", "--stable", stdin=diff,
                                  timeout=timeout)
        if rc != 0:
            return None
        ids = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 2 or not _FULL_SHA.fullmatch(parts[0]) \
                    or not _FULL_SHA.fullmatch(parts[1]) or parts[1] in ids:
                return None
            ids[parts[1]] = parts[0]
        if set(ids) != set(commits):
            return None
        return tuple(ids[c] for c in commits)

    def patch_sequence_containment(self, root, reviewed, candidate, timeout=30):
        """Typed, unique contiguous containment of one reviewed patch train.

        The unique merge base defines both histories. A repeated match is
        AMBIGUOUS, not stronger evidence: patch-id drops location, so two
        placements cannot tell which bytes the review names. Empty reviewed
        work supplies no evidence. UNKNOWN never authorizes.
        """
        rc, out, _err = self.text(root, "merge-base", "--all", reviewed,
                                  candidate, timeout=timeout)
        bases = out.split() if rc == 0 else []
        if len(bases) != 1 or not _FULL_SHA.fullmatch(bases[0]):
            return PATCH_SEQUENCE_UNKNOWN, None, None, None
        base = bases[0]
        needle = self.patch_sequence(root, base, reviewed, timeout)
        train = self.patch_sequence(root, base, candidate, timeout)
        if needle is None or train is None:
            return PATCH_SEQUENCE_UNKNOWN, None, None, None
        if not needle:
            return PATCH_SEQUENCE_EMPTY, None, 0, len(train)
        if needle == train:
            return PATCH_SEQUENCE_EXACT, 0, len(needle), len(train)
        width = len(needle)
        hits = [i for i in range(len(train) - width + 1)
                if train[i:i + width] == needle]
        if len(hits) == 1:
            return PATCH_SEQUENCE_CONTAINED, hits[0], width, len(train)
        if len(hits) > 1:
            return PATCH_SEQUENCE_AMBIGUOUS, None, width, len(train)
        return PATCH_SEQUENCE_ABSENT, None, width, len(train)

    def landed_state(self, root, tip, ref, cap=None, timeout=30):
        """ANCESTOR / PATCH_EQUIVALENT / NOT_ANCESTOR / UNKNOWN — did the WORK
        in `tip` reach `ref`?

        ANCESTRY ASKS THE WRONG QUESTION AND GETS A TRUTHFUL 'NO'. Almost
        nothing lands under the sha its author wrote: the integrator REBASES
        the chain onto current trunk and gates the rebased tree, so the commit
        on trunk is patch-identical and object-different. `merge-base
        --is-ancestor` then correctly answers "that exact object is not
        reachable", the reaper reads NOT LANDED, and the well-behaved lane
        keeps its branch FOREVER. Every agent following the rule correctly
        accumulates lanes — measured on this repo 2026-08-03: 107 `lane/*`
        branches, 20 reachable by ancestry, and 7 whose entire content was
        already on trunk under different shas and therefore invisible to the
        only predicate the reaper had.

        The refuted case, kept because it is the whole reason this is a seam
        op and not a boolean: lane/orca-seam-s1-doorway tip 5905f65 against
        origin/main -> `--is-ancestor` rc 1, while `git patch-id --stable` on
        5905f65 and landed ba5e740 both print the content identity registered
        in docref_guard.PATCH_IDS. Same content, different object. Only patch
        identity could see it.

        `git cherry <upstream> <head>` IS THAT COMPARISON, in git's own
        plumbing: it patch-ids every commit in `upstream..head` and prints
        '-' for one whose patch already exists upstream, '+' for one that does
        not. Preferred over hand-rolled `patch-id` plumbing — landreq.py's
        `_landing_proof` already proved the primitive here (measured
        2026-08-02: 1.2ms for ancestry, 3.4ms for cherry at distance 10,
        44.1ms at distance 577; cost scales with DISTANCE FROM TRUNK, not repo
        size).

        THIS IS ASKED OF A BRANCH, NOT A COMMIT, and that is the difference
        from `_landing_proof`. A lane retires only when EVERY commit it carries
        is upstream, so a single '+' is decisive against retirement — landing
        SOME of a stack is exactly the state that must keep its branch.
        Measured on the same 107: 8 lanes are mixed.

        THE COUNT CROSS-CHECK IS THE SAFETY BELT, and it is not theoretical.
        `git cherry` is `rev-list --no-merges` underneath, so it says NOTHING
        about a merge commit — a branch whose only unlanded commit is a merge
        produces EMPTY output, which a naive "no '+' lines means landed" reader
        would call retirable. Measured 2026-08-03 on this repo: exactly that
        shape, three times (lane/aspublic-gate, lane/lr-merge-prep,
        lane/never-track-needle-breadth — one merge commit each, all genuinely
        uncontained). So the range is counted independently and every commit in
        it must be ACCOUNTED FOR by a cherry line; any shortfall is UNKNOWN.
        A reaper that had shipped without this would have deleted three
        branches holding real work and called it success.

        THE PRIMITIVE'S OTHER BLIND SPOT, found while building the fixture and
        stated so nobody reasons from it: `git cherry` compares against
        `<merge-base>..<upstream>`, so once a lane has MERGED TRUNK IN the
        merge-base becomes trunk's tip, that comparison set is EMPTY, and every
        lane commit reads '+' — including one whose `patch-id --stable` matches
        a trunk commit exactly. A '+' is therefore NOT evidence the work is
        absent; it is only the absence of evidence that it is present. That
        costs recall (such a lane is never proven retirable) and never safety,
        which is the correct direction, but a later "just trust the '+'"
        simplification would be reading a blind instrument as a verdict.

        FAIL SAFE, LOUDLY: unreadable objects, a spawn failure, an unparsable
        line, an unexpected sign, a count mismatch, a range over `cap`, and the
        rev-list/NOT_ANCESTOR contradiction all return UNKNOWN. This function
        gates a deletion, so UNKNOWN must never be spendable as a verdict —
        the caller keeps.
        """
        anc = self.ancestry(root, tip, ref)
        if anc != NOT_ANCESTOR:
            return anc          # ANCESTOR is decisive and costs one call
        rc, out, _err = self.text(root, "rev-list", "--count",
                                  "%s..%s" % (ref, tip), timeout=timeout)
        if rc != 0 or not out.isdigit():
            return UNKNOWN
        ahead = int(out)
        cap = CHERRY_RANGE_CAP if cap is None else cap
        # ahead == 0 CONTRADICTS the NOT_ANCESTOR above (nothing in the range
        # means tip is reachable from ref). Two reads disagreeing is not a
        # negative, it is an unknown.
        if ahead == 0 or ahead > cap:
            return UNKNOWN
        rc, out, _err = self.text(root, "cherry", ref, tip, timeout=timeout)
        if rc != 0:
            return UNKNOWN
        lines = [ln for ln in out.split("\n") if ln.strip()]
        if len(lines) != ahead:
            return UNKNOWN            # merges + anything else cherry skipped
        signs = set()
        for line in lines:
            parts = line.split()
            if len(parts) != 2 or parts[0] not in ("+", "-") \
                    or not _SHA_RE.fullmatch(parts[1]):
                return UNKNOWN        # a changed format is never a verdict
            signs.add(parts[0])
        if signs != {"-"}:
            return NOT_ANCESTOR       # a '+': that content is NOT upstream
        # BOTH predicates must vouch, and they answer different questions:
        # the first that patch identity supplies ANY evidence here (an empty
        # diff supplies none), the second that the evidence is BYTE-exact
        # rather than whitespace-normalised. Either one silent -> UNKNOWN,
        # which keeps. A '-' from cherry is now necessary and not sufficient.
        if not self._range_has_no_empty_commit(root, ref, tip, timeout):
            return UNKNOWN
        if not self._range_is_verbatim_identical(root, ref, tip, timeout):
            return UNKNOWN
        return PATCH_EQUIVALENT

    def _range_has_no_empty_commit(self, root, ref, tip, timeout=30):
        """True only when every non-merge commit in `ref..tip` touches a file.

        AN EMPTY DIFF HAS AN EMPTY PATCH-ID, SO EVERY EMPTY COMMIT IS
        "PATCH-IDENTICAL" TO EVERY OTHER ONE. Measured 2026-08-03: a lane
        carrying one `commit --allow-empty` marker reads '-' from `git cherry`
        against a trunk whose only empty commit is TOTALLY UNRELATED, and the
        predicate called that lane fully landed. Nothing about the two commits
        corresponds; they merely both changed no files.

        No bytes are at risk in that specific case — an empty commit has no
        content to lose — but the VERDICT is false, and a false landed verdict
        is the input to a deletion. The grade of evidence patch identity
        supplies collapses to nothing on an empty diff, so the honest answer is
        that this range cannot be judged by content: UNKNOWN, which keeps.

        One extra spawn, and only on the patch-identity path — the ancestry
        fast path never reaches here. `--name-only` after a NUL-prefixed
        `%H` gives one record per commit; a record with no file lines is an
        empty commit. An unreadable/unparsable read is False (refuse), same
        direction as every other failure in this predicate."""
        rc, out, _err = self.text(root, "log", "--no-merges", "--format=%x00%H",
                                  "--name-only", "%s..%s" % (ref, tip),
                                  timeout=timeout)
        if rc != 0:
            return False
        records = [r for r in out.split("\0") if r.strip()]
        if not records:
            return False              # nothing to vouch for is not a vouch
        for record in records:
            body = record.strip().split("\n")
            if len(body) < 2 or not any(line.strip() for line in body[1:]):
                return False          # a commit that touched no file
        return True

    def _verbatim_ids(self, root, rng, timeout=30):
        """The set of `--verbatim` patch-ids for the non-merge commits in
        `rng`, or None if the range could not be read. None is never a match."""
        rc, diff, _err = self.run(root, "log", "--no-merges", "-p", rng,
                                  timeout=timeout)
        if rc != 0 or not diff:
            return None
        rc2, out, _e2 = self.text(root, "patch-id", "--verbatim",
                                  stdin=diff, timeout=timeout)
        if rc2 != 0:
            return None
        ids = set()
        for line in out.split("\n"):
            parts = line.split()
            if len(parts) == 2 and _SHA_RE.fullmatch(parts[0]):
                ids.add(parts[0])
        return ids or None

    def _verbatim_map(self, root, ref, timeout=60):
        """{commit sha: --verbatim patch-id} for ALL of `ref`, or None.

        ONE WALK INSTEAD OF ONE PER ROW. `_range_is_verbatim_identical` needs
        the ids of `base..ref`, and it recomputed them per land loop: measured
        on the live ledger, 364 calls over 248 DISTINCT ranges, every one of
        them ending at the same trunk sha, costing 89 of the projection's 162
        seconds. The ranges are nested subsets of ONE history, so one walk
        answers all of them.

        A COMMIT'S PATCH-ID DOES NOT DEPEND ON THE RANGE IT IS LISTED IN — it
        is the diff against that commit's own parent — so selecting a range's
        ids out of this map is the SAME SET the range walk produces, not an
        approximation of it. That is what makes this admissible on a predicate
        that guards a branch delete.

        None on any unreadable half, exactly as `_verbatim_ids` does, so the
        caller falls back to the per-range walk rather than treating a failed
        map as an empty history."""
        # THE FORMAT IS PINNED BECAUSE THE PARSER DEPENDS ON IT. `patch-id`
        # reports "<patch-id> <commit>" by reading the commit header out of the
        # diff stream, so the grammar this map parses is produced by `log`'s
        # PRETTY FORMAT — and a repository-local `format.pretty` silently
        # rewrites it. Measured by a reviewer in a real repo: with
        # `format.pretty='format:commit <trunk-sha>'` every patch in the walk
        # was attributed to that one commit, so an OLD pre-base patch was
        # imported as the trunk tip and `landed_state` answered
        # PATCH_EQUIVALENT for work that had not landed.
        #
        # THAT IS THE ONE DIRECTION THIS MAY NOT BE WRONG IN. A false
        # PATCH_EQUIVALENT feeds work-GC forced retirement and landreq proof
        # relief — it authorises DELETING a branch. Every other failure mode
        # here degrades toward refusing; this one degraded toward destroying,
        # and repository config cannot be allowed to own the grammar.
        # MEMOISED AS A PARSED MAP, INCLUDING ITS FAILURES, because only raw
        # subprocess bytes are cached one layer down and that is not enough in
        # either outcome.
        #
        # ON FAILURE it is strictly worse than no shortcut at all: `run`
        # deliberately evicts rc=-1, so a 30s whole-history TIMEOUT is retried
        # for EVERY row before each narrow fallback. A reviewer measured two
        # identical full-walk retries from a two-call probe. A failure is
        # cached so it is paid ONCE per scope.
        #
        # THIS USED TO CITE `_LR_HARD_TTL_S=120` AND CALL FOUR SUCH ROWS A
        # LIVELOCK. That number is gone: trunk raised the hard TTL to 600
        # (helm/web_land_model.py), and the rebase of this lane took trunk's
        # value rather than carrying the 120 the sentence was reasoned over.
        # AT 600 THE LIVELOCK PREMISE IS DEAD, not merely quieter — 20 retried
        # full walks now fit inside the budget where 4 did not, so nothing here
        # is load-bearing against a deadline any more. The failure cache stays
        # for the reason that survives the TTL: a retried whole-history walk is
        # a straight regression against the bounded per-range walk it replaced,
        # and paying it once per scope is right at any budget. Do not re-derive
        # urgency from this paragraph; re-read the TTL if a number is needed.
        #
        # ON SUCCESS the bytes are memoised but the PARSE is not, so a
        # 2,424-entry map is rebuilt per row — measured at 1.54s for 25
        # same-ref calls, projecting ~22s across the 364 this projection makes.
        #
        # projscope.memo caches None, which is what makes the failure half
        # expressible; outside a scope it computes and caches nothing, and this
        # is only reached inside one.
        from . import projscope
        return projscope.memo(("vcs._verbatim_map", root, ref),
                              lambda: self._verbatim_map_uncached(root, ref, timeout))

    def _verbatim_map_uncached(self, root, ref, timeout=60):
        """The live walk `_verbatim_map` memoises. Split out so the memo has
        something to call, exactly as `landreq._git_spawn` is split from
        `landreq._git`."""
        rc, diff, _err = self.run(root, "log", "--no-merges", "-p",
                                  "--pretty=format:commit %H", ref,
                                  timeout=timeout)
        if rc != 0 or not diff:
            return None
        rc2, out, _e2 = self.text(root, "patch-id", "--verbatim",
                                  stdin=diff, timeout=timeout)
        if rc2 != 0:
            return None
        got = {}
        for line in out.split("\n"):
            parts = line.split()
            if len(parts) == 2 and _SHA_RE.fullmatch(parts[0]) \
                    and _SHA_RE.fullmatch(parts[1]):
                got[parts[1]] = parts[0]
        return got or None

    def _verbatim_ids_via_map(self, root, base, ref, timeout=30):
        """`_verbatim_ids(root, base..ref)` answered from the whole-ref map.

        The membership question is unchanged; only the way the set is obtained
        moves. Returns None whenever the map or the range listing is
        unreadable, or whenever ANY commit in the range is absent from the map
        — an incomplete set would be a SMALLER `theirs`, and a smaller theirs
        makes `mine <= theirs` fail, which keeps the branch. Wrong in the safe
        direction, and refused outright rather than relied on."""
        got = self._verbatim_map(root, ref, timeout)
        if not got:
            return None
        rc, out, _err = self.text(root, "rev-list", "--no-merges",
                                  "%s..%s" % (base, ref), timeout=timeout)
        if rc != 0:
            return None
        ids = set()
        for sha in (out or "").split():
            # A COMMIT ABSENT FROM THE MAP CONTRIBUTED NO DIFF, and the range
            # walk omits it for exactly the same reason — `log -p` emits no
            # patch for an empty commit, so `patch-id` emits no line. Measured:
            # a 642-commit range had ONE such commit, a message-only
            # retraction, and refusing the whole map over it disabled the
            # optimisation entirely while changing no answer.
            #
            # AND IF A COMMIT WERE MISSING FOR SOME OTHER REASON, omitting it
            # yields a SMALLER `theirs`, which makes `mine <= theirs` fail,
            # which KEEPS the branch. Every failure mode of this shortcut lands
            # on the refusing side, which is the only direction this predicate
            # is allowed to be wrong in.
            if sha in got:
                ids.add(got[sha])
        return ids or None

    def _range_is_verbatim_identical(self, root, ref, tip, timeout=30):
        """True only when every lane commit's diff is upstream BYTE-FOR-BYTE.

        `git cherry`'s '-' is patch-id's DEFAULT algorithm, which normalizes
        whitespace. That is correct for patch-id's real job — recognising a
        commit across a rebase — and it is the wrong bar for authorising a
        delete. Measured 2026-08-04 in a scratch repo: trunk carrying `ADDED`
        and a lane carrying `ADDED  ` (trailing spaces) are different blobs,
        DIFFERENT BLOBS, and `git cherry` answers '-' — ALREADY UPSTREAM.
        (Shas from that scratch repo are deliberately not quoted here: they
        resolve nowhere in THIS history, and a citation a reader cannot
        dereference is worse than no citation. The repro is four commands and
        reproduces anywhere; the claim is the RELATIONSHIP, not the hex.)

        THIS IS THE ONE cherry BLIND SPOT THAT FAILS TOWARD "LANDED". The two
        already named above — a merge commit producing empty output, a
        merged-in trunk making every commit read '+' — both fail toward
        UNKNOWN, which keeps, and the docstrings say so approvingly. Whitespace
        blindness fails the other way, and the losing direction is the one that
        authorises a branch deletion on a false verdict.

        `--verbatim` is the exact primitive because it drops ONLY the
        whitespace normalisation. Measured the same day, both arms:
          benign rebase, identical content -> the two ids are IDENTICAL (recall kept)
          trailing-whitespace difference   -> the two ids DIFFER      (hole closed)
        The first arm is what makes this admissible at all: this predicate
        guards the path that exists BECAUSE helm lands work rebased, so a check
        that broke on a rebase would delete the feature it is protecting.

        HONEST LIMIT: `--verbatim` keeps context lines, so a rebase that shifts
        the lines AROUND a hunk changes the id and this reads False -> UNKNOWN
        -> the branch is KEPT. That costs recall and never safety, the same
        direction as every other refusal here.

        Two extra spawns, and only on the patch-identity path — ancestry never
        reaches this. Any unreadable half is False (refuse)."""
        rc, base, _err = self.text(root, "merge-base", ref, tip,
                                   timeout=timeout)
        base = (base or "").strip()
        if rc != 0 or not base:
            return False
        mine = self._verbatim_ids(root, "%s..%s" % (base, tip), timeout)
        # `theirs` is base..TRUNK and is the expensive half — the same walk
        # repeated per row over nested subsets of one history. Answered from a
        # single whole-ref map when that is readable, and by the original walk
        # when it is not, so behaviour is identical and only the cost moves.
        # THE WHOLE-HISTORY MAP IS ONLY EVER A WIN WHERE IT IS REUSED, AND
        # REUSE EXISTS ONLY INSIDE A PROJECTION SCOPE. `landed_state` is
        # generic: work GC, gate and the dispatch sweep call it OUTSIDE any
        # scope, one branch at a time, and for those callers the map is a
        # straight regression — a reviewer measured 44.6MB/8.54s of full
        # history against 29KB/0.78s for the bounded `base..ref` proof they
        # used to do. Optimising the projection must not make every standalone
        # authorisation path slower.
        #
        # So the shortcut is taken ONLY when a scope is active, which is
        # exactly the condition under which the map's cost is amortised across
        # many rows. Outside a scope the original bounded walk runs unchanged.
        from . import projscope
        theirs = None
        if projscope.active():
            theirs = self._verbatim_ids_via_map(root, base, ref, timeout)
        if theirs is None:
            theirs = self._verbatim_ids(root, "%s..%s" % (base, ref), timeout)
        if not mine or not theirs:
            return False
        return mine <= theirs

    def delete_branch(self, root, branch, force=False, expect=None):
        """`branch -d` — SAFE by construction: git itself refuses an unmerged
        branch.

        `force` spells `-D`, and it is NOT a relaxation of "no helm code path
        discards authored work". `-d`'s refusal is a REACHABILITY test, which
        is the same wrong question `landed_state` exists to correct: a branch
        whose every commit is on trunk under a rebased sha is not unmerged in
        any sense a human means, yet `-d` refuses it forever. Passing `force`
        is a claim that the caller holds an INDEPENDENT proof of containment —
        `landed_state(...) == PATCH_EQUIVALENT`, git's own patch-id comparison
        — and callers here must also have resolved and printed the tip sha, so
        the delete is one `git branch <name> <sha>` away from undone. Never
        reachable from an ancestry-negative or an UNKNOWN.

        `expect` CLOSES THE WINDOW BETWEEN THE DECISION AND THE DELETE, and it
        is not hypothetical — measured 2026-08-04 in a scratch repo: a reaper
        read the tip, the branch then advanced, and `git branch -D` reported
        deleting it "was <THE ADVANCED SHA>" — not the one the reaper had
        decided about. `-D` deletes what the ref
        points at NOW; it does not care which sha the caller reasoned about. So
        the retirement ref preserved the OLD tip while the delete ate the NEW
        one, and the commits in between were referenced by nothing.

        `git update-ref -d <ref> <oldvalue>` is git's own compare-and-delete
        and refuses the mismatch loudly — "cannot lock ref: is at <CURRENT> but
        expected <READ-BACK>", rc 1, branch intact (rc 0 and gone when the sha
        still matches). Passing `expect` therefore makes PRESERVE-THEN-DELETE
        atomic in the only sense that matters: what gets deleted is exactly
        what was proven saved, or nothing is."""
        if expect:
            return self.text(root, "update-ref", "-d",
                             "refs/heads/" + branch, expect)
        return self.text(root, "branch", "-D" if force else "-d", branch)

    def wip_commit(self, path, msg):
        """The lost-and-found write: stage EVERYTHING, commit --no-verify onto
        whatever branch the checkout is on, under the janitor identity.
        Strictly loss-reducing and reversible.

        THE IDENTITY GOES IN THE ENVIRONMENT, NOT IN `-c`, AND THE ORDER IS THE
        WHOLE POINT: GIT_AUTHOR_NAME / GIT_COMMITTER_NAME OUTRANK
        `git -c user.name=…`. Measured 2026-07-30 in a clean scratch repo —
        `GIT_AUTHOR_NAME=seat-x git -c user.name=helm-work commit` produces
        seat-x, not helm-work. So once launched seats export a per-seat git
        identity (launch.build_env), a `-c` flag can no longer pin a SERVICE
        commit, and this janitor write would silently be attributed to whichever
        seat happened to trigger the lost-and-found.
        `-c` is kept alongside for a caller that scrubs the environment."""
        self.text(path, "add", "-A")
        env = dict(os.environ)
        env.update(GIT_AUTHOR_NAME="helm-work", GIT_COMMITTER_NAME="helm-work",
                   GIT_AUTHOR_EMAIL="helm-work@local",
                   GIT_COMMITTER_EMAIL="helm-work@local")
        return self.text(path, "-c", "user.name=helm-work",
                         "-c", "user.email=helm-work@local",
                         "commit", "--no-verify", "-m", msg, env=env)


    def ahead_behind(self, root, base, tip):
        """(behind, ahead) exactly as git counted them, or None when the read
        failed. STRINGS, not ints: the board renders '?' for unknown and never
        does arithmetic on them."""
        rc, out, _err = self.text(root, "rev-list", "--left-right", "--count",
                                  "%s...%s" % (base, tip))
        if rc != 0 or "\t" not in out:
            return None
        behind, ahead = out.split("\t")
        return behind, ahead


# ---------------------------------------------------------------------------
# backend selection — HELM_VCS, else the nearest marker, else DEFAULT (git)
# ---------------------------------------------------------------------------

_INSTANCES = {GIT: GitVcs()}
_NOTED = set()          # one note per process, not one per git call


PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"

# The ancestry trio (see GitVcs.ancestry) SHARES `UNKNOWN` with the marker trio
# above — one seam, one spelling for "I could not see it", so a consumer of
# either tri-state imports a single sentinel for the unreadable case.
# (landreq.py's private `--git-dir` copy spells the same state UNDETERMINED;
# states are compared by identity, never by prose.)
ANCESTOR, NOT_ANCESTOR = "ancestor", "not-ancestor"

# The FOURTH state, and the one this seam existed without for too long: the
# work is on the ref under a DIFFERENT object id. `landed_state` returns it;
# the spelling matches landreq.py's `_landing_proof`, which reached the same
# answer first for a different surface. Ancestry alone cannot see this state,
# and a reaper that cannot see it keeps every rebased lane forever.
PATCH_EQUIVALENT = "patch-equivalent"

# Ordered patch-train containment. UNKNOWN shares the seam-wide spelling above;
# every other state is a measured answer and callers decide which ones authorize.
PATCH_SEQUENCE_EXACT = "patch-sequence-exact"
PATCH_SEQUENCE_CONTAINED = "patch-sequence-contained"
PATCH_SEQUENCE_ABSENT = "patch-sequence-absent"
PATCH_SEQUENCE_EMPTY = "patch-sequence-empty"
PATCH_SEQUENCE_AMBIGUOUS = "patch-sequence-ambiguous"
PATCH_SEQUENCE_UNKNOWN = UNKNOWN
PATCH_SEQUENCE_CAP = 500

# The bound on `git cherry`'s range. Cost scales with the tip's DISTANCE from
# trunk (measured 2026-08-02: 3.4ms at 10 commits, 44.1ms at 577), so a cap is
# a latency bound, not a correctness one — and it fails toward UNKNOWN, which
# KEEPS. A lane 500 commits from trunk is not a routine reap candidate; it is
# something for a human to look at.
CHERRY_RANGE_CAP = 500

_SHA_RE = re.compile(r"[0-9a-f]{7,64}")


def marker_state(path):
    """PRESENT / ABSENT / UNKNOWN for one marker path.

    TRI-STATE on purpose. `os.path.exists` answers False both for "not there"
    and for "a parent directory denied me the lookup", and collapsing those is
    the same bug as helm's vacuous-pass premise — recording "I could not read
    it" as "it is not there". Here that bug is a BACKEND HIJACK: a git checkout
    whose `.git` helm cannot stat reads as marker-less, the walk continues
    outward, and an accessible `.jj` above it captures a repo that is really
    git. So an unreadable marker is UNKNOWN, and UNKNOWN stops the walk.
    """
    try:
        os.stat(path)                     # follows links, like `git -C` does
        return PRESENT
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT
    except OSError:                       # EACCES, ELOOP, ENAMETOOLONG, EIO…
        return UNKNOWN


def resolved_target(target):
    """`target` as the kernel would see it: symlinks resolved through the
    EXISTING prefix, the not-yet-existing tail preserved verbatim.

    `git -C <path>` chdirs, so git operates on the link TARGET; a lexical
    answer here would disagree with git about which repo a path belongs to.
    `os.path.realpath` is exactly this (non-strict since 3.6): it resolves what
    exists and leaves the rest alone, which is what a lane path that has not
    been created yet needs.
    """
    try:
        return os.path.realpath(os.path.abspath(target))
    except (OSError, ValueError):
        return None


def detect(target=None):
    """The VCS governing `target`: JJ, GIT, or None (unknown → caller defaults).

    Walks UP from the resolved target, and the NEAREST level carrying a marker
    decides — git's own discovery rule. That matters twice: a path nested deep
    inside a repo still finds its repo (a `.jj` only ever sits at a workspace
    root, never beside the file you are touching), and a `.jj` far above an
    unrelated git checkout cannot hijack it, because the inner `.git` is found
    first.

    A COLOCATED repo carries both markers at one level; jj wins there, because
    that is what colocation means — jj drives the working copy and `.git` is the
    compatibility surface kept for tools like orca/herdr.

    THE WALK STOPS AT THE FIRST UNKNOWN. If a marker at some level cannot be
    read, helm does not know what that level is, so it refuses to adopt anything
    FURTHER OUT and answers None (the caller falls back to the default). A
    present marker at the same level still wins — that is a same-level fact, not
    an outward adoption. This is the anti-hijack rule; see `marker_state`.

    `_MAX_WALK` is a bound in PARENT EDGES: the target itself plus that many
    ancestors are examined, so a marker exactly `_MAX_WALK` edges up is found
    and one edge beyond it is not.

    `target=None` falls back to the cwd, which is a LAST resort: every caller
    inside this seam's scope passes the repo or checkout it is operating on. A
    vanished cwd must never raise here (the eager-getcwd class).
    """
    if target is None:
        try:
            target = os.getcwd()
        except OSError:
            return None
    cur = resolved_target(target)
    if cur is None:
        return None
    for _edge in range(_MAX_WALK + 1):
        unreadable = False
        for name, marker in MARKERS:
            state = marker_state(os.path.join(cur, marker))
            if state == PRESENT:
                return name
            unreadable = unreadable or state == UNKNOWN
        if unreadable:
            return None                   # never adopt a marker further out
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def select(requested=None, target=None):
    """(backend-name, note) — which backend serves this TARGET, plus the ONE
    line to say when the request could not be honored (else None).

    PURE: one env read and, only when the env is unset, one bounded marker
    walk. No process state, so tests read it directly. Order: `HELM_VCS=git|jj`
    wins (an operator override is global by intent); else the nearest marker
    above `target` (helm honors what the repo already IS); else DEFAULT = git,
    spelled out rather than implied.

    Only GitVcs exists today, so a jj or garbage request resolves to git WITH
    a note. That is deliberate: JjVcs is a later slice, and a VCS preference
    must never be able to take helm down. When JjVcs lands, `_INSTANCES` gains
    a key and this fallback leg simply stops firing.
    """
    if requested is None:
        requested = (os.environ.get(ENV_VAR) or "").strip().lower()
    if not requested:
        requested = detect(target) or DEFAULT
    if requested in _INSTANCES:
        return requested, None
    if requested == JJ:
        return DEFAULT, ("[helm vcs] jj governs this repo but the jj backend is "
                         "not built yet — using git (HELM_VCS=git silences)")
    return DEFAULT, ("[helm vcs] unknown backend %r — using git "
                     "(HELM_VCS=git|jj)" % requested)


def backend(target=None):
    """The Vcs for the repo/checkout `target` names.

    `target` is the thing being OPERATED ON, not the cwd the verb was typed in
    — pass the root, worktree path or helm home the very next call will touch.
    Selection is re-derived per call (one env read, plus a bounded stat walk
    only when the env is unset — nothing beside the fork/exec that follows), so
    two repos under different VCSs in one process each get their own answer.

    The note is said ONCE per process on stderr, and nothing here raises: an
    unimplemented backend degrades, it does not fail the verb.
    """
    name, note = select(target=target)
    if note and note not in _NOTED:
        _NOTED.add(note)
        try:
            print(note, file=sys.stderr)
        except Exception:
            pass
    return _INSTANCES[name]
