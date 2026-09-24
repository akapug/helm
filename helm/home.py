#!/usr/bin/env python3
"""The ~/.helm home resolver. HOME-anchored, never cwd-derived — a helm command
invoked from any worktree resolves the same root every time (the Q132/Q133
cwd-independence class).

Env transition law: HELM_* preferred, legacy MELD_* accepted as fallback (the
env2 new-with-old-fallback pattern). ~/.helm is the durable USER knowledge
corpus; engine runtime state (any tool's ~/.config/<tool>) stays out of it.
"""
import os
import re

# A legitimate seat name is an IDENTIFIER: codex-2, opus-integrator, ds4pro —
# [A-Za-z0-9._-], bounded. HELM_CHAT_NAME is the one unvalidated join seam every
# chat surface trusts (roster keys, chat from/tfrom/rfrom, hook pane names,
# todos, codex capacity, …); it is validated HERE, at the source, exactly once,
# so a control-char name never enters the system rather than being laundered at
# each of a dozen sinks forever (the ESC/bidi display-launder class, closed at
# the owner layer — decision-spirit #15).
_SEAT_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


class SeatNameError(ValueError):
    """A hostile HELM_CHAT_NAME reached the join seam. The message names the
    offending bytes SAFELY (ASCII-escaped via _safe_name) — the raw ESC/bidi
    payload never rides the error onward into a terminal or log."""


def _safe_name(raw):
    """The offending name rendered as pure printable ASCII — ESC becomes \\x1b,
    a bidi override U+202E becomes \\u202e — so the rejection message itself
    can never carry the control/format payload it is reporting on. Bounded, so
    a pathologically long name cannot flood the error."""
    return str(raw)[:80].encode("unicode_escape").decode("ascii")


def chat_name():
    """The seat identity from HELM_CHAT_NAME (legacy MELD_CHAT_NAME) — THE one
    validated ingestion seam for the seat name. Every os.environ read of this
    var routes here (chat.whoname, seats.derive_seat, human.operator_name,
    launch); no other module reads it raw (tests/test_display_launder_tripwire
    enforces that with a source grep).

    Returns the name when it is a legitimate seat identifier ([A-Za-z0-9._-],
    like codex-2 / opus-integrator / ds4pro); None when unset OR empty (callers
    fall through to their auto-name floor, preserving the old `if name:` /
    `or <derived-owner>` semantics); and RAISES SeatNameError when the name carries ESC
    / C0-C1 controls / Unicode bidi overrides (U+202A-E, U+2066-9) / any other
    format-Cf. A control-char seat name is never legitimate, so it is REJECTED
    at the source — it never becomes a roster key, a chat from-field, a hook
    pane name, a todo row, or any future sink.

    PANE-ENV CONTAGION HAZARD (owner-declared P0, 2026-08-02): an exported
    HELM_CHAT_NAME on a shell ANCESTOR OUTLIVES the pane it named. A crashed
    pane restarted under that ancestor inherits the dead seat's name for
    free, and every resolver then agrees the new process IS that seat — the
    measured incident armed the integrator's beacon from a stranger's pane
    and drained ~60 of its DMs. helm cannot stop a metaharness (orca)
    ancestor from exporting this var. What helm owns: (1) every helm-owned
    launch/resume path SETS the name explicitly per-seat instead of
    inheriting it (launch.build_env, seat.launch_line, pi, orcaadopt,
    sessions.resume_identity_env), and (2) the disagreement law
    (seats.identity_disagreement) — a declared name that contradicts the
    session's roster binding REFUSES at every acting/delivery/registration
    boundary — is the backstop when the free env name lies."""
    raw = env("CHAT_NAME")
    if not raw:                     # unset or explicitly empty -> fall through
        return None
    if not _SEAT_NAME_RE.match(raw):
        raise SeatNameError(
            "HELM_CHAT_NAME is not a legitimate seat name: '%s' "
            "(a seat name is [A-Za-z0-9._-], like codex-2) — refusing to join "
            "or post under it" % _safe_name(raw))
    return raw


def validate_seat_arg(raw):
    """The SECOND seat-name ingestion beside the env seam: a name supplied as a
    CLI arg (helm launch/spawn --seat). Same rule as chat_name — None when empty
    (caller falls through), the name when a legit identifier, SeatNameError on
    ESC/control/bidi — so a hostile --seat can never be exported as the child's
    HELM_CHAT_NAME or written as a roster key (the source-grep tripwire guards
    env reads only; this closes the arg path)."""
    if not raw:
        return None
    if not _SEAT_NAME_RE.match(raw):
        raise SeatNameError(
            "--seat is not a legitimate seat name: '%s' "
            "(a seat name is [A-Za-z0-9._-], like codex-2) — refusing to "
            "launch under it" % _safe_name(raw))
    return raw


# Per-project concept-category chain (the predecessor's .local family, lifted to a
# user-level product-namespace home). Order is the organic dev cycle:
# priors art feeds prd, build happens in the repo, evals then journal then archive.
PROJECT_CATEGORIES = (
    "premises", "heuristics", "lexicon", "prd", "journal", "evals", "archive",
)

# Global-only categories: shared prior-art corpus, the one operator profile,
# cross-project truths, reflexes (fire regardless of project).
GLOBAL_CATEGORIES = (
    "priors", "know-your-user", "premises", "heuristics", "lexicon",
    "reflexes", "archive",
)

GLOBAL = "_global"


def env(name, default=None):
    """HELM_<name> preferred; legacy MELD_<name> accepted as fallback."""
    v = os.environ.get("HELM_" + name)
    if v is None:
        v = os.environ.get("MELD_" + name)
    return default if v is None else v


def env_pair(name, companion):
    """A value plus metadata selected atomically from one env namespace.
    A preferred HELM value must never inherit stale MELD provenance."""
    value = os.environ.get("HELM_" + name)
    if value is not None:
        return value, os.environ.get("HELM_" + companion)
    value = os.environ.get("MELD_" + name)
    return value, os.environ.get("MELD_" + companion)


# The harness session-id vars, in resolution order. CLAUDE_CODE_SESSION_ID is
# the REAL var Claude Code exports; CLAUDE_SESSION_ID is the legacy/hook-injected
# alias (the SessionStart join hook passes session_id explicitly, so it worked
# even while a bare CLI post fell through to the anon floor —
# 2026-07-21: a manual `helm chat post` posted as 'agent', and the a2a per-session
# cursor silently no-op'd for every claude-code session). CODEX_SESSION_ID is the
# codex seat. One resolver so no call site misses the real var again.
_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def session_id():
    """The current harness session id across every harness that sets one, or
    None so callers fall through to their auto-name/anon floor."""
    for k in _SESSION_ENV:
        v = os.environ.get(k)
        if v:
            return v
    return None


def helm_home():
    """The root. HELM_HOME env else ~/.helm — anchored on $HOME, never cwd."""
    override = env("HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".helm")


def default_home():
    """What helm_home() answers when NO env speaks — the live root."""
    return os.path.join(os.path.expanduser("~"), ".helm")


EXPLICIT = "EXPLICIT"
REDIRECTED = "REDIRECTED"
DEFAULT = "DEFAULT"


def default_surface(default):
    """Where this surface lives with NO overrides at all — the DEFAULT arm's
    answer, including the no-tmpfs fallback.

    THE DEFAULT IS NOT A LITERAL, which is the whole reason this is a
    function. On a host with no /dev/shm the shared bus moves under
    ram_root(), so a caller comparing a resolved surface against the literal
    "/dev/shm/helm-chat" concludes "not production" on every such machine —
    and any predicate built on that comparison silently stops doing its job
    there rather than failing."""
    if default.startswith("/dev/shm") and not os.path.isdir("/dev/shm"):
        return default.replace("/dev/shm", ram_root(), 1)
    return default


def surface_origin(env_name, name, default):
    """(path, WHICH ARM CHOSE IT) — the precedence decision, stated.

    Three arms, in trust order:
      explicit HELM_<env_name> (legacy MELD_ via env()) — the operator chose.
      a REDIRECTED root — HELM_HOME points off ~/.helm, so the caller asked
        for an isolated estate; the surface follows it as <root>/<name>.
        Before this arm existed, an isolated-looking probe (HELM_HOME=tmp)
        still resolved the LIVE fleet bus: the roster — the identity index —
        plus claims, rooms and DMs sat one careless write from fleet
        corruption (tests/test_gc.py setUp records the measured near-miss:
        a 24,001-victim reap of live cursors from inside the suite).
      the default root — the shared machine bus stays on tmpfs (RAM law).

    realpath on BOTH sides of the comparison: HELM_HOME spelled as the
    default through a symlink or trailing slash is the same root, not an
    isolation request — a false "redirected" here would silently move a
    live seat's bus off /dev/shm.

    THE ARM IS RETURNED BECAUSE CALLERS KEPT RE-DERIVING IT AND COULD NOT.
    Deciding "is this the production surface" by comparing this function's
    OUTPUT against a literal is undecidable from outside: the default itself
    moves on a tmpfs-less host, an explicit override that happens to name the
    production surface IS production, and reading the environment directly
    walks straight past this function's own precedence between HELM_ and
    MELD_ spellings. Each of those was a separately-cured bug in one
    predicate that had no way to ask which arm ran. Now it can ask."""
    explicit = env(env_name)
    if explicit:
        return explicit, EXPLICIT
    root = helm_home()
    if os.path.realpath(root) != os.path.realpath(default_home()):
        return os.path.join(root, name), REDIRECTED
    return default_surface(default), DEFAULT


def surface_dir(env_name, name, default):
    """The resolved surface path. See :func:`surface_origin` for the arms."""
    return surface_origin(env_name, name, default)[0]


def ram_root():
    """The machine's boot-scoped RAM root: /dev/shm where it exists (Linux).

    On hosts without it (macOS mounts no tmpfs), fall back to a subdir of a
    PER-USER boot-scoped root, derived in order: $TMPDIR when it is a
    directory this uid owns; else confstr Darwin constant 65537
    (_CS_DARWIN_USER_TEMP_DIR — Python accepts the integer on darwin even
    though confstr_names omits the name) under the same ownership check;
    else /tmp/helm-ram-<uid>, created 0700 and REFUSED LOUDLY if it exists
    under another uid. The first cut fell through to a literal /tmp — a
    predictable machine-global root, which is the shared-directory race
    Apple's secure-coding guide names (port review, measured with
    /dev/shm mocked absent and TMPDIR unset). The RAM law degrades to the
    boot-scope law, never to a durable path and never to a root another
    account can pre-own — and the write-behind log remains the durable
    truth either way, exactly as on Linux."""
    if os.path.isdir("/dev/shm"):
        return "/dev/shm"
    for root in (os.environ.get("TMPDIR"), _darwin_user_tmp()):
        if not root:
            continue
        resolved = os.path.realpath(root)   # checks bind the resolved path,
        if _owned_dir(resolved) \
                and _protected_ancestry(os.path.dirname(resolved)):
            # ancestry too (r4 D2): realpath+stat validates one
            # instant, and a swappable ANCESTOR invalidates the leaf after
            # validation — so ancestry must be protected, else the candidate
            # is skipped for the fixed fallback below. The DERIVED CHILD
            # gets the same establish discipline as the fallback
            # (r5: a preexisting `helm-ram -> victim` symlink inside a
            # valid candidate redirected every consumer).
            try:
                return _establish_private_dir(
                    os.path.join(resolved, "helm-ram"))
            except OSError:
                # ENVIRONMENTAL failure (EACCES under an odd candidate,
                # ENOENT from a vanished parent) falls through to the next
                # rung — a candidate is optional and the fixed fallback is
                # the guarantee (r7: a 0500 TMPDIR aborted the
                # whole ladder). RuntimeError refusals stay LOUD on
                # purpose: a planted symlink or foreign-owned entry is an
                # adversarial signal, not weather.
                continue
    return _establish_private_dir(_ram_fallback_path())


def _establish_private_dir(path):
    """Turn a NAME into an owned, exclusive, REAL directory — one
    discipline for the fixed fallback and every candidate's derived child.

    NO-FOLLOW throughout (r3 and r5: a symlink preplanted at
    the name — in shared /tmp or inside an otherwise-valid TMPDIR —
    routes consumers, and mode-fixing calls, into a victim directory;
    sticky protects existing entries, never the precreation of an absent
    name). mkdir claims the absent name 0700; lstat gives the loud symlink
    refusal; O_NOFOLLOW|O_DIRECTORY is the real guard; fstat answers about
    the fd's own inode; fchmod re-pins 0700 on that inode EVERY use, since
    mkdir's mode applies only at creation and an exist_ok-preserved
    permissive mode otherwise survives (r2)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return _create_hardened(path)
    # PRE-EXISTING entry: harden in place — its contents are legitimate
    # prior state. Fresh creation never reaches here; it publishes a
    # hardened, verified-empty inode by rename (r11: mkdir at
    # the FINAL name exposed a pre-hardened window an inherited ACE could
    # write through, proven with a Linux seam probe).
    if (st.st_mode & 0o170000) == 0o120000:
        raise RuntimeError(
            "ram_root: %s is a symlink — refusing a redirected RAM root"
            % path)
    if st.st_uid == os.geteuid() and (st.st_mode & 0o700) != 0o700:
        # a restrictive umask filters mkdir(0700) down — 0777 leaves the
        # fresh dir mode 0000, and the no-follow open below then fails
        # BEFORE fchmod could repair it (r7). The path-chmod is
        # safe here: the entry was proven a non-symlink one syscall ago and
        # only our own euid's entries are touched.
        os.chmod(path, 0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fst = os.fstat(fd)
        if fst.st_uid != os.geteuid():
            raise RuntimeError(
                "ram_root: %s exists under another uid — refusing a shared "
                "machine-global RAM root" % path)
        os.fchmod(fd, 0o700)
        _strip_or_refuse_acls(path, fd)
        # ONE residual documented, not computed: the fixed /tmp fallback's
        # ancestry is /tmp (sticky) and / (root 0755), guarded by exactly
        # this establish discipline rather than by _protected_ancestry.
    finally:
        os.close(fd)
    return path


def _create_hardened(path):
    """The final NAME never exposes a pre-hardened inode. Build at a
    RANDOM private sibling (mkdtemp), repair its mode (umask filters
    mkdir(0700) — the round-7 class applies to mkdtemp too), harden it
    (fchmod + ACL strip), VERIFY IT IS EMPTY through the same fd (a racer
    who found the random name and wrote through an inherited ACE is an
    attack signal, refused loudly), and only then publish by rename. A
    loser of the rename race re-establishes whatever entry won — and a
    planted symlink at the name makes the rename fail, sending the
    recurse into the loud symlink refusal."""
    import tempfile
    parent = os.path.dirname(path) or "."
    tmp = tempfile.mkdtemp(prefix=os.path.basename(path) + ".estab-",
                           dir=parent)
    try:
        st = os.lstat(tmp)
        if st.st_uid == os.geteuid() and (st.st_mode & 0o700) != 0o700:
            os.chmod(tmp, 0o700)
        fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
            _strip_or_refuse_acls(tmp, fd)
            if os.listdir(fd):
                raise RuntimeError(
                    "ram_root: content appeared inside %s before it was "
                    "published — refusing a RAM root another principal "
                    "could write" % tmp)
            # THE NAME MUST STILL MEAN THE INODE (r12 probe:
            # the temp dir was moved aside after the fd opened and a
            # symlink planted at its name — every check above examined
            # the held fd while rename publishes whatever the NAME says).
            # lstat/fstat identity, proven as the LAST act before rename
            # with the fd still open, closes it: in any parent where an
            # attacker could win the remaining two-syscall race they
            # could have owned the name outright, and those parents are
            # already excluded by the ancestry and sticky rules.
            fst = os.fstat(fd)
            st_name = os.lstat(tmp)
            if (st_name.st_dev, st_name.st_ino) != (fst.st_dev,
                                                    fst.st_ino):
                raise RuntimeError(
                    "ram_root: %s no longer names the hardened inode — "
                    "refusing to publish a substituted entry" % tmp)
            try:
                os.rename(tmp, path)
            except OSError:
                # a racer claimed the final name first: discard the
                # private build and establish whatever entry won
                os.rmdir(tmp)
                return _establish_private_dir(path)
        finally:
            os.close(fd)
        return path
    except BaseException:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _strip_or_refuse_acls(path, fd):
    """ACLs resolve BEFORE unix mode bits and inheritable ACEs are COPIED
    onto new directories (Apple's documented semantics — r8),
    so fchmod(0700) alone cannot promise exclusivity: a parent with an
    inheritable named-user ACE quietly makes the fresh 'private' child
    readable by that user. darwin: strip with /bin/chmod -N, Apple's own
    removal verb, and refuse loudly if it fails. Linux: POSIX ACLs surface
    as system.posix_acl_* attributes readable on the FD (no-follow by
    construction) — their presence REFUSES, because the stdlib cannot
    remove them and an ACL-bearing private dir is not private. A platform
    without xattr reads has nothing to detect and passes."""
    import subprocess
    import sys
    if sys.platform == "darwin":
        r = subprocess.run(["/bin/chmod", "-N", path],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(
                "ram_root: could not strip ACLs from %s — refusing a RAM "
                "root whose exclusivity cannot be promised" % path)
        return
    if not hasattr(os, "listxattr"):
        return                      # the build has no xattr API: ACLs are
                                    # not expressible through this python
    try:
        names = os.listxattr(fd)    # listxattr TAKES the fd directly —
                                    # there is no os.flistxattr, and the
                                    # first cut's getattr for it made this
                                    # whole detection dead code
                                    # (r9; the arm hid it by mocking
                                    # the nonexistent name into existence)
    except OSError as e:
        import errno
        if e.errno in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            return                  # the FILESYSTEM cannot hold xattrs, so
                                    # it cannot hold POSIX ACLs: provably
                                    # nothing to detect
        raise RuntimeError(         # any OTHER failure is could-not-look,
            "ram_root: cannot read ACL state of %s (%s) — refusing a RAM "
            "root whose exclusivity was not measured" % (path, e)) from e
                                    # and could-not-look must never answer
                                    # as verified-absent (r10:
                                    # the except-return promised exclusivity
                                    # while a real ACL sat unread)
    if any(str(n).startswith("system.posix_acl") for n in names):
        raise RuntimeError(
            "ram_root: %s carries POSIX ACLs — refusing a RAM root whose "
            "exclusivity fchmod cannot enforce" % path)


def _darwin_user_tmp():
    """confstr(_CS_DARWIN_USER_TEMP_DIR) by number, or None off-darwin."""
    try:
        return os.confstr(65537)
    except (OSError, ValueError):
        return None


def _ram_fallback_path():
    """The fixed uid-scoped fallback name — a seam, so test arms can own a
    PRIVATE target instead of mutating the LIVE bus parent (r4
    D1 overrule: the first arms rmtree'd and chmod'd the real
    /tmp/helm-ram-<uid>, which on a no-shm host is the running fleet's
    chat root)."""
    return "/tmp/helm-ram-%d" % os.geteuid()


def _protected_ancestry(path):
    """True when NO ancestor can have its next entry replaced by another
    account. Two rules per ancestor, both required:

    OWNER: root or the current uid (r5). A directory's owner can
    always rename entries within it — sticky restrains other WRITERS, never
    the owner, and a foreign owner can re-chmod their own dir at will — so
    a foreign-owned ancestor is unprotectable regardless of its mode today.

    MODE: no group/other write bits unless sticky (r4 D2): a
    writable non-sticky dir lets any writer swap the validated leaf.

    Real roots pass — darwin /var/folders chains and /tmp are root-owned,
    0755 or sticky — while lab chains with a 0777 intermediate or a
    foreign-owned component are refused toward the fixed fallback. An
    unreadable ancestor is refused: unverifiable is not protected."""
    cur = path
    while True:
        try:
            st = os.stat(cur)
        except OSError:
            return False
        if st.st_uid not in (0, os.geteuid()):
            return False
        if (st.st_mode & 0o022) and not (st.st_mode & 0o1000):
            return False
        parent = os.path.dirname(cur)
        if parent == cur:
            return True
        cur = parent


def _owned_dir(path):
    """True only for an existing directory this uid owns EXCLUSIVELY — no
    group/other WRITE bits. Ownership alone is not territory: an owned 0777
    dir lets any account create or substitute children (r2 probe,
    which selected <world-writable>/helm-ram), recreating the exact
    shared-directory race the ladder exists to close."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return ((st.st_mode & 0o170000) == 0o040000
            and st.st_uid == os.geteuid()
            and (st.st_mode & 0o700) == 0o700
            and not (st.st_mode & 0o022))


def global_dir():
    return os.path.join(helm_home(), GLOBAL)


def global_json(name):
    """(path, object, why) for the operator's JSON file `name` in the global
    dir: THIS HOST'S FACTS, which the source must not carry (an endpoint on the
    operator's LAN, a home reserved for one seat). Read on every call, so an
    edit needs no restart.

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS. Absent is (path, None, None):
    nothing is configured. A file that does not read as a JSON object is
    (path, None, why), and the caller must not read it as absent."""
    import json
    path = os.path.join(global_dir(), name)
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except FileNotFoundError:
        return path, None, None
    except (OSError, ValueError) as exc:
        return path, None, "%s did not read: %s" % (path, exc)
    if not isinstance(obj, dict):
        return path, None, "%s is not a JSON object" % path
    return path, obj, None


def seat_claude_roots():
    """Every minted fleet seat's Claude projects root, family and instance.
    These are harness transcript homes, not a second store."""
    root = os.path.join(global_dir(), "seats")
    out = []
    try:
        families = os.listdir(root)
    except OSError:
        return out
    for family in families:
        d = os.path.join(root, family)
        p = os.path.join(d, "claude", "projects")
        if os.path.isdir(p):
            out.append(os.path.realpath(p))
        inst = os.path.join(d, "instances")
        try:
            names = os.listdir(inst)
        except OSError:
            continue
        for name in names:
            p = os.path.join(inst, name, "claude", "projects")
            if os.path.isdir(p):
                out.append(os.path.realpath(p))
    return sorted(set(out))


def cv_env(base=None):
    """Environment for any cv subprocess: preserve caller overrides and add all
    fleet seat transcript roots through CV's generic multi-root contract."""
    e = dict(os.environ if base is None else base)
    roots = [p for p in e.get("CLUSTERVISION_CLAUDE_ROOTS", "").split(os.pathsep)
             if p]
    roots.extend(seat_claude_roots())
    if roots:
        e["CLUSTERVISION_CLAUDE_ROOTS"] = os.pathsep.join(dict.fromkeys(roots))
    return e


def scan_roots():
    """The repo-scan roots: HELM_SCAN_ROOTS env (colon-separated), else `~/dev`.
    No org-specific path ships — the two-tier walk finds both `~/dev/<repo>` and
    `~/dev/<org>/<repo>`, so a bare `~/dev` covers an org checkout without naming
    it. Matches HELM_CONFIG_ROOTS' convention (both default to `~/dev`).

    IT LIVES HERE AND NOT IN `automap`, which still answers through
    `automap._scan_roots`, because a per-tool-call hook asks this question and
    `automap` costs an interpreter's worth of imports (vcs, harnesses) to load.
    One reader of the variable, reachable from the cheapest module helm has."""
    raw = os.environ.get("HELM_SCAN_ROOTS")
    roots = raw.split(":") if raw else [os.path.join(os.path.expanduser("~"), "dev")]
    return [os.path.expanduser(r) for r in roots if r]


def project_dir(name):
    return os.path.join(helm_home(), name)


def registry_path():
    """The master project list (the auto-map output) — pure PROJECTION,
    rebuildable from a re-scan, safe to regenerate."""
    return os.path.join(global_dir(), "registry.json")


def authored_path():
    """The AUTHORED registry layer (notes/edges/aliases/external/retired),
    keyed by project name. Unrebuildable — registry.json can be wiped and
    re-synced, this file cannot."""
    return os.path.join(global_dir(), "registry-authored.json")


def adopted_memory_dir():
    """The already-live personal-knowledge store this user's agents write today:
    ~/.claude/projects/<slug-of-home>/memory (prior-*.md / lex-*.md / bulk).
    helm ADOPTS it in place — same files, one more resolver — so existing hooks
    and helm always see one store."""
    home = os.path.expanduser("~")
    import re
    slugged = re.sub(r"[^A-Za-z0-9-]", "-", home.replace("/", "-"))
    return os.path.join(home, ".claude", "projects", slugged, "memory")


def claude_memory_dir_for(path):
    """The claude per-project memory dir for an arbitrary project path (exists
    only if claude sessions ran there)."""
    import re
    slugged = re.sub(r"[^A-Za-z0-9-]", "-", path.replace("/", "-"))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", slugged, "memory")


def scaffold_global():
    """Ensure the _global chain exists. Idempotent, additive. Also seeds the
    shipped default reflex pack — a no-op for every id already present, so an
    operator edit or retire is never overwritten (reflex.seed_defaults law)."""
    g = global_dir()
    for c in GLOBAL_CATEGORIES:
        os.makedirs(os.path.join(g, c), exist_ok=True)
    from . import reflex
    reflex.seed_defaults()
    return g


def scaffold_project(name):
    """Ensure one project's chain exists. Idempotent, additive — never deletes.
    Returns the project dir. A symlinked project home (adoption of an existing
    external chain, e.g. project -> ~/.tool/project) is honored
    and never re-scaffolded inside."""
    p = project_dir(name)
    if os.path.islink(p):
        return p
    for c in PROJECT_CATEGORIES:
        os.makedirs(os.path.join(p, c), exist_ok=True)
    return p
