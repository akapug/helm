"""helm procid — WHAT a pid is, asked of the KERNEL rather than of the process.

THE ONE PREDICATE. `comm` and `argv[0]` are both answers the JUDGED PROCESS
controls: comm is settable by anything that calls prctl(PR_SET_NAME) (node's
`process.title` does exactly that), and argv[0] is free-form — orcaadopt's own
note has said so for months, "`/tmp/bash-claude` ends in claude". `/proc/<pid>/exe`
is the kernel's link to the mapped binary and no process can forge its own.

THIS RUNG IS WIDER. IT IS NOT A HARDENING, AND SAYING SO HERE WAS THE ERROR
THIS PARAGRAPH REPLACES. `is_claude` ORs its rungs, so anything that sets its
own comm to "claude" is accepted exactly as it was before this module existed —
including the hostile-`sleep` of the repro in `sessions._pid_is_claude`, which
this text previously claimed to defeat. Every clause of that claim was true and
the sentence was materially false, because it omitted the OR. The exe rung ADDS
an accept path for panes helm was WRONGLY REJECTING and REMOVES none: the spoof
surface is UNCHANGED, neither widened nor narrowed. Requiring exe INSTEAD of
comm would close that hole and would also reject every install layout nobody
has enumerated — a separate change with its own risk, not a silent rider on a
false-DEAD fix. `test_the_comm_ACCEPT_PATH_IS_UNCHANGED_and_so_is_its_spoof_surface`
pins it, and the docstring must not disagree with the test again.

WHY IT WAS NEEDED, measured 2026-08-06 on two LIVE claude panes on one box:

    comm=2.1.223  argv0=~/.local/share/claude/versions/2.1.223
    comm=claude   argv0=claude

Both are claude. `comm` is the EXEC'D BINARY'S BASENAME, so a pane that execs
the versioned binary DIRECTLY is named for the SEMVER. Every identity gate in
helm demanded the literal string "claude", so every one of them rejected the
first pane: `sessions._pid_is_claude` was False for it, `live_sids()` held 15
sessions and not that pane's, and `seat where` therefore reported a seat that
was mid-turn landing commits as "orca-adopted — DEAD", naming as its evidence
"no live claude process names this seat". The fleet was told to route around it.

AND THE BLINDNESS DID NOT SURFACE AS BLINDNESS. `claude_processes()` returned 14
rows and 0 unreadable — confident, not honest-unknown — because a pid that fails
the IDENTITY filter is SKIPPED before it can ever reach the `unreadable` bucket
that `cannot_look()` exists to guard. The safety net is downstream of this hole,
which is why the hole has to be closed here rather than described louder there.

`exe` is identical across both launch shapes (both resolve under
`.../claude/versions/<semver>`), so it is the join that spans them.
"""
import os
import re

# The versioned-install layout: `<anything>/claude/versions/<semver>`. Matched
# on SHAPE, never on this host's paths — an installed-elsewhere claude is the
# same claude (no-hardcoded-context-identity).
_VERSIONED_EXE = re.compile(r"/claude/versions/[^/]+/?$")

# An upgraded-in-place binary reads back as "<path> (deleted)". This is not an
# edge case to tolerate, it is the COMMON case for the panes that matter most:
# an auto-update unlinks the running version, and the longest-lived panes are
# both the likeliest to still hold a session and the likeliest to have been
# upgraded out from under. Stripping the suffix is what keeps them identifiable.
_DELETED = " (deleted)"

# comm is the exec'd binary's basename truncated to 15 chars, so a versioned
# launch is named for the SEMVER ("2.1.223"). This shape is what makes an
# UNREADABLE exe worth reporting as UNKNOWN rather than as "not claude" — see
# is_claude, and see the measurement in its docstring for why the distinction
# has to be this narrow.
_VERSION_COMM = re.compile(r"^\d+(\.\d+)+")


def proc_root(proc=None):
    """helm's standing proc-root indirection, same spelling as everywhere else."""
    return (proc or os.environ.get("HELM_PROC")
            or os.environ.get("MELD_PROC") or "/proc")


def exe_of(pid, proc=None):
    """The kernel's path for this pid's mapped binary, or None if it cannot be read.

    None is UNREADABLE, never "not claude" — the callers here treat it as one
    rung declining to answer, never as a rejection.
    """
    try:
        link = os.readlink(os.path.join(proc_root(proc), str(pid), "exe"))
    except OSError:
        return None
    return link[:-len(_DELETED)] if link.endswith(_DELETED) else link


def exe_is_claude(pid, proc=None):
    """True / False / None — AND NONE IS NOT FALSE.

    Accepts both shapes helm has actually observed: a binary literally named
    `claude` (wrapper/shim launches, comm agrees) and the versioned-install
    layout `.../claude/versions/<semver>` (direct-exec launches, comm carries
    the semver and disagrees). The second is the one every other gate missed.

    None means the link could not be READ, which is a fact about our PERMISSION
    and not about the process. readlink /proc/<pid>/exe requires
    PTRACE_MODE_READ where comm does not: measured 2026-08-06 on this box, 870
    pids had a readable comm and 450 of them refused the exe read with errno 13.
    Returning False for those would be THIS MODULE'S OWN DIAGNOSIS COMMITTED
    AGAIN — the docstring above says a pid that fails identity is SKIPPED before
    it can reach the unreadable bucket, and a predicate with no third state
    cannot let it reach one.
    """
    exe = exe_of(pid, proc)
    if exe is None:
        return None
    if not exe:
        return False
    return os.path.basename(exe) == "claude" or bool(_VERSIONED_EXE.search(exe))


def comm_is_claude(raw):
    """The original rung, unchanged, kept as a NAMED thing rather than a literal.

    `raw` is the bytes of /proc/<pid>/comm (or str). A caller that could not
    read comm passes None and gets False — which is why no caller may read this
    False as "not claude" on its own.
    """
    if raw is None:
        return False
    if isinstance(raw, bytes):
        return raw.strip() == b"claude"
    return raw.strip() == "claude"


def is_claude(pid, comm_raw=None, proc=None):
    """True / False / None. TRUE identifies, FALSE refutes, NONE cannot tell.

    Deliberately OR, and deliberately in this order: comm is one cheap read and
    answers for the overwhelming majority of panes, so exe is only paid for when
    comm has already declined.

    THE UNKNOWN IS DELIBERATELY NARROW, and the narrowness is the whole design.
    An unreadable exe alone must NOT mean UNKNOWN: 450 of the 870 readable-comm
    pids on this box refuse the exe read, and reporting all of them as
    unidentifiable would flood `unreadable` until `cannot_look()` made EVERY
    census consumer answer UNKNOWN — trading a census that is confidently wrong
    about two panes for one that cannot answer about anything. MEASURED both
    ways 2026-08-06: 450 blind under the wide rule, 0 under this one.

    So UNKNOWN requires BOTH that exe could not be read AND that comm is shaped
    like a versioned launch (a leading dotted-numeric run) — i.e. this pid looks
    like exactly the pane the wide rung exists to rescue, and we were not
    permitted to confirm it. Anything else with a readable non-claude comm is a
    genuine FALSE: `systemd` is not an unidentifiable claude.

    A caller that turns FALSE into a DEAD verdict is still adding a claim this
    function never made; NONE it must never turn into either answer.
    """
    if comm_is_claude(comm_raw):
        return True
    seen = exe_is_claude(pid, proc)
    if seen is not None:
        return seen
    return None if _comm_looks_versioned(comm_raw) else False


def _comm_looks_versioned(raw):
    """True when comm is shaped like the semver a versioned launch is named for."""
    if raw is None:
        return False
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return bool(_VERSION_COMM.match(text.strip()))
