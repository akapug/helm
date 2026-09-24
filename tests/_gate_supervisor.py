#!/usr/bin/env python3
"""Can the gate guard's cgroup supervisor RUN here? A read-only answer.

WHY THIS EXISTS. An arm that mints a REAL gate receipt spawns the gate guard,
and the guard refuses to start unless it can create its containment cgroup
(`helm.gatechild._create_cgroup`). `fab gate` runs the suite in a scope that
grants that; `fab test` does not. At trunk, one focused round
(`fab test --repo . -- python3 -m unittest tests.test_gate tests.test_gate_focus`)
produced 153 red arms, every one of them the same sentence -- "gate guard
exited before reporting its supervisor ... lacks write access" -- and none of
them about the code under cure. A check that CANNOT RUN was indistinguishable
from a check that RAN AND FAILED, so a seat curing a bug in either module read
153 failures that said nothing about the change and could not tell their own
breakage from the background.

THE PROBE ASKS THE MODULE'S OWN QUESTION. `_cgroup_root()` is imported, not
reimplemented: the hard part (reading /proc/self/cgroup, resolving it under
/sys/fs/cgroup, refusing an escape) stays owned by one function, so a change
there moves this probe with it. Only the three cheap questions
`_create_cgroup` asks after that are repeated here, in its order.

IT NEVER CALLS `_create_cgroup` ITSELF. That function MAKES a directory, and a
probe with a side effect is not a probe: it would leave a cgroup behind on
every collection of the test that consulted it.

THE SKIP IS NARROW ON PURPOSE. It fires ONLY on a precondition positively
measured as ABSENT -- the root is unreadable, missing, or not writable/
searchable by this process. Any OTHER guard failure (a malformed position
token, a cgroup that appears without its interface files, a child that dies
for its own reasons) must still be a FAILURE, because a blanket "the gate did
not work, skip it" would hide exactly the regressions these arms exist to
catch. That is a worse bug than the one this file cures, so when in doubt this
probe returns None and the arm fails.
"""
import os
import unittest

from helm import gatechild


SKIP = ("gate guard's cgroup supervisor is unavailable here (%s); these arms "
        "mint real receipts -- run them with `fab gate`")


def unavailable_reason():
    """The measured reason a real mint cannot run here, or None if it can.

    READ-ONLY: it opens /proc/self/cgroup (through `_cgroup_root`) and asks
    `os.path.exists` / `os.access`. It creates nothing and removes nothing.
    """
    root = gatechild._cgroup_root()
    if root is None:
        return "this process's cgroup is unreadable"
    if not os.path.exists(root):
        return "cgroup root %s is missing" % root
    # THE SAME TWO QUESTIONS `_create_cgroup` ASKS, in its order and with its
    # words, so the skip reason and the guard's own refusal read alike.
    missing = [name for name, mode in (("write", os.W_OK),
                                       ("search", os.X_OK))
               if not os.access(root, mode)]
    if missing:
        return "cgroup root %s lacks %s access" % (root, "/".join(missing))
    return None


def require_supervisor():
    """Skip the CALLING ARM when a real receipt cannot be minted here."""
    reason = unavailable_reason()
    if reason:
        raise unittest.SkipTest(SKIP % reason)
