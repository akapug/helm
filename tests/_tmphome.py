#!/usr/bin/env python3
"""ONE per-process temp HELM_HOME, created lazily and removed at exit.

The class this closes (/tmp hitting 100% INODE exhaustion —
1,048,575 of 1,048,576 used with 29G of space still free, so every space-based
check read healthy while every agent's tool calls failed ENOSPC):

    os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix=...))

leaks a directory per module per run, TWICE over:
  1. Python evaluates arguments EAGERLY, so mkdtemp() runs and creates the
     directory even when setdefault discards its value because HELM_HOME is
     already set. 31 test modules did this; the first one wins and the other
     30 each orphan a fresh directory on every single run.
  2. Nothing ever removed them — module-scope has no tearDown.

Use `home()` instead: it only creates when the var is genuinely unset, and it
registers the removal with atexit so a normal process exit takes the directory
with it.
"""
import atexit
import os
import shutil
import tempfile


def home(prefix="helm-test-home-", var="HELM_HOME"):
    """The process's temp home for `var`, created ONCE and cleaned at exit.
    Returns the existing value untouched when the caller (or a parent
    harness) already set one — without creating a directory to throw away."""
    current = os.environ.get(var)
    if current:
        return current
    d = tempfile.mkdtemp(prefix=prefix)
    os.environ[var] = d
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d


# ORDER-PROOFING THE FREEZE-AT-IMPORT MODULES (helm.configs computes its
# CWD_ROOTS when first imported): tests/conftest.py plants HELM_CONFIG_ROOTS
# before any import, but ONLY pytest loads conftest — under plain unittest
# discovery the guarantee vanishes, and whichever module first drags in
# helm.configs (helm.hooks does, transitively) freezes the roots on the REAL
# estate. test_configs then plants fixtures nowhere the frozen roots look and
# fails, but only in orders that include such a module — measured 2026-07-28
# when a new test file imported helm.hooks and three configs tests failed
# under unittest while pytest stayed green. Every test module imports THIS
# module before any helm.*, so planting the same guarantee here makes the
# freeze land on a tmp root no matter who imports what first, under either
# runner. conftest still wins when it ran first: home() honours an existing
# value untouched.
home(prefix="helm-test-cfgroots-", var="HELM_CONFIG_ROOTS")

# EXPLICIT ROOM FOR EVERY TEST PROCESS: chat.post's room default now DERIVES
# (env seam, then cwd project) instead of hardcoding "main" — the 2026-07-29
# room-partition class fix. A test process's cwd is the REPO, so a bare
# default-relying post would derive the repo's room and 60 hermetic tests
# asserting #main would fail for a reason unrelated to what they test. Tests
# therefore declare their room EXPLICITLY through the same env seam every
# launched seat uses (launch.sh sets HELM_CHAT_ROOM) — the old behavior, now
# stated instead of accidental. Tests OF the derivation itself mock
# _default_post_room and are untouched by this.
os.environ.setdefault("HELM_CHAT_ROOM", "main")
