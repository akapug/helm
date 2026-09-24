#!/usr/bin/env python3
"""helm gate — a suite result becomes a CLAIM only by being MINTED here.

WHY THIS EXISTS. Until this module a gate verdict entered helm as PROSE. You
typed `helm dispatch verdict <id> <tip> "whole-suite Ran 5115 OK"` and the only
thing any code did to that string was check its LENGTH (dispatches._clean, 256
characters). An integrator reading it could not learn which interpreter
produced it, which tree it ran against, or whether it was run at all.

That is not hypothetical. Two seats have been measured reporting
HONEST, CONTRADICTORY results on the same tree twenty minutes apart, because
`python` here is GraalPy 3.12.8 and `python3` is CPython 3.14.4, and one HANGS
forever where the other returns OK (premise
a-suite-result-is-not-a-claim-without-its-interpreter). Neither side was flaky
and neither was lying. The protocol delta — state your interpreter — is
advisory, not enforced: a scan for
sys.implementation / python_implementation / CPython / GraalPy across helm/ and
docs/ returned zero hits before this file.

WHY NOT JUST VALIDATE THE STRING. The obvious fix is to make the verdict verb
require an evidence line MATCHING a pattern that names an interpreter. That is
a check on SPELLING. It passes for anyone who types the token without running
anything, which is precisely the failure a review of the protocol named as
helm's single finding — a guard sound about a narrower neighbour of the
question being asked. A consumer census taken the same way
proved the point by matching substrings and passing every consumer that did not
hold the rule. So the evidence is not validated here. It is PRODUCED here, and
a verdict either carries a token that resolves to a real run or is recorded
UNVERIFIED in the open.

HOW THE INTERPRETER IS BOUND. Not probed, not parsed, not passed in: the
process that MINTS the receipt is the process that SPAWNS the suite, using its
own sys.executable. There is no window in which the recorded interpreter and
the running one can differ, because they are the same binary by construction.
To gate under the other interpreter you invoke helm under it —
`python3 -m helm gate run` versus `python -m helm gate run` — which makes the
choice explicit at the only place it was ever ambiguous.

A CUSTOM COMMAND (`helm gate run -- <argv>`) cannot offer that guarantee: helm
did not choose its interpreter and cannot see inside a shell script. Such a
receipt records interpreter UNKNOWN and is REFUSED as a binding for a verdict.
An honest refusal, never a pass whose input was missing.
"""
import binascii
import collections
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import chat, eventledger, gateauthority, gatechild, gatetestrecord, home, pk, scratch, seats, vcs

RECEIPTS = "gate-receipts.jsonl"
# THE SIDECAR BESIDE THE RECEIPT. A declared `exit`-protocol command's whole
# stdout+stderr is written here, in the SAME directory as the receipt ledger,
# so whatever fetches the ledger off a node (fab's artifact pull) can take the
# output by basename from the same place. The receipt carries only a bounded
# tail; this file is the whole text. Overwritten by every declared run.
COMMAND_OUTPUT = "gate-command-output.txt"
DECLARED_OUTPUT_SOURCE = "declared-command-output"

# The whole tree, as unittest discovers it in one process. `-t .` keeps the
# import root at the repo so `helm.*` resolves the checkout under test rather
# than an installed copy. This literal serial invocation is the sole landing
# authority; gateshard is diagnostic because fresh workers cannot reproduce
# arbitrary discovery-time process state.
SUITE = ("-m", "unittest", "discover", "-s", "tests", "-t", ".")

# THE PROJECT'S COMMAND, NOT HELM'S. `SUITE` above is HELM's suite, and for
# years it was every project's: `run()` read the constant with no question
# about which repo it had been handed, so `helm gate run --repo <an adopter's
# project>` spawned python unittest discovery in a tree with no `tests/`
# directory, read no unittest footer, and minted an UNKNOWN receipt about a
# command the project never asked for — an unbindable receipt rather than a
# refusal. helm was always meant to help agent teams build ANY project, and a
# team USING helm must never need to know how helm is made: wherever a verb
# assumes the helm checkout or helm's suite, that is the bug. So a registered
# project DECLARES the command its receipts bind, as the authored registry
# field below (`registry.AUTHORED_FIELDS`), and `suite_command` is the one
# place helm learns it.
DECLARED_GATE_FIELD = "gate"
# HOW THE RUN'S OWN VERDICT IS READ. `unittest` is the grammar `parse_result`
# has always read off stderr. `exit` is the command's exit status and NOTHING
# ELSE — deliberately not the child's prose, because a command that PRINTS
# `Ran 9 tests ... OK` must not be able to claim a count helm never counted.
PROTOCOL_UNITTEST = "unittest"
PROTOCOL_EXIT = "exit"
PROTOCOLS = (PROTOCOL_EXIT, PROTOCOL_UNITTEST)

_TOKEN = re.compile(r"gate:([0-9a-f]{4,32})")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ID = re.compile(r"[0-9a-f]{4,32}\Z")

# unittest's own summary lines, read off the REAL captured output. Nothing here
# reconstructs a count from the runner's exit status: a suite that dies after
# printing "Ran 300" has a truthful 300 and an untruthful OK, and only parsing
# both fields separately can say so.
_RAN = re.compile(r"^Ran (\d+) tests? in (\d+(?:\.\d+)?)s[ \t]*$", re.M)
_OK = re.compile(r"^OK(?: \((.*)\))?[ \t]*$", re.M)
_FAILED = re.compile(r"^FAILED \((.*)\)[ \t]*$", re.M)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Authentic unittest blocks are bracketed by its 70-column separators.
# TextTestResult may append subtest detail on the header and one shortDescription
# line before the dashed separator; neither changes the canonical identity.
_FAILURE_BLOCK = re.compile(
    r"^={70}\n(?P<kind>FAIL|ERROR): (?P<header>[^\n]+)\n"
    r"(?:(?P<description>[^\n]*)\n)?^-{70}\n", re.M)
_FAILURE_HEADER = re.compile(
    r"^(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*) "
    r"\((?P<test>(?:[A-Za-z_]\w*|<locals>)"
    r"(?:\.(?:[A-Za-z_]\w*|<locals>))*)\)[^\n]*$")
_DOCTEST_HEADER = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)* \(\)$")
_FIXTURE_NAMES = frozenset(("setUpClass", "tearDownClass",
                            "setUpModule", "tearDownModule"))
FAILURE_CAP = 20
_FAILURE_TEXT_CAP = 500
_FAILURE_CHUNK_EVENT = "gate-failure-identities"
_FAILURE_CHUNK_VERSION = 1
_FAILURE_CHUNK_ID_LEN = 32
_TIMING_EVENT = "gate-module-timings"
_TIMING_VERSION = 1
_TIMING_ID_LEN = 32
# unittest renders runner elapsed to milliseconds while module sums retain six
# decimals. Half a millisecond covers footer rounding; one more microsecond
# keeps the decimal/float boundary itself admissible.
_TIMING_RUNNER_TOLERANCE = 0.000501
# Sum-of-rounded rows versus rounded raw sum can differ by ceil(n / 2)
# whole microseconds at correlated half-even ties. For one row both sides round
# the exact same raw value, so its allowance is exactly zero.
_TIMING_ROW_UNIT = 0.000001
_NO_TIMING = object()
# THE LADDER AND THE KINDS ARE TWO DIFFERENT THINGS, and conflating them is
# what this block exists to prevent.
#
# 1-4, 8 and 9 are LADDER rungs: each inherits earlier fields, so a
# reader asks "does this version HAVE the field" and a bump can never silently
# stop binding something. 5, 6 and 7 are KINDS that happened to take the next
# free integer, and they inherit nothing from each other:
#
#   5  cached-receipt kind    — an `executed` key, its own structured id payload
#   6  focused kind          — a `focus` scope block; readable here, but the
#                              GENERIC import door refuses it on purpose (a
#                              plain artifact carries no independent run
#                              origin), so only gateroute's challenge-framed
#                              custody admits one. See gateimport.
#   7  WITHDRAWN             — the sharded script-runner receipt, withdrawn on
#                              trunk 2026-08-25 (a893712b). It never mints,
#                              never imports and never binds, and the integer
#                              STAYS BURNED so a row minted by an unlanded
#                              sharded helm can still be named as withdrawn
#                              rather than misread as a newer kind.
#
# THE BOUNDED FAILURE RECORD IS 8, NOT 7. This lane was authored against a
# 2026-08-11 trunk where 7 was free, and took it. Trunk took 7 for the
# withdrawal two weeks later. Two grammars under one version int make every
# reader call the other kind's honest rows tampered — which is exactly the
# reasoning that already sent the focused kind to 6 rather than to 5 — so the
# lane's new mintable kind moves to the next free rung instead. No ledger row
# is affected: the live ledger carries versions 1-6 only, and zero v7 rows.
MINTED_RECEIPT_VERSIONS = (4, 8)
HISTORICAL_SERIAL_VERSIONS = (4, 8)
CACHED_VERSION = 5
FOCUSED_VERSION = 6
WITHDRAWN_SHARD_VERSION = 7
# Reader-first checkpoint: v9 is readable but no execution path mints it yet.
# Its strict derived evidence is independent of the burned v7 receipt kind.
SHARDED_AUTHORITY_VERSION = gateauthority.RECEIPT_VERSION

# One extensible registry owns every receipt field introduced after v1. A
# future LADDER version inherits every earlier field by construction: adding
# v9 to a reader cannot silently drop v8's failure record the way
# hand-maintained ``in (4, 7)`` tuples did. ``keys`` are the version-specific
# import schema; the hash and display readers ask ``_version_has`` by the same
# field name.
RECEIPT_FIELD_REGISTRY = (
    ("post_run_bracket", 2, frozenset()),
    ("failure_identities", 2, frozenset()),
    ("base_check", 3, frozenset()),
    ("host", 4, frozenset(("host",))),
    ("failure_record", 8, frozenset((
        "failure_total", "failure_diagnostics_omitted", "failure_chunks"))),
    ("sharded_authority", 9, frozenset(("sharded_authority",))),
)

# A KIND's own keys, which no other version inherits. Cumulative inheritance
# cannot express these: `executed` belongs to 5 alone and `focus` to 6 alone,
# and letting either ride the ladder would require them of v8 — a receipt that
# is neither cached nor focused. Both entries sit beside the ladder rather
# than in it so ONE function still answers "what keys does this version
# carry", which is the property that let the narrowed reader be caught.
#
# A DECLARED COMMAND IS NOT A VERSION, and that is the decision this comment
# records. The project-declared command is carried by a `suite_command` block
# that any version may hold and that `_receipt_id` binds BY PRESENCE, so a row
# without one hashes byte-identically to what it always did and a row with one
# cannot have it edited. A new integer was the first design and it was wrong:
# every free integer sits above 8 and 9, so the ladder would have REQUIRED v8's
# bounded failure record (unittest's overflow grammar) and v9's sharded
# authority evidence of a receipt that can carry neither.
RECEIPT_KIND_KEYS = {CACHED_VERSION: frozenset(("executed",)),
                     FOCUSED_VERSION: frozenset(("focus",))}

# Every version any READER here can evaluate — wider than the generic import
# door, which additionally closes on the focused kind. 7 is absent by
# construction: withdrawn is not readable, and `by_id`/`row_refusal` name it
# as withdrawn rather than as a version from a newer helm.
RECEIPT_VERSIONS = (1, 2, 3, 4, 5, 6, 8, 9)


def receipt_version_known(row_or_version):
    version = row_or_version.get("v") if isinstance(row_or_version, dict) \
        else row_or_version
    return type(version) is int and version in RECEIPT_VERSIONS


def _version_has(row_or_version, field):
    version = row_or_version.get("v") if isinstance(row_or_version, dict) \
        else row_or_version
    introduced = next((since for name, since, _keys in RECEIPT_FIELD_REGISTRY
                       if name == field), None)
    return type(version) is int and introduced is not None and version >= introduced


def receipt_version_keys(version):
    if type(version) is not int:
        return frozenset()
    return frozenset().union(
        RECEIPT_KIND_KEYS.get(version, frozenset()),
        *(keys for _name, since, keys in RECEIPT_FIELD_REGISTRY
          if version >= since))


def receipts_path():
    return os.path.join(home.global_dir(), RECEIPTS)


def command_output_path():
    return os.path.join(home.global_dir(), COMMAND_OUTPUT)


def _keep_command_output(text):
    """Write a declared command's whole output beside the receipt. -> (path, err).

    A failed write never fails the mint: the verdict is the exit status and
    stands without its diagnostic, so the receipt's `detail` names the failure
    instead of the path and the reader knows to look at the console.
    """
    path = command_output_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
    except OSError as exc:
        return None, "%s: %s" % (path, exc)
    return path, None


def interpreter():
    """The MINTING process's own identity — never an argument, never a probe.

    sys.implementation.name is the field that separates the two pythons on this
    box; sys.version alone does not (GraalPy reports a 3.12.8 language level
    that reads like any CPython 3.12)."""
    impl = sys.implementation
    ver = ".".join(str(n) for n in impl.version[:3])
    return {"name": impl.name,
            "version": ver,
            "language": "%d.%d.%d" % sys.version_info[:3],
            "executable": os.path.realpath(sys.executable or "")}


def interpreter_label(ident):
    """The short human form: `CPython-3.14.4`, `graalpy-24.1.0`."""
    if not isinstance(ident, dict):
        return "UNKNOWN"
    raw_name = _failure_text(ident.get("name"))
    if not raw_name:
        return "UNKNOWN"
    name = "CPython" if raw_name == "cpython" else raw_name
    version = _failure_text(ident.get("language") or ident.get("version")) or "?"
    return "%s-%s" % (name, version)


# Absent on a box that has neither: the host is then named by hostname alone
# and `id` is empty, which the label says out loud rather than inventing one.
_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
_LABEL_CAP = 24


def host():
    """The MINTING process's own machine — never an argument, never a probe.

    Bound exactly the way `interpreter()` is, and for the same reason: the
    process that writes the receipt is the process that spawns the suite, so
    the recorded host and the running one are one machine BY CONSTRUCTION. A
    `--host` flag would reintroduce the whole class in one line.

    `id` is a HASH of the machine id, never the machine id itself. An evidence
    line TRAVELS — into commit messages, into chat rooms, onto a public remote
    — and a raw /etc/machine-id there is a host fingerprint nobody chose to
    publish (the same shape `hostpath_guard` refuses for filesystem paths). The
    hash still answers the only question a reader actually has, "is this the
    same box as that other receipt?", while naming nothing.
    """
    uname = platform.uname()
    raw = ""
    for path in _MACHINE_ID_PATHS:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw:
            break
    return {"node": _failure_text(uname.node),
            "system": _failure_text(uname.system),
            "release": _failure_text(uname.release),
            "id": hashlib.sha256(
                raw.encode("utf-8", "surrogatepass")).hexdigest()[:16]
            if raw else ""}



def host_label(ident):
    """The short human form: `build-2`. UNKNOWN when the receipt does not name
    its machine — absence is SAID, never rendered as blank space a reader
    fills in with the box they happen to be sitting at."""
    if not isinstance(ident, dict):
        return "UNKNOWN"
    return _failure_text(ident.get("node"))[:_LABEL_CAP] or "UNKNOWN"


def _suite_env(capacity=None):
    """Deterministic unittest rendering, with this Helm as the runner owner."""
    capacity = suite_capacity() if capacity is None else capacity
    env = dict(os.environ)
    env["PYTHON_COLORS"] = "0"
    env["NO_COLOR"] = "1"
    env["HELM_GATE_SUITE_CAP"] = str(max(1, capacity["cap"]))
    env.pop("FORCE_COLOR", None)
    return env


def _tree_at(repo, sha):
    """The tree sha `sha` names in `repo`, or "" — never raises, never refuses.

    Deliberately total. Its only caller uses it to UPGRADE a verdict to
    VERIFIED, so every failure mode must fall through to the logic that ran
    before it rather than mint a new refusal of its own."""
    try:
        rc, out, _err = vcs.backend(repo).text(
            repo, "rev-parse", "%s^{tree}" % sha)
    except Exception:                       # noqa: BLE001 — advisory only
        return ""
    out = (out or "").strip()
    return out if rc == 0 and _SHA.fullmatch(out) else ""


def tree_state(repo):
    """(head, tree, dirty, err) for the worktree the suite will run in.

    Through helm/vcs.py, not a private git spawn. The first draft of this
    module grew its own `_git` helper — the seventh spelling of one — and
    DirectSpawnAuditTest failed the land for it. That audit is right twice
    over here: a module whose whole purpose is refusing unverifiable claims
    has no business bypassing the seam that makes git calls accountable.

    DIRTY IS PART OF THE CLAIM, not a warning. A green suite on a tree with
    uncommitted edits proves something about a tree nobody else can check out,
    so it can name a commit but never bind one. vcs.dirty() reads an
    UNREADABLE checkout as dirty, which is the direction this verb needs: an
    unreadable worktree must never mint a bindable receipt."""
    git = vcs.backend(repo)
    head = git.head_sha(repo)
    if not head:
        return None, None, None, "cannot read HEAD in %s" % repo
    rc, out, err = git.text(repo, "rev-parse", "HEAD^{tree}")
    if rc != 0 or not out.strip():
        return None, None, None, "cannot read HEAD^{tree}: %s" \
            % ((err or "").strip() or "git exited %s" % rc)
    return head.strip().lower(), out.strip().lower(), git.dirty(repo), None


def _failure_text(value, cap=_FAILURE_TEXT_CAP):
    """One bounded printable line; truncation is visible, never silent.

    `cap` IS THE BOUND OF WHOEVER WROTE THE TEXT. A timing census reason is
    already bounded by the census (`gatetestrecord.TIMING_REASON_CAP`), and it
    names its gaps in order, so a 500-character cut here removed its LATER
    gaps whole: one receipt's reason lost a 418-module gap that way. The
    census path passes its own bound; every real failure keeps this default."""
    text = " ".join(str(value or "").split())
    if len(text) <= cap:
        return text
    return text[:cap - 3] + "..."


def _has_failure_text(value):
    """Would `_failure_text` return anything? Answered without building it.

    THE PREDICATE IS NOT `bool(value)`. A value of three spaces is truthy and
    yet carries no printable line, so the test is whether the value holds a
    non-whitespace character -- exactly when the split-and-join in
    `_failure_text` has something to join. Kept beside it so the two stay one
    definition: a caller that only asks IS THERE TEXT pays a strip instead of
    a normalized, capped copy, on a path a chunk walk enters per identity.
    """
    return bool(str(value or "").strip())


def _traceback_diagnostic(block):
    """One bounded diagnosis from a normal or ExceptionGroup traceback."""
    lines = str(block or "").splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"(?:Exception Group )?Traceback "
                         r"\(most recent call last\):$", line.strip()):
            continue
        useful = []
        for raw in lines[i + 1:]:
            stripped = raw.strip()
            if re.fullmatch(r"-{20,}", stripped) \
                    or _RAN.fullmatch(stripped) \
                    or _OK.fullmatch(stripped) \
                    or _FAILED.fullmatch(stripped):
                break
            candidate = stripped.lstrip("| ").strip()
            if candidate and any(ch.isalpha() for ch in candidate):
                useful.append(candidate)
        if not useful:
            return None
        text = " | ".join(useful)
        if len(text) <= _FAILURE_TEXT_CAP:
            return text
        terminal_index = next((j for j in range(len(useful) - 1, -1, -1)
                               if re.fullmatch(
                                   r"(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?::.*)?",
                                   useful[j])), len(useful) - 1)
        # THE TERMINAL SEGMENT RUNS TO THE END OF THE BLOCK, not to the
        # exception line. unittest emits an assertEqual's MESSAGE *after* the
        # exception and its diff:
        #     AssertionError: 'FAILED' != 'OK'
        #     - FAILED
        #     + OK
        #      : <the message>
        # Keeping only useful[terminal_index] therefore dropped every
        # caller-supplied message on the floor at any cap. Measured on a real
        # receipt: an arm attached the nested run's detail and failure list to
        # its message, this function returned 405 chars — UNDER the 500 cap, so
        # the cap never bound — and the only sentence that explained the
        # failure was the one discarded. What survived was the SOURCE of the
        # format string and never its rendered values.
        terminal_lines = useful[terminal_index:]
        terminal = " | ".join(terminal_lines)
        head_source = " | ".join(useful[:terminal_index])
        if not head_source:
            return _failure_text(terminal)
        marker = " | ... traceback truncated ... | "
        room = _FAILURE_TEXT_CAP - len(marker)
        # THE TAIL GETS THE BUDGET, because the head is recoverable and the
        # tail is not: a reader holding the receipt can open the named file and
        # read the frames, but the rendered message exists nowhere else. The
        # head keeps enough to name the failing frame and no more.
        head_room = min(len(head_source), max(120, room // 3))
        tail_room = room - head_room
        # WITHIN THE TERMINAL, BOTH ENDS MATTER AND THE MIDDLE DOES NOT.
        # A plain prefix of the terminal segment reproduces the very defect
        # this function is being cured of, one layer in: with a long
        # assertEqual diff the prefix keeps the exception and diff lines
        # 000..012 and pushes the rendered caller message — the only text that
        # names the cause — off the end again. So when the segment cannot fit,
        # keep its FIRST element (the exception, which says what kind of
        # failure this is) and its LAST (the rendered message, which says why),
        # and elide the diff between them.
        # THE MESSAGE IS A SUFFIX THAT BEGINS AT unittest's " : " SEPARATOR,
        # and it does NOT reliably begin a line. unittest renders
        # `standardMsg + " : " + msg`, so where that lands depends on the
        # assertion — measured against real TextTestRunner output:
        #   own line          " : nested detail follows"   (line-by-line diff)
        #   mid-line          "Diff is 801 characters long. ... : nested ..."
        #   exception line    "AssertionError: x != y : msg"  (generic asserts)
        # and every CONTINUATION line of a multiline message is plain prose.
        # A leading-colon test alone is therefore false for two of the three,
        # so a leading-colon test alone crowds out a cause that hides in a
        # continuation. Scan BACKWARDS for the last separator instead.
        #
        # STATED AMBIGUITY, not a hidden one: a diff VALUE containing " : "
        # is indistinguishable from the separator by this text protocol.
        # THE COST IS BOUNDED BUT NOT FREE, and understating it is easy: a
        # false hit replaces the useful EARLY diff with an arbitrary LATE
        # diff tail, so it is NOT equivalent to a prefix and can mislabel
        # noise as diagnosis — which cuts against the same no-tail-diff
        # intent the pinned contract protects. It is still the right trade:
        # the exception is retained either way, and losing a real caller
        # diagnosis is worse than surfacing the wrong slice of a diff.
        msg_at = None
        for j in range(len(terminal_lines) - 1, -1, -1):
            line = terminal_lines[j]
            # (line index, where the KIND ends, where the MESSAGE starts) —
            # two offsets, because the separator itself belongs to NEITHER. A
            # single offset made the kind include " : " while the joiner added
            # another, rendering " : : ".
            if line.startswith(":"):
                msg_at = (j, 0, 1)
                break
            k = line.find(" : ")
            if k != -1:
                msg_at = (j, k, k + 3)
                break
        if len(terminal) > tail_room and msg_at is not None:
            j, kind_end, off = msg_at
            # SPLIT THE LINE THE SEPARATOR IS ON — never charge a whole
            # unbounded line against the message's room. For a generic assert
            # the separator sits ON the exception line, so budgeting
            # `tail_room - len(terminal_lines[0])` goes NEGATIVE the moment the
            # message is long and skips the reconstruction entirely, leaving a
            # plain prefix and losing the tail. Measured: a 1200-char inline
            # message kept the exception and dropped the cause at its end.
            if j == 0:
                kind, joiner = terminal_lines[0][:kind_end].rstrip(), " : "
                rest = [terminal_lines[0][off:].strip()] + terminal_lines[1:]
            else:
                kind = terminal_lines[0]
                joiner = " | ... diff elided ... | : "
                rest = [terminal_lines[j][off:].strip()] + terminal_lines[j + 1:]
            # The KIND is bounded too: a long standardMsg must not eat the room
            # the cause needs.
            kind_cap = max(60, tail_room // 4)
            if len(kind) > kind_cap:
                kind = kind[:kind_cap - 3].rstrip() + "..."
            keep = tail_room - len(kind) - len(joiner)
            if keep > 0:
                msg = " | ".join(rest)
                if len(msg) > keep:
                    # BOUND FROM BOTH ENDS. The message's head names what it
                    # is and its tail carries the nested cause, so a plain
                    # prefix here would rebuild the same defect a third time.
                    half = max(20, (keep - 5) // 2)
                    msg = msg[:half].rstrip() + " ... " + msg[-half:].lstrip()
                terminal = kind + joiner + msg
        head = head_source[:head_room].rstrip()
        tail = terminal[:tail_room].rstrip()
        if len(head_source) > head_room:
            head = head[:-3].rstrip() + "..."
        if len(terminal) > tail_room:
            tail = tail[:-3].rstrip() + "..."
        return head + marker + tail
    return None


def _loader_error_diagnostic(block, test_id):
    """The exception line from a LOADER error, which HAS no traceback.

    unittest mints `unittest.loader._FailedTest` for an id it could not
    resolve, and the three shapes an explicit-id run can produce DO NOT AGREE
    about traceback. Measured under the gate's own interpreter, one reference
    run per shape, rather than reasoned about:

        absent METHOD on a resolved class  -> AttributeError, NO traceback
        absent CLASS on a resolved module  -> AttributeError, NO traceback
        absent MODULE                      -> ImportError, a real traceback

    _traceback_diagnostic requires the literal `Traceback (most recent call
    last):` header, so it answers None for the first two. That answer made
    `_parse_failures` call the block INCOMPLETE, which makes the whole RUN
    unreadable — and that is how a trunk reference run which named its absent
    test perfectly still reached base_check's "neither a pass nor a readable
    failure", the one sentence that leaves a red unattributed.

    NARROW ON PURPOSE. It is consulted only for a block whose identity IS a
    loader marker, and only after _traceback_diagnostic has already declined.
    A FAIL or ERROR from a test that actually RAN carries a traceback by
    construction, so one arriving without it is still a malformed block and
    still reads unreadable — this loosens the completeness rule for the one
    shape that can never satisfy it, and for nothing else.
    """
    if not _ABSENT_TEST.search(str(test_id or "")):
        return None
    useful = []
    for raw in str(block or "").splitlines():
        stripped = raw.strip()
        if re.fullmatch(r"-{20,}", stripped) or _RAN.fullmatch(stripped) \
                or _OK.fullmatch(stripped) or _FAILED.fullmatch(stripped):
            break
        if stripped and any(ch.isalpha() for ch in stripped):
            useful.append(stripped)
    return _failure_text(" | ".join(useful)) if useful else None


def _failure_counts(detail):
    counts = {"FAIL": 0, "ERROR": 0}
    seen = False
    for part in str(detail or "").split(","):
        hit = re.fullmatch(r"\s*(failures|errors)=(\d+)\s*", part)
        if not hit:
            continue
        counts["FAIL" if hit.group(1) == "failures" else "ERROR"] = \
            int(hit.group(2))
        seen = True
    return counts if seen else None


# unittest failure headers changed shape in 3.11; the sub-suite runs under
# sys.executable, so this process's version proves which shape it parses
_NEW_HEADER_FORMAT = sys.version_info >= (3, 11)


def _failure_identity(header):
    """Canonical id for a default-discovery unittest failure header."""
    hit = _FAILURE_HEADER.fullmatch(str(header or ""))
    if not hit:
        return None
    name, test_id = hit.group("name"), hit.group("test")
    if name.startswith("test") or name == "runTest":
        # 3.11+ prints (module.Class.method); older interpreters print
        # (module.Class) — canonicalize both to the dotted method id. The
        # NAME itself may be dotted (a dynamically minted test.with.dot), so
        # it is matched as a whole suffix, never split on dots. The format is
        # PROVEN, not guessed: the suite ran under sys.executable (recorded
        # in the receipt), so on an old interpreter the header NEVER carries
        # the method suffix and an endswith hit there would be a CLASS-name
        # collision truncating the id — concat is always right pre-3.11
        if _NEW_HEADER_FORMAT and test_id.endswith("." + name):
            return test_id
        return test_id + "." + name
    if name in _FIXTURE_NAMES:
        return test_id + "." + name
    if "._FailedTest." in test_id and test_id.endswith("." + name):
        return test_id
    if test_id.endswith("._FailedTest"):
        # <=3.10 prints the loader id bare; 3.11+ appends the failed name
        return test_id + "." + name
    return None


def _failure_protocol_like(header, description=None):
    """Could this separator-backed header be a unittest-owned test object?"""
    header = str(header or "")
    description = str(description or "")
    if _failure_identity(header) or _DOCTEST_HEADER.fullmatch(header) \
            or description.startswith("Doctest: "):
        return True
    hit = _FAILURE_HEADER.fullmatch(header)
    if not hit:
        return False
    name, test_id = hit.group("name"), hit.group("test")
    return ("." not in name and test_id.endswith("." + name)) \
        or name == "unittest.case.FunctionTestCase"


def _parse_failures(text, status, detail):
    """(complete entries, unreadable) from unittest's failure blocks.

    A FAILED summary is complete only when its failures/errors counts equal the
    headers we parsed and every header yields both an id and traceback diagnosis.
    UNKNOWN has no completeness witness, even if partial blocks are useful. OK
    is the sole state whose honest failure list is empty."""
    text = str(text or "")
    blocks = list(_FAILURE_BLOCK.finditer(text))
    protocol = [(i, hit, _failure_identity(hit.group("header")))
                for i, hit in enumerate(blocks)
                if _failure_protocol_like(hit.group("header"),
                                          hit.group("description"))]
    expected = _failure_counts(detail) if status == "FAILED" else None
    expected_total = sum(expected.values()) if expected else 0
    # unittest replays its authentic failure blocks after test execution. Test
    # logs occur earlier, so when the footer states N failures the last N
    # protocol-shaped blocks are the only candidates that can satisfy it. This
    # prevents earlier forged-looking logs from occupying those N slots and
    # hiding the real failing test.
    selected = protocol[-expected_total:] if expected_total \
        and len(protocol) >= expected_total else protocol
    entries, counts, complete = [], {"FAIL": 0, "ERROR": 0}, True
    # COUNTED WHERE IT IS SEEN. A block's completeness is decided inside this
    # loop and nowhere else, so the number of incomplete ones is tallied here
    # rather than re-derived afterwards from a second pass that could disagree
    # with the first.
    incomplete = 0
    for index, hit, test_id in selected:
        kind = hit.group("kind")
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        body = text[hit.end():end]
        traceback = _traceback_diagnostic(body) \
            or _loader_error_diagnostic(body, test_id)
        counts[kind] += 1
        whole = bool(test_id and traceback)
        incomplete += 0 if whole else 1
        complete = complete and whole
        if test_id:
            entries.append({"kind": kind, "test": _failure_text(test_id),
                            "traceback": traceback})
    # WHICH CONDITION FIRED, NOT JUST THAT ONE DID. Four independent things
    # make a FAILED run unreadable and they need four different fixes from
    # whoever reads the receipt — a footer with no counts, a block count that
    # disagrees with those counts, per-kind counts that disagree, and a block
    # carrying no id or no diagnosis. Collapsing them to a bool is why a
    # receipt could say "neither a pass nor a readable failure" and no reader,
    # including the author of this function, could say which. The reason is
    # WORDS and never a code: its only consumer is a sentence.
    if status == "OK":
        unreadable = bool(protocol)
        why = ("a passing summary carrying %d failure block%s"
               % (len(protocol), "" if len(protocol) == 1 else "s")
               if unreadable else None)
    elif status == "FAILED":
        if not expected:
            unreadable, why = True, ("the FAILED summary named no failures= "
                                     "or errors= count")
        elif len(protocol) != expected_total:
            unreadable, why = True, (
                "the summary counts %d failure%s and %d protocol-shaped "
                "block%s parsed"
                % (expected_total, "" if expected_total == 1 else "s",
                   len(protocol), "" if len(protocol) == 1 else "s"))
        elif counts != expected:
            unreadable, why = True, (
                "the summary counts %d failure/%d error and the blocks "
                "are %d failure/%d error"
                % (expected.get("FAIL", 0), expected.get("ERROR", 0),
                   counts["FAIL"], counts["ERROR"]))
        elif not complete:
            unreadable, why = True, (
                "%d of %d failure block%s carries no id or no diagnosis"
                % (incomplete, len(selected),
                   "" if len(selected) == 1 else "s"))
        else:
            unreadable, why = False, None
    else:
        unreadable, why = True, "the run printed no readable summary"
    return entries, unreadable, why


def parse_result(text):
    """The runner's OWN summary, or an honest UNKNOWN.

    Returns {status, ran, skipped, detail, elapsed, failures,
    failures_unreadable}. status is OK / FAILED / UNKNOWN, and UNKNOWN is a real
    outcome: a run killed by a timeout, a GraalPy hang, or an import error before
    collection prints no summary at all. `failures=[]` means confirmed pass only;
    a non-pass whose identities cannot be completed says unreadable instead."""
    text = _ANSI.sub("", str(text or ""))
    ran_hits = list(_RAN.finditer(text))
    summaries = [("OK", hit) for hit in _OK.finditer(text)] + \
        [("FAILED", hit) for hit in _FAILED.finditer(text)]
    out = {"status": "UNKNOWN", "ran": None, "skipped": None,
           "detail": "", "elapsed": None, "failures": [],
           "failures_unreadable": True}
    reported, detail, footer_ok = "UNKNOWN", "", False
    if summaries:
        reported, summary = max(summaries, key=lambda item: item[1].start())
        detail = summary.group(1) or ""
        before = [hit for hit in ran_hits if hit.end() <= summary.start()]
        if before:
            ran = before[-1]
            footer_ok = not text[ran.end():summary.start()].strip() \
                and not text[summary.end():].strip()
            if footer_ok:
                out["ran"] = int(ran.group(1))
                out["elapsed"] = float(ran.group(2))
                out["status"], out["detail"] = reported, detail
        if not footer_ok:
            out["detail"] = ("the summary says %s but the run count is "
                             "unreadable or not its terminal footer") % reported
    elif ran_hits:
        ran = ran_hits[-1]
        out["ran"] = int(ran.group(1))
        out["elapsed"] = float(ran.group(2))
    if footer_ok and detail:
        hit = re.search(r"skipped=(\d+)", detail)
        if hit:
            out["skipped"] = int(hit.group(1))
    failures, unreadable, why = _parse_failures(text, reported, detail)
    out["failures"] = failures
    out["failures_unreadable"] = unreadable or not footer_ok
    # THE FOOTER CHECK IS ITS OWN CONDITION and it outranks the block reasons:
    # without a terminal footer the summary is not the run's own verdict at
    # all, so a block-level reason would be describing text we have no cause
    # to trust. BUT IT OUTRANKS ONLY WHEN IT HAS SOMETHING TO SAY — `detail`
    # is empty whenever no summary line was found at all, so it is consulted
    # only when it HAS content. Preferring an empty string over the parse's
    # own reason is this same defect one layer up: an unreadable verdict with
    # no stated cause. Measured across all eight conditions: none blank.
    out["unreadable_reason"] = (
        ((out["detail"] or why) if not footer_ok else why)
        if out["failures_unreadable"] else None)
    if out["status"] == "OK" and failures:
        out["status"] = "UNKNOWN"
        out["detail"] = "the summary says OK but failure blocks were present"
        out["failures_unreadable"] = True
    return out


def _event_bytes(row):
    return len((json.dumps(row, ensure_ascii=False, separators=(",", ":")) +
                "\n").encode("utf-8"))


def _failure_chunk_id(row):
    """Content identity for one bounded failure-identity chunk."""
    payload = {"v": row.get("v"), "event": row.get("event"),
               "failures": row.get("failures")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:
        _FAILURE_CHUNK_ID_LEN]


def _failure_identity_chunks(failures):
    """Full ordered identities as independently bounded ledger events.

    Diagnostics stay in the receipt's display list. Chunks carry only the kind
    and canonical test id that base-check consumes, so the measured full-suite
    scale remains auditable without letting traceback prose breach the
    event-ledger's 64 KiB per-event boundary.
    """
    identities = [{"kind": item["kind"], "test": item["test"]}
                  for item in failures]
    empty = {"v": _FAILURE_CHUNK_VERSION, "event": _FAILURE_CHUNK_EVENT,
             "failures": [], "id": "0" * _FAILURE_CHUNK_ID_LEN}
    empty_size = _event_bytes(empty)
    chunks, current, size = [], [], empty_size
    for identity in identities:
        item_size = len(json.dumps(identity, ensure_ascii=False,
                                   separators=(",", ":")).encode(
                                       "utf-8", "surrogatepass"))
        next_size = size + item_size + (1 if current else 0)
        if next_size > eventledger.MAX_EVENT_BYTES:
            if not current:
                raise ValueError("one failure identity exceeds the ledger event limit")
            row = {"v": _FAILURE_CHUNK_VERSION,
                   "event": _FAILURE_CHUNK_EVENT, "failures": current}
            row["id"] = _failure_chunk_id(row)
            chunks.append(row)
            current, size = [], empty_size
            next_size = size + item_size
            if next_size > eventledger.MAX_EVENT_BYTES:
                raise ValueError("one failure identity exceeds the ledger event limit")
        current.append(identity)
        size = next_size
    if current:
        row = {"v": _FAILURE_CHUNK_VERSION, "event": _FAILURE_CHUNK_EVENT,
               "failures": current}
        row["id"] = _failure_chunk_id(row)
        chunks.append(row)
    return chunks


def _failure_chunk_version_known(row):
    version = row.get("v") if isinstance(row, dict) else None
    return type(version) is int and version in (_FAILURE_CHUNK_VERSION,)


def _failure_chunk_error(row):
    if not isinstance(row, dict):
        return "failure-identity chunk is not an object"
    if row.get("event") != _FAILURE_CHUNK_EVENT:
        return "failure-identity chunk has event %r" % (row.get("event"),)
    if not _failure_chunk_version_known(row):
        return "failure-identity chunk version %r is unsupported" % (row.get("v"),)
    if set(row) != {"v", "event", "failures", "id"}:
        return "failure-identity chunk has an unknown field shape"
    failures = row.get("failures")
    if not isinstance(failures, list) or not failures:
        return "failure-identity chunk has no identity list"
    for item in failures:
        if not isinstance(item, dict) or set(item) != {"kind", "test"} \
                or not _has_failure_text(item.get("kind")) \
                or not _has_failure_text(item.get("test")):
            return "failure-identity chunk has an unreadable identity"
    if not isinstance(row.get("id"), str) \
            or row["id"] != _failure_chunk_id(row):
        return "failure-identity chunk content id does not match"
    return None


def _failure_chunks_with_errors(rows):
    """Validated chunks plus rejection reasons keyed by their claimed id.

    The complete identity record exists to validate and transport one receipt
    object; it is not a second operational input. Readers normally pay one
    bounded ledger walk and keep only the referenced chunks. Detailed reasons
    survive for import/show so malformed shape, id mismatch, conflict and
    oversize are not collapsed into the misleading "own content mismatch".
    """
    out, errors, skipped = {}, {}, 0
    for row in rows:
        if not isinstance(row, dict) or row.get("event") != _FAILURE_CHUNK_EVENT:
            continue
        rid = row.get("id") if isinstance(row.get("id"), str) else None
        err = _failure_chunk_error(row)
        if err:
            skipped += 1
            if rid:
                errors.setdefault(rid, []).append(err)
                # Once one claimed id has invalid/conflicting content it is
                # poisoned for this object. Keeping an earlier valid-looking
                # row would let a later mismatch disappear behind it.
                out.pop(rid, None)
            continue
        prior = out.get(rid)
        if prior is not None and prior != row:
            skipped += 1
            errors.setdefault(rid, []).append(
                "failure-identity chunk id is claimed by conflicting content")
            out.pop(rid, None)
            continue
        if rid not in errors:
            out[rid] = row
    return out, errors, skipped


def _failure_chunks(rows):
    chunks, _errors, skipped = _failure_chunks_with_errors(rows)
    return chunks, skipped


def _timings(rows):
    """One valid timing sibling per authoritative receipt; conflicts poison it."""
    chunks, _skipped = _failure_chunks(rows)
    receipt_runs, ambiguous_runs = {}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get("event") != "gate" \
                or not _id_matches(row, chunks):
            continue
        rid, ran = row["id"], row.get("ran")
        if rid in receipt_runs and receipt_runs[rid] != ran:
            ambiguous_runs.add(rid)
        else:
            receipt_runs[rid] = ran
    out, poisoned, skipped = {}, set(), 0
    for row in rows:
        if not isinstance(row, dict) or row.get("event") != _TIMING_EVENT:
            continue
        receipt = row.get("receipt")
        err = _timing_event_error(row)
        if not err and row["state"] == "COMPLETE" \
                and receipt in receipt_runs and (
                    receipt in ambiguous_runs
                    or row["measured_tests"] != receipt_runs[receipt]):
            err = "module timing test census disagrees with its receipt"
        if err:
            skipped += 1
            if isinstance(receipt, str):
                out.pop(receipt, None)
                poisoned.add(receipt)
            continue
        prior = out.get(receipt)
        if prior is not None and prior != row:
            skipped += 1
            out.pop(receipt, None)
            poisoned.add(receipt)
        elif receipt not in poisoned:
            out[receipt] = row
    return out, poisoned, skipped


def _capped_failures(failures):
    """The LEGACY bounded list: a prefix plus a marker saying how many
    identities were dropped and never recorded anywhere.

    This is what every receipt did before the v8 failure record, and it is
    still what the focused kind does — see `_mint_result`. It is kept as a
    named function rather than inline so the one remaining caller is greppable
    and the behaviour cannot spread back by copy."""
    shown = failures[:FAILURE_CAP]
    if len(failures) <= len(shown):
        return list(shown)
    return list(shown) + [{"truncated": len(failures) - len(shown)}]


def _failure_record(failures):
    """The bounded v8 receipt fields plus every full identity chunk."""
    chunks = _failure_identity_chunks(failures)
    shown = [dict(item) for item in failures[:FAILURE_CAP]]
    return {"failures": shown, "failure_total": len(failures),
            "failure_diagnostics_omitted": len(failures) - len(shown),
            "failure_chunks": [row["id"] for row in chunks]}, chunks


def _timing_id(row):
    payload = {key: row.get(key) for key in (
        "v", "event", "receipt", "state", "reason", "planned_modules",
        "measured_modules", "measured_tests", "runner_elapsed",
        "module_wall", "process_cpu", "unattributed_wall", "top")}
    # OPTIONAL, AND HASHED ONLY WHEN PRESENT: every row written before the
    # field existed keeps the id it was written with.
    if "skipped_by_class" in row:
        payload["skipped_by_class"] = row["skipped_by_class"]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[
        :_TIMING_ID_LEN]


def _finite_nonnegative(value):
    if type(value) is int:
        return 0 <= value <= sys.float_info.max
    return type(value) is float and math.isfinite(value) and value >= 0


def _timing_event_error(row):
    required = {"v", "event", "receipt", "state", "reason",
                "planned_modules", "measured_modules", "measured_tests",
                "runner_elapsed", "module_wall", "process_cpu",
                "unattributed_wall", "top", "id"}
    if not isinstance(row, dict) \
            or set(row) - {"skipped_by_class"} != required \
            or row.get("v") != _TIMING_VERSION \
            or row.get("event") != _TIMING_EVENT:
        return "module timing event schema is unreadable"
    if "skipped_by_class" in row \
            and not gatetestrecord.skip_detail_ok(row["skipped_by_class"]):
        return "module timing skipped-by-class detail is unreadable"
    if not isinstance(row.get("receipt"), str) \
            or not _ID.fullmatch(row["receipt"]):
        return "module timing receipt reference is unreadable"
    if row.get("state") not in ("COMPLETE", "UNKNOWN") \
            or (row["state"] == "COMPLETE") != (row.get("reason") is None):
        return "module timing completeness is unreadable"
    if row.get("reason") is not None and (not isinstance(row["reason"], str)
                                          or not row["reason"]):
        return "module timing reason is unreadable"
    for name in ("planned_modules", "measured_modules", "measured_tests"):
        if type(row.get(name)) is not int or row[name] < 0:
            return "module timing %s is unreadable" % name
    for name in ("module_wall", "process_cpu"):
        if not _finite_nonnegative(row.get(name)):
            return "module timing %s is unreadable" % name
    for name in ("runner_elapsed", "unattributed_wall"):
        value = row.get(name)
        if value is not None and not _finite_nonnegative(value):
            return "module timing %s is unreadable" % name
    top = row.get("top")
    if not isinstance(top, list) or len(top) > gatetestrecord.TIMING_TOP \
            or row["state"] == "UNKNOWN" and top:
        return "module timing top list is unreadable"
    for item in top:
        if not isinstance(item, dict) or set(item) != {
                "module", "tests", "wall", "process_cpu"} \
                or not isinstance(item.get("module"), str) \
                or not item["module"] or len(item["module"]) > 512 \
                or type(item.get("tests")) is not int or item["tests"] <= 0:
            return "module timing top row is unreadable"
        for name in ("wall", "process_cpu"):
            if not _finite_nonnegative(item.get(name)):
                return "module timing top row is unreadable"
    ordered = sorted(top, key=lambda item: (-item["wall"], item["module"]))
    if top != ordered or len({item["module"] for item in top}) != len(top):
        return "module timing top order is unreadable"
    if row["state"] == "COMPLETE":
        modules = row["measured_modules"]
        omitted = modules - len(top)
        if not top or row["planned_modules"] != modules \
                or len(top) != min(gatetestrecord.TIMING_TOP, modules) \
                or row["measured_tests"] < modules \
                or any(row[name] is None for name in (
                    "runner_elapsed", "module_wall", "process_cpu",
                    "unattributed_wall")):
            return "complete module timing census is inconsistent"
        tests = sum(item["tests"] for item in top)
        wall = round(sum(item["wall"] for item in top), 6)
        cpu = round(sum(item["process_cpu"] for item in top), 6)
        aggregate_tolerance = _TIMING_ROW_UNIT * (
            (len(top) + 1) // 2 if len(top) > 1 else 0)
        if tests + omitted > row["measured_tests"] \
                or wall > row["module_wall"] + aggregate_tolerance \
                or cpu > row["process_cpu"] + aggregate_tolerance:
            return "complete module timing aggregates are inconsistent"
        if omitted == 0 and (
                tests != row["measured_tests"]
                or not math.isclose(wall, row["module_wall"],
                                    rel_tol=0.0,
                                    abs_tol=aggregate_tolerance)
                or not math.isclose(cpu, row["process_cpu"],
                                    rel_tol=0.0,
                                    abs_tol=aggregate_tolerance)):
            return "complete module timing aggregates are inconsistent"
        if row["module_wall"] > row["runner_elapsed"] \
                + _TIMING_RUNNER_TOLERANCE:
            return "complete module timing wall exceeds runner elapsed"
        unattributed = round(max(
            0.0, row["runner_elapsed"] - row["module_wall"]), 6)
        if not math.isclose(row["unattributed_wall"], unattributed,
                            rel_tol=0.0, abs_tol=0.000001):
            return "complete module timing unattributed wall is inconsistent"
    if not isinstance(row.get("id"), str) or row["id"] != _timing_id(row):
        return "module timing content id does not match"
    try:
        if _event_bytes(row) > eventledger.MAX_EVENT_BYTES:
            return "module timing event exceeds the ledger limit"
    except (TypeError, ValueError):
        return "module timing event is not durable JSON"
    return None


def _unknown_timing(reason):
    return {"state": "UNKNOWN", "reason": _failure_text(reason),
            "planned_modules": 0, "measured_modules": 0,
            "measured_tests": 0, "module_wall": 0.0,
            "process_cpu": 0.0, "top": []}


def _read_module_timing(directory, token):
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".timing"))
    except OSError as exc:
        return _unknown_timing("module timing artifact is unreadable: %s" % exc)
    if len(names) != 1:
        return _unknown_timing(
            "module timing artifact census found %d records, expected 1"
            % len(names))
    try:
        row = gatetestrecord.read_timing_artifact(
            os.path.join(directory, names[0]), token)
    except (OSError, ValueError, TypeError) as exc:
        return _unknown_timing("module timing artifact is unreadable: %s" % exc)
    return {key: row[key] for key in (
        "state", "reason", "planned_modules", "measured_modules",
        "measured_tests", "module_wall", "process_cpu", "top",
        "skipped_by_class") if key in row}


def _module_timing_event(receipt, timing):
    state = timing.get("state")
    reason = timing.get("reason")
    ran = receipt.get("ran")
    elapsed = receipt.get("elapsed")
    if state == "COMPLETE" and timing.get("measured_tests") != ran:
        state, reason = "UNKNOWN", (
            "module timing test census disagrees with the unittest footer")
    if state == "COMPLETE" and not _finite_nonnegative(elapsed):
        state, reason = "UNKNOWN", "unittest elapsed time is unreadable"
    module_wall = timing.get("module_wall")
    if state == "COMPLETE" and not _finite_nonnegative(module_wall):
        state, reason = "UNKNOWN", "module timing module wall is unreadable"
    top = timing.get("top") if state == "COMPLETE" else []
    row = {"v": _TIMING_VERSION, "event": _TIMING_EVENT,
           "receipt": receipt["id"], "state": state,
           "reason": _failure_text(reason, gatetestrecord.TIMING_REASON_CAP)
           if reason else None,
           "planned_modules": timing.get("planned_modules", 0),
           "measured_modules": timing.get("measured_modules", 0),
           "measured_tests": timing.get("measured_tests", 0),
           "runner_elapsed": elapsed if type(elapsed) in (int, float) else None,
           "module_wall": module_wall,
           "process_cpu": timing.get("process_cpu"),
           "unattributed_wall": round(max(0.0, elapsed - module_wall), 6)
           if state == "COMPLETE" else None,
           "top": top}
    if timing.get("skipped_by_class"):
        row["skipped_by_class"] = timing["skipped_by_class"]
    row["id"] = _timing_id(row)
    err = _timing_event_error(row)
    if err:
        fallback = {"v": _TIMING_VERSION, "event": _TIMING_EVENT,
                    "receipt": receipt["id"], "state": "UNKNOWN",
                    "reason": _failure_text(err), "planned_modules": 0,
                    "measured_modules": 0, "measured_tests": 0,
                    "runner_elapsed": elapsed
                    if _finite_nonnegative(elapsed) else None,
                    "module_wall": 0.0, "process_cpu": 0.0,
                    "unattributed_wall": None, "top": []}
        fallback["id"] = _timing_id(fallback)
        return fallback
    return row


def _failure_record_error(row, chunks, chunk_errors=None):
    """Validate the failure object: v8 overflow, or v9 inline/overflow."""
    failures = row.get("failures")
    total = row.get("failure_total")
    omitted = row.get("failure_diagnostics_omitted")
    refs = row.get("failure_chunks")
    if not isinstance(failures, list) or len(failures) > FAILURE_CAP:
        return "failure diagnostics are not a bounded list"
    if type(total) is not int or total < 0 or type(omitted) is not int:
        return "failure totals are not exact integers"
    if omitted != total - len(failures) or omitted < 0:
        return "failure diagnostic total and omitted count disagree"
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        return "failure chunk references are unreadable"
    if row.get("v") == SHARDED_AUTHORITY_VERSION and total <= FAILURE_CAP:
        # V9 is the whole-suite authority rung, not an overflow-only kind.
        # It inherits v8's count bindings even when all diagnostics fit inline.
        if refs or omitted or total != len(failures):
            return "v9 inline failure record carries overflow evidence"
        return None
    if total <= FAILURE_CAP or not refs:
        return "v8 failure record is not an overflow object"
    if len(failures) != FAILURE_CAP:
        return "failure diagnostics are not the complete bounded prefix"
    identities = []
    for ref in refs:
        chunk = chunks.get(ref)
        if chunk is None:
            reasons = (chunk_errors or {}).get(ref) or ()
            if reasons:
                return "failure identity chunk %s was rejected: %s" % (
                    ref, "; ".join(reasons))
            return "failure identity chunk %s is absent" % ref
        identities.extend(chunk["failures"])
    if len(identities) != total:
        return "failure identity chunks hold %d identities, receipt says %d" % (
            len(identities), total)
    shown_identities = [{"kind": item.get("kind"), "test": item.get("test")}
                        for item in failures if isinstance(item, dict)]
    if len(shown_identities) != len(failures) \
            or identities[:len(failures)] != shown_identities:
        return "failure diagnostics do not match the first recorded identities"
    return None


# ------------------------------------------------------------- stale base
#
# A THREE LINE lane once came back FAILED on
# test_tests_carry_no_private_fixture_labels — a test file the lane never
# touched. Re-running the gate serialized on an identical tree failed the same
# way (so not a race), running the same test on current trunk passed (so not
# the lane), and the lane's base simply predated a suite that trunk had since
# fixed. Three lanes blocked on a stale base in quick succession, and each one
# cost a full human-grade investigation, because the receipt HAD every input
# to that investigation and said only "FAILED". That is the bug class: a
# surface that holds the deciding information and does not say it.
#
# So a FAILED suite receipt now takes those same measurements itself and
# records a verdict about WHOSE failure this is:
#
#   STALE_BASE   every failing test file is outside the lane's own diff
#                (against ITS merge-base with trunk — `git diff trunk lane`
#                lists every file trunk touched since the fork and once read
#                "68 of 68 lanes collide"), the same tests PASS on current
#                trunk, STAY green with the lane's own changes applied on top
#                of that trunk, and the same failures REPRODUCE at the
#                merge-base without the lane's changes — actually run, never
#                inferred. The lane-on-trunk leg is the CAUSAL one, and it
#                exists because the first three were proven insufficient:
#                let test T fail for TWO reasons, cause A in the old
#                base and cause B the lane's — the file is untouched, T fails
#                at the merge-base (A), trunk fixed A so T is green there, and
#                the old predicate said STALE_BASE while a rebase stays red.
#                SAME TEST ID IS NOT CAUSAL PROOF. Only running the lane ON
#                trunk answers "will the rebase this verdict prescribes
#                actually go green?". The merge-base leg is KEPT beside it as
#                the reproduction guard: without it, a flaky or
#                old-base-interaction failure that never reproduces anywhere
#                WITHOUT the lane would mint STALE_BASE off two green
#                reference runs alone.
#   LANE_OWNED   a failing test file is in the lane's diff — ordinary FAILED,
#                nothing extra to say.
#   NOT_STALE    THESE failures — named, not counted — were seen failing on
#                trunk by id (a red trunk is its own problem and is not
#                laundered into "not yours"), they are still red with the lane
#                applied to trunk (the lane owns them, whatever the base once
#                broke), or they vanish at the merge-base (the lane is
#                implicated even though it never touched the test files).
#   UNKNOWN      any leg was unreadable or was skipped — said in words,
#                never a silent plain FAILED. A lane that does not APPLY
#                cleanly onto trunk is UNKNOWN too: an unresolvable rebase
#                means nobody can answer the causal question yet. A trunk run
#                that failed on something OTHER than the failures under
#                investigation lands here too (see the trunk-leg note below).
#
# ADVISORY BY CONSTRUCTION. bind() never reads this field: a stale-base run is
# still a run that did not go green, so the verdict changes what the receipt
# SAYS and never what it AUTHORIZES.

STALE_BASE = "STALE_BASE"
LANE_OWNED = "LANE_OWNED"
NOT_STALE = "NOT_STALE"
BASE_UNKNOWN = "UNKNOWN"

# Each reference run (trunk, merge-base) is bounded by this wall clock. A
# breached bound reports the run as SKIPPED inside an UNKNOWN verdict — a cap
# that silently drops work is its own bug class.
RECHECK_TIMEOUT = 120.0


def _base_unknown(reason, **extra):
    out = {"verdict": BASE_UNKNOWN, "reason": _failure_text(reason)}
    out.update(extra)
    return out


def _test_file(repo, test_id):
    """The file a dotted unittest id lives in (repo-relative, / separators),
    or None when no prefix of the id resolves to one."""
    parts = [p for p in str(test_id or "").split(".") if p]
    for i in range(len(parts) - 1, 0, -1):
        stem = os.path.join(*parts[:i])
        for rel in (stem + ".py", os.path.join(stem, "__init__.py")):
            if os.path.isfile(os.path.join(repo, rel)):
                return rel.replace(os.sep, "/")
    return None


def _changed_files(repo, git, merge_base):
    """Every path the lane touched relative to its own merge-base — committed,
    uncommitted, and untracked. None when any read fails: an unreadable
    changed set must widen to UNKNOWN, never narrow to 'nothing changed'."""
    rc, out, _err = git.text(repo, "diff", "--name-only", "-z", merge_base)
    if rc != 0:
        return None
    files = {p for p in out.split("\0") if p}
    rc, out, _err = git.text(repo, "ls-files", "--others",
                             "--exclude-standard", "-z")
    if rc != 0:
        return None
    return files | {p for p in out.split("\0") if p}


def _reference_run(repo, git, sha, test_ids, timeout=None, prepare=None):
    """Run exactly `test_ids` in a throwaway detached checkout of `sha`.

    -> (result, err). result = {status, rc, failed, unreadable} where `failed`
    is the set of failing ids parse_result could read; err is words when the
    measurement could not be taken at all, and the caller's verdict is then
    UNKNOWN, never a guess. The checkout goes through the vcs seam and is
    removed before this returns, success or not. `prepare(path)` — words on
    failure, None on success — runs after the checkout and before the tests;
    it is how the lane-on-trunk leg lays the lane's changes into the throwaway
    tree without ever touching the caller's worktree."""
    timeout = RECHECK_TIMEOUT if timeout is None else timeout
    parent = tempfile.mkdtemp(prefix="helm-gate-basecheck-")
    path = os.path.join(parent, "tree")
    rc, _out, err = git.text(repo, "worktree", "add", "--detach", path, sha,
                             timeout=60)
    if rc != 0:
        shutil.rmtree(parent, ignore_errors=True)
        return None, "cannot check out %s for the reference run: %s" % (
            sha[:12], err or "git exited %s" % rc)
    try:
        if prepare is not None:
            err = prepare(path)
            if err:
                return None, err
        try:
            p = subprocess.run(
                [sys.executable, "-m", "unittest"] + sorted(test_ids),
                cwd=path, capture_output=True, text=True, timeout=timeout,
                env=_suite_env())
        except subprocess.TimeoutExpired:
            return None, ("the reference run of %d test%s on %s exceeded %gs "
                          "and was SKIPPED" % (
                              len(test_ids),
                              "" if len(test_ids) == 1 else "s",
                              sha[:12], timeout))
        except Exception as exc:
            return None, "the reference run on %s did not start: %s: %s" % (
                sha[:12], type(exc).__name__, exc)
    finally:
        git.text(repo, "worktree", "remove", "--force", path, timeout=60)
        shutil.rmtree(parent, ignore_errors=True)
        git.text(repo, "worktree", "prune", timeout=60)
    parsed = parse_result(p.stderr or "")
    failed = {e.get("test") for e in parsed["failures"]
              if isinstance(e, dict) and e.get("test")}
    # AND THE DIAGNOSIS BESIDE EACH ID. A loader marker's identity says WHICH
    # name did not load and nothing about WHY, and the two whys need opposite
    # verdicts: a module that is gone is a stale inventory, a module that is
    # there and failed to import is a real breakage on trunk. Only the
    # diagnosis separates them, and this is the last place it exists.
    diagnoses = {e["test"]: e.get("traceback")
                 for e in parsed["failures"]
                 if isinstance(e, dict) and e.get("test")}
    # AND WHY IT COULD NOT BE READ. This dict is the whole of what survives
    # the reference run: the receipt keeps none of its text, so a reason
    # dropped at this seam cannot be recovered afterwards by anyone. The parse
    # knows which condition fired, and base_check can name it only if this
    # carries it across.
    return {"status": parsed["status"], "rc": p.returncode, "failed": failed,
            "diagnoses": diagnoses,
            "unreadable": parsed["failures_unreadable"],
            "unreadable_reason": parsed.get("unreadable_reason")}, None


_ABSENT_TEST = re.compile(r"\bunittest\.loader\._FailedTest\.(?P<name>.+)$")
# THE PARSE KEEPS THE TRACEBACK AND DROPS THE EXCEPTION HEADER ABOVE IT, so
# "Failed to import test module: <mod>" — the obvious thing to key on — is NOT
# in the text this predicate receives, however plainly it sits in the raw
# block. Anything keyed on that line matches nothing and reports every module
# as absent. What survives is the loader's OWN FRAME, and that is the marker
# of a module load. The rule: read parse_result's output, never the transcript
# it was built from.
_LOADER_IMPORT = re.compile(r"loadTestsFromName|__import__\(module_name\)")
_NO_MODULE = re.compile(r"ModuleNotFoundError: No module named '(?P<mod>[^']+)'")


_DOTTED_PATH = re.compile(r"'(?P<path>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)'")

# A DOTTED PATH IS ELIDED FROM THE MIDDLE BECAUSE BOTH ITS ENDS IDENTIFY. The
# leaf names the module that is missing and the root names the package it was
# looked for in; every segment between them is the part a reader can infer.
# THE BUDGET IS MEASURED, not chosen: the deepest dotted module path in this
# tree is 5 segments and a unittest id adds Class.method, so 7 is the deepest
# id this repo can produce today. Eight leaves a segment of headroom and means
# every path this tree currently emits is left BYTE-IDENTICAL — the ordinary
# case never reaches the elision at all, which is what the short-path control
# in the arms pins.
_MODULE_PATH_SEGMENTS = 8


def _elide_module_path(text):
    """Shorten any dotted path in `text` from the MIDDLE -> str.

    THE LEAF MUST SURVIVE A RIGHT-TRUNCATION AND ONLY THE CALLER KNOWS WHICH
    TOKEN IDENTIFIES. `_failure_text` caps a reason at 500 characters and cuts
    the tail, marking the cut with an ellipsis — so the reader is told that
    something was removed and cannot be told WHAT. For an import verdict the
    removed tail is the missing module's name, which is the whole content of
    the sentence. Measured through the real renderer: a forty-segment path
    puts a 770-character reason through the cap and `final_leaf` is gone,
    while the same failure with a short path fits in 325 and survives.

    ELIDING HERE RATHER THAN WIDENING THE CAP, and rather than teaching
    `_failure_text` to keep both ends: that function renders every reason in
    this module and cannot know which token identifies any of them. Which word
    must survive is caller knowledge, so the caller spends its budget.
    """
    def shorten(match):
        parts = match.group("path").split(".")
        if len(parts) <= _MODULE_PATH_SEGMENTS:
            return match.group(0)
        kept = _MODULE_PATH_SEGMENTS - 1
        head, leaf = parts[:kept - 1], parts[-1]
        return "'%s.<%d segments elided>.%s'" % (
            ".".join(head), len(parts) - kept, leaf)
    return _DOTTED_PATH.sub(shorten, str(text or ""))


def _import_cause(diagnosis):
    """The EXCEPTION LINE from an import diagnosis, not its frames.

    A verdict says what the import RAISED, and that is the last segment of the
    diagnosis: the frames above it name unittest's own loader and the test
    file, which the reader already knows. Pasting the whole chain costs the
    only token that identifies the failure — the missing module's NAME sits at
    the END, and the sentence carrying it is rendered through a 500-character
    cap that truncates from the right, so the whole-chain form drops exactly
    the word the reader needs. Measured: 610 characters in, the name gone.

    TAKING THE LAST SEGMENT MOVES THE THRESHOLD AND DOES NOT REMOVE THE CLASS.
    The name is still the last token of one prose sentence cut from the right,
    so a path long for any OTHER reason loses it again — and the ellipsis the
    cap leaves behind tells the reader that a word went missing without
    telling them which. `_elide_module_path` spends the budget where it buys
    the most: the middle of the path, which a reader can infer, so the two
    ends that identify both survive.
    """
    parts = [p.strip() for p in str(diagnosis or "").split("|") if p.strip()]
    return _elide_module_path(parts[-1] if parts else "no diagnosis")


# A BOUNDED SENTENCE WITH TWO GROWING LISTS IS BOUNDED BY NEITHER. Eliding a
# path bounds ONE cause, and the sentence still grows with the NUMBER of
# broken modules AND the length of their test ids — measured before this
# helper existed: four deep paths rendered 500 characters with only the FIRST
# leaf surviving, and nine rendered 500 with none. Rationing one part while
# its neighbours grow is the same defect one level up, so BOTH lists spend a
# budget and BOTH say what they dropped.
#
# A COUNT IS NOT A BUDGET. "Show three" is defeated by three long items, so a
# count bounds the sentence only for inputs that were never the problem. The
# rule that survives a longer input is CHARACTERS, with only whole items
# admitted so nothing is rendered half-said.
# THE TWO BUDGETS SUM TO WHAT THE PROSE LEAVES. The fixed words of the longest
# of these sentences measure 257 characters against a 500-character cap, so 90
# and 150 leave room for the finished reason to stay under it — every omission
# is then a COUNT this module wrote rather than an ellipsis `_failure_text`
# had to add.
#
# THE BOUND IS NOT ABSOLUTE AND THE EXCEPTION IS DELIBERATE: `_bounded_list`
# renders its FIRST item whatever the budget, because a sentence that named no
# cause at all and then counted four would spend its words describing its own
# silence. So a single item longer than its budget can still push the reason
# to the cap — MEASURED across twenty shapes, exactly ONE does, a 130-character
# test id, which is past anything this tree can produce. MEASURED over seven shapes — one module, four shallow, one
# 40-segment path, four and nine deep paths, five 130-character ids, forty
# modules — the reason stays under the cap for everything this tree can
# produce (its deepest dotted module path is 5 segments, 7 with Class.method),
# and past that it degrades in the one direction that matters: the FIRST
# leaf survives in all seven and the dropped count is always stated.
_IMPORT_NAMES_BUDGET = 90
_IMPORT_CAUSES_BUDGET = 150


def _bounded_list(items, budget, joiner="; "):
    """Whole items up to `budget` characters, then a COUNT of the rest -> str.

    A BOUND THAT DROPS WORK SAYS SO. `_failure_text` caps the finished reason
    and marks the cut with an ellipsis, which tells a reader that something
    went missing and cannot tell them WHAT or HOW MUCH — and for an import
    verdict the missing tail is the only token that identifies the failure.
    Counting the remainder turns an unknown loss into a stated one.

    AT LEAST ONE ITEM ALWAYS RENDERS, even past the budget: a sentence that
    named zero causes and then said "and 4 more not shown" would spend its
    words describing its own silence.
    """
    items = [str(i) for i in (items or ())]
    if not items:
        return ""
    # THE REMAINDER CLAUSE IS SPENT FROM THE BUDGET, NOT BESIDE IT. Appending
    # it after the budget was already full puts the one sentence the bound
    # exists to produce OUTSIDE the bound — so the cap cuts exactly the words
    # that said something was cut, and the omission goes back to being
    # implied. Measured before this: twelve of fifteen ordinary shapes
    # rendered at the cap with the count gone.
    #
    # SO THE FIT IS TRIED WHOLE, LONGEST FIRST. Dropping one more item makes
    # the clause no longer and frequently one digit shorter, so the length is
    # not monotone in a way a single arithmetic step can exploit; rendering
    # the candidate and measuring it is exact, and the list is short.
    for keep in range(len(items), 0, -1):
        rest = len(items) - keep
        tail = "" if not rest else "%sand %d more not shown" % (joiner, rest)
        text = joiner.join(items[:keep]) + tail
        if len(text) <= budget or keep == 1:
            return text
    return ""


def _import_causes(names, diagnoses):
    """The causes a bounded sentence can carry, plus a count of the rest -> str.

    ONE renderer for both legs that conclude absence. The trunk leg and the
    merge-base leg ask the same question and said it with the same expression
    written twice; a third leg would have written it a third time, and the
    budget is exactly the kind of decision that drifts between copies.
    """
    return _bounded_list([_import_cause((diagnoses or {}).get(n))
                          for n in (names or ())], _IMPORT_CAUSES_BUDGET)


def _import_broke_at(reference, ids):
    """The markers in one reference run whose module WAS THERE -> [str].

    EVERY LEG THAT CONCLUDES ABSENCE ASKS THIS ONE QUESTION. Two legs reach
    the same wrong sentence from the same evidence — trunk's "do not exist on
    current trunk" and the merge-base's "do not exist at the merge-base" —
    because both read a loader marker as a missing name. A predicate that
    lived at one of them would leave the other saying it, which is how a
    per-case cure reads as complete while its twin one screen down does not
    move.
    """
    return sorted(_existence_census(reference, ids)[_MARKER_PRESENT])


def _marker_payload(name):
    """The name a loader marker carries, or "" when it carries no marker."""
    hit = _ABSENT_TEST.search(str(name or ""))
    return hit.group("name") if hit else ""


def _import_reached_the_module(diagnosis, payload):
    """True when the module named by a loader marker IS THERE and failed anyway.

    MEASURED, one run per shape, on a build node's CPython 3.13.7 — and the
    first thing the measurement settled is which shapes EXIST:

        absent module        ERROR: _FailedTest.t_absent
                             ImportError: Failed to import test module: t_absent
                             ModuleNotFoundError: No module named 'tests.t_absent'

        module imports a     ERROR: _FailedTest.t_missing_dep
        missing dependency   ImportError: Failed to import test module: t_missing_dep
                             File ".../tests/t_missing_dep.py", line 1, in <module>
                             ModuleNotFoundError: No module named 'a_dependency...'

        module raises a      NO MARKER AT ALL. The loader catches ImportError
        NON-import error     and mints _FailedTest; anything else propagates
                             out of loadTestsFromNames and takes the whole run
                             with it, protocol output and footer included.

    So the ambiguity is narrower than "present but broken" and it is REAL: the
    first two mint the SAME marker shape for opposite facts, because both
    raise ModuleNotFoundError. The third is not a marker question at all — it
    has no summary to parse, and an unreadable run already says so.

    THE DISCRIMINATOR IS WHICH MODULE THE ERROR NAMES. If the missing module
    is the one being loaded, it is gone. If it names anything else, the test
    module was found, executed far enough to run its own imports, and the
    failure is trunk's. An import failure carrying no ModuleNotFoundError at
    all — `cannot import name X from Y` — reached the module too.
    """
    text = str(diagnosis or "")
    if not _LOADER_IMPORT.search(text):
        return False                 # not a module load: this says nothing
    hit = None
    for hit in _NO_MODULE.finditer(text):
        pass                         # the LAST one is the failure's own cause
    if hit is None:
        return True                  # imported, then raised something else
    missing = hit.group("mod").split(".")
    return missing[-1] != str(payload).split(".")[-1]


def _absent_id(name, ids):
    """True when `name` is unittest's marker for AN ID WE ASKED FOR that did
    not load.

    `unittest.loader._FailedTest.<x>` is minted for two different things and
    only one of them is a stale inventory: <x> either names part of an id
    this run REQUESTED — the names exist in the peek's tree and not here — or
    it names something we never asked about, which is no evidence either way.

    THE OLD TEST FOR THAT WAS "IS <x> DOTTED", AND IT IS FALSE FOR EVERY
    SHAPE THE WORLD ACTUALLY PRODUCES. A reference run is always given
    explicit ids, so the loader walks module -> class -> attribute and names
    the ONE COMPONENT it could not resolve. Measured under the gate's own
    interpreter, one run per shape:

        tests.t.NoSuchClass.test_x     -> _FailedTest.NoSuchClass
        tests.no_such_module.C.test_y  -> _FailedTest.no_such_module
        tests.t.Case.test_missing      -> _FailedTest.test_missing

    Not one of the three carries a dot, so the dot rule answered False for
    all of them and base_check's stale-inventory branch was unreachable in
    practice — the branch existed, its test supplied a dotted marker no
    explicit-id run can emit, and it stayed green while never firing in the
    world.

    So ASK THE QUESTION DIRECTLY instead of by proxy: does the marker's
    payload appear as a run of dot-components inside one of the ids under
    investigation? That answers both of the original cases on their own
    terms: a component of a requested id is a stale inventory, anything else
    is not.

    TWO SHAPES, AND ONLY ONE OF THEM IS ADMITTED. A payload that IS the whole
    dotted id matches, because it names the id exactly — though whether any
    producer emits that shape is UNMEASURED, and it is admitted because it
    cannot be wrong rather than because it was seen. A DOTTED payload naming
    something NOBODY ASKED ABOUT is REFUSED, and that refusal is the point of
    asking the ids rather than counting dots: a marker for a test this run
    never requested is evidence about neither trunk nor the lane, however
    many dots it carries."""
    return bool(_marker_ids(name, ids))


def _marker_ids(name, ids):
    """WHICH ids under investigation a loader marker names -> set.

    `_absent_id` is this question reduced to a yes/no, and the yes/no is what
    let a leg speak about ids it never asked after: one marker can name two
    ids, three markers can leave four ids unnamed, and a quantifier over the
    MARKERS then renders as a claim about the IDS. The set is the fact; the
    boolean is a view of it."""
    m = _ABSENT_TEST.search(str(name or ""))
    if not m:
        return set()
    payload = m.group("name").split(".")
    hit = set()
    for tid in ids or ():
        parts = str(tid).split(".")
        for i in range(len(parts) - len(payload) + 1):
            if parts[i:i + len(payload)] == payload:
                hit.add(tid)
                break
    return hit


# A CLASS OR A METHOD THAT IS GONE NEVER REACHES THE IMPORT MACHINERY. The
# loader imports the module, then walks getattr, and the failure it records is
# an AttributeError naming the component it could not resolve — no loader
# frame, no ModuleNotFoundError, nothing `_LOADER_IMPORT` can see. MEASURED on
# the specimen: `AttributeError: module 'tests.test_chain_ended' has no
# attribute '<class>'` is the WHOLE diagnosis, frames included, which is to say
# there are none. The attribute the error names is the discriminator, exactly
# as the module the ModuleNotFoundError names is for the import shapes.
_NO_ATTRIBUTE = re.compile(r"has no attribute '(?P<attr>[^']+)'")

_MARKER_ABSENT = "absent"
_MARKER_PRESENT = "present"
_MARKER_UNREADABLE = "unreadable"


def _marker_disposition(diagnosis, payload):
    """Which of THREE facts a loader marker's diagnosis established -> str.

    UNREADABLE, ABSENT AND PRESENT ARE THREE VALUES AND THE COLLAPSE OF THE
    FIRST TWO IS AN EXONERATION. "I could not read why this name did not load"
    and "this name is not there" are different facts, and only the second is a
    stale inventory. A leg that answers the first with the second tells an
    author their failures belong to the gap — the one sentence that is
    believed and acted on — from evidence it never had.

    So a diagnosis earns ABSENT and is not given it by default:

      - a module load whose OWN module was not found          -> ABSENT
      - a module load that reached the module and raised       -> PRESENT
      - an AttributeError naming the marker's own payload      -> ABSENT
      - anything else, including no diagnosis at all           -> UNREADABLE
    """
    text = str(diagnosis or "")
    if _LOADER_IMPORT.search(text):
        return (_MARKER_PRESENT
                if _import_reached_the_module(text, payload)
                else _MARKER_ABSENT)
    hit = None
    for hit in _NO_ATTRIBUTE.finditer(text):
        pass                         # the LAST one is the failure's own cause
    if hit is not None and hit.group("attr") == str(payload):
        return _MARKER_ABSENT
    return _MARKER_UNREADABLE


def _existence_census(reference, ids):
    """What ONE reference run established about whether each id EXISTS there.

    THE NULL FROM A SCAN IS A FACT ABOUT THE SCAN, and this function exists so
    that no leg quantifies over its own result set. A guard over the run's
    FAILURES — "every name that failed here is a marker for one of ours" —
    cannot license a sentence quantified over the IDS: "the test names are all
    absent here". An id that RESOLVED AND PASSED appears in no failure, so it
    is invisible to the first quantifier and exonerated by the second.
    MEASURED: seven ids, three unresolvable, four green on trunk, and the leg
    reported all seven as nonexistent while the four were live regressions.

    -> {absent|present|unreadable: {marker name: [ids]}, foreign: [names],
        marked: [ids], unmarked: [ids]}. `unmarked` is the set no marker
    named, which the caller reads together with whatever else it knows about
    the run; this function never guesses what an unmarked id did.
    """
    diagnoses = reference.get("diagnoses") or {}
    out = {_MARKER_ABSENT: {}, _MARKER_PRESENT: {}, _MARKER_UNREADABLE: {},
           "foreign": []}
    ids = set(ids or ())
    marked = set()
    for name in sorted(reference.get("failed") or ()):
        hit = _marker_ids(name, ids)
        if not hit:
            out["foreign"].append(name)
            continue
        marked |= hit
        out[_marker_disposition(diagnoses.get(name),
                                _marker_payload(name))][name] = sorted(hit)
    out["marked"] = sorted(marked)
    out["unmarked"] = sorted(ids - marked)
    return out


def _absence_not_established(census, where):
    """The sentence a leg OWES when it cannot say these names are gone -> str.

    None when absence IS established: every id under investigation was named
    by a marker and every one of those markers read ABSENT. Anything short of
    that resolves toward this lane, never toward the gap — the two wrong
    answers here do not cost the same. Wrongly blaming the gap costs a reader
    one look at a sha; wrongly clearing the lane puts a regression on trunk
    under a sentence nobody re-reads. So weak evidence buys silence with a
    reason, and never an alibi.

    ONE RENDERER FOR BOTH LEGS, for the reason `_import_broke_at` is one: the
    trunk leg and the merge-base leg ask the same question of different shas,
    and a cure written at one of them leaves the other saying the old
    sentence one screen down.
    """
    if census["foreign"]:
        return ("failed on %s, %s no id under investigation, so %s says "
                "nothing about whether the failing tests exist there" % (
                    _bounded_list(census["foreign"], _IMPORT_NAMES_BUDGET,
                                  ", "),
                    "which names" if len(census["foreign"]) == 1
                    else "which name", where))
    if census[_MARKER_UNREADABLE]:
        return ("could not load %s and recorded no readable reason why — an "
                "inventory %s cannot READ is not an inventory that is EMPTY, "
                "so nothing here shows these failures are anyone's but this "
                "lane's" % (
                    _bounded_list(sorted(census[_MARKER_UNREADABLE]),
                                  _IMPORT_NAMES_BUDGET, ", "), where))
    if census["unmarked"]:
        total = len(census["marked"]) + len(census["unmarked"])
        return ("did not resolve %d of the %d failing tests, but the other "
                "%d DID resolve there and did not fail — so this is no stale "
                "inventory and %s does not explain them: %s" % (
                    len(census["marked"]), total, len(census["unmarked"]),
                    where,
                    _bounded_list(census["unmarked"], _IMPORT_NAMES_BUDGET,
                                  ", ")))
    return None


def _reproduced(ids, failed):
    """The subset of `ids` a reference run actually SHOWED failing.

    Plain `ids & failed` is not enough in either direction, so every leg reads
    a reference run through this one function rather than three spellings:

    - a class- or module-level fixture that explodes takes down every test
      under it while unittest prints ONE header, `ERROR: setUpClass
      (tests.test_x.C)` -> id `tests.test_x.C.setUpClass`. Intersection alone
      reads a genuinely red reference run as reproducing nothing.
    - anything else in `failed` covers NOTHING. Most often that is
      `unittest.loader._FailedTest.<name>`, minted when an id does not exist
      at that sha — a DIFFERENT failure wearing the same run's exit status."""
    ids = set(ids)
    hit = ids & set(failed or ())
    for name in failed or ():
        text = str(name)
        for fixture in _FIXTURE_NAMES:
            if text.endswith("." + fixture):
                scope = text[:-len(fixture)]
                hit |= {tid for tid in ids if str(tid).startswith(scope)}
    return hit


def _lane_snapshot(repo, git):
    """A dangling commit of the lane's WHOLE working state — committed,
    uncommitted AND untracked — parented on HEAD. -> (sha, err).

    Staged through a throwaway GIT_INDEX_FILE so the caller's index and
    worktree are never touched (the same law the reference checkouts obey),
    and never written to any ref: the object exists only to be merged into a
    throwaway checkout of trunk and then forgotten. Identity is pinned in the
    env because a repo with no user.email must not turn the causal leg into a
    spawn error."""
    parent = tempfile.mkdtemp(prefix="helm-gate-lanesnap-")
    env = {"GIT_INDEX_FILE": os.path.join(parent, "index"),
           "GIT_AUTHOR_NAME": "helm gate", "GIT_AUTHOR_EMAIL": "gate@helm",
           "GIT_COMMITTER_NAME": "helm gate",
           "GIT_COMMITTER_EMAIL": "gate@helm"}
    try:
        rc, _out, err = git.text(repo, "add", "-A", env=env, timeout=60)
        if rc != 0:
            return None, "cannot stage the lane's working state: %s" % (
                err or "git exited %s" % rc)
        rc, tree, err = git.text(repo, "write-tree", env=env)
        if rc != 0 or not tree:
            return None, "cannot write the lane's snapshot tree: %s" % (
                err or "git exited %s" % rc)
        rc, snap, err = git.text(
            repo, "commit-tree", tree, "-p", "HEAD", "-m",
            "helm gate lane-on-trunk snapshot (dangling by design)", env=env)
        if rc != 0 or not snap:
            return None, "cannot commit the lane's snapshot: %s" % (
                err or "git exited %s" % rc)
        return snap.lower(), None
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _lane_touched_test_ids(repo, git, merge_base, files, ids):
    """Did the lane's diff touch the BODY of any failing test, or None when
    that cannot be answered. Line-approximate on purpose: a hunk header's
    line ranges are matched against each failing test `def`'s start line.
    Over-matching a neighbor test costs a NOT_STALE instead of a STALE_BASE
    (the cautious direction); under-matching is the direction that would let
    a lane edit a failing test green and be told to rebase."""
    spans = {}          # rel -> [(start, end), ...] of touched lines (new side)
    for rel in set(files.values()):
        rc, out, _err = git.text(repo, "diff", "--unified=0",
                                 merge_base + "..HEAD", "--", rel)
        if rc != 0:
            return None
        ranges = []
        for line in (out or "").splitlines():
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or "1")
                if count:
                    ranges.append((start, start + count - 1))
        spans[rel] = ranges
    # map each failing id to its def line inside its file
    import ast
    touched = set()
    for tid, rel in files.items():
        path = os.path.join(repo, rel)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError):
            return None
        meth = tid.split(".")[-1]
        lines = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == meth]
        if not lines:
            continue            # unresolvable name: no evidence either way
        start = min(lines)
        end = max(n.end_lineno or n.lineno for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == meth)
        for lo, hi in spans.get(rel, []):
            if lo <= end and hi >= start:
                touched.add(tid)
                break
    return touched


def _base_check(repo, failures, failures_unreadable, chunked=False):
    """The stale-base measurements plus the causal one, taken by the process
    that already holds the data. -> a dict with `verdict` in {STALE_BASE,
    LANE_OWNED, NOT_STALE, UNKNOWN} and `reason` in words, always.

    STALE_BASE requires ALL of: (a) every failing test file outside the lane's
    diff against its own merge-base with trunk (committed + uncommitted +
    untracked); (b) the same tests pass on current trunk, actually run in a
    detached checkout; (b') the same tests STAY green with the lane's whole
    working state applied on top of that trunk — the causal leg, added after
    the dual-cause finding: a test failing for cause A in
    the old base AND cause B the lane's satisfies (a), (a') and (b) while a
    rebase stays red, because SAME TEST ID IS NOT CAUSAL PROOF; (a') the same
    failures reproduce, readably, at the merge-base WITHOUT the lane's
    changes — kept beside (b') as the reproduction guard, or a flake that
    never fails anywhere without the lane would ride two green reference runs
    into STALE_BASE; (c) every one of those legs was readable. Any leg short
    of that is LANE_OWNED, NOT_STALE, or UNKNOWN — a wrong STALE_BASE would
    tell an author to ignore a failure they own, which is worse than saying
    nothing.

    Every leg reads its reference run through _reproduced(), BY ID. A leg that
    reads the run's exit status instead answers a question nobody asked — the
    trunk leg did exactly that to a lane once (see below)."""
    if failures_unreadable:
        return _base_unknown(
            "the failure identities are unreadable, so no failure can be "
            "shown to lie outside this lane")
    ids = set()
    for entry in failures or ():
        if not isinstance(entry, dict):
            return _base_unknown("a recorded failure entry is unreadable")
        if "truncated" in entry:
            # TWO DIFFERENT FACTS WORE ONE SENTENCE. "Never identified" is
            # true of a row that recorded no chunks, and FALSE of a v8 row
            # whose chunks hold every id — and the false version reads as
            # "the gate lost your data", which sends a reader to a node-side
            # log for identities the receipt already carries. `chunked` says
            # which row this is. The VERDICT is the same either way: this
            # check is bounded to FAILURE_CAP failures on purpose, and it
            # refuses rather than attributing from a partial set.
            return _base_unknown(
                "%s failure%s beyond the %d-entry cap %s — attribution is "
                "bounded to %d failures so a large red run cannot spend "
                "reference suite runs at mint time, and a partial set cannot "
                "show every failure lies outside this lane"
                % (entry["truncated"],
                   "" if entry["truncated"] == 1 else "s",
                   FAILURE_CAP,
                   "ARE recorded in this receipt's failure chunks but were "
                   "NOT read here" if chunked
                   else "were never identified and were SKIPPED",
                   FAILURE_CAP))
        if entry.get("test"):
            ids.add(str(entry["test"]))
    if not ids:
        return _base_unknown("no failing test identities were recorded")
    for tid in sorted(ids):
        if "<locals>" in tid:
            return _base_unknown(
                "%s is a <locals> test and cannot be re-run by name" % tid)
    git = vcs.backend(repo)
    trunk_name = git.trunk_ref(repo)
    trunk = git.head_sha(repo, ref=trunk_name)
    if not trunk:
        return _base_unknown("cannot resolve trunk (%s)" % trunk_name)
    trunk = trunk.strip().lower()
    rc, mb, err = git.text(repo, "merge-base", trunk, "HEAD")
    if rc != 0 or not mb:
        return _base_unknown("cannot find the merge-base with %s: %s" % (
            trunk_name, err or "git exited %s" % rc))
    mb = mb.strip().lower()
    changed = _changed_files(repo, git, mb)
    if changed is None:
        return _base_unknown(
            "cannot read the lane's changed-file set against merge-base %s"
            % mb[:12])
    files = {}
    for tid in sorted(ids):
        rel = _test_file(repo, tid)
        if not rel:
            return _base_unknown("cannot resolve %s to a test file" % tid)
        files[tid] = rel
    owned = sorted({rel for rel in files.values() if rel in changed})
    # FILE OVERLAP IS A CAUTION, NEVER A CAUSE (task/166). The first cut
    # returned LANE_OWNED here, before any measurement, solely because the
    # failing test's FILE appears in the lane diff. Distinguishing repro:
    # old base has failing test A; trunk fixes A; the lane touches a
    # comment or an unrelated test B in the SAME file. Rebase-and-rerun
    # cures it — and the early return said "yours, investigate" to an
    # author who owned nothing, which is the one sentence that stops the
    # right investigation. The causal ladder below already answers the
    # question by RUNNING: trunk leg, lane-on-trunk leg, merge-base leg.
    # Overlap survives only as (i) a caution folded into whatever verdict
    # the ladder earns, and (ii) the hunk guard just above STALE_BASE:
    # when the lane edited the very tests under investigation, a green
    # lane-on-trunk run can be the lane masking its own failure, and
    # that is NOT_STALE, never STALE_BASE.
    overlap_note = ""
    if owned:
        overlap_note = (
            " Note: the lane's diff also touches %s — overlap is a caution, "
            "not a cause; the causal legs above decided this verdict." % (
                ", ".join(owned)))
    failing_files = sorted(set(files.values()))
    shown_files = failing_files[:FAILURE_CAP]
    base = {"trunk": trunk, "trunk_ref": trunk_name, "merge_base": mb,
            "failing_files": shown_files,
            "failing_files_total": len(failing_files),
            "failing_files_omitted": len(failing_files) - len(shown_files)}
    on_trunk, err = _reference_run(repo, git, trunk, ids)
    if err:
        return _base_unknown("trunk leg: " + err, **base)
    if not (on_trunk["status"] == "OK" and on_trunk["rc"] == 0):
        # A FAILED trunk run is NOT, by itself, evidence that THESE failures
        # are trunk's. This branch once inferred exactly that from the exit
        # status alone — it never looked at WHICH ids failed there — and told
        # a lane "the failing tests ALSO fail on current trunk". The author
        # then ran those tests by hand at the named sha and they PASSED: the
        # trunk run had failed on a DIFFERENT id, because a `_FailedTest` for
        # a name that does not resolve at that sha ends the run non-zero
        # while the failures under investigation never even ran.
        #
        # That is the worst possible wrong answer, because "red trunk, not
        # your fault" is the one sentence that makes an author STOP
        # INVESTIGATING and ship the lane anyway. So the ids must be NAMED
        # here, exactly as the lane-on-trunk leg below already named them.
        if on_trunk["status"] == "FAILED" and on_trunk["rc"] not in (0, None) \
                and not on_trunk["unreadable"]:
            also = sorted(_reproduced(ids, on_trunk["failed"]))
            missing = sorted(ids - set(also))
            if also and not missing:
                return dict(base, verdict=NOT_STALE, reason=_failure_text(
                    "every failing test in this run ALSO fails on current "
                    "trunk %s (%s) — a red trunk, not a stale base, and it "
                    "still stands between this lane and green: %s" % (
                        trunk[:12], trunk_name, ", ".join(also))))
            if also:
                # Partly trunk's, and the remainder is nobody's alibi: a
                # rebase onto a trunk that is red for SOME of these cannot
                # turn the lane green, so STALE_BASE is off the table, but the
                # unreproduced ids must not ride the others' exculpation.
                return dict(base, verdict=NOT_STALE, reason=_failure_text(
                    "%d of the %d failing tests ALSO fail on current trunk %s "
                    "(%s) — a red trunk in part, not a stale base — but %s "
                    "did NOT fail there, so trunk does not explain %s and %s "
                    "still this lane's to answer for. Trunk's: %s" % (
                        len(also), len(ids), trunk[:12], trunk_name,
                        ", ".join(missing),
                        "it" if len(missing) == 1 else "them",
                        "it is" if len(missing) == 1 else "they are",
                        ", ".join(also))))
            # NOTHING REPRODUCED — AND THE TWO REASONS FOR THAT ARE DIFFERENT
            # DIAGNOSES, so they get different sentences. If every name the
            # trunk run failed on is a `_FailedTest` wrapper, the ids did not
            # RESOLVE at that sha: a stale test inventory, which is what trunk's
            # own arm says and asserts. If it failed on real ids instead, trunk
            # is red ELSEWHERE and this run is simply no evidence either way.
            # Collapsing them loses the one an author can act on.
            # AND THE PREDICATE IS "WRAPS ONE OF OUR IDS", NOT "IS A
            # _FailedTest". A `_FailedTest` for SOME OTHER name says trunk
            # broke somewhere we never asked about; a `_FailedTest` wrapping
            # OUR id says that test did not RESOLVE at this sha. Same marker,
            # opposite diagnoses, and only the second is a stale inventory.
            named = sorted(on_trunk["failed"])
            if named and all(_absent_id(n, ids) for n in named):
                # A MARKER SAYS WHICH NAME DID NOT LOAD AND NOTHING ABOUT WHY,
                # AND THE TWO WHYS WANT OPPOSITE VERDICTS. A module that is
                # gone is a stale inventory; a module that is THERE and fails
                # to import is trunk's own breakage, and telling its author
                # "these tests do not exist on trunk" sends them to look in
                # the one place the answer is not. Same marker, and only the
                # diagnosis separates them.
                census = _existence_census(on_trunk, ids)
                broken = sorted(census[_MARKER_PRESENT])
                if broken:
                    return _base_unknown(
                        "the trunk reference run on %s (%s) could not IMPORT "
                        "%s, and the module is present — the import itself "
                        "failed, so this is a breakage on trunk rather than a "
                        "stale inventory, and whose these failures are could "
                        "not be determined from it. What the import raised: %s"
                        % (trunk[:12], trunk_name,
                           _bounded_list(broken, _IMPORT_NAMES_BUDGET, ", "),
                           _import_causes(
                               broken, on_trunk.get("diagnoses"))), **base)
                # AND THE ABSENCE MUST COVER EVERY ID, NOT EVERY FAILURE. The
                # guard above quantifies over what FAILED here; the sentence
                # below quantifies over what was ASKED. An id that resolved on
                # trunk and passed sits in neither the failure list nor the
                # marker set, so it rode the others' exoneration out — four of
                # them did, on a lane that owned all ten of its failures.
                unearned = _absence_not_established(
                    census, "current trunk %s (%s)" % (trunk[:12], trunk_name))
                if unearned:
                    return _base_unknown(
                        "the trunk reference run " + unearned, **base)
                return _base_unknown(
                    "the failing tests do not exist on current trunk %s (%s) — "
                    "a stale test inventory, not a red trunk. The test names "
                    "resolved in the peek's tree but %s absent here." % (
                        trunk[:12], trunk_name,
                        "is" if len(ids) == 1 else "are all"), **base)
            return _base_unknown(
                "the trunk reference run on %s (%s) failed, but on none of "
                "the %d failure%s under investigation — it failed on %s "
                "instead, so this run is no evidence that trunk owns %s and "
                "whose failures these are could not be determined" % (
                    trunk[:12], trunk_name, len(ids),
                    "" if len(ids) == 1 else "s",
                    ", ".join(named) or "nothing it named",
                    "it" if len(ids) == 1 else "them"), **base)
        # THE REASON IS RENDERED WHEN THE RUN SUPPLIED ONE. An UNKNOWN that
        # cannot say which condition fired is a shrug; the same UNKNOWN
        # naming its condition is a bug report, and the difference costs one
        # clause. A run that carried no reason still degrades to the old
        # sentence rather than inventing one.
        why = on_trunk.get("unreadable_reason")
        return _base_unknown(
            "the trunk reference run on %s (%s) read %s (rc %s) — neither a "
            "pass nor a readable failure%s, so it is no evidence that trunk "
            "owns these failures and whose they are could not be determined "
            "yet" % (trunk[:12], trunk_name, on_trunk["status"],
                     on_trunk["rc"], " (%s)" % why if why else ""), **base)
    if mb == trunk:
        # NOT_STALE IS THE RIGHT VERDICT AND THE OLD SENTENCE OVERCLAIMED IT.
        # mb == trunk proves the base is not stale, which is all this check is
        # chartered to decide. It said more: "the failures need this lane's
        # changes to appear" — an authorship claim the evidence cannot carry,
        # because the two runs being compared are NOT THE SAME SHAPE. The
        # reference run is `unittest <those ids>` (focused); the lane's run was
        # `unittest discover` (whole suite). Any failure born of suite
        # COMPOSITION — ordering, cross-test state, a fixture another module
        # leaked — passes the focused control and fails the real run, and the
        # old sentence attributed it to the author's diff as fact.
        #
        # PROVEN on a DOCS-ONLY tree whose whole diff was .md, which cannot
        # make a Python test fail: the sentence still told the author the
        # defect was theirs. Composition is the only thing a focused control
        # cannot see, so it is the one thing this sentence must not deny.
        #
        # So the sentence now states the measurement and names what it did NOT
        # rule out. The verdict is unchanged, so no consumer moves.
        return dict(base, verdict=NOT_STALE, reason=_failure_text(
            "the lane is based on current trunk %s, so the base is not "
            "stale, and these failing tests PASS THERE WHEN RUN ALONE. That "
            "does not show the lane caused them: the reference run is those "
            "ids only, the failing run was the whole suite, so a failure "
            "coming from suite composition rather than from the diff would "
            "look exactly like this" % trunk[:12]))
    # The CAUSAL leg, second so the lane-owned answer arrives before a third
    # checkout is ever paid for: the lane's whole working state applied onto
    # current trunk. Green here is what "rebase and re-run" actually promises;
    # red here is the lane's no matter what the other legs said.
    snap, err = _lane_snapshot(repo, git)
    if err:
        return _base_unknown("lane-on-trunk leg: " + err, **base)
    # A dirty tree means the snapshot merges UNCOMMITTED text the base diff
    # does not see — a green lane-on-trunk run would measure code the
    # verdict cannot describe. STALE_BASE is refused outright below; every
    # other verdict carries the caution. THE TEST IS THE TREE DELTA, never
    # status --porcelain: the gate's own in-flight markers and pycache are
    # untracked too, and a porcelain read cannot tell them from lane work —
    # measured: a clean lane read DIRTY off the marker alone.
    dirty_note = ""
    rc, head_tree, _e = git.text(repo, "rev-parse", "HEAD^{tree}")
    parent = tempfile.mkdtemp(prefix="helm-gate-dirtyck-")
    try:
        env = {"GIT_INDEX_FILE": os.path.join(parent, "index"),
               "GIT_AUTHOR_NAME": "helm gate", "GIT_AUTHOR_EMAIL": "gate@helm",
               "GIT_COMMITTER_NAME": "helm gate",
               "GIT_COMMITTER_EMAIL": "gate@helm"}
        rc2, _o, _e2 = git.text(repo, "add", "-A", env=env, timeout=60)
        rc3, cached, _e3 = git.text(repo, "ls-files", "-z", env=env)
        if rc3 == 0 and cached:
            for n in [p for p in cached.split("\0")
                      if p and "__pycache__" in p.split("/")]:
                git.text(repo, "rm", "-q", "--cached", "--ignore-unmatch", n,
                         env=env)
        rc4, clean_tree, _e4 = git.text(repo, "write-tree", env=env)
    finally:
        # The synthetic-index dir must not leak one tmpdir per
        # call — 65 accumulated on an inode-capped tmpfs in one session
        # before this cleanup was added.
        shutil.rmtree(parent, ignore_errors=True)
    if rc == 0 and rc4 == 0 and head_tree and clean_tree \
            and head_tree != clean_tree:
        dirty_note = (" The lane's working tree is DIRTY — the lane-on-trunk "
                      "leg measured uncommitted text the lane diff does not "
                      "show; commit before trusting a green leg.")

    def _apply_lane(path):
        rc2, out2, err2 = git.text(path, "merge", "--no-commit", "--no-ff",
                                   snap, timeout=60)
        if rc2 != 0:
            return ("this lane does not apply cleanly onto current trunk "
                    "%s (%s), so nobody can say whose failures these are "
                    "until that rebase is resolved: %s" % (
                        trunk[:12], trunk_name,
                        err2 or out2 or "git exited %s" % rc2))
        # The snapshot carries the lane's untracked files, and that includes
        # its __pycache__: a stale .pyc whose recorded size+mtime happen to
        # match a just-checked-out source would silently run the WRONG code
        # (the mutation-testing scar, now a gate law). Scrub them all.
        for dirpath, dirnames, _files in os.walk(path):
            if "__pycache__" in dirnames:
                shutil.rmtree(os.path.join(dirpath, "__pycache__"),
                              ignore_errors=True)
                dirnames.remove("__pycache__")
        return None

    on_top, err = _reference_run(repo, git, trunk, ids, prepare=_apply_lane)
    if err:
        return _base_unknown("lane-on-trunk leg: " + err, **base)
    if not (on_top["status"] == "OK" and on_top["rc"] == 0):
        if on_top["status"] == "FAILED" and on_top["rc"] not in (0, None) \
                and not on_top["unreadable"]:
            still = sorted(_reproduced(ids, on_top["failed"]))
            if still:
                return dict(base, verdict=NOT_STALE, reason=_failure_text(
                    "%s STILL fail%s with this lane's changes applied onto "
                    "current trunk %s (%s) — same test id or not, a stale "
                    "base cannot explain a failure that survives the rebase; "
                    "the lane owns %s%s" % (
                        ", ".join(still),
                        "s" if len(still) == 1 else "",
                        trunk[:12], trunk_name,
                        "it" if len(still) == 1 else "them",
                        dirty_note)))
        return _base_unknown(
            "the lane-on-trunk run on %s read %s (rc %s) and its failures "
            "are not readably the ones under investigation" % (
                trunk[:12], on_top["status"], on_top["rc"]), **base)
    at_base, err = _reference_run(repo, git, mb, ids)
    if err:
        return _base_unknown("merge-base leg: " + err, **base)
    if at_base["status"] == "OK" and at_base["rc"] == 0:
        possibility = "a lane change" if owned else \
            "an indirect lane interaction"
        return dict(base, verdict=NOT_STALE, reason=_failure_text(
            "the failing tests pass at the lane's own merge-base %s, current "
            "trunk, and with the lane applied to trunk — this single "
            "non-reproduction cannot distinguish %s from a flake; it proves "
            "only that the failure is not a stale-base reproduction%s"
            % (mb[:12], possibility, dirty_note)))
    if at_base["status"] != "FAILED" or at_base["rc"] in (0, None) \
            or at_base["unreadable"]:
        return _base_unknown(
            "the merge-base run on %s read %s (rc %s) and did not readably "
            "reproduce the failures" % (mb[:12], at_base["status"],
                                        at_base["rc"]), **base)
    # BOTH SIDES WERE RIGHT AND NEITHER ALONE IS. Trunk grew the
    # stale-test-inventory branch (no id matched at all -> the names did not
    # RESOLVE here, which is not a stale base); this lane replaces the weak
    # `ids & failed` that decides it. Plain intersection is wrong in two ways
    # `_reproduced` covers: a setUpClass explosion reports ONE mangled id while
    # taking down every test under it, and a `_FailedTest` for an absent name
    # ends the run non-zero without running anything under investigation.
    reproduced_at_base = _reproduced(ids, at_base["failed"])
    unreproduced = sorted(ids - reproduced_at_base)
    if reproduced_at_base:
        # the failures reproduce at the merge-base — STALE_BASE or NOT_STALE
        # depends on whether every test reproduced
        if unreproduced:
            return dict(base, verdict=NOT_STALE, reason=_failure_text(
                "%s pass%s at the merge-base %s — %s fail%s only WITH this "
                "lane's changes, so the lane is implicated" % (
                    ", ".join(unreproduced),
                    "es" if len(unreproduced) == 1 else "",
                    mb[:12], "it" if len(unreproduced) == 1 else "they",
                    "s" if len(unreproduced) == 1 else "")))
    else:
        # no id reproduced — the names did not resolve at the merge-base, OR
        # a module that IS there failed to import. The second is not an
        # absence and must not be reported as one: it is the merge-base's own
        # breakage, and this leg is the trunk leg's twin, asking the same
        # question of a different sha.
        census = _existence_census(at_base, ids)
        broke = sorted(census[_MARKER_PRESENT])
        if broke:
            diagnoses = at_base.get("diagnoses") or {}
            return _base_unknown(
                "the merge-base run on %s could not IMPORT %s, and the "
                "module is present — the import itself failed, so this leg "
                "measures the merge-base's own breakage rather than whether "
                "these failures reproduce there. What the import raised: %s"
                % (mb[:12],
                   _bounded_list(broke, _IMPORT_NAMES_BUDGET, ", "),
                   _import_causes(broke, diagnoses)), **base)
        # THE TWIN OF THE TRUNK LEG'S SCOPE GUARD, and it is here for the
        # reason `_import_broke_at` was hoisted: the same wrong sentence lived
        # at both legs from the same evidence, so a cure at one of them reads
        # complete while its sibling one screen down still exonerates. An id
        # this run RESOLVED — and one it failed on that nobody asked about —
        # both make "the test names are all absent here" a claim the leg
        # cannot carry.
        unearned = _absence_not_established(
            census, "the merge-base %s" % mb[:12])
        if unearned:
            return _base_unknown("the merge-base run " + unearned, **base)
        return _base_unknown(
            "the failing tests do not exist at the merge-base %s — "
            "a stale test inventory, not a stale base. The test names "
            "resolved in the peek's tree but %s absent here." % (
                mb[:12], "are" if len(ids) == 1 else "are all"),
            **base)
    # THE HUNK GUARD, last before STALE_BASE: the causal legs ran the lane's
    # WORKING STATE, so a lane that edited the very tests under
    # investigation can arrive here with four green/readable legs precisely
    # BECAUSE its edit masked the failure (a weakened assertion is green
    # everywhere). Same test id staying the same id is not innocence.
    if owned:
        if dirty_note:
            return dict(base, verdict=NOT_STALE, reason=_failure_text(
                "the failing tests' files are in this lane's diff AND the "
                "working tree is dirty — the green legs measured uncommitted "
                "text, and a lane edit to a failing test can mask the "
                "failure it owns. Commit the state and re-run; STALE_BASE "
                "is refused, not revoked."))
        touched = _lane_touched_test_ids(repo, git, mb, files, ids)
        if touched is None:
            return _base_unknown(
                "the lane touches a failing test's file and whether it "
                "touched the failing TESTS could not be measured — "
                "STALE_BASE is not earnable over an unmeasured edit", **base)
        if touched:
            return dict(base, verdict=NOT_STALE, reason=_failure_text(
                "every causal leg is green/readable, BUT this lane edited "
                "the failing test%s %s — a green lane-on-trunk run can be "
                "the edit masking the failure it owns, so STALE_BASE is "
                "refused. If the edit is genuinely unrelated (a comment, a "
                "neighbor test), say so in the receipt's reason when you "
                "re-run after rebase." % (
                    " body" if len(touched) == 1 else " bodies",
                    ", ".join(sorted(touched)))))
    scope = ("live outside this lane's diff" if not owned else
             "share a file with this lane's diff but no failing test body")
    return dict(base, verdict=STALE_BASE, reason=_failure_text(
        "%d failing test%s %s: they fail the same way at the merge-base %s "
        "WITHOUT this lane's changes, pass on current trunk %s (%s), and "
        "STAY green with this lane's changes applied on top of it — the "
        "base is stale, not the lane; rebase onto %s and re-run. This "
        "receipt still authorizes nothing.%s" % (
            len(ids), "" if len(ids) == 1 else "s", scope, mb[:12],
            trunk[:12], trunk_name, trunk_name, overlap_note)))


def _ident_of(row):
    """The interpreter block, or {} for ANYTHING that is not a mapping.

    `row.get("interpreter") or {}` looks like it does this and does not: a
    non-empty STRING is truthy, so it comes straight back and the next `.get`
    raises. The whole module was once reached that way — one ledger line
    reading
    `"interpreter": "not-an-object"` made `receipts()` and every `bind()` raise
    AttributeError, so a single malformed row wedged all receipt lookup instead
    of being refused. Third time this exact `or {}` shape has bitten in this
    file's short life; it is centralized here so there is one place to be
    wrong."""
    ident = row.get("interpreter")
    return ident if isinstance(ident, dict) else {}


def _host_of(row):
    """The host block, or {} for ANYTHING that is not a mapping — the same
    total reader as `_ident_of`, born total because that one was not."""
    ident = row.get("host")
    return ident if isinstance(ident, dict) else {}


_V5_INTERPRETER_KEYS = frozenset(
    ("name", "version", "language", "executable"))

_V5_HOST_KEYS = frozenset(("node", "system", "release", "id"))

def receipt_version_error(row):
    """Why a receipt version is not the wire-format integer, or None."""
    version = row.get("v")
    if type(version) is not int:
        return "receipt version must be an exact integer; got %r" % (version,)
    return None

def _v5_receipt_identity(row):
    """The one structured v5 content-identity value.

    Field names and nested objects make boundaries intrinsic: a NUL inside one
    string cannot be shifted into its neighbour and preserve the serialized
    value. Compact sorted JSON below gives this structure one deterministic byte
    encoding without changing the frozen v1-v4 delimiter grammar.

    THE DECLARED COMMAND BLOCK IS BOUND HERE TOO, and by PRESENCE for the same
    reason the join grammar binds it that way: this payload is an EARLY RETURN
    out of `_receipt_id`, so a cached row reached its id without ever passing
    the `suite-command-v1` part below and its declaration was outside its own
    identity — the project could be re-labelled under an unchanged id while the
    argv stayed hashed. A row with no block gains no key here, so every cached
    receipt already in a ledger encodes to the same bytes it always did.
    """
    argv = row.get("argv")
    if not isinstance(argv, list) or not all(
            isinstance(arg, str) for arg in argv):
        raise ValueError("v5 receipt argv must be a JSON array of strings")
    ident, machine = _ident_of(row), _host_of(row)
    declared = {"suite_command": row.get("suite_command")} \
        if row.get("suite_command") is not None else {}
    return {
        **declared,
        "format": "helm-gate-receipt-id-v5",
        "v": row.get("v"),
        "event": row.get("event"),
        "tree": {
            "before": {"head": row.get("head"), "tree": row.get("tree"),
                       "dirty": row.get("dirty")},
            "after": {"head": row.get("head_after"),
                      "tree": row.get("tree_after"),
                      "dirty": row.get("dirty_after")},
        },
        "ts": row.get("ts"),
        "interpreter": {key: ident.get(key)
                        for key in sorted(_V5_INTERPRETER_KEYS)},
        "argv": argv,
        "suite": row.get("suite"),
        "result": {"status": row.get("status"), "ran": row.get("ran"),
                   "skipped": row.get("skipped"), "rc": row.get("rc"),
                   "executed": row.get("executed")},
        "repo_id": row.get("repo_id"),
        "failures": {"items": row.get("failures"),
                     "unreadable": row.get("failures_unreadable")},
        "base_check": row.get("base_check"),
        "host": {key: machine.get(key) for key in sorted(_V5_HOST_KEYS)},
    }


def _receipt_id(row):
    """Content identity over every field a reader would rely on. The timestamp
    is inside it so two honest runs of the same tree stay separately
    addressable — this is a handle, never a deduplication key.

    A HANDLE IS ALSO ALL IT CAN BE, and that is a property of the construction
    rather than a gap in it: a plain sha256 with no secret proves the row is
    unaltered since it was written, and says nothing about who wrote it. Read
    that before adding a caller that treats a recomputing id as evidence a
    suite ran. (The writer-side proof of this reasoning lives beside the
    green-evidence shape check; that function does not exist on this trunk
    yet, so the citation is deliberately not made here rather than pointing
    at nothing.)"""
    version_err = receipt_version_error(row)
    if version_err:
        raise ValueError(version_err)
    if row.get("v") == CACHED_VERSION:
        payload = json.dumps(_v5_receipt_identity(row), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode(
            "utf-8", "surrogatepass")).hexdigest()[:16]
    ident = _ident_of(row)
    parts = [row.get("ts"), row.get("head"), row.get("tree"), row.get("dirty")]
    bracket = tuple(key in row for key in
                    ("head_after", "tree_after", "dirty_after"))
    if not _version_has(row, "post_run_bracket") \
            and any(bracket) and not all(bracket):
        raise ValueError("partial v1 post-run bracket")
    if _version_has(row, "post_run_bracket") or all(bracket):
        parts.extend((row.get("head_after"), row.get("tree_after"),
                      row.get("dirty_after")))
    parts.extend((ident.get("name"), ident.get("language"),
                  ident.get("executable")))
    argv = row.get("argv")
    # Frozen v1-v4 grammar: historical ids joined argv tokens and did not bind
    # the suite discriminator. V5 returned through its structured encoding
    # above.
    #
    # V6 REACHES THIS LINE AND IS NOT FROZEN, so the sentence this comment used
    # to end on ("reachable only for frozen ids") is no longer true and is not
    # worth pretending: the focused kind extends this grammar rather than v5's
    # because v5's structured payload has no `focus` section, and a kind whose
    # SCOPE is its entire claim may not be hashed by an encoding that drops it.
    # What the join costs a focused row is nothing that matters: the ambiguity
    # here is between argv token boundaries, and a focused argv is composed by
    # helm out of dotted module names that cannot contain a space — while the
    # selection itself is bound again, structurally, by `focus-v6` below. The
    # scope claim is therefore protected by a JSON encoding either way, and
    # moving the focused kind onto the v5 payload is a merge of two grammars
    # that owes its own reviewed land.
    parts.append(" ".join(argv or ()))
    parts.extend((row.get("status"), row.get("ran"), row.get("skipped")))
    # Original v1 receipts predate the post-run bracket and did not bind rc.
    # Bracketed v1 receipts bind it; v2 retains that grammar and additionally
    # binds both failure-diagnostic fields.
    if _version_has(row, "post_run_bracket") or all(bracket):
        parts.append(row.get("rc"))
    parts.append(row.get("repo_id"))
    # EACH VERSION KEEPS EVERY EARLIER VERSION'S BINDINGS. Spelling these as
    # `== 2` / `== 3` makes the id a per-version SET rather than a growing one,
    # so a v4 bump would silently stop binding the failure list and the
    # base-check verdict — a version bump that WEAKENS the hash is the exact
    # shape of defect this file exists to refuse.
    #
    # THE REGISTRY ANSWERS THIS NOW, and the answer is the same one the tuples
    # gave. `failure_identities` is introduced at 2 and `base_check` at 3, so
    # every version at or above them binds the field — including the kinds.
    # V5 never reaches this line (the structured early return above binds the
    # same three fields under named keys); it is still true of v5, and saying
    # so through the registry rather than through a restated tuple is the
    # whole point: a tuple that forgets the newest version is a hash that
    # silently stops binding something.
    if _version_has(row, "failure_identities"):
        parts.extend(("failure-identities-v2", json.dumps(
            row.get("failures"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")), row.get("failures_unreadable")))
    if _version_has(row, "base_check"):
        # The base-check verdict is a field a reader RELIES on — an edited
        # "STALE_BASE" pasted into a lane-owned receipt must stop resolving.
        parts.extend(("base-check-v3", json.dumps(
            row.get("base_check"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))))
    # v4 and up, which is 4, 6 and 8 among versions that reach this line —
    # v5 returned through its structured payload, which binds the same host
    # block under named keys.
    if _version_has(row, "host"):
        # The host is the third axis and is bound like the other two: an
        # edited node name must stop the receipt resolving exactly as an
        # edited status does.
        #
        # THIS BRANCH IS WHY THIS COMMIT EXISTS ALONE. Nothing here MINTS a v4
        # receipt — that is the next commit's job — and shipping the reader
        # first is deliberate. A helm that mints v4 before every helm can READ
        # v4 produces receipts its own `receipts()` integrity filter recomputes
        # to a different id and SILENTLY SKIPS, so `by_id` answers "no minted
        # gate receipt" about a row physically present in the ledger. Measured
        # on receipt 756b936006bf0e47: 685 rows read, skipped 1, and
        # the reviewer traced three functions to find out why their APPROVE
        # could not bind. Teach every reader first; flip the writer after.
        host_ident = _host_of(row)
        parts.extend(("host-v4", host_ident.get("node"),
                      host_ident.get("system"), host_ident.get("release"),
                      host_ident.get("id")))
    if row.get("v") == FOCUSED_VERSION:
        # THE SCOPE IS THE CLAIM on a focused receipt, so every part of it is
        # bound: an edited changed-list or a padded selection stops the row
        # resolving exactly as an edited status does. `suite` joins the hash
        # here because v6 is the first version where the flag separates two
        # receipt KINDS a consumer treats differently — at v4 it was derivable
        # from the hashed argv, at v6 it is load-bearing on its own.
        # (v6, NOT v5: 5 belongs to the cached-receipt kind, whose structured
        # payload above is a different grammar entirely — the number was taken,
        # and two grammars under one version would make every reader call the
        # other kind's honest rows tampered.)
        #
        # THE WHOLE BLOCK, INCLUDING THE RUNNER'S HALF. `focus` is dumped
        # entire, so the RAN set and its id count are bound exactly as the
        # planned selection is: the record of what a child reported is a fact a
        # later editor must not be able to improve.
        parts.extend(("focus-v6", row.get("suite"), json.dumps(
            row.get("focus"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))))
    if row.get("suite_command") is not None:
        # THE DECLARED COMMAND IS THE CLAIM when there is one, exactly as the
        # scope is on a focused receipt. `suite: true` on a v4 row was derivable
        # from the hashed argv — it WAS helm's frozen serial discovery command,
        # and `row_refusal` still requires that of a row with no declaration —
        # while for a declared run the whole-suite word means "the command THIS
        # PROJECT declares, whole". So the block saying which command, from
        # where, and how its verdict was read is bound here.
        #
        # BOUND BY PRESENCE AND NOT BY VERSION, deliberately. A row without the
        # block reaches none of this and hashes byte-identically to what it
        # always did, at every version; a row WITH it cannot have its protocol
        # or its argv edited without the id ceasing to resolve. The alternative
        # — a new version integer — would have had to sit above the v8 and v9
        # rungs and inherit their required fields (see RECEIPT_KIND_KEYS).
        parts.extend(("suite-command-v1", row.get("suite"), json.dumps(
            row.get("suite_command"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))))
    if _version_has(row, "failure_record"):
        # THE BOUNDED FAILURE RECORD (v8). The main row binds each
        # content-addressed identity chunk plus the exact display diagnostic
        # count, total and omitted count, so the full identity list is
        # protected by the receipt id even though it lives in sibling events.
        parts.extend(("failure-record-v8", row.get("failure_total"),
                      row.get("failure_diagnostics_omitted"), json.dumps(
                          row.get("failure_chunks"), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))))
    if _version_has(row, "sharded_authority"):
        # V9 binds token boundaries, the scope discriminator and every derived
        # evidence fact. Receipts without this field keep their distinct grammar.
        parts.extend(("sharded-authority-v9", row.get("suite"), json.dumps(
            {"argv": argv, "interpreter": row.get("interpreter"),
             "authority": row.get("sharded_authority")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    else:
        payload = "\0".join(str(x) for x in parts)
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def receipt_room(row):
    """The BASENAME of the checkout the run happened in: `helm` for the shared
    tree, a lane name for a private room, a sha12 for a peek.

    THE BASENAME AND NEVER THE PATH. The full path is in `repo_id` and prints
    under `gate show`; putting it on the line that travels would put
    `/home/<user>/...` into commit messages and chat, which is precisely the
    shape `hostpath_guard` refuses on a public push. The basename carries the
    only distinction a reader needs — shared checkout, my room, or a peek —
    and carries no host path at all."""
    repo = _failure_text(row.get("repo_id"))
    return os.path.basename(repo.rstrip("/")) if repo else ""


def _measured_bool(value):
    """True/False only when the field IS a bool. Anything else is UNKNOWN.

    A receipt field can be absent, null, a string from a hand-edited row, or a
    sentinel a WRITER coerced for its own reader. A display that runs any of
    those through `bool()` reports a measurement it never had."""
    return value if type(value) is bool else None


def _pre_run_dirt(row):
    """(state, why) for the worktree BEFORE the run. None state is UNKNOWN."""
    if "dirty" not in row:
        return None, "the receipt carries no pre-run dirt field"
    state = _measured_bool(row.get("dirty"))
    return state, None if state is not None else \
        "the pre-run dirt field is not a measurement"


def _post_run_dirt(row):
    """(state, why) for the worktree AFTER the run. None state is UNKNOWN.

    THE MINT COERCES A FAILED READ TO True. `_mint_result` writes
    `"dirty_after": True if after_err else after_dirty` — fail-closed, and
    right for `bind`, whose job is to REFUSE what it cannot verify. A DISPLAY
    that inherits that sentinel prints "measured dirty" about a read that never
    completed, which is a stronger claim than silence and a worse one: the
    reader goes looking for edits nobody ever saw.

    THE RECEIPT CARRIES THE DISCRIMINATOR even though it never records
    `after_err` itself. On that same error path `tree_state` returns
    (None, None, None, err), so `head_after` and `tree_after` are BOTH empty —
    a present-but-empty post-run head IS the failed read. A row missing all
    three keys is a pre-bracket receipt whose after-state was never read at
    all: also UNKNOWN, for the other reason, and the two reasons are printed
    apart because "the read failed" and "there was no read" send a reader to
    different places."""
    if not any(key in row for key in
               ("dirty_after", "head_after", "tree_after")):
        return None, "the receipt predates the post-run read"
    if not row.get("head_after") or not row.get("tree_after"):
        return None, "the post-run read did not complete"
    state = _measured_bool(row.get("dirty_after"))
    return state, None if state is not None else \
        "the post-run dirt field is not a measurement"


def dirt_clause(row):
    """What this receipt SHOWS about uncommitted content, or None when both
    reads measured clean.

    THREE STATES PER HALF, NEVER TWO. A renderer that reads `row["dirty"]`
    alone calls a suite that DIRTIED the tree it ran on clean, which is the
    state `bind` refuses; and a renderer that reads `dirty_after` as a plain
    boolean reports the mint's fail-closed sentinel — set on a post-run read
    that ERRORED — as measured dirt, which is a stronger claim than silence
    and sends a reader hunting edits nobody saw.

    THE PROSE NAMES THE RIGHT SUBJECT. `row["tree"]` is HEAD^{tree}, a
    commit's tree, which that commit carries by construction; so no clause
    here may say no commit carries it. The dirt is the DELTA between that
    recorded tree and what the suite ran on, and the delta is what no commit
    names.

    So: name the halves measured dirty, name the halves not measured at all,
    and say nothing about the halves measured clean."""
    halves = (("before",) + _pre_run_dirt(row),
              ("after",) + _post_run_dirt(row))
    dirty = [half for half, state, _why in halves if state is True]
    unknown = [(half, why) for half, state, why in halves if state is None]
    said = []
    if dirty:
        said.append("DIRTY WORKTREE (%s the run) — the suite measured "
                    "UNCOMMITTED content; `tree=` above is HEAD's tree, not "
                    "what ran, and this receipt CANNOT bind"
                    % " and ".join(dirty))
    if unknown:
        said.append("DIRT UNKNOWN (%s the run) — %s, so this receipt does not "
                    "show the worktree held still"
                    % (" and ".join(half for half, _why in unknown),
                       "; ".join(dict.fromkeys(why for _half, why in unknown))))
    return " | ".join(said) or None


# THE THREE ANSWERS TO "DOES THIS RECEIPT COVER THAT COMMIT". A reader that
# collapses UNKNOWN into NO refuses work it never measured; one that collapses
# it into YES vouches for work it never measured. Both have happened here.
CARRIED = "carried"
NOT_CARRIED = "not-carried"
CARRIAGE_UNKNOWN = "carriage-unknown"


def carriage(repo, reviewed, candidate):
    """-> (verdict, relation, sequence): does `candidate` carry `reviewed`?

    THE ONE PREDICATE, because two readers depend on this exact question:
    `bind` decides whether a receipt may authorize a land, and
    `gateimport.head_divergence` decides whether an author is owed a warning.
    A TREE COMPARISON is not this question. A fab snapshot S and an author
    commit C built from the same content are SAME-TREE SIBLINGS — identical
    trees, neither carrying the other — so a tree test calls them equal while
    this predicate answers NOT_CARRIED and `bind` refuses. Two renderings of
    one fact drift apart; two PREDICATES for one fact contradict each other,
    and the contradiction shows up as a warning door that is silent in exactly
    the case it exists for.

    ANCESTRY FIRST, THEN THE PATCH SEQUENCE, which is the order `bind` has
    always used: a descendant carries its ancestor by construction, and a
    rebased or cherry-picked train carries it as an unambiguous contiguous run
    of patch ids. `sequence` is the 4-tuple the caller needs to explain
    itself, or None when ancestry answered without asking."""
    backend = vcs.backend(repo)
    relation = backend.ancestry(repo, reviewed, candidate)
    if relation == vcs.ANCESTOR:
        return CARRIED, relation, None
    if relation != vcs.NOT_ANCESTOR:
        return CARRIAGE_UNKNOWN, relation, None
    sequence = backend.patch_sequence_containment(repo, reviewed, candidate)
    state = sequence[0]
    if state in (vcs.PATCH_SEQUENCE_EXACT, vcs.PATCH_SEQUENCE_CONTAINED):
        return CARRIED, relation, sequence
    # AN UNDERIVABLE SEQUENCE IS NOT AN ABSENT ONE. `bind` refuses both, and
    # is right to — it must not authorize what it cannot verify — but a
    # DISCLOSURE that told an author "this receipt does not cover your commit"
    # on a read that never completed would be inventing a measurement.
    if state == vcs.PATCH_SEQUENCE_UNKNOWN:
        return CARRIAGE_UNKNOWN, relation, sequence
    return NOT_CARRIED, relation, sequence


def evidence_line(row):
    """The one canonical string a reviewer pastes into `dispatch verdict`.

    The adopted shape puts the binding token first: everything after the
    token is for a human to read, and nothing after it is trusted."""
    ident = _ident_of(row)
    declared = row.get("suite_command")
    if isinstance(declared, dict) and declared.get("protocol") == PROTOCOL_EXIT:
        # A DECLARED `exit` RUN HAS NO COUNT TO RENDER, and `Ran ?` is the wrong
        # sentence for it: the `?` means "I could not read the number", which is
        # exactly what a reader must not conclude here. The runner reports no
        # count and the receipt says so, so the line names the command and its
        # exit status — the two facts the verdict actually rests on.
        rc = row.get("rc")
        counts = "exit %s (%s)" % (
            rc if type(rc) is int else "?",
            " ".join(str(a) for a in (declared.get("argv") or ()))[:48]
            or "declared command")
    else:
        ran = row.get("ran")
        ran = ran if type(ran) is int and ran >= 0 else "?"
        counts = "Ran %s" % ran
        skipped = row.get("skipped")
        if type(skipped) is int and skipped > 0:
            counts += " (skipped=%d)" % skipped
    if row.get("suite"):
        scope = "whole-suite"
    elif _focus_of(row):
        # The ratio a reviewer would otherwise dig out of `gate show`: how
        # many of the repo's test modules this run actually RAN. The
        # numerator is the runner-measured set, never the selection — the
        # line a reviewer reads must state the run, and a selection that
        # asked for six modules while four reported tests is exactly the
        # discrepancy this number exists to make visible.
        focus = _focus_of(row)
        ran_mods = focus.get("executed")
        uni = focus.get("universe")
        scope = "focused %s/%s" % (
            len(ran_mods) if isinstance(ran_mods, list) else "?",
            uni if type(uni) is int else "?")
    else:
        scope = "custom"
    rid = _failure_text(row.get("id")) or "?"
    tree = (_failure_text(row.get("tree")) or "?")[:12]
    status = _failure_text(row.get("status")) or "UNKNOWN"
    line = "gate:%s | %s | host=%s | tree=%s | room=%s | %s | %s %s" % (
        rid, interpreter_label(ident), host_label(_host_of(row)), tree,
        receipt_room(row)[:_LABEL_CAP] or "?", scope, counts, status)
    # THE DIRT RIDES THE LINE THAT TRAVELS, because the fact that invalidates
    # a green is the one fact this line did not carry. Every other surface
    # said so somewhere the author was not looking: `fab gate` prints its
    # snapshot notice at the START of a five-to-ten minute run, `bind` refuses
    # on stderr AFTER this line has already printed green on stdout, and
    # `_fmt` bolted a bare " DIRTY" onto `gate list` alone. This is the one
    # string printed at the end of a run, pasted into `dispatch verdict`, and
    # re-read out of `gate show` days later — the only placement that reaches
    # a reader with no scrollback. `_fmt`'s flag is dropped in the same
    # breath: two renderings of one fact is how they drift apart.
    dirt = dirt_clause(row)
    if dirt:
        line += " | " + dirt
    # The deciding words ride the SAME line a reviewer reads — the incident
    # that motivated base_check was a receipt that held the answer and said
    # only FAILED. A lane-owned failure adds nothing: FAILED already means
    # exactly that.
    check = row.get("base_check")
    if isinstance(check, dict):
        if check.get("verdict") == STALE_BASE:
            line += " | STALE BASE — failures predate this lane; trunk %s " \
                "passes them even with this lane applied" \
                % (_failure_text(check.get("trunk")) or "?")[:12]
        elif check.get("verdict") == BASE_UNKNOWN:
            line += " | base-check UNKNOWN"
    return line


# ------------------------------------------------------------- fleet cap
#
# Concurrent whole-suite activity can coincide with resource pressure and
# loss of agent panes on a pane host. The exact causal mechanism was not proven,
# and Orca panes may span daemon generations rather than share one daemon. The
# conservative cap is TWO on boxes that carry panes, and `suite_cap` derives
# that cap per host. No lock can carry it:
# `helm gate run` claims a FIFO position and the legacy
# gatelock, but a bare `python3 -m unittest discover` touches neither, so a
# cap counted over lock-holders is a cap that does not exist. Admission
# therefore counts PROCESSES ACTUALLY RUNNING A SUITE — what a process IS
# (its argv, read off /proc), never what it holds. A bare run can still
# START outside helm's reach, but it cannot be invisible: every suite on the
# box occupies a slot, and the gate refuses to pile on top of it.

SUITE_CAP = 2

# ---------------------------------------------------------- per-host cap
#
# GATE CONCURRENCY IS PER-HOST, NOT GLOBAL. The resources it protects — memory,
# cores, and pane-serving processes — are per-BOX, so one number for every box
# necessarily fits none of them. Whole-suite runs route to the fab. Paneless
# build hosts derive a larger cap because they carry no irreplaceable pane
# context.
#
# Measurements put an ordinary suite's resident memory far below build-host
# headroom, so suite RSS alone cannot explain the pane-host outage. Page-cache
# churn from many temporary repositories, I/O contention, and process-count
# thrash remain plausible correlated load; the exact cause is unproven.
# `/usr/bin/time -v` also reports max RSS only across the process tree it waits
# on, so a test-spawned daemon that outlives that tree is absent from the
# reading: treat the measurement as a floor, not a ceiling. Even so, it would
# need to be wrong by orders of magnitude to justify a cap of two on a build
# host.
#
# SO THE COUNT IS NOT DERIVED FROM RAM, BECAUSE RAM IS NOT THE BINDING
# RESOURCE. That is the answer rather than a gap: the comment below already
# says the count is a PROXY and that PSI is the real, kernel-measured guard.
# This raises the proxy on boxes with nothing to lose and leaves PSI — and the
# pane-host cap of two — exactly as protective as they are today.
#
# THE PREDICATE IS "DOES THIS BOX CARRY PANES", NOT A HOSTNAME. A hostname
# allowlist rots the moment a host is added or renamed, and it encodes the
# wrong fact. The pane-host cap protects irreplaceable agent context when
# pane-serving processes fail. A build host running nothing but suites has no
# such casualty.

BUILD_HOST_SUITE_CAP_MAX = 16
# OWNER POLICY, NOT A THROUGHPUT MEASUREMENT: bound a paneless host at sixteen
# and budget two ONLINE cores per suite. Memory PSI remains the starvation
# guard. Fab's independent 12-slot per-host semaphore currently bounds routed
# gates below this maximum; concurrent I/O/PID safety is separate evidence.
_CORES_PER_SUITE = 2


def _agent_pane_pids(proc_dir=None):
    """PIDs on THIS box that are live agent panes. -> [pid] or None.

    DELEGATES TO `beacons.agent_index`, WHICH IS HELM'S CANONICAL AGENT
    DETECTOR, instead of scanning /proc a second time. This function
    previously hand-rolled that scan by matching HELM_CHAT_NAME in each
    environ, and every defect it shipped came from re-deriving something helm
    already knew how to do:

      * a stamp past a 64 KB read was invisible (a real pane read as absent);
      * an unreadable environ was first dropped, then counted as a pane —
        and counting it made the whole per-host cap INERT, because a build
        host is mostly other people's protected processes (measured on trunk:
        two build hosts counted 434 and 487 "panes", so neither ever left cap 2);
      * an inherited stamp counted CHILDREN as panes, inflating this box from
        its true ~13 agents to 47.

    `beacons.is_agent(argv, comm)` decides on argv/comm — which stay readable
    when environ does not — and `agent_index` deliberately keeps a pane whose
    environ it cannot read rather than dropping it. So the unreadable-environ
    case is handled at the layer that owns it, by evidence that does not
    disappear.

    THE FAIL-CLOSED ARM SURVIVES AND IS NOW THE ONLY ONE: `agent_index`
    returns None when the process table itself cannot be listed, and
    `suite_cap` reads that as the pane cap. That is refusing to answer, which
    is different in kind from answering "no agents here" — the distinction the
    hand-rolled version kept losing.

    (Existence sweep is the lesson: this primitive, its process-class tests
    and its unreadable-environ contract all predated the hand-rolled scan this
    function once carried.)
    """
    from . import beacons
    index = beacons.agent_index(proc_dir)
    if index is None:
        return None                  # unlistable process table: UNKNOWN
    return sorted(index.get("by_pid") or ())


def _online_cpu_count():
    """Machine-wide ONLINE CPU count from sysconf, or None.

    `nproc` answers for the caller's affinity mask and a subprocess probe can
    convoy every launcher while admission holds its flock. SC_NPROCESSORS_ONLN
    is the kernel-backed machine count, available in-process and independent of
    caller affinity. An unavailable or malformed answer fails closed.
    """
    try:
        cores = os.sysconf("SC_NPROCESSORS_ONLN")
    except (OSError, ValueError):
        return None
    return cores if type(cores) is int and cores > 0 else None


def _capacity_topology():
    """Captured BOX-WIDE online CPU capacity, outside admission."""
    cores = _online_cpu_count()
    return {"cores": cores,
            "reason": "%d host-online CPUs" % cores if cores is not None
            else "host online CPU count is unavailable"}


_CAPACITY_UNSET = object()


def _capacity_grant(proc_dir, topology):
    """Derive the effective grant from live panes and captured topology."""
    panes = _agent_pane_pids(proc_dir)
    if panes is None:
        return {"cap": SUITE_CAP,
                "reason": "capacity unknown: process table is unreadable"}
    if panes:
        return {"cap": SUITE_CAP, "reason": "carries agent panes"}
    if topology["cores"] is None:
        return {"cap": SUITE_CAP,
                "reason": "paneless host; %s" % topology["reason"]}
    cap = max(SUITE_CAP, min(
        BUILD_HOST_SUITE_CAP_MAX, topology["cores"] // _CORES_PER_SUITE))
    return {"cap": cap,
            "reason": "paneless build host, %s" % topology["reason"]}


def suite_capacity(proc_dir=None, topology=_CAPACITY_UNSET):
    """One structured capacity snapshot for non-admission callers."""
    if topology is _CAPACITY_UNSET:
        topology = _capacity_topology()
    return _capacity_grant(proc_dir, topology)


def suite_cap(proc_dir=None):
    """Compatibility scalar for callers that need only the cap."""
    return suite_capacity(proc_dir)["cap"]


# The count is a PROXY: a few enormous runs can starve a box as readily as many
# ordinary ones. The floor is the kernel's own starvation measurement — PSI,
# /proc/pressure/memory `some avg10`, the percentage of the last 10s in which at
# least one task was stalled on memory. The threshold is DERIVED, not guessed:
# the pane-host outage included systemd-journald's "Under memory pressure,
# flushing caches." warning and failures in pane-serving processes. That makes
# pressure a correlated warning, not proof of the exact cause. On this box the
# watch fires at
# MemoryPressureThresholdUSec = 200ms of some-stall per systemd's 2s watch
# window (measured via `systemctl show systemd-journald -p
# MemoryPressureThresholdUSec`), i.e. a 10% some-stall fraction. avg10 sustains
# that same fraction over a 10s window, so the floor is no earlier than the
# system watch. A box already stalling 10% of the time on memory reclaim is the
# condition this cap exists to protect, and another whole tree can compound it.

PSI_SOME_FLOOR = 10.0

_SUITE_INTERP = re.compile(r"(?:python|graalpy|pypy)[\d.]*\Z")
# Only interpreter flags that cannot consume or reinterpret the next token may
# precede the module runner in an authority or capacity classification.
_PY_HARMLESS_FLAGS = frozenset(("-B", "-E", "-I", "-P", "-s", "-S", "-u"))
# Interpreter options that consume the NEXT argv slot before `-m`.
_PY_VALUE_FLAGS = frozenset(("-W", "-X", "--check-hash-based-pycs"))
_UNITTEST_RUN_FLAGS = frozenset((
    "-v", "--verbose", "-q", "--quiet", "--locals", "-f", "--failfast",
    "-c", "--catch", "-b", "--buffer",
))
_DISCOVERY_VALUE_FLAGS = {
    "-s": "start", "--start-directory": "start",
    "-p": "pattern", "--pattern": "pattern",
    "-t": "top", "--top-level-directory": "top",
}
# The command modules a STORED whole-suite receipt may name, and the whole set.
# `-m unittest` is what `gate.run` spawns and the only runner this tree mints a
# whole-suite receipt with. `helm.gaterunner` is a LEGACY NAME, ACCEPTED AND
# NEVER PRODUCED: an in-process runner of that name lived only on a lane that
# never merged, and the whole-suite receipts it minted there stay in the ledger
# they were written to. No module of that name ships, so nothing on this tree
# can mint another; dropping the name would silently change what those stored
# rows arm and discharge at the seam rung (helm/work/_gc.py), which is the only
# reader of this set. tests/test_gateshard.py holds both halves. A receipt
# naming anything else was produced by a command nobody can vouch runs this
# tree's tests, so its whole-suite CLAIM has no runner behind it.
_STORED_SUITE_RUNNERS = frozenset(("unittest", "helm.gaterunner"))
_PSI_SOME = re.compile(r"^some .*\bavg10=(\d+(?:\.\d+)?)", re.M)


def _census_proc_dir(proc_dir=None):
    return proc_dir or home.env("PROC") or "/proc"


def _module_runner(argv, combined=False):
    """Return ``(module, tail)`` for one narrowly anchored python ``-m``."""
    argv = [str(a) for a in (argv or ())]
    if len(argv) < 2 or not _SUITE_INTERP.fullmatch(os.path.basename(argv[0])):
        return None, ()
    i = 1
    while i < len(argv) and argv[i] in _PY_HARMLESS_FLAGS:
        i += 1
    if i >= len(argv):
        return None, ()
    if argv[i] == "-m":
        return (argv[i + 1] if i + 1 < len(argv) else None, argv[i + 2:])
    if combined and argv[i].startswith("-m") and len(argv[i]) > 2:
        return argv[i][2:], argv[i + 1:]
    return None, ()


def _suite_shaped(argv, runner="unittest"):
    """Is this argv the exact authoritative serial discovery contract?

    The host-cap classifier deliberately accepts more documented discovery
    spellings below. Landing authority does not: filters, alternate roots and
    implicit discovery can omit tests while still consuming suite-scale RAM.
    Stored-receipt callers pass ``runner=None`` to admit the historical
    ``helm.gaterunner discover`` module without reopening arbitrary runners.
    """
    module, tail = _module_runner(argv)
    admitted = _STORED_SUITE_RUNNERS if runner is None else {runner}
    if module not in admitted:
        return False
    if module == "unittest":
        return tuple(tail) == tuple(SUITE[2:])
    return module == "helm.gaterunner" and tuple(tail) in {
        ("discover",), tuple(SUITE[2:]),
    }


def _unittest_cap_tail(tail):
    """Positive grammar for documented full-discovery unittest argv tails."""
    args = list(tail)
    discover = False
    seen = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _UNITTEST_RUN_FLAGS:
            i += 1
            continue
        if arg == "-k":
            if i + 1 >= len(args) or str(args[i + 1]).startswith("-"):
                return False
            i += 2
            continue
        if arg.startswith("-k") and len(arg) > 2:
            i += 1
            continue
        if not discover and arg == "discover":
            discover = True
            i += 1
            continue
        option = None
        if discover and arg in _DISCOVERY_VALUE_FLAGS:
            if i + 1 >= len(args):
                return False
            option = (_DISCOVERY_VALUE_FLAGS[arg], args[i + 1], 2)
        elif discover:
            for flag, kind in _DISCOVERY_VALUE_FLAGS.items():
                prefix = flag + "=" if flag.startswith("--") else flag
                if arg.startswith(prefix) and len(arg) > len(prefix):
                    option = (kind, arg[len(prefix):], 1)
                    break
        if option:
            kind, value, consumed = option
            if kind in seen or not value:
                return False
            seen[kind] = value
            i += consumed
            continue
        return False
    return (not discover or seen.get("start", "tests") in (".", "tests")) \
        and seen.get("top", ".") == "." \
        and seen.get("pattern", "test*.py") == "test*.py"


def _suite_cap_shaped(argv):
    """Does this process consume a whole-suite host-cap slot?"""
    if _suite_shaped(argv):
        return True
    module, tail = _module_runner(argv, combined=True)
    return module == "unittest" and _unittest_cap_tail(tail)


def _diagnostic_shard_shaped(argv):
    """Does argv run gateshard at suite scale, without granting authority?"""
    argv = [str(a) for a in (argv or ())]
    if len(argv) != 2 or not _SUITE_INTERP.fullmatch(
            os.path.basename(argv[0])) or not os.path.isabs(argv[1]):
        return False
    return os.path.normpath(argv[1]).split(os.sep)[-2:] == [
        "helm", "gateshard.py",
    ]


def _suite_cgroup_position(pid, proc_dir):
    """The helm-gate FIFO position whose cgroup holds this pid, or None."""
    try:
        with open(os.path.join(proc_dir, str(pid), "cgroup")) as f:
            row = next(line for line in f if line.startswith("0::"))
    except (OSError, StopIteration):
        return None
    name = os.path.basename(row.split("::", 1)[1].strip())
    prefix = gatechild._CGROUP_PREFIX
    return name[len(prefix):] if name.startswith(prefix) else None


def _proc_ppid(pid, proc_dir):
    """Parent pid from /proc/<pid>/stat, or None. comm (field 2) can itself
    hold spaces or parens, so split AFTER the last ')' — then field 3 (state)
    and field 4 (ppid) are the first two tokens (same idiom as _proc_start)."""
    try:
        with open(os.path.join(proc_dir, str(pid), "stat"),
                  encoding="utf-8") as f:
            stat = f.read()
    except (OSError, ValueError):
        return None
    tail = stat.rpartition(")")[2].split()
    try:
        return int(tail[1])
    except (IndexError, ValueError):
        return None


def _suite_root_kind(argv):
    """Classify a process argv as a suite-shaped ROOT the census must SEE.

      "suite"    authoritative unittest discovery or diagnostic gateshard —
                 both consume a whole-suite host-cap slot, while only the
                 former satisfies `_suite_shaped` landing authority.
      "targeted" a single-method/class unittest run
                 (`python3 -m unittest pkg.mod.Case.test_x`).
      "pytest"   any pytest run.
      None       not a suite root (a hook, a plain script, `-c` inline code);
                 such a process is only ever seen as a CHILD of a root.

    The whole-tree predicate was BLIND to "targeted" and "pytest": the owner's
    screenshot showed an approved-shape single-method run reach 3.6G RSS, so
    "narrow" bounds the TEST COUNT and never bounded COST (task/923). They are
    made VISIBLE here so their spawned children and their cost land on the
    record and can be handed to the fab. They do NOT count toward the
    whole-suite concurrency cap — that cap is a proxy for concurrent DISCOVERY
    runs (see gate.py:1211), and a widened count would silently re-price it;
    the enforce-on-cost decision is its own follow-on row."""
    if _suite_cap_shaped(argv) or _diagnostic_shard_shaped(argv):
        return "suite"
    argv = [str(a) for a in (argv or ())]
    if len(argv) < 2 or not _SUITE_INTERP.fullmatch(os.path.basename(argv[0])):
        return None
    i, module = 1, None
    while i < len(argv):
        arg = argv[i]
        if arg == "-m":
            module = argv[i + 1] if i + 1 < len(argv) else None
            break
        if arg.startswith("-m") and len(arg) > 2:
            module = arg[2:]
            break
        if arg in _PY_VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        break                     # a script path, not `-m <runner>` — no root
    if module == "unittest":
        return "targeted"         # not whole-tree (that returned "suite")
    if module in ("pytest", "py.test"):
        return "pytest"
    return None


def _proc_subtree(root, kids):
    """Depth-first descendant pids of root, sorted, cycle-safe."""
    out, seen, stack = [], {root}, list(kids.get(root, ()))
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        out.append(child)
        stack.extend(kids.get(child, ()))
    return sorted(out)


def _proc_ancestors(pid, proc_dir):
    """Immediate-parent-first pid lineage from one live process table."""
    out, seen = [], {pid}
    cur = _proc_ppid(pid, proc_dir)
    while cur is not None and cur not in seen:
        seen.add(cur)
        out.append(cur)
        cur = _proc_ppid(cur, proc_dir)
    return out


def suite_census(proc_dir=None):
    """Every suite OWNER on the box RIGHT NOW, with its whole process tree.

    -> [{pid, argv, cwd, kind, children, position, owner}] sorted by pid, or
    None when the process table itself is unreadable — an unreadable census
    must widen to a refusal upstream, never narrow to 'nothing is running'.

    ONE ADMISSION IS ONE OCCUPANT. A diagnostic runner may launch sibling shard
    processes in the same helm-gate cgroup, and a test may launch a nested gate
    whose child gets a new containment cgroup. Neither creates another host
    owner: same-position roots and suite roots below another suite root collapse
    onto the top admitted owner. A bare suite has no position, so its root pid is
    its owner. `children` contains every process below every collapsed root,
    including the other suite-shaped roots, so the process cost stays visible
    without multiplying occupancy.

    Independent "targeted"/"pytest" roots remain visible but do not count. If
    one is already below a suite owner it appears only in that owner's children;
    rendering it as a second root would describe one process tree twice."""
    proc_dir = _census_proc_dir(proc_dir)
    try:
        names = os.listdir(proc_dir)
    except OSError:
        return None
    argvs, kids, parents = {}, {}, {}
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(os.path.join(proc_dir, name, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        argvs[pid] = [a.decode("utf-8", "replace") for a in raw.split(b"\0")
                      if a]
        ppid = _proc_ppid(pid, proc_dir)
        if ppid is not None:
            parents[pid] = ppid
            kids.setdefault(ppid, []).append(pid)
    me = os.getpid()
    kinds = {pid: _suite_root_kind(argv) for pid, argv in argvs.items()
             if pid != me}
    suites = {pid for pid, kind in kinds.items() if kind == "suite"}
    positions = {pid: _suite_cgroup_position(pid, proc_dir) for pid in suites}

    def ancestors(pid):
        out, seen = [], {pid}
        cur = parents.get(pid)
        while cur is not None and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = parents.get(cur)
        return out

    def top_suite(pid):
        top = pid
        for cur in ancestors(pid):
            if cur in suites:
                top = cur
        return top

    groups = {}
    for pid in suites:
        top = top_suite(pid)
        position = positions.get(top)
        key = ("position", position) if position else ("pid", top)
        groups.setdefault(key, []).append(pid)

    rows = []
    owned = set()
    for key, roots in groups.items():
        roots = sorted(roots)
        top = top_suite(roots[0])
        representative = top if top in roots else roots[0]
        descendants = set(roots)
        for root in roots:
            descendants.update(_proc_subtree(root, kids))
        descendants.discard(representative)
        try:
            cwd = os.readlink(os.path.join(
                proc_dir, str(representative), "cwd"))
        except OSError:
            cwd = "?"
        owner = key[1] if key[0] == "position" else "pid:%d" % key[1]
        rows.append({
            "pid": representative,
            "argv": argvs[representative],
            "cwd": cwd,
            "kind": "suite",
            "children": [{"pid": child, "argv": argvs.get(child, [])}
                         for child in sorted(descendants)],
            "position": key[1] if key[0] == "position" else None,
            "owner": owner,
            # Admission uses this private lineage only when cgroup identity is
            # absent: a live queue launcher above the root still proves which
            # pending token this already-running bare owner covers.
            "_ancestors": ancestors(representative),
        })
        owned.update(descendants)
        owned.add(representative)

    for pid, kind in kinds.items():
        if kind not in ("targeted", "pytest") or pid in owned:
            continue
        try:
            cwd = os.readlink(os.path.join(proc_dir, str(pid), "cwd"))
        except OSError:
            cwd = "?"
        rows.append({
            "pid": pid,
            "argv": argvs[pid],
            "cwd": cwd,
            "kind": kind,
            "children": [{"pid": child, "argv": argvs.get(child, [])}
                         for child in _proc_subtree(pid, kids)],
            "position": None,
            "owner": "pid:%d" % pid,
        })
    return sorted(rows, key=lambda row: row["pid"])


def _psi_some_avg10(proc_dir=None):
    """The kernel's memory some-stall percentage over the last 10s, or None.

    Read under the explicit census proc root. Direct diagnostics/tests may use
    HELM_PROC through `_census_proc_dir`; host admission passes `/proc` itself
    and cannot be redirected by a child environment. None (CONFIG_PSI=n, psi=0,
    a fake proc tree) SKIPS the floor rather than refusing: the census cap still
    stands, and a box without PSI must not lose its gate — but the skip is a
    weaker guard,
    not an equivalent one."""
    path = os.path.join(_census_proc_dir(proc_dir), "pressure", "memory")
    try:
        with open(path) as f:
            hit = _PSI_SOME.search(f.read())
    except OSError:
        return None
    return float(hit.group(1)) if hit else None


def _admissions_path(runtime_root=None):
    """The node-local admission ledger shared by every Helm home on this host.

    Fab deliberately gives each run a private HELM_HOME so receipts and other
    mutable Helm state cannot collide. Admission is the exception: the protected
    resource is the BOX, and a lock below private HELM_HOME serializes nothing
    across concurrent runs. Use the boot-scoped host RAM root plus uid so every
    repo and run by this account reaches one node-local door; another node has a
    different filesystem and therefore a different cap.

    `runtime_root` is an internal test seam, never an environment override. A
    caller cannot buy a private cap by steering configuration away from the host
    owner path.
    """
    root = home.ram_root() if runtime_root is None else runtime_root
    return os.path.join(root, _ADMISSIONS_NAME % os.geteuid())


_ADMISSIONS_NAME = "helm-gate-admissions-%d.json"


def _host_admissions_path():
    """THIS BOX's ledger, by construction — what `_admissions_path` answers
    with no seam. Spelled out rather than CALLED, because a test that points
    `gate._admissions_path` at a fixture patches that NAME, and the host audit
    below must still recognise the host's own path."""
    return os.path.join(home.ram_root(), _ADMISSIONS_NAME % os.geteuid())


def _admissions_load(path):
    """Well-formed intent rows only. This is an OPERATIONAL ledger — every
    row is re-validated against a live process before it counts, so a
    malformed row is dropped rather than wedging admission the way one
    malformed receipt once wedged all receipt lookup."""
    raw = pk.read_json(path, None)
    rows = raw.get("admissions") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("position"), str) \
                and row["position"] and type(row.get("pid")) is int \
                and type(row.get("starttime")) is int \
                and isinstance(row.get("holder"), str) \
                and isinstance(row.get("ts"), str):
            out.append(row)
    return out


_FAB_HANDOFF = (
    "Run it on the fab instead — `fab gate --repo .` mints the suite on a "
    "build node and auto-imports the receipt into the binding ledger (#167); "
    "for a non-gate command, `fab build --repo . -- <cmd>`. The work still "
    "runs — just not on the box that has to stay typeable.")

#: THE EXIT FOR "NOT RUN: CAPACITY" (task/1740). A red suite exits 1 and so,
#: until this, did a run this node refused to start — the whole-suite cap, the
#: memory-stall floor, a pressured tmp — so a caller reading the exit code could
#: not tell "the tree failed" from "the box was full". Neither mints a receipt;
#: only the second is cured by waiting or by another node. 3 is the gate
#: family's existing not-run code (`helm gate window launch` exits 3 when a
#: running suite holds the window), and nothing that consumes `helm gate run` or
#: `fab gate` gives it another meaning: unittest exits 0/1/2/5, helm's usage
#: errors exit 2, and fab's own exits (as its deployed fab-* scripts write
#: them) are 1, 2, 75 (a detached follower), 92-99 (queue timeout,
#: superseded, scheduler, RAM, disk, snapshot, process lost, operator kill),
#: 124, 125, 127 and 143 — none of them 3. `fab gate` passes this exit through,
#: but spills to another node only on the two admission refusals, whose
#: wording (`whole-suite cap is`, `already stalling on memory`) its spoke
#: matches; a pressured-tmp NOT RUN is reported there, not spilled, until fab
#: keys its admission bit on this exit.
EXIT_NOT_RUN_CAPACITY = 3


class CapacityRefusal(str):
    """A refusal about THIS NODE's capacity, never about the tree. -> str

    It IS the refusal sentence, so every caller that prints, joins or compares
    the error keeps working unchanged; only a caller that maps the refusal to an
    exit code asks `isinstance`. A caller that rebuilds the text drops the type
    and falls back to exit 1 — the old answer, and never a green one."""

    kind = "capacity"


#: THE HOST'S OWN PROCESS TABLE. Admission reads this and nothing else; see
#: the note at the top of `_admit_suite`.
HOST_PROC = "/proc"
#: Raised through sys.audit immediately before an admission touches THIS box:
#: its /proc, its node-wide admission ledger, or its tmp mount (the scratch
#: preflight), with (proc_dir, path). Nothing in helm listens.
#: tests/__init__.py refuses it in a test process that did not ask, because a
#: test that reads the node's live cap is red or green by what else the node is
#: running. There is deliberately NO switch that answers the event instead: "no
#: override exists for this cap" is the law the refusal itself states.
HOST_ADMISSION_EVENT = "helm.gate.host_admission"


def _audit_host_admission(proc_dir, path):
    """Raise HOST_ADMISSION_EVENT when (proc_dir, path) is this box. EITHER is
    enough: the host's /proc counts the host's suites, and the host's ledger
    holds the host's pending owners whatever process table judges them — a
    fixture /proc beside the host ledger would retire the host's live rows."""
    host = proc_dir is not None \
        and os.path.normpath(str(proc_dir)) == HOST_PROC
    if not host and path is not None:
        try:
            host = os.path.normpath(path) \
                == os.path.normpath(_host_admissions_path())
        except (OSError, RuntimeError):
            host = False                  # no host ledger exists to touch
    if host:
        sys.audit(HOST_ADMISSION_EVENT, proc_dir, path)


def _scratch_preflight(usage=None):
    """None when this node's tmp can host a suite, else the refusal. -> str

    THE MEASURED DEFECT: a build node's /tmp — a tmpfs capped at
    nr_inodes=1048576 — reached 100% inodes and three whole-suite receipts
    minted FAILED with Errno 28 tracebacks about trees that were green. A receipt is a claim about a TREE; one minted on a mount that
    cannot take a mkdtemp is a claim about the node, and every reader
    downstream (bind, base-check, the reviewer) reads it as the tree's.

    So the node is measured BEFORE the FIFO, before the child, before the
    in-flight marker: the mount the suite will mint under (the ambient tmp —
    what `tempfile` resolves here, which is what the child inherits) on BOTH
    axes, and at or over `scratch.WARN_PCT` the run is REFUSED with no
    receipt. The refusal names the mount, both percentages, the threshold and
    the cure. Same reader as `helm scratch status` (`scratch.usage`,
    `scratch.worst_pct`), never a second census: one number, one threshold,
    the doctor's warn IS the gate's refusal.

    UNMEASURABLE IS NOT PRESSURE. `scratch.usage` returns None for a path it
    cannot statvfs, and the survey reads that as unmeasured rather than as a
    warning; the gate takes the same posture, because a refusal here has to
    be about a number the operator can go and read.

    THE THIRD CAPACITY DOOR, SO THE SAME TRIPWIRE (task/1740). With no `usage`
    this reads THIS box's mount, like `_admit_suite` reads its /proc, and
    raises HOST_ADMISSION_EVENT first. `usage` is the same internal test seam
    as `_admit_suite`'s `proc_dir`: a reader for a fixture box's tmp, never a
    configuration override.
    """
    ambient = scratch._ambient_tmp()
    if usage is None:
        sys.audit(HOST_ADMISSION_EVENT, None, ambient)
        usage = scratch.usage
    reading = usage(ambient)
    if reading is None:
        return None
    pct = scratch.worst_pct(reading)
    if pct < scratch.WARN_PCT:
        return None
    inodes = "%d%%" % reading["inodes_pct"] \
        if reading["inodes_pct"] is not None else "n/a"
    # A CAPACITY REFUSAL: the mount's fill, like the suite cap and the stall
    # floor, is a fact about the NODE, and another node or a reap cures it.
    return CapacityRefusal(
        "this node's tmp %s is under pressure — %s inodes, %d%% bytes "
        "(%s%s), at or over the %d%% scratch threshold: a suite minted "
        "here fails for the mount, not the tree, so the gate refuses "
        "before anything runs and mints NO receipt. Reap it first "
        "(`helm scratch status`, then `helm scratch gc --apply` on this "
        "node, or remove the stale test scratch under %s) and re-run."
        % (reading["mount"], inodes, reading["bytes_pct"],
           reading["fstype"],
           ", nr_inodes=%d" % reading["nr_inodes"]
           if reading["nr_inodes"] else "",
           scratch.WARN_PCT, ambient))


def _occupant_names(census, pending):
    names = []
    for row in census:
        tag = "" if row.get("kind", "suite") == "suite" \
            else " [%s, not counted toward the cap]" % row["kind"]
        kids = row.get("children") or []
        kidtag = "" if not kids else " +%d spawned child process%s" % (
            len(kids), "" if len(kids) == 1 else "es")
        names.append("pid %d (cwd %s)%s: %s%s" % (
            row["pid"], row["cwd"], tag,
            _failure_text(" ".join(row["argv"])), kidtag))
    names.extend("@%s admitted at %s (launcher pid %d, suite starting)" % (
        row["holder"], row["ts"], row["pid"]) for row in pending)
    return names


def _census_owner_for_pid(census, pid):
    """Canonical suite owner containing pid, or None.

    Admission callers are launcher processes, not suite-shaped roots. If a test
    or shard launches another gate, its launcher is already in one owner's
    process tree and must inherit that grant rather than purchase another one.
    """
    for row in census:
        if row.get("kind") != "suite":
            continue
        if row.get("pid") == pid or pid in {
                child.get("pid") for child in row.get("children") or ()}:
            return row.get("owner")
    return None


def _admit_suite(position=None, proc_dir=None, topology=_CAPACITY_UNSET,
                  admissions_path=None):
    """Admit one canonical suite OWNER onto the BOX. -> (grant, refusal)

    Topology is the slow/static half and is captured before the flock. Panes,
    suite owners and PSI are live authority facts: all are re-read under the
    host-global admission flock, so a pane that starts after topology capture
    still forces cap two and simultaneous Helm homes cannot all read the same N.

    A launcher already inside a censused suite tree, or below a still-pending
    owner launcher, inherits that owner's grant. It writes no second intent and
    count pressure cannot refuse work the admitted owner already contains. One
    effective grant then leaves this door with every child of that owner.
    """
    # Host admission never inherits HELM_PROC. Child containment may redirect
    # that variable for its own declared process root, but the protected
    # resource is this node and only its real /proc can authorize another owner.
    # `proc_dir` is an explicit internal test seam, not a configuration override.
    proc_dir = proc_dir or HOST_PROC
    if topology is _CAPACITY_UNSET:
        topology = _capacity_topology()
    capacity = None
    try:
        path, root_err = admissions_path or _admissions_path(), None
    except (OSError, RuntimeError) as exc:
        path, root_err = None, exc
    # BEFORE the first read of the box, and on every road that makes one —
    # the unavailable-root answer below still prices the host's panes.
    _audit_host_admission(proc_dir, path)
    if root_err is not None:
        return _capacity_grant(proc_dir, topology), \
            "gate admission runtime root is unavailable: %s" % root_err
    try:
        with seats._flocked(path + ".lock") as lock:
            if lock.f is None:
                return None, "gate admission lock is unavailable"
            census = suite_census(proc_dir)
            if census is None:
                capacity = _capacity_grant(proc_dir, topology)
                return capacity, \
                    "cannot count running suites (%s is unreadable); " \
                    "admission refused. %s" % (proc_dir, _FAB_HANDOFF)
            rows = _admissions_load(path)
            live = [row for row in rows
                    if seats._get_live_pid_starttime(
                        row["pid"], proc_dir=proc_dir) == row["starttime"]]
            # A nested gate can start before its owner's suite root exists. A
            # pre-fix launcher then persisted a second pending intent. Exact
            # process ancestry still names the outer live launcher, so retain
            # only the top pending owner before occupancy is priced.
            launchers = {row["pid"] for row in live}
            live = [row for row in live
                    if not launchers.intersection(
                        _proc_ancestors(row["pid"], proc_dir))]
            # Only whole-tree DISCOVERY runs count toward the cap — that is
            # exactly the set the census counted before it was widened to SEE
            # targeted/pytest runs and their children. The widened rows ride
            # in `census` for the refusal's full-picture handoff and for the
            # follow-on cost policy; they never re-price this proxy count.
            suite_rows = [row for row in census
                          if row.get("kind") == "suite"]
            # A pre-fix nested gate may already have persisted its own intent.
            # Its launcher sits BELOW the real owner, unlike the real owner's
            # launcher (which is an ancestor of the suite root). Retire that
            # duplicate while both identities are still live; otherwise the
            # new owner census would collapse the child root but the old intent
            # would survive as a phantom pending seventeenth owner.
            live = [row for row in live
                    if not _census_owner_for_pid(suite_rows, row["pid"])]
            by_launcher = {row["pid"]: row for row in live}
            for owner in suite_rows:
                if owner["position"]:
                    continue
                intent = next((by_launcher[pid]
                               for pid in owner.get("_ancestors", ())
                               if pid in by_launcher), None)
                if intent:
                    # A cgroup-less queued suite is not two occupants. Its root
                    # pid proves the running process; the exact live ancestor
                    # launcher binds that process back to the pending grant.
                    owner["position"] = intent["position"]
                    owner["owner"] = intent["position"]
            covered = {row["position"] for row in suite_rows
                       if row["position"]}
            pending = [row for row in live if row["position"] not in covered]
            occupants = len(suite_rows) + len(pending)
            stall = _psi_some_avg10(proc_dir)
            # FINAL AUTHORITY SAMPLE. A pane can start while suite_census walks
            # /proc; derive the grant only after that work, immediately before
            # the decision. A later pane start belongs to pane-launch admission,
            # not to prediction by this gate.
            capacity = _capacity_grant(proc_dir, topology)
            launcher_pid = position["pid"] if position else os.getpid()
            inherited = _census_owner_for_pid(suite_rows, launcher_pid)
            if not inherited:
                inherited = next((by_launcher[pid]["position"]
                                  for pid in _proc_ancestors(
                                      launcher_pid, proc_dir)
                                  if pid in by_launcher), None)
            if inherited:
                # This launcher is work INSIDE an owner the host already
                # admitted. Appending its new queue/cgroup token would make one
                # suite buy two slots until the child appeared, and counting the
                # child as a new root would keep the false occupancy afterwards.
                # Dead unrelated rows still self-heal on this pass.
                if live != rows:
                    pk.write_json(path, {"v": 1, "admissions": live})
                return capacity, None
            pressure_refusal = stall is not None and stall >= PSI_SOME_FLOOR
            count_refusal = occupants >= capacity["cap"]
            if pressure_refusal or count_refusal:
                running = "; ".join(_occupant_names(census, pending))
                refusals = []
                if pressure_refusal:
                    refusals.append(
                        "this box is already stalling on memory — PSI some "
                        "avg10 is %.2f%%, at or over the %.0f%% floor (the "
                        "stall fraction at which the system's own "
                        "memory-pressure watch fires, the failure this floor "
                        "exists to prevent) — REFUSED%s. Re-run once "
                        "the pressure clears — avg10 is a 10s window, so "
                        "watch `cat /proc/pressure/memory` fall under the "
                        "floor. No override exists for this floor: it is the "
                        "kernel's own starvation reading." % (
                            stall, PSI_SOME_FLOOR,
                            "; running now: %s" % running if running else ""))
                if count_refusal:
                    # The structured grant names WHY this host has this cap.
                    # Never infer panes from the scalar value two.
                    refusals.append(
                        "this host's whole-suite cap is %d (%s) and %d %s "
                        "already running — REFUSED at this host's admitted-"
                        "suite limit; running now: %s. Retry when one of "
                        "those named runs finishes — the slot frees itself, "
                        "nothing to clean "
                        "up. No override exists for this cap: it counts "
                        "processes, not permission." % (
                            capacity["cap"], capacity["reason"], occupants,
                            "is" if occupants == 1 else "are", running))
                if live != rows:
                    pk.write_json(path, {"v": 1, "admissions": live})
                # THE ONLY TWO CAPACITY REFUSALS HERE. Every other refusal in
                # this door is UNKNOWN (an unreadable census, lock or ledger)
                # and keeps the plain string, so it keeps exit 1.
                return capacity, CapacityRefusal(
                    " ".join(refusals + [_FAB_HANDOFF]))
            if position is not None:
                live.append({"position": position["id"],
                             "pid": position["pid"],
                             "starttime": position["starttime"],
                             "holder": position["holder"],
                             "ts": pk.now_ts()})
            if live != rows or position is not None:
                pk.write_json(path, {"v": 1, "admissions": live})
    except OSError as exc:
        return capacity, "gate admission state write failed: %s" % exc
    return capacity, None


def _admission_release(position_id, admissions_path=None):
    """Drop one admission intent by FIFO position id. -> err or None."""
    try:
        path = admissions_path or _admissions_path()
    except (OSError, RuntimeError) as exc:
        return "gate admission runtime root is unavailable: %s" % exc
    _audit_host_admission(None, path)
    try:
        with seats._flocked(path + ".lock") as lock:
            if lock.f is None:
                return "gate admission lock is unavailable"
            rows = _admissions_load(path)
            keep = [row for row in rows if row["position"] != position_id]
            if keep != rows:
                pk.write_json(path, {"v": 1, "admissions": keep})
    except OSError as exc:
        return "gate admission release failed: %s" % exc
    return None


_GATE_RENEW_S = 5
_GATE_WAIT_S = 0.25
_GATE_LEGACY_TTL_S = 30
_GATE_LEGACY_RENEW_S = 5
_GATE_KILL_GRACE_S = 1
_GATE_KILL_FORCE_S = 0.5
_GATE_GUARD_START_S = 5


def _legacy_gate_resource(repo):
    """The rollout bridge to the pre-FIFO `gatelock:<project>` mutex."""
    repo_id, err = seats._gate_repo_id(repo)
    if err:
        return None, err
    root = os.path.dirname(repo_id) if os.path.basename(repo_id) == ".git" \
        else repo_id
    project = os.path.basename(root.rstrip(os.sep))
    if not project:
        return None, "cannot derive the legacy gate-lock project"
    return "gatelock:" + project, None


class _LegacyGateLease:
    """Short-TTL compatibility hold renewed through receipt finalization.

    Old binaries and manual gate runners know only `gatelock:<project>`. The
    FIFO head takes that same lock during rollout, then a daemon renews it while
    stale-base analysis runs. Receipt append stops the daemon and validates the
    exact lease under the claims lock. If the launcher dies, the daemon dies
    with it and the short TTL recovers the old coordination surface.
    """
    def __init__(self, resource, holder, session, lease):
        self.resource = resource
        self.holder = holder
        self.session = session
        self.lease = lease
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error = None
        self._thread = threading.Thread(target=self._renew, daemon=True,
                                        name="helm-gate-legacy-renew")
        self._thread.start()

    def _set_error(self, message):
        with self._lock:
            self._error = str(message or "legacy gate-lock renewal failed")

    def error(self):
        with self._lock:
            return self._error

    def _renew(self):
        while not self._stop.wait(_GATE_LEGACY_RENEW_S):
            try:
                ok, message = seats.refresh_claim(
                    self.resource, self.holder, ttl=_GATE_LEGACY_TTL_S,
                    lease=self.lease, session=self.session, strict=True)
            except Exception as exc:
                self._set_error("%s: %s" % (type(exc).__name__, exc))
                return
            if not ok:
                self._set_error(message)
                return

    def _stop_renewer(self):
        self._stop.set()
        # A renewer inside refresh_claim can wait out the whole claims-lock
        # bound; outlast it, so its refusal (naming the holder) is the error.
        self._thread.join(timeout=max(1, _GATE_LEGACY_RENEW_S * 2,
                                      seats.CLAIM_LOCK_WAIT_S + 1))
        if self._thread.is_alive():
            self._set_error("renewal thread did not stop")
        return self.error()

    def finalize(self, action):
        renew_error = self._stop_renewer()
        if renew_error:
            return None, "legacy gate-lock renewal failed: %s" % renew_error
        return seats.claim_guard(self.resource, self.holder, self.lease,
                                 self.session, action)

    def close(self):
        renew_error = self._stop_renewer()
        try:
            ok, message = seats.release(
                self.resource, self.holder, lease=self.lease,
                session=self.session, strict=True)
        except Exception as exc:
            ok, message = False, "%s: %s" % (type(exc).__name__, exc)
        if renew_error:
            return False, "legacy gate-lock renewal failed: %s" % renew_error
        return ok, None if ok else message


def _acquire_legacy_gate(repo, holder, session):
    resource, err = _legacy_gate_resource(repo)
    if err:
        return None, err
    while True:
        try:
            ok, message, lease = seats.claim(
                resource, holder, ttl=_GATE_LEGACY_TTL_S,
                session=session, strict=True)
        except Exception as exc:
            return None, "legacy gate-lock claim failed: %s: %s" % (
                type(exc).__name__, exc)
        if not ok:
            time.sleep(_GATE_WAIT_S)
            continue
        try:
            return _LegacyGateLease(resource, holder, session, lease), None
        except BaseException:
            try:
                seats.release(resource, holder, lease=lease, session=session,
                              strict=True)
            except Exception:
                pass
            raise


def _gate_room(repo):
    room, _source = seats.resolve_homing(cwd=repo)
    return room or "main"


def _gate_post(repo, text, holder):
    """Post one observer line; true means chat returned the appended row.

    Admission never depends on chat. Durable terminal acknowledgement does, so
    "did not raise" is too weak at that consumer: only the concrete row returned
    by chat.post confirms that publication reached chat's append boundary.
    """
    try:
        return bool(chat.post(text, room=_gate_room(repo), who=holder,
                              sign=False, ambient=True))
    except Exception:
        return False


# WHAT AN ORPHAN MEANS DEPENDS ON HOW FAR THE DEAD LAUNCHER GOT, and the row
# carries exactly that and nothing more. THE LAUNCHER IS THE ONLY WRITER of
# this state and the ONLY MINTER of a receipt, so an orphan's state is the LAST
# THING HELM EVER LEARNED about that attempt — a floor under its progress,
# never a description of how it ended. Two consequences run through every
# sentence below:
#
#   IT SPEAKS ABOUT THE ATTEMPT, NEVER THE LANE. One row is one attempt. A lane
#   whose earlier run minted a green receipt still produces this notice when a
#   later attempt dies, and the first wording said "this lane has no verdict" —
#   an attempt-local fact reported lane-wide, which is false exactly when it
#   matters. The verdict lives in the ledger; the queue has never seen it.
#
#   WHERE THE STATE CANNOT DECIDE, IT SAYS UNKNOWN. A sentence that names a
#   state helm cannot measure is the same defect the "skipped" wording had,
#   wearing better words.
#
#   waiting/starting  — NO SUITE PROCESS EXISTED. gatechild's supervisor blocks
#                       on a one-byte barrier (gatechild._supervise) that the
#                       launcher writes ONLY after the row reaches `running`,
#                       so a row that died at or before `starting` ran nothing
#                       and can never be minted for. The two share ONE sentence
#                       on purpose: the CONSEQUENCE is identical, and the
#                       printed state= token keeps the events distinct.
#   running           — the barrier was released. Whether the suite finished,
#                       and whether the launcher minted before dying, are
#                       UNKNOWN: minting and the `finishing` write are separate
#                       steps and death fits between them. The queue cannot
#                       answer it. The ledger can.
#   finishing         — the launcher reached finalization, so the attempt is
#                       OVER. That one path is shared by a minted PASS/FAIL, an
#                       UNMINTED run, a REFUSED admission and a runner error,
#                       so WHICH of them is UNKNOWN from this row. It is also
#                       the state a row holds WHILE its own GATE FINISH is
#                       being posted (_finish_position posts before it
#                       removes), so this sentence must stay true for a reader
#                       who can already see that FINISH — it names it rather
#                       than contradicting it.
#   anything else     — helm did not read a state it understands, and says so
#                       rather than defaulting into one of the answers above.
#
# Measured: a wrapper earlyoom-killed while QUEUED produced exit=1,
# no local receipt, and the word "skipped" — which reads as benign and is not.
# An orphan notice that does not say whether a verdict exists makes the reader
# guess, and three seats guessed differently about the same failure.
# A RECEIPT IS NOT A VERDICT, AND THIS NOTICE MUST NOT SPEND THE WORD (row
# 45b6d0c46161). Helm holds two different nouns and the first wording used
# the wrong one in the sentence whose whole job is to say what a reader may
# CONCLUDE. A RECEIPT is what the gate mints: it binds a TREE, names the box it
# ran on, and is the thing `helm gate list` prints. A VERDICT is a REVIEWER's
# polarity in the dispatch ledger — approve/fix/supersede — and only an APPROVE
# authorizes landing. Calling a red receipt "a verdict" tells a reader their
# review question is settled when what they actually hold is a failed run, and
# it points them at the wrong surface: the ledger this notice names lists
# receipts, never verdicts. So every sentence below says RECEIPT, and says what
# the receipt licenses, without borrowing the review vocabulary.
_ORPHAN_LEDGER = "read `helm gate list`"
_ORPHAN_CONSEQUENCE = {
    "waiting": (
        "this attempt never started a suite process — NO RECEIPT EXISTS for "
        "it and none can arrive later; it is neither red nor green. Any "
        "receipt on this tree belongs to some OTHER run: " + _ORPHAN_LEDGER),
    # NO RECEIPT CAN BE ATTRIBUTED TO *THIS* ATTEMPT, AND THIS SENTENCE LOST
    # THAT DISCIPLINE WHILE THE `waiting` ONE KEPT IT. A gate receipt carries
    # head/tree/host/status and NO queue
    # position id, seq, attempt id or launcher generation — every key across
    # the whole receipt ledger was enumerated to check. So a PRIOR run on the same
    # tree can leave a FAILED or OK receipt, this attempt can die before
    # minting, and `helm gate list` has nothing to join them by. Telling a
    # reader "a FAILED receipt IS an answer" hands them another run's verdict
    # as this one's. The ledger is still the right place to LOOK — it is the
    # only surface that holds anything — but what it can settle is what exists
    # for the TREE, never what happened to this attempt.
    "running": (
        "this attempt was running and helm never saw it end — whether the "
        "suite finished, and whether it minted, are UNKNOWN here and cannot "
        "be recovered: receipts carry no queue-attempt identity, so NOTHING "
        "in the ledger can be attributed to THIS attempt. " + _ORPHAN_LEDGER +
        " to see what exists for this TREE, and read it as another run's "
        "evidence unless you can rule that out by hand: an ABSENT receipt "
        "answers nothing, a PRESENT one answers about the run that MINTED it, "
        "and an UNREADABLE row is a ledger fault that another run will not "
        "clear"),
    # MINTED-UNKNOWN IS THE FOURTH OUTCOME AND THE ONLY ONE THAT LOOKS LIKE AN
    # ANSWER WITHOUT BEING ONE. A run whose interpreter or
    # bound could not be established mints a receipt that EXISTS, prints in
    # `helm gate list`, and is REFUSED as a binding — so a reader who finds it
    # and sees only "minted PASS/FAIL" in this enumeration concludes the work
    # is done. Naming it here is what stops a present-but-unbindable receipt
    # from reading as a green one.
    "finishing": (
        "this attempt reached finalization, so it is OVER and its GATE FINISH "
        "line MAY already carry the result — a minted PASS/FAIL, a minted "
        "UNKNOWN (a receipt that EXISTS and still binds nothing), an UNMINTED "
        "run and a REFUSED admission all end here, so WHICH of them is "
        "UNKNOWN from this row. " + _ORPHAN_LEDGER +
        " before you re-run: the work may already be done"),
}
# ONE SENTENCE FOR TWO STATES, ON PURPOSE and asserted in the suite: the
# barrier makes their CONSEQUENCE identical, and the state= token in the
# notice keeps the two EVENTS distinct for anyone reading the queue.
_ORPHAN_CONSEQUENCE["starting"] = _ORPHAN_CONSEQUENCE["waiting"]
_ORPHAN_UNREADABLE = (
    "helm cannot read how far this attempt got, so whether a receipt exists "
    "is UNKNOWN — conclude nothing from this line; " + _ORPHAN_LEDGER +
    " is the only surface that can answer")


def _orphan_consequence(row):
    """The sentence a reader can ACT on, not the event that happened."""
    return _ORPHAN_CONSEQUENCE.get(row.get("state"), _ORPHAN_UNREADABLE)


# ONLY A MINTED BINDING RECEIPT EARNS "DO NOT RE-RUN" (row 02c7cddc6887).
# `_finish_position` stamps REFUSED (the suite never started),
# UNMINTED (it ran and no usable receipt exists) and UNKNOWN (a receipt that
# binds nothing) alongside OK and FAILED — and for the first three a retry can
# be exactly what is needed. The first version of this said "the attempt is
# OVER and re-running repeats work that is done" for EVERY stamped value,
# which is materially the opposite advice in three of five cases — the same
# mistake _ORPHAN_CONSEQUENCE had just cured: the minted-UNKNOWN distinction
# built into the notice, then contradicted by a blanket claim two functions
# away.
_ANNOUNCED_SETTLED = ("OK", "FAILED")


def _minted_token(text):
    """The canonical receipt id this line names, or None.

    A SUBSTRING TEST IS NOT A PARSE, and the first version was one:
    `"gate:" in text` classified
    `OK not-gate:available` as MINTED. The id shape is owned by
    dispatches._GATE_ID and asking IT is the only reading that cannot be
    spoofed by a word that happens to contain the prefix.
    """
    from . import dispatches
    for tok in str(text or "").split():
        if tok.startswith("gate:") and dispatches._GATE_ID.fullmatch(
                tok[len("gate:"):]):
            return tok[len("gate:"):]
    return None


def _announced_advice(announced):
    """What the stamped FINISH text licenses a reader to do, or "" for none.

    ONLY AN `OK` WITH A CANONICAL TOKEN MAY DISCOURAGE A RE-RUN, and the
    earlier version was wrong in three separate ways at once (row 7d2b5c1ae0f3):

      · IT TREATED `FAILED gate:<id>` AS BINDING. The binder REJECTS every
        FAILED receipt — a red run authorizes nothing — so telling a reader
        the attempt is OVER and re-running "repeats work that is done" was
        advice to leave a red gate unrepeated on the strength of its own
        redness.
      · IT MATCHED A SUBSTRING. `OK not-gate:available` classified as MINTED.
      · EVEN A WELL-FORMED `OK gate:<id>` IS NOT PROVEN BINDABLE from here.
        The binder can still refuse it for a dirty tree, a moved head or the
        wrong tree entirely, and this function has none of those facts. So it
        says MINTED — which is what the line actually witnesses — and never
        BINDING, which only the binder can decide.
    """
    text = str(announced or "").strip()
    if not text:
        return ""
    head = text.split(None, 1)[0].upper()
    token = _minted_token(text)
    if head == "OK" and token:
        return (". It was STAMPED %s before the FINISH post, naming a minted "
                "receipt — that is evidence the attempt reached a green "
                "finish, NOT proof the receipt binds: the binder can still "
                "refuse it for a dirty tree, a moved head or the wrong tree. "
                "Check `helm gate show %s` before re-running" % (text, token))
    if head == "FAILED" and token:
        return (". It was STAMPED %s before the FINISH post — a RED run. A "
                "FAILED receipt binds NOTHING, so this settles nothing about "
                "whether the work is done and a re-run is the normal next "
                "step" % text)
    if head in _ANNOUNCED_SETTLED:
        return (". It was STAMPED %s before the FINISH post and names no "
                "canonical receipt, so nothing here can be cited; treat a "
                "re-run as necessary unless you find the receipt yourself"
                % text)
    # REFUSED / UNMINTED / UNKNOWN / anything the producer grows later. A
    # minted UNKNOWN may carry a token and still binds nothing, which is why
    # the token is not consulted on this branch.
    return (". It was STAMPED %s before the FINISH post, which is NOT a "
            "settled result — a re-run may be exactly what is needed, and "
            "nothing here says otherwise" % text)


def _diagnostic_of(row):
    diagnostic = row.get("diagnostic")
    if isinstance(diagnostic, dict):
        return diagnostic
    return {"kind": "orphan", "status": "UNKNOWN",
            "stage": row.get("state") or "UNKNOWN",
            "reason": "launcher-gone"}


def _orphan_line(row, prefix, tail):
    """THE ONE FORMATTER BOTH SURFACES USE. The operational
    publisher and `helm gate list` were rendering the same row differently:
    the publisher ignored `announced` entirely, and the queue DELETES the row
    as it hands it over, so if any gate operation won before someone ran the
    list the stamp was gone and the only notice ever emitted omitted it. Two
    readers of one datum, and the bug lived between them where neither side's
    tests could see it — the test arm called the non-reaping accessor while
    the row still existed, so it stayed green on a path production never
    takes.
    """
    diagnostic = _diagnostic_of(row)
    return ("%s #%s @%s pid=%s state=%s diagnostic=%s stage=%s reason=%s — "
            "%s%s%s" % (
                prefix, row.get("seq"), row.get("holder"), row.get("pid"),
                row.get("state") or "UNKNOWN",
                _failure_text(diagnostic.get("status")) or "UNKNOWN",
                _failure_text(diagnostic.get("stage")) or "UNKNOWN",
                _failure_text(diagnostic.get("reason")) or "unknown",
                _orphan_consequence(row), tail,
                _announced_advice(row.get("announced"))))


def _gate_post_orphans(repo, rows, observer):
    for row in rows or ():
        diagnostic = _diagnostic_of(row)
        if diagnostic.get("kind") == "finish":
            text = "GATE FINISH #%s @%s %s%s" % (
                row.get("seq"), row.get("holder"),
                row.get("announced") or "UNKNOWN",
                " — NEXT @%s" % row["next_holder"]
                if row.get("next_holder") else "")
        else:
            prefix = "GATE ORPHAN" if diagnostic.get("kind") == "orphan" \
                else "GATE TERMINAL"
            text = _orphan_line(row, prefix, "")
        posted = _gate_post(repo, text, observer)
        if posted is True and row.get("diagnostic") and row.get("id"):
            acked, err = seats.gate_queue_ack_terminal(repo, row["id"])
            if not acked:
                print("helm gate: terminal diagnostic acknowledgement failed "
                      "for #%s (%s) — it remains pending and may be delivered "
                      "again" % (row.get("seq"), err or "row not found"),
                      file=sys.stderr)


def _finish_position(repo, position, status="UNKNOWN", receipt=None,
                     detail=None):
    legacy = position.get("_legacy")
    released, err = False, None
    # COMPUTED BEFORE THE SLOT IS ASKED FOR, so it can ride the SAME write that
    # sets `finishing` (task/408). It derives only from this function's own
    # arguments, so nothing is learned between here and where it used to be
    # built — moving it up costs nothing and buys the stamp a free window.
    result = "%s gate:%s" % (status, receipt) if receipt else status
    if detail:
        result += " — %s" % _failure_text(detail)
    try:
        ok, nxt, orphaned, err = seats.gate_queue_prepare_finish(
            repo, position["id"], result=result)
        if not ok:
            _gate_post_orphans(repo, orphaned, position["holder"])
        else:
            # POST BEFORE REMOVE, AND THE ORPHAN WORDING PAYS FOR IT. The row
            # and the FINISH line are two writes and a launcher can die
            # between them, so a `finishing` row can outlive its own announced
            # FINISH and be published as a GATE ORPHAN afterwards (task/109).
            # Removing FIRST closes that window and opens a worse
            # one: the next waiter polls every _GATE_WAIT_S and starts the
            # instant this row disappears, so its GATE START could precede
            # this FINISH and the queue narrative in chat — asserted right
            # here in test_gate_fifo (alpha_finish < beta_start) — would stop
            # being an ORDER and become a race, on the same box whose load
            # spikes produced this row. So the order stays deterministic and
            # the CONTRADICTION is cured where it is actually read: a
            # `finishing` orphan says the attempt is OVER and that a GATE
            # FINISH line for it MAY already carry the result, which is true
            # whether the launcher died before that post or after it.
            try:
                _gate_post_orphans(repo, orphaned, position["holder"])
                posted = _gate_post(repo, "GATE FINISH #%d @%s %s%s" % (
                    position["seq"], position["holder"], result,
                    " — NEXT @%s" % nxt["holder"] if nxt else ""),
                    position["holder"])
                if posted is True:
                    acked, ack_err = seats.gate_queue_ack_terminal(
                        repo, position["id"])
                    if not acked:
                        print("helm gate: FINISH diagnostic acknowledgement "
                              "failed for #%s (%s) — it remains pending and "
                              "may be delivered again" % (
                                  position["seq"], ack_err or "row not found"),
                              file=sys.stderr)
            finally:
                released, _nxt, orphaned, err = seats.gate_queue_finish(
                    repo, position["id"])
            _gate_post_orphans(repo, orphaned, position["holder"])
    finally:
        admission_err = _admission_release(position["id"])
        legacy_ok, legacy_err = legacy.close() if legacy else (True, None)
    trouble = [e for e in (
        err, admission_err, None if legacy_ok else legacy_err) if e]
    if admission_err or not legacy_ok:
        return False, "; ".join(trouble)
    return released, err


def _force_kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def _kill_group(proc):
    # SIGTERM lets gatechild's subreaper kill descendants that escaped the
    # process group. Collection remains bounded even if a foreign pipe holder
    # survives that containment boundary.
    try:
        os.killpg(proc.pid, signal.SIGCONT)
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        return proc.communicate(timeout=_GATE_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        _force_kill_group(proc)
    try:
        return proc.communicate(timeout=_GATE_KILL_FORCE_S)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        return "", ""


def _wait_process(repo, position, proc, supervisor, deadline):
    """Wait on one guarded child. -> (stdout, stderr, rc, err)

    ONE LOOP FOR BOTH KINDS OF RUN, not a simpler duplicate for the unqueued
    one. Deadline, communicate, exact guard/supervisor liveness and the
    terminal kill are CONTAINMENT and belong to every guarded run; only the
    FIFO heartbeat and the legacy-lock checks require a real slot.

    `position is None` means no slot was ever acquired -- a focused or custom
    run. It cannot renew what it does not hold, and it must not: a second
    implementation of this loop is how the two paths drift until one of them
    stops killing a child the other would have.
    """
    supervisor_pid, supervisor_start = supervisor
    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            out, errout = _kill_group(proc)
            return out or "", errout or "", None, None
        wait = _GATE_RENEW_S if remaining is None else min(_GATE_RENEW_S,
                                                           remaining)
        try:
            out, errout = proc.communicate(timeout=wait)
            legacy_err = position["_legacy"].error() if position else None
            if legacy_err:
                return "", "", None, \
                    "legacy gate-lock renewal failed: %s; suite result refused" \
                    % legacy_err
            return out or "", errout or "", proc.returncode, None
        except subprocess.TimeoutExpired:
            # AN UNQUEUED RUN RENEWS NOTHING because it holds nothing.
            # `ok` is True so the FIFO-failure branches below cannot fire on
            # a run that never had a slot to lose.
            if position:
                ok, err = seats.gate_queue_renew(
                    repo, position["id"], supervisor_pid)
                legacy_err = position["_legacy"].error()
            else:
                ok, err, legacy_err = True, None, None
            exact_live = seats._get_live_pid_starttime(supervisor_pid) \
                == supervisor_start
            if proc.poll() is not None and exact_live:
                _kill_group(proc)
                return "", "", None, \
                    "gate guard exited while its supervisor was live; suite killed"
            if not ok and not exact_live and not legacy_err:
                try:
                    out, errout = proc.communicate(
                        timeout=_GATE_KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    out = errout = None
                if out is not None:
                    bound, bound_err = seats.gate_queue_validate_binding(
                        repo, position["id"], supervisor_pid,
                        supervisor_start)
                    legacy_err = position["_legacy"].error()
                    if bound and not legacy_err:
                        return out or "", errout or "", proc.returncode, None
                    err = bound_err
            if not ok or legacy_err:
                _kill_group(proc)
                why = "gate FIFO renewal failed: %s" % err if not ok else \
                    "legacy gate-lock renewal failed: %s" % legacy_err
                return "", "", None, "%s; suite killed" % why


def _guard_supervisor(proc, report_fd):
    deadline, data = time.monotonic() + _GATE_GUARD_START_S, b""
    try:
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "gate guard did not report its supervisor"
            try:
                readable, _writable, _error = select.select(
                    (report_fd,), (), (), remaining)
            except OSError as exc:
                return None, "gate guard report failed: %s" % exc
            if not readable:
                return None, "gate guard did not report its supervisor"
            try:
                chunk = os.read(report_fd, 80 - len(data))
            except OSError as exc:
                return None, "gate guard report failed: %s" % exc
            if not chunk:
                return None, "gate guard exited before reporting its supervisor"
            data += chunk
            if len(data) >= 80 and b"\n" not in data:
                return None, "gate guard report is malformed"
    finally:
        os.close(report_fd)
    fields = data.splitlines()[0].split()
    if len(fields) != 2:
        return None, "gate guard report is malformed"
    try:
        pid, starttime = map(int, fields)
    except ValueError:
        return None, "gate guard report is malformed"
    if seats._get_live_pid_starttime(pid) != starttime:
        return None, "gate guard reported a supervisor that is not live"
    return (pid, starttime), None


def _guard_diagnostic(proc, err, limit=4096):
    """Append bounded child stderr without changing the UNRUNNABLE verdict."""
    stream = getattr(proc, "stderr", None)
    if stream is None:
        return err
    data = b""
    try:
        while len(data) < limit:
            readable, _writable, _error = select.select(
                (stream.fileno(),), (), (), 0)
            if not readable:
                break
            chunk = os.read(stream.fileno(), limit - len(data))
            if not chunk:
                break
            data += chunk
    except (OSError, ValueError):
        return err
    reason = data.decode("utf-8", "replace").strip()
    return "%s — %s" % (err, reason) if reason else err


# WHY THERE IS NO SELF-DELEGATION HELPER HERE, measured and kept
# because the next person will reach for one:
#
#   THE LAPTOP DOES NOT NEED IT. An agent pane's cgroup root is writable and
#   searchable and _create_cgroup succeeds outright. The belief that a pane is
#   root-owned came from gatechild reporting a malformed POSITION TOKEN as
#   "cgroup root is unreadable" -- a caller fault wearing an environment
#   fault's words.
#
#   THE REMOTE PATH ALREADY HAS IT. gateroute.py:780-799 probes
#   `systemd-run --user --scope` and wraps `helm gate run` ITSELF when it
#   works. A focused gate reaches a fab node through that path
#   (fab-gate:46-68), so it is delegated before it arrives.
#
#   AND THE WRAP CANNOT LIVE AT THIS LAYER ANYWAY. Applied around the guard
#   launch it inserts a process BETWEEN the launcher and the guard, so
#   _arm_parent_death watches the wrong one -- five containment arms went red
#   at once. Delegation is a property of the RUN, not of the child.
#
# So a helper here would be dead code that reads as protection, which is worse
# than none. Where delegation is genuinely absent the guard fails closed, and
# that refusal is the honest answer.


def _guard_identity():
    """The identity EVERY guarded run needs. -> {id, launcher_start}

    TWO IDENTITIES, NOT ONE SHAPE WITH OPTIONAL FIELDS. Containment needs a
    cgroup token and the launcher's exact start; the FIFO needs seq, holder,
    a legacy lock and renewal authority. They travelled in one `position`
    dict, which is why only whole-suite runs -- the ones that acquire a slot
    -- ever got a cgroup, while focus and every custom command ran unguarded.

    A POSITION-SHAPED VALUE WITH THE FIFO FIELDS OMITTED DOES NOT WORK: a
    fake queue capability is structurally ACCEPTED by every accidental FIFO
    consumer. Omitting fields only catches
    a consumer that reaches for those exact fields; one that needs `id` alone
    would take it as a slot and be right about the shape and wrong about the
    authority. Queue authority must be unforgeable by ABSENCE OF THE OBJECT,
    not by absence of keys.

    So: this is required for every guarded run and grants nothing. `position`
    is OPTIONAL and exists only when _acquire_gate minted a real slot; the
    FIFO consumers are guarded on `position is not None`, and an unqueued run
    literally cannot call them because there is no object to pass.

    The token is fresh lowercase hex because _cgroup_path requires it and
    reports a malformed one as "cgroup root is unreadable" -- an
    environment-shaped message for a caller-shaped fault.
    """
    return {
        "id": binascii.hexlify(os.urandom(16)).decode("ascii"),
        "launcher_start": seats._get_pid_starttime(os.getpid()),
    }


def _queued_process(repo, cmd, position, timeout, identity=None, env=None):
    """Run one guarded child, renewing its slot if it holds one.

    -> (stdout, stderr, rc, err). Queue wait happens before this
    function, so `timeout` measures the child run and never punishes a waiter.

    TWO IDENTITIES. `identity` is the containment one -- {id, launcher_start}
    -- and is REQUIRED; the cgroup, the guard argv and the watcher consume it.
    `position` is the FIFO slot and is OPTIONAL: None means this run acquired
    no slot, so it binds nothing, announces nothing and renews nothing.

    A queued run passes both, and its identity is DERIVED FROM its position so
    the cgroup token and the slot stay the same string they have always been
    -- this refactor must not move a whole-suite run's cgroup.

    `env` is explicit because it is not uniform: every suite-scale run gets
    _suite_env() with its admitted grant, while a non-suite custom diagnostic
    INHERITS. Routing custom through here without carrying that distinction
    would either unconstrain a shard or silently change what diagnostics see.
    """
    if identity is None:
        identity = {"id": position["id"],
                    "launcher_start": position["starttime"]}
    if env is None:
        env = _suite_env()
    wrapper = os.path.abspath(gatechild.__file__)
    cgroup = gatechild._cgroup_path(identity["id"])
    watcher = None

    def cleaned(result):
        ok = bool(cgroup and gatechild._kill_cgroup(cgroup))
        if watcher:
            try:
                rc = watcher.wait(timeout=_GATE_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.wait(timeout=_GATE_KILL_FORCE_S)
                rc = None
            ok = ok and rc == 0
        if ok:
            return result
        return "", "", None, \
            "gate cgroup cleanup failed; suite result refused"

    if not cgroup:
        return "", "", None, "gate cgroup path is unavailable"
    ready_r, ready_w = os.pipe()
    report_r, report_w = os.pipe()
    launch = [sys.executable, wrapper, "--guard",
              "--parent", str(os.getpid()),
              "--parent-start", str(identity["launcher_start"]),
              "--position", identity["id"],
              "--ready-fd", str(ready_r),
              "--report-fd", str(report_w), "--"] + cmd
    try:
        proc = subprocess.Popen(launch, cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env=env, start_new_session=True,
                                pass_fds=(ready_r, report_w))
    except Exception as exc:
        for fd in (ready_r, ready_w, report_r, report_w):
            os.close(fd)
        return "", "", None, "gate command did not run: %s: %s" % (
            type(exc).__name__, exc)
    os.close(ready_r)
    os.close(report_w)
    guard_start = seats._get_live_pid_starttime(proc.pid)
    if guard_start is None:
        os.close(ready_w)
        os.close(report_r)
        _kill_group(proc)
        return cleaned(("", "", None,
                        "gate guard generation is unavailable"))
    try:
        watcher = subprocess.Popen(
            [sys.executable, wrapper, "--watch", str(os.getpid()),
             str(identity["launcher_start"]), str(proc.pid), str(guard_start),
             cgroup], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            start_new_session=True)
    except Exception as exc:
        os.close(ready_w)
        os.close(report_r)
        _kill_group(proc)
        return cleaned(("", "", None,
                        "gate cgroup watcher did not run: %s" % exc))
    try:
        supervisor, err = _guard_supervisor(proc, report_r)
        report_r = None
        if err:
            os.close(ready_w)
            ready_w = None
            err = _guard_diagnostic(proc, err)
            _kill_group(proc)
            return cleaned(("", "", None, err))
        # BIND ONLY WHAT WE HOLD. An unqueued run has no slot to bind a child
        # to, and binding one would tell the FIFO a slot is occupied that
        # nobody acquired.
        ok, err = seats.gate_queue_bind_child(
            repo, position["id"], supervisor[0]) if position else (True, None)
        if not ok:
            os.close(ready_w)
            ready_w = None
            _kill_group(proc)
            return cleaned(("", "", None,
                            "gate FIFO lost before child bind: %s" % err))
        try:
            os.write(ready_w, b"\0")
        except OSError as exc:
            os.close(ready_w)
            ready_w = None
            _kill_group(proc)
            return cleaned(("", "", None,
                            "gate supervisor did not admit suite: %s" % exc))
        os.close(ready_w)
        ready_w = None
        deadline = time.monotonic() + timeout if timeout is not None else None
        # ANNOUNCE ONLY A REAL SLOT. GATE START carries seq and holder, which
        # an unqueued run does not have and must not invent -- an announcement
        # is how the room learns the queue advanced.
        if position:
            _gate_post(repo, "GATE START #%d @%s pid=%d" % (
                position["seq"], position["holder"], supervisor[0]),
                position["holder"])
        return cleaned(_wait_process(
            repo, position, proc, supervisor, deadline))
    except BaseException:
        for fd in (ready_w, report_r):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        _kill_group(proc)
        cleaned(("", "", None, None))
        raise


def _acquire_gate(repo):
    session = seats._env_session()
    # A QUEUE SLOT NEEDS A DISTINGUISHABLE HOLDER, NOT NECESSARILY A SEAT — and
    # those are different things, which `acting_seat` could not say. Its last
    # rung is `derive_seat`, so an operator who declares no HELM_CHAT_NAME
    # enqueued under a MINTED name that reads exactly like a real seat: "GATE
    # QUEUED #3 @helm-fable" is indistinguishable from the actual seat of that
    # name holding the fleet's gate.
    #
    # A SEAT HOLDS IT AS ITSELF; MACHINERY HOLDS IT AS MACHINERY. Refusing the
    # second would be the wrong cure — an env-less operator must be able to run
    # the gate, and that is the read-only-verb regression class — so it becomes
    # a SystemLeaseCapability, whose holder `system:gate:<sid8>` orders the
    # queue perfectly well and cannot be read as a seat.
    from . import actors
    actor, _err = actors.resolve_actor(session, repo, act="hold the gate")
    holder = (actor.canonical_name if actor
              else actors.SystemLeaseCapability("gate", session).holder)
    state, position, err = seats.gate_queue_enqueue(repo, holder)
    if err:
        return None, err
    granted = False
    try:
        _gate_post_orphans(repo, position.pop("orphaned", []), holder)
        _gate_post(repo, "GATE QUEUED #%d @%s" % (
            position["seq"], position["holder"]), position["holder"])
        while True:
            state, current, err = seats.gate_queue_try_start(
                repo, position["id"])
            if current:
                _gate_post_orphans(repo, current.pop("orphaned", []), holder)
            if err:
                return None, err
            if state == "START":
                legacy, err = _acquire_legacy_gate(repo, holder, session)
                if err:
                    return None, err
                current["_legacy"] = legacy
                granted = True
                return current, None
            time.sleep(_GATE_WAIT_S)
    finally:
        if not granted:
            _ok, _nxt, orphaned, _err = seats.gate_queue_finish(
                repo, position["id"], terminal={
                    "kind": "cancel", "status": "UNKNOWN",
                    "stage": "queue-acquire",
                    "reason": "launcher-exited-before-gate-start"})
            _gate_post_orphans(repo, orphaned, holder)


INFLIGHT = ".helm-gate-in-flight"


def inflight_path(repo):
    """The in-room marker naming THIS room's running gate, or None.

    RESOLVES THE REAL ADMIN DIR AND DOES NOT ASSUME `<repo>/.git` IS ONE.
    In a LANE WORKTREE — which is every room this guard exists for — `.git` is
    a FILE holding `gitdir: ...`, so writing `<repo>/.git/<name>` raises
    NotADirectoryError. Caught live by DOGFOODING this guard against
    its own gate: the marker never appeared, and the swallow in _inflight_open
    made the guard silently inert in exactly the rooms it protects. Seven unit
    tests passed throughout, because their fixture built `.git` as a real
    DIRECTORY and could not express the state the bug lives in.

    `--git-dir` is PER-WORKTREE (.git/worktrees/<lane>); `--git-common-dir` is
    SHARED by every worktree and would make this marker project-wide again —
    the precise thing it was designed not to be."""
    # THROUGH helm/vcs.py, NOT A PRIVATE GIT SPAWN. tests/test_vcs.py's
    # direct-spawn audit caught the first draft doing subprocess.run(["git",
    # ...]) here and named it: every git call routes through the seam or gets
    # declared as debt with a reason. There is no reason to declare — the
    # backend already exposes exactly this call, and gate.py uses it four
    # lines from here for rev-parse HEAD^{tree}.
    #
    # NOT backend.common_dir(): that is the SHARED admin dir, identical for
    # every worktree of a repo, which would make this marker project-wide
    # again — the precise property it exists not to have.
    try:
        rc, out, _err = vcs.backend(repo).text(repo, "rev-parse", "--git-dir")
    except Exception:                    # noqa: BLE001 — a path we cannot
        return None                      # resolve is no marker, never a raise
    if rc != 0:
        return None
    gitdir = (out or "").strip()
    if not gitdir:
        return None
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(repo, gitdir)
    return os.path.join(gitdir, INFLIGHT)


def inflight_dir(repo):
    """The per-owner marker DIRECTORY, or None.

    ONE FILE PER RUNNING GATE, because a single shared marker cannot express
    two owners and every single-file scheme we tried lost one of them. The
    first draft overwrote, so a short gate finishing second deleted a long
    gate's live marker. The second draft deferred to a live incumbent — and
    review found the mirror image: if the INCUMBENT finishes FIRST it removes
    the sole marker while the deferred gate is still running, so the room reads
    free mid-suite again. Deferral only ever worked when the incumbent outlived
    every gate that deferred to it, which nothing guarantees.

    A DIRECTORY IS A REFCOUNT AND THE FILESYSTEM MAKES IT ATOMIC: open creates
    its own file, close unlinks only that file, and the room is gating while
    ANY live owner file remains. No read-modify-write, so no lock, so no window
    between reading the owner set and writing it back."""
    path = inflight_path(repo)
    return (path + ".d") if path else None


def _boot_id():
    """This boot's identity, or None off Linux. A pid means nothing across a
    reboot: pids restart from a low number, so a marker written before a crash
    names a pid that a DIFFERENT process legitimately owns afterward."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _proc_start(pid):
    """Process start time in clock ticks, or None. Together with the boot id
    this makes (boot, pid, start) a key no reused pid can forge: a recycled pid
    is a DIFFERENT process and started at a different tick."""
    try:
        with open("/proc/%d/stat" % int(pid), encoding="utf-8") as fh:
            stat = fh.read()
    except (OSError, ValueError, TypeError):
        return None
    # field 22 is starttime, and comm (field 2) may itself contain spaces or
    # parentheses — split AFTER the last ')' or a process named "a b) c" lies.
    tail = stat.rpartition(")")[2].split()
    try:
        return tail[19]                  # 22nd field, 1-based, minus pid+comm
    except IndexError:
        return None


def _inflight_open(repo):
    """Register THIS gate as an owner; returns the nonce that identifies it.

    ONE FILE PER OWNER (see inflight_dir). Both single-file drafts lost a live
    gate — the first by overwriting the incumbent, the second by letting the
    incumbent's own close remove the sole marker while a deferred gate was
    still running (found in review). Neither is possible here: this
    gate creates its OWN file and can only ever remove that one.

    Never raises: a marker we cannot write is a missing warning, never a
    failed gate."""
    d = inflight_dir(repo)
    if not d:
        return None
    nonce = uuid.uuid4().hex
    pid = os.getpid()
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, nonce + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "ts": pk.now_ts(), "nonce": nonce,
                       "boot": _boot_id(), "start": _proc_start(pid)}, fh)
    except OSError:
        return None
    return nonce


def _inflight_close(repo, nonce=None):
    """Retire OUR owner file and nothing else.

    NO NONCE MEANS NO OWNERSHIP, and that is a refusal to delete rather than a
    licence to. Removing the DIRECTORY is deliberately not attempted: an empty
    one is harmless and reading it costs a listdir, while an rmdir racing a
    concurrent open would delete a room's marker out from under a gate that had
    just registered."""
    if not nonce:
        return
    d = inflight_dir(repo)
    if not d:
        return
    try:
        os.remove(os.path.join(d, str(nonce) + ".json"))
    except OSError:
        pass


def _inflight_owner_live(row):
    """Is the owner this marker names still the process that wrote it?

    A LIVE PID IS NOT A LIVE GATE. A marker left by a crash or a reboot names a
    pid the kernel has since reissued, so os.kill reports a STRANGER alive and
    the room stays refused for that unrelated process's whole lifetime. The
    marker therefore carries (boot, start) and both must still agree. Markers
    written WITHOUT them — older files, or any platform with no /proc — fall
    back to the bare liveness check, so this cannot disarm a gate that is in
    flight across the upgrade."""
    try:
        pid = int(row.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    boot, start = row.get("boot"), row.get("start")
    if boot is not None and boot != _boot_id():
        return False
    if start is not None and start != _proc_start(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True               # alive, owned by another uid
    except OSError:
        return False
    return True


# The gate census speaks in THREE states because two lost a war twice over:
# a census that can only say "owner" or "no owner" has no word for "I could
# not look", and every consumer inherits that muteness as a confident zero.
GATE_LIVE = "live"                # a measured owner; .live carries (pid, ts)
GATE_EMPTY = "empty"              # every source READ, nothing owns the room
GATE_UNREADABLE = "unreadable"    # a source could NOT be read; .reason says why

GateCensus = collections.namedtuple("GateCensus", ("state", "live", "reason"))


def inflight_census(repo):
    """GateCensus(state, live, reason) — THIS ROOM's gate owners, typed.

    THREE STATES, NEVER TWO, and the third is the whole reason this exists.
    `inflight()` below used to swallow an unreadable marker directory into
    None, so its consumer printed "no gate running" about a directory it
    never saw (task/112 read 4, reproduced with a PermissionError
    on listdir: findings=[] unknowns=[]). That was the THIRD instance of one
    class found by three different reviewers — `seat_homes.walk` swallowed
    every OSError so an unreadable subtree read as no-seats, and
    `dispatches.stop_candidate` signalled unavailability by a return value
    its caller destructured and DISCARDED, so an unreadable store read as
    owes-nothing. In all three the consumer did the RIGHT thing — it reused
    the existing primitive instead of writing a second enumerator — and
    inherited a swallow it could not see. The cure is at the SEAM: the census
    itself carries UNREADABLE-with-reason, and suite_census's law applies —
    an unreadable census must widen to a refusal upstream, never narrow to
    "nothing is running".

      GATE_LIVE        a measured owner. `live` is (pid, ts); `reason` still
                       names any source that could not be read alongside it,
                       because a partial census is a fact worth reporting
                       even when the answer is already YES.
      GATE_EMPTY       every source was READ (or measurably absent) and no
                       live owner exists. FileNotFoundError lands here on
                       purpose: a marker dir that has never been created is
                       the normal state of every never-gated room, and
                       calling it UNREADABLE would scream "could not
                       measure" on every clean stop in the fleet.
      GATE_UNREADABLE  no live owner was PROVEN and at least one source
                       could not be read, so absence is unmeasured. `reason`
                       says which source and why.

    BYTES READ AND REJECTED ARE NOT BLINDNESS: a marker whose JSON does not
    parse or whose owner is dead/foreign/recycled was MEASURED and found to
    be no gate (the stale-marker law below) — only a failure to read at all
    degrades the census.

    ROOM-SCOPED BY CONSTRUCTION, which is the whole point. `gatelock:<project>`
    records the HOLDER and never the REPO — measured: the claim
    carries holder/session/lease/fence/exp and nothing else — so a guard built
    on the LOCK would fire in every room whenever any seat gated anywhere. This
    marker lives in the room it describes, so no filtering can get it wrong.

    ANY LIVE OWNER MEANS GATING. The directory holds one file per running gate
    and this returns the OLDEST live one, so the answer does not flap as gates
    come and go: while two overlap, the room reports the one that started
    first, and it stops reporting only when the last owner is gone.

    A STALE MARKER IS NOT A GATE — dead pid, foreign boot, or a recycled pid
    that started at a different tick all read as no owner at all.

    THE LEGACY SINGLE FILE IS STILL HONOURED, because a gate that was already
    running when this upgrade landed wrote one, and treating it as absent would
    silently un-guard exactly the window the rung exists to close."""
    live, unread = [], []
    d = inflight_dir(repo)
    names = []
    if d:
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            pass                  # never gated: a TRUE empty, not blindness
        except OSError as err:
            unread.append("the owner directory could not be listed (%s)"
                          % (getattr(err, "strerror", None) or err))
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with pk.open_regular(os.path.join(d, name), encoding="utf-8") as fh:
                row = json.load(fh)
        except FileNotFoundError:
            continue              # closed between listdir and open: gone
        except OSError as err:
            unread.append("owner file %s could not be read (%s)"
                          % (name, getattr(err, "strerror", None) or err))
            continue
        except (ValueError, TypeError):
            continue              # bytes read and rejected: stale, measured
        if isinstance(row, dict) and _inflight_owner_live(row):
            live.append(row)
    legacy = inflight_path(repo)
    if legacy:
        try:
            with pk.open_regular(legacy, encoding="utf-8") as fh:
                row = json.load(fh)
            if isinstance(row, dict) and _inflight_owner_live(row):
                live.append(row)
        except FileNotFoundError:
            pass                  # no legacy marker: the normal state
        except OSError as err:
            unread.append("the legacy marker could not be read (%s)"
                          % (getattr(err, "strerror", None) or err))
        except (ValueError, TypeError):
            pass                  # bytes read and rejected: stale, measured
    else:
        # inflight_path/inflight_dir are a pair: both None means the room's
        # admin dir itself would not resolve, so NEITHER source was looked at.
        unread.append("the room's git admin dir could not be resolved")
    reason = "; ".join(unread) or None
    if live:
        oldest = min(live, key=lambda r: str(r.get("ts") or ""))
        return GateCensus(GATE_LIVE, (int(oldest.get("pid") or 0),
                                      str(oldest.get("ts") or "")), reason)
    if unread:
        return GateCensus(GATE_UNREADABLE, None, reason)
    return GateCensus(GATE_EMPTY, None, None)


def inflight(repo):
    """(pid, ts) for a LIVE gate in this room, or None.

    A PROJECTION of `inflight_census`, byte-for-byte the old contract: LIVE
    projects to its (pid, ts) tuple, and BOTH empty and unreadable project to
    None — which is exactly the conflation the census exists to escape, kept
    here deliberately so no existing caller changes behaviour. A caller for
    whom "could not look" must not read as "nothing there" — task/112's
    stop-guard read is the measured specimen — reads the census, not this."""
    return inflight_census(repo).live


STDERR_TAIL_CAP = 16384          # the most we will ever keep, in BYTES
STDERR_TAIL_FLOOR = 512          # under this, a tail is not worth its risk


def _row_fits(row):
    """Does this EXACT row survive `eventledger.append_unlocked`'s size rule?

    Weighed, never predicted. Two drafts of this feature tried to compute the
    room left for a tail and BOTH were wrong, because the serialized size of a
    string is not a function of its byte count: JSON escaping turns one control
    byte into six characters — measured: 16,384 control bytes became
    98,430 bytes against a fixed-headroom estimate that allowed for 256. The
    only honest answer is to serialize the candidate and weigh it.
    """
    try:
        payload = json.dumps(row, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return len(payload) + 1 <= eventledger.MAX_EVENT_BYTES   # +1 for "\n"


def _stderr_tail(out, row, source=None):
    """(text, meta) for a NOT-OK row, or (None, None) when nothing is kept.

    `source` names what stream the tail is OF when it is not unittest's stderr:
    a declared `exit`-protocol command contributes its combined stdout+stderr
    under `DECLARED_OUTPUT_SOURCE`, and the meta says so, because "stderr_tail"
    is the field's NAME and a reader of a declared receipt must not conclude
    the command's stdout was dropped. unittest rows pass nothing and their meta
    keeps the two keys it always had.

    Offers the largest tail that leaves `row` ACTUALLY appendable and halves it
    until one fits, weighing each candidate with `_row_fits` rather than
    computing a budget. Two earlier drafts computed one and both were wrong —
    first in characters, then in raw bytes plus fixed headroom — because JSON
    escaping is not a function of length. This shape cannot be wrong by the
    same class of arithmetic, because it never does the arithmetic.

    IT CAN STILL REFUSE, AND REFUSING IS THE CORRECT OUTCOME when the bare row
    is already at the ceiling: the receipt outranks the diagnostic, always.
    `eventledger.append_unlocked` DROPS an oversized payload rather than
    trimming it, so a tail that does not fit does not cost the tail — it costs
    the whole red receipt.

    The slice is taken from the END in bytes and then decoded with
    errors="ignore", which drops a partial multibyte character at the CUT (the
    front) and never at the tail — so the last line, which is the one that
    matters, always survives intact.

    `meta` carries what was thrown away: total_bytes is the child's whole
    stderr and truncated says the cut happened. A reader must be able to tell a
    COMPLETE tail from a WINDOW of a much larger one, because "no traceback
    above this line" means opposite things in the two cases — the same
    absence-versus-cannot-look distinction the receipt already draws for
    failures with failures_unreadable.

    WHY THIS EXISTS, measured. The child is launched with
    stderr=PIPE and `parse_result` mines it for a summary and a truncated
    per-failure traceback — and then the buffer is dropped. Nothing else keeps
    it: the fab job log holds only the wrapper (twelve lines), and the fetched
    artifact directory holds the receipt, the provenance and a helm-home. So a
    red receipt named a failing test and DESTROYED the only copy of why it
    failed, which is why task/1034 accumulated 19 instances across two weeks
    without ever producing a diagnosis. The evidence was not truncated
    anywhere; it was never written down.

    THE TAIL AND NOT THE HEAD: a suite prints 12k dots before it prints the
    thing that killed it, so the front of this buffer is the least useful part
    of it.

    DIAGNOSTIC ONLY — IT AUTHORIZES NOTHING AND IS BOUND TO NOTHING. It is
    deliberately absent from the content-id grammar in `_receipt_id`, so it
    perturbs no existing receipt id and a v4 row keeps the byte shape every
    reader already expects. The cost of that choice, stated plainly because a
    reviewer should weigh it rather than discover it: the field is EDITABLE
    without invalidating the receipt it rides on. That is acceptable only for
    as long as it stays a LEAD — something that tells a human where to look —
    and never becomes an input to bind(), a verdict, or a land door. The moment
    anything RELIES on it, it belongs in the grammar behind a version bump,
    because the grammar's whole job is to bind what a reader relies on. Do not
    read this field in code.

    Returns (None, None) when there is nothing to keep, so a row without a tail
    is silent rather than carrying an empty string that reads like a
    measurement that came back blank.
    """
    text = out or ""
    if not text.strip():
        return None, None
    raw = text.encode("utf-8")
    total = len(raw)
    keep = min(total, STDERR_TAIL_CAP)
    while keep > 0:
        kept = raw[-keep:].decode("utf-8", errors="ignore")
        if kept:
            truncated = keep < total
            # THE FLOOR JUDGES A FRAGMENT, NEVER A WHOLE STREAM. An earlier
            # loop bounded on `keep >= FLOOR` and so dropped every COMPLETE
            # stderr shorter than 512 bytes — which is the MOST useful case
            # there is, a short traceback that needed no cutting at all. The
            # floor exists to refuse a 40-byte SHRED of something much larger,
            # where the reader cannot see enough to learn anything and the row
            # carries the risk for nothing. A complete short stream is not a
            # shred; it is the whole answer.
            if truncated and keep < STDERR_TAIL_FLOOR:
                return None, None
            meta = {"total_bytes": total, "truncated": truncated}
            if source:
                meta["source"] = source
            if _row_fits(dict(row, stderr_tail=kept, stderr_tail_meta=meta)):
                return kept, meta
        keep //= 2
    return None, None


def _mint_result(repo, head, tree, dirty, ident, cmd, suite, label, rc,
                 started, out, legacy=None, focus=None, module_timing=_NO_TIMING,
                 command=None, sidecar=None):
    """Bracket, parse, reconcile and append one completed child result.

    `focus` is the measured plan from `focus_plan`, or None. A focused row
    mints at v6 — the version whose content id binds the `suite` flag and the
    whole focus block — while whole-suite and custom rows STAY v4, so every
    receipt the fleet already mints keeps its byte shape and its id, and
    only the new kind asks readers to know the new version.

    THE SCOPE IS COMPLETED HERE, NOT ACCEPTED HERE. The plan arrives as a
    request; this function is the only place holding the child's OUTPUT, so
    it is where the answer — which modules actually reported tests, and how
    many ids they reported — joins the block that gets hashed. A `focus`
    dict is never written to the ledger without that half."""
    after_head, after_tree, after_dirty, after_err = tree_state(repo)
    # THE DECLARED KIND IS THE ONE THIS PROJECT ASKED FOR. helm's own default
    # command mints exactly the rows it always did — same version, same keys,
    # same id — so every receipt the fleet already holds keeps its byte shape
    # and only the new kind asks a reader to know the new version.
    declared = command if isinstance(command, dict) \
        and command.get("source") == "registry" else None
    protocol = (command or {}).get("protocol") or PROTOCOL_UNITTEST
    parsed = _exit_protocol_result(rc, cmd, sidecar=sidecar) \
        if protocol == PROTOCOL_EXIT else parse_result(out)
    if focus:
        ran_modules, ran_ids = _executed_modules(out)
        focus = dict(focus, executed=ran_modules, executed_ids=ran_ids)
    full_failures = parsed["failures"]
    # A FOCUSED RUN DOES NOT CHUNK, and that is a scope limit, not an
    # oversight. The focused kind's whole claim is its `focus` block and the
    # bounded failure record is the v8 ladder rung; one receipt cannot be both
    # kinds under one version int without making every reader call the other
    # kind's honest rows tampered. A focused run over more than FAILURE_CAP
    # failures therefore keeps the OLD capped-with-a-marker diagnostics — the
    # residual this lane cures for the whole-suite gate and deliberately does
    # not cure here, because composing the two grammars is a new version and
    # owes its own reviewed land. `_show_failures` still renders the legacy
    # marker honestly ("their diagnostics and identities were not recorded").
    if focus:
        failure_record, chunks = {"failures": _capped_failures(full_failures)}, []
    else:
        failure_record, chunks = _failure_record(full_failures) \
            if len(full_failures) > FAILURE_CAP \
            else ({"failures": full_failures}, [])
    version = FOCUSED_VERSION if focus \
        else MINTED_RECEIPT_VERSIONS[1 if chunks else 0]
    row = {"v": version, "event": "gate", "ts": pk.now_ts(),
           "repo_id": repo, "head": head, "tree": tree, "dirty": dirty,
           "head_after": after_head, "tree_after": after_tree,
           "dirty_after": True if after_err else after_dirty,
           "interpreter": ident, "host": host(), "argv": cmd, "suite": suite,
           "label": (label or "").strip()[:120] or None,
           "rc": rc, "wall": round(time.time() - started, 3)}
    if focus:
        row["focus"] = focus
    if declared:
        # WHAT THE RECEIPT MUST SAY ABOUT ITSELF, because it travels: a reader
        # on another host has no registry of ours to consult, so the command,
        # where it came from and how its verdict was read ride IN the row and
        # are bound by its id. `row_refusal` re-reads `argv` against this block,
        # which is what stops a whole-suite claim from being pasted onto a
        # command the mint never composed.
        row["suite_command"] = {"source": declared["source"],
                                "argv": list(declared["argv"]),
                                "protocol": declared["protocol"],
                                "project": declared.get("project")}
    row.update(status=parsed["status"], ran=parsed["ran"],
               skipped=parsed["skipped"], detail=parsed["detail"],
               elapsed=parsed["elapsed"],
               failures_unreadable=parsed["failures_unreadable"])
    row.update(failure_record)
    # Summary and exit code are independent signals; disagreement is UNKNOWN.
    if rc is None:
        row["status"] = "UNKNOWN"
        row["detail"] = ("the runner never exited (timeout)%s" % (
            "; last summary said " + parsed["status"]
            if parsed["status"] != "UNKNOWN" else ""))
        row["failures_unreadable"] = True
    elif row["status"] == "OK" and rc != 0:
        row["status"] = "UNKNOWN"
        row["detail"] = ("the summary says OK and the runner exited %s — two "
                         "sources disagree%s" % (rc, "; " + parsed["detail"]
                                                 if parsed["detail"] else ""))
        row["failures_unreadable"] = True
    elif row["status"] == "FAILED" and rc == 0:
        row["status"] = "UNKNOWN"
        row["detail"] = ("the summary says FAILED and the runner exited 0 — two "
                         "sources disagree%s" % ("; " + parsed["detail"]
                                                 if parsed["detail"] else ""))
        row["failures_unreadable"] = True
    # WHOSE failure is this? Only a whole-suite FAILED can ask: a custom
    # command's failure identities are not minted by unittest's protocol, and
    # any other status has nothing to attribute. The answer is advisory —
    # bind() never reads it — but it is IN the receipt, because the stale-base
    # investigations that motivated it each re-derived it from data the gate
    # already had (see the stale-base block above).
    # AND IT IS BOUNDED TO FAILURE_CAP FAILURES, DELIBERATELY. Past the cap the
    # check short-circuits to UNKNOWN before it opens git at all, so a
    # 130-failure run never spawns reference suite runs at mint time. That is a
    # COST BOUND, pinned by
    # `test_large_failure_record_feeds_base_check_only_the_bounded_projection`,
    # which fails the mint if a second `vcs.backend` is ever opened. Do not
    # "fix" the truncation marker away by handing this call `full_failures`:
    # the marker IS the mechanism of the bound, and removing it buys attribution
    # on large runs with unbounded mint-time work.
    #
    # WHAT WAS WRONG WAS THE SENTENCE, NOT THE BOUND. The refusal read "N
    # failures beyond the 20-entry cap were NEVER IDENTIFIED and were SKIPPED",
    # which for a v8 row is false in its first clause: `_failure_identity_chunks`
    # has already written every identity into the receipt, and its own docstring
    # says those chunks carry "the kind and canonical test id that base-check
    # consumes". So the receipt said "your identities are gone" when it meant
    # "I declined to spend reference runs on a failure set this large", and two
    # seats spent the difference going to node-side logs for ids that were in
    # the receipt (measured on 5d2185eb69f5f335, task/1887: 55 failures, 35
    # declared unrecorded while its own import line named the chunk holding
# every one of them).
    # `chunked` exists only to word that sentence truthfully; it changes no
    # verdict and gates no work.
    #
    # `_capped_failures` rather than a second inline prefix-plus-marker: that
    # function's docstring asks for exactly this, so the behaviour "cannot
    # spread back by copy". It had already spread back by copy, here.
    # AND ONLY UNDER UNITTEST'S PROTOCOL. `_base_check` re-runs
    # `python -m unittest <ids>` against the base, so it is an instrument for
    # exactly one grammar: under `exit` there are no test identities to
    # attribute and asking would spawn python over a project that does not
    # run python at all.
    row["base_check"] = _base_check(repo, _capped_failures(full_failures),
                                    row["failures_unreadable"],
                                    chunked=bool(chunks)) \
        if suite and protocol == PROTOCOL_UNITTEST \
        and row["status"] == "FAILED" else None
    row["id"] = _receipt_id(row)
    timing_event = _module_timing_event(
        row, module_timing or _unknown_timing("module timing was not captured")) \
        if suite and module_timing is not _NO_TIMING else None
    # AFTER the id, deliberately: see `_stderr_tail`. A red receipt that names
    # a failing test and destroys the only copy of WHY it failed is why this
    # exists, and it must never become something a reader can rely on.
    # The budget is measured on the row AS IT NOW STANDS, so it accounts for a
    # long failure list or a fat argv rather than assuming a typical row.
    # A DECLARED `exit` COMMAND KEEPS ITS TAIL AT EVERY STATUS, and it is the
    # COMBINED stdout+stderr (`run` composes `out` that way for this protocol
    # only). Its receipt has no failure list and no count — the exit status is
    # the whole verdict — so the tail is the only thing in the row a reader can
    # diagnose from, red or green: measured twice on another project's lane
    # (receipts a973a6f73ea9e292 and 304cfecb480082c4, both rc 1) before this
    # existed, a red declared receipt could not say why, because the suite's
    # stdout went nowhere and only unittest's stderr was ever kept. unittest
    # rows keep the rule they had: stderr, on a NOT-OK row only.
    if protocol == PROTOCOL_EXIT:
        tail, meta = _stderr_tail(out, row, source=DECLARED_OUTPUT_SOURCE)
    elif row["status"] != "OK":
        tail, meta = _stderr_tail(out, row)
    else:
        tail = meta = None
    if tail:
        row["stderr_tail"], row["stderr_tail_meta"] = tail, meta
    # AND THE DURABILITY CHECK COMES AFTER THE TAIL, because the tail is the
    # last thing that adds bytes to the row. Measuring before it would prove a
    # row durable that is not the row being appended.
    try:
        oversized = _event_bytes(row) > eventledger.MAX_EVENT_BYTES
    except (TypeError, ValueError) as exc:
        return row, False, "main receipt is not durable UTF-8 JSON: %s" % exc
    if oversized:
        return row, False, ("main receipt exceeds the %d-byte ledger limit; "
                            "base-check diagnostics are not mintable" %
                            eventledger.MAX_EVENT_BYTES)

    def append():
        # One ledger lock, chunks first, receipt last. A crash may leave inert
        # orphan chunks; it can never leave a bindable receipt whose evidence
        # was not already durable under the same serialization boundary.
        path = receipts_path()
        with eventledger.locked(path) as held:
            if not held:
                return False
            # Failure chunks are authoritative receipt content; advisory timing is
            # not. A timing write failure must never discard a completed suite or
            # prevent the receipt from minting. The receipt remains LAST.
            for event in chunks:
                if not eventledger.append_unlocked(path, event):
                    return False
            if timing_event:
                eventledger.append_unlocked(path, timing_event)
            if not eventledger.append_unlocked(path, row):
                return False
        return True
    if legacy:
        minted, err = legacy.finalize(append)
        return row, bool(minted), err
    return row, append(), None


CROSS_TREE_OVERRIDE = "HELM_CROSS_TREE_GATE"


def _warn_trunk_deletions(repo):
    """Name the trunk symbols this lane removes, on stderr, before the gate
    spends. Returns nothing and refuses nothing — see `deletion_rung` for why
    the measured 23% fire rate makes a refusal here self-defeating.

    HERE AND NOT IN A PRE-COMMIT RUNG, because the deletion this catches never
    appears in a commit the author wrote — it arrives inside a rebase's own
    auto-merge. The lane-versus-trunk view is the only one that can see it, and
    the gate is the first place every lane passes through carrying that view.

    NEVER RAISES AND NEVER BLOCKS. A warning that can break the mint is a
    warning someone removes; if it cannot answer, it says so and the gate runs.
    """
    try:
        from . import deletion_rung
        rows, err = deletion_rung.scan(repo)
        if err == deletion_rung.NOT_APPLICABLE:
            return                  # no trunk ref: no question to answer here
        if err:
            print("[helm deletion-rung] not run: %s" % err, file=sys.stderr)
            return
        for line in deletion_rung.report(rows):
            print(line, file=sys.stderr)
    except Exception as e:                                  # noqa: BLE001
        print("[helm deletion-rung] not run: %s" % e, file=sys.stderr)


def _cross_tree_refusal(repo):
    """A reason string when this gate would test a tree it is not standing in,
    else None. `repo` is the EFFECTIVE repository, already resolved.

    WHY THE MINT REFUSES WHERE THE REST OF helm ONLY WARNS.
    `cli.which_helm_warning` is deliberately detect-and-report — its own
    docstring says "never refuse" — because a warning beside a wrong answer
    still lets an operator read the answer. A GATE RECEIPT IS NOT AN ANSWER,
    IT IS THE CLAIM THAT AUTHORISES A LAND. Measured: a seat with its shell in
    one tree ran another tree's suite, got a green, and read it as its lane's
    green; the after-the-fact warning printed next to "OK" was noise. The
    dangerous direction is the PASS — the other tree already carries an
    equivalent fix, so the dogfood succeeds and a broken version ships behind
    it. Detect-and-report cannot reach that, because there is no wrong answer
    on screen to be suspicious of.

    THE EFFECTIVE REPO, NEVER THE FLAG TOKEN (land-request row 6d41adc116e3,
    finding 1 — reproduced exactly as written). THE ROW ID AND
    NOT THE REVIEWED TIP, deliberately: that tip is a lane sha this lane was
    re-derived away from, no branch contains it, and a fresh clone cannot
    resolve it at all — a citation only the author's machine can follow is not
    provenance. The ledger row outlives every rebase. The first draft asked whether
    the string "--repo" appeared in argv. `run()` resolves `repo or
    os.getcwd()`, so `gate run --repo`, `--repo ""` and a `--repo` whose value
    was eaten by the next flag all presented a token while gating the cwd.
    This takes the resolved value as an argument and derives nothing itself:
    there is no second derivation to drift from the first.

    IT SITS BEFORE `_acquire_gate`, so a refused run never takes a FIFO
    position and starts nothing — and it returns a REASON rather than
    printing, so `_cmd_run`'s existing `--json` error path renders it as JSON
    instead of prose (finding 2, answered by placement rather than by a second
    printer).

    THE OVERRIDE CANNOT BE SILENCED (finding 3). An operator sometimes must
    gate a tree from outside it, so HELM_CROSS_TREE_GATE=1 admits the run —
    but it always says so, on stderr, and HELM_NO_TREE_WARNING DOES NOT REACH
    IT. Those two variables answer different questions: one quiets a routine
    advisory, the other spends the authority of a land-bearing receipt. The
    combination that silently admitted was the bypass, and an escape hatch
    nobody can see is not an escape hatch.
    """
    from . import cli
    warning = cli.which_helm_warning(cwd=repo)
    if not warning:
        return None
    if os.environ.get(CROSS_TREE_OVERRIDE) == "1":
        print("helm gate: CROSS-TREE RUN ADMITTED by %s=1 — this receipt "
              "describes %s, which is NOT the tree this helm came from. "
              "%s does not silence this line."
              % (CROSS_TREE_OVERRIDE, repo, "HELM_NO_TREE_WARNING"),
              file=sys.stderr)
        return None
    return ("refusing to mint a receipt for a tree this helm did not come "
            "from — the receipt would authorise a land it did not test. %s "
            "Run ./bin/helm from %s, or set %s=1 to admit it on the record."
            % (warning.replace("\n", " "), repo, CROSS_TREE_OVERRIDE))


# ------------------------------------------------------------------ focus
#
# THE FOCUSED GATE. The whole-suite run is the expensive unit of this
# system: every cure round pays for the entire suite, the runs serialize
# behind one FIFO, and a run that loses that queue comes back RED for
# CONTENTION rather than for code — which buys another cure round, which
# buys another whole-suite run. A cure round does not need the whole suite;
# it needs the tests the change can actually REACH. The land still needs
# everything — `landgate.gate_binds_tree`, `foldcheck` and the APPROVE
# binding refuse anything narrower — so focus shortens the cure loop and
# never the authority chain.
#
# SCOPE IS MEASURED, NEVER ACCEPTED. No caller hands this module a file list
# or a test list: `focus_plan` derives the changed set from the tree the same
# way `_base_check` does (merge-base with trunk; committed, uncommitted AND
# untracked), derives the consumers by READING every import in the repo, and
# `_mint_result` binds the whole plan into the receipt's content id (v6). A
# caller-supplied scope would be a guard fed its own answer — the exact shape
# `foldcheck._tree_matches_gate`'s docstring refuses for trees. `bind`
# re-derives the changed set AGAIN at consumption time and refuses any
# receipt whose recorded scope does not cover it, so the claim is checked by
# two independent measurements bracketing the run, not by the caller's word.

FOCUS_POLICY = "changed+importers-v2"

# A focus block must fit inside one ledger event with the rest of its row
# (eventledger.MAX_EVENT_BYTES = 64KB). A change big enough to blow this is a
# change whose closure is the suite anyway; the refusal names the honest cure.
_FOCUS_PLAN_BYTES = 32 * 1024


def _module_name(rel):
    """Dotted module name for a repo-relative .py path, or None when the path
    cannot be a Python module (wrong suffix, or a segment no import statement
    could name)."""
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def _is_test_module(name):
    """Is this dotted module one the whole-suite discovery would run?
    Mirrors SUITE exactly: rooted at `tests`, default `test*.py` pattern.
    Two parts minimum — the bare `tests` package starts with "test" too, and
    counting it once inflated the universe and put a package name in the
    selection."""
    parts = name.split(".")
    return len(parts) >= 2 and parts[0] == "tests" \
        and parts[-1].startswith("test")


def _import_aliases(tree, pkg):
    """{local name: the dotted thing this file's import statements bind it
    to}, relative imports resolved against `pkg` (the owning package parts,
    empty for a standalone snippet). Extracted from `_module_reads` so the
    snippet reader can build the same map and answer the same way about a
    renamed mechanism."""
    import ast
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = \
                    alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                head = pkg[:len(pkg) - (node.level - 1)] \
                    if node.level > 1 else list(pkg)
                if node.level - 1 > len(pkg):
                    continue
            else:
                head = []
            base = head + (node.module.split(".") if node.module else [])
            for alias in node.names:
                aliases[alias.asname or alias.name] = ".".join(
                    base + [alias.name]) if base else alias.name
    return aliases


_IMPORT_MECHANISMS = ("import_module", "__import__")


def _mechanism_tail(name):
    """Is this bare CALL-SITE name an import mechanism this reader knows?
    Returns it back (truthy) or "". The `_lazy` / `lazy_import` naming
    family counts here because it is a claim about what the author called
    the thing AT THE CALL SITE — it is deliberately NOT resolved through
    renames, see `_import_rebinds`."""
    name = str(name or "")
    return name if name in _IMPORT_MECHANISMS or "lazy" in name.lower() else ""


def _callee_name(func):
    """The identifier this call target is spelled with: an Attribute by its
    ATTRIBUTE name (`il.import_module`, `self._imp`), a Name by its id,
    anything cleverer by "". The RAW spelling — renames widen it in
    `_importish_callee` / `_strict_importish` rather than replacing it."""
    import ast
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _import_rebinds(tree, aliases):
    """{local name: the DEFINITE import mechanism it denotes} for one parsed
    file — the extra names `importlib.import_module` / `__import__` answer
    to here, and nothing else.

    Two sources, both READ: this file's import aliases (`from importlib
    import import_module as load`), and its own assignments of a mechanism
    to a name (`_imp = importlib.import_module`, `bi = __import__`,
    `self._imp = importlib.import_module`), resolved through those aliases
    by `_dotted_base`.

    DEFINITE ONLY, never the `lazy` naming family: a name is admitted here
    because it PROVABLY denotes an import mechanism, not because it reads
    like one. `from django.utils.translation import gettext_lazy as _` is
    the counter-example the bound exists for — admit the lazy family and
    every `_("...")` in that file becomes a string import, so ordinary
    translated text is harvested as module names, and any library that
    spells a non-importing helper with a lazy-sounding alias does the same.
    The lazy family is a claim about the SPELLING AT THE CALL SITE
    (`_mechanism_tail`), never about what a rename points at.

    FILE-WIDE AND UNION, deliberately not flow-sensitive: a name bound to a
    mechanism ANYWHERE here resolves here even when another statement
    rebinds it to something else. The two possible readings of an ambiguous
    name are "consumer of everything" (over-selects: costs test time) and
    "ordinary call" (under-selects: a focused gate goes green without ever
    running the tests that would have caught the change), so this reader
    takes the first. Attribute TARGETS are keyed by their attribute name for
    the same reason — an unrelated `.load` elsewhere in the file inherits
    the wider reading, which is the safe one."""
    import ast
    out = {}

    def definite(dotted):
        tail = str(dotted or "").rpartition(".")[2]
        return tail if tail in _IMPORT_MECHANISMS else ""

    for name, dotted in aliases.items():
        tail = definite(dotted)
        if tail:
            out[name] = tail

    def rebind(target, value):
        if isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, (ast.Tuple, ast.List)) \
                    and len(value.elts) == len(target.elts):
                for pair in zip(target.elts, value.elts):
                    rebind(*pair)
            return
        tail = definite(_dotted_base(value, aliases))
        if not tail:
            return
        if isinstance(target, ast.Name):
            out[target.id] = tail
        elif isinstance(target, ast.Attribute):
            out[target.attr] = tail

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                rebind(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
            rebind(node.target, node.value)
    return out


def _importish_callee(func, rebinds=None):
    """Does this call target look like a string-import mechanism? Matches
    `importlib.import_module` / `__import__` by name and the `_lazy` /
    `lazy_import` naming family — the shapes this repo actually uses — plus
    any rename `_import_rebinds` could read."""
    return bool(_mechanism_tail(_callee_name(func))) \
        or _strict_importish(func, rebinds)


def _strict_importish(func, rebinds=None):
    """The subset of `_importish_callee` that is DEFINITELY an import: the
    lazy-name family is a harvesting heuristic (extra edges are safe), but
    only a definite mechanism may declare a module an unbounded consumer —
    a foreign library's `.lazy()` must not poison a whole file.

    A RENAME MAY ONLY WIDEN THIS ANSWER, NEVER REPLACE IT, and that is the
    whole shape of both predicates above. Substituting the resolved name for
    the spelled one — `_callee_name(func, rebinds) in _IMPORT_MECHANISMS` —
    reads correctly and is the same defect one layer down: in a file holding
    `import_module = _lazy_loader` (or `from x import lazy_thing as
    import_module`) it turns a REAL `import_module(os.environ["M"])` from
    definite into merely-lazy, `consumes_all` goes False, and the plan
    under-selects in silence. That is a rename map making the reading WEAKER
    than no map at all — fail-closed without it, fail-open with it. Reading
    raw-OR-rebound makes the downgrade unrepresentable: extra knowledge can
    only add readings."""
    name = _callee_name(func)
    return name in _IMPORT_MECHANISMS \
        or (rebinds or {}).get(name, "") in _IMPORT_MECHANISMS


def _import_target(call):
    """The expression naming what a definite import call will import, or
    None when the call names it in a way this reader cannot see.

    First positional, else the `name` keyword — the parameter both
    `importlib.import_module(name, package)` and `__import__(name, ...)`
    call it. READING ONLY `call.args` IS A SILENT UNDER-SELECT: a
    keyword-passed dynamic import (`import_module(name=os.environ["M"])`)
    then produces neither an edge nor `consumes_all`, so the plan drops
    tests with nothing to show that it did. A definite import whose target
    this returns None for (`import_module(**kw)`) is an import nothing here
    can bound, and its caller widens rather than skipping it."""
    target = call.args[0] if call.args else None
    for kw in call.keywords:
        if kw.arg == "name":
            target = kw.value
    return target


# Process-spawn entry points, keyed by the module that owns them. A consumer
# wired only through a child process is invisible to import statements, and
# omitting it silently was measured as a real hole (a test that shells out to
# the module it exercises was never selected while err stayed null).
_SPAWN_CALLEES = {
    "subprocess": frozenset(("run", "Popen", "call", "check_call",
                             "check_output")),
    "os": frozenset(("system", "popen", "execl", "execle", "execlp",
                     "execv", "execve", "execvp", "execvpe", "spawnl",
                     "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
                     "spawnvp", "spawnvpe")),
}
_PATH_PASSTHROUGH = frozenset(("abspath", "realpath", "normpath"))


def _dotted_base(node, aliases):
    """The dotted name a Name/Attribute chain denotes, through this file's
    import aliases, or None for anything cleverer than a plain chain."""
    import ast
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        inner = _dotted_base(node.value, aliases)
        return inner + "." + node.attr if inner else None
    return None


def _scope_assigns(scope):
    """{name: value-expr, or None when reassigned} for one scope's own simple
    single-target assignments. Nested function/class bodies are their own
    scopes and are skipped; a name bound twice is AMBIGUOUS and never
    resolves, because picking either binding would be flow analysis this
    reader does not perform."""
    import ast
    out = {}

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Assign) and len(child.targets) == 1 \
                    and isinstance(child.targets[0], ast.Name):
                name = child.targets[0].id
                out[name] = None if name in out else child.value
            walk(child)
    walk(scope)
    return out


def _dir_of(mod, packages):
    """Repo-relative directory holding a module's file."""
    parts = mod.split(".")
    return "/".join(parts if mod in packages else parts[:-1])


def _resolve_value(expr, scopes, ctx, depth=0):
    """One argument expression, resolved as far as READING allows. -> a tag:
    ("str", s) exact text | ("prefix", s) constant head of a dynamic string |
    ("prefix-param", s, name) that head plus a parameter of the enclosing
    function | ("param", name) | ("sysexe",) sys.executable |
    ("selffile",) __file__ | ("modfile", dotted) an imported repo module's
    __file__ | ("dir", rel) a repo-relative directory | ("unknown",).

    NEVER GUESSES: a name assigned twice, an f-string with a leading hole, a
    call this table does not know — all come back unknown, and unknown is
    what the callers fail closed on."""
    import ast
    if depth > 8:
        return ("unknown",)
    if isinstance(expr, ast.Constant):
        return ("str", expr.value) if isinstance(expr.value, str) \
            else ("unknown",)
    if isinstance(expr, ast.Name):
        if expr.id == "__file__":
            return ("selffile",)
        if expr.id in ctx["params"]:
            return ("param", expr.id)
        for frame in reversed(scopes):
            if expr.id in frame:
                value = frame[expr.id]
                return ("unknown",) if value is None else _resolve_value(
                    value, scopes, ctx, depth + 1)
        return ("unknown",)
    if isinstance(expr, ast.Attribute):
        base = _dotted_base(expr.value, ctx["aliases"])
        if base == "sys" and expr.attr == "executable":
            return ("sysexe",)
        if expr.attr == "__file__" and base:
            hits = ctx["local"](base)
            if hits:
                return ("modfile", max(hits, key=len))
        return ("unknown",)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _resolve_value(expr.left, scopes, ctx, depth + 1)
        right = _resolve_value(expr.right, scopes, ctx, depth + 1)
        if left[0] == "str" and right[0] == "str":
            return ("str", left[1] + right[1])
        if left[0] == "str" and right[0] == "param":
            return ("prefix-param", left[1], right[1])
        if left[0] == "str":
            return ("prefix", left[1])
        return ("unknown",)
    if isinstance(expr, ast.JoinedStr):
        parts = []
        for piece in expr.values:
            if isinstance(piece, ast.Constant) \
                    and isinstance(piece.value, str):
                parts.append(piece.value)
                continue
            return ("prefix", "".join(parts)) if parts else ("unknown",)
        return ("str", "".join(parts))
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        base = _dotted_base(expr.func.value, ctx["aliases"])
        attr = expr.func.attr
        if base in ("os.path", "posixpath") and not expr.keywords:
            args = [_resolve_value(a, scopes, ctx, depth + 1)
                    for a in expr.args]
            if attr in _PATH_PASSTHROUGH and len(args) == 1:
                return args[0]
            if attr == "dirname" and len(args) == 1:
                inner = args[0]
                if inner[0] == "selffile":
                    return ("dir", _dir_of(ctx["mod"], ctx["packages"]))
                if inner[0] == "modfile":
                    return ("dir", _dir_of(inner[1], ctx["packages"]))
                if inner[0] == "str":
                    return ("str", inner[1].rsplit("/", 1)[0]
                            if "/" in inner[1] else "")
                return ("unknown",)
            if attr == "join" and args \
                    and all(t[0] == "str" for t in args[1:]):
                tail = "/".join(t[1] for t in args[1:])
                head = args[0]
                if head[0] == "str":
                    return ("str", (head[1].rstrip("/") + "/" + tail)
                            if head[1] else tail)
                if head[0] == "dir":
                    return ("str", (head[1] + "/" + tail)
                            if head[1] else tail)
        return ("unknown",)
    return ("unknown",)


def _argv_elements(expr, scopes, ctx, depth=0):
    """A spawn's command, flattened to resolved element tags. -> (tags,
    complete). Chases names through the scope maps and list/tuple literals,
    `list(...)`/`tuple(...)` wrappers and `+` concatenation; anything it
    cannot flatten contributes an ("unknown",) element."""
    import ast
    if depth > 8:
        return [("unknown",)], False
    if isinstance(expr, (ast.List, ast.Tuple)):
        tags, complete = [], True
        for el in expr.elts:
            if isinstance(el, ast.Starred):
                tags.append(("unknown",))
                complete = False
                continue
            tags.append(_resolve_value(el, scopes, ctx))
        return tags, complete
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, lc = _argv_elements(expr.left, scopes, ctx, depth + 1)
        right, rc = _argv_elements(expr.right, scopes, ctx, depth + 1)
        return left + right, lc and rc
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
            and expr.func.id in ("list", "tuple") and len(expr.args) == 1 \
            and not expr.keywords:
        return _argv_elements(expr.args[0], scopes, ctx, depth + 1)
    if isinstance(expr, ast.Name):
        for frame in reversed(scopes):
            if expr.id in frame:
                value = frame[expr.id]
                if value is None:
                    break
                return _argv_elements(value, scopes, ctx, depth + 1)
        return [("unknown",)], False
    tag = _resolve_value(expr, scopes, ctx)
    return [tag], tag[0] != "unknown"


def _snippet_edges(code, local):
    """Repo edges named by an inline `-c` code string. -> (deps, readable,
    bounded). The snippet is real Python handed to a real child, so its
    imports are real consumption; an unparseable snippet is unreadable, not
    empty.

    BOUNDED is the third answer because two answers could not hold it. It
    says this reader could NAME every module the snippet imports. A definite
    mechanism (`_strict_importish`) whose target is not a constant string —
    `load(os.environ["M"])`, `import_module(name=os.environ["M"])`,
    `__import__(**kw)` — is an import nothing here can name, and its caller
    must widen on it rather than take the edges beside it as the answer.

    THE FORBIDDEN OUTCOME IS A CONFIDENT PARTIAL. Under-selection with a
    STATED bound is a limit a caller can act on: told "one hop, no transitive
    rebinds", a reader knows to distrust the edge set. `readable=True` beside
    a populated deps set is this function ASSERTING it resolved the child,
    and nothing downstream can tell that apart from a complete read — so
    there is no surface left on which to state the bound, and the caller
    never learns to look. Measured: a `-c` snippet holding `from importlib
    import import_module as load` + `load(os.environ["M"])` returned exactly
    that, and its parent stayed a confident non-consumer of everything.

    The lazy-name family is deliberately NOT unbounding: it is a harvesting
    heuristic (`_importish_callee`), and a foreign library's `.lazy()` may
    not declare a whole child unreadable — the same bound `_strict_importish`
    carries one layer up. A snippet that spawns its OWN child is outside this
    reader on purpose; `-c` code is read for imports, not recursively."""
    import ast
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return set(), False, False
    deps, bounded = set(), True
    # The snippet is a module of its own, so it gets its own rename map: a
    # `-c` child that renames its loader must not read as a plain call here
    # either.
    rebinds = _import_rebinds(tree, _import_aliases(tree, []))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps |= local(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module \
                and not node.level:
            deps |= local(node.module)
            for alias in node.names:
                # `from <pkg> import <mod>` names a SUBMODULE in the alias
                # position — the same resolution `_module_reads` does for the
                # repo's own files. Resolving only `node.module` here would
                # drop that edge while still answering readable-and-bounded,
                # which is the confident partial this door exists to forbid:
                # the snippet's real consumer would be invisible AND the
                # child would look proven.
                deps |= local(node.module + "." + alias.name)
        elif isinstance(node, ast.Call) \
                and _importish_callee(node.func, rebinds):
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(arg, ast.Constant) \
                        and isinstance(arg.value, str):
                    deps |= local(arg.value)
            if _strict_importish(node.func, rebinds):
                target = _import_target(node)
                if not (isinstance(target, ast.Constant)
                        and isinstance(target.value, str)):
                    bounded = False
    return deps, True, bounded


def _spawn_reads(tags, complete, ctx):
    """What one spawned child provably touches. -> (deps, opaque).

    Element strings resolve to repo modules by dotted name, by path, by a
    constant `-m` target (plus its `__main__`), or through a constant `-c`
    snippet's own imports; a shell string is split into tokens first, and a
    token carrying `$` or a backtick is unresolvable by construction.

    OPAQUE — the child may be running repo code this reader cannot name —
    when the PROGRAM itself is unresolved, when a python child's own `-c`
    program holds an import this reader cannot name (see `_snippet_edges`,
    and note that arm is deliberately not filtered through `proven`), or when
    a python child (a sys.executable reference or a python-named program) has
    unresolved arguments and no proven target. A resolved non-python program
    (git,
    tmux) with unresolved arguments is OUTSIDE this model on purpose: those
    arguments are data to a foreign tool, and calling every such spawn a
    consumer of everything was measured to select the near-whole suite on
    every plan — the whole-suite land door remains the backstop for a
    foreign binary that secretly runs repo code."""
    flat, shredded = [], set()
    for tag in tags:
        if tag[0] == "str" and any(ch.isspace() for ch in tag[1]):
            for token in tag[1].split():
                shredded.add(len(flat))
                flat.append(("unknown",) if "$" in token or "`" in token
                            else ("str", token))
            continue
        flat.append(tag)
    deps, python_marked, unresolved, proven = set(), False, not complete, False
    # THE `-c` PROGRAM, READ WHOLE, off the UNSHREDDED tags. `flat` above
    # splits every whitespace-bearing element into shell words, and a real
    # snippet is nothing but whitespace-bearing — read as words it is only
    # fragments: a first word that need not parse, and loose words any one
    # of which may happen to name a module and mark the child PROVEN off
    # token luck rather than off anything the program actually imports.
    # This pre-pass is the reader that sees a LIST-FORM snippet whole, so
    # the token loop's fragmentary reading is never the only reading a
    # constant `-c` element gets.
    #
    # PURELY ADDITIVE, which is the whole reason it sits beside the tokeniser
    # instead of replacing it: it can only ADD edges and only ADD opacity.
    # The token reading survives underneath, so no spawn this reader used to
    # widen on can stop widening because a whole-snippet read looked tidier —
    # and a snippet doing its own `os.system(x)` stays opaque exactly as the
    # unparseable-fragment path already made it.
    #
    # A SHELL STRING IS THE ROAD THIS PRE-PASS CANNOT SEE: there the `-c`
    # flag and its payload live INSIDE one element, so no `-c` element
    # exists at this level and the token loop below is the only reader the
    # child gets. That road fails closed instead of whole: a `-c` born of
    # the split (`shredded`) whose fragment does not come back readable AND
    # bounded lands in `snippet_opaque`, immune to a loose sibling token
    # that happens to name a module — a fragment may neither vouch for the
    # child nor be vouched for by token luck.
    snippet_opaque = False
    for i, tag in enumerate(tags):
        if not (tag[0] == "str" and tag[1] == "-c"):
            continue
        after = tags[i + 1] if i + 1 < len(tags) else ("unknown",)
        if after[0] != "str":
            continue            # not a constant: the unresolved path has it
        got, readable, bounded = _snippet_edges(after[1], ctx["local"])
        deps |= got
        if not (readable and bounded):
            snippet_opaque = True
    skip = -1
    for i, tag in enumerate(flat):
        if i == skip:
            continue
        kind = tag[0]
        if kind == "sysexe":
            python_marked = True
        elif kind == "selffile":
            proven = True
        elif kind == "modfile":
            deps |= ctx["local"](tag[1])
            proven = True
        elif kind == "str":
            value = tag[1]
            if "python" in value.rsplit("/", 1)[-1]:
                python_marked = True
            follower = flat[i + 1] if i + 1 < len(flat) else ("unknown",)
            if value == "-m" and follower[0] == "str":
                hits = ctx["local"](follower[1]) \
                    | ctx["local"](follower[1] + ".__main__")
                if hits:
                    deps |= hits
                    proven = True
                elif not _is_test_module(ctx["mod"]):
                    # A resolved FOREIGN runner (unittest, pip) in a
                    # NON-test module counts as proven: the child runs a
                    # known outside program, and the runner's remaining
                    # argv is data — ids chosen at runtime from whatever
                    # tree the child runs in, a bound this reader cannot
                    # sharpen without becoming the suite itself (the
                    # measured specimen is the base-check leg re-running
                    # named test ids in a THROWAWAY checkout of another
                    # sha; calling its host a consumer of everything
                    # floored every plan at near-suite width). A TEST
                    # module never takes this exemption — an opaque spawn
                    # there still widens to consumer-of-everything,
                    # because tests are the selection currency and
                    # over-selecting one is cheap.
                    proven = True
                skip = i + 1
            elif value == "-c" and follower[0] == "str":
                got, readable, bounded = _snippet_edges(follower[1],
                                                        ctx["local"])
                deps |= got
                if readable and bounded:
                    proven = True
                elif i in shredded:
                    # A shell-string `-c`: the flag itself was born of the
                    # split, so no unshredded payload exists for the
                    # pre-pass and this fragment is the child's only
                    # reading. Its failure is the child's OPACITY, never a
                    # mere unresolvedness a proven sibling could swallow.
                    snippet_opaque = True
                else:
                    unresolved = True
                skip = i + 1
            elif value.endswith(".py"):
                mod = _module_name(value.lstrip("./"))
                if mod and mod in ctx["known"]:
                    deps.add(mod)
                    proven = True
            else:
                hits = ctx["local"](value)
                if hits:
                    deps |= hits
                    proven = True
        else:
            unresolved = True
    program_known = bool(flat) and flat[0][0] in ("str", "sysexe", "modfile",
                                                  "selffile")
    # `snippet_opaque` is NOT filtered through `proven`, and that placement is
    # the cure: `proven` says "some element named a real target, so the rest
    # is the runner's data", which is true of a trailing test id and false of
    # `-c` — the `-c` argument IS the child program, so an unnameable import
    # inside it is not data beside a known target, it is the target. Measured
    # before the move: `[sys.executable, "-c", SNIPPET, "pkg/alpha.py"]` had
    # the trailing path set `proven`, and the snippet's unbounded loader was
    # swallowed whole. It stays inside `python_marked` because a FOREIGN `-c`
    # (git -c user.email=..., sh -c 'make test') is not python at all — those
    # keep the foreign-binary exemption `_spawn_reads` already documents.
    opaque = (not program_known) or (python_marked
                                     and (snippet_opaque
                                          or (unresolved and not proven)))
    return deps, opaque


def _module_reads(mod, is_pkg, text, label, known, packages):
    """Everything one module provably consumes. -> (deps, consumes_all, err).

    READ, never executed: the file is ast-parsed and its import statements
    resolved against the repo's own module set; `ast.walk` sees
    function-local imports (this repo's dominant lazy style). Beyond
    statements, three dynamic shapes are read rather than ignored:

      STRING IMPORTS — a constant fed to `import_module`/`__import__`/the
      lazy-name family is an edge. A constant PREFIX (`"pkg." + x`) bounds
      the target to every known module under it. A bare parameter is chased
      to this file's own call sites and resolves when every one passes a
      constant; a dynamic import none of that can bound makes this module a
      CONSUMER OF EVERYTHING (`consumes_all`), because the one thing that is
      known about it is that nothing about its targets is. The mechanism is
      recognised THROUGH this file's own renames (`_import_rebinds`) and by
      its `name` keyword as well as its first positional
      (`_import_target`) — both were silent under-selects, measured.

      SPAWNED CHILDREN — see `_spawn_reads`: a provable python child adds
      its target's edges; an unprovable one makes this module a consumer of
      everything by the same logic.

      `-c` SNIPPETS — inline child code is parsed for its own imports, and
      one it cannot NAME makes the spawning module a consumer of everything
      by the same logic. A snippet may not come back readable-and-partial:
      see `_snippet_edges` for why a confident partial is the one answer
      with no surface left to state its own bound on.

    An UNPARSEABLE file refuses the whole graph — its imports are unknown,
    so any selection built beside it could silently omit a consumer, and a
    focused receipt that might have under-selected is worse than none."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return None, False, (
            "%s does not parse (%s) — its imports are unknown, so no "
            "focused selection built beside it can promise coverage; fix "
            "the file or run the whole suite" % (label, exc))
    pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]

    def local(dotted):
        """Every repo-local module importing `dotted` executes: the module
        itself when local, plus every package __init__ on its path."""
        parts = str(dotted).split(".")
        return {".".join(parts[:i]) for i in range(1, len(parts) + 1)
                if ".".join(parts[:i]) in known}

    aliases = _import_aliases(tree, pkg)
    rebinds = _import_rebinds(tree, aliases)

    defs, calls, call_names = {}, {}, set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.setdefault(node.func.id, []).append(node)
            call_names.add(id(node.func))
    escapes = {node.id for node in ast.walk(tree)
               if isinstance(node, ast.Name) and node.id in defs
               and id(node) not in call_names}

    def param_domain(owner, param, prefix):
        """The exact target set of a parameter-fed dynamic import, when this
        file's own call sites pin it to constants; None when they do not.
        The enclosing function must be defined once here under its name,
        never referenced except as a direct call, and every call must pass a
        constant string for the parameter — one variable call site unbounds
        the whole domain."""
        if owner is None or owner.name in escapes \
                or defs.get(owner.name, ()) != [owner]:
            return None
        names = [a.arg for a in owner.args.posonlyargs + owner.args.args]
        if param not in names or owner.args.vararg or owner.args.kwarg:
            return None
        pos = names.index(param)
        sites = calls.get(owner.name, ())
        if not sites:
            return None
        domain = set()
        for site in sites:
            got = site.args[pos] if pos < len(site.args) and not any(
                isinstance(a, ast.Starred) for a in site.args) else None
            for kw in site.keywords:
                if kw.arg == param:
                    got = kw.value
            if not (isinstance(got, ast.Constant)
                    and isinstance(got.value, str)):
                return None
            domain |= local(prefix + got.value)
            if pkg:
                domain |= local(".".join(
                    pkg + (prefix + got.value).split(".")))
        return domain

    deps, consumes_all = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps |= local(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                head = pkg[:len(pkg) - (node.level - 1)] \
                    if node.level > 1 else list(pkg)
                if node.level - 1 > len(pkg):
                    continue        # reaches above the repo: not local
            else:
                head = []
            target = head + (node.module.split(".") if node.module else [])
            if not target:
                continue
            deps |= local(".".join(target))
            for alias in node.names:
                # `from helm import gate` names a SUBMODULE in the alias
                # position; resolve it too, or every lazy import in this
                # repo's own style would be an invisible edge.
                deps |= local(".".join(target + [alias.name]))

    ctx = {"aliases": aliases, "local": local, "known": known, "mod": mod,
           "packages": packages, "params": ()}
    is_test = _is_test_module(mod)

    def read_calls(node, scopes, params, owner):
        nonlocal deps, consumes_all
        for child in ast.iter_child_nodes(node):
            child_scopes, child_params, child_owner = scopes, params, owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scopes = scopes + [_scope_assigns(child)]
                child_params = tuple(
                    a.arg for a in child.args.posonlyargs + child.args.args
                    + child.args.kwonlyargs)
                child_owner = child
            if isinstance(child, ast.Call):
                here = dict(ctx, params=params)
                if _importish_callee(child.func, rebinds):
                    # Constant-string edges first — the harvesting arm, which
                    # covers a hub that imports by constant name
                    # (`_lazy("verb", ...)`), bare or package-qualified.
                    for arg in list(child.args) + [k.value
                                                   for k in child.keywords]:
                        if isinstance(arg, ast.Constant) \
                                and isinstance(arg.value, str) \
                                and 0 < len(arg.value) < 128:
                            deps |= local(arg.value)
                            if pkg:
                                deps |= local(".".join(
                                    pkg + arg.value.split(".")))
                if _strict_importish(child.func, rebinds):
                    target = _import_target(child)
                    # A definite import whose target this reader cannot even
                    # LOCATE (`import_module(**kw)`) is the unbounded case by
                    # construction, so it takes the unbounded answer.
                    tag = _resolve_value(target, scopes, here) \
                        if target is not None else ("unknown",)
                    if tag[0] == "prefix":
                        for hit in known:
                            if hit.startswith(tag[1]):
                                deps |= local(hit)
                    elif tag[0] in ("param", "prefix-param"):
                        prefix = tag[1] if tag[0] == "prefix-param" else ""
                        param = tag[2] if tag[0] == "prefix-param" else tag[1]
                        domain = param_domain(owner, param, prefix)
                        if domain is None:
                            consumes_all = True
                        else:
                            deps |= domain
                    elif tag[0] != "str":
                        consumes_all = True
                if _spawn_callee(child.func, aliases):
                    argv = None
                    if child.args:
                        argv = child.args[0]
                    for kw in child.keywords:
                        if kw.arg == "args":
                            argv = kw.value
                    if argv is not None:
                        tags, complete = _argv_elements(argv, scopes, here)
                        got, opaque = _spawn_reads(tags, complete, here)
                        deps |= got
                        if opaque and (is_test or _spawn_python_marked(tags)):
                            consumes_all = True
            read_calls(child, child_scopes, child_params, child_owner)

    read_calls(tree, [_scope_assigns(tree)], (), None)
    deps.discard(mod)
    return deps, consumes_all, None


def _spawn_python_marked(tags):
    """Did the spawn visibly involve a python interpreter? Only a marked
    child widens a NON-test module to consumer-of-everything — see
    `_spawn_reads` for why an unmarked foreign binary stays outside the
    model. A test module widens on ANY opaque spawn: tests are the selection
    currency itself and over-selecting one is cheap."""
    for tag in tags:
        if tag[0] == "sysexe":
            return True
        if tag[0] == "str" and "python" in tag[1].rsplit("/", 1)[-1]:
            return True
    return False


def _spawn_callee(func, aliases):
    import ast
    if isinstance(func, ast.Attribute):
        base = _dotted_base(func.value, aliases)
        return base in _SPAWN_CALLEES and func.attr in _SPAWN_CALLEES[base]
    if isinstance(func, ast.Name):
        dotted = aliases.get(func.id, "")
        base, _sep, attr = dotted.rpartition(".")
        return base in _SPAWN_CALLEES and attr in _SPAWN_CALLEES[base]
    return False


def _module_shaped(path):
    """Could this repo path participate in the import namespace? A .py file
    that maps to a module, or an extensionless path of identifier segments
    (a candidate package directory)."""
    if path.endswith(".py"):
        return _module_name(path) is not None
    return all(p.isidentifier() for p in path.split("/"))


_SYMLINK_REFUSAL = (
    "%s is a symlink on a module-shaped name — an aliased import namespace "
    "means one file answers to two names, and a selection computed over "
    "either under-counts the other; run the whole suite")


def _worktree_module_files(repo):
    """{module: (is_pkg, text, label)} for the WORKTREE, through git's own
    view (tracked plus untracked-unignored, exactly the population
    `_changed_files` reads) so the mint-time universe and the bind-time
    re-derivation cannot disagree about what a file is. -> (files, err).
    Refuses on any module-shaped symlink, tracked (mode 120000) or not."""
    git = vcs.backend(repo)
    rc, out, err = git.text(repo, "ls-files", "-s", "-z")
    if rc != 0:
        return None, "cannot list the tracked tree: %s" % (
            err or "git exited %s" % rc)
    paths = []
    for entry in (out or "").split("\0"):
        if not entry.strip():
            continue
        meta, _tab, path = entry.partition("\t")
        mode = meta.split(" ", 1)[0]
        if mode == "120000" and _module_shaped(path):
            return None, _SYMLINK_REFUSAL % path
        paths.append(path)
    rc, out, err = git.text(repo, "ls-files", "--others",
                            "--exclude-standard", "-z")
    if rc != 0:
        return None, "cannot list untracked files: %s" % (
            err or "git exited %s" % rc)
    for path in (out or "").split("\0"):
        if path.strip():
            paths.append(path)
    files, seen_dirs = {}, set()
    for path in paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in seen_dirs:
                continue
            seen_dirs.add(prefix)
            if os.path.islink(os.path.join(repo, prefix)) \
                    and _module_shaped(prefix):
                return None, _SYMLINK_REFUSAL % prefix
        if os.path.islink(os.path.join(repo, path)) \
                and _module_shaped(path):
            return None, _SYMLINK_REFUSAL % path
        mod = _module_name(path)
        if not mod:
            continue
        full = os.path.join(repo, path)
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            continue            # deleted-but-tracked: absent, like any walk
        except (OSError, UnicodeDecodeError) as exc:
            return None, ("%s is unreadable (%s) — its imports are unknown; "
                          "fix the file or run the whole suite" % (full, exc))
        files[mod] = (path.endswith("__init__.py"), text, full)
    return files, None


def _tree_module_files(repo, tip):
    """{module: (is_pkg, text, label)} for a COMMITTED tree, read from the
    object store — never from anyone's worktree, which by bind time is some
    other commit entirely. -> (files, err). Same symlink refusal as the
    worktree reader, derived from the recorded mode."""
    git = vcs.backend(repo)
    rc, out, err = git.text(repo, "ls-tree", "-r", "-z", tip)
    if rc != 0:
        return None, "cannot read the tree of %s: %s" % (
            tip[:12], err or "git exited %s" % rc)
    wanted = []
    for entry in (out or "").split("\0"):
        if not entry.strip():
            continue
        meta, _tab, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) < 3:
            return None, "unreadable ls-tree entry %r in %s" % (
                entry[:80], tip[:12])
        mode, _kind, sha = fields[0], fields[1], fields[2]
        if mode == "120000" and _module_shaped(path):
            return None, _SYMLINK_REFUSAL % path
        mod = _module_name(path)
        if mod:
            wanted.append((mod, path, sha))
    files = {}
    for mod, path, sha in wanted:
        rc, text, err = git.text(repo, "cat-file", "blob", sha)
        if rc != 0:
            return None, "cannot read %s@%s: %s" % (
                path, tip[:12], err or "git exited %s" % rc)
        files[mod] = (path.endswith("__init__.py"), text,
                      "%s@%s" % (path, tip[:12]))
    return files, None


def _scope_selection(files, changed_modules):
    """The consumer closure over one file population. -> (selected,
    universe, err). `changed_modules` seeds the closure and joins the known
    set even when absent from the walk — the DELETED half of a changed set:
    an import of a deleted module must still resolve as an edge, so the
    tests of everything that still imports it are exactly the tests that
    will report the breakage. Modules the reader had to declare CONSUMERS OF
    EVERYTHING seed the closure on every plan, whatever changed — that is
    the fail-closed reading of an import nothing could bound."""
    known = set(files) | set(changed_modules)
    packages = {mod for mod, row in files.items() if row[0]}
    graph, all_consumers = {}, set()
    for mod in sorted(files):
        is_pkg, text, label = files[mod]
        deps, everything, err = _module_reads(mod, is_pkg, text, label,
                                              known, packages)
        if err:
            return None, None, err
        graph[mod] = deps
        if everything:
            all_consumers.add(mod)
    importers = {}
    for mod, deps in graph.items():
        for dep in deps:
            importers.setdefault(dep, set()).add(mod)
    reached = set(changed_modules) | all_consumers
    frontier = list(reached)
    while frontier:
        for imp in importers.get(frontier.pop(), ()):
            if imp not in reached:
                reached.add(imp)
                frontier.append(imp)
    selected = sorted(m for m in reached if _is_test_module(m))
    universe = sorted(m for m in known if _is_test_module(m))
    return selected, universe, None


def _single_merge_base(git, repo, trunk, tip, trunk_name):
    """The one merge-base, or (None, why). `--all`, because a criss-cross
    history has SEVERAL bases and plain merge-base silently picks one — a
    changed-set diffed from a chosen base is a chosen scope."""
    rc, out, err = git.text(repo, "merge-base", "--all", trunk, tip)
    if rc != 0 or not (out or "").strip():
        return None, "cannot find the merge-base with %s: %s" % (
            trunk_name, err or "git exited %s" % rc)
    bases = [b.strip().lower() for b in out.split() if b.strip()]
    if len(bases) != 1:
        return None, ("this history shares %d merge-bases with %s — an "
                      "ambiguous base makes the changed set a choice, and a "
                      "chosen scope is a caller-supplied scope; rebase to a "
                      "single base or run `helm gate run`"
                      % (len(bases), trunk_name))
    return bases[0], None


def focus_plan(repo):
    """The MEASURED scope of a focused run: -> (plan, err).

    plan = {"policy", "trunk", "base", "changed", "selected", "universe"} —
    changed is repo-relative file paths (committed, uncommitted, untracked,
    including deletions), selected is the dotted test modules whose
    transitive consumption reaches any changed module (plus every module the
    reader declared a consumer of everything), universe is how many test
    modules the repo holds so a reader can see the ratio a
    `focused <selected>/<universe>` evidence line claims.

    EVERY FIELD IS DERIVED HERE, from the tree and the repo's own source;
    the caller contributes nothing but the repo. FAIL-CLOSED on every edge
    this reader cannot see past, each with its own refusal: a changed file
    that is not a Python module; a change nothing consumes; a closure that
    IS the whole suite; a module-shaped symlink (an aliased namespace); a
    history with more than one merge-base; an unparseable file. A dynamic
    import or python child the reader cannot bound never under-selects
    silently — it makes its module a consumer of everything (see
    `_module_reads`), which either rides along or grows the closure into
    the whole-suite refusal. THAT SENTENCE IS ONLY AS TRUE AS THE CALLEE
    READER: it was false for a RENAMED mechanism until `_import_rebinds`
    existed (`from importlib import import_module as load` read as an
    ordinary call, so `consumes_all` stayed False and this plan came back
    confident and too small), which is why the rename map resolves a callee
    the way `_spawn_callee` always has."""
    git = vcs.backend(repo)
    trunk_name = git.trunk_ref(repo)
    trunk = git.head_sha(repo, ref=trunk_name)
    if not trunk:
        return None, "cannot resolve trunk (%s) to anchor a focused scope" \
            % trunk_name
    trunk = trunk.strip().lower()
    mb, err = _single_merge_base(git, repo, trunk, "HEAD", trunk_name)
    if err:
        return None, err
    changed = _changed_files(repo, git, mb)
    if changed is None:
        return None, ("cannot read the changed-file set against merge-base "
                      "%s — an unmeasurable scope must not narrow the gate"
                      % mb[:12])
    if not changed:
        return None, ("nothing changed against merge-base %s — there is no "
                      "scope to focus on; run `helm gate run`" % mb[:12])
    unmapped = sorted(f for f in changed if _module_name(f) is None)
    if unmapped:
        return None, ("focus can compute consumers only for Python modules — "
                      "%s %s outside the import graph; run the whole suite"
                      % (", ".join(unmapped[:4])
                         + (" (+%d more)" % (len(unmapped) - 4)
                            if len(unmapped) > 4 else ""),
                         "is" if len(unmapped) == 1 else "are"))
    changed_modules = {_module_name(f) for f in changed}
    files, err = _worktree_module_files(repo)
    if err:
        return None, err
    selected, universe, err = _scope_selection(files, changed_modules)
    if err:
        return None, err
    if not selected:
        return None, ("no test module consumes %s — a focused run would "
                      "prove itself by running nothing; write the test or "
                      "run the whole suite"
                      % ", ".join(sorted(changed_modules)[:4]))
    if set(selected) >= set(universe):
        return None, ("the consumer closure covers every test module "
                      "(%d/%d) — that is the whole suite by another name, "
                      "and it must take the queue: run `helm gate run`"
                      % (len(selected), len(universe)))
    plan = {"policy": FOCUS_POLICY, "trunk": trunk, "base": mb,
            "changed": sorted(changed), "selected": selected,
            "universe": len(universe)}
    if len(json.dumps(plan)) > _FOCUS_PLAN_BYTES:
        return None, ("the focused scope is too large to record verifiably "
                      "(%d files, %d test modules) — a scope that big is the "
                      "suite's job: run `helm gate run`"
                      % (len(changed), len(selected)))
    return plan, None


def _focus_of(row):
    """The focus block, or {} for ANYTHING that is not a mapping — the same
    total reader as `_ident_of`, for the same ledger-line reason."""
    focus = row.get("focus")
    return focus if isinstance(focus, dict) else {}


def _executed_modules(out):
    """WHICH TEST MODULES THE RUNNER ITSELF SAID IT RAN. -> (modules, ids).

    THE PLAN IS A REQUEST AND THIS IS THE ANSWER, and they are not the same
    fact. `focus_plan` derives which modules OUGHT to run; the argv asks the
    child for them; neither observes what the child DID. A module that
    contributes no test — an empty module, a module unittest resolved to
    something else, a collection that stopped early — leaves the plan and the
    argv completely unchanged, so a receipt whose scope came from either would
    claim coverage no test ever produced. This reader takes the child's own
    verbose protocol instead (`-m unittest -v`), which is why the focused
    command carries `-v` at all.

    ONE ID PER LINE, through the SAME header reader the failure parser uses
    (`_FAILURE_HEADER`), because these are the same strings: unittest prints
    `str(test)` to open a verbose line and to open a failure block. A test
    with a docstring puts the outcome on the NEXT line, so this reader must
    not require ` ... ` on the line it matches — and it therefore matches on
    shape, which is why the count comes back beside the set (see
    `_bind_focused` for the direction each half may be wrong in).

    THE CONTINUATION LINE IS THE ONE AMBIGUOUS SHAPE, and it is the defect
    task/1953 measured on a refused receipt: a docstring'd test prints TWO
    lines — the bare id alone, then `<docstring first line> ... ok` — and a
    docstring whose own first words are id-shaped (a narrative docstring
    quoting a test id: "test_b (tests.test_beta.T.test_b) matched ...")
    fullmatches the header grammar TOO. A shape-only reader then counts the
    continuation line as a second test: ids = ran + 1, and the module set
    gains a module the runner never reported — the exact inflation the bind
    rung exists to refuse, firing on an honest green run. So a bare-id line
    with NO outcome suffix arms a one-line sighting, and the line that
    follows — the docstring, however id-shaped — is consumed, never
    counted. The sighting is armed ONLY by a bare-id line, so a prose line
    in the output can never suppress the real header that follows it.

    THE MODULE IS THE SHORTEST PREFIX THAT IS A TEST MODULE, never the
    longest: `tests.test_alpha.T.test_a` ends in a segment starting with
    `test` too, so a longest-prefix reading would record the test id as a
    module and cover nothing. Shape-derived, not plan-derived, on purpose —
    grounding the resolution in the plan would let the plan re-enter through
    the back door of its own verification."""
    modules, ids = set(), 0
    continuation = False
    for line in _ANSI.sub("", str(out or "")).split("\n"):
        line = line.rstrip()
        if continuation:
            # The docstring line of the test sighted above: consumed whatever
            # it says, because its words are DATA about the sighted test,
            # never a test of their own.
            continuation = False
            continue
        hit = _FAILURE_HEADER.fullmatch(line)
        if not hit:
            continue
        parts = hit.group("test").split(".")
        for i in range(2, len(parts) + 1):
            candidate = ".".join(parts[:i])
            if _is_test_module(candidate):
                modules.add(candidate)
                ids += 1
                # A bare-id line — the whole line is the id, nothing after
                # the closing paren — is a docstring'd test's FIRST line;
                # its outcome rides the NEXT line, consumed above. A line
                # carrying its own outcome (`... ok`) completes in place.
                if hit.group(0) == hit.group("name") + " (" \
                        + hit.group("test") + ")":
                    continuation = True
                break
    return sorted(modules), ids


def _declared_refusal(project, sentence):
    return ("project %s %s — declare it as the authored `%s` field in %s: "
            '{"projects": {"%s": {"%s": {"command": ["pnpm", "-r", "test"], '
            '"protocol": "exit"}}}}'
            % (project, sentence, DECLARED_GATE_FIELD, home.authored_path(),
               project, DECLARED_GATE_FIELD))


def _declared_command(project, declared):
    """One project's declaration, validated -> (plan, err)."""
    if not isinstance(declared, dict):
        return None, _declared_refusal(
            project, "declares `%s` as %r, which is not an object"
            % (DECLARED_GATE_FIELD, declared))
    command = declared.get("command")
    if not isinstance(command, (list, tuple)) or not command \
            or any(type(arg) is not str or not arg for arg in command):
        return None, _declared_refusal(
            project, "declares `%s.command` as %r, which is not a non-empty "
            "list of non-empty strings. It is an ARGV LIST and never a shell "
            "string: helm spawns it directly from the repo root, so a project "
            "whose suite needs a pipeline declares its own script as argv[0]"
            % (DECLARED_GATE_FIELD, command))
    protocol = declared.get("protocol", PROTOCOL_EXIT)
    if protocol not in PROTOCOLS or type(protocol) is not str:
        return None, _declared_refusal(
            project, "declares gate protocol %r; helm reads %s — `exit` means "
            "the command's own exit status IS the verdict, `unittest` means "
            "helm reads python unittest's summary off stderr"
            % (protocol, " or ".join(PROTOCOLS)))
    return {"source": "registry", "project": project,
            "argv": [str(arg) for arg in command], "protocol": protocol}, None


def _repository_of(path, fresh=False):
    """The REPOSITORY a path belongs to, as its shared admin dir, or None.

    `vcs.common_dir` is `rev-parse --path-format=absolute --git-common-dir`
    through the seam: one value for a repository's main worktree and for every
    linked worktree of it, which is exactly the identity a declaration made at
    the root has to fold across. None means the read could not speak — never
    "these are different repositories" — so every caller treats it as unknown.

    `fresh` is what a MUTATING ACT asks: the seam's answer is memoised per
    process and a memo holds what was true when it was measured, so an act that
    validates a checkout asks the filesystem now and re-stamps the memo, never
    reads it (`vcs.GitVcs.common_dir`). A reader resolving a declaration's
    location may take the memo; nothing is spent on that read.
    """
    try:
        got = vcs.backend(path).common_dir(path, fresh=fresh)
    except Exception:                            # noqa: BLE001
        return None
    return os.path.realpath(got).rstrip(os.sep) if got else None


def _consuming_location(recorded, repo):
    """The ONE checkout an answer is about to be spent in -> (where, err).

    THE CONSUMING CHECKOUT IS A COORDINATE THAT TRAVELS WITH THE ROW, never a
    value a reader re-derives. `helm dispatch send` resolves the tree it was
    invoked in and stores it (`repo_root`, beside `repo_id`), so the row already
    knows which working tree its answer is for; every reader that resolved
    through the shared admin dir instead was asking a question the row had
    already answered. A repository root R and a linked worktree W that declare
    DIFFERENT commands are one repository, so the common dir's census finds two
    declarations and refuses as ambiguous — and an honest W receipt, minted by
    W's own declared command for a row dispatched in W, was refused with it.

    `recorded` is that coordinate (or a tree a caller holds outright, which is
    the same fact known one step earlier). Empty means the row carries none —
    a row written before the coordinate existed — and the answer is the
    repository, which is exactly where those readers stood: the ambiguity
    refusal is still the right answer when nothing says which tree.

    A COORDINATE THIS HELM CANNOT READ IS UNKNOWN AND ADMITS NOTHING. A
    recorded checkout that has since been removed cannot be asked what it
    declares, and falling back to the repository there would answer from a
    census the coordinate exists to narrow — either the ambiguity refusal (a
    refusal for the wrong reason) or, worse, R's declaration standing in for
    W's. And a readable coordinate that belongs to a DIFFERENT repository is
    refused rather than believed: `repo_root` is a field of the row, so reading
    it as authority without binding it to the repository this answer is being
    spent in would let a row nominate the declaration that judges it — the
    laundering channel `_declared_origin_refusal` closed for `repo_id`.

    AND THE CHECKOUT IS VALIDATED AGAINST THE FILESYSTEM AT THE INSTANT OF THE
    ACT, never against a memo. This door is asked by every act that spends a
    receipt — `bind` for a verdict, a composition capture, its replay, a scoped
    close — and the path-to-repository seam it reads is memoised per process:
    a checkout REMOVED after the memo warmed still answered its old repository
    from the entry, so the validation above passed for a tree that no longer
    existed and a scoped close admitted a receipt for it. The tree is resolved
    STRICTLY on disk first (gone or unresolvable is UNKNOWN, in the sentence
    above), and the repository identities on both sides are then read FRESH —
    the memo is re-read and re-stamped by the act, not trusted for it.
    """
    where = recorded if type(recorded) is str and recorded else ""
    if not where:
        return repo, None
    try:
        standing = pathlib.Path(where).resolve(strict=True).is_dir()
    except (OSError, RuntimeError):
        standing = False
    identity = _repository_of(where, fresh=True) if standing else None
    if identity is None:
        return None, (
            "the checkout this row was dispatched from (%s) cannot be read as "
            "a repository, so which command it declares is UNKNOWN — and an "
            "unreadable checkout is not licence to ask the repository instead, "
            "whose census this coordinate exists to narrow. Restore that "
            "worktree, or re-dispatch from the tree the answer is for" % where)
    mine = _repository_of(repo, fresh=True) \
        or os.path.realpath(repo).rstrip(os.sep)
    if identity != mine:
        return None, (
            "the checkout this row records (%s) belongs to repository %s and "
            "the answer is being spent in %s — a recorded checkout narrows "
            "WHICH TREE of the consuming repository declares the command, and "
            "it never carries the answer to another repository"
            % (where, identity, mine))
    return where, None


def _root_declaration(repo):
    """The declaration THIS REPOSITORY makes for `repo` -> (record, err).

    (None, None) when no authored declaration belongs to this repository, which
    is not a refusal — the caller goes on to the helm default and to its own
    sentences. A record is {"project", "path", "value"}.

    WHY A REPOSITORY AND NOT A DIRECTORY. An adopter registers and declares at
    its root R and then works in `git worktree`s of R, which is where a lane's
    gate actually runs; exact-directory lookup refused every one of them. The
    fold is by shared admin dir, so it cannot reach a DIFFERENT repository
    however similar its path looks.

    TWO DECLARATIONS IN ONE REPOSITORY REFUSE. Picking the first by sort order
    would run a command the owner declared for another tree, and a gate receipt
    is the claim that authorises a land. An entry for this EXACT directory still
    wins outright — that is the same-path precedence the caller relies on.

    AN UNREADABLE CANDIDATE IS UNKNOWN, NEVER "A DIFFERENT REPOSITORY".
    `_repository_of` answers None when git could not speak, and dropping such a
    candidate from the fold turned an AMBIGUOUS authority into a unique one: with
    two declarations in this repository, one whose location reads and one whose
    location does not, the fold saw a single hit and ran that command for a tree
    that carries two. An unknown counts in the census, so a lone readable hit
    beside one is no longer unique and REFUSES — the same answer two readable
    hits get, for the same reason.

    AND ZERO READABLE HITS BESIDE AN UNKNOWN IS UNKNOWN TOO, which the first cut
    of that census got wrong: it returned the ABSENT answer, and absent sends the
    caller on to helm's own default. In a shared checkout whose one custom
    declaration sits at a location git cannot speak for, that default SPAWNED
    helm's own unittest discovery in place of the command the project declared —
    an override feature answering with the very thing it overrides, on a census it
    knows is incomplete. "Refusing buys no safety" was the argument for absent,
    and it was wrong on its own terms: the alternative to refusing is not "no
    command", it is "a DIFFERENT command". An unreadable candidate may BE this
    repository's own declaration, so nothing here can count it out; the answer is
    UNKNOWN and the caller refuses toward no spawn. A candidate whose identity
    READS and belongs to another repository is not unknown at all, so an
    unrelated adopter's declaration still costs helm's own gate nothing — that is
    the absent answer, kept exactly where it was.
    """
    from . import registry
    try:
        declarations = registry.authored_declarations(DECLARED_GATE_FIELD)
    except Exception as exc:                     # noqa: BLE001
        return None, ("the authored project registry could not be read (%s), so "
                      "helm cannot tell which command this repository declares "
                      "— an UNREADABLE authority is not an absent declaration"
                      % exc)
    if not declarations:
        return None, None
    exact = [rec for rec in declarations
             if registry.same_location(rec["path"], repo)]
    if len(exact) == 1:
        return exact[0], None
    mine = _repository_of(repo)
    # `exact` here is two or more entries authored against ONE directory, which
    # is ambiguous without asking git anything. Otherwise fold by repository.
    # Every record names a location (`registry.authored_declarations` resolves a
    # pathless declaration to the project's registered one and emits no record it
    # cannot locate), so the three answers here are MINE, another repository, and
    # unknown.
    hits, unknown = list(exact), []
    if not exact and mine:
        for rec in declarations:
            identity = _repository_of(rec["path"])
            if identity == mine:
                hits.append(rec)
            elif identity is None:
                unknown.append(rec)
    if len(hits) == 1 and not unknown:
        return hits[0], None
    if not hits and not unknown:
        return None, None
    if not hits:
        # ZERO READABLE HITS BESIDE AN UNKNOWN IS UNKNOWN, NOT ABSENT — the
        # other half of the census question one branch above. `hits` empty reads
        # as "this repository declares nothing", which sends the caller to helm's own
        # default: in a shared checkout whose ONE custom declaration sits at a
        # location git could not speak for, the answer was helm's unittest
        # discovery rather than the project's declared command. That is a
        # DIFFERENT command spawned on an unreadable census, which is the one
        # thing an override feature must not do. An unreadable candidate may BE
        # this repository's declaration, so it can no longer be counted out; the
        # answer is UNKNOWN and the caller refuses toward no spawn.
        #
        # THE ABSENT ANSWER IS STILL THERE, and it is the established one: a
        # candidate whose identity READS and is a different repository leaves
        # `unknown` empty above, so an unrelated adopter's declaration goes on
        # costing helm's own gate nothing.
        return None, ("repository %s carries no readable authored gate "
                      "declaration, and %d declaration%s name%s a location this "
                      "helm cannot read (%s) — an unreadable location is not a "
                      "different repository, so whether %s declares a command "
                      "is UNKNOWN and helm will not fall back to its own. Make "
                      "that location readable, or withdraw the entry"
                      % (mine or repo, len(unknown),
                         "s" if len(unknown) != 1 else "",
                         "s" if len(unknown) == 1 else "",
                         ", ".join("%s at %s" % (rec["project"], rec["path"])
                                   for rec in unknown), repo))
    return None, ("repository %s carries %d authored gate declarations (%s), so "
                  "helm cannot tell which command %s should run.%s Declare the "
                  "command once, on the repository root, or give this worktree "
                  "its own authored `%s` entry"
                  % (mine or repo, len(hits) + len(unknown),
                     ", ".join("%s at %s" % (rec["project"], rec["path"])
                               for rec in hits + unknown), repo,
                     (" %d of them name%s a location this helm cannot read, so "
                      "the census is INCOMPLETE — an unreadable location is not "
                      "a different repository."
                      % (len(unknown), "s" if len(unknown) == 1 else ""))
                     if unknown else "",
                     DECLARED_GATE_FIELD))


def _effective_declaration(repo):
    """THE ONE DECLARATION `repo`'s GATE MAY RUN -> (decision, err).

    THE EFFECTIVE DECISION, EXTRACTED SO IT CANNOT BE APPROXIMATED. Two doors
    need it: `suite_command`, which SPAWNS the answer, and
    `_declared_origin_refusal`, which decides whether a receipt carrying a
    declared command may be SPENT here. The admission door asked a weaker
    question — is there ANY authored declaration in this repository whose argv
    and protocol match the row — and "any" is not "the one that would run".
    A repository root R declaring `["true"]` and a linked worktree W declaring
    its own real command are one repository, so R's honest green `true` receipt
    matched R's declaration, folded into W, and was admitted in W as a
    whole-suite claim — no forged receipt, no restamped hash, nothing the row
    had to lie about. The same weakness answered ADMITTED at a location whose
    census is ambiguous or UNKNOWN, which are precisely the two states
    `suite_command` refuses to spawn from. One resolver, one answer, both doors.

    `decision` is None only beside an `err`. Otherwise it is
    {"state", "project", "origin", "path", "plan"}: `plan` is the validated
    {"source", "project", "argv", "protocol"} when this location has an
    effective declaration and None when it has none, `path` is the location the
    declaration was authored for, `state`/`project` are `project_state`'s answer
    and `origin` is `declaration_provenance`'s for a registered exact hit.

    THE ORDER IS THE DECISION. An UNREADABLE registry answers first and refuses:
    `project_state` returns "unknown" for a strict-load failure, and every
    selection below it reads a layer that failure already told us not to trust —
    a stale command chosen under it was forwarded to a spawn. Then the REGISTERED
    EXACT authored declaration with its provenance validated, so a worktree that
    declares for itself keeps its own command; a malformed exact declaration
    REFUSES there rather than falling through to the repository, because falling
    through would run a command the owner did not declare for this tree. Then the
    repository fold (`_root_declaration`), which carries exact-path precedence,
    the ambiguity refusal and the unreadable-census UNKNOWN. Nothing declared at
    all is (decision, None) with a null plan — an ABSENCE and not a refusal,
    which is what leaves `suite_command`'s own default and sentences intact.
    """
    from . import foldcompose, registry
    state, project = foldcompose.project_state(repo)
    if state == "unknown":
        # ABOVE EVERY SELECTION, and that placement is the whole of it. This
        # refusal stood BELOW the exact-path and repository selections, so a
        # strict-load failure still returned whichever command the authored layer
        # happened to hold and `run` forwarded it to a spawn. A failed read
        # cannot license a command; it can only say the answer is UNKNOWN.
        return None, ("the project registry could not be read, so helm cannot "
                      "tell whether %s is a registered project or which "
                      "command it declares — an UNREADABLE registry is not an "
                      "unregistered repo" % repo)
    out = {"state": state, "project": project, "origin": None,
           "path": None, "plan": None}
    if project:
        try:
            declared, origin = registry.declaration_provenance(
                project, repo, DECLARED_GATE_FIELD)
        except Exception as exc:                # noqa: BLE001
            # A BROKEN REGISTRY IS NOT AN ABSENT DECLARATION. Falling through
            # to the helm default here would run helm's suite in someone
            # else's repo on the strength of a failed read.
            return None, ("the project registry could not be read (%s), so "
                          "helm cannot tell which command project %s declares"
                          % (exc, project))
        out["origin"] = origin
        if origin == "authored":
            plan, err = _declared_command(project, declared)
            if err:
                return None, err
            out.update(plan=plan, path=repo)
            return out, None
    # SAME-PATH AUTHORED PRECEDENCE IS ABOVE THIS LINE, so a worktree that has
    # its own authored declaration keeps it and never reaches the repository.
    root, root_err = _root_declaration(repo)
    if root_err:
        return None, root_err
    if root:
        plan, err = _declared_command(root["project"], root["value"])
        if err:
            return None, err
        out.update(plan=plan, path=root["path"])
    return out, None


def suite_command(repo):
    """THE WHOLE-SUITE COMMAND FOR THIS REPO -> (plan, err). The one door.

    plan is {"source", "project", "argv", "protocol"}. `source` is "registry"
    when the project declared the command and "helm-default" when the repo IS a
    helm checkout — the ONE tree whose command helm may assume, because it is
    the tree that ships `SUITE`. Everything else REFUSES by naming the field to
    set, which is the whole cure: the old constant-read could not refuse, so an
    adopter's project got an UNKNOWN receipt about python unittest discovery
    instead of a sentence telling it what to declare.

    A DECLARATION OUTRANKS THE DEFAULT, including in helm's own tree: the
    default exists so helm needs no registry edit, not so helm cannot be told.

    AUTHORED PROVENANCE AND NOTHING ELSE. The command read here is SPAWNED, so
    where the block came from is part of whether it may be run at all. The
    merged registry view cannot answer that — it carries the projection under
    the authored layer and once MIGRATED an inline block into it, so a `gate`
    written into registry.json (rebuildable by any re-scan, written by every
    discovery pass) read back exactly like one the owner wrote, and a
    projection saying `["true"]` could stand in for a failing default. So this
    asks `registry.declaration_provenance`, which answers authored / projection
    / absent as three different facts, and an inline block is REPORTED as a
    projection rather than executed as a declaration.

    A LINKED WORKTREE RESOLVES THROUGH ITS REPOSITORY. `project_state` compares
    exact directories, so an adopter that registered and declared at its root R
    got a refusal in every ordinary `git worktree` of R — and an adopter's lane
    worktree is exactly where a gate runs. The repository is identified by
    `git rev-parse --git-common-dir`, which is one value for R and all its
    linked worktrees, and the run still happens with cwd = the worktree handed
    in. Two declarations inside one repository are AMBIGUOUS and refuse: helm
    would otherwise pick one by sort order and run a command for a tree the
    owner declared for a different one.

    AN UNKNOWN CENSUS REFUSES ABOVE THE DEFAULT, and the order inside
    `_effective_declaration` is the whole of it. `_root_declaration` answers
    UNKNOWN when a candidate declaration names a location this helm cannot read —
    including when it is the ONLY candidate — and that refusal is returned before
    the is-this-helm predicate is ever asked. Otherwise the one tree that has a
    default would quietly run helm's unittest discovery instead of the command
    its own project declared, which is an override feature answering with the
    thing it overrides. The default is for a repository that declares NOTHING,
    never for one whose declaration could not be read. AN UNREADABLE REGISTRY
    REFUSES ABOVE EVERY SELECTION for the same reason and one step earlier.

    THE SELECTION ITSELF LIVES IN `_effective_declaration`, which is the door the
    receipt-admission check asks too: what may be SPENT here has to be what would
    RUN here, and a second resolver beside this one is how those two answers
    start differing. What is left below is this verb's own vocabulary — the
    projection report, helm's own default, and the two sentences that name the
    field to set.

    THE PREDICATE IS THE NARROW ONE, AND WHICH ONE IS NOT A DETAIL HERE.
    `selfrepo.is_helm_source_tree` answers about the PACKAGE ROOT alone; it is
    the reading trunk designates for "the caller whose worse failure is a false
    True", which is exactly this caller — a false True hands
    `python3 -m unittest discover -s tests -t .` to a tree that does not ship
    helm's suite and SPAWNS it there. `cli._is_helm_checkout` is the OTHER
    reading and must not be asked: task/2442 made it deliberately WIDE and
    FAIL-OPEN (it answers True when the tree cannot be reached at all), because
    its only consumer is an advisory warning whose worse failure is a false
    False. Asking it here turned a departed or empty directory into a spawn —
    the live defect this verb exists to prevent. So `UnreachableCheckout` (an
    OSError subclass, raised for an identity naming no reachable checkout) and
    every other OSError are read as NOT helm and fall through to the refusal
    below, which names the field to set.
    """
    from . import selfrepo
    decision, err = _effective_declaration(repo)
    if err:
        return None, err
    if decision["plan"]:
        return decision["plan"], None
    project = decision["project"]
    if decision["origin"] == "projection":
        return None, _declared_refusal(
            project, "has a `%s` block in the PROJECTION registry (%s) and not "
            "in the authored file, so helm will not run it: that file is "
            "rebuilt from a scan and is not where a project declares anything. "
            "A command helm spawns is read only from authored provenance, so "
            "move the same block"
            % (DECLARED_GATE_FIELD, home.registry_path()))
    # THE TREE'S OWN NARROW PREDICATE, asked rather than restated: a second copy
    # is how the answer starts differing by caller. See the docstring for why it
    # is this one and not `cli._is_helm_checkout`.
    try:
        ships_helm = selfrepo.is_helm_source_tree(repo)
    except OSError:                 # UnreachableCheckout included, by subclass
        ships_helm = False          # cannot look -> not a tree helm may assume
    if ships_helm:
        return {"source": "helm-default", "project": project,
                "argv": [interpreter()["executable"]] + list(SUITE),
                "protocol": PROTOCOL_UNITTEST}, None
    # THE UNREADABLE-REGISTRY REFUSAL IS NOT RESTATED HERE. It stood at this
    # point, below both selections and below the default, and that ordering is
    # the defect `_effective_declaration` fixes — it now answers before anything
    # is selected, so `decision["state"]` can no longer be "unknown" and a copy
    # of the sentence here would be a branch nothing can reach.
    if not project:
        return None, ("%s is not a registered helm project and does not ship "
                      "helm's own suite, so nothing declares the command this "
                      "gate would run. Register it (`helm sync`), then declare "
                      "its command as the authored `%s` field in %s"
                      % (repo, DECLARED_GATE_FIELD, home.authored_path()))
    return None, _declared_refusal(
        project, "declares no gate command, and does not ship helm's own "
        "suite either, so helm has no command to run on this tree")


def _exit_protocol_result(rc, argv, sidecar=None):
    """The result of a command whose EXIT STATUS is its verdict.

    `sidecar` is `_keep_command_output`'s (path, err): the detail names the
    file holding the command's whole stdout+stderr, or says why there is none,
    so a reader of a red receipt is told where the evidence is in the same
    sentence that gives the verdict.

    `parse_result` reads python unittest's own text protocol and nothing else,
    so a project whose suite is pnpm/cargo/go has no footer to read: before
    this existed such a run minted UNKNOWN with `failures_unreadable` over a
    perfectly green suite, which is a failed parse reported as a failed run.
    A project that declares `protocol: exit` has said its exit status is the
    answer, so that is the ONE source read here.

    THE CHILD'S PROSE IS NOT READ, and that is the point rather than a
    shortcut: a command that prints `Ran 9 tests in 0.1s / OK` must not be
    able to claim a count helm never counted. `ran`, `skipped` and `elapsed`
    are null and the detail SAYS which protocol produced the status, so a null
    count reads as "this runner reports no count" and never as a broken parse.
    """
    name = str((argv or [""])[0])
    path, keep_err = sidecar or (None, None)
    where = "; its stdout+stderr are in %s" % path if path else \
        "; its stdout+stderr were not kept (%s)" % keep_err if keep_err else ""
    if rc is None:
        return {"status": "UNKNOWN", "ran": None, "skipped": None,
                "elapsed": None, "failures": [], "failures_unreadable": True,
                "detail": "%s never exited; the declared command's exit status "
                          "is its verdict and there is none%s" % (name, where)}
    return {"status": "OK" if rc == 0 else "FAILED", "ran": None,
            "skipped": None, "elapsed": None, "failures": [],
            "failures_unreadable": True,
            "detail": "%s exited %d; the declared `%s` protocol reads that "
                      "status as the verdict and counts no tests%s"
                      % (name, rc, PROTOCOL_EXIT, where)}


def run(repo=None, argv=None, label=None, timeout=None, focus=False):
    """Run the gate and MINT its receipt. -> (row, err).

    Whole-suite runs first take one process-owned repository FIFO position.
    `focus=True` composes its own unittest command from `focus_plan` —
    helm-chosen interpreter, helm-measured scope — and runs OUTSIDE the
    expensive-suite queue, which is the point: a cure round must not buy a
    40-minute FIFO slot to prove a 30-second claim. The full-universe refusal
    in `focus_plan` keeps this from becoming the queue's own bypass. Custom
    diagnostic commands remain outside the queue and, as before, cannot bind
    a verdict. The receipt is written before this returns, so a claim can
    never exist in a reviewer's terminal without existing on disk."""
    repo = os.path.realpath(repo or os.getcwd())
    cross_err = _cross_tree_refusal(repo)
    if cross_err:
        return None, cross_err
    _warn_trunk_deletions(repo)
    if focus and argv:
        return None, ("gate run --focus composes its own command from the "
                      "measured scope; a `--` argv beside it would be a "
                      "caller-supplied scope, which is the thing focus "
                      "exists to refuse")
    plan = None
    if focus:
        plan, plan_err = focus_plan(repo)
        if plan_err:
            return None, plan_err
    suite = not argv and not focus
    command = None
    sidecar = None
    if suite:
        # RESOLVED BEFORE ANYTHING IS SPENT — ahead of the scratch preflight and
        # the repository FIFO, like `_cross_tree_refusal` above — so a repo that
        # declares no command costs the queue nothing and leaves no terminal row
        # to explain.
        command, command_err = suite_command(repo)
        if command_err:
            return None, command_err
    # The interpreter is recorded whenever HELM chose it — the suite and the
    # focused plan are both helm-composed commands. Only a caller's own `--`
    # argv leaves it None, and bind() keeps refusing that shape. A DECLARED
    # command is one helm chose too: it read the project's declaration instead
    # of its own constant, and `interpreter` stays the MINTING python because
    # that is what the field has always meant (see `interpreter`). The runtime
    # the project declared is argv[0] of the command itself, recorded in the
    # receipt's `suite_command` block.
    ident = interpreter() if suite or focus else None
    if suite:
        cmd = list(command["argv"])
    elif focus:
        # `-v` IS LOAD-BEARING, not a nicety: without it unittest prints dots
        # and the run's own answer about WHICH modules produced tests does not
        # exist anywhere. `_executed_modules` reads that protocol, so a
        # focused receipt records what ran instead of what was asked for.
        cmd = [ident["executable"], "-m", "unittest", "-v"] + plan["selected"]
    else:
        cmd = list(argv)
    custom_suite = not suite and not focus and (
        _suite_cap_shaped(cmd) or _diagnostic_shard_shaped(cmd))
    if suite or focus or custom_suite:
        # THE NODE BEFORE THE QUEUE. A pressured tmp refuses every run that
        # will mint scratch — whole-suite, focused, suite-shaped custom — and
        # it refuses BEFORE a FIFO position is bought, so the refusal costs
        # the queue nothing and leaves no terminal row to explain.
        pressure = _scratch_preflight()
        if pressure:
            return None, pressure
    position = None
    if suite:
        position, err = _acquire_gate(repo)
        if err:
            return None, err
    # Capture only static host topology outside the authority door. The
    # admission flock re-reads panes, occupants and PSI, then returns the one
    # effective grant that every suite-scale child environment will describe.
    topology = _capacity_topology() \
        if suite or focus or custom_suite else _CAPACITY_UNSET
    capacity = None
    if suite:
        # ADMISSION IS COUNTED OVER PROCESSES, NOT LOCK-HOLDERS, and it is
        # checked here — after the FIFO grants the head, before anything
        # spawns — so a refused run releases its position and starts nothing.
        capacity, admit_err = _admit_suite(position, topology=topology)
        if admit_err:
            _ok, finish_err = _finish_position(repo, position, "REFUSED")
            if finish_err:
                # type(): a capacity refusal stays one with the queue's
                # trouble appended — the suite still never ran.
                admit_err = type(admit_err)("%s; %s" % (admit_err, finish_err))
            return None, admit_err
    elif custom_suite:
        # A custom command that IS a whole-tree discovery or diagnostic shard
        # run occupies the same memory as the queued kind and must not be
        # helm's own capacity bypass. It remains a custom, nonbinding receipt.
        # No FIFO position, so no intent row: census-only. Its exact admitted
        # grant still reaches the child so a diagnostic shard cannot allocate
        # all visible CPUs behind a conservative host admission.
        capacity, admit_err = _admit_suite(topology=topology)
        if admit_err:
            return None, admit_err
    elif focus and len(plan["selected"]) * 2 > plan["universe"]:
        # A suite-SCALE focused run occupies the same memory as the queued
        # kind, and it is the ORDINARY outcome rather than a rare one: any
        # repo with a hub module the tests reach in common has most of its
        # suite closing over most changes, so focus routinely selects a
        # majority of the universe. It skips the FIFO — that is the point of
        # focus — but not the census: piling a near-suite on a saturated box can
        # compound host pressure, whatever the run is called. Suite concurrency
        # correlated with pane loss, but the exact cause remains unproven. Small
        # selections (a test-file cure round, a leaf repo) stay exempt.
        capacity, admit_err = _admit_suite(topology=topology)
        if admit_err:
            return None, admit_err
    if focus and capacity is None:
        capacity = suite_capacity(topology=topology)
    timing_dir = timing_token = None
    timing_setup_reason = None
    # THE CHILD'S TMPDIR IS A ROOT THIS PROCESS OWNS AND REMOVES. tests/
    # __init__.py routes every mint in the suite process under a per-process
    # root its atexit hook reaps — but atexit never runs in a child this
    # gate KILLS (timeout, cancellation, the cgroup sweep), and a killed
    # whole-suite run left its entire scratch tree on the node. So the
    # gate hands every helm-composed or suite-shaped child a TMPDIR of its
    # own and the `finally` below removes it whatever the child did. An
    # ordinary custom diagnostic inherits the environment unchanged, as it
    # always did.
    scratch_root = None
    if suite or focus or custom_suite:
        try:
            scratch_root = tempfile.mkdtemp(prefix="helm-gate-scratch-")
        except OSError as exc:
            # A FIFO slot already bought is released, as admission releases
            # it; a refusal that keeps the head would wedge the queue.
            reason = "gate scratch is unavailable: %s" % exc
            if position:
                _ok, finish_err = _finish_position(repo, position, "REFUSED")
                if finish_err:
                    reason = "%s; %s" % (reason, finish_err)
            return None, reason
    if suite:
        owner = gatetestrecord.process_identity()
        if owner is None:
            timing_setup_reason = "module timing root process identity is unavailable"
        else:
            try:
                timing_dir = tempfile.mkdtemp(prefix="helm-gate-module-timing-")
                timing_token = uuid.uuid4().hex
            except OSError as exc:
                timing_setup_reason = "module timing scratch is unavailable: %s" % exc
    # THE MARKER OPENS WITH THE BRACKET AND CLOSES WITH IT. A commit inside
    # this window moves HEAD under a run whose receipt already recorded the
    # OLD head, so the receipt describes no single tree and bind() refuses it.
    # Three seats hit that in quick succession; the bracket rule catches
    # it AFTER, and this is the cheap BEFORE.
    #
    # AFTER ADMISSION, NOT BEFORE, and the order is the whole of the merge:
    # both refusal paths above RETURN, and neither can close a marker it never
    # opened. Opening first would leak an in-flight marker for a run that never
    # started, and the next honest run in that repo would read it as a commit
    # racing its own gate.
    inflight_nonce = _inflight_open(repo)
    try:
        try:
            head, tree, dirty, err = tree_state(repo)
        except BaseException:
            _inflight_close(repo, inflight_nonce)
            if position:
                _finish_position(repo, position)
            raise
        if err:
            if position:
                _finish_position(repo, position)
            return None, err
        started = time.time()
        # THE TREE IS READ BEFORE **AND AFTER**, and both halves are recorded.
        # The gap was reproduced: a suite child that modifies a tracked file
        # leaves the worktree dirty AFTERWARD, while a receipt taken only before it
        # says clean — so the receipt did not prove execution against the immutable
        # tree it names. Same bracket this lane's sibling uses around a ProcIdent:
        # one read cannot witness a change that happens during the thing it is
        # describing.
        if suite:
            env = _suite_env(capacity)
            env["TMPDIR"] = scratch_root
            if timing_dir:
                env.update({
                    "HELM_GATE_RECORD_DIR": timing_dir,
                    "HELM_GATE_RECORD_TOKEN": timing_token,
                    "HELM_GATE_RECORD_ROLE": "serial",
                    "HELM_GATE_RECORD_ROOT_PID": str(owner["pid"]),
                    "HELM_GATE_RECORD_ROOT_START": str(owner["start"]),
                    "HELM_GATE_RECORD_TIMING": "1",
                })
            try:
                stdout, stderr, rc, run_err = _queued_process(
                    repo, cmd, position, timeout, env=env)
            except BaseException:
                _finish_position(repo, position)
                raise
            if run_err:
                _ok, finish_err = _finish_position(
                    repo, position, detail=run_err)
                return None, run_err if not finish_err else "%s; %s" % (
                    run_err, finish_err)
            # unittest's protocol is stderr. stdout belongs to the tests and can
            # contain arbitrary `ERROR:` / `FAILED` prose; admitting it lets test
            # logs forge or displace protocol candidates. Only unittest stderr is
            # authoritative failure evidence.
            #
            # A DECLARED `exit` COMMAND HAS NO PROTOCOL TO FORGE — nothing
            # parses its prose — so both streams are kept, stdout first: the
            # whole text goes to the sidecar beside the receipt and to this
            # process's stderr (the console and the node log), and the mint
            # keeps a bounded tail in the row. Before this, `out = stderr`
            # dropped the suite's stdout on the floor and a red declared
            # receipt was undiagnosable (task/2527).
            if command["protocol"] == PROTOCOL_EXIT:
                out = (stdout or "") + (stderr or "")
                sidecar = _keep_command_output(out)
                if out:
                    sys.stderr.write(out)
                    sys.stderr.flush()
            else:
                out = stderr
        else:
            # FOCUS AND CUSTOM RUN UNDER THE GUARD TOO, WITHOUT A QUEUE SLOT.
            # This branch used to be a bare subprocess.run, which is why
            # focused runs -- the entire cure-round loop -- executed test code
            # with no cgroup while whole-suite runs were contained. They take
            # the same guarded path now with position=None: containment
            # without buying a FIFO slot they never acquired.
            #
            # Every suite-scale run gets the exact grant returned by admission.
            # A non-suite custom diagnostic still inherits unchanged: it neither
            # consumed an admission slot nor has a runner budget to constrain.
            child_env = _suite_env(capacity) \
                if focus or custom_suite else os.environ.copy()
            if scratch_root:
                child_env["TMPDIR"] = scratch_root
            try:
                stdout, stderr, rc, run_err = _queued_process(
                    repo, cmd, None, timeout, identity=_guard_identity(),
                    env=child_env)
                if run_err:
                    return None, "gate command did not run: %s" % run_err
                # A FOCUSED run IS unittest, so it gets the suite's forgery
                # rule: the protocol is stderr, stdout belongs to the tests.
                # Custom commands keep both streams — their parse is best-
                # effort by construction.
                out = (stderr or "") if focus \
                    else (stdout or "") + (stderr or "")
            except Exception as exc:
                return None, "gate command did not run: %s: %s" % (
                    type(exc).__name__, exc)
        module_timing = _read_module_timing(timing_dir, timing_token) \
            if timing_dir else _unknown_timing(
                timing_setup_reason or "module timing was not captured")
        try:
            row, minted, mint_err = _mint_result(
                repo, head, tree, dirty, ident, cmd, suite, label, rc, started, out,
                position.get("_legacy") if position else None, focus=plan,
                module_timing=module_timing
                if suite and command["protocol"] == PROTOCOL_UNITTEST
                else _NO_TIMING, command=command, sidecar=sidecar)
        except BaseException:
            if position:
                _finish_position(repo, position)
            raise
        if position:
            ok, finish_err = _finish_position(
                repo, position, row["status"] if minted else "UNMINTED",
                row["id"] if minted else None)
            if not ok:
                state = "minted receipt %s" % row["id"] if minted else \
                    "unminted run"
                return None, "%s but gate FIFO release failed: %s" % (
                    state, finish_err)
        if mint_err:
            return None, "receipt not minted: %s" % mint_err
        if not minted:
            return None, "receipt ledger unwritable (%s) — the run is NOT minted" \
                % receipts_path()
        return row, None
    finally:
        # CLOSES ON EVERY EXIT — return, raise, or fallthrough. The
        # marker guards a WINDOW, so a path that leaves it behind
        # would refuse commits in this room forever; a finally is the
        # only shape a future `return` cannot defeat. The nonce keeps it
        # OURS: a gate that started after us owns the file now, and
        # deleting theirs would free a room whose suite is still running.
        _inflight_close(repo, inflight_nonce)
        if timing_dir:
            shutil.rmtree(timing_dir, ignore_errors=True)
        if scratch_root:
            # reap_owned, not rmtree: a suite that extracted a release left
            # 0o555 directories a plain ignore_errors rmtree keeps.
            scratch.reap_owned(scratch_root)



def _decode(blob):
    if blob is None:
        return ""
    return blob if isinstance(blob, str) else blob.decode("utf-8", "replace")


def receipts():
    """Every minted receipt whose id MATCHES ITS OWN CONTENT.

    -> (rows, unavailable, skipped). `skipped` counts rows this function could
    not judge — see the total-by-construction note below and `gate list`, which
    reports it.

    The id is a hash of every field a reader relies on, and until someone
    tried it, nothing ever recomputed it: `eventledger` strict mode validates
    that a
    line is a JSON object with a non-empty id, and `by_id` matched on the
    string. So one hand-written line — invented id, target head, dirty false,
    status OK, a made-up interpreter — bound as VERIFIED.

    Recomputing here rather than in `by_id` because this is the ONE place rows
    enter, and a filter that lives at the entrance cannot be walked around by
    the next reader somebody adds.

    WHAT THIS DOES AND DOES NOT BUY, stated plainly rather than implied. It
    makes every stored row SELF-CONSISTENT: a receipt edited after minting, a
    truncated or corrupted line, and the invented-id forgery above are all
    rejected. It does NOT make a receipt UNFORGEABLE, and no content hash can:
    `_receipt_id` is a pure function of the row, so anyone able to write this
    file can also compute a matching id. Every seat on this box runs as the
    same user, so there is no separation to lean on short of signing the
    receipt with the cell signer the way `landreq` signs land candidates.

    This verb is therefore an INTEGRITY instrument, not an adversarial one. It
    makes an honest claim reproducible and a careless one detectable. A seat
    that deliberately writes this ledger is a dishonest agent, which is a
    different problem with a different answer, and calling that closed here
    would be exactly the overclaim the whole lane exists to stop."""
    rows, unavailable = eventledger.checked_events(receipts_path(), strict=True)
    if unavailable:
        # THE ARITY BELONGS TO THE FUNCTION, NOT TO THE HAPPY PATH. Widening
        # the return and leaving this early exit at the old two made the
        # UNAVAILABLE branch raise ValueError in both callers — the ledger
        # said "I cannot be read" and helm crashed instead of saying so.
        # Third time in this lane the ERROR path is the broken one (the
        # AttributeError refusal, the raising rejection, now this): every one
        # was a guard whose success path was exercised by every test and whose
        # failure path was exercised by none.
        return [], unavailable, 0
    chunks, skipped = _failure_chunks(rows)
    _timing_rows, _timing_poisoned, timing_skipped = _timings(rows)
    skipped += timing_skipped
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("event") in (
                _FAILURE_CHUNK_EVENT, _TIMING_EVENT):
            continue
        # TOTAL BY CONSTRUCTION. Any row this loop cannot evaluate is a row the
        # ledger does not contain, never an exception in a caller's frame: one
        # malformed line must not be able to hide every honest receipt behind
        # it. The narrow `except` that only knew today's malformed shape is
        # exactly how the next shape gets through.
        if _id_matches(row, chunks):
            out.append(row)
        else:
            skipped += 1
    return out, None, skipped


def _id_matches(row, chunks=None):
    """Does this row's stored id recompute from its own content, and — when
    the caller can supply the sibling chunk events — is its failure record
    complete?

    ONE DEFINITION, TWO READERS, and that is the whole reason it is a function
    rather than four lines inline. `receipts()` uses it to DROP rows and
    `unreadable_versions()` uses it to EXPLAIN what got dropped. If those two
    ever disagreed, the explanation would describe rows the filter had kept —
    or stay silent about rows it threw away — and a wrong explanation of a
    silent drop is worse than no explanation at all, because it is believed.

    `chunks` IS THE ONLY ASYMMETRY AND IT IS ONE-DIRECTIONAL. A v8 row whose
    chunks are missing is dropped by `receipts()` and, with chunks omitted
    here, is NOT counted as an unreadable VERSION by `unreadable_versions()` —
    which is correct, because its version is perfectly readable and the reason
    it was dropped is its evidence, not this helm's age. The predicate never
    KEEPS a row the other reader would drop, which is the direction that would
    make the explanation lie."""
    try:
        rid = str(row.get("id") or "")
        if not (receipt_version_known(row) and bool(_ID.fullmatch(rid))
                and rid == _receipt_id(row)):
            return False
        if row.get("v") == SHARDED_AUTHORITY_VERSION \
                and gateauthority.receipt_refusal(row):
            return False
        if chunks is not None and _version_has(row, "failure_record"):
            return _failure_record_error(row, chunks) is None
        return True
    except Exception:                       # noqa: BLE001 — a row that cannot
        return False                        # be judged is a row that is dropped


def _known_versions():
    """The versions this helm can read, for saying so in a refusal."""
    from . import gateimport         # deferred: gateimport imports gate
    return gateimport.KNOWN_VERSIONS


def unreadable_versions():
    """{receipt version: count} for DROPPED rows this helm cannot read at all,
    or None when the ledger is unavailable.

    A ROW THIS READER CANNOT READ IS NOT A ROW THAT FAILED INTEGRITY. The
    ledger is GLOBAL and append-only, so a lane running a newer helm writes
    its receipts into the same file every older reader walks. Those rows fail
    the id recompute for an entirely innocent reason — this helm hashes a
    version it has never heard of — and until now they were counted into "did
    not match their own content", which reads as CORRUPTION.

    That wording cost a seat three probes hunting damage that did not exist
    (four v5 rows minted by an unlanded lane). The rows really are
    unusable here and really must be skipped; what was wrong was the reason
    given. This function exists only to tell the two cases apart at the
    surface — it grants nothing, verifies nothing, and deliberately does NOT
    widen `receipts()`, whose 3-arity has five production callers and fifteen
    test call sites and whose own docstring records what widening it cost."""
    from . import gateimport         # deferred: gateimport imports gate
    rows, unavailable = eventledger.checked_events(receipts_path(), strict=True)
    if unavailable:
        return None
    out = {}
    for row in rows:
        if isinstance(row, dict) and row.get("event") in (
                _FAILURE_CHUNK_EVENT, _TIMING_EVENT):
            continue
        # THE SAME PREDICATE THE FILTER USED, not a second opinion about it.
        if _id_matches(row):
            continue
        version = row.get("v") if isinstance(row, dict) else None
        # THE READER'S SET, NOT THE IMPORT DOOR'S. A focused v6 row is
        # perfectly readable here and merely refuses at the generic import
        # door; counting it as "minted by a newer helm" would send a reader
        # to update helm over a row this helm understands completely.
        if not receipt_version_known(version):
            out[version] = out.get(version, 0) + 1
    return out


def _stored_but_unverifiable(prefix):
    """A row whose STORED id matches `prefix` but which `receipts()` dropped.
    -> (row, computed, incomplete) where `computed` is None when no id could be
    derived and `incomplete` names a v8 chunk failure.

    TOTAL OVER EVERY WAY A ROW GETS DROPPED, because a partial answer here
    reproduces the very conflation this exists to end. `receipts()` skips a row
    if its id DISAGREES, if computing one RAISES (a non-string in argv, a
    malformed interpreter), or if a v8 failure chunk is absent or malformed.
    Reporting only one mode would leave another still answering "no minted gate
    receipt" — present, unjudgeable, and described as absent.

    Paid only when the honest answer would otherwise be "not found" AND the
    integrity filter actually dropped something, so the happy path reads the
    ledger exactly once as before."""
    rows, unavailable = eventledger.checked_events(receipts_path(), strict=True)
    if unavailable:
        return None, None, None
    chunks, _skipped = _failure_chunks(rows)
    for row in rows:
        if isinstance(row, dict) and row.get("event") in (
                _FAILURE_CHUNK_EVENT, _TIMING_EVENT):
            continue
        try:
            rid = str(row.get("id") or "")
        except Exception:                       # noqa: BLE001
            continue
        if not rid.startswith(prefix):
            continue
        try:
            computed = _receipt_id(row)
        except Exception:                       # noqa: BLE001 — uncomputable
            return row, None, None              # is still PRESENT
        if not receipt_version_known(row):
            return row, computed, None
        if _version_has(row, "failure_record"):
            incomplete = _failure_record_error(row, chunks)
            if incomplete:
                return row, computed, incomplete
        if computed != rid:
            return row, computed, None
    return None, None, None


def by_id(prefix):
    """Resolve one receipt by id or unique prefix. -> (row, err).

    Ambiguity REFUSES rather than picking the newest: a token that names two
    runs names neither.

    ABSENT AND UNVERIFIABLE ARE DIFFERENT ANSWERS AND USED TO SHARE A SENTENCE.
    `receipts()` is an integrity filter: it recomputes every row's content id
    and DROPS the ones that disagree, returning the count as its third value.
    This function discarded that count and reported "no minted gate receipt
    <id> — run `helm gate run`", which is false when the row is sitting in the
    ledger, and the advice is worse than the error: if the row is unverifiable
    because a NEWER helm minted it, re-running mints another one this helm also
    cannot read, forever. Measured on receipt 756b936006bf0e47 — 685
    rows read, 1 skipped, and a reviewer traced three functions to learn that
    their APPROVE could not bind because the id recomputed differently under a
    pre-v4 reader."""
    prefix = str(prefix or "").strip().lower()
    if not _ID.fullmatch(prefix):
        return None, "not a receipt id: %r" % prefix
    rows, unavailable, skipped = receipts()
    if unavailable:
        return None, "receipt ledger unavailable: %s" % unavailable
    hits = [r for r in rows if str(r["id"]).startswith(prefix)]
    if not hits:
        chunk_rows, chunk_unavailable = eventledger.checked_events(
            receipts_path(), strict=True)
        if chunk_unavailable is None:
            chunk_hits = [r for r in chunk_rows if isinstance(r, dict)
                          and r.get("event") == _FAILURE_CHUNK_EVENT
                          and str(r.get("id") or "").startswith(prefix)]
            if len({str(r.get("id")) for r in chunk_hits}) > 1:
                return None, "%s matches %d failure chunks; use more characters" % (
                    prefix, len({str(r.get("id")) for r in chunk_hits}))
            if chunk_hits:
                chunk = chunk_hits[-1]
                parents = [r.get("id") for r in chunk_rows
                           if isinstance(r, dict)
                           and _version_has(r, "failure_record")
                           and chunk.get("id") in (r.get("failure_chunks") or ())]
                guidance = (" Referencing receipt%s: %s; inspect with `helm gate "
                            "show %s`." % (
                                "" if len(parents) == 1 else "s",
                                ", ".join(parents), parents[0])) if parents else (
                            " No stored receipt references it; it is an inert "
                            "orphan chunk in %s." % receipts_path())
                return None, ("%s identifies a failure-identity CHUNK, not a gate "
                              "receipt.%s" % (prefix, guidance))
        stored, computed, incomplete = (
            _stored_but_unverifiable(prefix) if skipped else (None, None, None))
        if stored is not None and incomplete:
            return None, (
                "receipt %s IS in the ledger but its multi-event failure record "
                "is INCOMPLETE and UNJUDGEABLE: %s. It is not missing, and "
                "re-running would conceal the broken stored evidence; inspect "
                "%s" % (prefix, incomplete, receipts_path()))
        if stored is not None and computed is None:
            return None, (
                "receipt %s IS in the ledger and this helm cannot compute an "
                "id for it at all — the row is malformed, so it can never "
                "bind. It is not missing; it is unjudgeable. Read the row in "
                "%s rather than re-running the gate, which leaves this one "
                "sitting there" % (prefix, receipts_path()))
        if stored is not None:
            # NAME THE CAUSE, do not offer both and let the reader pick. The
            # two have OPPOSITE remedies — a tampered row must be investigated,
            # a future row must not be re-gated — so a message that hedges
            # sends half its readers the wrong way. The version set is imported
            # from the module that owns it rather than restated here; two
            # constants that must agree are the defect this file keeps finding.
            from . import gateimport
            version = stored.get("v")
            if version == WITHDRAWN_SHARD_VERSION:
                cause = ("Version 7 is a WITHDRAWN sharded script receipt: "
                         "fresh workers cannot preserve arbitrary serial "
                         "unittest process state, so it never binds and must "
                         "not be grandfathered. Run the literal serial gate")
            elif receipt_version_known(stored):
                cause = ("This helm KNOWS version %r, so the row was CHANGED "
                         "after it was minted — investigate the ledger, do not "
                         "paper over it by re-running" % (version,))
            else:
                cause = ("Its version field says %r, which this helm does not "
                         "know: it was minted by a NEWER helm. Re-running the "
                         "gate is the WRONG move — it mints another receipt "
                         "this helm cannot read either. Update helm, then "
                         "re-resolve" % (version,))
            # TWO DIFFERENT DROPS, AND ONLY ONE OF THEM IS TAMPERING. Until
            # the reader learned to refuse an unknown version STRUCTURALLY, a
            # row could only fall out of `receipts()` by failing its id
            # recompute, so one sentence covered every case. It no longer
            # does: a withdrawn or future row whose id recomputes perfectly is
            # now dropped for its VERSION, and saying "its content id does NOT
            # recompute" about it printed the same 16 characters twice — a
            # sentence that accuses the ledger of tampering and then shows its
            # own evidence disagreeing. This file's whole point is that a
            # wrong explanation of a silent drop is worse than none, because
            # it is believed.
            if computed is not None and str(stored.get("id"))[:16] == computed:
                return None, (
                    "receipt %s IS in the ledger and its content id recomputes "
                    "correctly — nothing was edited. It cannot bind because "
                    "this reader does not admit its VERSION. %s"
                    % (prefix, cause))
            return None, (
                "receipt %s IS in the ledger and its content id does NOT "
                "recompute, so nothing may bind to it: the row says %s and "
                "this helm computes %s. %s"
                % (prefix, str(stored.get("id"))[:16], computed, cause))
        return None, "no minted gate receipt %s — run `helm gate run`" % prefix
    if len({r["id"] for r in hits}) > 1:
        return None, "%s matches %d receipts; use more characters" \
            % (prefix, len({r["id"] for r in hits}))
    return hits[-1], None


def token(evidence):
    """The gate token inside an evidence line, or None."""
    hit = _TOKEN.search(str(evidence or ""))
    return hit.group(1) if hit else None


def _binding_ts(value):
    """One canonical UTC timestamp as an orderable tuple, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    if not value.startswith("20") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", parsed) != value:
        return None
    return parsed


NEED_SUITE = "suite"
NEED_FOCUSED = "focused"


def _repository_authority_refusal(row, repo):
    """The canonical repository-authority door, or its refusal.

    This door SPENDS authority; it does not validate receipt content. Callers
    first finish deterministic receipt/content/focus/scope diagnostics, then
    cross this seam so every success exit is dominated by its authority guard.
    Descendant binding crosses before timestamp/postdate/ancestry classification:
    standing repository state cannot shadow a missing capability. Neither
    UNKNOWN nor False can authorize any success path.
    """
    repo = str(repo or "")
    if not os.path.isabs(repo) or os.path.realpath(repo) != repo \
            or not os.path.isdir(repo):
        return "standing repository is unreadable — receipt authority is UNKNOWN"
    try:
        from . import gateimport
        authorized, err = gateimport.repository_authorization(row, repo)
    except Exception as exc:                       # noqa: BLE001
        authorized, err = None, type(exc).__name__
    if authorized is not True:
        return ("receipt %s has no canonical authority for this repository: %s"
                % (row["id"], err or "authority UNKNOWN"))
    return None


def _bind_focused(row, tip, head, repo_id):
    """The focused arm of `bind`, after the shared checks (clean bracket, not
    dirty, status OK, interpreter and host named). -> (state, rid, why).

    VERSION FIRST, because the hash is the whole defence: only v6 binds the
    `suite` flag and the focus block into the content id, so a v4-shaped row
    wearing a focus block is a row whose scope NOTHING protects — an edited
    changed-list would still resolve. Refused on version, never trusted on
    presence.

    EXACT TIP ONLY. A whole-suite receipt earns a descendant arm because its
    claim ("everything passed") survives commits it contains; a focused
    claim is anchored to the tree its scope was measured against, and every
    commit after it is a commit the selection never saw.

    EVERY DERIVABLE FIELD IS RE-DERIVED HERE, none is read back on its own
    word: a self-computed content id proves nobody EDITED the row after it
    was written — it says nothing about whether anything was ever RUN, and
    every plausible-looking field of an assembled row is a field its
    assembler chose. So the question at each field is "could the submitter
    have written this?", and the answer this reader accepts is only the
    standing repository's own: the BASE must equal the merge-base the
    standing repository derives against its trunk right now (a recorded
    base is otherwise the submitter's choice — record base == tip and an
    empty diff \"covers\" everything); the CHANGED set must equal the
    re-derived base..tip diff in both directions; the SELECTED modules and
    the UNIVERSE must equal a full re-derivation of the consumer closure
    over the tip's own committed tree, through the same reader the mint
    used. Coverage stays EXACT set membership — no globs, no prefixes — so
    a garbage scope covers nothing rather than everything.

    AND THE LAST FIELD IS THE ONLY ONE THE REPOSITORY CANNOT SUPPLY: which
    modules actually PRODUCED tests. Re-derivation answers what OUGHT to have
    run; the child's own verbose protocol is the sole record of what did
    (`_executed_modules`), so the closure is checked against `executed` and
    not against `selected`, which is only the request that was made. The two
    halves of that record are trusted in opposite directions, deliberately:
    an EXTRA module in the set cannot manufacture coverage (the closure is
    the thing that must be present, and a stranger is not one of its
    members), while a MISSING one is exactly the failure this check exists
    for — but an id COUNT above the run's own `Ran N` means the reader
    matched lines that were not test ids at all, and a set derived by that
    reader may then contain a module nothing ran. So a short count is
    tolerated and a long one refuses.

    WHAT REMAINS UNPROVEN, on the record: that a locally hand-appended run
    HAPPENED. Re-derivation pins every claim about the tree; origin is held by
    the honest path instead. A local mint writes its own store. A routed mint
    enters only through gateroute's challenge-framed live-session custody;
    generic v6 artifacts still refuse at gateimport.KNOWN_VERSIONS.

    THAT RESIDUE IS DELIBERATELY LEFT OPEN, and here is the whole argument.
    A row assembled locally from `focus_plan`'s own answer, with `ran`/`rc`/
    `status` chosen by its assembler and no child ever spawned, DOES bind
    here. It cannot be closed by measurement: mint and bind run under one
    uid against one ledger, so any marker the mint can write, an assembler
    can write. Closing it would take a secret this seam does not hold. What
    the design buys is a separation of HONEST paths; it does not defeat a
    writer who controls both ends, and nothing below pretends otherwise.

    SO THE QUESTION IS WHAT AN UNRUN RECEIPT BUYS BY ACCIDENT, and the
    answer is NOTHING — but for a narrower reason than the obvious one, and
    the obvious one is wrong. NEED_FOCUSED is reached only from a NON-approve
    verdict (dispatches.mark_verdict picks the need from the polarity), and
    an APPROVE asks NEED_SUITE, which refuses every focused receipt one
    branch above; `landgate.gate_binds_tree` and `foldcheck._tree_matches_gate`
    refuse the focused kind without consulting bind at all.

    IT IS NOT TRUE that a non-approve verdict closes nothing. A SUPERSEDE is
    admitted by `landreq.CONFIRMATION_POLARITIES` as the confirmation round
    of the close ladder's `resolved` rung, and that rung CLOSES a land
    request. The residue is harmless because that door decides on polarity,
    a head-anchored resolution statement and cross-family eyes and NEVER
    READS THE GATE FIELD — measured across every production reader of a
    verdict's `gate`, all of which require `polarity == "approve"` first
    (landreq's seven `_approval_refusal` call sites, the `subsumed` rung,
    seats_delegation's approved arm, rowstate's cross-family count). So a
    cure-round verdict backed by an assembled receipt ends up exactly where
    the same verdict with an empty evidence line ends up. What it changes is
    one WORD on an audit surface (`dispatches.gate_state` reads VERIFIED
    instead of UNVERIFIED), which is a reporting cost, not authority.

    THE ARMS THAT HOLD THAT ARGUMENT, so a future change cannot quietly
    invalidate it: FocusVerdictArms (approve + focused = refused before the
    ledger), FocusLandDoorArms (both land doors refuse the kind),
    CounterfeitArms.test_an_unrun_receipt_binds_and_buys_no_authority (the
    residue itself, asserted open, beside the floor it never rises above),
    and .test_no_door_admitting_a_non_approve_verdict_reads_its_gate — the
    tripwire, which goes RED the day the `resolved` rung learns to read a
    gate token. That is the day a focused receipt must start proving its
    run; until then this residue costs a word on a report."""
    focus = _focus_of(row)
    if row.get("v") != FOCUSED_VERSION or not focus:
        return "REFUSED", row["id"], (
            "receipt %s records no verifiable scope — only a v6 focused "
            "receipt binds its scope into its content id, and a scope the id "
            "does not protect is a scope anyone could have edited" % row["id"])
    if head != tip:
        return "REFUSED", row["id"], (
            "focused receipt %s ran on %s, the verdict reviews %s — a "
            "focused receipt binds only the exact tree its scope was "
            "measured against; re-run `helm gate run --focus` on the "
            "reviewed tip" % (row["id"], head[:12], tip[:12]))
    if focus.get("policy") != FOCUS_POLICY:
        return "REFUSED", row["id"], (
            "focused receipt %s was planned under policy %r and this binder "
            "re-derives %r — a scope this reader cannot reproduce cannot be "
            "checked, and an unchecked scope never binds; re-run `helm gate "
            "run --focus` on the reviewed tip"
            % (row["id"], focus.get("policy"), FOCUS_POLICY))
    base = str(focus.get("base") or "").strip().lower()
    if not _SHA.fullmatch(base):
        return "REFUSED", row["id"], (
            "focused receipt %s records no full-sha base — its scope cannot "
            "be re-derived, so it cannot be checked" % row["id"])
    recorded = focus.get("changed")
    if not isinstance(recorded, list) \
            or not all(isinstance(p, str) for p in recorded):
        return "REFUSED", row["id"], (
            "focused receipt %s's recorded changed-set is unreadable — an "
            "uncheckable scope refuses" % row["id"])
    recorded_sel = focus.get("selected")
    if not isinstance(recorded_sel, list) \
            or not all(isinstance(m, str) for m in recorded_sel):
        return "REFUSED", row["id"], (
            "focused receipt %s's recorded selection is unreadable — an "
            "uncheckable scope refuses" % row["id"])
    repo = str(repo_id or "")
    if not os.path.isabs(repo) or os.path.realpath(repo) != repo \
            or not os.path.isdir(repo):
        return "REFUSED", row["id"], (
            "standing dispatch repository is unreadable — the recorded scope "
            "cannot be re-measured against the reviewed tip, and a scope "
            "nobody re-measures is a guard fed its own answer")
    git = vcs.backend(repo)
    trunk_name = git.trunk_ref(repo)
    trunk = (git.head_sha(repo, ref=trunk_name) or "").strip().lower()
    if not trunk:
        return "REFUSED", row["id"], (
            "the standing repository cannot resolve trunk (%s), so the "
            "recorded base cannot be re-derived — and a base only the "
            "receipt vouches for is a base the submitter chose" % trunk_name)
    derived_base, mb_err = _single_merge_base(git, repo, trunk, tip,
                                              trunk_name)
    if mb_err:
        return "REFUSED", row["id"], mb_err
    if derived_base != base:
        return "REFUSED", row["id"], (
            "focused receipt %s records base %s but the standing repository "
            "derives merge-base %s for this tip — the recorded base is the "
            "submitter's word, and this reader only takes the repository's"
            % (row["id"], base[:12], derived_base[:12]))
    rc, out, err = git.text(repo, "diff", "--name-only", "-z", base, tip)
    if rc != 0:
        return "REFUSED", row["id"], (
            "cannot re-derive the changed set %s..%s in the standing "
            "repository (%s) — an unmeasurable scope refuses"
            % (base[:12], tip[:12], err or "git exited %s" % rc))
    derived = {p for p in (out or "").split("\0") if p}
    uncovered = sorted(derived - set(recorded))
    if uncovered:
        return "REFUSED", row["id"], (
            "focused receipt %s's recorded scope does not cover %s — the "
            "selected tests never measured %s change%s; re-run `helm gate "
            "run --focus` (or the whole suite) on the reviewed tip"
            % (row["id"],
               ", ".join(uncovered[:4]) + (" (+%d more)" % (len(uncovered) - 4)
                                           if len(uncovered) > 4 else ""),
               "that" if len(uncovered) == 1 else "those",
               "" if len(uncovered) == 1 else "s"))
    phantom = sorted(set(recorded) - derived)
    if phantom:
        return "REFUSED", row["id"], (
            "focused receipt %s records %s in its scope and the reviewed "
            "diff does not contain %s — the scope was measured against some "
            "other tree state (uncommitted work, or an assembled row); "
            "re-run `helm gate run --focus` on the clean reviewed tip"
            % (row["id"],
               ", ".join(phantom[:4]) + (" (+%d more)" % (len(phantom) - 4)
                                         if len(phantom) > 4 else ""),
               "it" if len(phantom) == 1 else "them"))
    unmapped = sorted(f for f in derived if _module_name(f) is None)
    if unmapped:
        return "REFUSED", row["id"], (
            "the reviewed diff includes %s, which no import graph can cover "
            "— this diff can never carry a focused receipt; run the whole "
            "suite" % ", ".join(unmapped[:4]))
    files, files_err = _tree_module_files(repo, tip)
    if files_err:
        return "REFUSED", row["id"], (
            "the reviewed tree cannot be re-read for scope derivation: %s"
            % files_err)
    changed_modules = {_module_name(f) for f in derived}
    derived_sel, derived_universe, sel_err = _scope_selection(
        files, changed_modules)
    if sel_err:
        return "REFUSED", row["id"], (
            "the consumer closure cannot be re-derived at %s: %s"
            % (tip[:12], sel_err))
    if sorted(recorded_sel) != derived_sel:
        missing = sorted(set(derived_sel) - set(recorded_sel))
        extra = sorted(set(recorded_sel) - set(derived_sel))
        return "REFUSED", row["id"], (
            "focused receipt %s's recorded selection disagrees with the "
            "re-derived consumer closure at %s%s%s — the selection is the "
            "receipt's whole claim, and this reader only takes the one it "
            "derives itself; re-run `helm gate run --focus` on the reviewed "
            "tip" % (
                row["id"], tip[:12],
                (" (never ran: %s)" % ", ".join(missing[:4])) if missing
                else "",
                (" (claims modules the closure does not select: %s)"
                 % ", ".join(extra[:4])) if extra else ""))
    if focus.get("universe") != len(derived_universe):
        return "REFUSED", row["id"], (
            "focused receipt %s records a %s-module test universe and the "
            "reviewed tree holds %d — the ratio on its evidence line "
            "describes some other tree" % (row["id"], focus.get("universe"),
                                           len(derived_universe)))
    executed = focus.get("executed")
    if not isinstance(executed, list) \
            or not all(isinstance(m, str) for m in executed):
        return "REFUSED", row["id"], (
            "focused receipt %s records no readable RAN set — the selection "
            "is only what was asked for, and a receipt that cannot say what "
            "the runner reported proves nothing about coverage; re-run `helm "
            "gate run --focus` on the reviewed tip" % row["id"])
    never_ran = sorted(set(derived_sel) - set(executed))
    if never_ran:
        return "REFUSED", row["id"], (
            "focused receipt %s selected %s and the runner never reported a "
            "test from %s — the consumer closure was asked for and not "
            "measured, so this receipt covers less than its scope claims"
            % (row["id"],
               "them" if len(never_ran) > 1 else "it",
               ", ".join(never_ran[:4])
               + (" (+%d more)" % (len(never_ran) - 4)
                  if len(never_ran) > 4 else "")))
    ids, ran = focus.get("executed_ids"), row.get("ran")
    if type(ids) is not int or ids < 0:
        return "REFUSED", row["id"], (
            "focused receipt %s records no test-id count from its runner — "
            "the RAN set it reports cannot be weighed against the run's own "
            "summary" % row["id"])
    if type(ran) is not int or ids > ran:
        return "REFUSED", row["id"], (
            "focused receipt %s reports %d test ids from a run whose own "
            "summary says it ran %s — a reader that matched more ids than "
            "the runner executed was matching something other than test "
            "ids, and the module set it derived cannot be trusted to say "
            "what ran" % (row["id"], ids, ran))
    return "VERIFIED", row["id"], (
        "%s on %s@%s — FOCUSED %d/%d, scope covers the %d-file reviewed diff, "
        "runner reported %d test%s across %d module%s"
        % (interpreter_label(_ident_of(row)), tip[:12],
           host_label(_host_of(row)), len(derived_sel),
           len(derived_universe), len(derived), ids, "" if ids == 1 else "s",
           len(executed), "" if len(executed) == 1 else "s"))


def _declared_row_refusal(row):
    """Why a DECLARED-COMMAND receipt is not self-consistent, or None.

    Called only for a row that carries a `suite_command` block — see
    `row_refusal`, which asks this INSTEAD of the frozen-serial-argv clause.
    Both halves of what it checks are inside the receipt id, so this refuses a
    row assembled by hand rather than minted, and it is the clause that stops
    the whole-suite word from being pasted onto a command the mint never
    composed.
    """
    rid = row.get("id") or "<unidentified>"
    if row.get("suite") is not True:
        return ("receipt %s is a declared-command receipt that does not claim a "
                "whole-suite run — the kind exists only to carry one" % rid)
    block = row.get("suite_command")
    if not isinstance(block, dict):
        return ("receipt %s carries no readable `suite_command` block, so it "
                "names no command it can be held to" % rid)
    if block.get("source") != "registry":
        return ("receipt %s records command source %r; the declared kind is "
                "minted only from a project's own registry declaration"
                % (rid, block.get("source")))
    argv, declared_argv = row.get("argv"), block.get("argv")
    if not isinstance(declared_argv, list) or not declared_argv \
            or any(type(arg) is not str for arg in declared_argv):
        return ("receipt %s declares no readable command argv" % rid)
    if argv != declared_argv:
        return ("receipt %s RAN %r and says it ran %r — a receipt whose "
                "recorded command is not the command it claims proves nothing "
                "about either" % (rid, argv, declared_argv))
    protocol = block.get("protocol")
    if protocol not in PROTOCOLS or type(protocol) is not str:
        return ("receipt %s records verdict protocol %r, which is not one this "
                "helm reads (%s)" % (rid, protocol, " or ".join(PROTOCOLS)))
    if protocol == PROTOCOL_EXIT:
        if row.get("ran") is not None:
            return ("receipt %s reports %r executed tests under the `exit` "
                    "protocol, which counts none — the count was not measured "
                    "by the run" % (rid, row.get("ran")))
        if (row.get("status") == "OK") != (row.get("rc") == 0):
            return ("receipt %s says %s beside exit status %r, and under the "
                    "`exit` protocol the exit status IS the verdict"
                    % (rid, row.get("status"), row.get("rc")))
    return None


def _declared_origin_refusal(row, consuming_repo=None):
    """Why this row's declared command has no ESTABLISHED ORIGIN, or None.

    `consuming_repo` is THE REPOSITORY ABOUT TO SPEND THE ROW, and it is the
    subject of the whole question below. A caller with no repository in hand
    (the row-level admissibility question, asked without a consumer) passes
    None and the row is judged about the repository it names — there is no
    second repository in that question for a declaration to travel to.

    A `suite_command` block is a row's claim ABOUT ITSELF. Every field
    `_declared_row_refusal` compares is inside the row and inside its content
    id, so a block that agrees with itself and hashes correctly proves only that
    nobody edited it after it was written — the public id carries no secret and
    says nothing about who wrote the row. The fact that makes the block a
    DECLARATION lives outside it: the owner's authored registry
    (`registry-authored.json`, the AUTHORED_FIELDS layer no scan rebuilds) says
    this project declares this command with this protocol. That is what is
    checked here, and matching self-asserted fields alone are not enough.

    THE PRICE IS PAID BY A RECEIPT THAT TRAVELS, and it is the right way round.
    A host with no declaration for that project cannot establish the origin, so
    the row is not admitted as a declaration there — and a whole-suite claim it
    cannot check is exactly the claim it must not spend. The alternative is a
    reader that believes a command block because the block says to.

    AND THE DECLARATION BINDS THE REPOSITORY THAT CONSUMES THE ROW, NEVER THE
    REPOSITORY THE ROW NAMES. Matching the project LABEL, the argv and the
    protocol asks only whether such a declaration exists SOMEWHERE in the
    owner's authored file — every one of those three values is inside the row
    and inside its content id, so a declaration authored for project A licensed
    a generic whole-suite row spent in an UNRELATED repository B: mint A's
    honest receipt, restamp its head/tree with B's, leave `repo_id` naming A,
    import it into B (which writes B a real binding), recompute the public id,
    and the frozen-argv admission below was skipped on the strength of a block
    A's declaration established. `repo_id` IS A FIELD OF THE ROW, so reading it
    as the subject let the row choose which declaration judged it.

    AND THE QUESTION IS A COMMAND FOR A REPOSITORY, never a label. The project
    NAME is a field of the row too, so filtering the authored declarations by it
    asks the row which declaration should judge it — and a registry name is
    PER-HOST, so an honest receipt from a box that registers this repository
    under another name was refused for a reason about two registries rather than
    about authority. The owner's authored declaration for the consuming
    repository, with the row's exact argv and protocol, is the whole predicate.

    THE SUBJECT IS THEREFORE THE CALLER'S REPOSITORY, resolved before the row is
    judged at all (`bind` hands it down; `work._gc` hands down the repository
    whose green trees it is discharging). The declaration must be authored for
    THAT repository — the same repository fold `_root_declaration` uses to
    resolve the command in the first place: the recorded location outright, or
    one shared common dir away (a linked worktree of the declared root is the
    tree a lane's gate runs in). A consuming repository this helm cannot read
    establishes nothing.

    AND TRAVEL STAYS HONEST, which the `repo_id` reading also got backwards. A
    receipt minted on a fab node records that node's path in `repo_id`, and
    `gate import` DOES NOT REWRITE IT — the import records the origin separately
    (`origin_repo` on the binding row) and keeps the receipt's content, id
    included, byte for byte. So an honest imported row's `repo_id` names a
    directory this box has never had, which under the old reading was refused as
    "a repository this helm cannot read" no matter how good the importing
    repository's own declaration was. Asking about the CONSUMING repository
    admits it on B's declaration and leaves the recorded origin as what it is:
    provenance for a reader, never authority.

    AND THE DECLARATION IS THE EFFECTIVE ONE, NOT ANY MATCHING ONE. Scanning the
    authored file for a declaration whose argv and protocol match the row and
    whose location folds into this repository answered ADMITTED for a declaration
    that would never be RUN here — and a receipt's whole value is the claim that
    the repository's own suite passed. A root R declaring `["true"]` and a linked
    worktree W declaring its own real command live in ONE repository, so R's
    honest green `true` receipt matched R's entry, folded in, and waived W's
    frozen-argv admission for a whole-suite claim about W. Nothing was forged;
    the scan simply asked a weaker question than the spawn does. The same scan
    admitted at a location whose census is AMBIGUOUS (two declarations, no exact
    entry) or UNKNOWN (a candidate whose location git cannot read) — the two
    states `suite_command` refuses to spawn from at all. So this asks
    `_effective_declaration` for the CONSUMING LOCATION and compares that ONE
    answer: ambiguity, a malformed exact declaration, an unreadable registry or
    census, and "nothing declared here" all REFUSE, each naming its own reason.
    A caller that names its repository by the shared ADMIN DIR has named the
    repository and not the tree, so it resolves the consuming location first
    (`bind`'s `consuming_repo`) — a common dir has no exact declaration of its
    own and would silently drop that precedence.

    THE MATCHING-ELSEWHERE PATHS ARE STILL NAMED, as a diagnostic under the
    refusal rather than as the authority: "your declaration lives over there" is
    the sentence an owner can act on, and it is computed only once the effective
    answer has already refused.
    """
    from . import registry
    rid = row.get("id") or "<unidentified>"
    block = row.get("suite_command")
    block = block if isinstance(block, dict) else {}
    project = block.get("project")
    if type(project) is not str or not project:
        return ("receipt %s names no project for its declared command (%r), so "
                "nothing outside the receipt can establish that the command was "
                "ever declared" % (rid, project))
    repo = consuming_repo if type(consuming_repo) is str and consuming_repo \
        else row.get("repo_id")
    if type(repo) is not str or not repo:
        return ("receipt %s names no repository (%r) and no consuming "
                "repository was resolved, so no declaration can be bound to "
                "the tree that is about to spend it" % (rid, repo))
    # THE PROJECT LABEL IS NOT PART OF THE QUESTION, and filtering on it was the
    # same error one level down. `project` is a field of the ROW, so a submitter
    # picks it; requiring it to match only means picking the label the target's
    # declaration wears. It is also a PER-HOST registry name — one repository is
    # legitimately registered as `helm` on one box and something else on another —
    # so matching it refuses honest travel for a reason that is about two
    # registries and not about authority. What the owner authored is a COMMAND FOR
    # A REPOSITORY: the EFFECTIVE one for the consuming location is the question,
    # and the label is carried below only so the refusal can quote the row.
    decision, err = _effective_declaration(repo)
    if err:
        return ("receipt %s carries project %s's declared command and helm "
                "cannot say which command %s itself declares: %s — a receipt is "
                "admitted as a DECLARED one only where the declaration that "
                "would RUN here establishes it, and that answer is not "
                "available" % (rid, project, repo, err))
    plan = decision["plan"]
    if plan is not None and plan["argv"] == block.get("argv") \
            and plan["protocol"] == block.get("protocol"):
        return None
    if plan is not None:
        return ("receipt %s says project %s declared %r under the %r protocol, "
                "and no authored declaration for %s establishes that — that "
                "location effectively declares %r under %r (authored for %s), "
                "and a declaration licenses only receipts that ran ITS OWN "
                "command, so a receipt from another tree of this repository "
                "proves nothing about the command this one declares"
                % (rid, project, block.get("argv"), block.get("protocol"), repo,
                   plan["argv"], plan["protocol"], decision["path"]))
    # NOTHING EFFECTIVE HERE. Name where a matching declaration DOES live, which
    # is the sentence the owner can act on — computed only now, and never as the
    # authority.
    elsewhere = []
    try:
        declarations = registry.authored_declarations(DECLARED_GATE_FIELD)
    except Exception:                            # noqa: BLE001
        declarations = ()
    for rec in declarations:
        matched, rec_err = _declared_command(rec["project"], rec["value"])
        if rec_err or matched["argv"] != block.get("argv") \
                or matched["protocol"] != block.get("protocol"):
            continue
        elsewhere.append(rec["path"])
    mine = _repository_of(repo)
    if elsewhere:
        return ("receipt %s carries project %s's declared command and is being "
                "spent in %s, which is not the repository that declaration was "
                "authored for (%s) — a declaration licenses receipts spent in "
                "ITS OWN repository, however exactly the recorded command "
                "matches%s"
                % (rid, project, repo, ", ".join(sorted(set(elsewhere))),
                   "" if mine is not None else
                   "; this helm cannot read %s as a repository at all, and an "
                   "unreadable tree establishes nothing" % repo))
    return ("receipt %s says project %s declared %r under the %r protocol, and "
            "no authored declaration in %s establishes that — a `suite_command` "
            "block is the receipt's own claim, and the declaration it claims "
            "lives in the project's authored registry entry"
            % (rid, project, block.get("argv"), block.get("protocol"),
               home.authored_path()))


def row_refusal(row, about="", consuming_repo=None):
    """Why this receipt ROW proves nothing about the tree it names, or None.

    `consuming_repo` IS THE REPOSITORY ABOUT TO SPEND THE ROW, and it is the
    subject of the declared-origin clause below: a declaration is authored for a
    repository, and the row's own `repo_id` is a field of the row. Every caller
    that has a repository in hand passes it (`bind` resolves it BEFORE asking
    this; `work._gc` passes the root whose green trees it is discharging), which
    is what stops a declaration authored for A licensing a row spent in B. A
    caller with no repository asks the row-level question and gets it: the row is
    judged about the repository it names, which is the only one in that
    question.

    THE ONE CANONICAL RECEIPT-ADMISSIBILITY PREDICATE, extracted from `bind`
    rather than written beside it.
    `helm.work._gc.receipt_inadmissible` had grown a SECOND reader of this same
    question and the two had already diverged in two ways that both admitted a
    receipt this one refuses:

      no `rc` clause, so a row saying status OK while the runner exited nonzero
        counted as green;
      and `tree != tree_after` as the bracket test, which on a PRE-BRACKET row
        compares None against None, is False, and therefore PASSES — the exact
        row that cannot show the worktree held still.

    Both are the shape this rung exists to complain about, committed inside the
    rung. So the clauses live here, once, and every consumer asks this.

    ORDER AND MESSAGES ARE PRESERVED EXACTLY as `bind` had them, because a
    refusal string is a contract with the reader and several arms pin it.
    `about` is only the subject named in the bracket message; it is cosmetic
    and defaults to empty so a caller with no tip in hand can still ask.

    THIS IS ROW-LEVEL ONLY. Whether the row is STRONG ENOUGH for what a caller
    is about to do — whole-suite versus focused, exact tip versus descendant —
    is `bind`'s remaining job and deliberately not here: that question needs
    the caller's `need`, and folding it in is how one predicate would start
    answering two questions again.

    A MISSING FIELD IS NOT A FIELD WHOSE VALUE IS FALSE, and every clause
    below used to read one as the other. `row.get("dirty")` on a row with no
    `dirty` key is None, which is falsy, which passed the cleanliness test —
    so a row that never recorded whether the worktree was clean was admitted
    as a row that recorded it CLEAN. `rc` had the same shape one clause down
    (`not in (0, None)`), so a row with no runner exit passed the exit test.
    That is the reader consuming its own default and calling the result
    evidence. Absence now refuses BY NAME, ahead of every value clause, so
    the three states a field can be in — absent, present-and-bad,
    present-and-good — get three answers instead of two.

    THE BRACKET HALVES ARE DELIBERATELY NOT IN THAT LIST. `head_after` and
    `tree_after` are absent on every pre-bracket v1 receipt, and the clause
    below already refuses them with a message that names what is missing and
    what it cost; arms pin that wording. Listing them here would only change
    which sentence a v1 row gets refused with.
    """
    if row.get("v") == WITHDRAWN_SHARD_VERSION:
        return ("receipt %s is a WITHDRAWN sharded script receipt (v7) — "
                "fresh workers cannot preserve arbitrary serial unittest "
                "process state; only a literal serial discovery receipt can "
                "authorize a land" % (row.get("id") or "<unidentified>"))
    if row.get("v") == SHARDED_AUTHORITY_VERSION:
        refusal = gateauthority.receipt_refusal(row)
        if refusal:
            return refusal
    for field in ("id", "head", "tree", "dirty", "status", "rc"):
        if field not in row:
            return ("receipt %s carries no %r field — a reader that DEFAULTS a "
                    "missing field is reading its own default, not the run"
                    % (row.get("id") or "<unidentified>", field))
    if row.get("dirty") or row.get("dirty_after"):
        return ("receipt %s ran on a DIRTY worktree (%s) — it proves a tree "
                "nobody else can check out"
                % (row["id"], "before" if row.get("dirty") else "after the run"))
    # THE BRACKET. A receipt whose tree MOVED under the run describes no single
    # tree, so it can name a commit but never bind one. `_after` is absent on a
    # pre-bracket receipt and its absence REFUSES rather than defaults.
    for half in ("head_after", "tree_after"):
        if not row.get(half):
            return ("receipt %s has no post-run tree read — it cannot show the "
                    "worktree held still, so it proves nothing about %s"
                    % (row["id"], about))
    if row.get("head_after") != row.get("head") \
            or row.get("tree_after") != row.get("tree"):
        return ("receipt %s: the worktree MOVED during the run (%s -> %s)"
                % (row["id"], str(row.get("head"))[:12],
                   str(row.get("head_after"))[:12]))
    if row.get("status") == "OK" and row.get("rc") != 0:
        # `not in (0, None)` STOOD HERE AND ADMITTED THE TIMEOUT SHAPE. `rc`
        # is None on a run whose child never exited, and `_mint_result` turns
        # exactly that into status UNKNOWN — so a row reading OK with a null
        # rc is not a receipt this helm can have written, and reading it as a
        # pass was the reader supplying the exit code the run never gave it.
        # Measured over the 2,439-row ledger: all 1,871 OK rows record rc 0,
        # so requiring it costs nothing honest.
        return ("receipt %s says OK and the runner exited %s"
                % (row["id"], row.get("rc")))
    declared = False
    if row.get("suite_command") is not None:
        # A DECLARED COMMAND IS HELD TO ITS OWN DECLARATION, AND THEN TO ITS
        # ORIGIN. Self-consistency is the cheap half and it is not the authority
        # half: every field it compares lives inside the row, so a hand-built
        # block that agrees with itself passes it. What makes the block a
        # DECLARATION is that something outside the row says so, which is why
        # `_declared_origin_refusal` reads the owner's authored registry — and
        # why the frozen-argv clause below is NOT skipped on the strength of the
        # block alone.
        refusal = _declared_row_refusal(row) \
            or _declared_origin_refusal(row, consuming_repo)
        if refusal:
            return refusal
        declared = True
    if row.get("v") in HISTORICAL_SERIAL_VERSIONS and row.get("suite"):
        # An old receipt cannot acquire the new runner's authority merely by
        # changing argv and recomputing its public content ID. Freeze this
        # grammar independently of SUITE, which the active writer may change.
        #
        # AND INDEPENDENTLY OF THE `suite_command` BLOCK, which is the whole
        # shape of this clause. Written as the `elif` of the block test, a row
        # bought its way out of the frozen grammar by ASSERTING a declaration:
        # add a block whose argv equals the row's own noncanonical argv,
        # recompute the public id, and an honest subset run held whole-suite
        # authority. `declared` is set above only after the origin was
        # established from the authored registry, so the escape from this clause
        # costs an authored declaration and cannot be paid for by the row.
        executable = _ident_of(row).get("executable")
        frozen = isinstance(executable, str) and os.path.isabs(executable) \
            and row.get("argv") == [executable] + list(gateauthority.SERIAL_ARGV)
        if not frozen and not declared:
            return ("receipt %s: v%s whole-suite authority requires the exact "
                    "historical serial discovery argv, or a project gate "
                    "declaration helm can establish independently of the "
                    "receipt; the sharded runner requires v9 evidence"
                    % (row["id"], row.get("v")))
    return None


def bind(evidence, reviewed_tip, repo_id=None, reviewed_ts=None,
         need=NEED_SUITE, consuming_repo=None):
    """Does this evidence prove a clean run STRONG ENOUGH for `need` at this
    tip?

    `consuming_repo` IS THE WORKING TREE THIS ANSWER IS SPENT IN, and it exists
    because `repo_id` is allowed to be a REPOSITORY rather than a tree. A caller
    that identifies the repository by its shared admin dir (`--git-common-dir`)
    is naming the thing `_repository_authority_refusal` wants and NOT the thing
    the declared-origin clause wants: a common dir has no authored declaration of
    its own, so the exact-path precedence a worktree's own declaration relies on
    would be silently skipped and the repository fold answered in its place.
    IT IS THE COORDINATE THE DISPATCH ROW ALREADY CARRIES (`repo_root`, written
    at send time from the tree the send ran in) and not a value this reader
    re-derives; `_consuming_location` validates it against the FILESYSTEM at
    the instant of this act and against the repository about to spend the row
    (the path-to-repository memo is re-read, never trusted, for a spend),
    answers UNKNOWN for a checkout that can no longer be read, and falls back to
    `repo_id` only for a row that carries no coordinate at all — where the
    common dir's ambiguity refusal is still the right answer.

    `need` is the consumer declaring what it is about to do with the answer —
    the question, never the receipt, decides the bar:

      NEED_SUITE (default)  "every test in this repo passed" — what a LAND
                            spends. Only a whole-suite receipt satisfies it;
                            every pre-existing caller gets this unchanged.
      NEED_FOCUSED          "the tests this change can reach passed" — what a
                            cure-round review verdict spends. A focused (v6)
                            receipt satisfies it at the EXACT tip, and only
                            after this reader RE-DERIVES the changed set from
                            the standing repository and proves the recorded
                            scope covers it — a scope nobody re-measures is a
                            guard fed its own answer. A whole-suite receipt
                            also satisfies it: strength covers weakness,
                            never the reverse.

    Exact-tip receipts retain their historical binding. A later whole-suite
    receipt may also bind when the standing dispatch repository proves the
    reviewed tip is its ancestor, or the two tips' unique merge base proves the
    reviewed tip's ordered patch sequence appears as one contiguous car in the
    tested train, and the receipt postdates that dispatch. The patch sequence is
    derived from repository commits; a caller cannot supply the answer. A focused
    receipt has NO descendant or train arm: its scope was measured against one
    tree, and every commit after that tree is a commit its selection never saw.
    UNKNOWN never binds.
    """
    if need not in (NEED_SUITE, NEED_FOCUSED):
        # Fail-closed on the AXIS itself: a typo'd need must not quietly
        # receive the weaker answer.
        return "REFUSED", None, ("unknown binding need %r — this reader "
                                 "answers %r and %r" % (need, NEED_SUITE,
                                                        NEED_FOCUSED))
    tip = str(reviewed_tip or "").strip().lower()
    tok = token(evidence)
    if not tok:
        return "UNVERIFIED", None, "evidence carries no minted gate receipt"
    row, err = by_id(tok)
    if err:
        return "REFUSED", None, err
    if not _SHA.fullmatch(tip):
        return "REFUSED", row["id"], "reviewed tip is not a full commit id"
    # THE CONSUMING REPOSITORY IS RESOLVED FIRST, because one clause below the
    # next line is ABOUT it: a declared command's origin is established against
    # the repository that is about to spend the row, and `repo_id` is a field of
    # the row. Resolved after, `row_refusal` had only the row's own word for its
    # subject, and a declaration authored for A admitted a row spent in B.
    # RESOLUTION IS UNCHANGED, only moved: the caller's repository when it named
    # one, else the repository the row names.
    repo = str((row.get("repo_id") if repo_id is None else repo_id) or "")
    # AND THE CONSUMING TREE IS A SECOND, NARROWER SUBJECT. `repo` is what the
    # authority door spends against (it accepts a shared admin dir); the declared
    # origin clause needs the LOCATION whose declaration would run, so a caller
    # that has the tree in hand says so. Absent that, the two are one value —
    # which is every caller that names a working tree in `repo_id`.
    consuming, consuming_why = _consuming_location(consuming_repo, repo)
    if consuming_why:
        return "REFUSED", row["id"], consuming_why
    refusal = row_refusal(row, about=tip[:12], consuming_repo=consuming)
    if refusal:
        return "REFUSED", row["id"], refusal
    head = str(row.get("head") or "").lower()
    if head != tip and repo_id is None and reviewed_ts is None:
        return "REFUSED", row["id"], (
            "receipt %s ran on %s, the verdict reviews %s — re-run the gate on "
            "the reviewed tip" % (row["id"], head[:12], tip[:12]))
    if row.get("status") != "OK":
        return "REFUSED", row["id"], (
            "receipt %s is %s, not OK%s" % (row["id"], row.get("status"),
                                            " (" + row["detail"] + ")"
                                            if row.get("detail") else ""))
    # Custom-command receipts carry no Helm-selected interpreter and therefore
    # cannot answer the whole-suite question, exact or descendant.
    if not _ident_of(row).get("name"):
        return "REFUSED", row["id"], (
            "receipt %s ran a custom command, so helm never chose its "
            "interpreter and cannot name it" % row["id"])
    # THE THIRD AXIS, and ABSENCE REFUSES — the same door the post-run bracket
    # holds one branch above. A receipt that cannot name its box is a receipt
    # nobody can audit: a fab-minted red was once honest about the
    # interpreter, honest about the tree, honest about the count, and cost a
    # reviewer a turn on 14 failures that only exist on the OTHER machine.
    # Pre-v4 receipts have no host and are refused here rather than
    # grandfathered, because "it was probably this box" is precisely the
    # inference the receipt exists to make unnecessary.
    if not _host_of(row).get("node"):
        return "REFUSED", row["id"], (
            "receipt %s does not name the HOST it ran on — a suite can be "
            "honest about the code and wrong about the box, and this one "
            "cannot be audited either way. Re-run `helm gate run` (or `fab "
            "gate`) for a receipt that names its machine; a FIX or SUPERSEDE "
            "needs no receipt at all, so dropping the token is the other "
            "honest answer" % row["id"])
    # REPOSITORY AUTHORITY IS THE FINAL ADMISSION AXIS, not an entry belt.
    # The receipt ledger is global; content identity proves WHAT ran, never
    # which repository may spend it. Every branch below first exhausts receipt
    # and scope diagnostics that authorize nothing. Exact-tip/tree/focus then
    # ask at their VERIFIED exits; descendant binding asks before time/ancestry,
    # because those classify standing repository state and cannot shadow a
    # missing capability. This is the retire-belt law: validate the whole object
    # before spending its capability, and spend before downstream classification.
    # `repo` was resolved above, at the top of this function, because the
    # declared-origin clause inside `row_refusal` is about it. One resolution,
    # one value: a second `repo = ...` here is how the two readers of "which
    # repository is this" would start disagreeing.
    if not row.get("suite"):
        # A helm-composed FOCUSED run: it carried an interpreter, so it fell
        # through the custom refusal above, and the `suite` flag is what now
        # separates "every test" from "these tests".
        if need != NEED_FOCUSED:
            focus = _focus_of(row)
            return "REFUSED", row["id"], (
                "receipt %s is FOCUSED (%s of %s test modules) — it proves "
                "the tests its scope selected and nothing about the rest, "
                "and what you are doing needs the whole suite. Run `helm "
                "gate run` on the reviewed tip"
                % (row["id"],
                   len(focus.get("selected"))
                   if isinstance(focus.get("selected"), list) else "?",
                   focus.get("universe")
                   if type(focus.get("universe")) is int else "?"))
        focused = _bind_focused(row, tip, head, repo_id)
        if focused[0] != "VERIFIED":
            return focused
        authority_refusal = _repository_authority_refusal(row, repo)
        if authority_refusal:
            return "REFUSED", row["id"], authority_refusal
        return focused
    if head == tip:
        authority_refusal = _repository_authority_refusal(row, repo)
        if authority_refusal:
            return "REFUSED", row["id"], authority_refusal
        return "VERIFIED", row["id"], "%s on %s@%s" % (
            interpreter_label(_ident_of(row)), tip[:12],
            host_label(_host_of(row)))
    # THE TREE IS THE SAME EVIDENCE AS head==tip, AND THE RECEIPT ALREADY
    # CARRIES IT. Below, a receipt whose head differs falls to a chain that
    # asks whether it POSTDATES the review — a proxy for "was it run on this
    # content". Tree identity answers that question directly: identical trees
    # are identical BYTES, so the suite provably ran on what is under review,
    # whatever sha the run wore or what o'clock it was.
    #
    # AND head != tip IS THE ORDINARY CASE, NOT AN EDGE ONE. `fab gate`
    # snapshots a dirty working tree into a synthetic commit ("fab snapshot
    # (tracked+untracked) of X+dirty"), so gate-then-commit — gate what you
    # have, commit what passed — always lands here. MEASURED: an
    # approve on task/1287 was refused for arriving ten minutes before its
    # review row, on a receipt naming the reviewed tip's EXACT tree, with an
    # empty diff between the two commits.
    #
    # IT CAN ONLY EVER UPGRADE. Any doubt — unreadable repo, unresolvable
    # tip, absent or malformed tree field — falls through to the chain that
    # ran before this existed, so no refusal below is removed, weakened, or
    # REORDERED. Adding a verdict is safe here in a way that adding a refusal
    # would not be.
    tree = str(row.get("tree") or "")
    if _SHA.fullmatch(tree) and repo_id and os.path.isabs(str(repo_id)) \
            and os.path.isdir(str(repo_id)):
        if _tree_at(str(repo_id), tip) == tree:
            authority_refusal = _repository_authority_refusal(row, repo)
            if authority_refusal:
                return "REFUSED", row["id"], authority_refusal
            return "VERIFIED", row["id"], "%s on tree %s (head %s) @%s" % (
                interpreter_label(_ident_of(row)), tree[:12], head[:12],
                host_label(_host_of(row)))
    if not _SHA.fullmatch(head):
        return "REFUSED", row["id"], "receipt head is not a full commit id"
    # Descendant classification can reveal/order repository state but can never
    # strengthen a receipt that has no capability in this repository. Content
    # diagnostics above still run first; authority then precedes timestamps and
    # ancestry so a missing capability cannot be shadowed by a postdate proxy.
    authority_refusal = _repository_authority_refusal(row, repo)
    if authority_refusal:
        return "REFUSED", row["id"], authority_refusal
    opened, minted = _binding_ts(reviewed_ts), _binding_ts(row.get("ts"))
    if opened is None:
        return "REFUSED", row["id"], (
            "standing review has no canonical opening timestamp — a later "
            "receipt cannot prove it postdates the review")
    if minted is None:
        return "REFUSED", row["id"], (
            "receipt %s has no canonical timestamp — its order after the "
            "review is UNKNOWN" % row["id"])
    if not os.path.isabs(repo) or os.path.realpath(repo) != repo \
            or not os.path.isdir(repo):
        return "REFUSED", row["id"], (
            "standing dispatch repository is unreadable — descendant ancestry "
            "is UNKNOWN")
    verdict, relation, sequence = carriage(repo, tip, head)
    # THE TIMESTAMP IS A PROXY FOR CONTAINMENT AND CARRIAGE MEASURES
    # CONTAINMENT DIRECTLY, so a receipt PROVEN to carry the reviewed tip does
    # not have to postdate anything. What the order protects against is a
    # receipt that cannot have run on this work; `carriage` answering CARRIED
    # is that same question answered from the repository instead of from two
    # clocks, and a direct reading outranks its own proxy.
    #
    # THE WORKFLOW MAKES THE PROXY WRONG, NOT MERELY REDUNDANT. An integrator
    # composes a train, gates it, and mints the reviewer's row ON THE GREEN —
    # so in a train the receipt ALWAYS predates the row. Ordered ahead of the
    # rung because a gap of seconds between a green train and the row minted
    # on it is the NORMAL shape, not an anomaly: carriage can report a car
    # contained at a known offset while the clocks disagree by under two
    # minutes, and a refusal there is a refusal of the ordinary case.
    #
    # THIS IS THE SECOND FACE OF A CURE THAT ALREADY LANDED. The tree-equality
    # fast path above sits over every timestamp rung for exactly this reason,
    # and its comment records the measured case (an approve on task/1287,
    # refused for arriving ten minutes before its review row on a receipt
    # naming the reviewed tip's EXACT tree). A train can never take that path:
    # its tree is trunk plus every car and cannot equal one car's tree.
    #
    # NARROW ON PURPOSE, and the two rungs above are NOT bypassed. An
    # unparseable or absent timestamp is a MALFORMED RECORD rather than an
    # ordering fact, and a malformed record still fails closed; only the
    # ORDER between two readable timestamps yields to the direct measurement.
    # CARRIAGE_UNKNOWN and NOT_CARRIED fall through to the rung unchanged,
    # which is the population it was written to police.
    if minted <= opened and verdict is not CARRIED:
        return "REFUSED", row["id"], (
            "receipt %s at %s does not postdate the review at %s"
            % (row["id"], row.get("ts"), reviewed_ts))
    success = "%s on %s containing reviewed %s" % (
        interpreter_label(_ident_of(row)), head[:12], tip[:12])
    if relation != vcs.ANCESTOR:
        if relation != vcs.NOT_ANCESTOR:
            return "REFUSED", row["id"], (
                "receipt %s ran on %s, which cannot be proven to contain the "
                "reviewed tip %s in the standing repository" %
                (row["id"], head[:12], tip[:12]))
        state, start, reviewed_n, carrier_n = sequence
        if verdict is not CARRIED:
            detail = {
                vcs.PATCH_SEQUENCE_AMBIGUOUS:
                    "its ordered patch sequence appears more than once",
                vcs.PATCH_SEQUENCE_ABSENT:
                    # Naming only absence and reordering sent integrators
                    # hunting their own composition. ANOTHER cause reaches
                    # this state: a NEIGHBOURING change in a shared file
                    # re-keys the reviewed car's patch-id by moving its diff
                    # CONTEXT. This list is not claimed to be exhaustive —
                    # patch-id equality is a sufficient test for presence and
                    # never a necessary one, so a miss never proves absence.
                    # In the fixture at tests/test_gate.py (a 30-line shared
                    # file, reviewed edit at line 20) a neighbour 4 lines away
                    # kept the id and one 3 lines away re-keyed it; that is
                    # what those arms pin, not a general distance law.
                    "its ordered patch sequence is absent or reordered — or "
                    "was re-keyed by a neighbouring change in a shared file, "
                    "since a patch-id miss never proves absence",
                vcs.PATCH_SEQUENCE_EMPTY:
                    "the review's patch sequence is empty",
                vcs.PATCH_SEQUENCE_UNKNOWN:
                    "its ordered patch sequence cannot be derived",
            }.get(state, "its ordered patch sequence is unclassified")
            return "REFUSED", row["id"], (
                "receipt %s ran on train %s, which does not contain reviewed "
                "%s by ancestry, and %s. Re-run on the exact reviewed tip or "
                "a train carrying one unambiguous contiguous copy of its "
                "patches" % (row["id"], head[:12], tip[:12], detail))
        success = (
            "%s on train %s carrying reviewed %s as patches %d..%d of %d" %
            (interpreter_label(_ident_of(row)), head[:12], tip[:12], start + 1,
             start + reviewed_n, carrier_n))
    return "VERIFIED", row["id"], success


# ---------------------------------------------------------------- CLI

USAGE = ("usage: helm gate run [--focus [--plan]] [--label TEXT] [--timeout S] "
         "[--repo PATH] [--box NAME] [--json] [-- <argv>...]\n"
         "       helm gate equiv [--repo PATH] [--repeats N] [--no-timing]\n"
         "       helm gate show <id> [--json]\n"
         "       helm gate list [--limit N] [--json]\n"
         "       helm gate import <artifact.jsonl> [--repo PATH] "
         "[--id RECEIPT-ID]\n"
         "       helm gate fab contract|reconcile|detached ...\n"
         "       helm gate window launch [--repo PATH] [--label TEXT] "
         "[--trunk REF] [--supersede]\n"
         "       helm gate window show [--recover]\n"
         "       helm gate audits [--repo PATH] [--json] [-- <test module>...]")


def _fmt(row):
    # NO SECOND DIRT RENDERING. `evidence_line` carries the fact, on the line
    # that also travels into a verdict and back out of `gate show`. A trailing
    # flag appended here would read one half of the bracket and disagree with
    # that line the moment a suite dirties its own tree. One rendering, both
    # halves, three states each.
    when = _failure_text(row.get("ts"))[:19]
    return "%s  %s  %-7s %s" % (
        _failure_text(row.get("id")) or "?", when,
        _failure_text(row.get("status")) or "UNKNOWN",
        evidence_line(row))


def _show_base_check(row):
    """One verdict line, only for receipts that carry the field at all."""
    check = row.get("base_check")
    # v4 keeps the field, so a v4 receipt must still SHOW its base check. An
    # `== 3` here would silently blank the line for every future receipt —
    # the display-side twin of the id's growing-set rule above.
    if _version_has(row, "base_check") and check is not None:
        if not isinstance(check, dict):
            print("  %-10s UNREADABLE (stored base check is not a mapping)"
                  % "base")
            return
        verdict = (_failure_text(check.get("verdict")) or "?").replace(
            "_", " ")
        print("  %-10s %s — %s" % ("base", verdict,
                                   _failure_text(check.get("reason")) or "?"))


def _show_focus(row):
    """The recorded scope, rendered whole — this is the surface a reviewer
    challenges ('you omitted X's consumers'), so it must show the SET, never
    a summary of the set."""
    focus = row.get("focus")
    if focus is None:
        return
    if not isinstance(focus, dict):
        print("  %-10s UNREADABLE (stored focus is not a mapping)" % "focus")
        return
    changed = focus.get("changed")
    selected = focus.get("selected")
    executed = focus.get("executed")
    ran = executed if isinstance(executed, list) else []
    print("  %-10s %s  base %s" % (
        "focus", _failure_text(focus.get("policy")) or "?",
        (_failure_text(focus.get("base")) or "?")[:12]))
    for path in changed if isinstance(changed, list) else ():
        print("    changed   %s" % _failure_text(path))
    for mod in selected if isinstance(selected, list) else ():
        # THE GAP IS THE FINDING, so it is printed beside the member rather
        # than left for a reader to compute: a selected module the runner
        # never reported is the one row that turns a green focused receipt
        # into a refusal, and bind names it — this surface must not be the
        # place a reviewer cannot see it.
        print("    selected  %s%s" % (
            _failure_text(mod),
            "" if mod in ran else "  NEVER RAN"))
    for mod in ran:
        if not (isinstance(selected, list) and mod in selected):
            print("    ran       %s  (outside the recorded selection)"
                  % _failure_text(mod))
    universe = focus.get("universe")
    ids = focus.get("executed_ids")
    print("    %d/%s test modules ran (%s selected), %s test ids reported" % (
        len(ran), universe if type(universe) is int else "?",
        len(selected) if isinstance(selected, list) else "?",
        ids if type(ids) is int else "?"))


def trunk_standing(row, root=None):
    """Does this receipt's head still stand on live ground?

    -> {"state", "ref", "ref_sha", "reason"} — ONE dict, because the text and
    --json surfaces both read it and a second shape is how they drifted apart
    the first time. `ref_sha` is a str or None; `state` is a `vcs` constant.
    (This line once said `-> (state, reason)` after the shape changed — a
    stale contract, caught in review. A docstring that describes the previous
    return type is worse than none: it is checked by nobody and believed by
    readers.)

    A DIFFERENT QUESTION FROM `base_check`, DELIBERATELY, and the names are
    close enough that the distinction has to be written down. `_base_check`
    asks WHO OWNS THESE FAILURES — STALE_BASE / LANE_OWNED / NOT_STALE — and
    is inherently about failures, so a GREEN receipt has nothing for it to
    say and correctly carries none. Measured: of 1484 receipts,
    203 carry a base check; 635 v4 receipts carry none, and every green one
    is in that set. That is not a bug in `_base_check`. It is the gap
    task/387 names: **a green gate says nothing about whether the ground it
    stood on is still there**, and a receipt whose head is now unreachable
    reads exactly as well as one that is on trunk.

    NO SECOND CENSUS. `vcs.landed_state` already answers this in the only
    terms that survive a rebase — ANCESTOR / PATCH_EQUIVALENT /
    NOT_ANCESTOR / UNKNOWN — and PATCH_EQUIVALENT is the whole reason not to
    hand-roll it: almost nothing lands under the sha its author wrote, so an
    ancestry check would call correctly-landed work missing.

    THE ROOT IS THE LOCAL REPO, NEVER row["repo_id"], and this is measured
    rather than assumed: a remote-run receipt records the BUILD NODE's own
    worktree path, and that whole directory tree does not exist on the
    machine reading the receipt. Reading repo_id as a local path would fail
    on the majority of receipts, or worse, resolve to something else. The
    head sha IS present locally once it has been fetched, so the question we
    can honestly answer is "does this receipt's work stand on THIS repo's
    trunk", and when we cannot answer it we say so rather than guessing."""
    from . import vcs
    head = row.get("head")
    if not isinstance(head, str) or not head.strip():
        return {"state": vcs.UNKNOWN, "ref": None, "ref_sha": None,
                "reason": "this receipt records no head, so there is nothing "
                          "to locate against trunk"}
    head = head.strip()
    root = root or os.getcwd()
    try:
        backend = vcs.backend(root)
        trunk = backend.trunk_ref(root)
        # RESOLVE ONCE, THEN COMPARE AGAINST THE RESOLVED SHA — never against
        # the movable ref name (caught by a deterministic real-git probe).
        # The previous shape read TWICE: it resolved `trunk` to a sha for the
        # label, then asked landed_state about `trunk` itself. A ref that moved
        # between those two reads produced a verdict whose STATE was measured
        # on one snapshot while its LABEL named another — the projection said
        # ANCESTOR "on <old sha>" while merge-base against that old sha proved
        # otherwise.
        #
        # THIS IS THE THIRD INSTANCE OF ONE FAMILY IN THIS FUNCTION, and that
        # is why the cure is structural rather than another patch: the readback
        # that proved presence-at-an-instant, the snapshot that spoke for
        # trunk, and now a label naming a different read than the measurement.
        # All three are A LABEL THAT DOES NOT BIND WHAT WAS MEASURED. Passing
        # the resolved sha collapses the two reads into ONE, so there is no
        # longer a gap for a fourth instance to live in.
        at = backend.head_sha(root, trunk)
        state = backend.landed_state(root, head, str(at) if at else trunk)
    except Exception as exc:
        return {"state": vcs.UNKNOWN, "ref": None, "ref_sha": None,
                "reason": "the repository could not be read (%s), so this "
                          "receipt's standing is unmeasured — not clean"
                          % (exc.__class__.__name__,)}
    # NAME THE REF *AND* ITS SHA IN EVERY VERDICT. Not decoration — it is what
    # `trunk_ref`'s own contract prescribes: "STALENESS IS A THIRD WRONG
    # ANSWER THAT LOOKS RIGHT: a remote-tracking ref is itself a local
    # snapshot... Naming the ref (and its sha) in the verdict is what makes a
    # stale answer diagnosable instead of merely wrong."
    #
    # The wrong answer was measured at this exact tip: with a stale
    # origin/main, this returned NOT_ANCESTOR and said "never landed" about
    # work that WAS on trunk; after nothing but `git fetch origin main` the
    # identical call returned ANCESTOR. THE READING WAS NOT WRONG — the CLAIM
    # was. `origin/main` moves only on fetch, so every answer here is about a
    # SNAPSHOT, and prose asserting what trunk contains is a stronger claim
    # than the comparison supports. The same defect class as a true
    # observation wearing a bigger name.
    #
    # NO FETCH FROM A READ VERB. `gate show` must not reach the network — it
    # would hang behind an unreachable remote and mutate refs as a side effect
    # of displaying a row. So the boundary is DECLARED instead: the ref and the
    # sha it resolved to travel with the verdict, and the unlanded case names
    # the fetch rather than pronouncing on trunk.
    # str() COERCED, not assumed: this is a DISPLAY path and must not raise on
    # a backend that hands back something unexpected. Caught by a test whose
    # stub returned a Mock and made the slice throw — a receipt that cannot be
    # shown is strictly worse than one shown with an odd ref.
    where = "%s (%s)" % (trunk, str(at or "unresolved")[:12])
    reasons = {
        vcs.ANCESTOR: "its head is on %s by ancestry" % where,
        vcs.PATCH_EQUIVALENT:
            "its head is not on %s by ancestry, but the same CONTENT is — the "
            "normal shape after a rebase, and the state an ancestry check "
            "would call missing" % where,
        vcs.NOT_ANCESTOR:
            "neither its head nor its content is on %s AS THIS REPO LAST SAW "
            "IT. That ref is a remote-tracking SNAPSHOT and moves only on "
            "fetch, so a stale one reads exactly like unlanded work — run "
            "`git fetch` and re-read before treating this as never landed"
            % where,
        vcs.UNKNOWN:
            "the comparison could not be made against %s — the head may not "
            "have been fetched into this repo, and a remote-run receipt "
            "records the build node's own worktree path, which does not exist "
            "here" % where,
    }
    # The fallback is not decoration: it is what caught a real spelling bug
    # (see the constants note above), and a future fifth state must announce
    # itself rather than render blank.
    # ref_sha STRINGIFIED, so the defensive non-string contract above does not
    # leak a foreign type into --json. The display path already
    # coerced; the payload did not, so the two disagreed about what this field
    # is — the same split that made text and --json diverge in the first place.
    return {"state": state, "ref": trunk,
            "ref_sha": str(at) if at is not None else None,
            "reason": reasons.get(state, "unrecognised state %r — this reader "
                                         "needs updating" % (state,))}


def _show_trunk_standing(row, root=None):
    """One line, and it is printed for EVERY receipt including green ones —
    the absence of this line is what task/387 measured as the defect."""
    got = trunk_standing(row, root)
    print("  %-10s %s — %s" % ("standing",
                               got["state"].replace("-", " ").upper(),
                               got["reason"]))


def _show_failures(row):
    """Render stored diagnostics without making legacy absence read as none."""
    # v4 BINDS the failure identities exactly as v2 and v3 do — `not in (2, 3)`
    # sent every v4 receipt down the legacy branch and printed "predates
    # failure identities" about a receipt that carries them. THIRD instance of
    # this shape in this file: the id's two version tuples, _show_base_check,
    # and now this one. The reader-first split is the only reason it was caught
    # before shipping — no v4 receipt existed to render until the writer
    # flipped. test_every_version_gated_reader_admits_the_minted_version now
    # reads the REGISTRY, so the v8 multi-event shape cannot repeat it and
    # future writers have one place to extend.
    if not _version_has(row, "failure_identities"):
        print("  %-10s UNAVAILABLE (receipt predates failure identities)"
              % "failures")
        return
    if "failures" not in row or "failures_unreadable" not in row:
        print("  %-10s UNREADABLE (v2 diagnostics are absent)" % "failures")
        return
    failures = row.get("failures")
    if not isinstance(failures, list):
        print("  %-10s UNREADABLE (stored failures is not a list)" % "failures")
        return
    marker = next((item for item in failures if isinstance(item, dict)
                   and "truncated" in item), None)
    diagnostics = [item for item in failures if not (
        isinstance(item, dict) and "truncated" in item)]
    if _version_has(row, "failure_record"):
        total = row.get("failure_total")
        omitted = row.get("failure_diagnostics_omitted")
        if type(total) is not int or total < 0 or type(omitted) is not int \
                or omitted < 0 or omitted != total - len(diagnostics):
            print("  %-10s UNREADABLE (v8 failure totals disagree)" % "failures")
            return
    else:
        legacy_omitted = marker.get("truncated") if marker else 0
        legacy_omitted = legacy_omitted if type(legacy_omitted) is int \
            and legacy_omitted >= 0 else 0
        total = sum(isinstance(item, dict) and bool(item.get("test"))
                    for item in diagnostics) + legacy_omitted
        omitted = legacy_omitted + max(0, len(diagnostics) - FAILURE_CAP)
    if marker and not _version_has(row, "failure_record"):
        state = ("UNREADABLE" if row.get("failures_unreadable") else
                 "%d diagnostics recorded; %d additional identities "
                 "UNAVAILABLE/SKIPPED" % (len(diagnostics), legacy_omitted))
    else:
        state = "UNREADABLE" if row.get("failures_unreadable") else \
            ("none" if not total else "%d recorded" % total)
    print("  %-10s %s" % ("failures", state))
    for item in diagnostics[:FAILURE_CAP]:
        if not isinstance(item, dict) or not _failure_text(item.get("test")):
            print("    <unreadable entry>")
        else:
            print("    %s %s — %s" % (
                _failure_text(item.get("kind")) or "FAILURE",
                _failure_text(item.get("test")),
                _failure_text(item.get("traceback")) or
                "<traceback unreadable>"))
    if omitted:
        suffix = ("(%d identities recorded)" % total
                  if _version_has(row, "failure_record") else
                  "(their diagnostics and identities were not recorded)")
        print("    ... %d more failure diagnostic%s omitted %s" % (
            omitted, "" if omitted == 1 else "s", suffix))


def _timing_for_receipt(receipt_id):
    rows, unavailable = eventledger.checked_events(receipts_path(), strict=True)
    if unavailable:
        return None, unavailable
    timings, poisoned, _skipped = _timings(rows)
    if receipt_id in timings:
        return timings[receipt_id], None
    if receipt_id in poisoned:
        return None, "the receipt's module timing record is unreadable or conflicting"
    return None, None


def _show_module_timing(receipt_id):
    row, err = _timing_for_receipt(receipt_id)
    if err:
        print("  %-10s UNKNOWN (%s)" % ("timing", _failure_text(err)))
        return
    if row is None:
        print("  %-10s UNAVAILABLE (no module timing sibling is available)"
              % "timing")
        return
    skipped = row.get("skipped_by_class")
    if row["state"] != "COMPLETE":
        print("  %-10s UNKNOWN (%s)" % (
            "timing", _failure_text(row.get("reason"),
                                    gatetestrecord.TIMING_REASON_CAP)
            or "incomplete census"))
        print("    measured %d/%d modules, %d tests; module wall %.3fs; "
              "process CPU %.3fs" % (
                  row["measured_modules"], row["planned_modules"],
                  row["measured_tests"], row["module_wall"],
                  row["process_cpu"]))
        if skipped:
            print("    %s" % _failure_text(skipped))
        return
    print("  %-10s COMPLETE — %d modules, %d tests; runner %.3fs; "
          "module wall %.3fs; process CPU %.3fs; unattributed %.3fs" % (
              "timing", row["measured_modules"], row["measured_tests"],
              row["runner_elapsed"], row["module_wall"], row["process_cpu"],
              row["unattributed_wall"]))
    if skipped:
        print("    %s" % _failure_text(skipped))
    for item in row["top"]:
        print("    %8.3fs wall  %8.3fs cpu  %5d tests  %s" % (
            item["wall"], item["process_cpu"], item["tests"], item["module"]))


def _exit_code_will_be_discarded():
    """Is stdout a PIPE — i.e. will this process's exit code be thrown away?

    A shell pipeline takes the status of its LAST stage, so `helm gate run |
    tail` reports tail's success no matter what the gate did. The caller reads
    0 and believes the gate passed. MEASURED: two seats hit this
    independently and repeatedly, every time with the premise
    naming it live in their context — a refused gate exited 0 through the
    harness, and one of them announced "gating now" off that reading.

    A PIPE AND A REDIRECT ARE DISTINGUISHABLE, which is the whole reason this
    can be a guard instead of a rule:
        helm gate run > out 2>&1   -> S_ISREG   the HONEST form, silent
        helm gate run | tail       -> S_ISFIFO  the trap, warned
    A TTY is neither, and an operator reading a refusal on screen is not at
    risk — so this must key on ISFIFO and never on "not a regular file".

    Unreadable fd is not a pipe by default: this decides whether to print an
    ADVISORY, and a guard that cannot look must not manufacture noise."""
    import stat
    try:
        return stat.S_ISFIFO(os.fstat(1).st_mode)
    except (OSError, ValueError):
        return False


def refusal_exit(err, as_json, **fields):
    """Print a `gate run` that minted nothing, and return its exit. -> int

    ONE MAPPING for every arm of the verb, local and routed (`--box`): a
    CapacityRefusal is NOT RUN — EXIT_NOT_RUN_CAPACITY, and `not_run:
    "capacity"` beside the reason in --json — while every other refusal keeps
    exit 1. `fields` are the arm's own keys, printed between `minted` and
    `reason` as they always were."""
    capacity = isinstance(err, CapacityRefusal)
    if as_json:
        out = {"minted": False}
        out.update(fields)
        out["reason"] = err
        if capacity:
            out["not_run"] = CapacityRefusal.kind
        print(json.dumps(out, indent=1))
    else:
        print("helm gate: %s%s" % (
            "NOT RUN (capacity, exit %d) — " % EXIT_NOT_RUN_CAPACITY
            if capacity else "", err), file=sys.stderr)
    return EXIT_NOT_RUN_CAPACITY if capacity else 1


def cmd_gate(args):
    """The gate verb, plus the one thing the caller cannot see for themselves.

    WHY THE WARNING LIVES HERE AND NOT AT EACH `return`: cmd_gate has eleven
    exit points and every one of them can be discarded the same way. Guarding
    the boundary catches all of them and cannot drift out of sync with a
    twelfth; guarding each site is how a check ends up covering ten."""
    rc = _gate_dispatch(args)
    # BOTH CONDITIONS, so this is silent for a successful piped run (nothing
    # was lost) and for an unpiped failure (the caller can see it). It fires
    # only where a real nonzero is about to become an invisible zero.
    if rc and _exit_code_will_be_discarded():
        print("helm gate: NOTE — this command exited %d, but stdout is a PIPE, "
              "so your shell will report the LAST stage's status instead "
              "(`| tail` always succeeds). Redirect if you are checking it: "
              "`helm gate run > out 2>&1; rc=$?`" % rc, file=sys.stderr)
    return rc


def _gate_dispatch(args):
    args = list(args or ())
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "run":
        return _cmd_run(rest)
    if sub == "equiv":
        from . import gateequiv
        return gateequiv.main(rest)
    if sub == "show":
        return _cmd_show(rest)
    if sub == "list":
        return _cmd_list(rest)
    if sub == "import":
        from . import gateimport
        return gateimport.cmd_import(rest)
    if sub == "fab":
        from . import fabgate
        return fabgate.cmd(rest)
    if sub == "window":
        from . import gatewindow
        return gatewindow.cmd(rest)
    if sub == "audits":
        from . import gateaudits
        return gateaudits.cmd(rest)
    print(USAGE, file=sys.stderr)
    return 2


def _opt(rest, name, default=None):
    if name in rest:
        i = rest.index(name)
        if i + 1 < len(rest):
            return rest[i + 1]
    return default


def _cmd_run(rest):
    rest = list(rest or ())
    cut = rest.index("--") if "--" in rest else len(rest)
    opts = rest[:cut]
    from .cli import guard_tail
    rc = guard_tail("helm gate run", opts, flags=("--json", "--focus",
                                                  "--plan"),
                    valued=("--label", "--timeout", "--repo", "--box"),
                    usage=USAGE)
    if rc is not None:
        return rc
    argv = rest[cut + 1:] if cut < len(rest) else None
    if cut < len(rest) and not argv:
        print("helm gate run: `--` needs a command after it", file=sys.stderr)
        return 2
    focus = "--focus" in opts
    if focus and argv:
        print("helm gate run: --focus composes its own command; drop the "
              "`--` argv", file=sys.stderr)
        return 2
    timeout = _opt(opts, "--timeout")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except ValueError:
            print("helm gate run: --timeout wants a number, got %r" % timeout,
                  file=sys.stderr)
            return 2
        if not math.isfinite(timeout):
            print("helm gate run: --timeout wants a finite number, got %r"
                  % _opt(opts, "--timeout"), file=sys.stderr)
            return 2
    as_json = "--json" in opts
    box = _opt(opts, "--box")
    if box is not None:
        if "--plan" in opts:
            # `--plan` NO LONGER IMPLIES `--focus`: it now also answers "which
            # command would run here", so the sentence must not name a flag the
            # caller may not have typed.
            print("helm gate run: --plan runs no tests and stays local; "
                  "drop --box", file=sys.stderr)
            return 2
        # The job-routing arm (#225): a whole or focused gate runs on a
        # consented inventory box and its receipt rides home through the strict
        # transport-owned import path. A
        # custom `--` command records interpreter UNKNOWN and can never bind,
        # so routing one would spend a remote box on an unbindable claim.
        if argv:
            print("helm gate run: --box routes the whole suite; a `--` "
                  "command cannot bind a verdict and does not route",
                  file=sys.stderr)
            return 2
        from . import gateroute
        return gateroute.cmd_route(box, repo=_opt(opts, "--repo"),
                                   label=_opt(opts, "--label"),
                                   timeout=timeout, as_json=as_json,
                                   focus=focus)
    if "--plan" in opts and not focus:
        # THE WHOLE-SUITE PLAN: which command would run here, and where it came
        # from. Runs nothing and mints nothing, so it is the question anything
        # OUTSIDE helm asks before spending a box — a remote gate runner that
        # decides for itself whether a tree is gateable is a second copy of this
        # policy, and the copy that exists today (`[ -e $TOP/helm/gate.py ]`)
        # refuses every adopter project helm now accepts.
        plan, plan_err = suite_command(
            os.path.realpath(_opt(opts, "--repo") or os.getcwd()))
        if plan_err:
            if as_json:
                print(json.dumps({"plan": None, "reason": plan_err}, indent=1))
            else:
                print("helm gate: " + plan_err, file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps({"plan": plan}, ensure_ascii=False, indent=1))
            return 0
        print("suite command  %s" % " ".join(plan["argv"]))
        print("  source       %s%s" % (
            plan["source"],
            " (project %s)" % plan["project"] if plan["project"] else ""))
        print("  protocol     %s" % plan["protocol"])
        print("  cwd          %s" % os.path.realpath(
            _opt(opts, "--repo") or os.getcwd()))
        return 0
    if "--plan" in opts:
        # THE REVIEWER'S CHALLENGE SURFACE: re-derive the selection with the
        # same policy, print it, run nothing, mint nothing. A reviewer who
        # suspects an omitted consumer diffs this against the receipt's
        # recorded scope instead of arguing about colour.
        plan, plan_err = focus_plan(
            os.path.realpath(_opt(opts, "--repo") or os.getcwd()))
        if plan_err:
            if as_json:
                print(json.dumps({"plan": None, "reason": plan_err},
                                 indent=1))
                return 1
            print("helm gate: " + plan_err, file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps({"plan": plan}, ensure_ascii=False, indent=1))
            return 0
        print("focus policy %s  base %s" % (plan["policy"],
                                            plan["base"][:12]))
        for path in plan["changed"]:
            print("  changed   %s" % path)
        for mod in plan["selected"]:
            print("  selected  %s" % mod)
        print("  %d/%d test modules" % (len(plan["selected"]),
                                        plan["universe"]))
        return 0
    row, err = run(repo=_opt(opts, "--repo"), argv=argv,
                   label=_opt(opts, "--label"), timeout=timeout, focus=focus)
    if err:
        return refusal_exit(err, as_json)
    # The bind feedback asks the question this run can actually answer: a
    # focused mint is judged at the focused bar (with its own repo standing
    # in for the dispatch repository), a suite mint at the suite bar. Asking
    # a focused receipt the suite question here would exit 1 on every green
    # focused run — the verb refusing its own purpose.
    bind_kw = {"need": NEED_FOCUSED, "repo_id": row.get("repo_id")} \
        if focus else {}
    # ONE BIND, READ TWICE — and the second reading is this variable, never a
    # second call. `bind` is not a pure function of its arguments: it resolves
    # a token against the LEDGER and, on the descendant arm, asks the standing
    # repository about refs. Both can move between two calls a few
    # milliseconds apart (a concurrent mint compacting the ledger, a rebase
    # landing under the run), and asking twice made the printed state and the
    # exit code two independent answers — a verb that prints VERIFIED and
    # exits 1, or worse prints a refusal and exits 0. That is the very
    # measure-twice-decide-twice shape this lane exists to close at the
    # receipt layer, so it may not stand in the verb that mints them.
    state, _rid, why = bind(evidence_line(row), row.get("head") or "",
                            **bind_kw)
    if as_json:
        print(json.dumps({"minted": True, "receipt": row,
                          "evidence": evidence_line(row)},
                         ensure_ascii=False, indent=1))
    else:
        print(evidence_line(row))
        if state != "VERIFIED":
            print("helm gate: %s — %s" % (state, why), file=sys.stderr)
        check = row.get("base_check")
        if isinstance(check, dict) and check.get("verdict") != LANE_OWNED:
            # LANE_OWNED is an ordinary FAILED and says nothing extra; every
            # other verdict — STALE BASE, NOT STALE, UNKNOWN — is information
            # the author would otherwise spend an investigation re-deriving.
            print("helm gate: base check %s — %s" % (
                (_failure_text(check.get("verdict")) or "?").replace("_", " "),
                _failure_text(check.get("reason")) or "?"), file=sys.stderr)
    # The exit status is THE bind above, not a second opinion about it and no
    # longer a second call to it: a run this verb calls green while the
    # binding would refuse it is exactly the gap the verb exists to close, and
    # two calls could disagree about which of those happened.
    return 0 if state == "VERIFIED" else 1


SHOW_USAGE = "gate show <id> [--json]"
LIST_USAGE = "gate list [--limit N] [--json]"


def _cmd_show(rest):
    rest = list(rest or ())
    want = rest.pop(0) if rest and not rest[0].startswith("-") else None
    from .cli import guard_tail
    rc = guard_tail("helm gate show", rest, flags=("--json",), usage=SHOW_USAGE)
    if rc is not None:
        return rc
    if want is None:
        print(USAGE, file=sys.stderr)
        return 2
    row, err = by_id(want)
    if err:
        print("helm gate: " + err, file=sys.stderr)
        return 1
    if "--json" in rest:
        # THE SAME PROJECTION THE TEXT SURFACE SHOWS (the two surfaces
        # had split, text carrying `standing` and --json returning only the
        # stored row). A tool reading --json is exactly the caller that would
        # act on a receipt, so withholding the standing from it and printing
        # it for the human is backwards.
        #
        # UNDER ITS OWN KEY, and marked, because it is a PROJECTION and not
        # part of the stored receipt: `standing` is computed against this
        # repo's trunk ref AT READ TIME and will differ between machines and
        # across a fetch. Merging it into the row would make a derived value
        # indistinguishable from recorded evidence, which is the whole
        # distinction the receipt exists to keep.
        out = dict(row)
        out["standing_projection"] = trunk_standing(row)
        timing, timing_err = _timing_for_receipt(row["id"])
        out["module_timing"] = timing if timing is not None else {
            "state": "UNKNOWN" if timing_err else "UNAVAILABLE",
            "reason": timing_err or "no module timing sibling is available"}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for key in ("id", "ts", "status", "ran", "skipped", "detail", "head",
                "tree", "dirty", "repo_id", "label", "rc", "wall", "elapsed"):
        if row.get(key) is not None:
            print("  %-10s %s" % (key, _failure_text(row[key])))
    _show_failures(row)
    _show_base_check(row)
    _show_focus(row)
    _show_module_timing(row["id"])
    _show_trunk_standing(row)
    print("  %-10s %s" % ("interpreter", interpreter_label(_ident_of(row))))
    ident = _host_of(row)
    print("  %-10s %s%s" % (
        "host", host_label(ident),
        " (%s %s, machine %s)" % (
            _failure_text(ident.get("system")) or "?",
            _failure_text(ident.get("release")) or "?",
            _failure_text(ident.get("id")) or "unnamed")
        if ident.get("node") else
        " — this receipt predates host recording and cannot bind"))
    argv = row.get("argv")
    argv = " ".join(_failure_text(arg) for arg in argv) \
        if isinstance(argv, (list, tuple)) else "<unreadable>"
    print("  %-10s %s" % ("argv", argv))
    print("\n  " + evidence_line(row))
    return 0


def _cmd_list(rest):
    rest = list(rest or ())
    from .cli import guard_tail
    rc = guard_tail("helm gate list", rest, flags=("--json",),
                    valued=("--limit",), usage=LIST_USAGE)
    if rc is not None:
        return rc
    # A NON-NUMERIC --limit used to raise ValueError straight out of the verb.
    # Parse it before reading the ledger: malformed input must not act first or
    # replace its own refusal with an unrelated availability/skipped-row report.
    raw = _opt(rest, "--limit", "20")
    try:
        limit = int(raw)
    except ValueError:
        print("helm gate list: --limit wants a number, got %r" % raw,
              file=sys.stderr)
        return 2
    # TERMINAL EVIDENCE OUTRANKS RECEIPT AVAILABILITY. A broken receipt ledger
    # is the exact moment an orphan with no receipt must not disappear behind an
    # early return; the queue outbox is independent and can still say UNKNOWN.
    _print_queue_orphans()
    rows, unavailable, skipped = receipts()
    if unavailable:
        print("helm gate: receipt ledger unavailable: %s" % unavailable,
              file=sys.stderr)
        return 1
    if skipped:
        # Dropping a row silently is how a ledger reports zero and gets
        # believed. The count is small and the surface is the right place.
        #
        # AND THE REASON MUST BE THE RIGHT ONE. A row written by a NEWER helm
        # fails the same recompute as a corrupted one, and reporting both as
        # "did not match their own content" sends the reader hunting damage
        # that is not there. Split the count; name the versions.
        newer = unreadable_versions() or {}
        unknown = sum(newer.values())
        if unknown:
            print("helm gate: %d ledger row%s carr%s a receipt version this "
                  "helm cannot read (%s) — NOT corruption: written by a newer "
                  "helm, and this one knows %s. Update helm to read %s; "
                  "nothing here is damaged."
                  % (unknown, "" if unknown == 1 else "s",
                     "ies" if unknown == 1 else "y",
                     ", ".join("v%s" % v for v in sorted(
                         newer, key=lambda x: (x is None, x))),
                     ", ".join("v%d" % v for v in _known_versions()),
                     "it" if unknown == 1 else "them"),
                  file=sys.stderr)
        if skipped - unknown:
            rest_n = skipped - unknown
            print("helm gate: %d ledger row%s did not match %s own content and "
                  "%s skipped" % (rest_n, "" if rest_n == 1 else "s",
                                  "its" if rest_n == 1 else "their",
                                  "was" if rest_n == 1 else "were"),
                  file=sys.stderr)
    rows = rows[-limit:] if limit > 0 else []
    if "--json" in rest:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    # THE SWEEP RUNS BEFORE THE EMPTY-LEDGER EXIT, because an orphan with NO
    # receipts is the whole case: a launcher that died before minting leaves
    # nothing in this ledger, and returning early there is how the surface the
    # orphan notices point at answers "nothing to see" about the very death
    # they are describing.
    if not rows:
        print("helm gate: no minted receipts yet — `helm gate run`")
        return 0
    for row in rows:
        print(_fmt(row))
    return 0


def _print_queue_orphans(repo=None):
    """Announce queue rows whose launcher is gone. Never raises, never blocks.

    task/407, gate-waiter-orphans-with-no-receipt: every existing
    publisher of a GATE ORPHAN notice sits inside a queue OPERATION, so a
    launcher that dies while nothing else is gating is announced to nobody.
    `helm gate list` is the surface every orphan notice already points at, so
    a reader who follows that advice finds the orphan instead of an empty
    ledger.

    IT NEVER SIGNALS A PROCESS OR DELETES EVIDENCE. It atomically moves rows
    whose exact process generations are already gone into the durable terminal
    outbox before removing them from the active FIFO. That is the safe backfill:
    the queue can advance without turning an old orphan into a vanished one.
    """
    try:
        from . import seats_gate_queue
        orphans, err = seats_gate_queue.gate_queue_recover_orphans(
            repo or os.getcwd())
    except Exception as exc:                                  # noqa: BLE001
        # A DIAGNOSTIC MUST NOT WEDGE THE VERB IT DECORATES, and it must not
        # go quiet either: an unreadable queue is UNKNOWN, never zero.
        print("helm gate: queue orphan sweep could not run (%s: %s) — orphan "
              "state is UNKNOWN, not clear" % (exc.__class__.__name__, exc),
              file=sys.stderr)
        return
    if err:
        print("helm gate: queue orphan sweep unavailable: %s — orphan state "
              "is UNKNOWN, not clear" % err, file=sys.stderr)
        return
    for row in orphans or ():
        diagnostic = _diagnostic_of(row)
        if diagnostic.get("kind") == "finish":
            announced = row.get("announced") or "UNKNOWN"
            print("helm gate: PENDING TERMINAL #%s @%s diagnostic=%s stage=%s "
                  "reason=%s — GATE FINISH %s%s was persisted before chat "
                  "delivery and has not been acknowledged" % (
                      row.get("seq"), row.get("holder"),
                      diagnostic.get("status") or "UNKNOWN",
                      diagnostic.get("stage") or "UNKNOWN",
                      diagnostic.get("reason") or "unknown", announced,
                      " — NEXT @%s" % row["next_holder"]
                      if row.get("next_holder") else ""), file=sys.stderr)
            continue
        print("helm gate: " + _orphan_line(
            row, "ORPHANED QUEUE ROW",
            " Its terminal diagnostic is durable and awaits successful chat "
            "publication."), file=sys.stderr)
