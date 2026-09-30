#!/usr/bin/env python3
"""Suite-wide env the CANONICAL RUNNER actually loads.

WHY HERE AND NOT conftest.py. This repo's tests are `unittest`, not pytest
(CONTRIBUTING.md, AGENTS.md, and the fab gate, which runs serial unittest
discovery). unittest NEVER imports a conftest.py, so anything planted there
is a no-op on the gate and in the canonical local run — the
plants read as protection while protecting nothing. `tests/_tmphome.py`
carries the other half of this same lesson, and it too is imported by only 43
of the 152 test modules.

A package __init__ is the one file BOTH runners load before any test in the
package runs: unittest discovery imports `tests.test_x`, which imports the
`tests` package first, and pytest does the same because this directory has an
__init__.py. Put here only what must hold for EVERY module under EVERY runner.
`tests/conftest.py` now imports THIS module rather than restating it, so there
is one copy of the logic and no second path to drift out of step.

BUG CLASS: `a-test-may-not-read-live-state-it-did-not-plant`. Green has to mean
"the code is correct", never "the fleet was quiet that minute". Four incidents,
one cause:

1. THE SUITE WAS ORDER-DEPENDENT. `helm/configs/_common.py` computes CWD_ROOTS
   and _HELM_HOME AT IMPORT TIME. Once any module has imported helm.configs
   those values are frozen and a later `os.environ[...] = ...` cannot move
   them. tests/test_configs.py plants a tmp tree at module scope, which works
   only when it is the FIRST module to reach helm.configs. Run `pytest
   tests/test_envtidy.py tests/test_configs.py` and three of its tests fail,
   because test_envtidy pulls in helm.configs first. Alone it passes. Measured
   2026-07-26; it also moved the full-suite failure set around as unrelated
   modules started and stopped failing.

2. WHEN THE FREEZE WON, CWD_ROOTS WAS THE DEFAULT — the developer's REAL dev
   tree. A test run was scanning the owner's actual working tree instead of a
   planted fixture. Verified: CWD_ROOTS froze as the developer's actual
   home-relative dev path.

3. THE SUITE'S ORACLE WAS THE LIVE FLEET. One variable further out:
   chat.chat_dir() defaults to the RUNNING fleet's tmpfs bus
   (/dev/shm/helm-chat), and seats.roster_path(), claims_path(), seen_path()
   and every per-session cursor/pending path derive from it. Planting
   HELM_HOME and not HELM_CHAT_DIR left all of those pointed at production.
   tests/test_gc.py records that with HELM_CHAT_DIR unset the suite read the
   live bus and scan() returned 24,001 victims that ApplyTest's `gc --apply`
   would have DELETED OUT FROM UNDER RUNNING SEATS. And on 2026-07-30 main
   went RED WITH NO COMMIT when ordinary fleet activity reaped a worktree a
   tracked test was reading — 4886 green forty minutes earlier on the same
   files, every land gate failing after, and the next seat to run one
   inheriting a failure it did not cause.

4. THE METAHARNESS QUERY REACHED THE OWNER'S REAL WORKSPACE. `helm work gc`
   asks the metaharness whether a pane is bound to a room before deleting one
   (helm/harness.py:worktree_panes — the 2026-07-30 killed-back-to-a-CWD
   incident). That query runs through `harness.detect()`, which reads the
   AMBIENT environment: measured on this host, a shell inside an Orca pane has
   `orca` on PATH and ORCA_USER_DATA_PATH set, so `detect()` returned a live
   OrcaAdapter and every removal test shelled out to the owner's real
   workspace — 0.25s per call, and a verdict that depended on what the fleet
   was doing. Worse, it fails the WRONG way: the guard refuses on an
   unanswerable host, so an orca that was merely restarting would have turned
   a dozen green tests red for a reason none of them are about.

Deliberately NOT a fixture and NOT setUp: those run too late. This must
execute at import, before any test module reaches a helm module.
"""
import atexit
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile


# Hook timeout suppression is process-external durable state. An inherited
# explicit directory belongs to the parent runner, so sharing it makes one
# test's timed handler silence the next and writes markers outside this suite's
# synthetic estate. Leave the key ABSENT: hookalarm's unittest seam then speaks
# for every arm, while a test remains free to choose a private directory after
# this bootstrap has run and exercise the real suppression mechanism.
os.environ.pop("HELM_HOOK_ALARM_DIR", None)

# A durable Fab job's launch label belongs to the gate that runs this suite,
# which has already minted it (gate.fab_job_label). Inherited, it makes every
# arm that drives `gate run` without a label a train's land gate: 18 arms on
# train209. gateshard.scrubbed_env drops both for the suite child; this covers
# a suite started any other way inside such a job.
for _key in ("FAB_GATE_LABEL", "FAB_GATE_GENERATION"):
    os.environ.pop(_key, None)
# A seat's launch stamps its model family and backend into its environment,
# and a reader of a local family gets the short `helm chat read`
# (helm/chatshort.py). Inherited from a local seat's pane, the pair would turn
# every arm that reads chat short; an arm that needs a family sets one.
for _key in ("HELM_MODEL_FAMILY", "MELD_MODEL_FAMILY", "HELM_MODEL_BACKEND",
             "MELD_MODEL_BACKEND"):
    os.environ.pop(_key, None)
del _key


def _identity_path_env_keys():
    """Load Helm's bounded path declaration without importing ``helm``.

    Import order is this module's safety boundary: importing the package here
    would run Helm code before the suite has redirected the paths that code may
    freeze.  Loading the dependency-free declaration by file keeps that order
    while making this bootstrap and the standalone shard runner consume one
    source of truth.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "helm", "pathenv.py")
    spec = importlib.util.spec_from_file_location(
        "_helm_test_identity_path_env", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("identity path environment declaration unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.IDENTITY_PATH_ENV_KEYS)


IDENTITY_PATH_ENV_KEYS = _identity_path_env_keys()
_TESTROOT = None


def _testroot():
    """The ONE temp tree for this process, removed at exit.

    ONE root per process, never one per plant — tests/_tmphome.py's
    inode-exhaustion lesson: an eager `tempfile.mkdtemp()` whose value
    setdefault then discards orphans a directory on every run, and /tmp died
    of 1,048,575-of-1,048,576 inodes used with 29G of space still free. The
    root is created once, here, and `_route_tmp` below is why it is no longer
    lazy: it is also the process's TMPDIR.
    """
    global _TESTROOT
    if _TESTROOT is None:
        _TESTROOT = tempfile.mkdtemp(prefix="helm-suite-env-")
        atexit.register(_reap, _TESTROOT)
    return _TESTROOT


def _reap(path):
    """The root goes WHATEVER MODES the suite left inside it.

    A fixture that extracts a release through gateauthority plants 0o555
    directories, and `shutil.rmtree(ignore_errors=True)` keeps a read-only
    subtree without a word — measured on a build node after the routing
    below landed: every whole-suite run left its root behind holding exactly
    that release tree. helm.scratch.reap_owned modes the owned directories
    first; it is imported HERE, at exit, because this module must not touch
    helm.* at import (the order law in the docstring above). A tree that
    carries this tests package without helm (a fixture repository) falls
    back to the plain remove it always had.
    """
    try:
        from helm.scratch import reap_owned
    except ImportError:
        shutil.rmtree(path, ignore_errors=True)
        return
    reap_owned(path)


def _route_tmp():
    """Every temp file THIS process mints lands under the one reaped root.

    THE MEASURED DEFECT: the suite leaks its scratch by the tens of
    thousands. One build node held 46,369 owner-uid top-level /tmp entries
    older than two hours — 21,265 bare `tmpXXXXXX`, 10,455 `helm-test-idlayer-*`,
    2,520 `helm-test-seat-identity-*`, 1,260 `seat-rename-*`, 1,127
    `helm-inflight-*`, 960 `probelog-*` ... — and its sibling hit 100% of its
    nr_inodes=1048576 cap, which poisoned three whole-suite receipts
    with Errno 28 about trees that were fine. Every shape is a fixture that
    calls `tempfile.mkdtemp(prefix=...)` in setUp and either never removes it
    (test_seat_ledger, test_inflight_gate, test_probelog, test_proxyjournal
    have no rmtree at all; test_identity_layer and test_seat_identity restore
    the env in tearDown and leave the directory) or removes it only on a
    normal exit that a gate timeout's SIGKILL never gives it.

    CURED AT THE DOOR, NOT PER TEST. tempfile resolves its directory from
    `tempfile.tempdir` first and `$TMPDIR` second, so pointing both at a
    subdirectory of the per-process root makes every mkdtemp / mkstemp /
    TemporaryDirectory / NamedTemporaryFile in this process — and in every
    child that inherits the environment, including the production verbs a
    test drives through subprocess — land inside the tree the atexit hook
    above removes. A fixture that forgets its rmtree now leaks into a
    directory that dies with the process instead of into the node's /tmp.

    NESTED UNDER THE AMBIENT TMP, NEVER REPLACING IT. `mkdtemp` above already
    honoured an operator's or a parent gate's TMPDIR when it chose where the
    root goes, so the mount choice is theirs and only the reaping is ours.
    The gate (`helm gate run`) sets the suite child's TMPDIR to a root of its
    own that its `finally` removes, which is the layer that survives a child
    the gate had to kill.

    ARMED by tests/test_scratch.py ScratchRoutingTripwireTest: a spawned
    canonical runner reports where its mints landed, and the arm asserts the
    ambient directory is EMPTY after the child exits — the effect, not the
    absence of a complaint — with a control that seeds one leak outside the
    root and proves the same census sees it.
    """
    path = os.path.join(_testroot(), "tmp")
    os.makedirs(path, exist_ok=True)
    os.environ["TMPDIR"] = path
    tempfile.tempdir = path
    return path


# ── AF_UNIX socket directories ───────────────────────────────────────────────
#
# NOT UNDER TMPDIR, AND THAT IS THE WHOLE POINT. An AF_UNIX address is
# `sun_path`: 108 bytes with its NUL. `_route_tmp` above nests every mint under
# the ambient TMPDIR, the gate nests that under its own scratch root, and a
# fixture adds its own directories and the socket name. Measured on a build
# node (task/2539): 107 bytes under a /tmp gate, one byte inside the limit;
# under a 92-byte TMPDIR the whole suite had 44 tests erroring "AF_UNIX path
# too long" (test_codexhomes, test_orcaadopt, test_harness). The length of a
# socket path must not depend on how deep the operator's TMPDIR is, so socket
# directories live under a short fixed base instead. CPython made the same
# choice for multiprocessing's forkserver socket, which falls back to /tmp.
#
# STILL ATTRIBUTABLE AND STILL CLEANED, by three means:
#   * ONE ROOT PER PROCESS, `<SOCKET_BASE>/helm-suite-sock-<8>`, minted on
#     first use and reaped by the same atexit `_reap` as the suite root.
#   * THE ROOT NAMES ITS OWNER: `owner` inside it is a symlink to this
#     process's suite root (`_testroot()`), which lives under TMPDIR.
#   * A KILLED RUN never runs atexit, and the gate's `finally` removes only its
#     own scratch — which takes the suite root with it and leaves `owner`
#     DANGLING. The next process that mints a socket root removes every root
#     of this uid whose `owner` dangles. A root whose owner still exists
#     belongs to a live run and is never touched; a root without an `owner`
#     link cannot be attributed and is left alone.
SUN_PATH_BYTES = 108
# THE BUDGET every test socket path stays inside: sun_path less its NUL and a
# 16-byte margin, so a fixture that grows a longer name, or a base that moves,
# is refused below before it reaches the kernel's limit.
SOCKET_PATH_MARGIN = 16
SOCKET_PATH_BUDGET = SUN_PATH_BYTES - 1 - SOCKET_PATH_MARGIN
SOCKET_NAME_BYTES = 16          # the longest name a fixture joins onto the dir
SOCKET_BASE = "/tmp"
SOCKET_ROOT_PREFIX = "helm-suite-sock-"
SOCKET_OWNER_LINK = "owner"
_SOCKROOT = None

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_SOCKROOT": "the suite's socket root, made once per process",
}


def _reap_orphan_socket_roots():
    """Remove this uid's socket roots whose owning suite root is gone."""
    try:
        names = os.listdir(SOCKET_BASE)
    except OSError:
        return
    for name in names:
        if not name.startswith(SOCKET_ROOT_PREFIX):
            continue
        root = os.path.join(SOCKET_BASE, name)
        owner = os.path.join(root, SOCKET_OWNER_LINK)
        try:
            st = os.lstat(root)
            os.readlink(owner)
        except OSError:
            continue
        # ONLY ENOENT IS "GONE". os.path.exists() answers False for EVERY
        # failure, so a live run whose suite root sits under a directory its
        # operator closed (EACCES on the way down) read as an orphan and had
        # its socket tree deleted by the next run's first socket_dir().
        # Unreadable is UNKNOWN: leave it alone.
        try:
            os.stat(owner)          # follows the link, so this is the OWNER
        except FileNotFoundError:
            pass                    # the suite root it names is really gone
        except OSError:
            continue                # cannot tell; never delete on a maybe
        else:
            continue                # the owner is alive
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            shutil.rmtree(root, ignore_errors=True)


def _socketroot():
    global _SOCKROOT
    if _SOCKROOT is None:
        if not os.access(SOCKET_BASE, os.W_OK | os.X_OK):
            raise OSError(
                "AF_UNIX test sockets need a short writable base directory: "
                "%s is not writable, and a socket under TMPDIR can exceed "
                "sun_path (%d bytes with its NUL)" % (SOCKET_BASE, SUN_PATH_BYTES))
        _reap_orphan_socket_roots()
        root = tempfile.mkdtemp(prefix=SOCKET_ROOT_PREFIX, dir=SOCKET_BASE)
        atexit.register(_reap, root)
        os.symlink(_testroot(), os.path.join(root, SOCKET_OWNER_LINK))
        _SOCKROOT = root
    return _SOCKROOT


def socket_dir(case=None):
    """A fresh directory for AF_UNIX socket files, whose length does not grow
    with TMPDIR. Every test-fixture socket goes through here.

    `case` (a TestCase) removes the directory at its cleanup; without one it
    goes with the process's socket root at exit. Refuses, naming the limit,
    when the directory leaves no room for a SOCKET_NAME_BYTES name inside
    SOCKET_PATH_BUDGET.
    """
    path = tempfile.mkdtemp(dir=_socketroot())
    if case is not None:
        case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    if len(os.fsencode(path)) + 1 + SOCKET_NAME_BYTES > SOCKET_PATH_BUDGET:
        raise OSError("socket directory %s leaves no room for a %d-byte socket "
                      "name inside the %d-byte socket path budget (sun_path %d "
                      "bytes with its NUL)"
                      % (path, SOCKET_NAME_BYTES, SOCKET_PATH_BUDGET,
                         SUN_PATH_BYTES))
    return path


# ── THE REFUSAL: a socket path that can grow with TMPDIR ─────────────────────
#
# socket_dir() is a door, and a door protects only the fixtures that use it.
# tests/test_socket_paths.py measures the sites it lists at the TMPDIR lengths
# it chooses, and round 1 of task/2539 found one it did not list (the
# forkserver socket behind test_board's multiprocessing.Pool) and a fixture
# spelling its static scan could not see. So every process that imports this
# package installs an audit hook that refuses an AF_UNIX bind, connect,
# connect_ex, sendto or sendmsg whose filesystem address
#   * is longer than SOCKET_PATH_BUDGET bytes, or
#   * lies under this process's suite root, where TMPDIR points. Such a path
#     is TMPDIR's length plus the fixture's, so it fits on one node and breaks
#     on the next; refusing it at EVERY length is what makes a short-TMPDIR
#     run fail the fixture that a long-TMPDIR run would break.
# An abstract-namespace name (leading NUL), an empty autobind address and a
# non-path address are not refused, and neither is a system socket inside the
# budget (/run/user, nscd, systemd), which does not live under the suite root.
#
# NOT AN OSError. Production code around a socket catches OSError and reports
# the endpoint as down (harness.OrcaAdapter wraps it as "orca runtime rpc ...
# failed"), so an OSError refusal would let a dead-endpoint test pass on the
# wrong error. SocketPathRefused derives from BaseException so that neither
# `except OSError` nor `except Exception` swallows it; unittest records it as
# an error.
#
# 108 BYTES OR MORE NEVER REACHES THE HOOK. CPython checks an AF_UNIX address
# against sun_path while parsing it, before it raises the audit event, so such
# a path fails with OSError "AF_UNIX path too long" and the hook sees nothing.
# The budget rule covers 92 to 107 bytes. A fixture whose path TMPDIR made
# that long lies under the suite root, so a run at a shorter TMPDIR refuses
# the same fixture through the second rule.
#
# COST AND REACH. An audit hook cannot be removed and sees every audited event
# in the process (every open, import and exec), so the first statement returns
# for anything that is not a socket address event. The hook exists only in a
# process that imported this package: production verbs a test runs as a
# subprocess do not import it.
_SOCKET_ADDRESS_EVENTS = frozenset(
    ("socket.bind", "socket.connect", "socket.sendto", "socket.sendmsg"))


class SocketPathRefused(BaseException):
    """An AF_UNIX address that is over the budget or grows with TMPDIR."""


def _refuse_socket_paths_that_grow_with_tmpdir(event, args):
    if event not in _SOCKET_ADDRESS_EVENTS:
        return
    sock, address = args[0], args[1]
    try:
        if sock.family != socket.AF_UNIX:
            return
        path = os.fsencode(bytes(address) if isinstance(address, bytearray)
                           else address)
    except (AttributeError, TypeError):
        return
    if not path or path[:1] == b"\0":
        return
    if len(path) > SOCKET_PATH_BUDGET:
        why = ("its %d bytes are over the %d-byte socket path budget (sun_path "
               "%d bytes with its NUL, less a %d-byte margin)"
               % (len(path), SOCKET_PATH_BUDGET, SUN_PATH_BYTES,
                  SOCKET_PATH_MARGIN))
    elif _TESTROOT and path.startswith(os.fsencode(_TESTROOT) + b"/"):
        why = ("it lies under the suite root %s, where TMPDIR points, so its "
               "length grows with TMPDIR" % _TESTROOT)
    else:
        return
    raise SocketPathRefused(
        "AF_UNIX %s to %s refused by tests/__init__.py: %s. Make the socket's "
        "directory with tests.socket_dir()."
        % (event.split(".", 1)[1], os.fsdecode(path), why))


def _plant(var, sub, legacy=None):
    """Point `var` at a tmp path unless the caller genuinely chose one.

    setdefault SEMANTICS, but not `os.environ.setdefault`, because the builtin
    is wrong here in two directions:

    EMPTY IS UNSET. `setdefault` only checks whether the KEY exists, so an
    ambient `HELM_CHAT_DIR=` (exported empty by a wrapper, a CI matrix cell, or
    a `docker run -e HELM_CHAT_DIR`) counts as already-chosen and the plant
    silently does nothing. Every consumer disagrees with that reading:
    chat_dir() is `home.surface_dir("CHAT_DIR", ...)` whose explicit arm is
    `env()`-read and falls through on empty exactly the same way, _common's home is
    `get("HELM_HOME") or get("MELD_HOME") or ~/.helm`, and detect() reads
    `(env.get("HELM_METAHARNESS") or "").strip()` — all of them treat "" as
    absent and fall through to the LIVE default. So an empty value was the one
    input that defeated the plant and re-armed every incident above.
    tests/_tmphome.home() already reads emptiness this way (`if current:`).

    THE LEGACY NAMESPACE MUST STILL WIN. Each of these vars has an older
    companion helm still honors, and planting the HELM_ name masks it:
    home.env prefers HELM_ over MELD_, and _common reads
    `get("HELM_CONFIG_ROOTS", <the predecessor's spelling>)` where the helm
    home declares a predecessor (`_declared_config_roots_spelling`). An operator who
    deliberately set MELD_CHAT_DIR would have been overridden by a plant whose
    whole contract is to defer to deliberate choices. When the legacy name
    holds a value we POP the HELM_ name rather than copy the value across:
    copying would give a MELD-provenance value a HELM-namespace spelling, and
    home.env_pair exists precisely so "a preferred HELM value must never
    inherit stale MELD provenance".

    What we refuse is only the case where NOBODY chose: there the frozen
    default is a real home or the running fleet's bus.
    """
    chosen = os.environ.get(var)
    if chosen:
        return chosen
    # legacy=None: a var with NO older companion (ORCA_USER_DATA_PATH is
    # orca's own name, not helm's). Naming an invented MELD_ spelling here to
    # satisfy the signature would document a namespace that does not exist.
    inherited = os.environ.get(legacy) if legacy else None
    if inherited:
        os.environ.pop(var, None)
        return inherited
    path = os.path.join(_testroot(), sub)
    os.makedirs(path, exist_ok=True)
    os.environ[var] = path
    return path


def _declared_config_roots_spelling(helm_home):
    """The predecessor's spelling of HELM_CONFIG_ROOTS where the helm home this
    suite runs under declares one (helm/localnames.py `predecessor`), else
    None. Production honours that spelling only there, so the plant defers to
    it only there. Read directly: no helm module may be imported before the
    plants, and helm.configs freezes its roots at import."""
    path = os.path.join(helm_home or "", "_global", "local-names.json")
    try:
        with open(path, encoding="utf-8") as f:
            pred = json.load(f).get("predecessor")
    except (OSError, ValueError, AttributeError):
        return None
    if not (isinstance(pred, str) and re.fullmatch(r"[a-z][a-z0-9]{0,31}", pred)):
        return None
    return "%s_CONFIG_ROOTS" % pred.upper()


def _plant_file(var, name, legacy):
    """`_plant` for a var naming a FILE, with the same two readings.

    SEPARATE FROM `_plant` BECAUSE THE DIRECTORY IS THE WHOLE DIFFERENCE:
    `_plant` mkdirs the path it hands out, and a consumer that opens its var
    as a file would get a directory. This one creates NOTHING. Absence is a
    first-class state for an authority file — seatname_guard distinguishes an
    absent authority from a malformed one and from an unresolvable one — so
    planting an empty file here would erase a distinction arms depend on.

    ABSOLUTE BY CONSTRUCTION, and that is load-bearing rather than incidental:
    seatname_guard.authority_path REFUSES a relative override, because it
    would name a different file from every working directory.
    """
    chosen = os.environ.get(var)
    if chosen:
        return chosen
    inherited = os.environ.get(legacy)
    if inherited:
        os.environ.pop(var, None)
        return inherited
    path = os.path.join(_testroot(), name)
    os.environ[var] = path
    return path


def _plant_seat_name_authority():
    """Replace a parent's authority target with an absent suite-owned file.

    Both spellings are real inputs to the production resolver, with HELM taking
    precedence over MELD.  Neither inherited spelling is a test-local choice:
    preserving one lets a real roster projection replace the parent's file.
    Scrub both immediately before the existing absent-file plant.  Tests remain
    free to set either spelling after bootstrap and exercise production
    precedence themselves.
    """
    os.environ.pop("HELM_SEAT_NAMES", None)
    os.environ.pop("MELD_SEAT_NAMES", None)
    return _plant_file("HELM_SEAT_NAMES", "seat-names.txt", "MELD_SEAT_NAMES")


def _redirect_actor_store():
    """Make Actor writes derive from this process's planted Helm home.

    Unlike an ordinary fixture override, an inherited Actor path can name the
    PARENT runner's identity authority.  Preserving it lets a worker test bind
    or rename a real Actor while every other path looks isolated.  Both modern
    and legacy spellings are therefore scrubbed unconditionally; leaving them
    absent is load-bearing, because per-test HELM_HOME fixtures must still move
    their own Actor stores rather than sharing one suite-global explicit path.
    """
    keys = tuple(key for key in IDENTITY_PATH_ENV_KEYS
                 if key.endswith("_ACTORS"))
    if set(keys) != {"HELM_ACTORS", "MELD_ACTORS"}:
        raise RuntimeError("identity path declaration has no exact Actor pair")
    for key in keys:
        os.environ.pop(key, None)


def _redirect_proxyjournal():
    """Make the journal default derive from this process's planted Helm home.

    An inherited explicit log path belongs to the PARENT runner.  Preserving it
    sends a real episode-end append outside the worker's synthetic home, while
    merely disabling the journal would discard the evidence the test produced.
    Leave the override absent here so the shipped resolver follows HELM_HOME;
    a test that needs an explicit path may still choose one after bootstrap.
    """
    os.environ.pop("HELM_PROXYJOURNAL_LOG", None)


def _redirect_home_following_stores():
    """Make writable home-following stores derive from the planted Helm home.

    Both families dynamically resolve their modern and legacy aliases on every
    write.  An inherited explicit directory therefore keeps naming the parent
    runner's store even after HELM_HOME is isolated.  Scrub both spellings and
    leave them absent: the shipped resolver then follows HELM_HOME, while tests
    remain free to install either private alias after bootstrap.
    """
    for key in ("HELM_TURNSTAMP_DIR", "MELD_TURNSTAMP_DIR",
                "HELM_MULTIPLAYER_DIR", "MELD_MULTIPLAYER_DIR"):
        os.environ.pop(key, None)


# The two that freeze inside helm.configs at import, the chat root every chat /
# seats / claims / cursor path derives from, and the Actor authority that must
# never inherit a parent runner's absolute override.
# FIRST, because every plant below is a path under the same root and the ROOT
# is what this one makes reapable: see `_route_tmp`.
_TEST_TMP = _route_tmp()
_TEST_HOME = _plant("HELM_HOME", "helm-home", "MELD_HOME")
_redirect_actor_store()
_redirect_proxyjournal()
_redirect_home_following_stores()
PLANTED = {
    "TMPDIR": _TEST_TMP,
    "HELM_CONFIG_ROOTS": _plant("HELM_CONFIG_ROOTS", "cwd-roots",
                                _declared_config_roots_spelling(_TEST_HOME)),
    "HELM_HOME": _TEST_HOME,
    "HELM_CHAT_DIR": _plant("HELM_CHAT_DIR", "chat", "MELD_CHAT_DIR"),
    # INCIDENT 5, AND IT IS THE ONE HELM_HOME DOES NOT COVER. The seat-name
    # authority does NOT derive from HELM_HOME: seatname_guard.authority_path
    # falls back to os.path.expanduser("~")/.helm/_global/seat-names.txt, so
    # with this var unset EVERY test in the suite reads the OPERATOR'S REAL
    # roster of seat names and lets it decide what a fixture may call a seat.
    # Measured on trunk: unset resolves to /home/<operator>/.helm/_global/
    # seat-names.txt while an explicit absolute path resolves to itself.
    # POPPING THE VAR IS NOT ISOLATION HERE, IT IS THE EXPOSURE: unset is
    # exactly the state that selects the real file, so a per-file setUp that
    # sandboxes by popping re-arms this on every one of its tests.
    "HELM_SEAT_NAMES": _plant_seat_name_authority(),
    # INCIDENT 4's OTHER HALF, and it is not a helm variable at all.
    # `harness.OrcaAdapter._user_data_path()` falls back to
    # $XDG_CONFIG_HOME/orca (else ~/.config/orca) when this is unset, and
    # three production readers stand on it: the RPC leg's runtime metadata
    # (which holds the daemon's AUTH TOKEN), codexhomes' orca-managed
    # credential store, and the orca cred-follow rung — which, wired into
    # `seat doctor --ensure` and the proxywatch pass, would translate the
    # OWNER'S LIVE CODEX CREDENTIAL into a fixture's temp pool on any test
    # that drives either pass. Three separate test modules
    # (tests/_fakeorca.py, tests/test_orcaadopt.py, tests/test_codexhomes.py)
    # each wrote their own copy of "without this the adapter falls back to
    # $HOME/.config/orca and a unit test talks to the owner's live
    # workspace"; a rule three files restate belongs at the ONE door every
    # runner loads. Those per-module pins still win — this is setdefault.
    "ORCA_USER_DATA_PATH": _plant("ORCA_USER_DATA_PATH", "orca-user-data"),
}

# EXPLICIT ROOM FOR EVERY TEST PROCESS. chat.post's room default DERIVES (the
# env seam, then the cwd's project) instead of hardcoding "main". A test
# process's cwd is the REPO, so a bare default-relying post would derive the
# repo's room, and the hermetic arms asserting #main would fail for a reason
# unrelated to what they test. Tests therefore declare their room through the
# same env seam every launched seat uses (launch.sh sets HELM_CHAT_ROOM).
# Tests OF the derivation mock _default_post_room and are untouched by this.
# PLANTED HERE, at the package import every runner makes before any test
# module, and never at a helper module's first import: that import happens
# inside whichever test module reaches the helper first, so the room appeared
# mid-run, after every module that ran earlier, and a slice worker's leak audit
# named that module for it. setdefault: a harness that exports a room keeps it.
os.environ.setdefault("HELM_CHAT_ROOM", "main")

# THE TREE UNDER TEST RUNS ITS OWN COORDINATION VERBS (task/3382). A process
# entry of `bin/helm` or `python3 -m helm` hands a coordination verb (dispatch,
# chat, work, ...) to the TRUNK checkout of its repository when that
# repository declares one (`helm/trunkroute.py`). A suite is the one caller
# whose whole point is this tree's code: an arm that spawns this tree's
# `bin/helm chat` from a lane worktree of a declared repository would
# otherwise test trunk's chat and pass or fail for trunk's reasons. The fab
# rooms are worktrees of a bare mirror that declares nothing, so there the
# route is never taken and the opt-in prints nothing; it is set here for
# every other place a suite runs. Must equal trunkroute.OPT_IN;
# tests/test_trunkroute.py pins it (this module may not import helm).
os.environ["HELM_LANE_COORDINATION"] = "1"
PLANTED["HELM_LANE_COORDINATION"] = "1"

# Installed after PLANTED has minted the suite root the hook compares
# against. THE REFUSAL, after socket_dir(), says what it refuses and why.
if hasattr(socket, "AF_UNIX"):
    sys.addaudithook(_refuse_socket_paths_that_grow_with_tmpdir)

# NO TEST MAY READ THIS BOX'S GATE ADMISSION UNLESS IT ASKS TO (task/1740).
#
# `gate._admit_suite` reads the HOST by design — its real /proc for suites,
# memory pressure and panes, and the node-wide ledger under the RAM root — and
# no environment redirects it, because the cap has no override. So an arm that
# ran `gate.run` was red or green by what else the node was running: MEASURED,
# 25 test_gate arms refused by the build host's live whole-suite cap while two whole
# suites ran there, all passing on a quiet box. The scratch preflight reads the
# host's tmp mount the same way. gate raises HOST_ADMISSION_EVENT through
# sys.audit just before any of the three touches the host, and this hook
# refuses it. The cure is `tests._tmphome.pin_admission(case)`, which runs the
# real door against a fixture box; an arm that is ABOUT the host's own reading
# opens `with HostAdmissionAsked():` around it. BaseException, like the socket
# refusal above, so no `except Exception` can fold it into a refused run.
# Must equal gate.HOST_ADMISSION_EVENT; tests/test_gate.py pins it (this
# module may not import helm).
HOST_ADMISSION_EVENT = "helm.gate.host_admission"


class HostAdmissionRefused(BaseException):
    """A test process reached this box's gate admission without asking."""


class HostAdmissionAsked(object):
    """The one opt-in: the block may read this box's admission. Nestable."""

    depth = 0

    def __enter__(self):
        HostAdmissionAsked.depth += 1
        return self

    def __exit__(self, *exc):
        HostAdmissionAsked.depth -= 1
        return False


def _refuse_unasked_host_admission(event, args):
    if event != HOST_ADMISSION_EVENT or HostAdmissionAsked.depth:
        return
    raise HostAdmissionRefused(
        "gate admission touched THIS box (proc=%r, ledger or tmp=%r) in a "
        "test that did not ask, refused by tests/__init__.py: the verdict "
        "would depend on how full the node is. Call tests._tmphome.pin_admission(self) in "
        "the fixture (the real door against a fixture box), or wrap an arm "
        "that is about the host's own reading in `with HostAdmissionAsked():`."
        % (args[0], args[1]))


sys.addaudithook(_refuse_unasked_host_admission)

# NO TEST MAY REACH THE OPERATOR'S LIVE METAHARNESS (incident 4 above).
#
# `none` is a first-class value of this variable (harness.detect: "none = pane
# ops off even with both installed"), not a test-only escape hatch, so this
# selects the headless adapter every CI box already runs under rather than
# faking one. It is not a path, so it does not go through _plant — but it takes
# the same empty-is-unset reading, because detect() coerces "" to a
# fall-through and an ambient `HELM_METAHARNESS=` would otherwise re-arm the
# live adapter. A test that WANTS an adapter still mocks its own seam
# (`detect` takes env=/which= for exactly that).
if not os.environ.get("HELM_METAHARNESS"):
    os.environ["HELM_METAHARNESS"] = "none"
PLANTED["HELM_METAHARNESS"] = os.environ["HELM_METAHARNESS"]

# NO LADDER MAY SPEAK INTO AN ARM THAT PINS ITS STDERR.
#
# The Stop guard's rung trace streams to stderr once a ladder passes a
# FRACTION OF ITS BUDGET, and its one-shot flush fires on WALL TIME into
# whatever `sys.stderr` names at that instant — so a ladder one arm left
# unfinished flushes its whole buffer inside whichever LATER arm holds a
# `contextlib.redirect_stderr`. That is a suite-wide channel keyed on how
# loaded the node is, and it reddened two whole-suite gates on two unrelated
# modules with `(0, '[helm stop-guard timing] BEGIN identity ...')` against an
# expected `(0, '')`, each of which passes alone.
#
# `off` is a first-class value of this variable (seats_stop_timing.STREAM_OFF),
# not a test-only escape hatch: it stops the time-keyed STREAM and leaves the
# buffer and the durable end-of-ladder report exactly as they are. `0` is the
# opposite setting and could not serve — it streams from the FIRST line. Not a
# path, so it does not go through _plant, but it takes the same empty-is-unset
# reading: `stream_after` reads an empty value as absent and falls through to
# the fraction. A test that is ABOUT the stream sets its own value and restores
# this one — tests/test_hook_wrapper.py, tests/test_seats.py and
# tests/test_stop_lease_latch.py all do.
if not os.environ.get("HELM_STOP_TIMING_AFTER"):
    os.environ["HELM_STOP_TIMING_AFTER"] = "off"
PLANTED["HELM_STOP_TIMING_AFTER"] = os.environ["HELM_STOP_TIMING_AFTER"]

# NO FILING IN THE SUITE MAY START A REAL MODEL READ.
#
# Every review row filed through `dispatches.add`, `send` or `retip` starts the
# qwen27 findings pass (helm/findingspass.py) in a DETACHED process, and its
# default script and endpoint are the owner's real ones. Hundreds of arms file
# review rows, so without this each of them would queue a real read against a
# shared model server and outlive the arm that filed it. `off` is the
# production switch, not a test-only escape hatch, and it is set
# UNCONDITIONALLY: an inherited `on` would re-arm exactly that. The arms that
# are ABOUT the pass (tests/test_findingspass.py) turn it on for themselves,
# point it at a fixture script, and restore this value.
os.environ["HELM_QWEN27_FINDINGS"] = "off"
PLANTED["HELM_QWEN27_FINDINGS"] = os.environ["HELM_QWEN27_FINDINGS"]

# NO TEST MAY CHANGE THIS HOST'S SCHEDULER.
#
# `helm seat launch`, `spawn` and `resume` call autocompact.ensure_timer, which
# writes ~/.config/systemd/user/helm-autocompact.service and .timer with this
# checkout as WorkingDirectory, then runs the real `systemctl --user
# daemon-reload` and `enable --now`. HELM_HOME does not move that path, and no
# mock reaches a child process: tests/test_new_agent_guide.py runs a real
# `helm seat launch codex -i 2` child, which got that far on any host holding
# a codex cred. `0` is the production switch (autocompact.TIMER_ENV), not a
# test-only escape hatch, and it is set UNCONDITIONALLY so an inherited value
# cannot re-arm the install. Every child that inherits this environment gets
# it too. The arms that are ABOUT the install (tests/test_autocompact.py)
# remove it inside mock.patch.dict and patch the writes and systemctl.
os.environ["HELM_AUTOCOMPACT_TIMER"] = "0"
PLANTED["HELM_AUTOCOMPACT_TIMER"] = os.environ["HELM_AUTOCOMPACT_TIMER"]
# The same for the gate canary's nightly timer, which `helm work install-guard
# --apply` installs with the rail: every arm that applies the rail would
# otherwise write ~/.config/systemd/user units and run the real systemctl.
# `0` is gatecanary.TIMER_ENV's production switch, set UNCONDITIONALLY; the
# arms ABOUT the install remove it inside mock.patch.dict and patch the
# writes and systemctl.
os.environ["HELM_GATE_CANARY_TIMER"] = "0"
PLANTED["HELM_GATE_CANARY_TIMER"] = os.environ["HELM_GATE_CANARY_TIMER"]
# The same for the nightly release dry run's timer (releasenightly.TIMER_ENV),
# which `helm release nightly --install-timer` installs; its arm ABOUT the
# install sets the switch itself and patches systemctl.
os.environ["HELM_RELEASE_NIGHTLY_TIMER"] = "0"
PLANTED["HELM_RELEASE_NIGHTLY_TIMER"] = os.environ["HELM_RELEASE_NIGHTLY_TIMER"]
# The same for the shared-checkout watch's timer (checkoutwatch.TIMER_ENV),
# which `helm work checkout-watch --install-timer` installs; the arms ABOUT
# the install remove it inside mock.patch.dict and install through
# tests._tmphome.fake_user_systemd.
os.environ["HELM_CHECKOUT_WATCH_TIMER"] = "0"
PLANTED["HELM_CHECKOUT_WATCH_TIMER"] = os.environ["HELM_CHECKOUT_WATCH_TIMER"]

# NO TEST MAY REACH THIS HOST'S SYSTEMD USER MANAGER OR ITS UNIT FILES
# (task/3306).
#
# The four switches above each close ONE installer, in a tree that carries
# the switch, while the environment keeps it. Twenty helm modules run
# `systemctl`, each spelling its own call, and a fab job runs with the host's
# own HOME, XDG_RUNTIME_DIR and session bus: fab's worker sets PATH, never
# HOME or XDG_*. MEASURED on both fab gate hosts: their gate-canary and
# autocompact units name a fab worktree as WorkingDirectory and are enabled,
# so test runs wrote AND enabled them. The canary timer has fired there since,
# running a serial and a sliced whole suite from a lane's worktree on a host
# every land depends on, and one autocompact unit names a worktree that is
# gone, so it fails every minute. A reviewed tip left `helm-test.timer`
# behind the same way. Moving HOME would not have been enough:
# `systemctl --user` reaches the manager over $XDG_RUNTIME_DIR/systemd/private
# or the session bus, whatever HOME says.
#
# TWO LAYERS, both here because this is the one file every runner loads.
#
#   THE BUS IS DEAD for this process and every child that inherits its
#   environment: XDG_RUNTIME_DIR names an empty directory under the suite root
#   and DBUS_SESSION_BUS_ADDRESS a socket that does not exist, set
#   UNCONDITIONALLY so an inherited value cannot re-arm the host's. Measured on
#   both hosts (systemd 257): `systemctl --user` then fails "Failed to connect
#   to user scope bus", and a cleared environment, with both unset, fails the
#   same way. This is the only layer that reaches a CHILD -- a verb a test runs
#   as a subprocess, a shell stage -- whatever path it spells systemctl by.
#
#   THE REFUSAL, an audit hook in this process, is what makes a missing fake
#   LOUD instead of a "systemctl failed" the installer reports and the arm
#   never reads. It refuses (1) an exec of any systemctl that does not resolve
#   under this process's suite root, by argv, a wrapper, a shell string,
#   os.system, os.exec* or posix_spawn: a fake a test built lives under the
#   root, and anything else is the host's binary; and (2) a write, rename,
#   removal, link or mkdir under the host's systemd directories. The host's
#   home is its passwd entry, never $HOME, which is what a test moves.
#
# THE SEAM a test fakes is tests._tmphome.fake_user_systemd(self): HOME in a
# temp dir, HELM_USER_UNIT_DIR at that home's unit directory, and a recording
# systemctl first on PATH. A test may still mock the module's subprocess.run
# instead; a mock execs nothing, so nothing here fires.
#
# WHAT THIS CANNOT SEE: a Python CHILD writing a unit under the owner's real
# home. It inherits HOME and carries no audit hook. The dead bus keeps the test
# from enabling or reloading that unit, but the file lands, and an overwrite of
# a unit the host already enabled is live at the host's next reload.
#
# THE UNIT DIRECTORY IS THE ARM'S OWN (task/3307). Every helm installer and
# reader asks timerhealth.user_unit_dir(), which answers HELM_USER_UNIT_DIR
# before HOME. An inherited value belongs to whoever started the suite, and it
# would outrank every temp HOME an arm moves to, in this process and in every
# child the arm runs, so it is removed. It is not PLANTED under the suite
# root: that would also outrank the temp HOME of every arm that moves HOME by
# hand to hold its units, and those arms would share one directory.
os.environ.pop("HELM_USER_UNIT_DIR", None)
_HOST_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")
_DEAD_BUS = os.path.join(_testroot(), "run")
os.makedirs(_DEAD_BUS, mode=0o700, exist_ok=True)
os.environ["XDG_RUNTIME_DIR"] = _DEAD_BUS
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + os.path.join(
    _DEAD_BUS, "bus")
PLANTED["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
PLANTED["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
# ADDITIVE ONLY: a directory named here is refused as the host's too, and the
# passwd home stays refused whatever it says. tests/test_systemd_isolation.py
# sets it in a child to prove the write refusal against a temp dir, so a
# regression there writes that temp dir and never the real one.
HOST_HOME_ENV = "HELM_SUITE_HOST_HOME"


def _host_systemd_dirs():
    """The directories the host's user manager reads its units and state from:
    the config, data and runtime `systemd` trees of the passwd home."""
    homes = [os.environ.get(HOST_HOME_ENV)]
    try:
        import pwd
        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        pass
    dirs = [os.path.join(os.environ[var], "systemd")
            for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")
            if os.environ.get(var)]
    for home in filter(None, homes):
        dirs += [os.path.join(home, ".config", "systemd"),
                 os.path.join(home, ".local", "share", "systemd")]
    for run in (_HOST_RUNTIME_DIR, "/run/user/%d" % os.getuid()):
        if run and run != _DEAD_BUS:
            dirs.append(os.path.join(run, "systemd"))
    found = set()
    for path in dirs:
        if os.path.isabs(path):
            found.update((os.path.abspath(path), os.path.realpath(path)))
    return tuple(sorted(found))


HOST_SYSTEMD_DIRS = _host_systemd_dirs()


class SystemdRefused(BaseException):
    """A test process reached the host's systemctl or its unit directories."""


_SYSTEMD_EXEC_EVENTS = frozenset((
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
    "os.posix_spawnp", "os.spawn"))
_SYSTEMD_WRITE_EVENTS = frozenset((
    "open", "os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.symlink",
    "os.link", "os.truncate", "shutil.rmtree"))
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# A program that runs the next word as its own command: `env X=1 systemctl`.
_EXEC_WRAPPERS = frozenset((
    "env", "exec", "command", "nohup", "setsid", "nice", "ionice", "timeout",
    "stdbuf", "sudo", "doas", "chrt", "taskset", "flock", "xargs", "time"))
_SHELLS = frozenset(("sh", "bash", "dash", "zsh", "ksh"))
# systemctl at a shell COMMAND position, after any assignments and wrappers.
_SHELL_SYSTEMCTL = re.compile(
    r"(?:^|[;&|(`{\n]|\$\()\s*"
    r"(?:(?:[A-Za-z_]\w*=\S*|env|exec|command|nohup|setsid|nice|sudo"
    r"|timeout\s+\S+|stdbuf\s+\S+)\s+)*"
    r"((?:\S*/)?systemctl)(?=$|[\s;&|)`}])")


def _text(value):
    try:
        return os.fsdecode(os.fspath(value))
    except TypeError:
        return None                 # a file descriptor, or not a path at all


def _systemctl_spelled(argv, executable=None):
    """The spelling of systemctl this command line would run, else None."""
    words = [w for w in map(_text, argv or ()) if w is not None]
    if executable is not None and os.path.basename(
            _text(executable) or "") == "systemctl":
        return _text(executable)
    if not words:
        return None
    head = os.path.basename(words[0])
    if head == "systemctl":
        return words[0]
    if head in _EXEC_WRAPPERS:
        return next((w for w in words[1:]
                     if os.path.basename(w) == "systemctl"), None)
    if head in _SHELLS:
        for i, word in enumerate(words[1:-1], 1):
            if word[:1] == "-" and word[1:2] != "-" and "c" in word:
                found = _SHELL_SYSTEMCTL.search(words[i + 1])
                return found.group(1) if found else None
    return None


def _host_systemctl(spelled, env=None, cwd=None):
    """The resolved host binary `spelled` would run, or None when it is a
    suite-built fake or resolves to nothing (the exec then fails on its own)."""
    if "/" in spelled:
        path = os.path.join(_text(cwd) or os.getcwd(), spelled)
    else:
        path = shutil.which(spelled, path=os.pathsep.join(
            os.get_exec_path(env)))
    if not path:
        return None
    root = os.path.realpath(_testroot())
    real = os.path.realpath(path)
    return None if real.startswith(root + os.sep) else real


def _under_host_systemd(value):
    path = _text(value)
    if path is None or ("systemd" not in path and os.path.isabs(path)):
        return None
    path = os.path.abspath(path)
    return next((d for d in HOST_SYSTEMD_DIRS
                 if path == d or path.startswith(d + os.sep)), None)


def _systemd_refusal(event, args):
    """The refusal text for one audited event, or None."""
    if event in _SYSTEMD_EXEC_EVENTS:
        if event == "subprocess.Popen":
            spelled = _systemctl_spelled(args[1], args[0])
            env, cwd = args[3], args[2]
        elif event == "os.system":
            found = _SHELL_SYSTEMCTL.search(_text(args[0]) or "")
            spelled, env, cwd = found and found.group(1), None, None
        else:                       # (path, argv, env), os.spawn leads with mode
            path, argv, env = args[-3:]
            spelled, cwd = _systemctl_spelled(argv, path), None
        host = spelled and _host_systemctl(spelled, env, cwd)
        if not host:
            return None
        return ("`%s` would run this host's systemctl (%s), which reaches its "
                "real user manager whatever HOME says" % (spelled, host))
    if event == "open":
        if not (args[2] or 0) & _WRITE_FLAGS:
            return None
        paths = (args[0],)
    elif event == "os.rename":
        paths = args[:2]            # moving a unit OUT removes it too
    elif event in ("os.symlink", "os.link"):
        paths = (args[1],)          # only the new name is written
    else:
        paths = (args[0],)
    for path in paths:
        where = _under_host_systemd(path)
        if where:
            return ("%s %s writes under this host's %s, which its user "
                    "manager reads units from" % (event, _text(path), where))
    return None


def _refuse_host_systemd(event, args):
    if event not in _SYSTEMD_EXEC_EVENTS and event not in _SYSTEMD_WRITE_EVENTS:
        return
    try:
        why = _systemd_refusal(event, args)
    except Exception:               # noqa: BLE001 -- a hook that raised would
        return                      # break the audited call itself, not refuse it
    if why:
        raise SystemdRefused(
            "%s: refused by tests/__init__.py. Fake it with "
            "tests._tmphome.fake_user_systemd(self), which moves HOME into a "
            "temp dir and puts a recording systemctl first on PATH, or mock "
            "the installer's subprocess.run." % why)


sys.addaudithook(_refuse_host_systemd)

# NO TEST MAY READ THIS BOX'S SEAT MEMORY UNLESS IT ASKS TO.
#
# `seats_report.roster_report()` called without `pressure=` takes its own
# reading through `seatceiling.fleet_pressure()`, which walks the host's /proc
# and reads every seat slice under /sys/fs/cgroup, and sleeps a 0.25 s window
# when any slice is past 80% of its ceiling. So every roster, web and scratch
# arm that built a report read whatever the box running it was doing.
# MEASURED on an instrumented copy of the suite: one whole-suite run on a
# gate node made 87 host walks from 14 test modules (85
# through roster_report, 2 through `helm scratch`; 5 on web-server request
# threads). There each walk cost 1.7-5 ms, 0.18 s per suite, because the gate
# nodes carry no seat slice. On a seat host (27 slices, one past 80%) one walk
# costs 0.33-0.46 s, so the same suite would spend about 30-40 s, and any
# fixture seat named like a live one would carry that seat's memory cell.
#
# `off` is the production switch (seatceiling.SWITCH), not a test-only escape
# hatch, and it is set UNCONDITIONALLY: an inherited `on` would re-arm the
# walk. It covers the host's tree only; a reading of a tree a fixture built
# runs the real code. An arm that is ABOUT the host's reading sets a value of
# its own and restores this one (tests/test_seat_pressure_hermetic.py).
os.environ["HELM_SEAT_PRESSURE"] = "off"
PLANTED["HELM_SEAT_PRESSURE"] = os.environ["HELM_SEAT_PRESSURE"]

# NO TEST MAY READ THE OPERATOR'S MCP CREDENTIALS OR LINK HIS GLOBAL RULES.
#
# Every seat mint (`seat add`/`launch`/`resume`/`spawn`) and every `helm tidy`
# now plans the canonical MCP servers and the global-instructions link for a
# config dir (task/3089). Unset, the MCP resolution reads the operator's
# private ~/.config/helm/mcps-canonical.json — concrete server configs that
# carry an Authorization header — and would COPY them into every fixture seat's
# .claude.json; the instructions link would point every fixture seat at his
# real ~/.claude/CLAUDE.md. HELM_HOME moves neither path. `off` is each
# variable's production switch (envtidy.private_mcps_path,
# skillsync.instructions_canonical), not a test-only escape hatch, and it is
# set UNCONDITIONALLY so an inherited value cannot re-arm the read. The arms
# that are ABOUT these sources point them at fixtures and restore these values.
os.environ["HELM_MCPS_PRIVATE"] = "off"
PLANTED["HELM_MCPS_PRIVATE"] = os.environ["HELM_MCPS_PRIVATE"]
os.environ["HELM_INSTRUCTIONS_CANONICAL"] = "off"
PLANTED["HELM_INSTRUCTIONS_CANONICAL"] = os.environ["HELM_INSTRUCTIONS_CANONICAL"]

# THE TRIPWIRE ON THAT DEFAULT. An env default is gone inside
# `mock.patch.dict(os.environ, clear=True)` or after a test pops the key, and
# then the walk runs again with nothing to say so. seatceiling raises
# HOST_READ_EVENT through sys.audit just before any reading of the host's
# tree, which it does only when the switch reads on; this hook refuses that
# event unless a test SET the switch to a value that is not off. Unset or
# empty is the lost default, never a choice, and an `off` that reached the
# event means the switch itself stopped working. BaseException, like
# SocketPathRefused, because the report's own
# `except Exception` would otherwise fold the refusal into an empty memory cell.
# Must equal seatceiling.HOST_READ_EVENT; tests/test_seat_pressure_hermetic.py
# pins it (this module may not import helm).
HOST_READ_EVENT = "helm.seatceiling.host_read"


class HostPressureRefused(BaseException):
    """A test process reached the host's seat-memory reading without asking."""


def _refuse_unasked_host_pressure(event, args):
    if event != HOST_READ_EVENT:
        return
    asked = (os.environ.get("HELM_SEAT_PRESSURE") or "").strip().lower()
    if asked and asked not in ("0", "off", "no"):
        return
    raise HostPressureRefused(
        "seatceiling.fleet_pressure read this box's seat memory (root=%r, "
        "proc=%r) in a test process that did not ask, refused by "
        "tests/__init__.py: HELM_SEAT_PRESSURE is %r, so the suite's `off` was "
        "lost (a cleared or popped environment) or no longer switches the "
        "reading off. Pass the reading in (roster_report(pressure=...)) or "
        "point it at a fixture tree; an arm about the host's own reading sets "
        "HELM_SEAT_PRESSURE=on itself."
        % (args[0], args[1], os.environ.get("HELM_SEAT_PRESSURE")))


sys.addaudithook(_refuse_unasked_host_pressure)

# ONE THREAD AT A TIME IN os.get_exec_path (task/3398). subprocess calls it for
# every bare program name, and it reads PATH inside a warnings.catch_warnings()
# window that is not thread-safe: two threads that leave theirs out of order
# leave warnings.filters bound to one thread's copy, and the gate's leak audit
# fails whichever unit started them ("warnings changed"). tests/_warnstate has
# the race, the census of the suite and the cure (stdlib only, no helm import);
# tests/test_warnings_isolation proves it.
from tests import _warnstate
_warnstate.serialize_exec_path()

# The literal historical unittest CLI imports this package during discovery,
# before its root TestProgram calls runTests. The opt-in arm wraps only that
# program instance; ordinary suite imports and nested runners are untouched.
if os.environ.get("HELM_GATE_RECORD_ROLE") == "serial":
    try:
        from helm import gatetestrecord
        gatetestrecord.arm_serial_from_env()
    except Exception:
        pass
