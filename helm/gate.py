#!/usr/bin/env python3
"""helm gate — a suite result becomes a CLAIM only by being MINTED here.

WHY THIS EXISTS. Until this module a gate verdict entered helm as PROSE. You
typed `helm dispatch verdict <id> <tip> "whole-suite Ran 5115 OK"` and the only
thing any code did to that string was check its LENGTH (dispatches._clean, 256
characters). An integrator reading it could not learn which interpreter
produced it, which tree it ran against, or whether it was run at all.

That is not hypothetical. On 2026-07-30 this box measured two seats reporting
HONEST, CONTRADICTORY results on the same tree twenty minutes apart, because
`python` here is GraalPy 3.12.8 and `python3` is CPython 3.14.4, and one HANGS
forever where the other returns OK (premise
a-suite-result-is-not-a-claim-without-its-interpreter). Neither seat was flaky
and neither was lying. The protocol delta — state your interpreter — was
adopted SOCIALLY the same day and enforced NOWHERE: a scan for
sys.implementation / python_implementation / CPython / GraalPy across helm/ and
docs/ returned zero hits before this file.

WHY NOT JUST VALIDATE THE STRING. The obvious fix is to make the verdict verb
require an evidence line MATCHING a pattern that names an interpreter. That is
a check on SPELLING. It passes for anyone who types the token without running
anything, which is precisely the failure the 0.2 council named as helm's single
finding — a guard sound about a narrower neighbour of the question being asked.
The same day, the round-7 consumer census on lane/resolve-matches-session
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
import collections
import hashlib
import json
import math
import os
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

from . import chat, eventledger, gatechild, home, pk, seats, vcs

RECEIPTS = "gate-receipts.jsonl"

# The whole tree, as unittest discovers it. `-t .` keeps the import root at the
# repo so `helm.*` resolves the checkout under test rather than an installed
# copy — the same reason bin/helm resolves the package relative to itself.
SUITE = ("-m", "unittest", "discover", "-s", "tests", "-t", ".")

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


def receipts_path():
    return os.path.join(home.global_dir(), RECEIPTS)


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
    """The short human form: `host-a`. UNKNOWN when the receipt does not name
    its machine — absence is SAID, never rendered as blank space a reader
    fills in with the box they happen to be sitting at."""
    if not isinstance(ident, dict):
        return "UNKNOWN"
    return _failure_text(ident.get("node"))[:_LABEL_CAP] or "UNKNOWN"


def _suite_env():
    """A deterministic unittest rendering environment, never inherited color."""
    env = dict(os.environ)
    env["PYTHON_COLORS"] = "0"
    env["NO_COLOR"] = "1"
    env.pop("FORCE_COLOR", None)
    return env


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


def _failure_text(value):
    """One bounded printable line; truncation is visible, never silent."""
    text = " ".join(str(value or "").split())
    if len(text) <= _FAILURE_TEXT_CAP:
        return text
    return text[:_FAILURE_TEXT_CAP - 3] + "..."


def _first_traceback_line(block):
    """First real frame from a normal or ExceptionGroup traceback."""
    lines = str(block or "").splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"(?:Exception Group )?Traceback "
                         r"\(most recent call last\):$", line.strip()):
            continue
        for candidate in lines[i + 1:]:
            candidate = candidate.strip().lstrip("| ").strip()
            if candidate and any(ch.isalnum() for ch in candidate):
                return _failure_text(candidate)
        return None
    return None


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


def _failure_identity(header):
    """Canonical id for a default-discovery unittest failure header."""
    hit = _FAILURE_HEADER.fullmatch(str(header or ""))
    if not hit:
        return None
    name, test_id = hit.group("name"), hit.group("test")
    if "." not in name and (name.startswith("test") or name == "runTest") \
            and test_id.endswith("." + name):
        return test_id
    if name in _FIXTURE_NAMES:
        return test_id + "." + name
    if "._FailedTest." in test_id and test_id.endswith("." + name):
        return test_id
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
    """(bounded entries, unreadable) from unittest's failure blocks.

    A FAILED summary is complete only when its failures/errors counts equal the
    headers we parsed and every header yields both an id and a traceback frame.
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
    # prevents earlier forged-looking logs from consuming the cap and hiding the
    # real failing test.
    selected = protocol[-expected_total:] if expected_total \
        and len(protocol) >= expected_total else protocol
    entries, counts, complete = [], {"FAIL": 0, "ERROR": 0}, True
    for index, hit, test_id in selected:
        kind = hit.group("kind")
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        traceback = _first_traceback_line(text[hit.end():end])
        counts[kind] += 1
        complete = complete and bool(test_id and traceback)
        if test_id:
            entries.append({"kind": kind, "test": _failure_text(test_id),
                            "traceback": traceback})
    if status == "OK":
        unreadable = bool(protocol)
    elif status == "FAILED":
        unreadable = not expected or len(protocol) != expected_total \
            or counts != expected or not complete
    else:
        unreadable = True
    if len(entries) > FAILURE_CAP:
        omitted = len(entries) - FAILURE_CAP
        entries = entries[:FAILURE_CAP]
        entries.append({"truncated": omitted})
    return entries, unreadable


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
    failures, unreadable = _parse_failures(text, reported, detail)
    out["failures"], out["failures_unreadable"] = failures, \
        unreadable or not footer_ok
    if out["status"] == "OK" and failures:
        out["status"] = "UNKNOWN"
        out["detail"] = "the summary says OK but failure blocks were present"
        out["failures_unreadable"] = True
    return out


# ------------------------------------------------------------- stale base
#
# On 2026-07-31 a THREE LINE lane came back FAILED on
# test_tests_carry_no_private_fixture_labels — a test file the lane never
# touched. @kimi re-ran the gate serialized on an identical tree (same failure,
# so not a race), ran the same test on current trunk (passed, so not the lane),
# and concluded the lane's Jul-29 base simply predates a suite that trunk has
# since fixed. Third lane that night whose blocker was a stale base, and each
# one cost a full human-grade investigation, because the receipt HAD every
# input to that investigation and said only "FAILED". That is the bug class:
# a surface that holds the deciding information and does not say it.
#
# So a FAILED suite receipt now takes kimi's measurements itself and records a
# verdict about WHOSE failure this is:
#
#   STALE_BASE   every failing test file is outside the lane's own diff
#                (against ITS merge-base with trunk — `git diff trunk lane`
#                lists every file trunk touched since the fork and once read
#                "68 of 68 lanes collide"), the same tests PASS on current
#                trunk, STAY green with the lane's own changes applied on top
#                of that trunk, and the same failures REPRODUCE at the
#                merge-base without the lane's changes — actually run, never
#                inferred. The lane-on-trunk leg is the CAUSAL one, and it
#                exists because @codex-3 proved (2026-08-01) the first three
#                were not: let test T fail for TWO reasons, cause A in the old
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
#                investigation lands here too (2026-08-03, below).
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
    return {"status": parsed["status"], "rc": p.returncode, "failed": failed,
            "unreadable": parsed["failures_unreadable"]}, None


_ABSENT_TEST = re.compile(r"\bunittest\.loader\._FailedTest\.(?P<name>.+)$")


def _absent_id(name):
    """True when `name` is unittest's marker for an id that DID NOT LOAD.

    `unittest.loader._FailedTest.<x>` is minted for two different things and
    only one of them is a stale inventory. When <x> is a DOTTED id
    (`tests.test_probe.StaleProbe.test_probe`) the loader was asked for that
    test and could not resolve it — the names exist in the peek's tree and not
    here. When <x> is a bare token (`StaleProbe`) it names no id at all: the
    run broke on something we never asked about, which is no evidence either
    way. Same marker, opposite diagnoses; the dot is what separates them."""
    m = _ABSENT_TEST.search(str(name or ""))
    return bool(m) and "." in m.group("name")


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


def _base_check(repo, failures, failures_unreadable):
    """kimi's measurements plus the causal one, taken by the process that
    already holds the data. -> a dict with `verdict` in {STALE_BASE,
    LANE_OWNED, NOT_STALE, UNKNOWN} and `reason` in words, always.

    STALE_BASE requires ALL of: (a) every failing test file outside the lane's
    diff against its own merge-base with trunk (committed + uncommitted +
    untracked); (b) the same tests pass on current trunk, actually run in a
    detached checkout; (b') the same tests STAY green with the lane's whole
    working state applied on top of that trunk — the causal leg, added after
    @codex-3's dual-cause finding (2026-08-01): a test failing for cause A in
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
    reads the run's exit status instead answers a question nobody asked, and
    on 2026-08-03 the trunk leg did exactly that to lane/gemini-window-750k."""
    if failures_unreadable:
        return _base_unknown(
            "the failure identities are unreadable, so no failure can be "
            "shown to lie outside this lane")
    ids = set()
    for entry in failures or ():
        if not isinstance(entry, dict):
            return _base_unknown("a recorded failure entry is unreadable")
        if "truncated" in entry:
            return _base_unknown(
                "%s failure%s beyond the %d-entry cap were never identified "
                "and were SKIPPED — cannot show every failure lies outside "
                "this lane" % (entry["truncated"],
                               "" if entry["truncated"] == 1 else "s",
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
    base = {"trunk": trunk, "trunk_ref": trunk_name, "merge_base": mb,
            "failing_files": sorted(set(files.values()))}
    on_trunk, err = _reference_run(repo, git, trunk, ids)
    if err:
        return _base_unknown("trunk leg: " + err, **base)
    if not (on_trunk["status"] == "OK" and on_trunk["rc"] == 0):
        # A FAILED trunk run is NOT, by itself, evidence that THESE failures
        # are trunk's. Until 2026-08-03 this branch inferred exactly that from
        # the exit status alone — it never looked at WHICH ids failed there —
        # and told lane/gemini-window-750k "the failing tests ALSO fail on
        # current trunk <tip>" (fold: restart-restores-the-wake-path).
        # The author then ran two of those four at that tip by hand and both
        # PASSED: the trunk run had failed on a DIFFERENT id, because a
        # `_FailedTest` for a name that does not resolve at that sha ends the
        # run non-zero while the failures under investigation never even ran.
        # (The lane tip is deliberately not cited — it was rebased within the
        # hour, and a citation to an unlanded lane is dead on arrival.)
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
            if named and all(_absent_id(n) for n in named):
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
        return _base_unknown(
            "the trunk reference run on %s (%s) read %s (rc %s) — neither a "
            "pass nor a readable failure, so it is no evidence that trunk "
            "owns these failures and whose they are could not be determined "
            "yet" % (trunk[:12], trunk_name, on_trunk["status"],
                     on_trunk["rc"]), **base)
    if mb == trunk:
        return dict(base, verdict=NOT_STALE, reason=_failure_text(
            "the lane is based on current trunk %s and the failing tests "
            "pass there — the failures need this lane's changes to appear"
            % trunk[:12]))
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
    # measured 2026-08-06, a clean lane read DIRTY off the marker alone.
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
        # codex-2's 166 r1: the synthetic-index dir leaked one tmpdir per
        # call — 65 on an inode-capped tmpfs in one review session.
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
        return dict(base, verdict=NOT_STALE, reason=_failure_text(
            "the failing tests pass at the lane's own merge-base %s — the "
            "failures appear only WITH this lane's changes, so the lane is "
            "implicated even though it never touched the test files and the "
            "lane applied to current trunk ran green (a failure never "
            "reproduced without the lane is not the base's to own)"
            % mb[:12]))
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
        # no id reproduced — the names did not resolve at the merge-base
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
    raises. @codex reached the whole module that way — one ledger line reading
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


def _receipt_id(row):
    """Content identity over every field a reader would rely on. The timestamp
    is inside it so two honest runs of the same tree stay separately
    addressable — this is a handle, never a deduplication key."""
    ident = _ident_of(row)
    parts = [row.get("ts"), row.get("head"), row.get("tree"), row.get("dirty")]
    bracket = tuple(key in row for key in
                    ("head_after", "tree_after", "dirty_after"))
    if row.get("v") == 1 and any(bracket) and not all(bracket):
        raise ValueError("partial v1 post-run bracket")
    if row.get("v") != 1 or all(bracket):
        parts.extend((row.get("head_after"), row.get("tree_after"),
                      row.get("dirty_after")))
    parts.extend((ident.get("name"), ident.get("language"),
                  ident.get("executable"), " ".join(row.get("argv") or ()),
                  row.get("status"), row.get("ran"), row.get("skipped")))
    # Original v1 receipts predate the post-run bracket and did not bind rc.
    # Bracketed v1 receipts bind it; v2 retains that grammar and additionally
    # binds both failure-diagnostic fields.
    if row.get("v") != 1 or all(bracket):
        parts.append(row.get("rc"))
    parts.append(row.get("repo_id"))
    # EACH VERSION KEEPS EVERY EARLIER VERSION'S BINDINGS. Spelling these as
    # `== 2` / `== 3` makes the id a per-version SET rather than a growing one,
    # so a v4 bump would silently stop binding the failure list and the
    # base-check verdict — a version bump that WEAKENS the hash is the exact
    # shape of defect this file exists to refuse.
    if row.get("v") in (2, 3, 4):
        parts.extend(("failure-identities-v2", json.dumps(
            row.get("failures"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")), row.get("failures_unreadable")))
    if row.get("v") in (3, 4):
        # The base-check verdict is a field a reader RELIES on — an edited
        # "STALE_BASE" pasted into a lane-owned receipt must stop resolving.
        parts.extend(("base-check-v3", json.dumps(
            row.get("base_check"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))))
    if row.get("v") == 4:
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
        # 2026-08-04 on receipt 756b936006bf0e47: 685 rows read, skipped 1, and
        # the reviewer traced three functions to find out why their APPROVE
        # could not bind. Teach every reader first; flip the writer after.
        host_ident = _host_of(row)
        parts.extend(("host-v4", host_ident.get("node"),
                      host_ident.get("system"), host_ident.get("release"),
                      host_ident.get("id")))
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


def evidence_line(row):
    """The one canonical string a reviewer pastes into `dispatch verdict`.

    kimi's adopted shape, with the binding token first: everything after the
    token is for a human to read, and nothing after it is trusted."""
    ident = _ident_of(row)
    ran = row.get("ran")
    ran = ran if type(ran) is int and ran >= 0 else "?"
    counts = "Ran %s" % ran
    skipped = row.get("skipped")
    if type(skipped) is int and skipped > 0:
        counts += " (skipped=%d)" % skipped
    scope = "whole-suite" if row.get("suite") else "custom"
    rid = _failure_text(row.get("id")) or "?"
    tree = (_failure_text(row.get("tree")) or "?")[:12]
    status = _failure_text(row.get("status")) or "UNKNOWN"
    line = "gate:%s | %s | host=%s | tree=%s | room=%s | %s | %s %s" % (
        rid, interpreter_label(ident), host_label(_host_of(row)), tree,
        receipt_room(row)[:_LABEL_CAP] or "?", scope, counts, status)
    # The deciding words ride the SAME line a reviewer reads — the 2026-07-31
    # incident was a receipt that held the answer and said only FAILED. A
    # lane-owned failure adds nothing: FAILED already means exactly that.
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
# At 03:44:48 on 2026-08-03 the orca PTY daemon on this box died of memory
# starvation and every agent pane died within two seconds, under SIX
# concurrent whole-suite runs. The cap was ruled TWO and FLEET-WIDE; the
# fleet-wide half was superseded on 2026-08-05 (see `suite_cap` below — the
# TWO stands, for boxes that carry panes) — and no lock
# can carry it: `helm gate run` claims a FIFO position and the legacy
# gatelock, but a bare `python3 -m unittest discover` touches neither, so a
# cap counted over lock-holders is a cap that does not exist. Admission
# therefore counts PROCESSES ACTUALLY RUNNING A SUITE — what a process IS
# (its argv, read off /proc), never what it holds. A bare run can still
# START outside helm's reach, but it cannot be invisible: every suite on the
# box occupies a slot, and the gate refuses to pile on top of it.

SUITE_CAP = 2

# ---------------------------------------------------------- per-host cap
#
# THE CAP ABOVE WAS RULED "FLEET-WIDE" AND THAT WAS THE MISTAKE (owner ruling
# 2026-08-05: make gate concurrency PER-HOST, not global). The resource it
# protects — memory, cores, the PTY daemon — is per-BOX, so one number for
# every box necessarily fits none of them. Since 2026-08-03 `helm gate` suites
# route to the fab (`fab gate`; this box is agents-only), so the cap that
# actually bites now bites on a build host that has no panes to lose, while
# being justified by an outage that killed panes on this one.
#
# MEASURED 2026-08-05, whole suite on the 32-core build host under /usr/bin/time -v:
#
#     Maximum resident set size   123056 KB  = 120 MB
#     Percent of CPU this job got 26%        = I/O-bound, not CPU-bound
#     build-host headroom         32 cores, 179 GB available
#
# 120 MB per run against 179 GB is a memory argument off by three orders of
# magnitude. It also reframes the outage above: SIX runs is ~720 MB of RSS,
# which cannot starve an 87 GB box. Whatever killed the daemon that night, the
# resident memory of six suites was not it — page-cache churn from thousands
# of temp git repos (not RSS), I/O contention, and process-count thrash on a
# box already carrying 46 agent processes are the live candidates. (`time -v`
# reports max RSS across the tree it WAITS on, so daemons a test spawns and
# abandons are not in that 120 MB: treat it as a floor, not a ceiling. It
# would have to be ~100x low to justify a cap of two on a build host.)
#
# SO THE COUNT IS NOT DERIVED FROM RAM, BECAUSE RAM IS NOT THE BINDING
# RESOURCE. That is the answer rather than a gap: the comment below already
# says the count is a PROXY and that PSI is the real, kernel-measured guard.
# This raises the proxy on boxes with nothing to lose and leaves PSI — and the
# pane-host cap of two — exactly as protective as they are today.
#
# THE PREDICATE IS "DOES THIS BOX CARRY PANES", NOT A HOSTNAME. A hostname
# allowlist rots the moment a host is added or renamed, and it encodes the
# wrong fact: what made the outage expensive was never the box's NAME, it
# was that irreplaceable agent context died with the daemon. A
# build host running nothing but suites has no such casualty.

BUILD_HOST_SUITE_CAP_MAX = 8
# Deliberately conservative: cores//4 leaves 75% of the box idle, and the
# workload measured I/O-bound, where added concurrency yields less than it
# costs in contention. Raise it on a measurement of real concurrency, not on
# the fact that it looks low next to 32 cores.
_CORES_PER_SUITE = 4


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
        the two live hosts read 434 and 487 "panes", so neither ever left cap 2);
      * an inherited stamp counted CHILDREN as panes, inflating this box from
        its true ~13 agents to 47.

    `beacons.is_agent(argv, comm)` decides on argv/comm — which stay readable
    when environ does not — and `agent_index` deliberately keeps a pane whose
    environ it cannot read rather than dropping it. So the case @codex-3 found
    is handled at the layer that owns it, by evidence that does not disappear.

    THE FAIL-CLOSED ARM SURVIVES AND IS NOW THE ONLY ONE: `agent_index`
    returns None when the process table itself cannot be listed, and
    `suite_cap` reads that as the pane cap. That is refusing to answer, which
    is different in kind from answering "no agents here" — the distinction the
    hand-rolled version kept losing.

    (Existence sweep is the lesson: this primitive, its process-class tests
    and its unreadable-environ contract all predated the scan I wrote.
    @codex-3 found it.)
    """
    from . import beacons
    index = beacons.agent_index(proc_dir)
    if index is None:
        return None                  # unlistable process table: UNKNOWN
    return sorted(index.get("by_pid") or ())


def suite_cap(proc_dir=None):
    """This box's whole-suite admission cap. -> int

    FAILS CLOSED ON THE ONE THING THAT CAN GO WRONG. An unlistable process
    table cannot prove the box is paneless, so it takes the pane-host cap; any
    agent pane takes it too. The raised cap is granted only on a census that
    ran and came back empty.

    ENV INHERITANCE IS NO LONGER A CONCERN HERE, AND THE OLD NOTE SAYING IT
    WAS IS THE MECHANISM THIS REPLACED. That note argued a leaked
    HELM_CHAT_NAME would pin a build host at two — conservative but wrong, and
    it was written when this scanned environs itself. Identity now comes from
    `beacons.is_agent`, which reads comm/argv; a child process inherits the
    ENV stamp but is not a claude process, so it is correctly not a pane. That
    is what took this box's count from an inflated 47 to its true ~12.
    """
    panes = _agent_pane_pids(proc_dir)
    if panes is None or panes:
        return SUITE_CAP
    cores = os.cpu_count() or 0
    return max(SUITE_CAP,
               min(BUILD_HOST_SUITE_CAP_MAX, cores // _CORES_PER_SUITE))


# The count is a PROXY: two enormous runs can starve this box exactly as six
# ordinary ones did. The floor is the kernel's own starvation measurement —
# PSI, /proc/pressure/memory `some avg10`, the percentage of the last 10s in
# which at least one task was stalled on memory. The threshold is DERIVED,
# not guessed: the one affirmative pre-death signal this morning's outage
# produced was systemd-journald's PSI watch — journald logged "Under memory
# pressure, flushing caches." at 03:35:29, nine minutes before the daemon
# aborted — and on this box that watch fires at MemoryPressureThresholdUSec
# = 200ms of some-stall per systemd's 2s watch window (measured 2026-08-03
# via `systemctl show systemd-journald -p MemoryPressureThresholdUSec`),
# i.e. a 10% some-stall fraction. avg10 sustains that same fraction over a
# 10s window, so this floor fires strictly later than the watch that still
# left nine minutes of warning. A box already stalling 10% of the time on
# memory reclaim is the box this cap exists for, and admitting another whole
# tree into it is this morning's outage again.

PSI_SOME_FLOOR = 10.0

_SUITE_INTERP = re.compile(r"(?:python|graalpy|pypy)[\d.]*\Z")
# unittest options that consume the NEXT argv slot; their values must not be
# read as test ids when deciding bare-invocation discovery.
_UNITTEST_VALUE_FLAGS = frozenset(("-k", "-s", "-p", "-t",
                                   "--start-directory", "--pattern",
                                   "--top-level-directory"))
# Interpreter options that consume the NEXT argv slot before `-m`.
_PY_VALUE_FLAGS = frozenset(("-W", "-X", "--check-hash-based-pycs"))
_ADMISSIONS = ".gate-admissions.json"
_PSI_SOME = re.compile(r"^some .*\bavg10=(\d+(?:\.\d+)?)", re.M)


def _census_proc_dir(proc_dir=None):
    return proc_dir or home.env("PROC") or "/proc"


def _suite_shaped(argv):
    """Is this argv a WHOLE-TREE unittest discovery run?

    Anchored at the FRONT: the process must BE `<python> -m unittest` with a
    discovery-shaped tail — `discover`, or no test ids at all (bare
    `python3 -m unittest` IS discovery). A targeted module run
    (`python3 -m unittest tests.test_x`) does not count, and neither do the
    gatechild wrapper processes, whose argv carries the suite command in its
    TAIL but starts with a script path — an unanchored substring match would
    count every gate run three times (guard, supervisor, suite)."""
    argv = [str(a) for a in (argv or ())]
    if len(argv) < 2 or not _SUITE_INTERP.fullmatch(os.path.basename(argv[0])):
        return False
    i, module = 1, None
    while i < len(argv):
        arg = argv[i]
        if arg == "-m":
            module = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            break
        if arg.startswith("-m") and len(arg) > 2:
            module, i = arg[2:], i + 1
            break
        if arg in _PY_VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return False    # a script path, not `-m unittest`
    if module != "unittest":
        return False
    positional, j = [], i
    while j < len(argv):
        arg = argv[j]
        if arg in _UNITTEST_VALUE_FLAGS:
            j += 2
            continue
        if arg.startswith("-"):
            j += 1
            continue
        positional.append(arg)
        j += 1
    return not positional or positional[0] == "discover"


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


def suite_census(proc_dir=None):
    """Every process on the box running a whole-tree suite RIGHT NOW.

    -> [{pid, argv, cwd, position}] sorted by pid, or None when the process
    table itself is unreadable — an unreadable census must widen to a
    refusal upstream, never narrow to 'nothing is running'. `position` is
    the gate FIFO position whose cgroup contains the suite (None for a bare
    run outside helm), and is what lets an admission-in-flight and its own
    running suite count as ONE occupant, not two."""
    proc_dir = _census_proc_dir(proc_dir)
    try:
        names = os.listdir(proc_dir)
    except OSError:
        return None
    rows = []
    for name in names:
        if not name.isdigit() or int(name) == os.getpid():
            continue
        pid = int(name)
        try:
            with open(os.path.join(proc_dir, name, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not _suite_shaped(argv):
            continue
        try:
            cwd = os.readlink(os.path.join(proc_dir, name, "cwd"))
        except OSError:
            cwd = "?"
        rows.append({"pid": pid, "argv": argv, "cwd": cwd,
                     "position": _suite_cgroup_position(pid, proc_dir)})
    return sorted(rows, key=lambda row: row["pid"])


def _psi_some_avg10(proc_dir=None):
    """The kernel's memory some-stall percentage over the last 10s, or None.

    Read under the census proc root so a redirected HELM_PROC redirects the
    pressure file with it. None (CONFIG_PSI=n, psi=0, a fake proc tree)
    SKIPS the floor rather than refusing: the census cap still stands, and a
    box without PSI must not lose its gate — but the skip is a weaker guard,
    not an equivalent one."""
    path = os.path.join(_census_proc_dir(proc_dir), "pressure", "memory")
    try:
        with open(path) as f:
            hit = _PSI_SOME.search(f.read())
    except OSError:
        return None
    return float(hit.group(1)) if hit else None


def _admissions_path():
    return os.path.join(chat.chat_dir(), _ADMISSIONS)


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


def _occupant_names(census, pending):
    names = ["pid %d (cwd %s): %s" % (
        row["pid"], row["cwd"], _failure_text(" ".join(row["argv"])))
        for row in census]
    names.extend("@%s admitted at %s (launcher pid %d, suite starting)" % (
        row["holder"], row["ts"], row["pid"]) for row in pending)
    return names


def _admit_suite(position=None, proc_dir=None):
    """Admit one whole-suite run onto the BOX, or say in words why not.

    -> None, or the refusal. Counted under one flock so two launchers cannot
    both read N and both start: an admission that has not yet spawned its
    suite holds its slot as an INTENT row bound to the exact launcher
    process (pid + starttime, the FIFO's own liveness primitive), and a
    dead launcher's intent self-clears. A running gate suite is matched to
    its intent through its helm-gate cgroup, so the pair counts once. Bare
    runs never write intents and are counted purely by census — which is the
    entire point: the slot exists because the PROCESS exists."""
    proc_dir = _census_proc_dir(proc_dir)
    census = suite_census(proc_dir)
    if census is None:
        return "cannot count running suites (%s is unreadable); admission " \
            "refused" % proc_dir
    chat._ensure_dir()
    path = _admissions_path()
    try:
        with seats._flocked(path + ".lock") as lock:
            if lock.f is None:
                return "gate admission lock is unavailable"
            rows = _admissions_load(path)
            live = [row for row in rows
                    if seats._get_live_pid_starttime(
                        row["pid"], proc_dir=proc_dir) == row["starttime"]]
            covered = {row["position"] for row in census if row["position"]}
            pending = [row for row in live if row["position"] not in covered]
            occupants = len(census) + len(pending)
            cap = suite_cap(proc_dir)
            if occupants >= cap:
                if live != rows:
                    pk.write_json(path, {"v": 1, "admissions": live})
                # THIS HOST'S cap, and the message says which kind of host it
                # is: a refusal that quotes a fleet-wide number sends the
                # reader looking for a fleet-wide occupant that does not
                # exist. The count is per-box because /proc is per-box.
                return ("this host's whole-suite cap is %d (%s) and %d %s "
                        "already running — REFUSED so this box does not "
                        "starve again (six concurrent runs killed the PTY "
                        "daemon and all seven panes at 03:44 on 2026-08-03); "
                        "running now: %s. "
                        "Retry when one of those named runs finishes — the "
                        "slot frees itself, nothing to clean up. No override "
                        "exists for this cap: it counts processes, not "
                        "permission." % (
                            cap,
                            "carries agent panes" if cap == SUITE_CAP
                            else "build host, %d cores" % (os.cpu_count() or 0),
                            occupants,
                            "is" if occupants == 1 else "are",
                            "; ".join(_occupant_names(census, pending))))
            stall = _psi_some_avg10(proc_dir)
            if stall is not None and stall >= PSI_SOME_FLOOR:
                if live != rows:
                    pk.write_json(path, {"v": 1, "admissions": live})
                return ("this box is already stalling on memory — PSI "
                        "some avg10 is %.2f%%, at or over the %.0f%% floor "
                        "(the stall fraction systemd-journald's pressure "
                        "watch fired on at 03:35:29, nine minutes before the "
                        "2026-08-03 daemon death) — REFUSED%s. Re-run once "
                        "the pressure clears — avg10 is a 10s window, so "
                        "watch `cat /proc/pressure/memory` fall under the "
                        "floor. No override exists for this floor: it is the "
                        "kernel's own starvation reading."
                        % (stall, PSI_SOME_FLOOR,
                           "; running now: %s" % "; ".join(
                               _occupant_names(census, pending))
                           if census or pending else ""))
            if position is not None:
                live.append({"position": position["id"],
                             "pid": position["pid"],
                             "starttime": position["starttime"],
                             "holder": position["holder"],
                             "ts": pk.now_ts()})
            if live != rows or position is not None:
                pk.write_json(path, {"v": 1, "admissions": live})
    except OSError as exc:
        return "gate admission state write failed: %s" % exc
    return None


def _admission_release(position_id):
    """Drop one admission intent by FIFO position id. -> err or None."""
    path = _admissions_path()
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
        self._thread.join(timeout=max(1, _GATE_LEGACY_RENEW_S * 2))
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
    """Observer-only lifecycle line; queue authority never depends on chat."""
    try:
        chat.post(text, room=_gate_room(repo), who=holder, sign=False,
                  ambient=True)
        return True
    except Exception:
        return False


def _gate_post_orphans(repo, rows, observer):
    for row in rows or ():
        _gate_post(repo, "GATE ORPHAN #%d @%s pid=%d — skipped" % (
            row["seq"], row["holder"], row["pid"]), observer)


def _finish_position(repo, position, status="UNKNOWN", receipt=None,
                     detail=None):
    legacy = position.get("_legacy")
    released, err = False, None
    try:
        ok, nxt, orphaned, err = seats.gate_queue_prepare_finish(
            repo, position["id"])
        if not ok:
            _gate_post_orphans(repo, orphaned, position["holder"])
        else:
            result = "%s gate:%s" % (status, receipt) if receipt else status
            if detail:
                result += " — %s" % _failure_text(detail)
            try:
                _gate_post_orphans(repo, orphaned, position["holder"])
                _gate_post(repo, "GATE FINISH #%d @%s %s%s" % (
                    position["seq"], position["holder"], result,
                    " — NEXT @%s" % nxt["holder"] if nxt else ""),
                    position["holder"])
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
            legacy_err = position["_legacy"].error()
            if legacy_err:
                return "", "", None, \
                    "legacy gate-lock renewal failed: %s; suite result refused" \
                    % legacy_err
            return out or "", errout or "", proc.returncode, None
        except subprocess.TimeoutExpired:
            ok, err = seats.gate_queue_renew(
                repo, position["id"], supervisor_pid)
            legacy_err = position["_legacy"].error()
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


def _queued_process(repo, cmd, position, timeout):
    """Run one admitted suite, renewing its process-bound slot until exit.

    -> (stdout, stderr, rc, err). Queue wait happens before this
    function, so `timeout` measures the child run and never punishes a waiter.
    """
    wrapper = os.path.abspath(gatechild.__file__)
    cgroup = gatechild._cgroup_path(position["id"])
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
              "--parent-start", str(position["starttime"]),
              "--position", position["id"],
              "--ready-fd", str(ready_r),
              "--report-fd", str(report_w), "--"] + cmd
    try:
        proc = subprocess.Popen(launch, cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env=_suite_env(), start_new_session=True,
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
             str(position["starttime"]), str(proc.pid), str(guard_start),
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
            _kill_group(proc)
            return cleaned(("", "", None, err))
        ok, err = seats.gate_queue_bind_child(
            repo, position["id"], supervisor[0])
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
    holder = seats.acting_seat(session, repo)
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
                repo, position["id"])
            _gate_post_orphans(repo, orphaned, holder)


INFLIGHT = ".helm-gate-in-flight"


def inflight_path(repo):
    """The in-room marker naming THIS room's running gate, or None.

    RESOLVES THE REAL ADMIN DIR AND DOES NOT ASSUME `<repo>/.git` IS ONE.
    In a LANE WORKTREE — which is every room this guard exists for — `.git` is
    a FILE holding `gitdir: ...`, so writing `<repo>/.git/<name>` raises
    NotADirectoryError. Caught live 2026-08-03 by DOGFOODING this guard against
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
    codex found the mirror image: if the INCUMBENT finishes FIRST it removes
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
    still running (codex review). Neither is possible here: this
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
    never saw (task/112 read 4, reproduced by @codex-2 with a PermissionError
    on listdir: findings=[] unknowns=[]). That was the THIRD instance of one
    class found in one night by three reviewers — `seat_homes.walk` swallowed
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
    records the HOLDER and never the REPO — measured 2026-08-03, the claim
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
            with open(os.path.join(d, name), encoding="utf-8") as fh:
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
            with open(legacy, encoding="utf-8") as fh:
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


def _mint_result(repo, head, tree, dirty, ident, cmd, suite, label, rc,
                 started, out, legacy=None):
    """Bracket, parse, reconcile and append one completed child result."""
    after_head, after_tree, after_dirty, after_err = tree_state(repo)
    parsed = parse_result(out)
    row = {"v": 4, "event": "gate", "ts": pk.now_ts(),
           "repo_id": repo, "head": head, "tree": tree, "dirty": dirty,
           "head_after": after_head, "tree_after": after_tree,
           "dirty_after": True if after_err else after_dirty,
           "interpreter": ident, "host": host(), "argv": cmd, "suite": suite,
           "label": (label or "").strip()[:120] or None,
           "rc": rc, "wall": round(time.time() - started, 3)}
    row.update(status=parsed["status"], ran=parsed["ran"],
               skipped=parsed["skipped"], detail=parsed["detail"],
               elapsed=parsed["elapsed"], failures=parsed["failures"],
               failures_unreadable=parsed["failures_unreadable"])
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
    # bind() never reads it — but it is IN the receipt, because the 2026-07-31
    # stale-base investigations each re-derived it from data the gate already
    # had (see the stale-base block above).
    row["base_check"] = _base_check(repo, row["failures"],
                                    row["failures_unreadable"]) \
        if suite and row["status"] == "FAILED" else None
    row["id"] = _receipt_id(row)
    append = lambda: eventledger.append(receipts_path(), row)
    if legacy:
        minted, err = legacy.finalize(append)
        return row, bool(minted), err
    return row, eventledger.append(receipts_path(), row), None


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

    THE EFFECTIVE REPO, NEVER THE FLAG TOKEN (@codex-2, land-request row
    6d41adc116e3, finding 1 — reproduced exactly as written). THE ROW ID AND
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


def run(repo=None, argv=None, label=None, timeout=None):
    """Run the gate and MINT its receipt. -> (row, err).

    Whole-suite runs first take one process-owned repository FIFO position.
    Custom diagnostic commands remain outside the expensive-suite queue and,
    as before, cannot bind a verdict. The receipt is written before this
    returns, so a claim can never exist in a reviewer's terminal without
    existing on disk."""
    repo = os.path.realpath(repo or os.getcwd())
    cross_err = _cross_tree_refusal(repo)
    if cross_err:
        return None, cross_err
    _warn_trunk_deletions(repo)
    suite = not argv
    ident = interpreter() if suite else None
    cmd = [sys.executable] + list(SUITE) if suite else list(argv)
    position = None
    if suite:
        position, err = _acquire_gate(repo)
        if err:
            return None, err
        # ADMISSION IS COUNTED OVER PROCESSES, NOT LOCK-HOLDERS, and it is
        # checked here — after the FIFO grants the head, before anything
        # spawns — so a refused run releases its position and starts nothing.
        admit_err = _admit_suite(position)
        if admit_err:
            _ok, finish_err = _finish_position(repo, position, "REFUSED")
            return None, admit_err if not finish_err else "%s; %s" % (
                admit_err, finish_err)
    elif _suite_shaped(cmd):
        # A custom command that IS a whole-tree discovery run occupies the
        # same memory as the queued kind and must not be helm's own bypass.
        # No FIFO position, so no intent row: census-only.
        admit_err = _admit_suite()
        if admit_err:
            return None, admit_err
    # THE MARKER OPENS WITH THE BRACKET AND CLOSES WITH IT. A commit inside
    # this window moves HEAD under a run whose receipt already recorded the
    # OLD head, so the receipt describes no single tree and bind() refuses it.
    # Three seats hit that in one hour on 2026-08-03; the bracket rule catches
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
        # @codex reproduced the gap: a suite child that modifies a tracked file
        # leaves the worktree dirty AFTERWARD, while a receipt taken only before it
        # says clean — so the receipt did not prove execution against the immutable
        # tree it names. Same bracket this lane's sibling uses around a ProcIdent:
        # one read cannot witness a change that happens during the thing it is
        # describing.
        if suite:
            try:
                stdout, stderr, rc, run_err = _queued_process(
                    repo, cmd, position, timeout)
            except BaseException:
                _finish_position(repo, position)
                raise
            if run_err:
                _ok, finish_err = _finish_position(
                    repo, position, detail=run_err)
                return None, run_err if not finish_err else "%s; %s" % (
                    run_err, finish_err)
            # unittest's protocol is stderr. stdout belongs to the tests and can
            # contain arbitrary `ERROR:` / `FAILED` prose; admitting it let logs
            # forge identities and even evict the real failure from the cap.
            out = stderr
        else:
            try:
                p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                                   timeout=timeout)
                out = (p.stdout or "") + (p.stderr or "")
                rc = p.returncode
            except subprocess.TimeoutExpired as exc:
                # Custom diagnostic commands retain their historical timeout shape.
                out = _decode(exc.stdout) + _decode(exc.stderr)
                rc = None
            except Exception as exc:
                return None, "gate command did not run: %s: %s" % (
                    type(exc).__name__, exc)
        try:
            row, minted, mint_err = _mint_result(
                repo, head, tree, dirty, ident, cmd, suite, label, rc, started, out,
                position.get("_legacy") if position else None)
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



def _decode(blob):
    if blob is None:
        return ""
    return blob if isinstance(blob, str) else blob.decode("utf-8", "replace")


def receipts():
    """Every minted receipt whose id MATCHES ITS OWN CONTENT.

    -> (rows, unavailable, skipped). `skipped` counts rows this function could
    not judge — see the total-by-construction note below and `gate list`, which
    reports it.

    The id is a hash of every field a reader relies on, and until @codex tried
    it, nothing ever recomputed it: `eventledger` strict mode validates that a
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
    out, skipped = [], 0
    for row in rows:
        # TOTAL BY CONSTRUCTION. Any row this loop cannot evaluate is a row the
        # ledger does not contain, never an exception in a caller's frame: one
        # malformed line must not be able to hide every honest receipt behind
        # it. The narrow `except` that only knew today's malformed shape is
        # exactly how the next shape gets through.
        try:
            rid = str(row.get("id") or "")
            ok = bool(_ID.fullmatch(rid)) and rid == _receipt_id(row)
        except Exception:                       # noqa: BLE001 — see above
            ok = False
        if ok:
            out.append(row)
        else:
            skipped += 1
    return out, None, skipped


def _stored_but_unverifiable(prefix):
    """A row whose STORED id matches `prefix` but which `receipts()` dropped.
    -> (row, computed) where `computed` is None when no id could be derived.

    TOTAL OVER THE TWO WAYS A ROW GETS DROPPED, because a partial answer here
    reproduces the very conflation this exists to end. `receipts()` skips a row
    if its id DISAGREES or if computing one RAISES (a non-string in argv, a
    malformed interpreter). Reporting only the first would leave the malformed
    shape still answering "no minted gate receipt" — present, unjudgeable, and
    described as absent.

    Paid only when the honest answer would otherwise be "not found" AND the
    integrity filter actually dropped something, so the happy path reads the
    ledger exactly once as before."""
    rows, unavailable = eventledger.checked_events(receipts_path(), strict=True)
    if unavailable:
        return None, None
    for row in rows:
        try:
            rid = str(row.get("id") or "")
        except Exception:                       # noqa: BLE001
            continue
        if not rid.startswith(prefix):
            continue
        try:
            computed = _receipt_id(row)
        except Exception:                       # noqa: BLE001 — uncomputable
            return row, None                    # is still PRESENT
        if computed != rid:
            return row, computed
    return None, None


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
    cannot read, forever. Measured 2026-08-04 on receipt 756b936006bf0e47 — 685
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
        stored, computed = (_stored_but_unverifiable(prefix) if skipped
                            else (None, None))
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
            if version in gateimport.KNOWN_VERSIONS:
                cause = ("This helm KNOWS version %r, so the row was CHANGED "
                         "after it was minted — investigate the ledger, do not "
                         "paper over it by re-running" % (version,))
            else:
                cause = ("Its version field says %r, which this helm does not "
                         "know: it was minted by a NEWER helm. Re-running the "
                         "gate is the WRONG move — it mints another receipt "
                         "this helm cannot read either. Update helm, then "
                         "re-resolve" % (version,))
            return None, (
                "receipt %s IS in the ledger and its content id does NOT "
                "recompute, so nothing may bind to it: the row says %s and "
                "this helm computes %s. %s"
                % (prefix, str(stored.get("id"))[:16], _receipt_id(stored),
                   cause))
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


def bind(evidence, reviewed_tip, repo_id=None, reviewed_ts=None):
    """Does this evidence prove a clean whole-suite run containing this tip?

    Exact-tip receipts retain their historical binding. A later receipt may
    also bind when the standing dispatch repository proves the reviewed tip is
    its ancestor and the receipt postdates that dispatch. UNKNOWN never binds.
    """
    tip = str(reviewed_tip or "").strip().lower()
    tok = token(evidence)
    if not tok:
        return "UNVERIFIED", None, "evidence carries no minted gate receipt"
    row, err = by_id(tok)
    if err:
        return "REFUSED", None, err
    if not _SHA.fullmatch(tip):
        return "REFUSED", row["id"], "reviewed tip is not a full commit id"
    if row.get("dirty") or row.get("dirty_after"):
        return "REFUSED", row["id"], (
            "receipt %s ran on a DIRTY worktree (%s) — it proves a tree nobody "
            "else can check out"
            % (row["id"], "before" if row.get("dirty") else "after the run"))
    # THE BRACKET. A receipt whose tree MOVED under the run describes no single
    # tree, so it can name a commit but never bind one. `_after` is absent on a
    # pre-bracket receipt and its absence REFUSES rather than defaults.
    for half in ("head_after", "tree_after"):
        if not row.get(half):
            return "REFUSED", row["id"], (
                "receipt %s has no post-run tree read — it cannot show the "
                "worktree held still, so it proves nothing about %s"
                % (row["id"], tip[:12]))
    if row.get("head_after") != row.get("head") \
            or row.get("tree_after") != row.get("tree"):
        return "REFUSED", row["id"], (
            "receipt %s: the worktree MOVED during the run (%s -> %s)"
            % (row["id"], str(row.get("head"))[:12],
               str(row.get("head_after"))[:12]))
    if row.get("rc") not in (0, None) and row.get("status") == "OK":
        return "REFUSED", row["id"], (
            "receipt %s says OK and the runner exited %s"
            % (row["id"], row.get("rc")))
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
    # nobody can audit: on 2026-08-03 a fab-minted red was honest about the
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
    if head == tip:
        return "VERIFIED", row["id"], "%s on %s@%s" % (
            interpreter_label(_ident_of(row)), tip[:12],
            host_label(_host_of(row)))
    if not _SHA.fullmatch(head):
        return "REFUSED", row["id"], "receipt head is not a full commit id"
    opened, minted = _binding_ts(reviewed_ts), _binding_ts(row.get("ts"))
    if opened is None:
        return "REFUSED", row["id"], (
            "standing review has no canonical opening timestamp — a later "
            "receipt cannot prove it postdates the review")
    if minted is None:
        return "REFUSED", row["id"], (
            "receipt %s has no canonical timestamp — its order after the "
            "review is UNKNOWN" % row["id"])
    if minted <= opened:
        return "REFUSED", row["id"], (
            "receipt %s at %s does not postdate the review at %s"
            % (row["id"], row.get("ts"), reviewed_ts))
    repo = str(repo_id or "")
    if not os.path.isabs(repo) or os.path.realpath(repo) != repo \
            or not os.path.isdir(repo):
        return "REFUSED", row["id"], (
            "standing dispatch repository is unreadable — descendant ancestry "
            "is UNKNOWN")
    relation = vcs.backend(repo).ancestry(repo, tip, head)
    if relation != vcs.ANCESTOR:
        return "REFUSED", row["id"], (
            "receipt %s ran on %s, which %s the reviewed tip %s in the standing "
            "repository" % (row["id"], head[:12],
                             "does not contain" if relation == vcs.NOT_ANCESTOR
                             else "cannot be proven to contain", tip[:12]))
    return "VERIFIED", row["id"], "%s on %s containing reviewed %s" % (
        interpreter_label(_ident_of(row)), head[:12], tip[:12])


# ---------------------------------------------------------------- CLI

USAGE = ("usage: helm gate run [--label TEXT] [--timeout S] [--repo PATH] "
         "[--box NAME] [--json] [-- <argv>...]\n"
         "       helm gate show <id> [--json]\n"
         "       helm gate list [--limit N] [--json]\n"
         "       helm gate import <artifact.jsonl> [--repo PATH] "
         "[--id RECEIPT-ID]")


def _fmt(row):
    when = _failure_text(row.get("ts"))[:19]
    flag = " DIRTY" if row.get("dirty") else ""
    return "%s  %s  %-7s %s%s" % (
        _failure_text(row.get("id")) or "?", when,
        _failure_text(row.get("status")) or "UNKNOWN",
        evidence_line(row), flag)


def _show_base_check(row):
    """One verdict line, only for receipts that carry the field at all."""
    check = row.get("base_check")
    # v4 keeps the field, so a v4 receipt must still SHOW its base check. An
    # `== 3` here would silently blank the line for every future receipt —
    # the display-side twin of the id's growing-set rule above.
    if row.get("v") in (3, 4) and check is not None:
        if not isinstance(check, dict):
            print("  %-10s UNREADABLE (stored base check is not a mapping)"
                  % "base")
            return
        verdict = (_failure_text(check.get("verdict")) or "?").replace(
            "_", " ")
        print("  %-10s %s — %s" % ("base", verdict,
                                   _failure_text(check.get("reason")) or "?"))


def _show_failures(row):
    """Render stored diagnostics without making legacy absence read as none."""
    # v4 BINDS the failure identities exactly as v2 and v3 do — `not in (2, 3)`
    # sent every v4 receipt down the legacy branch and printed "predates
    # failure identities" about a receipt that carries them. THIRD instance of
    # this shape in this file: the id's two version tuples, _show_base_check,
    # and now this one. The reader-first split is the only reason it was caught
    # before shipping — no v4 receipt existed to render until the writer
    # flipped. test_every_version_gated_reader_admits_the_minted_version now
    # holds the whole class, so a v5 bump cannot repeat it three more times.
    if row.get("v") not in (2, 3, 4):
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
    state = "UNREADABLE" if row.get("failures_unreadable") else \
        ("none" if not failures else "%d recorded" % sum(
            isinstance(item, dict) and "test" in item for item in failures))
    print("  %-10s %s" % ("failures", state))
    for item in failures:
        if not isinstance(item, dict):
            print("    <unreadable entry>")
        elif "truncated" in item:
            truncated = _failure_text(item["truncated"]) or "?"
            print("    ... %s more failure%s (truncated)" % (
                truncated, "" if item["truncated"] == 1 else "s"))
        else:
            print("    %s %s — %s" % (
                _failure_text(item.get("kind")) or "FAILURE",
                _failure_text(item.get("test")) or "<unknown>",
                _failure_text(item.get("traceback")) or
                "<traceback unreadable>"))


def _exit_code_will_be_discarded():
    """Is stdout a PIPE — i.e. will this process's exit code be thrown away?

    A shell pipeline takes the status of its LAST stage, so `helm gate run |
    tail` reports tail's success no matter what the gate did. The caller reads
    0 and believes the gate passed. MEASURED 2026-08-04: two seats hit this
    independently and repeatedly in one night, every time with the premise
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
    if sub == "show":
        return _cmd_show(rest)
    if sub == "list":
        return _cmd_list(rest)
    if sub == "import":
        from . import gateimport
        return gateimport.cmd_import(rest)
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
    rc = guard_tail("helm gate run", opts, flags=("--json",),
                    valued=("--label", "--timeout", "--repo", "--box"),
                    usage=USAGE)
    if rc is not None:
        return rc
    argv = rest[cut + 1:] if cut < len(rest) else None
    if cut < len(rest) and not argv:
        print("helm gate run: `--` needs a command after it", file=sys.stderr)
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
        # The job-routing arm (#225): the WHOLE-SUITE gate runs on a consented
        # inventory box and its receipt rides home through `gate import`. A
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
                                   timeout=timeout, as_json=as_json)
    row, err = run(repo=_opt(opts, "--repo"), argv=argv,
                   label=_opt(opts, "--label"), timeout=timeout)
    if err:
        if as_json:
            print(json.dumps({"minted": False, "reason": err}, indent=1))
            return 1
        print("helm gate: " + err, file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"minted": True, "receipt": row,
                          "evidence": evidence_line(row)},
                         ensure_ascii=False, indent=1))
    else:
        print(evidence_line(row))
        state, _rid, why = bind(evidence_line(row), row.get("head") or "")
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
    # The exit status is `bind` itself, not a second opinion about it: a run
    # this verb calls green while the binding would refuse it is exactly the
    # gap the verb exists to close.
    return 0 if bind(evidence_line(row), row.get("head") or "")[0] == "VERIFIED" else 1


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
        print(json.dumps(row, ensure_ascii=False, indent=1))
        return 0
    for key in ("id", "ts", "status", "ran", "skipped", "detail", "head",
                "tree", "dirty", "repo_id", "label", "rc", "wall", "elapsed"):
        if row.get(key) is not None:
            print("  %-10s %s" % (key, _failure_text(row[key])))
    _show_failures(row)
    _show_base_check(row)
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
    rows, unavailable, skipped = receipts()
    if unavailable:
        print("helm gate: receipt ledger unavailable: %s" % unavailable,
              file=sys.stderr)
        return 1
    if skipped:
        # Dropping a row silently is how a ledger reports zero and gets
        # believed. The count is small and the surface is the right place.
        print("helm gate: %d ledger row%s did not match %s own content and "
              "were skipped" % (skipped, "" if skipped == 1 else "s",
                                "its" if skipped == 1 else "their"),
              file=sys.stderr)
    rows = rows[-limit:] if limit > 0 else []
    if "--json" in rest:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("helm gate: no minted receipts yet — `helm gate run`")
        return 0
    for row in rows:
        print(_fmt(row))
    return 0
