#!/usr/bin/env python3
"""Suite-wide env the CANONICAL RUNNER actually loads.

WHY HERE AND NOT conftest.py. This repo's tests are `unittest`, not pytest
(CONTRIBUTING.md, AGENTS.md, and `.github/workflows/ci.yml`, which runs
`python -m unittest discover`). unittest NEVER imports a conftest.py, so
anything planted there is a no-op in CI and in the canonical local run — the
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
   because test_envtidy pulls in helm.configs first. Alone it passes.
   Measured; it also moved the full-suite failure set around as unrelated
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
   would have DELETED OUT FROM UNDER RUNNING SEATS. And in one incident main
   went RED WITH NO COMMIT when ordinary fleet activity reaped a worktree a
   tracked test was reading — 4886 green forty minutes earlier on the same
   files, every land gate failing after, and the next seat to run one
   inheriting a failure it did not cause.

4. THE METAHARNESS QUERY REACHED THE OWNER'S REAL WORKSPACE. `helm work gc`
   asks the metaharness whether a pane is bound to a room before deleting one
   (helm/harness.py:worktree_panes — the killed-back-to-a-CWD
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
import os
import shutil
import tempfile

_TESTROOT = None


def _testroot():
    """The ONE temp tree for this process, created only when we actually plant.

    Lazy on purpose — tests/_tmphome.py's inode-exhaustion lesson: an eager
    `tempfile.mkdtemp()` whose value setdefault then discards orphans a
    directory on every run, and /tmp died of 1,048,575-of-1,048,576 inodes
    used with 29G of space still free.
    """
    global _TESTROOT
    if _TESTROOT is None:
        _TESTROOT = tempfile.mkdtemp(prefix="helm-suite-env-")
        atexit.register(shutil.rmtree, _TESTROOT, ignore_errors=True)
    return _TESTROOT


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

    THE LEGACY NAMESPACE MUST STILL WIN. Some of these vars have an older
    MELD_ companion helm still honors, and planting the HELM_ name masks it:
    home.env prefers HELM_ over MELD_. An operator who
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
    inherited = os.environ.get(legacy) if legacy else None
    if inherited:
        os.environ.pop(var, None)
        return inherited
    path = os.path.join(_testroot(), sub)
    os.makedirs(path, exist_ok=True)
    os.environ[var] = path
    return path


# The two that freeze inside helm.configs at import, and the one every chat /
# seats / claims / cursor path derives from.
PLANTED = {
    "HELM_CONFIG_ROOTS": _plant("HELM_CONFIG_ROOTS", "cwd-roots"),
    "HELM_HOME": _plant("HELM_HOME", "helm-home", "MELD_HOME"),
    "HELM_CHAT_DIR": _plant("HELM_CHAT_DIR", "chat", "MELD_CHAT_DIR"),
}

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
