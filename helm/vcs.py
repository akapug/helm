#!/usr/bin/env python3
"""helm vcs — the seam the worktree/ship/capsule/handoff git calls go through.

SCOPE, stated honestly up front: this module owns the version-control calls of
`helm work` (the worktree + lane lifecycle), `helm ship`, `helm capsule`, the
handoff/now probes, the lane gc verdict reads, and automap's root resolver. It
is NOT yet every git call in helm — 43 direct spawns remain outside it (lane_discipline.py and conflict_marker.py joined hardcode.py as standing exceptions, the nevertrack class: script-run by the pre-commit hook with no importable helm, which inflight_gate.py joined 2026-08-03 after importing helm proved to make it inert wherever it was installed; the close ladder added two reads — worktree porcelain and the out-of-scope dirty probe — and dispatch raw-SHA binding added one exact-tip local-branch read; clearspan.py joined the living-pipeline's re-measure-on-read with 9 git calls; docref_guard.py joined the nevertrack class 2026-08-04 with ONE helper, the citation rung that refuses an unaccounted cited token at COMMIT rather than 228 minutes of gate later). Those
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
import os
import re
import shutil
import stat
import subprocess
import sys

GIT = "git"
JJ = "jj"
DEFAULT = GIT          # explicit — an unset default is what burned helm before

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

    def common_dir(self, path, timeout=10):
        """The MAIN repo's admin dir (absolute) for a path inside a repo or a
        linked worktree, or None — the fold every helm root resolver needs."""
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
                       "locked": False, "reason": ""}
                rows.append(cur)
            elif cur is None:
                raise ValueError("worktree field before record")
            elif field.startswith(b"branch "):
                cur["branch"] = os.fsdecode(field[7:])
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
        `_READ_VERBS` and only inside a `projscope.scope()`. Every write verb,
        every `env=` call (the ref-guard bit above is a WRITE signal, never a
        read) and every call outside a scope goes straight to the spawn, so
        this cannot serve a stale answer to anything that changes the repo.

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
        if env is None and args and str(args[0]) in _READ_VERBS:
            from . import projscope
            # TWO THINGS THE FIRST CUT GOT WRONG, both found by @codex-2 on
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
            key = ("vcs.run", cwd, tuple(str(a) for a in args), stdin, timeout)
            hit = projscope.memo(
                key, lambda: self._spawn(cwd, args, timeout, env, stdin))
            if hit[0] == -1:
                projscope.forget(key)
            return hit
        return self._spawn(cwd, args, timeout, env, stdin)

    def _spawn(self, cwd, args, timeout, env, stdin):
        """The bare spawn — the memoising door above is the only reason this
        is a separate method."""
        try:
            r = subprocess.run([GIT, "-C", cwd] + list(args),
                               capture_output=True, timeout=timeout,
                               input=stdin,
                               env=dict(os.environ, **env) if env else None)
            return r.returncode, r.stdout, r.stderr
        except Exception as exc:
            return -1, b"", os.fsencode(str(exc))

    def text(self, cwd, *args, timeout=30, env=None, stdin=None):
        """(rc, out, err) — work/_lanes.py `_git`. Spawn trouble reads as
        rc -1 so every caller fails toward its SAFE verdict."""
        rc, out, err = self.run(cwd, *args, timeout=timeout, env=env,
                                stdin=stdin)
        return rc, os.fsdecode(out).strip(), os.fsdecode(err).strip()

    def _capture(self, cwd, args, timeout):
        """(rc, stripped stdout), or None when the spawn itself failed."""
        try:
            p = subprocess.run([GIT, "-C", cwd] + list(args),
                               capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        return p.returncode, p.stdout.strip()

    def probe(self, cwd, *args, timeout=None):
        """handoff.py `_git`: stripped stdout on rc 0 — '' is a LEGITIMATE
        answer (a clean `status --porcelain`) and must not fold to None;
        nonzero, missing git and timeout all read as None. A probe must never
        block a compaction, so every exception is swallowed."""
        got = self._capture(cwd, args, timeout)
        return None if got is None or got[0] != 0 else got[1]

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
        return subprocess.run((GIT, "-C", cwd) + args, capture_output=True,
                              text=True, timeout=timeout)

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
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) \
                    | getattr(os, "O_NOFOLLOW", 0) \
                    | getattr(os, "O_NONBLOCK", 0)
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

    def common_dir(self, path, timeout=10):
        """`rev-parse --path-format=absolute --git-common-dir` — the MAIN repo's
        `.git` even when `path` is a linked worktree, which is how helm folds a
        worktree back to the project that owns it. None when the read fails.

        Selection does NOT depend on this: `detect()` reads markers off the
        filesystem and calls nothing here, so a root resolver may sit behind the
        seam without a cycle."""
        return self.probe(path, "rev-parse", "--path-format=absolute",
                          "--git-common-dir", timeout=timeout)

    def base_branch(self, root):
        """The integration branch: main when it exists, else the shared
        checkout's own HEAD."""
        if self.text(root, "rev-parse", "--verify", "-q",
                     "refs/heads/main")[0] == 0:
            return "main"
        rc, out, _err = self.text(root, "symbolic-ref", "--short", "HEAD")
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
        """
        rc = self.text(root, "merge-base", "--is-ancestor", tip, ref)[0]
        if rc == 0:
            return ANCESTOR
        return NOT_ANCESTOR if rc == 1 else UNKNOWN

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
        that tip and its landed twin printed the SAME content identity.
        Same content, different object. Only patch identity could see it.

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
